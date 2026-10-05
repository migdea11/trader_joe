import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import IntEnum

from common.enums.data_stock import DataSource, UpdateType
from common.environment import get_env_var
from common.errors.vocabulary import ExogenousError, Reason
from common.logging import get_logger


log = get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RequestPriority(IntEnum):
    """Priority class of a vendor request. Lower is served first under contention."""

    LIVE = 1
    INTERACTIVE = 2
    BACKFILL = 3


def priority_for_update_type(update_type: UpdateType) -> RequestPriority:
    """Map a request's update type onto a priority class.

    PROVISIONAL. Nothing in the contract yet says "this is catch-up", so this reads the
    only signal the request carries: a streamed feed is live, a scheduled end-of-day pull
    is interactive, and a one-shot historical pull is the bulk history that must yield.
    Ledger-driven catch-up (tj-84ty47 section 3) will pass its priority explicitly rather
    than have it inferred here.

    Args:
        update_type (UpdateType): How the requested dataset is kept up to date.

    Returns:
        RequestPriority: Priority class for the vendor calls serving that request.
    """
    match update_type:
        case UpdateType.STREAM:
            return RequestPriority.LIVE
        case UpdateType.STATIC:
            return RequestPriority.BACKFILL
        case _:
            return RequestPriority.INTERACTIVE


class RateBudget:
    """Per-vendor token bucket that keeps backfill off the live path.

    THE PRIORITY SPLIT IS THE POINT (tj-84ty47 section 5): backfill draws only what live
    and interactive calls leave. It does that by refusing to spend the last `reserve`
    tokens, so a live call always finds headroom no matter how much history is being
    pulled behind it. A long outage then degrades -- live resumes at once, history
    trickles at whatever rate is spare -- instead of thrashing against the vendor's limit.

    This belongs in the adapter layer rather than in a generic HTTP wrapper precisely
    because a generic wrapper cannot tell the two kinds of call apart.
    """

    def __init__(
        self,
        vendor: str,
        rate_per_sec: float | None,
        burst: float | None = None,
        backfill_reserve: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.__vendor = vendor
        self.__rate_per_sec = rate_per_sec
        # One second of budget, so a burst never exceeds what the vendor would allow in the
        # window its limit is documented over -- but never below one whole token, or a slow
        # vendor (IBKR's 1 req/5s) would have a bucket no call could ever draw from.
        self.__burst = max(burst if burst is not None else (rate_per_sec or 0.0), 1.0)
        # Half the bucket held back from backfill unless configured otherwise, capped so
        # that one token always remains reachable -- a reserve of the whole bucket would
        # starve backfill outright rather than deprioritise it.
        reserve = backfill_reserve if backfill_reserve is not None else self.__burst / 2
        self.__reserve = min(max(reserve, 0.0), self.__burst - 1.0)
        self.__clock = clock
        # Wall time, for the one comparison a monotonic clock cannot make: a caller's deadline is an
        # instant, and so is the reset_at an error reports.
        self.__wall_clock = wall_clock
        self.__tokens = self.__burst
        self.__updated = clock()

        if rate_per_sec is None:
            log.warning(f'No rate budget configured for {vendor}; vendor calls are unthrottled')

    @classmethod
    def from_env(cls, source: DataSource, clock: Callable[[], float] = time.monotonic) -> 'RateBudget':
        """Build a vendor's budget from its environment configuration.

        The numbers are configured, never hard-coded: tj-wss8a2 documents Questrade REST at
        30 req/s and IBKR at 10 req/s per username, while Alpaca's figure is still
        unconfirmed (tj-3mk3u5.2). An unset rate leaves the vendor unthrottled rather than
        throttled at a number nobody has verified.

        Args:
            source (DataSource): Vendor whose budget to build.
            clock (Callable[[], float]): Monotonic clock, injectable for tests.

        Returns:
            RateBudget: Budget for that vendor.
        """
        return cls(
            vendor=source.value,
            rate_per_sec=get_env_var(f'{source.value}_RATE_LIMIT_PER_SEC', cast_type=float),
            burst=get_env_var(f'{source.value}_RATE_BURST', cast_type=float),
            backfill_reserve=get_env_var(f'{source.value}_BACKFILL_RESERVE', cast_type=float),
            clock=clock,
        )

    @property
    def tokens(self) -> float:
        """Report the tokens available right now.

        Returns:
            float: Token count after accounting for the time since the last draw.
        """
        self.__refill()
        return self.__tokens

    @property
    def full_refill_seconds(self) -> float:
        """Report how long an empty bucket takes to fill completely: this budget's own model of the window.

        What a vendor's rate-limit answer is turned into when it names no reset of its own (the
        fallback for a 429 with neither a reset nor a Retry-After header). Measured from empty, not
        from the tokens held now: a vendor that rate-limited a call has told us its window is
        spent, whatever this bucket believes.

        Returns:
            float: Burst divided by the refill rate, in seconds. 0.0 when no rate is configured:
                an unthrottled budget models no window, and a number nobody has verified is not
                invented for it (see from_env).
        """
        if self.__rate_per_sec is None:
            return 0.0
        return self.__burst / self.__rate_per_sec

    def try_acquire(self, priority: RequestPriority = RequestPriority.INTERACTIVE) -> bool:
        """Take one token if this priority class is allowed to; never waits.

        Args:
            priority (RequestPriority): Priority class of the call.

        Returns:
            bool: True if a token was taken and the call may proceed.
        """
        if self.__rate_per_sec is None:
            return True

        self.__refill()
        # Nothing awaits between this check and the decrement, so no other caller can take
        # the token in between.
        if self.__tokens - self.__floor(priority) >= 1.0:
            self.__tokens -= 1.0
            return True
        return False

    async def acquire(
        self, priority: RequestPriority = RequestPriority.INTERACTIVE, deadline: datetime | None = None
    ) -> None:
        """Wait until this priority class may spend a token, then spend it.

        Without a deadline this waits as long as it takes. With one it FAILS FAST (ADR tj-fa1rpu U4 = A):
        when the wait __wait_for() computes would end after the deadline it raises at once rather than
        sleep past it, handing the caller the number the budget already knew. The check runs on every
        pass, against the time then, so a token taken by another caller cannot stretch the wait past
        the deadline.

        Args:
            priority (RequestPriority): Priority class of the call.
            deadline (datetime | None): When the caller stops waiting, timezone aware, or None for
                no bound. A token available at once is spent whatever the deadline says.

        Raises:
            ExogenousError: With the reason RATE_BUDGET and a reset_at of now plus the wait, if the
                wait would pass the deadline.
        """
        while not self.try_acquire(priority):
            wait = self.__wait_for(priority)
            if deadline is not None:
                now = self.__wall_clock()
                reset_at = now + timedelta(seconds=wait)
                if reset_at > deadline:
                    raise ExogenousError(
                        Reason.RATE_BUDGET,
                        f'The {self.__vendor} rate budget has no token for {priority.name} before the request '
                        f'deadline; the next is due in {wait:.1f}s.',
                        metadata={'vendor': self.__vendor},
                        reset_at=reset_at,
                        clock=self.__wall_clock,
                    )
            log.debug(f'{self.__vendor} rate budget exhausted for {priority.name}, waiting {wait:.3f}s')
            await asyncio.sleep(wait)

    def __floor(self, priority: RequestPriority) -> float:
        # The reserve is spendable by everything except backfill. That one line is the
        # whole anti-thrash rule.
        return self.__reserve if priority is RequestPriority.BACKFILL else 0.0

    def __wait_for(self, priority: RequestPriority) -> float:
        deficit = 1.0 + self.__floor(priority) - self.__tokens
        return max(deficit, 0.0) / self.__rate_per_sec

    def __refill(self) -> None:
        if self.__rate_per_sec is None:
            return

        now = self.__clock()
        elapsed = now - self.__updated
        if elapsed <= 0:
            return
        self.__updated = now
        self.__tokens = min(self.__burst, self.__tokens + elapsed * self.__rate_per_sec)
