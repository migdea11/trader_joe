"""The retry budget and its waits: bounded exponential backoff for temporary failures, the reset for a rate limit.

Ruled in tj-26jt9w (addendum 1 item 4, addendum 4). Ingest holds the BUDGET: how many attempts a request may
make and how long to wait between them. The store holds the COUNT (the durable attempt number) and the verdict,
so a request recovered after a restart resumes from the store's count and a restart never resets the budget.
"""

import math
from dataclasses import dataclass
from datetime import datetime


# The attempts one request gets, the first included, unless a handle's deployment setting lowers or raises it.
DEFAULT_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class RetryPolicy:
    """How many attempts a request gets and how long to wait between them.

    Attributes:
        max_attempts (int): The attempts one request may make in all, the first included. The attempt numbered
            max_attempts is the last: ingest sets last_attempt on its report when it fails.
        base_delay_s (float): The wait after a failed first attempt, in seconds.
        max_delay_s (float): The most a backoff ever waits, in seconds.
        factor (float): What the wait is multiplied by after each further failure. At least 1.
        max_reset_wait_s (float): The longest a rate-limited attempt waits for a vendor's reset, in seconds. A
            reset further off than this is waited for in part and the next attempt finds the limit still on.
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0
    factor: float = 2.0
    max_reset_wait_s: float = 3600.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f'max_attempts must be at least 1, got {self.max_attempts}')
        for name in ('base_delay_s', 'max_delay_s', 'max_reset_wait_s'):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f'{name} must be a finite, non-negative number of seconds, got {value!r}')
        if not math.isfinite(self.factor) or self.factor < 1:
            raise ValueError(f'factor must be at least 1, got {self.factor!r}')

    def is_last(self, attempt_no: int) -> bool:
        """Report whether an attempt spends the budget.

        Args:
            attempt_no (int): The attempt's number, counted from 1 by the store.

        Returns:
            bool: True when no further attempt is allowed after this one.
        """
        return attempt_no >= self.max_attempts

    def backoff(self, attempt_no: int) -> float:
        """Give the wait after a failed attempt: base * factor ** (attempt_no - 1), never above the cap.

        Args:
            attempt_no (int): The attempt that failed, counted from 1.

        Returns:
            float: Seconds to wait before the next attempt.
        """
        try:
            delay = self.base_delay_s * self.factor ** max(attempt_no - 1, 0)
        except OverflowError:
            return self.max_delay_s
        return min(delay, self.max_delay_s)

    def reset_wait(self, reset_at: datetime, now: datetime) -> float:
        """Give the wait for a rate limit to reset: until reset_at, never negative, never above the cap.

        Args:
            reset_at (datetime): When the vendor's window resets, timezone aware.
            now (datetime): The instant now, timezone aware.

        Returns:
            float: Seconds to wait before the next attempt.
        """
        return min(max((reset_at - now).total_seconds(), 0.0), self.max_reset_wait_s)
