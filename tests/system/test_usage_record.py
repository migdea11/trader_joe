"""The usage record against a real Postgres: migration b3d9e1a47c62, the recorder's flush, the rollup and the read helper.

Design: tj-grna9p.46 (body and the architect's notes of 2026-10-13, which win over it): one additive revision on
6f5a798746a1 whose downgrade drops both tables and both enum types; a recorder that buffers reads in process and
writes them in ONE batch per flush, so the read path issues no INSERT; at most 32 distinct client ids per dataset
per UTC day, applied against the ids already stored so it holds across a restart; a rollup that folds rows past the
window into dataset_read_daily idempotently, where a kill between the fold and the delete neither double-counts nor
loses a row; a helper over the union of per-read rows and the rollup; GET /internal/asset-data records one read per
dataset it returned, labelled by X-Client-Id (OPTION R, tj-grna9p.131), and listing reads record nothing.

WHY A SCRATCH DATABASE. The rollup is global -- it folds EVERY row past its cutoff -- and the running data_store runs
the same rollup on its own schedule, so neither may be pointed at the stack's database without folding rows this
suite does not own or racing a fold it does not control. Every test here except the last therefore migrates a
database it creates itself, exactly as test_migration_with_data.py does (the tj-vhboky.62 exception; its ScratchDb
is reused, not copied), and drops it afterwards. The last test drives the RUNNING data_store over HTTP and reads
back only rows keyed by an entry this run created, which it deletes.

Run by make test-system and the agent-stack MCP's run_system_tests. FAIL, NEVER SKIP.
"""

import hashlib
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from common.enums.data_select import AssetType, DataType
from data.store.app import usage_recorder as recorder_module
from data.store.app.client_label import (
    CLIENT_ID_HEADER,
    OPERATOR_PRINCIPAL,
    OVERFLOW,
    UNATTRIBUTED,
    ClientLabel,
    classify_client_label,
)
from data.store.app.database.crud.dataset_read import (
    NewRead,
    fold_reads_before,
    insert_reads,
    read_daily_reads,
    read_dataset_usage,
)
from data.store.app.database.models.dataset_read import DatasetRead, DatasetReadKind, ReadClientKind
from data.store.app.database.models.dataset_read_daily import DatasetReadDaily
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.usage_recorder import MAX_IDS_PER_DATASET_DAY, UsageRecorder
from routers.data_store.app_endpoints import AssetDataInterface, AssetDatasetStoreInterface, UiDatasetsInterface
from tests.system.test_migration_with_data import MIGRATIONS_DIR, ScratchDb, _quote


pytestmark = pytest.mark.data_store

READ_TABLE = DatasetRead.__table__
DAILY_TABLE = DatasetReadDaily.__table__
DATASET_READ_REVISION = 'b3d9e1a47c62'
PARENT_REVISION = '6f5a798746a1'
NEW_TABLES = {READ_TABLE.name, DAILY_TABLE.name}
NEW_ENUMS = {'read_client_kind', 'dataset_read_kind'}
ROUTE = AssetDataInterface.GET_ASSET_DATA.value

SDK_ONE = ClientLabel('client-one', ReadClientKind.SDK)
SDK_TWO = ClientLabel('client-two', ReadClientKind.SDK)
OPERATOR = ClientLabel(OPERATOR_PRINCIPAL, ReadClientKind.UI)

# Fixed instants, far from now, so nothing here depends on the clock.
DAY_ONE = datetime(2026, 3, 2, tzinfo=UTC)
DAY_TWO = datetime(2026, 3, 3, tzinfo=UTC)


def _head() -> str:
    (head,) = ScriptDirectory(str(MIGRATIONS_DIR)).get_heads()
    return head


# ---------------------------------------------------------------------------------------------
# A scratch database, and the recorder pointed at it.


@pytest.fixture
def scratch_db(request: pytest.FixtureRequest, pg_settings, pg_engine: Engine, run_identity) -> Iterator[ScratchDb]:
    """A scratch database for this test alone, NOT migrated: created here, dropped (WITH FORCE) in teardown."""
    digest = hashlib.blake2s(request.node.nodeid.encode(), digest_size=4).hexdigest()
    name = f'usage_{run_identity.run_id}_{digest}'
    admin = pg_engine.execution_options(isolation_level='AUTOCOMMIT')
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE {_quote(name)}'))
    ini = MIGRATIONS_DIR.parent / 'alembic.ini'
    assert ini.is_file(), f'{ini} is missing: is it mounted into test_client?'
    db = ScratchDb(name, pg_settings.url().set(database=name), ini, pg_settings.password)
    try:
        yield db
    finally:
        db.engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS {_quote(name)} WITH (FORCE)'))


@pytest.fixture
def migrated(scratch_db: ScratchDb) -> ScratchDb:
    """The scratch database at head."""
    scratch_db.must('upgrade', _head())
    return scratch_db


class Statements:
    """Every statement the async engine sends, as (text, executemany)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.sent: list[tuple[str, bool]] = []
        event.listen(engine.sync_engine, 'before_cursor_execute', self._record)

    def _record(self, conn, cursor, statement, parameters, context, executemany) -> None:
        self.sent.append((statement, executemany))

    def inserts_into(self, table: str) -> list[tuple[str, bool]]:
        return [
            (text, many)
            for text, many in self.sent
            if text.lstrip().upper().startswith(f'INSERT INTO {table.upper()} ')
        ]


@pytest_asyncio.fixture
async def scratch_async(scratch_db: ScratchDb) -> AsyncIterator[AsyncEngine]:
    """An asyncpg engine on the scratch database: the driver data_store runs."""
    engine = create_async_engine(scratch_db.url.set(drivername='postgresql+asyncpg'), poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def point_recorder(monkeypatch: pytest.MonkeyPatch, scratch_async: AsyncEngine) -> Callable[[], int]:
    """Point the recorder module's own_session at the scratch database; returns the count of sessions opened."""
    opened = [0]

    @asynccontextmanager
    async def own_session() -> AsyncIterator[AsyncSession]:
        opened[0] += 1
        async with AsyncSession(scratch_async, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(recorder_module, 'own_session', own_session)
    return lambda: opened[0]


def _read(dataset_id: uuid.UUID, label: ClientLabel, read_at: datetime, *, bars: int = 10, **extra: Any) -> NewRead:
    return NewRead(
        dataset_id=dataset_id,
        client_id=label.client_id,
        client_kind=label.client_kind,
        route=ROUTE,
        bars_returned=bars,
        read_at=read_at,
        **extra,
    )


async def _insert(engine: AsyncEngine, reads: list[NewRead]) -> None:
    async with AsyncSession(engine) as session:
        await insert_reads(session, reads)


async def _fold(engine: AsyncEngine, cutoff: datetime) -> int:
    async with AsyncSession(engine) as session:
        return await fold_reads_before(session, cutoff)


def _rows(db: ScratchDb, table: sa.Table) -> list[sa.Row]:
    with db.engine.connect() as conn:
        return list(conn.execute(sa.select(table)).all())


def _daily(db: ScratchDb) -> dict[tuple[uuid.UUID, str, date], dict[str, Any]]:
    return {
        (row.dataset_id, row.client_id, row.day): {
            'client_kind': row.client_kind,
            'reads': row.reads,
            'sessions': row.sessions,
            'bars_returned': row.bars_returned,
            'last_read_at': row.last_read_at,
        }
        for row in _rows(db, DAILY_TABLE)
    }


# ---------------------------------------------------------------------------------------------
# The migration.


def test_the_revision_is_below_the_head_and_sits_on_ingest_run():
    # Was "is the head" until tj-grna9p.86 put d7e2a9c35b18 on it; the head's own pin is test_head_revision_shape.py.
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    assert DATASET_READ_REVISION in {revision.revision for revision in script.walk_revisions('base', _head())}
    assert script.get_revision(DATASET_READ_REVISION).down_revision == PARENT_REVISION


def test_upgrade_downgrade_upgrade_creates_then_drops_exactly_the_two_tables_and_two_types(scratch_db: ScratchDb):
    """Up adds the tables and types; down leaves the parent's schema exactly; up again is the same schema as before."""
    scratch_db.must('upgrade', PARENT_REVISION)
    parent_schema = scratch_db.schema()
    assert not {name for name in NEW_ENUMS if scratch_db.enum_exists(name)}

    scratch_db.must('upgrade', DATASET_READ_REVISION)
    head_schema = scratch_db.schema()
    tables = {row[0] for row in head_schema['columns']}
    assert tables >= NEW_TABLES
    assert all(scratch_db.enum_exists(name) for name in NEW_ENUMS)
    # Additive: nothing the parent had is changed or missing.
    for kind in ('columns', 'constraints', 'indexes', 'enums'):
        assert set(parent_schema[kind]) <= set(head_schema[kind]), f'{kind} of the parent changed'

    scratch_db.must('downgrade', PARENT_REVISION)
    assert scratch_db.schema() == parent_schema
    assert not {name for name in NEW_ENUMS if scratch_db.enum_exists(name)}

    scratch_db.must('upgrade', DATASET_READ_REVISION)
    assert scratch_db.schema() == head_schema


def test_the_label_is_not_null_and_the_daily_key_is_unique(migrated: ScratchDb):
    """NOT NULL client_id (the reserved values stand in for NULL), and one daily row per (dataset, client, day)."""
    dataset_id = uuid.uuid4()
    with migrated.engine.connect() as conn:
        with pytest.raises(sa.exc.IntegrityError), conn.begin():
            conn.execute(
                sa.insert(READ_TABLE).values(
                    dataset_id=dataset_id, client_id=None, client_kind='SDK', route=ROUTE, bars_returned=1
                )
            )
        row = {
            'dataset_id': dataset_id,
            'client_id': 'c',
            'client_kind': 'SDK',
            'day': DAY_ONE.date(),
            'last_read_at': DAY_ONE,
        }
        with conn.begin():
            conn.execute(sa.insert(DAILY_TABLE).values(**row))
        with pytest.raises(sa.exc.IntegrityError), conn.begin():
            conn.execute(sa.insert(DAILY_TABLE).values(**row))


# ---------------------------------------------------------------------------------------------
# The recorder's flush.


@pytest.mark.asyncio
async def test_n_reads_are_n_rows_after_one_flush_written_by_one_insert(
    migrated: ScratchDb, scratch_async: AsyncEngine, point_recorder
):
    """The acceptance case: record_read touches no database; one flush writes all N rows in ONE INSERT."""
    statements = Statements(scratch_async)
    recorder = UsageRecorder(flush_batch_size=1000)
    dataset_id = uuid.uuid4()
    n = 7
    for index in range(n):
        recorder.record_read(dataset_id, SDK_ONE, ROUTE, index)
    assert point_recorder() == 0, 'record_read opened a session'
    assert statements.sent == [], 'record_read sent a statement'

    assert await recorder.flush() == n

    rows = _rows(migrated, READ_TABLE)
    assert len(rows) == n
    assert sorted(row.bars_returned for row in rows) == list(range(n))
    assert {(row.dataset_id, row.client_id, row.client_kind, row.kind, row.route) for row in rows} == {
        (dataset_id, SDK_ONE.client_id, 'SDK', 'READ', ROUTE)
    }
    assert len(statements.inserts_into(READ_TABLE.name)) == 1, statements.inserts_into(READ_TABLE.name)
    assert recorder.buffered == 0
    assert await recorder.flush() == 0


@pytest.mark.asyncio
async def test_the_cap_holds_across_a_restart_and_exempts_the_operator_and_reserved_labels(
    migrated: ScratchDb, scratch_async: AsyncEngine, point_recorder
):
    """32 distinct ids per (dataset, UTC day) from the STORED ids; the 33rd is _overflow; known ids still pass."""
    dataset_id, other_dataset = uuid.uuid4(), uuid.uuid4()
    first = UsageRecorder(flush_batch_size=1000)
    for index in range(MAX_IDS_PER_DATASET_DAY):
        first.record_read(dataset_id, ClientLabel(f'client-{index:02d}', ReadClientKind.SDK), ROUTE, 1, read_at=DAY_ONE)
    assert await first.flush() == MAX_IDS_PER_DATASET_DAY

    # A new recorder: the per-day ids it knows come only from the database.
    second = UsageRecorder(flush_batch_size=1000)
    late = DAY_ONE + timedelta(hours=5)
    second.record_read(dataset_id, ClientLabel('client-new', ReadClientKind.SDK), ROUTE, 1, read_at=late)
    second.record_read(dataset_id, ClientLabel('client-00', ReadClientKind.SDK), ROUTE, 1, read_at=late)
    second.record_read(dataset_id, OPERATOR, ROUTE, 1, read_at=late)
    second.record_read(dataset_id, classify_client_label(None), ROUTE, 1, read_at=late)
    second.record_read(dataset_id, ClientLabel('client-new', ReadClientKind.SDK), ROUTE, 1, read_at=DAY_TWO)
    second.record_read(other_dataset, ClientLabel('client-new', ReadClientKind.SDK), ROUTE, 1, read_at=late)
    assert await second.flush() == 6

    with migrated.engine.connect() as conn:
        written = conn.execute(
            sa.select(READ_TABLE.c.dataset_id, READ_TABLE.c.client_id, READ_TABLE.c.client_kind, READ_TABLE.c.read_at)
            .where(READ_TABLE.c.read_at >= late)
            .order_by(READ_TABLE.c.read_at, READ_TABLE.c.client_id)
        ).all()
    labels = sorted(
        (row.dataset_id == dataset_id, row.read_at.date(), row.client_id, row.client_kind) for row in written
    )
    assert labels == sorted(
        [
            (True, DAY_ONE.date(), OVERFLOW, 'SDK'),  # client-new, past the cap
            (True, DAY_ONE.date(), 'client-00', 'SDK'),  # already one of the 32
            (True, DAY_ONE.date(), OPERATOR_PRINCIPAL, 'UI'),  # exempt
            (True, DAY_ONE.date(), UNATTRIBUTED, 'SDK'),  # exempt
            (True, DAY_TWO.date(), 'client-new', 'SDK'),  # a new UTC day
            (False, DAY_ONE.date(), 'client-new', 'SDK'),  # another dataset
        ]
    )


@pytest.mark.asyncio
async def test_a_failed_flush_keeps_the_rows_for_the_next_one(
    migrated: ScratchDb, scratch_async: AsyncEngine, monkeypatch: pytest.MonkeyPatch
):
    """Best effort: a database failure never raises out of flush, and the rows are written by the next flush."""
    calls = [0]

    @asynccontextmanager
    async def flaky_session() -> AsyncIterator[AsyncSession]:
        calls[0] += 1
        if calls[0] == 1:
            raise OSError('the database went away')
        async with AsyncSession(scratch_async, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(recorder_module, 'own_session', flaky_session)
    recorder = UsageRecorder(flush_batch_size=1000)
    recorder.record_read(uuid.uuid4(), SDK_ONE, ROUTE, 3)
    recorder.record_read(uuid.uuid4(), SDK_TWO, ROUTE, 4)

    assert await recorder.flush() == 0
    assert recorder.buffered == 2
    assert _rows(migrated, READ_TABLE) == []
    assert await recorder.flush() == 2
    assert len(_rows(migrated, READ_TABLE)) == 2


@pytest.mark.asyncio
async def test_an_unmigrated_database_warns_once_drops_the_rows_and_recovers_after_migrate(
    scratch_db: ScratchDb, scratch_async: AsyncEngine, point_recorder, caplog: pytest.LogCaptureFixture
):
    """The run-sweep pattern: one warning naming make migrate, rows dropped, never a raise; usage starts once migrated."""
    scratch_db.must('upgrade', PARENT_REVISION)
    recorder = UsageRecorder(flush_batch_size=1000)
    caplog.set_level('DEBUG', logger=recorder_module.log.name)

    recorder.record_read(uuid.uuid4(), SDK_ONE, ROUTE, 1)
    assert await recorder.flush() == 0
    recorder.record_read(uuid.uuid4(), SDK_ONE, ROUTE, 1)
    assert await recorder.flush() == 0
    assert await recorder.roll_up(now=DAY_TWO, after_days=1) == 0
    assert recorder.buffered == 0, 'rows for a table that does not exist are dropped, not kept'

    warnings = [record for record in caplog.records if record.name == recorder_module.log.name]
    assert len(warnings) == 1, [record.getMessage() for record in warnings]
    assert warnings[0].levelname == 'WARNING' and 'make migrate' in warnings[0].getMessage()

    scratch_db.must('upgrade', DATASET_READ_REVISION)
    recorder.record_read(uuid.uuid4(), SDK_ONE, ROUTE, 1)
    assert await recorder.flush() == 1
    assert len(_rows(scratch_db, READ_TABLE)) == 1


# ---------------------------------------------------------------------------------------------
# The rollup.


def _reads_for_rollup(dataset_id: uuid.UUID) -> list[NewRead]:
    """Two UTC days, two clients and the operator, a stream session, and two reads past the cutoff."""
    return [
        _read(dataset_id, SDK_ONE, DAY_ONE + timedelta(hours=1), bars=10),
        _read(dataset_id, SDK_ONE, DAY_ONE + timedelta(hours=23, minutes=59), bars=5),
        _read(dataset_id, SDK_TWO, DAY_ONE + timedelta(hours=2), bars=7),
        _read(dataset_id, OPERATOR, DAY_ONE + timedelta(hours=3), bars=1),
        _read(
            dataset_id,
            SDK_ONE,
            DAY_ONE + timedelta(hours=4),
            bars=100,
            kind=DatasetReadKind.STREAM_SESSION,
            session_opened_at=DAY_ONE + timedelta(hours=4),
            session_closed_at=DAY_ONE + timedelta(hours=5),
        ),
        _read(dataset_id, SDK_ONE, DAY_TWO + timedelta(hours=1), bars=2),
        # Past the cutoff (DAY_TWO + 12h): kept as per-read rows.
        _read(dataset_id, SDK_ONE, DAY_TWO + timedelta(hours=13), bars=3),
        _read(dataset_id, SDK_TWO, DAY_TWO + timedelta(hours=14), bars=4),
    ]


def _expected_daily(dataset_id: uuid.UUID) -> dict[tuple[uuid.UUID, str, date], dict[str, Any]]:
    return {
        (dataset_id, SDK_ONE.client_id, DAY_ONE.date()): {
            'client_kind': 'SDK',
            'reads': 2,
            'sessions': 1,
            'bars_returned': 115,
            'last_read_at': DAY_ONE + timedelta(hours=23, minutes=59),
        },
        (dataset_id, SDK_TWO.client_id, DAY_ONE.date()): {
            'client_kind': 'SDK',
            'reads': 1,
            'sessions': 0,
            'bars_returned': 7,
            'last_read_at': DAY_ONE + timedelta(hours=2),
        },
        (dataset_id, OPERATOR_PRINCIPAL, DAY_ONE.date()): {
            'client_kind': 'UI',
            'reads': 1,
            'sessions': 0,
            'bars_returned': 1,
            'last_read_at': DAY_ONE + timedelta(hours=3),
        },
        (dataset_id, SDK_ONE.client_id, DAY_TWO.date()): {
            'client_kind': 'SDK',
            'reads': 1,
            'sessions': 0,
            'bars_returned': 2,
            'last_read_at': DAY_TWO + timedelta(hours=1),
        },
    }


@pytest.mark.asyncio
async def test_the_rollup_folds_old_rows_into_correct_daily_sums_and_rerunning_changes_nothing(
    migrated: ScratchDb, scratch_async: AsyncEngine
):
    dataset_id = uuid.uuid4()
    await _insert(scratch_async, _reads_for_rollup(dataset_id))
    cutoff = DAY_TWO + timedelta(hours=12)

    assert await _fold(scratch_async, cutoff) == 4
    assert _daily(migrated) == _expected_daily(dataset_id)
    left = _rows(migrated, READ_TABLE)
    assert sorted(row.read_at for row in left) == [DAY_TWO + timedelta(hours=13), DAY_TWO + timedelta(hours=14)]

    assert await _fold(scratch_async, cutoff) == 0
    assert _daily(migrated) == _expected_daily(dataset_id)
    assert len(_rows(migrated, READ_TABLE)) == 2


@pytest.mark.asyncio
async def test_a_day_folded_in_two_runs_adds_up(migrated: ScratchDb, scratch_async: AsyncEngine):
    """The upsert ADDS to an existing summary: a day split by the cutoff sums to the same as one fold."""
    dataset_id = uuid.uuid4()
    await _insert(scratch_async, _reads_for_rollup(dataset_id))

    await _fold(scratch_async, DAY_ONE + timedelta(hours=12))
    await _fold(scratch_async, DAY_TWO + timedelta(hours=12))

    assert _daily(migrated) == _expected_daily(dataset_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(('operation', 'table'), [('INSERT', 'dataset_read_daily'), ('DELETE', 'dataset_read')])
async def test_a_rollup_killed_between_fold_and_delete_loses_nothing_and_never_double_counts(
    migrated: ScratchDb, scratch_async: AsyncEngine, monkeypatch: pytest.MonkeyPatch, operation: str, table: str
):
    """The fold and the delete are ONE statement: a fault in either half leaves the database exactly as it was.

    The fault is a trigger that raises on the INSERT into dataset_read_daily (the fold) or on the DELETE from
    dataset_read. A rollup that folded in one transaction and deleted in another would, with the DELETE faulted,
    leave the day summed AND its rows in place -- a double count on the next run; with the INSERT faulted after a
    committed delete, lost rows. The recorder's roll_up swallows the fault (best effort); the re-run after the fault
    is gone counts every row exactly once.
    """
    dataset_id = uuid.uuid4()
    await _insert(scratch_async, _reads_for_rollup(dataset_id))
    with migrated.engine.begin() as conn:
        conn.execute(
            sa.text(
                'CREATE FUNCTION usage_test_abort() RETURNS trigger LANGUAGE plpgsql AS '
                "$$ BEGIN RAISE EXCEPTION 'killed mid-rollup'; END $$"
            )
        )
        conn.execute(
            sa.text(
                f'CREATE TRIGGER usage_test_abort BEFORE {operation} ON {table} '
                'FOR EACH ROW EXECUTE FUNCTION usage_test_abort()'
            )
        )

    @asynccontextmanager
    async def own_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(scratch_async, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(recorder_module, 'own_session', own_session)
    recorder = UsageRecorder()
    now = DAY_TWO + timedelta(hours=12, days=1)

    assert await recorder.roll_up(now=now, after_days=1) == 0
    assert len(_rows(migrated, READ_TABLE)) == len(_reads_for_rollup(dataset_id))
    assert _daily(migrated) == {}

    with migrated.engine.begin() as conn:
        conn.execute(sa.text(f'DROP TRIGGER usage_test_abort ON {table}'))
    assert await recorder.roll_up(now=now, after_days=1) == 4
    assert await recorder.roll_up(now=now, after_days=1) == 0
    assert _daily(migrated) == _expected_daily(dataset_id)


# ---------------------------------------------------------------------------------------------
# The read helper, over the per-read rows and the rollup.


@pytest.mark.asyncio
async def test_the_usage_helper_reads_the_union_of_rows_and_rollup_by_kind_or_id(
    migrated: ScratchDb, scratch_async: AsyncEngine
):
    dataset_id, unread = uuid.uuid4(), uuid.uuid4()
    await _insert(scratch_async, _reads_for_rollup(dataset_id))
    await _fold(scratch_async, DAY_TWO + timedelta(hours=12))
    now = DAY_TWO + timedelta(hours=20)

    async with AsyncSession(scratch_async) as session:
        sdk = await read_dataset_usage(session, [dataset_id, unread], now=now, window_days=1)
        everything = await read_dataset_usage(
            session, [dataset_id], now=now, window_days=30, client_kinds={ReadClientKind.SDK, ReadClientKind.UI}
        )
        ui_only = await read_dataset_usage(session, None, now=now, window_days=30, client_kinds={ReadClientKind.UI})
        by_id = await read_dataset_usage(session, [dataset_id], now=now, window_days=30, client_ids=[SDK_TWO.client_id])
        per_day = await read_daily_reads(session, [dataset_id], now=now, days=30, client_kinds=set(ReadClientKind))

    # Default kind is SDK. The window of one day starts at DAY_ONE's date (now - 1 day, whole UTC days): the
    # folded SDK reads of both days (3 + 1) plus the two per-read rows after the cutoff. The session is not a read.
    assert set(sdk) == {dataset_id}, 'a dataset with no read is absent'
    assert sdk[dataset_id].reads_in_window == 6
    assert sdk[dataset_id].last_read_at == DAY_TWO + timedelta(hours=14)
    assert everything[dataset_id].reads_in_window == 7
    assert ui_only[dataset_id].reads_in_window == 1
    assert ui_only[dataset_id].last_read_at == DAY_ONE + timedelta(hours=3)
    assert by_id[dataset_id].reads_in_window == 2
    # Per day and kind, summed over client ids: DAY_TWO's SDK is one folded read plus the two per-read rows.
    # Ordered by day, then kind in the Postgres enum's order (UI before SDK).
    assert [(row.dataset_id, row.day, row.client_kind, row.reads) for row in per_day] == [
        (dataset_id, DAY_ONE.date(), ReadClientKind.UI, 1),
        (dataset_id, DAY_ONE.date(), ReadClientKind.SDK, 3),
        (dataset_id, DAY_TWO.date(), ReadClientKind.SDK, 3),
    ]


@pytest.mark.asyncio
async def test_a_stream_session_opens_and_closes_through_the_recorder(
    migrated: ScratchDb, scratch_async: AsyncEngine, point_recorder
):
    recorder = UsageRecorder()
    dataset_id = uuid.uuid4()
    session_id = await recorder.record_session_open(dataset_id, SDK_ONE, '/stream', opened_at=DAY_ONE)
    assert session_id is not None
    await recorder.record_session_close(session_id, 42, closed_at=DAY_ONE + timedelta(minutes=5))

    (row,) = _rows(migrated, READ_TABLE)
    assert (row.id, row.kind, row.bars_returned) == (session_id, 'STREAM_SESSION', 42)
    assert (row.session_opened_at, row.session_closed_at) == (DAY_ONE, DAY_ONE + timedelta(minutes=5))


# ---------------------------------------------------------------------------------------------
# The running data_store: the hook on GET /internal/asset-data.

READ_PATH = AssetDataInterface.GET_ASSET_DATA.format(
    asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value
)
# The stack's flush interval is USAGE_FLUSH_INTERVAL_SECONDS (default 5): wait several of them.
FLUSH_WAIT_SECONDS = 30.0


def _usage_rows(pg_engine: Engine, dataset_id: uuid.UUID) -> list[sa.Row]:
    with pg_engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(
                    READ_TABLE.c.client_id, READ_TABLE.c.client_kind, READ_TABLE.c.route, READ_TABLE.c.bars_returned
                ).where(READ_TABLE.c.dataset_id == dataset_id)
            ).all()
        )


def test_bar_reads_on_the_running_store_are_recorded_one_per_call_and_listing_reads_are_not(
    pg_engine: Engine, insert_entry, bar_values, data_store_url: str, run_identity
):
    entry = insert_entry()
    bars = [bar_values(entry, timestamp=entry.start + timedelta(minutes=minute)) for minute in range(3)]
    try:
        with pg_engine.begin() as conn:
            conn.execute(sa.insert(StockMarketActivity.__table__), bars)
        with httpx.Client(base_url=data_store_url, timeout=30.0) as client:
            # Listing and metadata reads first: none of them may write a row.
            for path in (
                AssetDatasetStoreInterface.GET_STORE_ASSET_DATASET_BY_ID.format(id=entry.id),
                UiDatasetsInterface.GET_UI_DATASET.format(dataset_id=entry.id),
                UiDatasetsInterface.GET_UI_DATASET_COVERAGE.format(dataset_id=entry.id),
            ):
                client.get(path, headers={CLIENT_ID_HEADER: run_identity.owner})
            labels = [run_identity.owner, OPERATOR_PRINCIPAL, None, '_reserved', 'x' * 129]
            for label in labels:
                headers = {} if label is None else {CLIENT_ID_HEADER: label}
                response = client.get(READ_PATH, params={'dataset_id': str(entry.id)}, headers=headers)
                assert response.status_code == 200, response.text[:500]
                assert len(response.json()) == len(bars)
            # A query by dataset id that returns no bars is still one read, of 0 bars.
            empty = client.get(
                READ_PATH,
                params={
                    'dataset_id': str(entry.id),
                    'start': (entry.start - timedelta(days=9)).isoformat(),
                    'end': (entry.start - timedelta(days=8)).isoformat(),
                },
                headers={CLIENT_ID_HEADER: run_identity.owner},
            )
            assert empty.status_code == 200 and empty.json() == [], empty.text[:500]

        expected = len(labels) + 1
        deadline = time.monotonic() + FLUSH_WAIT_SECONDS
        rows = _usage_rows(pg_engine, entry.id)
        while len(rows) < expected and time.monotonic() < deadline:
            time.sleep(0.5)
            rows = _usage_rows(pg_engine, entry.id)
        # One more flush interval: nothing beyond the bar reads may arrive.
        time.sleep(6)
        rows = _usage_rows(pg_engine, entry.id)

        assert sorted((row.client_id, row.client_kind, row.route, row.bars_returned) for row in rows) == sorted(
            [
                (run_identity.owner, 'SDK', AssetDataInterface.GET_ASSET_DATA.value, len(bars)),
                (OPERATOR_PRINCIPAL, 'UI', AssetDataInterface.GET_ASSET_DATA.value, len(bars)),
                (UNATTRIBUTED, 'SDK', AssetDataInterface.GET_ASSET_DATA.value, len(bars)),
                (UNATTRIBUTED, 'SDK', AssetDataInterface.GET_ASSET_DATA.value, len(bars)),
                (UNATTRIBUTED, 'SDK', AssetDataInterface.GET_ASSET_DATA.value, len(bars)),
                (run_identity.owner, 'SDK', AssetDataInterface.GET_ASSET_DATA.value, 0),
            ]
        )
    finally:
        with pg_engine.begin() as conn:
            conn.execute(sa.delete(READ_TABLE).where(READ_TABLE.c.dataset_id == entry.id))
            conn.execute(sa.delete(DAILY_TABLE).where(DAILY_TABLE.c.dataset_id == entry.id))
