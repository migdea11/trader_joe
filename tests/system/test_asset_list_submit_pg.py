"""An asset-list Submit round trip into the REAL delivery intake on the stack's Postgres (P12s, tj-7zjov1.125).

THE DESIGN: the bead's acceptance (an asset_list Submit is accepted and queued) with the architect's additions on it:
the 21:56 UTC served-range seam ruling (an asset list has no served range; rule (i) of validate_report excludes it by
name; the runner sends none), the 23:33 UTC note (the request goes through the store's own new_asset_list_request and
submit_request_of for BOTH lists; the snapshot lands under the list asked for, an EMPTY inactive list included) and the
00:46 UTC note (a recovered asset_list Submit is accepted as a recovered bars one is); ADR tj-26jt9w addenda 17 and 18.

WHAT IS REAL. The store's half: the request row written by new_asset_list_request through create_request and
mark_queued, sent as submit_request_of builds it, and every report written by the real DeliveryIntake (validate_report
and the asset-list arm) on the real migrated tables. Ingest's half is production code end to end except the reader:
the production IngestSubmitHandler routes the Submit to a production BrokerHandle, whose production Scheduler (built
through SchedulerBuilder) queues it and whose production AckGatedExecutor runs it through the production
AssetListAttemptRunner, reading a paced FakeRead (tests/fakes/market_data.py) over a one-day world.

WHY NOT tests.fakes.ingest.FakeIngest: it imports the Alpaca handle module, which imports alpaca-py, and the test_client
image installs the base, data-store and testing groups only (pyproject.toml; data-ingest holds alpaca-py). So the handle
is assembled here as FakeIngest assembles it, minus the Alpaca module, as tests/system/test_corporate_actions_submit_pg.py
does: the asset-list intake is restated (the trading scope, BACKFILL, no tape, as AlpacaHandle's and FakeIngest's,
which test_fake_ingest_intakes_pinned.py keeps equal), and the cost function is the production estimate for this
kind, one call (alpaca/handle.py estimate_request_calls; pinned in test_alpaca_asset_handle.py).

ISOLATION. The diff base of a snapshot is the newest non-empty snapshot of its (source, list), shared by every writer,
so these tests use the source MANUAL_ENTRY, which no service writes asset lists for (as test_asset_list_pg.py), and
delete exactly the requests, attempt rows, snapshots, rows, events and instances they wrote. A first sighting of an
instance is GLOBAL by design; safe on the disposable stack this suite requires. Run by make test-system and the
agent-stack MCP's run_system_tests.

THE CASES:
  (1) per list: accepted, read once as that list, written FINAL: the attempt row SUCCEEDED with no served range, no
      vendor asset id and the trading quota key; the snapshot under the list asked for with every row, in order; the
      request SUCCEEDED;
  (2) an EMPTY inactive list: a success with no parts, an inactive zero-row snapshot, the attempt row and request EMPTY;
  (3) a replay of the delivered report writes nothing twice and repeats its answer, and the same report judged as a
      corporate-actions report is refused by rule (i), so (1) guards that seam rather than passing around it;
  (4) a recovered asset_list Submit (count 1) is accepted, read and written as attempt 2.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource, Feed
from common.errors.vocabulary import InvalidRequestError, TraderJoeError
from data.ingest.app.brokers.alpaca.asset_attempt import AssetListAttemptRunner, AssetListWork
from data.ingest.app.brokers.handle import BrokerHandle, Intake, no_body
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler import (
    AckGatedExecutor,
    Credential,
    ExecutorSettings,
    Limit,
    RetryPolicy,
    SchedulerBuilder,
    StaticCredentialProvider,
)
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.models.asset_list import AssetListEvent, AssetListRow, AssetListSnapshot
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestKind, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.ingest import delivery_intake
from data.store.app.ingest.data_action_request import new_asset_list_request, submit_request_of
from data.store.app.ingest.delivery_intake import DeliveryIntake, validate_report
from routers.data_ingest.submit_handler import IngestSubmitHandler
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    DeliverAttemptResponse,
    DeliveryOutcome,
    DeliveryVerdict,
    RegisterInstanceRequest,
)
from schemas.data_ingest.submit import AssetListStatus, RequestKind, SubmitRequest
from tests.fakes.market_data import AssetDay, FakeRead, FakeWorld, WorldAsset


# A first sighting supersedes the stack's live ingest too; the fixture makes it live again (tj-7zjov1.233).
pytestmark = [pytest.mark.data_store, pytest.mark.asyncio, pytest.mark.usefixtures('live_instances_kept')]

REQUESTS = IngestRequest.__table__
RUNS = IngestRun.__table__
INSTANCES = IngestInstance.__table__
SNAPSHOTS, ROWS, EVENTS = AssetListSnapshot.__table__, AssetListRow.__table__, AssetListEvent.__table__
SOURCE = DataSource.MANUAL_ENTRY
# The handle's scopes, named as Alpaca's are (a scope starts with its source's value), on this test's own source.
DATA_SCOPE = f'{SOURCE.value}_DATA'
TRADING_SCOPE = f'{SOURCE.value}_TRADING'
ACTIVE, INACTIVE = AssetListStatus.ACTIVE, AssetListStatus.INACTIVE
# Every wait for the code under test is bounded by this, so a regression fails rather than hangs.
GUARD_S = 20.0
FAKE_CREDENTIAL = Credential(key_id='system-test-key-id', secret='not-a-vendor-secret')
OWNER = 'world-manager'

_LISTED = (
    WorldAsset('zzsubmit-a', 'ZZSUBMITA', 'Synthetic A'),
    WorldAsset('zzsubmit-b', 'ZZSUBMITB', ''),
    WorldAsset('zzsubmit-c', 'ZZSUBMITC', 'Synthetic C', 'ARCA'),
)
_DELISTED = (WorldAsset('zzsubmit-d', 'ZZSUBMITD', 'Synthetic D', 'OTC'), WorldAsset('zzsubmit-e', 'ZZSUBMITE', 'E'))
# One day, so the fake's clock always serves it (the world clamps to its days).
BOTH_LISTS = FakeWorld((AssetDay(date(2026, 10, 26), active=_LISTED, inactive=_DELISTED),), {}, ())
NO_INACTIVE = FakeWorld((AssetDay(date(2026, 10, 26), active=_LISTED, inactive=()),), {}, ())


# --------------------------------------------------------------------------------------------- the store's half


class Created:
    """The ids one test wrote, deleted at its end: nothing this run did not write is deleted."""

    def __init__(self) -> None:
        self.requests: list[uuid.UUID] = []
        self.instances: list[str] = []


@pytest.fixture
def created(pg_engine: Engine) -> Iterator[Created]:
    ids = Created()
    yield ids
    with pg_engine.begin() as conn:
        if ids.requests:
            snapshots = sa.select(SNAPSHOTS.c.id).where(SNAPSHOTS.c.request_id.in_(ids.requests))
            conn.execute(sa.delete(EVENTS).where(EVENTS.c.snapshot_id.in_(snapshots)))
            conn.execute(sa.delete(ROWS).where(ROWS.c.snapshot_id.in_(snapshots)))
            conn.execute(sa.delete(SNAPSHOTS).where(SNAPSHOTS.c.request_id.in_(ids.requests)))
            conn.execute(sa.delete(RUNS).where(RUNS.c.request_id.in_(ids.requests)))
            conn.execute(sa.delete(REQUESTS).where(REQUESTS.c.id.in_(ids.requests)))
        if ids.instances:
            conn.execute(sa.delete(INSTANCES).where(INSTANCES.c.instance_id.in_(ids.instances)))


class RecordingIntake:
    """The real DeliveryIntake on this stack's Postgres, standing as ingest's StoreDeliveryClient.

    Every answer and every refusal is recorded, with a signal on each, so a refusal is reported by the test rather
    than redelivered silently.
    """

    def __init__(self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
        self.engine = engine
        self.answers: list[DeliverAttemptResponse] = []
        self.refusals: list[TraderJoeError] = []
        self.delivered: list[AttemptDelivery] = []
        self.signal = asyncio.Event()

        @asynccontextmanager
        async def own_session() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session

        monkeypatch.setattr(delivery_intake, 'own_session', own_session)
        monkeypatch.setattr(delivery_intake, 'publish', lambda event: None)
        self.intake = DeliveryIntake()

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        self.delivered.append(attempt)
        try:
            answer = await self.intake.deliver(attempt)
        except TraderJoeError as refused:
            self.refusals.append(refused)
            self.signal.set()
            raise
        self.answers.append(answer)
        self.signal.set()
        return answer

    async def register_instance(self, request: RegisterInstanceRequest) -> None:
        await self.intake.register_instance(request)


@pytest_asyncio.fixture
async def store(pg_async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> RecordingIntake:
    return RecordingIntake(pg_async_engine, monkeypatch)


async def _all(engine: AsyncEngine, statement) -> list[sa.Row]:
    async with engine.connect() as conn:
        return list((await conn.execute(statement)).all())


async def _snapshots(engine: AsyncEngine, request_id: uuid.UUID) -> list[sa.Row]:
    return await _all(engine, sa.select(SNAPSHOTS).where(SNAPSHOTS.c.request_id == request_id))


async def _symbols(engine: AsyncEngine, snapshot_id: uuid.UUID) -> list[str]:
    statement = sa.select(ROWS.c.symbol).where(ROWS.c.snapshot_id == snapshot_id)
    return [row.symbol for row in await _all(engine, statement)]


async def _written(engine: AsyncEngine, request_id: uuid.UUID) -> tuple:
    """Everything a delivery of this request writes: the request, its attempt rows, snapshots, rows and events."""
    snapshots = await _snapshots(engine, request_id)
    ids = [snapshot.id for snapshot in snapshots]
    return (
        [tuple(row) for row in await _all(engine, sa.select(REQUESTS).where(REQUESTS.c.id == request_id))],
        [tuple(row) for row in await _all(engine, sa.select(RUNS).where(RUNS.c.request_id == request_id))],
        [tuple(row) for row in snapshots],
        sorted(tuple(row) for row in await _all(engine, sa.select(ROWS).where(ROWS.c.snapshot_id.in_(ids)))),
        sorted(tuple(row) for row in await _all(engine, sa.select(EVENTS).where(EVENTS.c.snapshot_id.in_(ids)))),
    )


async def _request(engine: AsyncEngine, request_id: uuid.UUID) -> sa.Row:
    (row,) = await _all(engine, sa.select(REQUESTS).where(REQUESTS.c.id == request_id))
    return row


# --------------------------------------------------------------------------------------------- ingest's half


def _asset_list_intake(request: SubmitRequest) -> Intake:
    """AlpacaHandle's asset-list intake, as tests/fakes/ingest.py restates it: trading scope, BACKFILL, no tape."""
    listing = request.asset_list
    if listing is None:
        raise no_body(request.kind)
    return Intake(scope=TRADING_SCOPE, payload=AssetListWork(request=listing, priority=RequestPriority.BACKFILL))


class Ingest:
    """One ingest process's asset-list path, assembled from production parts around a paced FakeRead."""

    def __init__(self, store: RecordingIntake, world: FakeWorld, instance_id: str) -> None:
        minute = timedelta(minutes=1)
        builder = SchedulerBuilder()
        for scope in (DATA_SCOPE, TRADING_SCOPE):
            builder.declare_scope(scope, [Limit(minute, 200)]).set_limit(minute, 200, scope=scope)
        builder.cost_function(lambda request, page_size: 1, page_size=10_000)
        builder.credential_provider(StaticCredentialProvider(FAKE_CREDENTIAL))
        builder.on_expired(self.__report_expired)
        self.scheduler = builder.build()
        self.reader = FakeRead(world=world, scheduler=self.scheduler, scope=DATA_SCOPE, trading_scope=TRADING_SCOPE)
        executor = AckGatedExecutor(
            name=DATA_SCOPE,
            scheduler=self.scheduler,
            run_attempt=AssetListAttemptRunner(self.reader),
            settings=ExecutorSettings(
                buffer_size=8,
                workers=1,
                stall_timeout_s=300.0,
                stall_check_interval_s=3600.0,
                redelivery_base_s=5.0,
                redelivery_max_s=5.0,
                retry=RetryPolicy(max_attempts=3, base_delay_s=60.0, max_delay_s=60.0, factor=2.0),
            ),
            on_finished=lambda request: self.handle.finished(request),
        )
        self.handle = BrokerHandle(
            source=SOURCE,
            scope=DATA_SCOPE,
            scheduler=self.scheduler,
            executor=executor,
            reader=self.reader,
            feed=Feed.IEX,
        )
        self.handle.register_intake(RequestKind.ASSET_LIST, _asset_list_intake)
        self.submit = IngestSubmitHandler({SOURCE: self.handle}, instance_id)
        self.__store = store
        self.__instance_id = instance_id

    async def __report_expired(self, expired) -> None:
        await self.handle.report_expired(expired)

    @asynccontextmanager
    async def running(self) -> AsyncIterator['Ingest']:
        self.handle.start(self.__store, self.__instance_id)
        try:
            yield self
        finally:
            await asyncio.wait_for(self.handle.stop(), GUARD_S)


async def _round_trip(store, created, run_identity, world, status, name: str, *, attempts=0, recovered=False):
    """Write the request as the store does, mark it held by this ingest, submit it, wait for the store's answer."""
    holder = f'system-test-{run_identity.run_id}-{name}-{uuid.uuid4().hex[:6]}'
    created.instances.append(holder)
    await store.register_instance(RegisterInstanceRequest(instance_id=holder, started_at=datetime.now(UTC)))
    async with AsyncSession(store.engine, expire_on_commit=False) as db:
        written = await request_crud.create_request(
            db,
            new_asset_list_request(
                SOURCE,
                AssetType.STOCK,
                status,
                trigger=IngestRunTrigger.SCHEDULE,
                client_label=OWNER,
                ready_by=datetime.now(UTC) + timedelta(minutes=5),
            ),
        )
    created.requests.append(written.id)
    async with AsyncSession(store.engine, expire_on_commit=False) as db:
        if attempts:
            await db.execute(sa.update(REQUESTS).where(REQUESTS.c.id == written.id).values(attempts=attempts))
            await db.commit()
        assert await request_crud.mark_queued(db, written.id, holder) is not None
        record = await request_crud.get_request(db, written.id)
    ingest = Ingest(store, world, holder)
    async with ingest.running():
        response = await ingest.submit.submit(submit_request_of(record, recovered=recovered))
        await asyncio.wait_for(store.signal.wait(), GUARD_S)
    assert store.refusals == [], f'the store refused the delivered report: {store.refusals}'
    return written.id, ingest, response


# --------------------------------------------------------------------------------------------- the cases


@pytest.mark.parametrize('status', list(AssetListStatus), ids=lambda status: status.value)
async def test_1_an_asset_list_submit_the_store_wrote_is_filed_under_the_list_asked_for(
    store, created, run_identity, status
):
    request_id, ingest, response = await _round_trip(store, created, run_identity, BOTH_LISTS, status, 'al-submit')

    assert response.ack.refused is None
    assert (response.ack.accepted.feed, response.ack.accepted.quota_key) == (None, TRADING_SCOPE)
    assert ingest.reader.calls == (('assets', status.value),), 'not one read of the list the store asked for'
    assert store.answers == [DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=1)]
    (run,) = await _all(store.engine, sa.select(RUNS).where(RUNS.c.request_id == request_id))
    assert run.state is IngestRunState.SUCCEEDED
    assert (run.served_start, run.served_end, run.vendor_asset_id, run.feed) == (None,) * 4
    assert (run.quota_key, run.attempt_no) == (TRADING_SCOPE, 1)
    (snapshot,) = await _snapshots(store.engine, request_id)
    expected = [asset.symbol for asset in (_LISTED if status is ACTIVE else _DELISTED)]
    assert (snapshot.source, snapshot.asset_list, snapshot.row_count) == (SOURCE, status, len(expected))
    assert sorted(await _symbols(store.engine, snapshot.id)) == sorted(expected)
    request = await _request(store.engine, request_id)
    assert (request.state, request.asset_list_status) == (IngestRequestState.SUCCEEDED, status)


async def test_2_an_empty_inactive_list_is_an_inactive_zero_row_snapshot_and_an_empty_request(
    store, created, run_identity
):
    request_id, ingest, _ = await _round_trip(store, created, run_identity, NO_INACTIVE, INACTIVE, 'al-empty')

    [report] = store.delivered
    assert report.trailer.outcome is DeliveryOutcome.SUCCEEDED and report.parts == []
    assert ingest.reader.calls == (('assets', INACTIVE.value),)
    assert store.answers == [DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=1)]
    (snapshot,) = await _snapshots(store.engine, request_id)
    assert (snapshot.asset_list, snapshot.row_count) == (INACTIVE, 0)
    assert await _symbols(store.engine, snapshot.id) == []
    (run,) = await _all(store.engine, sa.select(RUNS).where(RUNS.c.request_id == request_id))
    assert run.state is IngestRunState.EMPTY
    assert (await _request(store.engine, request_id)).state is IngestRequestState.EMPTY


async def test_3_a_replay_writes_nothing_twice_and_the_report_fails_rule_i_only_as_another_kind(
    store, created, run_identity
):
    request_id, _, _ = await _round_trip(store, created, run_identity, BOTH_LISTS, ACTIVE, 'al-replay')
    [report] = store.delivered
    assert report.trailer.served_range is None and report.trailer.vendor_stats is not None
    assert validate_report(report, IngestRequestKind.ASSET_LIST) is None
    with pytest.raises(InvalidRequestError, match='served range'):
        validate_report(report, IngestRequestKind.CORPORATE_ACTIONS)
    before = await _written(store.engine, request_id)

    again = await store.deliver(report)

    assert again == DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=1)
    assert await _written(store.engine, request_id) == before, 'a replayed delivery wrote something'


async def test_4_a_recovered_asset_list_submit_is_accepted_and_written_as_the_next_attempt(
    store, created, run_identity
):
    request_id, ingest, response = await _round_trip(
        store, created, run_identity, BOTH_LISTS, INACTIVE, 'al-recovered', attempts=1, recovered=True
    )

    assert response.ack.accepted.quota_key == TRADING_SCOPE
    assert ingest.reader.calls == (('assets', INACTIVE.value),)
    assert store.answers == [DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=2)]
    (snapshot,) = await _snapshots(store.engine, request_id)
    assert (snapshot.asset_list, snapshot.attempt_no) == (INACTIVE, 2)
