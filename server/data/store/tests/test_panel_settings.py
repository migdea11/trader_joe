"""The panel settings repository (tj-7zjov1.176, P20b), over SQLite: crud/panel_settings.py's real statements.

THE DESIGN: ADR tj-26jt9w addendum 20 (markers, enable and disable are versions, the source is fixed per panel,
append-only) and its 2026-10-30 note, the P20b bead and its architect note of 03:25 UTC 2026-10-28 (no fallback rate
numbers: current_source_settings is None for a source with no version and create_panel REFUSES such a source unless
given its settings, written in the same transaction; the only named defaults are DEFAULT_PANEL_CADENCE (MONTHLY) and the
90-day window), the P20bv pins (tj-7zjov1.178, with the 03:27 UTC note), and the P20b-fix (tj-7zjov1.209/.210):
create_panel REFUSES source settings for a source that already has a version, and effective-from has ONE definition,
the boundary read's (crud/panel_ledger.py boundary_read, pinned in test_panel_boundary_read.py), so this module has
no effective-from function any more.

REVERSED IN PART by ADR tj-2ayia2 (owner ruling 2026-11-07, D1-D5): the two rates are OPTIONAL and None is NO LIMIT
(stored NULL); a set ramp is MIN_RAMP_DATASETS_PER_HOUR (1) to INT32_MAX, a set cap MIN_REQUEST_CAP_PER_HOUR (2) to
INT32_MAX, zero refused; create_panel on a source with NO version ALWAYS writes that source's version 1 (from what it is
given, else SourceSettings(None, None): no limits, 90/90), so "create refuses a source without settings" is gone; the
source lock is taken on EVERY create, before the has-version read.

  S1 create_panel writes the panel and version 1 in ONE commit; a failure after the source settings were written
     leaves nothing; source settings for a source that already has a version are REFUSED and nothing is written (the
     source's rates change only through append_source_settings_version); on a source with no version it writes the
     source's version 1 in all four given/absent combinations of the rates, the window 90 when not given;
  S11 the ranges (tj-2ayia2 D1): ramp 0, cap 0, cap 1 and any value above INT32_MAX refused on create and on the source
     append, nothing written; ramp 1, cap 2, INT32_MAX and None accepted; the minimum constants equal the CHECKs and
     MIN_REQUEST_CAP_PER_HOUR equals panel_manager.REQUESTS_PER_CREATION;
  S2 a version append is a new row, version_no plus one, saved_at from the database, actor and reason stored trimmed,
     a blank or whitespace actor refused, a missing, blank or whitespace reason stored NULL (the reason is optional,
     ADR tj-46jkey, owner ruling 2026-11-06), earlier rows unchanged; a panel has no size;
  S3 the source cannot change: no write takes one for an existing panel;
  S4 enable and disable are versions;
  S5 current_settings and enabled_panels_on_source read the LATEST version;
  S6 no fallback: current_source_settings is None without a version, no constant names a rate's VALUE (the range
     constants are bounds, not defaults); DEFAULT_PANEL_CADENCE is MONTHLY and applied only where no cadence is named; the window defaults to
     DEFAULT_CORPORATE_ACTION_WINDOW_DAYS (90) on both sides;
  S7 version numbering is serialised: the panel row is read FOR UPDATE, the source under a transaction advisory lock,
     each BEFORE the max(version_no) read (the race itself is shown on Postgres, tests/system/test_panel_settings_pg.py);
  S8 (retired: effective-from is boundary_read's, see above);
  S9 append-only: every statement the module sends is a SELECT or an INSERT, and it exposes no other write;
  S10 create_panel's name lock (tj-7zjov1.244): a transaction-scoped advisory lock keyed on the TRIMMED name, taken
     first, before the name read; then the source lock (on EVERY create, tj-2ayia2 D4); then rows; a duplicate is the
     ordinary 422; the unique violation is not caught (the race itself: tests/system/test_panel_routes_pg.py PR3).

TIER. SQLite has no ARRAY, no JSONB, no btrim, no hashtext and no advisory locks, so the two array columns and the
pass counts are JSON here, and btrim, hashtext and pg_advisory_xact_lock are registered on the connection (the lock as a
recorder). FOR UPDATE is not rendered by SQLite: it is read from the statements the module sent, compiled for Postgres.
SQLite gives a column declared UUID NUMERIC affinity, so every id column is declared CHAR(32) here (as the ledger tier,
b9e9b2b): a random id that reads as a number would otherwise be stored as an integer or a float and fail to read back.
"""

import ast
import dataclasses
import inspect
import re
import uuid
import zlib
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
from sqlalchemy.sql import Insert, Select

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource, Granularity
from common.errors.vocabulary import REASONS, InvalidRequestError, Reason
from data.store.app.database.crud import panel_settings
from data.store.app.database.crud.panel_reads import get_panel, list_panels
from data.store.app.database.crud.panel_settings import (
    DEFAULT_PANEL_CADENCE,
    EnabledPanel,
    PanelRecord,
    PanelSettings,
    PanelSettingsRecord,
    SourceSettings,
    SourceSettingsRecord,
    append_settings_version,
    append_source_settings_version,
    create_panel,
    current_settings,
    current_source_settings,
    enabled_panels_on_source,
)
from data.store.app.database.models.panel import (
    DEFAULT_CORPORATE_ACTION_WINDOW_DAYS,
    PANEL_GRANULARITIES,
    Panel,
    PanelCadence,
    PanelPass,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
)
from data.store.tests.test_ui_datasets_route import aware_sqlite_datetimes  # noqa: F401 (a fixture)


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

PANELS = Panel.__table__
SETTINGS = PanelSettingsVersion.__table__
SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__
PASSES = PanelPass.__table__
TABLES = (PANELS, SETTINGS, SOURCE_SETTINGS, PASSES)

SOURCE = DataSource.ALPACA_API
OTHER_SOURCE = DataSource.IB_API
ACTOR = 'operator'
RATES = SourceSettings(ramp_datasets_per_hour=7, request_cap_per_hour=41)
SETTINGS_V1 = PanelSettings(enabled=True, cadence=PanelCadence.WEEKLY)

PUBLIC_FUNCTIONS = {
    'create_panel',
    'append_settings_version',
    'append_source_settings_version',
    'current_settings',
    'current_source_settings',
    'enabled_panels_on_source',
}


# --------------------------------------------------------------------------------------------- fixtures


class World:
    """The engine, every statement the module's sessions sent, their commits and the advisory-lock keys taken."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.statements: list[Any] = []
        self.commits = 0
        self.locks: list[int] = []

    def session(self) -> AsyncSession:
        world = self

        class Recording(AsyncSession):
            async def execute(self, statement, *args, **kwargs):
                world.statements.append(statement)
                return await super().execute(statement, *args, **kwargs)

            async def commit(self) -> None:
                await super().commit()
                world.commits += 1

        return Recording(self.engine, expire_on_commit=False)

    async def rows(self, table: sa.Table) -> list[sa.Row]:
        async with AsyncSession(self.engine) as db:
            return list((await db.execute(sa.select(table))).all())

    async def counts(self) -> dict[str, int]:
        return {table.name: len(await self.rows(table)) for table in TABLES}

    async def put(self, statement: Any) -> None:
        """A write behind the module's back (test set-up only)."""
        async with AsyncSession(self.engine) as db:
            await db.execute(statement)
            await db.commit()


@pytest_asyncio.fixture
async def world(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    request.getfixturevalue('aware_sqlite_datetimes')
    for column in (SETTINGS.c.allow_symbols, SETTINGS.c.deny_symbols, PASSES.c.counts):
        monkeypatch.setattr(column, 'type', sa.JSON())
    metadata = sa.MetaData()
    copies = {table.name: table.to_metadata(metadata) for table in TABLES}
    for name in ('allow_symbols', 'deny_symbols'):
        copies[SETTINGS.name].c[name].server_default = sa.DefaultClause(sa.text("'[]'"))
    # SQLite gives a column declared UUID NUMERIC affinity, so a random hex id that reads as a number (all digits, or
    # digits around one 'e') would be stored as an integer or a float and fail to read back. CHAR(32) is TEXT affinity.
    for copy in copies.values():
        for column in copy.columns:
            if isinstance(column.type, sa.UUID):
                column.type = sa.Uuid()
    engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)
    state = World(engine)

    def take_lock(key: int) -> None:
        state.locks.append(key)

    @event.listens_for(engine.sync_engine, 'connect')
    def register(dbapi_connection, _record):
        dbapi_connection.run_async(lambda c: c.create_function('gen_random_uuid', 0, lambda: uuid.uuid4().hex))
        dbapi_connection.run_async(lambda c: c.create_function('btrim', 1, lambda s: None if s is None else s.strip()))
        dbapi_connection.run_async(lambda c: c.create_function('hashtext', 1, lambda s: zlib.crc32(s.encode())))
        dbapi_connection.run_async(lambda c: c.create_function('pg_advisory_xact_lock', 1, take_lock))
        dbapi_connection.run_async(lambda c: c.execute('PRAGMA foreign_keys = ON'))

    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        declared = await connection.exec_driver_sql(
            "SELECT group_concat(sql, ';') FROM sqlite_master WHERE type = 'table'"
        )
        assert ' UUID' not in declared.scalar_one(), 'an id column kept NUMERIC affinity'
    try:
        yield state
    finally:
        await engine.dispose()


async def _source(world: World, source: DataSource = SOURCE, settings: SourceSettings = RATES, **kw):
    async with world.session() as db:
        return await append_source_settings_version(
            db, source=source, settings=settings, actor=kw.get('actor', ACTOR), reason=kw.get('reason', 'rates')
        )


async def _panel(world: World, name: str = 'alpha', source: DataSource = SOURCE, settings=SETTINGS_V1, **kw):
    async with world.session() as db:
        return await create_panel(
            db,
            name=name,
            source=source,
            granularity=kw.get('granularity', Granularity.ONE_DAY),
            settings=settings,
            actor=kw.get('actor', ACTOR),
            reason=kw.get('reason', 'first panel'),
            source_settings=kw.get('source_settings'),
        )


async def _append(world: World, panel_id: uuid.UUID, settings: PanelSettings = SETTINGS_V1, **kw):
    async with world.session() as db:
        return await append_settings_version(
            db, panel_id=panel_id, settings=settings, actor=kw.get('actor', ACTOR), reason=kw.get('reason', 'change')
        )


def _refused(excinfo: pytest.ExceptionInfo, reason: Reason = Reason.INVALID_REQUEST) -> None:
    assert excinfo.value.reason is reason, excinfo.value


def _sql(statement: Any) -> str:
    return ' '.join(str(statement.compile(dialect=postgresql.dialect())).split())


# --------------------------------------------------------------------------------------------- S1 create_panel


async def test_create_panel_writes_the_panel_and_version_1_in_one_commit(world):
    await _source(world)
    commits_before = world.commits

    panel, version = await _panel(world, name='  alpha  ', reason='  split per country  ', actor='  operator ')

    assert world.commits == commits_before + 1, 'the panel and its version 1 are one transaction'
    (panel_row,) = await world.rows(PANELS)
    (version_row,) = await world.rows(SETTINGS)
    assert (panel_row.id, panel_row.name, panel_row.source) == (panel.id, 'alpha', SOURCE)
    assert panel_row.created_at is not None
    assert (version_row.panel_id, version_row.version_no) == (panel.id, 1)
    assert (version_row.actor, version_row.reason) == ('operator', 'split per country')
    assert (version_row.enabled, version_row.cadence) == (True, PanelCadence.WEEKLY)
    assert 'size' not in version_row._fields, 'a panel has no size (ADR tj-46jkey)'
    assert version_row.saved_at is not None
    assert version == PanelSettingsRecord(
        id=version_row.id,
        panel_id=panel.id,
        version_no=1,
        settings=dataclasses.replace(SETTINGS_V1, allow_symbols=(), deny_symbols=()),
        saved_at=version_row.saved_at,
        actor='operator',
        reason='split per country',
    )
    assert panel == PanelRecord(
        id=panel_row.id, name='alpha', source=SOURCE, created_at=panel_row.created_at, granularity=Granularity.ONE_DAY
    )


@pytest.mark.parametrize('granularity', list(PANEL_GRANULARITIES), ids=lambda g: g.name)
async def test_create_panel_stores_the_given_granularity_and_every_read_returns_it(world, granularity):
    """ADR tj-rwoasb D1/D2: the row stores the Bar Interval as given.

    get_panel, list_panels and enabled_panels_on_source carry it.
    """
    await _source(world)

    panel, _ = await _panel(world, granularity=granularity)

    (panel_row,) = await world.rows(PANELS)
    assert panel_row.granularity is granularity and panel.granularity is granularity
    async with world.session() as db:
        assert (await get_panel(db, panel.id)).granularity is granularity
        (listed,) = await list_panels(db)
        assert listed.panel.granularity is granularity
        (enabled,) = await enabled_panels_on_source(db, SOURCE)
        assert enabled.panel.granularity is granularity


async def test_create_panel_without_a_granularity_is_a_type_error_and_writes_nothing(world):
    """ADR tj-rwoasb D2: a required keyword with no default; the column has no server default either."""
    await _source(world)
    before = len(await world.rows(PANELS))
    async with world.session() as db:
        with pytest.raises(TypeError, match='granularity'):
            await create_panel(db, name='alpha', source=SOURCE, settings=SETTINGS_V1, actor=ACTOR, reason='x')  # type: ignore[call-arg]
    assert len(await world.rows(PANELS)) == before
    assert inspect.signature(create_panel).parameters['granularity'].default is inspect.Parameter.empty


async def test_default_panel_cadence_is_monthly():
    assert DEFAULT_PANEL_CADENCE is PanelCadence.MONTHLY


async def test_create_panel_with_no_cadence_applies_the_default_and_stores_it(world):
    await _source(world)

    _, version = await _panel(world, settings=PanelSettings(enabled=True))

    (row,) = await world.rows(SETTINGS)
    assert row.cadence is PanelCadence.MONTHLY
    assert version.settings.cadence is PanelCadence.MONTHLY


@pytest.mark.parametrize('cadence', list(PanelCadence))
async def test_create_panel_keeps_a_named_cadence(world, cadence):
    await _source(world)

    await _panel(world, settings=PanelSettings(enabled=False, cadence=cadence))

    (row,) = await world.rows(SETTINGS)
    assert row.cadence is cadence


@pytest.mark.parametrize(
    ('given', 'stored'),
    [
        (None, (None, None, 90, 90)),
        (SourceSettings(ramp_datasets_per_hour=5, request_cap_per_hour=None), (5, None, 90, 90)),
        (SourceSettings(ramp_datasets_per_hour=None, request_cap_per_hour=6), (None, 6, 90, 90)),
        (SourceSettings(ramp_datasets_per_hour=5, request_cap_per_hour=6), (5, 6, 90, 90)),
        (SourceSettings(None, None, 10, 20), (None, None, 10, 20)),
    ],
    ids=['nothing-given', 'ramp-only', 'cap-only', 'both-rates', 'window-only'],
)
async def test_create_panel_on_a_source_with_no_version_always_writes_its_version_1(world, given, stored):
    """ADR tj-2ayia2 D4: no refusal for want of rates. An absent rate is NULL (no limit), an absent window 90.

    Exactly one source version, in the create's one commit, with the create's trimmed actor and reason.
    """
    commits_before = world.commits

    panel, _ = await _panel(world, source_settings=given, actor='  operator ', reason='  first panel  ')

    assert world.commits == commits_before + 1, 'the source version 1 and the panel are one transaction'
    (source_row,) = await world.rows(SOURCE_SETTINGS)
    assert (source_row.source, source_row.version_no) == (SOURCE, 1)
    assert (
        source_row.ramp_datasets_per_hour,
        source_row.request_cap_per_hour,
        source_row.corporate_action_window_pre_days,
        source_row.corporate_action_window_post_days,
    ) == stored
    assert (source_row.actor, source_row.reason) == ('operator', 'first panel')
    current = await _current_source(world)
    assert current is not None and current.settings == SourceSettings(*stored)
    assert [row.id for row in await world.rows(PANELS)] == [panel.id]


async def test_create_panel_writes_its_own_version_1_when_only_another_source_has_one(world):
    """The has-version read is per source: another source's version neither refuses nor stands in for this one's."""
    other = await _source(world, source=OTHER_SOURCE)

    await _panel(world, source=SOURCE)

    first = await _current_source(world)
    assert first is not None and (first.version_no, first.settings) == (1, SourceSettings(None, None))
    assert await _current_source(world, OTHER_SOURCE) == other
    assert len(await world.rows(PANELS)) == 1


async def test_create_panel_writes_given_source_settings_in_the_same_transaction(world):
    commits_before = world.commits

    await _panel(world, source_settings=RATES, reason=' first ')

    assert world.commits == commits_before + 1
    (source_row,) = await world.rows(SOURCE_SETTINGS)
    assert (source_row.source, source_row.version_no) == (SOURCE, 1)
    assert (source_row.ramp_datasets_per_hour, source_row.request_cap_per_hour) == (7, 41)
    assert (source_row.corporate_action_window_pre_days, source_row.corporate_action_window_post_days) == (90, 90)
    assert (source_row.actor, source_row.reason) == (ACTOR, 'first')
    assert len(await world.rows(PANELS)) == len(await world.rows(SETTINGS)) == 1


async def test_a_failure_after_the_source_settings_rolls_all_of_create_panel_back(world, monkeypatch):
    """One transaction: the source version written by create_panel goes with a panel write that fails after it."""

    def broken(_row):
        raise RuntimeError('the version read-back failed')

    monkeypatch.setattr(panel_settings, '_settings_record', broken)

    with pytest.raises(RuntimeError):
        await _panel(world, source_settings=RATES)

    assert await world.counts() == {table.name: 0 for table in TABLES}


async def test_create_panel_on_a_source_with_settings_writes_no_source_version(world):
    current = await _source(world)
    before = await world.rows(SOURCE_SETTINGS)

    await _panel(world)

    assert await world.rows(SOURCE_SETTINGS) == before
    assert await _current_source(world) == current
    assert len(await world.rows(PANELS)) == len(await world.rows(SETTINGS)) == 1


@pytest.mark.parametrize(
    'given',
    [
        RATES,
        SourceSettings(ramp_datasets_per_hour=1, request_cap_per_hour=2),
        # tj-2ayia2: a given source settings with no limits, or with the window only, is still "given".
        SourceSettings(None, None),
        SourceSettings(None, None, 10, 20),
    ],
    ids=['same-rates', 'new-rates', 'no-limits', 'window-only'],
)
async def test_create_panel_refuses_source_settings_for_a_source_that_has_a_version_and_writes_nothing(world, given):
    """P20b-fix (tj-7zjov1.209): a source's rates change for every panel on it, so only through their own write."""
    current = await _source(world)
    before_rows = await world.rows(SOURCE_SETTINGS)
    commits_before = world.commits
    world.statements.clear()

    with pytest.raises(InvalidRequestError) as excinfo:
        await _panel(world, source_settings=given)

    _refused(excinfo)
    assert SOURCE.name in excinfo.value.detail and 'already has settings' in excinfo.value.detail
    assert world.commits == commits_before, 'a refused create committed'
    assert not [statement for statement in world.statements if isinstance(statement, Insert)], 'an INSERT was sent'
    assert await world.rows(PANELS) == [] and await world.rows(SETTINGS) == []
    assert await world.rows(SOURCE_SETTINGS) == before_rows
    assert await _current_source(world) == current, "the source's current rates changed"


async def test_create_panel_takes_source_settings_when_only_another_source_has_a_version(world):
    """The guard is per source: another source's version does not refuse this one's first settings."""
    other = await _source(world, source=OTHER_SOURCE)

    await _panel(world, source_settings=RATES)

    first = await _current_source(world)
    assert first is not None and (first.version_no, first.settings) == (1, RATES)
    assert await _current_source(world, OTHER_SOURCE) == other


async def test_a_second_panel_on_a_source_its_first_panel_configured_needs_no_source_settings(world):
    """Source settings with the first panel, none with the next: allowed, and the second writes no source version."""
    await _panel(world, name='first', source_settings=RATES)
    before = await world.rows(SOURCE_SETTINGS)

    await _panel(world, name='second')

    assert await world.rows(SOURCE_SETTINGS) == before
    assert len(await world.rows(PANELS)) == 2


async def test_a_second_panel_with_the_same_trimmed_name_is_refused(world):
    await _source(world)
    await _panel(world, name='alpha')

    with pytest.raises(InvalidRequestError) as excinfo:
        await _panel(world, name='  alpha ')

    _refused(excinfo)
    assert len(await world.rows(PANELS)) == len(await world.rows(SETTINGS)) == 1


@pytest.mark.parametrize('field', ['name', 'actor'])
@pytest.mark.parametrize('blank', ['', '   ', '\t\n '])
async def test_create_panel_refuses_a_blank_name_or_actor(world, field, blank):
    await _source(world)
    before = await world.counts()

    with pytest.raises(InvalidRequestError) as excinfo:
        await _panel(world, **{field: blank})

    _refused(excinfo)
    assert await world.counts() == before


@pytest.mark.parametrize('reason', [None, '', '   ', '\t\n '], ids=['none', 'empty', 'spaces', 'whitespace'])
async def test_create_panel_with_no_reason_stores_null_for_both_new_versions(world, reason):
    """The reason is optional (ADR tj-46jkey): missing or blank after trimming is stored NULL, never ''.

    Both rows a create can write, the panel's version 1 and the source's first version, take the same rule.
    """
    panel, version = await _panel(world, reason=reason, source_settings=RATES)

    (version_row,) = await world.rows(SETTINGS)
    (source_row,) = await world.rows(SOURCE_SETTINGS)
    assert version_row.reason is None and source_row.reason is None
    assert version.reason is None and version_row.actor == ACTOR
    assert version.panel_id == panel.id


async def test_create_panel_refuses_negative_source_settings(world):
    await _source(world)
    before = await world.counts()

    with pytest.raises(InvalidRequestError) as rates:
        # OTHER_SOURCE has no version, so only the negative number can refuse this one (not the existing-version guard).
        await _panel(
            world,
            name='beta',
            source=OTHER_SOURCE,
            source_settings=SourceSettings(ramp_datasets_per_hour=-1, request_cap_per_hour=2),
        )

    _refused(rates)
    assert await world.counts() == before


# --------------------------------------------------------------------------------------------- S2 S3 S4 appends


async def test_an_append_is_a_new_row_numbered_plus_one_and_leaves_earlier_rows_unchanged(world):
    await _source(world)
    panel, _ = await _panel(world)
    before = await world.rows(SETTINGS)
    second = PanelSettings(
        enabled=True,
        cadence=PanelCadence.DAILY,
        allow_symbols=('AAA', 'BBB'),
        deny_symbols=('CCC',),
        asset_class=AssetType.STOCK,
        exchange='NYSE',
        active=True,
        tradable=False,
    )

    v2 = await _append(world, panel.id, second, actor=' operator ', reason='  widen  ')
    v3 = await _append(world, panel.id, SETTINGS_V1, reason='narrow')

    rows = sorted(await world.rows(SETTINGS), key=lambda row: row.version_no)
    assert [row.version_no for row in rows] == [1, 2, 3]
    assert rows[0] == before[0], 'an append rewrote an earlier version'
    assert (v2.version_no, v2.actor, v2.reason, v2.panel_id) == (2, 'operator', 'widen', panel.id)
    assert v2.settings == second
    assert (rows[1].allow_symbols, rows[1].deny_symbols, rows[1].exchange) == (['AAA', 'BBB'], ['CCC'], 'NYSE')
    assert (rows[1].asset_class, rows[1].active, rows[1].tradable) == (AssetType.STOCK, True, False)
    assert v3.version_no == 3 and v3.id not in {rows[0].id, rows[1].id}
    assert len({row.id for row in rows}) == 3


async def test_saved_at_is_the_databases_never_a_value_the_module_sends(world):
    await _source(world)
    panel, _ = await _panel(world)
    world.statements.clear()

    version = await _append(world, panel.id)
    await _source(world, reason='second rates')

    inserts = [statement for statement in world.statements if isinstance(statement, Insert)]
    assert len(inserts) == 2
    for statement in inserts:
        sent = statement.compile(dialect=postgresql.dialect()).params
        assert 'saved_at' not in sent, f'{statement.table.name} sent its own saved_at'
    assert version.saved_at is not None
    assert all(row.saved_at is not None for row in await world.rows(SOURCE_SETTINGS))


@pytest.mark.parametrize('blank', ['', '   ', '\t\n '])
async def test_an_append_refuses_a_blank_actor_and_writes_nothing(world, blank):
    """The actor stays required everywhere (ADR tj-46jkey): only the reason became optional."""
    await _source(world)
    panel, _ = await _panel(world)
    before = await world.counts()

    with pytest.raises(InvalidRequestError) as panel_refusal:
        await _append(world, panel.id, actor=blank)
    with pytest.raises(InvalidRequestError) as source_refusal:
        await _source(world, actor=blank)

    _refused(panel_refusal)
    _refused(source_refusal)
    assert 'actor' in str(panel_refusal.value) and 'actor' in str(source_refusal.value)
    assert await world.counts() == before


@pytest.mark.parametrize('reason', [None, '', '   ', '\t\n '], ids=['none', 'empty', 'spaces', 'whitespace'])
async def test_an_append_with_no_reason_stores_null_and_reads_back_none(world, reason):
    """Missing or blank after trimming is NULL on both version tables, the one spelling of 'none' (never '')."""
    await _source(world)
    panel, _ = await _panel(world)

    version = await _append(world, panel.id, reason=reason)
    source_version = await _source(world, reason=reason)

    assert version.reason is None and source_version.reason is None
    (stored,) = [row for row in await world.rows(SETTINGS) if row.version_no == 2]
    (stored_source,) = [row for row in await world.rows(SOURCE_SETTINGS) if row.version_no == 2]
    assert stored.reason is None and stored_source.reason is None
    async with world.session() as db:
        assert (await current_settings(db, panel.id)).reason is None


async def test_a_reason_is_still_stored_trimmed_when_one_is_given(world):
    await _source(world)
    panel, _ = await _panel(world)
    version = await _append(world, panel.id, reason='  widen  ')
    assert version.reason == 'widen'


async def test_an_append_refuses_a_missing_cadence(world):
    """A new version states its cadence in full: the default is create_panel's alone."""
    await _source(world)
    panel, _ = await _panel(world)
    before = await world.counts()

    with pytest.raises(InvalidRequestError) as no_cadence:
        await _append(world, panel.id, PanelSettings(enabled=True))

    _refused(no_cadence)
    assert await world.counts() == before


# --------------------------------------------------------------------------------------------- S12 include_otc


async def test_default_include_otc_is_false_and_the_dataclass_defaults_to_it():
    """ADR tj-ed4t8m D2(a): off by default, applied by the repository (the column has no default)."""
    assert panel_settings.DEFAULT_INCLUDE_OTC is False
    assert PanelSettings(enabled=True).include_otc is False


async def test_settings_that_do_not_name_include_otc_store_false_on_create_and_on_append(world):
    """The default reaches the row on both writes: the NOT NULL column with no default is always given a value."""
    await _source(world)
    panel, created = await _panel(world, settings=PanelSettings(enabled=True))
    appended = await _append(world, panel.id, PanelSettings(enabled=True, cadence=PanelCadence.DAILY))

    assert created.settings.include_otc is False and appended.settings.include_otc is False
    rows = sorted(await world.rows(SETTINGS), key=lambda row: row.version_no)
    assert [row.include_otc for row in rows] == [False, False]
    for statement in world.statements:
        if isinstance(statement, Insert) and statement.table.name == SETTINGS.name:
            assert statement.compile(dialect=postgresql.dialect()).params['include_otc'] is False


async def test_include_otc_true_is_stored_and_read_back_by_current_settings_and_enabled_panels(world):
    await _source(world)
    panel, created = await _panel(world, settings=PanelSettings(enabled=True, include_otc=True))
    appended = await _append(world, panel.id, PanelSettings(enabled=True, cadence=PanelCadence.DAILY, include_otc=True))

    assert created.settings.include_otc is True and appended.settings.include_otc is True
    assert [row.include_otc for row in await world.rows(SETTINGS)] == [True, True]
    async with world.session() as db:
        assert (await current_settings(db, panel.id)).settings.include_otc is True
    (enabled,) = await _enabled(world)
    assert enabled.current.settings.include_otc is True


@pytest.mark.parametrize('write', ['create', 'append'])
async def test_include_otc_false_with_the_otc_exchange_is_refused_and_writes_nothing(world, write):
    """ADR tj-ed4t8m D4: the contradiction selects nothing and would exit every member: 422 naming both fields."""
    await _source(world)
    panel, _ = await _panel(world)
    before = await world.counts()
    contradiction = PanelSettings(enabled=True, cadence=PanelCadence.WEEKLY, exchange='OTC', include_otc=False)

    with pytest.raises(InvalidRequestError) as excinfo:
        if write == 'create':
            await _panel(world, name='beta', settings=contradiction)
        else:
            await _append(world, panel.id, contradiction)

    _refused(excinfo)
    assert REASONS[excinfo.value.reason].http_status == 422
    assert 'include_otc' in str(excinfo.value) and 'exchange' in str(excinfo.value), excinfo.value
    assert await world.counts() == before


@pytest.mark.parametrize('write', ['create', 'append'])
@pytest.mark.parametrize(
    'settings',
    [
        PanelSettings(enabled=True, cadence=PanelCadence.WEEKLY, exchange='OTC', include_otc=True),
        # Exact and case-sensitive like the exchange filter: 'otc' is not an OTC exchange, so no contradiction.
        PanelSettings(enabled=True, cadence=PanelCadence.WEEKLY, exchange='otc', include_otc=False),
        PanelSettings(enabled=True, cadence=PanelCadence.WEEKLY, exchange='NASDAQ', include_otc=False),
    ],
    ids=['otc-only-panel', 'lower-case-otc', 'other-exchange'],
)
async def test_the_otc_refusal_is_only_false_with_the_exact_otc_exchange(world, write, settings):
    await _source(world)
    panel, _ = await _panel(world)

    if write == 'create':
        _, version = await _panel(world, name='beta', settings=settings)
    else:
        version = await _append(world, panel.id, settings)

    assert (version.settings.exchange, version.settings.include_otc) == (settings.exchange, settings.include_otc)


@pytest.mark.parametrize(
    'settings',
    [
        # Each case is otherwise valid (ramp 1, cap 2, the window 0), so only the named field can refuse it.
        SourceSettings(ramp_datasets_per_hour=-1, request_cap_per_hour=2),
        SourceSettings(ramp_datasets_per_hour=1, request_cap_per_hour=-1),
        SourceSettings(ramp_datasets_per_hour=1, request_cap_per_hour=2, corporate_action_window_pre_days=-1),
        SourceSettings(ramp_datasets_per_hour=1, request_cap_per_hour=2, corporate_action_window_post_days=-1),
    ],
    ids=['ramp', 'cap', 'pre', 'post'],
)
async def test_a_source_append_refuses_a_negative_number(world, settings):
    with pytest.raises(InvalidRequestError) as excinfo:
        await _source(world, settings=settings)

    _refused(excinfo)
    assert await world.rows(SOURCE_SETTINGS) == []


# ----------------------------------------------------------------------------------------- S11 ranges (tj-2ayia2)

INT32_MAX = 2**31 - 1

OUT_OF_RANGE = [
    pytest.param(SourceSettings(0, None), 'ramp_datasets_per_hour', id='ramp-zero'),
    pytest.param(SourceSettings(None, 0), 'request_cap_per_hour', id='cap-zero'),
    pytest.param(SourceSettings(0, 0), 'ramp_datasets_per_hour', id='both-zero'),
    pytest.param(SourceSettings(None, 1), 'request_cap_per_hour', id='cap-one'),
    pytest.param(SourceSettings(1, 1), 'request_cap_per_hour', id='ramp-one-cap-one'),
    pytest.param(SourceSettings(INT32_MAX + 1, None), 'ramp_datasets_per_hour', id='ramp-above-int32'),
    pytest.param(SourceSettings(None, INT32_MAX + 1), 'request_cap_per_hour', id='cap-above-int32'),
    pytest.param(SourceSettings(None, None, INT32_MAX + 1, 0), 'corporate_action_window_pre_days', id='pre-above'),
    pytest.param(SourceSettings(None, None, 0, INT32_MAX + 1), 'corporate_action_window_post_days', id='post-above'),
]

IN_RANGE = [
    pytest.param(SourceSettings(1, 2), id='the-minimums'),
    pytest.param(SourceSettings(INT32_MAX, INT32_MAX, INT32_MAX, INT32_MAX), id='int32-max'),
    pytest.param(SourceSettings(None, None, 0, 0), id='no-limit-window-zero'),
    pytest.param(SourceSettings(1, None), id='ramp-one-no-cap'),
    pytest.param(SourceSettings(None, 2), id='no-ramp-cap-two'),
]


@pytest.mark.parametrize(('settings', 'field'), OUT_OF_RANGE)
async def test_a_source_append_refuses_a_rate_or_window_out_of_range_and_writes_nothing(world, settings, field):
    """Zero is refused (a pause is enabled=false), a cap of 1 could never admit a creation, int32 max is the column's."""
    await _source(world)
    before = await world.rows(SOURCE_SETTINGS)
    commits_before = world.commits

    with pytest.raises(InvalidRequestError) as excinfo:
        await _source(world, settings=settings)

    _refused(excinfo)
    assert field in excinfo.value.detail, excinfo.value.detail
    assert world.commits == commits_before
    assert await world.rows(SOURCE_SETTINGS) == before


@pytest.mark.parametrize(('settings', 'field'), OUT_OF_RANGE)
async def test_create_panel_refuses_source_settings_out_of_range_and_writes_nothing(world, settings, field):
    """On a source with no version (so only the range can refuse it): nothing at all is written."""
    world.statements.clear()

    with pytest.raises(InvalidRequestError) as excinfo:
        await _panel(world, source_settings=settings)

    _refused(excinfo)
    assert field in excinfo.value.detail, excinfo.value.detail
    assert world.commits == 0
    assert not [statement for statement in world.statements if isinstance(statement, Insert)]
    assert await world.counts() == {table.name: 0 for table in TABLES}


@pytest.mark.parametrize('settings', IN_RANGE)
async def test_a_source_append_accepts_the_minimums_int32_max_and_no_limit(world, settings):
    record = await _source(world, settings=settings)

    (row,) = await world.rows(SOURCE_SETTINGS)
    assert record.settings == settings
    assert (row.ramp_datasets_per_hour, row.request_cap_per_hour) == (
        settings.ramp_datasets_per_hour,
        settings.request_cap_per_hour,
    )


@pytest.mark.parametrize('settings', IN_RANGE)
async def test_create_panel_accepts_the_minimums_int32_max_and_no_limit(world, settings):
    await _panel(world, source_settings=settings)

    first = await _current_source(world)
    assert first is not None and first.settings == settings


async def test_an_unset_rate_is_stored_null_never_zero(world):
    """None is NO LIMIT and is stored as NULL: 0 would be a refused value, so it must never stand in for None."""
    await _source(world, settings=SourceSettings(None, None))

    async with AsyncSession(world.engine) as db:
        nulls = (
            await db.execute(
                sa.select(sa.func.count()).where(
                    SOURCE_SETTINGS.c.ramp_datasets_per_hour.is_(None), SOURCE_SETTINGS.c.request_cap_per_hour.is_(None)
                )
            )
        ).scalar_one()
    assert nulls == 1
    current = await _current_source(world)
    assert current is not None
    assert current.settings.ramp_datasets_per_hour is None and current.settings.request_cap_per_hour is None


def _check_literal(name: str) -> int:
    """The number a single-comparison CHECK of the model compares against ('<column> >= <n>')."""
    (constraint,) = [c for c in SOURCE_SETTINGS.constraints if isinstance(c, sa.CheckConstraint) and c.name == name]
    found = re.fullmatch(r'\s*(\w+)\s*>=\s*(\d+)\s*', str(constraint.sqltext))
    assert found is not None, constraint.sqltext
    return int(found.group(2))


def test_the_minimum_constants_equal_the_checks_and_the_creation_cost():
    """D1: a DB CHECK needs a literal, so this pins the crud constant, the CHECK and REQUESTS_PER_CREATION together."""
    from data.store.app import panel_manager

    assert panel_settings.MIN_RAMP_DATASETS_PER_HOUR == 1
    assert _check_literal(PanelSourceSettingsVersion.RAMP_POSITIVE) == panel_settings.MIN_RAMP_DATASETS_PER_HOUR
    assert panel_settings.MIN_REQUEST_CAP_PER_HOUR == panel_manager.REQUESTS_PER_CREATION == 2
    assert _check_literal(PanelSourceSettingsVersion.REQUEST_CAP_MIN) == panel_settings.MIN_REQUEST_CAP_PER_HOUR
    assert panel_settings.INT32_MAX == INT32_MAX == 2147483647


async def test_an_append_to_an_unknown_panel_is_not_found_and_writes_nothing(world):
    await _source(world)
    before = await world.counts()

    with pytest.raises(InvalidRequestError) as excinfo:
        await _append(world, uuid.uuid4())

    _refused(excinfo, Reason.NOT_FOUND)
    assert await world.counts() == before


async def test_the_source_of_a_panel_cannot_change():
    """No write takes a source for an existing panel: another source is another panel."""
    assert 'source' not in inspect.signature(append_settings_version).parameters
    assert 'source' not in {f.name for f in dataclasses.fields(PanelSettings)}


async def test_enable_and_disable_are_settings_versions(world):
    await _source(world)
    panel, _ = await _panel(world)

    off = await _append(world, panel.id, dataclasses.replace(SETTINGS_V1, enabled=False), reason='pause')
    off_panels = await _enabled(world)
    on = await _append(world, panel.id, SETTINGS_V1, reason='resume')

    assert [row.enabled for row in sorted(await world.rows(SETTINGS), key=lambda r: r.version_no)] == [
        True,
        False,
        True,
    ]
    assert (off.version_no, off.reason, on.version_no, on.reason) == (2, 'pause', 3, 'resume')
    assert off_panels == []
    assert [item.current.version_no for item in await _enabled(world)] == [3]
    (panel_row,) = await world.rows(PANELS)
    assert panel_row.source is SOURCE


# --------------------------------------------------------------------------------------------- S5 S6 reads


async def _enabled(world: World, source: DataSource = SOURCE) -> list[EnabledPanel]:
    async with world.session() as db:
        return await enabled_panels_on_source(db, source)


async def _current(world: World, panel_id: uuid.UUID) -> PanelSettingsRecord | None:
    async with world.session() as db:
        return await current_settings(db, panel_id)


async def _current_source(world: World, source: DataSource = SOURCE) -> SourceSettingsRecord | None:
    async with world.session() as db:
        return await current_source_settings(db, source)


async def test_current_settings_is_the_latest_version_and_none_for_an_unknown_panel(world):
    await _source(world)
    panel, v1 = await _panel(world)

    assert await _current(world, uuid.uuid4()) is None
    assert await _current(world, panel.id) == v1
    v2 = await _append(world, panel.id, dataclasses.replace(SETTINGS_V1, exchange='NYSE'))
    assert await _current(world, panel.id) == v2


async def test_enabled_panels_on_source_reads_each_panels_latest_version_on_that_source_only(world):
    await _source(world)
    await _source(world, source=OTHER_SOURCE)
    disabled = PanelSettings(enabled=False, cadence=PanelCadence.DAILY)
    stays_on, _ = await _panel(world, name='stays-on')
    turned_off, _ = await _panel(world, name='turned-off')
    turned_on, _ = await _panel(world, name='turned-on', settings=disabled)
    never_on, _ = await _panel(world, name='never-on', settings=disabled)
    elsewhere, _ = await _panel(world, name='elsewhere', source=OTHER_SOURCE)
    await _append(world, turned_off.id, disabled)
    latest_on = await _append(world, turned_on.id, SETTINGS_V1)
    # Creation order is the rate-sharing order: pinned with created_at set apart (SQLite's clock has whole seconds).
    for offset, panel in enumerate([turned_on, stays_on, turned_off, never_on, elsewhere]):
        at = datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=offset)
        await world.put(sa.update(PANELS).where(PANELS.c.id == panel.id).values(created_at=at))

    found = await _enabled(world)

    assert [item.panel.name for item in found] == ['turned-on', 'stays-on']
    assert found[0].current == latest_on
    assert found[0].panel.source is SOURCE and found[0].panel.id == turned_on.id
    assert [item.panel.name for item in await _enabled(world, OTHER_SOURCE)] == ['elsewhere']
    assert never_on.id not in {item.panel.id for item in found}


async def test_enabled_panels_with_equal_creation_times_are_ordered_by_id(world):
    await _source(world)
    panels = [(await _panel(world, name=f'p{index}'))[0] for index in range(4)]
    at = datetime(2026, 10, 1, tzinfo=UTC)
    await world.put(sa.update(PANELS).values(created_at=at))

    found = await _enabled(world)

    assert [item.panel.id for item in found] == sorted(panel.id for panel in panels)


async def test_current_source_settings_is_none_without_a_version_and_the_latest_with_some(world):
    """No fallback: None, never a record of defaults."""
    assert await _current_source(world) is None
    first = await _source(world)
    assert await _current_source(world) == first
    second = await _source(world, settings=SourceSettings(3, 4, 10, 20), reason='tighter')
    assert await _current_source(world) == second
    assert await _current_source(world, OTHER_SOURCE) is None
    assert (second.version_no, second.settings) == (2, SourceSettings(3, 4, 10, 20))


async def test_source_versions_are_numbered_per_source(world):
    a1 = await _source(world)
    b1 = await _source(world, source=OTHER_SOURCE)
    a2 = await _source(world)

    assert (a1.version_no, b1.version_no, a2.version_no) == (1, 1, 2)


async def test_no_constant_names_a_rate_and_the_only_defaults_are_the_ruled_ones():
    """The 03:25 UTC ruling: DEFAULT_PANEL_CADENCE and the 90-day window, nothing else; the rates have none.

    ADR tj-2ayia2 adds three BOUNDS (the minimums and int32 max), which are no default: an unset rate is None, no
    limit, and the dataclass still makes the caller say it (no field default for either rate).
    And a panel has no size at all (ADR tj-46jkey): not a field with no default, no field.
    """
    constants = {name for name in vars(panel_settings) if name.isupper() and not name.startswith('_')}
    assert constants == {
        'DEFAULT_PANEL_CADENCE',
        'DEFAULT_CORPORATE_ACTION_WINDOW_DAYS',
        'MIN_RAMP_DATASETS_PER_HOUR',
        'MIN_REQUEST_CAP_PER_HOUR',
        'INT32_MAX',
        # ADR tj-ed4t8m D2 and D3 (owner ruling 2026-11-10): the include_otc default (off, a ruled default, not a rate)
        # and the exchange values it governs (a predicate, not a default).
        'DEFAULT_INCLUDE_OTC',
        'OTC_EXCHANGES',
    }
    assert panel_settings.DEFAULT_INCLUDE_OTC is False
    assert frozenset({'OTC'}) == panel_settings.OTC_EXCHANGES
    assert DEFAULT_CORPORATE_ACTION_WINDOW_DAYS == 90
    source_defaults = {f.name: f.default for f in dataclasses.fields(SourceSettings)}
    assert source_defaults == {
        'ramp_datasets_per_hour': dataclasses.MISSING,
        'request_cap_per_hour': dataclasses.MISSING,
        'corporate_action_window_pre_days': 90,
        'corporate_action_window_post_days': 90,
    }
    panel_defaults = {f.name: f.default for f in dataclasses.fields(PanelSettings)}
    assert panel_defaults['enabled'] is dataclasses.MISSING
    assert 'size' not in panel_defaults
    assert panel_defaults['cadence'] is None, 'the default cadence is applied by create_panel, not the dataclass'
    assert panel_defaults['allow_symbols'] == panel_defaults['deny_symbols'] == ()
    assert all(panel_defaults[name] is None for name in ('asset_class', 'exchange', 'active', 'tradable'))
    assert panel_defaults['include_otc'] is panel_settings.DEFAULT_INCLUDE_OTC


# --------------------------------------------------------------------------------------------- S7 numbering locks


async def test_a_panel_version_reads_the_panel_for_update_before_its_max_version_read(world):
    await _source(world)
    panel, _ = await _panel(world)
    world.statements.clear()

    await _append(world, panel.id)

    sql = [_sql(statement) for statement in world.statements]
    locked = next(i for i, text in enumerate(sql) if text.endswith('FOR UPDATE') and 'FROM panel ' in text)
    numbered = next(i for i, text in enumerate(sql) if 'max(panel_settings_version.version_no)' in text)
    inserted = next(i for i, text in enumerate(sql) if text.startswith('INSERT INTO panel_settings_version'))
    assert locked < numbered < inserted, sql


async def test_a_source_version_takes_the_sources_advisory_lock_before_its_max_version_read(world):
    world.statements.clear()

    await _source(world)
    await _source(world, source=OTHER_SOURCE)
    await _source(world)

    sql = [_sql(statement) for statement in world.statements]
    lock_at = [i for i, text in enumerate(sql) if 'pg_advisory_xact_lock(hashtext(' in text]
    max_at = [i for i, text in enumerate(sql) if 'max(panel_source_settings_version.version_no)' in text]
    assert len(lock_at) == len(max_at) == 3
    assert all(lock < numbered for lock, numbered in zip(lock_at, max_at, strict=True))
    # One key per source, the same key every time for one source: the lock serialises a source, not every source.
    assert len(world.locks) == 3
    assert world.locks[0] == world.locks[2] != world.locks[1]


SOURCE_LOCK_KEY = f'{SOURCE_SETTINGS.name}:{SOURCE.name}'
_LOCK_KEY = re.compile(r"pg_advisory_xact_lock\(hashtext\('((?:[^']|'')*)'\)\)")


def _name_key(name: str) -> str:
    """The name lock's key as the bead rules it: 'panel:name:' then the TRIMMED name (tj-7zjov1.244)."""
    return f'panel:name:{name}'


def _crc(key: str) -> int:
    """What the tier's hashtext stand-in makes of a key, so world.locks can be matched to a key."""
    return zlib.crc32(key.encode())


def _lock_key(statement: Any) -> str | None:
    """The literal key a pg_advisory_xact_lock(hashtext(...)) statement locks on, or None for any other statement."""
    if not isinstance(statement, Select):
        return None
    text = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))
    found = _LOCK_KEY.search(text)
    return None if found is None else found.group(1).replace("''", "'")


def _is_name_read(text: str) -> bool:
    """create_panel's name check: the panel id by name."""
    return text.startswith('SELECT panel.id FROM panel WHERE panel.name =')


def _is_source_guard_read(text: str) -> bool:
    """create_panel's has_version read: the source's newest version, not the numbering's max."""
    return text.startswith('SELECT') and 'FROM panel_source_settings_version' in text and 'DESC' in text


@pytest.mark.parametrize('with_source_settings', [True, False], ids=['with-source-settings', 'none-given'])
@pytest.mark.parametrize('has_version', [False, True], ids=['source-without-a-version', 'source-with-one'])
async def test_create_panel_locks_the_source_before_its_guard_read(world, has_version, with_source_settings):
    """tj-7zjov1.212: the source lock comes BEFORE the has_version read, so two concurrent creates cannot both see none.

    ADR tj-2ayia2 D4: on EVERY create, with source settings or not, since a create with none given now writes the
    source's version 1 too (two concurrent creates must write one version 1, not a 1 and a 2).
    The ORDER is the pin, not a count: the second lock (inside _next_source_version_no) is re-entrant and harmless, and
    a lock moved back after the read would still be counted. Whether the order actually serialises two creates is a
    Postgres question SQLite cannot answer (one writer, no advisory locks): tests/system/test_panel_settings_pg.py PS8.
    """
    if has_version:
        await _source(world)
    world.statements.clear()
    world.locks.clear()

    if has_version and with_source_settings:
        with pytest.raises(InvalidRequestError) as excinfo:
            await _panel(world, source_settings=RATES)
        _refused(excinfo)
    else:
        await _panel(world, source_settings=RATES if with_source_settings else None)

    sql = [_sql(statement) for statement in world.statements]
    keys = [_lock_key(statement) for statement in world.statements]
    # Filtered to the SOURCE key (tj-7zjov1.244 added the name lock, taken first): the name lock at index 0 must not
    # stand in for a source lock moved after the guard read.
    source_at = [i for i, key in enumerate(keys) if key == SOURCE_LOCK_KEY]
    guard_at = [i for i, text in enumerate(sql) if _is_source_guard_read(text)]
    assert len(guard_at) == 1, sql
    assert source_at and source_at[0] < guard_at[0], f'the guard read ran before the source lock: {sql}'
    # One source, one key: the early lock and the numbering's re-entrant one are the same lock.
    assert len(source_at) == (1 if has_version else 2), keys
    assert world.locks.count(_crc(SOURCE_LOCK_KEY)) == len(source_at), world.locks
    # Counted separately: the name lock, once, and no third key.
    assert [key for key in keys if key is not None] == [_name_key('alpha'), *([SOURCE_LOCK_KEY] * len(source_at))]
    assert len(world.locks) == len(source_at) + 1, world.locks
    if not has_version:
        numbered = next(i for i, text in enumerate(sql) if 'max(panel_source_settings_version.version_no)' in text)
        assert guard_at[0] < numbered, sql


# --------------------------------------------------------------------------------------------- S10 the name lock
#
# tj-7zjov1.244 (ruled by the architect gate on tj-7zjov1.36): create_panel takes a transaction-scoped advisory lock
# on the TRIMMED name FIRST inside its transaction, before the name read, so the loser of two concurrent creates of one
# name waits, then reads the winner's row and gets the ordinary 422 refusal instead of the unique index's 409. Lock
# order: name, then source (every create since ADR tj-2ayia2 D4), then rows. The unique violation is NOT caught. Whether the lock
# actually serialises two creates is a Postgres question SQLite cannot answer: tests/system/test_panel_routes_pg.py PR3.


@pytest.mark.parametrize('with_source_settings', [False, True], ids=['no-source-settings', 'with-source-settings'])
async def test_create_panel_takes_the_name_lock_first_then_the_source_lock_then_writes(world, with_source_settings):
    if not with_source_settings:
        await _source(world)
    world.statements.clear()
    world.locks.clear()

    await _panel(world, name='alpha', source_settings=RATES if with_source_settings else None)

    sql = [_sql(statement) for statement in world.statements]
    keys = [_lock_key(statement) for statement in world.statements]
    name_lock = keys.index(_name_key('alpha'))
    name_read = next(i for i, text in enumerate(sql) if _is_name_read(text))
    guard = next(i for i, text in enumerate(sql) if _is_source_guard_read(text))
    panel_insert = next(i for i, text in enumerate(sql) if text.startswith('INSERT INTO panel '))
    assert name_lock == 0, f'the name lock is the first statement of the transaction: {sql}'
    assert name_lock < name_read < guard < panel_insert, sql
    # The source lock on EVERY create (ADR tj-2ayia2 D4), after the name read and before the guard read.
    source_lock = keys.index(SOURCE_LOCK_KEY)
    assert name_read < source_lock < guard, sql
    if with_source_settings:
        # The source had no version: the numbering's re-entrant lock follows.
        assert [key for key in keys if key is not None] == [_name_key('alpha'), SOURCE_LOCK_KEY, SOURCE_LOCK_KEY]
        assert world.locks == [_crc(_name_key('alpha')), _crc(SOURCE_LOCK_KEY), _crc(SOURCE_LOCK_KEY)]
    else:
        # The source had a version and none is given: the name lock, then the source lock once, nothing written to it.
        assert [key for key in keys if key is not None] == [_name_key('alpha'), SOURCE_LOCK_KEY], keys
        assert world.locks == [_crc(_name_key('alpha')), _crc(SOURCE_LOCK_KEY)]


async def test_the_name_lock_key_is_the_trimmed_name(world):
    """' A ' and 'A' share one key, so creates differing only by surrounding whitespace serialise; 'A' and 'B' do not."""
    await _source(world)
    world.locks.clear()

    await _panel(world, name='  A \t')
    with pytest.raises(InvalidRequestError):
        await _panel(world, name='A')
    await _panel(world, name='B')

    # The source lock (every create, tj-2ayia2 D4) is filtered out: this pins the name keys only.
    name_locks = [key for key in world.locks if key != _crc(SOURCE_LOCK_KEY)]
    assert name_locks == [_crc(_name_key('A')), _crc(_name_key('A')), _crc(_name_key('B'))]
    assert world.locks.count(_crc(SOURCE_LOCK_KEY)) == 2, 'the refused duplicate took no source lock'
    assert _crc(_name_key('A')) != _crc(_name_key('B'))


async def test_a_duplicate_name_is_refused_with_the_ordinary_422_after_the_name_lock(world):
    await _source(world)
    await _panel(world, name='alpha')
    before = await world.counts()
    world.statements.clear()
    world.locks.clear()

    with pytest.raises(InvalidRequestError) as excinfo:
        await _panel(world, name=' alpha ', source_settings=RATES)

    _refused(excinfo)
    assert excinfo.value.detail == 'A panel with that name already exists.'
    assert REASONS[excinfo.value.reason].http_status == 422
    sql = [_sql(statement) for statement in world.statements]
    # The lock, then the read, then the refusal: no source lock, no guard read, no write.
    assert [_lock_key(statement) for statement in world.statements] == [_name_key('alpha'), None], sql
    assert _is_name_read(sql[1]), sql
    assert world.locks == [_crc(_name_key('alpha'))]
    assert await world.counts() == before


@pytest.mark.parametrize('refused', [False, True], ids=['created', 'refused'])
async def test_the_name_lock_is_transaction_scoped_and_ends_with_the_transaction(world, refused):
    """pg_advisory_xact_lock, never the session-level pg_advisory_lock: the lock goes at the commit or the rollback.

    A session-level lock would outlive a refusal on a pooled connection and stall the next create of that name. SQLite
    has no locks to hold, so the pin is the function named and the transaction closed on both paths.
    """
    await _source(world)
    if refused:
        await _panel(world, name='alpha')
    world.statements.clear()

    async with world.session() as db:
        if refused:
            with pytest.raises(InvalidRequestError):
                await create_panel(
                    db,
                    name='alpha',
                    source=SOURCE,
                    granularity=Granularity.ONE_DAY,
                    settings=SETTINGS_V1,
                    actor=ACTOR,
                    reason='again',
                )
        else:
            await create_panel(
                db,
                name='alpha',
                source=SOURCE,
                granularity=Granularity.ONE_DAY,
                settings=SETTINGS_V1,
                actor=ACTOR,
                reason='first',
            )
        assert not db.in_transaction(), 'create_panel left its transaction, and so the name lock, open'

    lock_sql = [_sql(statement) for statement in world.statements if _lock_key(statement) is not None]
    assert lock_sql and all('pg_advisory_xact_lock(' in text for text in lock_sql), lock_sql
    assert not any(re.search(r'pg_advisory_lock\(|pg_try_advisory', text) for text in lock_sql), lock_sql


async def test_create_panel_does_not_catch_the_unique_violation():
    """Ruled (tj-7zjov1.244): the lock makes the read decide, and the unique violation is not caught.

    A catch would make the constraint name a parsed contract and give one message two sources. So create_panel has no
    try statement and the module never names IntegrityError.
    """
    tree = ast.parse(inspect.getsource(panel_settings))
    (function,) = [
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == 'create_panel'
    ]
    assert not [node for node in ast.walk(function) if isinstance(node, ast.Try | ast.TryStar)]
    assert 'IntegrityError' not in {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert 'IntegrityError' not in {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


# --------------------------------------------------------------------------------------------- ids on SQLite


@pytest.mark.parametrize('hex_id', ['1' * 32, '1234567890123456789012345e789012'], ids=['integer-like', 'real-like'])
async def test_an_id_that_reads_as_a_number_round_trips(world, hex_id):
    """The tier's own guard: ids are stored as text, so a random id never fails a read now and then."""
    await _source(world)
    panel_id = uuid.UUID(hex_id)
    await world.put(
        sa.insert(PANELS).values(id=panel_id, name='numeric', source=SOURCE, granularity=Granularity.ONE_DAY)
    )

    version = await _append(world, panel_id)

    current = await _current(world, panel_id)
    assert current is not None and current.panel_id == panel_id and current == version
    assert [item.panel.id for item in await _enabled(world)] == [panel_id]


# --------------------------------------------------------------------------------------------- S9 append-only


async def test_every_statement_the_module_sends_is_a_select_or_an_insert(world):
    """Append-only, observed: a full round of every function sends no UPDATE and no DELETE."""
    panel, _ = await _panel(world, source_settings=RATES)
    await _source(world)
    await _append(world, panel.id, dataclasses.replace(SETTINGS_V1, enabled=False))
    await _current(world, panel.id)
    await _current_source(world)
    await _enabled(world)
    for refused in (_append(world, uuid.uuid4()), _panel(world, name='alpha'), _source(world, actor=' ')):
        with pytest.raises(InvalidRequestError):
            await refused

    kinds = {type(statement).__name__ for statement in world.statements}
    assert all(isinstance(statement, Select | Insert) for statement in world.statements), kinds


async def test_the_module_exposes_no_update_or_delete():
    public = {
        name
        for name, value in vars(panel_settings).items()
        if inspect.iscoroutinefunction(value) and not name.startswith('_')
    }
    assert public == PUBLIC_FUNCTIONS
    source = inspect.getsource(panel_settings)
    # with_for_update is a lock, not a write: the word boundary keeps it out.
    writes = re.findall(
        r'(?<![\w.])(?:update|delete)\(|\.(?:update|delete)\(|\b(?:UPDATE|DELETE FROM|TRUNCATE)\b', source
    )
    assert writes == [], f'panel_settings.py carries {writes}'
    imported = {
        alias.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom) for alias in node.names
    }
    assert not {'update', 'delete'} & imported


async def test_the_records_are_frozen_dataclasses():
    for record in (PanelSettings, SourceSettings, PanelRecord, PanelSettingsRecord, SourceSettingsRecord, EnabledPanel):
        assert dataclasses.is_dataclass(record) and record.__dataclass_params__.frozen, record.__name__
    assert [f.name for f in dataclasses.fields(EnabledPanel)] == ['panel', 'current']
