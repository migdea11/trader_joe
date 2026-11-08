"""Stand-ins for the Alpaca handle's scheduler, for tests that drive AlpacaRead directly (tj-7zjov1.20).

Since P11 every Alpaca vendor call is paced by the handle's Scheduler, under the quota scope ALPACA_DATA_SCOPE,
and nothing else: the environment-built RateBudget broker_api used to hold is gone. AlpacaRead therefore takes the
scheduler and the scope as required keywords. Tests that used to replace broker_api's budget with one that granted
every token and counted the asks now hand AlpacaRead a RecordingScheduler instead, which records the SCOPE as well
as the priority and the deadline, so a reader that paced its calls under the wrong scope is caught too.

Tests that need the real pacing arithmetic (a deadline the window cannot meet, the 429 fallback to the scope's
refill) build a real Scheduler with real_scheduler, through the production SchedulerBuilder.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from data.ingest.app.brokers.alpaca.handle import ALPACA_DATA_SCOPE
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler import Credential, Limit, Scheduler, SchedulerBuilder, StaticCredentialProvider


@dataclass(frozen=True)
class Acquire:
    """One ask of the scheduler for a vendor call.

    Attributes:
        scope (str): The quota scope the call was charged to.
        priority (RequestPriority): Its priority class.
        deadline (datetime | None): When the caller stopped waiting.
    """

    scope: str
    priority: RequestPriority
    deadline: datetime | None


@dataclass(frozen=True)
class FixedWindow:
    """A scope window with a fixed declared window, conforming to classify.RefillWindow.

    declared_window_seconds is what a 429 naming no reset waits (tj-7zjov1.20, 18ff3f5: the vendor's declared
    window, the scope's longest limit interval). full_refill_seconds, the bucket's refill, is kept so a classifier
    that read it instead is caught: the two differ by default.
    """

    declared_window_seconds: float
    full_refill_seconds: float = 1.0


class RecordingScheduler:
    """A scheduler that grants every call at once and records each ask, and every window it was asked for.

    Conforms by shape to the two members AlpacaRead uses: acquire and window.

    Args:
        declared_window_seconds (float): What window(scope).declared_window_seconds answers, the 429 fallback.
            Alpaca data's declared window, 60 s, by default.
    """

    def __init__(self, declared_window_seconds: float = 60.0) -> None:
        self.declared_window_seconds = declared_window_seconds
        self.acquires: list[Acquire] = []
        self.windows: list[str] = []

    async def acquire(
        self, scope: str, priority: RequestPriority = RequestPriority.INTERACTIVE, deadline: datetime | None = None
    ) -> None:
        self.acquires.append(Acquire(scope, priority, deadline))

    def window(self, scope: str) -> FixedWindow:
        self.windows.append(scope)
        return FixedWindow(self.declared_window_seconds)


def alpaca_reader(
    client=None,
    executor: ThreadPoolExecutor | None = None,
    clock: Callable[[], datetime] | None = None,
    *,
    scheduler=None,
    scope: str = ALPACA_DATA_SCOPE,
) -> AlpacaRead:
    """A real AlpacaRead paced by the given scheduler (a fresh RecordingScheduler when None).

    Args:
        client: The vendor client to inject, or None for the process's own.
        executor (ThreadPoolExecutor | None): The pool for the blocking SDK call, or None for the shared one.
        clock (Callable[[], datetime] | None): The reader's clock, or None for its default.
        scheduler: What paces the reader's vendor calls.
        scope (str): The quota scope the reader charges.

    Returns:
        AlpacaRead: The reader.
    """
    kwargs = {}
    if executor is not None:
        kwargs['executor_provider'] = lambda: executor
    if clock is not None:
        kwargs['clock'] = clock
    return AlpacaRead(
        client=client, scheduler=scheduler if scheduler is not None else RecordingScheduler(), scope=scope, **kwargs
    )


async def _no_expired_sink(_expired) -> None:
    raise AssertionError('no request should expire in this test')


def real_scheduler(
    limits: dict[str, Limit],
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    budget_clock: Callable[[], float] | None = None,
) -> Scheduler:
    """A real Scheduler with one scope per entry, each at its limit (declared as its own ceiling).

    Args:
        limits (dict[str, Limit]): The limit of each quota scope.
        clock (Callable[[], datetime]): The scheduler's wall clock.
        budget_clock (Callable[[], float] | None): The monotonic clock its budgets refill on, or the default.

    Returns:
        Scheduler: The scheduler, with an empty queue.
    """
    builder = SchedulerBuilder(clock=clock)
    for scope, limit in limits.items():
        builder.declare_scope(scope, [limit]).set_limit(limit.interval, limit.count, scope=scope)
    builder.cost_function(lambda _request, _page: 1, page_size=1)
    builder.credential_provider(StaticCredentialProvider(Credential(key_id='test-key', secret='test-secret')))
    builder.on_expired(_no_expired_sink)
    if budget_clock is not None:
        builder.budget_clock(budget_clock)
    return builder.build()


# A limit whose bucket holds one token and refills one per second: Limit.burst is max(1, min(1, 6)) = 1 and
# Limit.refill_per_sec is (60 - 1 + 1) / 60 = 1, so its full refill from empty is exactly one second. Its declared
# window (the interval) is sixty seconds, so the two answers a 429 fallback could read differ by sixty times.
ONE_PER_SECOND = Limit(timedelta(minutes=1), 60)
# One token a minute over an hour: burst 1, refill 60 / 3600 per second (a full refill of sixty seconds), and a
# declared window of 3600 seconds.
ONE_PER_MINUTE_OVER_AN_HOUR = Limit(timedelta(hours=1), 60)
