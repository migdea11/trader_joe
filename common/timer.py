import time
from uuid import UUID, uuid4


class Timer:
    def __init__(self):
        self.start = {}
        self._latest_key = None
        self._duration = 0

    def tick(self):
        self._latest_key = uuid4()
        self.start[self._latest_key] = time.perf_counter()
        return self._latest_key

    def tock(self, key: UUID = None):
        end = time.perf_counter()

        if key is None:
            key = self._latest_key

        start = self.start.pop(key)
        duration = end - start
        self._duration += duration
        return duration

    def total_time(self):
        return self._duration

    def reset(self):
        self.start = {}
        self._duration = 0


def timeit(timer: Timer):
    # This is the decorator factory
    def decorator(func):
        # This is the actual decorator
        def wrapper(*args, **kwargs):
            # Use the provided Timer instance
            key = timer.tick()
            result = func(*args, **kwargs)
            timer.tock(key)
            return result
        return wrapper
    return decorator
