"""Keep the running stack's ingest instance live across a test that sights a minted instance id (tj-7zjov1.233).

THE HAZARD. A first sighting of an instance id (note_instance_seen, server/data/store/app/database/crud/
ingest_instance.py) supersedes EVERY other instance that is not superseded yet and marks every QUEUED request held by
another instance NOT_DELIVERED. That is global by design: in production only ingest mints ids, once per process. The
system suite mints its own ids against the SHARED stack database (through RegisterInstance, a delivery header, a fake
Submit answer or a probe), so each such test supersedes the REAL running ingest instance too. The tests' cleanups
delete only the ids they minted, and nothing in production ever clears superseded_at, so from the first such test on
every later POST of the session works against a superseded instance: mark_queued refuses to queue under it, a
transient FAILED attempt is answered FINAL with no state change, and only the instance watcher's 30 s re-send moves the
request on, which the suite's 30 s HTTP timeout does not outlast. test_ingest_e2e's FAIL_ / FAILONCE_ POSTs timed out.

THE FIX. Before such a test, note_live_instances records which instances are live and which QUEUED requests they hold.
After it, restore_live_instances undoes what the test's sightings did to them: superseded_at back to NULL, and each of
those requests that is still NOT_DELIVERED under the same holder back to QUEUED. Rows the test wrote are its own and its
own cleanup deletes them; nothing else is touched.

A REAL RESTART DURING THE TEST. If an instance the test did not mint (its id does not carry the run's marker) first
appeared while the test ran, ingest really restarted: the old live ids were superseded for real and stay so, and only
the newest such arrival is made live again (a minted id the test sighted after it superseded it too). Its predecessors'
requests are left NOT_DELIVERED for the watcher, as production would leave them.

The fixture that applies this is live_instances_kept in conftest.py; server/data/store/tests/
test_system_live_instances.py pins the logic on SQLite and fails when a system module that can sight an instance does
not use the fixture.
"""

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestState


INSTANCES = IngestInstance.__table__
REQUESTS = IngestRequest.__table__


@dataclass(frozen=True)
class LiveInstances:
    """What a test's sightings could change, read before the test."""

    # Every instance id the store knew.
    known: frozenset[str]
    # The ids that were not superseded.
    live: frozenset[str]
    # The QUEUED requests a live instance held: (request id, holder).
    queued: tuple[tuple[uuid.UUID, str], ...]


@dataclass(frozen=True)
class Restored:
    """What restore_live_instances changed. Ids only."""

    instances: tuple[str, ...] = ()
    requests: tuple[uuid.UUID, ...] = ()


def note_live_instances(conn: Connection) -> LiveInstances:
    """Read the live instances and the requests they hold, before a test that may sight a minted id.

    Args:
        conn: A connection on the stack's database.

    Returns:
        LiveInstances: The state restore_live_instances puts back.
    """
    rows = conn.execute(sa.select(INSTANCES.c.instance_id, INSTANCES.c.superseded_at)).all()
    live = frozenset(row.instance_id for row in rows if row.superseded_at is None)
    queued = (
        conn.execute(
            sa.select(REQUESTS.c.id, REQUESTS.c.instance_id)
            .where(REQUESTS.c.state == IngestRequestState.QUEUED, REQUESTS.c.instance_id.in_(live))
            .order_by(REQUESTS.c.id)
        ).all()
        if live
        else []
    )
    return LiveInstances(
        known=frozenset(row.instance_id for row in rows),
        live=live,
        queued=tuple((row.id, row.instance_id) for row in queued),
    )


def restore_live_instances(conn: Connection, before: LiveInstances, *, minted_marker: str) -> Restored:
    """Undo what a test's sightings did to the instances that were live before it, and to their QUEUED requests.

    Args:
        conn: A connection on the stack's database, inside a transaction the caller commits.
        before: What note_live_instances read before the test.
        minted_marker: A string every instance id the test suite mints carries (the run id), so an arrival that does
            not carry it is a real ingest process.

    Returns:
        Restored: The instances made live again and the requests put back to QUEUED.
    """
    if not minted_marker:
        raise ValueError('minted_marker must be non-empty: an empty marker would read every arrival as minted')
    # Newest first, so the first real arrival is the process that is running now.
    newest_first = conn.execute(
        sa.select(INSTANCES.c.instance_id).order_by(INSTANCES.c.first_seen_at.desc(), INSTANCES.c.instance_id.desc())
    ).scalars()
    real_arrivals = [
        instance for instance in newest_first if instance not in before.known and minted_marker not in instance
    ]
    keep = frozenset(real_arrivals[:1]) if real_arrivals else before.live
    if not keep:
        return Restored()
    instances = tuple(
        sorted(
            conn.execute(
                sa.update(INSTANCES)
                .where(INSTANCES.c.instance_id.in_(keep), INSTANCES.c.superseded_at.is_not(None))
                .values(superseded_at=None)
                .returning(INSTANCES.c.instance_id)
            ).scalars()
        )
    )
    requests = []
    for request_id, holder in before.queued:
        if holder not in keep:
            continue
        put_back = conn.execute(
            sa.update(REQUESTS)
            .where(
                REQUESTS.c.id == request_id,
                REQUESTS.c.state == IngestRequestState.NOT_DELIVERED,
                REQUESTS.c.instance_id == holder,
            )
            .values(state=IngestRequestState.QUEUED)
        )
        if put_back.rowcount:
            requests.append(request_id)
    return Restored(instances=instances, requests=tuple(requests))
