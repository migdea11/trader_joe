"""retry_after, DERIVED from reset_at through an injectable clock and never stored (TE-1 tj-3mk3u5.37.3).

The bead: retry_after = max(0, ceil(reset_at - now)) whole seconds, so a relayed error's delay never goes
stale across a hop, and a constructor that takes retry_after seconds sets reset_at = now + retry_after.
Every clock here is fixed in 2001, decades from the real one, so a read of real time cannot pass by accident,
and nothing sleeps: time moves only when a test advances its clock.
"""

import math
from datetime import UTC, datetime, timedelta

import pytest

from common.errors.vocabulary import REASONS, ExogenousError, Outcome, Reason


pytestmark = pytest.mark.common

NOW = datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)

# NOT_READY and free to carry reset_at or not; the arithmetic does not depend on the reason.
ANY_REASON = Reason.VENDOR_UNAVAILABLE

REFUSED_REASONS = [reason for reason in Reason if REASONS[reason].outcome is Outcome.REFUSED]


class FakeClock:
    """A clock that reads NOW until a test moves it."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.mark.parametrize(
    ('until_reset', 'expected'),
    [
        (timedelta(seconds=5), 5),
        (timedelta(seconds=4, microseconds=1), 5),
        (timedelta(seconds=4, microseconds=999_999), 5),
        (timedelta(microseconds=1), 1),
        (timedelta(0), 0),
        (timedelta(microseconds=-1), 0),
        (timedelta(seconds=-1), 0),
        (timedelta(days=-1), 0),
        (timedelta(days=1), 86_400),
    ],
    ids=['5s', '4.000001s', '4.999999s', '1us', 'now', '-1us', '-1s', '-1d', '1d'],
)
def test_retry_after_is_whole_seconds_rounded_up_and_never_negative(until_reset: timedelta, expected: int):
    """max(0, ceil(reset_at - now)): never short of the real wait, and 0 once reset_at has passed."""
    retry_after = ExogenousError(ANY_REASON, 'detail', reset_at=NOW + until_reset, clock=FakeClock(NOW)).retry_after
    assert retry_after == expected
    assert type(retry_after) is int


def test_retry_after_is_derived_on_every_read_never_stored():
    """The delay counts down as the clock moves, while reset_at stays fixed."""
    clock = FakeClock(NOW)
    error = ExogenousError(ANY_REASON, 'detail', reset_at=NOW + timedelta(seconds=10), clock=clock)
    assert error.retry_after == 10
    clock.advance(timedelta(seconds=2, microseconds=500_000))
    assert error.retry_after == 8
    clock.advance(timedelta(seconds=30))
    assert error.retry_after == 0
    assert error.reset_at == NOW + timedelta(seconds=10)


def test_no_reset_at_means_no_retry_after():
    """D4: a delay is attached only where the server can name one."""
    assert ExogenousError(ANY_REASON, 'detail', clock=FakeClock(NOW)).retry_after is None


def test_the_default_clock_is_the_real_utc_clock():
    """Without an injected clock, retry_after is measured from the real current time."""
    retry_after = ExogenousError(ANY_REASON, 'detail', reset_at=datetime.now(UTC) + timedelta(hours=1)).retry_after
    assert retry_after is not None
    assert 3_500 < retry_after <= 3_600


@pytest.mark.parametrize(('seconds', 'expected'), [(30, 30), (4.5, 5), (0, 0)], ids=['30s', '4.5s', '0s'])
def test_from_retry_after_sets_reset_at_to_now_plus_the_delay(seconds: float, expected: int):
    """The bead: a constructor that takes retry_after seconds sets reset_at = now + retry_after."""
    error = ExogenousError.from_retry_after(Reason.RATE_BUDGET, 'detail', seconds, clock=FakeClock(NOW))
    assert error.reset_at == NOW + timedelta(seconds=seconds)
    assert error.retry_after == expected


def test_a_relayed_delay_counts_down_rather_than_going_stale():
    """The point of deriving: an error built from a delay and read later reports what is left, not what was given."""
    clock = FakeClock(NOW)
    error = ExogenousError.from_retry_after(Reason.VENDOR_RATE_LIMITED, 'detail', 10, clock=clock)
    clock.advance(timedelta(seconds=4))
    assert error.retry_after == 6


def test_from_retry_after_builds_the_class_it_is_called_on():
    """A leaf's from_retry_after builds the leaf, so its except clauses still catch it."""

    class Leaf(ExogenousError):
        pass

    assert type(Leaf.from_retry_after(Reason.RATE_BUDGET, 'detail', 5, clock=FakeClock(NOW))) is Leaf


@pytest.mark.parametrize(
    'seconds', [-1, -0.000001, math.nan, math.inf, -math.inf], ids=['-1', '-1us', 'nan', 'inf', '-inf']
)
def test_from_retry_after_refuses_a_delay_that_is_not_finite_and_non_negative(seconds: float):
    """A negative, NaN or infinite delay names no instant to wait for."""
    with pytest.raises(ValueError):
        ExogenousError.from_retry_after(ANY_REASON, 'detail', seconds, clock=FakeClock(NOW))


@pytest.mark.parametrize('reason', REFUSED_REASONS, ids=str)
def test_from_retry_after_refuses_a_refused_reason(reason: Reason):
    """D3: no wait cures a REFUSED reason, whichever constructor names the wait."""
    with pytest.raises(ValueError):
        REASONS[reason].branch.from_retry_after(reason, 'detail', 5, clock=FakeClock(NOW))
