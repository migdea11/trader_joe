"""THE ASSET-LIST SNAPSHOT SCHEDULE: when the store asks ingest for the vendor's asset lists (tj-7zjov1.138, P47).

Design: owner ruling on tj-7zjov1.117 (2026-10-25), ADR tj-26jt9w addenda 5, 9 a, 10, 11 and 17.

This module decides WHEN and WHICH list. It writes no snapshot: each request is written through new_asset_list_request
(trigger SCHEDULE, actor scheduler, no ready_by), sent through the submit path, and NOT waited for; the intake's
asset-list arm writes the snapshot inside the delivery transaction.

PER SOURCE, NOTHING HARD-CODED TO ONE VENDOR. CADENCES maps a source to its SnapshotCadence, and one loop runs per source
that has a trading calendar, exactly as the DAILY refresh does. Today's cadence for every source is the Alpaca one: once
when an ingest instance starts or restarts, then daily at the session close plus the settle margin of the source's
calendar. A source that needs another cadence (a Canadian list may be monthly) gets its own SnapshotCadence.

WHEN, per source.
  INGEST STARTED OR RESTARTED   the instance watcher's probe names an instance id this schedule has not handled: the
                                ACTIVE list is requested at once. The handled id is seeded at start from the newest
                                instance the store already knows, so a store restart does not count as an ingest restart.
  DAILY                         at the close plus the settle margin, and once at start (the catch-up): the ACTIVE list is
                                requested when the newest non-empty active snapshot is older than the most recent
                                due time. One rule serves both, so a store that was down when a snapshot was due asks at
                                once and a store that was not does nothing.
  INACTIVE LIST                 with any active request, also when no non-empty inactive snapshot exists or the newest is
                                inactive_max_age (7 days) old or more. A fixed age rule, no setting.

IN FLIGHT. No request for a list while an asset_list request for that (source, list) is open (open_asset_list_request_exists),
so a restart never stacks them. A second same-day snapshot from a restart is allowed. One lock serialises the check and the
write: the daily loop and the instance hook share this process.

KILL SWITCH. ASSET_SNAPSHOT_ENABLED, ON by default, off only for false, 0, no or off; read at start. Independent of the
DAILY refresh and of the world manager.

ONE PROCESS: correct only while data_store runs one worker, like the refresh and the reaper.

NOTHING IN A LOG NAMES AN OWNER OR A SYMBOL: sources, lists, ids and exception types only.
"""

import asyncio
import contextlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource
from common.environment import get_env_var
from common.errors.vocabulary import TraderJoeError
from common.logging import get_logger
from common.rpc.clients.ingest_submit import IngestSubmitClient
from common.scheduling import AfterTradingEvent, TradingEvent, run_periodic
from data.store.app.daily_refresh import SCHEDULER_ACTOR, settle_delay
from data.store.app.database.crud.asset_list import newest_snapshot
from data.store.app.database.crud.ingest_instance import ingest_instance_table_exists, live_instance_id
from data.store.app.database.crud.ingest_request import create_request, open_asset_list_request_exists
from data.store.app.database.database import own_session
from data.store.app.database.models.ingest_run import IngestRunTrigger
from data.store.app.ingest.data_action_request import new_asset_list_request, submit_record
from data.store.app.trading_calendar import SOURCE_CALENDARS, TradingCalendar, UnknownCalendarError, calendar_for_source
from schemas.data_ingest.submit import AssetListStatus


log = get_logger(__name__)

ENABLED_ENV: Final = 'ASSET_SNAPSHOT_ENABLED'

_FALSE: Final = ('false', '0', 'no', 'off')

# The inactive list is asked for again once its newest non-empty snapshot is this old.
DEFAULT_INACTIVE_MAX_AGE: Final = timedelta(days=7)


@dataclass(frozen=True)
class SnapshotCadence:
    """How often one source's asset lists are asked for. A parameter per source, so the cadences may differ later.

    Attributes:
        asset_type: The asset type of the lists asked for.
        at_ingest_start: Whether an ingest instance starting or restarting asks for the active list.
        daily_offset: The wait after each session's close before the daily request, or None for no daily request.
        inactive_max_age: The age at which the inactive list is asked for again alongside an active request.
    """

    asset_type: AssetType = AssetType.STOCK
    at_ingest_start: bool = True
    daily_offset: timedelta | None = field(default_factory=settle_delay)
    inactive_max_age: timedelta = DEFAULT_INACTIVE_MAX_AGE


def default_cadences() -> dict[DataSource, SnapshotCadence]:
    """Today's cadence (the Alpaca one) for every source that has a trading calendar."""
    return {source: SnapshotCadence() for source in SOURCE_CALENDARS}


def is_enabled() -> bool:
    """Whether the schedule runs. Anything but an explicit false (`false`, `0`, `no`, `off`) leaves it on."""
    return get_env_var(ENABLED_ENV, default='true', cast_type=str).strip().lower() not in _FALSE


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AssetSnapshotSchedule:
    """Decides and writes the asset-list requests, over the Submit client the lifespan built.

    Args:
        client: The Submit seam, as the interface.
        cadences: The cadence of each source that is scheduled.
        calendars: The trading calendar of each source with a daily request.
        clock: The current aware instant; injected so a test does not wait.
    """

    def __init__(
        self,
        client: IngestSubmitClient,
        cadences: Mapping[DataSource, SnapshotCadence],
        calendars: Mapping[DataSource, TradingCalendar],
        *,
        clock=_utc_now,
    ) -> None:
        self._client = client
        self._cadences = dict(cadences)
        self._calendars = dict(calendars)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._handled_instance: str | None = None

    async def seed(self) -> None:
        """Take the newest instance the store already knows as handled, so a store restart is not an ingest restart."""
        async with own_session() as db:
            if not await ingest_instance_table_exists(db):
                log.warning(
                    'Asset snapshot seed skipped: the ingest_instance table does not exist; the database has not '
                    'been migrated, run make migrate. The first instance seen after migrating counts as an ingest start'
                )
                return
            self._handled_instance = await live_instance_id(db)

    async def on_instance_seen(self, instance_id: str) -> None:
        """The hook the instance watcher calls after it noted the instance answering its probe.

        An id other than the handled one is an ingest start or restart: the active list is requested for every
        source whose cadence says so, and the inactive one when its age rule says so.

        Args:
            instance_id: The opaque id of the instance that answered.
        """
        if instance_id == self._handled_instance:
            return
        self._handled_instance = instance_id
        for source, cadence in self._cadences.items():
            if not cadence.at_ingest_start:
                log.info(f'Asset snapshot skipped for {source.value}: not due (no request at ingest start)')
                continue
            await self._request_lists(source, cadence, 'ingest started or restarted')

    async def run_daily(self, source: DataSource) -> None:
        """One daily pass (also the catch-up at start): ask when the newest active snapshot predates the due time.

        Args:
            source: The source whose daily request this is.
        """
        cadence = self._cadences[source]
        now = self._clock()
        due = self._last_due(source, cadence, now)
        if due is None:
            log.info(f'Asset snapshot skipped for {source.value}: not due (no daily request or no session yet)')
            return
        async with own_session() as db:
            newest = await newest_snapshot(db, source, AssetListStatus.ACTIVE)
        if newest is not None and newest.as_of >= due:
            log.info(f'Asset snapshot skipped for {source.value}: not due (the newest active snapshot is current)')
            return
        await self._request_lists(source, cadence, 'daily')

    def _last_due(self, source: DataSource, cadence: SnapshotCadence, now: datetime) -> datetime | None:
        """The most recent daily due time at `now` (a session's close plus the offset), or None."""
        calendar = self._calendars.get(source)
        if calendar is None or cadence.daily_offset is None:
            return None
        session = calendar.last_completed_session(now - cadence.daily_offset)
        if session is None:
            return None
        return calendar.session_close(session).astimezone(UTC) + cadence.daily_offset

    async def _request_lists(self, source: DataSource, cadence: SnapshotCadence, reason: str) -> None:
        """Request the active list and, by the age rule, the inactive one. One at a time, guarded against stacking."""
        async with self._lock:
            await self._request(source, cadence, AssetListStatus.ACTIVE, reason)
            async with own_session() as db:
                newest = await newest_snapshot(db, source, AssetListStatus.INACTIVE)
            if newest is None or newest.as_of <= self._clock() - cadence.inactive_max_age:
                await self._request(source, cadence, AssetListStatus.INACTIVE, reason)
            else:
                log.info(f'Asset snapshot skipped for {source.value} inactive list: not due (newest is under age)')

    async def _request(
        self, source: DataSource, cadence: SnapshotCadence, status: AssetListStatus, reason: str
    ) -> None:
        """Write and send one asset-list request unless one for the list is open. Never waits for the verdict."""
        try:
            async with own_session() as db:
                if await open_asset_list_request_exists(db, source, status):
                    log.info(f'Asset snapshot skipped for {source.value} {status.value} list: in flight')
                    return
                record = await create_request(
                    db,
                    new_asset_list_request(
                        source,
                        cadence.asset_type,
                        status,
                        trigger=IngestRunTrigger.SCHEDULE,
                        client_label=SCHEDULER_ACTOR,
                    ),
                )
            await submit_record(record, self._client)
        except TraderJoeError as error:
            # The row stays PENDING (or FAILED when refused): the instance watcher re-sends a pending one.
            log.warning(
                f'Asset snapshot request for {source.value} {status.value} list ({reason}) did not complete: '
                f'{type(error).__name__} {error.reason}'
            )
            return
        log.info(f'Asset snapshot requested for {source.value} {status.value} list ({reason}): request {record.id}')


@dataclass
class AssetSnapshotLoops:
    """What the lifespan holds: the schedule (for the instance hook) and its daily loop tasks (for the stop)."""

    schedule: AssetSnapshotSchedule
    tasks: list[asyncio.Task[None]]


async def start_asset_snapshot_loops(
    client: IngestSubmitClient, cadences: Mapping[DataSource, SnapshotCadence] | None = None
) -> AssetSnapshotLoops | None:
    """Start one daily loop per scheduled source. Call from the running loop, after the Submit client exists.

    Each loop's first pass runs at once (the catch-up), then at every session close plus the offset.

    Args:
        client: The Submit seam the requests go through.
        cadences: The cadence per source; today's default for every source with a calendar when None.

    Returns:
        AssetSnapshotLoops | None: The schedule and tasks, for stop_asset_snapshot_loops; None when ASSET_SNAPSHOT_ENABLED is off.
    """
    if not is_enabled():
        log.info(f'Asset snapshot requests are off ({ENABLED_ENV}); no asset list is requested by the store')
        return None
    cadences = default_cadences() if cadences is None else cadences
    calendars: dict[DataSource, TradingCalendar] = {}
    for source in cadences:
        # Building a calendar is slow (cached after the first): off the event loop.
        try:
            calendars[source] = await asyncio.to_thread(calendar_for_source, source)
        except UnknownCalendarError:
            continue
    schedule = AssetSnapshotSchedule(client, cadences, calendars)
    await schedule.seed()
    tasks = [
        _start_source_loop(schedule, source, calendars[source], cadences[source])
        for source in cadences
        if source in calendars and cadences[source].daily_offset is not None
    ]
    return AssetSnapshotLoops(schedule, tasks)


def _start_source_loop(
    schedule: AssetSnapshotSchedule, source: DataSource, calendar: TradingCalendar, cadence: SnapshotCadence
) -> asyncio.Task[None]:
    # Set at creation: the runner never replays, so the start-up catch-up is this wake.
    wake = asyncio.Event()
    wake.set()

    async def job() -> None:
        await schedule.run_daily(source)

    return run_periodic(
        f'asset-snapshot-{source.value}',
        AfterTradingEvent(calendar, TradingEvent.CLOSE, cadence.daily_offset),
        job,
        wake=wake,
        log=log,
    )


async def stop_asset_snapshot_loops(loops: AssetSnapshotLoops | None) -> None:
    """Cancel the loops and await them. Call before the Submit channel and the database go."""
    if loops is None:
        return
    for task in loops.tasks:
        task.cancel()
    for task in loops.tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
