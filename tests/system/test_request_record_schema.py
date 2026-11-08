"""The request record and instance marker (revision f2b7d4a91c36, tj-7zjov1.25 P33) on the real, migrated Postgres.

WRITTEN, NOT YET RUN: there was no Postgres where it was written. It runs with the rest of the suite (make
test-system, or agent-stack run_system_tests after test_up) on a stack migrated to the head.

WHAT IT PROVES that server/data/store/tests cannot, because SQLite has neither partial indexes nor Postgres's now():
  * the partial unique index uq_ingest_run_request_id_attempt_no REFUSES a second attempt row with the same
    (request_id, attempt_no) with 23505 naming the index, and ADMITS every row where either column is NULL, and a
    second attempt number of the same request;
  * the idempotent redelivery upsert the store CLAUDE.md prescribes (ON CONFLICT on the two columns WITH the index's
    WHERE) infers that index and writes nothing on a repeat;
  * note_instance_seen's three writes are ONE transaction: on success the supersede and the instance's first
    sighting carry the same now() (Postgres's transaction start), and a failure in the last write (the
    NOT_DELIVERED mark) leaves no new instance, no supersede and no mark;
  * the repositories run on asyncpg, the ARRAY column included;
  * the two new enum types hold the models' labels in order;
  * ingest_request.expiry_type (39c28a39b290, tj-7zjov1.153) round-trips each ExpiryType and None on asyncpg.
The DDL itself is compared offline in server/data/store/tests/test_request_record_revision.py; that the revision
applies from empty and over the a6d3f8e1c205 seed, and a repeat writes nothing, is test_migration_with_data.py's.

ISOLATION. Every row is keyed by this run (conftest.py): request and run rows by ids minted here, instance ids
prefixed with the run id, and each is deleted at the end of its test. note_instance_seen is GLOBAL by design: a
first sighting supersedes every older instance and marks every QUEUED request of another instance in the database.
That is the behaviour under test, and it is safe on the disposable test stack this suite requires; the assertions
read only this run's rows.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app.database.crud import ingest_instance as instance_crud
from data.store.app.database.crud import ingest_request as crud
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestKind, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger


pytestmark = pytest.mark.data_store

RUN = IngestRun.__table__
REQUESTS = IngestRequest.__table__
INSTANCES = IngestInstance.__table__
SQLSTATE_UNIQUE_VIOLATION = '23505'


# --------------------------------------------------------------------------------------------- fixtures


class Created:
    """The ids one test wrote, deleted at its end: nothing this run did not write is deleted."""

    def __init__(self) -> None:
        self.runs: list[uuid.UUID] = []
        self.requests: list[uuid.UUID] = []
        self.instances: list[str] = []


@pytest.fixture
def created(pg_engine: Engine) -> Iterator[Created]:
    ids = Created()
    yield ids
    with pg_engine.begin() as conn:
        if ids.runs:
            conn.execute(sa.delete(RUN).where(RUN.c.id.in_(ids.runs)))
        if ids.requests:
            conn.execute(sa.delete(REQUESTS).where(REQUESTS.c.id.in_(ids.requests)))
        if ids.instances:
            conn.execute(sa.delete(INSTANCES).where(INSTANCES.c.instance_id.in_(ids.instances)))


def _run_values(run_identity, created: Created, **overrides) -> dict:
    run_id = uuid.uuid4()
    created.runs.append(run_id)
    return {
        'id': run_id,
        'trigger': IngestRunTrigger.REQUEST,
        'actor': run_identity.owner,
        'asset_symbol': run_identity.symbol,
        'asset_type': AssetType.STOCK,
        'data_type': DataType.MARKET_ACTIVITY,
        'source': DataSource.ALPACA_API,
        'granularity': Granularity.ONE_DAY,
        'update_type': UpdateType.STATIC.value,
        'state': IngestRunState.SUCCEEDED,
        **overrides,
    }


def _instance_id(run_identity, created: Created, name: str) -> str:
    instance_id = f'system-test-{run_identity.run_id}-{name}-{uuid.uuid4().hex[:6]}'
    created.instances.append(instance_id)
    return instance_id


@pytest_asyncio.fixture
async def db(pg_async_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(pg_async_engine, expire_on_commit=False) as session:
        yield session


# --------------------------------------------------------------------------------------------- the attempt index


def test_a_second_attempt_row_with_the_same_request_and_number_is_refused_by_the_partial_index(
    attempt, run_identity, created
):
    request_id = uuid.uuid4()
    assert (
        attempt(sa.insert(RUN).values(_run_values(run_identity, created, request_id=request_id, attempt_no=1))) is None
    )

    error = attempt(sa.insert(RUN).values(_run_values(run_identity, created, request_id=request_id, attempt_no=1)))

    assert error is not None, 'a duplicate (request_id, attempt_no) was admitted'
    assert error.orig.pgcode == SQLSTATE_UNIQUE_VIOLATION, error.orig
    assert error.orig.diag.constraint_name == IngestRun.ATTEMPT_UNIQUE_INDEX


@pytest.mark.parametrize(
    ('first', 'second'),
    [
        ({'attempt_no': 1}, {'attempt_no': 2}),
        ({'attempt_no': None}, {'attempt_no': None}),
        ({'request_id': None, 'attempt_no': 1}, {'request_id': None, 'attempt_no': 1}),
        ({'request_id': None, 'attempt_no': None}, {'request_id': None, 'attempt_no': None}),
    ],
    ids=['next-attempt', 'no-attempt-number', 'no-request', 'store-driven-runs'],
)
def test_the_partial_index_admits_rows_it_does_not_cover(attempt, run_identity, created, first, second):
    """Where either column is NULL the index does not apply, so the many store-driven runs never collide."""
    request_id = uuid.uuid4()
    for values in (first, second):
        row = {'request_id': request_id, **values}
        assert attempt(sa.insert(RUN).values(_run_values(run_identity, created, **row))) is None, values


def test_the_attempt_index_is_unique_and_partial_in_the_catalogue(pg_conn):
    row = pg_conn.execute(
        sa.text(
            'SELECT i.indisunique, pg_get_expr(i.indpred, i.indrelid) AS predicate '
            'FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :name'
        ),
        {'name': IngestRun.ATTEMPT_UNIQUE_INDEX},
    ).one()
    assert row.indisunique
    assert row.predicate == '((request_id IS NOT NULL) AND (attempt_no IS NOT NULL))'


def test_the_redelivery_upsert_names_the_index_with_its_where_and_writes_nothing_on_a_repeat(
    pg_engine, run_identity, created
):
    """The form the store CLAUDE.md prescribes for the intake: without the WHERE, Postgres cannot infer the index."""
    request_id = uuid.uuid4()

    def upsert(verdict: str) -> int:
        statement = (
            insert(RUN)
            .values(_run_values(run_identity, created, request_id=request_id, attempt_no=1, verdict=verdict))
            .on_conflict_do_nothing(
                index_elements=['request_id', 'attempt_no'],
                index_where=sa.text('request_id IS NOT NULL AND attempt_no IS NOT NULL'),
            )
        )
        with pg_engine.begin() as conn:
            return conn.execute(statement).rowcount

    assert upsert('CONTINUE') == 1
    assert upsert('FINAL') == 0
    with pg_engine.connect() as conn:
        verdicts = conn.execute(sa.select(RUN.c.verdict).where(RUN.c.request_id == request_id)).scalars().all()
    assert verdicts == ['CONTINUE'], 'the stored verdict answers the redelivery'


# --------------------------------------------------------------------------------------------- the enum types


@pytest.mark.parametrize(
    ('type_name', 'enum_cls'),
    [('ingest_request_kind', IngestRequestKind), ('ingest_request_state', IngestRequestState)],
)
def test_the_new_enum_types_hold_the_models_labels_in_order(pg_conn, type_name, enum_cls):
    labels = pg_conn.execute(sa.text(f'SELECT enum_range(NULL::{type_name})::text[]')).scalar_one()  # nosec B608
    assert labels == [member.value for member in enum_cls]


# --------------------------------------------------------------------------------------------- the repositories on asyncpg


@pytest.mark.asyncio
async def test_a_request_round_trips_through_the_repositories_on_asyncpg(db, run_identity, created):
    start = datetime(2026, 1, 2, tzinfo=UTC)
    request_id = uuid.uuid4()
    created.requests.append(request_id)

    record = await crud.create_request(
        db,
        crud.NewIngestRequest(
            id=request_id,
            kind=IngestRequestKind.BARS,
            source=DataSource.ALPACA_API,
            client_label=run_identity.owner,
            asset_symbol=run_identity.symbol,
            asset_type=AssetType.STOCK,
            data_types=(DataType.MARKET_ACTIVITY.value,),
            granularity=Granularity.ONE_DAY,
            update_type=UpdateType.DAILY,
            feed=Feed.IEX,
            requested_start=start,
            requested_end=start + timedelta(days=5),
            ready_by=start + timedelta(hours=1),
        ),
    )
    instance_id = _instance_id(run_identity, created, 'answer')
    queued = await crud.mark_queued(db, request_id, instance_id)
    finished = await crud.finish_request(db, request_id, state=IngestRequestState.SUCCEEDED)
    again = await crud.finish_request(db, request_id, state=IngestRequestState.EMPTY)

    assert record.data_types == (DataType.MARKET_ACTIVITY.value,)
    assert record.feed is Feed.IEX and record.update_type is UpdateType.DAILY
    assert (queued.state, queued.instance_id) == (IngestRequestState.QUEUED, instance_id)
    assert finished.state is IngestRequestState.SUCCEEDED and finished.finished_at is not None
    assert again is None, 'the first verdict stands'
    assert (await crud.get_request(db, request_id)).state is IngestRequestState.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize('expiry_type', [*ExpiryType, None], ids=lambda value: getattr(value, 'name', 'none'))
async def test_the_expiry_type_round_trips_through_create_and_get_request_on_asyncpg(
    db, pg_engine, run_identity, created, expiry_type
):
    """tj-7zjov1.153 (P35r): each ExpiryType and None stored as its integer value (or NULL) and read back equal."""
    request_id = uuid.uuid4()
    created.requests.append(request_id)

    record = await crud.create_request(
        db,
        crud.NewIngestRequest(
            id=request_id,
            kind=IngestRequestKind.BARS,
            source=DataSource.ALPACA_API,
            client_label=run_identity.owner,
            asset_symbol=run_identity.symbol,
            expiry_type=expiry_type,
        ),
    )

    assert record.expiry_type is expiry_type
    assert (await crud.get_request(db, request_id)).expiry_type is expiry_type
    with pg_engine.connect() as conn:
        stored = conn.execute(sa.select(REQUESTS.c.expiry_type).where(REQUESTS.c.id == request_id)).scalar_one()
    assert stored == (None if expiry_type is None else expiry_type.value), 'stored as ExpiryType integer value'


# --------------------------------------------------------------------------------------------- one transaction


async def _queued_request(db: AsyncSession, run_identity, created: Created, instance_id: str) -> uuid.UUID:
    request_id = uuid.uuid4()
    created.requests.append(request_id)
    await crud.create_request(
        db,
        crud.NewIngestRequest(
            id=request_id,
            kind=IngestRequestKind.ASSET_LIST,
            source=DataSource.ALPACA_API,
            client_label=run_identity.owner,
        ),
    )
    assert await crud.mark_queued(db, request_id, instance_id) is not None
    return request_id


@pytest.mark.asyncio
async def test_a_new_instance_supersedes_and_marks_not_delivered_in_one_transaction(db, run_identity, created):
    old = _instance_id(run_identity, created, 'old')
    new = _instance_id(run_identity, created, 'new')
    await instance_crud.note_instance_seen(db, old)
    held = await _queued_request(db, run_identity, created, old)

    sighting = await instance_crud.note_instance_seen(db, new)

    assert sighting.is_new and old in sighting.superseded and held in sighting.not_delivered
    old_row = (await db.execute(sa.select(INSTANCES).where(INSTANCES.c.instance_id == old))).one()
    new_row = (await db.execute(sa.select(INSTANCES).where(INSTANCES.c.instance_id == new))).one()
    # now() is the transaction's start time in Postgres: equal stamps are the two writes sharing one transaction.
    assert old_row.superseded_at == new_row.first_seen_at
    assert (await crud.get_request(db, held)).state is IngestRequestState.NOT_DELIVERED


class _FailingMark(Exception):
    """Raised in place of the NOT_DELIVERED UPDATE: the third statement of a first sighting."""


@pytest.mark.asyncio
async def test_a_failure_in_the_not_delivered_mark_leaves_no_sighting_no_supersede_and_no_mark(
    db, run_identity, created, monkeypatch
):
    old = _instance_id(run_identity, created, 'old')
    new = _instance_id(run_identity, created, 'new')
    await instance_crud.note_instance_seen(db, old)
    held = await _queued_request(db, run_identity, created, old)

    real_execute = db.execute
    statements: list[str] = []

    async def execute(statement, *args, **kwargs):
        statements.append(str(statement))
        if len(statements) == 3:
            assert 'UPDATE ingest_request' in statements[-1], statements[-1]
            raise _FailingMark
        return await real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, 'execute', execute)
    with pytest.raises(_FailingMark):
        await instance_crud.note_instance_seen(db, new)
    monkeypatch.setattr(db, 'execute', real_execute)

    assert len(statements) == 3, statements
    assert (await db.execute(sa.select(INSTANCES).where(INSTANCES.c.instance_id == new))).one_or_none() is None
    old_row = (await db.execute(sa.select(INSTANCES).where(INSTANCES.c.instance_id == old))).one()
    assert old_row.superseded_at is None
    assert (await crud.get_request(db, held)).state is IngestRequestState.QUEUED
