"""THE IN-PROCESS EVENT HUB behind WS /ui/v1/events (tj-grna9p.73, ADR tj-grna9p.5).

EVENTS ARE NOTIFICATIONS, NOT DATA. A client treats every event as a hint to reload over REST, so nothing
here is the only copy of anything: the state an event announces is already committed. That is also why the
hub may drop what it cannot deliver.

PUBLISH ONLY AFTER A COMMIT, never from inside a transaction: an event that announces a write which then
rolls back would be a lie the client acts on. Every publisher in this service calls publish() once its own
commit has returned.

THE HUB CARRIES DOMAIN EVENTS, not wire messages. data/store/app may not import trader_joe.proto (ruff
TID251); the router maps each event to the UiEvent message (routers/data_store/ui_mapping.py). server_time is
set by the router when the frame is SENT, not here.

ONE HUB PER PROCESS. It lives in this process's memory, so it is correct only while data_store runs ONE
worker: with more, an event published in one worker would reach only the clients connected to that worker.
Scaling out would move fan-out to Postgres LISTEN/NOTIFY (each worker listens and republishes into its own
hub). The run recorder's startup sweep makes the same single-process assumption.

A SLOW CLIENT IS DROPPED, NEVER WAITED FOR. Each subscriber has a bounded queue and publish() uses
put_nowait: a full queue marks that subscriber dropped and publish() carries on, so one stalled socket can
never delay the publisher, which is a fetch's recorder. The router closes a dropped client with 1013 and the
client reloads over REST on reconnect.
"""

import asyncio
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from common.environment import get_env_var
from common.logging import get_logger
from data.store.app.database.crud.ingest_run import IngestRunRecord


log = get_logger(__name__)

# Events a subscriber may hold undelivered before it is dropped.
QUEUE_SIZE_ENV: Final = 'EVENTS_CLIENT_QUEUE_SIZE'
DEFAULT_QUEUE_SIZE: Final = 256


class DatasetChange(StrEnum):
    """What happened to a dataset. Members mirror the wire's DatasetChange without its prefix."""

    CREATED = 'CREATED'
    UPDATED = 'UPDATED'
    RETIRED = 'RETIRED'
    RESUMED = 'RESUMED'
    DELETED = 'DELETED'


@dataclass(frozen=True)
class DatasetChanged:
    """A dataset was written, or deleted. The client reloads the catalog."""

    id: uuid.UUID
    change: DatasetChange


@dataclass(frozen=True)
class IngestRunChanged:
    """A run started, was acknowledged, or finished. run is the row after the change."""

    run: IngestRunRecord


@dataclass(frozen=True)
class RequestChanged:
    """A request-log entry changed state. Defined now; no request log exists yet.

    Its first publisher will be the scheduler's request log (tj-pd41mt).

    entry is typed Any until the request log defines the entry's domain type.
    """

    entry: Any


@dataclass(frozen=True)
class ReaperPendingChanged:
    """The expiry reaper has deletions waiting, or no longer does. First published in phase 3."""


DomainEvent = DatasetChanged | IngestRunChanged | RequestChanged | ReaperPendingChanged


class _Dropped:
    """The sentinel a dropped subscriber's queue ends with."""


_DROPPED: Final = _Dropped()


class Subscription:
    """One client's bounded queue of events."""

    def __init__(self, hub: 'EventHub', size: int):
        self._hub = hub
        self._queue: asyncio.Queue[DomainEvent | _Dropped] = asyncio.Queue(maxsize=size)
        self.dropped = False

    async def get(self) -> DomainEvent | None:
        """The next event, waiting for one. None means this subscriber was dropped for being slow.

        Returns:
            DomainEvent | None: The event, or None once the subscriber has been dropped.
        """
        item = await self._queue.get()
        if isinstance(item, _Dropped):
            return None
        return item

    def close(self) -> None:
        """Leave the hub. Idempotent; the router calls it in a finally."""
        self._hub.unsubscribe(self)

    def _offer(self, event: DomainEvent) -> None:
        """Queue an event without waiting; a full queue drops this subscriber."""
        if self.dropped:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped = True
            # Empty the backlog and end with the sentinel, so a consumer parked on get() wakes at once
            # and a consumer behind a full queue stops reading stale events.
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(_DROPPED)


class EventHub:
    """Fan-out of domain events to this process's connected clients."""

    def __init__(self, queue_size: int | None = None):
        self._queue_size = (
            queue_size
            if queue_size is not None
            else get_env_var(QUEUE_SIZE_ENV, default=DEFAULT_QUEUE_SIZE, cast_type=int)
        )
        self._subscribers: set[Subscription] = set()

    @property
    def subscriber_count(self) -> int:
        """int: Clients currently subscribed."""
        return len(self._subscribers)

    def subscribe(self) -> Subscription:
        """Add a client and return its queue."""
        subscription = Subscription(self, self._queue_size)
        self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a client. Removing one that is already gone is not an error."""
        self._subscribers.discard(subscription)

    def publish(self, event: DomainEvent) -> None:
        """Hand an event to every subscriber, without ever waiting.

        A subscriber whose queue is full is dropped and removed; the others are unaffected.

        Args:
            event: What happened, already committed.
        """
        for subscription in tuple(self._subscribers):
            subscription._offer(event)
            if subscription.dropped:
                self._subscribers.discard(subscription)
                log.warning('Dropped a slow events client; it will reload over REST on reconnect')


# The process's one hub.
hub: Final = EventHub()


def publish(event: DomainEvent) -> None:
    """Publish to this process's hub. The call every store publisher makes after its commit."""
    hub.publish(event)
