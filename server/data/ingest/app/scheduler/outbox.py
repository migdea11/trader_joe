"""The outgoing buffer: a broker handle's attempts that the store has not yet acknowledged, bounded.

Ruled in tj-26jt9w (addendum 4 and the R2 ruling of addendum 6). Nothing is dropped. While the buffer is full the
handle takes nothing new from the queue (OutgoingBuffer.wait_for_space), and a report waiting to enter it holds
its worker until a slot frees. The bound caps what an ingest crash can lose.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime

from data.ingest.app.scheduler.queue import ScheduledRequest
from schemas.data_ingest.delivery import AttemptDelivery


@dataclass(eq=False)
class PendingReport:
    """One attempt's report, held until the store acknowledges it.

    Attributes:
        request (ScheduledRequest): The request the attempt belongs to, with the count it ran at.
        delivery (AttemptDelivery): The whole report. Redelivered unchanged, so the store sees the same
            (request_id, attempt_no) every time.
        attempt_no (int): The attempt's number.
        reset_at (datetime | None): When a rate limit the attempt met resets, if it named one.
        enqueued_at (datetime): When the report entered the buffer; the age of the oldest is what the alarm reads.
        redeliveries (int): How many times delivery has failed and been tried again.
    """

    request: ScheduledRequest
    delivery: AttemptDelivery
    attempt_no: int
    reset_at: datetime | None
    enqueued_at: datetime
    redeliveries: int = 0


class OutgoingBuffer:
    """A bounded, ordered set of unacknowledged reports."""

    def __init__(self, capacity: int) -> None:
        """Make an empty buffer.

        Args:
            capacity (int): The most unacknowledged reports it holds. At least 1.

        Raises:
            ValueError: If the capacity is below 1.
        """
        if capacity < 1:
            raise ValueError(f'the outgoing buffer needs a capacity of at least 1, got {capacity}')
        self.__capacity = capacity
        self.__reports: dict[int, PendingReport] = {}
        self.__changed = asyncio.Condition()

    @property
    def capacity(self) -> int:
        """int: The most unacknowledged reports the buffer holds."""
        return self.__capacity

    def __len__(self) -> int:
        return len(self.__reports)

    @property
    def full(self) -> bool:
        """bool: True when no slot is free."""
        return len(self.__reports) >= self.__capacity

    def oldest(self) -> PendingReport | None:
        """Give the report that has waited longest.

        Returns:
            PendingReport | None: The earliest to enter, or None when the buffer is empty.
        """
        return next(iter(self.__reports.values()), None)

    async def wait_for_space(self) -> None:
        """Wait until at least one slot is free. A worker calls this before it takes anything from the queue."""
        async with self.__changed:
            await self.__changed.wait_for(lambda: not self.full)

    async def put(self, report: PendingReport) -> None:
        """Add a report, waiting for a slot while the buffer is full.

        Args:
            report (PendingReport): The report to hold until it is acknowledged.
        """
        async with self.__changed:
            await self.__changed.wait_for(lambda: not self.full)
            self.__reports[id(report)] = report

    async def remove(self, report: PendingReport) -> None:
        """Free the report's slot once the store has acknowledged it.

        Args:
            report (PendingReport): A report this buffer holds. Removing one it does not hold does nothing.
        """
        async with self.__changed:
            self.__reports.pop(id(report), None)
            self.__changed.notify_all()
