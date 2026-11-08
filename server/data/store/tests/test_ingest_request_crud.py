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
  * P35 (tj-7zjov1.28, notes C and F): mark_queued never queues under an instance that is already superseded;
    list_pending_before, list_abandoned and claim_abandoned are the instance watcher's reads (NOT_DELIVERED and
    QUEUED under a superseded instance are one case, only once superseded at or before the cutoff, the count read
    under the request lock).

TIER. The repositories' real statements over SQLite (aiosqlite), the pattern test_run_recording.py uses. Two things
SQLite cannot show and tests/system/test_request_record_schema.py shows on Postgres: that note_instance_seen's three
writes are ONE transaction (a failure in the last leaves none), and the partial unique index on ingest_run. SQLite has
no ARRAY type, so ingest_request is created here with data_types as JSON; every other column is the model's, and the
statements are the repositories' own, built on the model's table.
"""

import dataclasses
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.sqlite import DATETIME
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
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
from data.store.app.database.models.ingest_run import IngestRunTrigger
from schemas.data_ingest.submit import AssetListStatus


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


@pytest.mark.parametrize('trigger', [IngestRunTrigger.SCHEDULE, IngestRunTrigger.REQUEST])
@pytest.mark.parametrize('collect', [True, False])
async def test_create_request_stores_and_reads_the_trigger_and_the_lifecycle_fields(db, engine, trigger, collect):
    """P34c (tj-7zjov1.123): NewIngestRequest maps trigger, expiry and collect, stored as given and read back."""
    expiry = START + timedelta(days=90, hours=5)
    record = await crud.create_request(db, _bars(trigger=trigger, expiry=expiry, collect=collect))

    assert (record.trigger, record.expiry, record.collect) == (trigger, expiry, collect)
    assert record.spend_unknown_attempts == 0
    committed = await _row(engine, record.id)
    assert (committed.trigger, committed.expiry, committed.collect) == (trigger, expiry, collect)
    assert crud.IngestRequestRecord.from_row(committed) == record


async def test_a_request_that_names_none_of_them_stores_null_and_counts_no_unknown_spend(db, engine):
    """NULL trigger reads as REQUEST at the attempt row (the intake's rule), and the record keeps it None."""
    record = await crud.create_request(db, _bars())

    assert (record.trigger, record.expiry, record.collect, record.spend_unknown_attempts) == (None, None, None, 0)
    committed = await _row(engine, record.id)
    assert (committed.trigger, committed.expiry, committed.collect) == (None, None, None)
    assert committed.spend_unknown_attempts == 0, 'the server default, written by no caller'


async def test_from_row_reads_a_stored_trigger_as_the_enum_and_the_unknown_spend_count(db, engine):
    record = await crud.create_request(db, _bars())
    await _set(
        engine, REQUESTS, REQUESTS.c.id == record.id, trigger=IngestRunTrigger.SCHEDULE, spend_unknown_attempts=3
    )

    read = crud.IngestRequestRecord.from_row(await _row(engine, record.id))

    assert read.trigger is IngestRunTrigger.SCHEDULE
    assert read.spend_unknown_attempts == 3


@pytest.mark.parametrize('expiry_type', [*ExpiryType, None], ids=lambda value: getattr(value, 'name', 'none'))
async def test_create_request_stores_the_expiry_type_as_its_value_and_get_request_reads_it_back(
    db, engine, expiry_type
):
    """P35r (tj-7zjov1.153): each ExpiryType stored as its integer value, None as NULL; record and re-read equal."""
    record = await crud.create_request(db, _bars(expiry_type=expiry_type))

    assert record.expiry_type is expiry_type
    committed = await _row(engine, record.id)
    assert committed.expiry_type == (None if expiry_type is None else expiry_type.value)
    assert (await crud.get_request(db, record.id)).expiry_type is expiry_type
    assert crud.IngestRequestRecord.from_row(committed) == record


async def test_a_request_that_names_no_expiry_type_stores_null(db, engine):
    """The field defaults to None on both dataclasses, so every existing caller writes NULL."""
    assert crud.NewIngestRequest.__dataclass_fields__['expiry_type'].default is None
    assert crud.IngestRequestRecord.__dataclass_fields__['expiry_type'].default is None
    record = await crud.create_request(db, _bars())

    assert record.expiry_type is None
    assert (await _row(engine, record.id)).expiry_type is None


async def test_from_row_reads_a_stored_expiry_type_integer_as_the_enum(db, engine):
    """Written behind the repository's back, so the mapping in from_row alone turns the integer into the member."""
    record = await crud.create_request(db, _bars())
    await _set(engine, REQUESTS, REQUESTS.c.id == record.id, expiry_type=ExpiryType.ROLLING.value)

    read = await crud.get_request(db, record.id)

    assert read.expiry_type is ExpiryType.ROLLING


async def test_create_request_mints_an_id_when_the_caller_gives_none(db, engine):
    first = await crud.create_request(db, _bars())
    second = await crud.create_request(db, _bars())

    assert isinstance(first.id, uuid.UUID) and first.id != second.id
    assert (await _row(engine, first.id)).id == first.id


def _asset_list(status: AssetListStatus | None = AssetListStatus.ACTIVE, **overrides) -> crud.NewIngestRequest:
    """An asset-list row as new_asset_list_request writes it: the list named (ADR tj-26jt9w addendum 17)."""
    values = {
        'kind': IngestRequestKind.ASSET_LIST,
        'source': DataSource.ALPACA_API,
        'client_label': LABEL,
        'asset_type': AssetType.STOCK,
        'asset_list_status': status,
    }
    return crud.NewIngestRequest(**{**values, **overrides})


async def test_an_asset_list_request_carries_no_bar_parameters_and_no_deadline(db):
    record = await crud.create_request(db, _asset_list())

    assert record.kind is IngestRequestKind.ASSET_LIST
    assert (record.asset_symbol, record.granularity, record.update_type, record.feed, record.data_types) == (None,) * 5
    assert (record.ready_by, record.best_effort) == (None, False)


# --------------------------------------------------------------------------------------------- the asset list's list
#
# THE DESIGN: the architect's ruling of 23:31 UTC on tj-7zjov1.30 (P17) and ADR tj-26jt9w addendum 17. The request row
# names its list (asset_list_status, the AssetListStatus domain enum); ASSET_LIST and a list go together, both ways, in
# the table's CHECK and in NewIngestRequest.__post_init__; create_request writes it and get_request reads it back;
# open_asset_list_request_exists answers for an ASSET_LIST row of that source and list in an open state.


@pytest.mark.parametrize('status', list(AssetListStatus), ids=lambda status: status.value)
async def test_create_then_get_round_trips_each_list(db, engine, status):
    record = await crud.create_request(db, _asset_list(status))

    assert record.asset_list_status is status
    assert (await crud.get_request(db, record.id)).asset_list_status is status
    committed = await _row(engine, record.id)
    assert committed.asset_list_status == status
    assert crud.IngestRequestRecord.from_row(committed) == record


async def test_another_kinds_request_reads_no_list(db):
    assert crud.NewIngestRequest.__dataclass_fields__['asset_list_status'].default is None
    assert crud.IngestRequestRecord.__dataclass_fields__['asset_list_status'].default is None
    record = await crud.create_request(db, _bars())
    assert record.asset_list_status is None
    assert (await crud.get_request(db, record.id)).asset_list_status is None


def test_new_ingest_request_refuses_an_asset_list_naming_no_list():
    """The Python mirror of the CHECK: refused before the insert, never defaulted to a list."""
    with pytest.raises(ValueError, match='must name its list'):
        _asset_list(None)


@pytest.mark.parametrize('kind', [IngestRequestKind.BARS, IngestRequestKind.CORPORATE_ACTIONS])
@pytest.mark.parametrize('status', list(AssetListStatus), ids=lambda status: status.value)
def test_new_ingest_request_refuses_another_kind_naming_a_list(kind, status):
    with pytest.raises(ValueError, match='cannot name an asset list'):
        crud.NewIngestRequest(kind=kind, source=DataSource.ALPACA_API, client_label=LABEL, asset_list_status=status)


@pytest.mark.parametrize(
    ('kind', 'status'),
    [(IngestRequestKind.ASSET_LIST, None), (IngestRequestKind.BARS, AssetListStatus.ACTIVE)],
    ids=['asset-list-without-a-list', 'bars-with-a-list'],
)
async def test_the_tables_check_refuses_both_wrong_combinations_written_behind_the_repository(engine, kind, status):
    """The model's CHECK (ck_ingest_request_asset_list_status) on SQLite.

    Its Postgres twin is in tests/system/test_asset_list_pg.py. Written by a raw insert, so __post_init__ is not what
    refuses.
    """
    async with AsyncSession(engine) as other:
        with pytest.raises(sa.exc.IntegrityError, match=IngestRequest.ASSET_LIST_STATUS_CONSTRAINT):
            await other.execute(
                sa.insert(REQUESTS).values(
                    id=uuid.uuid4(),
                    kind=kind,
                    source=DataSource.ALPACA_API,
                    client_label=LABEL,
                    asset_list_status=status,
                    state=IngestRequestState.PENDING,
                    attempts=0,
                    created_at=START,
                )
            )


def test_the_model_declares_the_check_both_ways_under_the_designs_name():
    (check,) = [
        constraint
        for constraint in REQUESTS.constraints
        if isinstance(constraint, sa.CheckConstraint) and constraint.name == IngestRequest.ASSET_LIST_STATUS_CONSTRAINT
    ]
    assert IngestRequest.ASSET_LIST_STATUS_CONSTRAINT == 'ck_ingest_request_asset_list_status'
    assert ' '.join(str(check.sqltext).split()) == "(kind = 'ASSET_LIST') = (asset_list_status IS NOT NULL)"


@pytest.mark.parametrize('state', list(IngestRequestState), ids=lambda state: state.value)
async def test_open_asset_list_request_exists_is_true_for_an_open_row_of_that_list_only(db, engine, state):
    """Open (PENDING, QUEUED, NOT_DELIVERED) of THAT list and source: True; terminal, or the other list: False."""
    record = await crud.create_request(db, _asset_list(AssetListStatus.INACTIVE))
    if state is not IngestRequestState.PENDING:
        await _set(engine, REQUESTS, REQUESTS.c.id == record.id, state=state)

    assert await crud.open_asset_list_request_exists(db, DataSource.ALPACA_API, AssetListStatus.INACTIVE) is (
        state in OPEN_STATES
    )
    assert await crud.open_asset_list_request_exists(db, DataSource.ALPACA_API, AssetListStatus.ACTIVE) is False


async def test_open_asset_list_request_exists_ignores_other_kinds_and_is_false_on_an_empty_table(db, engine):
    assert await crud.open_asset_list_request_exists(db, DataSource.ALPACA_API, AssetListStatus.ACTIVE) is False
    await _in_state(db, engine, IngestRequestState.PENDING)
    assert await crud.open_asset_list_request_exists(db, DataSource.ALPACA_API, AssetListStatus.ACTIVE) is False


async def test_open_asset_list_request_exists_answers_per_source(db):
    await crud.create_request(db, _asset_list(AssetListStatus.ACTIVE, source=DataSource.IB_API))

    assert await crud.open_asset_list_request_exists(db, DataSource.IB_API, AssetListStatus.ACTIVE) is True
    assert await crud.open_asset_list_request_exists(db, DataSource.ALPACA_API, AssetListStatus.ACTIVE) is False


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


# P35 (tj-7zjov1.28, architect note F): NEVER QUEUED UNDER A SUPERSEDED INSTANCE. The sighting marks only the rows
# QUEUED in its own snapshot, so a row queued under an instance that is already superseded would be held by nobody.


@pytest.mark.parametrize('state', [IngestRequestState.PENDING, IngestRequestState.NOT_DELIVERED])
async def test_f_mark_queued_refuses_an_instance_that_is_already_superseded_and_leaves_the_row(db, engine, state):
    await instance_crud.note_instance_seen(db, 'instance-a')
    await instance_crud.note_instance_seen(db, 'instance-b')
    request_id = await _in_state(db, engine, state)
    before = await _row(engine, request_id)

    assert await crud.mark_queued(db, request_id, 'instance-a') is None

    assert tuple(await _row(engine, request_id)) == tuple(before)


@pytest.mark.parametrize('state', [IngestRequestState.PENDING, IngestRequestState.NOT_DELIVERED])
async def test_f_mark_queued_still_queues_under_the_live_instance_after_a_supersession(db, engine, state):
    await instance_crud.note_instance_seen(db, 'instance-a')
    await instance_crud.note_instance_seen(db, 'instance-b')
    request_id = await _in_state(db, engine, state)

    record = await crud.mark_queued(db, request_id, 'instance-b')

    assert (record.state, record.instance_id) == (IngestRequestState.QUEUED, 'instance-b')


async def test_f_mark_queued_under_an_instance_the_marker_does_not_hold_queues(db, engine):
    """An instance the marker does not hold is not superseded.

    Only a SUPERSEDED instance is refused; an id never sighted is not superseded (the submit path notes it first, so
    this is the guard's other edge, not a path the store takes).
    """
    request_id = await _in_state(db, engine, IngestRequestState.PENDING)

    record = await crud.mark_queued(db, request_id, 'instance-never-seen')

    assert (record.state, record.instance_id) == (IngestRequestState.QUEUED, 'instance-never-seen')


# --------------------------------------------------------------------------------------------- the watcher's reads

T0 = datetime(2026, 3, 1, 12, tzinfo=UTC)


async def _created_at(engine: AsyncEngine, request_id: uuid.UUID, at: datetime) -> None:
    await _set(engine, REQUESTS, REQUESTS.c.id == request_id, created_at=at)


async def test_list_pending_before_reads_only_pending_rows_older_than_the_cutoff_oldest_first(db, engine):
    old = await _in_state(db, engine, IngestRequestState.PENDING)
    older = await _in_state(db, engine, IngestRequestState.PENDING)
    at_cutoff = await _in_state(db, engine, IngestRequestState.PENDING)
    young = await _in_state(db, engine, IngestRequestState.PENDING)
    queued = await _in_state(db, engine, IngestRequestState.QUEUED)
    not_delivered = await _in_state(db, engine, IngestRequestState.NOT_DELIVERED)
    for request_id, at in (
        (old, T0 - timedelta(seconds=40)),
        (older, T0 - timedelta(minutes=5)),
        (at_cutoff, T0),
        (young, T0 + timedelta(seconds=1)),
        (queued, T0 - timedelta(hours=1)),
        (not_delivered, T0 - timedelta(hours=1)),
    ):
        await _created_at(engine, request_id, at)

    listed = await crud.list_pending_before(db, T0)

    assert [record.id for record in listed] == [older, old], 'strictly before the cutoff, PENDING only, oldest first'


async def _held(
    db: AsyncSession, engine: AsyncEngine, state: IngestRequestState, instance_id: str | None, **values
) -> uuid.UUID:
    request_id = await _in_state(db, engine, state)
    await _set(engine, REQUESTS, REQUESTS.c.id == request_id, instance_id=instance_id, **values)
    return request_id


async def _instances(db: AsyncSession, engine: AsyncEngine, superseded: dict[str, datetime | None]) -> None:
    for instance_id in superseded:
        await instance_crud.note_instance_seen(db, instance_id)
    for instance_id, at in superseded.items():
        await _set(engine, INSTANCES, INSTANCES.c.instance_id == instance_id, superseded_at=at)


async def test_f_list_abandoned_reads_not_delivered_and_queued_under_an_instance_superseded_by_the_cutoff(db, engine):
    """Abandoned is NOT_DELIVERED or QUEUED under an instance superseded by the cutoff.

    NOT_DELIVERED and QUEUED-under-a-superseded-instance are one case (note F), but only once the supersession is at
    or before the cutoff (ruling C: the delivery deadline plus a margin).
    """
    await _instances(
        db,
        engine,
        {'old': T0 - timedelta(minutes=5), 'recent': T0 + timedelta(seconds=1), 'at-cutoff': T0, 'live': None},
    )
    not_delivered = await _held(db, engine, IngestRequestState.NOT_DELIVERED, 'old', created_at=T0 - timedelta(hours=2))
    queued_old = await _held(db, engine, IngestRequestState.QUEUED, 'old', created_at=T0 - timedelta(hours=1))
    queued_at_cutoff = await _held(db, engine, IngestRequestState.QUEUED, 'at-cutoff', created_at=T0)
    no_holder = await _held(db, engine, IngestRequestState.NOT_DELIVERED, None, created_at=T0 - timedelta(hours=3))
    # Not abandoned: superseded too recently, held by the live instance, finished, pending, or queued with no holder.
    await _held(db, engine, IngestRequestState.NOT_DELIVERED, 'recent')
    await _held(db, engine, IngestRequestState.QUEUED, 'recent')
    await _held(db, engine, IngestRequestState.QUEUED, 'live')
    await _held(db, engine, IngestRequestState.SUCCEEDED, 'old')
    await _held(db, engine, IngestRequestState.FAILED, 'old')
    await _held(db, engine, IngestRequestState.PENDING, 'old')
    await _held(db, engine, IngestRequestState.QUEUED, None)

    listed = await crud.list_abandoned(db, T0)

    assert listed == [no_holder, not_delivered, queued_old, queued_at_cutoff]


async def test_f_claim_abandoned_moves_a_queued_row_under_a_superseded_instance_to_not_delivered_with_its_count(
    db, engine
):
    await _instances(db, engine, {'old': T0 - timedelta(minutes=5)})
    request_id = await _held(db, engine, IngestRequestState.QUEUED, 'old', attempts=3)

    record = await crud.claim_abandoned(db, request_id, T0)

    assert (record.state, record.attempts, record.instance_id) == (IngestRequestState.NOT_DELIVERED, 3, 'old')
    committed = await _row(engine, request_id)
    assert (committed.state, committed.attempts) == (IngestRequestState.NOT_DELIVERED, 3), 'the move is committed'


@pytest.mark.parametrize('holder', ['old', None], ids=['superseded-holder', 'no-holder'])
async def test_claim_abandoned_returns_a_not_delivered_row_with_its_durable_count(db, engine, holder):
    await _instances(db, engine, {'old': T0 - timedelta(minutes=5)})
    request_id = await _held(db, engine, IngestRequestState.NOT_DELIVERED, holder, attempts=2)

    record = await crud.claim_abandoned(db, request_id, T0)

    assert (record.id, record.state, record.attempts) == (request_id, IngestRequestState.NOT_DELIVERED, 2)


@pytest.mark.parametrize(
    ('state', 'holder'),
    [
        (IngestRequestState.QUEUED, 'live'),
        (IngestRequestState.NOT_DELIVERED, 'recent'),
        (IngestRequestState.QUEUED, 'recent'),
        (IngestRequestState.QUEUED, None),
        (IngestRequestState.QUEUED, 'never-seen'),
        (IngestRequestState.SUCCEEDED, 'old'),
        (IngestRequestState.PENDING, 'old'),
    ],
    ids=['live', 'nd-recent', 'queued-recent', 'queued-no-holder', 'queued-unknown', 'finished', 'pending'],
)
async def test_claim_abandoned_is_none_for_a_row_that_is_not_abandoned_and_leaves_it(db, engine, state, holder):
    """Re-read under the lock: a row taken by a newer instance, finished, or superseded after the cutoff is left."""
    await _instances(db, engine, {'old': T0 - timedelta(minutes=5), 'recent': T0 + timedelta(seconds=1), 'live': None})
    request_id = await _held(db, engine, state, holder)
    before = tuple(await _row(engine, request_id))

    assert await crud.claim_abandoned(db, request_id, T0) is None

    assert tuple(await _row(engine, request_id)) == before


async def test_claim_abandoned_of_an_unknown_request_is_none(db):
    assert await crud.claim_abandoned(db, uuid.uuid4(), T0) is None


async def test_claim_abandoned_reads_the_row_for_update(monkeypatch):
    """claim_abandoned reads the request row FOR UPDATE.

    The count is read UNDER THE REQUEST LOCK (ruling C), so the old instance's last report has committed or is
    refused for ever; the race itself is tests/system/test_resubmit_races_pg.py's.
    """
    statements: list = []

    class _Session:
        async def execute(self, statement):
            statements.append(statement)
            return self

        def scalar_one_or_none(self):
            return None

    @asynccontextmanager
    async def no_transaction(db, label):
        yield

    monkeypatch.setattr(crud, 'write_transaction', no_transaction)

    assert await crud.claim_abandoned(_Session(), uuid.uuid4(), T0) is None

    sql = ' '.join(str(statements[0].compile(dialect=postgresql.dialect())).split())
    assert 'FROM ingest_request WHERE ingest_request.id =' in sql and sql.endswith('FOR UPDATE')


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
