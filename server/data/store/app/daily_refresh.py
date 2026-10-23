"""THE DAILY REFRESH: the store's INTERIM scheduler for DAILY subscriptions (tj-grna9p.22).

INTERIM, BY RULING (tj-utqd77 Q1, "interim store loop"). The scheduler that belongs to ingest (tj-pd41mt Addendum 1)
is not built. Until it lands the STORE triggers the refresh, and ingest sees an ORDINARY FetchDataset request like any
other: no new message type, no request queue (tj-grna9p.47 is deferred, tj-grna9p.21 is not built). When ingest's
scheduler lands this module is removed, not extended.

WHEN. One periodic runner per source that has a trading calendar (SOURCE_CALENDARS), on AfterTradingEvent(calendar,
CLOSE, settle): a session's close plus DAILY_REFRESH_SETTLE_MINUTES, from the store's own calendar for that source.
The calendar is an argument to the schedule; common imports none. A pass also runs once at start (catch-up).

WHAT. Every pass derives its work from the DATABASE; nothing is stored and nothing is replayed (common/scheduling.py).
It reads the DAILY entries of its source, drops the retired ones (an expiry on a DAILY dataset retires it at once,
the same rule the UI judges by: is_retired), drops those with a RUNNING ingest, and keeps the ones the freshness rule
reads LATE or OVERDUE: the last stored bar is before the last completed session. For each, oldest first, it asks
ingest for ONLY the missing range: from the last stored bar plus one bar (the declared start when there are no
bars; from the last bar ITSELF when it was written before its period closed, so the vendor's part-way bar is
overwritten by the final one, tj-grna9p.167), through the same fetch path POST /store uses (store_market_activity_worker, through the run recorder with
trigger SCHEDULE and actor `scheduler`). The entry keeps its declared range as identity, so the refresh extends the
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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import DataSource, UpdateType
from common.environment import get_env_var
from common.errors.vocabulary import ERROR_DOMAIN, REASONS, Outcome, TraderJoeError
from common.logging import get_logger
from common.rpc.clients.ingest_fetch import IngestFetchClient
from common.scheduling import AfterTradingEvent, TradingEvent, run_periodic
from data.store.app.database.crud.ingest_run import IngestRunRecord, finished_scheduled_runs, running_dataset_ids
from data.store.app.database.crud.stock.dataset_catalog import CatalogFilter, CatalogRow, list_catalog
from data.store.app.database.database import own_session
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.dataset_catalog import effective_end, is_retired
from data.store.app.events import DatasetChange, DatasetChanged, publish
from data.store.app.health import FreshnessStatus, evaluate_dataset, last_bar_is_provisional
from data.store.app.ingest.data_action_request import DatasetRetiredDuringFetch, store_market_activity_worker
from data.store.app.ingest.run_recording import recorded_run
from data.store.app.trading_calendar import (
    SOURCE_CALENDARS,
    CalendarRangeError,
    TradingCalendar,
    UnknownCalendarError,
    calendar_for_source,
)
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath


log = get_logger(__name__)

ENABLED_ENV: Final = 'DAILY_REFRESH_ENABLED'
SETTLE_ENV: Final = 'DAILY_REFRESH_SETTLE_MINUTES'
MAX_PER_PASS_ENV: Final = 'DAILY_REFRESH_MAX_PER_PASS'
BACKOFF_ENV: Final = 'DAILY_REFRESH_BACKOFF_SECONDS'

DEFAULT_SETTLE_MINUTES: Final = 30
DEFAULT_MAX_PER_PASS: Final = 10
DEFAULT_BACKOFF_SECONDS: Final = 30

BACKOFF_CAP_SECONDS: Final = 600
MAX_CONSECUTIVE_FAILURES: Final = 3

# The run's actor: a system action, whatever dataset owner it works for.
SCHEDULER_ACTOR: Final = 'scheduler'

# How far back the run history is read for the order and the skip rule. A dataset not attempted within it counts
# as never attempted, which only puts it earlier.
ATTEMPT_LOOKBACK: Final = timedelta(days=7)

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


def _fetch_start(row: CatalogRow, calendar: TradingCalendar) -> datetime:
    """Where the refresh's tail starts: the declared start with no bars, else just after the newest bar.

    A newest bar written before its period closed may be the vendor's part-way bar (see last_bar_is_provisional), so
    the tail starts AT that bar and the upsert overwrites it with the final one. Once it is rewritten after the close
    it is final and the tail starts after it again.
    """
    if row.last_bar is None:
        return row.start
    if last_bar_is_provisional(row.last_bar, row.last_bar_written_at, calendar, row.granularity):
        return row.last_bar
    return row.last_bar + row.granularity.offset


def _request_for(
    row: CatalogRow, calendar: TradingCalendar
) -> tuple[StoreAssetDatasetPath, StoreAssetDatasetBody, datetime]:
    """The request a dataset's refresh sends: its path, its body and the start of the tail it asks ingest for.

    Raises:
        ValidationError: The stored row no longer validates as a request.
    """
    fetch_start = _fetch_start(row, calendar)
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


def _request_key(asset_symbol: str, asset_type: object, data_type: object, granularity: object, start: datetime | None):
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
    return (
        run.state is IngestRunState.FAILED
        and run.error_domain == ERROR_DOMAIN
        and run.error_reason in PERMANENT_REASONS
    )


def _last_close(calendar: TradingCalendar, now: datetime) -> datetime | None:
    """The close of the last completed session at `now`, or None when the calendar has none."""
    session = calendar.last_completed_session(now)
    return None if session is None else calendar.session_close(session)


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
    minutes = get_env_var(SETTLE_ENV, default=DEFAULT_SETTLE_MINUTES, cast_type=int)
    return timedelta(minutes=minutes if minutes >= 0 else DEFAULT_SETTLE_MINUTES)


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
    discarded: int = 0  # retired or deleted while the vendor call was in flight; the answer was dropped


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
    """One source's refresh pass, over the fetch client the lifespan built.

    Args:
        fetch_client: The FetchDataset seam, as the interface.
        clock: The current aware instant.
        sleep: Sleeps for seconds; injected so a test does not wait out a backoff.
    """

    def __init__(
        self,
        fetch_client: IngestFetchClient,
        *,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = fetch_client
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
        due, skipped = self._plan(owed, history, _last_close(calendar, now), calendar)
        batch = self._take(due, max_per_pass())
        refreshed = failed = refused = discarded = 0
        deferred = len(due[0]) + len(due[1]) - len(batch)
        consecutive = 0
        for position, planned in enumerate(batch):
            try:
                kept = await self._refresh(planned)
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
            if kept:
                refreshed += 1
            else:
                discarded += 1
        if skipped:
            log.info(f'Daily refresh: {skipped} dataset(s) not requested: refused or empty since the last close')
        if discarded:
            log.warning(f'Daily refresh: {discarded} answer(s) dropped, the dataset was retired during the fetch')
        if owed:
            log.info(
                f'Daily refresh: {len(owed)} dataset(s) behind, {refreshed} refreshed, {failed} failed, '
                f'{refused} refused, {deferred} left for the next pass'
            )
        return PassResult(
            behind=len(owed),
            refreshed=refreshed,
            failed=failed,
            deferred=deferred,
            refused=refused,
            skipped=skipped,
            discarded=discarded,
        )

    @staticmethod
    def _plan(
        owed: list[CatalogRow], history: list[IngestRunRecord], last_close: datetime | None, calendar: TradingCalendar
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
                path, body, fetch_start = _request_for(row, calendar)
            except ValidationError:
                # A stored row that no longer validates as a request: never sent, and nothing to record.
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
        for row in rows:
            if is_retired(row) or row.id in running:
                continue
            try:
                health = evaluate_dataset(
                    update_type=row.update_type,
                    granularity=row.granularity,
                    expiry=row.expiry,
                    start=row.start,
                    end=effective_end(row, now),
                    last_bar=row.last_bar,
                    covered=None,
                    calendar=calendar,
                    now=now,
                    last_bar_written_at=row.last_bar_written_at,
                )
            except CalendarRangeError:
                continue
            if health.status in (FreshnessStatus.LATE, FreshnessStatus.OVERDUE):
                owed.append(row)
        owed.sort(key=lambda row: (row.last_bar or row.start, str(row.id)))
        return owed

    async def _refresh(self, planned: _Planned) -> bool:
        """Fetch one dataset's missing tail through the store's own fetch path, recorded as a SCHEDULE run.

        Returns:
            bool: True when the answer was stored; False when the dataset was retired (or deleted) during the
            fetch and the answer was dropped. That run is recorded EMPTY with no bars, never FAILED.
        """
        path, body = planned.path, planned.body
        async with recorded_run(
            path, body, trigger=IngestRunTrigger.SCHEDULE, actor=SCHEDULER_ACTOR, requested_start=planned.fetch_start
        ) as run:
            try:
                async with own_session() as session:
                    stored = await store_market_activity_worker(
                        path,
                        body,
                        session,
                        self._client,
                        on_ack=run.acknowledge,
                        fetch_start=planned.fetch_start,
                        preserve_lifecycle=True,
                    )
            except DatasetRetiredDuringFetch:
                await run.succeed(0)
                return False
            await run.succeed(stored.data_points)
        # After the fetch's commit, never inside it.
        if run.dataset_id is not None:
            publish(DatasetChanged(run.dataset_id, DatasetChange.UPDATED))
        return True


async def start_daily_refresh_loops(fetch_client: IngestFetchClient) -> list[asyncio.Task[None]]:
    """Start one refresh loop per source that has a calendar. Call from the running loop, after the client exists.

    Each loop's first pass runs at once (the catch-up), then at every session close plus the settle delay.

    Args:
        fetch_client: The seam the refresh fetches through.

    Returns:
        list[asyncio.Task[None]]: The tasks, for stop_daily_refresh_loops; empty when DAILY_REFRESH_ENABLED is off.
    """
    if not is_enabled():
        log.info(f'Daily refresh is off ({ENABLED_ENV}); DAILY datasets are not refreshed by the store')
        return []
    refresher = DailyRefresher(fetch_client)
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
