"""The ack-gated executor's stalled state on gRPC health, one named health service per broker handle (P11c).

Bead tj-7zjov1.55 against builder-ingest's e136efb. The design: the bead's acceptance (a Health Check on the handle's
name reads NOT_SERVING while stalled and SERVING after deliveries resume; '' stays SERVING throughout), the architect
ruling (a) on P11 tj-7zjov1.20, P11b tj-7zjov1.54 (GrpcServerHost.set_service_health refuses ''), ADR tj-fa1rpu D8
(the name carries the quota scope, never an account or a credential), and the architect's 13:55 note on this bead:
the alarm's OWN LOOP, the task start() creates, is driven here without calling check_stall, and removing that task
from start() reds those tests.

HOW IT IS DRIVEN.

* Every status is read back through a standard grpc.health.v1 Check on a real loopback GrpcServerHost, never through
  private state. The handles are production BrokerHandles over a real Scheduler and a real AckGatedExecutor; their
  reports reach the host through the production NamedHealthReporter. The runner stands for the vendor, the store
  double holds every delivery until the test answers it, and the clock is moved by hand.
* The alarm's sleep is TickSleep: its check interval waits for a tick the test releases, so "the loop ticked" is a
  step the test takes, not a race. Some cases call check_stall directly, as the P11 rig does; the alarm-loop cases
  never do.
* The last case is the production lifespan (main.create_app -> app_depends) with a real AlpacaHandle, real time and
  the real asyncio.sleep, the store a double: the stall is raised by the running loop alone and read over gRPC.

Every wait is bounded by GUARD_S, so a regression fails rather than hangs.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.rpc.channel import create_channel
from common.rpc.server import BindAddress, GrpcServerHost
from data.ingest.app import app_depends, main
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.handle import ALPACA_DATA_SCOPE, AlpacaHandle
from data.ingest.app.brokers.handle import INGEST_DELIVERY_HEALTH_PREFIX, BrokerHandle
from data.ingest.app.grpc_host import NamedHealthReporter
from data.ingest.app.scheduler import (
    AckGatedExecutor,
    AttemptResult,
    ExecutorSettings,
    Limit,
    RetryPolicy,
    ScheduledRequest,
)
from data.ingest.tests.grpc_bind import LOOPBACK, LoopbackGrpc
from data.ingest.tests.scheduler_doubles import real_scheduler
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    DeliverAttemptResponse,
    DeliveryOutcome,
    DeliveryTrailer,
    DeliveryVerdict,
)
from schemas.data_ingest.submit import BarsRequest, SubmitRequest


pytestmark = pytest.mark.data_ingest

SCOPE_A = f'{DataSource.ALPACA_API.value}_HEALTH_A'
SCOPE_B = f'{DataSource.ALPACA_API.value}_HEALTH_B'
INSTANCE = 'instance-under-test'
T0 = datetime(2026, 10, 24, 12, tzinfo=UTC)
GUARD_S = 5.0
STALL_TIMEOUT_S = 300.0
# The alarm's check interval in the unit rig; TickSleep holds a sleep of exactly this length until a tick.
WATCH_S = 5.0
SERVING = health_pb2.HealthCheckResponse.SERVING
NOT_SERVING = health_pb2.HealthCheckResponse.NOT_SERVING
OVERALL = ''


# ---------------------------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------------------------


class FakeClock:
    """Now, moved only by the test."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class TickSleep:
    """The executor's sleep: the alarm's interval waits for a tick from the test; any other wait returns at once."""

    def __init__(self, interval: float = WATCH_S) -> None:
        self.interval = interval
        self.__ticks: asyncio.Queue[None] = asyncio.Queue()

    async def __call__(self, seconds: float) -> None:
        if seconds == self.interval:
            await self.__ticks.get()
            return
        await asyncio.sleep(0)

    async def tick(self) -> None:
        """Let the alarm's loop wake once, and let whatever that wakes run."""
        self.__ticks.put_nowait(None)
        await settle()


@dataclass
class Delivered:
    """One delivery the store has received and not yet answered."""

    attempt: AttemptDelivery
    answer: asyncio.Future


class HeldStore:
    """A StoreDeliveryClient that holds every delivery until the test answers it; registration acknowledges."""

    def __init__(self) -> None:
        self.received: list[AttemptDelivery] = []
        self.__waiting: asyncio.Queue[Delivered] = asyncio.Queue()

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        self.received.append(attempt)
        answer = asyncio.get_running_loop().create_future()
        await self.__waiting.put(Delivered(attempt, answer))
        return await answer

    async def register_instance(self, request) -> None:
        return None

    async def next(self) -> Delivered:
        return await asyncio.wait_for(self.__waiting.get(), GUARD_S)


def acknowledge(delivered: Delivered) -> None:
    """The store's FINAL answer: the report is acknowledged and the request ends."""
    delivered.answer.set_result(
        DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=delivered.attempt.header.attempt_no)
    )


async def succeeded(request: ScheduledRequest, attempt_no: int) -> AttemptResult:
    """The vendor answered: one SUCCEEDED report per attempt."""
    return AttemptResult(trailer=DeliveryTrailer(outcome=DeliveryOutcome.SUCCEEDED), quota_key=request.quota_scope)


async def settle() -> None:
    """Let every task that can run, run."""
    for _ in range(200):
        await asyncio.sleep(0)


async def eventually(condition: Callable[[], Awaitable[bool]], what: str) -> None:
    for _ in range(int(GUARD_S / 0.01)):
        if await condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f'timed out waiting for {what}')


def alarm_tasks(name: str) -> list[asyncio.Task]:
    """The stall alarm's own loop: the task start() creates, named after the handle."""
    return [task for task in asyncio.all_tasks() if task.get_name() == f'{name}-stall-alarm']


def unit_settings(**overrides) -> ExecutorSettings:
    fields = {
        'buffer_size': 8,
        'workers': 1,
        'stall_timeout_s': STALL_TIMEOUT_S,
        'stall_check_interval_s': WATCH_S,
        'retry': RetryPolicy(max_attempts=5),
    }
    return ExecutorSettings(**{**fields, **overrides})


def make_handle(scope: str, clock: FakeClock, sleep: TickSleep) -> BrokerHandle:
    """A production BrokerHandle over a real Scheduler and a real AckGatedExecutor, the vendor a stand-in."""
    scheduler = real_scheduler({scope: Limit(timedelta(minutes=1), 6000)})
    executor = AckGatedExecutor(
        name=scope, scheduler=scheduler, run_attempt=succeeded, settings=unit_settings(), clock=clock, sleep=sleep
    )
    return BrokerHandle(
        source=DataSource.ALPACA_API,
        scope=scope,
        scheduler=scheduler,
        executor=executor,
        reader=Mock(name='reader, never read here'),
        resolve_feed=lambda: Feed.IEX,
    )


async def status(stub: health_pb2_grpc.HealthStub, service: str) -> int:
    response = await asyncio.wait_for(stub.Check(health_pb2.HealthCheckRequest(service=service), timeout=5), GUARD_S)
    return response.status


class HealthRig:
    """Handles reporting to a real loopback GrpcServerHost through the production NamedHealthReporter.

    Every report the handles make is recorded on its way to the reporter, so "nothing new was reported" is observed.
    """

    def __init__(self, *scopes: str) -> None:
        self.clock = FakeClock()
        self.sleep = TickSleep()
        self.store = HeldStore()
        self.handles = {scope: make_handle(scope, self.clock, self.sleep) for scope in scopes}
        self.host = GrpcServerHost(BindAddress(LOOPBACK, 0), [])
        self.reporter = NamedHealthReporter(self.host)
        self.reports: list[tuple[str, bool]] = []
        self.__requests = 0
        self.__stack = ExitStack()

    def report(self, service: str, serving: bool) -> None:
        self.reports.append((service, serving))
        self.reporter.report(service, serving)

    async def __aenter__(self) -> 'HealthRig':
        await self.host.start()
        self.channel = create_channel(f'{LOOPBACK}:{self.host.port}')
        self.stub = health_pb2_grpc.HealthStub(self.channel)
        for handle in self.handles.values():
            handle.start(self.store, INSTANCE, self.report)
        await settle()
        return self

    async def __aexit__(self, *exc_info) -> None:
        try:
            for handle in self.handles.values():
                await asyncio.wait_for(handle.stop(), GUARD_S)
            await asyncio.wait_for(self.reporter.aclose(), GUARD_S)
            await self.channel.close()
        finally:
            await self.host.stop()

    async def status(self, service: str) -> int:
        await settle()
        return await status(self.stub, service)

    async def hold(self, scope: str) -> Delivered:
        """Submit one request on the scope's handle and return its report, held unanswered by the store."""
        self.__requests += 1
        await self.handles[scope].scheduler.submit(
            ScheduledRequest(request_id=f'r{self.__requests}', quota_scope=scope, payload=None, ready_by=None)
        )
        return await self.store.next()


@pytest.fixture(autouse=True)
def iex_deployment_and_no_cached_client(monkeypatch):
    monkeypatch.delenv('ALPACA_SIP_ENABLED', raising=False)
    monkeypatch.delenv('ALPACA_RATE_LIMIT_PER_MINUTE', raising=False)
    broker_api.set_client(None)
    yield
    broker_api.set_client(None)


# ---------------------------------------------------------------------------------------------
# The name
# ---------------------------------------------------------------------------------------------


def test_the_prefix_is_the_fixed_ingest_delivery_namespace():
    # Written out, so a prefix that drifted (or became '') is caught here and not agreed with below.
    assert INGEST_DELIVERY_HEALTH_PREFIX == 'trader_joe.ingest.delivery.'


@pytest.mark.parametrize('scope', [SCOPE_A, SCOPE_B, f'{DataSource.ALPACA_API.value}_DATA'])
def test_a_handles_health_name_is_the_prefix_plus_its_scope_and_never_empty(scope: str):
    handle = make_handle(scope, FakeClock(), TickSleep())
    assert handle.health_service == f'trader_joe.ingest.delivery.{scope}'
    assert handle.health_service != OVERALL


def test_the_alpaca_handles_health_name_names_its_data_scope():
    """The one production handle: the name starts after the prefix with the vendor's DataSource value (D8)."""
    handle = AlpacaHandle(settings=unit_settings(), calls_per_minute=60)
    assert ALPACA_DATA_SCOPE == 'ALPACA_DATA'
    assert handle.health_service == 'trader_joe.ingest.delivery.ALPACA_DATA'


def test_two_handles_with_different_scopes_have_different_names():
    clock, sleep = FakeClock(), TickSleep()
    assert make_handle(SCOPE_A, clock, sleep).health_service != make_handle(SCOPE_B, clock, sleep).health_service


# ---------------------------------------------------------------------------------------------
# Start, stall, recover: read over a real Health Check
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_handles_name_reads_serving_right_after_the_handle_starts():
    async with HealthRig(SCOPE_A) as rig:
        name = rig.handles[SCOPE_A].health_service
        assert await rig.status(name) == SERVING
        assert await rig.status(OVERALL) == SERVING
        assert rig.reports == [(name, True)]


@pytest.mark.asyncio
async def test_a_stall_reads_not_serving_and_the_acknowledgement_serving_again_with_the_overall_status_untouched():
    async with HealthRig(SCOPE_A) as rig:
        handle = rig.handles[SCOPE_A]
        name = handle.health_service
        held = await rig.hold(SCOPE_A)
        assert await rig.status(name) == SERVING
        assert await rig.status(OVERALL) == SERVING

        rig.clock.advance(STALL_TIMEOUT_S + 1)
        assert handle.executor.check_stall() is True
        assert await rig.status(name) == NOT_SERVING
        assert await rig.status(OVERALL) == SERVING, "a stall flipped the overall status ''"

        acknowledge(held)
        assert await rig.status(name) == SERVING, 'the name did not recover when the oldest report was acknowledged'
        assert handle.executor.stalled is False
        assert await rig.status(OVERALL) == SERVING


@pytest.mark.asyncio
async def test_two_handles_with_different_scopes_flip_independently():
    async with HealthRig(SCOPE_A, SCOPE_B) as rig:
        a, b = rig.handles[SCOPE_A], rig.handles[SCOPE_B]
        held_a = await rig.hold(SCOPE_A)
        rig.clock.advance(STALL_TIMEOUT_S + 1)
        a.executor.check_stall()
        b.executor.check_stall()
        assert await rig.status(a.health_service) == NOT_SERVING
        assert await rig.status(b.health_service) == SERVING, "A's stall reached B's name"
        assert await rig.status(OVERALL) == SERVING

        held_b = await rig.hold(SCOPE_B)
        rig.clock.advance(STALL_TIMEOUT_S + 1)
        b.executor.check_stall()
        assert await rig.status(b.health_service) == NOT_SERVING

        acknowledge(held_a)
        assert await rig.status(a.health_service) == SERVING
        assert await rig.status(b.health_service) == NOT_SERVING, "A's recovery reached B's name"

        acknowledge(held_b)
        assert await rig.status(b.health_service) == SERVING
        assert await rig.status(OVERALL) == SERVING


@pytest.mark.asyncio
async def test_only_transitions_are_reported_never_a_repeat_of_the_state_already_reported():
    async with HealthRig(SCOPE_A) as rig:
        handle = rig.handles[SCOPE_A]
        name = handle.health_service
        for _ in range(3):
            assert handle.executor.check_stall() is False
        assert rig.reports == [(name, True)], 'a check while healthy reported again'

        held = await rig.hold(SCOPE_A)
        rig.clock.advance(STALL_TIMEOUT_S + 1)
        for _ in range(3):
            assert handle.executor.check_stall() is True
        # Past another timeout the ERROR log repeats; the health report must not.
        rig.clock.advance(STALL_TIMEOUT_S + 1)
        assert handle.executor.check_stall() is True
        assert rig.reports == [(name, True), (name, False)], 'a check while stalled reported again'

        acknowledge(held)
        await settle()
        for _ in range(3):
            assert handle.executor.check_stall() is False
        assert rig.reports == [(name, True), (name, False), (name, True)]


# ---------------------------------------------------------------------------------------------
# The alarm's own loop: no check_stall call anywhere below
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_running_alarm_loop_alone_raises_not_serving_and_stop_ends_it():
    """The architect's 13:55 requirement: the task start() creates is what calls check_stall in production.

    Mutation: drop the asyncio.create_task(self.__watch(), ...) line from AckGatedExecutor.start and this test reds
    at the first NOT_SERVING, because nothing else looks at the buffer while the store holds the report.
    """
    rig = HealthRig(SCOPE_A)
    handle = rig.handles[SCOPE_A]
    name = handle.health_service
    async with rig:
        alarms = alarm_tasks(SCOPE_A)
        held = await rig.hold(SCOPE_A)
        await rig.sleep.tick()
        assert await rig.status(name) == SERVING, 'stalled before the timeout had passed'

        rig.clock.advance(STALL_TIMEOUT_S + 1)
        await rig.sleep.tick()
        assert handle.executor.stalled is True, 'the alarm loop did not raise the alarm'
        assert await rig.status(name) == NOT_SERVING
        assert await rig.status(OVERALL) == SERVING, "the alarm loop flipped the overall status ''"

        acknowledge(held)
        assert await rig.status(name) == SERVING
        assert await rig.status(OVERALL) == SERVING

        # Stalled again, then stopped: the loop is cancelled and its later ticks report nothing.
        await rig.hold(SCOPE_A)
        rig.clock.advance(STALL_TIMEOUT_S + 1)
        await rig.sleep.tick()
        assert await rig.status(name) == NOT_SERVING
        await asyncio.wait_for(handle.stop(), GUARD_S)
        assert len(alarms) == 1, f'start() created {len(alarms)} alarm loops'
        assert alarms[0].cancelled(), 'stop() did not cancel the alarm loop'
        assert all(task.done() for task in alarm_tasks(SCOPE_A))
        reported = list(rig.reports)
        rig.clock.advance(10 * STALL_TIMEOUT_S)
        for _ in range(3):
            await rig.sleep.tick()
        assert rig.reports == reported, 'something reported after stop()'
        assert rig.reports == [(name, True), (name, False), (name, True), (name, False)]


@pytest.mark.asyncio
async def test_a_raising_observer_does_not_stop_the_alarm(caplog):
    """on_stalled_change raising every time: logged, and the loop, the flag and the ERROR log carry on."""
    caplog.set_level(logging.INFO)
    clock, sleep, store = FakeClock(), TickSleep(), HeldStore()
    handle = make_handle(SCOPE_A, clock, sleep)
    calls: list[bool] = []

    def raising(stalled: bool) -> None:
        calls.append(stalled)
        raise RuntimeError('the observer is broken')

    executor = handle.executor
    executor.start(store, INSTANCE, raising)
    try:
        alarms = alarm_tasks(SCOPE_A)
        await handle.scheduler.submit(ScheduledRequest(request_id='r1', quota_scope=SCOPE_A, payload=None))
        first = await store.next()
        clock.advance(STALL_TIMEOUT_S + 1)
        await sleep.tick()
        assert executor.stalled is True, 'the alarm loop did not raise the alarm'
        assert alarms and not any(task.done() for task in alarms), 'a raising observer ended the alarm loop'
        assert any(
            record.levelno == logging.ERROR and 'Store delivery stalled' in record.getMessage()
            for record in caplog.records
        ), 'the alarm ERROR log is gone'
        assert any('reporting the stalled state' in record.getMessage() for record in caplog.records)

        acknowledge(first)
        await settle()
        assert executor.stalled is False

        # The loop still runs: a second stall is raised by it alone.
        await handle.scheduler.submit(ScheduledRequest(request_id='r2', quota_scope=SCOPE_A, payload=None))
        second = await store.next()
        clock.advance(STALL_TIMEOUT_S + 1)
        await sleep.tick()
        assert executor.stalled is True, 'the alarm did not fire again after a raising observer'
        assert calls == [False, True, False, True]
        acknowledge(second)
        await settle()
    finally:
        await asyncio.wait_for(executor.stop(), GUARD_S)


# ---------------------------------------------------------------------------------------------
# NamedHealthReporter
# ---------------------------------------------------------------------------------------------


class GatedHost:
    """A host whose set_service_health records each status applied; the first set waits for the gate."""

    def __init__(self) -> None:
        self.applied: list[tuple[str, bool]] = []
        self.gate = asyncio.Event()
        self.__first = True

    async def set_service_health(self, service: str, serving: bool) -> None:
        if self.__first:
            self.__first = False
            await self.gate.wait()
        self.applied.append((service, serving))


@pytest.mark.asyncio
async def test_back_to_back_reports_for_one_service_apply_in_order_and_the_last_wins():
    """An older flag never lands after a newer one, even when the host is slow with the older one."""
    host = GatedHost()
    reporter = NamedHealthReporter(host)
    reporter.report('svc', False)
    await settle()
    # The first set is now held by the host; two newer reports arrive meanwhile.
    reporter.report('svc', False)
    reporter.report('svc', True)
    await settle()
    assert host.applied == [], 'a newer status landed while an older one was still being set'
    host.gate.set()
    await asyncio.wait_for(reporter.aclose(), GUARD_S)
    assert host.applied[-1] == ('svc', True), f'the last report did not win: {host.applied}'
    assert host.applied[0] == ('svc', False)


@pytest.mark.asyncio
async def test_a_burst_of_reports_reads_back_the_last_over_a_real_health_check():
    async with GrpcServerHost(BindAddress(LOOPBACK, 0), []) as host:
        reporter = NamedHealthReporter(host)
        for serving in (True, False, True, False, False, True, False):
            reporter.report('trader_joe.ingest.delivery.X', serving)
        await asyncio.wait_for(reporter.aclose(), GUARD_S)
        channel = create_channel(f'{LOOPBACK}:{host.port}')
        try:
            stub = health_pb2_grpc.HealthStub(channel)
            assert await status(stub, 'trader_joe.ingest.delivery.X') == NOT_SERVING
            assert await status(stub, OVERALL) == SERVING
        finally:
            await channel.close()


@pytest.mark.asyncio
async def test_reporting_the_overall_name_surfaces_the_hosts_value_error_and_leaves_it_serving():
    async with GrpcServerHost(BindAddress(LOOPBACK, 0), []) as host:
        reporter = NamedHealthReporter(host)
        reporter.report(OVERALL, False)
        with pytest.raises(ValueError, match="''"):
            await asyncio.wait_for(reporter.aclose(), GUARD_S)
        channel = create_channel(f'{LOOPBACK}:{host.port}')
        try:
            assert await status(health_pb2_grpc.HealthStub(channel), OVERALL) == SERVING
        finally:
            await channel.close()


@pytest.mark.asyncio
async def test_aclose_flushes_every_pending_status():
    """Reported and closed with no yield in between: every service's last flag still lands."""
    host = GatedHost()
    host.gate.set()
    reporter = NamedHealthReporter(host)
    reporter.report('a', False)
    reporter.report('b', True)
    reporter.report('a', True)
    await asyncio.wait_for(reporter.aclose(), GUARD_S)
    assert sorted(host.applied) == [('a', True), ('b', True)]


# ---------------------------------------------------------------------------------------------
# The production lifespan: real handle, real host, real reporter, real loop, real time
# ---------------------------------------------------------------------------------------------

LIFESPAN_MAX_ATTEMPTS = 3


def spent_request() -> SubmitRequest:
    """A request arriving at its budget: reported FAILED, PERMANENT with no vendor call, so no network is needed."""
    bars = BarsRequest(
        source=DataSource.ALPACA_API,
        asset_symbol='VFV',
        asset_type=AssetType.STOCK,
        data_types=[DataType.MARKET_ACTIVITY],
        granularity=Granularity.THIRTY_MINUTES,
        start=datetime(2026, 1, 2, 14, 30, tzinfo=UTC),
        end=datetime(2026, 1, 2, 15, 30, tzinfo=UTC),
        update_type=UpdateType.STATIC,
    )
    return SubmitRequest(
        request_id='spent', owner='rebalancer', bars=bars, attempt_count=LIFESPAN_MAX_ATTEMPTS, recovered=True
    )


@asynccontextmanager
async def production_app(handle: AlpacaHandle, store: HeldStore) -> AsyncIterator[health_pb2_grpc.HealthStub]:
    """The production lifespan on a loopback port, the store a double; yields a health stub on its gRPC port."""
    app = main.create_app({DataSource.ALPACA_API: handle.reader}, {DataSource.ALPACA_API: handle})
    channel_to_store = Mock(close=AsyncMock())
    with ExitStack() as stubs:
        stubs.enter_context(patch.object(app_depends, 'initialize_latency_server', Mock()))
        stubs.enter_context(patch.object(app_depends, 'target_from_env', lambda _name: 'store-double:1'))
        stubs.enter_context(patch.object(app_depends, 'create_channel', lambda _target: channel_to_store))
        stubs.enter_context(patch.object(app_depends, 'GrpcStoreDeliveryClient', lambda _channel: store))
        async with LoopbackGrpc() as grpc_bind:
            async with asyncio.timeout(GUARD_S):
                async with app.router.lifespan_context(app):
                    channel = create_channel(f'{LOOPBACK}:{grpc_bind.port}')
                    try:
                        yield health_pb2_grpc.HealthStub(channel)
                    finally:
                        await channel.close()


@pytest.mark.asyncio
async def test_the_production_app_reports_a_stall_on_the_handles_name_through_its_running_alarm():
    """Acceptance, end to end: SERVING at start, NOT_SERVING while stalled, SERVING once acknowledged; '' throughout.

    Nothing here calls check_stall: the stall is raised by the alarm loop the handle's start() created, sleeping
    the real asyncio.sleep, and read by a Health Check on the production gRPC host.
    """
    handle = AlpacaHandle(
        settings=ExecutorSettings(
            stall_timeout_s=0.2, stall_check_interval_s=0.02, retry=RetryPolicy(max_attempts=LIFESPAN_MAX_ATTEMPTS)
        ),
        calls_per_minute=60,
    )
    name = handle.health_service
    store = HeldStore()

    async def reads(service: str, expected: int) -> bool:
        return await status(stub, service) == expected

    async with production_app(handle, store) as stub:
        assert await status(stub, name) == SERVING, 'the handle name did not read SERVING once the app started'
        assert await status(stub, OVERALL) == SERVING

        await handle.submit(spent_request())
        held = await store.next()
        await eventually(lambda: reads(name, NOT_SERVING), 'the running alarm to report the stall')
        assert await status(stub, OVERALL) == SERVING, "the stall flipped the overall status ''"

        acknowledge(held)
        await eventually(lambda: reads(name, SERVING), 'the name to recover after the acknowledgement')
        assert await status(stub, OVERALL) == SERVING
