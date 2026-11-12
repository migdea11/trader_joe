"""The system suite keeps the stack's live ingest instance live (tj-7zjov1.233): the restore, and the guard on its use.

THE DEFECT, from the architect's diagnosis on tj-7zjov1.233. System modules sight minted instance ids against the
SHARED stack database; a first sighting (note_instance_seen) supersedes every other instance, the real ingest
included, and marks the requests it holds NOT_DELIVERED. The modules' cleanups delete only their minted ids, so every
later POST of the session ran against a superseded instance (mark_queued refused, a transient FAILED was answered
FINAL) and test_ingest_e2e's FAIL_ / FAILONCE_ POSTs outlived the suite's 30 s HTTP timeout.

WHAT THIS FILE PINS, without Postgres (the system tier runs the fixture itself on Postgres):
  * the logic in tests/system/live_instances.py, driven through the REAL note_instance_seen and mark_queued over
    SQLite (aiosqlite), as server/data/store/tests/test_ingest_request_crud.py drives them: the hazard reproduces,
    the restore undoes it whether it runs before or after the module's cleanup, leaves what the test did not cause
    alone, and follows a real ingest restart instead of undoing it;
  * THE GUARD: every tests/system module that can sight an instance applies the live_instances_kept fixture to all
    of its tests through its module pytestmark, or is exempt with a reason. The scan is static (ast), so it needs no
    stack and runs in the PR gate.
"""

import ast
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import DATETIME
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from common.enums.data_stock import DataSource
from data.store.app.database.crud import ingest_instance as instance_crud
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestKind, IngestRequestState
from data.store.tests.request_tables import create_request_tables
from tests.system.live_instances import LiveInstances, Restored, note_live_instances, restore_live_instances


pytestmark = pytest.mark.data_store

INSTANCES = IngestInstance.__table__
REQUESTS = IngestRequest.__table__
RUN_ID = 'a1b2c3d4'
LIVE = 'ingest-live-0f3e'
OLD = 'ingest-old-77aa'
T0 = datetime(2026, 11, 1, 12, tzinfo=UTC)

SYSTEM_DIR = Path(__file__).resolve().parents[4] / 'tests' / 'system'
FIXTURE = 'live_instances_kept'
# The names through which a test reaches a sighting of an instance id: the intake's RegisterInstance and every
# delivery (its header), the crud itself, a Submit answer noted by submit_record (fake Submit and Instance answers),
# and the watcher's probe.
SIGHTING_NAMES = frozenset(
    {
        'register_instance',
        'note_instance_seen',
        'DeliveryIntake',
        'InstanceWatcher',
        'submit_record',
        'submit_bars',
        'store_market_activity_worker',
        'SubmitResponse',
        'InstanceResponse',
    }
)
# A module that can sight an instance but never on the SHARED database. Each entry says why.
EXEMPT = {
    'test_daily_refresh_pass.py': 'every session it opens is on the scratch database, never on the stack database'
}


def _minted(name: str) -> str:
    return f'system-test-{RUN_ID}-{name}-{uuid.uuid4().hex[:6]}'


# --------------------------------------------------------------------------------------------- the SQLite tier


@pytest.fixture
def aware_sqlite_datetimes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read a timezone=True column back UTC-aware, as asyncpg reads timestamptz."""
    original = DATETIME.result_processor

    def result_processor(self, dialect, coltype):
        process = original(self, dialect, coltype)
        if not self.timezone:
            return process

        def aware(value):
            value = process(value) if process else value
            return None if value is None else value.replace(tzinfo=UTC)

        return aware

    monkeypatch.setattr(DATETIME, 'result_processor', result_processor)


@pytest_asyncio.fixture
async def engine(aware_sqlite_datetimes) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(create_request_tables)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _sync(engine: AsyncEngine, work: Callable[[Connection], object]):
    async with engine.begin() as connection:
        return await connection.run_sync(work)


async def _sight(engine: AsyncEngine, instance_id: str, first_seen_at: datetime) -> instance_crud.InstanceSighting:
    """A sighting through the store's own crud, then a first_seen_at that orders it (SQLite stamps whole seconds)."""
    async with AsyncSession(engine) as db:
        sighting = await instance_crud.note_instance_seen(db, instance_id)
    await _sync(
        engine,
        lambda c: c.execute(
            sa.update(INSTANCES).where(INSTANCES.c.instance_id == instance_id).values(first_seen_at=first_seen_at)
        ),
    )
    return sighting


async def _request(engine: AsyncEngine, state: IngestRequestState, holder: str | None) -> uuid.UUID:
    request_id = uuid.uuid4()
    await _sync(
        engine,
        lambda c: c.execute(
            sa.insert(REQUESTS).values(
                id=request_id,
                kind=IngestRequestKind.BARS,
                source=DataSource.ALPACA_API,
                client_label='live-instances-test',
                state=state,
                instance_id=holder,
            )
        ),
    )
    return request_id


async def _delete_instances(engine: AsyncEngine, *instance_ids: str) -> None:
    """What every minting module's cleanup does: delete its own ids, nothing else."""
    await _sync(engine, lambda c: c.execute(sa.delete(INSTANCES).where(INSTANCES.c.instance_id.in_(instance_ids))))


async def _superseded(engine: AsyncEngine) -> dict[str, bool]:
    rows = await _sync(engine, lambda c: c.execute(sa.select(INSTANCES)).all())
    return {row.instance_id: row.superseded_at is not None for row in rows}


async def _state(engine: AsyncEngine, request_id: uuid.UUID) -> IngestRequestState:
    return await _sync(
        engine, lambda c: c.execute(sa.select(REQUESTS.c.state).where(REQUESTS.c.id == request_id)).scalar_one()
    )


async def _stack(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """The stack before a test: OLD superseded by LIVE; a QUEUED request under each and a PENDING one."""
    await _sight(engine, OLD, T0)
    await _sight(engine, LIVE, T0 + timedelta(minutes=1))
    queued_live = await _request(engine, IngestRequestState.QUEUED, LIVE)
    queued_old = await _request(engine, IngestRequestState.QUEUED, OLD)
    pending = await _request(engine, IngestRequestState.PENDING, None)
    return queued_live, queued_old, pending


async def _restore(engine: AsyncEngine, before: LiveInstances) -> Restored:
    return await _sync(engine, lambda c: restore_live_instances(c, before, minted_marker=RUN_ID))


@pytest.mark.asyncio
async def test_a_minted_sighting_supersedes_the_live_instance_and_the_restore_undoes_it(engine):
    queued_live, queued_old, pending = await _stack(engine)
    before = await _sync(engine, note_live_instances)
    assert before == LiveInstances(known=frozenset({OLD, LIVE}), live=frozenset({LIVE}), queued=((queued_live, LIVE),))

    minted = _minted('pg8')
    sighting = await _sight(engine, minted, T0 + timedelta(minutes=2))
    # THE HAZARD, through the store's own code: the stack's live ingest is superseded, its request not delivered,
    # and a POST answered by it is never queued (mark_queued refuses a superseded instance).
    assert sighting.superseded == (LIVE,) and queued_live in sighting.not_delivered
    async with AsyncSession(engine) as db:
        assert await request_crud.mark_queued(db, pending, LIVE) is None
    await _delete_instances(engine, minted)

    restored = await _restore(engine, before)

    assert restored == Restored(instances=(LIVE,), requests=(queued_live,))
    assert await _superseded(engine) == {OLD: True, LIVE: False}
    assert await _state(engine, queued_live) is IngestRequestState.QUEUED
    # QUEUED under an instance that was already superseded is the watcher's (it re-submits both alike): left as marked.
    assert await _state(engine, queued_old) is IngestRequestState.NOT_DELIVERED
    async with AsyncSession(engine) as db:
        assert await instance_crud.live_instance_id(db) == LIVE
        queued = await request_crud.mark_queued(db, pending, LIVE)
    assert queued is not None and queued.state is IngestRequestState.QUEUED


@pytest.mark.asyncio
async def test_the_restore_holds_when_it_runs_before_the_modules_cleanup(engine):
    """Fixture order is not load-bearing: a minted id still present is never read as a real ingest."""
    queued_live, _, _ = await _stack(engine)
    before = await _sync(engine, note_live_instances)
    old_minted, new_minted = _minted('old'), _minted('new')
    await _sight(engine, old_minted, T0 + timedelta(minutes=2))
    await _sight(engine, new_minted, T0 + timedelta(minutes=3))

    restored = await _restore(engine, before)

    assert restored == Restored(instances=(LIVE,), requests=(queued_live,))
    assert (await _superseded(engine))[LIVE] is False
    await _delete_instances(engine, old_minted, new_minted)
    assert await _superseded(engine) == {OLD: True, LIVE: False}


@pytest.mark.asyncio
async def test_a_test_that_sights_nothing_restores_nothing(engine):
    queued_live, _, _ = await _stack(engine)
    before = await _sync(engine, note_live_instances)
    # A sighting of the known live id only moves last_seen_at.
    await _sight(engine, LIVE, T0 + timedelta(minutes=1))

    assert await _restore(engine, before) == Restored()
    assert await _superseded(engine) == {OLD: True, LIVE: False}
    assert await _state(engine, queued_live) is IngestRequestState.QUEUED


@pytest.mark.asyncio
async def test_a_request_that_moved_on_during_the_test_is_left_as_it_is(engine):
    """Only a request still NOT_DELIVERED under the same holder is put back: never one finished or taken over since."""
    await _sight(engine, LIVE, T0)
    finished = await _request(engine, IngestRequestState.QUEUED, LIVE)
    taken_over = await _request(engine, IngestRequestState.QUEUED, LIVE)
    before = await _sync(engine, note_live_instances)
    minted = _minted('moved')
    await _sight(engine, minted, T0 + timedelta(minutes=1))
    await _sync(
        engine,
        lambda c: (
            c.execute(sa.update(REQUESTS).where(REQUESTS.c.id == finished).values(state=IngestRequestState.SUCCEEDED)),
            c.execute(
                sa.update(REQUESTS)
                .where(REQUESTS.c.id == taken_over)
                .values(state=IngestRequestState.NOT_DELIVERED, instance_id=minted)
            ),
        ),
    )
    await _delete_instances(engine, minted)

    assert await _restore(engine, before) == Restored(instances=(LIVE,), requests=())
    assert await _state(engine, finished) is IngestRequestState.SUCCEEDED
    assert await _state(engine, taken_over) is IngestRequestState.NOT_DELIVERED


@pytest.mark.asyncio
async def test_a_real_ingest_restart_during_the_test_is_kept_and_only_the_new_process_made_live(engine):
    """An arrival without the run's marker is a real process: the old live stays superseded, its request not delivered.

    The minted id sighted after the restart superseded the new process too; the restore makes that one live again.
    """
    queued_live, _, _ = await _stack(engine)
    before = await _sync(engine, note_live_instances)
    first_minted, later_minted, restarted = _minted('a'), _minted('b'), 'ingest-restarted-5c1d'
    await _sight(engine, first_minted, T0 + timedelta(minutes=2))
    await _sight(engine, restarted, T0 + timedelta(minutes=3))
    await _sight(engine, later_minted, T0 + timedelta(minutes=4))
    await _delete_instances(engine, first_minted, later_minted)

    restored = await _restore(engine, before)

    assert restored == Restored(instances=(restarted,), requests=())
    assert await _superseded(engine) == {OLD: True, LIVE: True, restarted: False}
    assert await _state(engine, queued_live) is IngestRequestState.NOT_DELIVERED
    async with AsyncSession(engine) as db:
        assert await instance_crud.live_instance_id(db) == restarted


@pytest.mark.asyncio
async def test_an_empty_marker_is_refused(engine):
    before = await _sync(engine, note_live_instances)
    with pytest.raises(ValueError, match='minted_marker'):
        await _sync(engine, lambda c: restore_live_instances(c, before, minted_marker=''))


# --------------------------------------------------------------------------------------------- the guard


def _names(tree: ast.Module) -> set[str]:
    """Every name and attribute the module mentions, imports included."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.rsplit('.', 1)[-1])
            if node.asname:
                found.add(node.asname)
    return found


def _module_uses_fixture(tree: ast.Module, fixture: str) -> bool:
    """Whether the module-level pytestmark carries pytest.mark.usefixtures naming `fixture`, so every test gets it."""
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'pytestmark' for t in node.targets)):
            continue
        for call in ast.walk(node.value):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == 'usefixtures'
                and any(isinstance(arg, ast.Constant) and arg.value == fixture for arg in call.args)
            ):
                return True
    return False


def _sighting_modules() -> dict[str, ast.Module]:
    modules = {}
    for path in sorted(SYSTEM_DIR.glob('test_*.py')):
        tree = ast.parse(path.read_text(), filename=str(path))
        if _names(tree) & SIGHTING_NAMES:
            modules[path.name] = tree
    return modules


def test_every_system_module_that_can_sight_an_instance_keeps_the_live_one():
    modules = _sighting_modules()
    # The guard has teeth only while it finds the modules it exists for.
    assert {'test_delivery_intake_pg.py', 'test_resubmit_races_pg.py', 'test_asset_list_pg.py'} <= set(modules)
    missing = sorted(
        name for name, tree in modules.items() if name not in EXEMPT and not _module_uses_fixture(tree, FIXTURE)
    )
    assert missing == [], (
        f'these tests/system modules can sight an instance id on the shared stack database and do not apply '
        f"{FIXTURE} to every test (pytestmark = [..., pytest.mark.usefixtures('{FIXTURE}')]): {missing}"
    )


def test_every_exemption_is_still_a_module_that_can_sight_an_instance():
    """An exemption whose module is gone or no longer sights anything is stale, and would hide a future one."""
    assert sorted(set(EXEMPT) - set(_sighting_modules())) == []


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        (f"pytestmark = [pytest.mark.data_store, pytest.mark.usefixtures('{FIXTURE}')]", True),
        (f"pytestmark = pytest.mark.usefixtures('other', '{FIXTURE}')", True),
        ('pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]', False),
        (f"pytestmark = pytest.mark.usefixtures('other')\nX = pytest.mark.usefixtures('{FIXTURE}')", False),
        (f"@pytest.mark.usefixtures('{FIXTURE}')\ndef test_one(): pass", False),
    ],
)
def test_the_guard_reads_only_a_module_wide_usefixtures(source, expected):
    """A mark on one test, or on a name other than pytestmark, leaves the module's other tests unguarded."""
    assert _module_uses_fixture(ast.parse(source), FIXTURE) is expected
