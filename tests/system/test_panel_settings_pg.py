"""The panel settings repository and the reserved panel owner (tj-7zjov1.176, P20b) on the real, migrated Postgres.

WRITTEN, NOT YET RUN where it was written (validator tj-7zjov1.178): it runs with the rest of the suite (make
test-system, or the agent-stack run_system_tests after test_up) on a stack migrated to the head (b6d1f93a7c42 or later).

THE DESIGN: ADR tj-26jt9w addendum 20 and the P20b architect note of 03:25 UTC 2026-10-28. WHAT THIS PROVES that
server/data/store/tests/test_panel_settings.py cannot, because SQLite has one writer, no row locks, no advisory locks,
no arrays and no Postgres enums:
  PS1 version numbering of ONE PANEL is serialised: concurrent append_settings_version calls all succeed, numbered
      2..n+1 without a gap or a unique violation (the panel row is read FOR UPDATE before the max read);
  PS2 version numbering of ONE SOURCE is serialised by the transaction advisory lock: concurrent
      append_source_settings_version calls all succeed with consecutive numbers;
  PS3 the rows round-trip on asyncpg: the varchar[] filters, the panel_cadence and assettype enums, NULL filters,
      saved_at the DATABASE's clock (inside the transaction window read from now()), the window defaulted to 90;
  PS4 (retired by tj-7zjov1.209/.210: effective-from has ONE definition, crud/panel_ledger.py boundary_read's, per
      panel and non-dry-run, pinned on Postgres in test_panel_ledger_pg.py; the two P20b functions are deleted);
  PS7 create_panel REFUSES source settings for a source that already has a version (INVALID_REQUEST) and writes
      nothing: no panel, no settings version, and the source's current rates unchanged;
  PS8 the guard is judged under the source lock (tj-7zjov1.212/.213): CONCURRENT creates giving source settings on a
      source with no version leave exactly one panel and ONE source version, the winner's; every other create is
      refused INVALID_REQUEST and writes nothing;
  PS6 enabled_panels_on_source compiles and runs on Postgres (its correlated latest-version subquery);
  PS5 POST /store with the body owner 'panel' on the running data_store: 422 INVALID_REQUEST with the exact message,
      and no ingest_request row written for it.

ISOLATION. No service writes the panel tables yet (P22 is not built). Panel names carry the run id; source-version
numbers are asserted RELATIVE to the latest version present at the start, so a seeded or earlier version never matters.
The panel fixture writes its source version through append_source_settings_version and then creates the panel WITHOUT
source settings: since tj-7zjov1.209 create_panel refuses them for a source that already has a version, so the fixture
works whatever versions SOURCE already holds.
Each test deletes exactly the rows it wrote, failed or not (a test-only delete: the module itself is append-only).
"""

import asyncio
import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource
from common.errors.vocabulary import InvalidRequestError, Reason
from data.store.app.client_label import PANEL_OWNER
from data.store.app.database.crud.panel_settings import (
    PanelSettings,
    SourceSettings,
    _lock_source,
    append_settings_version,
    append_source_settings_version,
    create_panel,
    current_settings,
    current_source_settings,
    enabled_panels_on_source,
)
from data.store.app.database.models.ingest_request import IngestRequest
from data.store.app.database.models.panel import (
    Panel,
    PanelCadence,
    PanelPass,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
)
from routers.data_store.app_endpoints import AssetDatasetStoreInterface
from tests.system.problem_json import assert_problem


# asyncio is marked per coroutine test, not here: the PS5 route test is a plain def, and a module-level asyncio mark on
# it raised a PytestWarning (tj-7zjov1.213).
pytestmark = [pytest.mark.data_store]

PANELS = Panel.__table__
SETTINGS = PanelSettingsVersion.__table__
SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__
PASSES = PanelPass.__table__
REQUESTS = IngestRequest.__table__
SOURCE = DataSource.MANUAL_ENTRY
CONCURRENT = 8
RATES = SourceSettings(ramp_datasets_per_hour=5, request_cap_per_hour=60)
SETTINGS_V1 = PanelSettings(enabled=True, size=10, cadence=PanelCadence.DAILY)
REFUSAL = "The owner 'panel' is reserved for the panel manager: use another owner."


class Written:
    """The ids one test wrote, deleted at its end."""

    def __init__(self) -> None:
        self.panels: list[uuid.UUID] = []
        self.source_versions: list[uuid.UUID] = []


@pytest.fixture
def written(pg_engine: Engine) -> Iterator[Written]:
    ids = Written()
    yield ids
    with pg_engine.begin() as conn:
        if ids.panels:
            conn.execute(sa.delete(PASSES).where(PASSES.c.panel_id.in_(ids.panels)))
            conn.execute(sa.delete(SETTINGS).where(SETTINGS.c.panel_id.in_(ids.panels)))
            conn.execute(sa.delete(PANELS).where(PANELS.c.id.in_(ids.panels)))
        if ids.source_versions:
            conn.execute(sa.delete(SOURCE_SETTINGS).where(SOURCE_SETTINGS.c.id.in_(ids.source_versions)))


@pytest_asyncio.fixture
async def panel(pg_async_engine: AsyncEngine, written: Written, run_identity):
    """One panel on SOURCE with version 1, on a new source settings version of ours written as its own write first."""
    async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
        source_version = await append_source_settings_version(
            db, source=SOURCE, settings=RATES, actor='operator', reason='system test rates'
        )
    written.source_versions.append(source_version.id)
    async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
        record, version = await create_panel(
            db,
            name=f'system-test-{run_identity.run_id}-panel-{uuid.uuid4().hex[:8]}',
            source=SOURCE,
            settings=SETTINGS_V1,
            actor='operator',
            reason='system test panel',
        )
    written.panels.append(record.id)
    return record, version, source_version


async def _latest_source_version_no(engine: AsyncEngine) -> int:
    async with AsyncSession(engine) as db:
        latest = await current_source_settings(db, SOURCE)
    return 0 if latest is None else latest.version_no


# --------------------------------------------------------------------------------------------- PS1 PS2 numbering


@pytest.mark.asyncio
async def test_concurrent_versions_of_one_panel_are_numbered_without_a_gap_or_a_collision(pg_async_engine, panel):
    record, _, _ = panel

    async def append(index: int):
        async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
            return await append_settings_version(
                db,
                panel_id=record.id,
                settings=PanelSettings(enabled=True, size=index, cadence=PanelCadence.WEEKLY),
                actor='operator',
                reason=f'concurrent {index}',
            )

    versions = await asyncio.gather(*(append(index) for index in range(CONCURRENT)))

    assert sorted(version.version_no for version in versions) == list(range(2, CONCURRENT + 2))
    async with AsyncSession(pg_async_engine) as db:
        current = await current_settings(db, record.id)
    assert current is not None and current.version_no == CONCURRENT + 1


@pytest.mark.asyncio
async def test_concurrent_versions_of_one_source_are_numbered_consecutively(pg_async_engine, written):
    start = await _latest_source_version_no(pg_async_engine)

    async def append(index: int):
        async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
            return await append_source_settings_version(
                db, source=SOURCE, settings=SourceSettings(index, index), actor='operator', reason=f'source {index}'
            )

    try:
        versions = await asyncio.gather(*(append(index) for index in range(CONCURRENT)), return_exceptions=True)
    finally:
        async with AsyncSession(pg_async_engine) as db:
            mine = await db.execute(
                sa.select(SOURCE_SETTINGS.c.id).where(
                    SOURCE_SETTINGS.c.source == SOURCE, SOURCE_SETTINGS.c.reason.like('source %')
                )
            )
            written.source_versions.extend(mine.scalars())

    failures = [version for version in versions if isinstance(version, BaseException)]
    assert failures == [], f'{len(failures)} concurrent appends failed: {[type(f).__name__ for f in failures]}'
    assert sorted(version.version_no for version in versions) == list(range(start + 1, start + CONCURRENT + 1))


# --------------------------------------------------------------------------------------------- PS3 round trip


@pytest.mark.asyncio
async def test_a_full_version_round_trips_on_asyncpg_with_the_database_clock(pg_async_engine, panel):
    record, version_1, source_version = panel
    settings = PanelSettings(
        enabled=False,
        size=0,
        cadence=PanelCadence.MONTHLY,
        allow_symbols=('ZZPNLA', 'ZZPNLB'),
        deny_symbols=('ZZPNLC',),
        asset_class=AssetType.STOCK,
        exchange='NYSE',
        active=True,
        tradable=None,
    )

    async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
        before = (await db.execute(sa.select(sa.func.now()))).scalar_one()
        await db.rollback()
        version_2 = await append_settings_version(
            db, panel_id=record.id, settings=settings, actor='operator', reason='  round trip  '
        )
        after = (await db.execute(sa.select(sa.func.now()))).scalar_one()
    async with AsyncSession(pg_async_engine) as db:
        stored = await current_settings(db, record.id)

    assert stored == version_2
    assert stored.settings == settings and stored.reason == 'round trip' and stored.version_no == 2
    assert before <= stored.saved_at <= after, 'saved_at is not the database transaction time'
    assert version_1.settings.allow_symbols == () and version_1.settings.asset_class is None
    window = source_version.settings
    assert (window.corporate_action_window_pre_days, window.corporate_action_window_post_days) == (90, 90)
    assert (window.ramp_datasets_per_hour, window.request_cap_per_hour) == (5, 60)


# --------------------------------------------------------------------------------------------- PS7 the source guard


@pytest.mark.asyncio
async def test_create_panel_refuses_source_settings_for_a_source_with_a_version_on_postgres(
    pg_async_engine, panel, written, run_identity
):
    """SOURCE has a version (the fixture's, at least): source settings with a new panel are refused, nothing written."""
    _, _, source_version = panel
    name = f'system-test-{run_identity.run_id}-refused-{uuid.uuid4().hex[:8]}'
    async with AsyncSession(pg_async_engine) as db:
        before = await current_source_settings(db, SOURCE)
        rows_before = (
            await db.execute(
                sa.select(sa.func.count()).select_from(SOURCE_SETTINGS).where(SOURCE_SETTINGS.c.source == SOURCE)
            )
        ).scalar_one()

    try:
        with pytest.raises(InvalidRequestError) as excinfo:
            async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
                await create_panel(
                    db,
                    name=name,
                    source=SOURCE,
                    settings=SETTINGS_V1,
                    actor='operator',
                    reason='system test refused panel',
                    source_settings=SourceSettings(ramp_datasets_per_hour=1, request_cap_per_hour=2),
                )
    finally:
        # A regression must not leave a MANUAL_ENTRY version behind: the next panel fixture would be refused by it.
        async with AsyncSession(pg_async_engine) as db:
            leaked_panels = (await db.execute(sa.select(PANELS.c.id).where(PANELS.c.name == name))).scalars().all()
            leaked_sources = (
                (
                    await db.execute(
                        sa.select(SOURCE_SETTINGS.c.id).where(SOURCE_SETTINGS.c.reason == 'system test refused panel')
                    )
                )
                .scalars()
                .all()
            )
        written.panels.extend(leaked_panels)
        written.source_versions.extend(leaked_sources)

    assert excinfo.value.reason is Reason.INVALID_REQUEST, excinfo.value
    async with AsyncSession(pg_async_engine) as db:
        after = await current_source_settings(db, SOURCE)
        rows_after = (
            await db.execute(
                sa.select(sa.func.count()).select_from(SOURCE_SETTINGS).where(SOURCE_SETTINGS.c.source == SOURCE)
            )
        ).scalar_one()
        panels = (await db.execute(sa.select(PANELS.c.id).where(PANELS.c.name == name))).all()
        versions = (
            await db.execute(
                sa.select(sa.func.count()).select_from(SETTINGS).where(SETTINGS.c.reason == 'system test refused panel')
            )
        ).scalar_one()
    assert before is not None and before.version_no >= source_version.version_no
    assert after == before, "the source's current rates changed"
    assert rows_after == rows_before, 'a source settings version was written'
    assert panels == [] and versions == 0, 'a refused create wrote a panel or a settings version'


# --------------------------------------------------------------------------------------------- PS8 the guard's race


async def _source_with_no_version(engine: AsyncEngine) -> DataSource:
    """SOURCE when it holds no source settings version, else the first DataSource that holds none."""
    async with AsyncSession(engine) as db:
        held = set((await db.execute(sa.select(SOURCE_SETTINGS.c.source).distinct())).scalars())
    for candidate in (SOURCE, *DataSource):
        if candidate not in held:
            return candidate
    pytest.fail(
        'PS8 needs a source with no source settings version, and every DataSource holds one: reset the test '
        'database (agent-stack test_reset, then migrate) before the system suite.',
        pytrace=False,
    )


async def _await_lock_waiters(engine: AsyncEngine, holder_pid: int, expected: int, timeout_s: float = 30.0) -> None:
    """Wait until `expected` backends are queued on the advisory lock the backend `holder_pid` holds."""
    waiting = sa.text(
        'SELECT count(*) FROM pg_locks w JOIN pg_locks g ON g.locktype = w.locktype AND g.database = w.database '
        'AND g.classid = w.classid AND g.objid = w.objid AND g.objsubid = w.objsubid '
        "WHERE w.locktype = 'advisory' AND NOT w.granted AND g.granted AND g.pid = :holder"
    )
    seen = 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        async with engine.connect() as conn:
            seen = (await conn.execute(waiting, {'holder': holder_pid})).scalar_one()
        if seen >= expected:
            return
        await asyncio.sleep(0.05)
    pytest.fail(f'only {seen} of {expected} concurrent creates queued on the source lock within {timeout_s} s')


@pytest.mark.asyncio
async def test_concurrent_creates_with_source_settings_on_a_source_with_no_version_write_one_version(
    pg_async_engine, written, run_identity
):
    """tj-7zjov1.212: CONCURRENT creates, each giving source settings: one wins, every other is refused, ONE version.

    Made deterministic, not a race left to the scheduler: a gate session takes the source's own lock (_lock_source, the
    module's key) first, every create is started and is waited for until all of them queue on that lock, then the gate
    lets go. With the lock taken BEFORE the has_version read (the fix) each create queues before reading, so the second
    to run reads the winner's version 1 and is refused. With the read before the lock (the pre-fix code) every create
    has already read "no version" when it queues, so every one passes the guard and the source gets CONCURRENT versions.
    """
    source = await _source_with_no_version(pg_async_engine)
    tag = f'system-test-{run_identity.run_id}-race-{uuid.uuid4().hex[:8]}'
    rates = {
        index: SourceSettings(ramp_datasets_per_hour=index + 1, request_cap_per_hour=100 + index)
        for index in range(CONCURRENT)
    }

    async def create(index: int):
        async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
            return await create_panel(
                db,
                name=f'{tag}-{index}',
                source=source,
                settings=SETTINGS_V1,
                actor='operator',
                reason=f'{tag} reason {index}',
                source_settings=rates[index],
            )

    tasks: list[asyncio.Task] = []
    try:
        async with AsyncSession(pg_async_engine) as gate:
            holder = (await gate.execute(sa.select(sa.func.pg_backend_pid()))).scalar_one()
            await _lock_source(gate, source)
            tasks = [asyncio.create_task(create(index)) for index in range(CONCURRENT)]
            try:
                await _await_lock_waiters(pg_async_engine, holder, CONCURRENT)
            finally:
                await gate.rollback()
    finally:
        # Every create has finished, committed or refused, before the rows are collected for deletion.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        async with AsyncSession(pg_async_engine) as db:
            written.panels.extend(
                (await db.execute(sa.select(PANELS.c.id).where(PANELS.c.name.like(f'{tag}-%')))).scalars()
            )
            written.source_versions.extend(
                (
                    await db.execute(
                        sa.select(SOURCE_SETTINGS.c.id).where(
                            SOURCE_SETTINGS.c.source == source, SOURCE_SETTINGS.c.reason.like(f'{tag} reason %')
                        )
                    )
                ).scalars()
            )

    won = [result for result in results if not isinstance(result, BaseException)]
    refused = [result for result in results if isinstance(result, BaseException)]
    assert len(won) == 1, f'{len(won)} of {CONCURRENT} concurrent creates passed the source guard'
    assert all(isinstance(error, InvalidRequestError) for error in refused), [type(e).__name__ for e in refused]
    assert all(error.reason is Reason.INVALID_REQUEST for error in refused), [str(e) for e in refused]
    assert all('already has settings' in error.detail for error in refused), [error.detail for error in refused]
    ((winner, winner_v1),) = won
    index = int(winner.name.rsplit('-', 1)[1])

    async with AsyncSession(pg_async_engine) as db:
        versions = (await db.execute(sa.select(*SOURCE_SETTINGS.c).where(SOURCE_SETTINGS.c.source == source))).all()
        current = await current_source_settings(db, source)
        panels = (await db.execute(sa.select(PANELS.c.id).where(PANELS.c.name.like(f'{tag}-%')))).scalars().all()
        panel_versions = (
            (await db.execute(sa.select(SETTINGS.c.panel_id).where(SETTINGS.c.reason.like(f'{tag} reason %'))))
            .scalars()
            .all()
        )
    assert len(versions) == 1, f'{source.name} holds {len(versions)} source settings versions after the race'
    assert versions[0].version_no == 1 and versions[0].reason == f'{tag} reason {index}'
    assert current is not None and current.settings == rates[index], "the source's rates are not the winner's"
    assert panels == [winner.id], 'a refused create left a panel row'
    assert panel_versions == [winner.id] and winner_v1.version_no == 1, 'a refused create left a settings version'


@pytest.mark.asyncio
async def test_enabled_panels_on_source_compiles_and_reads_the_latest_version_on_postgres(pg_async_engine, panel):
    """The correlated latest-version read runs on Postgres; a disable (a version) drops the panel from the set."""
    record, _, _ = panel

    async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
        on = {item.panel.id: item.current for item in await enabled_panels_on_source(db, SOURCE)}
        off = await append_settings_version(
            db,
            panel_id=record.id,
            settings=PanelSettings(enabled=False, size=10, cadence=PanelCadence.DAILY),
            actor='operator',
            reason='pause',
        )
        after = {item.panel.id for item in await enabled_panels_on_source(db, SOURCE)}

    assert record.id in on and on[record.id].version_no == 1
    assert off.version_no == 2 and record.id not in after


# --------------------------------------------------------------------------------------------- PS5 the route edge


def _store_path(symbol: str) -> str:
    return AssetDatasetStoreInterface.GET_STORE_ASSET_DATASET.format(
        asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value, asset_symbol=symbol
    )


def test_a_post_claiming_the_panel_owner_is_refused_and_writes_no_request(data_store, pg_engine, own_symbol):
    body = {'owner': PANEL_OWNER, 'source': 'ALPACA', 'granularity': '1day', 'start': '2026-01-02T00:00:00Z'}

    response = data_store.post(_store_path(own_symbol), json=body, auth='right')

    problem = assert_problem(
        response, status=422, reason='INVALID_REQUEST', title='Unprocessable Entity', describe=data_store.describe
    )
    assert problem['detail'] == REFUSAL
    with pg_engine.connect() as conn:
        rows = conn.execute(
            sa.select(sa.func.count()).select_from(REQUESTS).where(REQUESTS.c.asset_symbol == own_symbol.upper())
        ).scalar_one()
    assert rows == 0, 'a request row was written for a refused POST'
