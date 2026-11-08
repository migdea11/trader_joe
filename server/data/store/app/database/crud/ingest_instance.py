"""The ingest_instance repository: the instance marker (tj-7zjov1.25, P33; tj-26jt9w addendum 5).

note_instance_seen is the one write. It commits on its own, through write_transaction, on the session the caller
hands in, and does three things in that ONE transaction so a crash between them leaves no half-noticed restart:

  1. records the instance (a first sighting inserts it; any later sighting moves last_seen_at);
  2. on a first sighting, supersedes every older instance that is not already superseded;
  3. on a first sighting, marks every QUEUED request held by an older instance NOT_DELIVERED.

A NOT_DELIVERED request is never a failure and never an attempt: the store re-submits it to the new instance with its
durable attempt count. A report from a superseded instance is still written by the intake, and its sighting here moves
nothing: only a first sighting supersedes, so an old id arriving late can never supersede the live one.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestState
from data.store.app.database.transaction import write_transaction


@dataclass(frozen=True)
class InstanceSighting:
    """What one sighting of an instance id changed."""

    instance_id: str
    # True when the store had never seen this id: a new ingest process.
    is_new: bool
    # The instances this sighting superseded (empty unless is_new).
    superseded: tuple[str, ...]
    # The QUEUED requests of older instances this sighting marked NOT_DELIVERED, for the caller to re-submit.
    not_delivered: tuple[uuid.UUID, ...]


async def note_instance_seen(db: AsyncSession, instance_id: str) -> InstanceSighting:
    """Record a sighting of an ingest instance, and act on a first one.

    Args:
        db: The caller's own session.
        instance_id: The opaque id ingest minted at start, from RegisterInstance, a Submit answer, a delivery
            header or the probe.

    Returns:
        InstanceSighting: Whether the id was new, and what a new one superseded and marked NOT_DELIVERED.
    """
    instances = IngestInstance.__table__
    requests = IngestRequest.__table__
    async with write_transaction(db, 'record ingest instance sighting'):
        inserted = (
            await db.execute(
                insert(instances)
                .values(instance_id=instance_id)
                .on_conflict_do_nothing(index_elements=[instances.c.instance_id])
                .returning(instances.c.instance_id)
            )
        ).first()
        if inserted is None:
            await db.execute(
                update(instances).where(instances.c.instance_id == instance_id).values(last_seen_at=func.now())
            )
            return InstanceSighting(instance_id, is_new=False, superseded=(), not_delivered=())

        superseded = (
            await db.execute(
                update(instances)
                .where(instances.c.instance_id != instance_id, instances.c.superseded_at.is_(None))
                .values(superseded_at=func.now())
                .returning(instances.c.instance_id)
            )
        ).scalars()
        superseded_ids = tuple(sorted(superseded))
        not_delivered = (
            await db.execute(
                update(requests)
                .where(
                    requests.c.state == IngestRequestState.QUEUED,
                    requests.c.instance_id.is_not(None),
                    requests.c.instance_id != instance_id,
                )
                .values(state=IngestRequestState.NOT_DELIVERED)
                .returning(requests.c.id)
            )
        ).scalars()
        return InstanceSighting(
            instance_id, is_new=True, superseded=superseded_ids, not_delivered=tuple(sorted(not_delivered))
        )


async def live_instance_id(db: AsyncSession) -> str | None:
    """The newest instance the store has seen (the one not superseded), or None when it has seen none.

    Args:
        db: A session.

    Returns:
        str | None: The instance id, newest first sighting when more than one is unsuperseded.
    """
    instances = IngestInstance.__table__
    return (
        await db.execute(
            select(instances.c.instance_id)
            .where(instances.c.superseded_at.is_(None))
            .order_by(instances.c.first_seen_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
