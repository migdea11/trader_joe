"""The P14 scenarios on the fakes: queue, ack gate, redelivery, recovered charge, expiry, buffer, restarts.

Bead tj-7zjov1.24. Design: ADR tj-26jt9w addendum 2 (the FIFO queue, null ready-by last), addendum 4 (every attempt
reported and acknowledged before any retry; a failed delivery redelivered unchanged; the bounded buffer and its
alarm, ruling R2), addendum 5 (the restart: a new instance id; every request the old instance held re-submitted,
flagged recovered, with its durable count, and charged its estimate before it is queued), the owner's expiry ruling
of 2026-10-24 (checked at dequeue only: expired at dequeue is never sent and goes out failed with cause expired; a
null ready-by never expires; a retry is checked again at its next dequeue), and the architect's P10 gate note on this
bead (the recovered charge is booked at one instant before the request is queued, never clamped, and a charge above
the window leaves the budget in debt so the first new call waits until the refill pays it off: assert the delay).

HOW IT IS DRIVEN. Everything runs on the fakes of tests/fakes: FakeIngest (the production BrokerHandle, Scheduler,
AckGatedExecutor and attempt runners around a FakeRead paced on that scheduler), FakeStore (the store's verdicts and
its request side, in memory) and, for the store restart, FakeStoreServer (FakeStore behind the production
StoreServicer over loopback gRPC, dialled by the production GrpcStoreDeliveryClient). No vendor key, no network beyond
loopback. Clocks are fakes: the wall clock is moved by hand; the rate budgets' monotonic clock is moved by the
scheduler's patched sleep, which records every wait; the executor's sleep returns at once except for the stall
alarm's interval and the retry backoff, which it parks, so a retry runs only when a test lets it.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.rpc.channel import create_channel
from common.rpc.clients.store_delivery import GrpcStoreDeliveryClient
from data.ingest.app.brokers import rate_budget
from data.ingest.app.brokers.alpaca.handle import ALPACA_TRADING_SCOPE
from data.ingest.app.scheduler import ExecutorSettings, RetryPolicy
from data.ingest.app.scheduler import scheduler as scheduler_module
from schemas.data_ingest.delivery import DeliveryErrorClass, DeliveryOutcome, DeliveryVerdict
from schemas.data_ingest.submit import AssetListRequest, AssetListStatus, BarsRequest, SubmitRequest
from tests.fakes.ingest import FakeIngest
from tests.fakes.market_data import FAILONCE_PREFIX, NOTFOUND_PREFIX, REUSED_PREFIX, WORLD, WORLD_DAY_0
from tests.fakes.store import FakeStore, FakeStoreServer


pytestmark = pytest.mark.data_ingest

# Noon on the world's first day: every request below starts well before it.
T0 = datetime(WORLD_DAY_0.year, WORLD_DAY_0.month, WORLD_DAY_0.day, 12, tzinfo=UTC)
START = datetime(2026, 1, 5, tzinfo=UTC)
MAX_ATTEMPTS = 3
# The executor's sleeps the fake parks: the stall alarm's interval (so a test drives check_stall itself) and the
# retry backoff after a failed first attempt (so a retry waits until the test releases it).
WATCH_S = 7777.0
RETRY_S = 4444.0
STALL_TIMEOUT_S = 300.0
# Every wait for the code under test is bounded by this, so a regression fails rather than hangs.
GUARD_S = 10.0


# ---------------------------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------------------------


class Wall:
    """The wall clock every part reads: an aware instant the test moves by hand."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class Monotonic:
    """The rate budgets' refill clock, moved only by the scheduler's patched sleep."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class BudgetSleeps:
    """Stands in for asyncio.sleep in the scheduler's modules: records each wait and advances the refill clock by it.

    The clock moves a hair past the wait, never short of it (as test_scheduler.py's Sleeps), so a float rounding in the
    refill cannot leave a token at 0.9999999 and spin.
    """

    def __init__(self, monotonic: Monotonic) -> None:
        self.monotonic = monotonic
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.monotonic.now += seconds * (1 + 1e-9) + 1e-9
        await asyncio.sleep(0)


class ExecutorSleep:
    """The executor's sleep: parks the alarm interval and the retry backoff until released; the rest return at once."""

    def __init__(self) -> None:
        self.slept: list[float] = []
        self.__retries = asyncio.Event()

    def release_retries(self) -> None:
        self.__retries.set()

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        if seconds == WATCH_S:
            await asyncio.Event().wait()
        if seconds == RETRY_S:
            await self.__retries.wait()
        await asyncio.sleep(0)


class Rig:
    """The clocks and the settings every scenario builds its FakeIngest with."""

    def __init__(self, monkeypatch) -> None:
        self.wall = Wall()
        self.monotonic = Monotonic()
        self.budget_sleeps = BudgetSleeps(self.monotonic)
        self.executor_sleep = ExecutorSleep()
        # Scheduler.acquire sleeps through its own module, RateBudget.acquire through rate_budget: both are replaced.
        monkeypatch.setattr(scheduler_module, 'asyncio', SimpleNamespace(sleep=self.budget_sleeps.sleep))
        monkeypatch.setattr(rate_budget, 'asyncio', SimpleNamespace(sleep=self.budget_sleeps.sleep))

    def ingest(self, store, *, buffer_size: int = 8, calls_per_minute: int = 200, page_size: int = 10_000):
        settings = ExecutorSettings(
            buffer_size=buffer_size,
            workers=1,
            stall_timeout_s=STALL_TIMEOUT_S,
            stall_check_interval_s=WATCH_S,
            redelivery_base_s=1.0,
            redelivery_max_s=2.0,
            retry=RetryPolicy(max_attempts=MAX_ATTEMPTS, base_delay_s=RETRY_S, max_delay_s=RETRY_S),
        )
        return FakeIngest(
            store,
            settings=settings,
            calls_per_minute=calls_per_minute,
            page_size=page_size,
            world=WORLD,
            clock=self.wall,
            budget_clock=self.monotonic,
            sleep=self.executor_sleep,
        )


@pytest.fixture
def rig(monkeypatch) -> Rig:
    return Rig(monkeypatch)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


async def until(predicate: Callable[[], bool], what: str, guard_s: float = GUARD_S) -> None:
    """Yield to the loop until the predicate holds, failing after guard_s of real time."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + guard_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f'timed out waiting for {what}')
        await asyncio.sleep(0.001)


async def settle(rounds: int = 300) -> None:
    """Let every task that can run, run."""
    for _ in range(rounds):
        await asyncio.sleep(0)


def bars_request(request_id: str, symbol: str = 'VFV', *, days: int = 3, **overrides) -> SubmitRequest:
    bars = BarsRequest(
        source=DataSource.ALPACA_API,
        asset_symbol=symbol,
        asset_type=AssetType.STOCK,
        data_types=[DataType.MARKET_ACTIVITY],
        granularity=Granularity.ONE_DAY,
        start=START,
        end=START + timedelta(days=days),
        update_type=UpdateType.STATIC,
    )
    return SubmitRequest(**{'request_id': request_id, 'owner': 'rebalancer', 'bars': bars, **overrides})


def bars_calls(ingest: FakeIngest) -> list[str]:
    return [symbol for read, symbol in ingest.reader.calls if read == 'bars']


def written(store: FakeStore) -> list[tuple[str, int, DeliveryOutcome, DeliveryVerdict]]:
    return [
        (
            entry.attempt.header.request_id,
            entry.attempt.header.attempt_no,
            entry.attempt.trailer.outcome,
            entry.answer.verdict,
        )
        for entry in store.written
    ]


# ---------------------------------------------------------------------------------------------
# The FIFO queue: dated requests first, null ready-by last, each in arrival order
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_vendor_sees_dated_requests_first_and_null_ready_by_last_each_in_arrival_order(rig):
    store = FakeStore()
    ingest = rig.ingest(store)
    later = T0 + timedelta(days=1)
    # The dated pair arrives LATER ready-by first: the tier is arrival order, never sorted by deadline.
    for request_id, symbol, ready_by in [
        ('u1', 'AAPL', None),
        ('d1', 'META', later + timedelta(hours=1)),
        ('u2', 'XYZ', None),
        ('d2', 'AMD', later),
    ]:
        await store.submit(ingest.submit, bars_request(request_id, symbol, ready_by=ready_by))

    async with ingest:
        await until(lambda: len(store.written) == 4, 'four reports')

    assert bars_calls(ingest) == ['META', 'AMD', 'AAPL', 'XYZ']
    assert [request_id for request_id, *_ in written(store)] == ['d1', 'd2', 'u1', 'u2']


# ---------------------------------------------------------------------------------------------
# The ack gate
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_retry_reaches_the_vendor_before_the_store_answers_the_failed_attempt(rig):
    store = FakeStore()
    ingest = rig.ingest(store)
    rig.executor_sleep.release_retries()
    symbol = f'{FAILONCE_PREFIX}VFV'
    store.hold()
    await store.submit(ingest.submit, bars_request('r1', symbol))

    async with ingest:
        await until(lambda: len(store.received) == 1, 'the first report')
        await settle()
        # The retry's backoff is released and the vendor would answer: only the held answer stands in the way.
        assert bars_calls(ingest) == [symbol]
        assert written(store) == [('r1', 1, DeliveryOutcome.FAILED, DeliveryVerdict.CONTINUE)]
        assert store.received[0].trailer.error_class is DeliveryErrorClass.TRANSIENT

        store.release()
        await until(lambda: len(store.written) == 2, 'the retry report')

    assert bars_calls(ingest) == [symbol, symbol]
    assert written(store) == [
        ('r1', 1, DeliveryOutcome.FAILED, DeliveryVerdict.CONTINUE),
        ('r1', 2, DeliveryOutcome.SUCCEEDED, DeliveryVerdict.FINAL),
    ]
    assert store.count('r1') == 2 and store.is_finished('r1')


# ---------------------------------------------------------------------------------------------
# Idempotent redelivery
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lost_answer_is_redelivered_unchanged_and_written_once_with_no_second_vendor_call(rig):
    store = FakeStore()
    ingest = rig.ingest(store)
    store.hold()
    await store.submit(ingest.submit, bars_request('r1', 'AAPL'))

    async with ingest:
        await until(lambda: len(store.received) == 1, 'the first delivery')
        # The store committed the attempt; its answer is lost on the way back.
        store.go_down()
        store.release()
        await until(lambda: store.refused_while_down >= 3, 'redeliveries to a store that is down')
        store.come_up()
        await until(lambda: ingest.executor.unacknowledged == 0, 'the acknowledgement')

    assert len(store.received) >= 2
    assert all(delivery == store.received[0] for delivery in store.received)
    assert written(store) == [('r1', 1, DeliveryOutcome.SUCCEEDED, DeliveryVerdict.FINAL)]
    assert bars_calls(ingest) == ['AAPL']
    assert ingest.finished == ('r1',)


# ---------------------------------------------------------------------------------------------
# The restarts: a new ingest instance, the recovered charge, a store restart
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_ingest_restart_mints_a_new_id_and_the_open_request_continues_its_numbering_under_it(rig):
    store = FakeStore()
    ingest = rig.ingest(store)
    symbol = f'{FAILONCE_PREFIX}AAPL'
    await store.submit(ingest.submit, bars_request('r1', symbol))
    async with ingest:
        await ingest.announce()
        # Attempt 1 fails, the store counts it and answers CONTINUE; the retry waits out its (parked) backoff.
        await until(lambda: store.count('r1') == 1, 'the first attempt counted')
        old = ingest.identity

        await ingest.restart()
        acks = await store.recover(ingest.submit)
        await until(lambda: len(store.written) == 2, 'the recovered attempt')

    new = ingest.identity
    assert new.instance_id != old.instance_id
    assert [announced.instance_id for announced in store.announced] == [old.instance_id, new.instance_id]
    assert store.live_instance == new.instance_id
    assert [ack.accepted is not None for ack in acks] == [True]
    second = store.written[1].attempt
    assert (second.header.instance_id, second.header.attempt_no) == (new.instance_id, 2)
    # The new process's reader has its own memory: its first call for the symbol fails again, on attempt 2.
    assert written(store)[1] == ('r1', 2, DeliveryOutcome.FAILED, DeliveryVerdict.CONTINUE)
    assert bars_calls(ingest) == [symbol]


@pytest.mark.asyncio
async def test_a_finished_request_is_not_recovered_and_a_queued_one_is_re_submitted_with_its_count(rig):
    store = FakeStore()
    ingest = rig.ingest(store, buffer_size=1)
    store.hold()
    await store.submit(ingest.submit, bars_request('done', 'AAPL'))
    await store.submit(ingest.submit, bars_request('queued', 'AMD'))
    async with ingest:
        await until(lambda: len(store.received) == 1, 'the first delivery, held')
        await settle()
        assert ingest.scheduler.queued == 1

        await ingest.restart()
        store.release()
        acks = await store.recover(ingest.submit)
        await until(lambda: len(store.written) == 2, 'the queued request, attempted by the new instance')

    assert len(acks) == 1
    assert [(request_id, attempt_no) for request_id, attempt_no, *_ in written(store)] == [('done', 1), ('queued', 1)]
    assert store.written[1].attempt.header.instance_id == ingest.identity.instance_id
    assert bars_calls(ingest) == ['AMD']


@pytest.mark.asyncio
async def test_the_recovered_charge_leaves_the_budget_in_debt_so_the_first_new_call_waits_for_the_refill(rig):
    """Six calls a minute: a bucket of one token refilling 0.1 a second.

    Ten daily periods at a page size of one estimate ten calls; charged to a fresh bucket of one, the first new call
    waits (10 - 1 + 1) / 0.1 = 100 s.
    """
    store = FakeStore()
    ingest = rig.ingest(store, calls_per_minute=6, page_size=1)
    store.hold()
    await store.submit(ingest.submit, bars_request('r1', f'{FAILONCE_PREFIX}AAPL', days=10))
    async with ingest:
        await until(lambda: store.count('r1') == 1, 'the first attempt, its answer held')
        assert rig.budget_sleeps.slept == []

        await ingest.restart()
        await store.recover(ingest.submit)
        # Charged before it is queued, so the first data call of the new process waits for the refill.
        await until(lambda: len(bars_calls(ingest)) == 1, 'the recovered attempt at the vendor')

    assert sum(rig.budget_sleeps.slept) == pytest.approx(100.0)
    assert store.received[-1].header.attempt_no == 2


@pytest.mark.asyncio
async def test_a_request_not_flagged_recovered_is_never_charged_and_waits_for_nothing(rig):
    store = FakeStore()
    ingest = rig.ingest(store, calls_per_minute=6, page_size=1)
    await store.submit(ingest.submit, bars_request('r1', 'AAPL', days=10))

    async with ingest:
        await until(lambda: len(store.written) == 1, 'the report')

    assert rig.budget_sleeps.slept == []


@pytest.mark.asyncio
async def test_a_store_restart_keeps_ingests_queue_redelivers_and_duplicates_nothing(rig):
    store = FakeStore()
    server = FakeStoreServer(store)
    await server.start()
    channel = create_channel(server.target)
    try:
        client = GrpcStoreDeliveryClient(channel, deadline_s=0.3, register_deadline_s=0.3)
        ingest = rig.ingest(client, buffer_size=1)
        await store.submit(ingest.submit, bars_request('r1', 'AAPL'))
        await store.submit(ingest.submit, bars_request('r2', 'AMD'))
        await server.stop()

        async with ingest:
            # The store is down: r1 is attempted once and its report waits in the buffer; r2 stays queued.
            await until(lambda: rig.executor_sleep.slept.count(1.0) >= 1, 'a failed delivery and its backoff')
            await settle()
            assert bars_calls(ingest) == ['AAPL']
            assert ingest.scheduler.queued == 1 and ingest.executor.unacknowledged == 1

            await server.start()
            await until(lambda: len(store.written) == 2, 'both reports, after the store restart')
    finally:
        await channel.close()
        await server.stop()

    assert written(store) == [
        ('r1', 1, DeliveryOutcome.SUCCEEDED, DeliveryVerdict.FINAL),
        ('r2', 1, DeliveryOutcome.SUCCEEDED, DeliveryVerdict.FINAL),
    ]
    assert len(store.received) == 2
    assert bars_calls(ingest) == ['AAPL', 'AMD']


# ---------------------------------------------------------------------------------------------
# Queue expiry (owner ruling 2026-10-24)
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_request_expired_at_dequeue_is_never_sent_and_is_returned_failed_as_expired(rig):
    store = FakeStore()
    ingest = rig.ingest(store)
    await store.submit(ingest.submit, bars_request('r1', 'AAPL', ready_by=T0 + timedelta(minutes=1)))
    rig.wall.advance(timedelta(minutes=2))

    async with ingest:
        await until(lambda: len(store.written) == 1, 'the expired report')

    assert ingest.reader.calls == ()
    assert written(store) == [('r1', 1, DeliveryOutcome.EXPIRED, DeliveryVerdict.FINAL)]
    report = store.written[0].attempt
    assert (report.header.feed, report.trailer.vendor_stats, report.trailer.as_of) == (Feed.IEX, None, None)
    assert store.count('r1') == 0 and store.is_finished('r1')


@pytest.mark.asyncio
async def test_a_null_ready_by_never_expires(rig):
    store = FakeStore()
    ingest = rig.ingest(store)
    await store.submit(ingest.submit, bars_request('r1', 'AAPL'))
    rig.wall.advance(timedelta(days=3650))

    async with ingest:
        await until(lambda: len(store.written) == 1, 'the report')

    assert written(store) == [('r1', 1, DeliveryOutcome.SUCCEEDED, DeliveryVerdict.FINAL)]


@pytest.mark.asyncio
async def test_a_retry_whose_ready_by_passed_meanwhile_expires_when_it_re_enters_the_queue(rig):
    store = FakeStore()
    ingest = rig.ingest(store)
    symbol = f'{FAILONCE_PREFIX}AAPL'
    await store.submit(ingest.submit, bars_request('r1', symbol, ready_by=T0 + timedelta(minutes=1)))

    async with ingest:
        await until(lambda: store.count('r1') == 1, 'the failed first attempt')
        rig.wall.advance(timedelta(minutes=2))
        rig.executor_sleep.release_retries()
        await until(lambda: len(store.written) == 2, 'the expired retry')

    assert written(store) == [
        ('r1', 1, DeliveryOutcome.FAILED, DeliveryVerdict.CONTINUE),
        ('r1', 2, DeliveryOutcome.EXPIRED, DeliveryVerdict.FINAL),
    ]
    assert bars_calls(ingest) == [symbol]


# ---------------------------------------------------------------------------------------------
# The outgoing buffer (ruling R2)
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_a_full_buffer_the_worker_takes_nothing_and_resumes_on_the_answers_dropping_nothing(rig):
    store = FakeStore()
    ingest = rig.ingest(store, buffer_size=1)
    store.hold()
    for request_id, symbol in [('r1', 'AAPL'), ('r2', 'AMD'), ('r3', 'XYZ')]:
        await store.submit(ingest.submit, bars_request(request_id, symbol))

    async with ingest:
        await until(lambda: len(store.received) == 1, 'the first report')
        await settle()
        assert bars_calls(ingest) == ['AAPL']
        assert (ingest.scheduler.queued, ingest.executor.unacknowledged) == (2, 1)

        store.release()
        await until(lambda: len(store.written) == 3, 'every report')

    assert [request_id for request_id, *_ in written(store)] == ['r1', 'r2', 'r3']
    assert bars_calls(ingest) == ['AAPL', 'AMD', 'XYZ']


@pytest.mark.asyncio
async def test_the_alarm_fires_after_the_timeout_with_the_store_unreachable_and_clears_on_recovery(rig, caplog):
    store = FakeStore()
    ingest = rig.ingest(store)
    store.go_down()
    await store.submit(ingest.submit, bars_request('r1', 'AAPL'))

    async with ingest:
        await until(lambda: store.refused_while_down >= 1, 'a refused delivery')
        rig.wall.advance(timedelta(seconds=STALL_TIMEOUT_S))
        assert ingest.executor.check_stall() is False

        rig.wall.advance(timedelta(seconds=1))
        with caplog.at_level(logging.ERROR):
            assert ingest.executor.check_stall() is True
        assert ingest.executor.stalled
        assert any(
            'stalled' in record.getMessage() and 'request_id=r1' in record.getMessage() for record in caplog.records
        )

        store.come_up()
        await until(lambda: ingest.executor.unacknowledged == 0, 'the acknowledgement after recovery')

    assert ingest.executor.stalled is False
    assert written(store) == [('r1', 1, DeliveryOutcome.SUCCEEDED, DeliveryVerdict.FINAL)]


# ---------------------------------------------------------------------------------------------
# The world through the push path: what the store receives
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_asset_list_attempt_carries_the_days_list_to_the_store(rig):
    store = FakeStore()
    ingest = rig.ingest(store)
    request = SubmitRequest(
        request_id='list',
        owner='rebalancer',
        asset_list=AssetListRequest(
            source=DataSource.ALPACA_API, asset_type=AssetType.STOCK, status=AssetListStatus.ACTIVE
        ),
    )
    await store.submit(ingest.submit, request)
    rig.wall.advance(timedelta(days=2))

    async with ingest:
        await until(lambda: len(store.written) == 1, 'the asset-list report')

    report = store.written[0].attempt
    symbols = [record.symbol for part in report.parts for record in part.records]
    assert symbols == ['AAPL', 'META', 'METV', 'XYZ', 'AMD', 'FB', 'BUUU']
    assert (report.header.quota_key, report.header.feed) == (ALPACA_TRADING_SCOPE, None)


@pytest.mark.asyncio
async def test_a_bars_attempt_carries_the_lookups_vendor_id_and_a_reused_ticker_changes_it(rig):
    store = FakeStore()
    ingest = rig.ingest(store)
    symbol = f'{REUSED_PREFIX}VFV'
    await store.submit(ingest.submit, bars_request('first', symbol))
    await store.submit(ingest.submit, bars_request('second', symbol))

    async with ingest:
        await until(lambda: len(store.written) == 2, 'both reports')

    first, second = (entry.attempt.header.vendor_asset_id for entry in store.written)
    assert first and second and first != second


@pytest.mark.asyncio
async def test_a_bars_attempt_whose_symbol_the_vendor_no_longer_lists_fails_permanent(rig):
    store = FakeStore()
    ingest = rig.ingest(store)
    await store.submit(ingest.submit, bars_request('gone', f'{NOTFOUND_PREFIX}VFV'))
    await store.submit(ingest.submit, bars_request('sq', 'SQ'))
    rig.wall.advance(timedelta(days=1))

    async with ingest:
        await until(lambda: len(store.written) == 2, 'both reports')

    assert [(entry.attempt.trailer.outcome, entry.attempt.trailer.error_class) for entry in store.written] == [
        (DeliveryOutcome.FAILED, DeliveryErrorClass.PERMANENT),
        (DeliveryOutcome.FAILED, DeliveryErrorClass.PERMANENT),
    ]
    assert [entry.answer.verdict for entry in store.written] == [DeliveryVerdict.FINAL] * 2
