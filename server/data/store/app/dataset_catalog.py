"""The dataset catalog the /ui/v1 routes serve: filtering, keyset paging, facets and freshness (tj-grna9p.20).

This module composes the read repository (database/crud/stock/dataset_catalog.py) with the pure freshness rules
(freshness.py). The routes call it and map its results to messages; it knows no HTTP and no protobuf.

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

THE CURSOR is opaque to clients: URL-safe base64 of a small JSON document holding the sort and the last item's
sort key. It is bound to the sort it was made under, and anything that does not decode to exactly that shape is a
422, never a 500 and never echoed.
"""

import base64
import binascii
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import DataSource, Granularity, UpdateType
from common.errors.vocabulary import InvalidRequestError, Reason
from common.logging import get_logger
from data.store.app.database.crud.stock import dataset_catalog as repo
from data.store.app.database.crud.stock.dataset_catalog import BarExtent, CatalogFilter, CatalogRow
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.freshness import (
    CalendarRangeError,
    DatasetHealth,
    FreshnessConfig,
    FreshnessStatus,
    TradingCalendar,
    UnknownCalendarError,
    calendar_for_source,
    evaluate_dataset,
)


log = get_logger(__name__)

DEFAULT_PAGE_LIMIT: Final = 100
MAX_PAGE_LIMIT: Final = 500
DEFAULT_BAR_LIMIT: Final = 1000
MAX_BAR_LIMIT: Final = 10_000

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
class CatalogQuery:
    """What a list or facets request asks for.

    Attributes:
        catalog_filter: The SQL-level filters (symbol prefix, source, update type).
        status: Keep datasets whose computed freshness is in this group.
        needs_attention: Keep only Late and Failed datasets.
        sort: The order of the list.
        cursor: The opaque cursor of the previous page, or None for the first.
        limit: The page size.
    """

    catalog_filter: CatalogFilter = field(default_factory=CatalogFilter)
    status: StatusGroup | None = None
    needs_attention: bool = False
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
    """

    views: list[DatasetView]
    extents: dict[uuid.UUID, BarExtent]
    siblings: dict[uuid.UUID, list[tuple[uuid.UUID, Granularity]]]
    next_cursor: str
    as_of: datetime


@dataclass(frozen=True)
class Facets:
    """The sidebar counts, see the module docstring for what each equals.

    Attributes:
        all: The All Datasets view's total.
        needs_attention: The Needs Attention view's total.
        sources: A count for every source.
        update_types: A count for every update type.
        statuses: A count for every computed freshness status.
    """

    all: int
    needs_attention: int
    sources: dict[DataSource, int]
    update_types: dict[UpdateType, int]
    statuses: dict[FreshnessStatus, int]


@dataclass(frozen=True)
class DatasetDetail:
    """One dataset as the viewer opens it.

    Attributes:
        view: The entry with its freshness.
        extent: What is stored.
        siblings: The other granularities of the series.
        as_of: The instant freshness was computed at.
    """

    view: DatasetView
    extent: BarExtent
    siblings: list[tuple[uuid.UUID, Granularity]]
    as_of: datetime


@dataclass(frozen=True)
class BarPageResult:
    """One page of a dataset's bars.

    Attributes:
        bars: The bar rows, ascending by timestamp.
        next_cursor: The cursor for the next page; empty on the last.
    """

    bars: list[StockMarketActivity]
    next_cursor: str


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
                )
            except CalendarRangeError as err:
                # A range before the calendar's start cannot be judged. It is a known state of the data (nothing
                # bounds a dataset's start below), so this row reads like a source with no calendar and every
                # other row is judged as before. Not clamped: that would be a silent answer.
                log.warning('Dataset %s starting %s has no health: %s', row.id, row.start.isoformat(), err)
                health = None
        views.append(DatasetView(row=row, health=health))
    return views


def _sort_key(row: CatalogRow, sort: DatasetSort) -> tuple[Any, ...]:
    tail = (row.asset_symbol, str(row.id))
    if sort is DatasetSort.SYMBOL:
        return tail
    if row.expiry is None:
        return (1, 0, *tail)
    return (0, (row.expiry - _EPOCH) // _ONE_MICROSECOND, *tail)


def _cursor_for(row: CatalogRow, sort: DatasetSort) -> str:
    return _encode_cursor({'o': sort.value, 'k': list(_sort_key(row, sort))})


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
) -> bool:
    if source is not None and view.row.source != source:
        return False
    if update_type is not None and view.row.update_type != update_type:
        return False
    if status is not None and view.group != status:
        return False
    return not (needs_attention and view.group not in _ATTENTION)


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
    rows = sorted(await repo.list_catalog(db, query.catalog_filter), key=lambda row: _sort_key(row, query.sort))
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
        views = [view for view in views if _sort_key(view.row, query.sort) > after]
    window = views[: query.limit + 1]
    has_more = len(window) > query.limit
    page = window[: query.limit]
    if not judged_everywhere:
        page = await evaluate_views(db, [view.row for view in page], now, config)
    page_rows = [view.row for view in page]
    return DatasetPage(
        views=page,
        extents=await repo.bar_extents(db, page_rows),
        siblings=await repo.siblings(db, page_rows),
        next_cursor=_cursor_for(page[-1].row, query.sort) if has_more else '',
        as_of=now,
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

    def match(view: DatasetView, *, omit: str | None = None, attention: bool | None = None) -> bool:
        return _matches(
            view,
            source=None if omit == 'source' else flt.source,
            update_type=None if omit == 'update_type' else flt.update_type,
            status=None if omit == 'status' else query.status,
            needs_attention=query.needs_attention if attention is None else attention,
        )

    def by(key: Callable[[DatasetView], Any], omit: str) -> dict[Any, int]:
        return _count_where(views, lambda view: match(view, omit=omit), key)

    source_counts = by(lambda view: view.row.source, 'source')
    update_counts = by(lambda view: view.row.update_type, 'update_type')
    status_counts = by(lambda view: view.health.status if view.health else None, 'status')
    return Facets(
        all=sum(match(view, attention=False) for view in views),
        needs_attention=sum(match(view, attention=True) for view in views),
        sources={member: source_counts.get(member, 0) for member in DataSource},
        update_types={member: update_counts.get(member, 0) for member in UpdateType},
        statuses={member: status_counts.get(member, 0) for member in FreshnessStatus},
    )


async def dataset_detail(
    db: AsyncSession, dataset_id: uuid.UUID, now: datetime, config: FreshnessConfig | None = None
) -> DatasetDetail | None:
    """One dataset with its freshness, extent and siblings, or None when no such dataset exists.

    Args:
        db: The request's session.
        dataset_id: The dataset's id.
        now: The instant to judge freshness at; aware.
        config: The freshness settings; the environment's when omitted.

    Returns:
        DatasetDetail | None: The dataset, or None.
    """
    row = await repo.get_catalog_row(db, dataset_id)
    if row is None:
        return None
    (view,) = await evaluate_views(db, [row], now, config)
    extents = await repo.bar_extents(db, [row])
    siblings = await repo.siblings(db, [row])
    return DatasetDetail(view=view, extent=extents[row.id], siblings=siblings[row.id], as_of=now)


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
