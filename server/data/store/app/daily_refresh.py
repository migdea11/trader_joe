"""THE DAILY REFRESH: the store's INTERIM scheduler for DAILY subscriptions (tj-grna9p.22).

INTERIM, BY RULING (tj-utqd77 Q1, "interim store loop"). The scheduler that belongs to ingest (tj-pd41mt Addendum 1)
is not built. Until it lands the STORE triggers the refresh, and ingest sees an ORDINARY Submit request like any
other: no new message type, no request queue (tj-grna9p.47 is deferred, tj-grna9p.21 is not built). When ingest's
scheduler lands this module is removed, not extended.

WHEN. One periodic runner per source that has a trading calendar (SOURCE_CALENDARS), on AfterTradingEvent(calendar,
CLOSE, settle): a session's close plus DAILY_REFRESH_SETTLE_MINUTES, from the store's own calendar for that source.
The calendar is an argument to the schedule; common imports none. A pass also runs once at start (catch-up).

WHAT. Every pass derives its work from the DATABASE; nothing is stored and nothing is replayed (common/scheduling.py).
It reads the DAILY entries of its source, drops the retired ones (collect is false: the operator stopped it, the
same rule the UI judges by: is_retired; an expiry alone retires nothing), drops those with a RUNNING ingest, and keeps the ones the health rule
reads LATE or OVERDUE: the last stored bar is before the last completed session. For each, oldest first, it asks
ingest for ONLY the missing range: from the newest STORED bar plus one bar, a placeholder bar included (the declared
start when there are no bars), reaching back to the newest real bar when it was written before its period closed
(the vendor's part-way bar is overwritten by the final one, tj-grna9p.167) and to the oldest placeholder not yet
settled for PLACEHOLDER_GRACE (a pad the vendor may replace, tj-7zjov1.114); never before the declared start, and
nothing is requested when that leaves no period to ask for. The request is written and submitted by submit_bars
(trigger SCHEDULE, actor `scheduler`) and not awaited. The entry keeps its declared range as identity, so the refresh extends the
existing entry and never creates a second one. A dataset requested earlier is therefore picked up by the next pass.

BOUNDED. One dataset at a time, never concurrently; at most DAILY_REFRESH_MAX_PER_PASS datasets REQUESTED per pass,
so the rest wait for the next. A TRANSIENT failure (a peer or vendor that is not ready: the vocabulary's NOT_READY
outcome) is logged by exception TYPE (never the owner or the symbol) and backs off before the next dataset; the pass
stops after MAX_CONSECUTIVE_FAILURES in a row, or at once on an answer that names a reset (a rate limit). After a
pass with transient failures NO retry is scheduled (decision tj-grna9p.55: no automatic retry; Retry Now is
manual): a dataset that failed waits for the next pass, the next session close or the catch-up at start. The backoff
is pacing before the NEXT dataset, never a second try of the failed one.

A PERMANENT refusal (the vocabulary's REFUSED outcome: a tape ingest can no longer serve, an invalid request, a
vendor 4xx) cannot succeed on retry. It does not count toward the stop and does not back off; a stored row that
no longer validates as a request is never sent at all.

FAIR. Nothing is stored for the order either: it is derived from the ingest_run history of trigger SCHEDULE. A
dataset never attempted goes first (oldest newest-bar first), then those attempted least recently, so a dataset that
failed or came back empty waits behind the others. One whose last scheduled attempt finished EMPTY or as a permanent
failure since the last session close is not requested again before the next close, and only while the tail it asks
for is the same. Datasets that last came back empty or refused and are due again share each pass with the rest: at
most half the cap, so neither class starves the other. The cap counts only datasets requested.

LIFECYCLE IS NOT THE REFRESH'S. It never writes the entry's expiry, update type or owner: the entry is locked and
re-checked on the ack, right before bars are written, and a dataset retired (or deleted) while the vendor call was in
flight has the answer DROPPED, not stored, and counted. An operator's retire is therefore never undone.

SAFETY. Nothing here counts vendor quota (ingest's counting, tj-grna9p.24, is not built). What bounds the spend is
the cap and the serialisation above, ingest's per-vendor rate budget on every call, and the fact that a request is
only the missing tail. DAILY_REFRESH_ENABLED is the kill switch.

ONE PROCESS: like the reaper and the usage loops this is correct only while data_store runs one worker.

NOTHING IN A LOG NAMES AN OWNER OR A SYMBOL: counts and exception types only (D8).
"""

import asyncio
import contextlib
import os
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Final

from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import DataSource, Granularity, UpdateType
from common.environment import get_env_var
from common.errors.vocabulary import ERROR_DOMAIN, REASONS, Outcome, TraderJoeError
from common.logging import get_logger
from common.rpc.clients.ingest_submit import IngestSubmitClient
from common.scheduling import AfterTradingEvent, TradingEvent, run_periodic
from data.store.app.completeness import next_period_start, period_start
from data.store.app.database.crud.ingest_run import IngestRunRecord, finished_scheduled_runs, running_dataset_ids
from data.store.app.database.crud.stock.dataset_catalog import (
    CatalogFilter,
    CatalogRow,
    CountedBar,
    PlaceholderBar,
    list_catalog,
    newest_counted_real_bar,
    recent_placeholder_bars,
)
from data.store.app.database.database import own_session
from data.store.app.database.models.ingest_request import IngestRequestFailureCause
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.dataset_catalog import effective_end, is_retired
from data.store.app.health import (
    DEFAULT_SETTLE_MINUTES as DEFAULT_SETTLE_MINUTES,  # the refresh's setting, defined once in health and re-exported
)
from data.store.app.health import SETTLE_ENV as SETTLE_ENV
from data.store.app.health import (
    DatasetHealthStatus,
    HealthConfig,
    bar_period_close,
    evaluate_dataset,
    last_bar_is_provisional,
    settle_margin_from_env,
    written_before_its_stamp,
)
from data.store.app.ingest.data_action_request import submit_bars
from data.store.app.trading_calendar import (
    SOURCE_CALENDARS,
    CalendarRangeError,
    TradingCalendar,
    UnknownCalendarError,
    calendar_for_source,
    local_midnight,
    session_of,
)
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath


log = get_logger(__name__)

ENABLED_ENV: Final = 'DAILY_REFRESH_ENABLED'
MAX_PER_PASS_ENV: Final = 'DAILY_REFRESH_MAX_PER_PASS'
BACKOFF_ENV: Final = 'DAILY_REFRESH_BACKOFF_SECONDS'

DEFAULT_MAX_PER_PASS: Final = 10
DEFAULT_BACKOFF_SECONDS: Final = 30

BACKOFF_CAP_SECONDS: Final = 600
MAX_CONSECUTIVE_FAILURES: Final = 3

# The run's actor: a system action, whatever dataset owner it works for.
SCHEDULER_ACTOR: Final = 'scheduler'

# How far back the run history is read for the order and the skip rule. A dataset not attempted within it counts
# as never attempted, which only puts it earlier.
ATTEMPT_LOOKBACK: Final = timedelta(days=7)

# How long after its period's close (and the settle margin) a stored placeholder bar is still asked for again. The
# cost is bounded and constant: a dead series re-requests this window in one call per pass rather than a tail that
# grows by a session per close.
PLACEHOLDER_GRACE: Final = timedelta(days=7)
# How far before the newest real bar the placeholders are read: the grace plus the longest bar period (a month).
PLACEHOLDER_LOOKBACK: Final = PLACEHOLDER_GRACE + timedelta(days=35)
# The placeholders read per page; older pages are read while the page just read still ends in an unsettled one.
PLACEHOLDER_SCAN_LIMIT: Final = 5000

# The reasons a typed refusal can carry that no retry cures: the vocabulary's REFUSED outcome, by the single table
# every error's disposition is decided from. As the strings a finished run stores.
PERMANENT_REASONS: Final = frozenset(str(reason) for reason, spec in REASONS.items() if spec.outcome is Outcome.REFUSED)

# The warning that names `make migrate` is given once per process, as the reaper's and the recorder's are.
_unmigrated_warned = False


def is_permanent(error: BaseException) -> bool:
    """Whether a failed fetch can never succeed on retry: a typed error whose reason is REFUSED in the vocabulary.

    Everything else is transient: NOT_READY reasons (a peer or vendor not there, a deadline, a rate limit that names
    its reset, a 5xx) and an error nobody typed, which is counted and retried rather than trusted to be final.
    """
    return isinstance(error, TraderJoeError) and REASONS[error.reason].outcome is Outcome.REFUSED


def _as_aware(instant: datetime) -> datetime:
    return instant if instant.tzinfo is not None else instant.replace(tzinfo=UTC)


def _can_be_real(
    bar: datetime,
    written_at: datetime | None,
    row: CatalogRow,
    calendar: TradingCalendar,
    now: datetime | None,
    *,
    placeholder: bool,
) -> bool:
    """Whether a stored bar COUNTS as fetched, so that it may advance the start of the tail.

    THE RULE (tj-7zjov1.114, decision tj-7zjov1.120): only a bar that was a real bar of the dataset when it was
    fetched may advance the start. A stored bar that does not count never stands for the sessions before it, because
    the start would then pass a session never requested and nothing would ever ask for it again (a permanent gap).
    It does not count when it
      - is stamped at or after `now` (a bar of a period that has not happened);
      - was WRITTEN BEFORE ITS OWN STAMP (health.written_before_its_stamp: written_at < timestamp, equality counts):
        it was a future bar when it was fetched, so the
        sessions between that fetch and its stamp were never requested. The `now` test alone is not enough: the bar
        starts to count once now passes its stamp, and a pass that did not run in between (downtime) would leave
        those sessions unrequested for good. Only a rewrite after the stamp makes it count. An unknown write time
        (None) leaves the other tests to decide;
      - is outside the declared range [start, end);
      - is a PLACEHOLDER in a period that holds no session (a weekend or holiday pad), or one the calendar cannot
        place.
    A real bar (trades) in a no-session period is data the vendor served and still counts. A bar that does not count
    is ignored here (it stays stored, raw). The cost is a re-request of the sessions before it.
    """
    stamp = _as_aware(bar)
    if now is not None and stamp >= _as_aware(now):
        return False
    if written_at is not None and written_before_its_stamp(stamp, _as_aware(written_at)):
        return False
    if stamp < _as_aware(row.start) or (row.end is not None and stamp >= _as_aware(row.end)):
        return False
    if not placeholder:
        return True
    try:
        return bar_period_close(stamp, calendar, row.granularity) is not None
    except CalendarRangeError:
        return False


def _written_early_inside_range(row: CatalogRow) -> bool:
    """Whether the newest real bar fails to count ONLY by being written before its stamp, with a known write time."""
    if row.last_bar is None or row.last_bar_written_at is None:
        return False
    stamp = _as_aware(row.last_bar)
    inside = _as_aware(row.start) <= stamp and (row.end is None or stamp < _as_aware(row.end))
    return inside and written_before_its_stamp(stamp, _as_aware(row.last_bar_written_at))


def _skips_a_period(earlier: datetime, later: datetime, granularity: Granularity, calendar: TradingCalendar) -> bool:
    """Whether a period holding a session lies wholly between two stored bars, with no bar of its own.

    A hole inside the run of stored pads (the vendor padded around a session without padding it) is a session never
    fetched; the tail must reach back to it, as the old rule did by starting at the last real bar. Only the calendar
    decides: a weekend between two 1day bars holds no session and is no hole. Within one period, or between adjacent
    periods, there is nothing between.
    """
    first = next_period_start(period_start(session_of(earlier, calendar), granularity), granularity)
    last = period_start(session_of(later, calendar), granularity)
    if first >= last:
        return False
    try:
        return bool(calendar.sessions_overlapping(local_midnight(first, calendar), local_midnight(last, calendar)))
    except CalendarRangeError:
        return True


def _placeholder_unsettled(
    bar: PlaceholderBar, calendar: TradingCalendar, granularity: Granularity, settle_margin: timedelta
) -> bool:
    """Whether a stored placeholder may still be replaced by a real bar, so the tail must reach back to it.

    A placeholder is SETTLED only when it was written at or after its period's close plus the settle margin plus
    PLACEHOLDER_GRACE. The vendor pads a series with flat zero-volume bars (PARA); the evidence does not say whether
    a pad is ever later replaced by real data, so a recent one is re-asked for until it has stood for the grace. One
    written long after its close (a backfill of years ago) is settled at once. A re-fetch rewrites updated_at, so a
    placeholder leaves the window by itself and the window never grows. A period holding no session (a stray weekend
    bar) can never become a real bar and is settled; a period the calendar cannot place is NOT settled.
    """
    try:
        closes = bar_period_close(_as_aware(bar.timestamp), calendar, granularity)
    except CalendarRangeError:
        return True
    return closes is not None and _as_aware(bar.written_at) < closes + settle_margin + PLACEHOLDER_GRACE


@dataclass(frozen=True)
class LostAnchor:
    """The newest COUNTED real bar of a dataset whose newest real bar does not count (tj-7zjov1.122).

    Attributes:
        bar: The newest counted real bar, or None when the dataset holds none (pads only).
    """

    bar: CountedBar | None


def _fetch_start(
    row: CatalogRow,
    calendar: TradingCalendar,
    placeholders: Sequence[PlaceholderBar] = (),
    now: datetime | None = None,
    *,
    truncated: bool = False,
    lost_anchor: LostAnchor | None = None,
) -> datetime:
    """Where the refresh's tail starts: the declared start with no bars, else just after the newest STORED bar.

    The newest stored bar is the newest of the newest real bar (row.last_bar) and the stored placeholders that
    _can_be_real, so a placeholder tail the vendor keeps padding is not asked for again from the last real bar on every
    pass (tj-7zjov1.114). A stored bar that cannot be a real period (future, outside the range, in no session) never
    counts. Health and the pass order still judge the real bar. The tail reaches BACK, never leaving a gap or a
    stale bar, to the oldest of:
      - the newest real bar, when it is provisional (written before its period closed and settled, see
        last_bar_is_provisional; for 1min..1hour the period is the whole session, not the bar's own);
      - every stored placeholder not yet settled (_placeholder_unsettled), because a pad may become a real bar;
      - the first bar of the trailing run (the newest real bar and the pads after it) that is followed by a period
        with a session and no bar (_skips_a_period).
    The upsert overwrites each with the final bar. Once rewritten after the close (and the grace) they are final and
    the tail starts after the newest again. The start is never before the declared start.

    LOST ANCHOR. When the newest real bar exists but does not count (_can_be_real), the newest COUNTED real bar, the
    anchor of the hole check, is not known. Two cases:
      - It fails ONLY by being written before its own stamp (it is inside [start, end) and its write time is known).
        The write time is the fetch transaction's start, which opens before the vendor request, so it is a lower bound
        on when the bar was served: everything before that write was requested by that fetch or an earlier one
        (decision tj-7zjov1.120 addendum 1). The tail starts at the local midnight of the period holding the WRITE TIME,
        or at the oldest unsettled counted pad if earlier, never before the declared start. That period is requested
        whole, so a part-way bar in it is rewritten. This is NORMAL for a live 1min dataset (the last bar is stamped a
        few seconds after the transaction start): it costs at most one extra session (about 960 bars) inside the call
        the pass makes anyway. No hole pairing and no provisional reach-back are needed, since every such candidate
        lies at or after that midnight.
      - Any other case (outside the declared range, or no write time): the newest COUNTED real bar was read for
        this case (`lost_anchor`, one statement, tj-7zjov1.122) and takes the place of the newest real bar: it is the
        anchor of the run and of the hole check, its own provisional test applies, and the tail starts after the
        newest counted bar as for any dataset. No counted real bar at all (`LostAnchor(None)`) means the dataset
        has pads only. Without a read (`lost_anchor` None: the caller did not read it) the tail starts at the
        declared start, the full re-fetch the read removes.

    TRUNCATED READ. `truncated` says the placeholder read stopped on a full page of settled bars, so older pads exist
    that were not read. The newest real bar is then NOT paired with the oldest pad read: the unread pads between them
    would look like a hole and the tail would reach back to the real bar, the growing re-fetch this rule removes.
    Pairs among the pads read are still checked. A hole in the unread part was requested by an earlier fetch, at least
    once (every fetch is one transaction from the declared start or a counted bar, decision tj-7zjov1.120); that is
    the accepted page residual. The leading gap (before the first stored bar of a dataset with no real bar) is NOT a
    hole, by the same decision: the first fetch asked for it and the vendor answered empty there.

    These rules do not interact badly: the lost anchor returns before the pad rules; the truncation only removes a
    pairing; the counting rule only removes bars; and the unsettled rule is independent of all of them.
    """
    pads = sorted(
        (p for p in placeholders if _can_be_real(p.timestamp, p.written_at, row, calendar, now, placeholder=True)),
        key=lambda p: _as_aware(p.timestamp),
    )
    real, real_written_at = row.last_bar, row.last_bar_written_at
    if real is not None and not _can_be_real(real, real_written_at, row, calendar, now, placeholder=False):
        if not _written_early_inside_range(row):
            if lost_anchor is None:
                return row.start
            # The newest counted real bar stands in for the newest real bar; None means pads only.
            counted = lost_anchor.bar
            real, real_written_at = (None, None) if counted is None else (counted.timestamp, counted.written_at)
        else:
            margin = settle_margin_from_env()
            floor = local_midnight(
                period_start(session_of(_as_aware(real_written_at), calendar), row.granularity), calendar
            )
            candidates = [
                floor,
                *(p.timestamp for p in pads if _placeholder_unsettled(p, calendar, row.granularity, margin)),
            ]
            earliest = min(candidates, key=_as_aware)
            return row.start if _as_aware(earliest) < _as_aware(row.start) else earliest
    stored = [bar for bar in (real, *(p.timestamp for p in pads)) if bar is not None]
    if not stored:
        return row.start
    margin = settle_margin_from_env()
    starts = [_after_bar(max(stored, key=_as_aware), row.granularity, calendar)]
    if real is not None and last_bar_is_provisional(real, real_written_at, calendar, row.granularity, margin):
        starts.append(real)
    starts.extend(p.timestamp for p in pads if _placeholder_unsettled(p, calendar, row.granularity, margin))
    after_real = [p.timestamp for p in pads if real is None or _as_aware(p.timestamp) > _as_aware(real)]
    # A truncated read: the real bar is not the neighbour of the oldest pad read (see TRUNCATED READ above).
    run = [real, *after_real] if real is not None and not truncated else after_real
    for earlier, later in pairwise(run):
        if _skips_a_period(earlier, later, row.granularity, calendar):
            starts.append(_after_bar(earlier, row.granularity, calendar))
            break
    start = min(starts, key=_as_aware)
    return row.start if _as_aware(start) < _as_aware(row.start) else start


def _after_bar(last_bar: datetime, granularity: Granularity, calendar: TradingCalendar) -> datetime:
    """The first instant after the bar's period, where the next bar can be stamped.

    A 1day or 1week bar is stamped at a LOCAL midnight, which is 05:00Z in winter and 04:00Z in summer, so a fixed
    offset from a winter stamp can land an hour AFTER the next summer stamp (a 1week bar of Monday 2026-03-02
    05:00Z plus 7 days is 2026-03-09 05:00Z, past the next bar's 04:00Z), and a vendor that filters on
    timestamp >= start would drop that bar. So those two step by local date to the next local midnight. The
    vendor may stamp a week on any day of it, so the week is taken from its Monday. Shorter bars are absolute
    durations, and a month's 4-week offset always lands before the next month's stamp.
    """
    if granularity is Granularity.ONE_DAY:
        return local_midnight(session_of(last_bar, calendar) + timedelta(days=1), calendar)
    if granularity is Granularity.ONE_WEEK:
        day = session_of(last_bar, calendar)
        return local_midnight(day - timedelta(days=day.weekday()) + timedelta(weeks=1), calendar)
    return last_bar + granularity.offset


def _request_for(
    row: CatalogRow,
    calendar: TradingCalendar,
    placeholders: Sequence[PlaceholderBar] = (),
    now: datetime | None = None,
    *,
    truncated: bool = False,
    lost_anchor: LostAnchor | None = None,
) -> tuple[StoreAssetDatasetPath, StoreAssetDatasetBody, datetime]:
    """The request a dataset's refresh sends: its path, its body and the start of the tail it asks ingest for.

    Raises:
        ValidationError: The stored row no longer validates as a request.
    """
    fetch_start = _fetch_start(row, calendar, placeholders, now, truncated=truncated, lost_anchor=lost_anchor)
    # The entry's own identity, so ingest's ack resolves to THIS entry. The stored tape is named as the
    # preference: a tape ingest can no longer serve is a refused ack, never a second entry.
    body = StoreAssetDatasetBody(
        owner=row.owner,
        source=row.source,
        feed=row.feed,
        granularity=row.granularity,
        start=row.start,
        end=row.end,
        expiry=row.expiry,
        expiry_type=row.expiry_type,
        update_type=row.update_type,
    )
    path = StoreAssetDatasetPath(asset_type=row.asset_type, data_type=row.data_type, asset_symbol=row.asset_symbol)
    return path, body, fetch_start


def _request_key(
    asset_symbol: str | None, asset_type: object, data_type: object, granularity: object, start: datetime | None
):
    """What identifies a request in the history when its run never learned a dataset id (it failed before the ack)."""
    return (asset_symbol, asset_type, data_type, granularity, start)


def _unproductive(run: IngestRunRecord, fetch_start: datetime) -> bool:
    """Whether a finished run asked for this same tail and got nothing a retry would change.

    EMPTY (the vendor held no bars) or FAILED with a typed reason that is REFUSED in the vocabulary. A run whose
    tail differs (a bar has arrived since) says nothing about this one, and an untyped or NOT_READY failure is
    transient.
    """
    if run.requested_start is None or _as_aware(run.requested_start) != _as_aware(fetch_start):
        return False
    if run.state is IngestRunState.EMPTY:
        return True
    if run.state is not IngestRunState.FAILED:
        return False
    # A typed refusal of the legacy run record, or a request the vendor refused for good (its failure cause, read
    # with the run: an attempt row carries only the sentence, not a typed reason).
    return (run.error_domain == ERROR_DOMAIN and run.error_reason in PERMANENT_REASONS) or (
        run.request_failure_cause == IngestRequestFailureCause.PERMANENT
    )


def _last_close(calendar: TradingCalendar, now: datetime) -> datetime | None:
    """The close of the last completed session at `now`, or None when the calendar has none."""
    session = calendar.last_completed_session(now)
    return None if session is None else calendar.session_close(session)


@dataclass(frozen=True)
class PlaceholderTail:
    """The placeholders read for one dataset, newest first, and whether older ones were left unread.

    Attributes:
        bars: The placeholders read.
        truncated: True when the read stopped on a full page ending in a settled bar, so older placeholders may exist.
        lost_anchor: The newest counted real bar, read only when the newest real bar does not count and the tail
            cannot be bounded by its write time; None when it was not read.
    """

    bars: list[PlaceholderBar]
    truncated: bool = False
    lost_anchor: LostAnchor | None = None


def _needs_lost_anchor(row: CatalogRow, calendar: TradingCalendar) -> bool:
    """Whether the newest real bar does not count and its write time cannot bound the tail (tj-7zjov1.122)."""
    return (
        row.last_bar is not None
        and not _can_be_real(row.last_bar, row.last_bar_written_at, row, calendar, None, placeholder=False)
        and not _written_early_inside_range(row)
    )


async def _read_placeholder_tails(
    owed: list[CatalogRow], calendar: TradingCalendar
) -> dict[uuid.UUID, PlaceholderTail]:
    """Each owed dataset's stored placeholders that the tail rule needs (newest first), once per pass.

    Read from `PLACEHOLDER_LOOKBACK` before the newest real bar (the whole history when there is none), in pages of
    `PLACEHOLDER_SCAN_LIMIT`, newest first. The next, older page is read only while the page just read ends in a
    placeholder that is still UNSETTLED, so the unsettled ones are all read however many bars a session holds (an
    extended-hours 1min series has about 960 a session) and a long settled history is not. A live series with no
    padding reads nothing. When the read stops on a full page the tail says `truncated`.

    There is NO iteration cap: a cap would silently drop pages and leave an unsettled pad unread. Instead a PROGRESS
    GUARD raises when a full page does not end strictly older than where it started, so a defect (a repeated page)
    fails loudly instead of hanging the pass.

    LOST ANCHOR (tj-7zjov1.122). When the newest real bar does not count and its write time cannot bound the tail
    (`_needs_lost_anchor`, judged without `now`: a bar written at or after its stamp was stamped no later than its write
    time, which is past, so the stamp-before-now test adds nothing), the newest counted real bar is read first, in ONE extra statement for that dataset only,
    and the placeholder lookback hangs from it. Otherwise the lookback hangs from the earlier of the newest real
    bar's stamp and its write time, so a stamp far in the future (a phantom bar) never leaves unsettled pads unread.

    Raises:
        RuntimeError: A full page did not end strictly older than the bar it was read before.
    """
    if not owed:
        return {}
    margin = settle_margin_from_env()
    tails: dict[uuid.UUID, PlaceholderTail] = {}
    async with own_session() as session:
        for row in owed:
            anchor: LostAnchor | None = None
            if _needs_lost_anchor(row, calendar):
                anchor = LostAnchor(await newest_counted_real_bar(session, row))
                reference = None if anchor.bar is None else anchor.bar.timestamp
            elif row.last_bar is None:
                reference = None
            elif row.last_bar_written_at is None:
                reference = row.last_bar
            else:
                reference = min(_as_aware(row.last_bar), _as_aware(row.last_bar_written_at))
            floor = None if reference is None else reference - PLACEHOLDER_LOOKBACK
            found: list[PlaceholderBar] = []
            before: datetime | None = None
            truncated = False
            while True:
                page = await recent_placeholder_bars(
                    session, row, not_before=floor, limit=PLACEHOLDER_SCAN_LIMIT, before=before
                )
                found.extend(page)
                if len(page) < PLACEHOLDER_SCAN_LIMIT:
                    break  # reached the floor or the first bar: nothing older exists
                ceiling = page[0].timestamp if before is None else before
                if _as_aware(page[-1].timestamp) >= _as_aware(ceiling):
                    raise RuntimeError(
                        'The placeholder read made no progress: a full page did not end older than it began'
                    )
                if not _placeholder_unsettled(page[-1], calendar, row.granularity, margin):
                    truncated = True
                    break
                before = page[-1].timestamp
            tails[row.id] = PlaceholderTail(found, truncated, anchor)
    return tails


async def _tables_exist(session: AsyncSession) -> bool:
    """Whether the tables a pass reads are there; a probe that cannot run answers True and the pass says why."""
    wanted = (StoreDatasetEntry.__tablename__, IngestRun.__tablename__)
    try:
        connection = await session.connection()
        return await connection.run_sync(lambda sync: all(inspect(sync).has_table(name) for name in wanted))
    except Exception:
        await session.rollback()
        return True


def _warn_unmigrated() -> None:
    global _unmigrated_warned
    if not _unmigrated_warned:
        _unmigrated_warned = True
        log.warning(
            'Daily refresh skipped: the store tables do not exist; the database has not been migrated, run make migrate'
        )


def is_enabled() -> bool:
    """Whether the refresh runs. Anything but an explicit false (`false`, `0`, `no`, `off`) leaves it on."""
    return os.environ.get(ENABLED_ENV, 'true').strip().lower() not in ('false', '0', 'no', 'off')


def _positive_int(name: str, default: int) -> int:
    value = get_env_var(name, default=default, cast_type=int)
    return value if value > 0 else default


def settle_delay() -> timedelta:
    """The wait after a session's close before the pass; zero is allowed, a negative or malformed value is the default."""
    return settle_margin_from_env()


def max_per_pass() -> int:
    """How many datasets one pass refreshes at most; a non-positive or malformed value is the default."""
    return _positive_int(MAX_PER_PASS_ENV, DEFAULT_MAX_PER_PASS)


def backoff_base_seconds() -> int:
    """The wait after a first failure, doubled for each further one in a row (capped at BACKOFF_CAP_SECONDS)."""
    return _positive_int(BACKOFF_ENV, DEFAULT_BACKOFF_SECONDS)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class PassResult:
    """What one pass did. Counts only: no owner, no dataset name."""

    behind: int = 0  # DAILY datasets of the source that read LATE or OVERDUE and were not running
    refreshed: int = 0
    failed: int = 0  # TRANSIENT failures only; each waits for the next pass
    deferred: int = 0  # behind and due, but not requested: over the cap, or the pass stopped early
    refused: int = 0  # requested and permanently refused this pass; not tried again before the next close
    skipped: int = 0  # not requested: empty or refused at their last attempt since the last close, or invalid


@dataclass(frozen=True)
class _Planned:
    """One dataset the pass may request: its row, the request it would send and what the history says."""

    row: CatalogRow
    path: StoreAssetDatasetPath
    body: StoreAssetDatasetBody
    fetch_start: datetime
    last_attempt: IngestRunRecord | None

    @property
    def order(self) -> tuple[bool, datetime, datetime, str]:
        """Never attempted first, then least recently attempted, then oldest newest-bar, then id."""
        started = (
            _as_aware(self.last_attempt.started_at)
            if self.last_attempt is not None
            else datetime.min.replace(tzinfo=UTC)
        )
        return (
            self.last_attempt is not None,
            started,
            _as_aware(self.row.last_bar or self.row.start),
            str(self.row.id),
        )


class DailyRefresher:
    """One source's refresh pass, over the Submit client the lifespan built.

    The pass WRITES AND SUBMITS a request per owed dataset and does not wait for it: the result is pushed to the
    store's delivery service, which writes the bars; a dataset with an open request is not owed again.

    Args:
        client: The Submit seam, as the interface.
        clock: The current aware instant.
        sleep: Sleeps for seconds; injected so a test does not wait out a backoff.
    """

    def __init__(
        self,
        client: IngestSubmitClient,
        *,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._clock = clock
        self._sleep = sleep

    async def run_pass(self, source: DataSource, calendar: TradingCalendar | None = None) -> PassResult:
        """Refresh the DAILY datasets of one source that are behind. Idempotent: the database says what is owed.

        Args:
            source: The vendor whose datasets this pass refreshes.
            calendar: The source's trading calendar; looked up when omitted.

        Returns:
            PassResult: The counts. All zero when nothing is behind.
        """
        now = self._clock()
        calendar = calendar or calendar_for_source(source)
        async with own_session() as session:
            if not await _tables_exist(session):
                _warn_unmigrated()
                return PassResult()
            rows = await list_catalog(session, CatalogFilter(source=source, update_type=UpdateType.DAILY))
            running = await running_dataset_ids(session)
            history = await finished_scheduled_runs(session, source, now - ATTEMPT_LOOKBACK)

        owed = self._behind(rows, running, calendar, now)
        tails = await _read_placeholder_tails(owed, calendar)
        due, skipped = self._plan(owed, history, _last_close(calendar, now), calendar, tails, now)
        batch = self._take(due, max_per_pass())
        refreshed = failed = refused = 0
        deferred = len(due[0]) + len(due[1]) - len(batch)
        consecutive = 0
        ready_by = next_market_open(calendar, now)
        for position, planned in enumerate(batch):
            try:
                await self._refresh(planned, ready_by)
            except Exception as error:
                if is_permanent(error):
                    # Cannot succeed on retry, and the peer answered: no stop, no backoff.
                    refused += 1
                    continue
                # The type only: a message can carry a bound parameter, and the owner is sensitive.
                log.error(f'Daily refresh could not refresh a dataset: {type(error).__name__}')
                failed += 1
                consecutive += 1
                if getattr(error, 'reset_at', None) is not None:
                    # A rate limit names its own reset: more calls now would only be refused.
                    deferred += len(batch) - position - 1
                    break
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    deferred += len(batch) - position - 1
                    break
                await self._sleep(min(backoff_base_seconds() * 2 ** (consecutive - 1), BACKOFF_CAP_SECONDS))
                continue
            consecutive = 0
            refreshed += 1
        if skipped:
            log.info(f'Daily refresh: {skipped} dataset(s) not requested: refused or empty since the last close')
        if owed:
            log.info(
                f'Daily refresh: {len(owed)} dataset(s) behind, {refreshed} requested, {failed} failed, '
                f'{refused} refused, {deferred} left for the next pass'
            )
        return PassResult(
            behind=len(owed), refreshed=refreshed, failed=failed, deferred=deferred, refused=refused, skipped=skipped
        )

    @staticmethod
    def _plan(
        owed: list[CatalogRow],
        history: list[IngestRunRecord],
        last_close: datetime | None,
        calendar: TradingCalendar,
        tails: dict[uuid.UUID, PlaceholderTail] | None = None,
        now: datetime | None = None,
    ) -> tuple[tuple[list[_Planned], list[_Planned]], int]:
        """Split the behind datasets into ((productive, due-again), skipped_count), each list in request order.

        The history is the database's: the latest finished SCHEDULE run per dataset, and, for a run that failed
        before its acknowledgement (no dataset id), per the request's own fields. A dataset whose last attempt
        asked for the same tail and ended EMPTY or in a permanent failure is `due again` once a session has closed
        since, and skipped until then.
        """
        by_dataset: dict[uuid.UUID, IngestRunRecord] = {}
        by_request: dict[tuple, IngestRunRecord] = {}
        for run in history:  # ascending: a later run replaces an earlier one
            if run.dataset_id is not None:
                by_dataset[run.dataset_id] = run
            else:
                by_request[
                    _request_key(run.asset_symbol, run.asset_type, run.data_type, run.granularity, run.requested_start)
                ] = run

        productive: list[_Planned] = []
        again: list[_Planned] = []
        skipped = 0
        for row in owed:
            try:
                tail = (tails or {}).get(row.id, PlaceholderTail([]))
                path, body, fetch_start = _request_for(
                    row, calendar, tail.bars, now, truncated=tail.truncated, lost_anchor=tail.lost_anchor
                )
            except ValidationError:
                # A stored row that no longer validates as a request: never sent, and nothing to record.
                skipped += 1
                continue
            if now is not None and _as_aware(fetch_start) >= _as_aware(min(effective_end(row, now), now)):
                # Every period up to the declared end (or now) is stored, placeholders included: the tail is empty,
                # and a request that starts at or after its end is no valid request. The end half is a guard: a DAILY
                # row's declared end does not validate as a request today, but the guard costs nothing and keeps an
                # invalid request from ever being sent.
                skipped += 1
                continue
            candidates = [
                by_dataset.get(row.id),
                by_request.get(
                    _request_key(row.asset_symbol, row.asset_type, row.data_type, row.granularity, fetch_start)
                ),
            ]
            attempts = [run for run in candidates if run is not None]
            last = max(attempts, key=lambda run: _as_aware(run.started_at)) if attempts else None
            planned = _Planned(row, path, body, fetch_start, last)
            if last is None or not _unproductive(last, fetch_start):
                productive.append(planned)
            elif last_close is not None and last.finished_at is not None and _as_aware(last.finished_at) >= last_close:
                skipped += 1
            else:
                again.append(planned)
        productive.sort(key=lambda planned: planned.order)
        again.sort(key=lambda planned: planned.order)
        return (productive, again), skipped

    @staticmethod
    def _take(due: tuple[list[_Planned], list[_Planned]], cap: int) -> list[_Planned]:
        """The datasets to request: at most `cap`, the productive ones first, and never one class only.

        The ones due again (empty or refused last time) hold at most half the cap while there are others, so
        a crowd of delisted symbols cannot fill every pass; they take whatever the others leave.
        """
        productive, again = due
        reserved = min(len(again), cap // 2)
        first = productive[: cap - reserved]
        return first + again[: cap - len(first)]

    @staticmethod
    def _behind(
        rows: list[CatalogRow], running: frozenset[uuid.UUID], calendar: TradingCalendar, now: datetime
    ) -> list[CatalogRow]:
        """The rows to refresh, oldest newest-bar first: DAILY, active, not running, and LATE or OVERDUE."""
        owed: list[CatalogRow] = []
        # Only the settle margin, from the refresh's own setting: the stream multiple means nothing to a DAILY dataset.
        config = HealthConfig(settle_margin=settle_margin_from_env())
        for row in rows:
            if is_retired(row) or row.id in running:
                continue
            try:
                health = evaluate_dataset(
                    update_type=row.update_type,
                    granularity=row.granularity,
                    collect=row.collect,
                    start=row.start,
                    end=effective_end(row, now),
                    last_bar=row.last_bar,
                    covered=None,
                    calendar=calendar,
                    now=now,
                    last_bar_written_at=row.last_bar_written_at,
                    config=config,
                )
            except CalendarRangeError:
                continue
            if health.status in (DatasetHealthStatus.LATE, DatasetHealthStatus.OVERDUE):
                owed.append(row)
        owed.sort(key=lambda row: (row.last_bar or row.start, str(row.id)))
        return owed

    async def _refresh(self, planned: _Planned, ready_by: datetime | None) -> None:
        """Write and submit one dataset's missing-tail request, trigger SCHEDULE, and do not wait for it.

        The request names the dataset (dataset_id), so the first delivered attempt refreshes that entry and never
        writes a lifecycle field; a dataset stopped or deleted before the delivery lands has the answer DROPPED by
        the intake (the request finishes EMPTY, cause DISCARDED). Its deadline is the next market open.

        Args:
            planned: The dataset, its request and its tail.
            ready_by: The next market open, or None when the calendar has none to name.

        Raises:
            TraderJoeError: A refusal or a failed call; a permanent one is counted by the pass, a transient one backs
                it off. A request whose call failed stays PENDING for the instance watcher.
        """
        async with own_session() as session:
            await submit_bars(
                planned.path,
                planned.body,
                session,
                self._client,
                trigger=IngestRunTrigger.SCHEDULE,
                client_label=SCHEDULER_ACTOR,
                start=planned.fetch_start,
                ready_by=ready_by,
                dataset_id=planned.row.id,
            )


def next_market_open(calendar: TradingCalendar, now: datetime) -> datetime | None:
    """The open of the first session that starts after `now`: the DAILY loop's ready_by (ADR addendum 2026-10-24 (1) 3).

    Args:
        calendar: The source's trading calendar.
        now: The current aware instant.

    Returns:
        datetime | None: The open, or None when the calendar cannot name one (a deadline-free request then).
    """
    try:
        base = calendar.session_in_progress(now) or calendar.last_completed_session(now)
        if base is None:
            return None
        session = calendar.next_session(base)
        while calendar.session_open(session) <= now:
            session = calendar.next_session(session)
        return calendar.session_open(session)
    except CalendarRangeError:
        return None


async def start_daily_refresh_loops(client: IngestSubmitClient) -> list[asyncio.Task[None]]:
    """Start one refresh loop per source that has a calendar. Call from the running loop, after the client exists.

    Each loop's first pass runs at once (the catch-up), then at every session close plus the settle delay.

    Args:
        client: The seam the refresh submits through.

    Returns:
        list[asyncio.Task[None]]: The tasks, for stop_daily_refresh_loops; empty when DAILY_REFRESH_ENABLED is off.
    """
    if not is_enabled():
        log.info(f'Daily refresh is off ({ENABLED_ENV}); DAILY datasets are not refreshed by the store')
        return []
    refresher = DailyRefresher(client)
    tasks: list[asyncio.Task[None]] = []
    for source in SOURCE_CALENDARS:
        # Building a calendar is slow (cached after the first): off the event loop.
        try:
            calendar = await asyncio.to_thread(calendar_for_source, source)
        except UnknownCalendarError:
            continue
        tasks.append(_start_source_loop(refresher, source, calendar))
    return tasks


def _start_source_loop(refresher: DailyRefresher, source: DataSource, calendar: TradingCalendar) -> asyncio.Task[None]:
    # Set at creation: the runner never replays, so the start-up catch-up is this wake.
    wake = asyncio.Event()
    wake.set()

    async def job() -> None:
        await refresher.run_pass(source, calendar)

    return run_periodic(
        f'daily-refresh-{source.value}',
        AfterTradingEvent(calendar, TradingEvent.CLOSE, settle_delay()),
        job,
        wake=wake,
        log=log,
    )


async def stop_daily_refresh_loops(tasks: list[asyncio.Task[None]]) -> None:
    """Cancel the loops and await them. Call before the fetch channel and the database go."""
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
