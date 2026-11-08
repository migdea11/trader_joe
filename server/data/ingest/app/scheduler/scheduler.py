"""The scheduler a broker handle builds for itself: quota limits, a queue, a cost function, a credential provider.

Ruled in tj-26jt9w (addendum 1 item 1, addenda 2, 3 and 6). One module serves every handle; each builds its own
instance through SchedulerBuilder. PR 5 behaviour:

- Rate limiting is the existing RateBudget, one per (quota scope, interval), behind set_limit.
- The queue is FIFO, null ready-by last. Admission admits everything.
- Expiry is checked ONLY when a request is taken off the queue (take); a request waiting in the queue is not
  swept. An expired request is never returned: it goes to the expired sink, and take moves on. A null ready-by
  never expires. A retry re-enters through submit and is checked again at its next take.
- The cost function is called only by charge_restart.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Self

from common.errors.vocabulary import ExogenousError, Reason
from common.logging import get_logger
from data.ingest.app.brokers.rate_budget import RateBudget, RequestPriority
from data.ingest.app.scheduler.credentials import Credential, CredentialProvider, most_headroom
from data.ingest.app.scheduler.limits import Limit, SchedulerConfigError
from data.ingest.app.scheduler.queue import ExpiredRequest, FifoQueue, ScheduledRequest


log = get_logger(__name__)

# Estimated vendor calls for a request, given the handle's page size. A conservative (high) estimate.
CostFunction = Callable[[ScheduledRequest, int], int]
ExpiredSink = Callable[[ExpiredRequest], Awaitable[None]]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Scheduler:
    """Built by SchedulerBuilder; not constructed directly."""

    def __init__(
        self,
        budgets: dict[str, list[RateBudget]],
        cost_function: CostFunction,
        page_size: int,
        credential_provider: CredentialProvider,
        expired_sink: ExpiredSink,
        clock: Clock,
    ) -> None:
        self.__budgets = budgets
        self.__cost_function = cost_function
        self.__page_size = page_size
        self.__credential_provider = credential_provider
        self.__expired_sink = expired_sink
        self.__clock = clock
        self.__queue = FifoQueue()

    @property
    def queued(self) -> int:
        """Count the requests waiting in the queue.

        Returns:
            int: Dated plus undated requests not yet taken.
        """
        return len(self.__queue)

    async def submit(self, request: ScheduledRequest) -> None:
        """Admit a request, or a retry re-entering, at the tail of its tier.

        Args:
            request (ScheduledRequest): The request to queue.

        Raises:
            SchedulerConfigError: If the request names a quota scope this scheduler has no limits for.
        """
        self.__scope(request.quota_scope)
        await self.__queue.put(request)

    async def take(self) -> ScheduledRequest:
        """Take the next request that may still go to the vendor, waiting while there is none.

        Each request popped has its ready-by checked against the clock now. An expired one is handed to the
        expired sink and skipped; it is never returned.

        Returns:
            ScheduledRequest: A request whose ready-by is null or has not passed.
        """
        while True:
            request = await self.__queue.get()
            now = self.__clock()
            if request.ready_by is not None and now > request.ready_by:
                await self.__expired_sink(ExpiredRequest(request=request, checked_at=now))
                continue
            return request

    async def acquire(
        self, scope: str, priority: RequestPriority = RequestPriority.INTERACTIVE, deadline: datetime | None = None
    ) -> None:
        """Wait until every limit of a scope admits one vendor call, then spend it from all of them at once.

        All-or-nothing: a token is spent from every budget of the scope at one instant, or from none. While any
        budget has to wait, nothing is held, so the call is placed at the instant every limit allows it.

        Args:
            scope (str): Quota scope of the call.
            priority (RequestPriority): Priority class; bulk keeps its reserve.
            deadline (datetime | None): When the caller stops waiting; fails fast rather than wait past it.

        Raises:
            ExogenousError: With the reason RATE_BUDGET, when the wait would pass the deadline. Nothing is spent.
            SchedulerConfigError: If the scope is unknown.
        """
        budgets = self.__scope(scope)
        while True:
            wait = max(budget.wait_seconds(priority) for budget in budgets)
            if wait <= 0:
                # No await between the check and the spend, so no other caller can take a token in between.
                for budget in budgets:
                    budget.try_acquire(priority)
                return
            if deadline is not None:
                now = self.__clock()
                reset_at = now + timedelta(seconds=wait)
                if reset_at > deadline:
                    raise ExogenousError(
                        Reason.RATE_BUDGET,
                        f'The {scope} rate budget has no token for {priority.name} before the request '
                        f'deadline; the next is due in {wait:.1f}s.',
                        metadata={'vendor': scope},
                        reset_at=reset_at,
                        clock=self.__clock,
                    )
            await asyncio.sleep(wait)

    def charge_restart(self, request: ScheduledRequest) -> int:
        """Charge a request's estimated cost to its scope's current window, as after a restart.

        The only caller of the cost function in PR 5 (tj-26jt9w addendum 5).

        Args:
            request (ScheduledRequest): A request the previous process may have sent.

        Returns:
            int: The calls charged, at least 1.
        """
        cost = max(1, self.__cost_function(request, self.__page_size))
        for budget in self.__scope(request.quota_scope):
            budget.charge(cost)
        return cost

    def credential(self, scope: str) -> Credential:
        """Choose the credential to call with: the one with the most headroom in the scope.

        Budgets are per scope until the account mapping exists, so every credential has the same headroom
        today and the choice reduces to the provider's first.

        Args:
            scope (str): Quota scope of the call.

        Returns:
            Credential: The chosen credential.
        """
        budgets = self.__scope(scope)
        left = min(budget.tokens for budget in budgets)
        return most_headroom(self.__credential_provider, lambda _credential: left)

    def __scope(self, scope: str) -> list[RateBudget]:
        try:
            return self.__budgets[scope]
        except KeyError:
            raise SchedulerConfigError(f'No limits are set for quota scope {scope!r}') from None


class SchedulerBuilder:
    """Fluent builder for a Scheduler.

    Usage: declare each quota scope with the broker's hard ceilings, set limits at or under them, register the
    cost function, the credential provider and the expired sink, then build().
    """

    def __init__(self, clock: Clock = _utc_now) -> None:
        self.__clock = clock
        self.__ceilings: dict[str, dict[timedelta, int]] = {}
        self.__limits: dict[str, dict[timedelta, Limit]] = {}
        self.__cost_function: CostFunction | None = None
        self.__page_size: int | None = None
        self.__credential_provider: CredentialProvider | None = None
        self.__expired_sink: ExpiredSink | None = None
        self.__budget_clock: Callable[[], float] | None = None

    def declare_scope(self, scope: str, ceilings: list[Limit]) -> Self:
        """Declare a quota scope with the broker's own hard-coded ceilings.

        Args:
            scope (str): Quota scope name, such as one vendor API's budget.
            ceilings (list[Limit]): The most the broker allows per interval. Configuration may only go lower.

        Returns:
            Self: The builder.

        Raises:
            SchedulerConfigError: If the scope is declared twice, or no ceiling is given.
        """
        if scope in self.__ceilings:
            raise SchedulerConfigError(f'Quota scope {scope!r} is declared twice')
        if not ceilings:
            raise SchedulerConfigError(f'Quota scope {scope!r} needs at least one ceiling')
        self.__ceilings[scope] = {ceiling.interval: ceiling.count for ceiling in ceilings}
        self.__limits[scope] = {}
        return self

    def set_limit(self, interval: timedelta, count: int, *, scope: str) -> Self:
        """Limit a scope to `count` calls per `interval`. Repeatable, one per interval.

        Args:
            interval (timedelta): The window. Must match one of the scope's ceilings.
            count (int): Calls allowed in it.
            scope (str): Quota scope the limit applies to.

        Returns:
            Self: The builder.

        Raises:
            SchedulerConfigError: If the scope is undeclared, the interval has no ceiling, or the count is above it.
        """
        limit = Limit(interval, count)
        if scope not in self.__ceilings:
            raise SchedulerConfigError(f'Quota scope {scope!r} is not declared')
        ceiling = self.__ceilings[scope].get(interval)
        if ceiling is None:
            raise SchedulerConfigError(f'Quota scope {scope!r} has no ceiling for the interval {interval}')
        if count > ceiling:
            raise SchedulerConfigError(
                f'Limit of {count} per {interval} on {scope!r} is above the broker ceiling of {ceiling}'
            )
        self.__limits[scope][interval] = limit
        return self

    def cost_function(self, function: CostFunction, *, page_size: int) -> Self:
        """Register the broker's conservative cost estimate and the page size it assumes.

        Args:
            function (CostFunction): Estimated vendor calls for a request given the page size.
            page_size (int): Rows per vendor page. Positive.

        Returns:
            Self: The builder.

        Raises:
            SchedulerConfigError: If page_size is below 1.
        """
        if page_size < 1:
            raise SchedulerConfigError(f'page_size must be at least 1, got {page_size}')
        self.__cost_function = function
        self.__page_size = page_size
        return self

    def credential_provider(self, provider: CredentialProvider) -> Self:
        """Register where credentials come from.

        Args:
            provider (CredentialProvider): The provider.

        Returns:
            Self: The builder.
        """
        self.__credential_provider = provider
        return self

    def on_expired(self, sink: ExpiredSink) -> Self:
        """Register where an expired request goes: the return path to the store, wired by the handle.

        Args:
            sink (ExpiredSink): Awaited with each ExpiredRequest.

        Returns:
            Self: The builder.
        """
        self.__expired_sink = sink
        return self

    def budget_clock(self, clock: Callable[[], float]) -> Self:
        """Replace the monotonic clock the rate budgets refill on, for tests.

        Args:
            clock (Callable[[], float]): Monotonic seconds.

        Returns:
            Self: The builder.
        """
        self.__budget_clock = clock
        return self

    def build(self) -> Scheduler:
        """Build the scheduler.

        Returns:
            Scheduler: A scheduler with an empty queue.

        Raises:
            SchedulerConfigError: If a part is missing, or a declared scope has no limit set. A scope with no
                limit would run unthrottled by omission, so it is refused rather than defaulted to its ceiling.
        """
        if self.__cost_function is None or self.__page_size is None:
            raise SchedulerConfigError('A cost function is required: register one with cost_function()')
        if self.__credential_provider is None:
            raise SchedulerConfigError('A credential provider is required')
        if self.__expired_sink is None:
            raise SchedulerConfigError('An expired sink is required: expired requests must reach the return path')
        if not self.__ceilings:
            raise SchedulerConfigError('At least one quota scope is required')

        budget_args: dict[str, Any] = {'wall_clock': self.__clock}
        if self.__budget_clock is not None:
            budget_args['clock'] = self.__budget_clock
        budgets: dict[str, list[RateBudget]] = {}
        for scope, limits in self.__limits.items():
            if not limits:
                raise SchedulerConfigError(f'Quota scope {scope!r} has no limit set')
            # Contract: for Limit(T, N), no half-open window [t, t+T) holds more than N spent tokens (calls plus
            # charged restart cost), from any starting balance. A bucket of size B refilling at r spends at most
            # B + r*T in a window, so B = max(1, min(N per second, N / 10)) and r = (N - B + 1) / T keep it at N.
            # Limit.burst and Limit.refill_per_sec hold the formula.
            budgets[scope] = [
                RateBudget(
                    f'{scope}/{limit.interval}', rate_per_sec=limit.refill_per_sec, burst=limit.burst, **budget_args
                )
                for limit in limits.values()
            ]
        return Scheduler(
            budgets,
            self.__cost_function,
            self.__page_size,
            self.__credential_provider,
            self.__expired_sink,
            self.__clock,
        )
