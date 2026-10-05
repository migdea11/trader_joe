import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from common.enums.data_stock import DataSource, UpdateType
from common.errors.vocabulary import REASONS, ExogenousError, Outcome, Reason
from data.ingest.app.brokers import rate_budget
from data.ingest.app.brokers.rate_budget import RateBudget, RequestPriority, priority_for_update_type


class FakeClock:
    """A monotonic clock the test advances by hand, so refill is not timing-dependent."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# The wall-clock instant the FakeClock's zero stands for, so a deadline and a reset_at are instants.
WALL_ZERO = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


class Clocks:
    """One hand-driven time, read both ways: monotonic seconds for refill, an aware instant for deadlines.

    sleep() is what acquire() awaits in place of asyncio.sleep: it records the wait and advances both
    clocks by it, so a test observes every sleep and none of them takes real time.
    """

    def __init__(self) -> None:
        self.monotonic = FakeClock()
        self.slept: list[float] = []
        self.on_sleep = None

    def wall(self) -> datetime:
        return WALL_ZERO + timedelta(seconds=self.monotonic.now)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.monotonic.advance(seconds)
        if self.on_sleep is not None:
            self.on_sleep()

    def budget(self, rate_per_sec: float = 1.0, burst: float = 1.0, backfill_reserve: float = 0.0) -> RateBudget:
        return RateBudget(
            'ALPACA',
            rate_per_sec=rate_per_sec,
            burst=burst,
            backfill_reserve=backfill_reserve,
            clock=self.monotonic,
            wall_clock=self.wall,
        )


@pytest.fixture
def clocks(monkeypatch) -> Clocks:
    """Hand-driven clocks, with acquire()'s asyncio.sleep replaced by Clocks.sleep: no test here really sleeps."""
    driven = Clocks()
    monkeypatch.setattr(rate_budget, 'asyncio', SimpleNamespace(sleep=driven.sleep))
    return driven


def test_unconfigured_vendor_is_not_throttled():
    # An unset rate must leave the vendor alone rather than throttle it at a number
    # nobody has verified -- Alpaca's figure is still pending (tj-3mk3u5.2).
    budget = RateBudget('ALPACA', rate_per_sec=None)

    assert all(budget.try_acquire(RequestPriority.BACKFILL) for _ in range(100))


def test_burst_is_spent_then_refilled():
    clock = FakeClock()
    budget = RateBudget('ALPACA', rate_per_sec=2.0, burst=2.0, backfill_reserve=0.0, clock=clock)

    assert budget.try_acquire(RequestPriority.LIVE)
    assert budget.try_acquire(RequestPriority.LIVE)
    assert not budget.try_acquire(RequestPriority.LIVE)

    clock.advance(1.0)
    assert budget.try_acquire(RequestPriority.LIVE)
    assert budget.try_acquire(RequestPriority.LIVE)
    assert not budget.try_acquire(RequestPriority.LIVE)


def test_refill_is_capped_at_the_burst():
    clock = FakeClock()
    budget = RateBudget('ALPACA', rate_per_sec=2.0, burst=2.0, backfill_reserve=0.0, clock=clock)

    budget.try_acquire(RequestPriority.LIVE)
    clock.advance(3600)

    assert budget.tokens == 2.0


def test_backfill_draws_only_what_live_leaves():
    clock = FakeClock()
    budget = RateBudget('ALPACA', rate_per_sec=4.0, burst=4.0, backfill_reserve=2.0, clock=clock)

    # Backfill stops at the reserve...
    assert budget.try_acquire(RequestPriority.BACKFILL)
    assert budget.try_acquire(RequestPriority.BACKFILL)
    assert not budget.try_acquire(RequestPriority.BACKFILL)

    # ...and what it left is still there for the live path. That is the anti-thrash rule.
    assert budget.try_acquire(RequestPriority.LIVE)
    assert budget.try_acquire(RequestPriority.INTERACTIVE)
    assert not budget.try_acquire(RequestPriority.LIVE)


def test_reserve_never_starves_backfill_outright():
    # A bucket too small to hold both a reserve and a token would refuse backfill forever.
    budget = RateBudget('IB', rate_per_sec=0.2, backfill_reserve=5.0)

    assert budget.try_acquire(RequestPriority.BACKFILL)


@pytest.mark.asyncio
async def test_acquire_waits_for_the_next_token():
    # 1000/s so the wait is about a millisecond of real time rather than a real budget.
    budget = RateBudget('ALPACA', rate_per_sec=1000.0, burst=1.0)

    await budget.acquire(RequestPriority.LIVE)
    start = time.monotonic()
    await budget.acquire(RequestPriority.LIVE)

    assert time.monotonic() - start >= 0.0005


def test_from_env_reads_the_vendor_specific_names():
    env = {'ALPACA_RATE_LIMIT_PER_SEC': '3', 'ALPACA_RATE_BURST': '6', 'ALPACA_BACKFILL_RESERVE': '2'}
    with patch.dict(os.environ, env, clear=False):
        budget = RateBudget.from_env(DataSource.ALPACA_API)

    assert budget.tokens == 6.0
    for _ in range(4):
        assert budget.try_acquire(RequestPriority.BACKFILL)
    assert not budget.try_acquire(RequestPriority.BACKFILL)


def test_update_type_maps_to_priority():
    assert priority_for_update_type(UpdateType.STREAM) is RequestPriority.LIVE
    assert priority_for_update_type(UpdateType.DAILY) is RequestPriority.INTERACTIVE
    assert priority_for_update_type(UpdateType.STATIC) is RequestPriority.BACKFILL


# ---------------------------------------------------------------------------------------------
# U4 = A (ADR tj-fa1rpu; TE-4 tj-3mk3u5.37.5 item 5): with a deadline, acquire FAILS FAST instead of
# sleeping past it, and hands back the wait __wait_for() computed as reset_at. Without one, today's loop.
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_deadline_the_wait_would_pass_raises_rate_budget_at_once_without_sleeping(clocks):
    budget = clocks.budget(rate_per_sec=2.0, burst=1.0)
    assert budget.try_acquire(RequestPriority.LIVE)
    # One token at 2/s: the next is due in 0.5 s, and the caller can wait 0.2 s.
    deadline = WALL_ZERO + timedelta(seconds=0.2)

    with pytest.raises(ExogenousError) as raised:
        await budget.acquire(RequestPriority.LIVE, deadline=deadline)

    error = raised.value
    assert error.reason is Reason.RATE_BUDGET
    assert REASONS[error.reason].outcome is Outcome.NOT_READY
    assert error.reset_at == WALL_ZERO + timedelta(seconds=0.5)
    assert error.retry_after == 1
    assert dict(error.metadata) == {'vendor': 'ALPACA'}
    assert error.__cause__ is None
    assert clocks.slept == [], 'acquire slept although the deadline could not be met'
    # Refused, so nothing was spent: the budget is exactly as the refusal found it.
    assert budget.tokens == 0.0


@pytest.mark.asyncio
async def test_the_refusals_retry_after_is_derived_from_the_budgets_wall_clock(clocks):
    budget = clocks.budget(rate_per_sec=0.1, burst=1.0)
    assert budget.try_acquire(RequestPriority.LIVE)

    with pytest.raises(ExogenousError) as raised:
        await budget.acquire(RequestPriority.LIVE, deadline=WALL_ZERO)

    assert raised.value.retry_after == 10
    clocks.monotonic.advance(4.0)
    assert raised.value.retry_after == 6


@pytest.mark.asyncio
async def test_a_deadline_the_wait_meets_sleeps_that_wait_once_and_takes_the_token(clocks):
    budget = clocks.budget(rate_per_sec=2.0, burst=1.0)
    assert budget.try_acquire(RequestPriority.LIVE)

    await budget.acquire(RequestPriority.LIVE, deadline=WALL_ZERO + timedelta(seconds=5))

    assert clocks.slept == [0.5]
    assert budget.tokens == 0.0


@pytest.mark.asyncio
async def test_a_wait_ending_exactly_at_the_deadline_is_admitted(clocks):
    # 'Would pass it' is strictly after: a token due AT the deadline is still in time.
    budget = clocks.budget(rate_per_sec=2.0, burst=1.0)
    assert budget.try_acquire(RequestPriority.LIVE)

    await budget.acquire(RequestPriority.LIVE, deadline=WALL_ZERO + timedelta(seconds=0.5))

    assert clocks.slept == [0.5]


@pytest.mark.asyncio
async def test_a_token_available_now_is_spent_whatever_the_deadline_says(clocks):
    budget = clocks.budget(rate_per_sec=1.0, burst=2.0)

    await budget.acquire(RequestPriority.LIVE, deadline=WALL_ZERO - timedelta(hours=1))

    assert clocks.slept == []
    assert budget.tokens == 1.0


@pytest.mark.asyncio
async def test_the_deadline_is_checked_again_on_every_pass_against_the_time_then(clocks):
    """A token taken by another caller during the sleep cannot stretch the wait past the deadline."""
    budget = clocks.budget(rate_per_sec=1.0, burst=1.0)
    assert budget.try_acquire(RequestPriority.LIVE)
    # While this caller sleeps its 1 s, another takes the token that refills: once, so a budget that
    # failed to re-check would acquire on its third pass and fail this test rather than spin forever.
    steals = [True]
    clocks.on_sleep = lambda: steals and steals.pop() and budget.try_acquire(RequestPriority.LIVE)

    with pytest.raises(ExogenousError) as raised:
        await budget.acquire(RequestPriority.LIVE, deadline=WALL_ZERO + timedelta(seconds=1.5))

    # The first pass fit (due at 1 s); the second, measured at 1 s, would end at 2 s, past 1.5 s.
    assert clocks.slept == [1.0]
    assert raised.value.reset_at == WALL_ZERO + timedelta(seconds=2)


@pytest.mark.asyncio
async def test_a_backfill_wait_counts_the_reserve_it_may_not_spend(clocks):
    # Burst 4 with 2 held back from backfill: at 2 tokens backfill needs a third, due in 1 s at 1/s,
    # while live may spend one now.
    budget = clocks.budget(rate_per_sec=1.0, burst=4.0, backfill_reserve=2.0)
    assert budget.try_acquire(RequestPriority.LIVE)
    assert budget.try_acquire(RequestPriority.LIVE)
    deadline = WALL_ZERO + timedelta(seconds=0.5)

    with pytest.raises(ExogenousError) as raised:
        await budget.acquire(RequestPriority.BACKFILL, deadline=deadline)
    await budget.acquire(RequestPriority.LIVE, deadline=deadline)

    assert raised.value.reset_at == WALL_ZERO + timedelta(seconds=1)
    assert clocks.slept == []


@pytest.mark.asyncio
async def test_without_a_deadline_acquire_keeps_todays_unbounded_wait(clocks):
    # The Kafka path passes no deadline (TE-4 item 6), so a long wait is still waited out, never refused.
    budget = clocks.budget(rate_per_sec=0.01, burst=1.0)
    assert budget.try_acquire(RequestPriority.LIVE)

    await budget.acquire(RequestPriority.LIVE)

    assert clocks.slept == [100.0]


@pytest.mark.asyncio
async def test_an_unthrottled_budget_never_refuses_even_past_its_deadline(clocks):
    budget = RateBudget('ALPACA', rate_per_sec=None, clock=clocks.monotonic, wall_clock=clocks.wall)

    for _ in range(5):
        await budget.acquire(RequestPriority.BACKFILL, deadline=WALL_ZERO - timedelta(days=1))

    assert clocks.slept == []


@pytest.mark.asyncio
async def test_the_default_wall_clock_is_aware_utc():
    # A naive default would raise TypeError at the comparison with an aware deadline instead.
    budget = RateBudget('ALPACA', rate_per_sec=1.0, burst=1.0)
    assert budget.try_acquire(RequestPriority.LIVE)
    before = datetime.now(UTC)

    with pytest.raises(ExogenousError) as raised:
        await budget.acquire(RequestPriority.LIVE, deadline=before - timedelta(days=1))

    assert raised.value.reset_at.utcoffset() == timedelta(0)
    assert before <= raised.value.reset_at <= datetime.now(UTC) + timedelta(seconds=2)


@pytest.mark.parametrize(
    ('rate_per_sec', 'burst', 'seconds'),
    [
        pytest.param(3.0, 3.0, 1.0, id='deployed-default'),
        pytest.param(2.0, 10.0, 5.0, id='burst-over-rate'),
        pytest.param(0.2, None, 5.0, id='burst-floored-at-one-token'),
        pytest.param(None, None, 0.0, id='unthrottled-models-no-window'),
    ],
)
def test_full_refill_seconds_is_an_empty_bucket_filled_whatever_is_held_now(rate_per_sec, burst, seconds):
    budget = RateBudget('ALPACA', rate_per_sec=rate_per_sec, burst=burst)
    full = budget.full_refill_seconds

    budget.try_acquire(RequestPriority.LIVE)

    assert full == budget.full_refill_seconds == seconds
