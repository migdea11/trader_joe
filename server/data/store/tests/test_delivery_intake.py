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
    attempt that writes one, an empty id as NULL; a repeat delivery changes nothing;
  * P34c (tj-7zjov1.123): a SUCCEEDED report's served_range is stored on its attempt row (served_start, served_end)
    and NULL on every other; the first attempt row takes the request's trigger (NULL reads as REQUEST) and every later
    one is RETRY; a counted FAILED report with no vendor stats adds 1 to ingest_request.spend_unknown_attempts.
  * P35 (tj-7zjov1.28, architect notes A, E, G and H, ADR tj-26jt9w addendum 14): THE ENTRY IS WRITTEN ON THE FIRST
    DELIVERED ATTEMPT from the request row and the header feed (expiry, expiry type, collect from the row; a NULL
    expiry type is refused INVALID_REQUEST, never BULK, nothing written); a request with a dataset_id writes only the
    entry's updated_at; a delivery to a GONE or STOPPED entry is DROPPED (attempt row EMPTY, request EMPTY with cause
    DISCARDED, FINAL, no bar, no dataset event); a SUCCEEDED bars report without a served range is refused (note G);
    a duplicate whose stored verdict was CONTINUE is answered FINAL once the request is no longer open (note E).

TIER, AND WHAT SQLITE CANNOT PROVE. The intake's own statements run on SQLite (aiosqlite) through the module's
own_session, pointed at the test engine. The bar upsert is Postgres-only (ON CONFLICT ON CONSTRAINT), so the bars
arm's writer is replaced by a stand-in that inserts into a scratch table ON THE SAME SESSION: what reaches it, and
whether it is rolled back, is real; the upsert itself is tests/system/test_delivery_intake_pg.py's. The ENTRY upsert
is Postgres-only for the same reason (ON CONFLICT ON CONSTRAINT), so it is replaced by SQLite's ON CONFLICT on the same
eleven identity columns, on the same session, journalling what the arm handed it; the real one runs in
test_delivery_intake_pg.py (PG10). SQLite cannot
show: FOR UPDATE (it ignores it, and has one writer, so no two deliveries ever interleave here), the partial unique
index (it builds a full one from the model, the predicate being postgresql_where), ON CONFLICT naming that partial
index WITH its predicate, the enum and ARRAY types, or a dataset entry deleted concurrently. Those are PG1 to PG8
in tests/system/test_delivery_intake_pg.py. What IS shown here about the locks is the statements' shape, compiled
for Postgres: lock_request and lock_dataset_entry carry FOR UPDATE, and the entry is locked before any bar.
"""

import dataclasses
import hashlib
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.sqlite import DATETIME
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from common.database.sql_alchemy_nullable_datetime import NullableDateTime
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import InvalidRequestError, Reason
from data.store.app import daily_refresh
from data.store.app.corporate_actions import observations_of
from data.store.app.database.crud import corporate_action as corporate_action_crud
from data.store.app.database.crud import ingest_delivery
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.crud.ingest_run import IngestRunRecord, last_accepted_vendor_asset_id
from data.store.app.database.models.corporate_action import CorporateAction
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import (
    IngestRequest,
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import (
    VENDOR_ASSET_CHANGED_REASON,
    IngestRun,
    IngestRunState,
    IngestRunTrigger,
)
from data.store.app.database.models.store_dataset_entry import DatasetStopCause, StoreDatasetEntry
from data.store.app.dataset_catalog import is_retired
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
    asset_records,
    bar,
    bar_page,
    report,
    stats,
    with_header,
    with_trailer,
)
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    CorporateActionRecord,
    CorporateActionRecords,
    DeliverAttemptResponse,
    DeliveryErrorClass,
    DeliveryOutcome,
    DeliveryVerdict,
    RegisterInstanceRequest,
)
from schemas.data_ingest.fetch_dataset import BarPage, ServedRange
from schemas.data_store.asset_dataset_store import AssetDatasetStoreCreate


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

LABEL = 'client-label-sentinel-0b7d'
START = datetime(2026, 2, 2, tzinfo=UTC)
CHUNK = 7
REQUESTS = IngestRequest.__table__
ACTIONS = CorporateAction.__table__
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
    # corporate_action (P18): SQLite has no JSONB and cannot parse the '{}'::jsonb default, so the COPY created here
    # holds identifiers as JSON with no server default (the arm always writes the column). The statements the arm runs
    # are built on the model's table and bind by name, so they are the production statements; the unique index on
    # (source, vendor_action_id, payload_hash) is the model's and is SQLite's ON CONFLICT arbiter too.
    actions = ACTIONS.to_metadata(metadata)
    actions.c.identifiers.type = sa.JSON()
    actions.c.identifiers.server_default = None
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

        # (entry, collect, preserve_lifecycle) as the bars arm handed each to the entry upsert.
        self.upserts: list[tuple[AssetDatasetStoreCreate, bool | None, bool]] = []

        async def upsert_entry(
            db: AsyncSession, entry: AssetDatasetStoreCreate, *, preserve_lifecycle: bool = False, collect=None
        ) -> uuid.UUID:
            """The stand-in entry upsert: SQLite's ON CONFLICT on the eleven identity columns, on the same session.

            A repeat sets expiry from the entry and collect only when explicit, as the real one does; the real one's
            own behaviour is PG10's. The CHECK on collect is the model's, so a wrong collect shape still fails here.
            """
            self.upserts.append((entry, collect, preserve_lifecycle))
            values = StoreDatasetEntry.get_fields(entry, exclude_none=True)
            resolved = None if entry.update_type is UpdateType.STATIC else (True if collect is None else collect)
            on_repeat: dict = {'expiry': values.get('expiry'), 'updated_at': sa.func.now()}
            if collect is not None and resolved is not None:
                on_repeat['collect'] = resolved
                if resolved is True:
                    # The Resume clear (P50), as the real one does (pinned by test_dataset_entry_identity.py's
                    # _bound_conflict and on Postgres): without it the model's CHECK refuses the repeat.
                    on_repeat['stopped_cause'] = None
            statement = (
                sqlite_insert(ENTRIES)
                .values(id=uuid.uuid4(), **values, collect=resolved, created_at=sa.func.now(), updated_at=sa.func.now())
                .on_conflict_do_update(index_elements=list(StoreDatasetEntry.NATURAL_KEY), set_=on_repeat)
                .returning(ENTRIES.c.id)
            )
            return (await db.execute(statement)).scalar_one()

        monkeypatch.setattr(delivery_intake, 'own_session', own_session)
        monkeypatch.setattr(delivery_intake, 'publish', self.published.append)
        monkeypatch.setattr(delivery_intake, 'write_market_activity_in_transaction', write_bars)
        monkeypatch.setattr(delivery_intake, 'upsert_entry_in_transaction', upsert_entry)
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

    async def entry(self, **overrides) -> uuid.UUID:
        entry_id = uuid.uuid4()
        values = {
            'id': entry_id,
            'owner': LABEL,
            'source': DataSource.ALPACA_API,
            'feed': Feed.IEX,
            'asset_symbol': 'VFV',
            'asset_type': AssetType.STOCK,
            'data_type': DataType.MARKET_ACTIVITY,
            'granularity': Granularity.ONE_DAY,
            'start': START,
            'end': START + timedelta(days=30),
            'expiry_type': ExpiryType.BULK,
            'update_type': UpdateType.DAILY,
            'collect': True,
            'created_at': START,
            'updated_at': START,
        }
        values.update(overrides)
        if values['end'] is None:
            # An open end is stored as the EPOCH sentinel; the ORM row takes the model value, not the schema's None.
            values['end'] = NullableDateTime.EPOCH
        async with AsyncSession(self.engine) as db:
            db.add(StoreDatasetEntry(**values))
            await db.commit()
        return entry_id

    async def entry_row(self, entry_id: uuid.UUID) -> sa.Row | None:
        async with AsyncSession(self.engine) as db:
            return (await db.execute(sa.select(ENTRIES).where(ENTRIES.c.id == entry_id))).one_or_none()

    async def entries(self) -> list[sa.Row]:
        async with AsyncSession(self.engine) as db:
            return list((await db.execute(sa.select(ENTRIES))).all())

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
            [tuple(row) for row in await self.actions()],
        )

    async def actions(self) -> list[sa.Row]:
        """The corporate_action rows, in insertion order (SQLite's rowid)."""
        statement = sa.select(ACTIONS).order_by(sa.literal_column('rowid'))
        async with AsyncSession(self.engine) as db:
            return list((await db.execute(statement)).all())

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


async def test_the_vendor_asset_id_and_the_served_range_are_stored_on_the_attempt_row_only_in_their_own_columns(store):
    """P49 (tj-7zjov1.143): the header id lands in ingest_run.vendor_asset_id and in no other column of either row.

    P34c (tj-7zjov1.123) REVERSED the served_range half of this test: it was stored nowhere, and now each instant is
    stored in its own column of the attempt row (start in served_start, end in served_end, never swapped) and in no
    other column of either row. The instants are chosen to equal no other value the report carries.
    """
    request_id = await store.request()
    served = ServedRange(start=BAR_START + timedelta(hours=1, minutes=7), end=AS_OF + timedelta(days=3, minutes=11))
    await store.deliver(with_trailer(report(request_id), served_range=served))

    (run,) = await store.runs(request_id)
    request_row = await store.request_row(request_id)
    rows = (('ingest_run', run), ('ingest_request', request_row))

    def holding(wanted) -> list[tuple[str, str]]:
        return [(table, key) for table, row in rows for key, value in row._mapping.items() if value == wanted]

    assert holding(VENDOR_ASSET_ID) == [('ingest_run', 'vendor_asset_id')]
    assert holding(served.start) == [('ingest_run', 'served_start')]
    assert holding(served.end) == [('ingest_run', 'served_end')]


@pytest.mark.parametrize('outcome', ['bars', 'empty'])
async def test_a_succeeded_report_stores_its_served_range_on_its_attempt_row(store, outcome):
    """With bars (row SUCCEEDED) or none (row EMPTY): the outcome on the wire is SUCCEEDED either way."""
    request_id = await store.request()
    served = ServedRange(start=BAR_START - timedelta(days=2), end=BAR_START + timedelta(days=9, hours=3))
    attempt = report(request_id, parts=[BarPage(bars=[])]) if outcome == 'empty' else report(request_id)

    await store.deliver(with_trailer(attempt, served_range=served))

    (run,) = await store.runs(request_id)
    assert run.state is (IngestRunState.EMPTY if outcome == 'empty' else IngestRunState.SUCCEEDED)
    assert (run.served_start, run.served_end) == (served.start, served.end)


@pytest.mark.parametrize('dataset', [True, False], ids=['refresh', 'first-attempt'])
@pytest.mark.parametrize('parts', ['bars', 'empty'])
async def test_g_a_succeeded_bars_report_without_a_served_range_is_refused_and_nothing_written(store, dataset, parts):
    """A SUCCEEDED bars report without a served range is refused and writes nothing.

    Note G (tj-7zjov1.28, replacing the P34c test that stored NULL): the waiter answers from the attempt row's
    served range, so a SUCCEEDED bars report with none is a sender defect, refused INVALID_REQUEST naming the rule.

    Nothing is written (no attempt row, no bar, no entry, the count unchanged), whether the request has its entry or
    would have created it, and with bars or none.
    """
    request_id = await store.request(dataset=dataset, expiry_type=ExpiryType.ROLLING)
    attempt = report(request_id, parts=[BarPage(bars=[])] if parts == 'empty' else None)

    error = await store.refused(with_trailer(attempt, served_range=None))

    assert 'served range' in error.detail and 'SUCCEEDED bars' in error.detail
    assert store.batches == [] and store.upserts == []
    row = await store.request_row(request_id)
    assert (row.attempts, row.state, row.dataset_id is None) == (0, IngestRequestState.QUEUED, not dataset)


@pytest.mark.parametrize('with_stats', [True, False], ids=['with-stats', 'no-stats'])
async def test_a_failed_attempt_row_holds_no_served_range(store, with_stats):
    """Rule (d) refuses a served range on FAILED, so the row of a failed attempt, then a later success, keep theirs apart."""
    request_id = await store.request()
    failed = report(request_id, DeliveryOutcome.FAILED)
    await store.deliver(failed if with_stats else with_trailer(failed, vendor_stats=None))
    served = ServedRange(start=BAR_START, end=BAR_START + timedelta(days=4))
    await store.deliver(with_trailer(report(request_id, attempt_no=2), served_range=served))

    first, second = await store.runs(request_id)
    assert (first.state, first.served_start, first.served_end) == (IngestRunState.FAILED, None, None)
    assert (second.served_start, second.served_end) == (served.start, served.end)


async def test_the_published_record_and_from_row_read_the_served_range(store, monkeypatch):
    """NewAttempt carries it to insert_attempt, and IngestRunRecord.from_row reads it back off the stored row."""
    built: list[ingest_delivery.NewAttempt] = []
    original = delivery_intake.insert_attempt

    async def spy(db, attempt):
        built.append(attempt)
        return await original(db, attempt)

    monkeypatch.setattr(delivery_intake, 'insert_attempt', spy)
    request_id = await store.request()
    served = ServedRange(start=BAR_START + timedelta(minutes=3), end=BAR_START + timedelta(days=6, minutes=5))

    await store.deliver(with_trailer(report(request_id), served_range=served))

    (attempt,) = built
    assert (attempt.served_start, attempt.served_end) == (served.start, served.end)
    run_event = store.published[0]
    assert isinstance(run_event, IngestRunChanged)
    assert (run_event.run.served_start, run_event.run.served_end) == (served.start, served.end)
    (row,) = await store.runs(request_id)
    record = IngestRunRecord.from_row(row)
    assert (record.served_start, record.served_end) == (served.start, served.end)


def test_new_attempt_and_the_run_record_default_the_served_range_to_none():
    """A caller that names none (a non-bars attempt, once P18 records them) stores NULL."""
    for cls in (ingest_delivery.NewAttempt, IngestRunRecord):
        defaults = {f.name: f.default for f in dataclasses.fields(cls) if f.name in ('served_start', 'served_end')}
        assert defaults == {'served_start': None, 'served_end': None}, cls


@pytest.mark.parametrize(
    'outcome', [DeliveryOutcome.SUCCEEDED, 'empty', DeliveryOutcome.FAILED], ids=['succeeded', 'empty', 'failed']
)
async def test_every_bars_attempt_row_stores_the_header_vendor_asset_id_as_received(store, outcome):
    """As received, on whatever outcome writes an attempt row: opaque, not trimmed, not case-folded."""
    request_id = await store.request()
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


async def test_a_success_with_no_bars_is_empty_and_writes_no_bar(store):
    """A request with its entry: the vendor answered and held none. The entry was touched, so it is announced."""
    request_id = await store.request()
    entry_id = (await store.request_row(request_id)).dataset_id

    answer = await store.deliver(report(request_id, parts=[BarPage(bars=[])]))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert store.batches == [] and await store.bars() == []
    (run,) = await store.runs(request_id)
    assert (run.state, run.bars_written, run.verdict, run.dataset_id) == (IngestRunState.EMPTY, 0, 'FINAL', entry_id)
    assert (await store.request_row(request_id)).state is IngestRequestState.EMPTY
    assert [type(event) for event in store.published] == [IngestRunChanged, DatasetChanged]


async def test_the_first_attempt_row_is_request_and_a_later_one_retry(store):
    request_id = await store.request()

    await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    await store.deliver(report(request_id, attempt_no=2))

    assert [run.trigger for run in await store.runs(request_id)] == [IngestRunTrigger.REQUEST, IngestRunTrigger.RETRY]
    row = await store.request_row(request_id)
    assert row.attempts == 2
    assert row.trigger is None, 'this request names no trigger: the REQUEST above is NULL read as REQUEST'


@pytest.mark.parametrize('trigger', [IngestRunTrigger.SCHEDULE, IngestRunTrigger.REQUEST])
async def test_the_first_attempt_row_takes_the_requests_trigger_and_every_later_one_is_retry(store, trigger):
    """P34c: a SCHEDULE request (the DAILY loop) records its first attempt SCHEDULE, a POST one REQUEST; then RETRY."""
    request_id = await store.request(trigger=trigger)
    assert (await store.request_row(request_id)).trigger is trigger

    await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    await store.deliver(report(request_id, DeliveryOutcome.FAILED, attempt_no=2))
    await store.deliver(report(request_id, attempt_no=3))

    runs = await store.runs(request_id)
    assert [run.trigger for run in runs] == [trigger, IngestRunTrigger.RETRY, IngestRunTrigger.RETRY]
    assert (await store.request_row(request_id)).trigger is trigger, 'the request keeps what started it'


async def test_a_scheduled_requests_redelivered_first_attempt_keeps_its_trigger(store):
    """The duplicate is answered from the stored row: a redelivery after the count moved writes no RETRY row."""
    request_id = await store.request(trigger=IngestRunTrigger.SCHEDULE)
    await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    await store.deliver(report(request_id, DeliveryOutcome.FAILED, attempt_no=2))

    await store.deliver(report(request_id, DeliveryOutcome.FAILED))

    assert [run.trigger for run in await store.runs(request_id)] == [IngestRunTrigger.SCHEDULE, IngestRunTrigger.RETRY]


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


# --------------------------------------------------------------------------------------------- P35: the entry on the
# first delivered attempt (architect ruling A, notes H, addendum 14)

# A POST's request row: no dataset_id yet, and the body's lifecycle fields. DAILY needs a non-BULK expiry type and no
# end (the entry model's own rules), which is what a valid POST body carries.
POST_EXPIRY = START + timedelta(days=200, hours=3)


async def _post_request(store: Store, **values) -> uuid.UUID:
    fields = {
        'dataset': False,
        'requested_end': None,
        'expiry_type': ExpiryType.ROLLING,
        'expiry': POST_EXPIRY,
        'trigger': IngestRunTrigger.REQUEST,
        **values,
    }
    return await store.request(**fields)


@pytest.mark.parametrize(
    ('update_type', 'expiry_type', 'end', 'collect', 'stored_collect'),
    [
        (UpdateType.DAILY, ExpiryType.ROLLING, None, None, True),
        (UpdateType.DAILY, ExpiryType.BUFFER_1K, None, False, False),
        (UpdateType.DAILY, ExpiryType.BUFFER_10K, None, True, True),
        (UpdateType.STATIC, ExpiryType.BUFFER_100K, START + timedelta(days=30), None, None),
        (UpdateType.STATIC, ExpiryType.BULK, START + timedelta(days=30), None, None),
    ],
    ids=['daily-collect-omitted', 'daily-collect-false', 'daily-collect-true', 'static-non-bulk', 'static-bulk'],
)
async def test_h_the_first_delivered_attempt_creates_the_entry_from_the_request_row_and_the_header_feed(
    store, update_type, expiry_type, end, collect, stored_collect
):
    """The first delivered attempt writes the entry from the request row and the header feed.

    The entry is the request row's (owner = client_label, symbol, asset type, the one data type, source,
    granularity, update type, window, expiry, expiry type, collect) plus the HEADER's resolved feed, written in the
    delivery transaction; the request takes its id, and so do the attempt row and the bars.
    """
    request_id = await _post_request(
        store, update_type=update_type, expiry_type=expiry_type, requested_end=end, collect=collect, feed=None
    )
    attempt = with_header(report(request_id, parts=[bar_page(0, 1, feed=Feed.SIP)]), feed=Feed.SIP)

    answer = await store.deliver(attempt)

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    ((entry, handed_collect, preserve),) = store.upserts
    assert (handed_collect, preserve) == (collect, False), 'the request row collect, as given, and a POST upsert'
    assert entry.owner == LABEL
    assert (entry.asset_symbol, entry.asset_type, entry.data_type) == ('VFV', AssetType.STOCK, DataType.MARKET_ACTIVITY)
    assert (entry.source, entry.granularity, entry.feed) == (DataSource.ALPACA_API, Granularity.ONE_DAY, Feed.SIP)
    assert (entry.update_type, entry.expiry_type) == (update_type, expiry_type), 'the body value, never a guess'
    assert (entry.start, entry.end, entry.expiry) == (START, end, POST_EXPIRY)
    (stored,) = await store.entries()
    assert (stored.feed, stored.expiry_type, stored.collect) == (Feed.SIP, expiry_type, stored_collect)
    row = await store.request_row(request_id)
    assert row.dataset_id == stored.id
    (run,) = await store.runs(request_id)
    assert run.dataset_id == stored.id
    assert {batch.dataset_id for batch, _ in store.batches} == {stored.id}
    assert {bar.dataset_id for bar in await store.bars()} == {stored.id}
    assert store.published[-1] == DatasetChanged(stored.id, DatasetChange.UPDATED)


async def test_h_the_entry_takes_the_headers_resolved_feed_never_the_requests_preference(store):
    """The request row's feed is the caller's preference; the tape the attempt was served from is the header's."""
    request_id = await _post_request(store, feed=Feed.IEX)
    attempt = with_header(report(request_id, parts=[bar_page(0, feed=Feed.SIP)]), feed=Feed.SIP)

    await store.deliver(attempt)

    ((entry, _, _),) = store.upserts
    assert entry.feed is Feed.SIP
    (stored,) = await store.entries()
    assert stored.feed is Feed.SIP


async def test_h_a_zero_bar_success_creates_the_entry_too(store):
    """Parity with the old POST: the vendor answered empty, the dataset exists (EMPTY), and it is announced."""
    request_id = await _post_request(store)

    answer = await store.deliver(report(request_id, parts=[BarPage(bars=[])]))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    (stored,) = await store.entries()
    assert (await store.request_row(request_id)).dataset_id == stored.id
    (run,) = await store.runs(request_id)
    assert (run.state, run.dataset_id) == (IngestRunState.EMPTY, stored.id)
    assert store.batches == []
    assert store.published[-1] == DatasetChanged(stored.id, DatasetChange.UPDATED)


async def test_h_a_failed_attempt_creates_no_entry_and_the_later_success_does(store):
    """Only a SUCCEEDED attempt runs the arm: a failure leaves the request without a dataset."""
    request_id = await _post_request(store)

    await store.deliver(report(request_id, DeliveryOutcome.FAILED))

    assert await store.entries() == [] and store.upserts == []
    first = await store.request_row(request_id)
    assert first.dataset_id is None
    (failed_run,) = await store.runs(request_id)
    assert failed_run.dataset_id is None

    await store.deliver(report(request_id, attempt_no=2))

    (stored,) = await store.entries()
    assert (await store.request_row(request_id)).dataset_id == stored.id


@pytest.mark.parametrize('parts', ['bars', 'empty'])
async def test_h_a_request_with_no_dataset_and_no_expiry_type_is_refused_never_bulk_and_nothing_written(store, parts):
    """A NULL expiry type on a request with no dataset is refused, never BULK.

    Addendum 14: a NULL expiry_type on a request with no dataset is refused INVALID_REQUEST naming the rule; the
    store never falls back to BULK. Nothing is written: no entry, no attempt row, no bar, the count unchanged.
    """
    request_id = await _post_request(
        store, expiry_type=None, update_type=UpdateType.STATIC, requested_end=START + timedelta(days=3)
    )
    attempt = report(request_id, parts=[BarPage(bars=[])] if parts == 'empty' else None)

    error = await store.refused(attempt)

    assert 'expiry type' in error.detail
    assert store.upserts == [] and await store.entries() == []
    row = await store.request_row(request_id)
    assert (row.attempts, row.dataset_id, row.state) == (0, None, IngestRequestState.QUEUED)


async def test_h_a_request_with_a_dataset_and_no_expiry_type_is_a_refresh_and_is_not_refused(store):
    """The DAILY loop's request carries no expiry type: its entry exists, so nothing is built from the row."""
    request_id = await store.request(expiry_type=None, trigger=IngestRunTrigger.SCHEDULE)

    answer = await store.deliver(report(request_id))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert store.upserts == []
    assert len(await store.bars()) == 2


@pytest.mark.parametrize(
    ('stored', 'sent', 'change'),
    [
        (True, False, DatasetChange.RETIRED),
        (False, True, DatasetChange.RESUMED),
        (True, True, DatasetChange.UPDATED),
        (False, None, DatasetChange.UPDATED),
    ],
)
async def test_h_a_repeat_post_that_flips_collect_publishes_retired_or_resumed(store, stored, sent, change):
    """A repeat POST sets collect only when explicit, and a flip is announced.

    An exact repeat of an existing entry's identity: collect changes only when explicit, and a flip is announced as
    RETIRED (to false) or RESUMED (to true); a repeat that leaves it as it was is UPDATED.
    """
    entry_id = await store.entry(end=None, expiry_type=ExpiryType.ROLLING, collect=stored)
    request_id = await _post_request(store, collect=sent)

    await store.deliver(report(request_id))

    assert (await store.request_row(request_id)).dataset_id == entry_id, 'the repeat matched the existing entry'
    after = await store.entry_row(entry_id)
    assert after.collect is (stored if sent is None else sent)
    assert after.expiry == POST_EXPIRY, 'a repeat sets expiry from the request, as POST /store always did'
    assert store.published[-1] == DatasetChanged(entry_id, change)


async def test_h_a_failure_after_the_entry_upsert_rolls_the_entry_and_the_request_dataset_back(store, monkeypatch):
    """The entry, the request's dataset_id and the bars are one transaction with the attempt row."""
    request_id = await _post_request(store)

    async def failing(db, request_id, outcome):
        raise RuntimeError('the count update failed')

    monkeypatch.setattr(delivery_intake, 'apply_outcome', failing)
    with pytest.raises(RuntimeError):
        await store.deliver(report(request_id))

    assert len(store.upserts) == 1, 'the entry was upserted inside the transaction'
    assert await store.entries() == [] and await store.bars() == []
    assert (await store.request_row(request_id)).dataset_id is None


# --------------------------------------------------------------------------------------------- P35: the refresh and
# the drop rule (ruling A, the store CLAUDE.md No orphan bars line)


async def test_a_refresh_writes_only_the_entrys_updated_at(store):
    """A refresh writes the entry's updated_at and nothing else.

    A request with a dataset_id never writes a lifecycle field: expiry, collect and stopped_cause stay as they were,
    even when the request row carries lifecycle values; only updated_at moves.
    """
    entry_id = await store.entry(expiry=POST_EXPIRY, end=None, expiry_type=ExpiryType.ROLLING)
    request_id = await store.request(dataset=False, dataset_id=entry_id, expiry=START, collect=False)
    before = await store.entry_row(entry_id)

    await store.deliver(report(request_id))

    after = await store.entry_row(entry_id)
    assert store.upserts == [], 'the refresh never upserts the entry'
    assert after.updated_at != before.updated_at
    unchanged = {key: value for key, value in before._mapping.items() if key != 'updated_at'}
    assert {key: after._mapping[key] for key in unchanged} == unchanged


async def test_c1_a_delivery_for_a_gone_entry_is_dropped_nothing_stored_request_empty_discarded_final(store):
    """The drop rule (P35, replacing P34's C1 refusal).

    Bars DO carry a foreign key to their entry (ON DELETE CASCADE, models/base_market_activity.py), so the database
    itself never keeps a bar whose entry is gone, and that key stays the backstop. The arm still locks the entry FOR
    UPDATE and judges it first: so that a delivery for a deleted entry is dropped cleanly instead of surfacing a
    foreign-key violation, so that it serialises with the operator's delete, the Stop and the reaper (they lock the
    same row), and so that the drop rule has one place to judge.

    Dropped, not refused: a refusal would be redelivered for ever. The attempt happened and is recorded (EMPTY, no
    bars, its spend), the request finishes EMPTY with cause DISCARDED, the answer is FINAL, and no dataset event.
    """
    request_id = await store.request()
    entry_id = (await store.request_row(request_id)).dataset_id
    async with AsyncSession(store.engine) as db:
        await db.execute(sa.delete(ENTRIES).where(ENTRIES.c.id == entry_id))
        await db.commit()

    answer = await store.deliver(report(request_id))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert store.batches == [] and await store.bars() == [], 'no bar reached the writer'
    assert store.upserts == [] and await store.entries() == [], 'the entry is never recreated by a refresh'
    (run,) = await store.runs(request_id)
    assert (run.state, run.bars_written, run.verdict) == (IngestRunState.EMPTY, 0, 'FINAL')
    row = await store.request_row(request_id)
    assert (row.state, row.failure_cause) == (IngestRequestState.EMPTY, IngestRequestFailureCause.DISCARDED.value)
    assert row.failure_detail == delivery_intake.DISCARDED_DETAIL and row.finished_at is not None
    assert (row.attempts, row.spent_calls) == (1, 3), 'the attempt happened and cost what it cost'
    assert [type(event) for event in store.published] == [IngestRunChanged], 'no dataset change for a drop'


@pytest.mark.parametrize('update_type', [UpdateType.DAILY, UpdateType.STREAM])
async def test_a_delivery_for_a_stopped_subscription_is_dropped(store, update_type):
    """A delivery for a Stopped subscription is dropped.

    Stopped is collect false on a DAILY or STREAM entry (is_retired): the operator's Stop wins over a delivery
    already in flight.
    """
    entry_id = await store.entry(update_type=update_type, collect=False)
    request_id = await store.request(dataset=False, dataset_id=entry_id)

    answer = await store.deliver(report(request_id))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert await store.bars() == []
    row = await store.request_row(request_id)
    assert (row.state, row.failure_cause) == (IngestRequestState.EMPTY, IngestRequestFailureCause.DISCARDED.value)
    assert (await store.entry_row(entry_id)).updated_at == START, 'a dropped delivery does not touch the entry'


async def test_a_static_entry_with_no_collect_intent_is_not_dropped(store):
    """STATIC holds collect NULL (no intent), which is never Stopped: its delivery is stored."""
    entry_id = await store.entry(update_type=UpdateType.STATIC, collect=None)
    request_id = await store.request(dataset=False, dataset_id=entry_id)

    await store.deliver(report(request_id))

    assert len(await store.bars()) == 2
    assert (await store.request_row(request_id)).state is IngestRequestState.SUCCEEDED


async def test_a_failed_delivery_for_a_gone_entry_is_written_as_a_failure_not_dropped(store):
    """The arm runs only on SUCCEEDED: a failed attempt for a gone entry is recorded as a failure and continues."""
    request_id = await store.request()
    entry_id = (await store.request_row(request_id)).dataset_id
    async with AsyncSession(store.engine) as db:
        await db.execute(sa.delete(ENTRIES).where(ENTRIES.c.id == entry_id))
        await db.commit()

    answer = await store.deliver(report(request_id, DeliveryOutcome.FAILED))

    assert answer == DeliverAttemptResponse(verdict=CONTINUE, attempt_count=1)
    assert (await store.request_row(request_id)).failure_cause is None


async def test_c1_the_entry_is_locked_and_judged_before_the_first_bar_is_written(store, monkeypatch):
    request_id = await store.request()
    order: list[str] = []
    real_lock = delivery_intake.lock_active_dataset_entry
    real_writer = delivery_intake.write_market_activity_in_transaction

    async def lock(db, dataset_id):
        order.append('lock')
        return await real_lock(db, dataset_id)

    async def writer(db, batch, requested_chunk_size=None):
        order.append('write')
        return await real_writer(db, batch, requested_chunk_size)

    monkeypatch.setattr(delivery_intake, 'lock_active_dataset_entry', lock)
    monkeypatch.setattr(delivery_intake, 'write_market_activity_in_transaction', writer)

    await store.deliver(report(request_id, parts=[bar_page(0), bar_page(1)]))

    assert order == ['lock', 'write', 'write']


async def test_lock_active_dataset_entry_selects_collect_and_update_type_for_update():
    class _ActiveRow:
        collect = True
        update_type = UpdateType.DAILY.value

    class _Active(_Captured):
        def first(self):
            return _ActiveRow()

    session = _Active()
    assert await ingest_delivery.lock_active_dataset_entry(session, uuid.uuid4()) is True
    (statement,) = session.statements
    sql = _postgres_sql(statement)
    assert sql.startswith(
        'SELECT store_dataset_entry.collect, store_dataset_entry.update_type FROM store_dataset_entry WHERE'
    )
    assert sql.endswith('FOR UPDATE')


# --------------------------------------------------------------------------------------------- note E: a stale
# CONTINUE is answered FINAL once the request is closed


@pytest.mark.parametrize(
    'finisher', ['success', 'permanent', 'exhausted', 'drop'], ids=lambda name: f'finished-by-{name}'
)
async def test_e_a_redelivered_continue_is_answered_final_once_the_request_is_finished_and_nothing_changes(
    store, finisher
):
    """A stale CONTINUE is answered FINAL once the request is finished.

    A late redelivery of attempt 1 (stored CONTINUE) after attempt 2 finished the request: FINAL, with the durable
    count, and the stored row is not changed.
    """
    request_id = await store.request()
    first = await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    assert first.verdict is CONTINUE
    if finisher == 'success':
        await store.deliver(report(request_id, attempt_no=2))
    elif finisher == 'permanent':
        await store.deliver(
            report(request_id, DeliveryOutcome.FAILED, attempt_no=2, error_class=DeliveryErrorClass.PERMANENT)
        )
    elif finisher == 'exhausted':
        await store.deliver(report(request_id, DeliveryOutcome.EXHAUSTED, attempt_no=2))
    else:
        entry_id = (await store.request_row(request_id)).dataset_id
        async with AsyncSession(store.engine) as db:
            await db.execute(sa.update(ENTRIES).where(ENTRIES.c.id == entry_id).values(collect=False))
            await db.commit()
        await store.deliver(report(request_id, attempt_no=2))
    count = (await store.request_row(request_id)).attempts
    before = await store.snapshot()

    again = await store.deliver(report(request_id, DeliveryOutcome.FAILED))

    assert again == DeliverAttemptResponse(verdict=FINAL, attempt_count=count)
    assert await store.snapshot() == before, 'the stored row and everything else unchanged'
    assert (await store.runs(request_id))[0].verdict == 'CONTINUE', 'the stored verdict is kept as it was'


@pytest.mark.parametrize('state', OPEN)
async def test_e_a_redelivered_continue_is_still_continue_while_the_request_is_open(store, state):
    request_id = await store.request()
    await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    await store.set_request(request_id, state=state)

    again = await store.deliver(report(request_id, DeliveryOutcome.FAILED))

    assert again == DeliverAttemptResponse(verdict=CONTINUE, attempt_count=1)


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
    ('outcome', 'parts'),
    [(DeliveryOutcome.SUCCEEDED, [asset_records()]), (DeliveryOutcome.SUCCEEDED, []), (DeliveryOutcome.FAILED, [])],
    ids=['asset-list-records', 'asset-list-empty', 'asset-list-failed'],
)
async def test_a_counted_asset_list_attempt_is_refused_never_acknowledged_unwritten(store, outcome, parts):
    """Decision 4 rule (2) and ruling 1: a counted asset-list attempt is refused with nothing written.

    An asset list cannot be fetched again, and its attempt row lands with its arm (P17). The corporate-actions arm is
    built since P18 (tj-7zjov1.31) and is pinned in the section below.
    """
    request_id = await store.request(IngestRequestKind.ASSET_LIST)
    await store.refused(report(request_id, outcome, kind=IngestRequestKind.ASSET_LIST, parts=parts))
    assert (await store.request_row(request_id)).state is IngestRequestState.QUEUED


# --------------------------------------------------------------------------------------------- the corporate-actions arm
#
# THE DESIGN (tj-7zjov1.31, P18): the owner ruling of 2026-10-26 (04:47 UTC note: raw as published, append-only, with
# process and effective dates, every identifier, the attempt and the requesting symbol; nothing tied to a listing),
# the 15:31 UTC note (WINDOW AND REPEATS: the window is a fetch parameter, never a filter; same id and same bytes is
# one observation, same id and other bytes a new one with its own as_of, never an overwrite), the 20:17 UTC note (the
# attempt row keeps the symbol and NULL for the other four labels), the 11:50 UTC note (vendor_asset_id NULL for a
# non-bars attempt whatever the header carries) and the 15:14 UTC note (rule i covers a SUCCEEDED corporate-actions
# report; the served range is stored on its attempt row).

CA = IngestRequestKind.CORPORATE_ACTIONS
CA_START = datetime(2026, 1, 5, tzinfo=UTC)
CA_END = datetime(2026, 2, 5, tzinfo=UTC)
LATER_AS_OF = AS_OF + timedelta(hours=6)


def action(vendor_action_id: str = 'ca-1', raw: bytes | None = None, **fields) -> CorporateActionRecord:
    """One vendor record; the raw bytes default to bytes naming the id, so two ids never share a hash."""
    values = {'action_type': 'name_change', 'identifiers': {'old_symbol': 'XLNX', 'new_symbol': 'AMD'}}
    payload = raw if raw is not None else b'{"id":"' + vendor_action_id.encode() + b'"}'
    return CorporateActionRecord(vendor_action_id=vendor_action_id, raw_payload=payload, **{**values, **fields})


def actions(*records: CorporateActionRecord) -> CorporateActionRecords:
    return CorporateActionRecords(records=list(records))


async def ca_request(store: Store, **values) -> uuid.UUID:
    """A corporate-actions request as the submit path leaves it: the symbol asked about and the window, no labels."""
    fields = {'asset_symbol': 'XLNX', 'requested_start': CA_START, 'requested_end': CA_END}
    return await store.request(CA, **{**fields, **values})


def ca_report(request_id: uuid.UUID, *parts, **kwargs) -> AttemptDelivery:
    return report(request_id, kind=CA, parts=list(parts), **kwargs)


async def test_a_corporate_actions_success_stores_every_record_raw_with_what_delivered_it(store):
    """Every field of each record, as published, and the request, attempt, symbol and window that delivered it."""
    request_id = await ca_request(store)
    dated = action(
        'ca-dated',
        raw=b'\x00\xff{"id":"ca-dated","note":"not utf-8"}\n',
        action_type='stock_merger',
        ex_date=datetime(2026, 1, 10, tzinfo=UTC),
        record_date=datetime(2026, 1, 11, tzinfo=UTC),
        payable_date=datetime(2026, 1, 12, tzinfo=UTC),
        process_date=datetime(2026, 1, 13, tzinfo=UTC),
        effective_date=datetime(2026, 1, 14, tzinfo=UTC),
        identifiers={'acquirer_symbol': 'AMD', 'target_symbol': 'XLNX', 'cusip': '983919101'},
    )
    bare = action('ca-bare', action_type='unknown_vendor_wording', identifiers={})

    answer = await store.deliver(ca_report(request_id, actions(dated), actions(bare)))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    rows = await store.actions()
    assert [row.vendor_action_id for row in rows] == ['ca-dated', 'ca-bare'], 'every record, in payload order'
    for row, record in zip(rows, (dated, bare), strict=True):
        assert row.source is DataSource.ALPACA_API
        assert row.action_type == record.action_type
        assert (row.ex_date, row.record_date, row.payable_date) == (
            record.ex_date,
            record.record_date,
            record.payable_date,
        )
        assert (row.process_date, row.effective_date) == (record.process_date, record.effective_date)
        assert row.identifiers == record.identifiers
        # The bytes untouched, and the hash is the SHA-256 of exactly those bytes, hex.
        assert isinstance(row.raw_payload, bytes) and row.raw_payload == record.raw_payload
        assert row.payload_hash == hashlib.sha256(record.raw_payload).hexdigest()
        # What delivered it.
        assert (row.request_id, row.attempt_no, row.requesting_symbol) == (request_id, 1, 'XLNX')
        assert (row.requested_start, row.requested_end) == (CA_START, CA_END)
        assert row.as_of == AS_OF, "the trailer's as_of: when the vendor answered"
        assert row.received_at is not None
    assert len({row.id for row in rows}) == 2
    # No bar, no entry, no dataset: nothing is tied to a listing or a dataset.
    assert store.batches == [] and await store.bars() == [] and await store.entries() == []
    assert not [event for event in store.published if isinstance(event, DatasetChanged)]
    row = await store.request_row(request_id)
    assert (row.state, row.attempts, row.dataset_id) == (IngestRequestState.SUCCEEDED, 1, None)


async def test_a_corporate_actions_attempt_row_keeps_the_symbol_null_labels_no_bars_and_the_served_range(store):
    """The 20:17 UTC note: symbol kept, the other four NULL; bars_written counts bars only; rule i's range stored."""
    request_id = await ca_request(store)

    await store.deliver(ca_report(request_id, actions(action('ca-1'), action('ca-2'))))

    (run,) = await store.runs(request_id)
    assert (run.state, run.verdict, run.trigger) == (IngestRunState.SUCCEEDED, 'FINAL', IngestRunTrigger.REQUEST)
    assert run.bars_written == 0, 'two records were written, and none of them is a bar'
    assert (run.asset_symbol, run.asset_type, run.data_type, run.granularity, run.update_type) == (
        'XLNX',
        None,
        None,
        None,
        None,
    )
    assert (run.source, run.feed, run.dataset_id) == (DataSource.ALPACA_API, None, None)
    assert (run.served_start, run.served_end) == (BAR_START, BAR_START + timedelta(days=5))
    assert (run.requested_start, run.requested_end) == (CA_START, CA_END)
    assert (run.quota_key, run.vendor_calls) == (QUOTA_KEY, 3)
    # The record the intake publishes is read back through IngestRunRecord: the NULL labels stay None.
    (changed,) = [event for event in store.published if isinstance(event, IngestRunChanged)]
    assert isinstance(changed.run, IngestRunRecord)
    assert (changed.run.asset_symbol, changed.run.granularity, changed.run.update_type) == ('XLNX', None, None)
    assert (changed.run.asset_type, changed.run.data_type, changed.run.bars_written) == (None, None, 0)


async def test_a_corporate_actions_request_with_no_symbol_writes_an_attempt_row_with_every_label_null(store):
    request_id = await ca_request(store, asset_symbol=None)

    await store.deliver(ca_report(request_id, actions(action())))

    (run,) = await store.runs(request_id)
    assert (run.asset_symbol, run.asset_type, run.data_type, run.granularity, run.update_type) == (None,) * 5
    (row,) = await store.actions()
    assert row.requesting_symbol is None


@pytest.mark.parametrize('outcome', [DeliveryOutcome.SUCCEEDED, DeliveryOutcome.FAILED])
async def test_a_corporate_actions_attempt_row_stores_no_vendor_asset_id_even_when_the_header_carries_one(
    store, outcome
):
    """The 11:50 UTC note (P49 gate): the id is the bars stop rule's detector, so another kind stores NULL."""
    request_id = await ca_request(store)
    parts = [actions(action())] if outcome is DeliveryOutcome.SUCCEEDED else []

    await store.deliver(with_header(ca_report(request_id, *parts, outcome=outcome), vendor_asset_id=VENDOR_ASSET_ID))

    (run,) = await store.runs(request_id)
    assert run.vendor_asset_id is None


async def test_a_record_outside_the_window_or_naming_other_symbols_only_is_stored_all_the_same(store):
    """The 15:31 UTC note: the requested window is a fetch parameter, never a filter."""
    request_id = await ca_request(store)
    before = action('ca-before', process_date=CA_START - timedelta(days=400), ex_date=CA_START - timedelta(days=1))
    at_end = action('ca-at-end', process_date=CA_END, effective_date=CA_END)
    after = action('ca-after', process_date=CA_END + timedelta(days=30))
    elsewhere = action('ca-elsewhere', identifiers={'old_symbol': 'SIVB', 'new_symbol': 'SIVBQ'})
    undated = action('ca-undated')

    await store.deliver(ca_report(request_id, actions(before, at_end, after, elsewhere, undated)))

    stored = {row.vendor_action_id: row for row in await store.actions()}
    assert set(stored) == {'ca-before', 'ca-at-end', 'ca-after', 'ca-elsewhere', 'ca-undated'}
    assert stored['ca-elsewhere'].identifiers == {'old_symbol': 'SIVB', 'new_symbol': 'SIVBQ'}
    assert stored['ca-after'].process_date == CA_END + timedelta(days=30)


async def test_a_repeat_is_kept_once_and_a_vendor_revision_is_a_new_observation_with_its_own_as_of(store):
    """The 15:31 UTC note: same id and same bytes is one observation, kept first; other bytes are a new row."""
    first = await ca_request(store)
    overlapping = await ca_request(store, requested_start=CA_START + timedelta(days=10))
    original = action('ca-1', raw=b'{"id":"ca-1","ratio":"1:2"}')
    revised = action('ca-1', raw=b'{"id":"ca-1","ratio":"1:3"}')

    await store.deliver(ca_report(first, actions(original)))
    later = with_trailer(ca_report(overlapping, actions(original, revised, action('ca-2'))), as_of=LATER_AS_OF)
    answer = await store.deliver(later)

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    rows = await store.actions()
    assert [(row.vendor_action_id, row.raw_payload) for row in rows] == [
        ('ca-1', original.raw_payload),
        ('ca-1', revised.raw_payload),
        ('ca-2', b'{"id":"ca-2"}'),
    ]
    # The first observation is untouched: its request, symbol window and as_of are the first delivery's.
    assert (rows[0].request_id, rows[0].requested_start, rows[0].as_of) == (first, CA_START, AS_OF)
    # The revision is its own row, with its own hash and the later delivery's as_of.
    assert (rows[1].request_id, rows[1].as_of) == (overlapping, LATER_AS_OF)
    assert rows[1].payload_hash != rows[0].payload_hash
    assert rows[1].id != rows[0].id


async def test_the_same_record_repeated_within_one_delivery_is_kept_once(store):
    """A record repeated inside one payload message and across two (overlapping vendor pages) is one observation."""
    request_id = await ca_request(store)
    record = action('ca-1')

    await store.deliver(ca_report(request_id, actions(record, record), actions(record, action('ca-2'))))

    assert [row.vendor_action_id for row in await store.actions()] == ['ca-1', 'ca-2']
    (run,) = await store.runs(request_id)
    assert run.state is IngestRunState.SUCCEEDED


async def test_insert_observations_counts_only_what_is_new(store):
    """The repository's count: a repeat of (source, id, bytes) writes nothing and counts nothing; a revision counts."""
    request_id = await ca_request(store)
    request = await _request_record(store, request_id)
    attempt = ca_report(request_id, actions(action('ca-1'), action('ca-2')))
    revision = ca_report(request_id, actions(action('ca-1', raw=b'{"id":"ca-1","v":2}')))

    async with AsyncSession(store.engine) as db:
        first = await corporate_action_crud.insert_observations(db, observations_of(request, attempt))
        repeat = await corporate_action_crud.insert_observations(db, observations_of(request, attempt))
        revised = await corporate_action_crud.insert_observations(db, observations_of(request, revision))
        await db.commit()

    assert (first, repeat, revised) == (2, 0, 1)
    assert len(await store.actions()) == 3


async def test_a_failed_corporate_actions_attempt_writes_its_attempt_row_and_no_observation(store):
    request_id = await ca_request(store)

    answer = await store.deliver(ca_report(request_id, outcome=DeliveryOutcome.FAILED, detail='503'))

    assert answer == DeliverAttemptResponse(verdict=CONTINUE, attempt_count=1)
    assert await store.actions() == []
    (run,) = await store.runs(request_id)
    assert (run.state, run.verdict, run.error_message, run.bars_written) == (
        IngestRunState.FAILED,
        'CONTINUE',
        '503',
        0,
    )
    assert (run.asset_symbol, run.granularity, run.served_start, run.served_end) == ('XLNX', None, None, None)
    assert (await store.request_row(request_id)).state is IngestRequestState.QUEUED


async def test_a_corporate_actions_success_with_no_record_is_empty_and_keeps_its_served_range(store):
    request_id = await ca_request(store)

    answer = await store.deliver(ca_report(request_id))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert await store.actions() == []
    (run,) = await store.runs(request_id)
    assert (run.state, run.bars_written) == (IngestRunState.EMPTY, 0)
    assert (run.served_start, run.served_end) == (BAR_START, BAR_START + timedelta(days=5))
    assert (await store.request_row(request_id)).state is IngestRequestState.EMPTY


async def test_the_observations_roll_back_with_the_delivery_when_a_later_step_fails(store, monkeypatch):
    """One transaction: the arm writes first, and a failure after it (the attempt row here) leaves no observation."""
    request_id = await ca_request(store)

    async def failing_insert_attempt(db, attempt):
        assert await db.scalar(sa.select(sa.func.count()).select_from(ACTIONS)) == 1, 'the arm ran first'
        raise InvalidRequestError(Reason.INVALID_REQUEST, 'the attempt row could not be written')

    monkeypatch.setattr(delivery_intake, 'insert_attempt', failing_insert_attempt)

    await store.refused(ca_report(request_id, actions(action())))
    assert await store.actions() == []


async def test_a_corporate_actions_delivery_of_records_all_already_held_is_succeeded_not_empty(store):
    """tj-7zjov1.161, the architect's P18 gate ruling (b): the outcome describes the VENDOR'S ANSWER, not the writes.

    A second request whose report carries only records already stored (same source, vendor id and bytes) writes no
    observation, yet the vendor answered records: its attempt row and its request are SUCCEEDED, never EMPTY (EMPTY
    means the vendor held none), with bars_written 0.
    """
    records = (action('ca-held-1'), action('ca-held-2'))
    first = await ca_request(store)
    await store.deliver(ca_report(first, actions(*records)))
    held = await store.actions()
    assert len(held) == 2
    second = await ca_request(store, requested_start=CA_START + timedelta(days=3))

    answer = await store.deliver(with_trailer(ca_report(second, actions(*records)), as_of=LATER_AS_OF))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert await store.actions() == held, 'a record already held was written again'
    (run,) = await store.runs(second)
    assert (run.state, run.bars_written, run.verdict) == (IngestRunState.SUCCEEDED, 0, 'FINAL')
    row = await store.request_row(second)
    assert (row.state, row.failure_cause) == (IngestRequestState.SUCCEEDED, None)


async def test_a_redelivered_corporate_actions_attempt_writes_nothing_and_repeats_its_answer(store):
    request_id = await ca_request(store)
    attempt = ca_report(request_id, actions(action('ca-1')))
    first = await store.deliver(attempt)
    before = await store.snapshot()

    again = await store.deliver(attempt)

    assert again == first == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert await store.snapshot() == before


async def _request_record(store: Store, request_id: uuid.UUID) -> request_crud.IngestRequestRecord:
    async with AsyncSession(store.engine) as db:
        return await ingest_delivery.lock_request(db, request_id)


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
    assert row.spend_unknown_attempts == 1, 'P34c: the unknown spend is counted, so the sums read as a lower bound'
    warnings = _app_records(caplog, logging.WARNING)
    assert len(warnings) == 1, [record.getMessage() for record in warnings]
    message = warnings[0].getMessage()
    assert str(request_id) in message and 'Attempt 1 ' in message and 'spend unknown' in message
    _logs_hold_no_secret(caplog)


def test_every_vendor_column_is_spend_or_the_vendor_asset_id():
    """A new vendor_* column must be classified here, or the NULL-spend check above silently stops covering it."""
    vendor = {column.name for column in RUNS.columns if column.name.startswith('vendor_')}
    assert vendor == {*SPEND_COLUMNS, 'vendor_asset_id'}


def _no_stats(attempt):
    return with_trailer(attempt, vendor_stats=None)


async def test_a_new_request_counts_no_unknown_spend(store):
    request_id = await store.request()
    assert (await store.request_row(request_id)).spend_unknown_attempts == 0


async def test_each_counted_failed_report_without_stats_adds_one_and_one_with_stats_adds_nothing(store):
    """P34c: 1 per counted FAILED report with no vendor_stats; a report with stats adds 0 and its spend to the sums."""
    request_id = await store.request()

    await store.deliver(_no_stats(report(request_id, DeliveryOutcome.FAILED)))
    assert (await store.request_row(request_id)).spend_unknown_attempts == 1
    await store.deliver(report(request_id, DeliveryOutcome.FAILED, attempt_no=2, vendor_stats=stats(2, 1, 0, 0)))
    assert (await store.request_row(request_id)).spend_unknown_attempts == 1
    await store.deliver(_no_stats(report(request_id, DeliveryOutcome.FAILED, attempt_no=3)))

    row = await store.request_row(request_id)
    assert (row.attempts, row.spend_unknown_attempts, row.spent_calls, row.spent_pages) == (3, 2, 2, 1)
    first, second, third = await store.runs(request_id)
    assert (first.vendor_calls, second.vendor_calls, third.vendor_calls) == (None, 2, None)


async def test_a_failed_report_with_stats_and_a_success_add_no_unknown_spend(store):
    request_id = await store.request()

    await store.deliver(report(request_id, DeliveryOutcome.FAILED))
    await store.deliver(report(request_id, attempt_no=2))

    row = await store.request_row(request_id)
    assert (row.attempts, row.spend_unknown_attempts) == (2, 0)


@pytest.mark.parametrize('outcome', NO_CALL, ids=lambda outcome: outcome.lower())
async def test_an_expired_or_exhausted_report_adds_no_unknown_spend(store, outcome):
    """It carries no stats but made no vendor call: it is not counted, so its spend is not unknown, it is none."""
    request_id = await store.request()

    await store.deliver(report(request_id, outcome))

    row = await store.request_row(request_id)
    assert (row.attempts, row.spend_unknown_attempts) == (0, 0)


async def test_a_redelivered_failed_report_without_stats_adds_nothing(store):
    """The duplicate is answered from the stored row: the counter, like the count, moves once per attempt."""
    request_id = await store.request()
    await store.deliver(_no_stats(report(request_id, DeliveryOutcome.FAILED)))
    before = await store.snapshot()

    await store.deliver(_no_stats(report(request_id, DeliveryOutcome.FAILED)))
    await store.deliver(report(request_id, DeliveryOutcome.FAILED))

    assert await store.snapshot() == before
    assert (await store.request_row(request_id)).spend_unknown_attempts == 1


async def test_a_late_failed_report_without_stats_on_a_finished_request_still_counts_unknown_spend(store):
    """The attempt happened and its cost is unknown, whatever the request's state: the counter says so."""
    request_id = await store.request()
    await store.deliver(report(request_id))

    await store.deliver(_no_stats(report(request_id, DeliveryOutcome.FAILED, attempt_no=2)))

    row = await store.request_row(request_id)
    assert (row.state, row.attempts, row.spend_unknown_attempts) == (IngestRequestState.SUCCEEDED, 2, 1)


async def test_a_rolled_back_delivery_without_stats_counts_nothing(store, monkeypatch):
    """The counter is in the delivery's one transaction: a failure after it leaves it where it was."""
    request_id = await store.request()

    async def broken(db, request_id, outcome):
        await original(db, request_id, outcome)
        raise RuntimeError('after the request update')

    original = delivery_intake.apply_outcome
    monkeypatch.setattr(delivery_intake, 'apply_outcome', broken)
    with pytest.raises(Exception):  # noqa: B017 -- the delivery's own wrapping of the failure is not this test's
        await store.deliver(_no_stats(report(request_id, DeliveryOutcome.FAILED)))

    row = await store.request_row(request_id)
    assert (row.attempts, row.spend_unknown_attempts) == (0, 0)
    assert await store.runs(request_id) == []


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


# --------------------------------------------------------------------------------------------- P50: the stop rule
#
# tj-7zjov1.144 (P50), tests tj-7zjov1.145 (P50v); ADR tj-26jt9w addenda 12 and 13; the architect's P18 gate note on
# P50 (the comparison base reads only bars attempts of the same dataset). A SUCCEEDED bars delivery whose vendor asset
# id differs from the dataset's LAST ACCEPTED one (SUCCEEDED or EMPTY, a bars attempt of this dataset, with an id), or
# is empty after a set one, writes no bar; the same transaction stops the entry (collect false, stopped_cause
# VENDOR_ASSET_CHANGED), fails the attempt row (marker, error_domain NULL, the delivered id) and the request (cause and
# sentence), answers FINAL, logs one WARNING, and publishes RETIRED after the commit. A refused attempt is never a base.

ID_A = 'vendor-asset-A-1f0'
ID_B = 'vendor-asset-B-7c4'
MARKER = 'VENDOR_ASSET_CHANGED'


class Series:
    """Deliveries to one dataset entry, each on its own refresh request, each finishing later than the one before.

    The trailer's as_of is the attempt row's finished_at, which orders the comparison base; the shared builder gives
    every report the same instant, so each delivery here is given its own, one hour after the previous one.
    """

    clock = 0

    def __init__(self, store: Store, entry_id: uuid.UUID) -> None:
        self.store, self.entry_id = store, entry_id

    async def deliver(
        self,
        vendor_asset_id: str,
        *,
        parts: list | None = None,
        outcome: DeliveryOutcome = DeliveryOutcome.SUCCEEDED,
        hours: int | None = None,
    ) -> tuple[uuid.UUID, DeliverAttemptResponse]:
        Series.clock += 1
        request_id = await self.store.request(dataset=False, dataset_id=self.entry_id)
        attempt = with_header(report(request_id, outcome, parts=parts), vendor_asset_id=vendor_asset_id)
        attempt = with_trailer(attempt, as_of=AS_OF + timedelta(hours=Series.clock if hours is None else hours))
        return request_id, await self.store.deliver(attempt)


async def _series(store: Store, **entry) -> Series:
    return Series(store, await store.entry(**entry))


async def _bar_count(store: Store, entry_id: uuid.UUID) -> int:
    return len([row for row in await store.bars() if row.dataset_id == entry_id])


async def _resume(store: Store, entry_id: uuid.UUID) -> None:
    """A Resume as the operator's crud writes it (collect true and stopped_cause NULL in one UPDATE).

    The crud itself is pinned in test_ui_dataset_collecting_route.py and on Postgres; here only its effect is needed.
    """
    async with AsyncSession(store.engine) as db:
        await db.execute(sa.update(ENTRIES).where(ENTRIES.c.id == entry_id).values(collect=True, stopped_cause=None))
        await db.commit()


async def _operator_stop(store: Store, entry_id: uuid.UUID) -> None:
    async with AsyncSession(store.engine) as db:
        await db.execute(sa.update(ENTRIES).where(ENTRIES.c.id == entry_id).values(collect=False))
        await db.commit()


async def _move_run(store: Store, request_id: uuid.UUID, **values) -> None:
    async with AsyncSession(store.engine) as db:
        await db.execute(sa.update(RUNS).where(RUNS.c.request_id == request_id).values(**values))
        await db.commit()


async def _assert_refused_and_stopped(
    store: Store, series: Series, request_id: uuid.UUID, answer, earlier: str, delivered: str | None
) -> str:
    """Every field of the stop, as ruled (addendum 13 (i) to (v)). Returns the sentence for the log check."""
    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    entry = await store.entry_row(series.entry_id)
    assert (entry.collect, entry.stopped_cause) == (False, MARKER), 'the entry is not stopped by the store'
    assert entry.updated_at != START, 'the stop did not move updated_at'
    (run,) = await store.runs(request_id)
    assert (run.state, run.bars_written, run.verdict) == (IngestRunState.FAILED, 0, 'FINAL')
    assert (run.error_domain, run.error_reason) == (None, MARKER), 'the marker, never a typed (domain, reason)'
    assert run.vendor_asset_id == delivered, 'the delivered id, as received (NULL when empty)'
    assert run.dataset_id == series.entry_id
    sentence = run.error_message
    assert sentence and 'VFV' in sentence and f"'{earlier}'" in sentence, sentence
    if delivered is None:
        assert 'no vendor asset id' in sentence, sentence
    else:
        assert f"'{delivered}'" in sentence, sentence
    row = await store.request_row(request_id)
    assert (row.state, row.failure_cause) == (IngestRequestState.FAILED, MARKER)
    assert row.failure_detail == sentence, 'the request carries the same sentence as the attempt row'
    assert row.finished_at is not None
    events = [event for event in store.published if not isinstance(event, IngestRunChanged)]
    assert events[-1] == DatasetChanged(series.entry_id, DatasetChange.RETIRED), 'RETIRED, never UPDATED'
    return sentence


async def test_p50_the_fb_case_an_id_then_a_different_id_stops_the_dataset_and_writes_no_bar(store, caplog):
    """The FB case from the spikes: one symbol, an id, then another id. Every field of the stop is pinned."""
    caplog.set_level(logging.DEBUG, logger='data.store')
    series = await _series(store)
    first, answer = await series.deliver(ID_A)
    assert answer.verdict is FINAL and (await store.request_row(first)).state is IngestRequestState.SUCCEEDED
    assert await _bar_count(store, series.entry_id) == 2
    batches = len(store.batches)
    store.published.clear()

    second, answer = await series.deliver(ID_B)

    sentence = await _assert_refused_and_stopped(store, series, second, answer, ID_A, ID_B)
    assert len(store.batches) == batches, 'a bar of the refused attempt reached the writer'
    assert await _bar_count(store, series.entry_id) == 2, 'a bar of the refused attempt was stored'
    run_event, dataset_event = store.published
    assert isinstance(run_event, IngestRunChanged) and run_event.run.error_reason == MARKER
    assert dataset_event == DatasetChanged(series.entry_id, DatasetChange.RETIRED)
    # The earlier attempt is left exactly as it was: nothing relabels or moves a bar.
    (earlier,) = await store.runs(first)
    assert (earlier.state, earlier.vendor_asset_id, earlier.bars_written) == (IngestRunState.SUCCEEDED, ID_A, 2)
    # ONE WARNING with the sentence, the request id and the dataset id; never the label or the quota key.
    warnings = [r.getMessage() for r in _app_records(caplog, logging.WARNING) if sentence in r.getMessage()]
    assert len(warnings) == 1, warnings
    assert str(second) in warnings[0] and str(series.entry_id) in warnings[0]
    for record in _app_records(caplog):
        assert LABEL not in record.getMessage() and QUOTA_KEY not in record.getMessage()


async def test_p50_a_same_id_series_runs_on(store):
    series = await _series(store)
    for _ in range(3):
        request_id, answer = await series.deliver(ID_A)
        assert answer.verdict is FINAL
        assert (await store.request_row(request_id)).state is IngestRequestState.SUCCEEDED
    assert await _bar_count(store, series.entry_id) == 6
    entry = await store.entry_row(series.entry_id)
    assert (entry.collect, entry.stopped_cause) == (True, None)
    assert DatasetChanged(series.entry_id, DatasetChange.RETIRED) not in store.published


@pytest.mark.parametrize('history', ['none', 'empty-id', 'failed-only'])
async def test_p50_with_no_earlier_accepted_id_the_delivery_is_accepted_and_its_id_becomes_the_base(store, history):
    """No earlier id: accept and record (P49). An empty-id success and a FAILED attempt with an id set no base."""
    series = await _series(store)
    if history == 'empty-id':
        await series.deliver('')
    elif history == 'failed-only':
        await series.deliver(ID_B, outcome=DeliveryOutcome.FAILED)

    request_id, _ = await series.deliver(ID_A)

    assert (await store.request_row(request_id)).state is IngestRequestState.SUCCEEDED
    assert (await store.entry_row(series.entry_id)).stopped_cause is None
    # ... and that id is now the base: another one stops.
    request_id, answer = await series.deliver(ID_B)
    await _assert_refused_and_stopped(store, series, request_id, answer, ID_A, ID_B)


async def test_p50_an_empty_id_after_a_set_one_stops_and_stores_null(store):
    series = await _series(store)
    await series.deliver(ID_A)

    request_id, answer = await series.deliver('')

    await _assert_refused_and_stopped(store, series, request_id, answer, ID_A, None)
    assert await _bar_count(store, series.entry_id) == 2


@pytest.mark.parametrize(
    ('delivered', 'stops'), [(ID_B, True), ('', True), (ID_A, False)], ids=['other', 'empty', 'same']
)
async def test_p50_a_zero_bar_success_is_judged_too(store, delivered, stops):
    """The check sits before the arm's early return for no bars: a zero-bar success with another id stops."""
    series = await _series(store)
    await series.deliver(ID_A)

    request_id, answer = await series.deliver(delivered, parts=[BarPage(bars=[])])

    if stops:
        await _assert_refused_and_stopped(store, series, request_id, answer, ID_A, delivered or None)
    else:
        (run,) = await store.runs(request_id)
        assert (run.state, run.error_reason) == (IngestRunState.EMPTY, None)
        assert (await store.entry_row(series.entry_id)).collect is True


async def test_p50_resume_is_a_recheck_a_new_id_stops_again_and_the_earlier_id_is_accepted(store):
    """A refused attempt is never a base: after a Resume the base is still the last ACCEPTED id."""
    series = await _series(store)
    await series.deliver(ID_A)
    await series.deliver(ID_B)  # refused: the dataset stops
    await _resume(store, series.entry_id)

    again, answer = await series.deliver(ID_B)
    await _assert_refused_and_stopped(store, series, again, answer, ID_A, ID_B)
    assert await _bar_count(store, series.entry_id) == 2

    await _resume(store, series.entry_id)
    back, answer = await series.deliver(ID_A)
    assert answer.verdict is FINAL
    (run,) = await store.runs(back)
    assert (run.state, run.bars_written, run.error_reason) == (IngestRunState.SUCCEEDED, 2, None)
    entry = await store.entry_row(series.entry_id)
    assert (entry.collect, entry.stopped_cause) == (True, None)
    assert await _bar_count(store, series.entry_id) == 4


async def test_p50_a_failed_vendor_attempt_with_another_id_never_becomes_the_base(store):
    """A FAILED attempt (the vendor's transient failure here), newer or not, is not accepted: the base excludes FAILED."""
    series = await _series(store)
    await series.deliver(ID_A)
    await series.deliver(ID_B, outcome=DeliveryOutcome.FAILED)

    request_id, _ = await series.deliver(ID_A)

    (run,) = await store.runs(request_id)
    assert (run.state, run.error_reason) == (IngestRunState.SUCCEEDED, None)
    assert (await store.entry_row(series.entry_id)).stopped_cause is None


async def test_p50_a_corporate_actions_or_other_non_bars_row_in_the_datasets_history_is_never_the_base(store):
    """The architect's P18 gate note: the base reads bars attempts of this dataset only (granularity set).

    Two rows newer than the accepted A, both naming this dataset: a real corporate-actions attempt (its id NULL by
    kind) moved onto the dataset, and a crafted non-bars row (granularity NULL) that DOES carry another id. Neither is
    a base, so A delivered again is accepted; read as a base, the second would stop the dataset.
    """
    series = await _series(store)
    await series.deliver(ID_A, hours=1)
    ca = await ca_request(store)
    await store.deliver(with_header(ca_report(ca, actions(action())), vendor_asset_id=ID_B))
    await _move_run(store, ca, dataset_id=series.entry_id, finished_at=AS_OF + timedelta(hours=50))
    other = await _series(store, asset_symbol='ZZOTHER')
    crafted, _ = await other.deliver(ID_B)
    await _move_run(
        store, crafted, dataset_id=series.entry_id, granularity=None, finished_at=AS_OF + timedelta(hours=60)
    )
    (ca_run,) = await store.runs(ca)
    assert (ca_run.vendor_asset_id, ca_run.granularity) == (None, None), 'the corporate-actions row, as P18 writes it'

    request_id, _ = await series.deliver(ID_A, hours=100)

    (run,) = await store.runs(request_id)
    assert (run.state, run.error_reason) == (IngestRunState.SUCCEEDED, None)
    assert (await store.entry_row(series.entry_id)).collect is True


async def test_p50_another_datasets_attempts_never_count(store):
    """Same symbol, two datasets, two ids: each dataset is judged against its own history only."""
    mine = await _series(store)
    theirs = await _series(store, granularity=Granularity.ONE_HOUR)
    await mine.deliver(ID_A)
    await theirs.deliver(ID_B)

    mine_request, _ = await mine.deliver(ID_A)
    theirs_request, _ = await theirs.deliver(ID_B)

    for request_id in (mine_request, theirs_request):
        assert (await store.request_row(request_id)).state is IngestRequestState.SUCCEEDED
    for series in (mine, theirs):
        assert (await store.entry_row(series.entry_id)).collect is True


async def test_p50_the_base_is_the_newest_accepted_attempt_by_finished_at_not_by_insertion(store):
    """Ordered by finished_at descending: a row inserted later but finished earlier is not the base."""
    series = await _series(store)
    await series.deliver(ID_A, hours=10)
    other = await _series(store, asset_symbol='ZZOTHER')
    late_insert, _ = await other.deliver(ID_B, hours=1)
    await _move_run(store, late_insert, dataset_id=series.entry_id)

    request_id, _ = await series.deliver(ID_A, hours=20)

    assert (await store.request_row(request_id)).state is IngestRequestState.SUCCEEDED


RECEIVED = '  Vendor-Asset/ÄBC 01  '


@pytest.mark.parametrize(
    ('delivered', 'stops'),
    [(RECEIVED, False), (RECEIVED.strip(), True), (RECEIVED.lower(), True), (RECEIVED.upper(), True)],
    ids=['as-received', 'trimmed', 'lower', 'upper'],
)
async def test_p50_the_id_is_stored_and_compared_as_received_never_normalised(store, delivered, stops):
    """The P49 gate note: a mixed-case id with surrounding characters is stored and compared unchanged."""
    series = await _series(store)
    first, _ = await series.deliver(RECEIVED)
    assert (await store.runs(first))[0].vendor_asset_id == RECEIVED

    request_id, answer = await series.deliver(delivered)

    if stops:
        await _assert_refused_and_stopped(store, series, request_id, answer, RECEIVED, delivered)
    else:
        assert (await store.request_row(request_id)).state is IngestRequestState.SUCCEEDED
    assert (await store.runs(request_id))[0].vendor_asset_id == delivered


async def test_p50_the_entry_is_stopped_in_the_transaction_and_retired_is_published_only_after_the_commit(
    store, monkeypatch
):
    series = await _series(store)
    await series.deliver(ID_A)
    order: list[object] = []
    real_transaction, real_stop = delivery_intake.write_transaction, delivery_intake.stop_entry

    @asynccontextmanager
    async def tracking(db, name):
        async with real_transaction(db, name):
            yield
        order.append('commit')

    async def stop(db, dataset_id, cause):
        order.append('stop')
        return await real_stop(db, dataset_id, cause)

    monkeypatch.setattr(delivery_intake, 'write_transaction', tracking)
    monkeypatch.setattr(delivery_intake, 'stop_entry', stop)
    monkeypatch.setattr(delivery_intake, 'publish', order.append)

    await series.deliver(ID_B)

    assert order[:2] == ['stop', 'commit'], order
    assert [type(event) for event in order[2:]] == [IngestRunChanged, DatasetChanged], order
    assert order[3] == DatasetChanged(series.entry_id, DatasetChange.RETIRED)


async def test_p50_the_drop_rule_runs_first_a_stopped_entry_is_dropped_not_compared(store, monkeypatch):
    """After the entry lock and the drop rule: a store-stopped entry's next delivery is DISCARDED, cause unchanged."""
    series = await _series(store)
    await series.deliver(ID_A)
    await series.deliver(ID_B)
    order: list[str] = []
    real_lock, real_base = delivery_intake.lock_active_dataset_entry, delivery_intake.last_accepted_vendor_asset_id

    async def lock(db, dataset_id):
        order.append('lock')
        return await real_lock(db, dataset_id)

    async def base(db, dataset_id):
        order.append('base')
        return await real_base(db, dataset_id)

    monkeypatch.setattr(delivery_intake, 'lock_active_dataset_entry', lock)
    monkeypatch.setattr(delivery_intake, 'last_accepted_vendor_asset_id', base)
    store.published.clear()

    request_id, answer = await series.deliver('vendor-asset-C')

    assert order == ['lock'], 'a dropped delivery is never compared'
    assert answer.verdict is FINAL
    row = await store.request_row(request_id)
    assert (row.state, row.failure_cause) == (IngestRequestState.EMPTY, IngestRequestFailureCause.DISCARDED.value)
    entry = await store.entry_row(series.entry_id)
    assert (entry.collect, entry.stopped_cause) == (False, MARKER)
    assert [type(event) for event in store.published] == [IngestRunChanged], 'a drop announces no dataset change'

    await _resume(store, series.entry_id)
    order.clear()
    await series.deliver(ID_A)
    assert order == ['lock', 'base'], 'on a live entry the base is read after the lock'


async def _base(store: Store, entry_id: uuid.UUID) -> str | None:
    """The stop rule's base as the production query reads it."""
    async with AsyncSession(store.engine) as db:
        return await last_accepted_vendor_asset_id(db, entry_id)


@pytest.mark.parametrize('stopped_by', ['operator', 'store'])
async def test_p50_a_delivery_dropped_while_stopped_never_becomes_the_base(store, stopped_by):
    """P50f (tj-7zjov1.163), ADR tj-26jt9w addendum 16: a dropped bars attempt row stores NULL vendor_asset_id.

    The drop rule runs before the stop rule, so a refresh in flight when the entry is stopped is dropped unjudged. Its
    row keeps state EMPTY, its dataset id and its verdict, but never its id, so after a Resume the base is still the
    last ACCEPTED id: a delivery with the new id stops again naming the earlier one, and the earlier id is accepted.
    """
    series = await _series(store)
    await series.deliver(ID_A)
    if stopped_by == 'operator':
        await _operator_stop(store, series.entry_id)
    else:
        await series.deliver(ID_B)  # the store's stop
    dropped, answer = await series.deliver(ID_B)  # a refresh already in flight: dropped, nothing compared

    assert answer.verdict is FINAL
    (row,) = await store.runs(dropped)
    assert row.state is IngestRunState.EMPTY, 'a dropped delivery is still written EMPTY'
    assert row.vendor_asset_id is None, 'a dropped (unjudged) delivery stored its vendor asset id'
    assert row.dataset_id == series.entry_id, 'a dropped attempt row keeps its dataset id'
    assert (row.bars_written, row.error_reason, row.verdict) == (0, None, 'FINAL')
    request = await store.request_row(dropped)
    assert (request.state, request.failure_cause) == (
        IngestRequestState.EMPTY,
        IngestRequestFailureCause.DISCARDED.value,
    )
    assert await _base(store, series.entry_id) == ID_A, 'the base is the last accepted id, never the dropped one'
    await _resume(store, series.entry_id)

    request_id, answer = await series.deliver(ID_B)

    await _assert_refused_and_stopped(store, series, request_id, answer, ID_A, ID_B)
    assert await _bar_count(store, series.entry_id) == 2, 'the new holder reached the old series'

    await _resume(store, series.entry_id)
    back, answer = await series.deliver(ID_A)

    assert answer.verdict is FINAL
    (run,) = await store.runs(back)
    assert (run.state, run.bars_written, run.error_reason, run.vendor_asset_id) == (
        IngestRunState.SUCCEEDED,
        2,
        None,
        ID_A,
    ), 'the earlier id is judged against the last accepted id and accepted'
    entry = await store.entry_row(series.entry_id)
    assert (entry.collect, entry.stopped_cause) == (True, None)
    assert await _bar_count(store, series.entry_id) == 4


async def test_p50_a_judged_empty_delivery_still_stores_its_id_and_is_the_base(store):
    """The other side of P50f: only a DROPPED attempt loses its id.

    A judged zero-bar success (EMPTY) keeps it as received and is an accepted base, so a later different id stops
    naming it.
    """
    series = await _series(store)
    first, _ = await series.deliver(ID_A, parts=[BarPage(bars=[])])
    (row,) = await store.runs(first)
    assert (row.state, row.vendor_asset_id, row.dataset_id) == (IngestRunState.EMPTY, ID_A, series.entry_id)
    assert await _base(store, series.entry_id) == ID_A

    request_id, answer = await series.deliver(ID_B)

    await _assert_refused_and_stopped(store, series, request_id, answer, ID_A, ID_B)


async def test_p50_a_static_entry_refuses_the_bars_and_fails_the_attempt_but_is_left_as_it_is(store):
    """AS BUILT, for the architect's ruling: STATIC (collect NULL, so it cannot carry a cause by CHECK).

    The bars are refused and the attempt row and request fail exactly as for a subscription; the entry keeps collect
    NULL and no cause; the event is UPDATED, not RETIRED. stop_entry returns False for it.
    """
    series = await _series(store, update_type=UpdateType.STATIC, collect=None)
    await series.deliver(ID_A)
    store.published.clear()

    request_id, answer = await series.deliver(ID_B)

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert await _bar_count(store, series.entry_id) == 2
    (run,) = await store.runs(request_id)
    assert (run.state, run.error_reason, run.error_domain, run.bars_written) == (IngestRunState.FAILED, MARKER, None, 0)
    row = await store.request_row(request_id)
    assert (row.state, row.failure_cause) == (IngestRequestState.FAILED, MARKER)
    entry = await store.entry_row(series.entry_id)
    assert (entry.collect, entry.stopped_cause) == (None, None), 'the STATIC entry was stopped (only updated_at moves)'
    assert store.published[-1] == DatasetChanged(series.entry_id, DatasetChange.UPDATED)
    async with AsyncSession(store.engine) as db:
        assert await ingest_delivery.stop_entry(db, series.entry_id, DatasetStopCause.VENDOR_ASSET_CHANGED) is False


async def test_p50_stop_entry_sets_collect_false_the_cause_and_updated_at_in_one_update(store):
    entry_id = await store.entry()
    async with AsyncSession(store.engine) as db:
        assert await ingest_delivery.stop_entry(db, entry_id, DatasetStopCause.VENDOR_ASSET_CHANGED) is True
        await db.commit()
    entry = await store.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == (False, 'VENDOR_ASSET_CHANGED')
    assert entry.updated_at != START
    async with AsyncSession(store.engine) as db:
        assert await ingest_delivery.stop_entry(db, uuid.uuid4(), DatasetStopCause.VENDOR_ASSET_CHANGED) is False


async def test_p50_the_check_still_refuses_a_cause_on_a_collecting_row(store):
    """ck_store_dataset_entry_stopped_cause_needs_stop holds: a Resume that forgot the cause is refused."""
    series = await _series(store)
    await series.deliver(ID_A)
    await series.deliver(ID_B)
    async with AsyncSession(store.engine) as db:
        with pytest.raises(sa.exc.IntegrityError):
            await db.execute(sa.update(ENTRIES).where(ENTRIES.c.id == series.entry_id).values(collect=True))
            await db.commit()


@pytest.mark.parametrize(
    ('sent', 'collect', 'cause', 'change'),
    [(True, True, None, DatasetChange.RESUMED), (False, False, MARKER, DatasetChange.UPDATED)],
    ids=['explicit-true-resumes', 'explicit-false-leaves-it'],
)
async def test_p50_a_post_repeat_with_explicit_collect_decides_the_cause_and_the_earlier_id_is_accepted(
    store, sent, collect, cause, change
):
    """A POST /store repeat (no dataset on the request: the upsert path) on a store-stopped entry, with the earlier id.

    Explicit true clears stopped_cause with the collect it sets (the CHECK refuses otherwise); explicit false leaves
    the stored cause. The delivery carries the last accepted id, so it is accepted and its bars are written.
    """
    entry_id = await store.entry(end=None, expiry_type=ExpiryType.ROLLING)
    series = Series(store, entry_id)
    await series.deliver(ID_A)
    await series.deliver(ID_B)
    store.published.clear()
    request_id = await _post_request(store, collect=sent)

    await store.deliver(with_trailer(with_header(report(request_id), vendor_asset_id=ID_A), as_of=LATER_AS_OF))

    assert (await store.request_row(request_id)).dataset_id == entry_id
    assert (await store.request_row(request_id)).state is IngestRequestState.SUCCEEDED
    entry = await store.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == (collect, cause)
    assert store.published[-1] == DatasetChanged(entry_id, change)


@pytest.mark.parametrize('sent', [True, None], ids=['explicit-true', 'omitted'])
async def test_p50_a_post_repeat_with_a_new_id_is_judged_and_stops_again(store, sent):
    """The POST path has no drop rule, so the compare runs: a new holder stops the dataset again, RETIRED."""
    entry_id = await store.entry(end=None, expiry_type=ExpiryType.ROLLING)
    series = Series(store, entry_id)
    await series.deliver(ID_A)
    await series.deliver(ID_B)
    store.published.clear()
    request_id = await _post_request(store, collect=sent)

    answer = await store.deliver(with_header(report(request_id), vendor_asset_id=ID_B))

    assert answer.verdict is FINAL
    entry = await store.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == (False, MARKER)
    (run,) = await store.runs(request_id)
    assert (run.state, run.error_reason) == (IngestRunState.FAILED, MARKER)
    assert await _bar_count(store, entry_id) == 2
    assert store.published[-1] == DatasetChanged(entry_id, DatasetChange.RETIRED)


async def test_p50_the_stopped_entry_reads_stopped_to_the_catalog_the_status_group_and_the_daily_loop(store):
    """Nothing new reads collect: is_retired (health, the status group, the refresh's owed set) sees the store's stop."""
    series = await _series(store)
    await series.deliver(ID_A)
    await series.deliver(ID_B)
    entry = await store.entry_row(series.entry_id)
    assert is_retired(SimpleNamespace(update_type=UpdateType(entry.update_type), collect=entry.collect)) is True
    async with AsyncSession(store.engine) as db:
        assert await ingest_delivery.lock_active_dataset_entry(db, series.entry_id) is False, 'the drop rule sees it'


async def test_p50_decide_answers_final_failed_with_the_cause_and_the_sentence():
    stop = delivery_intake.VendorAssetStop('VFV', ID_A, ID_B)
    decision = delivery_intake.decide(
        report(uuid.uuid4()), bars_written=0, has_records=True, superseded=False, vendor_asset_stop=stop
    )
    assert decision == delivery_intake.Decision(
        FINAL, IngestRequestState.FAILED, IngestRequestFailureCause.VENDOR_ASSET_CHANGED, stop.sentence
    )
    assert IngestRequestFailureCause.VENDOR_ASSET_CHANGED.value == MARKER
    assert VENDOR_ASSET_CHANGED_REASON == MARKER and DatasetStopCause.VENDOR_ASSET_CHANGED.value == MARKER


async def test_p50_the_refresh_permanence_read_tolerates_the_marker(store):
    """The refresh's permanence read needs the vendor error domain: the marker (domain NULL) is no permanent refusal."""
    series = await _series(store)
    await series.deliver(ID_A)
    request_id, _ = await series.deliver(ID_B)
    (row,) = await store.runs(request_id)
    run = dataclasses.replace(IngestRunRecord.from_row(row), request_failure_cause=MARKER)
    assert (run.error_domain, run.error_reason) == (None, MARKER)
    assert daily_refresh._unproductive(run, row.requested_start) is False
