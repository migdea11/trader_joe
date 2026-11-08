"""The restart intake and the instance id (P36).

Bead tj-7zjov1.21 against builder-ingest's 85ad218. The design: tj-26jt9w addendum 5 (RESTART: an instance id minted
at start, announced at start and seen on every answer and report; every request the old instance held re-requested,
flagged recovered, with its durable attempt count, and charged its registered conservative estimate to the current
window BEFORE it is scheduled), addendum 4 (reports go through the acknowledgement-gated buffer), the bead's
acceptance criteria and the architect's P11 gate note on the bead (items 1 to 5: one mint, idempotency confirmed
against the recovered flag, the at-or-past-budget gap bounded at intake, charge before the queue, instance= on the
ingest_service call).

HOW IT IS DRIVEN.

* The identity and the announcement loop are driven directly, with an injected clock and sleep.
* The lifespan is the production one (main.create_app -> app_depends.make_lifespan) on a loopback port, with a
  real AlpacaHandle. Only the store is a double: the channel factory, the target and the store client are patched
  in app_depends, and mint_instance is wrapped only to count its calls. The probe and Submit are asked over real
  gRPC through the production GrpcIngestSubmitClient.
* The intake is a production BrokerHandle over a real Scheduler (production SchedulerBuilder) and a real
  AckGatedExecutor. The scheduler the handle sees is a pass-through spy that logs charge_restart and submit in
  order, so "charged before it is queued" is observed, not inferred. The runner stands for the vendor and records
  every call; the store double holds each delivery until the test answers it.
"""

import asyncio
import logging
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import ExogenousError, Reason
from common.rpc.channel import create_channel
from common.rpc.clients.ingest_submit import GrpcIngestSubmitClient
from common.rpc.mapping.values import ProtoMappingError
from data.ingest.app import app_depends, main
from data.ingest.app import instance as instance_module
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.handle import AlpacaHandle
from data.ingest.app.brokers.handle import BrokerHandle
from data.ingest.app.instance import (
    InstanceIdentity,
    announce_until_acknowledged,
    log_announcement_death,
    mint_instance,
)
from data.ingest.app.scheduler import (
    AckGatedExecutor,
    AttemptResult,
    Credential,
    ExecutorSettings,
    Limit,
    RetryPolicy,
    ScheduledRequest,
    SchedulerBuilder,
    StaticCredentialProvider,
)
from data.ingest.tests.feed_rule import assert_a_bars_report_names_the_tape, assert_a_no_tape_report_is_unspecified
from data.ingest.tests.grpc_bind import LOOPBACK, LoopbackGrpc
from routers.data_ingest.instance_handler import IngestInstanceHandler
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    DeliverAttemptResponse,
    DeliveryOutcome,
    DeliveryTrailer,
    DeliveryVerdict,
    RegisterInstanceRequest,
)
from schemas.data_ingest.submit import BarsRequest, SubmitRequest


pytestmark = pytest.mark.data_ingest

SCOPE = f'{DataSource.ALPACA_API.value}_INTAKE'
INSTANCE = 'instance-under-test'
T0 = datetime(2026, 10, 24, 12, tzinfo=UTC)
START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
END = START + timedelta(hours=1)
MAX_ATTEMPTS = 3
# The estimate the intake scheduler's cost function registers, so a charge is told apart from a default of 1.
ESTIMATE = 7
# Every wait for the code under test is bounded by this, so a regression fails rather than hangs.
GUARD_S = 5.0
# The stall alarm's check interval. FakeSleep parks a sleep of exactly this length, so the alarm never runs.
WATCH_S = 7777.0


# ---------------------------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------------------------


class FakeSleep:
    """Records every wait and returns at once; parks the stall alarm's interval until it is cancelled."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        if seconds == WATCH_S:
            await asyncio.Event().wait()
        self.slept.append(seconds)
        await asyncio.sleep(0)


@dataclass
class Delivered:
    """One delivery the store has received and not yet answered."""

    attempt: AttemptDelivery
    answer: asyncio.Future


class HeldStore:
    """A StoreDeliveryClient that holds every delivery until the test answers it."""

    def __init__(self) -> None:
        self.received: list[AttemptDelivery] = []
        self.__waiting: asyncio.Queue[Delivered] = asyncio.Queue()

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        self.received.append(attempt)
        answer = asyncio.get_running_loop().create_future()
        await self.__waiting.put(Delivered(attempt, answer))
        return await answer

    async def register_instance(self, request):
        raise AssertionError('a broker handle never registers the instance')

    async def next(self) -> Delivered:
        return await asyncio.wait_for(self.__waiting.get(), GUARD_S)


def answer(delivered: Delivered, verdict: DeliveryVerdict) -> None:
    count = delivered.attempt.header.attempt_no
    delivered.answer.set_result(DeliverAttemptResponse(verdict=verdict, attempt_count=count))


class FakeRunner:
    """Stands for the vendor: records every attempt it is asked to make and succeeds."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, request: ScheduledRequest, attempt_no: int) -> AttemptResult:
        self.calls.append((request.request_id, attempt_no))
        return AttemptResult(trailer=DeliveryTrailer(outcome=DeliveryOutcome.SUCCEEDED), quota_key=request.quota_scope)


class SpyScheduler:
    """Passes every call through to the real Scheduler, logging charge_restart and submit in the order they come."""

    def __init__(self, real, order: list[str]) -> None:
        self.real = real
        self.log: list[tuple[str, str, int]] = []
        self.charged: list[int] = []
        self.__order = order

    def charge_restart(self, request: ScheduledRequest) -> int:
        self.log.append(('charge_restart', request.request_id, request.attempt_count))
        self.__order.append('charge_restart')
        cost = self.real.charge_restart(request)
        self.charged.append(cost)
        return cost

    async def submit(self, request: ScheduledRequest) -> None:
        self.log.append(('submit', request.request_id, request.attempt_count))
        self.__order.append('submit')
        await self.real.submit(request)

    def __getattr__(self, name):
        return getattr(self.real, name)


async def _no_expired_sink(_expired) -> None:
    raise AssertionError('no request should expire in this test')


class Intake:
    """A production BrokerHandle over a real Scheduler and AckGatedExecutor, with the doubles above.

    Enter it to start the handle (the executor needs its instance id before it can report anything); leave it
    unentered to look at the queue with no worker taking from it.
    """

    def __init__(self, *, buffer_size: int = 8, tape: Feed = Feed.IEX) -> None:
        self.runner = FakeRunner()
        self.store = HeldStore()
        self.sleep = FakeSleep()
        self.finished: list[str] = []
        minute = timedelta(minutes=1)
        self.real = (
            SchedulerBuilder(clock=lambda: T0)
            .declare_scope(SCOPE, [Limit(minute, 6000)])
            .set_limit(minute, 6000, scope=SCOPE)
            .cost_function(lambda _request, _page_size: ESTIMATE, page_size=1)
            .credential_provider(StaticCredentialProvider(Credential(key_id='key', secret='secret')))
            .on_expired(_no_expired_sink)
            .build()
        )
        # Every charge, queueing and exhausted report the handle makes, in the order it makes them.
        self.order: list[str] = []
        self.scheduler = SpyScheduler(self.real, self.order)
        self.executor = AckGatedExecutor(
            name='INTAKE',
            scheduler=self.real,
            run_attempt=self.runner,
            settings=ExecutorSettings(
                buffer_size=buffer_size,
                workers=1,
                stall_timeout_s=300.0,
                stall_check_interval_s=WATCH_S,
                redelivery_base_s=1.0,
                redelivery_max_s=1.5,
                retry=RetryPolicy(max_attempts=MAX_ATTEMPTS),
            ),
            on_finished=self.__finished,
            clock=lambda: T0,
            sleep=self.sleep,
        )
        # Pass-through: the handle holds this same executor, so its call to report_exhausted lands in the order log.
        report_exhausted = self.executor.report_exhausted

        def logged_report_exhausted(request: ScheduledRequest) -> None:
            self.order.append('report_exhausted')
            report_exhausted(request)

        self.executor.report_exhausted = logged_report_exhausted
        self.handle = BrokerHandle(
            source=DataSource.ALPACA_API,
            scope=SCOPE,
            scheduler=self.scheduler,
            executor=self.executor,
            reader=Mock(),
            resolve_feed=lambda: tape,
        )

    def __finished(self, request: ScheduledRequest) -> None:
        self.finished.append(request.request_id)
        self.handle.finished(request)

    async def __aenter__(self) -> 'Intake':
        self.handle.start(self.store, INSTANCE)
        return self

    async def __aexit__(self, *exc_info) -> None:
        await asyncio.wait_for(self.handle.stop(), GUARD_S)

    @property
    def charges(self) -> list[tuple[str, str, int]]:
        return [entry for entry in self.scheduler.log if entry[0] == 'charge_restart']

    @property
    def submits(self) -> list[tuple[str, str, int]]:
        return [entry for entry in self.scheduler.log if entry[0] == 'submit']


async def settle() -> None:
    """Let every task that can run, run."""
    for _ in range(200):
        await asyncio.sleep(0)


def bars_request(feed: Feed | None = None) -> BarsRequest:
    return BarsRequest(
        source=DataSource.ALPACA_API,
        asset_symbol='VFV',
        asset_type=AssetType.STOCK,
        data_types=[DataType.MARKET_ACTIVITY],
        granularity=Granularity.THIRTY_MINUTES,
        start=START,
        end=END,
        update_type=UpdateType.STATIC,
        feed=feed,
    )


def submit_request(request_id: str = 'req-1', **overrides) -> SubmitRequest:
    fields = {'request_id': request_id, 'owner': 'rebalancer', 'bars': bars_request()}
    return SubmitRequest(**{**fields, **overrides})


@pytest.fixture(autouse=True)
def iex_deployment_and_no_cached_client(monkeypatch):
    monkeypatch.delenv('ALPACA_SIP_ENABLED', raising=False)
    monkeypatch.delenv('ALPACA_RATE_LIMIT_PER_MINUTE', raising=False)
    broker_api.set_client(None)
    yield
    broker_api.set_client(None)


# ---------------------------------------------------------------------------------------------
# The identity
# ---------------------------------------------------------------------------------------------


def test_the_identity_is_a_fresh_id_and_a_timezone_aware_start_time():
    before = datetime.now(UTC)
    first = mint_instance()
    second = mint_instance()
    after = datetime.now(UTC)

    assert first.instance_id and second.instance_id
    assert first.instance_id != second.instance_id, 'two mints gave the same id'
    assert first.started_at.tzinfo is not None and first.started_at.utcoffset() is not None
    assert before <= first.started_at <= after


def test_the_identity_takes_its_start_time_from_the_clock_it_is_given():
    clock = datetime(2026, 10, 24, 9, tzinfo=timezone(timedelta(hours=-4)))

    assert mint_instance(lambda: clock).started_at == clock


@pytest.mark.asyncio
async def test_the_probe_handler_reports_the_identity_it_was_given():
    identity = InstanceIdentity(instance_id='abc', started_at=T0)

    response = await IngestInstanceHandler(identity).instance()

    assert response.instance_id == 'abc'
    assert response.started_at == T0


# ---------------------------------------------------------------------------------------------
# The announcement: retried with bounded backoff until acknowledged
# ---------------------------------------------------------------------------------------------


class ScriptedRegistry:
    """A store whose register_instance raises each scripted error in turn, then acknowledges."""

    def __init__(self, *errors: BaseException) -> None:
        self.calls: list[RegisterInstanceRequest] = []
        self.__errors = list(errors)

    async def register_instance(self, request: RegisterInstanceRequest):
        self.calls.append(request)
        if self.__errors:
            raise self.__errors.pop(0)
        return None


def unavailable() -> ExogenousError:
    return ExogenousError(Reason.PEER_UNAVAILABLE, 'the store is unreachable')


@pytest.mark.asyncio
async def test_the_announcement_carries_the_id_and_start_time_and_stops_once_acknowledged():
    identity = InstanceIdentity(instance_id='abc', started_at=T0)
    store = ScriptedRegistry()
    sleep = FakeSleep()

    await asyncio.wait_for(announce_until_acknowledged(store, identity, sleep=sleep), GUARD_S)

    assert store.calls == [RegisterInstanceRequest(instance_id='abc', started_at=T0)]
    assert sleep.slept == []


@pytest.mark.asyncio
async def test_the_announcement_retries_through_store_and_mapping_errors_with_bounded_backoff():
    """Doubling from 1 s, capped at 30 s; a TraderJoeError and a ProtoMappingError are both retried."""
    identity = InstanceIdentity(instance_id='abc', started_at=T0)
    errors = [unavailable(), ProtoMappingError('the answer will not map'), *[unavailable() for _ in range(6)]]
    store = ScriptedRegistry(*errors)
    sleep = FakeSleep()

    await asyncio.wait_for(announce_until_acknowledged(store, identity, sleep=sleep), GUARD_S)

    assert sleep.slept == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
    assert len(store.calls) == len(errors) + 1, 'it did not stop at the acknowledgement, or gave up before it'
    assert {(call.instance_id, call.started_at) for call in store.calls} == {('abc', T0)}


@pytest.mark.asyncio
async def test_the_announcement_backoff_honours_its_bounds():
    identity = InstanceIdentity(instance_id='abc', started_at=T0)
    store = ScriptedRegistry(*[unavailable() for _ in range(4)])
    sleep = FakeSleep()

    await asyncio.wait_for(
        announce_until_acknowledged(store, identity, base_delay_s=0.5, max_delay_s=1.5, sleep=sleep), GUARD_S
    )

    assert sleep.slept == [0.5, 1.0, 1.5, 1.5]


@pytest.mark.asyncio
async def test_a_bug_in_the_store_client_is_not_swallowed_by_the_announcement():
    identity = InstanceIdentity(instance_id='abc', started_at=T0)
    store = ScriptedRegistry(RuntimeError('a bug'))

    with pytest.raises(RuntimeError, match='a bug'):
        await asyncio.wait_for(announce_until_acknowledged(store, identity, sleep=FakeSleep()), GUARD_S)


# ---------------------------------------------------------------------------------------------
# The lifespan: one mint, the same id everywhere, an announcement that never holds start back
# ---------------------------------------------------------------------------------------------


class LifespanStore:
    """The store as the lifespan sees it: every delivery answered FINAL at once, registration per mode.

    mode 'ack' acknowledges at once; 'hang' never answers (a store that is down and holding the call); 'fail'
    raises PEER_UNAVAILABLE every time; 'bug' raises a RuntimeError, which the announcement does not retry.
    """

    def __init__(self, mode: str = 'ack') -> None:
        self.mode = mode
        self.registered: list[RegisterInstanceRequest] = []
        self.received: list[AttemptDelivery] = []
        self.cancelled = 0

    async def register_instance(self, request: RegisterInstanceRequest):
        self.registered.append(request)
        if self.mode == 'fail':
            raise unavailable()
        if self.mode == 'bug':
            raise RuntimeError('a bug in the store client')
        if self.mode == 'hang':
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        return None

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        self.received.append(attempt)
        return DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=attempt.header.attempt_no)


def lifespan_handle() -> AlpacaHandle:
    return AlpacaHandle(
        settings=ExecutorSettings(stall_check_interval_s=3600.0, retry=RetryPolicy(max_attempts=MAX_ATTEMPTS)),
        calls_per_minute=60,
    )


class RunningApp:
    """The production app with one AlpacaHandle, its store a double, on a loopback gRPC port."""

    def __init__(self, store: LifespanStore) -> None:
        self.store = store
        self.handle = lifespan_handle()
        self.mints: list[InstanceIdentity] = []
        self.app = main.create_app({DataSource.ALPACA_API: self.handle.reader}, {DataSource.ALPACA_API: self.handle})
        self.port: int | None = None

    def __mint(self, *args, **kwargs) -> InstanceIdentity:
        identity = mint_instance(*args, **kwargs)
        self.mints.append(identity)
        return identity

    @asynccontextmanager
    async def running(self):
        channel = Mock(close=AsyncMock())
        with ExitStack() as stubs:
            stubs.enter_context(patch.object(app_depends, 'initialize_latency_server', Mock()))
            stubs.enter_context(patch.object(app_depends, 'target_from_env', lambda _name: 'store-double:1'))
            stubs.enter_context(patch.object(app_depends, 'create_channel', lambda _target: channel))
            stubs.enter_context(patch.object(app_depends, 'GrpcStoreDeliveryClient', lambda _channel: self.store))
            stubs.enter_context(patch.object(app_depends, 'mint_instance', side_effect=self.__mint))
            async with LoopbackGrpc() as grpc_bind:
                self.port = grpc_bind.port
                # Entering is bounded: an announcement awaited before the yield would hold start back forever on a
                # store that never answers, and must fail here rather than hang the run.
                async with asyncio.timeout(GUARD_S):
                    async with self.app.router.lifespan_context(self.app):
                        yield self

    @asynccontextmanager
    async def client(self):
        channel = create_channel(f'{LOOPBACK}:{self.port}')
        try:
            yield GrpcIngestSubmitClient(channel)
        finally:
            await channel.close()


async def eventually(condition, what: str) -> None:
    for _ in range(int(GUARD_S / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f'timed out waiting for {what}')


def announcement_tasks() -> list[asyncio.Task]:
    return [task for task in asyncio.all_tasks() if task.get_name() == 'announce-instance' and not task.done()]


@pytest.mark.asyncio
async def test_one_id_minted_once_is_on_the_probe_the_submit_answer_the_report_header_and_the_announcement():
    store = LifespanStore('ack')
    running = RunningApp(store)

    async with running.running(), running.client() as client:
        probe = await asyncio.wait_for(client.instance(), GUARD_S)
        accepted = await asyncio.wait_for(client.submit(submit_request('fresh')), GUARD_S)
        # At the budget: reported without a vendor call, so the report header is observed with no network.
        exhausted = await asyncio.wait_for(
            client.submit(submit_request('spent', attempt_count=MAX_ATTEMPTS, recovered=True)), GUARD_S
        )
        await eventually(lambda: store.registered, 'the announcement')
        await eventually(
            lambda: any(item.header.request_id == 'spent' for item in store.received), 'the exhausted report'
        )

    assert len(running.mints) == 1, f'the lifespan minted {len(running.mints)} identities'
    identity = running.mints[0]
    assert probe.instance_id == identity.instance_id
    assert probe.started_at == identity.started_at
    assert probe.started_at.utcoffset() is not None, 'the probe answered a naive started_at'
    assert accepted.instance_id == identity.instance_id
    assert exhausted.instance_id == identity.instance_id
    report = next(item for item in store.received if item.header.request_id == 'spent')
    assert report.header.instance_id == identity.instance_id
    assert store.registered == [
        RegisterInstanceRequest(instance_id=identity.instance_id, started_at=identity.started_at)
    ], 'the announcement was not made exactly once with the minted identity, or did not stop once acknowledged'


@pytest.mark.asyncio
async def test_a_store_that_never_answers_the_announcement_does_not_hold_start_and_shutdown_cancels_it():
    store = LifespanStore('hang')
    running = RunningApp(store)

    async with running.running(), running.client() as client:
        # Serving while the announcement is still waiting on the store.
        probe = await asyncio.wait_for(client.instance(), GUARD_S)
        await eventually(lambda: store.registered, 'the announcement to start')
        assert announcement_tasks(), 'no announcement is pending while the store holds it'
        assert store.cancelled == 0

    assert probe.instance_id == running.mints[0].instance_id
    assert store.cancelled == 1, 'the pending announcement was not cancelled at shutdown'
    assert announcement_tasks() == [], 'the announcement outlived the lifespan'


@pytest.mark.asyncio
async def test_a_store_refusing_the_announcement_leaves_ingest_serving_and_shutdown_stops_the_retries():
    store = LifespanStore('fail')
    running = RunningApp(store)

    async with running.running(), running.client() as client:
        await eventually(lambda: store.registered, 'the first announcement')
        response = await asyncio.wait_for(client.submit(submit_request('fresh')), GUARD_S)
        assert response.ack.accepted is not None
        assert announcement_tasks(), 'the announcement gave up after a refusal instead of retrying'

    calls = len(store.registered)
    await asyncio.sleep(1.5)
    assert len(store.registered) == calls, 'the announcement kept retrying after the lifespan ended'
    assert announcement_tasks() == []


# ---------------------------------------------------------------------------------------------
# The intake: a recovered request is charged before it is queued, and keeps its count
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('attempt_count', [0, 2], ids=['never-started', 'attempted-before'])
async def test_a_recovered_request_is_charged_its_estimate_before_it_is_queued_and_keeps_its_count(attempt_count):
    """Addendum 5: charged to the current window BEFORE it is scheduled, never-started requests included."""
    intake = Intake()

    ack = await intake.handle.submit(submit_request('r1', attempt_count=attempt_count, recovered=True))

    assert ack.accepted is not None
    assert ack.accepted.quota_key == SCOPE
    assert intake.scheduler.log == [('charge_restart', 'r1', attempt_count), ('submit', 'r1', attempt_count)], (
        'not charged exactly once, before the queue, with the count it arrived with'
    )
    assert intake.scheduler.charged == [ESTIMATE], 'the charge is not the registered estimate'
    queued = await asyncio.wait_for(intake.real.take(), GUARD_S)
    assert queued.request_id == 'r1'
    assert queued.attempt_count == attempt_count


@pytest.mark.asyncio
async def test_a_recovered_request_continues_its_numbering_from_the_count_it_arrived_with():
    async with Intake() as intake:
        await intake.handle.submit(submit_request('r1', attempt_count=MAX_ATTEMPTS - 1, recovered=True))
        delivered = await intake.store.next()
        answer(delivered, DeliveryVerdict.FINAL)
        await settle()

    assert intake.runner.calls == [('r1', MAX_ATTEMPTS)], 'one below the budget still gets its one last attempt'
    assert delivered.attempt.header.attempt_no == MAX_ATTEMPTS
    assert len(intake.charges) == 1


@pytest.mark.asyncio
async def test_a_request_not_flagged_recovered_is_never_charged():
    intake = Intake()

    await intake.handle.submit(submit_request('r1', attempt_count=2))

    assert intake.charges == []
    assert intake.submits == [('submit', 'r1', 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize('first_recovered', [True, False], ids=['held-as-recovered', 'held-as-new'])
async def test_a_recovered_resubmit_of_an_id_already_held_is_a_duplicate_neither_charged_nor_queued_again(
    first_recovered,
):
    intake = Intake()

    first = await intake.handle.submit(submit_request('r1', attempt_count=1, recovered=first_recovered))
    again = await intake.handle.submit(submit_request('r1', attempt_count=1, recovered=True))

    assert again == first
    assert len(intake.charges) == (1 if first_recovered else 0), 'the duplicate was charged'
    assert len(intake.submits) == 1, 'the duplicate was queued'
    assert intake.real.queued == 1
    assert intake.handle.active == 1


# ---------------------------------------------------------------------------------------------
# The intake: a request whose budget is already spent gets no vendor attempt
# ---------------------------------------------------------------------------------------------


EXHAUSTED = pytest.mark.parametrize(
    'attempt_count', [MAX_ATTEMPTS, MAX_ATTEMPTS + 2], ids=['exactly-at-max', 'past-max']
)


@pytest.mark.asyncio
@EXHAUSTED
@pytest.mark.parametrize('recovered', [True, False], ids=['recovered', 'not-recovered'])
async def test_a_request_arriving_at_or_past_the_budget_is_reported_exhausted_with_no_vendor_call(
    attempt_count, recovered
):
    """Decision tj-wisp-g9rmw0 ruling 1: shaped exactly as EXPIRED; ruling 4: the check ignores the recovered flag."""
    async with Intake() as intake:
        ack = await intake.handle.submit(submit_request('r1', attempt_count=attempt_count, recovered=recovered))
        delivered = await intake.store.next()
        answer(delivered, DeliveryVerdict.FINAL)
        await settle()

    assert ack.accepted is not None, 'the exhausted request was not accepted in the ack'
    assert ack.accepted.quota_key == SCOPE
    assert intake.runner.calls == [], 'a vendor call was made for a request whose budget was spent'
    assert intake.submits == [], 'the exhausted request was queued'
    assert intake.real.queued == 0
    assert len(intake.store.received) == 1, 'not exactly one report'
    report = delivered.attempt
    assert report.header.request_id == 'r1'
    assert report.header.instance_id == INSTANCE
    assert report.header.attempt_no == attempt_count + 1, 'the idempotency key is not the count plus one'
    assert report.header.quota_key == '', 'an exhausted report named a quota although nothing was spent'
    assert report.header.vendor_asset_id == ''
    assert report.trailer.outcome is DeliveryOutcome.EXHAUSTED
    assert report.trailer.error_class is None, 'an exhausted report carried an error class'
    assert report.trailer.last_attempt is False, 'last_attempt is meaningful only on FAILED'
    assert report.trailer.vendor_stats is None, 'vendor stats reported for a call that was never made'
    assert report.trailer.as_of is None
    assert report.trailer.served_range is None
    assert 'no vendor call was made' in report.trailer.detail
    assert not report.parts
    # tj-7zjov1.66: the feed follows the kind rule unchanged, so the bars report names the tape of the ack.
    assert report.header.feed is ack.accepted.feed is Feed.IEX, 'the exhausted report and the ack name different tapes'
    assert_a_bars_report_names_the_tape(report.header, Feed.IEX)


@pytest.mark.asyncio
@pytest.mark.parametrize('tape', [Feed.IEX, Feed.SIP], ids=['iex', 'sip'])
@pytest.mark.parametrize('named', [False, True], ids=['default-feed', 'named-feed'])
@pytest.mark.parametrize('recovered', [True, False], ids=['recovered', 'not-recovered'])
async def test_an_exhausted_bars_report_names_the_tape_of_the_submit_ack(tape, named, recovered):
    """store.proto DeliveryHeader.feed and DELIVERY_OUTCOME_EXHAUSTED; decision tj-wisp-g9rmw0 ruling 1.

    The tape BrokerHandle.submit resolved at intake is the one the ack names and the one the EXHAUSTED report
    carries, whether the request named it or left it to the deployment's default. Nothing else in the report
    moves off its no-call shape.
    """
    async with Intake(tape=tape) as intake:
        bars = bars_request(feed=tape if named else None)
        ack = await intake.handle.submit(
            submit_request('r1', bars=bars, attempt_count=MAX_ATTEMPTS, recovered=recovered)
        )
        delivered = await intake.store.next()
        answer(delivered, DeliveryVerdict.FINAL)
        await settle()

    report = delivered.attempt
    assert ack.accepted.feed is tape
    assert report.trailer.outcome is DeliveryOutcome.EXHAUSTED
    assert report.header.feed is ack.accepted.feed, 'the exhausted report and the ack name different tapes'
    assert_a_bars_report_names_the_tape(report.header, tape)
    assert report.header.quota_key == ''
    assert report.header.vendor_asset_id == ''
    assert report.trailer.vendor_stats is None
    assert report.trailer.error_class is None
    assert report.trailer.last_attempt is False
    assert intake.runner.calls == []


@pytest.mark.asyncio
async def test_an_exhausted_report_for_a_kind_with_no_tape_names_no_feed():
    """store.proto DeliveryHeader.feed: an ASSET_LIST or CORPORATE_ACTIONS report is always FEED_UNSPECIFIED.

    BrokerHandle serves bars only in PR 5, so the executor's report_exhausted is fed directly the request a
    no-tape kind's handle would build: feed None.
    """
    async with Intake() as intake:
        intake.executor.report_exhausted(
            ScheduledRequest(request_id='r1', quota_scope=SCOPE, payload=None, attempt_count=MAX_ATTEMPTS)
        )
        delivered = await intake.store.next()
        answer(delivered, DeliveryVerdict.FINAL)
        await settle()

    report = delivered.attempt
    assert report.trailer.outcome is DeliveryOutcome.EXHAUSTED
    assert report.header.feed is None
    assert_a_no_tape_report_is_unspecified(report.header)
    assert report.header.attempt_no == MAX_ATTEMPTS + 1
    assert report.header.quota_key == ''
    assert report.trailer.vendor_stats is None
    assert report.trailer.error_class is None
    assert report.trailer.last_attempt is False


@pytest.mark.asyncio
@EXHAUSTED
async def test_a_recovered_exhausted_request_is_charged_its_estimate_once_before_it_is_reported_and_never_queued(
    attempt_count,
):
    """Ruling 2: the old instance may have had attempt count + 1 in flight, so the estimate stands in for its spend."""
    async with Intake() as intake:
        await intake.handle.submit(submit_request('r1', attempt_count=attempt_count, recovered=True))
        assert intake.order == ['charge_restart', 'report_exhausted'], 'not charged once, before the report'
        delivered = await intake.store.next()
        answer(delivered, DeliveryVerdict.FINAL)
        await settle()

    assert intake.scheduler.log == [('charge_restart', 'r1', attempt_count)], 'not exactly one charge and no queue'
    assert intake.scheduler.charged == [ESTIMATE], 'the charge is not the registered estimate'
    assert intake.real.queued == 0
    assert intake.order == ['charge_restart', 'report_exhausted']


@pytest.mark.asyncio
@EXHAUSTED
async def test_an_exhausted_request_not_flagged_recovered_is_reported_and_never_charged(attempt_count):
    async with Intake() as intake:
        await intake.handle.submit(submit_request('r1', attempt_count=attempt_count))
        delivered = await intake.store.next()
        answer(delivered, DeliveryVerdict.FINAL)
        await settle()

    assert intake.scheduler.log == [], 'a request not flagged recovered was charged or queued'
    assert intake.order == ['report_exhausted']
    assert delivered.attempt.trailer.outcome is DeliveryOutcome.EXHAUSTED


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict', [DeliveryVerdict.FINAL, DeliveryVerdict.CONTINUE], ids=['final', 'continue'])
async def test_the_exhausted_request_is_forgotten_after_the_acknowledgement_whatever_the_store_answers(verdict):
    async with Intake() as intake:
        await intake.handle.submit(submit_request('r1', attempt_count=MAX_ATTEMPTS, recovered=True))
        delivered = await intake.store.next()
        assert intake.handle.active == 1, 'the id was forgotten before the store acknowledged its report'
        answer(delivered, verdict)
        await settle()

        assert intake.finished == ['r1']
        assert intake.handle.active == 0, 'the id is still held after the acknowledgement'
        assert intake.runner.calls == [], 'CONTINUE bought a vendor attempt past the budget'
        assert intake.sleep.slept == [], 'a retry was scheduled after the exhausted report'

        # Forgotten: the same id is a new request again.
        await intake.handle.submit(submit_request('r1'))
        assert intake.submits == [('submit', 'r1', 0)]


@pytest.mark.asyncio
@pytest.mark.parametrize('recovered', [True, False], ids=['recovered', 'not-recovered'])
async def test_an_exhausted_resubmit_while_its_report_is_pending_is_answered_again_neither_charged_nor_reported_twice(
    recovered,
):
    """The duplicate check comes first: a held id is answered again, and is neither charged nor reported again."""
    async with Intake() as intake:
        first = await intake.handle.submit(submit_request('r1', attempt_count=MAX_ATTEMPTS, recovered=recovered))
        delivered = await intake.store.next()
        again = await intake.handle.submit(submit_request('r1', attempt_count=MAX_ATTEMPTS, recovered=True))
        await settle()
        assert again == first
        assert len(intake.store.received) == 1, 'the duplicate was reported twice'
        answer(delivered, DeliveryVerdict.FINAL)
        await settle()

    assert intake.runner.calls == []
    assert len(intake.charges) == (1 if recovered else 0), 'the duplicate was charged'
    assert intake.order.count('report_exhausted') == 1, 'the duplicate was reported again'


@pytest.mark.asyncio
async def test_an_exhausted_report_waits_for_a_buffer_slot_without_holding_submit_and_nothing_is_dropped():
    async with Intake(buffer_size=1) as intake:
        await intake.handle.submit(submit_request('a', attempt_count=MAX_ATTEMPTS, recovered=True))
        first = await intake.store.next()

        # The one slot is held by a's unanswered report. Submit still answers at once.
        ack = await asyncio.wait_for(
            intake.handle.submit(submit_request('b', attempt_count=MAX_ATTEMPTS, recovered=True)), 0.5
        )
        assert ack.accepted is not None
        await settle()
        assert [item.header.request_id for item in intake.store.received] == ['a'], 'the buffer bound was ignored'
        assert intake.handle.active == 2

        answer(first, DeliveryVerdict.FINAL)
        second = await intake.store.next()
        assert second.attempt.header.request_id == 'b'
        assert second.attempt.trailer.outcome is DeliveryOutcome.EXHAUSTED
        assert second.attempt.trailer.last_attempt is False
        answer(second, DeliveryVerdict.FINAL)
        await settle()

    assert [item.attempt.trailer.outcome for item in (first, second)] == [DeliveryOutcome.EXHAUSTED] * 2
    assert intake.finished == ['a', 'b'], 'a report was dropped'
    assert intake.handle.active == 0
    assert intake.runner.calls == []
    assert len(intake.charges) == 2


# ---------------------------------------------------------------------------------------------
# The announcement's done-callback: a bug is logged when it happens, a cancellation is not
# ---------------------------------------------------------------------------------------------


def announcement_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == instance_module.log.name and record.levelno == logging.ERROR
    ]


@pytest.mark.asyncio
async def test_the_done_callback_logs_at_error_with_the_exception_when_the_announcement_raises(caplog):
    caplog.set_level(logging.DEBUG)

    async def dies() -> None:
        raise RuntimeError('a bug in the announcement')

    task = asyncio.create_task(dies())
    await asyncio.gather(task, return_exceptions=True)
    log_announcement_death(task)

    errors = announcement_errors(caplog)
    assert len(errors) == 1, 'the death of the announcement was not logged once at ERROR'
    assert errors[0].exc_info is not None and isinstance(errors[0].exc_info[1], RuntimeError)
    assert 'a bug in the announcement' in errors[0].getMessage()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['cancelled', 'acknowledged'])
async def test_the_done_callback_is_silent_when_the_announcement_is_cancelled_or_ends_normally(ending, caplog):
    caplog.set_level(logging.DEBUG)
    task = asyncio.create_task(asyncio.Event().wait() if ending == 'cancelled' else asyncio.sleep(0))
    if ending == 'cancelled':
        await asyncio.sleep(0)
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    log_announcement_death(task)

    assert [record for record in caplog.records if record.name == instance_module.log.name] == []


@pytest.mark.asyncio
async def test_an_announcement_dying_on_a_bug_is_logged_at_error_before_shutdown(caplog):
    """Ruling 3, through the production lifespan: the record lands while ingest is still up, not only at shutdown.

    Shutdown is otherwise unchanged: awaiting the dead task there still raises the bug out of the lifespan.
    """
    caplog.set_level(logging.DEBUG)
    store = LifespanStore('bug')
    running = RunningApp(store)
    logged_while_up = False

    with pytest.raises(RuntimeError, match='a bug in the store client'):
        async with running.running():
            for _ in range(int(GUARD_S / 0.01)):
                if announcement_errors(caplog):
                    logged_while_up = True
                    break
                await asyncio.sleep(0.01)

    assert store.registered, 'the announcement was never made'
    assert logged_while_up, 'the announcement died on a bug and nothing was logged at ERROR while ingest was up'
    errors = announcement_errors(caplog)
    assert len(errors) == 1
    assert isinstance(errors[0].exc_info[1], RuntimeError)
