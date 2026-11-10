"""The SQLite harness for the panel manager tests (validator, tj-7zjov1.180, P22v). Not a test module.

What it stands up, for test_panel_manager_pass.py:

  * one SQLite engine (aiosqlite, StaticPool) holding every table the pass reads or writes: the five panel tables,
    ingest_request and ingest_instance, store_dataset_entry, asset_list_snapshot and asset_list_row, copied into a
    private MetaData (the Postgres ARRAY and JSONB columns as JSON, UUID columns as TEXT-affinity Uuid, as
    test_panel_ledger.py does), with timestamptz read back aware;
  * panel_manager.own_session pointed at it, every session RECORDED: its commits, and which one opened a pass (the
    pass session P);
  * the three seams the pass reaches past the database replaced IN THE MANAGER'S NAMESPACE ONLY:
      first_request_clamp  a double that writes the corporate-actions request row on the session it is GIVEN (as the
                           real one does, so the request window counts it), records that session, and answers a clamp,
                           no clamp, or raises, per symbol; like the real one, a clamped start not after the body's
                           start is no clamp;
      submit_record        a recorder (the real one is pinned by test_post_request_path.py); it reads the bars request
                           and its CREATED row from a SECOND session when called, which is what "submit after the
                           commit" means;
      publish              a recorder.
    The real ledger crud, settings crud, request crud, asset-list reads, check_own_overlap, stop_collecting,
    resume_panel_entry and last_accepted_vendor_asset_id (over ingest_run, tj-7zjov1.264) run.

Postgres-only behaviour (asyncpg, the ARRAY and JSONB columns, enum casts, the window counts on database time under
real concurrency) is the system tier's: see the verdict note on tj-7zjov1.180.
"""

import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from common.database.sql_alchemy_nullable_datetime import NullableDateTime
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app import panel_manager
from data.store.app.client_label import PANEL_OWNER
from data.store.app.database.crud.ingest_request import create_request
from data.store.app.database.crud.panel_ledger import NewTransition, append_transitions, finish_pass, open_pass
from data.store.app.database.models.asset_list import AssetListRow, AssetListSnapshot
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import (
    IngestRequest,
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.database.models.panel import (
    Panel,
    PanelCadence,
    PanelMemberTransition,
    PanelPass,
    PanelPassKind,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
)
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.ingest.data_action_request import StartClamp, new_corporate_actions_request
from data.store.app.trading_calendar import CALENDAR_START
from data.store.tests.request_path_harness import patch_aware_sqlite_datetimes
from schemas.data_ingest.submit import AssetListStatus


PANELS = Panel.__table__
SETTINGS = PanelSettingsVersion.__table__
SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__
PASSES = PanelPass.__table__
TRANSITIONS = PanelMemberTransition.__table__
REQUESTS = IngestRequest.__table__
INSTANCES = IngestInstance.__table__
ENTRIES = StoreDatasetEntry.__table__
SNAPSHOTS = AssetListSnapshot.__table__
ASSET_ROWS = AssetListRow.__table__
# ingest_run: the re-entry restart reads last_accepted_vendor_asset_id (tj-7zjov1.264, ADR tj-26jt9w addendum 23).
RUNS = IngestRun.__table__
TABLES = (
    PANELS,
    SETTINGS,
    SOURCE_SETTINGS,
    PASSES,
    TRANSITIONS,
    REQUESTS,
    INSTANCES,
    ENTRIES,
    SNAPSHOTS,
    ASSET_ROWS,
    RUNS,
)

SOURCE = DataSource.ALPACA_API
# An existing panel entry's declared start (World.entry): the point-in-time start (ADR tj-iov38l P1) of a member that
# entered on 2026-01-01, New York midnight (EST). A stored start is never rewritten by a later pass (P4, re-entry).
ENTRY_START = datetime(2026, 1, 1, 5, tzinfo=UTC)
SETTLE = timedelta(minutes=30)
CLOSE_UTC = time(21, 0)


class EveryDayCalendar:
    """A SessionCalendar where every calendar day is a session closing at 21:00 UTC.

    The pass tests' clock arithmetic stays obvious. The real XNYS calendar is test_panel_manager_rules.py's (pin (10)).
    """

    def session_open(self, session: date) -> datetime:
        return datetime.combine(session, time(14, 30), tzinfo=UTC)

    def session_close(self, session: date) -> datetime:
        return datetime.combine(session, CLOSE_UTC, tzinfo=UTC)

    def next_session(self, session: date) -> date:
        return session + timedelta(days=1)

    def last_completed_session(self, now: datetime) -> date | None:
        today = now.astimezone(UTC).date()
        return today if now >= self.session_close(today) else today - timedelta(days=1)


class Clock:
    """The injected clock: real now by default (the rate windows compare it with database time), movable."""

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset


@dataclass
class ClampCall:
    symbol: str
    session: AsyncSession
    body: Any
    pass_session_in_transaction: bool | None
    # How many panel_pass rows exist at the moment of the clamp (a RELEASE pass row must not exist yet, tj-7zjov1.228).
    passes_visible: int = 0


@dataclass
class SubmitCall:
    request_id: uuid.UUID
    kind: IngestRequestKind
    # Read from a second session at the moment of the call.
    request_row_visible: bool
    created_row_visible: bool
    # The CREATED row's own pass row exists (a lazily opened RELEASE pass commits with it, tj-7zjov1.228).
    pass_row_visible: bool = False


@dataclass(frozen=True)
class Seeded:
    panel_id: uuid.UUID
    settings_id: uuid.UUID


@dataclass
class World:
    """The engine, the recorders and the set-up writes."""

    engine: AsyncEngine
    sessions: list[AsyncSession] = field(default_factory=list)
    commits: dict[int, int] = field(default_factory=dict)
    pass_sessions: list[AsyncSession] = field(default_factory=list)
    clamps: list[ClampCall] = field(default_factory=list)
    submits: list[SubmitCall] = field(default_factory=list)
    published: list[Any] = field(default_factory=list)
    clamp_starts: dict[str, datetime] = field(default_factory=dict)
    clamp_errors: dict[str, Exception] = field(default_factory=dict)
    submit_error: Exception | None = None
    _versions: dict[Any, int] = field(default_factory=dict)

    # ------------------------------------------------------------------ sessions

    def session(self) -> AsyncSession:
        world = self

        class Recording(AsyncSession):
            async def commit(self) -> None:
                await super().commit()
                world.commits[id(self)] = world.commits.get(id(self), 0) + 1

        db = Recording(self.engine, expire_on_commit=False)
        self.sessions.append(db)
        return db

    @asynccontextmanager
    async def own_session(self) -> AsyncIterator[AsyncSession]:
        db = self.session()
        try:
            yield db
        finally:
            await db.close()

    async def put(self, statement: Any) -> Any:
        async with AsyncSession(self.engine) as db:
            result = await db.execute(statement)
            await db.commit()
            return result

    async def rows(self, table: sa.Table, *where: Any) -> list[sa.Row]:
        async with AsyncSession(self.engine) as db:
            return list((await db.execute(sa.select(table).where(*where))).all())

    # ------------------------------------------------------------------ set-up rows

    async def source_settings(
        self, ramp: int | None = 10, cap: int | None = 100, source: DataSource = SOURCE
    ) -> uuid.UUID:
        """A source settings version; a rate of None is stored NULL, NO LIMIT (ADR tj-2ayia2)."""
        number = self._versions.get(source, 0) + 1
        self._versions[source] = number
        result = await self.put(
            SOURCE_SETTINGS.insert()
            .values(
                source=source,
                version_no=number,
                ramp_datasets_per_hour=ramp,
                request_cap_per_hour=cap,
                corporate_action_window_pre_days=90,
                corporate_action_window_post_days=90,
                actor='operator',
                reason=f'rates {number}',
            )
            .returning(SOURCE_SETTINGS.c.id)
        )
        return result.scalar_one()

    async def settings(self, panel_id: uuid.UUID, **kw: Any) -> uuid.UUID:
        number = self._versions.get(panel_id, 0) + 1
        self._versions[panel_id] = number
        values = {
            'panel_id': panel_id,
            'version_no': number,
            'enabled': True,
            'cadence': PanelCadence.DAILY,
            'allow_symbols': [],
            'deny_symbols': [],
            'actor': 'operator',
            'reason': f'settings {number}',
            # The column is NOT NULL with no schema default (ADR tj-ed4t8m D1), so a row written behind the crud's back
            # states it: off, the ruled default, unless the test names it.
            'include_otc': False,
        } | kw
        return (await self.put(SETTINGS.insert().values(**values).returning(SETTINGS.c.id))).scalar_one()

    async def panel(self, created_at: datetime, *, name: str | None = None, **settings: Any) -> Seeded:
        panel_id = (
            await self.put(
                PANELS.insert()
                .values(name=name or f'panel-{uuid.uuid4().hex[:8]}', source=SOURCE, created_at=created_at)
                .returning(PANELS.c.id)
            )
        ).scalar_one()
        return Seeded(panel_id, await self.settings(panel_id, **settings))

    async def snapshot(
        self,
        as_of: datetime,
        assets: Iterable[tuple[str, str] | tuple[str, str, dict[str, Any]]],
        status: AssetListStatus = AssetListStatus.ACTIVE,
    ) -> uuid.UUID:
        """A snapshot of (vendor id, symbol[, row overrides]) rows; exchange NYSE, active and tradable True."""
        assets = list(assets)
        snapshot_id = uuid.uuid4()
        await self.put(
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
            vendor, symbol, extra = asset[0], asset[1], (asset[2] if len(asset) > 2 else {})
            values = {
                'snapshot_id': snapshot_id,
                'vendor_asset_id': vendor,
                'symbol': symbol,
                'name': symbol,
                'exchange': 'NYSE',
                'active': status is AssetListStatus.ACTIVE,
                'tradable': True,
                'raw_payload': b'{}',
            } | extra
            await self.put(ASSET_ROWS.insert().values(**values))
        return snapshot_id

    async def entry(
        self, symbol: str, *, collect: bool | None = True, stopped_cause: str | None = None, **kw
    ) -> uuid.UUID:
        entry_id = uuid.uuid4()
        values = {
            'id': entry_id,
            'owner': PANEL_OWNER,
            'source': SOURCE,
            'feed': Feed.IEX,
            'asset_symbol': symbol,
            'asset_type': AssetType.STOCK,
            'data_type': DataType.MARKET_ACTIVITY,
            'granularity': Granularity.ONE_DAY,
            'start': ENTRY_START,
            'end': NullableDateTime.EPOCH,
            'expiry_type': ExpiryType.ROLLING,
            'update_type': UpdateType.DAILY,
            'collect': collect,
            'stopped_cause': stopped_cause,
            'created_at': datetime(2026, 1, 1, tzinfo=UTC),
            'updated_at': datetime(2026, 1, 1, tzinfo=UTC),
        } | kw
        async with AsyncSession(self.engine) as db:
            db.add(StoreDatasetEntry(**values))
            await db.commit()
        return entry_id

    async def request(
        self,
        symbol: str,
        *,
        kind: IngestRequestKind = IngestRequestKind.BARS,
        state: IngestRequestState = IngestRequestState.QUEUED,
        client_label: str = PANEL_OWNER,
        **values: Any,
    ) -> uuid.UUID:
        row = {
            'kind': kind,
            'source': SOURCE,
            'asset_symbol': symbol,
            'client_label': client_label,
            'state': state,
        } | values
        return (await self.put(REQUESTS.insert().values(**row).returning(REQUESTS.c.id))).scalar_one()

    async def bars_attempt(
        self,
        dataset_id: uuid.UUID,
        vendor_asset_id: str | None,
        *,
        state: IngestRunState = IngestRunState.SUCCEEDED,
        finished_at: datetime | None = None,
    ) -> uuid.UUID:
        """A bars attempt row of a dataset (granularity set), the base last_accepted_vendor_asset_id reads."""
        values = {
            'dataset_id': dataset_id,
            'trigger': IngestRunTrigger.SCHEDULE,
            'actor': 'scheduler',
            'asset_symbol': 'AAA',
            'asset_type': AssetType.STOCK,
            'data_type': DataType.MARKET_ACTIVITY,
            'source': SOURCE,
            'granularity': Granularity.ONE_DAY,
            'state': state,
            'vendor_asset_id': vendor_asset_id,
            'finished_at': finished_at or datetime.now(UTC) - timedelta(days=5),
        }
        return (await self.put(RUNS.insert().values(**values).returning(RUNS.c.id))).scalar_one()

    async def set_request(self, request_id: uuid.UUID, **values: Any) -> None:
        await self.put(REQUESTS.update().where(REQUESTS.c.id == request_id).values(**values))

    async def ledger_pass(
        self,
        seeded: Seeded,
        snapshot_id: uuid.UUID,
        rows: Iterable[NewTransition],
        *,
        started_at: datetime,
        kind: PanelPassKind = PanelPassKind.MEMBERSHIP,
        settings_id: uuid.UUID | None = None,
        dry_run: bool = False,
    ) -> uuid.UUID:
        """A finished pass written through the ledger crud, behind the manager (an earlier pass's rows)."""
        source_settings = (await self.rows(SOURCE_SETTINGS, SOURCE_SETTINGS.c.source == SOURCE))[-1].id
        async with AsyncSession(self.engine) as db:
            pass_id = await open_pass(
                db,
                panel_id=seeded.panel_id,
                kind=kind,
                settings_version_id=settings_id or seeded.settings_id,
                source_settings_version_id=source_settings,
                active_snapshot_id=snapshot_id,
                inactive_snapshot_id=None,
                dry_run=dry_run,
            )
            await append_transitions(db, pass_id, list(rows))
            await finish_pass(db, pass_id, {})
            await db.commit()
        await self.put(PASSES.update().where(PASSES.c.id == pass_id).values(started_at=started_at))
        return pass_id

    async def age_window(self, by: timedelta = timedelta(hours=2)) -> None:
        """Move every CREATED row and every request out of the trailing hour (database time is real time)."""
        old = datetime.now(UTC) - by
        await self.put(TRANSITIONS.update().values(recorded_at=old))
        await self.put(REQUESTS.update().values(created_at=old))

    # ------------------------------------------------------------------ reads

    async def passes(self, seeded: Seeded | None = None) -> list[sa.Row]:
        where = [] if seeded is None else [PASSES.c.panel_id == seeded.panel_id]
        found = await self.rows(PASSES, *where)
        return sorted(found, key=lambda row: (row.started_at, str(row.id)))

    async def transitions(self, seeded: Seeded | None = None) -> list[sa.Row]:
        where = [] if seeded is None else [TRANSITIONS.c.panel_id == seeded.panel_id]
        found = await self.rows(TRANSITIONS, *where)
        order = {row.id: index for index, row in enumerate(await self.passes())}
        return sorted(found, key=lambda row: (order.get(row.pass_id, -1), row.seq))

    async def requests(self, kind: IngestRequestKind | None = None) -> list[sa.Row]:
        found = await self.rows(REQUESTS)
        return [row for row in found if kind is None or row.kind is kind]

    async def entry_row(self, entry_id: uuid.UUID) -> sa.Row:
        (row,) = await self.rows(ENTRIES, ENTRIES.c.id == entry_id)
        return row


def kinds(rows: Iterable[sa.Row]) -> list[tuple[str, str]]:
    """(vendor id, transition name) per ledger row, in order."""
    return [(row.vendor_asset_id, row.transition.name) for row in rows]


async def make_world(monkeypatch: pytest.MonkeyPatch) -> World:
    patch_aware_sqlite_datetimes(monkeypatch)
    for column in (SETTINGS.c.allow_symbols, SETTINGS.c.deny_symbols, PASSES.c.counts, REQUESTS.c.data_types):
        monkeypatch.setattr(column, 'type', sa.JSON())
    metadata = sa.MetaData()
    copies = {table.name: table.to_metadata(metadata) for table in TABLES}
    for name in ('allow_symbols', 'deny_symbols'):
        copies[SETTINGS.name].c[name].server_default = sa.DefaultClause(sa.text("'[]'"))
    for copy in copies.values():
        for column in copy.columns:
            if isinstance(column.type, sa.UUID):
                column.type = sa.Uuid()
    engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, 'connect')
    def register(dbapi_connection, _record):
        dbapi_connection.run_async(lambda c: c.create_function('gen_random_uuid', 0, lambda: uuid.uuid4().hex))
        dbapi_connection.run_async(lambda c: c.create_function('btrim', 1, lambda s: None if s is None else s.strip()))

    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)

    world = World(engine)
    install(world, monkeypatch)
    return world


def install(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the manager's own_session at the engine and replace its three outward seams."""
    monkeypatch.setattr(panel_manager, 'own_session', world.own_session)
    monkeypatch.setattr(panel_manager, 'publish', world.published.append)

    real_open_pass = panel_manager.open_pass

    async def recording_open_pass(db, **kw):
        world.pass_sessions.append(db)
        return await real_open_pass(db, **kw)

    monkeypatch.setattr(panel_manager, 'open_pass', recording_open_pass)

    async def first_request_clamp(path, body, db, client, *, deadline_s):
        symbol = path.asset_symbol
        pass_db = world.pass_sessions[-1] if world.pass_sessions else None
        world.clamps.append(
            ClampCall(
                symbol,
                db,
                body,
                None if pass_db is None else pass_db.in_transaction(),
                passes_visible=len(await world.rows(PASSES)),
            )
        )
        record = await create_request(
            db,
            new_corporate_actions_request(
                body.source,
                symbol,
                # The real clamp's window (P51): CALENDAR_START at UTC midnight to now, whatever the body's start.
                datetime.combine(CALENDAR_START, time.min, tzinfo=UTC),
                datetime.now(UTC),
                trigger=IngestRunTrigger.REQUEST,
                client_label=body.owner,
                ready_by=body.ready_by,
            ),
        )
        if symbol in world.clamp_errors:
            raise world.clamp_errors[symbol]
        clamped = world.clamp_starts.get(symbol)
        # As the real clamp (first_request_clamp): a clamped start not after the body's start leaves it standing. Since
        # the point-in-time start (ADR tj-iov38l P5) a past rename usually clamps nothing, so the double must say so.
        if clamped is None or clamped <= body.start:
            return None, record.id
        return StartClamp(symbol, clamped, body.start, clamped), record.id

    monkeypatch.setattr(panel_manager, 'first_request_clamp', first_request_clamp)

    async def submit_record(record, client, *, recovered: bool = False):
        request_rows = await world.rows(REQUESTS, REQUESTS.c.id == record.id)
        created_rows = await world.rows(TRANSITIONS, TRANSITIONS.c.request_id == record.id)
        pass_rows = await world.rows(PASSES, PASSES.c.id == created_rows[0].pass_id) if created_rows else []
        world.submits.append(
            SubmitCall(record.id, record.kind, bool(request_rows), bool(created_rows), pass_row_visible=bool(pass_rows))
        )
        if world.submit_error is not None:
            raise world.submit_error
        return record

    monkeypatch.setattr(panel_manager, 'submit_record', submit_record)


class NoClient:
    """The Submit client the manager is built with: the seams that would use it are replaced, so it is never called."""

    async def submit(self, request):  # pragma: no cover - a call is a test failure
        raise AssertionError('the manager called the Submit client directly')

    async def instance(self):  # pragma: no cover
        raise AssertionError('the manager probed the instance')


def manager(clock: Callable[[], datetime], *, dry_run: bool = False) -> panel_manager.PanelManager:
    return panel_manager.PanelManager(
        NoClient(), {SOURCE: EveryDayCalendar()}, clock=clock, settle=SETTLE, dry_run=dry_run
    )


PERMANENT = IngestRequestFailureCause.PERMANENT.value
