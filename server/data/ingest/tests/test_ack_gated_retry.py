"""The acknowledgement-gated executor, the bounded outgoing buffer, Submit and the Alpaca handle's pacing (P11).

Bead tj-7zjov1.20 against builder-ingest's 59d74a6. The design: tj-26jt9w addendum 1 item 4, addenda 4, 5 and 6
(R2 ruled (c) with a bound, the ready-by check at dequeue), the bead's acceptance criteria, and the architect's
P10 gate notes on the bead (scope names start with the vendor's DataSource value; nothing paces an Alpaca call
but the handle's scheduler).

HOW IT IS DRIVEN. The executor runs on a REAL Scheduler from the production SchedulerBuilder, so the FIFO queue
and the ready-by check at dequeue are the production ones. Everything around it is a double: the store client
(DoubleStore) holds every delivery until the test answers it, so "no second vendor call until the store answers"
is observed rather than inferred; the runner (FakeRunner) stands for the vendor and records every attempt it is
asked to make; the clock (FakeClock) is moved by hand; and the sleep (FakeSleep) records every wait and returns at
once, except the stall alarm's own interval, which it parks so the tests drive check_stall themselves.

The Submit and pacing half drives the production AlpacaHandle with a stub vendor client, never the network.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests
from alpaca.common.exceptions import APIError

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import ExogenousError, InvalidRequestError, Reason
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.classify import RATE_LIMIT_RESET_HEADER, RETRY_AFTER_HEADER
from data.ingest.app.brokers.alpaca.handle import (
    ALPACA_BARS_PAGE_SIZE,
    ALPACA_CALLS_PER_MINUTE_CEILING,
    ALPACA_DATA_SCOPE,
    AlpacaHandle,
    estimate_bars_calls,
    executor_settings_from_env,
)
from data.ingest.app.brokers.bars_attempt import BarsAttemptRunner, BarsWork, error_class
from data.ingest.app.brokers.handle import BrokerHandle
from data.ingest.app.brokers.interface import BarsQuery, Instrument
from data.ingest.app.brokers.rate_budget import RateBudget, RequestPriority, priority_for_update_type
from data.ingest.app.scheduler import (
    AckGatedExecutor,
    AttemptResult,
    Credential,
    ExecutorSettings,
    Limit,
    RetryPolicy,
    ScheduledRequest,
    SchedulerBuilder,
    SchedulerConfigError,
    StaticCredentialProvider,
)
from data.ingest.tests.feed_rule import assert_a_bars_report_names_the_tape, assert_a_no_tape_report_is_unspecified
from data.ingest.tests.scheduler_doubles import RecordingScheduler, alpaca_reader
from routers.data_ingest.submit_handler import IngestSubmitHandler
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    DeliverAttemptResponse,
    DeliveryErrorClass,
    DeliveryOutcome,
    DeliveryTrailer,
    DeliveryVerdict,
)
from schemas.data_ingest.fetch_dataset import BarPage
from schemas.data_ingest.submit import AssetListRequest, BarsRequest, CorporateActionsRequest, SubmitRequest


pytestmark = pytest.mark.data_ingest

SCOPE = f'{DataSource.ALPACA_API.value}_TEST'
HANDLE = 'TEST_HANDLE'
INSTANCE = 'instance-under-test'
T0 = datetime(2026, 10, 24, 12, tzinfo=UTC)
# Every wait for the code under test is bounded by this, so a regression fails rather than hangs.
GUARD_S = 5.0
# The stall alarm's check interval. FakeSleep parks a sleep of exactly this length, so the alarm's loop never
# runs on its own and each test calls check_stall itself.
WATCH_S = 7777.0

START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
END = START + timedelta(hours=1)


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


class DoubleStore:
    """A StoreDeliveryClient that holds every delivery until the test answers it."""

    def __init__(self) -> None:
        self.received: list[AttemptDelivery] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.__waiting: asyncio.Queue[Delivered] = asyncio.Queue()

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        self.received.append(attempt)
        answer = asyncio.get_running_loop().create_future()
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await self.__waiting.put(Delivered(attempt, answer))
        try:
            return await answer
        finally:
            self.in_flight -= 1

    async def register_instance(self, request):
        raise AssertionError('the executor never registers an instance; that is P36')

    async def next(self) -> Delivered:
        return await asyncio.wait_for(self.__waiting.get(), GUARD_S)

    @property
    def unanswered(self) -> int:
        return self.__waiting.qsize()


def answer(delivered: Delivered, verdict: DeliveryVerdict, attempt_count: int | None = None) -> None:
    """The store's acknowledgement, with the durable count it reached (the attempt's own number by default)."""
    count = delivered.attempt.header.attempt_no if attempt_count is None else attempt_count
    delivered.answer.set_result(DeliverAttemptResponse(verdict=verdict, attempt_count=count))


def fail_delivery(delivered: Delivered) -> None:
    """The store could not be reached: the delivery raises, as GrpcStoreDeliveryClient's does."""
    delivered.answer.set_exception(ExogenousError(Reason.PEER_UNAVAILABLE, 'the store is unreachable'))


class FakeRunner:
    """Stands for the vendor: records each attempt it is asked to make and answers from a script.

    The script's last entry repeats. An exception in the script is raised.
    """

    def __init__(self, *script: AttemptResult | BaseException) -> None:
        self.calls: list[tuple[str, int]] = []
        self.__script = script

    async def __call__(self, request: ScheduledRequest, attempt_no: int) -> AttemptResult:
        self.calls.append((request.request_id, attempt_no))
        result = self.__script[min(len(self.calls), len(self.__script)) - 1]
        if isinstance(result, BaseException):
            raise result
        return result


def succeeded() -> AttemptResult:
    return AttemptResult(trailer=DeliveryTrailer(outcome=DeliveryOutcome.SUCCEEDED), quota_key=SCOPE)


def failed(klass: DeliveryErrorClass | None, reset_at: datetime | None = None) -> AttemptResult:
    return AttemptResult(
        trailer=DeliveryTrailer(outcome=DeliveryOutcome.FAILED, error_class=klass, detail='the vendor failed'),
        quota_key=SCOPE,
        reset_at=reset_at,
    )


def settings(**overrides) -> ExecutorSettings:
    retry = overrides.pop('retry', RetryPolicy(max_attempts=5, base_delay_s=1.0, max_delay_s=60.0, factor=2.0))
    fields = {
        'buffer_size': 8,
        'workers': 1,
        'stall_timeout_s': 300.0,
        'stall_check_interval_s': WATCH_S,
        'redelivery_base_s': 1.0,
        'redelivery_max_s': 1.5,
        'retry': retry,
    }
    return ExecutorSettings(**{**fields, **overrides})


class Rig:
    """An AckGatedExecutor on a real Scheduler, with the doubles above. Enter it to start and stop the executor."""

    def __init__(self, runner: FakeRunner, executor_settings: ExecutorSettings | None = None) -> None:
        self.runner = runner
        self.clock = FakeClock()
        self.sleep = FakeSleep()
        self.store = DoubleStore()
        self.finished: list[str] = []
        minute = timedelta(minutes=1)
        self.scheduler = (
            SchedulerBuilder(clock=self.clock)
            .declare_scope(SCOPE, [Limit(minute, 6000)])
            .set_limit(minute, 6000, scope=SCOPE)
            .cost_function(lambda _request, _page_size: 1, page_size=1)
            .credential_provider(StaticCredentialProvider(Credential(key_id='key', secret='secret')))
            .on_expired(self.__expired)
            .build()
        )
        self.executor = AckGatedExecutor(
            name=HANDLE,
            scheduler=self.scheduler,
            run_attempt=runner,
            settings=executor_settings if executor_settings is not None else settings(),
            on_finished=lambda request: self.finished.append(request.request_id),
            clock=self.clock,
            sleep=self.sleep,
        )

    async def __expired(self, expired) -> None:
        # The scheduler's expired sink, wired as BrokerHandle wires it.
        await self.executor.report_expired(expired)

    async def __aenter__(self) -> 'Rig':
        self.executor.start(self.store, INSTANCE)
        return self

    async def __aexit__(self, *exc_info) -> None:
        await asyncio.wait_for(self.executor.stop(), GUARD_S)

    async def submit(
        self,
        request_id: str,
        *,
        ready_by: datetime | None = None,
        attempt_count: int = 0,
        payload=None,
        feed: Feed | None = None,
    ) -> None:
        await self.scheduler.submit(
            ScheduledRequest(
                request_id=request_id,
                quota_scope=SCOPE,
                payload=payload,
                ready_by=ready_by,
                attempt_count=attempt_count,
                feed=feed,
            )
        )


async def settle() -> None:
    """Let every task that can run, run: a step the code under test could take, it has taken by now."""
    for _ in range(200):
        await asyncio.sleep(0)


def numbers(*delivered: Delivered) -> list[int]:
    return [item.attempt.header.attempt_no for item in delivered]


def assert_attempt_numbers_never_reused(received: list[AttemptDelivery]) -> None:
    """The store-seam idempotency key: one (request_id, attempt_no) is one report, however often it is sent."""
    seen: dict[tuple[str, int], AttemptDelivery] = {}
    for attempt in received:
        key = (attempt.header.request_id, attempt.header.attempt_no)
        assert seen.setdefault(key, attempt) == attempt, f'attempt number {key} was reused for a different report'


# ---------------------------------------------------------------------------------------------
# The gate: no retry before the store's acknowledgement
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_retry_waits_for_the_stores_answer_and_the_attempts_are_numbered_one_two_three():
    """Addendum 4: no retry of a request runs until the store has acknowledged the report of its last attempt.

    Each FAILED TRANSIENT attempt is delivered and then NOTHING happens -- no second vendor call, no backoff --
    until the store answers CONTINUE. Only then is the backoff waited and the next attempt made.
    """
    runner = FakeRunner(failed(DeliveryErrorClass.TRANSIENT))
    async with Rig(runner) as rig:
        await rig.submit('r1')

        first = await rig.store.next()
        await settle()
        assert runner.calls == [('r1', 1)], 'a second vendor call was made before the store answered'
        assert rig.sleep.slept == [], 'the backoff started before the store answered'
        answer(first, DeliveryVerdict.CONTINUE)

        second = await rig.store.next()
        await settle()
        assert runner.calls == [('r1', 1), ('r1', 2)]
        answer(second, DeliveryVerdict.CONTINUE)

        third = await rig.store.next()
        answer(third, DeliveryVerdict.FINAL)
        await settle()

    assert numbers(first, second, third) == [1, 2, 3]
    assert runner.calls == [('r1', 1), ('r1', 2), ('r1', 3)]
    # The backoff after attempts 1 and 2 (base 1 s, factor 2), each waited after its acknowledgement.
    assert rig.sleep.slept == [1.0, 2.0]
    assert {item.header.instance_id for item in rig.store.received} == {INSTANCE}
    assert_attempt_numbers_never_reused(rig.store.received)
    # FINAL drops the request: it is finished, and nothing more is attempted.
    assert rig.finished == ['r1']


@pytest.mark.asyncio
async def test_the_next_attempt_is_numbered_from_the_count_the_store_acknowledged():
    """A recovered request resumes from the store's durable count, never from one ingest kept (retry.py)."""
    runner = FakeRunner(failed(DeliveryErrorClass.TRANSIENT))
    async with Rig(runner) as rig:
        await rig.submit('r1')
        first = await rig.store.next()
        answer(first, DeliveryVerdict.CONTINUE, attempt_count=3)
        second = await rig.store.next()
        answer(second, DeliveryVerdict.FINAL)

    assert numbers(first, second) == [1, 4]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'klass', [pytest.param(DeliveryErrorClass.PERMANENT, id='permanent'), pytest.param(None, id='no-class')]
)
async def test_a_permanent_refusal_is_not_retried_even_when_the_store_answers_continue(klass):
    runner = FakeRunner(failed(klass))
    async with Rig(runner) as rig:
        await rig.submit('r1')
        first = await rig.store.next()
        answer(first, DeliveryVerdict.CONTINUE)
        await settle()

    assert runner.calls == [('r1', 1)]
    assert len(rig.store.received) == 1
    assert rig.sleep.slept == []
    assert rig.finished == ['r1']


@pytest.mark.asyncio
async def test_final_drops_a_temporary_failure():
    runner = FakeRunner(failed(DeliveryErrorClass.TRANSIENT))
    async with Rig(runner) as rig:
        await rig.submit('r1')
        answer(await rig.store.next(), DeliveryVerdict.FINAL)
        await settle()

    assert runner.calls == [('r1', 1)]
    assert rig.sleep.slept == []
    assert rig.finished == ['r1']


@pytest.mark.asyncio
async def test_a_success_is_never_retried_whatever_the_store_answers():
    runner = FakeRunner(succeeded())
    async with Rig(runner) as rig:
        await rig.submit('r1')
        answer(await rig.store.next(), DeliveryVerdict.CONTINUE)
        await settle()

    assert runner.calls == [('r1', 1)]
    assert rig.finished == ['r1']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('reset_in_s', 'expected_wait_s'),
    [
        pytest.param(30.0, 30.0, id='until-the-reset'),
        pytest.param(10_000.0, 600.0, id='capped-by-max-reset-wait'),
        pytest.param(-5.0, 0.0, id='a-reset-already-past-waits-nothing'),
        pytest.param(None, 1.0, id='no-reset-uses-the-backoff'),
    ],
)
async def test_a_rate_limited_attempt_waits_for_the_reset_capped_and_without_one_the_backoff(
    reset_in_s, expected_wait_s
):
    reset_at = None if reset_in_s is None else T0 + timedelta(seconds=reset_in_s)
    runner = FakeRunner(failed(DeliveryErrorClass.RATE_LIMITED, reset_at=reset_at), succeeded())
    retry = RetryPolicy(max_attempts=5, base_delay_s=1.0, max_delay_s=60.0, factor=2.0, max_reset_wait_s=600.0)
    async with Rig(runner, settings(retry=retry)) as rig:
        await rig.submit('r1')
        answer(await rig.store.next(), DeliveryVerdict.CONTINUE)
        second = await rig.store.next()
        answer(second, DeliveryVerdict.FINAL)

    assert rig.sleep.slept == [expected_wait_s]
    assert numbers(second) == [2]


# ---------------------------------------------------------------------------------------------
# last_attempt: set exactly when a FAILED attempt spends the budget
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_attempt_is_set_only_on_the_failed_attempt_that_reaches_max_attempts():
    runner = FakeRunner(failed(DeliveryErrorClass.TRANSIENT))
    async with Rig(runner, settings(retry=RetryPolicy(max_attempts=3))) as rig:
        await rig.submit('r1')
        flags = []
        for verdict in (DeliveryVerdict.CONTINUE, DeliveryVerdict.CONTINUE, DeliveryVerdict.FINAL):
            delivered = await rig.store.next()
            flags.append((delivered.attempt.header.attempt_no, delivered.attempt.trailer.last_attempt))
            answer(delivered, verdict)
        await settle()

    assert flags == [(1, False), (2, False), (3, True)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('result', 'expected'),
    [
        pytest.param(failed(DeliveryErrorClass.TRANSIENT), True, id='failed-at-the-store-count-reaching-max'),
        pytest.param(failed(DeliveryErrorClass.PERMANENT), True, id='permanent-at-max'),
        pytest.param(succeeded(), False, id='succeeded-at-max'),
    ],
)
async def test_last_attempt_follows_the_attempt_number_from_the_stores_count(result, expected):
    # attempt_count 2 with max_attempts 3: this attempt is number 3, the last the budget allows.
    async with Rig(FakeRunner(result), settings(retry=RetryPolicy(max_attempts=3))) as rig:
        await rig.submit('r1', attempt_count=2)
        delivered = await rig.store.next()
        answer(delivered, DeliveryVerdict.FINAL)

    assert delivered.attempt.header.attempt_no == 3
    assert delivered.attempt.trailer.last_attempt is expected


@pytest.mark.asyncio
async def test_an_expired_report_never_carries_last_attempt_even_at_max_attempts():
    runner = FakeRunner(succeeded())
    async with Rig(runner, settings(retry=RetryPolicy(max_attempts=3))) as rig:
        await rig.submit('r1', ready_by=T0 - timedelta(seconds=1), attempt_count=2)
        delivered = await rig.store.next()
        answer(delivered, DeliveryVerdict.FINAL)

    assert delivered.attempt.trailer.outcome is DeliveryOutcome.EXPIRED
    assert delivered.attempt.header.attempt_no == 3
    assert delivered.attempt.trailer.last_attempt is False


@pytest.mark.asyncio
async def test_an_attempt_that_spent_the_budget_is_not_retried_even_when_the_store_answers_continue():
    """A CONTINUE on the attempt that spent the budget must not buy another.

    store.proto DeliveryTrailer.last_attempt: 'Set by ingest when it will make no further attempt for this request
    because its attempt budget is spent with this attempt. Ingest holds the budget'. executor.py's module docstring
    says the same: an attempt that spent the budget is never retried whatever the store answers.
    """
    runner = FakeRunner(failed(DeliveryErrorClass.TRANSIENT))
    async with Rig(runner, settings(retry=RetryPolicy(max_attempts=3))) as rig:
        await rig.submit('r1', attempt_count=2)
        last = await rig.store.next()
        assert last.attempt.trailer.last_attempt is True
        answer(last, DeliveryVerdict.CONTINUE)
        await settle()

    assert runner.calls == [('r1', 3)], 'ingest attempted past its own retry budget'
    assert rig.finished == ['r1']
    assert_attempt_numbers_never_reused(rig.store.received)


@pytest.mark.asyncio
@pytest.mark.parametrize('attempt_count', [3, 5], ids=['entering-at-max', 'entering-past-max'])
async def test_a_request_entering_at_or_past_the_budget_gets_no_vendor_attempt_through_the_handle(attempt_count):
    """The guarantee lives at BrokerHandle.submit, NOT in the executor.

    Formerly a KNOWN GAP pinned on tj-7zjov1.20; closed by P36 (tj-7zjov1.21, 85ad218). The executor itself still attempts whatever it takes from the queue: fed a request at or past max_attempts
    directly, it would make one attempt numbered attempt_count + 1. What keeps that from happening is the restart
    intake in BrokerHandle.submit, which never queues such a request and reports it EXHAUSTED at attempt_count + 1
    with no vendor call (decision tj-wisp-g9rmw0 ruling 1, P36b tj-7zjov1.65; it was FAILED, PERMANENT,
    last_attempt before). So this test goes through the handle, over the same Rig executor and
    scheduler: a request that bypassed the handle would not be covered by it, and is not the designed path.
    test_restart_intake.py holds the full set of intake cases.
    """
    runner = FakeRunner(failed(DeliveryErrorClass.TRANSIENT))
    async with Rig(runner, settings(retry=RetryPolicy(max_attempts=3))) as rig:
        handle = BrokerHandle(
            source=DataSource.ALPACA_API,
            scope=SCOPE,
            scheduler=rig.scheduler,
            executor=rig.executor,
            reader=Mock(),
            resolve_feed=lambda: Feed.IEX,
        )
        ack = await handle.submit(submit_request('r1', attempt_count=attempt_count, recovered=True))
        only = await rig.store.next()
        answer(only, DeliveryVerdict.CONTINUE)
        await settle()

    assert ack.accepted is not None
    assert runner.calls == [], 'a vendor attempt was made past the budget'
    assert rig.scheduler.queued == 0
    assert only.attempt.header.attempt_no == attempt_count + 1
    assert only.attempt.header.quota_key == '', 'an exhausted report named a quota it never spent'
    assert only.attempt.trailer.outcome is DeliveryOutcome.EXHAUSTED
    assert only.attempt.trailer.error_class is None
    assert only.attempt.trailer.last_attempt is False
    assert only.attempt.trailer.vendor_stats is None
    assert only.attempt.header.feed is ack.accepted.feed is Feed.IEX
    assert_a_bars_report_names_the_tape(only.attempt.header, Feed.IEX)
    assert len(rig.store.received) == 1
    assert rig.finished == ['r1']


@pytest.mark.asyncio
async def test_the_alpaca_handle_does_not_queue_a_request_already_past_the_budget_and_reports_it():
    """The Alpaca handle reports a request already past the budget and never queues it.

    Formerly KNOWN GAP test_known_gap_the_handle_queues_a_request_already_past_the_budget_unchanged, which pinned
    the Alpaca handle queueing such a request with its count unchanged. P36 (tj-7zjov1.21) bounds it at intake: the
    request is accepted in the ack, never queued, and reported at count + 1: EXHAUSTED since P36b (tj-7zjov1.65,
    decision tj-wisp-g9rmw0 ruling 1), FAILED PERMANENT last_attempt before it.
    """
    handle = AlpacaHandle(settings=settings(retry=RetryPolicy(max_attempts=3)), calls_per_minute=60)
    store = DoubleStore()
    handle.start(store, INSTANCE)
    try:
        ack = await handle.submit(submit_request(attempt_count=5))
        delivered = await store.next()
        assert handle.scheduler.queued == 0, 'the exhausted request was queued'
        answer(delivered, DeliveryVerdict.FINAL)
        await settle()
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)

    assert ack.accepted is not None
    assert ack.accepted.quota_key == ALPACA_DATA_SCOPE
    assert delivered.attempt.header.attempt_no == 6
    assert delivered.attempt.header.instance_id == INSTANCE
    assert delivered.attempt.header.quota_key == ''
    assert delivered.attempt.trailer.outcome is DeliveryOutcome.EXHAUSTED
    assert delivered.attempt.trailer.error_class is None
    assert delivered.attempt.trailer.last_attempt is False
    assert delivered.attempt.header.feed is ack.accepted.feed is Feed.IEX
    assert_a_bars_report_names_the_tape(delivered.attempt.header, Feed.IEX)
    assert handle.active == 0


@pytest.mark.asyncio
async def test_at_the_store_seam_an_attempt_number_is_never_reused_for_a_different_outcome():
    """Each (request_id, attempt_no) the store sees carries one report.

    A redelivery repeats it unchanged; a failure, its retry and the retry's expiry each take a new number.
    """
    runner = FakeRunner(failed(DeliveryErrorClass.TRANSIENT))
    async with Rig(runner) as rig:
        await rig.submit('r1', ready_by=T0 + timedelta(seconds=10))
        fail_delivery(await rig.store.next())
        fail_delivery(await rig.store.next())
        answer(await rig.store.next(), DeliveryVerdict.CONTINUE)
        second = await rig.store.next()
        answer(second, DeliveryVerdict.CONTINUE)
        rig.clock.advance(20)
        third = await rig.store.next()
        answer(third, DeliveryVerdict.FINAL)
        await settle()

    assert [(item.header.attempt_no, item.trailer.outcome) for item in rig.store.received] == [
        (1, DeliveryOutcome.FAILED),
        (1, DeliveryOutcome.FAILED),
        (1, DeliveryOutcome.FAILED),
        (2, DeliveryOutcome.FAILED),
        (3, DeliveryOutcome.EXPIRED),
    ]
    assert_attempt_numbers_never_reused(rig.store.received)


# ---------------------------------------------------------------------------------------------
# The bounded outgoing buffer (R2 ruled (c) with a bound)
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_the_buffer_full_the_worker_takes_nothing_and_resumes_as_acks_free_slots_dropping_nothing():
    runner = FakeRunner(succeeded())
    ids = [f'r{n}' for n in range(1, 6)]
    async with Rig(runner, settings(buffer_size=2, workers=1)) as rig:
        for request_id in ids:
            await rig.submit(request_id)
        await settle()

        # Two reports fill the buffer; the worker has taken nothing more from the queue.
        assert runner.calls == [('r1', 1), ('r2', 1)]
        assert rig.scheduler.queued == 3
        assert rig.executor.unacknowledged == 2

        # One acknowledgement frees one slot, and exactly one more request is taken.
        answer(await rig.store.next(), DeliveryVerdict.FINAL)
        await settle()
        assert [call[0] for call in runner.calls] == ['r1', 'r2', 'r3']
        assert rig.scheduler.queued == 2
        assert rig.executor.unacknowledged == 2

        while len(rig.finished) < len(ids):
            answer(await rig.store.next(), DeliveryVerdict.FINAL)
            await settle()

    assert [item.header.request_id for item in rig.store.received] == ids, 'a report was dropped or repeated'
    assert sorted(rig.finished) == ids
    assert rig.store.max_in_flight == 2, 'the buffer held more than its size'
    assert rig.executor.unacknowledged == 0


@pytest.mark.asyncio
async def test_several_workers_never_hold_more_than_the_buffer_plus_one_attempt_each():
    """The stated crash bound: at most buffer_size + workers attempts are in flight, and the buffer never exceeds."""
    runner = FakeRunner(succeeded())
    async with Rig(runner, settings(buffer_size=2, workers=3)) as rig:
        for n in range(10):
            await rig.submit(f'r{n}')
        await settle()

        assert rig.executor.unacknowledged == 2
        assert len(runner.calls) <= 2 + 3
        assert rig.scheduler.queued >= 10 - (2 + 3)

        while len(rig.finished) < 10:
            answer(await rig.store.next(), DeliveryVerdict.FINAL)
            await settle()
            assert rig.executor.unacknowledged <= 2

    assert rig.store.max_in_flight <= 2
    assert sorted(item.header.request_id for item in rig.store.received) == sorted(f'r{n}' for n in range(10))


def test_a_buffer_or_a_worker_count_below_one_is_refused():
    with pytest.raises(ValueError, match='buffer_size'):
        ExecutorSettings(buffer_size=0)
    with pytest.raises(ValueError, match='workers'):
        ExecutorSettings(workers=0)


# ---------------------------------------------------------------------------------------------
# The stall alarm
# ---------------------------------------------------------------------------------------------


def stall_errors(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and record.name == 'data.ingest.app.scheduler.executor'
    ]


@pytest.mark.asyncio
async def test_the_stall_alarm_fires_past_the_timeout_repeats_only_after_another_and_clears_on_the_ack(caplog):
    caplog.set_level(logging.INFO, logger='data.ingest.app.scheduler.executor')
    async with Rig(FakeRunner(succeeded()), settings(stall_timeout_s=300.0)) as rig:
        await rig.submit('r1')
        stuck = await rig.store.next()

        rig.clock.advance(300)
        assert rig.executor.check_stall() is False, 'an age equal to the timeout is not past it'
        assert rig.executor.stalled is False
        assert stall_errors(caplog) == []

        rig.clock.advance(1)
        assert rig.executor.check_stall() is True
        assert rig.executor.stalled is True
        [alarm] = stall_errors(caplog)
        message = alarm.getMessage()
        assert HANDLE in message
        assert 'request_id=r1' in message
        assert 'attempt_no=1' in message
        assert '301s' in message

        rig.clock.advance(100)
        assert rig.executor.check_stall() is True
        assert len(stall_errors(caplog)) == 1, 'the alarm repeated before another timeout had passed'

        rig.clock.advance(200)
        assert rig.executor.check_stall() is True
        assert len(stall_errors(caplog)) == 2, 'the alarm did not repeat once another timeout had passed'

        answer(stuck, DeliveryVerdict.FINAL)
        await settle()
        assert rig.executor.stalled is False, 'the alarm did not clear when the stuck report was acknowledged'
        assert any('no longer stalled' in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_an_empty_buffer_is_never_stalled():
    async with Rig(FakeRunner(succeeded())) as rig:
        rig.clock.advance(10_000)
        assert rig.executor.check_stall() is False
        assert rig.executor.stalled is False


# ---------------------------------------------------------------------------------------------
# Expiry at dequeue (R4 and the ready-by check)
# ---------------------------------------------------------------------------------------------


def assert_shaped_as_a_no_call_report(attempt: AttemptDelivery) -> None:
    """store.proto DELIVERY_OUTCOME_EXPIRED and EXHAUSTED: nothing of an attempt, whatever the feed says.

    No vendor_stats, no payload, no served_range, no as_of, empty quota_key, empty vendor_asset_id, error_class
    UNSPECIFIED and last_attempt false.
    """
    assert attempt.trailer.vendor_stats is None
    assert attempt.trailer.served_range is None
    assert attempt.trailer.as_of is None
    assert attempt.trailer.error_class is None
    assert attempt.trailer.last_attempt is False
    assert attempt.header.quota_key == ''
    assert attempt.header.vendor_asset_id == ''
    assert attempt.parts == []


@pytest.mark.asyncio
@pytest.mark.parametrize('attempt_count', [0, 2])
@pytest.mark.parametrize('tape', [Feed.IEX, Feed.SIP], ids=['iex', 'sip'])
async def test_a_request_popped_past_its_ready_by_is_reported_expired_with_no_vendor_call(attempt_count, tape):
    """FLIPPED by tj-7zjov1.66: it pinned the header feed as None, against the contract.

    store.proto DeliveryHeader.feed: an EXPIRED report made no vendor call and carries the request's kind rule
    unchanged, so a bars request that expired still names the resolved tape it carried.
    """
    runner = FakeRunner(succeeded())
    async with Rig(runner) as rig:
        await rig.submit('r1', ready_by=T0 - timedelta(seconds=1), attempt_count=attempt_count, feed=tape)
        delivered = await rig.store.next()
        answer(delivered, DeliveryVerdict.FINAL)
        await settle()

    attempt = delivered.attempt
    assert runner.calls == [], 'an expired request reached the vendor'
    assert attempt.header.attempt_no == attempt_count + 1
    assert attempt.header.request_id == 'r1'
    assert attempt.header.instance_id == INSTANCE
    assert attempt.trailer.outcome is DeliveryOutcome.EXPIRED
    assert attempt.header.feed is tape, 'an expired bars request lost the tape it was resolved to'
    assert_a_bars_report_names_the_tape(attempt.header, tape)
    # No vendor call, so nothing of an attempt: no stats, no quota key, no payload.
    assert_shaped_as_a_no_call_report(attempt)
    assert rig.finished == ['r1']


@pytest.mark.asyncio
async def test_an_expired_request_of_a_kind_with_no_tape_reports_no_feed():
    """store.proto DeliveryHeader.feed: an ASSET_LIST or CORPORATE_ACTIONS report is always FEED_UNSPECIFIED."""
    runner = FakeRunner(succeeded())
    async with Rig(runner) as rig:
        await rig.submit('r1', ready_by=T0 - timedelta(seconds=1))
        delivered = await rig.store.next()
        answer(delivered, DeliveryVerdict.FINAL)
        await settle()

    assert delivered.attempt.trailer.outcome is DeliveryOutcome.EXPIRED
    assert delivered.attempt.header.feed is None
    assert_a_no_tape_report_is_unspecified(delivered.attempt.header)
    assert_shaped_as_a_no_call_report(delivered.attempt)


@pytest.mark.asyncio
@pytest.mark.parametrize('tape', [Feed.IEX, Feed.SIP], ids=['iex', 'sip'])
async def test_a_retry_re_entering_the_queue_keeps_its_feed_and_its_expiry_names_it(tape):
    """The retry is the request rebuilt by dataclasses.replace with the store's count; the tape rides along.

    The first attempt fails transiently, the store answers CONTINUE, and the retry is popped past its ready_by,
    so the EXPIRED report is built from the re-entered request, not the one submitted.
    """
    runner = FakeRunner(failed(DeliveryErrorClass.TRANSIENT))
    async with Rig(runner) as rig:
        await rig.submit('r1', ready_by=T0 + timedelta(seconds=10), feed=tape)
        first = await rig.store.next()
        rig.clock.advance(20)
        answer(first, DeliveryVerdict.CONTINUE)
        second = await rig.store.next()
        answer(second, DeliveryVerdict.FINAL)
        await settle()

    assert runner.calls == [('r1', 1)]
    assert second.attempt.trailer.outcome is DeliveryOutcome.EXPIRED
    assert second.attempt.header.attempt_no == 2
    assert second.attempt.header.feed is tape, 'the retry lost the tape resolved at intake'
    assert_a_bars_report_names_the_tape(second.attempt.header, tape)
    assert_shaped_as_a_no_call_report(second.attempt)


@pytest.mark.asyncio
@pytest.mark.parametrize('tape', [Feed.IEX, Feed.SIP], ids=['iex', 'sip'])
@pytest.mark.parametrize('named', [False, True], ids=['default-feed', 'named-feed'])
async def test_a_bars_request_expiring_after_submit_names_the_feed_of_its_ack(tape, named):
    """Through BrokerHandle.submit: the EXPIRED report names the same tape as the Submit ack.

    The tape is the deployment's (resolve_feed), whether the request named it or left it to the default.
    """
    runner = FakeRunner(succeeded())
    async with Rig(runner) as rig:
        handle = BrokerHandle(
            source=DataSource.ALPACA_API,
            scope=SCOPE,
            scheduler=rig.scheduler,
            executor=rig.executor,
            reader=Mock(),
            resolve_feed=lambda: tape,
        )
        bars = bars_request(feed=tape) if named else bars_request()
        ack = await handle.submit(submit_request('r1', bars=bars, ready_by=T0 - timedelta(seconds=1)))
        delivered = await rig.store.next()
        answer(delivered, DeliveryVerdict.FINAL)
        await settle()

    assert ack.accepted.feed is tape
    assert runner.calls == []
    assert delivered.attempt.trailer.outcome is DeliveryOutcome.EXPIRED
    assert delivered.attempt.header.feed is ack.accepted.feed, 'the expired report and the ack name different tapes'
    assert_a_bars_report_names_the_tape(delivered.attempt.header, tape)
    assert_shaped_as_a_no_call_report(delivered.attempt)


@pytest.mark.asyncio
async def test_a_null_ready_by_never_expires():
    runner = FakeRunner(succeeded())
    async with Rig(runner) as rig:
        rig.clock.advance(100 * 365 * 24 * 3600)
        await rig.submit('r1', ready_by=None)
        delivered = await rig.store.next()
        answer(delivered, DeliveryVerdict.FINAL)

    assert runner.calls == [('r1', 1)]
    assert delivered.attempt.trailer.outcome is DeliveryOutcome.SUCCEEDED


@pytest.mark.asyncio
async def test_a_retry_is_checked_again_at_its_next_pop():
    runner = FakeRunner(failed(DeliveryErrorClass.TRANSIENT))
    async with Rig(runner) as rig:
        await rig.submit('r1', ready_by=T0 + timedelta(seconds=10))
        first = await rig.store.next()
        rig.clock.advance(20)
        answer(first, DeliveryVerdict.CONTINUE)
        second = await rig.store.next()
        answer(second, DeliveryVerdict.FINAL)
        await settle()

    assert runner.calls == [('r1', 1)], 'a retry past its ready_by reached the vendor'
    assert first.attempt.trailer.outcome is DeliveryOutcome.FAILED
    assert second.attempt.trailer.outcome is DeliveryOutcome.EXPIRED
    assert numbers(first, second) == [1, 2]


# ---------------------------------------------------------------------------------------------
# Redelivery: the same attempt, with bounded backoff, and no vendor call
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_delivery_is_redelivered_unchanged_with_bounded_backoff_and_no_vendor_call():
    runner = FakeRunner(succeeded())
    async with Rig(runner, settings(redelivery_base_s=1.0, redelivery_max_s=1.5)) as rig:
        await rig.submit('r1')
        fail_delivery(await rig.store.next())
        fail_delivery(await rig.store.next())
        fail_delivery(await rig.store.next())
        assert rig.executor.unacknowledged == 1, 'the report left the buffer before it was acknowledged'
        answer(await rig.store.next(), DeliveryVerdict.FINAL)
        await settle()

    first, *again = rig.store.received
    assert len(again) == 3
    assert all(item is first for item in again), 'a redelivery is not the same AttemptDelivery'
    assert {(item.header.request_id, item.header.attempt_no) for item in rig.store.received} == {('r1', 1)}
    assert runner.calls == [('r1', 1)], 'a redelivery made a vendor call'
    assert_attempt_numbers_never_reused(rig.store.received)
    # Doubling from 1 s, capped at 1.5 s.
    assert rig.sleep.slept == [1.0, 1.5, 1.5]
    assert rig.finished == ['r1']
    assert rig.executor.unacknowledged == 0


# ---------------------------------------------------------------------------------------------
# A runner that raises
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_runner_exception_is_reported_failed_permanent_and_the_worker_survives():
    runner = FakeRunner(RuntimeError('a bug in the runner'), succeeded())
    async with Rig(runner) as rig:
        await rig.submit('r1')
        await rig.submit('r2')
        bug = await rig.store.next()
        answer(bug, DeliveryVerdict.CONTINUE)
        after = await rig.store.next()
        answer(after, DeliveryVerdict.FINAL)
        await settle()

    assert bug.attempt.header.request_id == 'r1'
    assert bug.attempt.trailer.outcome is DeliveryOutcome.FAILED
    assert bug.attempt.trailer.error_class is DeliveryErrorClass.PERMANENT
    assert bug.attempt.header.quota_key == SCOPE
    assert 'a bug in the runner' not in bug.attempt.trailer.detail
    # The worker lived on to run the next request; the permanent failure was not retried.
    assert after.attempt.header.request_id == 'r2'
    assert after.attempt.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert runner.calls == [('r1', 1), ('r2', 1)]


@pytest.mark.asyncio
async def test_start_twice_is_refused():
    async with Rig(FakeRunner(succeeded())) as rig:
        with pytest.raises(RuntimeError, match='already running'):
            rig.executor.start(rig.store, INSTANCE)


# ---------------------------------------------------------------------------------------------
# Submit: the Alpaca handle and the Submit handler
# ---------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def iex_deployment_and_no_cached_client(monkeypatch):
    monkeypatch.delenv('ALPACA_SIP_ENABLED', raising=False)
    monkeypatch.delenv('ALPACA_RATE_LIMIT_PER_MINUTE', raising=False)
    broker_api.set_client(None)
    yield
    broker_api.set_client(None)


def bars_request(**overrides) -> BarsRequest:
    fields = {
        'source': DataSource.ALPACA_API,
        'asset_symbol': 'VFV',
        'asset_type': AssetType.STOCK,
        'data_types': [DataType.MARKET_ACTIVITY],
        'granularity': Granularity.THIRTY_MINUTES,
        'start': START,
        'end': END,
        'update_type': UpdateType.STATIC,
    }
    return BarsRequest(**{**fields, **overrides})


def submit_request(request_id: str = 'req-1', **overrides) -> SubmitRequest:
    fields = {'request_id': request_id, 'owner': 'rebalancer', 'bars': bars_request()}
    return SubmitRequest(**{**fields, **overrides})


def alpaca_handle() -> AlpacaHandle:
    return AlpacaHandle(settings=settings(stall_check_interval_s=3600.0), calls_per_minute=60)


@pytest.mark.asyncio
async def test_submit_queues_the_request_and_answers_at_once_with_the_feed_and_the_scope():
    handle = alpaca_handle()
    ready_by = T0 + timedelta(days=3650)

    ack = await handle.submit(submit_request(ready_by=ready_by, attempt_count=2, client='backtest'))

    assert ack.refused is None
    assert ack.accepted.feed is Feed.IEX
    assert ack.accepted.quota_key == ALPACA_DATA_SCOPE
    assert handle.scheduler.queued == 1
    queued = await asyncio.wait_for(handle.scheduler.take(), GUARD_S)
    assert queued.request_id == 'req-1'
    assert queued.quota_scope == ALPACA_DATA_SCOPE
    assert queued.ready_by == ready_by
    assert queued.attempt_count == 2
    assert queued.client_label == 'backtest'
    assert queued.feed is Feed.IEX, 'the request does not carry the tape its ack named'
    assert queued.payload == BarsWork(
        request=bars_request(), priority=priority_for_update_type(UpdateType.STATIC), feed=Feed.IEX
    )


@pytest.mark.asyncio
async def test_submit_is_idempotent_on_request_id_while_the_request_is_active():
    handle = alpaca_handle()

    first = await handle.submit(submit_request('req-1'))
    again = await handle.submit(submit_request('req-1'))

    assert again == first
    assert handle.scheduler.queued == 1, 'a re-sent request was queued twice'
    assert handle.active == 1

    # Once the executor is done with it, the same id is a new request.
    handle.finished(ScheduledRequest(request_id='req-1', quota_scope=ALPACA_DATA_SCOPE, payload=None))
    assert handle.active == 0
    await handle.submit(submit_request('req-1'))
    assert handle.scheduler.queued == 2


@pytest.mark.asyncio
async def test_a_feed_the_deployment_cannot_serve_is_refused_and_not_queued():
    handle = alpaca_handle()

    ack = await handle.submit(submit_request(bars=bars_request(feed=Feed.SIP)))

    assert ack.accepted is None
    assert ack.refused.reason == Reason.FEED_NOT_AVAILABLE.value
    assert ack.refused.metadata == {'feed': Feed.SIP.value}
    assert handle.scheduler.queued == 0
    assert handle.active == 0


@pytest.mark.asyncio
async def test_a_named_feed_the_deployment_serves_is_accepted():
    handle = alpaca_handle()

    ack = await handle.submit(submit_request(bars=bars_request(feed=Feed.IEX)))

    assert ack.accepted.feed is Feed.IEX
    assert handle.scheduler.queued == 1


NON_BARS = [
    pytest.param(
        {'bars': None, 'asset_list': AssetListRequest(source=DataSource.ALPACA_API, asset_type=AssetType.STOCK)},
        id='asset-list',
    ),
    pytest.param(
        {
            'bars': None,
            'corporate_actions': CorporateActionsRequest(
                source=DataSource.ALPACA_API, asset_symbol='VFV', start=START, end=END
            ),
        },
        id='corporate-actions',
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', NON_BARS)
async def test_the_submit_handler_refuses_a_kind_not_served_yet_as_invalid_request(kind):
    handle = alpaca_handle()
    handler = IngestSubmitHandler({DataSource.ALPACA_API: handle}, INSTANCE)

    with pytest.raises(InvalidRequestError) as raised:
        await handler.submit(submit_request(**kind))

    assert raised.value.reason is Reason.INVALID_REQUEST
    assert handle.scheduler.queued == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', NON_BARS)
async def test_the_handle_itself_refuses_a_kind_not_served_yet(kind):
    handle = alpaca_handle()

    with pytest.raises(InvalidRequestError) as raised:
        await handle.submit(submit_request(**kind))

    assert raised.value.reason is Reason.INVALID_REQUEST
    assert handle.scheduler.queued == 0


@pytest.mark.asyncio
async def test_the_submit_handler_routes_by_source_and_answers_with_its_instance_id():
    handle = alpaca_handle()
    handler = IngestSubmitHandler({DataSource.ALPACA_API: handle}, INSTANCE)

    response = await handler.submit(submit_request())

    assert response.instance_id == INSTANCE
    assert response.ack.accepted.quota_key == ALPACA_DATA_SCOPE
    assert handle.scheduler.queued == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('overrides', 'reason'),
    [
        pytest.param({'data_types': [DataType.QUOTE]}, Reason.UNSUPPORTED_ASSET_TYPE, id='quote'),
        pytest.param({'data_types': []}, Reason.INVALID_REQUEST, id='empty-data-types'),
        pytest.param({'asset_type': AssetType.CRYPTO}, Reason.UNSUPPORTED_ASSET_TYPE, id='crypto'),
    ],
)
async def test_the_submit_handler_refuses_an_unservable_bars_request_before_queueing_it(overrides, reason):
    handle = alpaca_handle()
    handler = IngestSubmitHandler({DataSource.ALPACA_API: handle}, INSTANCE)

    with pytest.raises(InvalidRequestError) as raised:
        await handler.submit(submit_request(bars=bars_request(**overrides)))

    assert raised.value.reason is reason
    assert handle.scheduler.queued == 0


@pytest.mark.asyncio
async def test_a_source_with_no_handle_is_not_implemented():
    handler = IngestSubmitHandler({}, INSTANCE)

    with pytest.raises(NotImplementedError):
        await handler.submit(submit_request())


# ---------------------------------------------------------------------------------------------
# Pacing: the Alpaca handle's scheduler, and nothing else
# ---------------------------------------------------------------------------------------------


class StubBarSet:
    def __init__(self, symbol: str, bars: list[SimpleNamespace]) -> None:
        self.data = {symbol: bars}

    def __getitem__(self, symbol: str) -> list[SimpleNamespace]:
        return self.data[symbol]


def stub_client(*timestamps: datetime) -> Mock:
    client = Mock()
    client.get_stock_bars.return_value = StubBarSet(
        'VFV',
        [
            SimpleNamespace(open=1.0, high=2.0, low=0.5, close=10.0, volume=10, trade_count=3, vwap=1.5, timestamp=t)
            for t in timestamps
        ],
    )
    return client


def api_error(status: int, headers: dict[str, str] | None = None) -> APIError:
    response = requests.Response()
    response.status_code = status
    response._content = b'{"message": "no"}'
    response.encoding = 'utf-8'
    response.headers.update(headers or {})
    try:
        response.raise_for_status()
    except requests.HTTPError as http_error:
        return APIError(response.text, http_error)
    raise AssertionError(f'{status} is not an error status')


def bars_query(**overrides) -> BarsQuery:
    fields = {
        'instrument': Instrument('VFV', AssetType.STOCK),
        'granularity': Granularity.THIRTY_MINUTES,
        'start': START,
        'end': END,
        'priority': RequestPriority.INTERACTIVE,
    }
    return BarsQuery(**{**fields, **overrides})


def test_the_scope_name_starts_with_the_vendors_data_source_value():
    """ADR tj-fa1rpu D8 via the P10 gate note: the metadata key vendor carries the scope and must name the vendor."""
    assert ALPACA_DATA_SCOPE.startswith(DataSource.ALPACA_API.value)
    assert DataSource.ALPACA_API.value != ALPACA_DATA_SCOPE


@pytest.mark.asyncio
async def test_get_bars_acquires_one_call_from_the_handles_scheduler_scope():
    handle = alpaca_handle()
    broker_api.set_client(stub_client(START))
    deadline = datetime.now(UTC) + timedelta(minutes=5)

    with patch.object(handle.scheduler, 'acquire', wraps=handle.scheduler.acquire) as acquire:
        response = await handle.reader.get_bars(bars_query(priority=RequestPriority.BACKFILL, deadline=deadline))
        assert [bar async for bar in response.bars]

    acquire.assert_awaited_once_with(ALPACA_DATA_SCOPE, RequestPriority.BACKFILL, deadline=deadline)


@pytest.mark.asyncio
async def test_a_deadline_the_handles_scope_cannot_meet_is_refused_naming_the_vendor():
    handle = alpaca_handle()  # 60 a minute: one token, refilled in a second
    client = stub_client(START)
    broker_api.set_client(client)
    await handle.scheduler.acquire(ALPACA_DATA_SCOPE)

    outcome = await handle.reader.get_bars(bars_query(deadline=datetime.now(UTC) + timedelta(milliseconds=100)))

    assert outcome.error.reason is Reason.RATE_BUDGET
    assert outcome.error.metadata['vendor'] == ALPACA_DATA_SCOPE
    assert outcome.error.metadata['vendor'].startswith(DataSource.ALPACA_API.value)
    assert client.mock_calls == []


def test_alpaca_has_no_rate_budget_of_its_own():
    assert not hasattr(broker_api, 'get_rate_budget')
    assert not hasattr(broker_api, '__RATE_BUDGET')


@pytest.mark.asyncio
async def test_the_retired_rate_variables_are_not_read(monkeypatch):
    """ALPACA_RATE_LIMIT_PER_SEC, ALPACA_RATE_BURST and ALPACA_BACKFILL_RESERVE pace nothing now.

    Set to values that would refuse the second call at once, they change nothing: three calls, each with a
    deadline of now, all reach the vendor through the handle's own scope at its ceiling.
    """
    monkeypatch.setenv('ALPACA_RATE_LIMIT_PER_SEC', '0.0001')
    monkeypatch.setenv('ALPACA_RATE_BURST', '1')
    monkeypatch.setenv('ALPACA_BACKFILL_RESERVE', '1000')
    client = stub_client(START)
    broker_api.set_client(client)

    with patch.object(RateBudget, 'from_env', side_effect=AssertionError('RateBudget.from_env was called')):
        handle = AlpacaHandle(settings=settings())
        for _ in range(3):
            outcome = await handle.reader.get_bars(bars_query(deadline=datetime.now(UTC) + timedelta(seconds=0.05)))
            assert [bar async for bar in outcome.bars]

    assert client.get_stock_bars.call_count == 3


def ceiling_refill_seconds(count: int) -> float:
    limit = Limit(timedelta(minutes=1), count)
    return limit.burst / limit.refill_per_sec


def test_alpaca_rate_limit_per_minute_sets_the_scope_and_defaults_to_the_ceiling(monkeypatch):
    assert AlpacaHandle(settings=settings()).scheduler.window(ALPACA_DATA_SCOPE).full_refill_seconds == (
        pytest.approx(ceiling_refill_seconds(ALPACA_CALLS_PER_MINUTE_CEILING))
    )
    monkeypatch.setenv('ALPACA_RATE_LIMIT_PER_MINUTE', '60')
    assert AlpacaHandle(settings=settings()).scheduler.window(ALPACA_DATA_SCOPE).full_refill_seconds == 1.0


def test_alpaca_rate_limit_per_minute_may_not_exceed_the_ceiling(monkeypatch):
    monkeypatch.setenv('ALPACA_RATE_LIMIT_PER_MINUTE', str(ALPACA_CALLS_PER_MINUTE_CEILING + 1))
    with pytest.raises(SchedulerConfigError, match='ceiling'):
        AlpacaHandle(settings=settings())


@pytest.mark.asyncio
async def test_a_header_less_429_waits_the_handles_declared_window_of_sixty_seconds():
    """Finding (b), ruled by the architect and built in 18ff3f5: no header gives now plus the declared window.

    The declared window is the scope's longest limit interval, a minute for Alpaca data, NOT the bucket's refill
    (one second at 60 a minute), which re-429ed inside the same vendor window.
    """
    handle = alpaca_handle()  # 60 a minute: a refill of one second, a declared window of sixty
    client = Mock()
    client.get_stock_bars.side_effect = api_error(429)
    broker_api.set_client(client)

    before = datetime.now(UTC)
    outcome = await handle.reader.get_bars(bars_query())
    after = datetime.now(UTC)

    assert outcome.error.reason is Reason.VENDOR_RATE_LIMITED
    assert before + timedelta(seconds=60) <= outcome.error.reset_at <= after + timedelta(seconds=60)


@pytest.mark.parametrize('calls_per_minute', [60, ALPACA_CALLS_PER_MINUTE_CEILING])
def test_the_alpaca_data_scope_declares_a_sixty_second_window_whatever_its_count(calls_per_minute):
    window = AlpacaHandle(settings=settings(), calls_per_minute=calls_per_minute).scheduler.window(ALPACA_DATA_SCOPE)

    assert window.declared_window_seconds == 60.0
    assert window.full_refill_seconds < 60.0, 'the refill and the declared window must differ for this to pin'


RATE_LIMIT_RESET = RATE_LIMIT_RESET_HEADER
RETRY_AFTER = RETRY_AFTER_HEADER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('headers', 'expected_s'),
    [
        pytest.param({RATE_LIMIT_RESET: 'RESET+42', RETRY_AFTER: '7'}, 42, id='x-ratelimit-reset-wins'),
        pytest.param({RETRY_AFTER: '7'}, 7, id='retry-after-beats-the-fallback'),
        pytest.param({}, 60, id='the-declared-window-last'),
    ],
)
async def test_the_reset_is_x_ratelimit_reset_else_retry_after_else_the_declared_window(headers, expected_s):
    """The header order is unchanged by 18ff3f5; only the last source moved, to the declared window."""
    now = datetime.now(UTC).replace(microsecond=0)
    reset = now + timedelta(seconds=42)
    headers = {name: str(reset.timestamp()) if value == 'RESET+42' else value for name, value in headers.items()}
    client = Mock()
    client.get_stock_bars.side_effect = api_error(429, headers)

    outcome = await alpaca_reader(client, clock=lambda: now, scheduler=RecordingScheduler()).get_bars(bars_query())

    assert outcome.error.reason is Reason.VENDOR_RATE_LIMITED
    assert outcome.error.reset_at == now + timedelta(seconds=expected_s)


@pytest.mark.asyncio
async def test_a_scope_with_two_intervals_falls_back_to_the_longer():
    minute, ten_seconds = timedelta(seconds=60), timedelta(seconds=10)
    scheduler = (
        SchedulerBuilder()
        .declare_scope(ALPACA_DATA_SCOPE, [Limit(ten_seconds, 50), Limit(minute, 200)])
        .set_limit(ten_seconds, 50, scope=ALPACA_DATA_SCOPE)
        .set_limit(minute, 200, scope=ALPACA_DATA_SCOPE)
        .cost_function(lambda _request, _page_size: 1, page_size=1)
        .credential_provider(StaticCredentialProvider(Credential(key_id='key', secret='secret')))
        .on_expired(lambda _expired: asyncio.sleep(0))
        .build()
    )
    assert scheduler.window(ALPACA_DATA_SCOPE).declared_window_seconds == 60.0

    now = datetime.now(UTC)
    client = Mock()
    client.get_stock_bars.side_effect = api_error(429)
    outcome = await alpaca_reader(client, clock=lambda: now, scheduler=scheduler).get_bars(bars_query())

    assert outcome.error.reset_at == now + timedelta(seconds=60)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('max_reset_wait_s', 'expected_wait_s'),
    [pytest.param(600.0, 60.0, id='the-declared-window'), pytest.param(30.0, 30.0, id='capped-by-max-reset-wait')],
)
async def test_a_header_less_429_retries_after_the_declared_window_capped_by_max_reset_wait(
    max_reset_wait_s, expected_wait_s
):
    """End to end through the executor: the bars runner's 429, its reset, and RetryPolicy.reset_wait's cap."""
    client = Mock()
    client.get_stock_bars.side_effect = api_error(429)
    retry = RetryPolicy(max_attempts=5, max_reset_wait_s=max_reset_wait_s)
    placeholder = FakeRunner(succeeded())
    rig = Rig(placeholder, settings(retry=retry))
    # The runner reads the vendor on the rig's own clock, so the reset and the wait are measured on one clock.
    runner = BarsAttemptRunner(alpaca_reader(client, clock=rig.clock, scheduler=RecordingScheduler()))
    rig.executor = AckGatedExecutor(
        name=HANDLE,
        scheduler=rig.scheduler,
        run_attempt=runner,
        settings=settings(retry=retry),
        on_finished=lambda request: rig.finished.append(request.request_id),
        clock=rig.clock,
        sleep=rig.sleep,
    )
    work = BarsWork(request=bars_request(), priority=RequestPriority.BACKFILL, feed=Feed.IEX)
    async with rig:
        await rig.submit('r1', payload=work)
        first = await rig.store.next()
        assert first.attempt.trailer.error_class is DeliveryErrorClass.RATE_LIMITED
        answer(first, DeliveryVerdict.CONTINUE)
        answer(await rig.store.next(), DeliveryVerdict.FINAL)

    assert rig.sleep.slept == [expected_wait_s]
    assert retry.reset_wait(T0 + timedelta(seconds=60), T0) == expected_wait_s


def test_the_executor_settings_come_from_the_environment_with_defaults(monkeypatch):
    for name in ('ALPACA_OUTGOING_BUFFER_SIZE', 'ALPACA_STALL_TIMEOUT_S', 'ALPACA_MAX_ATTEMPTS'):
        monkeypatch.delenv(name, raising=False)
    defaults = executor_settings_from_env()
    assert (defaults.buffer_size, defaults.stall_timeout_s, defaults.retry.max_attempts) == (64, 300.0, 5)

    monkeypatch.setenv('ALPACA_OUTGOING_BUFFER_SIZE', '7')
    monkeypatch.setenv('ALPACA_STALL_TIMEOUT_S', '12.5')
    monkeypatch.setenv('ALPACA_MAX_ATTEMPTS', '2')
    configured = executor_settings_from_env()
    assert (configured.buffer_size, configured.stall_timeout_s, configured.retry.max_attempts) == (7, 12.5, 2)
    assert AlpacaHandle().executor.settings == configured


def test_the_bars_cost_estimate_counts_every_period_in_pages_of_the_page_size():
    def request(granularity: Granularity, start: datetime, end: datetime) -> ScheduledRequest:
        work = BarsWork(
            request=bars_request(granularity=granularity, start=start, end=end),
            priority=RequestPriority.BACKFILL,
            feed=Feed.IEX,
        )
        return ScheduledRequest(request_id='r', quota_scope=ALPACA_DATA_SCOPE, payload=work)

    assert ALPACA_BARS_PAGE_SIZE == 10_000
    # 30 days of minutes is 43,200 periods: five pages of 10,000.
    month = request(Granularity.ONE_MINUTE, START, START + timedelta(days=30))
    assert estimate_bars_calls(month, ALPACA_BARS_PAGE_SIZE) == 5
    # Anything at all is at least one call.
    assert estimate_bars_calls(request(Granularity.ONE_DAY, START, START), ALPACA_BARS_PAGE_SIZE) == 1


# ---------------------------------------------------------------------------------------------
# The bars runner: CallTally in vendor_stats, and the error class from the reason
# ---------------------------------------------------------------------------------------------


def bars_scheduled(**overrides) -> ScheduledRequest:
    fields = {
        'request_id': 'r1',
        'quota_scope': ALPACA_DATA_SCOPE,
        'payload': BarsWork(request=bars_request(), priority=RequestPriority.BACKFILL, feed=Feed.IEX),
    }
    return ScheduledRequest(**{**fields, **overrides})


@pytest.mark.asyncio
async def test_a_served_attempt_reports_its_vendor_call_count_and_times():
    pacing = RecordingScheduler()
    runner = BarsAttemptRunner(alpaca_reader(stub_client(START, START + timedelta(minutes=30)), scheduler=pacing))

    before = datetime.now(UTC)
    result = await runner(bars_scheduled(), 1)
    after = datetime.now(UTC)

    stats = result.trailer.vendor_stats
    assert result.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert (stats.calls, stats.pages, stats.throttled_429) == (1, 1, 0)
    assert before <= stats.first_call_at == stats.last_call_at <= after
    assert len(pacing.acquires) == 1
    assert result.quota_key == ALPACA_DATA_SCOPE
    assert result.feed is Feed.IEX
    [page] = result.parts
    assert isinstance(page, BarPage)
    assert [bar.bar_start for bar in page.bars] == [START, START + timedelta(minutes=30)]
    assert {bar.feed for bar in page.bars} == {Feed.IEX}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('raised', 'klass', 'calls', 'throttled'),
    [
        pytest.param(api_error(500), DeliveryErrorClass.TRANSIENT, 1, 0, id='500'),
        pytest.param(api_error(403), DeliveryErrorClass.TRANSIENT, 1, 0, id='403-vendor-auth-is-not-ready'),
        pytest.param(api_error(404), DeliveryErrorClass.PERMANENT, 1, 0, id='404'),
        pytest.param(api_error(429), DeliveryErrorClass.RATE_LIMITED, 1, 1, id='429'),
    ],
)
async def test_a_failed_attempt_reports_its_class_and_what_it_spent(raised, klass, calls, throttled):
    client = Mock()
    client.get_stock_bars.side_effect = raised
    runner = BarsAttemptRunner(alpaca_reader(client, scheduler=RecordingScheduler()))

    result = await runner(bars_scheduled(), 1)

    assert result.trailer.outcome is DeliveryOutcome.FAILED
    assert result.trailer.error_class is klass
    assert result.trailer.vendor_stats.calls == calls
    assert result.trailer.vendor_stats.throttled_429 == throttled
    assert result.feed is Feed.IEX
    assert result.parts == []
    if klass is DeliveryErrorClass.RATE_LIMITED:
        assert result.reset_at is not None
    else:
        assert result.reset_at is None


@pytest.mark.asyncio
async def test_a_refusal_before_the_vendor_spends_nothing_and_is_permanent():
    pacing = RecordingScheduler()
    future = datetime.now(UTC) + timedelta(days=30)
    work = BarsWork(request=bars_request(start=future, end=None), priority=RequestPriority.BACKFILL, feed=Feed.IEX)
    runner = BarsAttemptRunner(alpaca_reader(Mock(), scheduler=pacing))

    result = await runner(bars_scheduled(payload=work), 1)

    assert result.trailer.error_class is DeliveryErrorClass.PERMANENT
    assert result.trailer.vendor_stats.calls == 0
    assert result.trailer.vendor_stats.first_call_at is None
    assert pacing.acquires == []


def test_the_error_class_is_read_from_the_reasons_outcome():
    assert error_class(InvalidRequestError(Reason.RANGE_IN_FUTURE, 'later')) is DeliveryErrorClass.PERMANENT
    assert error_class(ExogenousError(Reason.VENDOR_UNAVAILABLE, 'down')) is DeliveryErrorClass.TRANSIENT
    rate_limited = ExogenousError(Reason.VENDOR_RATE_LIMITED, 'slow down', reset_at=datetime.now(UTC))
    assert error_class(rate_limited) is DeliveryErrorClass.RATE_LIMITED


# ---------------------------------------------------------------------------------------------
# End to end through the production AlpacaHandle
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_alpaca_handle_attempts_a_submitted_request_and_reports_it_to_the_store():
    broker_api.set_client(stub_client(START))
    handle = alpaca_handle()
    store = DoubleStore()
    handle.start(store, INSTANCE)
    try:
        await handle.submit(submit_request('req-1'))
        delivered = await store.next()
        answer(delivered, DeliveryVerdict.FINAL)
        await settle()
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)

    attempt = delivered.attempt
    assert (attempt.header.request_id, attempt.header.attempt_no, attempt.header.instance_id) == ('req-1', 1, INSTANCE)
    assert attempt.header.quota_key == ALPACA_DATA_SCOPE
    assert attempt.header.feed is Feed.IEX
    assert attempt.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert attempt.trailer.vendor_stats.calls == 1
    # FINAL finishes the request, so the handle forgets it and the id may be submitted afresh.
    assert handle.active == 0
