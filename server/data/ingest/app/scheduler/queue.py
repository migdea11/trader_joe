"""The FIFO queue between the scheduler and a broker handle's worker, and the request it carries.

Ruled in tj-26jt9w addendum 2: requests that carry a ready-by in arrival order, then requests with a null
ready-by in arrival order. Admission admits everything; there is no priority beyond that order.
"""

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from data.ingest.app.scheduler.limits import SchedulerConfigError


class FailureCause(StrEnum):
    """Why the scheduler itself failed a request, before any vendor call."""

    EXPIRED = 'expired'


@dataclass(frozen=True)
class ScheduledRequest:
    """One unit of work for a broker handle.

    Attributes:
        request_id (str): Identifies the request across retries.
        quota_scope (str): The quota scope the request's vendor calls draw from.
        payload (Any): What the handle executes; opaque to the scheduler.
        ready_by (datetime | None): Timezone aware instant after which the request is not sent to the vendor,
            or None for no deadline: such a request sorts after every dated one and never expires.
        client_label (str | None): Who asked. Carried and unused in PR 5.
    """

    request_id: str
    quota_scope: str
    payload: Any
    ready_by: datetime | None = None
    client_label: str | None = None

    def __post_init__(self) -> None:
        if self.ready_by is not None and self.ready_by.tzinfo is None:
            raise SchedulerConfigError(f'ready_by must be timezone aware (request {self.request_id})')


@dataclass(frozen=True)
class ExpiredRequest:
    """A request whose ready-by had passed when it was taken off the queue; it made no vendor call.

    Attributes:
        request (ScheduledRequest): The request that expired.
        checked_at (datetime): The instant the dequeue check ran.
        cause (FailureCause): Always EXPIRED.
    """

    request: ScheduledRequest
    checked_at: datetime
    cause: FailureCause = FailureCause.EXPIRED


class FifoQueue:
    """Two arrival-ordered tiers: dated requests first, then undated ones. Unbounded; admits everything."""

    def __init__(self) -> None:
        self.__dated: deque[ScheduledRequest] = deque()
        self.__undated: deque[ScheduledRequest] = deque()
        self.__available = asyncio.Condition()

    def __len__(self) -> int:
        return len(self.__dated) + len(self.__undated)

    async def put(self, request: ScheduledRequest) -> None:
        """Add a request at the tail of its tier. A retry re-entering the queue goes through here too.

        Args:
            request (ScheduledRequest): The request to queue.
        """
        async with self.__available:
            (self.__undated if request.ready_by is None else self.__dated).append(request)
            self.__available.notify()

    async def get(self) -> ScheduledRequest:
        """Remove and return the next request, waiting while the queue is empty.

        Returns:
            ScheduledRequest: The oldest dated request, else the oldest undated one.
        """
        async with self.__available:
            await self.__available.wait_for(lambda: len(self) > 0)
            return self.__dated.popleft() if self.__dated else self.__undated.popleft()
