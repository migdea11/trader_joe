"""This ingest process's identity, and the announcement of it to the store.

Ruled in tj-26jt9w (addendum 5). Every ingest process has an instance id, minted ONCE at start; this module is the
single place it is minted, and everything that carries it (every Submit answer, every attempt report header, the
Instance probe, RegisterInstance) receives the same InstanceIdentity. The store notices a new id, treats every
request the old instance held whose result never reached it as NOT DELIVERED, and re-requests them from this one.

THE ANNOUNCEMENT NEVER BLOCKS START. announce_until_acknowledged runs as a background task: a store that is down
or restarting leaves ingest serving, and the announcement is retried with bounded backoff until the store
acknowledges it. The store has two other ways to learn the id (an unknown id on any answer or report, and its own
probe), so a late announcement costs only latency.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from common.errors.vocabulary import TraderJoeError
from common.logging import get_logger
from common.rpc.clients.store_delivery import StoreDeliveryClient
from common.rpc.mapping.values import ProtoMappingError
from schemas.data_ingest.delivery import RegisterInstanceRequest


log = get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class InstanceIdentity:
    """Which ingest process this is.

    Attributes:
        instance_id (str): Opaque and unique to the process.
        started_at (datetime): When the process minted it, timezone aware.
    """

    instance_id: str
    started_at: datetime


def mint_instance(now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> InstanceIdentity:
    """Mint this process's identity. Call once, at start.

    Args:
        now (Callable[[], datetime]): The clock, timezone aware. Injectable for tests.

    Returns:
        InstanceIdentity: A fresh id and the start time.
    """
    return InstanceIdentity(instance_id=uuid.uuid4().hex, started_at=now())


def log_announcement_death(task: asyncio.Future[None]) -> None:
    """Log at ERROR when the announcement task ended with an exception. Cancellation is not an error.

    Add it as the task's done-callback so a bug surfaces when it happens, not only at shutdown. Visibility only: the
    store has two other ways to learn the id.

    Args:
        task (asyncio.Future[None]): The finished announcement task.
    """
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error(
            f'The instance announcement task died: {type(error).__name__}: {error}. '
            'The store can still learn the id from an unknown id on a report or its probe.',
            exc_info=error,
        )


async def announce_until_acknowledged(
    store: StoreDeliveryClient,
    identity: InstanceIdentity,
    *,
    base_delay_s: float = 1.0,
    max_delay_s: float = 30.0,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Announce the instance to the store, retrying with bounded exponential backoff until it acknowledges.

    Args:
        store (StoreDeliveryClient): The store's client; the call succeeding is the acknowledgement.
        identity (InstanceIdentity): The identity to announce.
        base_delay_s (float): The wait after the first failure.
        max_delay_s (float): The most a wait ever lasts.
        sleep (Sleep): Waits, in seconds. Injectable for tests.
    """
    delay = base_delay_s
    failures = 0
    while True:
        try:
            await store.register_instance(
                RegisterInstanceRequest(instance_id=identity.instance_id, started_at=identity.started_at)
            )
        except (TraderJoeError, ProtoMappingError) as error:
            failures += 1
            log.warning(
                f'Announcing instance {identity.instance_id} to the store failed ({type(error).__name__}, '
                f'failure {failures}); trying again in {delay:.1f}s. Ingest keeps serving.'
            )
            await sleep(delay)
            delay = min(delay * 2, max_delay_s)
            continue
        log.info(f'Instance {identity.instance_id} announced to the store')
        return
