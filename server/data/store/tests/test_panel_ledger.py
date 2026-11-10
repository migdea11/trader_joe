"""The panel ledger's writes (tj-7zjov1.177, P20c), over SQLite: crud/panel_ledger.py's real statements.

THE DESIGN: the P20c bead (BUILD (1) to (3), no update or delete beyond the finish), its architect notes of 14:02 UTC
2026-10-27 (as_of a snapshot instant), 14:16 UTC (open_pass takes kind) and 03:26 UTC 2026-10-28 (open_pass refuses
another panel's settings version and another source's source settings version; the count keys a named constant;
finish_pass an UPDATE guarded by finished_at IS NULL), ADR tj-26jt9w addendum 20, the P20cv pins (tj-7zjov1.179), and
the architect's RULING of 01:26 UTC 2026-10-30 on tj-7zjov1.177 (P20c-fix tj-7zjov1.207, pins tj-7zjov1.208): seq, the
row's 1-based append ordinal within its pass, assigned by append_transitions, unique with pass_id; the newest order is
as_of, the pass's started_at, pass_id, seq; a finished pass takes no transitions; finish_pass raises on an unknown pass.

  W1 open_pass writes one pass row with its kind, versions, snapshots and dry-run flag; started_at is the database's;
     finished_at and counts stay NULL; a RELEASE pass is stored and read back as one;
  W2 open_pass refuses (PanelPassMismatch) an unknown panel, a settings version of another panel or none, and a source
     settings version of another source or none, and writes nothing;
  W3 finish_pass finishes once: True the first time, False after (never an overwrite of finished_at or counts); an
     unknown pass RAISES PanelPassMismatch and writes nothing; counts carry all seven COUNT_KEYS (0 when not given); an
     unknown key, a negative, a bool or a non-integer count is refused before any write; one SELECT of the pass, then
     the one UPDATE, which sets only finished_at and counts, guarded by finished_at IS NULL;
  W4 append_transitions takes the panel from the pass row, sends no recorded_at (database time), writes in ROW_CHUNK
     statements, refuses an unknown pass and a FINISHED pass (writing nothing); a CREATED NewTransition must name
     its request_id and must NOT name a dataset_id (P20d tj-7zjov1.216: the dataset is the request's own), with
     value-free messages; every other kind is unchanged; as_of is aware;
  W4s seq is 1..n in list order within a pass, continuing across ROW_CHUNK statements and across calls in one pass
     (the pass's max plus one), restarting at 1 in another pass; it is read back on MemberTransition;
  W5 no function commits: the caller's rollback leaves nothing;
  W6 append-only: the public surface is exactly the seven functions, and the only non-SELECT, non-INSERT statement the
     module sends is finish_pass's guarded UPDATE;
  W7 every statement the module sends compiles for the postgresql dialect (the auto-correlation class of defect found
     in panel_settings.enabled_panels_on_source raises at compile time).

The reads (latest_state, members_at, created_in_window, boundary_read) are test_panel_boundary_read.py. Postgres-only
behaviour (asyncpg, JSONB, enum casts, the concurrent finish) is tests/system/test_panel_ledger_pg.py.

TIER. SQLite has no ARRAY, no JSONB and no btrim, so the two array columns and counts are JSON here and btrim is
registered on the connection. Timestamps are stored as UTC instants (aware_sqlite_datetimes).
"""

import ast
import dataclasses
import inspect
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql import Insert, Select, Update

from common.enums.data_stock import DataSource
from data.store.app.database.crud import panel_ledger
from data.store.app.database.crud.panel_ledger import (
    COUNT_KEYS,
    MEMBERSHIP_TRANSITIONS,
    ROW_CHUNK,
    BoundaryRead,
    CollectStop,
    MemberAt,
    MemberTransition,
    NewTransition,
    PanelPassMismatch,
    SettingsBoundary,
    SourceSettingsBoundary,
    append_transitions,
    boundary_read,
    created_in_window,
    finish_pass,
    latest_state,
    members_at,
    open_pass,
)
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog, OverrideAction
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestKind
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
from data.store.tests.test_ui_datasets_route import aware_sqlite_datetimes  # noqa: F401 (a fixture)


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

PANELS = Panel.__table__
SETTINGS = PanelSettingsVersion.__table__
SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__
PASSES = PanelPass.__table__
TRANSITIONS = PanelMemberTransition.__table__
OVERRIDES = DatasetOverrideLog.__table__
# The bars requests a CREATED row names (P20d tj-7zjov1.216): boundary_read reaches a member's dataset through them.
REQUESTS = IngestRequest.__table__
TABLES = (PANELS, SETTINGS, SOURCE_SETTINGS, PASSES, TRANSITIONS, OVERRIDES, REQUESTS)
LEDGER_TABLES = (PASSES, TRANSITIONS)

SOURCE = DataSource.ALPACA_API
OTHER_SOURCE = DataSource.IB_API
T0 = datetime(2026, 10, 5, 20, tzinfo=UTC)

PUBLIC_FUNCTIONS = {
    'open_pass',
    'append_transitions',
    'finish_pass',
    'latest_state',
    'members_at',
    'boundary_read',
    'created_in_window',
}
ALL_COUNT_KEYS = ('entered', 'created', 'deferred', 'refused', 'exited', 'draining', 'drained')


# --------------------------------------------------------------------------------------------- the world


@dataclasses.dataclass(frozen=True)
class Seeded:
    """A panel with one settings version, and the current source settings version of its source."""

    panel_id: uuid.UUID
    settings_id: uuid.UUID
    source_settings_id: uuid.UUID
    source: DataSource


class World:
    """The engine, every statement the module's sessions sent, and every commit made on them."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.statements: list[Any] = []
        self.parameters: list[Any] = []
        self.commits = 0
        self._source_versions: dict[DataSource, int] = {}
        self._settings_versions: dict[uuid.UUID, int] = {}

    def session(self) -> AsyncSession:
        world = self

        class Recording(AsyncSession):
            async def execute(self, statement, *args, **kwargs):
                world.statements.append(statement)
                world.parameters.append(args[0] if args else kwargs.get('params'))
                return await super().execute(statement, *args, **kwargs)

            async def commit(self) -> None:
                await super().commit()
                world.commits += 1

        return Recording(self.engine, expire_on_commit=False)

    async def rows(self, table: sa.Table, *where: Any) -> list[sa.Row]:
        async with AsyncSession(self.engine) as db:
            return list((await db.execute(sa.select(table).where(*where))).all())

    async def counts(self) -> dict[str, int]:
        return {table.name: len(await self.rows(table)) for table in TABLES}

    async def put(self, statement: Any) -> Any:
        """A write behind the module's back (test set-up only: the module itself is append-only)."""
        async with AsyncSession(self.engine) as db:
            result = await db.execute(statement)
            await db.commit()
            return result

    # ---- set-up rows, written directly so these tests depend on the ledger module alone

    async def source_version(self, source: DataSource = SOURCE, **kw: Any) -> uuid.UUID:
        number = self._source_versions.get(source, 0) + 1
        self._source_versions[source] = number
        values = {
            'source': source,
            'version_no': number,
            'ramp_datasets_per_hour': 5,
            'request_cap_per_hour': 60,
            'corporate_action_window_pre_days': 90,
            'corporate_action_window_post_days': 90,
            'actor': 'operator',
            'reason': f'rates {number}',
        } | kw
        result = await self.put(SOURCE_SETTINGS.insert().values(**values).returning(SOURCE_SETTINGS.c.id))
        return result.scalar_one()

    async def settings_version(self, panel_id: uuid.UUID, **kw: Any) -> uuid.UUID:
        number = self._settings_versions.get(panel_id, 0) + 1
        self._settings_versions[panel_id] = number
        values = {
            'panel_id': panel_id,
            'version_no': number,
            'enabled': True,
            'cadence': PanelCadence.DAILY,
            'actor': 'operator',
            'reason': f'settings {number}',
            # NOT NULL with no schema default (ADR tj-ed4t8m D1): a row written behind the crud's back states it, off.
            'include_otc': False,
        } | kw
        result = await self.put(SETTINGS.insert().values(**values).returning(SETTINGS.c.id))
        return result.scalar_one()

    async def request(
        self, dataset_id: uuid.UUID | None = None, source: DataSource = SOURCE, request_id: uuid.UUID | None = None
    ) -> uuid.UUID:
        """A bars request row, POST-shaped: dataset_id NULL until the intake's first delivered attempt sets it."""
        values = {'kind': IngestRequestKind.BARS, 'source': source, 'dataset_id': dataset_id, 'client_label': 'panel'}
        if request_id is not None:
            values['id'] = request_id
        return (await self.put(REQUESTS.insert().values(**values).returning(REQUESTS.c.id))).scalar_one()

    async def panel(self, source: DataSource = SOURCE, name: str | None = None) -> Seeded:
        result = await self.put(
            PANELS.insert().values(name=name or f'panel-{uuid.uuid4().hex[:8]}', source=source).returning(PANELS.c.id)
        )
        panel_id = result.scalar_one()
        settings_id = await self.settings_version(panel_id)
        source_settings_id = await self.source_version(source)
        return Seeded(panel_id, settings_id, source_settings_id, source)

    async def open(
        self,
        seeded: Seeded,
        *,
        kind: PanelPassKind = PanelPassKind.MEMBERSHIP,
        dry_run: bool = False,
        started_at: datetime | None = None,
        settings_id: uuid.UUID | None = None,
        source_settings_id: uuid.UUID | None = None,
        rows: tuple[NewTransition, ...] = (),
    ) -> uuid.UUID:
        """A pass opened and filled through the module, committed by the TEST (the caller's transaction)."""
        async with self.session() as db:
            pass_id = await open_pass(
                db,
                panel_id=seeded.panel_id,
                kind=kind,
                settings_version_id=settings_id or seeded.settings_id,
                source_settings_version_id=source_settings_id or seeded.source_settings_id,
                active_snapshot_id=uuid.uuid4(),
                inactive_snapshot_id=None,
                dry_run=dry_run,
            )
            await append_transitions(db, pass_id, rows)
            await db.commit()
        if started_at is not None:
            await self.put(PASSES.update().where(PASSES.c.id == pass_id).values(started_at=started_at))
        return pass_id


@pytest_asyncio.fixture
async def world(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    request.getfixturevalue('aware_sqlite_datetimes')
    for column in (SETTINGS.c.allow_symbols, SETTINGS.c.deny_symbols, PASSES.c.counts):
        monkeypatch.setattr(column, 'type', sa.JSON())
    metadata = sa.MetaData()
    copies = {table.name: table.to_metadata(metadata) for table in TABLES}
    for name in ('allow_symbols', 'deny_symbols'):
        copies[SETTINGS.name].c[name].server_default = sa.DefaultClause(sa.text("'[]'"))
    copies[REQUESTS.name].c.data_types.type = sa.JSON()  # a Postgres ARRAY; only the copy changes (request_tables.py)
    # SQLite gives a column declared UUID NUMERIC affinity, so a random hex id that reads as a number (all digits, or
    # digits around one 'e') would be stored as an integer or a float and fail to read back. CHAR(32) is TEXT affinity.
    for copy in copies.values():
        for column in copy.columns:
            if isinstance(column.type, sa.UUID):
                column.type = sa.Uuid()
    engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, 'connect')
    def register(dbapi_connection, _record):
        dbapi_connection.run_async(lambda c: c.create_function('gen_random_uuid', 0, lambda: uuid.uuid4().hex))
        dbapi_connection.run_async(lambda c: c.create_function('btrim', 1, lambda s: None if s is None else s.strip()))
        dbapi_connection.run_async(lambda c: c.execute('PRAGMA foreign_keys = ON'))

    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        declared = await connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'panel_member_transition'"
        )
        assert ' UUID' not in declared.scalar_one(), 'an id column kept NUMERIC affinity'
    try:
        yield World(engine)
    finally:
        await engine.dispose()


def entered(vendor: str, as_of: datetime = T0, symbol: str | None = None) -> NewTransition:
    return NewTransition(vendor, symbol or vendor.upper(), PanelTransition.ENTERED, as_of)


def created(vendor: str, as_of: datetime = T0, request_id: uuid.UUID | None = None) -> NewTransition:
    """A CREATED row as P20d shapes it: it names its bars request only, never a dataset."""
    return NewTransition(vendor, vendor.upper(), PanelTransition.CREATED, as_of, request_id=request_id or uuid.uuid4())


def transition(vendor: str, kind: PanelTransition, as_of: datetime = T0, cause: str | None = None) -> NewTransition:
    return NewTransition(vendor, vendor.upper(), kind, as_of, cause=cause)


def pg_sql(statement: Any) -> str:
    return ' '.join(str(statement.compile(dialect=postgresql.dialect())).split())


async def _open_direct(world: World, seeded: Seeded, **overrides: Any) -> uuid.UUID:
    arguments = {
        'panel_id': seeded.panel_id,
        'kind': PanelPassKind.MEMBERSHIP,
        'settings_version_id': seeded.settings_id,
        'source_settings_version_id': seeded.source_settings_id,
        'active_snapshot_id': uuid.uuid4(),
        'inactive_snapshot_id': None,
        'dry_run': False,
    } | overrides
    async with world.session() as db:
        pass_id = await open_pass(db, **arguments)
        await db.commit()
    return pass_id


# --------------------------------------------------------------------------------------------- W1 open_pass


@pytest.mark.parametrize('kind', list(PanelPassKind))
@pytest.mark.parametrize('dry_run', [False, True])
async def test_open_pass_writes_one_row_with_its_kind_versions_snapshots_and_flag(world, kind, dry_run):
    seeded = await world.panel()
    active, inactive = uuid.uuid4(), uuid.uuid4()

    pass_id = await _open_direct(
        world, seeded, kind=kind, dry_run=dry_run, active_snapshot_id=active, inactive_snapshot_id=inactive
    )

    (row,) = await world.rows(PASSES)
    assert row.id == pass_id
    assert (row.panel_id, row.kind, row.dry_run) == (seeded.panel_id, kind, dry_run)
    assert (row.settings_version_id, row.source_settings_version_id) == (seeded.settings_id, seeded.source_settings_id)
    assert (row.active_snapshot_id, row.inactive_snapshot_id) == (active, inactive)
    assert row.started_at is not None
    assert (row.finished_at, row.counts) == (None, None), 'a pass is opened unfinished'


async def test_open_pass_accepts_no_inactive_snapshot(world):
    seeded = await world.panel()
    await _open_direct(world, seeded, inactive_snapshot_id=None)
    (row,) = await world.rows(PASSES)
    assert row.inactive_snapshot_id is None


async def test_open_pass_sends_no_started_at_the_database_stamps_it(world):
    seeded = await world.panel()
    world.statements.clear()
    await _open_direct(world, seeded)
    (insert,) = [s for s in world.statements if isinstance(s, Insert)]
    assert 'started_at' not in insert.compile(dialect=postgresql.dialect()).params
    assert 'finished_at' not in pg_sql(insert) and 'counts' not in pg_sql(insert)


async def test_a_release_pass_is_read_back_as_one(world):
    seeded = await world.panel()
    deferred_at = T0
    await world.open(
        seeded, started_at=T0, rows=(entered('a'), transition('a', PanelTransition.DEFERRED, cause='ramp_rate'))
    )
    release = await world.open(
        seeded, kind=PanelPassKind.RELEASE, started_at=T0 + timedelta(hours=1), rows=(created('a', deferred_at),)
    )

    async with world.session() as db:
        state = await latest_state(db, seeded.panel_id)
    assert state['a'].pass_id == release and state['a'].pass_kind is PanelPassKind.RELEASE


# --------------------------------------------------------------------------------------------- W2 ownership


async def test_open_pass_refuses_an_unknown_panel_and_writes_nothing(world):
    seeded = await world.panel()
    with pytest.raises(PanelPassMismatch):
        await _open_direct(world, seeded, panel_id=uuid.uuid4())
    assert await world.rows(PASSES) == []


async def test_open_pass_refuses_a_settings_version_of_another_panel(world):
    mine = await world.panel()
    theirs = await world.panel()
    with pytest.raises(PanelPassMismatch, match='settings version'):
        await _open_direct(world, mine, settings_version_id=theirs.settings_id)
    assert await world.rows(PASSES) == []


async def test_open_pass_refuses_an_unknown_settings_version(world):
    seeded = await world.panel()
    with pytest.raises(PanelPassMismatch, match='settings version'):
        await _open_direct(world, seeded, settings_version_id=uuid.uuid4())
    assert await world.rows(PASSES) == []


async def test_open_pass_refuses_a_source_settings_version_of_another_source(world):
    mine = await world.panel(SOURCE)
    other_source_version = await world.source_version(OTHER_SOURCE)
    with pytest.raises(PanelPassMismatch, match='source settings version'):
        await _open_direct(world, mine, source_settings_version_id=other_source_version)
    assert await world.rows(PASSES) == []


async def test_open_pass_refuses_an_unknown_source_settings_version(world):
    seeded = await world.panel()
    with pytest.raises(PanelPassMismatch, match='source settings version'):
        await _open_direct(world, seeded, source_settings_version_id=uuid.uuid4())
    assert await world.rows(PASSES) == []


async def test_open_pass_accepts_any_version_of_its_own_source_and_panel(world):
    """Another panel on the SAME source shares the source's versions; an older version of its own panel is its own."""
    mine = await world.panel(SOURCE)
    neighbour = await world.panel(SOURCE)
    older_settings = mine.settings_id
    await world.settings_version(mine.panel_id)

    await _open_direct(
        world, mine, settings_version_id=older_settings, source_settings_version_id=neighbour.source_settings_id
    )
    assert len(await world.rows(PASSES)) == 1


async def test_the_mismatch_names_ids_only_and_is_a_value_error():
    assert issubclass(PanelPassMismatch, ValueError)


# --------------------------------------------------------------------------------------------- W3 finish once


async def test_count_keys_are_the_seven_transitions_lower_cased():
    assert COUNT_KEYS == ALL_COUNT_KEYS
    assert set(COUNT_KEYS) == {t.value.lower() for t in PanelTransition}


async def test_finish_pass_finishes_once_and_never_overwrites(world):
    seeded = await world.panel()
    pass_id = await _open_direct(world, seeded)

    async with world.session() as db:
        first = await finish_pass(db, pass_id, {'entered': 3, 'created': 2})
        await db.commit()
    (after_first,) = await world.rows(PASSES)
    async with world.session() as db:
        second = await finish_pass(db, pass_id, {'entered': 99})
        await db.commit()
    (after_second,) = await world.rows(PASSES)

    assert (first, second) == (True, False)
    assert after_first.finished_at is not None
    assert after_first.counts == {key: {'entered': 3, 'created': 2}.get(key, 0) for key in ALL_COUNT_KEYS}
    assert (after_second.finished_at, after_second.counts) == (after_first.finished_at, after_first.counts)


async def test_finish_pass_on_an_unknown_pass_raises_and_writes_nothing(world):
    """An unknown pass is an error, not a quiet False: False now means only 'already finished' (ruling 2026-10-30)."""
    seeded = await world.panel()
    await _open_direct(world, seeded)
    unknown = uuid.uuid4()
    world.statements.clear()
    async with world.session() as db:
        with pytest.raises(PanelPassMismatch, match=f'pass {unknown} does not exist'):
            await finish_pass(db, unknown, {'entered': 1})
        await db.commit()
    assert not [s for s in world.statements if isinstance(s, Update)], 'an unknown pass reached the UPDATE'
    (row,) = await world.rows(PASSES)
    assert (row.finished_at, row.counts) == (None, None)


async def test_finish_pass_on_a_finished_pass_is_false_and_never_raises(world):
    seeded = await world.panel()
    pass_id = await _open_direct(world, seeded)
    async with world.session() as db:
        assert await finish_pass(db, pass_id, {}) is True
        await db.commit()
    async with world.session() as db:
        assert await finish_pass(db, pass_id, {'entered': 5}) is False
        await db.commit()
    (row,) = await world.rows(PASSES)
    assert row.counts['entered'] == 0


async def test_finish_pass_writes_every_count_key_even_with_no_counts(world):
    seeded = await world.panel()
    pass_id = await _open_direct(world, seeded)
    async with world.session() as db:
        assert await finish_pass(db, pass_id, {}) is True
        await db.commit()
    (row,) = await world.rows(PASSES)
    assert row.counts == dict.fromkeys(ALL_COUNT_KEYS, 0)
    assert list(row.counts) == list(ALL_COUNT_KEYS)


async def test_finish_pass_accepts_every_key_at_once_and_zero(world):
    seeded = await world.panel()
    pass_id = await _open_direct(world, seeded)
    counts = {key: index for index, key in enumerate(ALL_COUNT_KEYS)}
    async with world.session() as db:
        assert await finish_pass(db, pass_id, counts) is True
        await db.commit()
    (row,) = await world.rows(PASSES)
    assert row.counts == counts


@pytest.mark.parametrize(
    'counts',
    [
        {'ENTERED': 1},
        {'entred': 1},
        {'requests': 4},
        {'entered': -1},
        {'drained': True},
        {'created': 1.0},
        {'created': '1'},
        {'created': None},
    ],
    ids=['upper-case-key', 'misspelt-key', 'foreign-key', 'negative', 'bool', 'float', 'str', 'none'],
)
async def test_finish_pass_refuses_bad_counts_before_any_write(world, counts):
    seeded = await world.panel()
    pass_id = await _open_direct(world, seeded)
    world.statements.clear()
    async with world.session() as db:
        with pytest.raises(ValueError):
            await finish_pass(db, pass_id, counts)
    assert world.statements == [], 'a refused finish sent a statement'
    (row,) = await world.rows(PASSES)
    assert (row.finished_at, row.counts) == (None, None)


async def test_finish_pass_is_one_update_of_finished_at_and_counts_guarded_by_finished_at_is_null(world):
    """One SELECT of the pass's id (the unknown-pass check), then the one guarded UPDATE, and nothing else."""
    seeded = await world.panel()
    pass_id = await _open_direct(world, seeded)
    world.statements.clear()
    async with world.session() as db:
        await finish_pass(db, pass_id, {})
    exists, update = world.statements
    assert isinstance(exists, Select)
    assert re.fullmatch(
        r'SELECT panel_pass\.id FROM panel_pass WHERE panel_pass\.id = %\(id_1\)s::UUID', pg_sql(exists)
    ), pg_sql(exists)
    assert isinstance(update, Update) and update.table is PASSES
    sql = pg_sql(update)
    assert re.fullmatch(
        # counts carries a ::JSON cast here only because the SQLite tier swaps JSONB for JSON.
        r'UPDATE panel_pass SET finished_at=now\(\), counts=%\(counts\)s(::JSONB?)? '
        r'WHERE panel_pass\.id = %\(id_1\)s::UUID AND panel_pass\.finished_at IS NULL',
        sql,
    ), sql


async def test_a_finished_pass_whose_finish_is_attempted_twice_in_one_transaction_still_finishes_once(world):
    seeded = await world.panel()
    pass_id = await _open_direct(world, seeded)
    async with world.session() as db:
        results = [await finish_pass(db, pass_id, {'entered': n}) for n in (1, 2)]
        await db.commit()
    (row,) = await world.rows(PASSES)
    assert results == [True, False] and row.counts['entered'] == 1


# --------------------------------------------------------------------------------------------- W4 transitions


async def test_append_transitions_takes_the_panel_from_the_pass_and_writes_every_field(world):
    seeded = await world.panel()
    await world.panel()  # another panel, never named
    dataset_id, request_id = uuid.uuid4(), uuid.uuid4()
    rows = (
        entered('v1', T0, symbol='AAA'),
        NewTransition('v1', 'AAA', PanelTransition.CREATED, T0, request_id=request_id),
        transition('v2', PanelTransition.DEFERRED, T0 - timedelta(days=1), cause='ramp_rate'),
        NewTransition('v3', 'CCC', PanelTransition.EXITED, T0, dataset_id=dataset_id),
    )
    async with world.session() as db:
        pass_id = await open_pass(
            db,
            panel_id=seeded.panel_id,
            kind=PanelPassKind.MEMBERSHIP,
            settings_version_id=seeded.settings_id,
            source_settings_version_id=seeded.source_settings_id,
            active_snapshot_id=uuid.uuid4(),
            inactive_snapshot_id=None,
            dry_run=False,
        )
        written = await append_transitions(db, pass_id, rows)
        await db.commit()

    assert written == 4
    stored = sorted(await world.rows(TRANSITIONS), key=lambda r: (r.vendor_asset_id, r.transition.value))
    assert {(r.pass_id, r.panel_id) for r in stored} == {(pass_id, seeded.panel_id)}
    assert [
        (r.vendor_asset_id, r.symbol, r.transition, r.dataset_id, r.request_id, r.cause, r.as_of) for r in stored
    ] == [
        ('v1', 'AAA', PanelTransition.CREATED, None, request_id, None, T0),
        ('v1', 'AAA', PanelTransition.ENTERED, None, None, None, T0),
        ('v2', 'V2', PanelTransition.DEFERRED, None, None, 'ramp_rate', T0 - timedelta(days=1)),
        ('v3', 'CCC', PanelTransition.EXITED, dataset_id, None, None, T0),
    ]
    assert all(r.recorded_at is not None for r in stored)


async def test_append_transitions_sends_no_recorded_at_and_no_panel_from_the_caller(world):
    """recorded_at is database time; the panel id is read from the pass, never an argument."""
    assert 'panel_id' not in inspect.signature(append_transitions).parameters
    seeded = await world.panel()
    pass_id = await world.open(seeded)
    world.statements.clear()
    world.parameters.clear()
    async with world.session() as db:
        await append_transitions(db, pass_id, (entered('x'), entered('y')))
        await db.commit()
    select_pass, select_max, insert = world.statements
    assert isinstance(select_pass, Select) and re.fullmatch(
        r'SELECT panel_pass\.panel_id, panel_pass\.finished_at FROM panel_pass WHERE panel_pass\.id = %\(id_1\)s::UUID',
        pg_sql(select_pass),
    ), pg_sql(select_pass)
    # The pass's current maximum, read in the caller's session (so inside the caller's transaction) once per call.
    assert isinstance(select_max, Select) and re.fullmatch(
        r'SELECT coalesce\(max\(panel_member_transition\.seq\), %\(coalesce_\d\)s\) AS coalesce_1 '
        r'FROM panel_member_transition WHERE panel_member_transition\.pass_id = %\(pass_id_1\)s::UUID',
        pg_sql(select_max),
    ), pg_sql(select_max)
    assert isinstance(insert, Insert) and insert.table is TRANSITIONS
    sent = world.parameters[2]
    assert len(sent) == 2
    assert [row['seq'] for row in sent] == [1, 2]
    for row in sent:
        assert set(row) == {
            'pass_id',
            'panel_id',
            'vendor_asset_id',
            'symbol',
            'transition',
            'dataset_id',
            'request_id',
            'cause',
            'as_of',
            'seq',
        }, "no recorded_at and no id: the database writes both; seq is the module's own"
        assert row['panel_id'] == seeded.panel_id


async def test_append_transitions_refuses_an_unknown_pass_and_writes_nothing(world):
    with pytest.raises(PanelPassMismatch):
        async with world.session() as db:
            await append_transitions(db, uuid.uuid4(), (entered('x'),))
    assert await world.rows(TRANSITIONS) == []


async def test_append_transitions_of_nothing_is_zero_and_sends_nothing(world):
    world.statements.clear()
    async with world.session() as db:
        assert await append_transitions(db, uuid.uuid4(), ()) == 0
    assert world.statements == []


async def test_append_transitions_writes_in_row_chunks(world, monkeypatch):
    monkeypatch.setattr(panel_ledger, 'ROW_CHUNK', 2)
    seeded = await world.panel()
    pass_id = await world.open(seeded)
    world.statements.clear()
    rows = tuple(entered(f'v{index}') for index in range(5))
    async with world.session() as db:
        assert await append_transitions(db, pass_id, rows) == 5
        await db.commit()
    inserts = [s for s in world.statements if isinstance(s, Insert)]
    assert len(inserts) == 3, 'five rows in chunks of two are three statements'
    assert sorted(r.vendor_asset_id for r in await world.rows(TRANSITIONS)) == [f'v{i}' for i in range(5)]


async def _seqs(world: World, pass_id: uuid.UUID) -> list[tuple[str, int]]:
    """(vendor asset id, seq) of a pass's rows, in seq order."""
    rows = await world.rows(TRANSITIONS, TRANSITIONS.c.pass_id == pass_id)
    return sorted(((r.vendor_asset_id, r.seq) for r in rows), key=lambda pair: pair[1])


async def test_seq_is_one_to_n_in_list_order_within_a_pass(world):
    """The caller's list order IS the append order: row i of the list gets seq i + 1 (ruling 2026-10-30).

    The vendor ids run against the list order, so a seq assigned by sorting, or in reverse, cannot pass.
    """
    seeded = await world.panel()
    vendors = ['m', 'c', 'x', 'a', 'k']
    pass_id = await world.open(seeded, rows=tuple(entered(v) for v in vendors))
    assert await _seqs(world, pass_id) == [(v, n) for n, v in enumerate(vendors, start=1)]


async def test_seq_continues_across_row_chunks(world, monkeypatch):
    monkeypatch.setattr(panel_ledger, 'ROW_CHUNK', 2)
    seeded = await world.panel()
    vendors = [f'v{9 - index}' for index in range(7)]  # four statements of 2, 2, 2, 1
    pass_id = await world.open(seeded)
    world.parameters.clear()
    async with world.session() as db:
        assert await append_transitions(db, pass_id, tuple(entered(v) for v in vendors)) == 7
        await db.commit()
    sent = [[row['seq'] for row in batch] for batch in world.parameters if isinstance(batch, list)]
    assert sent == [[1, 2], [3, 4], [5, 6], [7]], 'each chunk continues where the last one stopped'
    assert await _seqs(world, pass_id) == [(v, n) for n, v in enumerate(vendors, start=1)]


async def test_seq_continues_across_calls_in_one_pass_and_one_transaction(world, monkeypatch):
    """A second call adds to the pass's current maximum, read in the caller's own (uncommitted) transaction."""
    monkeypatch.setattr(panel_ledger, 'ROW_CHUNK', 2)
    seeded = await world.panel()
    async with world.session() as db:
        pass_id = await open_pass(
            db,
            panel_id=seeded.panel_id,
            kind=PanelPassKind.MEMBERSHIP,
            settings_version_id=seeded.settings_id,
            source_settings_version_id=seeded.source_settings_id,
            active_snapshot_id=uuid.uuid4(),
            inactive_snapshot_id=None,
            dry_run=False,
        )
        await append_transitions(db, pass_id, (entered('p'), entered('q'), entered('r')))
        await append_transitions(db, pass_id, (created('p'), created('q'), created('r')))
        await db.commit()
    async with world.session() as db:
        await append_transitions(db, pass_id, (transition('s', PanelTransition.DEFERRED, cause='ramp_rate'),))
        await db.commit()
    assert [seq for _, seq in await _seqs(world, pass_id)] == list(range(1, 8))
    rows = sorted(await world.rows(TRANSITIONS), key=lambda r: r.seq)
    assert [(r.vendor_asset_id, r.transition) for r in rows] == [
        ('p', PanelTransition.ENTERED),
        ('q', PanelTransition.ENTERED),
        ('r', PanelTransition.ENTERED),
        ('p', PanelTransition.CREATED),
        ('q', PanelTransition.CREATED),
        ('r', PanelTransition.CREATED),
        ('s', PanelTransition.DEFERRED),
    ]


async def test_seq_restarts_at_one_in_another_pass_and_is_pass_local(world):
    seeded = await world.panel()
    other = await world.panel()
    first = await world.open(seeded, rows=(entered('a'), entered('b'), entered('c')))
    second = await world.open(seeded, rows=(entered('d'), entered('e')))
    theirs = await world.open(other, rows=(entered('f'),))
    assert await _seqs(world, first) == [('a', 1), ('b', 2), ('c', 3)]
    assert await _seqs(world, second) == [('d', 1), ('e', 2)]
    assert await _seqs(world, theirs) == [('f', 1)]


async def test_a_duplicate_seq_in_one_pass_is_refused_by_the_unique_constraint(world):
    """The safety net for a second writer of one pass (one manager writes a pass): unique (pass_id, seq)."""
    seeded = await world.panel()
    pass_id = await world.open(seeded, rows=(entered('a'),))
    (row,) = await world.rows(TRANSITIONS)
    duplicate = {
        'pass_id': pass_id,
        'panel_id': seeded.panel_id,
        'vendor_asset_id': 'b',
        'symbol': 'B',
        'transition': PanelTransition.ENTERED,
        'as_of': T0,
        'seq': row.seq,
    }
    with pytest.raises(sa.exc.IntegrityError, match='seq'):
        await world.put(TRANSITIONS.insert().values(**duplicate))
    assert len(await world.rows(TRANSITIONS)) == 1


async def test_member_transition_carries_seq(world):
    assert 'seq' in {f.name for f in dataclasses.fields(MemberTransition)}
    seeded = await world.panel()
    await world.open(seeded, rows=(entered('a'), entered('b'), transition('a', PanelTransition.DEFERRED, cause='c')))
    async with world.session() as db:
        state = await latest_state(db, seeded.panel_id)
    assert {vendor: (row.transition, row.seq) for vendor, row in state.items()} == {
        'a': (PanelTransition.DEFERRED, 3),
        'b': (PanelTransition.ENTERED, 2),
    }


async def test_append_transitions_refuses_a_finished_pass_and_writes_nothing(world):
    """A finished pass's counts are its summary; a row appended after them would contradict it (ruling 2026-10-30)."""
    seeded = await world.panel()
    pass_id = await world.open(seeded, rows=(entered('a'),))
    async with world.session() as db:
        assert await finish_pass(db, pass_id, {'entered': 1}) is True
        await db.commit()
    world.statements.clear()
    async with world.session() as db:
        with pytest.raises(PanelPassMismatch, match=f'pass {pass_id} is finished'):
            await append_transitions(db, pass_id, (entered('b'), created('a')))
        await db.commit()
    assert not [s for s in world.statements if isinstance(s, Insert)], 'a finished pass reached the INSERT'
    assert [r.vendor_asset_id for r in await world.rows(TRANSITIONS)] == ['a']


async def test_an_unfinished_pass_of_the_same_panel_still_takes_rows_beside_a_finished_one(world):
    seeded = await world.panel()
    done = await world.open(seeded, rows=(entered('a'),))
    async with world.session() as db:
        await finish_pass(db, done, {'entered': 1})
        await db.commit()
    live = await world.open(seeded, rows=(entered('b'),))
    assert await _seqs(world, live) == [('b', 1)]


async def test_row_chunk_stays_inside_the_asyncpg_bind_limit():
    columns_bound = len(
        {
            'pass_id',
            'panel_id',
            'vendor_asset_id',
            'symbol',
            'transition',
            'dataset_id',
            'request_id',
            'cause',
            'as_of',
            'seq',
        }
    )
    assert columns_bound == 10
    assert ROW_CHUNK * columns_bound < 32_767


async def test_a_created_transition_names_its_request_only():
    """P20d (tj-7zjov1.216): CREATED names the bars request it wrote; its dataset is that request's, not the row's."""
    request_id = uuid.uuid4()
    row = NewTransition('v', 'V', PanelTransition.CREATED, T0, request_id=request_id)
    assert (row.dataset_id, row.request_id) == (None, request_id)


@pytest.mark.parametrize('with_dataset', [False, True], ids=['no-ids', 'dataset-only'])
async def test_a_created_transition_without_a_request_is_refused_with_a_value_free_message(with_dataset):
    dataset_id = uuid.uuid4()
    ids = {'dataset_id': dataset_id} if with_dataset else {}
    with pytest.raises(ValueError, match=r'^a CREATED transition must name its request_id$') as refused:
        NewTransition('vendor-x', 'SYMX', PanelTransition.CREATED, T0, **ids)
    message = str(refused.value)
    for value in ('vendor-x', 'SYMX', str(dataset_id), dataset_id.hex):
        assert value not in message


async def test_a_created_transition_naming_a_dataset_is_refused_with_a_value_free_message():
    """The ruling's option D: a dataset id on CREATED would be a guess (the intake mints the entry on delivery)."""
    dataset_id, request_id = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ValueError, match=r'^a CREATED transition must not name a dataset_id') as refused:
        NewTransition('vendor-x', 'SYMX', PanelTransition.CREATED, T0, dataset_id=dataset_id, request_id=request_id)
    message = str(refused.value)
    for value in ('vendor-x', 'SYMX', str(dataset_id), dataset_id.hex, str(request_id), request_id.hex):
        assert value not in message


@pytest.mark.parametrize('kind', [t for t in PanelTransition if t is not PanelTransition.CREATED])
async def test_every_other_transition_may_name_no_ids(kind):
    row = NewTransition('v', 'V', kind, T0)
    assert (row.dataset_id, row.request_id) == (None, None)


@pytest.mark.parametrize('kind', [t for t in PanelTransition if t is not PanelTransition.CREATED])
async def test_every_other_transition_may_still_name_a_dataset_and_a_request(kind):
    """Other kinds unchanged by P20d: EXITED, DRAINING and DRAINED name the dataset when they have one."""
    dataset_id, request_id = uuid.uuid4(), uuid.uuid4()
    row = NewTransition('v', 'V', kind, T0, dataset_id=dataset_id, request_id=request_id)
    assert (row.dataset_id, row.request_id) == (dataset_id, request_id)


async def test_a_transition_refuses_a_naive_as_of():
    with pytest.raises(ValueError, match='as_of'):
        NewTransition('v', 'V', PanelTransition.ENTERED, datetime(2026, 10, 5, 20))


async def test_a_transition_whose_dataset_was_deleted_stays_readable(world):
    """No foreign key from the ledger to datasets or requests: a dataset deleted after its CREATED leaves the row."""
    for column in (TRANSITIONS.c.dataset_id, TRANSITIONS.c.request_id):
        assert not column.foreign_keys, column
    seeded = await world.panel()
    gone = uuid.uuid4()  # names no dataset anywhere: as after a delete
    request_id = await world.request(dataset_id=gone)
    await world.open(
        seeded,
        rows=(
            entered('a'),
            created('a', request_id=request_id),
            NewTransition('a', 'A', PanelTransition.EXITED, T0, dataset_id=gone),
        ),
    )
    async with world.session() as db:
        state = await latest_state(db, seeded.panel_id)
        stops = await boundary_read(db, seeded.panel_id)
    assert {r.dataset_id for r in await world.rows(TRANSITIONS)} == {None, gone}
    assert 'a' in state and stops.collect_stops == ()


async def test_a_created_row_whose_request_is_missing_stays_readable(world):
    """No foreign key to requests either: a CREATED naming a request with no row reads back and names no dataset."""
    seeded = await world.panel()
    missing = uuid.uuid4()
    await world.open(seeded, rows=(entered('a'), created('a', request_id=missing)))
    async with world.session() as db:
        state = await latest_state(db, seeded.panel_id)
        stops = await boundary_read(db, seeded.panel_id)
    assert (state['a'].transition, state['a'].request_id, state['a'].dataset_id) == (
        PanelTransition.CREATED,
        missing,
        None,
    )
    assert stops.collect_stops == ()


@pytest.mark.parametrize('hex_id', ['1' * 32, '1234567890123456789012345e789012'], ids=['integer-like', 'real-like'])
async def test_an_id_that_reads_as_a_number_round_trips(world, hex_id):
    """The tier's own guard: ids are stored as text, so a random id never fails a read now and then.

    The request id is the one a CREATED row carries (P20d), and the join to the request row matches on it too.
    """
    seeded = await world.panel()
    number_like = uuid.UUID(hex_id)
    dataset_id = uuid.uuid4()
    await world.request(dataset_id=dataset_id, request_id=number_like)
    await world.open(seeded, rows=(created('a', request_id=number_like),))
    await world.put(
        OVERRIDES.insert().values(
            actor='operator', dataset_id=dataset_id, owner='o', action=OverrideAction.SET_COLLECTING, reason='r', at=T0
        )
    )
    async with world.session() as db:
        assert (await latest_state(db, seeded.panel_id))['a'].request_id == number_like
        stops = (await boundary_read(db, seeded.panel_id)).collect_stops
    assert [stop.dataset_id for stop in stops] == [dataset_id]


# --------------------------------------------------------------------------------------------- W5 no commits


async def test_no_function_commits_a_caller_rollback_leaves_nothing(world):
    seeded = await world.panel()
    before = await world.counts()
    commits_before = world.commits

    async with world.session() as db:
        pass_id = await open_pass(
            db,
            panel_id=seeded.panel_id,
            kind=PanelPassKind.MEMBERSHIP,
            settings_version_id=seeded.settings_id,
            source_settings_version_id=seeded.source_settings_id,
            active_snapshot_id=uuid.uuid4(),
            inactive_snapshot_id=None,
            dry_run=False,
        )
        await append_transitions(db, pass_id, (entered('a'), created('a')))
        assert await finish_pass(db, pass_id, {'entered': 1, 'created': 1}) is True
        await latest_state(db, seeded.panel_id)
        await members_at(db, seeded.panel_id, T0)
        await created_in_window(db, SOURCE, T0 - timedelta(days=1))
        await boundary_read(db, seeded.panel_id)
        assert db.in_transaction(), 'a function ended the caller transaction'
        await db.rollback()

    assert world.commits == commits_before, 'a ledger function committed'
    assert await world.counts() == before, 'the caller rollback left ledger rows'


@pytest.mark.parametrize('name', sorted(PUBLIC_FUNCTIONS))
async def test_no_function_calls_commit_rollback_or_begin(name):
    source = inspect.getsource(getattr(panel_ledger, name))
    assert not re.search(r'\.(commit|rollback|begin|begin_nested|close)\(', source), name
    assert 'write_transaction' not in source and 'own_session' not in source


# --------------------------------------------------------------------------------------------- W6 append-only


async def test_the_public_surface_is_exactly_the_seven_functions():
    public = {
        name
        for name, value in vars(panel_ledger).items()
        if inspect.iscoroutinefunction(value) and not name.startswith('_')
    }
    assert public == PUBLIC_FUNCTIONS


async def test_the_only_update_in_the_module_is_finish_pass_and_there_is_no_delete():
    source = inspect.getsource(panel_ledger)
    tree = ast.parse(source)
    updates, deletes = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute) and inner.attr == 'update':
                    updates.append(node.name)
                if isinstance(inner, ast.Attribute) and inner.attr == 'delete':
                    deletes.append(node.name)
    assert updates == ['finish_pass'] and deletes == []
    assert not re.search(r'\b(UPDATE|DELETE FROM|TRUNCATE)\b', source.replace('UPDATE in this module', ''))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert not {'update', 'delete'} & imported


async def test_a_full_round_sends_only_selects_inserts_and_the_one_guarded_update(world):
    seeded = await world.panel()
    world.statements.clear()
    pass_id = await world.open(seeded, rows=(entered('a'), created('a')))
    async with world.session() as db:
        await finish_pass(db, pass_id, {})
        await latest_state(db, seeded.panel_id)
        await members_at(db, seeded.panel_id, T0)
        await created_in_window(db, SOURCE, T0)
        await boundary_read(db, seeded.panel_id)
        await boundary_read(db, uuid.uuid4())
        await db.commit()

    others = [s for s in world.statements if not isinstance(s, Select | Insert)]
    assert len(others) == 1 and isinstance(others[0], Update) and others[0].table is PASSES
    assert 'finished_at IS NULL' in pg_sql(others[0])


# --------------------------------------------------------------------------------------------- W7 postgres compile


async def test_every_statement_the_module_sends_compiles_for_postgres(world):
    """The auto-correlation defect of P20b raised at compile time; every ledger statement compiles for Postgres."""
    seeded = await world.panel()
    other = await world.panel(OTHER_SOURCE)
    world.statements.clear()
    pass_id = await world.open(seeded, rows=(entered('a'), created('a')))
    await world.open(other, rows=(entered('b'),))
    async with world.session() as db:
        await finish_pass(db, pass_id, {'entered': 1})
        await latest_state(db, seeded.panel_id)
        await members_at(db, seeded.panel_id, T0)
        await created_in_window(db, SOURCE, T0)
        await boundary_read(db, seeded.panel_id)
        await db.commit()

    sent = [s for s in world.statements if isinstance(s, Select | Insert | Update)]
    assert len(sent) >= 15
    compiled = [pg_sql(statement) for statement in sent]
    assert all(sql for sql in compiled)


async def test_the_member_state_order_is_as_of_then_pass_started_at_then_pass_then_seq_newest_first(world):
    """The ruling of 2026-10-30: never id (a random uuid), and pass_id before seq because seq is pass-local."""
    seeded = await world.panel()
    world.statements.clear()
    async with world.session() as db:
        await latest_state(db, seeded.panel_id)
        await members_at(db, seeded.panel_id, T0)
    over = (
        'row_number() OVER (PARTITION BY panel_member_transition.vendor_asset_id '
        'ORDER BY panel_member_transition.as_of DESC, panel_pass.started_at DESC, '
        'panel_member_transition.pass_id DESC, panel_member_transition.seq DESC)'
    )
    latest, at = (pg_sql(s) for s in world.statements)
    for sql in (latest, at):
        assert over in sql, sql
        assert 'panel_pass.dry_run IS false' in sql
        assert 'JOIN panel_pass ON panel_pass.id = panel_member_transition.pass_id' in sql
    assert 'panel_member_transition.as_of <= %(as_of_1)s' in at
    assert 'panel_member_transition.transition IN (__[POSTCOMPILE_transition_1])' in at


# --------------------------------------------------------------------------------------------- the types


async def test_the_records_are_frozen_dataclasses():
    for record in (
        NewTransition,
        MemberTransition,
        MemberAt,
        SettingsBoundary,
        SourceSettingsBoundary,
        CollectStop,
        BoundaryRead,
    ):
        assert dataclasses.is_dataclass(record) and record.__dataclass_params__.frozen, record.__name__


async def test_membership_is_decided_by_entered_exited_and_drained_only():
    assert MEMBERSHIP_TRANSITIONS == (PanelTransition.ENTERED, PanelTransition.EXITED, PanelTransition.DRAINED)
    assert PanelTransition.DRAINING not in MEMBERSHIP_TRANSITIONS


async def test_a_collect_stop_never_carries_the_owner():
    assert 'owner' not in {f.name for f in dataclasses.fields(CollectStop)}
    assert OverrideAction.SET_COLLECTING.value == 'SET_COLLECTING'
