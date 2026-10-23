"""The dataset catalog the /ui/v1 routes serve: filtering, keyset paging, facets and freshness (tj-grna9p.20).

This module composes the read repository (database/crud/stock/dataset_catalog.py) with the pure health and
completeness rules (health.py, completeness.py). The routes call it and map its results to messages; it knows no HTTP and no protobuf.

THE QUERY BUDGET. A request runs a constant number of statements, never one per dataset (tj-grna9p.8 item 2):
the matching entries with their newest bar in one, and then, for the datasets the answer needs, one each for
bar extents, covered session dates (STATIC datasets only, one per calendar time zone) and siblings.

FILTERS THAT NEED THE COMPUTED FRESHNESS ARE APPLIED OVER ALL MATCHING ROWS, not over the page window (the .8
item 4 option "computed server-side over all rows", with the measured ceiling of 1000 datasets in the task's
acceptance). That is what lets a facet count equal the filtered list's total. A request with no status filter
evaluates freshness for the page only.

THE STATUS VOCABULARY. The `status` filter speaks the UI's words (tj-grna9p.60): FRESH and COMPLETE are Healthy,
LATE is Late, OVERDUE and GAPS are Failed, RETIRED is Retired. needs_attention is Late or Failed. A dataset whose
source has no trading calendar has no computed freshness (status UNSPECIFIED on the wire) and matches no status.

THE COUNTS, defined so each equals a list total. Given the filters of a request:
    all              the total with every filter except needs_attention (the All Datasets view)
    needs_attention  the total with every filter, needs_attention forced on (the Needs Attention view)
    each source      the total with every filter except source, and that source
    each update type the total with every filter except update_type, and that update type
    each status      the total with every filter except status, and that status (the raw freshness status)

THE USAGE FILTERS (tj-grna9p.85). The client filter picks whose reads count: client_id (repeatable, wins) else
client_kind (repeatable), with neither the kind SDK. Under it:
    READ_30D   read within 30 days: the UTC date of the last read by them is on or after today's UTC date minus 30
    STALE      NOT read within stale_days (the same day-aligned rule), never-read included
    NEVER_READ no read by them at all
One clock decides all three: whole UTC calendar days, 0 = today, as the last-read buckets, from the last-read
time over the selected clients (a stream session counts as a read); so READ_30D and STALE are exact opposites at
stale_days = 30. DatasetUsage.reads_30d still counts ordinary reads only.
Expiring Soon is expiry < expires_before PLUS every dataset in the reaper's pending set, and ignores the client
filter. Each usage count of a facets read omits the usage filter itself and equals the list's total with it set;
expiring_soon omits the expires_before filter. The usage of the datasets is read in one statement for every
dataset when a filter or the sort needs all of them, else for the page's, so the count stays constant.

THE CURSOR is opaque to clients: URL-safe base64 of a small JSON document holding the sort and the last item's
sort key. It is bound to the sort it was made under, and anything that does not decode to exactly that shape is a
422, never a 500 and never echoed.
"""

import base64
import binascii
import hashlib
import json
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import InvalidRequestError, Reason
from common.logging import get_logger
from data.store.app.completeness import CoverageBucket, gap_intervals
from data.store.app.completeness import coverage_buckets as compute_coverage_buckets
from data.store.app.database.crud.dataset_read import (
    ClientDatasets,
    ClientDayReads,
    DatasetUsage,
    read_client_datasets,
    read_client_day_reads,
    read_client_last_reads,
    read_dataset_usage,
    read_readers,
    usage_tables_exist,
)
from data.store.app.database.crud.ingest_run import (
    IngestRunRecord,
    latest_runs_for_datasets,
    list_runs_for_dataset,
    running_dataset_ids,
)
from data.store.app.database.crud.stock import dataset_catalog as repo
from data.store.app.database.crud.stock.dataset_catalog import (
    BarExtent,
    BarPageOffset,
    BarStorage,
    CatalogFilter,
    CatalogRow,
)
from data.store.app.database.models.dataset_read import ReadClientKind
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.health import DatasetHealth, FreshnessConfig, FreshnessStatus, evaluate_dataset
from data.store.app.trading_calendar import (
    CalendarRangeError,
    TradingCalendar,
    UnknownCalendarError,
    calendar_for_source,
    local_midnight,
)


log = get_logger(__name__)

DEFAULT_PAGE_LIMIT: Final = 100
MAX_PAGE_LIMIT: Final = 500
DEFAULT_RUN_LIMIT: Final = 50
MAX_RUN_LIMIT: Final = 500
DEFAULT_BAR_LIMIT: Final = 1000
# The most bars one /bars read returns. About 220 B of JSON per bar: 50,000 bars is about 10 MB uncompressed and
# about 1 MB gzipped, so a million bars is 20 requests. The UI keeps its own 1,000-bar pages (DEFAULT_BAR_LIMIT);
# the cap is for programmatic readers and the CSV export, which walks the cursor at this size (tj-grna9p.125).
MAX_BAR_LIMIT: Final = 50_000
DEFAULT_PAGE_INDEX_SIZE: Final = 1000
MAX_PAGE_INDEX_SIZE: Final = 1000
DEFAULT_PAGE_INDEX_OVERLAP: Final = 50
DEFAULT_COVERAGE_BUCKETS: Final = 120
MAX_COVERAGE_BUCKETS: Final = 400
MAX_COVERAGE_GAPS: Final = 500
# The usage window of reads_30d, read_by_30d and the client picker's counts.
USAGE_WINDOW_DAYS: Final = 30
DEFAULT_STALE_DAYS: Final = 90
MAX_STALE_DAYS: Final = 3650
# Expiring Soon with no expires_before sent: expiry before now plus this.
EXPIRING_SOON_DAYS: Final = 7

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_MICROSECOND: Final = timedelta(microseconds=1)


class StatusGroup(StrEnum):
    """The status words the UI filters by."""

    HEALTHY = 'healthy'
    LATE = 'late'
    FAILED = 'failed'
    RETIRED = 'retired'


class DatasetSort(StrEnum):
    """The catalog's orders. Both end in (symbol, id), so the order is total and a cursor is exact."""

    SYMBOL = 'symbol'
    # Soonest expiry first, datasets with no expiry last.
    EXPIRES = 'expires'
    # Most recently read first (by the selected clients), never-read datasets last.
    LAST_READ = 'last_read'


class UsageFilter(StrEnum):
    """The list's usage filter, by the member names of ui/v1 UsageFilter (without the USAGE_FILTER_ prefix)."""

    READ_30D = 'read_30d'
    STALE = 'stale'
    NEVER_READ = 'never_read'


_GROUP_OF: Final[dict[FreshnessStatus, StatusGroup]] = {
    FreshnessStatus.FRESH: StatusGroup.HEALTHY,
    FreshnessStatus.COMPLETE: StatusGroup.HEALTHY,
    FreshnessStatus.LATE: StatusGroup.LATE,
    FreshnessStatus.OVERDUE: StatusGroup.FAILED,
    FreshnessStatus.GAPS: StatusGroup.FAILED,
    FreshnessStatus.RETIRED: StatusGroup.RETIRED,
}
_ATTENTION: Final = frozenset({StatusGroup.LATE, StatusGroup.FAILED})


@dataclass(frozen=True)
class ClientFilter:
    """Whose reads count. ids win over kinds; with neither the kind is SDK.

    Attributes:
        client_ids: Exact client ids (labels, never logged). Empty means filter by kind.
        client_kinds: The kinds, used when no id is given. Empty means the default, SDK.
    """

    client_ids: tuple[str, ...] = field(default=(), repr=False)
    client_kinds: tuple[ReadClientKind, ...] = ()

    @property
    def applied_ids(self) -> tuple[str, ...]:
        """The ids applied, in the order sent without repeats; empty when the kind filter applies."""
        return tuple(dict.fromkeys(self.client_ids))

    @property
    def applied_kinds(self) -> tuple[ReadClientKind, ...]:
        """The kinds applied, EMPTY when ids were sent (the echo rule); [SDK] by default."""
        if self.client_ids:
            return ()
        kinds = set(self.client_kinds) or {ReadClientKind.SDK}
        return tuple(kind for kind in ReadClientKind if kind in kinds)


@dataclass(frozen=True)
class DatasetUsageView:
    """How one dataset was read, as DatasetSummary.usage carries it.

    Attributes:
        last_read_at: The latest read by the selected clients, or None when they never read it.
        reads_30d: Reads by the selected clients in the last 30 days.
        read_by_30d: The kinds that read it in the last 30 days, whatever the filter.
        read_by_clients_30d: The client ids that did (sorted), whatever the filter. Labels, never logged.
    """

    last_read_at: datetime | None = None
    reads_30d: int = 0
    read_by_30d: tuple[ReadClientKind, ...] = ()
    read_by_clients_30d: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class ClientCountView:
    """A client seen reading, for the picker. client_id is repr=False."""

    client_id: str = field(repr=False)
    client_kind: ReadClientKind
    datasets: int


@dataclass(frozen=True)
class CatalogQuery:
    """What a list or facets request asks for.

    Attributes:
        catalog_filter: The SQL-level filters (symbol prefix, source, update type).
        status: Keep datasets whose computed freshness is in this group.
        needs_attention: Keep only Late and Failed datasets.
        running: Keep only datasets with a run in state RUNNING. Orthogonal to status: a dataset can be Running
            and Late at once, and how the two rank in one column is the UI's choice.
        client_filter: Whose reads the usage figures and the usage filter count.
        usage: Keep datasets in this usage group under the client filter.
        stale_days: The staleness threshold of the STALE group, 1..MAX_STALE_DAYS.
        expires_before: The Expiring Soon filter: keep datasets whose expiry is before this or that the reaper
            would delete. None applies no such filter (a facets read then counts against now + 7 days).
        sort: The order of the list.
        cursor: The opaque cursor of the previous page, or None for the first.
        limit: The page size.
    """

    catalog_filter: CatalogFilter = field(default_factory=CatalogFilter)
    status: StatusGroup | None = None
    needs_attention: bool = False
    running: bool = False
    client_filter: ClientFilter = field(default_factory=ClientFilter)
    usage: UsageFilter | None = None
    stale_days: int = DEFAULT_STALE_DAYS
    expires_before: datetime | None = None
    sort: DatasetSort = DatasetSort.SYMBOL
    cursor: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT


@dataclass(frozen=True)
class DatasetView:
    """One catalog entry with its computed freshness.

    Attributes:
        row: The entry.
        health: The freshness judgement, or None when the source has no trading calendar.
    """

    row: CatalogRow
    health: DatasetHealth | None

    @property
    def group(self) -> StatusGroup | None:
        """The UI status group, or None when there is no computed freshness."""
        return None if self.health is None else _GROUP_OF[self.health.status]


@dataclass(frozen=True)
class DatasetPage:
    """One page of the catalog.

    Attributes:
        views: The datasets, in the requested order.
        extents: The stored extent of each dataset on the page.
        siblings: The other granularities of each dataset on the page.
        next_cursor: The cursor for the next page; empty on the last.
        as_of: The instant freshness was computed at.
        last_runs: The newest run of each dataset on the page that has one, whatever its state.
        usage: The usage of each dataset on the page (an unread one has the zero view).
        reaper_pending: The page's datasets the reaper would delete.
        client_filter: The client filter the usage figures were computed under.
    """

    views: list[DatasetView]
    extents: dict[uuid.UUID, BarExtent]
    siblings: dict[uuid.UUID, list[tuple[uuid.UUID, Granularity]]]
    next_cursor: str
    as_of: datetime
    last_runs: dict[uuid.UUID, IngestRunRecord] = field(default_factory=dict)
    usage: dict[uuid.UUID, DatasetUsageView] = field(default_factory=dict)
    reaper_pending: frozenset[uuid.UUID] = frozenset()
    client_filter: ClientFilter = field(default_factory=ClientFilter)


@dataclass(frozen=True)
class Facets:
    """The sidebar counts, see the module docstring for what each equals.

    Attributes:
        all: The All Datasets view's total.
        needs_attention: The Needs Attention view's total.
        sources: A count for every source.
        update_types: A count for every update type.
        statuses: A count for every computed freshness status.
        running: Datasets with a RUNNING run, under every filter except running itself.
        clients: Each client seen reading in the last 30 days; ignores the client filter.
        client_filter: The client filter applied.
        stale: The list's total with usage=STALE, under the other filters.
        read_30d: The list's total with usage=READ_30D.
        never_read: The list's total with usage=NEVER_READ.
        expiring_soon: The list's total with expires_before (the default when none was sent), under the others.
        stale_days: The threshold used.
        expires_before: The bound used.
    """

    all: int
    needs_attention: int
    sources: dict[DataSource, int]
    update_types: dict[UpdateType, int]
    statuses: dict[FreshnessStatus, int]
    running: int = 0
    clients: list[ClientCountView] = field(default_factory=list)
    client_filter: ClientFilter = field(default_factory=ClientFilter)
    stale: int = 0
    read_30d: int = 0
    never_read: int = 0
    expiring_soon: int = 0
    stale_days: int = DEFAULT_STALE_DAYS
    expires_before: datetime | None = None


@dataclass(frozen=True)
class DatasetDetail:
    """One dataset as the viewer opens it.

    Attributes:
        view: The entry with its freshness.
        extent: What is stored.
        siblings: The other granularities of the series.
        as_of: The instant freshness was computed at.
        last_run: The dataset's newest run, whatever its state, or None when it has never run.
        usage: How it was read, under the default client filter.
        reaper_pending: Whether the reaper would delete it.
        client_filter: The client filter the usage was computed under.
    """

    view: DatasetView
    extent: BarExtent
    siblings: list[tuple[uuid.UUID, Granularity]]
    as_of: datetime
    last_run: IngestRunRecord | None = None
    usage: DatasetUsageView = field(default_factory=DatasetUsageView)
    reaper_pending: bool = False
    client_filter: ClientFilter = field(default_factory=ClientFilter)


@dataclass(frozen=True)
class RunPageResult:
    """One page of a dataset's runs.

    Attributes:
        runs: The runs, newest first.
        next_cursor: The cursor for the next page; empty on the last.
    """

    runs: list[IngestRunRecord]
    next_cursor: str


@dataclass(frozen=True)
class BarPageResult:
    """One page of a dataset's bars.

    Attributes:
        bars: The bar rows, ascending by timestamp.
        next_cursor: The cursor for the next page; empty on the last.
    """

    bars: list[StockMarketActivity]
    next_cursor: str


@dataclass(frozen=True)
class BarPageIndexResult:
    """The page index of a bar window.

    Attributes:
        bar_count: How many bars the half-open window holds.
        page_size: The page width the index was cut with.
        overlap: The look-ahead the client adds from the next block, as the index was asked for.
        pages: Where each page starts, ascending by offset; empty when bar_count is 0.
    """

    bar_count: int
    page_size: int
    overlap: int
    pages: list[BarPageOffset]


@dataclass(frozen=True)
class CoverageGapWindow:
    """A gap as the strip lists it: the half-open window that holds exactly the missing sessions' bars.

    Attributes:
        start: The local midnight of the gap's first session.
        end: The local midnight of the day after the gap's last session.
        sessions: How many sessions are missing.
    """

    start: datetime
    end: datetime
    sessions: int


@dataclass(frozen=True)
class CoverageResult:
    """A dataset's coverage over its range.

    Attributes:
        start: The range start, half-open.
        end: The range end, as DatasetSummary.end reads it.
        buckets: The strip's buckets, tiling [start, end); empty when the range holds no session.
        gaps: The first MAX_COVERAGE_GAPS gaps, ascending.
        gap_count: The number of gaps, which may exceed len(gaps).
        as_of: The instant coverage was computed at.
    """

    start: datetime
    end: datetime
    buckets: list[CoverageBucket]
    gaps: list[CoverageGapWindow]
    gap_count: int
    as_of: datetime


def invalid_cursor() -> InvalidRequestError:
    """The refusal for a cursor that does not decode. It names nothing of the cursor, which a client chose."""
    return InvalidRequestError(Reason.INVALID_REQUEST, 'The cursor is not valid; start again from the first page.')


def _encode_cursor(payload: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + '=' * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode('ascii')))
    except (binascii.Error, ValueError, UnicodeError):
        raise invalid_cursor() from None
    if not isinstance(payload, dict):
        raise invalid_cursor()
    return payload


def is_retired(row: CatalogRow) -> bool:
    """Whether the dataset is retired: an expiry on a DAILY or STREAM subscription means it stopped collecting."""
    return row.expiry is not None and row.update_type in (UpdateType.DAILY, UpdateType.STREAM)


def effective_end(row: CatalogRow, now: datetime) -> datetime:
    """The end of the dataset's range as the UI sees it: the declared end, or `now` for an open-ended request.

    The proto promises an end that is always set and after the start, so an open end reads as the present, and as
    one microsecond past the start for a dataset that starts in the future.
    """
    if row.end is not None:
        return row.end
    return max(now, row.start + _ONE_MICROSECOND)


async def evaluate_views(
    db: AsyncSession, rows: list[CatalogRow], now: datetime, config: FreshnessConfig | None = None
) -> list[DatasetView]:
    """Compute freshness for each row, one query per calendar time zone for the STATIC ones, none otherwise.

    Args:
        db: The request's session.
        rows: The entries to judge.
        now: The instant to judge at; aware.
        config: The freshness settings; the environment's when omitted.

    Returns:
        list[DatasetView]: One view per row, in order. A row whose source has no calendar, or whose range starts
        before the calendar does (CalendarRangeError, logged at WARNING), has no health, unless it is retired,
        which needs none.
    """
    config = config or FreshnessConfig.from_env()
    calendars: dict[uuid.UUID, TradingCalendar | None] = {}
    static_by_zone: dict[str, list[CatalogRow]] = {}
    for row in rows:
        try:
            calendar = calendar_for_source(row.source)
        except UnknownCalendarError:
            calendars[row.id] = None
            continue
        calendars[row.id] = calendar
        if row.update_type is UpdateType.STATIC:
            static_by_zone.setdefault(str(calendar.tz), []).append(row)
    covered: dict[uuid.UUID, frozenset[date]] = {}
    for zone, zone_rows in static_by_zone.items():
        covered.update(await repo.covered_dates(db, zone_rows, zone))

    views: list[DatasetView] = []
    for row in rows:
        calendar = calendars[row.id]
        if calendar is None:
            health = DatasetHealth(FreshnessStatus.RETIRED, retired_on=row.expiry) if is_retired(row) else None
        else:
            try:
                health = evaluate_dataset(
                    update_type=row.update_type,
                    granularity=row.granularity,
                    expiry=row.expiry,
                    start=row.start,
                    end=effective_end(row, now),
                    last_bar=row.last_bar,
                    covered=covered.get(row.id),
                    calendar=calendar,
                    now=now,
                    config=config,
                    last_bar_written_at=row.last_bar_written_at,
                )
            except CalendarRangeError as err:
                # A range before the calendar's start cannot be judged. It is a known state of the data (nothing
                # bounds a dataset's start below), so this row reads like a source with no calendar and every
                # other row is judged as before. Not clamped: that would be a silent answer.
                log.warning('Dataset %s starting %s has no health: %s', row.id, row.start.isoformat(), err)
                health = None
        views.append(DatasetView(row=row, health=health))
    return views


def _micros(moment: datetime) -> int:
    return (moment - _EPOCH) // _ONE_MICROSECOND


def _sort_key(
    row: CatalogRow, sort: DatasetSort, usage: dict[uuid.UUID, DatasetUsage] | None = None
) -> tuple[Any, ...]:
    tail = (row.asset_symbol, str(row.id))
    if sort is DatasetSort.SYMBOL:
        return tail
    if sort is DatasetSort.LAST_READ:
        # Newest read first: the negated microseconds ascend; never-read rows (key 1) come last.
        last_read = None if usage is None or row.id not in usage else usage[row.id].last_read_at
        if last_read is None:
            return (1, 0, *tail)
        return (0, -_micros(last_read), *tail)
    if row.expiry is None:
        return (1, 0, *tail)
    return (0, (row.expiry - _EPOCH) // _ONE_MICROSECOND, *tail)


def _cursor_for(row: CatalogRow, sort: DatasetSort, usage: dict[uuid.UUID, DatasetUsage] | None = None) -> str:
    return _encode_cursor({'o': sort.value, 'k': list(_sort_key(row, sort, usage))})


def _key_from_cursor(cursor: str, sort: DatasetSort) -> tuple[Any, ...]:
    payload = _decode_cursor(cursor)
    key = payload.get('k')
    shape: tuple[type, ...] = (str, str) if sort is DatasetSort.SYMBOL else (int, int, str, str)
    if payload.get('o') != sort.value or not isinstance(key, list) or len(key) != len(shape):
        raise invalid_cursor()
    if any(isinstance(part, bool) or not isinstance(part, kind) for part, kind in zip(key, shape, strict=True)):
        raise invalid_cursor()
    try:
        uuid.UUID(key[-1])
    except ValueError:
        raise invalid_cursor() from None
    return tuple(key)


def _matches(
    view: DatasetView,
    *,
    source: DataSource | None,
    update_type: UpdateType | None,
    status: StatusGroup | None,
    needs_attention: bool,
    running: frozenset[uuid.UUID] | None = None,
    usage: Callable[[CatalogRow], bool] | None = None,
    expiring: Callable[[CatalogRow], bool] | None = None,
) -> bool:
    if running is not None and view.row.id not in running:
        return False
    if usage is not None and not usage(view.row):
        return False
    if expiring is not None and not expiring(view.row):
        return False
    if source is not None and view.row.source != source:
        return False
    if update_type is not None and view.row.update_type != update_type:
        return False
    if status is not None and view.group != status:
        return False
    return not (needs_attention and view.group not in _ATTENTION)


_warned_usage_missing = False
# The probe's answer per request session, forgotten with the session.
_usage_ready_by_session: 'weakref.WeakKeyDictionary[AsyncSession, bool]' = weakref.WeakKeyDictionary()


async def _usage_ready(db: AsyncSession) -> bool:
    """Whether the usage tables exist; when not, usage reads as empty (one warning names `make migrate`).

    An unmigrated database must not turn a catalog read into a 500: the figures are then the zero view, the same
    as the recorder skipping its writes. A failing probe is not swallowed: it propagates with its own cause.
    """
    global _warned_usage_missing
    # Asked once per request: the answer is kept against the request's session, never across requests.
    if (cached := _usage_ready_by_session.get(db)) is not None:
        return cached
    # to_regclass is NULL for a missing table and never raises: a real database failure propagates, unwarned.
    ready = await usage_tables_exist(db)
    _usage_ready_by_session[db] = ready
    if not ready and not _warned_usage_missing:
        _warned_usage_missing = True
        log.warning(
            'Dataset usage reads as empty: the usage tables do not exist; the database has not been migrated, run make migrate'
        )
    return ready


async def _read_usage(
    db: AsyncSession, dataset_ids: list[uuid.UUID] | None, client_filter: ClientFilter, now: datetime
) -> dict[uuid.UUID, DatasetUsage]:
    if not await _usage_ready(db):
        return {}
    return await read_dataset_usage(
        db, dataset_ids, now=now, window_days=USAGE_WINDOW_DAYS, **_client_args(client_filter)
    )


async def _read_readers(
    db: AsyncSession, dataset_ids: list[uuid.UUID], now: datetime
) -> dict[uuid.UUID, tuple[frozenset[ReadClientKind], frozenset[str]]]:
    if not dataset_ids or not await _usage_ready(db):
        return {}
    return await read_readers(db, dataset_ids, now=now, window_days=USAGE_WINDOW_DAYS)


async def _read_clients(db: AsyncSession, dataset_ids: list[uuid.UUID], now: datetime) -> list[ClientDatasets]:
    if not dataset_ids or not await _usage_ready(db):
        return []
    return await read_client_datasets(db, dataset_ids, now=now, window_days=USAGE_WINDOW_DAYS)


def _client_args(client_filter: ClientFilter) -> dict[str, Any]:
    """The client selection as read_dataset_usage's keyword arguments."""
    if client_filter.client_ids:
        return {'client_ids': client_filter.applied_ids}
    return {'client_kinds': client_filter.applied_kinds}


def utc_today(now: datetime) -> date:
    """Today's UTC calendar date at `now`; a naive `now` is refused, as the usage helpers refuse it."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be timezone-aware')
    return now.astimezone(UTC).date()


def read_within(last_read_at: datetime | None, now: datetime, days: int) -> bool:
    """THE DAY-ALIGNED READ RULE, written once: the last read's UTC date is on or after today's UTC date minus `days`.

    No read at all is within no window. The usage filters (READ_30D, STALE), the facets, the usage summary and the
    stale list all decide through this, so their counts can never disagree about which datasets are stale.
    """
    if last_read_at is None:
        return False
    return last_read_at.astimezone(UTC).date() >= utc_today(now) - timedelta(days=days)


def _usage_predicate(
    group: UsageFilter, usage: dict[uuid.UUID, DatasetUsage], now: datetime, stale_days: int
) -> Callable[[CatalogRow], bool]:
    """The membership test of one usage group, over the usage read under the client filter."""

    def test(row: CatalogRow) -> bool:
        found = usage.get(row.id)
        last_read_at = None if found is None else found.last_read_at
        match group:
            case UsageFilter.NEVER_READ:
                return last_read_at is None
            case UsageFilter.READ_30D:
                return read_within(last_read_at, now, 30)
            case _:
                return not read_within(last_read_at, now, stale_days)

    return test


def _expiring_predicate(expires_before: datetime, pending: frozenset[uuid.UUID]) -> Callable[[CatalogRow], bool]:
    """Expiring Soon: an expiry before the bound, or a dataset the reaper would delete."""

    def test(row: CatalogRow) -> bool:
        return row.id in pending or (row.expiry is not None and row.expiry < expires_before)

    return test


async def _page_usage(
    db: AsyncSession,
    page_ids: list[uuid.UUID],
    known: dict[uuid.UUID, DatasetUsage] | None,
    client_filter: ClientFilter,
    now: datetime,
) -> dict[uuid.UUID, DatasetUsageView]:
    """The usage view of each page dataset: two statements at most, whatever the page holds.

    `known` is the usage already read for every dataset, when a filter or the sort needed it.
    """
    if not page_ids:
        return {}
    if known is None:
        known = await _read_usage(db, page_ids, client_filter, now)
    readers = await _read_readers(db, page_ids, now)
    views: dict[uuid.UUID, DatasetUsageView] = {}
    for dataset_id in page_ids:
        found = known.get(dataset_id)
        kinds, clients = readers.get(dataset_id, (frozenset(), frozenset()))
        views[dataset_id] = DatasetUsageView(
            last_read_at=None if found is None else found.last_read_at,
            reads_30d=0 if found is None else found.reads_in_window,
            read_by_30d=tuple(kind for kind in ReadClientKind if kind in kinds),
            read_by_clients_30d=tuple(sorted(clients)),
        )
    return views


async def list_page(
    db: AsyncSession, query: CatalogQuery, now: datetime, config: FreshnessConfig | None = None
) -> DatasetPage:
    """One page of the catalog under the query's filters and order.

    Args:
        db: The request's session.
        query: The filters, order, cursor and page size.
        now: The instant to judge freshness at; aware.
        config: The freshness settings; the environment's when omitted.

    Returns:
        DatasetPage: The page, its extents and siblings, and the cursor for the next one (empty on the last).

    Raises:
        InvalidRequestError: If the cursor does not decode, or was made under a different sort (422).
    """
    after = None if query.cursor is None else _key_from_cursor(query.cursor, query.sort)
    rows = await repo.list_catalog(db, query.catalog_filter)
    # Usage for EVERY dataset in one statement, only when the filter or the sort needs it; else the page's own.
    usage_all: dict[uuid.UUID, DatasetUsage] | None = None
    if query.usage is not None or query.sort is DatasetSort.LAST_READ:
        usage_all = await _read_usage(db, None, query.client_filter, now)
    pending_all: frozenset[uuid.UUID] | None = None
    if query.expires_before is not None:
        pending_all = await repo.pending_ids(db, now)
        keep_expiring = _expiring_predicate(query.expires_before, pending_all)
        rows = [row for row in rows if keep_expiring(row)]
    if query.usage is not None and usage_all is not None:
        keep_usage = _usage_predicate(query.usage, usage_all, now, query.stale_days)
        rows = [row for row in rows if keep_usage(row)]
    rows = sorted(rows, key=lambda row: _sort_key(row, query.sort, usage_all))
    if query.running:
        # One more statement, only for a request that asks for it; rows narrow before freshness is judged.
        in_flight = await running_dataset_ids(db)
        rows = [row for row in rows if row.id in in_flight]
    judged_everywhere = query.status is not None or query.needs_attention

    views: list[DatasetView]
    if judged_everywhere:
        views = [
            view
            for view in await evaluate_views(db, rows, now, config)
            if _matches(view, source=None, update_type=None, status=query.status, needs_attention=query.needs_attention)
        ]
    else:
        views = [DatasetView(row=row, health=None) for row in rows]
    if after is not None:
        views = [view for view in views if _sort_key(view.row, query.sort, usage_all) > after]
    window = views[: query.limit + 1]
    has_more = len(window) > query.limit
    page = window[: query.limit]
    if not judged_everywhere:
        page = await evaluate_views(db, [view.row for view in page], now, config)
    page_rows = [view.row for view in page]
    page_ids = [row.id for row in page_rows]
    return DatasetPage(
        views=page,
        extents=await repo.bar_extents(db, page_rows),
        siblings=await repo.siblings(db, page_rows),
        next_cursor=_cursor_for(page[-1].row, query.sort, usage_all) if has_more else '',
        as_of=now,
        usage=await _page_usage(db, page_ids, usage_all, query.client_filter, now),
        reaper_pending=(
            await repo.pending_ids(db, now, page_ids) if pending_all is None else pending_all & frozenset(page_ids)
        ),
        client_filter=query.client_filter,
        # ONE statement for the whole page, never one per dataset. latest_runs_for_datasets ranks each
        # dataset's runs with a row_number() window function: DISTINCT ON is Postgres-only and would
        # silently misbehave on the suite's sqlite.
        last_runs=await latest_runs_for_datasets(db, page_ids),
    )


def _pending_cursor_for(row: CatalogRow) -> str:
    if row.expiry is None:
        raise ValueError('a pending dataset always has an expiry')
    return _encode_cursor({'o': 'reaper', 'k': [(row.expiry - _EPOCH) // _ONE_MICROSECOND, str(row.id)]})


def _pending_key_from_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    payload = _decode_cursor(cursor)
    key = payload.get('k')
    if payload.get('o') != 'reaper' or not isinstance(key, list) or len(key) != 2:
        raise invalid_cursor()
    micros, dataset_id = key
    if isinstance(micros, bool) or not isinstance(micros, int) or not isinstance(dataset_id, str):
        raise invalid_cursor()
    try:
        return _EPOCH + timedelta(microseconds=micros), uuid.UUID(dataset_id)
    except (ValueError, OverflowError):
        raise invalid_cursor() from None


async def reaper_pending_page(
    db: AsyncSession, now: datetime, *, cursor: str | None, limit: int, config: FreshnessConfig | None = None
) -> DatasetPage:
    """One page of the datasets the reaper would delete: those whose expiry has passed, oldest expiry first.

    Keyset paged over (expiry, id) with an opaque cursor bound to this read (one made by the catalog list is a
    422). Records no usage. The page carries what DatasetPage carries, so the route maps its items like the
    catalog's, with reaper_pending true and usage under the default client filter.

    Args:
        db: The request's session.
        now: The instant the pending set is computed at; aware.
        cursor: The next_cursor of the previous page, or None for the first.
        limit: The page size.
        config: The freshness settings; the environment's when omitted.

    Returns:
        DatasetPage: The page; next_cursor is empty on the last.

    Raises:
        InvalidRequestError: If the cursor does not decode (422).
    """
    after = None if cursor is None else _pending_key_from_cursor(cursor)
    rows = await repo.list_pending(db, now, after=after, limit=limit + 1)
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    client_filter = ClientFilter()
    return DatasetPage(
        views=await evaluate_views(db, page_rows, now, config),
        extents=await repo.bar_extents(db, page_rows),
        siblings=await repo.siblings(db, page_rows),
        next_cursor=_pending_cursor_for(page_rows[-1]) if has_more else '',
        as_of=now,
        last_runs=await latest_runs_for_datasets(db, [row.id for row in page_rows]),
        # The default client filter (SDK): the page has no client parameter.
        usage=await _page_usage(db, [row.id for row in page_rows], None, client_filter, now),
        reaper_pending=frozenset(row.id for row in page_rows),
        client_filter=client_filter,
    )


def _count_where(
    views: list[DatasetView], predicate: Callable[[DatasetView], bool], key: Callable[[DatasetView], Any]
) -> dict[Any, int]:
    counts: dict[Any, int] = {}
    for view in views:
        if predicate(view):
            counts[key(view)] = counts.get(key(view), 0) + 1
    return counts


async def facets(db: AsyncSession, query: CatalogQuery, now: datetime, config: FreshnessConfig | None = None) -> Facets:
    """The sidebar counts under the query's filters; see the module docstring for what each equals.

    Only the symbol prefix is applied in SQL here: source and update type are facets themselves, so each count
    leaves its own filter out, which needs the unfiltered rows. Cursor, sort and limit are ignored.

    Args:
        db: The request's session.
        query: The filters.
        now: The instant to judge freshness at; aware.
        config: The freshness settings; the environment's when omitted.

    Returns:
        Facets: The counts. A source, update type or status nothing matches is present with a zero.
    """
    flt = query.catalog_filter
    rows = await repo.list_catalog(db, CatalogFilter(asset_symbol_prefix=flt.asset_symbol_prefix))
    views = await evaluate_views(db, rows, now, config)
    in_flight = await running_dataset_ids(db)
    usage = await _read_usage(db, None, query.client_filter, now)
    pending = await repo.pending_ids(db, now)
    bound = query.expires_before or now + timedelta(days=EXPIRING_SOON_DAYS)
    expiring = _expiring_predicate(bound, pending)
    in_group = {group: _usage_predicate(group, usage, now, query.stale_days) for group in UsageFilter}

    def match(view: DatasetView, *, omit: frozenset[str] = frozenset(), attention: bool | None = None) -> bool:
        return _matches(
            view,
            source=None if 'source' in omit else flt.source,
            update_type=None if 'update_type' in omit else flt.update_type,
            status=None if 'status' in omit else query.status,
            needs_attention=query.needs_attention if attention is None else attention,
            running=in_flight if query.running and 'running' not in omit else None,
            usage=None if query.usage is None or 'usage' in omit else in_group[query.usage],
            # An expires_before that was sent filters like the list's; one defaulted here only feeds its count.
            expiring=None if query.expires_before is None or 'expiring' in omit else expiring,
        )

    def by(key: Callable[[DatasetView], Any], omit: str) -> dict[Any, int]:
        return _count_where(views, lambda view: match(view, omit=frozenset({omit})), key)

    def group_count(group: UsageFilter) -> int:
        return sum(match(view, omit=frozenset({'usage'})) and in_group[group](view.row) for view in views)

    # The picker ignores the client filter, and the usage and expiry views, which are not structure.
    picker_ids = [view.row.id for view in views if match(view, omit=frozenset({'usage', 'expiring'}))]
    clients = await _read_clients(db, picker_ids, now)

    source_counts = by(lambda view: view.row.source, 'source')
    update_counts = by(lambda view: view.row.update_type, 'update_type')
    status_counts = by(lambda view: view.health.status if view.health else None, 'status')
    return Facets(
        all=sum(match(view, attention=False) for view in views),
        needs_attention=sum(match(view, attention=True) for view in views),
        sources={member: source_counts.get(member, 0) for member in DataSource},
        update_types={member: update_counts.get(member, 0) for member in UpdateType},
        statuses={member: status_counts.get(member, 0) for member in FreshnessStatus},
        # Under every filter but running itself, so with running=true it equals the list's total.
        running=sum(match(view, omit=frozenset({'running'})) and view.row.id in in_flight for view in views),
        clients=[ClientCountView(item.client_id, item.client_kind, item.datasets) for item in clients],
        client_filter=query.client_filter,
        stale=group_count(UsageFilter.STALE),
        read_30d=group_count(UsageFilter.READ_30D),
        never_read=group_count(UsageFilter.NEVER_READ),
        expiring_soon=sum(match(view, omit=frozenset({'expiring'})) and expiring(view.row) for view in views),
        stale_days=query.stale_days,
        expires_before=bound,
    )


async def dataset_detail(
    db: AsyncSession,
    dataset_id: uuid.UUID,
    now: datetime,
    config: FreshnessConfig | None = None,
    client_filter: ClientFilter | None = None,
) -> DatasetDetail | None:
    """One dataset with its freshness, extent, siblings and usage, or None when no such dataset exists.

    Args:
        db: The request's session.
        dataset_id: The dataset's id.
        now: The instant to judge freshness at; aware.
        config: The freshness settings; the environment's when omitted.
        client_filter: Whose reads the usage counts; the default (kind SDK) when omitted.

    Returns:
        DatasetDetail | None: The dataset, or None.
    """
    row = await repo.get_catalog_row(db, dataset_id)
    if row is None:
        return None
    (view,) = await evaluate_views(db, [row], now, config)
    extents = await repo.bar_extents(db, [row])
    siblings = await repo.siblings(db, [row])
    last_run = (await latest_runs_for_datasets(db, [row.id])).get(row.id)
    client_filter = client_filter or ClientFilter()
    usage = (await _page_usage(db, [row.id], None, client_filter, now))[row.id]
    return DatasetDetail(
        view=view,
        extent=extents[row.id],
        siblings=siblings[row.id],
        as_of=now,
        last_run=last_run,
        usage=usage,
        reaper_pending=row.id in await repo.pending_ids(db, now, [row.id]),
        client_filter=client_filter,
    )


async def dataset_coverage(
    db: AsyncSession, dataset_id: uuid.UUID, now: datetime, buckets: int = DEFAULT_COVERAGE_BUCKETS
) -> CoverageResult | None:
    """A dataset's coverage strip over the trading calendar, or None when no such dataset exists (tj-grna9p.63).

    Two statements, whatever the range: the entry, and the dates its bars fall on. The calendar logic is
    completeness.py's, unchanged: the same completed-session rule and the same maximal runs as the GAPS status.
    The range is the one DatasetSummary shows (start, effective_end). It applies to every update type, and a
    retired DAILY or STREAM dataset is computed over the same range (when its retirement began is not recorded).
    A bar belongs to the session whose local date it falls on, so a gap's window runs from the local midnight of
    its first session to the local midnight of the day after its last: it holds exactly the missing sessions' bars.

    Args:
        db: The request's session.
        dataset_id: The dataset's id.
        now: The instant to judge at; aware.
        buckets: The number of buckets wanted, at most; the caller bounds it to 1..MAX_COVERAGE_BUCKETS.

    Returns:
        CoverageResult | None: The strip, or None for an unknown dataset.

    Raises:
        InvalidRequestError: INVALID_REQUEST (422) when no trading calendar covers the dataset: its source has
            none, or its range starts before the calendar does. Never an empty strip, which would read as no gaps.
    """
    row = await repo.get_catalog_row(db, dataset_id)
    if row is None:
        return None
    end = effective_end(row, now)
    try:
        calendar = calendar_for_source(row.source)
        covered = (await repo.covered_dates(db, [row], str(calendar.tz)))[row.id]
        strip = compute_coverage_buckets(row.start, end, covered, calendar, now, buckets, row.granularity)
        intervals = gap_intervals(row.start, end, covered, calendar, now, row.granularity)
    except (UnknownCalendarError, CalendarRangeError):
        raise InvalidRequestError(
            Reason.INVALID_REQUEST, 'No trading calendar covers this dataset, so its coverage cannot be computed.'
        ) from None
    gaps = [
        CoverageGapWindow(
            start=local_midnight(interval.first_session, calendar),
            end=local_midnight(interval.last_session + timedelta(days=1), calendar),
            sessions=interval.sessions,
        )
        for interval in intervals[:MAX_COVERAGE_GAPS]
    ]
    return CoverageResult(start=row.start, end=end, buckets=strip, gaps=gaps, gap_count=len(intervals), as_of=now)


def _run_cursor_key(cursor: str) -> tuple[datetime, uuid.UUID]:
    payload = _decode_cursor(cursor)
    key = payload.get('k')
    if payload.get('o') != 'runs' or not isinstance(key, list) or len(key) != 2:
        raise invalid_cursor()
    started_at, run_id = key
    if not isinstance(started_at, str) or not isinstance(run_id, str):
        raise invalid_cursor()
    try:
        moment = datetime.fromisoformat(started_at)
        parsed_id = uuid.UUID(run_id)
    except ValueError:
        raise invalid_cursor() from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise invalid_cursor()
    return moment, parsed_id


async def run_page(db: AsyncSession, dataset_id: uuid.UUID, *, cursor: str | None, limit: int) -> RunPageResult | None:
    """One page of a dataset's runs, newest first, or None when no such dataset exists.

    Keyset on (started_at DESC, id DESC): a run inserted while a client pages starts later than every cursor, so
    it never shifts a page the client has not read yet. A deleted dataset's runs are not reachable here.

    Args:
        db: The request's session.
        dataset_id: The dataset's id.
        cursor: The opaque cursor of the previous page, or None for the first.
        limit: How many runs at most.

    Returns:
        RunPageResult | None: The page, or None for an unknown dataset.

    Raises:
        InvalidRequestError: If the cursor does not decode (422).
    """
    after = None if cursor is None else _run_cursor_key(cursor)
    if not await repo.dataset_exists(db, dataset_id):
        return None
    runs = await list_runs_for_dataset(db, dataset_id, limit=limit + 1, after=after)
    page = runs[:limit]
    next_cursor = (
        _encode_cursor({'o': 'runs', 'k': [page[-1].started_at.isoformat(), str(page[-1].id)]})
        if len(runs) > limit
        else ''
    )
    return RunPageResult(runs=page, next_cursor=next_cursor)


async def bar_page(
    db: AsyncSession,
    dataset_id: uuid.UUID,
    *,
    start: datetime | None,
    end: datetime | None,
    cursor: str | None,
    limit: int,
) -> BarPageResult | None:
    """One page of a dataset's bars in [start, end), ascending, or None when no such dataset exists.

    Args:
        db: The request's session.
        dataset_id: The dataset's id.
        start: Include bars at or after this instant, or None for no lower bound.
        end: Exclude bars at or after this instant, or None for no upper bound.
        cursor: The opaque cursor of the previous page, or None for the first.
        limit: How many bars at most.

    Returns:
        BarPageResult | None: The page, or None for an unknown dataset.

    Raises:
        InvalidRequestError: If the cursor does not decode (422).
    """
    after = None if cursor is None else _bar_cursor_timestamp(cursor)
    row = await repo.get_catalog_row(db, dataset_id)
    if row is None:
        return None
    bars = await repo.read_bar_page(db, row, start=start, end=end, after=after, limit=limit + 1)
    page = bars[:limit]
    next_cursor = _encode_cursor({'t': page[-1].timestamp.isoformat()}) if len(bars) > limit else ''
    return BarPageResult(bars=page, next_cursor=next_cursor)


async def bar_page_index(
    db: AsyncSession, dataset_id: uuid.UUID, *, start: datetime | None, end: datetime | None, size: int, overlap: int
) -> BarPageIndexResult | None:
    """The page index of a dataset's bars in [start, end), or None when no such dataset exists.

    The rule, for N bars in the half-open window, q = N div size, r = N mod size: N = 0 has no pages; N <= size
    is one page at offset 0; otherwise the pages, oldest first, are offset 0 when r > 0 (the earliest page, which
    holds `size` bars and so overlaps the next block), then r, r + size, ..., N - size: the full blocks counted
    back from the newest bar (r = 0 lists offset 0 once). Page k's bars are the bars read (bar_page) with
    start=pages[k].start, end=<this window's end> and limit=size + overlap, the overlap being the look-ahead
    taken from the next block (none past the newest). `overlap` does not enter the rule. An empty or inverted
    window is an empty index, not an error.

    Two statements whatever N: the entry, then the index read (repo.read_bar_page_starts).

    Args:
        db: The request's session.
        dataset_id: The dataset's id.
        start: Include bars at or after this instant, or None for no lower bound.
        end: Exclude bars at or after this instant, or None for no upper bound.
        size: The page width, 1 to MAX_PAGE_INDEX_SIZE.
        overlap: The look-ahead bars the client adds from the next block, 0 to size - 1.

    Returns:
        BarPageIndexResult | None: The index, or None for an unknown dataset.

    Raises:
        InvalidRequestError: If size or overlap is out of range, or overlap is not below size (422).
    """
    if not 1 <= size <= MAX_PAGE_INDEX_SIZE:
        raise InvalidRequestError(Reason.INVALID_REQUEST, f'size must be between 1 and {MAX_PAGE_INDEX_SIZE}.')
    if not 0 <= overlap < size:
        raise InvalidRequestError(Reason.INVALID_REQUEST, 'overlap must be at least 0 and less than size.')
    row = await repo.get_catalog_row(db, dataset_id)
    if row is None:
        return None
    bar_count, pages = await repo.read_bar_page_starts(db, row, start=start, end=end, size=size, overlap=overlap)
    return BarPageIndexResult(bar_count=bar_count, page_size=size, overlap=overlap, pages=pages)


def _bar_cursor_timestamp(cursor: str) -> datetime:
    payload = _decode_cursor(cursor)
    raw = payload.get('t')
    if set(payload) != {'t'} or not isinstance(raw, str):
        raise invalid_cursor()
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise invalid_cursor() from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise invalid_cursor()
    return moment


# USAGE STATISTICS (tj-grna9p.64). Each read below runs a constant number of statements and records no usage.
# Every one of them decides staleness through read_within, the one day-aligned rule the list's usage filter and
# the facets use, so the summary, the stale list and the Stale facet agree for the same filter.

# The first day of each last-read bucket, in whole UTC days since the last read (0 = today); the last is open.
LAST_READ_BUCKET_STARTS: Final = (0, 1, 8, 31, 91)
DEFAULT_READS_PER_DAY_DAYS: Final = 30
MAX_READS_PER_DAY_DAYS: Final = 365


@dataclass(frozen=True)
class UsageSummary:
    """The usage page's headline figures under a client filter.

    Attributes:
        stale: Datasets not read within stale_days by the selected clients, never-read included.
        read_last_30d: Datasets read within 30 days; the Facets.read_30d figure.
        total: Datasets matching the source.
        never_read: Datasets the selected clients never read.
        reads_today: Reads on today's UTC date by the selected clients.
        distinct_clients_today: Distinct client ids among them. Labels, counted only.
        client_filter: The client filter applied.
        stale_days: The threshold used.
    """

    stale: int
    read_last_30d: int
    total: int
    never_read: int
    reads_today: int
    distinct_clients_today: int
    client_filter: ClientFilter
    stale_days: int


@dataclass(frozen=True)
class LastReadBucketsResult:
    """Datasets by whole UTC days since their last read.

    Attributes:
        buckets: (min_days, max_days, datasets) per bucket, ascending, half-open [min, max); max is None for the
            open last bucket.
        never_read: Datasets never read by the selected clients. buckets plus never_read sum to the total.
        client_filter: The client filter applied.
    """

    buckets: list[tuple[int, int | None, int]]
    never_read: int
    client_filter: ClientFilter


@dataclass(frozen=True)
class ReadsPerDayResult:
    """Reads per UTC day per client.

    Attributes:
        entries: One per (day, client id, kind) with a read, ascending by day then client id.
        client_filter: The client filter applied.
    """

    entries: list[ClientDayReads]
    client_filter: ClientFilter


@dataclass(frozen=True)
class ClientLastReadView:
    """When one client last read a dataset. client_id is repr=False."""

    client_id: str = field(repr=False)
    client_kind: ReadClientKind
    last_read_at: datetime


@dataclass(frozen=True)
class StaleDatasetPageResult:
    """One page of stale datasets.

    Attributes:
        page: The datasets, with the usage under the client filter, as the catalog list carries them; its
            next_cursor is the stale list's own.
        est_bytes: The estimated stored size of each dataset: its bar count times the average row size.
        last_reads: The last read of each dataset by every client that ever read it, whatever the filter.
        stale_days: The threshold used.
    """

    page: DatasetPage
    est_bytes: dict[uuid.UUID, int]
    last_reads: dict[uuid.UUID, list[ClientLastReadView]]
    stale_days: int


@dataclass(frozen=True)
class StorageShare:
    """The estimated storage of one (source, feed).

    Attributes:
        source: The vendor.
        feed: The tape.
        bar_count: The bars held.
        est_bytes: bar_count times the average row size; an estimate.
    """

    source: DataSource
    feed: Feed
    bar_count: int
    est_bytes: int


@dataclass(frozen=True)
class StorageCurrent:
    """What the bar table holds now. The split is an ESTIMATE; the total is measured unless a source was asked for.

    Attributes:
        total_bytes: pg_total_relation_size of the bar table; with a source filter, the sum of that source's
            estimates, so the split always sums to the total (within rounding).
        by_source: One share per (source, feed) holding bars.
        as_of: The instant it was read at.
    """

    total_bytes: int
    by_source: list[StorageShare]
    as_of: datetime


def estimate_bytes(bar_count: int, total_bytes: int, total_bars: int) -> int:
    """A bar count's share of the table's bytes: bar_count times the average row size (total_bytes / total_bars).

    Computed as bar_count * total_bytes // total_bars, so a split's parts add up to the total within one byte per
    part. 0 when the table holds no bars. An estimate: every bar is assumed as large as the average.
    """
    if total_bars <= 0:
        return 0
    return bar_count * total_bytes // total_bars


async def _read_day_reads(
    db: AsyncSession, dataset_ids: list[uuid.UUID] | None, client_filter: ClientFilter, now: datetime, days: int
) -> list[ClientDayReads]:
    if (dataset_ids is not None and not dataset_ids) or not await _usage_ready(db):
        return []
    return await read_client_day_reads(db, dataset_ids, now=now, days=days, **_client_args(client_filter))


def _last_read(usage: dict[uuid.UUID, DatasetUsage], dataset_id: uuid.UUID) -> datetime | None:
    found = usage.get(dataset_id)
    return None if found is None else found.last_read_at


async def usage_summary(
    db: AsyncSession,
    now: datetime,
    *,
    client_filter: ClientFilter,
    stale_days: int = DEFAULT_STALE_DAYS,
    source: DataSource | None = None,
) -> UsageSummary:
    """The usage page's headline figures: a constant number of statements, whatever the store holds.

    Args:
        db: The request's session.
        now: The instant the figures are judged at; aware, else ValueError.
        client_filter: Whose reads count.
        stale_days: The staleness threshold, 1..MAX_STALE_DAYS.
        source: Count only this source's datasets (and the reads of them), or None for every dataset.

    Returns:
        UsageSummary: Everything never read, and no reads today, on an unmigrated database.
    """
    utc_today(now)
    rows = await repo.list_catalog(db, CatalogFilter(source=source))
    usage = await _read_usage(db, None, client_filter, now)
    last_reads = [_last_read(usage, row.id) for row in rows]
    reads_today = await _read_day_reads(
        db, None if source is None else [row.id for row in rows], client_filter, now, days=1
    )
    return UsageSummary(
        stale=sum(not read_within(last, now, stale_days) for last in last_reads),
        read_last_30d=sum(read_within(last, now, 30) for last in last_reads),
        total=len(rows),
        never_read=sum(last is None for last in last_reads),
        reads_today=sum(entry.reads for entry in reads_today),
        distinct_clients_today=len({entry.client_id for entry in reads_today}),
        client_filter=client_filter,
        stale_days=stale_days,
    )


async def last_read_buckets(
    db: AsyncSession, now: datetime, *, client_filter: ClientFilter, source: DataSource | None = None
) -> LastReadBucketsResult:
    """Datasets by whole UTC days since the last read, in [0,1), [1,8), [8,31), [31,91) and [91, open), plus never.

    The days run from the UTC date of the last read to today's UTC date, so 0 is today, as reads_today and the
    reads per day are. The buckets and never_read sum to the number of datasets.

    Args:
        db: The request's session.
        now: The instant the buckets are judged at; aware, else ValueError.
        client_filter: Whose reads count.
        source: Count only this source's datasets, or None for every dataset.

    Returns:
        LastReadBucketsResult: Every bucket present, with a zero where nothing falls in it.
    """
    today = utc_today(now)
    rows = await repo.list_catalog(db, CatalogFilter(source=source))
    usage = await _read_usage(db, None, client_filter, now)
    counts = [0] * len(LAST_READ_BUCKET_STARTS)
    never_read = 0
    for row in rows:
        last = _last_read(usage, row.id)
        if last is None:
            never_read += 1
            continue
        # A read dated after today (a skewed clock) is today's: it must land in a bucket.
        days = max(0, (today - last.astimezone(UTC).date()).days)
        position = max(index for index, start in enumerate(LAST_READ_BUCKET_STARTS) if days >= start)
        counts[position] += 1
    ends: list[int | None] = [*LAST_READ_BUCKET_STARTS[1:], None]
    return LastReadBucketsResult(
        buckets=list(zip(LAST_READ_BUCKET_STARTS, ends, counts, strict=True)),
        never_read=never_read,
        client_filter=client_filter,
    )


async def reads_per_day(
    db: AsyncSession,
    now: datetime,
    *,
    client_filter: ClientFilter,
    days: int = DEFAULT_READS_PER_DAY_DAYS,
    dataset_id: uuid.UUID | None = None,
    source: DataSource | None = None,
) -> ReadsPerDayResult:
    """Reads per UTC day per client over the last `days` days, today included, across the 90-day rollup.

    One entry per (day, client id, kind) with a read. The per-read rows and the daily summaries are summed in one
    statement, so a day split across the rollup boundary is one entry. With neither dataset_id nor source it counts
    every read, those of deleted datasets too, which is also what the summary's reads_today counts.

    Args:
        db: The request's session.
        now: The instant the window ends at; aware, else ValueError.
        client_filter: Whose reads count.
        days: The window, 1..MAX_READS_PER_DAY_DAYS.
        dataset_id: Count only this dataset (Dataset detail), or None.
        source: Count only this source's datasets, or None. With a dataset_id of another source the answer is empty.

    Returns:
        ReadsPerDayResult: Empty on an unmigrated database.
    """
    utc_today(now)
    dataset_ids: list[uuid.UUID] | None = None
    if source is not None:
        rows = await repo.list_catalog(db, CatalogFilter(source=source))
        dataset_ids = [row.id for row in rows if dataset_id is None or row.id == dataset_id]
    elif dataset_id is not None:
        dataset_ids = [dataset_id]
    entries = await _read_day_reads(db, dataset_ids, client_filter, now, days=days)
    return ReadsPerDayResult(entries=entries, client_filter=client_filter)


def _stale_fingerprint(client_filter: ClientFilter, stale_days: int, source: DataSource | None) -> str:
    """What a stale cursor is bound to, as a digest so the cursor does not carry the client ids it was made under."""
    material = json.dumps(
        [
            stale_days,
            None if source is None else source.name,
            sorted(client_filter.applied_ids),
            [kind.value for kind in client_filter.applied_kinds],
        ],
        separators=(',', ':'),
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _stale_key(row: CatalogRow, usage: dict[uuid.UUID, DatasetUsage]) -> tuple[int, int, str]:
    """Never-read first (0), then the oldest last read, then the id."""
    last = _last_read(usage, row.id)
    if last is None:
        return (0, 0, str(row.id))
    return (1, _micros(last), str(row.id))


def _stale_key_from_cursor(cursor: str, fingerprint: str) -> tuple[Any, ...]:
    payload = _decode_cursor(cursor)
    key = payload.get('k')
    if payload.get('o') != 'stale' or payload.get('f') != fingerprint or not isinstance(key, list) or len(key) != 3:
        raise invalid_cursor()
    shape: tuple[type, ...] = (int, int, str)
    if any(isinstance(part, bool) or not isinstance(part, kind) for part, kind in zip(key, shape, strict=True)):
        raise invalid_cursor()
    try:
        uuid.UUID(key[-1])
    except ValueError:
        raise invalid_cursor() from None
    return tuple(key)


async def stale_page(
    db: AsyncSession,
    now: datetime,
    *,
    client_filter: ClientFilter,
    stale_days: int = DEFAULT_STALE_DAYS,
    source: DataSource | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    config: FreshnessConfig | None = None,
) -> StaleDatasetPageResult:
    """One page of the stale group under the client filter, never-read first, then by last read, then id.

    The group is the STALE usage filter's: the Stale facet's count equals the total of this list. Keyset paged with
    an opaque cursor bound to stale_days, the source and the client filter (one made under another is a 422).
    A constant number of statements: the entries, the usage of all of them, then for the page its freshness,
    extents, siblings, runs, reaper flag, readers, last read per client and the table's storage. Records no usage.

    Args:
        db: The request's session.
        now: The instant staleness and freshness are judged at; aware, else ValueError.
        client_filter: Whose reads count toward the usage under dataset.usage.
        stale_days: The staleness threshold, 1..MAX_STALE_DAYS.
        source: Only this source's datasets, or None.
        cursor: The next_cursor of the previous page, or None for the first.
        limit: The page size.
        config: The freshness settings; the environment's when omitted.

    Returns:
        StaleDatasetPageResult: The page; next_cursor is empty on the last.

    Raises:
        InvalidRequestError: If the cursor does not decode or was made under another filter (422).
    """
    utc_today(now)
    fingerprint = _stale_fingerprint(client_filter, stale_days, source)
    after = None if cursor is None else _stale_key_from_cursor(cursor, fingerprint)
    rows = await repo.list_catalog(db, CatalogFilter(source=source))
    usage = await _read_usage(db, None, client_filter, now)
    keep = _usage_predicate(UsageFilter.STALE, usage, now, stale_days)
    keyed = sorted(((_stale_key(row, usage), row) for row in rows if keep(row)), key=lambda item: item[0])
    if after is not None:
        keyed = [item for item in keyed if item[0] > after]
    window = keyed[: limit + 1]
    has_more = len(window) > limit
    page_rows = [row for _, row in window[:limit]]
    page_ids = [row.id for row in page_rows]

    storage = await repo.read_bar_storage(db) if page_rows else BarStorage(total_bytes=0, by_source=[])
    extents = await repo.bar_extents(db, page_rows)
    total_bars = sum(count for _, _, count in storage.by_source)
    last_reads: dict[uuid.UUID, list[ClientLastReadView]] = {dataset_id: [] for dataset_id in page_ids}
    if page_ids and await _usage_ready(db):
        for item in await read_client_last_reads(db, page_ids):
            last_reads[item.dataset_id].append(ClientLastReadView(item.client_id, item.client_kind, item.last_read_at))
    page = DatasetPage(
        views=await evaluate_views(db, page_rows, now, config),
        extents=extents,
        siblings=await repo.siblings(db, page_rows),
        next_cursor=(
            _encode_cursor({'o': 'stale', 'k': list(window[limit - 1][0]), 'f': fingerprint}) if has_more else ''
        ),
        as_of=now,
        usage=await _page_usage(db, page_ids, usage, client_filter, now),
        reaper_pending=await repo.pending_ids(db, now, page_ids),
        client_filter=client_filter,
        last_runs=await latest_runs_for_datasets(db, page_ids),
    )
    return StaleDatasetPageResult(
        page=page,
        est_bytes={row.id: estimate_bytes(extents[row.id].count, storage.total_bytes, total_bars) for row in page_rows},
        last_reads=last_reads,
        stale_days=stale_days,
    )


async def storage_current(db: AsyncSession, now: datetime, *, source: DataSource | None = None) -> StorageCurrent:
    """The bar table's size now, split by (source, feed) as ESTIMATES. Two statements. No history.

    The average row size is the table's measured total over its bar count; a share is its bar count times that.
    With `source` the split is that source's only and total_bytes is the sum of its shares, so the split always
    sums to the total.

    Args:
        db: The request's session.
        now: The instant it was read at; aware, else ValueError.
        source: Only this source, or None for the whole table.

    Returns:
        StorageCurrent: Zero bytes and no shares for an empty table.
    """
    utc_today(now)
    storage = await repo.read_bar_storage(db)
    total_bars = sum(count for _, _, count in storage.by_source)
    shares = [
        StorageShare(held_source, feed, count, estimate_bytes(count, storage.total_bytes, total_bars))
        for held_source, feed, count in storage.by_source
        if source is None or held_source is source
    ]
    return StorageCurrent(
        total_bytes=storage.total_bytes if source is None else sum(share.est_bytes for share in shares),
        by_source=shares,
        as_of=now,
    )
