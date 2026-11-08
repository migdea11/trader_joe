"""The name_change clamp (P51, tj-7zjov1.146) on the real, migrated Postgres: the read and a clamped POST.

WRITTEN, NOT YET RUN by its author (no Postgres where it was written). It runs with the rest of the suite (make
test-system, or agent-stack run_system_tests after test_up) on a stack migrated to the head (c7e19a4d5b28 or later).

THE DESIGN: ADR tj-26jt9w addendum 19 (the architect's ruling A on tj-7zjov1.146, 2026-10-27 01:29 UTC) and the
validator brief on tj-7zjov1.147. WHAT THIS PROVES that server/data/store/tests cannot, because the read is Postgres-only
(JSONB ->>, DISTINCT ON) and the SQLite tiers answer it from a book:
  NC1 name_changes_naming over REAL corporate_action rows delivered by SEVERAL requests through the real intake: every
      name_change naming the symbol as old or new symbol is read whichever request delivered it; one row per vendor
      action id, the NEWEST observation by as_of (a revision delivered later with an OLDER as_of does not win); a row
      with no process date is read (the rule ignores it); another type and a record naming other symbols are not.
  NC2 the strictly-later rule on those rows: an arrival on the same process date keeps the candidate, one a day later
      drops it.
  NC3 a POST through the real tables (request rows, the intake's corporate-actions and bars arms, the real entry
      upsert), ingest played by a fake that answers the query with NO record: a name_change stored by an EARLIER
      request clamps the start to the local midnight of 2022-06-09 in XNYS; requested_start and the entry carry it;
      an EXACT REPEAT of the body clamps to the same start and extends the same entry with no 409.

HOW. Like test_delivery_intake_pg.py: DeliveryIntake and data_action_request are driven directly on the test stack's
Postgres over asyncpg (pg_async_engine), with every own_session pointed at that engine. Symbols and vendor action ids
carry this run's id (the dedupe index is global and the table append-only); the fixture deletes this test's request
rows, attempt rows and observations by symbol, and the entries go through entry_registry. A first sighting of an
instance is GLOBAL by design: safe on the disposable stack this suite requires.
"""

import functools
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app.corporate_actions import name_change_clamp
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.crud.corporate_action import NameChange, name_changes_naming
from data.store.app.database.models.corporate_action import CorporateAction
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestKind, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRun, IngestRunTrigger
from data.store.app.ingest import data_action_request, delivery_intake
from data.store.app.ingest.data_action_request import new_corporate_actions_request, store_market_activity_worker
from data.store.app.ingest.delivery_intake import DeliveryIntake
from data.store.app.trading_calendar import calendar_for_source, local_midnight
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    CorporateActionRecord,
    CorporateActionRecords,
    DeliveryHeader,
    DeliveryOutcome,
    DeliveryTrailer,
    RegisterInstanceRequest,
    VendorCallStats,
)
from schemas.data_ingest.fetch_dataset import ServedRange
from schemas.data_ingest.submit import SubmitAccepted, SubmitAck, SubmitRequest, SubmitResponse
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

REQUESTS = IngestRequest.__table__
RUNS = IngestRun.__table__
INSTANCES = IngestInstance.__table__
ACTIONS = CorporateAction.__table__
ENTRIES = sa.table('store_dataset_entry', sa.column('id'), sa.column('start'), sa.column('asset_symbol'))
QUOTA_KEY = 'system-test-quota-key'
AS_OF = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
ASKED = datetime(2015, 1, 2, tzinfo=UTC)
CLAMPED = local_midnight(date(2022, 6, 9), calendar_for_source(DataSource.ALPACA_API))


def _day(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def stats() -> VendorCallStats:
    return VendorCallStats(
        calls=1, pages=1, throttled_429=0, retries=0, first_call_at=AS_OF, last_call_at=AS_OF + timedelta(seconds=1)
    )


def rename(vendor_id: str, old: str, new: str, process_date: datetime | None, tag: str = 'v1', **fields):
    """A vendor name_change record, raw bytes distinct per tag (a revision of the same id is other bytes)."""
    return CorporateActionRecord(
        vendor_action_id=vendor_id,
        action_type=fields.pop('action_type', 'name_change'),
        process_date=process_date,
        identifiers={'old_symbol': old, 'new_symbol': new},
        raw_payload=f'{{"id":"{vendor_id}","tag":"{tag}"}}'.encode(),
        **fields,
    )


def actions_report(request_id, records, *, instance_id: str, as_of: datetime = AS_OF) -> AttemptDelivery:
    return AttemptDelivery(
        header=DeliveryHeader(
            request_id=str(request_id), instance_id=instance_id, attempt_no=1, quota_key=QUOTA_KEY, feed=None
        ),
        parts=[CorporateActionRecords(records=records)] if records else [],
        trailer=DeliveryTrailer(
            outcome=DeliveryOutcome.SUCCEEDED,
            vendor_stats=stats(),
            served_range=ServedRange(start=_day(1990, 1, 1), end=AS_OF),
            as_of=as_of,
        ),
    )


def bars_report(request: SubmitRequest, *, instance_id: str) -> AttemptDelivery:
    """A served bars window with no bar in it: the entry is still written on this first delivered attempt."""
    end = request.bars.end or request.bars.start + timedelta(days=5)
    return AttemptDelivery(
        header=DeliveryHeader(
            request_id=request.request_id,
            instance_id=instance_id,
            attempt_no=1,
            quota_key=QUOTA_KEY,
            feed=Feed.IEX,
            vendor_asset_id='',
        ),
        parts=[],
        trailer=DeliveryTrailer(
            outcome=DeliveryOutcome.SUCCEEDED,
            vendor_stats=stats(),
            served_range=ServedRange(start=request.bars.start, end=end),
            as_of=AS_OF,
        ),
    )


# --------------------------------------------------------------------------------------------- fixtures


class Created:
    """The symbols and instances one test used: its request rows, attempt rows and observations are deleted after."""

    def __init__(self) -> None:
        self.symbols: list[str] = []
        self.instances: list[str] = []


@pytest.fixture
def created(pg_engine: Engine) -> Iterator[Created]:
    ids = Created()
    yield ids
    with pg_engine.begin() as conn:
        if ids.symbols:
            mine = sa.select(REQUESTS.c.id).where(REQUESTS.c.asset_symbol.in_(ids.symbols)).scalar_subquery()
            # corporate_action is append-only for the service; the test removes only the observations it delivered.
            conn.execute(sa.delete(ACTIONS).where(ACTIONS.c.request_id.in_(mine)))
            conn.execute(sa.delete(RUNS).where(RUNS.c.request_id.in_(mine)))
            conn.execute(sa.delete(REQUESTS).where(REQUESTS.c.asset_symbol.in_(ids.symbols)))
        if ids.instances:
            conn.execute(sa.delete(INSTANCES).where(INSTANCES.c.instance_id.in_(ids.instances)))


class World:
    """The intake and the request path on the test stack's Postgres."""

    def __init__(self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
        self.engine = engine

        @asynccontextmanager
        async def own_session() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session

        for module in (delivery_intake, data_action_request):
            monkeypatch.setattr(module, 'own_session', own_session)
        monkeypatch.setattr(delivery_intake, 'publish', lambda _event: None)
        self.intake = DeliveryIntake(chunk_size=100)

    async def register(self, instance_id: str) -> None:
        await self.intake.register_instance(RegisterInstanceRequest(instance_id=instance_id, started_at=AS_OF))

    async def query_request(self, symbol: str, holder: str, label: str) -> uuid.UUID:
        """A corporate-actions request as a POST writes it, QUEUED under the holder: an EARLIER request's query."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            record = await request_crud.create_request(
                db,
                new_corporate_actions_request(
                    DataSource.ALPACA_API,
                    symbol,
                    _day(1990, 1, 1),
                    AS_OF,
                    trigger=IngestRunTrigger.REQUEST,
                    client_label=label,
                ),
            )
            await db.execute(
                sa.update(REQUESTS)
                .where(REQUESTS.c.id == record.id)
                .values(state=IngestRequestState.QUEUED, instance_id=holder)
            )
            await db.commit()
        return record.id

    async def store(self, symbol: str, holder: str, label: str, records, as_of: datetime = AS_OF) -> uuid.UUID:
        """One earlier request delivers these records through the real corporate-actions arm."""
        request_id = await self.query_request(symbol, holder, label)
        await self.intake.deliver(actions_report(request_id, records, instance_id=holder, as_of=as_of))
        return request_id

    async def read(self, symbol: str) -> list[NameChange]:
        async with AsyncSession(self.engine) as db:
            return await name_changes_naming(db, DataSource.ALPACA_API, symbol)

    async def rows(self, symbol: str, kind: IngestRequestKind) -> list[sa.Row]:
        async with self.engine.connect() as conn:
            statement = (
                sa.select(REQUESTS)
                .where(REQUESTS.c.asset_symbol == symbol, REQUESTS.c.kind == kind)
                .order_by(REQUESTS.c.created_at)
            )
            return list((await conn.execute(statement)).all())


@pytest_asyncio.fixture
async def world(pg_async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(pg_async_engine, monkeypatch)


@pytest.fixture
def holder(run_identity, created: Created) -> Callable[[str], str]:
    def _mint(name: str) -> str:
        minted = f'system-test-{run_identity.run_id}-{name}-{uuid.uuid4().hex[:6]}'
        created.instances.append(minted)
        return minted

    return _mint


@pytest.fixture
def symbol(own_symbol: str, created: Created) -> str:
    created.symbols.append(own_symbol)
    return own_symbol


def _vendor_id(run_identity, name: str) -> str:
    return f'nc-{run_identity.run_id}-{name}-{uuid.uuid4().hex[:6]}'


# --------------------------------------------------------------------------------------------- NC1, NC2: the read


async def test_nc1_the_read_takes_every_request_newest_observation_per_id_naming_the_symbol(
    world, created, run_identity, symbol, holder
):
    instance = holder('nc1')
    await world.register(instance)
    away, arrive, undated, other, merger = (
        _vendor_id(run_identity, name) for name in ('away', 'arrive', 'undated', 'other', 'merger')
    )
    label = run_identity.owner
    # Request 1 delivers five records at AS_OF.
    await world.store(
        symbol,
        instance,
        label,
        [
            rename(away, symbol, f'{symbol}N', _day(2018, 3, 1)),
            rename(arrive, f'{symbol}O', symbol, _day(2016, 1, 4)),
            rename(undated, symbol, f'{symbol}U', None),
            rename(other, f'{symbol}Q', f'{symbol}R', _day(2020, 2, 3)),
            rename(merger, symbol, f'{symbol}W', _day(2024, 1, 2), action_type='stock_merger'),
        ],
    )
    # Request 2: a vendor revision of `away` (other bytes), answered a day LATER: the newest observation.
    await world.store(
        symbol, instance, label, [rename(away, symbol, f'{symbol}N', _day(2021, 5, 3), tag='v2')], AS_OF + timedelta(1)
    )
    # Request 3, delivered LAST but answered a day EARLIER: an older observation, which must not win.
    await world.store(
        symbol, instance, label, [rename(away, symbol, f'{symbol}N', _day(2023, 6, 1), tag='v0')], AS_OF - timedelta(1)
    )

    changes = sorted(await world.read(symbol), key=lambda change: change.vendor_action_id)

    assert changes == sorted(
        [
            NameChange(away, _day(2021, 5, 3), symbol, f'{symbol}N'),
            NameChange(arrive, _day(2016, 1, 4), f'{symbol}O', symbol),
            NameChange(undated, None, symbol, f'{symbol}U'),
        ],
        key=lambda change: change.vendor_action_id,
    ), 'not one newest observation per id of every name_change naming the symbol, from every request'
    assert name_change_clamp(changes, symbol) == _day(2021, 5, 3), 'the earlier arrival must not drop it'


async def test_nc2_an_arrival_on_the_same_day_keeps_the_candidate_and_a_day_later_drops_it(
    world, created, run_identity, symbol, holder
):
    instance = holder('nc2')
    await world.register(instance)
    label = run_identity.owner
    await world.store(
        symbol, instance, label, [rename(_vendor_id(run_identity, 'away'), symbol, f'{symbol}N', _day(2021, 5, 3))]
    )
    await world.store(
        symbol, instance, label, [rename(_vendor_id(run_identity, 'same'), f'{symbol}P', symbol, _day(2021, 5, 3))]
    )

    assert name_change_clamp(await world.read(symbol), symbol) == _day(2021, 5, 3)

    await world.store(
        symbol, instance, label, [rename(_vendor_id(run_identity, 'back'), f'{symbol}Z', symbol, _day(2021, 5, 4))]
    )

    assert name_change_clamp(await world.read(symbol), symbol) is None


# --------------------------------------------------------------------------------------------- NC3: the clamped POST


class PlayedIngest:
    """ingest: accepts each Submit and delivers its report through the real intake inside the waiter's next sleep.

    The query is answered with NO record (the observation is already stored, under an earlier request), the bars with
    an empty served window equal to the row's.
    """

    def __init__(self, world: World, instance_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.world = world
        self.instance_id = instance_id
        self.sent: list[SubmitRequest] = []
        self.queued: list[AttemptDelivery] = []

        async def sleep(_seconds: float) -> None:
            while self.queued:
                await world.intake.deliver(self.queued.pop(0))

        monkeypatch.setattr(
            data_action_request, 'await_verdict', functools.partial(data_action_request.await_verdict, sleep=sleep)
        )

    async def submit(self, request: SubmitRequest) -> SubmitResponse:
        self.sent.append(request)
        if request.corporate_actions is not None:
            self.queued.append(actions_report(request.request_id, [], instance_id=self.instance_id))
            accepted = SubmitAccepted(feed=None, quota_key=QUOTA_KEY)
        else:
            self.queued.append(bars_report(request, instance_id=self.instance_id))
            accepted = SubmitAccepted(feed=Feed.IEX, quota_key=QUOTA_KEY)
        return SubmitResponse(ack=SubmitAck(accepted=accepted), instance_id=self.instance_id)

    async def instance(self):
        raise AssertionError('the POST never probes the instance')


async def test_nc3_a_record_stored_by_an_earlier_request_clamps_the_post_and_an_exact_repeat_extends_its_entry(
    world, created, run_identity, symbol, holder, entry_registry, monkeypatch
):
    instance = holder('nc3')
    await world.register(instance)
    owner = run_identity.owner
    earlier = await world.store(
        symbol, instance, owner, [rename(_vendor_id(run_identity, 'away'), symbol, f'{symbol}M', _day(2022, 6, 9))]
    )
    ingest = PlayedIngest(world, instance, monkeypatch)
    path = StoreAssetDatasetPath(asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol=symbol)
    body = StoreAssetDatasetBody(
        owner=owner,
        source=DataSource.ALPACA_API,
        granularity=Granularity.ONE_DAY,
        start=ASKED,
        update_type=UpdateType.DAILY,
        expiry_type=ExpiryType.ROLLING,
    )

    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        first = await store_market_activity_worker(path, body, db, ingest, deadline_s=30)
    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        second = await store_market_activity_worker(path, body, db, ingest, deadline_s=30)

    bars_rows = await world.rows(symbol, IngestRequestKind.BARS)
    for row in bars_rows:
        if row.dataset_id is not None:
            entry_registry.add(row.dataset_id)
    assert first.clamp is not None and first.clamp.clamped_start == CLAMPED
    assert second.clamp == first.clamp, 'an exact repeat did not clamp to the same start'
    assert [row.requested_start for row in bars_rows] == [CLAMPED, CLAMPED]
    assert len({row.dataset_id for row in bars_rows}) == 1, 'the repeat did not extend the same entry'
    async with world.engine.connect() as conn:
        (entry_start,) = (
            await conn.execute(sa.select(ENTRIES.c.start).where(ENTRIES.c.id == bars_rows[0].dataset_id))
        ).one()
    assert entry_start == CLAMPED, 'the entry does not declare the clamped start'
    queries = await world.rows(symbol, IngestRequestKind.CORPORATE_ACTIONS)
    assert queries[0].id == earlier and len(queries) == 3, 'one query per POST beside the earlier one'
    assert all(row.state is IngestRequestState.EMPTY for row in queries[1:]), 'the POSTs did not await their queries'
    assert [request.bars.start for request in ingest.sent if request.bars is not None] == [CLAMPED, CLAMPED]
