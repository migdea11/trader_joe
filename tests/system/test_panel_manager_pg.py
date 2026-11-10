"""The panel manager's OWN pass (tj-7zjov1.34, P22) on the real, migrated Postgres, through asyncpg (P26a, tj-7zjov1.260).

WRITTEN, NOT YET RUN by its author (validator, tj-7zjov1.260: the agent stack's budget was spent, so the orchestrator
runs the confirming session). It runs with the rest of the suite (make test-system, or agent-stack run_system_tests
after test_up) on a stack migrated to the head (b6d1f93a7c42 or later).

THE DESIGN: the consolidated creation ruling (option D) on tj-7zjov1.34 and its 23:18 UTC notes, the 14:02 UTC as_of
rule, the 14:16 UTC rate design, ADR tj-26jt9w addenda 20 and 21, the follow-ups tj-7zjov1.228 (a RELEASE pass row
opens lazily), tj-7zjov1.229 (an unfinished pass holds the next one off for UNFINISHED_PASS_RETRY) and tj-7zjov1.230
(the first pass runs at creation), and the Postgres-proof list carried by P26 tj-7zjov1.38 (notes 23:36 UTC
2026-10-31 and 00:50 UTC 2026-11-01). The SQLite tier (server/data/store/tests/test_panel_manager_*.py) pins the rules;
its StaticPool shares ONE connection, so it proves existence, never commit isolation, and it has no enums, JSONB,
ARRAY, timestamptz or database clock. This module runs the SAME pass on Postgres.

HOW IT IS DRIVEN. The manager is not reachable over the store's REST (no route runs a pass), so it is driven IN
PROCESS, the way test_name_change_clamp_pg.py drives the POST path: a PanelManager built in the test process with an
injected clock and calendar, its own_session (and data_action_request's and delivery_intake's) pointed at an asyncpg
engine on the test database, and a fake Submit client (PlayedIngest) whose reports go through the REAL DeliveryIntake.
The clamp's corporate-actions report is delivered inside the verdict waiter's sleep (so the REAL first_request_clamp
writes, submits, awaits and reads its request); a bars report is delivered only when the test says, because the pass
never awaits it. Everything else is production code: the ledger, settings and request crud, the asset-list reads,
check_own_overlap, stop_collecting, the intake's bars arm minting the entry and setting ingest_request.dataset_id.
The engine carries its own application_name, so pg_stat_activity can say whether any manager connection sits idle IN
a transaction while the clamp waits. A second connection (the sync pg_engine, READ COMMITTED) is the observer of what
is committed.

WHY SOURCE MANUAL_ENTRY. The running data_store's own panel loop ticks every 5 minutes over SOURCE_CALENDARS
(ALPACA_API only). An enabled panel on ALPACA_API would be picked up by it mid-test and its pass would race this
one. MANUAL_ENTRY has no calendar there, so the live loop never sees these panels; the test maps the XNYS calendar in
for MANUAL_ENTRY by patching calendar_for_source in panel_manager and data_action_request ONLY (the declared start and
the clamp's local midnight are then XNYS's, as for ALPACA_API). The manager is handed {MANUAL_ENTRY: XNYS}. Its
enabled_panels_on_source runs for real; the wrapper only drops panels this test did not create, so a stray panel left
by another module cannot be ticked here.

A RESTART is a fresh PanelManager instance on the same database (nothing in memory survives), not an agent-stack
restart of the container: the manager stores nothing, so a new instance is exactly what a restarted process holds.

WHAT EACH TEST PINS:
  PM1 a MEMBERSHIP pass on asyncpg: enabled_panels_on_source, current_source_settings, latest_pass, newest_snapshot of
      both lists (the asset_list_status enum cast), snapshot_rows, latest_state, panel_entries (the SensitiveString
      owner comparison), requests_by_ids, symbols_with_open_requests (the IN over OPEN_STATES), created_in_window and
      requests_in_window, open_pass (panel_pass_kind cast), append_transitions (panel_transition cast), the REAL
      first_request_clamp on its OWN session with no manager connection idle in a transaction, check_own_overlap with
      the CLAMPED start, create_request, finish_pass (JSONB counts); ENTERED+CREATED and the POST-shaped bars request
      become visible to another connection in ONE commit, never before it.
  PM2 the clamp round trip completes: the intake mints each entry (owner panel, the clamped start), sets
      ingest_request.dataset_id, and the next MEMBERSHIP pass (same settings version: snapshots_between and
      rows_of_ids run) sees the entries as existing and creates nothing.
  PM3 a RELEASE tick: a release whose clamp fails again leaves NO pass row and one more corporate-actions request; the
      next one opens its RELEASE pass row lazily and commits it with CREATED and the request row in the one
      create_request commit (none visible during the clamp or before that commit, all three after); a MEMBERSHIP and a
      RELEASE pass started in the same second are ordered at microsecond resolution.
  PM4 a pass crashed before finish_pass leaves finished_at and counts NULL on Postgres; the next pass is held off until
      exactly UNFINISHED_PASS_RETRY after the DATABASE's started_at, measured with the app clock; then it runs and the
      crashed row stays as written.
  PM5 a restart mid-window creates nothing more: the ramp window is read from rows by database time (recorded_at), its
      edge at exactly 60 minutes after the CREATED's recorded_at on the app clock.
  PM6 the request window likewise, on the clamp request's created_at.
  PM7 a filter exit: EXITED; collect stays true while another enabled panel on the source still selects the symbol,
      and goes false (stopped_cause PANEL_MEMBER_LEFT since P22r, RETIRED published) when none does.
  PM8 a delisting: DRAINING, held while no fetch finished since (fetch_finished_since), DRAINED and collect false
      (stopped_cause PANEL_MEMBER_LEFT) once one has.
  PM9 the re-entry restart (P22r tj-7zjov1.264, ADR tj-26jt9w addendum 23 rule A; validator P22rv tj-7zjov1.265):
      three members exit (each entry stopped PANEL_MEMBER_LEFT, the CHECK ck_store_dataset_entry_stopped_cause_needs_stop
      holding on Postgres) and re-enter. a (last accepted vendor id its own) is resumed: from the observer connection
      neither the guarded UPDATE nor its ENTERED row (dataset_id, cause dataset_resumed) is visible before the pass's
      commit, both after it, and RESUMED is published only once the resumed row is committed. b (last accepted vendor
      id another's: a reused symbol) stays stopped with a plain ENTERED. c is stopped and resumed by an operator on
      another connection between the pass's read and its guarded UPDATE: the UPDATE matches nothing on READ
      COMMITTED, c's ENTERED is plain and the operator's row stands.
  PM10 include_otc (ADR tj-ed4t8m, validator tj-7zjov1.364.4, WRITTEN NOT YET RUN): off exits and stops the OTC member
      only (PANEL_MEMBER_LEFT, RETIRED); on again restarts it (dataset_resumed, RESUMED), no new request.

  THE POINT-IN-TIME START (ADR tj-iov38l, validator tj-7zjov1.364.11, WRITTEN NOT YET RUN): a declared start is the
  XNYS local midnight of the local date of the member's ENTERED as_of, no longer CALENDAR_START. PM1 and PM2 date their
  first snapshot FIRST_SNAPSHOT_AS_OF, 21:30 New York on 30 May 2022 (already 31 May in UTC), so b starts on 30 May
  (the local date, on asyncpg's timestamptz) and the clamp still moves a, renamed away on RENAMED_ON (9 June 2022,
  after that start; a rename before the start no longer clamps). PM3's release reads the ENTERED date through
  entered_as_of_by_member (the row_number window on Postgres) and its bars request starts there.

  The PM1 and PM3 idle_in_transaction checks carry a positive control (tj-7zjov1.262 change (A)): at each clamp at
  least one connection is listed under the engine's application_name, so their zero is not vacuous.

ISOLATION. Every symbol is this test's own (own_symbol plus a letter); panel names carry the run id; the source
settings versions, panels, settings versions, passes, transitions, snapshots and rows this test wrote are deleted by
id, and its request rows, attempt rows, corporate-action observations and entries by its symbols, failed or not (a
test-only delete: the panel and asset-list tables are append-only for the service). A first sighting of an instance is
GLOBAL by design; live_instances_kept restores the stack's ingest after each test.
"""

import functools
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import ERROR_DOMAIN, Reason
from data.store.app import panel_manager
from data.store.app.client_label import PANEL_OWNER
from data.store.app.database.crud.ingest_request import requests_in_window, symbols_with_open_requests
from data.store.app.database.crud.panel_ledger import COUNT_KEYS, created_in_window, latest_state
from data.store.app.database.crud.panel_manager_reads import panel_entries
from data.store.app.database.crud.panel_settings import (
    PanelSettings,
    SourceSettings,
    append_settings_version,
    append_source_settings_version,
    create_panel,
)
from data.store.app.database.models.asset_list import AssetListRow, AssetListSnapshot
from data.store.app.database.models.corporate_action import CorporateAction
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import (
    IngestRequest,
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRun, IngestRunTrigger
from data.store.app.database.models.panel import (
    Panel,
    PanelCadence,
    PanelMemberTransition,
    PanelPass,
    PanelPassKind,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
    PanelTransition,
)
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.events import DatasetChange, DatasetChanged
from data.store.app.ingest import data_action_request, delivery_intake
from data.store.app.ingest.delivery_intake import DeliveryIntake
from data.store.app.trading_calendar import calendar_for_source, local_midnight
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    CorporateActionRecord,
    CorporateActionRecords,
    DeliveryHeader,
    DeliveryOutcome,
    DeliveryTrailer,
    RegisterInstanceRequest,
    VendorCallStats,
)
from schemas.data_ingest.fetch_dataset import FetchRefused, ServedRange
from schemas.data_ingest.submit import AssetListStatus, SubmitAccepted, SubmitAck, SubmitRequest, SubmitResponse


# A first sighting supersedes the stack's live ingest too; the fixture makes it live again (tj-7zjov1.233).
pytestmark = [pytest.mark.data_store, pytest.mark.asyncio, pytest.mark.usefixtures('live_instances_kept')]

PANELS = Panel.__table__
SETTINGS = PanelSettingsVersion.__table__
SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__
PASSES = PanelPass.__table__
TRANSITIONS = PanelMemberTransition.__table__
REQUESTS = IngestRequest.__table__
RUNS = IngestRun.__table__
ACTIONS = CorporateAction.__table__
INSTANCES = IngestInstance.__table__
ENTRIES = StoreDatasetEntry.__table__
SNAPSHOTS = AssetListSnapshot.__table__
ASSET_ROWS = AssetListRow.__table__

SOURCE = DataSource.MANUAL_ENTRY
# The calendar mapped in for SOURCE (see the module docstring): ALPACA_API's, XNYS.
CALENDAR_SOURCE = DataSource.ALPACA_API
SETTLE = timedelta(minutes=30)
QUOTA_KEY = 'system-test-quota-key'
# The process date of the rename away from a symbol that the clamp query answers with (PM1, PM2).
RENAMED_ON = datetime(2022, 6, 9, tzinfo=UTC)
# PM1 and PM2's first active snapshot (ADR tj-iov38l): 21:30 New York (EDT) on 30 May 2022, 01:30Z on the 31st. Before
# RENAMED_ON, so the clamp still moves a's point-in-time start; an evening, so the local date is not the UTC one.
FIRST_SNAPSHOT_AS_OF = datetime(2022, 5, 31, 1, 30, tzinfo=UTC)
NEW_YORK = ZoneInfo('America/New_York')
MICRO = timedelta(microseconds=1)
# The manager's stop mark, as the column stores it (tj-7zjov1.264).
LEFT = 'PANEL_MEMBER_LEFT'
_REAL_CALENDAR_FOR_SOURCE = calendar_for_source


def _calendar_for(source: DataSource):
    """calendar_for_source with XNYS mapped in for SOURCE; every other source is unchanged."""
    return _REAL_CALENDAR_FOR_SOURCE(CALENDAR_SOURCE if source is SOURCE else source)


@functools.cache
def _xnys():
    return _REAL_CALENDAR_FOR_SOURCE(CALENDAR_SOURCE)


def declared_start(entered_as_of: datetime) -> datetime:
    """panel_dataset_start for SOURCE under the mapped calendar, by hand (ADR tj-iov38l P1).

    New York midnight of the New York date of the member's ENTERED as_of.
    """
    return datetime.combine(entered_as_of.astimezone(NEW_YORK).date(), time.min, tzinfo=NEW_YORK)


def clamped_start() -> datetime:
    """Where the clamp moves a start to when the symbol was renamed away on RENAMED_ON: XNYS local midnight."""
    return local_midnight(date(RENAMED_ON.year, RENAMED_ON.month, RENAMED_ON.day), _xnys())


def stats() -> VendorCallStats:
    now = datetime.now(UTC)
    return VendorCallStats(
        calls=1, pages=1, throttled_429=0, retries=0, first_call_at=now, last_call_at=now + timedelta(seconds=1)
    )


# --------------------------------------------------------------------------------------------- cleanup


class Written:
    """What one test wrote: deleted at its end, by id or by its own symbols."""

    def __init__(self) -> None:
        self.panels: list[uuid.UUID] = []
        self.source_versions: list[uuid.UUID] = []
        self.snapshots: list[uuid.UUID] = []
        self.symbols: list[str] = []
        self.instances: list[str] = []


@pytest.fixture
def written(pg_engine: Engine) -> Iterator[Written]:
    ids = Written()
    yield ids
    with pg_engine.begin() as conn:
        if ids.symbols:
            mine = (
                sa.select(REQUESTS.c.id)
                .where(REQUESTS.c.source == SOURCE, REQUESTS.c.asset_symbol.in_(ids.symbols))
                .scalar_subquery()
            )
            conn.execute(sa.delete(ACTIONS).where(ACTIONS.c.request_id.in_(mine)))
            conn.execute(sa.delete(RUNS).where(RUNS.c.request_id.in_(mine)))
            conn.execute(
                sa.delete(REQUESTS).where(REQUESTS.c.source == SOURCE, REQUESTS.c.asset_symbol.in_(ids.symbols))
            )
            # The cascade removes any bar; the entries are this test's symbols only.
            conn.execute(sa.delete(ENTRIES).where(ENTRIES.c.source == SOURCE, ENTRIES.c.asset_symbol.in_(ids.symbols)))
        if ids.panels:
            conn.execute(sa.delete(TRANSITIONS).where(TRANSITIONS.c.panel_id.in_(ids.panels)))
            conn.execute(sa.delete(PASSES).where(PASSES.c.panel_id.in_(ids.panels)))
            conn.execute(sa.delete(SETTINGS).where(SETTINGS.c.panel_id.in_(ids.panels)))
            conn.execute(sa.delete(PANELS).where(PANELS.c.id.in_(ids.panels)))
        if ids.source_versions:
            conn.execute(sa.delete(SOURCE_SETTINGS).where(SOURCE_SETTINGS.c.id.in_(ids.source_versions)))
        if ids.snapshots:
            conn.execute(sa.delete(ASSET_ROWS).where(ASSET_ROWS.c.snapshot_id.in_(ids.snapshots)))
            conn.execute(sa.delete(SNAPSHOTS).where(SNAPSHOTS.c.id.in_(ids.snapshots)))
        if ids.instances:
            conn.execute(sa.delete(INSTANCES).where(INSTANCES.c.instance_id.in_(ids.instances)))


# --------------------------------------------------------------------------------------------- the world


@dataclass(frozen=True)
class Asset:
    vendor_asset_id: str
    symbol: str
    tradable: bool = True
    # The snapshot row's exchange; 'OTC' is the value include_otc governs (ADR tj-ed4t8m D3).
    exchange: str = 'NYSE'


@dataclass(frozen=True)
class Snap:
    id: uuid.UUID
    as_of: datetime


@dataclass
class PanelUnderTest:
    id: uuid.UUID
    version_id: uuid.UUID
    created_at: datetime
    settings: PanelSettings


@dataclass(frozen=True)
class ClampSeen:
    """What another connection saw while the clamp's corporate-actions request was being submitted."""

    symbol: str
    idle_in_transaction: int
    # The positive control for idle_in_transaction (tj-7zjov1.262 change (A)): the manager engine's connections that
    # pg_stat_activity lists under its application_name at all. Zero would make idle_in_transaction == 0 vacuous.
    app_connections: int
    membership_passes: int
    release_passes: int
    created_rows: int
    bars_requests: int


@dataclass(frozen=True)
class Seen:
    """What another connection saw of one bars request's commit: just before create_request, and just after."""

    request_rows: int
    created_rows: int
    entered_rows: int
    release_passes: int


@dataclass(frozen=True)
class Creation:
    request_id: uuid.UUID
    before: Seen
    after: Seen


@dataclass(frozen=True)
class BarsSeen:
    """At the bars submit (after the commit): the request row as another connection reads it, and its CREATED row."""

    request_id: uuid.UUID
    state: IngestRequestState
    dataset_id: uuid.UUID | None
    created_rows: int


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple[Any, ...]
    result: Any


class Clock:
    """The manager's clock: database-aligned real time (the skew measured at start removed), or a pinned instant."""

    def __init__(self, skew: timedelta) -> None:
        self.skew = skew
        self.pinned: datetime | None = None

    def __call__(self) -> datetime:
        return self.pinned if self.pinned is not None else datetime.now(UTC) + self.skew


class PlayedIngest:
    """ingest, played: accepts each Submit and delivers its reports through the real intake.

    A corporate-actions report is delivered in the verdict waiter's sleep, a bars report only when the test calls
    World.deliver_bars. A symbol in `refuse` has its clamp query refused (FEED_NOT_AVAILABLE), which finishes the
    request FAILED and fails the clamp; a symbol in `renames` is answered with a name_change away from it on that
    process date.
    """

    def __init__(self, world: 'World', instance_id: str) -> None:
        self.world = world
        self.instance_id = instance_id
        self.refuse: set[str] = set()
        self.renames: dict[str, datetime] = {}
        self.sent: list[SubmitRequest] = []
        self.pending_actions: list[AttemptDelivery] = []
        self.pending_bars: list[SubmitRequest] = []

    def _accepted(self, feed: Feed | None) -> SubmitResponse:
        return SubmitResponse(
            ack=SubmitAck(accepted=SubmitAccepted(feed=feed, quota_key=QUOTA_KEY)), instance_id=self.instance_id
        )

    async def submit(self, request: SubmitRequest) -> SubmitResponse:
        self.sent.append(request)
        if request.corporate_actions is not None:
            symbol = request.corporate_actions.asset_symbol
            self.world.saw_clamp(symbol)
            if symbol in self.refuse:
                refused = FetchRefused(
                    reason=Reason.FEED_NOT_AVAILABLE.value,
                    domain=ERROR_DOMAIN,
                    detail='refused by the panel manager system test',
                )
                return SubmitResponse(ack=SubmitAck(refused=refused), instance_id=self.instance_id)
            self.pending_actions.append(self._actions_report(request, symbol))
            return self._accepted(None)
        if request.bars is None:
            raise AssertionError('the panel manager sent a request that is neither a clamp query nor bars')
        self.world.saw_bars(request)
        self.pending_bars.append(request)
        return self._accepted(Feed.IEX)

    async def instance(self):
        raise AssertionError('the panel manager never probes the instance')

    def _actions_report(self, request: SubmitRequest, symbol: str) -> AttemptDelivery:
        records = []
        if symbol in self.renames:
            vendor_id = f'pm-{self.world.run_id}-{uuid.uuid4().hex[:8]}'
            records.append(
                CorporateActionRecord(
                    vendor_action_id=vendor_id,
                    action_type='name_change',
                    process_date=self.renames[symbol],
                    identifiers={'old_symbol': symbol, 'new_symbol': f'{symbol}Q'},
                    raw_payload=f'{{"id":"{vendor_id}"}}'.encode(),
                )
            )
        now = datetime.now(UTC)
        return AttemptDelivery(
            header=DeliveryHeader(
                request_id=request.request_id,
                instance_id=self.instance_id,
                attempt_no=1,
                quota_key=QUOTA_KEY,
                feed=None,
            ),
            parts=[CorporateActionRecords(records=records)] if records else [],
            trailer=DeliveryTrailer(
                outcome=DeliveryOutcome.SUCCEEDED,
                vendor_stats=stats(),
                served_range=ServedRange(start=request.corporate_actions.start, end=request.corporate_actions.end),
                as_of=now,
            ),
        )

    def bars_report(self, request: SubmitRequest, vendor_asset_id: str) -> AttemptDelivery:
        """A served window with no bar in it: the intake still mints the entry on this first delivered attempt."""
        now = datetime.now(UTC)
        return AttemptDelivery(
            header=DeliveryHeader(
                request_id=request.request_id,
                instance_id=self.instance_id,
                attempt_no=1,
                quota_key=QUOTA_KEY,
                feed=Feed.IEX,
                vendor_asset_id=vendor_asset_id,
            ),
            parts=[],
            trailer=DeliveryTrailer(
                outcome=DeliveryOutcome.SUCCEEDED,
                vendor_stats=stats(),
                served_range=ServedRange(start=request.bars.start, end=now),
                as_of=now,
            ),
        )


class World:
    """The manager, its seams and the observer, on the test stack's Postgres."""

    def __init__(
        self,
        engine: AsyncEngine,
        app_name: str,
        observer: Engine,
        written: Written,
        run_id: str,
        base_symbol: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.engine = engine
        self.app_name = app_name
        self.observer = observer
        self.written = written
        self.run_id = run_id
        self.base_symbol = base_symbol
        self.monkeypatch = monkeypatch
        self.panel_ids: set[uuid.UUID] = set()
        self.vendor_of: dict[str, str] = {}
        self.calls: list[Call] = []
        self.clamps: list[ClampSeen] = []
        self.creations: list[Creation] = []
        self.bars_seen: list[BarsSeen] = []
        self.published: list[Any] = []
        self.failures: list[BaseException] = []
        self.enabled_seen: list[set[uuid.UUID]] = []
        self.crash_finish = False
        self.instance_id = f'system-test-{run_id}-pm-{uuid.uuid4().hex[:6]}'
        written.instances.append(self.instance_id)
        self.ingest = PlayedIngest(self, self.instance_id)
        self.clock = Clock(timedelta(0))
        self._install()
        self.intake = DeliveryIntake(chunk_size=100)
        self.manager = self.new_manager()

    # ------------------------------------------------------------------ seams

    def _install(self) -> None:
        engine, mp = self.engine, self.monkeypatch

        @asynccontextmanager
        async def own_session() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session

        for module in (panel_manager, data_action_request, delivery_intake):
            mp.setattr(module, 'own_session', own_session)
        mp.setattr(panel_manager, 'publish', self.published.append)
        mp.setattr(delivery_intake, 'publish', lambda _event: None)
        mp.setattr(panel_manager, 'calendar_for_source', _calendar_for)
        mp.setattr(data_action_request, 'calendar_for_source', _calendar_for)
        mp.setenv('STORE_VERDICT_DEADLINE_SECONDS', '20')

        ingest = self.ingest

        async def sleep(_seconds: float) -> None:
            if not ingest.pending_actions:
                raise AssertionError('the clamp waited for a corporate-actions request nobody answered')
            while ingest.pending_actions:
                await self.intake.deliver(ingest.pending_actions.pop(0))

        mp.setattr(
            data_action_request, 'await_verdict', functools.partial(data_action_request.await_verdict, sleep=sleep)
        )

        real_enabled = panel_manager.enabled_panels_on_source

        async def enabled(db, source):
            found = await real_enabled(db, source)
            self.enabled_seen.append({panel.panel.id for panel in found})
            self.calls.append(Call('enabled_panels_on_source', (source,), found))
            return [panel for panel in found if panel.panel.id in self.panel_ids]

        mp.setattr(panel_manager, 'enabled_panels_on_source', enabled)

        for name in (
            'panel_entries',
            'symbols_with_open_requests',
            'requests_in_window',
            'created_in_window',
            'snapshots_between',
            'rows_of_ids',
            'requests_by_ids',
            'fetch_finished_since',
            'stop_collecting',
            'check_own_overlap',
            # The pass's one ledger read of the ENTERED dates (ADR tj-iov38l, tj-7zjov1.364.10).
            'entered_as_of_by_member',
        ):
            self._spy(name)

        real_create = panel_manager.create_request

        async def create(db, request):
            before = self.seen(request.id)
            record = await real_create(db, request)
            self.creations.append(Creation(request.id, before, self.seen(request.id)))
            return record

        mp.setattr(panel_manager, 'create_request', create)

        real_finish = panel_manager.finish_pass

        async def finish(db, pass_id, counts):
            if self.crash_finish:
                self.crash_finish = False
                raise RuntimeError('the panel manager system test crashes this pass before finish_pass')
            return await real_finish(db, pass_id, counts)

        mp.setattr(panel_manager, 'finish_pass', finish)

    def _spy(self, name: str) -> None:
        real = getattr(panel_manager, name)

        async def wrapper(*args, **kwargs):
            result = await real(*args, **kwargs)
            self.calls.append(Call(name, args[1:] + tuple(kwargs.values()), result))
            return result

        self.monkeypatch.setattr(panel_manager, name, wrapper)

    def called(self, name: str) -> list[Call]:
        return [call for call in self.calls if call.name == name]

    def new_manager(self) -> panel_manager.PanelManager:
        """A manager with nothing in memory: also what a restarted data_store process holds."""
        manager = panel_manager.PanelManager(self.ingest, {SOURCE: _xnys()}, clock=self.clock, settle=SETTLE)
        real = manager._tick_panel

        async def recording(*args):
            try:
                return await real(*args)
            except Exception as error:
                self.failures.append(error)
                raise

        manager._tick_panel = recording
        return manager

    async def tick(self, manager: panel_manager.PanelManager | None = None, *, expect_failure: bool = False) -> None:
        """One tick of the manager; a panel's failure, which the manager logs and swallows, is surfaced here."""
        before = len(self.failures)
        await (manager or self.manager).tick()
        failures = self.failures[before:]
        if not expect_failure:
            assert not failures, f'a panel pass failed: {[type(error).__name__ for error in failures]} {failures}'

    # ------------------------------------------------------------------ the observer (another connection)

    def rows(self, statement) -> list[sa.Row]:
        with self.observer.connect() as conn:
            return list(conn.execute(statement).all())

    def idle_in_transaction(self) -> int:
        """How many of the manager engine's connections sit idle INSIDE a transaction right now."""
        with self.observer.connect() as conn:
            return conn.execute(
                sa.text(
                    'SELECT count(*) FROM pg_stat_activity WHERE application_name = :name '
                    "AND state = 'idle in transaction'"
                ),
                {'name': self.app_name},
            ).scalar_one()

    def app_connections(self) -> int:
        """How many connections pg_stat_activity lists under the manager engine's application_name, in any state."""
        with self.observer.connect() as conn:
            return conn.execute(
                sa.text('SELECT count(*) FROM pg_stat_activity WHERE application_name = :name'), {'name': self.app_name}
            ).scalar_one()

    def passes(self, panel: PanelUnderTest, kind: PanelPassKind | None = None) -> list[sa.Row]:
        statement = sa.select(PASSES).where(PASSES.c.panel_id == panel.id)
        if kind is not None:
            statement = statement.where(PASSES.c.kind == kind)
        return self.rows(statement.order_by(PASSES.c.started_at, PASSES.c.id))

    def transitions(self, panel: PanelUnderTest) -> list[sa.Row]:
        return self.rows(
            sa.select(TRANSITIONS, PASSES.c.kind.label('pass_kind'))
            .join(PASSES, PASSES.c.id == TRANSITIONS.c.pass_id)
            .where(TRANSITIONS.c.panel_id == panel.id)
            .order_by(PASSES.c.started_at, PASSES.c.id, TRANSITIONS.c.seq)
        )

    def requests(self, *assets: Asset, kind: IngestRequestKind | None = None) -> list[sa.Row]:
        statement = sa.select(REQUESTS).where(
            REQUESTS.c.source == SOURCE, REQUESTS.c.asset_symbol.in_([asset.symbol for asset in assets])
        )
        if kind is not None:
            statement = statement.where(REQUESTS.c.kind == kind)
        return self.rows(statement.order_by(REQUESTS.c.created_at, REQUESTS.c.id))

    def entries(self, *assets: Asset) -> dict[str, sa.Row]:
        found = self.rows(
            sa.select(ENTRIES).where(
                ENTRIES.c.source == SOURCE, ENTRIES.c.asset_symbol.in_([asset.symbol for asset in assets])
            )
        )
        by_symbol: dict[str, sa.Row] = {}
        for row in found:
            assert row.asset_symbol not in by_symbol, f'two entries for one symbol: {row.asset_symbol}'
            by_symbol[row.asset_symbol] = row
        return by_symbol

    def _count(self, statement) -> int:
        with self.observer.connect() as conn:
            return conn.execute(statement).scalar_one()

    def _panel_passes(self, kind: PanelPassKind) -> int:
        if not self.panel_ids:
            return 0
        return self._count(
            sa.select(sa.func.count())
            .select_from(PASSES)
            .where(PASSES.c.panel_id.in_(list(self.panel_ids)), PASSES.c.kind == kind)
        )

    def _transitions_of(self, transition: PanelTransition, request_id: uuid.UUID | None = None) -> int:
        if not self.panel_ids:
            return 0
        statement = (
            sa.select(sa.func.count())
            .select_from(TRANSITIONS)
            .where(TRANSITIONS.c.panel_id.in_(list(self.panel_ids)), TRANSITIONS.c.transition == transition)
        )
        if request_id is not None:
            statement = statement.where(TRANSITIONS.c.request_id == request_id)
        return self._count(statement)

    def seen(self, request_id: uuid.UUID) -> Seen:
        return Seen(
            request_rows=self._count(
                sa.select(sa.func.count()).select_from(REQUESTS).where(REQUESTS.c.id == request_id)
            ),
            created_rows=self._transitions_of(PanelTransition.CREATED, request_id),
            entered_rows=self._transitions_of(PanelTransition.ENTERED),
            release_passes=self._panel_passes(PanelPassKind.RELEASE),
        )

    def saw_clamp(self, symbol: str) -> None:
        self.clamps.append(
            ClampSeen(
                symbol=symbol,
                idle_in_transaction=self.idle_in_transaction(),
                app_connections=self.app_connections(),
                membership_passes=self._panel_passes(PanelPassKind.MEMBERSHIP),
                release_passes=self._panel_passes(PanelPassKind.RELEASE),
                created_rows=self._transitions_of(PanelTransition.CREATED),
                bars_requests=self._count(
                    sa.select(sa.func.count())
                    .select_from(REQUESTS)
                    .where(
                        REQUESTS.c.source == SOURCE,
                        REQUESTS.c.kind == IngestRequestKind.BARS,
                        REQUESTS.c.asset_symbol.in_(self.written.symbols),
                    )
                ),
            )
        )

    def saw_bars(self, request: SubmitRequest) -> None:
        request_id = uuid.UUID(request.request_id)
        (row,) = self.rows(sa.select(REQUESTS).where(REQUESTS.c.id == request_id))
        self.bars_seen.append(
            BarsSeen(request_id, row.state, row.dataset_id, self._transitions_of(PanelTransition.CREATED, request_id))
        )

    # ------------------------------------------------------------------ set-up writes

    async def start(self) -> None:
        async with self.engine.connect() as conn:
            database_now = (await conn.execute(sa.select(sa.func.clock_timestamp()))).scalar_one()
        self.clock.skew = database_now - datetime.now(UTC)
        await self.intake.register_instance(
            RegisterInstanceRequest(instance_id=self.instance_id, started_at=datetime.now(UTC))
        )

    async def database_now(self) -> datetime:
        async with self.engine.connect() as conn:
            return (await conn.execute(sa.select(sa.func.clock_timestamp()))).scalar_one()

    def asset(self, letter: str, *, tradable: bool = True, exchange: str = 'NYSE') -> Asset:
        symbol = f'{self.base_symbol}{letter}'
        vendor = f'pm-{self.run_id}-{letter}-{uuid.uuid4().hex[:8]}'
        self.written.symbols.append(symbol)
        self.vendor_of[symbol] = vendor
        return Asset(vendor, symbol, tradable, exchange)

    async def source_settings(self, *, ramp: int, cap: int) -> uuid.UUID:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            record = await append_source_settings_version(
                db,
                source=SOURCE,
                settings=SourceSettings(ramp_datasets_per_hour=ramp, request_cap_per_hour=cap),
                actor='operator',
                reason=f'panel manager system test {uuid.uuid4().hex[:8]}',
            )
        self.written.source_versions.append(record.id)
        return record.id

    async def panel(self, name: str, members: list[Asset], **filters: Any) -> PanelUnderTest:
        settings = PanelSettings(
            enabled=True,
            cadence=PanelCadence.MONTHLY,
            allow_symbols=tuple(asset.symbol for asset in members),
            **filters,
        )
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            record, version = await create_panel(
                db,
                name=f'system-test-{self.run_id}-pm-{name}-{uuid.uuid4().hex[:8]}',
                source=SOURCE,
                settings=settings,
                actor='operator',
                reason='panel manager system test panel',
            )
        self.written.panels.append(record.id)
        self.panel_ids.add(record.id)
        return PanelUnderTest(record.id, version.id, record.created_at, settings)

    async def new_version(self, panel: PanelUnderTest, members: list[Asset], **changes: Any) -> uuid.UUID:
        settings = replace(panel.settings, allow_symbols=tuple(asset.symbol for asset in members), **changes)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            version = await append_settings_version(
                db, panel_id=panel.id, settings=settings, actor='operator', reason='panel manager system test change'
            )
        panel.version_id, panel.settings = version.id, settings
        return version.id

    async def snapshot(
        self, assets: list[Asset], *, as_of: datetime | None = None, status: AssetListStatus = AssetListStatus.ACTIVE
    ) -> Snap:
        as_of = as_of if as_of is not None else await self.database_now()
        snapshot_id = uuid.uuid4()
        self.written.snapshots.append(snapshot_id)
        async with self.engine.begin() as conn:
            await conn.execute(
                SNAPSHOTS.insert().values(
                    id=snapshot_id,
                    source=SOURCE,
                    asset_type=AssetType.STOCK,
                    asset_list=status,
                    request_id=uuid.uuid4(),
                    attempt_no=1,
                    as_of=as_of,
                    row_count=len(assets),
                )
            )
            for asset in assets:
                await conn.execute(
                    ASSET_ROWS.insert().values(
                        snapshot_id=snapshot_id,
                        vendor_asset_id=asset.vendor_asset_id,
                        symbol=asset.symbol,
                        name=asset.symbol,
                        exchange=asset.exchange,
                        active=status is AssetListStatus.ACTIVE,
                        tradable=asset.tradable,
                        raw_payload=b'{}',
                    )
                )
        return Snap(snapshot_id, as_of)

    async def deliver_bars(self) -> None:
        """Deliver every bars request sent so far through the real intake: the entry is minted on this attempt."""
        while self.ingest.pending_bars:
            request = self.ingest.pending_bars.pop(0)
            await self.intake.deliver(self.ingest.bars_report(request, self.vendor_of[request.bars.asset_symbol]))

    async def next_membership_pass(self, panels: list[PanelUnderTest], assets: list[Asset]) -> Snap:
        """Make the next MEMBERSHIP pass of these panels due: a snapshot at the due instant, the clock just after it.

        The due instant is membership_due_at from each panel's last FINISHED membership pass's started_at (database
        time) under MONTHLY, the latest of them.
        """
        dues = []
        for panel in panels:
            finished = [row for row in self.passes(panel, PanelPassKind.MEMBERSHIP) if row.finished_at is not None]
            last = finished[-1].started_at if finished else None
            dues.append(panel_manager.membership_due_at(PanelCadence.MONTHLY, last, panel.created_at, _xnys(), SETTLE))
        due = max(dues)
        snap = await self.snapshot(assets, as_of=due)
        self.clock.pinned = due + timedelta(minutes=1)
        return snap

    async def latest(self, panel: PanelUnderTest):
        async with AsyncSession(self.engine) as db:
            return await latest_state(db, panel.id)

    async def require_quiet_windows(self) -> None:
        """The rate tests read the trailing windows exactly: no other CREATED row or panel request may sit in them."""
        since = await self.database_now() - timedelta(hours=2)
        async with AsyncSession(self.engine) as db:
            created = await created_in_window(db, SOURCE, since)
            requested = await requests_in_window(db, SOURCE, PANEL_OWNER, since)
        if created or requested:
            pytest.fail(
                f'{SOURCE.name} already has {created} CREATED rows and {requested} panel requests in the last two hours '
                '(left by another run); the window boundaries below would be off by them',
                pytrace=False,
            )


@pytest_asyncio.fixture
async def world(pg_settings, pg_engine, written, run_identity, own_symbol, monkeypatch) -> AsyncIterator[World]:
    app_name = f'pm-{run_identity.run_id}-{uuid.uuid4().hex[:8]}'
    engine = create_async_engine(
        pg_settings.url('postgresql+asyncpg'),
        connect_args={'timeout': pg_settings.connect_timeout, 'server_settings': {'application_name': app_name}},
    )
    try:
        built = World(engine, app_name, pg_engine, written, run_identity.run_id, own_symbol, monkeypatch)
        await built.start()
        yield built
    finally:
        await engine.dispose()


def _counts(**nonzero: int) -> dict[str, int]:
    return dict.fromkeys(COUNT_KEYS, 0) | nonzero


def _kinds(rows: list[sa.Row]) -> list[tuple[str, PanelTransition]]:
    return [(row.symbol, row.transition) for row in rows]


async def _first_pass(world: World):
    """PM1's set-up and its first pass: a, b selected (a renamed away on RENAMED_ON), c filtered out (not tradable)."""
    a, b, c = world.asset('A'), world.asset('B'), world.asset('C', tradable=False)
    world.ingest.renames[a.symbol] = RENAMED_ON
    source_version = await world.source_settings(ramp=10, cap=100)
    panel = await world.panel('pass', [a, b, c], tradable=True)
    active = await world.snapshot([a, b, c], as_of=FIRST_SNAPSHOT_AS_OF)
    await world.tick()
    return a, b, c, panel, active, source_version


# --------------------------------------------------------------------------------------------- PM1


async def test_pm1_a_membership_pass_on_asyncpg_commits_entered_created_and_its_request_together(world):
    await world.require_quiet_windows()
    a, b, c = world.asset('A'), world.asset('B'), world.asset('C', tradable=False)
    world.ingest.renames[a.symbol] = RENAMED_ON
    source_version = await world.source_settings(ramp=10, cap=100)
    panel = await world.panel('pm1', [a, b, c], tradable=True)
    # An old evening snapshot: the first pass runs on it at once (it only logs that it is older), ADR tj-iov38l.
    active = await world.snapshot([a, b, c], as_of=FIRST_SNAPSHOT_AS_OF)
    inactive = await world.snapshot([world.asset('D')], status=AssetListStatus.INACTIVE)

    await world.tick()

    # The pass row: kinds, versions and both snapshots (the asset_list_status enum cast read both lists), JSONB counts.
    (pass_row,) = world.passes(panel)
    assert pass_row.kind == PanelPassKind.MEMBERSHIP and pass_row.dry_run is False
    assert (pass_row.settings_version_id, pass_row.source_settings_version_id) == (panel.version_id, source_version)
    assert (pass_row.active_snapshot_id, pass_row.inactive_snapshot_id) == (active.id, inactive.id)
    assert pass_row.finished_at is not None and pass_row.finished_at >= pass_row.started_at
    assert pass_row.counts == _counts(entered=2, created=2), pass_row.counts

    # The ledger: ENTERED before its CREATED, in symbol order; c (not tradable) is filtered out; seq 1..4.
    rows = world.transitions(panel)
    assert _kinds(rows) == [
        (a.symbol, PanelTransition.ENTERED),
        (a.symbol, PanelTransition.CREATED),
        (b.symbol, PanelTransition.ENTERED),
        (b.symbol, PanelTransition.CREATED),
    ]
    assert [row.seq for row in rows] == [1, 2, 3, 4]
    assert all(row.as_of == active.as_of for row in rows), 'a first pass dates every row by its snapshot'
    created = [row for row in rows if row.transition is PanelTransition.CREATED]
    assert all(row.request_id is not None and row.dataset_id is None for row in created), 'CREATED names its request'

    # Each CREATED names a POST-shaped bars request: panel owner, no dataset, no ready_by, REQUEST, the clamped start.
    bars = {row.asset_symbol: row for row in world.requests(a, b, c, kind=IngestRequestKind.BARS)}
    assert set(bars) == {a.symbol, b.symbol}
    assert {row.request_id for row in created} == {bars[a.symbol].id, bars[b.symbol].id}
    for row in bars.values():
        assert row.client_label == PANEL_OWNER and row.dataset_id is None and row.ready_by is None
        assert row.trigger == IngestRunTrigger.REQUEST and row.collect is True
        assert row.expiry_type == ExpiryType.ROLLING.value and row.requested_end is None
        assert row.granularity == Granularity.ONE_DAY and row.update_type == UpdateType.DAILY.value
        assert row.state == IngestRequestState.QUEUED and row.instance_id == world.instance_id
    assert bars[a.symbol].requested_start == clamped_start(), 'the clamp did not move the stored start'
    assert bars[b.symbol].requested_start == declared_start(active.as_of) == datetime(2022, 5, 30, 4, tzinfo=UTC)
    assert declared_start(active.as_of) < clamped_start(), 'the precondition: the rename is after the start'

    # The REAL clamp ran: one finished corporate-actions request per member, written before its bars request.
    queries = {row.asset_symbol: row for row in world.requests(a, b, c, kind=IngestRequestKind.CORPORATE_ACTIONS)}
    assert set(queries) == {a.symbol, b.symbol}
    for symbol, query in queries.items():
        assert query.client_label == PANEL_OWNER and not query.state.is_open
        assert query.created_at < bars[symbol].created_at
    assert [(r.corporate_actions or r.bars).asset_symbol for r in world.ingest.sent] == [a.symbol] * 2 + [b.symbol] * 2

    # While each clamp waited, NO manager connection was idle inside a transaction, and the pass row was committed.
    assert [seen.symbol for seen in world.clamps] == [a.symbol, b.symbol]
    assert all(seen.idle_in_transaction == 0 for seen in world.clamps), world.clamps
    # The zero above means something only if the filter matches: the clamp's own session is open while it waits.
    assert all(seen.app_connections >= 1 for seen in world.clamps), 'application_name never reached pg_stat_activity'
    assert all(seen.membership_passes == 1 for seen in world.clamps), 'the pass row was not committed before a clamp'
    assert (world.clamps[0].created_rows, world.clamps[0].bars_requests) == (0, 0)
    assert (world.clamps[1].created_rows, world.clamps[1].bars_requests) == (1, 1), "a's commit landed before b's clamp"

    # ONE COMMIT: another connection sees neither ENTERED, CREATED nor the request before create_request, all after.
    assert [creation.request_id for creation in world.creations] == [bars[a.symbol].id, bars[b.symbol].id]
    for index, creation in enumerate(world.creations):
        assert (creation.before.request_rows, creation.before.created_rows) == (0, 0), creation
        assert creation.before.entered_rows == index, 'an ENTERED became visible before its CREATED commit'
        assert (creation.after.request_rows, creation.after.created_rows) == (1, 1), creation
        assert creation.after.entered_rows == index + 1, creation
    # Submitted only after that commit: the request and its CREATED row were already visible, still PENDING.
    assert all(seen.state == IngestRequestState.PENDING and seen.created_rows == 1 for seen in world.bars_seen)

    # The SQL behind the decisions ran on asyncpg and answered.
    assert any(panel.id in seen for seen in world.enabled_seen), 'enabled_panels_on_source did not list the panel'
    assert [call.result for call in world.called('panel_entries')] == [{}]
    assert world.called('symbols_with_open_requests')[0].result.isdisjoint({a.symbol, b.symbol})
    assert [call.result for call in world.called('requests_in_window')] == [0, 2]
    assert [call.result for call in world.called('created_in_window')] == [0, 1]
    overlap_starts = [call.args[0].start for call in world.called('check_own_overlap')]
    assert overlap_starts == [clamped_start(), declared_start(active.as_of)], (
        'check_own_overlap missed the clamped start'
    )
    assert world.called('entered_as_of_by_member') == [], 'a first pass of new members reads no ENTERED dates'
    assert world.entries(a, b) == {}, 'the manager minted an entry; only the delivery intake may'

    async with AsyncSession(world.engine) as db:
        assert {a.symbol, b.symbol} <= await symbols_with_open_requests(db, SOURCE, PANEL_OWNER)
        assert await requests_in_window(db, SOURCE, PANEL_OWNER, min(q.created_at for q in queries.values())) == 4
        assert await created_in_window(db, SOURCE, min(row.recorded_at for row in created)) == 2


# --------------------------------------------------------------------------------------------- PM2


async def test_pm2_the_intake_mints_each_entry_and_the_next_pass_sees_them_as_existing(world):
    a, b, c, panel, first, _ = await _first_pass(world)
    bars = {row.asset_symbol: row for row in world.requests(a, b, kind=IngestRequestKind.BARS)}

    await world.deliver_bars()

    entries = world.entries(a, b)
    assert set(entries) == {a.symbol, b.symbol}
    for entry in entries.values():
        assert entry.owner == PANEL_OWNER and entry.collect is True and entry.stopped_cause is None
        assert entry.feed == Feed.IEX and entry.update_type == UpdateType.DAILY.value
    assert entries[a.symbol].start == clamped_start(), 'the entry does not declare the clamped start'
    assert entries[b.symbol].start == declared_start(first.as_of)
    for symbol, request in bars.items():
        (after,) = world.rows(sa.select(REQUESTS).where(REQUESTS.c.id == request.id))
        assert after.dataset_id == entries[symbol].id and not after.state.is_open, 'the intake did not set dataset_id'
    async with AsyncSession(world.engine) as db:
        found = await panel_entries(db, SOURCE, PANEL_OWNER, {a.symbol, b.symbol})
    assert {symbol: entry.id for symbol, entry in found.items()} == {s: e.id for s, e in entries.items()}

    world.calls.clear()
    sent = len(world.ingest.sent)
    second = await world.next_membership_pass([panel], [a, b, c])

    await world.tick()

    passes = world.passes(panel, PanelPassKind.MEMBERSHIP)
    assert len(passes) == 2 and passes[1].finished_at is not None
    assert passes[1].active_snapshot_id == second.id
    assert passes[1].counts == _counts(), 'the next pass wrote a row for members whose datasets exist'
    assert len(world.transitions(panel)) == 4
    assert len(world.requests(a, b, c)) == 4 and len(world.ingest.sent) == sent, 'an existing member was re-created'
    # Same settings version: the evidence reads ran over the snapshots since the previous pass.
    (between,) = world.called('snapshots_between')
    assert [snapshot.id for snapshot in between.result] == [second.id]
    (lookup,) = world.called('rows_of_ids')
    assert {(first.id, a.vendor_asset_id), (second.id, a.vendor_asset_id), (second.id, b.vendor_asset_id)} <= set(
        lookup.result
    )
    (by_ids,) = world.called('requests_by_ids')
    assert {record.dataset_id for record in by_ids.result.values()} == {entries[a.symbol].id, entries[b.symbol].id}
    assert set(world.called('panel_entries')[0].result) == {a.symbol, b.symbol}


# --------------------------------------------------------------------------------------------- PM3


async def test_pm3_a_release_pass_row_opens_lazily_and_commits_with_created_and_its_request(world):
    a = world.asset('A')
    world.ingest.refuse.add(a.symbol)
    source_version = await world.source_settings(ramp=10, cap=100)
    panel = await world.panel('pm3', [a])
    active = await world.snapshot([a])

    await world.tick()

    (membership,) = world.passes(panel)
    assert _kinds(world.transitions(panel)) == [
        (a.symbol, PanelTransition.ENTERED),
        (a.symbol, PanelTransition.DEFERRED),
    ]
    assert world.transitions(panel)[1].cause == panel_manager.CAUSE_CLAMP_UNAVAILABLE
    (query,) = world.requests(a, kind=IngestRequestKind.CORPORATE_ACTIONS)
    assert query.state == IngestRequestState.FAILED and query.failure_cause == IngestRequestFailureCause.PERMANENT.value
    assert world.requests(a, kind=IngestRequestKind.BARS) == []

    # A release whose clamp fails again: no pass row at all, one more corporate-actions request, no ledger row.
    await world.tick()

    assert world.passes(panel, PanelPassKind.RELEASE) == [], 'a release that appended nothing left a pass row'
    assert len(world.requests(a, kind=IngestRequestKind.CORPORATE_ACTIONS)) == 2
    assert len(world.transitions(panel)) == 2, 'a repeat deferral with the same cause wrote a row'

    # The release that succeeds.
    world.ingest.refuse.clear()
    world.clamps.clear()

    await world.tick()

    (release,) = world.passes(panel, PanelPassKind.RELEASE)
    (seen_at_clamp,) = world.clamps
    assert seen_at_clamp.release_passes == 0 and seen_at_clamp.idle_in_transaction == 0, seen_at_clamp
    assert seen_at_clamp.app_connections >= 1, 'application_name never reached pg_stat_activity'
    (creation,) = world.creations
    assert (creation.before.release_passes, creation.before.created_rows, creation.before.request_rows) == (0, 0, 0)
    assert (creation.after.release_passes, creation.after.created_rows, creation.after.request_rows) == (1, 1, 1)
    assert release.finished_at is not None and release.counts == _counts(created=1)
    assert (release.settings_version_id, release.source_settings_version_id) == (panel.version_id, source_version)
    assert (release.active_snapshot_id, release.inactive_snapshot_id) == (
        membership.active_snapshot_id,
        membership.inactive_snapshot_id,
    )
    rows = world.transitions(panel)
    assert _kinds(rows)[-1] == (a.symbol, PanelTransition.CREATED) and rows[-1].pass_id == release.id
    deferred, created = rows[1], rows[-1]
    assert deferred.as_of == created.as_of == active.as_of, 'the release did not date CREATED by the deciding snapshot'
    assert release.started_at > membership.started_at

    # The point-in-time start (ADR tj-iov38l P4): the release read the ENTERED date on asyncpg (one read per release
    # tick: the failed one and this one; none in the membership pass, whose member was new) and the bars request starts
    # at its New York midnight.
    entered_reads = world.called('entered_as_of_by_member')
    assert len(entered_reads) == 2 and all(call.args == (panel.id,) for call in entered_reads), entered_reads
    assert (
        entered_reads[-1].result == {a.vendor_asset_id: rows[0].as_of} and rows[0].transition is PanelTransition.ENTERED
    )
    (bars,) = world.requests(a, kind=IngestRequestKind.BARS)
    assert bars.requested_start == declared_start(rows[0].as_of)

    # The tie on as_of is broken by started_at: the release's CREATED is the member's state.
    assert (await world.latest(panel))[a.vendor_asset_id].transition is PanelTransition.CREATED

    # Same second, microsecond resolution: rewrite both started_at into one second, 1 us apart, either way round.
    second = datetime(2026, 10, 5, 20, 0, 0, 250_000, tzinfo=UTC)
    for membership_at, release_at, expected in (
        (second, second + MICRO, PanelTransition.CREATED),
        (second + MICRO, second, PanelTransition.DEFERRED),
    ):
        with world.observer.begin() as conn:
            conn.execute(sa.update(PASSES).where(PASSES.c.id == membership.id).values(started_at=membership_at))
            conn.execute(sa.update(PASSES).where(PASSES.c.id == release.id).values(started_at=release_at))
        stored = {row.id: row.started_at for row in world.passes(panel)}
        assert (stored[membership.id], stored[release.id]) == (membership_at, release_at), 'microseconds were lost'
        assert (await world.latest(panel))[a.vendor_asset_id].transition is expected, (membership_at, release_at)


# --------------------------------------------------------------------------------------------- PM4


async def test_pm4_a_crashed_pass_holds_the_next_off_for_the_retry_floor_from_its_database_started_at(world):
    a = world.asset('A')
    await world.source_settings(ramp=10, cap=100)
    panel = await world.panel('pm4', [a])
    await world.snapshot([a])
    world.crash_finish = True

    await world.tick(expect_failure=True)

    (crashed,) = world.passes(panel)
    assert crashed.finished_at is None and crashed.counts is None, 'the crashed pass was finished'
    assert _kinds(world.transitions(panel)) == [
        (a.symbol, PanelTransition.ENTERED),
        (a.symbol, PanelTransition.CREATED),
    ]
    await world.deliver_bars()
    held_rows = len(world.transitions(panel))

    # Held off, on the app clock, right after the crash and up to 1 us before started_at + the floor.
    for pinned in (None, crashed.started_at + panel_manager.UNFINISHED_PASS_RETRY - MICRO):
        world.clock.pinned = pinned
        await world.tick()
        assert len(world.passes(panel)) == 1, f'a pass ran inside the retry floor (clock {pinned})'

    # At exactly started_at + the floor the next pass runs; the crashed row stays as written.
    world.clock.pinned = crashed.started_at + panel_manager.UNFINISHED_PASS_RETRY
    await world.tick()

    passes = world.passes(panel)
    assert len(passes) == 2 and passes[0].id == crashed.id
    assert passes[1].kind == PanelPassKind.MEMBERSHIP and passes[1].finished_at is not None
    assert (passes[0].finished_at, passes[0].counts, passes[0].started_at) == (None, None, crashed.started_at)
    assert passes[1].counts == _counts() and len(world.transitions(panel)) == held_rows
    assert len(world.requests(a, kind=IngestRequestKind.BARS)) == 1, 'the crashed pass member was created twice'


# --------------------------------------------------------------------------------------------- PM5, PM6


async def test_pm5_a_restart_mid_window_creates_nothing_more_and_the_ramp_edge_is_database_time(world):
    await world.require_quiet_windows()
    a, b = world.asset('A'), world.asset('B')
    await world.source_settings(ramp=1, cap=100)
    panel = await world.panel('pm5', [a, b])
    await world.snapshot([a, b])

    await world.tick()

    rows = world.transitions(panel)
    assert _kinds(rows) == [
        (a.symbol, PanelTransition.ENTERED),
        (a.symbol, PanelTransition.CREATED),
        (b.symbol, PanelTransition.ENTERED),
        (b.symbol, PanelTransition.DEFERRED),
    ]
    assert rows[3].cause == panel_manager.CAUSE_RAMP_RATE
    recorded_at = rows[1].recorded_at
    await world.deliver_bars()
    requests = len(world.requests(a, b))

    # A restart: a fresh manager, nothing in memory, on the real clock and then at the window's last instant.
    for pinned in (None, recorded_at + panel_manager.RATE_WINDOW):
        world.clock.pinned = pinned
        await world.tick(world.new_manager())
        assert world.passes(panel, PanelPassKind.RELEASE) == [], f'a restart released inside the window ({pinned})'
        assert len(world.requests(a, b)) == requests and len(world.transitions(panel)) == 4

    # 1 us later the CREATED has left the window (its recorded_at, the database's clock) and b is released.
    world.clock.pinned = recorded_at + panel_manager.RATE_WINDOW + MICRO
    await world.tick(world.new_manager())

    (release,) = world.passes(panel, PanelPassKind.RELEASE)
    assert _kinds(world.transitions(panel))[-1] == (b.symbol, PanelTransition.CREATED)
    assert release.counts == _counts(created=1)


async def test_pm6_the_request_window_edge_is_the_clamp_requests_database_created_at(world):
    await world.require_quiet_windows()
    a, b = world.asset('A'), world.asset('B')
    await world.source_settings(ramp=10, cap=3)
    panel = await world.panel('pm6', [a, b])
    await world.snapshot([a, b])

    await world.tick()

    rows = world.transitions(panel)
    assert _kinds(rows)[-1] == (b.symbol, PanelTransition.DEFERRED)
    assert rows[-1].cause == panel_manager.CAUSE_REQUEST_RATE, 'two requests a creation: 2 + 2 > 3'
    (query,) = world.requests(a, kind=IngestRequestKind.CORPORATE_ACTIONS)
    (bars,) = world.requests(a, kind=IngestRequestKind.BARS)
    assert query.created_at < bars.created_at
    await world.deliver_bars()

    # Window start at the clamp request's created_at: both of a's requests count, b stays deferred.
    world.clock.pinned = query.created_at + panel_manager.RATE_WINDOW
    await world.tick(world.new_manager())
    assert world.passes(panel, PanelPassKind.RELEASE) == []

    # 1 us later only the bars request counts: 1 + 2 <= 3, b is released.
    world.clock.pinned = query.created_at + panel_manager.RATE_WINDOW + MICRO
    await world.tick(world.new_manager())

    (release,) = world.passes(panel, PanelPassKind.RELEASE)
    assert release.counts == _counts(created=1)
    assert len(world.requests(b, kind=IngestRequestKind.BARS)) == 1


# --------------------------------------------------------------------------------------------- PM7


async def test_pm7_a_filter_exit_stops_the_entry_only_when_no_enabled_panel_still_selects_it(world):
    a, b = world.asset('A'), world.asset('B')
    await world.source_settings(ramp=10, cap=100)
    first = await world.panel('pm7-first', [a, b])
    other = await world.panel('pm7-other', [a, b])
    await world.snapshot([a, b])

    await world.tick()

    # One dataset per symbol: the first panel creates, the other sees the requests in flight and only enters.
    assert [t for _, t in _kinds(world.transitions(first))].count(PanelTransition.CREATED) == 2
    assert _kinds(world.transitions(other)) == [
        (a.symbol, PanelTransition.ENTERED),
        (b.symbol, PanelTransition.ENTERED),
    ]
    assert len(world.requests(a, b, kind=IngestRequestKind.BARS)) == 2
    await world.deliver_bars()

    # The first panel drops b; the other still selects it: EXITED, and the entry keeps collecting.
    await world.new_version(first, [a])
    snap = await world.next_membership_pass([first, other], [a, b])
    await world.tick()

    exit_row = world.transitions(first)[-1]
    assert (exit_row.symbol, exit_row.transition, exit_row.as_of) == (b.symbol, PanelTransition.EXITED, snap.as_of)
    assert world.entries(b)[b.symbol].collect is True, 'a shared member was stopped while another panel selects it'
    assert world.called('stop_collecting') == [] and world.published == []

    # The other panel drops b too: no enabled panel selects it any more, so its entry is stopped.
    await world.new_version(other, [a])
    await world.next_membership_pass([first, other], [a, b])
    await world.tick()

    assert _kinds(world.transitions(other))[-1] == (b.symbol, PanelTransition.EXITED)
    entries = world.entries(a, b)
    # A manager stop is marked PANEL_MEMBER_LEFT in the same UPDATE (P22r tj-7zjov1.264, addendum 23 rule A (1)).
    assert (entries[b.symbol].collect, entries[b.symbol].stopped_cause) == (False, LEFT)
    assert entries[a.symbol].collect is True
    assert [call.result for call in world.called('stop_collecting')] == [True]
    assert world.published == [DatasetChanged(entries[b.symbol].id, DatasetChange.RETIRED)]


# --------------------------------------------------------------------------------------------- PM8


async def test_pm8_a_delisted_member_drains_until_a_fetch_finishes_then_its_entry_stops(world):
    a, z = world.asset('A'), world.asset('Z')
    await world.source_settings(ramp=10, cap=100)
    panel = await world.panel('pm8', [a])
    await world.snapshot([a, z])
    await world.tick()
    await world.deliver_bars()
    entry = world.entries(a)[a.symbol]

    # a is gone from the list (z keeps the snapshot non-empty, and is not selected): DRAINING while the entry exists.
    gone = await world.next_membership_pass([panel], [z])
    await world.tick()

    draining = world.transitions(panel)[-1]
    assert (draining.transition, draining.as_of) == (PanelTransition.DRAINING, gone.as_of)

    # No fetch has finished since: the next pass writes nothing.
    await world.next_membership_pass([panel], [z])
    await world.tick()
    assert world.transitions(panel)[-1].id == draining.id
    assert world.called('fetch_finished_since')[-1].result is False

    # A refresh of the entry finished after the drain began: DRAINED, and the entry is stopped.
    with world.observer.begin() as conn:
        conn.execute(
            REQUESTS.insert().values(
                kind=IngestRequestKind.BARS,
                source=SOURCE,
                dataset_id=entry.id,
                asset_symbol=a.symbol,
                client_label='scheduler',
                state=IngestRequestState.SUCCEEDED,
                created_at=draining.as_of + timedelta(minutes=1),
            )
        )
    await world.next_membership_pass([panel], [z])
    await world.tick()

    assert world.transitions(panel)[-1].transition is PanelTransition.DRAINED
    assert world.called('fetch_finished_since')[-1].result is True
    after = world.entries(a)[a.symbol]
    assert (after.collect, after.stopped_cause) == (False, LEFT), 'a DRAINED stop is a manager stop: marked'
    assert world.published == [DatasetChanged(entry.id, DatasetChange.RETIRED)]


# --------------------------------------------------------------------------------------------- PM9


@dataclass(frozen=True)
class Restart:
    """What the observer connection saw of one entry and its panel's ENTERED rows at one point of the pass."""

    at: str
    collect: bool | None
    stopped_cause: str | None
    resumed_entered_rows: int


async def test_pm9_a_re_entry_restarts_a_manager_stopped_entry_in_the_commit_of_its_entered_row(world):
    a, b, c = world.asset('A'), world.asset('B'), world.asset('C')
    # d is never listed: a version allowing only d selects none of a, b, c, so all three are filter exits.
    d = world.asset('D')
    await world.source_settings(ramp=10, cap=100)
    panel = await world.panel('pm9', [a, b, c])
    await world.snapshot([a, b, c])
    await world.tick()

    # b's bars are delivered under ANOTHER vendor asset id: its last accepted id is not b's member id (a reused symbol).
    world.vendor_of[b.symbol] = f'{b.vendor_asset_id}-other-holder'
    await world.deliver_bars()
    entries = world.entries(a, b, c)
    assert all(entry.collect is True and entry.stopped_cause is None for entry in entries.values())
    ids = {symbol: entry.id for symbol, entry in entries.items()}

    # All three exit: each entry stopped by the manager, marked in the same UPDATE.
    await world.new_version(panel, [d])
    await world.next_membership_pass([panel], [a, b, c])
    await world.tick()

    stopped = world.entries(a, b, c)
    assert {s: (e.collect, e.stopped_cause) for s, e in stopped.items()} == dict.fromkeys(ids, (False, LEFT))
    assert sorted(call.result for call in world.called('stop_collecting')) == [True] * 3
    # The CHECK holds on Postgres: a cause on a collecting row is refused.
    with pytest.raises(sa.exc.IntegrityError), world.observer.begin() as conn:
        conn.execute(sa.update(ENTRIES).where(ENTRIES.c.id == ids[a.symbol]).values(collect=True))

    # Observe a's entry and the panel's resumed ENTERED rows from the second connection at each step of the next pass.
    seen: list[Restart] = []

    def observe(at: str) -> None:
        (row,) = world.rows(sa.select(ENTRIES).where(ENTRIES.c.id == ids[a.symbol]))
        resumed = world._count(
            sa.select(sa.func.count())
            .select_from(TRANSITIONS)
            .where(
                TRANSITIONS.c.panel_id == panel.id,
                TRANSITIONS.c.transition == PanelTransition.ENTERED,
                TRANSITIONS.c.dataset_id.is_not(None),
            )
        )
        seen.append(Restart(at, row.collect, row.stopped_cause, resumed))

    mp = world.monkeypatch
    real_vendor = panel_manager.last_accepted_vendor_asset_id

    async def vendor_then_operator(db, dataset_id):
        if dataset_id == ids[c.symbol]:
            # An operator Resume then Stop of c, committed on another connection between the pass's read of c
            # (collect false, PANEL_MEMBER_LEFT) and its guarded UPDATE: c is now collect false with NO cause.
            with world.observer.begin() as conn:
                conn.execute(
                    sa.update(ENTRIES).where(ENTRIES.c.id == dataset_id).values(collect=False, stopped_cause=None)
                )
        return await real_vendor(db, dataset_id)

    mp.setattr(panel_manager, 'last_accepted_vendor_asset_id', vendor_then_operator)

    resumes: list[tuple[uuid.UUID, bool]] = []
    real_resume = panel_manager.resume_panel_entry

    async def resume(db, dataset_id):
        flipped = await real_resume(db, dataset_id)
        resumes.append((dataset_id, flipped))
        if dataset_id == ids[a.symbol]:
            observe('after the guarded UPDATE, before the commit')
        return flipped

    mp.setattr(panel_manager, 'resume_panel_entry', resume)

    real_finish = panel_manager.finish_pass

    async def finish(db, pass_id, counts):
        observe('at finish_pass, before the commit')
        return await real_finish(db, pass_id, counts)

    mp.setattr(panel_manager, 'finish_pass', finish)

    published_seen: list[tuple[Any, Restart]] = []

    def publish(event) -> None:
        observe(f'at publish {event.change.name}')
        published_seen.append((event, seen[-1]))

    mp.setattr(panel_manager, 'publish', publish)

    # They re-enter.
    await world.new_version(panel, [a, b, c])
    await world.next_membership_pass([panel], [a, b, c])
    await world.tick()

    # a: never visible half done; resumed and named by its ENTERED row together, at the commit.
    assert seen[:2] == [
        Restart('after the guarded UPDATE, before the commit', False, LEFT, 0),
        Restart('at finish_pass, before the commit', False, LEFT, 0),
    ], seen
    assert published_seen == [
        (DatasetChanged(ids[a.symbol], DatasetChange.RESUMED), Restart('at publish RESUMED', True, None, 1))
    ], 'RESUMED must be published once, for a, after its commit'
    # The guarded UPDATE ran for a (flipped) and c (matched nothing: the operator's write won), never for b.
    assert resumes == [(ids[a.symbol], True), (ids[c.symbol], False)], resumes

    entered = {row.symbol: row for row in world.transitions(panel)[-3:]}
    assert set(entered) == {a.symbol, b.symbol, c.symbol}
    assert all(row.transition is PanelTransition.ENTERED for row in entered.values())
    assert (entered[a.symbol].dataset_id, entered[a.symbol].cause) == (
        ids[a.symbol],
        panel_manager.CAUSE_DATASET_RESUMED,
    )
    assert (entered[b.symbol].dataset_id, entered[b.symbol].cause) == (None, None), 'a reused symbol was resumed'
    assert (entered[c.symbol].dataset_id, entered[c.symbol].cause) == (None, None), 'the operator race named c'

    after = world.entries(a, b, c)
    assert (after[a.symbol].collect, after[a.symbol].stopped_cause) == (True, None)
    assert (after[b.symbol].collect, after[b.symbol].stopped_cause) == (False, LEFT)
    assert (after[c.symbol].collect, after[c.symbol].stopped_cause) == (False, None), (
        "the operator's row was overwritten"
    )
    assert after[a.symbol].updated_at > stopped[a.symbol].updated_at
    # A restart writes no request: the gap is the DAILY refresh's.
    assert len(world.requests(a, b, c, kind=IngestRequestKind.BARS)) == 3


# --------------------------------------------------------------------------------------------- PM10 include_otc


async def test_pm10_include_otc_off_exits_and_stops_the_otc_member_and_on_restarts_it(world):
    """ADR tj-ed4t8m D3/D5 on asyncpg (validator tj-7zjov1.364.4).

    WRITTEN, NOT YET RUN: the Postgres window is tj-7zjov1.364.7, on a test database dropped and migrated again, since
    the baseline was edited in place. The include_otc column round-trips through create_panel and append_settings_version on the real schema; turning it
    off is a filter exit of the OTC member only (its entry stopped PANEL_MEMBER_LEFT, RETIRED); turning it on again
    re-enters it with dataset_resumed and RESUMED, and writes no request.
    """
    a, o = world.asset('A'), world.asset('O', exchange='OTC')
    await world.source_settings(ramp=10, cap=100)
    panel = await world.panel('pm10', [a, o], include_otc=True)
    await world.snapshot([a, o])
    await world.tick()

    assert [t for _, t in _kinds(world.transitions(panel))].count(PanelTransition.CREATED) == 2
    await world.deliver_bars()
    ids = {symbol: entry.id for symbol, entry in world.entries(a, o).items()}

    await world.new_version(panel, [a, o], include_otc=False)
    await world.next_membership_pass([panel], [a, o])
    await world.tick()

    assert _kinds(world.transitions(panel))[-1] == (o.symbol, PanelTransition.EXITED)
    stopped = world.entries(a, o)
    assert (stopped[o.symbol].collect, stopped[o.symbol].stopped_cause) == (False, LEFT)
    assert (stopped[a.symbol].collect, stopped[a.symbol].stopped_cause) == (True, None)
    assert world.published == [DatasetChanged(ids[o.symbol], DatasetChange.RETIRED)]

    await world.new_version(panel, [a, o], include_otc=True)
    await world.next_membership_pass([panel], [a, o])
    await world.tick()

    last = world.transitions(panel)[-1]
    assert (last.symbol, last.transition, last.dataset_id, last.cause) == (
        o.symbol,
        PanelTransition.ENTERED,
        ids[o.symbol],
        panel_manager.CAUSE_DATASET_RESUMED,
    )
    resumed = world.entries(o)[o.symbol]
    assert (resumed.collect, resumed.stopped_cause) == (True, None)
    assert world.published == [
        DatasetChanged(ids[o.symbol], DatasetChange.RETIRED),
        DatasetChanged(ids[o.symbol], DatasetChange.RESUMED),
    ]
    assert len(world.requests(a, o, kind=IngestRequestKind.BARS)) == 2
