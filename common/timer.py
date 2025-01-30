import time
from typing import Dict
from uuid import UUID, uuid4


class Timer:
    """Timer util to help measure time taken for a block of code to execute."""
    def __init__(self):
        self.start: Dict[UUID, float] = {}
        self._latest_key = None
        self._duration = 0

    def tick(self) -> UUID:
        """Start the timer.

        Returns:
            UUID: Key to identify the timer in case measuring concurrent tasks.
        """
        self._latest_key = uuid4()
        self.start[self._latest_key] = time.perf_counter()
        return self._latest_key

    def tock(self, key: UUID = None) -> float:
        """End the timer and return the duration.

        Args:
            key (UUID, optional): Key to identify a specific timer. Defaults to None.

        Returns:
            float: Elapsed time in seconds.
        """
        end = time.perf_counter()

        if key is None:
            key = self._latest_key

        start = self.start.pop(key)
        duration = end - start
        self._duration += duration
        return duration

    def total_time(self) -> float:
        """Get the total time elapsed.

        Returns:
            float: Total time elapsed in seconds.
        """
        return self._duration

    def reset(self):
        """Reset all timers, and running total."""
        self.start = {}
        self._duration = 0


def timeit(timer: Timer):
    """Decorator to measure the time taken for a function to execute

    Args:
        timer (Timer): Timer instance to use.
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            # Use the provided Timer instance
            key = timer.tick()
            result = func(*args, **kwargs)
            timer.tock(key)
            return result
        return wrapper
    return decorator
