"""The scheduler module (tj-7zjov1.19), against the rulings in tj-26jt9w addenda 1 to 6.

Every clock here is driven by hand: the scheduler's wall clock (ready-by checks, deadlines) is a FakeWall, the
rate budgets refill on a FakeMonotonic, and asyncio.sleep is replaced in the scheduler module (where acquire
waits) and in rate_budget (RateBudget.acquire) so a wait is recorded and advances the refill clock, never slept.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from common.errors.vocabulary import ExogenousError, Reason
from data.ingest.app.brokers import rate_budget
from data.ingest.app.brokers.rate_budget import RateBudget, RequestPriority
from data.ingest.app.scheduler import (
    Credential,
    ExpiredRequest,
    FailureCause,
    Limit,
    ScheduledRequest,
    Scheduler,
    SchedulerBuilder,
    SchedulerConfigError,
    StaticCredentialProvider,
    most_headroom,
)
from data.ingest.app.scheduler import scheduler as scheduler_module


T0 = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
DAY = timedelta(days=1)
SECOND = timedelta(seconds=1)
DATA = 'alpaca-data'
TRADING = 'alpaca-trading'


class FakeWall:
    """The scheduler's wall clock: an aware instant the test moves by hand."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class FakeMonotonic:
    """The budgets' refill clock, advanced by the patched sleep."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Sleeps:
    """Stands in for asyncio.sleep: records each wait and advances the refill clock by it.

    The clock moves a hair past the wait (one part in a billion, plus a nanosecond), never short of it, so a
    float rounding in the refill cannot leave a token at 0.9999999 and spin, nor place a spend early.
    """

    def __init__(self, monotonic: FakeMonotonic) -> None:
        self.monotonic = monotonic
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.monotonic.now += seconds * (1 + 1e-9) + 1e-9

    @property
    def total(self) -> float:
        return sum(self.slept)


class Sink:
    """The expired sink: collects what the scheduler hands to the return path."""

    def __init__(self) -> None:
        self.expired: list[ExpiredRequest] = []

    async def __call__(self, expired: ExpiredRequest) -> None:
        self.expired.append(expired)


class CostSpy:
    """A cost function that records every call and returns a fixed estimate."""

    def __init__(self, estimate: int = 3) -> None:
        self.estimate = estimate
        self.calls: list[tuple[ScheduledRequest, int]] = []

    def __call__(self, request: ScheduledRequest, page_size: int) -> int:
        self.calls.append((request, page_size))
        return self.estimate


@pytest.fixture
def wall() -> FakeWall:
    return FakeWall()


@pytest.fixture
def monotonic() -> FakeMonotonic:
    return FakeMonotonic()


@pytest.fixture
def sleeps(monkeypatch, monotonic) -> Sleeps:
    driven = Sleeps(monotonic)
    # Scheduler.acquire sleeps through its own module; RateBudget.acquire through rate_budget. Both are
    # replaced, so no wait on either path can block on a clock the test never advances.
    monkeypatch.setattr(scheduler_module, 'asyncio', SimpleNamespace(sleep=driven.sleep))
    monkeypatch.setattr(rate_budget, 'asyncio', SimpleNamespace(sleep=driven.sleep))
    return driven


@pytest.fixture
def sink() -> Sink:
    return Sink()


@pytest.fixture
def cost() -> CostSpy:
    return CostSpy()


def provider() -> StaticCredentialProvider:
    return StaticCredentialProvider(Credential(key_id='key-1', secret='s3cret-1'))


def builder(wall, monotonic, sink, cost, page_size: int = 10_000) -> SchedulerBuilder:
    """Every part but the scopes, so each test declares exactly the limits it is about."""
    return (
        SchedulerBuilder(clock=wall)
        .budget_clock(monotonic)
        .cost_function(cost, page_size=page_size)
        .credential_provider(provider())
        .on_expired(sink)
    )


def one_scope(wall, monotonic, sink, cost, count: int = 200, interval: timedelta = MINUTE) -> Scheduler:
    return (
        builder(wall, monotonic, sink, cost)
        .declare_scope(DATA, [Limit(interval, count)])
        .set_limit(interval, count, scope=DATA)
        .build()
    )


def req(request_id: str, ready_by: datetime | None = None, scope: str = DATA) -> ScheduledRequest:
    return ScheduledRequest(request_id=request_id, quota_scope=scope, payload={'id': request_id}, ready_by=ready_by)


async def take_ids(scheduler: Scheduler, n: int) -> list[str]:
    return [(await asyncio.wait_for(scheduler.take(), timeout=1)).request_id for _ in range(n)]


# FIFO order (addendum 2): dated in arrival order, then undated in arrival order.


@pytest.mark.asyncio
async def test_dated_requests_leave_before_undated_ones_each_in_arrival_order(wall, monotonic, sink, cost):
    scheduler = one_scope(wall, monotonic, sink, cost)
    for request in (
        req('undated-1'),
        req('dated-1', T0 + timedelta(hours=2)),
        req('undated-2'),
        req('dated-2', T0 + timedelta(hours=1)),
    ):
        await scheduler.submit(request)

    assert scheduler.queued == 4
    # dated-2 has the earlier ready-by but arrived later: FIFO, not earliest-deadline-first.
    assert await take_ids(scheduler, 4) == ['dated-1', 'dated-2', 'undated-1', 'undated-2']
    assert scheduler.queued == 0


@pytest.mark.asyncio
async def test_a_dated_request_arriving_after_undated_ones_still_goes_first(wall, monotonic, sink, cost):
    scheduler = one_scope(wall, monotonic, sink, cost)
    await scheduler.submit(req('undated-1'))
    await scheduler.submit(req('undated-2'))
    await scheduler.submit(req('dated', T0 + timedelta(hours=1)))

    assert await take_ids(scheduler, 3) == ['dated', 'undated-1', 'undated-2']


@pytest.mark.asyncio
async def test_take_waits_on_an_empty_queue_and_wakes_on_submit(wall, monotonic, sink, cost):
    scheduler = one_scope(wall, monotonic, sink, cost)
    taking = asyncio.create_task(scheduler.take())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not taking.done()

    await scheduler.submit(req('late'))

    taken = await asyncio.wait_for(taking, timeout=1)
    assert taken.request_id == 'late'


# The ready-by check at dequeue (addendum 6), the three ruled scenarios.


@pytest.mark.asyncio
async def test_a_request_expired_when_popped_goes_to_the_sink_and_is_not_returned(wall, monotonic, sink, cost):
    scheduler = one_scope(wall, monotonic, sink, cost)
    stale = req('stale', T0 + timedelta(minutes=5))
    await scheduler.submit(stale)
    await scheduler.submit(req('fresh', T0 + timedelta(hours=1)))
    wall.advance(timedelta(minutes=6))

    taken = await asyncio.wait_for(scheduler.take(), timeout=1)

    assert taken.request_id == 'fresh'
    assert len(sink.expired) == 1
    expired = sink.expired[0]
    assert expired.request is stale
    assert expired.cause is FailureCause.EXPIRED
    assert expired.checked_at == wall.now


@pytest.mark.asyncio
async def test_a_null_ready_by_never_expires(wall, monotonic, sink, cost):
    scheduler = one_scope(wall, monotonic, sink, cost)
    await scheduler.submit(req('backtest-prep'))
    wall.advance(timedelta(days=3650))

    taken = await asyncio.wait_for(scheduler.take(), timeout=1)

    assert taken.request_id == 'backtest-prep'
    assert sink.expired == []


@pytest.mark.asyncio
async def test_a_retry_resubmitted_after_its_ready_by_expires_on_re_entry(wall, monotonic, sink, cost):
    scheduler = one_scope(wall, monotonic, sink, cost)
    attempt = req('daily', T0 + timedelta(minutes=10))
    await scheduler.submit(attempt)
    first = await asyncio.wait_for(scheduler.take(), timeout=1)
    assert first is attempt
    assert sink.expired == []

    # The attempt failed transiently; its backoff carried it past ready-by, and the retry re-enters.
    wall.advance(timedelta(minutes=11))
    await scheduler.submit(attempt)
    await scheduler.submit(req('other'))

    taken = await asyncio.wait_for(scheduler.take(), timeout=1)

    assert taken.request_id == 'other'
    assert [expired.request for expired in sink.expired] == [attempt]


@pytest.mark.asyncio
async def test_expiry_is_checked_only_at_dequeue_not_while_waiting(wall, monotonic, sink, cost):
    scheduler = one_scope(wall, monotonic, sink, cost)
    await scheduler.submit(req('waiting', T0 + timedelta(minutes=1)))
    wall.advance(timedelta(hours=1))
    await asyncio.sleep(0)

    # Not swept: still queued, nothing reported, until a take pops it.
    assert scheduler.queued == 1
    assert sink.expired == []


@pytest.mark.asyncio
async def test_a_ready_by_exactly_now_has_not_passed(wall, monotonic, sink, cost):
    scheduler = one_scope(wall, monotonic, sink, cost)
    await scheduler.submit(req('on-the-line', T0 + MINUTE))
    wall.advance(MINUTE)

    taken = await asyncio.wait_for(scheduler.take(), timeout=1)

    assert taken.request_id == 'on-the-line'
    assert sink.expired == []


# ScheduledRequest and submit.


def test_a_naive_ready_by_is_refused():
    with pytest.raises(SchedulerConfigError, match='timezone aware'):
        ScheduledRequest(request_id='r', quota_scope=DATA, payload=None, ready_by=datetime(2026, 10, 24, 14, 0))


def test_the_client_label_is_carried():
    assert ScheduledRequest(request_id='r', quota_scope=DATA, payload=None, client_label='ui').client_label == 'ui'


@pytest.mark.asyncio
async def test_submitting_to_an_unknown_scope_is_refused_and_queues_nothing(wall, monotonic, sink, cost):
    scheduler = one_scope(wall, monotonic, sink, cost)

    with pytest.raises(SchedulerConfigError, match='No limits'):
        await scheduler.submit(req('r', scope='nowhere'))
    assert scheduler.queued == 0


# set_limit and the broker ceiling (OQ3-rate).


def test_a_limit_above_the_ceiling_is_refused(wall, monotonic, sink, cost):
    scopes = builder(wall, monotonic, sink, cost).declare_scope(DATA, [Limit(MINUTE, 200)])

    with pytest.raises(SchedulerConfigError, match='above the broker ceiling'):
        scopes.set_limit(MINUTE, 201, scope=DATA)


def test_a_limit_at_or_under_the_ceiling_is_accepted(wall, monotonic, sink, cost):
    scopes = builder(wall, monotonic, sink, cost).declare_scope(DATA, [Limit(MINUTE, 200)])

    assert scopes.set_limit(MINUTE, 200, scope=DATA) is scopes
    assert scopes.set_limit(MINUTE, 50, scope=DATA) is scopes


def test_a_limit_on_an_undeclared_scope_is_refused(wall, monotonic, sink, cost):
    with pytest.raises(SchedulerConfigError, match='not declared'):
        builder(wall, monotonic, sink, cost).set_limit(MINUTE, 10, scope=DATA)


def test_a_limit_on_an_interval_with_no_ceiling_is_refused(wall, monotonic, sink, cost):
    scopes = builder(wall, monotonic, sink, cost).declare_scope(DATA, [Limit(MINUTE, 200)])

    with pytest.raises(SchedulerConfigError, match='no ceiling'):
        scopes.set_limit(DAY, 1, scope=DATA)


def test_a_scope_declared_twice_or_without_ceilings_is_refused(wall, monotonic, sink, cost):
    scopes = builder(wall, monotonic, sink, cost).declare_scope(DATA, [Limit(MINUTE, 200)])

    with pytest.raises(SchedulerConfigError, match='declared twice'):
        scopes.declare_scope(DATA, [Limit(MINUTE, 100)])
    with pytest.raises(SchedulerConfigError, match='at least one ceiling'):
        scopes.declare_scope(TRADING, [])


@pytest.mark.parametrize(('interval', 'count'), [(timedelta(0), 1), (-MINUTE, 1), (MINUTE, 0)])
def test_a_limit_needs_a_positive_interval_and_count(interval, count):
    with pytest.raises(SchedulerConfigError):
        Limit(interval, count)


@pytest.mark.asyncio
async def test_set_limit_repeats_per_interval_and_the_tighter_one_binds(wall, monotonic, sink, cost, sleeps):
    # 10 a second (bucket max(1, min(10, 1)) = 1, refill 10/s) and 2 per 10 seconds (bucket 1, refill
    # (2 - 1 + 1)/10 = 0.2/s). After the first call the first limit waits 0.1s and the second 5s: the
    # tighter one binds, and the call waits 5s.
    scheduler = (
        builder(wall, monotonic, sink, cost)
        .declare_scope(DATA, [Limit(SECOND, 10), Limit(timedelta(seconds=10), 2)])
        .set_limit(SECOND, 10, scope=DATA)
        .set_limit(timedelta(seconds=10), 2, scope=DATA)
        .build()
    )

    await scheduler.acquire(DATA)
    assert sleeps.slept == []
    await scheduler.acquire(DATA)
    assert sleeps.total == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_set_limit_again_on_the_same_interval_replaces_it(wall, monotonic, sink, cost, sleeps):
    scheduler = (
        builder(wall, monotonic, sink, cost)
        .declare_scope(DATA, [Limit(SECOND, 10)])
        .set_limit(SECOND, 2, scope=DATA)
        .set_limit(SECOND, 10, scope=DATA)
        .build()
    )

    # The looser setting comes second, so only replacement lets it through. One budget of 10 a second
    # (bucket 1, refill 10/s): one call free, the next waits 0.1s. Had the 2 a second setting survived
    # alongside, or won, the wait would be its 0.5s (bucket 1, refill 2/s).
    await scheduler.acquire(DATA)
    assert sleeps.slept == []
    await scheduler.acquire(DATA)
    assert sleeps.total == pytest.approx(0.1)
    assert len(scheduler._Scheduler__budgets[DATA]) == 1


@pytest.mark.asyncio
async def test_scopes_are_independent_budgets(wall, monotonic, sink, cost, sleeps):
    scheduler = (
        builder(wall, monotonic, sink, cost)
        .declare_scope(DATA, [Limit(SECOND, 1)])
        .declare_scope(TRADING, [Limit(SECOND, 1)])
        .set_limit(SECOND, 1, scope=DATA)
        .set_limit(SECOND, 1, scope=TRADING)
        .build()
    )

    await scheduler.acquire(DATA)
    await scheduler.acquire(TRADING)
    assert sleeps.slept == []
    await scheduler.acquire(DATA)
    assert sleeps.total == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_acquire_on_an_unknown_scope_is_refused(wall, monotonic, sink, cost, sleeps):
    scheduler = one_scope(wall, monotonic, sink, cost)

    with pytest.raises(SchedulerConfigError, match='No limits'):
        await scheduler.acquire('nowhere')


# The wrapped RateBudget: priority, the bulk reserve and the deadline fail-fast.


@pytest.mark.asyncio
async def test_backfill_keeps_off_the_reserve_interactive_does_not(wall, monotonic, sink, cost, sleeps):
    # 200 a minute: bucket B = 200/60 (3.333), refill (200 - B + 1)/60 a second, half the bucket (1.667)
    # held back from backfill. Backfill gets one call free (3.333 -> 2.333 leaves 0.667 above the
    # reserve), then waits for the 0.333 it is short. Interactive spends into the reserve: three free.
    bucket = 200 / 60
    refill = (200 - bucket + 1) / 60
    backfill = one_scope(wall, monotonic, sink, cost, count=200, interval=MINUTE)
    await backfill.acquire(DATA, RequestPriority.BACKFILL)
    assert sleeps.slept == []
    await backfill.acquire(DATA, RequestPriority.BACKFILL)
    assert sleeps.total == pytest.approx((1 + bucket / 2 - (bucket - 1)) / refill)

    sleeps.slept.clear()
    interactive = one_scope(wall, monotonic, sink, cost, count=200, interval=MINUTE)
    for _ in range(3):
        await interactive.acquire(DATA, RequestPriority.INTERACTIVE)
    assert sleeps.slept == []
    await interactive.acquire(DATA, RequestPriority.INTERACTIVE)
    assert sleeps.total == pytest.approx((1 - (bucket - 3)) / refill)


@pytest.mark.asyncio
async def test_acquire_fails_fast_when_the_wait_would_pass_the_deadline(wall, monotonic, sink, cost, sleeps):
    # One a minute (bucket 1, refill 1/60 a second): the second call would wait 60s. The deadline and
    # the reset_at are both on the scheduler's FakeWall.
    scheduler = one_scope(wall, monotonic, sink, cost, count=1, interval=MINUTE)
    await scheduler.acquire(DATA)
    (budget,) = scheduler._Scheduler__budgets[DATA]
    before = budget.tokens

    with pytest.raises(ExogenousError) as raised:
        await scheduler.acquire(DATA, deadline=wall.now + SECOND)

    assert raised.value.reason is Reason.RATE_BUDGET
    # The closed metadata allowlist has no scope key (ADR tj-fa1rpu D8): the scope travels under vendor.
    assert raised.value.metadata == {'vendor': DATA}
    assert abs(raised.value.reset_at - (wall.now + timedelta(seconds=60))) < timedelta(milliseconds=1)
    assert sleeps.slept == []
    assert budget.tokens == before


# The cost function (O2 a) and the restart charge (addendum 5).


@pytest.mark.asyncio
async def test_the_cost_function_is_called_only_by_charge_restart(wall, monotonic, sink, cost, sleeps):
    scheduler = one_scope(wall, monotonic, sink, cost)
    request = req('r', T0 + timedelta(hours=1))

    await scheduler.submit(request)
    await asyncio.wait_for(scheduler.take(), timeout=1)
    await scheduler.acquire(DATA)
    scheduler.credential(DATA)
    assert cost.calls == []

    assert scheduler.charge_restart(request) == 3
    assert cost.calls == [(request, 10_000)]


@pytest.mark.parametrize(('estimate', 'charged'), [(0, 1), (-4, 1), (1, 1), (7, 7)])
def test_charge_restart_charges_at_least_one_call(wall, monotonic, sink, estimate, charged):
    scheduler = one_scope(wall, monotonic, sink, CostSpy(estimate))

    assert scheduler.charge_restart(req('r')) == charged


@pytest.mark.asyncio
async def test_restart_debt_delays_later_acquires(wall, monotonic, sink, sleeps):
    # One a second, a bucket of one. Charging 5 leaves it at -4, so the next call waits 5s, not 0.
    scheduler = one_scope(wall, monotonic, sink, CostSpy(5), count=1, interval=SECOND)

    scheduler.charge_restart(req('r'))
    await scheduler.acquire(DATA)

    assert sleeps.total == pytest.approx(5.0)


def test_charge_restart_charges_every_budget_of_the_scope(wall, monotonic, sink):
    scheduler = (
        builder(wall, monotonic, sink, CostSpy(5))
        .declare_scope(DATA, [Limit(SECOND, 10), Limit(timedelta(seconds=10), 2)])
        .declare_scope(TRADING, [Limit(SECOND, 10)])
        .set_limit(SECOND, 10, scope=DATA)
        .set_limit(timedelta(seconds=10), 2, scope=DATA)
        .set_limit(SECOND, 10, scope=TRADING)
        .build()
    )
    # The budgets are private; reading their balances is the only direct view of "every budget".
    budgets = scheduler._Scheduler__budgets

    scheduler.charge_restart(req('r'))

    # Each budget starts full at B = max(1, min(N per second, N / 10)): 10 a second gives max(1, min(10, 1))
    # = 1, and 2 per 10 seconds gives max(1, min(0.2, 0.2)) = 1. Charging 5 takes both of DATA's to -4 and
    # leaves TRADING's full.
    assert [budget.tokens for budget in budgets[DATA]] == pytest.approx([1.0 - 5, 1.0 - 5])
    assert [budget.tokens for budget in budgets[TRADING]] == pytest.approx([1.0])


def test_rate_budget_charge_ignores_zero_and_unthrottled():
    budget = rate_budget.RateBudget('ALPACA', rate_per_sec=1.0, burst=3.0, clock=FakeMonotonic())
    budget.charge(0)
    budget.charge(-2)
    assert budget.tokens == pytest.approx(3.0)

    unthrottled = rate_budget.RateBudget('ALPACA', rate_per_sec=None)
    unthrottled.charge(100)
    assert unthrottled.try_acquire()


# The window contract (architect gate ruling, tj-wisp-6cxp9k): for Limit(T, N), no half-open window [t, t+T)
# holds more than N spent tokens, calls plus charged restart cost. Burst B = max(1, min(N per second, N / 10)),
# refill (N - B + 1) / T. Expected values below are worked from that formula by hand, not read off the code.


@pytest.mark.parametrize(
    ('interval', 'count', 'burst', 'refill_per_interval'),
    [
        # 200/min: N per second 3.333 is under N/10 = 20, so B = 10/3; refill 200 - 10/3 + 1 = 593/3 a minute.
        (MINUTE, 200, 10 / 3, 593 / 3),
        # Slow limits: B = 1, so the refill is N/T, unchanged from a plain rate.
        (DAY, 5, 1.0, 5.0),
        (timedelta(seconds=5), 1, 1.0, 1.0),
        # 4 a second: N/10 = 0.4 is under 1, so B = 1 and the refill is 4 a second.
        (SECOND, 4, 1.0, 4.0),
        # 1000 a second: N/10 = 100 binds below N per second, so B = 100; refill 1000 - 100 + 1 = 901.
        (SECOND, 1000, 100.0, 901.0),
    ],
)
def test_limit_burst_and_refill_follow_the_window_formula(interval, count, burst, refill_per_interval):
    limit = Limit(interval, count)

    assert limit.burst == pytest.approx(burst)
    assert limit.refill_per_sec * interval.total_seconds() == pytest.approx(refill_per_interval)


def test_the_200_a_minute_figures_are_the_ruled_ones():
    limit = Limit(MINUTE, 200)

    assert limit.burst == pytest.approx(3.333, abs=1e-3)
    assert limit.refill_per_sec * 60 == pytest.approx(197.667, abs=1e-3)


@pytest.mark.parametrize('interval', [SECOND, timedelta(seconds=5), MINUTE, DAY])
def test_a_count_of_one_keeps_a_whole_token_and_a_positive_refill(interval):
    limit = Limit(interval, 1)

    assert limit.burst == 1.0
    assert limit.refill_per_sec > 0
    assert limit.refill_per_sec == pytest.approx(1 / interval.total_seconds())


def test_build_hands_each_budget_the_limit_burst_and_refill(wall, monotonic, sink, cost):
    scheduler = one_scope(wall, monotonic, sink, cost, count=200, interval=MINUTE)
    (budget,) = scheduler._Scheduler__budgets[DATA]

    # A fresh budget is full at B, and fills from empty in B / refill seconds.
    assert budget.tokens == pytest.approx(10 / 3)
    assert budget.full_refill_seconds == pytest.approx((10 / 3) / (593 / 3 / 60))


def most_in_any_window(spends: list[tuple[float, int]], window: float) -> int:
    """The most tokens spent in any half-open [t, t + window); the maximum is reached at some spend's instant."""
    spends = sorted(spends)
    most = held = 0
    end = 0
    for start, n_start in spends:
        while end < len(spends) and spends[end][0] < start + window:
            held += spends[end][1]
            end += 1
        most = max(most, held)
        held -= n_start
    return most


async def drain(scheduler: Scheduler, monotonic: FakeMonotonic, calls: int) -> list[tuple[float, int]]:
    """The worst case for the window: take every token the instant the scheduler allows it."""
    spends = []
    for _ in range(calls):
        await scheduler.acquire(DATA)
        spends.append((monotonic.now, 1))
    return spends


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('interval', 'count'),
    [(MINUTE, 200), (DAY, 5), (timedelta(seconds=5), 1), (SECOND, 1000)],
    ids=['200-per-minute', '5-per-day', '1-per-5s', '1000-per-second'],
)
@pytest.mark.parametrize('charge', ['none', 'half', 'full'])
async def test_no_half_open_window_holds_more_than_the_count(wall, monotonic, sleeps, sink, interval, count, charge):
    # The restart charge, when there is one, lands at the window start on a full bucket, as after a restart:
    # 'half' charges N // 2 (at least 1), 'full' charges N, the most one window can hold.
    cost = {'none': 0, 'half': max(1, count // 2), 'full': count}[charge]
    scheduler = one_scope(wall, monotonic, sink, CostSpy(cost), count=count, interval=interval)
    limit = Limit(interval, count)
    calls = count + int(limit.burst) + count // 2 + 1  # past the end of the first window

    spends: list[tuple[float, int]] = []
    if cost:
        assert scheduler.charge_restart(req('in-flight-at-restart')) == cost
        spends.append((monotonic.now, cost))
    # Burst at the window start, then every refill boundary, past one whole window.
    spends += await drain(scheduler, monotonic, calls)
    # Idle until the bucket is full again, then burst at that boundary and drain again.
    monotonic.now += limit.burst / limit.refill_per_sec * 2
    spends += await drain(scheduler, monotonic, calls)

    assert most_in_any_window(spends, interval.total_seconds()) <= count
    # And the bound is reached, so the test is not passing on a budget that is merely slow.
    assert most_in_any_window(spends, interval.total_seconds()) == count


# acquire is all-or-nothing across the budgets of a scope (architect gate R2).


@pytest.mark.asyncio
async def test_a_deadline_failure_on_the_second_budget_leaves_the_first_untouched(wall, monotonic, sink, cost, sleeps):
    # 200 a minute (bucket 10/3) and 2 a day (bucket 1, refill 2 a day). After one call the minute budget
    # has tokens and the daily one has none, so the next call fails fast on the daily budget's half-day wait.
    scheduler = (
        builder(wall, monotonic, sink, cost)
        .declare_scope(DATA, [Limit(MINUTE, 200), Limit(DAY, 2)])
        .set_limit(MINUTE, 200, scope=DATA)
        .set_limit(DAY, 2, scope=DATA)
        .build()
    )
    minute, daily = scheduler._Scheduler__budgets[DATA]
    await scheduler.acquire(DATA)
    assert minute.tokens == pytest.approx(10 / 3 - 1)
    assert daily.tokens == pytest.approx(0.0)

    with pytest.raises(ExogenousError) as raised:
        await scheduler.acquire(DATA, deadline=wall.now + SECOND)

    assert minute.tokens == pytest.approx(10 / 3 - 1)
    assert daily.tokens == pytest.approx(0.0)
    assert sleeps.slept == []
    assert raised.value.reason is Reason.RATE_BUDGET
    # reset_at is the longest wait of the scope's budgets: the daily one, half a day.
    assert abs(raised.value.reset_at - (wall.now + DAY / 2)) < SECOND


@pytest.mark.asyncio
async def test_a_call_spends_from_every_budget_at_the_same_instant(wall, monotonic, sink, cost, sleeps):
    # 10 a second (bucket 1, refill 10/s) and 2 per 10 seconds (bucket 1, refill 0.2/s).
    scheduler = (
        builder(wall, monotonic, sink, cost)
        .declare_scope(DATA, [Limit(SECOND, 10), Limit(timedelta(seconds=10), 2)])
        .set_limit(SECOND, 10, scope=DATA)
        .set_limit(timedelta(seconds=10), 2, scope=DATA)
        .build()
    )
    fast, slow = scheduler._Scheduler__budgets[DATA]
    await scheduler.acquire(DATA)
    monotonic.now += 0.5  # the fast budget is full again; the slow one holds 0.1

    await scheduler.acquire(DATA)

    # The call waited 4.5s for the slow budget and only then spent from both. Had the fast budget's token
    # been taken at 0.5s and held, it would have refilled to full by now.
    assert sleeps.total == pytest.approx(4.5)
    assert fast.tokens == pytest.approx(0.0, abs=1e-6)
    assert slow.tokens == pytest.approx(0.0, abs=1e-6)


# One wall clock per scheduler (architect gate R3).


@pytest.mark.asyncio
async def test_the_acquire_deadline_is_judged_on_the_scheduler_clock(monotonic, sink, cost, sleeps):
    # Years from real time either way, so a deadline judged on the real clock gives the opposite answer.
    past = FakeWall(datetime(2000, 1, 1, tzinfo=UTC))
    scheduler = one_scope(past, monotonic, sink, cost, count=1, interval=MINUTE)
    await scheduler.acquire(DATA)
    # The 60s wait fits inside a deadline two minutes out on the scheduler's clock: it waits, not raises.
    await scheduler.acquire(DATA, deadline=past.now + 2 * MINUTE)
    assert sleeps.total == pytest.approx(60.0)

    future = FakeWall(datetime(2100, 1, 1, tzinfo=UTC))
    scheduler = one_scope(future, monotonic, sink, cost, count=1, interval=MINUTE)
    await scheduler.acquire(DATA)
    with pytest.raises(ExogenousError) as raised:
        await scheduler.acquire(DATA, deadline=future.now + SECOND)
    assert abs(raised.value.reset_at - (future.now + MINUTE)) < timedelta(milliseconds=1)


@pytest.mark.asyncio
async def test_the_scheduler_wall_clock_reaches_every_budget(monotonic, sink, cost, sleeps):
    future = FakeWall(datetime(2100, 1, 1, tzinfo=UTC))
    scheduler = (
        builder(future, monotonic, sink, cost)
        .declare_scope(DATA, [Limit(MINUTE, 1), Limit(DAY, 1)])
        .set_limit(MINUTE, 1, scope=DATA)
        .set_limit(DAY, 1, scope=DATA)
        .build()
    )

    for budget in scheduler._Scheduler__budgets[DATA]:
        assert budget.try_acquire()
        # The budget's own deadline check: on the real clock a 2100 deadline is decades off and it would wait.
        with pytest.raises(ExogenousError) as raised:
            await budget.acquire(deadline=future.now + SECOND)
        assert raised.value.reset_at > future.now
        assert raised.value.reset_at - future.now <= DAY + SECOND
    assert sleeps.slept == []


# RateBudget.wait_seconds: read-only, zero when a token is spendable.


def test_wait_seconds_is_zero_while_a_token_is_spendable_and_spends_nothing():
    clock = FakeMonotonic()
    # 2 a second, bucket 2, the default reserve min(2 / 2, 2 - 1) = 1 held back from backfill.
    budget = RateBudget('ALPACA', rate_per_sec=2.0, burst=2.0, clock=clock)

    assert budget.wait_seconds(RequestPriority.INTERACTIVE) == 0.0
    assert budget.wait_seconds(RequestPriority.BACKFILL) == 0.0
    assert budget.tokens == pytest.approx(2.0)

    assert budget.try_acquire()
    # One token left: interactive may spend it, backfill may not go under the reserve of 1.
    assert budget.wait_seconds(RequestPriority.INTERACTIVE) == 0.0
    assert budget.wait_seconds(RequestPriority.LIVE) == 0.0
    assert budget.wait_seconds(RequestPriority.BACKFILL) == pytest.approx(0.5)

    assert budget.try_acquire()
    assert budget.wait_seconds(RequestPriority.INTERACTIVE) == pytest.approx(0.5)
    assert budget.wait_seconds(RequestPriority.BACKFILL) == pytest.approx(1.0)
    assert budget.tokens == pytest.approx(0.0)

    clock.now += 0.5
    assert budget.wait_seconds(RequestPriority.INTERACTIVE) == 0.0
    assert budget.try_acquire()


def test_wait_seconds_counts_restart_debt():
    budget = RateBudget('ALPACA', rate_per_sec=1.0, burst=1.0, clock=FakeMonotonic())
    budget.charge(3)

    # -2 tokens: three seconds to get back to one whole token.
    assert budget.wait_seconds() == pytest.approx(3.0)


def test_wait_seconds_is_zero_when_unthrottled():
    budget = RateBudget('ALPACA', rate_per_sec=None)
    for _ in range(5):
        assert budget.try_acquire()

    assert all(budget.wait_seconds(priority) == 0.0 for priority in RequestPriority)


def test_a_page_size_below_one_is_refused(cost):
    with pytest.raises(SchedulerConfigError, match='page_size'):
        SchedulerBuilder().cost_function(cost, page_size=0)


# build() refusals.


def test_build_refuses_a_missing_part(wall, monotonic, sink, cost):
    def scoped(b: SchedulerBuilder) -> SchedulerBuilder:
        return b.declare_scope(DATA, [Limit(MINUTE, 200)]).set_limit(MINUTE, 200, scope=DATA)

    with pytest.raises(SchedulerConfigError, match='cost function'):
        scoped(SchedulerBuilder(clock=wall).credential_provider(provider()).on_expired(sink)).build()
    with pytest.raises(SchedulerConfigError, match='credential provider'):
        scoped(SchedulerBuilder(clock=wall).cost_function(cost, page_size=1).on_expired(sink)).build()
    with pytest.raises(SchedulerConfigError, match='expired sink'):
        scoped(SchedulerBuilder(clock=wall).cost_function(cost, page_size=1).credential_provider(provider())).build()
    with pytest.raises(SchedulerConfigError, match='quota scope is required'):
        builder(wall, monotonic, sink, cost).build()


def test_build_refuses_a_declared_scope_with_no_limit(wall, monotonic, sink, cost):
    scopes = (
        builder(wall, monotonic, sink, cost)
        .declare_scope(DATA, [Limit(MINUTE, 200)])
        .set_limit(MINUTE, 200, scope=DATA)
        .declare_scope(TRADING, [Limit(MINUTE, 200)])
    )

    with pytest.raises(SchedulerConfigError, match=f"'{TRADING}' has no limit set"):
        scopes.build()


# Credentials (addendum 1, owner 2026-10-01: the credential with the most quota left).


def test_from_env_reads_the_configured_key(monkeypatch):
    monkeypatch.setenv('TEST_SCHED_KEY', 'kid')
    monkeypatch.setenv('TEST_SCHED_SECRET', 'shh-value')

    (credential,) = StaticCredentialProvider.from_env('TEST_SCHED_KEY', 'TEST_SCHED_SECRET').credentials()

    assert credential == Credential(key_id='kid', secret='shh-value')


@pytest.mark.parametrize(('key', 'secret'), [('visible-key-id', None), (None, 'visible-secret'), ('', 'x')])
def test_from_env_refuses_a_missing_value_naming_the_variables_never_a_value(monkeypatch, key, secret):
    for name, value in (('TEST_SCHED_KEY', key), ('TEST_SCHED_SECRET', secret)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    with pytest.raises(SchedulerConfigError) as raised:
        StaticCredentialProvider.from_env('TEST_SCHED_KEY', 'TEST_SCHED_SECRET')

    message = str(raised.value)
    assert 'TEST_SCHED_KEY' in message
    assert 'TEST_SCHED_SECRET' in message
    assert 'visible' not in message


def test_a_credential_repr_hides_the_secret():
    shown = repr(Credential(key_id='kid', secret='do-not-print-me'))

    assert 'kid' in shown
    assert 'do-not-print-me' not in shown


def test_most_headroom_picks_the_largest_and_the_earliest_on_a_tie():
    a, b, c = (Credential(key_id=k, secret='s') for k in 'abc')
    left = {'a': 5.0, 'b': 9.0, 'c': 9.0}

    assert most_headroom(StaticCredentialProvider(a, b, c), lambda cr: left[cr.key_id]) is b


def test_an_empty_provider_is_refused():
    with pytest.raises(SchedulerConfigError):
        StaticCredentialProvider()

    class Empty:
        def credentials(self):
            return []

    with pytest.raises(SchedulerConfigError, match='no credentials'):
        most_headroom(Empty(), lambda _: 0.0)


def test_the_scheduler_chooses_the_provider_credential(wall, monotonic, sink, cost):
    first, second = Credential(key_id='first', secret='s'), Credential(key_id='second', secret='s')
    scheduler = (
        SchedulerBuilder(clock=wall)
        .budget_clock(monotonic)
        .cost_function(cost, page_size=1)
        .credential_provider(StaticCredentialProvider(first, second))
        .on_expired(sink)
        .declare_scope(DATA, [Limit(MINUTE, 200)])
        .set_limit(MINUTE, 200, scope=DATA)
        .build()
    )

    # Budgets are per scope until the account mapping exists, so every credential ties: the first wins.
    assert scheduler.credential(DATA) is first
    with pytest.raises(SchedulerConfigError):
        scheduler.credential('nowhere')
