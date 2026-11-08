"""The request record and instance marker repositories (tj-7zjov1.25, P33), over SQLite.

THE DESIGN is the bead's acceptance criteria and tj-26jt9w addenda 4 and 5, as the repositories' docstrings carry it:
  * create_request writes a PENDING row and COMMITS it before anything is sent; its id is the request id;
  * mark_queued moves PENDING or NOT_DELIVERED to QUEUED with the answering instance, and nothing else: a late or
    repeated answer cannot reopen a finished request;
  * finish_request moves an OPEN request to SUCCEEDED, EMPTY or FAILED, and THE FIRST VERDICT STANDS;
  * list_requests_in_states reads the open requests (by default), oldest first;
  * note_instance_seen: a first sighting inserts the instance, supersedes every older one and marks every QUEUED
    request of an older instance NOT_DELIVERED; a repeat only moves last_seen_at; an OLD id arriving late is a
    repeat, so it can never supersede the live one.

TIER. The repositories' real statements over SQLite (aiosqlite), the pattern test_run_recording.py uses. Two things
SQLite cannot show and tests/system/test_request_record_schema.py shows on Postgres: that note_instance_seen's three
writes are ONE transaction (a failure in the last leaves none), and the partial unique index on ingest_run. SQLite has
no ARRAY type, so ingest_request is created here with data_types as JSON; every other column is the model's, and the
statements are the repositories' own, built on the model's table.
"""

import dataclasses
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import DATETIME
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from data.store.app.database.crud import ingest_instance as instance_crud
from data.store.app.database.crud import ingest_request as crud
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import (
    OPEN_STATES,
    IngestRequest,
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

LABEL = 'client-label-sentinel-4e1b9c'
START = datetime(2026, 1, 2, tzinfo=UTC)
REQUESTS = IngestRequest.__table__
INSTANCES = IngestInstance.__table__
TERMINAL = [IngestRequestState.SUCCEEDED, IngestRequestState.EMPTY, IngestRequestState.FAILED]


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
    metadata = sa.MetaData()
    request_copy = REQUESTS.to_metadata(metadata)
    request_copy.c.data_types.type = sa.JSON()
    INSTANCES.to_metadata(metadata)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session


async def _row(engine: AsyncEngine, request_id: uuid.UUID) -> sa.Row:
    """Read a request from a SECOND session, so what is seen is what was committed."""
    async with AsyncSession(engine) as other:
        return (await other.execute(sa.select(REQUESTS).where(REQUESTS.c.id == request_id))).one()


async def _instance(engine: AsyncEngine, instance_id: str) -> sa.Row:
    async with AsyncSession(engine) as other:
        return (await other.execute(sa.select(INSTANCES).where(INSTANCES.c.instance_id == instance_id))).one()


async def _set(engine: AsyncEngine, table: sa.Table, where: sa.ColumnElement, **values) -> None:
    async with AsyncSession(engine) as other:
        await other.execute(sa.update(table).where(where).values(**values))
        await other.commit()


def _bars(**overrides) -> crud.NewIngestRequest:
    values = {
        'kind': IngestRequestKind.BARS,
        'source': DataSource.ALPACA_API,
        'client_label': LABEL,
        'asset_symbol': 'VFV',
        'asset_type': AssetType.STOCK,
        'granularity': Granularity.ONE_DAY,
        'update_type': UpdateType.DAILY,
        'feed': Feed.IEX,
        'requested_start': START,
        'requested_end': START + timedelta(days=30),
        'ready_by': START + timedelta(hours=6),
        'best_effort': True,
    }
    return crud.NewIngestRequest(**{**values, **overrides})


async def _in_state(db: AsyncSession, engine: AsyncEngine, state: IngestRequestState, **overrides) -> uuid.UUID:
    record = await crud.create_request(db, _bars(**overrides))
    if state is not IngestRequestState.PENDING:
        await _set(engine, REQUESTS, REQUESTS.c.id == record.id, state=state)
    return record.id


# --------------------------------------------------------------------------------------------- create_request


async def test_create_request_commits_a_pending_row_with_the_callers_id_and_every_parameter(db, engine):
    request_id = uuid.uuid4()
    record = await crud.create_request(db, _bars(id=request_id))

    assert record.id == request_id
    assert record.state is IngestRequestState.PENDING
    assert (record.attempts, record.spent_calls, record.spent_pages) == (0, 0, 0)
    assert (record.spent_throttled_429, record.spent_retries) == (0, 0)
    assert (record.instance_id, record.finished_at, record.failure_cause, record.failure_detail) == (None,) * 4
    assert record.created_at is not None
    assert record.client_label == LABEL
    assert (record.kind, record.source, record.asset_symbol) == (IngestRequestKind.BARS, DataSource.ALPACA_API, 'VFV')
    assert (record.asset_type, record.granularity, record.update_type, record.feed) == (
        AssetType.STOCK,
        Granularity.ONE_DAY,
        UpdateType.DAILY,
        Feed.IEX,
    )
    assert (record.requested_start, record.requested_end) == (START, START + timedelta(days=30))
    assert (record.ready_by, record.best_effort) == (START + timedelta(hours=6), True)
    assert record.dataset_id is None
    # P49 (tj-7zjov1.143): the never-written listing_id is gone from the record, the row and the request.
    assert not hasattr(record, 'listing_id')
    assert 'listing_id' not in {f.name for f in dataclasses.fields(crud.NewIngestRequest)}

    committed = await _row(engine, request_id)
    assert committed.state == IngestRequestState.PENDING
    assert committed.client_label == LABEL


async def test_create_request_mints_an_id_when_the_caller_gives_none(db, engine):
    first = await crud.create_request(db, _bars())
    second = await crud.create_request(db, _bars())

    assert isinstance(first.id, uuid.UUID) and first.id != second.id
    assert (await _row(engine, first.id)).id == first.id


async def test_an_asset_list_request_carries_no_bar_parameters_and_no_deadline(db):
    record = await crud.create_request(
        db, crud.NewIngestRequest(kind=IngestRequestKind.ASSET_LIST, source=DataSource.ALPACA_API, client_label=LABEL)
    )

    assert record.kind is IngestRequestKind.ASSET_LIST
    assert (record.asset_symbol, record.granularity, record.update_type, record.feed, record.data_types) == (None,) * 5
    assert (record.ready_by, record.best_effort) == (None, False)


async def test_the_client_label_is_in_no_repr():
    """It is the caller's label, kept to re-submit and never logged (the builder's open decision 2).

    Async only because the module's tests all carry the asyncio mark.
    """
    new = _bars()
    assert LABEL not in repr(new)
    row = IngestRequest(id=uuid.uuid4(), kind=IngestRequestKind.BARS, client_label=LABEL)
    assert LABEL not in repr(row)


async def test_the_record_hides_the_client_label_from_its_repr(db):
    record = await crud.create_request(db, _bars())
    assert LABEL not in repr(record)


# --------------------------------------------------------------------------------------------- get_request


async def test_get_request_returns_the_row_or_none(db):
    record = await crud.create_request(db, _bars())

    assert await crud.get_request(db, record.id) == record
    assert await crud.get_request(db, uuid.uuid4()) is None


# --------------------------------------------------------------------------------------------- mark_queued


@pytest.mark.parametrize('state', [IngestRequestState.PENDING, IngestRequestState.NOT_DELIVERED])
async def test_mark_queued_takes_a_pending_or_not_delivered_request_to_queued_with_its_instance(db, engine, state):
    request_id = await _in_state(db, engine, state)

    record = await crud.mark_queued(db, request_id, 'instance-b')

    assert (record.state, record.instance_id) == (IngestRequestState.QUEUED, 'instance-b')
    committed = await _row(engine, request_id)
    assert (committed.state, committed.instance_id) == (IngestRequestState.QUEUED, 'instance-b')


@pytest.mark.parametrize('state', [IngestRequestState.QUEUED, *TERMINAL])
async def test_mark_queued_leaves_a_queued_or_finished_request_alone(db, engine, state):
    """A repeated answer does not move the holding instance, and a late one cannot reopen a finished request."""
    request_id = await _in_state(db, engine, state, id=uuid.uuid4())
    await _set(engine, REQUESTS, REQUESTS.c.id == request_id, instance_id='instance-a')

    assert await crud.mark_queued(db, request_id, 'instance-b') is None

    committed = await _row(engine, request_id)
    assert (committed.state, committed.instance_id) == (state, 'instance-a')


async def test_mark_queued_of_an_unknown_request_is_none(db):
    assert await crud.mark_queued(db, uuid.uuid4(), 'instance-b') is None


# --------------------------------------------------------------------------------------------- finish_request


@pytest.mark.parametrize('opened', sorted(OPEN_STATES))
@pytest.mark.parametrize('verdict', [IngestRequestState.SUCCEEDED, IngestRequestState.EMPTY])
async def test_finish_request_ends_an_open_request_with_its_verdict_and_a_finish_time(db, engine, opened, verdict):
    request_id = await _in_state(db, engine, opened)

    record = await crud.finish_request(db, request_id, state=verdict)

    assert record.state is verdict
    assert record.finished_at is not None
    assert (record.failure_cause, record.failure_detail) == (None, None)
    assert (await _row(engine, request_id)).state == verdict


async def test_a_failed_request_keeps_its_cause_and_detail(db, engine):
    request_id = await _in_state(db, engine, IngestRequestState.QUEUED)

    record = await crud.finish_request(
        db,
        request_id,
        state=IngestRequestState.FAILED,
        failure_cause=IngestRequestFailureCause.EXPIRED,
        failure_detail='taken off the queue after its ready-by',
    )

    assert (record.state, record.failure_cause) == (IngestRequestState.FAILED, 'EXPIRED')
    committed = await _row(engine, request_id)
    assert (committed.failure_cause, committed.failure_detail) == ('EXPIRED', 'taken off the queue after its ready-by')


@pytest.mark.parametrize('first', TERMINAL)
@pytest.mark.parametrize('second', TERMINAL)
async def test_the_first_verdict_stands(db, engine, first, second):
    request_id = await _in_state(db, engine, IngestRequestState.QUEUED)
    cause = {'failure_cause': IngestRequestFailureCause.PERMANENT} if first is IngestRequestState.FAILED else {}
    finished = await crud.finish_request(db, request_id, state=first, **cause)
    later = (
        {'failure_cause': IngestRequestFailureCause.ATTEMPTS_EXHAUSTED} if second is IngestRequestState.FAILED else {}
    )

    assert await crud.finish_request(db, request_id, state=second, **later) is None

    committed = await _row(engine, request_id)
    assert (committed.state, committed.failure_cause) == (first, finished.failure_cause)
    assert committed.finished_at == finished.finished_at


@pytest.mark.parametrize('state', sorted(OPEN_STATES))
async def test_finish_request_refuses_an_open_state_and_writes_nothing(db, engine, state):
    request_id = await _in_state(db, engine, IngestRequestState.QUEUED)

    with pytest.raises(ValueError, match='terminal'):
        await crud.finish_request(db, request_id, state=state)

    assert (await _row(engine, request_id)).state == IngestRequestState.QUEUED


@pytest.mark.parametrize('state', [IngestRequestState.SUCCEEDED, IngestRequestState.EMPTY])
@pytest.mark.parametrize(
    'failure', [{'failure_cause': IngestRequestFailureCause.EXPIRED}, {'failure_detail': 'a sentence'}]
)
async def test_a_failure_cause_or_detail_belongs_to_failed_only(db, engine, state, failure):
    request_id = await _in_state(db, engine, IngestRequestState.QUEUED)

    with pytest.raises(ValueError, match='FAILED'):
        await crud.finish_request(db, request_id, state=state, **failure)

    assert (await _row(engine, request_id)).state == IngestRequestState.QUEUED


async def test_finish_request_of_an_unknown_request_is_none(db):
    assert await crud.finish_request(db, uuid.uuid4(), state=IngestRequestState.SUCCEEDED) is None


# --------------------------------------------------------------------------------------------- list_requests_in_states


async def test_the_default_list_is_the_open_requests_oldest_first(db, engine):
    ids = {}
    for offset, state in enumerate(
        [
            IngestRequestState.QUEUED,
            IngestRequestState.SUCCEEDED,
            IngestRequestState.PENDING,
            IngestRequestState.FAILED,
            IngestRequestState.NOT_DELIVERED,
            IngestRequestState.EMPTY,
        ]
    ):
        ids[state] = await _in_state(db, engine, state)
        # Oldest is the first written; set apart by a minute so the order is the creation time, not the id.
        await _set(engine, REQUESTS, REQUESTS.c.id == ids[state], created_at=START + timedelta(minutes=10 - offset))

    listed = await crud.list_requests_in_states(db)

    assert [record.id for record in listed] == [
        ids[IngestRequestState.NOT_DELIVERED],
        ids[IngestRequestState.PENDING],
        ids[IngestRequestState.QUEUED],
    ]


async def test_a_list_of_named_states_is_those_only_and_ties_on_time_break_by_id(db, engine):
    pending = [await _in_state(db, engine, IngestRequestState.PENDING) for _ in range(3)]
    await _in_state(db, engine, IngestRequestState.QUEUED)
    await _set(engine, REQUESTS, REQUESTS.c.id.in_(pending), created_at=START)

    listed = await crud.list_requests_in_states(db, [IngestRequestState.PENDING])

    assert [record.id for record in listed] == sorted(pending, key=lambda value: value.hex)
    assert await crud.list_requests_in_states(db, [IngestRequestState.EMPTY]) == []


# --------------------------------------------------------------------------------------------- note_instance_seen


async def test_a_first_instance_on_an_empty_marker_is_new_and_supersedes_nothing(db, engine):
    sighting = await instance_crud.note_instance_seen(db, 'instance-a')

    assert sighting == instance_crud.InstanceSighting('instance-a', is_new=True, superseded=(), not_delivered=())
    row = await _instance(engine, 'instance-a')
    assert row.superseded_at is None and row.first_seen_at is not None and row.last_seen_at is not None


async def test_a_new_instance_supersedes_the_older_ones_and_marks_only_their_queued_requests(db, engine):
    await instance_crud.note_instance_seen(db, 'instance-a')
    held_by_a = await _in_state(db, engine, IngestRequestState.QUEUED, id=uuid.uuid4())
    await _set(engine, REQUESTS, REQUESTS.c.id == held_by_a, instance_id='instance-a')
    pending = await _in_state(db, engine, IngestRequestState.PENDING)
    finished = await _in_state(db, engine, IngestRequestState.SUCCEEDED)
    await _set(engine, REQUESTS, REQUESTS.c.id == finished, instance_id='instance-a')
    # A QUEUED request already answered by the NEW instance (its Submit answer arrived before the sighting).
    held_by_b = await _in_state(db, engine, IngestRequestState.QUEUED)
    await _set(engine, REQUESTS, REQUESTS.c.id == held_by_b, instance_id='instance-b')

    sighting = await instance_crud.note_instance_seen(db, 'instance-b')

    assert sighting == instance_crud.InstanceSighting(
        'instance-b', is_new=True, superseded=('instance-a',), not_delivered=(held_by_a,)
    )
    assert (await _instance(engine, 'instance-a')).superseded_at is not None
    assert (await _instance(engine, 'instance-b')).superseded_at is None
    assert (await _row(engine, held_by_a)).state == IngestRequestState.NOT_DELIVERED
    assert (await _row(engine, held_by_a)).instance_id == 'instance-a', 'the mark keeps who held it'
    assert (await _row(engine, pending)).state == IngestRequestState.PENDING
    assert (await _row(engine, finished)).state == IngestRequestState.SUCCEEDED
    assert (await _row(engine, held_by_b)).state == IngestRequestState.QUEUED


async def test_an_instance_already_superseded_is_not_superseded_again(db, engine):
    await instance_crud.note_instance_seen(db, 'instance-a')
    await instance_crud.note_instance_seen(db, 'instance-b')
    first_superseded = (await _instance(engine, 'instance-a')).superseded_at
    await _set(engine, INSTANCES, INSTANCES.c.instance_id == 'instance-a', superseded_at=START)

    sighting = await instance_crud.note_instance_seen(db, 'instance-c')

    assert first_superseded is not None
    assert sighting.superseded == ('instance-b',)
    assert (await _instance(engine, 'instance-a')).superseded_at == START, 'superseded_at is set once, never moved'


async def test_a_repeat_sighting_moves_only_last_seen(db, engine):
    await instance_crud.note_instance_seen(db, 'instance-a')
    await _set(engine, INSTANCES, INSTANCES.c.instance_id == 'instance-a', first_seen_at=START, last_seen_at=START)
    queued = await _in_state(db, engine, IngestRequestState.QUEUED)
    await _set(engine, REQUESTS, REQUESTS.c.id == queued, instance_id='instance-a')

    sighting = await instance_crud.note_instance_seen(db, 'instance-a')

    assert sighting == instance_crud.InstanceSighting('instance-a', is_new=False, superseded=(), not_delivered=())
    row = await _instance(engine, 'instance-a')
    assert row.first_seen_at == START
    assert row.last_seen_at > START
    assert row.superseded_at is None
    assert (await _row(engine, queued)).state == IngestRequestState.QUEUED


async def test_a_late_sighting_of_an_old_id_never_supersedes_the_live_one(db, engine):
    """A report from a superseded instance arrives after the restart: its id is a repeat, so it moves nothing."""
    await instance_crud.note_instance_seen(db, 'instance-a')
    await instance_crud.note_instance_seen(db, 'instance-b')
    a_superseded = (await _instance(engine, 'instance-a')).superseded_at
    held_by_b = await _in_state(db, engine, IngestRequestState.QUEUED)
    await _set(engine, REQUESTS, REQUESTS.c.id == held_by_b, instance_id='instance-b')

    sighting = await instance_crud.note_instance_seen(db, 'instance-a')

    assert sighting == instance_crud.InstanceSighting('instance-a', is_new=False, superseded=(), not_delivered=())
    assert (await _instance(engine, 'instance-b')).superseded_at is None
    assert (await _instance(engine, 'instance-a')).superseded_at == a_superseded
    assert (await _row(engine, held_by_b)).state == IngestRequestState.QUEUED
