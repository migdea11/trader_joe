import os
import time
from unittest.mock import patch

import pytest

from common.enums.data_stock import DataSource, UpdateType
from data.ingest.app.brokers.rate_budget import RateBudget, RequestPriority, priority_for_update_type


class FakeClock:
    """A monotonic clock the test advances by hand, so refill is not timing-dependent."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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
