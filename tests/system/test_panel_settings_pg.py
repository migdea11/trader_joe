"""The panel settings repository and the reserved panel owner (tj-7zjov1.176, P20b) on the real, migrated Postgres.

WRITTEN, NOT YET RUN where it was written (validator tj-7zjov1.178): it runs with the rest of the suite (make
test-system, or the agent-stack run_system_tests after test_up) on a stack migrated to the alpha baseline (ADR
tj-kg521d).

THE DESIGN: ADR tj-26jt9w addendum 20 and the P20b architect note of 03:25 UTC 2026-10-28. WHAT THIS PROVES that
server/data/store/tests/test_panel_settings.py cannot, because SQLite has one writer, no row locks, no advisory locks,
no arrays and no Postgres enums:
  PS1 version numbering of ONE PANEL is serialised: concurrent append_settings_version calls all succeed, numbered
      2..n+1 without a gap or a unique violation (the panel row is read FOR UPDATE before the max read);
  PS2 version numbering of ONE SOURCE is serialised by the transaction advisory lock: concurrent
      append_source_settings_version calls all succeed with consecutive numbers;
  PS3 the rows round-trip on asyncpg: the varchar[] filters, the panel_cadence and assettype enums, NULL filters,
      saved_at the DATABASE's clock (inside the transaction window read from now()), the window defaulted to 90; and a
      version with NO reason (ADR tj-46jkey) on both version tables: stored NULL, which the kept not-blank CHECKs pass
      on Postgres, and read back as None;
  PS4 (retired by tj-7zjov1.209/.210: effective-from has ONE definition, crud/panel_ledger.py boundary_read's, per
      panel and non-dry-run, pinned on Postgres in test_panel_ledger_pg.py; the two P20b functions are deleted);
  PS7 create_panel REFUSES source settings for a source that already has a version (INVALID_REQUEST) and writes
      nothing: no panel, no settings version, and the source's current rates unchanged;
  PS8 the guard is judged under the source lock (tj-7zjov1.212/.213): CONCURRENT creates giving source settings on a
      source with no version leave exactly one panel and ONE source version, the winner's; every other create is
      refused INVALID_REQUEST and writes nothing;
  PS9 (ADR tj-2ayia2 D4) the source lock on EVERY create: CONCURRENT creates with NO source settings on a source with
      no version all succeed and write ONE version 1, no limits (NULL, NULL) and 90/90;
  PS11 (ADR tj-2ayia2 D4) the MIXED race: a bare create and a create with source settings on a source with no
      version write ONE version 1, the first-served create's; the other is refused (settings given) or writes none;
  PS10 (ADR tj-2ayia2 D1/D2) the CHECKs ramp_positive, request_cap_min and window_non_negative on the real schema: a
      NULL rate, the minimums and int32 max pass; ramp 0, cap 0, cap 1 and a negative window are refused naming the
      constraint; a value above int32 is refused by the int4 column; a no-limit version round-trips NULL on asyncpg.
      WRITTEN, NOT YET RUN (validator tj-7zjov1.363.4): the test-stack window bead tj-7zjov1.363.7 runs them, on a
      test database dropped and migrated again (a database already at 5d9a3c7e1b84 keeps the old schema);
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
SETTINGS_V1 = PanelSettings(enabled=True, cadence=PanelCadence.DAILY)
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
                settings=PanelSettings(enabled=True, cadence=PanelCadence.WEEKLY, allow_symbols=(f'ZZPNL{index}',)),
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
                # In range (ADR tj-2ayia2: ramp >= 1, cap >= 2): index 0 would be a refused 0 now.
                db,
                source=SOURCE,
                settings=SourceSettings(index + 1, index + 2),
                actor='operator',
                reason=f'source {index}',
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


@pytest.mark.asyncio
async def test_a_version_with_no_reason_is_stored_null_on_postgres_and_a_blank_one_too(
    pg_async_engine, pg_engine, panel, written
):
    """ADR tj-46jkey on the real schema: the reason columns are NULLABLE and the not-blank CHECKs pass a NULL.

    The CHECKs are kept ('NULL or non-blank'), so a raw '' or '  ' is still refused by Postgres itself: the store's
    trim-to-NULL is the only way a blank reaches the table, and NULL is the one spelling of 'none'.
    """
    record, _, _ = panel
    async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
        version = await append_settings_version(
            db, panel_id=record.id, settings=SETTINGS_V1, actor='operator', reason=None
        )
    async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
        source_version = await append_source_settings_version(
            db, source=SOURCE, settings=RATES, actor='operator', reason='   '
        )
    written.source_versions.append(source_version.id)
    async with AsyncSession(pg_async_engine) as db:
        assert (await current_settings(db, record.id)).reason is None
    with pg_engine.connect() as conn:
        assert conn.execute(sa.select(SETTINGS.c.reason).where(SETTINGS.c.id == version.id)).scalar_one() is None
        assert (
            conn.execute(sa.select(SOURCE_SETTINGS.c.reason).where(SOURCE_SETTINGS.c.id == source_version.id))
        ).scalar_one() is None
    for blank in ('', '  '):
        with pg_engine.connect() as conn, pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.update(SETTINGS).where(SETTINGS.c.id == version.id).values(reason=blank)
            )  # a test-only write: the CHECK, not the module, refuses it


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
async def test_concurrent_creates_with_no_source_settings_on_a_source_with_no_version_write_one_version_1(
    pg_async_engine, written, run_identity
):
    """PS9 (ADR tj-2ayia2 D4): concurrent creates with nothing given on a source with no version write ONE version 1.

    A create on a source with NO version ALWAYS writes its version 1, even with nothing given, and takes the source
    lock on EVERY create. CONCURRENT such creates (distinct names) ALL succeed, and the source gets
    exactly ONE version: version 1, no limits (NULL, NULL), the window 90/90, with the first-served create's reason.

    The same gate as PS8: every create queues on the source lock before the gate lets go. With the lock taken only when
    source settings are given (the code before tj-2ayia2) none of these would queue, _await_lock_waiters fails, and the
    creates would race to write version 1 several times (a unique violation or several versions).
    """
    source = await _source_with_no_version(pg_async_engine)
    tag = f'system-test-{run_identity.run_id}-nolimit-{uuid.uuid4().hex[:8]}'

    async def create(index: int):
        async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
            return await create_panel(
                db,
                name=f'{tag}-{index}',
                source=source,
                settings=SETTINGS_V1,
                actor='operator',
                reason=f'{tag} reason {index}',
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

    failures = [result for result in results if isinstance(result, BaseException)]
    assert failures == [], [f'{type(f).__name__}: {f}' for f in failures]
    async with AsyncSession(pg_async_engine) as db:
        versions = (await db.execute(sa.select(*SOURCE_SETTINGS.c).where(SOURCE_SETTINGS.c.source == source))).all()
        panels = (await db.execute(sa.select(PANELS.c.id).where(PANELS.c.name.like(f'{tag}-%')))).scalars().all()
    assert len(versions) == 1, f'{source.name} holds {len(versions)} source settings versions after the race'
    (only,) = versions
    assert only.version_no == 1 and only.reason.startswith(f'{tag} reason ')
    assert (only.ramp_datasets_per_hour, only.request_cap_per_hour) == (None, None)
    assert (only.corporate_action_window_pre_days, only.corporate_action_window_post_days) == (90, 90)
    assert len(panels) == CONCURRENT


@pytest.mark.asyncio
async def test_a_mixed_race_bare_create_and_create_with_settings_writes_one_version_1(
    pg_async_engine, written, run_identity
):
    """PS11 (ADR tj-2ayia2 D4, the architect's note on tj-7zjov1.363.7): one create WITHOUT source settings and one WITH.

    Gated on the source lock as PS8/PS9. Exactly one version 1 exists after, never a version 2 or a unique violation.
    If the bare create ran first, version 1 is NULL/NULL/90/90 and the create with settings is INVALID_REQUEST 'already
    has settings' with nothing written (no panel, no settings row). If the create with settings ran first, version 1
    is its values and the bare create succeeds without writing a version. Which one runs first is the lock's queue
    order; both branches are asserted, whichever happens.
    """
    source = await _source_with_no_version(pg_async_engine)
    tag = f'system-test-{run_identity.run_id}-mixed-{uuid.uuid4().hex[:8]}'
    given = SourceSettings(ramp_datasets_per_hour=3, request_cap_per_hour=7, corporate_action_window_pre_days=11)

    async def create(kind: str):
        async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
            return await create_panel(
                db,
                name=f'{tag}-{kind}',
                source=source,
                settings=SETTINGS_V1,
                actor='operator',
                reason=f'{tag} reason {kind}',
                source_settings=given if kind == 'given' else None,
            )

    tasks: dict[str, asyncio.Task] = {}
    try:
        async with AsyncSession(pg_async_engine) as gate:
            holder = (await gate.execute(sa.select(sa.func.pg_backend_pid()))).scalar_one()
            await _lock_source(gate, source)
            tasks = {kind: asyncio.create_task(create(kind)) for kind in ('bare', 'given')}
            try:
                await _await_lock_waiters(pg_async_engine, holder, 2)
            finally:
                await gate.rollback()
    finally:
        results = dict(zip(tasks, await asyncio.gather(*tasks.values(), return_exceptions=True), strict=True))
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

    assert not isinstance(results['bare'], BaseException), f'the bare create failed: {results["bare"]!r}'
    async with AsyncSession(pg_async_engine) as db:
        versions = (await db.execute(sa.select(*SOURCE_SETTINGS.c).where(SOURCE_SETTINGS.c.source == source))).all()
        panels = (await db.execute(sa.select(PANELS.c.name).where(PANELS.c.name.like(f'{tag}-%')))).scalars().all()
        settings_rows = (
            (await db.execute(sa.select(SETTINGS.c.reason).where(SETTINGS.c.reason.like(f'{tag} reason %'))))
            .scalars()
            .all()
        )
    assert len(versions) == 1, f'{source.name} holds {len(versions)} source settings versions after the mixed race'
    (only,) = versions
    assert only.version_no == 1
    numbers = (
        only.ramp_datasets_per_hour,
        only.request_cap_per_hour,
        only.corporate_action_window_pre_days,
        only.corporate_action_window_post_days,
    )
    if only.reason == f'{tag} reason bare':
        # The bare create ran first: no limits; the create with settings is refused and wrote nothing.
        assert numbers == (None, None, 90, 90)
        refused = results['given']
        assert isinstance(refused, InvalidRequestError), f'expected the store refusal, got {refused!r}'
        assert refused.reason is Reason.INVALID_REQUEST and 'already has settings' in refused.detail, refused.detail
        assert panels == [f'{tag}-bare'] and settings_rows == [f'{tag} reason bare']
    else:
        # The create with settings ran first: its values; the bare create succeeded and wrote no version.
        assert only.reason == f'{tag} reason given', only.reason
        assert not isinstance(results['given'], BaseException), repr(results['given'])
        assert numbers == (3, 7, 11, 90)
        assert sorted(panels) == [f'{tag}-bare', f'{tag}-given']
        assert sorted(settings_rows) == [f'{tag} reason bare', f'{tag} reason given']


# --------------------------------------------------------------------------------------------- PS10 the rate CHECKs

RAMP_CHECK = PanelSourceSettingsVersion.RAMP_POSITIVE
CAP_CHECK = PanelSourceSettingsVersion.REQUEST_CAP_MIN
WINDOW_CHECK = PanelSourceSettingsVersion.WINDOW_NON_NEGATIVE


def _raw_source_row(**rates) -> dict:
    """A row written BEHIND the crud (test-only): the CHECK, not the module, is what is under test."""
    return {
        'source': SOURCE,
        'version_no': 1_000_000 + int(uuid.uuid4().int % 1_000_000),
        'ramp_datasets_per_hour': None,
        'request_cap_per_hour': None,
        'corporate_action_window_pre_days': 0,
        'corporate_action_window_post_days': 0,
        'actor': 'operator',
        'reason': 'system test raw check row',
    } | rates


@pytest.mark.parametrize(
    'rates',
    [
        {},
        {'ramp_datasets_per_hour': 1, 'request_cap_per_hour': 2},
        {'ramp_datasets_per_hour': 2147483647, 'request_cap_per_hour': 2147483647},
        {'ramp_datasets_per_hour': None, 'request_cap_per_hour': 2},
        {'ramp_datasets_per_hour': 1, 'request_cap_per_hour': None},
    ],
    ids=['both-null', 'the-minimums', 'int32-max', 'ramp-null', 'cap-null'],
)
def test_the_rate_checks_pass_a_null_and_the_minimums_on_postgres(pg_engine, written, rates):
    with pg_engine.begin() as conn:
        row_id = conn.execute(
            sa.insert(SOURCE_SETTINGS).values(**_raw_source_row(**rates)).returning(SOURCE_SETTINGS.c.id)
        ).scalar_one()
    written.source_versions.append(row_id)
    with pg_engine.connect() as conn:
        stored = conn.execute(sa.select(SOURCE_SETTINGS).where(SOURCE_SETTINGS.c.id == row_id)).one()
    expected = _raw_source_row(**rates)
    assert (stored.ramp_datasets_per_hour, stored.request_cap_per_hour) == (
        expected['ramp_datasets_per_hour'],
        expected['request_cap_per_hour'],
    )


@pytest.mark.parametrize(
    ('rates', 'constraint'),
    [
        ({'ramp_datasets_per_hour': 0}, RAMP_CHECK),
        ({'ramp_datasets_per_hour': -1}, RAMP_CHECK),
        ({'request_cap_per_hour': 0}, CAP_CHECK),
        ({'request_cap_per_hour': 1}, CAP_CHECK),
        ({'corporate_action_window_pre_days': -1}, WINDOW_CHECK),
        ({'corporate_action_window_post_days': -1}, WINDOW_CHECK),
    ],
    ids=['ramp-zero', 'ramp-negative', 'cap-zero', 'cap-one', 'pre-negative', 'post-negative'],
)
def test_the_rate_checks_refuse_zero_a_cap_of_one_and_name_the_constraint_on_postgres(
    pg_engine, written, rates, constraint
):
    """Each out-of-range value is refused by its own CHECK, named in Postgres's error text.

    The text: 'new row for relation "panel_source_settings_version" violates check constraint "<name>"'. Pinned by
    name so a renamed or merged CHECK is red.
    """
    with pg_engine.connect() as conn, pytest.raises(sa.exc.IntegrityError) as excinfo:
        conn.execute(sa.insert(SOURCE_SETTINGS).values(**_raw_source_row(**rates)))
    text = str(excinfo.value.orig)
    assert 'violates check constraint' in text and f'"{constraint}"' in text, text
    assert '"panel_source_settings_version"' in text, text
    with pg_engine.connect() as conn:
        leaked = conn.execute(
            sa.select(sa.func.count())
            .select_from(SOURCE_SETTINGS)
            .where(SOURCE_SETTINGS.c.reason == 'system test raw check row', SOURCE_SETTINGS.c.version_no >= 1_000_000)
        ).scalar_one()
    assert leaked == 0


def test_a_rate_above_int32_is_refused_by_the_int4_column_on_postgres(pg_engine):
    """No CHECK for the upper bound (D2): the int4 column is it."""
    with pg_engine.connect() as conn, pytest.raises(sa.exc.DBAPIError) as excinfo:
        conn.execute(sa.insert(SOURCE_SETTINGS).values(**_raw_source_row(request_cap_per_hour=2147483648)))
    assert 'out of range' in str(excinfo.value.orig).lower(), excinfo.value.orig


@pytest.mark.asyncio
async def test_a_no_limit_source_version_round_trips_null_on_asyncpg(pg_async_engine, pg_engine, written):
    """append_source_settings_version with None rates writes NULL (never 0) and current_source_settings reads None."""
    async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
        record = await append_source_settings_version(
            db, source=SOURCE, settings=SourceSettings(None, None), actor='operator', reason='system test no limit'
        )
    written.source_versions.append(record.id)
    with pg_engine.connect() as conn:
        stored = conn.execute(sa.select(SOURCE_SETTINGS).where(SOURCE_SETTINGS.c.id == record.id)).one()
    assert stored.ramp_datasets_per_hour is None and stored.request_cap_per_hour is None
    async with AsyncSession(pg_async_engine) as db:
        current = await current_source_settings(db, SOURCE)
    assert current is not None and current.id == record.id
    assert current.settings == SourceSettings(None, None)


@pytest.mark.asyncio
async def test_enabled_panels_on_source_compiles_and_reads_the_latest_version_on_postgres(pg_async_engine, panel):
    """The correlated latest-version read runs on Postgres; a disable (a version) drops the panel from the set."""
    record, _, _ = panel

    async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
        on = {item.panel.id: item.current for item in await enabled_panels_on_source(db, SOURCE)}
        off = await append_settings_version(
            db,
            panel_id=record.id,
            settings=PanelSettings(enabled=False, cadence=PanelCadence.DAILY),
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
