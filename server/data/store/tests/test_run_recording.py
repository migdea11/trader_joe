"""The run recorder and its crud: one ingest_run row around every store-driven fetch (tj-grna9p.18, tj-grna9p.73).

The design is tj-grna9p.18's architect re-baseline of 2026-10-11 (items 1, 2, 4, 5, 6) and tj-grna9p.73's item 4:

* the RUNNING row is COMMITTED before the fetch body runs, on the recorder's OWN session, never the fetch's;
* the ack sets dataset_id (and the resolved feed) as a separate committed UPDATE;
* FetchDone with bars is SUCCEEDED, with none EMPTY; a TraderJoeError is FAILED with its domain, reason, detail
  and error_id; any other exception is FAILED with a NULL reason and a FIXED message, never str() of it (D8);
  a cancellation is finished inside asyncio.shield; and in every case the exception propagates UNCHANGED (D6);
* a FAILED run whose ack named an entry the rolled-back fetch never committed reverts to a NULL dataset_id;
* the startup sweep turns every RUNNING row FAILED with error_reason INTERRUPTED (a run-record marker, not a
  Reason member), a NULL domain, a fixed message and finished_at set -- and touches nothing else;
* the record is BEST EFFORT: a recording failure is logged and the fetch goes on;
* each committed start, ack and finish is published as IngestRunChanged, strictly after its commit;
* the actor (the body's owner) is never logged.

TIER. The crud's real SQL over SQLite, through an engine the recorder's own_session is pointed at. SQLite has one
writer, so a fetch transaction held OPEN beside a recorder write cannot be shown here: that half -- the RUNNING row
visible from a second session while the fetch is in flight, and the fetch's rollback leaving the run -- is
tests/system/test_http_ingest_runs.py's, over Postgres.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import DATETIME
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import ERROR_DOMAIN, ExogenousError, Reason
from data.store.app.database.crud import ingest_run as crud
from data.store.app.database.models.ingest_run import INTERRUPTED_REASON, IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.events import IngestRunChanged
from data.store.app.ingest import run_recording
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath


pytestmark = pytest.mark.data_store

OWNER = 'owner-sentinel-7c3d1a'
START = datetime(2026, 1, 2, tzinfo=UTC)
PATH = StoreAssetDatasetPath(asset_type='stock', data_type='market-activity', asset_symbol='VFV')
BODY = StoreAssetDatasetBody(
    owner=OWNER, source='ALPACA', granularity='1day', start=START, end=datetime(2026, 2, 2, tzinfo=UTC)
)


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
        await connection.run_sync(
            lambda sync: IngestRun.metadata.create_all(sync, tables=[IngestRun.__table__, StoreDatasetEntry.__table__])
        )
    try:
        yield engine
    finally:
        await engine.dispose()


class Recorder:
    """The recorder pointed at the test engine, with every published event and every session opened journalled."""

    def __init__(self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch):
        self.engine = engine
        self.published: list[object] = []
        self.sessions_opened = 0
        self.fail_sessions_after: int | None = None

        @asynccontextmanager
        async def own_session() -> AsyncIterator[AsyncSession]:
            self.sessions_opened += 1
            if self.fail_sessions_after is not None and self.sessions_opened > self.fail_sessions_after:
                raise ExogenousError(Reason.DATABASE_UNAVAILABLE, 'the database is not there')
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session

        monkeypatch.setattr(run_recording, 'own_session', own_session)
        monkeypatch.setattr(run_recording, 'publish', self.published.append)

    async def rows(self) -> list[IngestRun]:
        async with AsyncSession(self.engine) as session:
            return list((await session.execute(select(IngestRun))).scalars())

    async def only_row(self) -> IngestRun:
        (row,) = await self.rows()
        return row

    def states(self) -> list[IngestRunState]:
        return [event.run.state for event in self.published]


@pytest.fixture
def recorder(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    return Recorder(engine, monkeypatch)


async def an_entry(engine: AsyncEngine) -> uuid.UUID:
    entry_id = uuid.uuid4()
    async with AsyncSession(engine) as session:
        session.add(
            StoreDatasetEntry(
                id=entry_id,
                owner=OWNER,
                source=DataSource.ALPACA_API,
                feed=Feed.IEX,
                asset_symbol='VFV',
                asset_type=AssetType.STOCK,
                data_type=DataType.MARKET_ACTIVITY,
                granularity=Granularity.ONE_DAY,
                start=START,
                end=START,
                expiry_type=ExpiryType.BULK,
                update_type=UpdateType.STATIC,
                created_at=START,
                updated_at=START,
            )
        )
        await session.commit()
    return entry_id


# ------------------------------------------------------------------------------------------------- the lifecycle


@pytest.mark.asyncio
@pytest.mark.parametrize(('bars', 'state'), [(42, IngestRunState.SUCCEEDED), (0, IngestRunState.EMPTY)])
async def test_a_served_fetch_is_running_then_acknowledged_then_succeeded_or_empty(recorder, engine, bars, state):
    entry_id = await an_entry(engine)

    async with run_recording.recorded_run(PATH, BODY) as run:
        # BEFORE the body does anything, the RUNNING row is committed and readable from another session.
        running = await recorder.only_row()
        assert (running.state, running.dataset_id, running.finished_at) == (IngestRunState.RUNNING, None, None)
        await run.acknowledge(entry_id, Feed.SIP)
        acked = await recorder.only_row()
        assert (acked.state, acked.dataset_id, acked.feed) == (IngestRunState.RUNNING, entry_id, Feed.SIP)
        await run.succeed(bars)

    row = await recorder.only_row()
    assert (row.state, row.bars_written, row.dataset_id) == (state, bars, entry_id)
    assert row.finished_at is not None and row.error_reason is None and row.error_message is None
    assert (row.trigger, row.asset_symbol, row.source, row.granularity) == (
        IngestRunTrigger.REQUEST,
        'VFV',
        DataSource.ALPACA_API,
        Granularity.ONE_DAY,
    )
    assert (row.requested_start, row.requested_end, row.actor) == (BODY.start, BODY.end, OWNER)
    assert recorder.states() == [IngestRunState.RUNNING, IngestRunState.RUNNING, state]
    assert all(isinstance(event, IngestRunChanged) for event in recorder.published)
    assert recorder.published[1].run.dataset_id == entry_id, 'the ack event carries the row after the ack'


@pytest.mark.asyncio
async def test_a_typed_failure_is_recorded_with_its_identity_and_propagates_unchanged(recorder, engine):
    """A refused ack: FAILED with (domain, reason), detail and error_id, NULL dataset_id, and the SAME exception out."""
    error = ExogenousError(Reason.PEER_UNAVAILABLE, 'data_ingest is not answering', metadata={'error_id': 'e-1'})

    with pytest.raises(ExogenousError) as raised:
        async with run_recording.recorded_run(PATH, BODY):
            raise error

    assert raised.value is error, 'the recorder must re-raise the fetch error itself, not a copy or a wrapper'
    row = await recorder.only_row()
    assert (row.state, row.dataset_id, row.bars_written) == (IngestRunState.FAILED, None, 0)
    assert (row.error_domain, row.error_reason, row.error_message, row.error_id) == (
        ERROR_DOMAIN,
        'PEER_UNAVAILABLE',
        'data_ingest is not answering',
        'e-1',
    )
    assert recorder.states() == [IngestRunState.RUNNING, IngestRunState.FAILED]


@pytest.mark.asyncio
async def test_an_untyped_failure_records_a_fixed_message_and_never_the_exceptions_text(recorder):
    secret = 'SELECT * FROM store_dataset_entry WHERE owner = owner-sentinel'
    with pytest.raises(RuntimeError, match='owner-sentinel'):
        async with run_recording.recorded_run(PATH, BODY):
            raise RuntimeError(secret)

    row = await recorder.only_row()
    assert (row.state, row.error_domain, row.error_reason) == (IngestRunState.FAILED, None, None)
    assert row.error_message == run_recording.UNEXPECTED_MESSAGE
    assert secret not in (row.error_message or '')


@pytest.mark.asyncio
async def test_a_cancelled_fetch_is_finished_failed_inside_a_shield_and_still_cancelled(recorder):
    entered = asyncio.Event()

    async def fetch() -> None:
        async with run_recording.recorded_run(PATH, BODY):
            entered.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(fetch())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await recorder.only_row()
    assert (row.state, row.error_reason, row.error_message) == (
        IngestRunState.FAILED,
        None,
        run_recording.CANCELLED_MESSAGE,
    )
    assert row.finished_at is not None


@pytest.mark.asyncio
async def test_a_failure_after_an_ack_naming_an_uncommitted_entry_reverts_to_no_dataset(recorder):
    """The fetch rolled back the entry its ack upserted: the run must not point at a dataset that never existed."""
    never_committed = uuid.uuid4()
    with pytest.raises(ExogenousError):
        async with run_recording.recorded_run(PATH, BODY) as run:
            await run.acknowledge(never_committed, Feed.IEX)
            assert (await recorder.only_row()).dataset_id == never_committed
            raise ExogenousError(Reason.PEER_INTERNAL, 'the stream broke after the ack')

    row = await recorder.only_row()
    assert (row.state, row.dataset_id) == (IngestRunState.FAILED, None)
    assert run.dataset_id == never_committed, 'the handle still knows what the ack named'


@pytest.mark.asyncio
async def test_a_failure_on_an_existing_dataset_keeps_its_dataset_id(recorder, engine):
    """A repeat of an existing dataset: the entry is committed, so a later failure keeps the link."""
    entry_id = await an_entry(engine)
    with pytest.raises(ExogenousError):
        async with run_recording.recorded_run(PATH, BODY) as run:
            await run.acknowledge(entry_id, Feed.IEX)
            raise ExogenousError(Reason.PEER_INTERNAL, 'the stream broke after the ack')

    assert (await recorder.only_row()).dataset_id == entry_id


@pytest.mark.asyncio
async def test_a_finish_overwrites_any_earlier_error(engine, recorder):
    """finish_run always writes the four error columns, so a SUCCEEDED row never carries a stale FAILED reason."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        record = await crud.start_run(session, _new_run())
        await crud.finish_run(
            session,
            record.id,
            state=IngestRunState.FAILED,
            bars_written=0,
            failure=crud.RunFailure('d', 'R', 'm', 'id'),
        )
        again = await crud.finish_run(session, record.id, state=IngestRunState.SUCCEEDED, bars_written=3)
    assert (again.error_domain, again.error_reason, again.error_message, again.error_id) == (None, None, None, None)


def _new_run(**overrides) -> crud.NewRun:
    fields = {
        'trigger': IngestRunTrigger.REQUEST,
        'actor': OWNER,
        'asset_symbol': 'VFV',
        'asset_type': AssetType.STOCK,
        'data_type': DataType.MARKET_ACTIVITY,
        'source': DataSource.ALPACA_API,
        'granularity': Granularity.ONE_DAY,
        'update_type': UpdateType.STATIC,
        'requested_start': START,
        'requested_end': None,
    }
    return crud.NewRun(**fields | overrides)


# ------------------------------------------------------------------------------------------------- best effort


@pytest.mark.asyncio
async def test_a_start_that_cannot_be_recorded_is_logged_and_the_fetch_goes_on(recorder, caplog):
    recorder.fail_sessions_after = 0
    ran = False
    with caplog.at_level(logging.ERROR, logger=run_recording.__name__):
        async with run_recording.recorded_run(PATH, BODY) as run:
            await run.acknowledge(uuid.uuid4(), Feed.IEX)
            ran = True
            await run.succeed(5)

    assert ran, 'the fetch body must run even when its record could not be started'
    assert run.record is None
    assert await recorder.rows() == []
    assert recorder.published == [], 'nothing was committed, so nothing may be announced'
    assert recorder.sessions_opened == 1, 'with no row, the ack and finish must not try to write'
    assert any('Could not record the start' in record.message for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize('failing_write', ['ack', 'finish'])
async def test_a_later_write_that_cannot_be_recorded_is_logged_and_never_masks_the_fetch(
    recorder, engine, caplog, failing_write
):
    entry_id = await an_entry(engine)
    recorder.fail_sessions_after = 1 if failing_write == 'ack' else 2
    with caplog.at_level(logging.ERROR, logger=run_recording.__name__):
        async with run_recording.recorded_run(PATH, BODY) as run:
            await run.acknowledge(entry_id, Feed.IEX)
            await run.succeed(5)

    assert run.dataset_id == entry_id, 'the route still learns the dataset to announce'
    assert any(
        f'Could not record the {"acknowledgement" if failing_write == "ack" else "finish"}' in r.message
        for r in caplog.records
    )
    assert (await recorder.only_row()).state is IngestRunState.RUNNING, 'left for the startup sweep'


@pytest.mark.asyncio
async def test_a_failing_fetch_whose_finish_cannot_be_recorded_still_raises_its_own_error(recorder):
    recorder.fail_sessions_after = 1
    error = ExogenousError(Reason.PEER_UNAVAILABLE, 'down')
    with pytest.raises(ExogenousError) as raised:
        async with run_recording.recorded_run(PATH, BODY):
            raise error
    assert raised.value is error, 'a recording failure must not replace the fetch error'


@pytest.mark.asyncio
async def test_the_actor_appears_in_no_log_line(recorder, engine, caplog):
    """D8: the actor is the body's owner, and nothing the recorder logs may carry it -- success, failure or blip."""
    entry_id = await an_entry(engine)
    with caplog.at_level(logging.DEBUG):
        async with run_recording.recorded_run(PATH, BODY) as run:
            await run.acknowledge(entry_id, Feed.IEX)
            await run.succeed(1)
        with pytest.raises(RuntimeError):
            async with run_recording.recorded_run(PATH, BODY):
                raise RuntimeError('boom')
        recorder.fail_sessions_after = recorder.sessions_opened
        async with run_recording.recorded_run(PATH, BODY) as run:
            await run.succeed(1)
        await run_recording.sweep_interrupted_runs()

    assert caplog.records, 'nothing was logged, so this proves nothing'
    leaked = [record.getMessage() for record in caplog.records if OWNER in record.getMessage()]
    assert not leaked, leaked
    assert OWNER not in repr((await recorder.rows())[0]) and OWNER not in repr(recorder.published[0].run)


# ------------------------------------------------------------------------------------------------- the startup sweep


@pytest.mark.asyncio
async def test_the_sweep_fails_every_running_row_as_interrupted_and_touches_nothing_else(recorder, engine):
    async with AsyncSession(engine, expire_on_commit=False) as session:
        left = [await crud.start_run(session, _new_run()) for _ in range(2)]
        done = await crud.start_run(session, _new_run())
        done = await crud.finish_run(session, done.id, state=IngestRunState.SUCCEEDED, bars_written=9)
        failed = await crud.start_run(session, _new_run())
        failed = await crud.finish_run(
            session, failed.id, state=IngestRunState.FAILED, bars_written=0, failure=crud.RunFailure('d', 'R', 'm')
        )

    assert await run_recording.sweep_interrupted_runs() == 2

    rows = {row.id: row for row in await recorder.rows()}
    for record in left:
        row = rows[record.id]
        assert (row.state, row.error_domain, row.error_reason, row.error_message) == (
            IngestRunState.FAILED,
            None,
            INTERRUPTED_REASON,
            crud.INTERRUPTED_MESSAGE,
        )
        assert row.finished_at is not None
    assert (rows[done.id].state, rows[done.id].error_reason, rows[done.id].bars_written) == (
        IngestRunState.SUCCEEDED,
        None,
        9,
    )
    assert (rows[failed.id].error_reason, rows[failed.id].error_message) == ('R', 'm')
    assert await run_recording.sweep_interrupted_runs() == 0, 'a second sweep finds nothing'


def test_interrupted_is_a_run_record_marker_and_not_a_reason():
    """It never reaches problem+json: a client branching on Reason must never meet it there."""
    assert INTERRUPTED_REASON == 'INTERRUPTED'
    assert INTERRUPTED_REASON not in {reason.value for reason in Reason} | set(Reason.__members__)


@pytest.mark.asyncio
async def test_a_sweep_that_cannot_run_is_logged_and_startup_goes_on(recorder, caplog):
    recorder.fail_sessions_after = 0
    with caplog.at_level(logging.ERROR, logger=run_recording.__name__):
        assert await run_recording.sweep_interrupted_runs() == 0
    assert any('Could not sweep' in record.message for record in caplog.records)


# --- the sweep on a never-migrated database (tj-s6r3rt.5) -----------------------------------------------------------
#
# DESIGN: tj-s6r3rt.5. Before its UPDATE the sweep asks whether ingest_run exists, by a READ (to_regclass) that does
# not go through write_transaction. Missing: ONE warning naming the table and make migrate, no UPDATE, return 0, no
# traceback -- write_transaction (transaction.py, unchanged) logs every unclassified write failure with exc_info, and
# that was the ~100 lines. Present: the sweep as before. The probe raising: fall through to the sweep as before.
#
# SQLite has no to_regclass, so `to_regclass` below registers a stand-in with Postgres's contract (the name when the
# table exists, NULL when it does not) on the test's one connection. That runs the crud's real statement end to end
# against a table that is really there or really dropped. What it cannot show is Postgres binding the argument
# (asyncpg); that half is tests/system's.


@pytest_asyncio.fixture
async def to_regclass(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        driver = (await connection.get_raw_connection()).driver_connection
        catalogue = driver._conn  # aiosqlite's sqlite3 handle; the stand-in runs on aiosqlite's own thread

        def stand_in(name: str) -> str | None:
            row = catalogue.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))
            return None if row.fetchone() is None else name

        await driver.create_function('to_regclass', 1, stand_in)


def _spy(monkeypatch: pytest.MonkeyPatch, name: str) -> list[object]:
    """Journal each call of run_recording.<name> and what it returned, still calling the real one."""
    journal: list[object] = []
    real = getattr(run_recording, name)

    async def spy(*args, **kwargs):
        result = await real(*args, **kwargs)
        journal.append(result)
        return result

    monkeypatch.setattr(run_recording, name, spy)
    return journal


async def _two_running_runs(engine: AsyncEngine) -> list[crud.IngestRunRecord]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return [await crud.start_run(session, _new_run()) for _ in range(2)]


@pytest.mark.asyncio
async def test_on_a_never_migrated_database_the_sweep_warns_once_and_writes_nothing(
    recorder, engine, to_regclass, caplog, monkeypatch
):
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: IngestRun.__table__.drop(sync))
    probed = _spy(monkeypatch, 'ingest_run_table_exists')
    updates = _spy(monkeypatch, 'fail_interrupted_runs')

    with caplog.at_level(logging.DEBUG):
        assert await run_recording.sweep_interrupted_runs() == 0

    assert probed == [False], 'the probe must have run, and answered that the table is missing'
    assert updates == [], 'no UPDATE may be attempted against a table that does not exist'
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == [], 'no ERROR, from transaction.py or anywhere'
    assert [r for r in caplog.records if r.exc_info] == [], 'no traceback'
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.name == run_recording.__name__
    assert IngestRun.__tablename__ in warning.getMessage() and 'make migrate' in warning.getMessage()


@pytest.mark.asyncio
async def test_on_a_migrated_database_the_probe_finds_the_table_and_the_sweep_runs(
    recorder, engine, to_regclass, caplog, monkeypatch
):
    left = await _two_running_runs(engine)
    probed = _spy(monkeypatch, 'ingest_run_table_exists')

    with caplog.at_level(logging.DEBUG):
        assert await run_recording.sweep_interrupted_runs() == 2

    assert probed == [True]
    rows = {row.id: row for row in await recorder.rows()}
    assert {(rows[r.id].state, rows[r.id].error_reason) for r in left} == {(IngestRunState.FAILED, INTERRUPTED_REASON)}
    assert not any('Startup sweep skipped' in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize('probe', ['raises', 'unknown function'])
async def test_a_probe_that_fails_falls_through_to_the_sweep(recorder, engine, caplog, monkeypatch, probe):
    """A probe that cannot answer leaves the sweep exactly as it was.

    'unknown function' is SQLite without the stand-in: the probe's own statement fails, as on a database
    that cannot answer it; 'raises' is any exception out of the probe.
    """
    left = await _two_running_runs(engine)
    if probe == 'raises':

        async def failing_probe(db):
            raise RuntimeError('the catalogue cannot be read')

        monkeypatch.setattr(run_recording, 'ingest_run_table_exists', failing_probe)

    with caplog.at_level(logging.DEBUG):
        assert await run_recording.sweep_interrupted_runs() == 2

    rows = {row.id: row for row in await recorder.rows()}
    assert {rows[r.id].state for r in left} == {IngestRunState.FAILED}
    assert not any('Startup sweep skipped' in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize('exists', [True, False])
async def test_the_probe_is_one_to_regclass_read_of_the_models_table_name(exists):
    """Under the Postgres dialect: SELECT to_regclass(<IngestRun.__tablename__>) IS NOT NULL, a plain read."""
    from sqlalchemy.dialects import postgresql

    executed = []

    class Result:
        def scalar_one(self):
            return exists

    class Session:
        async def execute(self, statement):
            executed.append(statement)
            return Result()

    assert await crud.ingest_run_table_exists(Session()) is exists
    (statement,) = executed
    compiled = statement.compile(dialect=postgresql.dialect())
    assert str(compiled).replace('\n', ' ') == 'SELECT to_regclass(%(to_regclass_1)s) IS NOT NULL AS anon_1'
    assert compiled.params == {'to_regclass_1': IngestRun.__tablename__}


@pytest.mark.asyncio
async def test_the_lifespan_sweeps_after_the_database_is_initialised(monkeypatch):
    """Item 5: the sweep runs in data_store's lifespan, AFTER database.initialize, before anything is served."""
    from data.store.app import app_depends

    calls: list[str] = []

    async def initialize() -> None:
        calls.append('initialize')

    async def sweep() -> int:
        calls.append('sweep')
        raise _Stop

    monkeypatch.setattr(app_depends.database, 'initialize', initialize)
    monkeypatch.setattr(app_depends, 'sweep_interrupted_runs', sweep)
    # The process-wide start-up the lifespan does first, none of which this test is about.
    for name in ('initialize_latency_client', 'init_debugger', 'startup_logs'):
        monkeypatch.setattr(app_depends, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(app_depends.SharedWorkerPool, 'worker_startup', lambda *args, **kwargs: None)
    # Never reached (the sweep stops the lifespan first), but if it were, no real DAILY refresh pass (tj-grna9p.92).
    monkeypatch.setenv('DAILY_REFRESH_ENABLED', 'false')

    with pytest.raises(_Stop):
        async with app_depends.lifespan(object()):
            pass
    assert calls == ['initialize', 'sweep']


class _Stop(Exception):
    """Ends the lifespan at the sweep, before it builds the gRPC channel this test has no peer for."""


# ------------------------------------------------------------------------------------------------- the reads


@pytest.mark.asyncio
async def test_the_running_set_names_only_acknowledged_running_runs(engine):
    async with AsyncSession(engine, expire_on_commit=False) as session:
        acked = await crud.start_run(session, _new_run())
        dataset_id = uuid.uuid4()
        await crud.acknowledge_run(session, acked.id, dataset_id, Feed.IEX)
        await crud.start_run(session, _new_run())  # not yet acknowledged: names no dataset
        finished = await crud.start_run(session, _new_run())
        await crud.acknowledge_run(session, finished.id, uuid.uuid4(), Feed.IEX)
        await crud.finish_run(session, finished.id, state=IngestRunState.EMPTY, bars_written=0)

        assert await crud.running_dataset_ids(session) == frozenset({dataset_id})


@pytest.mark.asyncio
async def test_latest_runs_are_one_per_dataset_and_absent_for_none(engine):
    first, second = uuid.uuid4(), uuid.uuid4()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        runs = []
        for dataset_id in (first, first, second):
            record = await crud.start_run(session, _new_run())
            runs.append(await crud.acknowledge_run(session, record.id, dataset_id, Feed.IEX))
        latest = await crud.latest_runs_for_datasets(session, [first, second, uuid.uuid4()])
        assert await crud.latest_runs_for_datasets(session, []) == {}

    assert set(latest) == {first, second}
    newest_first = max(runs[:2], key=lambda run: (run.started_at, run.id))
    assert latest[first].id == newest_first.id and latest[second].id == runs[2].id
