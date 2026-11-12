"""Quota limits: how many vendor calls a quota scope allows in an interval, and the ceiling above which none may go.

Caps exist only to keep inside the broker's own limits (tj-26jt9w addendum 1 item 1). The broker states its
documented ceiling in code; configuration may lower a limit and never raise it.
"""

from dataclasses import dataclass
from datetime import timedelta


class SchedulerConfigError(ValueError):
    """A scheduler was configured in a way the module refuses: a limit above its ceiling, an unknown scope, a missing part."""


@dataclass(frozen=True)
class Limit:
    """At most `count` vendor calls in any `interval`.

    Attributes:
        interval (timedelta): Window the count applies to. Positive.
        count (int): Calls allowed in the window. Positive.
    """

    interval: timedelta
    count: int

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0):
            raise SchedulerConfigError(f'A limit interval must be positive, got {self.interval}')
        if self.count < 1:
            raise SchedulerConfigError(f'A limit count must be at least 1, got {self.count}')

    @property
    def rate_per_sec(self) -> float:
        """Report the steady refill rate this limit allows.

        Returns:
            float: Count divided by the interval in seconds.
        """
        return self.count / self.interval.total_seconds()

    @property
    def burst(self) -> float:
        """Report the bucket size that keeps this limit's window contract: max(1, min(count per second, count / 10)).

        Returns:
            float: Tokens the bucket holds when full; never below one whole token.
        """
        return max(1.0, min(self.rate_per_sec, self.count / 10))

    @property
    def refill_per_sec(self) -> float:
        """Report the refill rate that keeps this limit's window contract: (count - burst + 1) / interval seconds.

        A bucket of size B refilling at rate r spends at most B + r*T in a window of length T. Choosing
        r = (N - B + 1) / T makes that N + 1 less a sliver, and tokens are whole, so no half-open window
        [t, t+T) holds more than N spent tokens.

        Returns:
            float: Tokens added per second.
        """
        return (self.count - self.burst + 1) / self.interval.total_seconds()
