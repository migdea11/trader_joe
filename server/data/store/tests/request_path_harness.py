"""The SQLite harness for the request path tests of P35 (tj-7zjov1.28). Not a test module.

What it stands up, for test_post_request_path.py, test_await_verdict.py, test_instance_watch.py and
test_daily_loop_requests.py:

  * one SQLite engine (aiosqlite, StaticPool) holding ingest_request, ingest_run, ingest_instance and
    store_dataset_entry, built from the models (data_types swapped to JSON: SQLite has no ARRAY), with timestamptz
    read back aware as asyncpg reads it;
  * every own_session the request path opens (data_action_request, delivery_intake, instance_watch) pointed at it;
  * the delivery intake with its two Postgres-only writes replaced on the SAME session, exactly as
    test_delivery_intake.py does: the bar upsert by an insert into a scratch table, the entry upsert by SQLite's
    ON CONFLICT on the eleven identity columns. The real ones run in tests/system;
  * FakeSubmit, a test double of the IngestSubmitClient interface: it records every SubmitRequest, answers accepted
    (or refused, or raises) as the test sets it, and can run a hook between being called and answering, which is
    where a test plays ingest (delivers an attempt) or another process (a new instance registering).

Kept beside the tests rather than in conftest.py: the shared fixtures are another validator's (part A) scope.
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import DATETIME
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from common.database.sql_alchemy_nullable_datetime import NullableDateTime
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import ERROR_DOMAIN
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest
from data.store.app.database.models.ingest_run import IngestRun
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.ingest import data_action_request, delivery_intake, instance_watch
from data.store.app.ingest.delivery_intake import DeliveryIntake
from schemas.data_ingest.delivery import DeliverAttemptResponse, RegisterInstanceRequest
from schemas.data_ingest.fetch_dataset import FetchRefused
from schemas.data_ingest.submit import InstanceResponse, SubmitAccepted, SubmitAck, SubmitRequest, SubmitResponse
from schemas.data_store.asset_dataset_store import AssetDatasetStoreCreate


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
)

OWNER = 'owner-sentinel-7c1d'
QUOTA_KEY = 'quota-key-sentinel-91f3'
START = datetime(2026, 2, 2, tzinfo=UTC)
INSTANCE_A = 'instance-a'
INSTANCE_B = 'instance-b'


def patch_aware_sqlite_datetimes(monkeypatch: pytest.MonkeyPatch) -> None:
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


async def make_engine(monkeypatch: pytest.MonkeyPatch) -> AsyncEngine:
    patch_aware_sqlite_datetimes(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)
    monkeypatch.setattr(REQUESTS.c.data_types, 'type', sa.JSON())
    metadata = sa.MetaData()
    for table in (REQUESTS, RUNS, INSTANCES, ENTRIES):
        table.to_metadata(metadata)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.run_sync(_SCRATCH.create_all)
    return engine


class FakeSubmit:
    """The IngestSubmitClient interface, as a double. Structural: it implements submit and instance."""

    def __init__(self, instance_id: str = INSTANCE_A) -> None:
        self.instance_id = instance_id
        self.sent: list[SubmitRequest] = []
        self.probes = 0
        # Set one of these to change the answer: a refusal reason, or an exception the call raises.
        self.refuse_with: str | None = None
        self.raises: Exception | None = None
        self.probe_raises: Exception | None = None
        # Runs after the request is recorded and before the answer: a test plays ingest or another process here.
        self.on_submit: Callable[[SubmitRequest], Awaitable[None]] | None = None

    async def submit(self, request: SubmitRequest) -> SubmitResponse:
        self.sent.append(request)
        if self.on_submit is not None:
            await self.on_submit(request)
        if self.raises is not None:
            raise self.raises
        if self.refuse_with is not None:
            ack = SubmitAck(
                refused=FetchRefused(
                    reason=self.refuse_with, domain=ERROR_DOMAIN, detail='the feed asked for is not served', metadata={}
                )
            )
        else:
            ack = SubmitAck(accepted=SubmitAccepted(feed=Feed.IEX, quota_key=QUOTA_KEY))
        return SubmitResponse(ack=ack, instance_id=self.instance_id)

    async def instance(self) -> InstanceResponse:
        self.probes += 1
        if self.probe_raises is not None:
            raise self.probe_raises
        return InstanceResponse(instance_id=self.instance_id, started_at=START)


class Harness:
    """The request path, the intake and the watcher on one SQLite engine."""

    def __init__(self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
        self.engine = engine
        self.published: list[object] = []
        self.batches: list[object] = []
        self.upserts: list[tuple[AssetDatasetStoreCreate, bool | None, bool]] = []

        @asynccontextmanager
        async def own_session() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session

        async def write_bars(db: AsyncSession, batch, requested_chunk_size: int | None = None) -> int:
            self.batches.append(batch)
            rows = batch.dataset.get(DataType.MARKET_ACTIVITY, [])
            for row in rows:
                await db.execute(sa.insert(FAKE_BARS).values(dataset_id=batch.dataset_id, timestamp=row.timestamp))
            return len(rows)

        async def upsert_entry(
            db: AsyncSession, entry: AssetDatasetStoreCreate, *, preserve_lifecycle: bool = False, collect=None
        ) -> uuid.UUID:
            self.upserts.append((entry, collect, preserve_lifecycle))
            values = StoreDatasetEntry.get_fields(entry, exclude_none=True)
            resolved = None if entry.update_type is UpdateType.STATIC else (True if collect is None else collect)
            on_repeat: dict = {'expiry': values.get('expiry'), 'updated_at': sa.func.now()}
            if collect is not None and resolved is not None:
                on_repeat['collect'] = resolved
            statement = (
                sqlite_insert(ENTRIES)
                .values(id=uuid.uuid4(), **values, collect=resolved, created_at=sa.func.now(), updated_at=sa.func.now())
                .on_conflict_do_update(index_elements=list(StoreDatasetEntry.NATURAL_KEY), set_=on_repeat)
                .returning(ENTRIES.c.id)
            )
            return (await db.execute(statement)).scalar_one()

        for module in (data_action_request, delivery_intake, instance_watch):
            monkeypatch.setattr(module, 'own_session', own_session)
        monkeypatch.setattr(delivery_intake, 'publish', self.published.append)
        monkeypatch.setattr(delivery_intake, 'write_market_activity_in_transaction', write_bars)
        monkeypatch.setattr(delivery_intake, 'upsert_entry_in_transaction', upsert_entry)
        self.intake = DeliveryIntake(chunk_size=10)
        self.session = own_session

    # ------------------------------------------------------------------ the intake

    async def deliver(self, attempt) -> DeliverAttemptResponse:
        return await self.intake.deliver(attempt)

    async def register(self, instance_id: str) -> None:
        await self.intake.register_instance(RegisterInstanceRequest(instance_id=instance_id, started_at=START))

    # ------------------------------------------------------------------ writes behind the code's back

    async def set_request(self, request_id: uuid.UUID, **values) -> None:
        async with AsyncSession(self.engine) as db:
            await db.execute(sa.update(REQUESTS).where(REQUESTS.c.id == request_id).values(**values))
            await db.commit()

    async def set_instance(self, instance_id: str, **values) -> None:
        async with AsyncSession(self.engine) as db:
            await db.execute(sa.update(INSTANCES).where(INSTANCES.c.instance_id == instance_id).values(**values))
            await db.commit()

    async def entry(self, **overrides) -> uuid.UUID:
        entry_id = uuid.uuid4()
        values = {
            'id': entry_id,
            'owner': OWNER,
            'source': DataSource.ALPACA_API,
            'feed': Feed.IEX,
            'asset_symbol': 'VFV',
            'asset_type': AssetType.STOCK,
            'data_type': DataType.MARKET_ACTIVITY,
            'granularity': Granularity.ONE_DAY,
            'start': START,
            'end': NullableDateTime.EPOCH,
            'expiry_type': ExpiryType.ROLLING,
            'update_type': UpdateType.DAILY,
            'collect': True,
            'created_at': START,
            'updated_at': START,
            **overrides,
        }
        async with AsyncSession(self.engine) as db:
            db.add(StoreDatasetEntry(**values))
            await db.commit()
        return entry_id

    # ------------------------------------------------------------------ reads, from a second session

    async def request_row(self, request_id: uuid.UUID) -> sa.Row:
        async with AsyncSession(self.engine) as db:
            return (await db.execute(sa.select(REQUESTS).where(REQUESTS.c.id == request_id))).one()

    async def requests(self) -> list[sa.Row]:
        async with AsyncSession(self.engine) as db:
            return list((await db.execute(sa.select(REQUESTS).order_by(REQUESTS.c.created_at))).all())

    async def runs(self, request_id: uuid.UUID) -> list[sa.Row]:
        async with AsyncSession(self.engine) as db:
            statement = sa.select(RUNS).where(RUNS.c.request_id == request_id).order_by(RUNS.c.attempt_no)
            return list((await db.execute(statement)).all())

    async def entries(self) -> list[sa.Row]:
        async with AsyncSession(self.engine) as db:
            return list((await db.execute(sa.select(ENTRIES))).all())

    async def instance(self, instance_id: str) -> sa.Row | None:
        async with AsyncSession(self.engine) as db:
            return (await db.execute(sa.select(INSTANCES).where(INSTANCES.c.instance_id == instance_id))).one_or_none()


def aged(at: datetime, seconds: float) -> datetime:
    return at - timedelta(seconds=seconds)
