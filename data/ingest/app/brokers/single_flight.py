import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import TypeVar

from common.logging import get_logger


log = get_logger(__name__)

T = TypeVar('T')


class SingleFlight:
    """Collapse concurrent identical calls onto one in-flight execution.

    This is deliberately NOT a cache (tj-84ty47 section 6). Nothing is retained once a
    call finishes: the entry is dropped BEFORE the result is published, so a caller
    arriving after completion always starts a fresh call. That is why there is no TTL,
    no eviction policy and no memory bound to choose -- the map holds one entry per call
    currently in progress and nothing else.
    """

    def __init__(self) -> None:
        self.__in_flight: dict[Hashable, asyncio.Future] = {}

    @property
    def in_flight(self) -> int:
        """Count the calls currently in progress.

        Returns:
            int: Number of live entries; zero whenever nothing is running.
        """
        return len(self.__in_flight)

    async def run(self, key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
        """Run the call for a key, or attach to the one already running for it.

        Args:
            key (Hashable): Content key for the call. Callers sharing a key share one answer.
            factory (Callable[[], Awaitable[T]]): Builds the coroutine to run. Only the leader calls it.

        Returns:
            T: The leader's result, shared by every caller that attached to it.
        """
        leader = self.__in_flight.get(key)
        if leader is not None:
            log.debug(f'single-flight attach: {key}')
            # Shielded: a follower that is cancelled must not cancel the shared call.
            return await asyncio.shield(leader)

        # Nothing awaits between the lookup above and this insert, so on one event loop
        # exactly one caller can become the leader for a given key.
        leader = asyncio.get_running_loop().create_future()
        self.__in_flight[key] = leader
        try:
            result = await factory()
        except asyncio.CancelledError:
            self.__release(key, leader)
            leader.cancel()
            raise
        except BaseException as e:
            self.__release(key, leader)
            leader.set_exception(e)
            # Mark the exception retrieved: every follower may have gone away, and an
            # unread future exception is reported by asyncio as an unhandled error.
            leader.exception()
            raise
        else:
            self.__release(key, leader)
            leader.set_result(result)
            return result

    def __release(self, key: Hashable, leader: asyncio.Future) -> None:
        # Released before the result is published, so a caller arriving after completion
        # starts a new call rather than attaching to a finished one -- that is the whole
        # difference between a single-flight guard and a one-shot cache.
        if self.__in_flight.get(key) is leader:
            del self.__in_flight[key]
