"""The delivery intake end to end over SQLite: DeliveryIntake and crud/ingest_delivery.py (tj-7zjov1.27, P34).

THE DESIGN: the bead's acceptance criteria and notes (the P32 and P36 gate notes, decision 4, the escalation rulings
1 to 6 and C1 to C4 of 2026-10-25, the validator case list of 20:17 UTC), ADR tj-26jt9w addenda (4) (5) (6) (9),
and the store CLAUDE.md delivery section:
  * ONE TRANSACTION per delivered attempt: the data (the kind's arm), the attempt row with quota key and spend
    (failed attempts too), the request's durable count, spend and state; commit, then the answer;
  * a duplicate (request_id, attempt_no) is answered from the stored row and writes NOTHING, before any rule;
  * every refusal (rules a to h, the arms, C1) writes NOTHING;
  * EXPIRED and EXHAUSTED: the request FAILED with its own cause, no attempt row, the count and spend unchanged,
    FINAL with the stored count; a repeat is FINAL and writes nothing;
  * C1: no bar is written under a dataset entry that is gone; C3: the instance collision is logged at ERROR with
    ids only; ruling 6: a FAILED report without stats is written with NULL spend columns and one WARNING;
  * RegisterInstance records the instance and supersedes the older ones;
  * P49 (tj-7zjov1.143): the bars header's vendor_asset_id is stored as received on the attempt row of every bars
    attempt that writes one, an empty id as NULL; a repeat delivery changes nothing; served_range is stored nowhere.

TIER, AND WHAT SQLITE CANNOT PROVE. The intake's own statements run on SQLite (aiosqlite) through the module's
own_session, pointed at the test engine. The bar upsert is Postgres-only (ON CONFLICT ON CONSTRAINT), so the bars
arm's writer is replaced by a stand-in that inserts into a scratch table ON THE SAME SESSION: what reaches it, and
whether it is rolled back, is real; the upsert itself is tests/system/test_delivery_intake_pg.py's. SQLite cannot
show: FOR UPDATE (it ignores it, and has one writer, so no two deliveries ever interleave here), the partial unique
index (it builds a full one from the model, the predicate being postgresql_where), ON CONFLICT naming that partial
index WITH its predicate, the enum and ARRAY types, or a dataset entry deleted concurrently. Those are PG1 to PG8
in tests/system/test_delivery_intake_pg.py. What IS shown here about the locks is the statements' shape, compiled
for Postgres: lock_request and lock_dataset_entry carry FOR UPDATE, and the entry is locked before any bar.
"""

import dataclasses
import logging
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

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import InvalidRequestError, Reason
from data.store.app.database.crud import ingest_delivery
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.crud.ingest_run import IngestRunRecord
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import (
    IngestRequest,
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.events import DatasetChange, DatasetChanged, IngestRunChanged
from data.store.app.ingest import delivery_intake
from data.store.app.ingest.delivery_intake import DeliveryIntake
from data.store.tests.delivery_reports import (
    AS_OF,
    BAR_START,
    FIRST_CALL,
    INSTANCE_A,
    INSTANCE_B,
    LAST_CALL,
    NO_CALL,
    NON_BARS,
    QUOTA_KEY,
    RED_CASES,
    VENDOR_ASSET_ID,
    action_records,
    asset_records,
    bar,
    bar_page,
    report,
    stats,
    with_header,
    with_trailer,
)
from schemas.data_ingest.delivery import (
    DeliverAttemptResponse,
    DeliveryErrorClass,
    DeliveryOutcome,
    DeliveryVerdict,
    RegisterInstanceRequest,
)
from schemas.data_ingest.fetch_dataset import BarPage, ServedRange


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

LABEL = 'client-label-sentinel-0b7d'
START = datetime(2026, 2, 2, tzinfo=UTC)
CHUNK = 7
REQUESTS = IngestRequest.__table__
RUNS = IngestRun.__table__
INSTANCES = IngestInstance.__table__
ENTRIES = StoreDatasetEntry.__table__
_SCRATCH = sa.MetaData()
FAKE_BARS = sa.Table(
    'fake_bars',
    _SCRATCH,
    sa.Column('dataset_id', sa.Uuid, nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('feed', sa.String, nullable=False),
)
# The attempt row's spend: the vendor call stats, by name. vendor_asset_id shares the prefix and is NOT spend (P49);
# test_every_vendor_column_is_spend_or_the_vendor_asset_id keeps this list whole as the model grows.
SPEND_COLUMNS = (
    'vendor_calls',
    'vendor_pages',
    'vendor_throttled_429',
    'vendor_retries',
    'vendor_first_call_at',
    'vendor_last_call_at',
    'vendor_reported_limit',
    'vendor_reported_remaining',
    'vendor_reported_reset_at',
)
FINAL, CONTINUE = DeliveryVerdict.FINAL, DeliveryVerdict.CONTINUE
OPEN = [IngestRequestState.PENDING, IngestRequestState.QUEUED, IngestRequestState.NOT_DELIVERED]


# --------------------------------------------------------------------------------------------- harness


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
async def engine(aware_sqlite_datetimes, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)
    # SQLite has no ARRAY: data_types binds and reads as JSON for this test only (the intake's statements are built
    # on the model's table, so the model's column is swapped and restored). Every other column is the model's.
    monkeypatch.setattr(REQUESTS.c.data_types, 'type', sa.JSON())
    metadata = sa.MetaData()
    REQUESTS.to_metadata(metadata)
    for table in (RUNS, INSTANCES, ENTRIES):
        table.to_metadata(metadata)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.run_sync(_SCRATCH.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


class Store:
    """The intake pointed at the test engine: its sessions, its published events and its bar writes journalled."""

    def __init__(self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch):
        self.engine = engine
        self.published: list[object] = []
        self.batches: list[tuple[object, int | None]] = []
        self.fail_on_write: int | None = None

        @asynccontextmanager
        async def own_session() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session

        async def write_bars(db: AsyncSession, batch, requested_chunk_size: int | None = None) -> int:
            """The stand-in bar writer: the same session, so a rollback of the delivery takes its rows too."""
            self.batches.append((batch, requested_chunk_size))
            if self.fail_on_write is not None and len(self.batches) >= self.fail_on_write:
                raise InvalidRequestError(Reason.DUPLICATE_BAR_TIMESTAMP, 'one page carries a timestamp twice')
            rows = batch.dataset.get(DataType.MARKET_ACTIVITY, [])
            for row in rows:
                await db.execute(
                    sa.insert(FAKE_BARS).values(dataset_id=batch.dataset_id, timestamp=row.timestamp, feed=batch.feed)
                )
            return len(rows)

        monkeypatch.setattr(delivery_intake, 'own_session', own_session)
        monkeypatch.setattr(delivery_intake, 'publish', self.published.append)
        monkeypatch.setattr(delivery_intake, 'write_market_activity_in_transaction', write_bars)
        self.intake = DeliveryIntake(chunk_size=CHUNK)

    # ------------------------------------------------------------------ setup

    async def request(
        self,
        kind: IngestRequestKind = IngestRequestKind.BARS,
        *,
        state: IngestRequestState = IngestRequestState.QUEUED,
        dataset: bool = True,
        **values,
    ) -> uuid.UUID:
        """A request row as the submit path would leave it, with an existing dataset entry for a bars request."""
        dataset_id = await self.entry() if (dataset and kind is IngestRequestKind.BARS) else None
        if kind is IngestRequestKind.BARS:
            fields = {
                'asset_symbol': 'VFV',
                'asset_type': AssetType.STOCK,
                'data_types': (DataType.MARKET_ACTIVITY.value,),
                'granularity': Granularity.ONE_DAY,
                'update_type': UpdateType.DAILY,
                'feed': Feed.IEX,
                'requested_start': START,
                'requested_end': START + timedelta(days=30),
                'dataset_id': dataset_id,
            }
        else:
            fields = {}
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            record = await request_crud.create_request(
                db,
                request_crud.NewIngestRequest(
                    kind=kind, source=DataSource.ALPACA_API, client_label=LABEL, **{**fields, **values}
                ),
            )
        await self.set_request(record.id, state=state, instance_id=INSTANCE_A)
        return record.id

    async def entry(self) -> uuid.UUID:
        entry_id = uuid.uuid4()
        async with AsyncSession(self.engine) as db:
            db.add(
                StoreDatasetEntry(
                    id=entry_id,
                    owner=LABEL,
                    source=DataSource.ALPACA_API,
                    feed=Feed.IEX,
                    asset_symbol='VFV',
                    asset_type=AssetType.STOCK,
                    data_type=DataType.MARKET_ACTIVITY,
                    granularity=Granularity.ONE_DAY,
                    start=START,
                    end=START + timedelta(days=30),
                    expiry_type=ExpiryType.BULK,
                    update_type=UpdateType.DAILY,
                    collect=True,
                    created_at=START,
                    updated_at=START,
                )
            )
            await db.commit()
        return entry_id

    async def set_request(self, request_id: uuid.UUID, **values) -> None:
        async with AsyncSession(self.engine) as db:
            await db.execute(sa.update(REQUESTS).where(REQUESTS.c.id == request_id).values(**values))
            await db.commit()

    async def register(self, instance_id: str) -> None:
        await self.intake.register_instance(RegisterInstanceRequest(instance_id=instance_id, started_at=START))

    # ------------------------------------------------------------------ reads, from a second session

    async def request_row(self, request_id: uuid.UUID) -> sa.Row:
        async with AsyncSession(self.engine) as db:
            return (await db.execute(sa.select(REQUESTS).where(REQUESTS.c.id == request_id))).one()

    async def runs(self, request_id: uuid.UUID | None = None) -> list[sa.Row]:
        statement = sa.select(RUNS).order_by(RUNS.c.attempt_no)
        if request_id is not None:
            statement = statement.where(RUNS.c.request_id == request_id)
        async with AsyncSession(self.engine) as db:
            return list((await db.execute(statement)).all())

    async def bars(self) -> list[sa.Row]:
        async with AsyncSession(self.engine) as db:
            return list((await db.execute(sa.select(FAKE_BARS).order_by(FAKE_BARS.c.timestamp))).all())

    async def instance(self, instance_id: str) -> sa.Row | None:
        async with AsyncSession(self.engine) as db:
            return (await db.execute(sa.select(INSTANCES).where(INSTANCES.c.instance_id == instance_id))).one_or_none()

    async def snapshot(self) -> tuple:
        """Everything a delivery may write, besides the instance sighting (which commits first by design)."""
        async with AsyncSession(self.engine) as db:
            requests = (await db.execute(sa.select(REQUESTS).order_by(REQUESTS.c.id))).all()
            entries = (await db.execute(sa.select(ENTRIES).order_by(ENTRIES.c.id))).all()
        return (
            [tuple(row) for row in requests],
            [tuple(row) for row in await self.runs()],
            [tuple(row) for row in await self.bars()],
            [tuple(row) for row in entries],
        )

    async def deliver(self, attempt) -> DeliverAttemptResponse:
        return await self.intake.deliver(attempt)

    async def refused(self, attempt, reason: Reason = Reason.INVALID_REQUEST) -> InvalidRequestError:
        """Deliver, expect a refusal, and prove nothing was written and nothing published."""
        before = await self.snapshot()
        published = len(self.published)
        with pytest.raises(InvalidRequestError) as caught:
            await self.deliver(attempt)
        assert caught.value.reason is reason, caught.value
        assert await self.snapshot() == before, 'a refused delivery wrote something'
        assert len(self.published) == published, 'a refused delivery published an event'
        return caught.value


@pytest.fixture
def store(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Store:
    return Store(engine, monkeypatch)


def _app_records(caplog: pytest.LogCaptureFixture, level: int | None = None) -> list[logging.LogRecord]:
    """The application's own log records (not the driver's statement echo, which is not the intake's to word)."""
    return [
        record
        for record in caplog.records
        if record.name.startswith('data.store') and (level is None or record.levelno == level)
    ]


def _logs_hold_no_secret(caplog: pytest.LogCaptureFixture) -> None:
    for record in _app_records(caplog):
        message = record.getMessage()
        assert LABEL not in message, message
        assert QUOTA_KEY not in message, message
        assert VENDOR_ASSET_ID not in message, message


# --------------------------------------------------------------------------------------------- a success


async def test_a_succeeded_bars_delivery_writes_bars_attempt_row_count_spend_and_state_and_answers_final(store):
    request_id = await store.request()
    entry_id = (await store.request_row(request_id)).dataset_id
    attempt = report(request_id, parts=[bar_page(0, 1), bar_page(2)])

    answer = await store.deliver(attempt)

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    # The bars: under the REQUEST's dataset, symbol and source, the HEADER's tape, one batch per page, the chunk size.
    assert [len(batch.dataset[DataType.MARKET_ACTIVITY]) for batch, _ in store.batches] == [2, 1]
    for batch, chunk in store.batches:
        assert (batch.dataset_id, batch.asset_symbol, batch.source) == (entry_id, 'VFV', DataSource.ALPACA_API)
        assert (batch.granularity, batch.feed, chunk) == (Granularity.ONE_DAY, Feed.IEX, CHUNK)
    assert [row.timestamp for row in await store.bars()] == [BAR_START + timedelta(days=d) for d in (0, 1, 2)]
    # The attempt row.
    (run,) = await store.runs(request_id)
    assert (run.attempt_no, run.instance_id, run.quota_key, run.verdict) == (1, INSTANCE_A, QUOTA_KEY, 'FINAL')
    assert (run.state, run.trigger, run.bars_written) == (IngestRunState.SUCCEEDED, IngestRunTrigger.REQUEST, 3)
    assert (run.dataset_id, run.request_id, run.actor) == (entry_id, request_id, LABEL)
    assert (run.asset_symbol, run.asset_type, run.data_type) == ('VFV', AssetType.STOCK, DataType.MARKET_ACTIVITY)
    assert (run.source, run.granularity, run.feed) == (DataSource.ALPACA_API, Granularity.ONE_DAY, Feed.IEX)
    assert run.update_type == UpdateType.DAILY.value
    assert (run.requested_start, run.requested_end) == (START, START + timedelta(days=30))
    assert (run.vendor_calls, run.vendor_pages, run.vendor_throttled_429, run.vendor_retries) == (3, 2, 1, 1)
    assert (run.vendor_first_call_at, run.vendor_last_call_at) == (FIRST_CALL, LAST_CALL)
    assert (run.vendor_reported_limit, run.vendor_reported_remaining) == (200, 150)
    assert (run.started_at, run.finished_at, run.error_message) == (FIRST_CALL, AS_OF, None)
    assert run.vendor_asset_id == VENDOR_ASSET_ID, 'P49: the header id, as received, on the attempt row'
    # The request.
    row = await store.request_row(request_id)
    assert (row.attempts, row.spent_calls, row.spent_pages, row.spent_throttled_429, row.spent_retries) == (
        1,
        3,
        2,
        1,
        1,
    )
    assert (row.state, row.failure_cause) == (IngestRequestState.SUCCEEDED, None)
    assert 'listing_id' not in row._mapping, 'P49 dropped the never-written listing_id'
    assert row.finished_at is not None
    # After the commit: the run and the dataset change.
    run_event, dataset_event = store.published
    assert isinstance(run_event, IngestRunChanged) and run_event.run.id == run.id
    assert dataset_event == DatasetChanged(entry_id, DatasetChange.UPDATED)


async def test_the_vendor_asset_id_is_stored_on_the_attempt_row_only_and_served_range_nowhere(store):
    """P49 (tj-7zjov1.143): the header id lands in ingest_run.vendor_asset_id and in no other column of either row.

    served_range is still persisted nowhere until P34c (ruling 2): no column of the attempt row or the request holds
    either of its instants.
    """
    request_id = await store.request()
    served = ServedRange(start=BAR_START + timedelta(hours=1, minutes=7), end=AS_OF + timedelta(days=3, minutes=11))
    await store.deliver(with_trailer(report(request_id), served_range=served))

    (run,) = await store.runs(request_id)
    request_row = await store.request_row(request_id)
    holding = [
        (table, key)
        for table, row in (('ingest_run', run), ('ingest_request', request_row))
        for key, value in row._mapping.items()
        if value == VENDOR_ASSET_ID
    ]
    assert holding == [('ingest_run', 'vendor_asset_id')]
    for row in (run, request_row):
        values = list(row._mapping.values())
        assert served.start not in values and served.end not in values, 'served_range has no column until P34c'


@pytest.mark.parametrize(
    'outcome', [DeliveryOutcome.SUCCEEDED, 'empty', DeliveryOutcome.FAILED], ids=['succeeded', 'empty', 'failed']
)
async def test_every_bars_attempt_row_stores_the_header_vendor_asset_id_as_received(store, outcome):
    """As received, on whatever outcome writes an attempt row: opaque, not trimmed, not case-folded."""
    request_id = await store.request(dataset=outcome != 'empty')
    received = '  Vendor-Asset/ÄBC 01  '
    attempt = report(request_id, parts=[BarPage(bars=[])]) if outcome == 'empty' else report(request_id, outcome)
    await store.deliver(with_header(attempt, vendor_asset_id=received))

    (run,) = await store.runs(request_id)
    assert run.vendor_asset_id == received


async def test_an_empty_header_vendor_asset_id_is_stored_null_never_an_empty_string(store):
    request_id = await store.request()

    await store.deliver(with_header(report(request_id), vendor_asset_id=''))

    (run,) = await store.runs(request_id)
    assert run.vendor_asset_id is None


async def test_a_repeat_delivery_does_not_change_the_stored_vendor_asset_id(store):
    """The duplicate is answered from the stored row: a redelivery naming another id, or none, writes nothing."""
    request_id = await store.request()
    await store.deliver(report(request_id, DeliveryOutcome.FAILED))

    for other in ('another-vendor-asset', ''):
        await store.deliver(with_header(report(request_id, DeliveryOutcome.FAILED), vendor_asset_id=other))

    (run,) = await store.runs(request_id)
    assert run.vendor_asset_id == VENDOR_ASSET_ID


async def test_each_attempt_keeps_the_vendor_asset_id_its_own_header_carried(store):
    """Per attempt, not per request: a later attempt with another id, or none, leaves the earlier row as it was."""
    request_id = await store.request()

    await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    await store.deliver(with_header(report(request_id, DeliveryOutcome.FAILED, attempt_no=2), vendor_asset_id='v-2'))
    await store.deliver(with_header(report(request_id, attempt_no=3), vendor_asset_id=''))

    assert [run.vendor_asset_id for run in await store.runs(request_id)] == [VENDOR_ASSET_ID, 'v-2', None]


@pytest.mark.parametrize('kind', NON_BARS, ids=lambda kind: kind.lower())
@pytest.mark.parametrize('outcome', NO_CALL, ids=lambda outcome: outcome.lower())
async def test_an_accepted_non_bars_report_stores_no_vendor_asset_id(store, kind, outcome):
    """The only non-bars reports accepted today (EXPIRED, EXHAUSTED) write no attempt row, so nothing holds an id.

    A counted non-bars attempt is refused whole (test_a_counted_non_bars_attempt_is_refused_never_acknowledged_unwritten);
    the NULL a non-bars attempt row will carry once P18 records them is NewAttempt's default, pinned below.
    """
    request_id = await store.request(kind)

    await store.deliver(report(request_id, outcome, kind=kind))

    assert await store.runs() == []
    assert VENDOR_ASSET_ID not in list((await store.request_row(request_id))._mapping.values())


async def test_the_attempt_the_intake_builds_carries_the_header_id_and_the_published_record_reads_it(
    store, monkeypatch
):
    """NewAttempt carries the id to insert_attempt, and IngestRunRecord.from_row reads it back off the stored row."""
    built: list[ingest_delivery.NewAttempt] = []
    original = delivery_intake.insert_attempt

    async def spy(db, attempt):
        built.append(attempt)
        return await original(db, attempt)

    monkeypatch.setattr(delivery_intake, 'insert_attempt', spy)
    request_id = await store.request()

    await store.deliver(report(request_id))

    (attempt,) = built
    assert attempt.vendor_asset_id == VENDOR_ASSET_ID
    run_event = store.published[0]
    assert isinstance(run_event, IngestRunChanged) and run_event.run.vendor_asset_id == VENDOR_ASSET_ID
    (row,) = await store.runs(request_id)
    assert IngestRunRecord.from_row(row).vendor_asset_id == VENDOR_ASSET_ID


async def test_from_row_reads_a_null_vendor_asset_id_as_none(store):
    request_id = await store.request()
    await store.deliver(with_header(report(request_id), vendor_asset_id=''))

    (row,) = await store.runs(request_id)
    assert IngestRunRecord.from_row(row).vendor_asset_id is None


def test_new_attempt_carries_a_vendor_asset_id_that_defaults_to_none():
    """A caller that names none (a non-bars attempt, once P18 records them) stores NULL, never an empty string."""
    (declared,) = [f for f in dataclasses.fields(ingest_delivery.NewAttempt) if f.name == 'vendor_asset_id']
    assert declared.default is None
    (read,) = [f for f in dataclasses.fields(IngestRunRecord) if f.name == 'vendor_asset_id']
    assert read.default is None


async def test_insert_attempt_stores_null_when_the_attempt_names_no_vendor_asset_id(store, monkeypatch):
    """A NewAttempt built without naming the field, as a non-bars caller will, writes NULL into the column."""
    original = delivery_intake.insert_attempt

    async def without_id(db, attempt):
        named = {f.name: getattr(attempt, f.name) for f in dataclasses.fields(attempt) if f.name != 'vendor_asset_id'}
        return await original(db, ingest_delivery.NewAttempt(**named))

    monkeypatch.setattr(delivery_intake, 'insert_attempt', without_id)
    request_id = await store.request()

    await store.deliver(report(request_id))

    (run,) = await store.runs(request_id)
    assert run.vendor_asset_id is None


async def test_a_success_with_no_bars_is_empty_needs_no_dataset_and_writes_none(store):
    request_id = await store.request(dataset=False)

    answer = await store.deliver(report(request_id, parts=[BarPage(bars=[])]))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert store.batches == [] and await store.bars() == []
    (run,) = await store.runs(request_id)
    assert (run.state, run.bars_written, run.verdict) == (IngestRunState.EMPTY, 0, 'FINAL')
    assert (await store.request_row(request_id)).state is IngestRequestState.EMPTY
    assert [type(event) for event in store.published] == [IngestRunChanged], 'no bars, no dataset change'


async def test_the_first_attempt_row_is_request_and_a_later_one_retry(store):
    request_id = await store.request()

    await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    await store.deliver(report(request_id, attempt_no=2))

    assert [run.trigger for run in await store.runs(request_id)] == [IngestRunTrigger.REQUEST, IngestRunTrigger.RETRY]
    assert (await store.request_row(request_id)).attempts == 2


async def test_spend_sums_across_attempts(store):
    request_id = await store.request()

    await store.deliver(report(request_id, DeliveryOutcome.FAILED, vendor_stats=stats(1, 1, 0, 0)))
    await store.deliver(report(request_id, attempt_no=2, vendor_stats=stats(4, 3, 2, 1)))

    row = await store.request_row(request_id)
    assert (row.spent_calls, row.spent_pages, row.spent_throttled_429, row.spent_retries) == (5, 4, 2, 1)


async def test_logs_name_ids_and_never_the_label_quota_key_or_vendor_asset_id(store, caplog):
    caplog.set_level(logging.DEBUG, logger='data.store')
    request_id = await store.request()

    await store.deliver(report(request_id, DeliveryOutcome.FAILED, detail='503'))
    await store.deliver(report(request_id, attempt_no=2))

    assert any(str(request_id) in record.getMessage() for record in _app_records(caplog))
    _logs_hold_no_secret(caplog)


# --------------------------------------------------------------------------------------------- duplicates


async def test_a_repeated_success_is_answered_from_the_stored_row_and_writes_nothing(store):
    request_id = await store.request()
    first = await store.deliver(report(request_id))
    before, published, batches = await store.snapshot(), len(store.published), len(store.batches)

    again = await store.deliver(report(request_id))

    assert again == first == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert await store.snapshot() == before
    assert (len(store.published), len(store.batches)) == (published, batches)


async def test_a_redelivery_is_answered_by_the_stored_verdict_whatever_it_now_says(store):
    """The key is (request_id, attempt_no): the stored CONTINUE answers, even when the repeat claims a success."""
    request_id = await store.request()
    first = await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    before = await store.snapshot()

    again = await store.deliver(report(request_id))

    assert first == again == DeliverAttemptResponse(verdict=CONTINUE, attempt_count=1)
    assert await store.snapshot() == before
    assert store.batches == []


async def test_a_duplicate_is_answered_before_the_rules_so_it_is_never_refused(store):
    """The P32 gate note: a duplicate is answered from the stored row before any cross-field check."""
    request_id = await store.request()
    first = await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    before = await store.snapshot()

    again = await store.deliver(with_header(report(request_id, DeliveryOutcome.FAILED), quota_key='', feed=None))

    assert again == first
    assert await store.snapshot() == before


async def test_an_earlier_attempt_redelivered_late_carries_the_current_durable_count(store):
    request_id = await store.request()
    await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    await store.deliver(report(request_id, DeliveryOutcome.FAILED, attempt_no=2))
    before = await store.snapshot()

    again = await store.deliver(report(request_id, DeliveryOutcome.FAILED))

    assert again == DeliverAttemptResponse(verdict=CONTINUE, attempt_count=2)
    assert await store.snapshot() == before


async def test_an_attempt_row_with_no_verdict_is_never_guessed_into_an_acknowledgement(store):
    """A row this intake did not write (no verdict) must not be answered as acknowledged."""
    request_id = await store.request()
    await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    async with AsyncSession(store.engine) as db:
        await db.execute(sa.update(RUNS).where(RUNS.c.request_id == request_id).values(verdict=None))
        await db.commit()

    with pytest.raises(RuntimeError):
        await store.deliver(report(request_id, DeliveryOutcome.FAILED))


# --------------------------------------------------------------------------------------------- expired and exhausted


@pytest.mark.parametrize(
    ('outcome', 'cause'),
    [
        (DeliveryOutcome.EXPIRED, IngestRequestFailureCause.EXPIRED),
        (DeliveryOutcome.EXHAUSTED, IngestRequestFailureCause.ATTEMPTS_EXHAUSTED),
    ],
)
@pytest.mark.parametrize('state', OPEN)
async def test_expired_and_exhausted_fail_the_request_with_no_attempt_row_and_the_count_unchanged(
    store, outcome, cause, state
):
    request_id = await store.request(state=state)
    # Two attempts already counted, with their spend: an EXHAUSTED report numbers count + 1 and adds nothing.
    await store.set_request(request_id, attempts=2, spent_calls=5, spent_pages=4, spent_throttled_429=1)

    answer = await store.deliver(report(request_id, outcome, attempt_no=3))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=2)
    assert await store.runs() == []
    row = await store.request_row(request_id)
    assert (row.state, row.failure_cause) == (IngestRequestState.FAILED, cause.value)
    assert row.finished_at is not None
    assert (row.attempts, row.spent_calls, row.spent_pages, row.spent_throttled_429, row.spent_retries) == (
        2,
        5,
        4,
        1,
        0,
    )
    assert store.batches == [] and store.published == []


@pytest.mark.parametrize('outcome', NO_CALL)
@pytest.mark.parametrize('kind', [IngestRequestKind.ASSET_LIST, IngestRequestKind.CORPORATE_ACTIONS])
async def test_expired_and_exhausted_are_accepted_for_every_kind(store, outcome, kind):
    request_id = await store.request(kind)

    answer = await store.deliver(report(request_id, outcome, kind=kind))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=0)
    assert (await store.request_row(request_id)).state is IngestRequestState.FAILED
    assert await store.runs() == []


@pytest.mark.parametrize('outcome', NO_CALL)
async def test_a_repeated_expired_or_exhausted_report_is_final_and_writes_nothing(store, outcome):
    request_id = await store.request()
    await store.deliver(report(request_id, outcome))
    before = await store.snapshot()

    again = await store.deliver(report(request_id, outcome))

    assert again == DeliverAttemptResponse(verdict=FINAL, attempt_count=0)
    assert await store.snapshot() == before


@pytest.mark.parametrize('outcome', NO_CALL)
@pytest.mark.parametrize('finished', [IngestRequestState.SUCCEEDED, IngestRequestState.EMPTY])
async def test_a_late_no_call_report_never_rewrites_a_finished_requests_verdict(store, outcome, finished):
    request_id = await store.request()
    await store.deliver(report(request_id, parts=[] if finished is IngestRequestState.EMPTY else None))
    before = await store.snapshot()

    answer = await store.deliver(report(request_id, outcome, attempt_no=2))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert await store.snapshot() == before
    assert (await store.request_row(request_id)).state is finished


# --------------------------------------------------------------------------------------------- refusals write nothing


@pytest.mark.parametrize(
    ('kind', 'outcome', 'breaks'),
    [pytest.param(kind, outcome, breaks, id=f'{rule}-{name}') for rule, name, kind, outcome, breaks in RED_CASES],
)
async def test_every_cross_field_refusal_writes_nothing(store, kind, outcome, breaks):
    request_id = await store.request(kind)

    await store.refused(breaks(report(request_id, outcome, kind=kind)))

    assert store.batches == []


@pytest.mark.parametrize('state', OPEN)
async def test_a_bars_expired_or_exhausted_report_with_no_feed_is_refused_not_tolerated(store, state):
    """P36b: ingest's interim empty feed on these reports is refused, so the request is NOT failed by it."""
    request_id = await store.request(state=state)
    for outcome in NO_CALL:
        await store.refused(with_header(report(request_id, outcome), feed=None))
    assert (await store.request_row(request_id)).state is state


async def test_a_request_the_store_does_not_hold_is_not_found(store):
    await store.request()
    await store.refused(report(uuid.uuid4()), Reason.NOT_FOUND)


async def test_a_request_id_that_is_not_a_uuid_is_refused_before_the_instance_is_noted(store):
    await store.refused(with_header(report(uuid.uuid4()), request_id='not-a-uuid'))
    assert await store.instance(INSTANCE_A) is None


async def test_the_instance_sighting_commits_even_when_the_delivery_is_refused(store):
    """ORDERING: the marker is true whether or not the delivery succeeds."""
    await store.refused(report(uuid.uuid4()), Reason.NOT_FOUND)
    assert await store.instance(INSTANCE_A) is not None


# --------------------------------------------------------------------------------------------- the bars arm


async def test_a_bar_from_another_tape_than_the_header_is_refused_and_nothing_kept(store):
    request_id = await store.request()
    attempt = report(request_id, parts=[bar_page(0), BarPage(bars=[bar(1), bar(2, feed=Feed.SIP)])])

    error = await store.refused(attempt)

    assert 'tape' in error.detail
    assert await store.bars() == [], 'page 1 was rolled back with the rest'


@pytest.mark.parametrize(
    'bad_bar', [bar(1, trade_count=None), bar(1, volume=10.5)], ids=['no-trade-count', 'fractional-volume']
)
async def test_a_bar_the_stored_shape_cannot_hold_is_refused_not_given_a_made_up_value(store, bad_bar):
    request_id = await store.request()
    await store.refused(report(request_id, parts=[bar_page(0), BarPage(bars=[bad_bar])]))


async def test_bars_for_a_request_with_no_dataset_are_refused(store):
    request_id = await store.request(dataset=False)
    error = await store.refused(report(request_id))
    assert error.detail == 'the request has no dataset to store these bars under'


async def test_c1_bars_for_a_dataset_entry_that_is_gone_are_refused_and_nothing_written(store):
    """C1: bars carry no foreign key, so an entry deleted while the attempt was in flight refuses the bars."""
    request_id = await store.request()
    entry_id = (await store.request_row(request_id)).dataset_id
    async with AsyncSession(store.engine) as db:
        await db.execute(sa.delete(ENTRIES).where(ENTRIES.c.id == entry_id))
        await db.commit()

    error = await store.refused(report(request_id))

    assert error.detail == 'the dataset this request writes to no longer exists'
    assert store.batches == [], 'no bar reached the writer'


async def test_c1_the_entry_is_locked_before_the_first_bar_is_written(store, monkeypatch):
    request_id = await store.request()
    order: list[str] = []
    real_lock = delivery_intake.lock_dataset_entry
    real_writer = delivery_intake.write_market_activity_in_transaction

    async def lock(db, dataset_id):
        order.append('lock')
        return await real_lock(db, dataset_id)

    async def writer(db, batch, requested_chunk_size=None):
        order.append('write')
        return await real_writer(db, batch, requested_chunk_size)

    monkeypatch.setattr(delivery_intake, 'lock_dataset_entry', lock)
    monkeypatch.setattr(delivery_intake, 'write_market_activity_in_transaction', writer)

    await store.deliver(report(request_id, parts=[bar_page(0), bar_page(1)]))

    assert order == ['lock', 'write', 'write']


class _Captured:
    """A session stand-in that records the statement and answers one row."""

    def __init__(self):
        self.statements: list[object] = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self

    def first(self):
        return (uuid.uuid4(),)

    def scalar_one_or_none(self):
        return None


def _postgres_sql(statement) -> str:
    return ' '.join(str(statement.compile(dialect=postgresql.dialect())).split())


async def test_c1_lock_dataset_entry_selects_the_entry_for_update():
    session = _Captured()
    assert await ingest_delivery.lock_dataset_entry(session, uuid.uuid4()) is True
    (statement,) = session.statements
    sql = _postgres_sql(statement)
    assert sql.startswith('SELECT store_dataset_entry.id FROM store_dataset_entry WHERE store_dataset_entry.id =')
    assert sql.endswith('FOR UPDATE')


async def test_lock_request_selects_the_request_for_update():
    session = _Captured()
    assert await ingest_delivery.lock_request(session, uuid.uuid4()) is None
    (statement,) = session.statements
    sql = _postgres_sql(statement)
    assert 'FROM ingest_request WHERE ingest_request.id =' in sql
    assert sql.endswith('FOR UPDATE')


async def test_insert_attempt_names_the_partial_index_with_its_predicate():
    """Postgres refuses a partial unique index as arbiter without its WHERE (epic note 62); PG1 runs it."""

    class _Insert(_Captured):
        def one_or_none(self):
            return None

    session = _Insert()
    request = request_crud.IngestRequestRecord.from_row(
        IngestRequest(
            id=uuid.uuid4(),
            kind=IngestRequestKind.BARS,
            source=DataSource.ALPACA_API,
            client_label=LABEL,
            state=IngestRequestState.QUEUED,
            attempts=0,
            spent_calls=0,
            spent_pages=0,
            spent_throttled_429=0,
            spent_retries=0,
            best_effort=False,
            created_at=START,
        )
    )
    attempt = ingest_delivery.NewAttempt(
        request=request,
        attempt_no=1,
        instance_id=INSTANCE_A,
        quota_key=QUOTA_KEY,
        verdict='FINAL',
        state=IngestRunState.SUCCEEDED,
        trigger=IngestRunTrigger.REQUEST,
        bars_written=0,
        feed=Feed.IEX,
        stats=None,
        started_at=None,
        finished_at=None,
        error_message=None,
        label=ingest_delivery.AttemptLabel(
            'VFV', AssetType.STOCK, DataType.MARKET_ACTIVITY, Granularity.ONE_DAY, UpdateType.DAILY
        ),
    )

    assert await ingest_delivery.insert_attempt(session, attempt) is None
    (statement,) = session.statements
    sql = _postgres_sql(statement)
    assert (
        'ON CONFLICT (request_id, attempt_no) WHERE request_id IS NOT NULL AND attempt_no IS NOT NULL DO NOTHING' in sql
    )
    assert ' RETURNING ' in sql


# --------------------------------------------------------------------------------------------- the other arms


@pytest.mark.parametrize(
    ('kind', 'outcome', 'parts'),
    [
        (IngestRequestKind.ASSET_LIST, DeliveryOutcome.SUCCEEDED, [asset_records()]),
        (IngestRequestKind.ASSET_LIST, DeliveryOutcome.SUCCEEDED, []),
        (IngestRequestKind.ASSET_LIST, DeliveryOutcome.FAILED, []),
        (IngestRequestKind.CORPORATE_ACTIONS, DeliveryOutcome.SUCCEEDED, [action_records()]),
        (IngestRequestKind.CORPORATE_ACTIONS, DeliveryOutcome.SUCCEEDED, []),
        (IngestRequestKind.CORPORATE_ACTIONS, DeliveryOutcome.FAILED, []),
    ],
    ids=[
        'asset-list-records',
        'asset-list-empty',
        'asset-list-failed',
        'actions-records',
        'actions-empty',
        'actions-failed',
    ],
)
async def test_a_counted_non_bars_attempt_is_refused_never_acknowledged_unwritten(store, kind, outcome, parts):
    """Decision 4 rule (2) and ruling 1: a counted non-bars attempt is refused with nothing written.

    An asset list cannot be fetched again, and no attempt row can be written for these kinds until P18.
    """
    request_id = await store.request(kind)
    await store.refused(report(request_id, outcome, kind=kind, parts=parts))
    assert (await store.request_row(request_id)).state is IngestRequestState.QUEUED


# --------------------------------------------------------------------------------------------- the verdict, live


@pytest.mark.parametrize('error_class', [DeliveryErrorClass.TRANSIENT, DeliveryErrorClass.RATE_LIMITED])
@pytest.mark.parametrize('state', OPEN)
async def test_a_retryable_failure_continues_counts_spends_and_queues_the_request_under_the_live_instance(
    store, error_class, state
):
    request_id = await store.request(state=state)
    await store.set_request(request_id, instance_id=None)

    answer = await store.deliver(report(request_id, DeliveryOutcome.FAILED, error_class=error_class, detail='503'))

    assert answer == DeliverAttemptResponse(verdict=CONTINUE, attempt_count=1)
    row = await store.request_row(request_id)
    assert (row.state, row.instance_id, row.finished_at) == (IngestRequestState.QUEUED, INSTANCE_A, None)
    assert (row.attempts, row.spent_calls) == (1, 3)
    (run,) = await store.runs(request_id)
    assert (run.state, run.verdict, run.error_message, run.bars_written) == (
        IngestRunState.FAILED,
        'CONTINUE',
        '503',
        0,
    )


async def test_a_permanent_refusal_is_final_and_fails_the_request_permanent(store):
    request_id = await store.request()

    answer = await store.deliver(
        report(request_id, DeliveryOutcome.FAILED, error_class=DeliveryErrorClass.PERMANENT, detail='symbol unknown')
    )

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    row = await store.request_row(request_id)
    assert (row.state, row.failure_cause, row.failure_detail) == (
        IngestRequestState.FAILED,
        'PERMANENT',
        'symbol unknown',
    )
    assert row.finished_at is not None
    assert [run.verdict for run in await store.runs()] == ['FINAL']


async def test_the_last_attempt_is_final_and_fails_the_request_attempts_exhausted(store):
    request_id = await store.request()

    answer = await store.deliver(report(request_id, DeliveryOutcome.FAILED, last_attempt=True))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    row = await store.request_row(request_id)
    assert (row.state, row.failure_cause) == (IngestRequestState.FAILED, 'ATTEMPTS_EXHAUSTED')


@pytest.mark.parametrize('finished', [IngestRequestState.SUCCEEDED, IngestRequestState.FAILED])
async def test_a_late_counted_report_on_a_finished_request_is_written_and_final_and_keeps_the_verdict(store, finished):
    request_id = await store.request()
    first = (
        report(request_id)
        if finished is IngestRequestState.SUCCEEDED
        else report(request_id, DeliveryOutcome.FAILED, error_class=DeliveryErrorClass.PERMANENT)
    )
    await store.deliver(first)
    finished_row = await store.request_row(request_id)

    answer = await store.deliver(report(request_id, DeliveryOutcome.FAILED, attempt_no=2))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=2)
    row = await store.request_row(request_id)
    assert (row.state, row.failure_cause, row.finished_at) == (
        finished,
        finished_row.failure_cause,
        finished_row.finished_at,
    )
    assert (row.attempts, row.spent_calls) == (2, 6), 'the attempt happened and cost what it cost'
    assert [run.verdict for run in await store.runs()] == ['FINAL', 'FINAL']


# --------------------------------------------------------------------------------------------- rule h accepted


async def test_a_failed_report_without_stats_is_written_with_null_vendor_columns_and_one_warning(store, caplog):
    caplog.set_level(logging.DEBUG, logger='data.store')
    request_id = await store.request()
    await store.set_request(request_id, spent_calls=4, spent_pages=2)

    answer = await store.deliver(
        with_trailer(
            report(request_id, DeliveryOutcome.FAILED, error_class=DeliveryErrorClass.PERMANENT), vendor_stats=None
        )
    )

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    (run,) = await store.runs(request_id)
    spend = {name: run._mapping[name] for name in SPEND_COLUMNS}
    assert spend == dict.fromkeys(SPEND_COLUMNS), 'unknown spend is NULL, never zero'
    # The vendor asset id is not spend: the header still carried it, so it is stored (P49).
    assert run.vendor_asset_id == VENDOR_ASSET_ID
    row = await store.request_row(request_id)
    assert (row.attempts, row.spent_calls, row.spent_pages, row.spent_throttled_429, row.spent_retries) == (
        1,
        4,
        2,
        0,
        0,
    )
    warnings = _app_records(caplog, logging.WARNING)
    assert len(warnings) == 1, [record.getMessage() for record in warnings]
    message = warnings[0].getMessage()
    assert str(request_id) in message and 'Attempt 1 ' in message and 'spend unknown' in message
    _logs_hold_no_secret(caplog)


def test_every_vendor_column_is_spend_or_the_vendor_asset_id():
    """A new vendor_* column must be classified here, or the NULL-spend check above silently stops covering it."""
    vendor = {column.name for column in RUNS.columns if column.name.startswith('vendor_')}
    assert vendor == {*SPEND_COLUMNS, 'vendor_asset_id'}


async def test_a_failed_report_with_stats_logs_no_warning(store, caplog):
    caplog.set_level(logging.DEBUG, logger='data.store')
    request_id = await store.request()
    await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    assert [r for r in _app_records(caplog) if r.levelno >= logging.WARNING] == []


# --------------------------------------------------------------------------------------------- rollback


async def test_a_failure_after_the_attempt_row_rolls_back_the_bars_the_row_and_the_count(store, monkeypatch):
    request_id = await store.request()
    before = await store.snapshot()

    async def failing(db, request_id, outcome):
        raise RuntimeError('the count update failed')

    monkeypatch.setattr(delivery_intake, 'apply_outcome', failing)
    with pytest.raises(RuntimeError):
        await store.deliver(report(request_id))

    assert await store.snapshot() == before
    assert len(store.batches) == 1, 'the bars were written in the transaction, then rolled back'
    assert store.published == [], 'nothing is published for a delivery that did not commit'


async def test_a_refused_second_page_rolls_back_the_first(store):
    request_id = await store.request()
    store.fail_on_write = 2

    await store.refused(report(request_id, parts=[bar_page(0), bar_page(1)]), Reason.DUPLICATE_BAR_TIMESTAMP)

    assert await store.bars() == []


async def test_a_redelivery_after_a_rollback_is_written_once(store, monkeypatch):
    request_id = await store.request()
    real = delivery_intake.apply_outcome
    calls = {'n': 0}

    async def flaky(db, request_id, outcome):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('lost')
        return await real(db, request_id, outcome)

    monkeypatch.setattr(delivery_intake, 'apply_outcome', flaky)
    with pytest.raises(RuntimeError):
        await store.deliver(report(request_id))
    answer = await store.deliver(report(request_id))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert len(await store.runs()) == 1 and len(await store.bars()) == 2


# --------------------------------------------------------------------------------------------- instances


async def test_register_instance_records_supersedes_and_marks_queued_requests_not_delivered(store):
    await store.register(INSTANCE_A)
    held_by_a = await store.request()

    await store.register(INSTANCE_B)

    assert (await store.instance(INSTANCE_A)).superseded_at is not None
    assert (await store.instance(INSTANCE_B)).superseded_at is None
    assert (await store.request_row(held_by_a)).state is IngestRequestState.NOT_DELIVERED


async def test_register_instance_again_is_idempotent(store):
    await store.register(INSTANCE_A)
    await store.register(INSTANCE_B)
    held = await store.request()
    await store.set_request(held, instance_id=INSTANCE_B)
    a_superseded = (await store.instance(INSTANCE_A)).superseded_at

    await store.register(INSTANCE_B)
    await store.register(INSTANCE_A)

    assert (await store.instance(INSTANCE_B)).superseded_at is None, 'a late old id never supersedes the live one'
    assert (await store.instance(INSTANCE_A)).superseded_at == a_superseded
    assert (await store.request_row(held)).state is IngestRequestState.QUEUED


async def _superseded_a(store: Store) -> uuid.UUID:
    """Instance A held a request, then B registered: A is superseded and its request NOT_DELIVERED."""
    await store.register(INSTANCE_A)
    request_id = await store.request()
    await store.register(INSTANCE_B)
    assert (await store.request_row(request_id)).state is IngestRequestState.NOT_DELIVERED
    return request_id


@pytest.mark.parametrize('last_attempt', [False, True])
@pytest.mark.parametrize('error_class', [DeliveryErrorClass.TRANSIENT, DeliveryErrorClass.RATE_LIMITED])
async def test_a_superseded_instances_retryable_failure_is_written_final_and_leaves_the_request(
    store, error_class, last_attempt
):
    request_id = await _superseded_a(store)

    answer = await store.deliver(
        report(request_id, DeliveryOutcome.FAILED, error_class=error_class, last_attempt=last_attempt)
    )

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    row = await store.request_row(request_id)
    assert (row.state, row.instance_id, row.finished_at) == (IngestRequestState.NOT_DELIVERED, INSTANCE_A, None)
    assert (row.attempts, row.spent_calls) == (1, 3)
    (run,) = await store.runs(request_id)
    assert (run.instance_id, run.verdict) == (INSTANCE_A, 'FINAL')


@pytest.mark.parametrize(
    ('attempt_kwargs', 'state'),
    [
        ({}, IngestRequestState.SUCCEEDED),
        ({'outcome': DeliveryOutcome.FAILED, 'error_class': DeliveryErrorClass.PERMANENT}, IngestRequestState.FAILED),
    ],
    ids=['success', 'permanent'],
)
async def test_a_superseded_instances_conclusive_report_finishes_the_request(store, attempt_kwargs, state):
    request_id = await _superseded_a(store)

    answer = await store.deliver(report(request_id, **attempt_kwargs))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert (await store.request_row(request_id)).state is state


async def test_c3_the_live_instance_reusing_a_number_another_instance_recorded_is_refused_and_logged(store, caplog):
    caplog.set_level(logging.DEBUG, logger='data.store')
    await store.register(INSTANCE_A)
    request_id = await store.request()
    await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    await store.register(INSTANCE_B)

    error = await store.refused(report(request_id, DeliveryOutcome.FAILED, instance_id=INSTANCE_B))

    assert 'another ingest instance' in error.detail
    errors = _app_records(caplog, logging.ERROR)
    assert len(errors) == 1
    message = errors[0].getMessage()
    for expected in (str(request_id), 'Attempt 1 ', INSTANCE_A, INSTANCE_B, 'live'):
        assert expected in message, message
    _logs_hold_no_secret(caplog)


async def test_c3_a_superseded_instance_reusing_a_number_the_live_one_recorded_is_answered_final_and_logged(
    store, caplog
):
    caplog.set_level(logging.DEBUG, logger='data.store')
    await store.register(INSTANCE_A)
    request_id = await store.request()
    await store.register(INSTANCE_B)
    await store.deliver(report(request_id, DeliveryOutcome.FAILED, instance_id=INSTANCE_B))
    before = await store.snapshot()

    answer = await store.deliver(report(request_id, DeliveryOutcome.FAILED, instance_id=INSTANCE_A))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert await store.snapshot() == before
    (run,) = await store.runs(request_id)
    assert (run.instance_id, run.verdict) == (INSTANCE_B, 'CONTINUE'), 'the live instance record stands'
    errors = _app_records(caplog, logging.ERROR)
    assert len(errors) == 1
    message = errors[0].getMessage()
    for expected in (str(request_id), 'Attempt 1 ', INSTANCE_A, INSTANCE_B, 'superseded'):
        assert expected in message, message
    _logs_hold_no_secret(caplog)


async def test_a_redelivery_from_the_same_instance_logs_no_error(store, caplog):
    caplog.set_level(logging.DEBUG, logger='data.store')
    request_id = await store.request()
    await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    assert [r for r in _app_records(caplog) if r.levelno >= logging.ERROR] == []
