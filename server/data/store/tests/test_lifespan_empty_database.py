"""data_store's REAL lifespan against a database with NO tables at all (tj-7zjov1.376).

DESIGN: tj-7zjov1.375's acceptance, "data_store's lifespan reaches ready on a database with no tables". CI brings the
stack up THEN migrates, so every statement data_store awaits at start, and every loop it starts, meets an empty
database first. AssetSnapshotSchedule.seed() was the statement that took the process down (exit 3); the sweep
(tj-s6r3rt.5), the daily refresh (_tables_exist) and the usage loops guard theirs.

WHAT RUNS FOR REAL. app_depends.lifespan as written, top to bottom and back: the gRPC host (real StoreService and
delivery intake, bound on loopback, an ephemeral port), the sweep, the usage loops, the reaper, the DAILY refresh, the
asset snapshot schedule (seed and daily loop), the instance watcher (its probe answers through FakeSubmit, so its pass
goes on to note the instance and call the snapshot hook), and the panel manager loop (the conftest's inert stand-in
is put back to the real one here). database.own_session is the real one: the session factory is handed one empty
in-memory SQLite engine in place of asyncpg, so every module's own_session reaches the same empty database.

WHAT IS FAKED, none of it a database access: the ingest channel and Submit client (FakeSubmit), the latency client,
the debugger, the process-wide startup and teardown logs, the shared worker pool.

THE EVIDENCE. A handle_error listener on the engine records EVERY statement that raised, with its exception and the
asyncio task that ran it: the lifespan's own task, or a loop's (run_periodic names it periodic:<name>). Two tiers:
  * the LIFESPAN tier (must pass): the lifespan yields, logs ready, and nothing it awaits itself raises. A statement
    that raises there exits the process (the exit 3 of tj-7zjov1.375);
  * the LOOP tier, one case per loop: its first pass (every loop fires at once at start) raises no statement. A
    failure there does not stop the process: run_periodic logs it at ERROR by exception type and the next pass runs.
    The loops that do raise today are marked xfail(strict=True), naming the statement, so a fix turns the case XPASS
    (red) and the mark must come off, and a NEW loop that raises fails its own unmarked case.

ONE LIFESPAN RUN PER MODULE (per xdist worker). Every case reads the same run, made by whichever case runs first: the
run takes about three seconds and is what every case is about.

WHAT IT CANNOT SHOW. The to_regclass probes run on the conftest's stand-in (the name when the table exists, NULL when
not), not on Postgres. A statement that is Postgres-only for another reason (an advisory lock, JSONB) would fail here
for that reason and not for the missing table, and a guard that catches asyncpg's UndefinedTable by type would not
see SQLite's OperationalError. Every failing statement is named in the failure message, so either case shows; none
of the ones recorded today is of that kind (each is "no such table"). The real proof is CI's System Start and
Migration Gate against an empty Postgres.
"""

import asyncio
import logging
from dataclasses import dataclass, field

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from common.database.postgres_tools import PostgresSessionFactory
from common.rpc.server import BindAddress, GrpcServerHost
from data.store.app import app_depends, panel_manager
from data.store.app.database import database
from data.store.app.grpc_host import registered_services
from data.store.app.ingest.delivery_intake import DeliveryIntake
from data.store.tests.request_path_harness import INSTANCE_A, FakeSubmit


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

LOOPBACK = '127.0.0.1'
READY = 'Data Store App Ready!!!'
PERIODIC = 'periodic:'
SEED_SKIPPED = 'Asset snapshot seed skipped: the ingest_instance table does not exist'

# Every loop the lifespan starts, by its run_periodic name. The lifespan test checks this is the whole set.
LOOPS = (
    'usage-flush',
    'usage-rollup',
    'expiry-reaper',
    'daily-refresh-ALPACA',
    'asset-snapshot-ALPACA',
    'instance-watch',
    'panel-manager',
)


def _unguarded(statement: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(
        strict=True, reason=f'CHANGES NEEDED, builder-store (tj-7zjov1.376 verdict): first pass raises on {statement}'
    )


LOOP_CASES = [
    pytest.param('usage-flush'),
    pytest.param('usage-rollup'),
    pytest.param(
        'expiry-reaper',
        marks=_unguarded('SELECT store_dataset_entry pending ids (reaper pass, dry-run); also on origin/main'),
    ),
    pytest.param('daily-refresh-ALPACA'),
    pytest.param(
        'asset-snapshot-ALPACA',
        marks=_unguarded('SELECT asset_list_snapshot (run_daily newest_snapshot, the start-up catch-up)'),
    ),
    pytest.param(
        'instance-watch',
        marks=_unguarded('INSERT ingest_instance (note_instance_seen) and SELECT ingest_request (PENDING re-send)'),
    ),
    pytest.param('panel-manager', marks=_unguarded('SELECT panel (the tick reading the enabled panels)')),
]


@dataclass
class LifespanRun:
    """What one run of the real lifespan on an empty database showed."""

    tables_at_start: list[str] = field(default_factory=list)
    reached_ready: bool = False
    probes: int = 0
    # (task name, "Exception: message -- statement") for every statement that raised.
    failures: list[tuple[str, str]] = field(default_factory=list)
    # task names that executed any statement.
    executed: set[str] = field(default_factory=set)
    loops_started: set[str] = field(default_factory=set)
    # (logger, level, message) of every record.
    records: list[tuple[str, int, str]] = field(default_factory=list)

    def failures_of(self, task: str) -> list[str]:
        return [failure for name, failure in self.failures if name == task]

    def errors_of(self, loop: str) -> list[str]:
        return [m for _, level, m in self.records if level >= logging.ERROR and f'name={loop} ' in m]


_RUN: LifespanRun | None = None


def _task_name() -> str:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return '<no loop>'
    return '<no task>' if task is None else task.get_name()


def _fake_the_non_database_startup(monkeypatch: pytest.MonkeyPatch, client: FakeSubmit) -> None:
    class FakeChannel:
        async def close(self) -> None:
            return None

    class FakePool:
        @staticmethod
        def worker_startup() -> None: ...

        @staticmethod
        def worker_shutdown() -> None: ...

    monkeypatch.setattr(app_depends, 'initialize_latency_client', lambda *a, **k: None)
    monkeypatch.setattr(app_depends, 'init_debugger', lambda: None)
    monkeypatch.setattr(app_depends, 'startup_logs', lambda _app: None)
    monkeypatch.setattr(app_depends, 'teardown_logs', lambda _app: None)
    monkeypatch.setattr(app_depends, 'SharedWorkerPool', FakePool)
    monkeypatch.setattr(app_depends, 'target_from_env', lambda _name: 'ingest:1')
    monkeypatch.setattr(app_depends, 'create_channel', lambda _target: FakeChannel())
    monkeypatch.setattr(app_depends, 'GrpcIngestSubmitClient', lambda _channel: client)
    # The REAL host and intake, on an ephemeral loopback port (the env reader refuses 0, the host does not).
    monkeypatch.setattr(
        app_depends,
        'build_grpc_host',
        lambda: GrpcServerHost(BindAddress(LOOPBACK, 0), registered_services(DeliveryIntake())),
    )
    # The REAL panel manager loop, which the conftest replaces for every other lifespan test.
    monkeypatch.setattr(app_depends, 'start_panel_manager_loop', panel_manager.start_panel_manager_loop)
    monkeypatch.setattr(app_depends, 'stop_panel_manager_loop', panel_manager.stop_panel_manager_loop)
    # Every loop on, at its defaults, whatever the environment says: each is a start-up path to the empty database.
    for name in (
        'DAILY_REFRESH_ENABLED',
        'ASSET_SNAPSHOT_ENABLED',
        'INSTANCE_WATCH_ENABLED',
        'PANEL_MANAGER_ENABLED',
        'INSTANCE_WATCH_DRY_RUN',
        'PANEL_MANAGER_DRY_RUN',
        'REAPER_DELETES_ENABLED',
    ):
        monkeypatch.delenv(name, raising=False)


async def _settle(run: LifespanRun, client: FakeSubmit, *, quiet: float = 1.0, limit: float = 30.0) -> None:
    """Wait for the watcher's first probe, then until no new statement has run for `quiet` seconds.

    Every loop fires at once (some after building calendars off the event loop), so a quiet second after the probe
    means each has had its first pass.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    seen, quiet_since = None, loop.time()
    while loop.time() < deadline:
        await asyncio.sleep(0.05)
        run.loops_started |= {
            t.get_name().removeprefix(PERIODIC) for t in asyncio.all_tasks() if t.get_name().startswith(PERIODIC)
        }
        # The quiet clock starts only once the probe has run, and restarts on every new statement.
        now = (client.probes >= 1, len(run.failures), len(run.executed))
        if now != seen or not now[0]:
            seen, quiet_since = now, loop.time()
        elif loop.time() - quiet_since >= quiet:
            return


async def _run_the_lifespan(monkeypatch: pytest.MonkeyPatch) -> LifespanRun:
    run = LifespanRun()
    engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, 'handle_error')
    def journal_failure(context) -> None:
        statement = ' '.join((context.statement or '').split())
        error = context.original_exception
        run.failures.append((_task_name(), f'{type(error).__name__}: {error} -- {statement}'))

    @event.listens_for(engine.sync_engine, 'before_cursor_execute')
    def journal_statement(*_args) -> None:
        run.executed.add(_task_name())

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            run.records.append((record.name, record.levelno, record.getMessage()))

    handle = PostgresSessionFactory.AsyncSessionHandle
    db_hash = PostgresSessionFactory._get_db_hash(database.DATABASE_ASYNC_URI)

    async def initialize() -> None:
        # What the real initialize does, minus asyncpg: register the engine and its sessionmaker under the URI.
        assert db_hash not in handle._async_engines
        handle._async_engines[db_hash] = engine
        handle._async_session_makers[db_hash] = async_sessionmaker(engine, autocommit=False, autoflush=False)

    monkeypatch.setattr(app_depends.database, 'initialize', initialize)
    client = FakeSubmit(INSTANCE_A)  # ingest answers the watcher's probe, so the snapshot hook runs too
    _fake_the_non_database_startup(monkeypatch, client)

    async with engine.connect() as connection:
        rows = (await connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")).all()
    run.tables_at_start = [row[0] for row in rows]

    root = logging.getLogger()
    collect, level = Collect(logging.DEBUG), root.level
    root.addHandler(collect)
    root.setLevel(logging.DEBUG)
    try:
        async with app_depends.lifespan(object()):
            run.reached_ready = True
            await _settle(run, client)
    finally:
        root.removeHandler(collect)
        root.setLevel(level)
        handle._async_engines.pop(db_hash, None)
        handle._async_session_makers.pop(db_hash, None)
        await engine.dispose()
    run.probes = client.probes
    return run


async def _the_run(monkeypatch: pytest.MonkeyPatch) -> LifespanRun:
    global _RUN
    if _RUN is None:
        _RUN = await _run_the_lifespan(monkeypatch)
    return _RUN


async def test_the_real_lifespan_reaches_ready_on_a_database_with_no_tables(monkeypatch):
    run = await _the_run(monkeypatch)

    assert run.tables_at_start == [], 'the database must start with no tables at all'
    assert run.reached_ready, 'the lifespan must yield on an empty database'
    assert any(message == READY for _, _, message in run.records)
    lifespan_failures = [f for name, f in run.failures if not name.startswith(PERIODIC)]
    assert lifespan_failures == [], 'statements the lifespan awaited that raised:\n' + '\n'.join(lifespan_failures)
    seed = [m for _, level, m in run.records if level == logging.WARNING and m.startswith(SEED_SKIPPED)]
    assert len(seed) == 1 and 'make migrate' in seed[0], 'the seed skipped once, naming make migrate'
    assert run.probes >= 1, "the instance watcher's first pass ran"
    assert run.loops_started == set(LOOPS), 'LOOPS must name every loop the lifespan starts'


@pytest.mark.parametrize('loop', LOOP_CASES)
async def test_no_loops_first_pass_raises_on_a_database_with_no_tables(monkeypatch, loop):
    run = await _the_run(monkeypatch)

    assert loop in run.loops_started
    failures = run.failures_of(PERIODIC + loop)
    assert failures == [], f'{loop}: statements that raised on an empty database:\n' + '\n'.join(failures)
    assert run.errors_of(loop) == []
