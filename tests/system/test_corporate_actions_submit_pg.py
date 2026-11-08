"""A corporate-actions Submit round trip into the REAL delivery intake on the stack's Postgres (P13s, tj-7zjov1.124).

THE DESIGN: the architect's ruling of 22:05 UTC on tj-7zjov1.124 (option A: a SUCCEEDED corporate-actions report, an
empty success included, names the requested window EXACTLY as given as its served range, end exclusive, never the
whole UTC days the read sends; the intake stores it on the attempt row as served_start and served_end with no store
change) and ADR tj-26jt9w addendum (9) b, with the store's delivery rule (i) (data/store/app/ingest/delivery_intake.py,
validate_report: a SUCCEEDED bars or corporate-actions report must name a served range).

WHAT IS REAL. Ingest's half is production code end to end except the reader: the production IngestSubmitHandler routes
the Submit to a production BrokerHandle, whose production Scheduler (built through SchedulerBuilder) queues it and whose
production AckGatedExecutor runs it through the production CorporateActionsAttemptRunner, reading a paced FakeRead
(tests/fakes/market_data.py) and delivering the report to DeliveryIntake in process, writing on this stack's Postgres.
The store's half is the real DeliveryIntake on the real migrated tables.

WHY NOT tests.fakes.ingest.FakeIngest: it imports the Alpaca handle module, which imports alpaca-py, and the test_client
image installs the base, data-store and testing groups only (pyproject.toml; data-ingest holds alpaca-py). So the
handle is assembled here as FakeIngest assembles it, minus the Alpaca module: the corporate-actions intake is restated
(data scope, BACKFILL, no tape, as AlpacaHandle's and FakeIngest's), and the cost function is the production estimate
for this kind, one call (alpaca/handle.py estimate_request_calls; pinned in test_alpaca_corporate_actions_attempt.py).

THE CASES:
  (1) a corporate_actions Submit with a mid-day window and one record is accepted, read once, delivered and answered
      FINAL; its attempt row is SUCCEEDED with served_start and served_end equal to the requested window to the
      second (not the floored start day, not the ceiling end day), the observation is stored, the request SUCCEEDED;
  (2) the same with no record: an EMPTY attempt row that still names the requested window, the request EMPTY;
  (3) THE CONTROL, which makes (1) and (2) a guard of the seam: the very report the runner delivered, with its served
      range removed, is refused INVALID_REQUEST by rule (i) with nothing written, and the unaltered report is then
      written. A runner that sends no served range therefore reds (1) and (2) with this refusal, never a pass.

Vendor action ids carry this run's id: corporate_action's unique index is global and the table append-only. Each test
removes only the rows it wrote. A first sighting of an instance is GLOBAL by design; safe on the disposable stack this
suite requires (as test_delivery_intake_pg.py). Run by make test-system and the agent-stack MCP's run_system_tests.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_stock import DataSource, Feed
from common.errors.vocabulary import InvalidRequestError, Reason, TraderJoeError
from data.ingest.app.brokers.alpaca.corporate_actions_attempt import CorporateActionsAttemptRunner, CorporateActionsWork
from data.ingest.app.brokers.handle import BrokerHandle, Intake
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
from data.store.app.database.models.corporate_action import CorporateAction
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestKind, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState
from data.store.app.ingest import delivery_intake
from data.store.app.ingest.delivery_intake import DeliveryIntake
from routers.data_ingest.submit_handler import IngestSubmitHandler
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    CorporateActionRecords,
    DeliverAttemptResponse,
    DeliveryOutcome,
    DeliveryVerdict,
    RegisterInstanceRequest,
)
from schemas.data_ingest.submit import CorporateActionsRequest, RequestKind, SubmitRequest
from tests.fakes.market_data import WORLD_DAYS, WORLD_LINEAGES, FakeRead, FakeWorld, world_action


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

RUNS = IngestRun.__table__
REQUESTS = IngestRequest.__table__
INSTANCES = IngestInstance.__table__
ACTIONS = CorporateAction.__table__
# Alpaca's data scope, by its production name (alpaca/handle.py ALPACA_DATA_SCOPE), restated: see the docstring.
DATA_SCOPE = f'{DataSource.ALPACA_API.value}_DATA'
TRADING_SCOPE = f'{DataSource.ALPACA_API.value}_TRADING'
# Every wait for the code under test is bounded by this, so a regression fails rather than hangs.
GUARD_S = 20.0
# Mid-day at both ends: the read sends the vendor 2022-02-01 to 2022-02-16 (P13f, the end day as a ceiling); the
# served range must be these instants. The record's process date, 2022-02-14, lies inside.
WITH_RECORD = (datetime(2022, 2, 1, 9, 30, tzinfo=UTC), datetime(2022, 2, 15, 16, 0, tzinfo=UTC))
NO_RECORD = (datetime(2023, 5, 1, 9, 30, tzinfo=UTC), datetime(2023, 5, 3, 16, 0, tzinfo=UTC))
FAKE_CREDENTIAL = Credential(key_id='system-test-key-id', secret='not-a-vendor-secret')


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
            conn.execute(sa.delete(ACTIONS).where(ACTIONS.c.request_id.in_(ids.requests)))
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


async def _request_record(
    store: RecordingIntake, created: Created, run_identity, *, symbol: str, window, holder: str
) -> uuid.UUID:
    """The corporate-actions request row the store's submit path writes, QUEUED under `holder`."""
    request_id = uuid.uuid4()
    created.requests.append(request_id)
    async with AsyncSession(store.engine, expire_on_commit=False) as db:
        await request_crud.create_request(
            db,
            request_crud.NewIngestRequest(
                id=request_id,
                kind=IngestRequestKind.CORPORATE_ACTIONS,
                source=DataSource.ALPACA_API,
                client_label=run_identity.owner,
                asset_symbol=symbol,
                requested_start=window[0],
                requested_end=window[1],
            ),
        )
        await db.execute(
            sa.update(REQUESTS)
            .where(REQUESTS.c.id == request_id)
            .values(state=IngestRequestState.QUEUED, instance_id=holder)
        )
        await db.commit()
    return request_id


async def _rows(engine: AsyncEngine, table: sa.Table, request_id: uuid.UUID) -> list[sa.Row]:
    async with engine.connect() as conn:
        return list((await conn.execute(sa.select(table).where(table.c.request_id == request_id))).all())


async def _request_state(engine: AsyncEngine, request_id: uuid.UUID) -> IngestRequestState:
    async with engine.connect() as conn:
        return (await conn.execute(sa.select(REQUESTS.c.state).where(REQUESTS.c.id == request_id))).scalar_one()


# --------------------------------------------------------------------------------------------- ingest's half


def _corporate_actions_intake(request: SubmitRequest) -> Intake:
    """AlpacaHandle's corporate-actions intake, as tests/fakes/ingest.py restates it: data scope, BACKFILL, no tape."""
    actions = request.corporate_actions
    if actions is None:
        raise InvalidRequestError(Reason.INVALID_REQUEST, f'{request.kind.value} requests are not served yet')
    return Intake(scope=DATA_SCOPE, payload=CorporateActionsWork(request=actions, priority=RequestPriority.BACKFILL))


class Ingest:
    """One ingest process's corporate-actions path, assembled from production parts around a paced FakeRead."""

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
            run_attempt=CorporateActionsAttemptRunner(self.reader),
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
            source=DataSource.ALPACA_API,
            scope=DATA_SCOPE,
            scheduler=self.scheduler,
            executor=executor,
            reader=self.reader,
            feed=Feed.IEX,
        )
        self.handle.register_intake(RequestKind.CORPORATE_ACTIONS, _corporate_actions_intake)
        self.submit = IngestSubmitHandler({DataSource.ALPACA_API: self.handle}, instance_id)
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


def _world(vendor_action_id: str, symbol: str) -> FakeWorld:
    """The fake world with one stock merger, under this run's own id and symbol, processed on 2022-02-14."""
    raw = (
        f'{{"acquiree_cusip":"983919101","acquiree_rate":1,"acquiree_symbol":"{symbol}","acquirer_cusip":"007903107",'
        f'"acquirer_rate":1.7234,"acquirer_symbol":"AMD","effective_date":"2022-02-14","id":"{vendor_action_id}",'
        '"process_date":"2022-02-14"}'
    )
    return FakeWorld(WORLD_DAYS, WORLD_LINEAGES, (world_action('stock_mergers', raw),))


async def _round_trip(store, created, run_identity, own_symbol, window, name: str):
    holder = f'system-test-{run_identity.run_id}-{name}-{uuid.uuid4().hex[:6]}'
    created.instances.append(holder)
    await store.register_instance(RegisterInstanceRequest(instance_id=holder, started_at=datetime.now(UTC)))
    request_id = await _request_record(store, created, run_identity, symbol=own_symbol, window=window, holder=holder)
    vendor_action_id = f'ca-{run_identity.run_id}-{uuid.uuid4().hex[:8]}'
    ingest = Ingest(store, _world(vendor_action_id, own_symbol), holder)
    submit = SubmitRequest(
        request_id=str(request_id),
        owner=run_identity.owner,
        ready_by=datetime.now(UTC) + timedelta(minutes=5),
        corporate_actions=CorporateActionsRequest(
            source=DataSource.ALPACA_API, asset_symbol=own_symbol, start=window[0], end=window[1]
        ),
    )
    async with ingest.running():
        response = await ingest.submit.submit(submit)
        await asyncio.wait_for(store.signal.wait(), GUARD_S)
    assert store.refusals == [], f'the store refused the delivered report: {store.refusals}'
    return request_id, vendor_action_id, ingest, response


# --------------------------------------------------------------------------------------------- the cases


async def test_1_a_corporate_actions_submit_lands_its_requested_window_on_the_attempt_row(
    store, created, run_identity, own_symbol
):
    request_id, vendor_action_id, ingest, response = await _round_trip(
        store, created, run_identity, own_symbol, WITH_RECORD, 'ca-submit'
    )

    assert response.ack.refused is None and response.ack.accepted.feed is None
    assert [call[0] for call in ingest.reader.calls] == ['corporate_actions']
    assert store.answers == [DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=1)]
    (run,) = await _rows(store.engine, RUNS, request_id)
    assert run.state is IngestRunState.SUCCEEDED
    assert (run.served_start, run.served_end) == WITH_RECORD
    assert run.served_end == datetime(2022, 2, 15, 16, 0, tzinfo=UTC), 'the served end is the ceiling day, not 16:00'
    (observation,) = await _rows(store.engine, ACTIONS, request_id)
    assert observation.vendor_action_id == vendor_action_id
    assert await _request_state(store.engine, request_id) is IngestRequestState.SUCCEEDED


async def test_2_an_empty_corporate_actions_success_still_lands_its_requested_window(
    store, created, run_identity, own_symbol
):
    request_id, _, ingest, response = await _round_trip(store, created, run_identity, own_symbol, NO_RECORD, 'ca-empty')

    assert [call[0] for call in ingest.reader.calls] == ['corporate_actions']

    assert response.ack.refused is None
    [report] = store.delivered
    assert report.trailer.outcome is DeliveryOutcome.SUCCEEDED and report.parts == []
    assert store.answers == [DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=1)]
    (run,) = await _rows(store.engine, RUNS, request_id)
    assert run.state is IngestRunState.EMPTY
    assert (run.served_start, run.served_end) == NO_RECORD
    assert await _rows(store.engine, ACTIONS, request_id) == []
    assert await _request_state(store.engine, request_id) is IngestRequestState.EMPTY


async def test_3_the_control_the_delivered_report_without_its_served_range_is_refused_by_rule_i(
    store, created, run_identity, own_symbol, monkeypatch
):
    """Capture the runner's report before the intake sees it, strip its served range, deliver that first."""
    holder = f'system-test-{run_identity.run_id}-ca-control-{uuid.uuid4().hex[:6]}'
    created.instances.append(holder)
    await store.register_instance(RegisterInstanceRequest(instance_id=holder, started_at=datetime.now(UTC)))
    request_id = await _request_record(
        store, created, run_identity, symbol=own_symbol, window=WITH_RECORD, holder=holder
    )
    captured: list[AttemptDelivery] = []
    arrived = asyncio.Event()

    class Capture:
        async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
            captured.append(attempt)
            arrived.set()
            return DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=attempt.header.attempt_no)

    ingest = Ingest(Capture(), _world(f'ca-{run_identity.run_id}-{uuid.uuid4().hex[:8]}', own_symbol), holder)
    async with ingest.running():
        await ingest.submit.submit(
            SubmitRequest(
                request_id=str(request_id),
                owner=run_identity.owner,
                ready_by=datetime.now(UTC) + timedelta(minutes=5),
                corporate_actions=CorporateActionsRequest(
                    source=DataSource.ALPACA_API, asset_symbol=own_symbol, start=WITH_RECORD[0], end=WITH_RECORD[1]
                ),
            )
        )
        await asyncio.wait_for(arrived.wait(), GUARD_S)
    [report] = captured
    assert any(isinstance(part, CorporateActionRecords) for part in report.parts)
    stripped = report.model_copy(update={'trailer': report.trailer.model_copy(update={'served_range': None})})

    with pytest.raises(InvalidRequestError) as raised:
        await store.deliver(stripped)

    assert raised.value.reason is Reason.INVALID_REQUEST
    assert await _rows(store.engine, RUNS, request_id) == [], 'a refused report wrote an attempt row'
    assert await _rows(store.engine, ACTIONS, request_id) == [], 'a refused report wrote an observation'
    assert await _request_state(store.engine, request_id) is IngestRequestState.QUEUED

    answer = await store.deliver(report)

    assert answer == DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=1)
    (run,) = await _rows(store.engine, RUNS, request_id)
    assert (run.served_start, run.served_end) == WITH_RECORD
