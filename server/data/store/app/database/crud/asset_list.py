"""The asset-list repository: dated snapshots, their raw rows and the events derived from them (tj-7zjov1.30, P17).

NOTHING HERE COMMITS. The delivery intake's asset-list arm composes these with the attempt row and the request's count in
one write_transaction (app/world_snapshot.py is the arm's logic). Every table is APPEND-ONLY: there is no update and no
delete in this module.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource
from data.store.app.database.models.asset_list import (
    AssetListEvent,
    AssetListEventKind,
    AssetListRow,
    AssetListSnapshot,
)
from schemas.data_ingest.delivery import AssetRecord
from schemas.data_ingest.submit import AssetListStatus


# asyncpg allows 32,767 bind parameters per statement; a row binds 9, so 2,000 rows a statement stays well inside.
ROW_CHUNK: Final = 2000
# Ids per IN clause.
ID_CHUNK: Final = 5000


@dataclass(frozen=True)
class SnapshotRecord:
    """One asset_list_snapshot row, detached."""

    id: uuid.UUID
    source: DataSource
    asset_type: AssetType
    asset_list: AssetListStatus
    request_id: uuid.UUID
    attempt_no: int
    as_of: datetime
    row_count: int


@dataclass(frozen=True)
class SeenInOther:
    """The latest sighting of an id in a snapshot of the other list: where, when and under which symbol."""

    snapshot_id: uuid.UUID
    as_of: datetime
    symbol: str


@dataclass(frozen=True)
class NewEvent:
    """One derived event, as it is written."""

    kind: AssetListEventKind
    vendor_asset_id: str
    symbol: str
    previous_symbol: str | None
    previous_snapshot_id: uuid.UUID
    previous_as_of: datetime


def _chunks(vendor_asset_ids: Sequence[str]) -> list[list[str]]:
    """The ids in lists small enough for one IN clause (the driver allows 32,767 bind parameters a statement)."""
    return [list(vendor_asset_ids[start : start + ID_CHUNK]) for start in range(0, len(vendor_asset_ids), ID_CHUNK)]


async def lock_list(db: AsyncSession, source: DataSource, status: AssetListStatus) -> None:
    """Serialise the snapshots of one (source, list) for the rest of the caller's transaction.

    Two deliveries of different requests for the same list are not serialised by their request locks, and the diff base
    of one is the other's snapshot, so the base read and the insert must not interleave. A transaction-scoped advisory
    lock keyed by the list; it is released at commit or rollback.

    Args:
        db: The caller's session, inside its transaction.
        source: The data source.
        status: The list.
    """
    if db.get_bind().dialect.name != 'postgresql':
        # Only Postgres has concurrent transactions to serialise; other dialects (the test engine) have no such lock.
        return
    await db.execute(
        text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': f'asset_list:{source}:{status}'}
    )


async def transaction_now(db: AsyncSession) -> datetime:
    """The transaction start instant, as Postgres stamps it (used when a delivery names no as_of)."""
    return (await db.execute(select(func.now()))).scalar_one()


async def newest_snapshot(
    db: AsyncSession, source: DataSource, status: AssetListStatus, *, include_empty: bool = False
) -> SnapshotRecord | None:
    """The newest snapshot of a (source, list), by snapshot instant, then arrival, then id.

    NON-EMPTY ONLY BY DEFAULT: an empty snapshot is a recorded fact but never a diff base and never the age P47's rule
    reads (a vendor glitch must not read as every listing vanishing). Pass include_empty to see it.

    Args:
        db: A session.
        source: The data source.
        status: The list.
        include_empty: Whether a zero-row snapshot may be the answer.

    Returns:
        SnapshotRecord | None: The snapshot, or None when the list has none (of the asked kind).
    """
    table = AssetListSnapshot.__table__
    stmt = select(table).where(table.c.source == source, table.c.asset_list == status)
    if not include_empty:
        stmt = stmt.where(table.c.row_count > 0)
    stmt = stmt.order_by(table.c.as_of.desc(), table.c.received_at.desc(), table.c.id.desc()).limit(1)
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        return None
    return SnapshotRecord(
        id=row.id,
        source=row.source,
        asset_type=row.asset_type,
        asset_list=AssetListStatus(row.asset_list),
        request_id=row.request_id,
        attempt_no=row.attempt_no,
        as_of=row.as_of,
        row_count=row.row_count,
    )


async def insert_snapshot(
    db: AsyncSession,
    *,
    source: DataSource,
    asset_type: AssetType,
    status: AssetListStatus,
    request_id: uuid.UUID,
    attempt_no: int,
    as_of: datetime,
    row_count: int,
) -> uuid.UUID:
    """Insert the snapshot header and return its id.

    Args:
        db: The caller's session, inside its transaction, with the request row locked.
        source: The data source.
        asset_type: The request's asset type.
        status: The list the request asked for.
        request_id: The delivering request.
        attempt_no: The delivering attempt.
        as_of: The snapshot instant.
        row_count: How many rows follow.

    Returns:
        uuid.UUID: The new snapshot's id.

    Raises:
        IntegrityError: If (request_id, attempt_no) already has a snapshot. The delivery's duplicate rule answers a
            redelivery before it reaches here, so this is a defect; the transaction rolls back.
    """
    snapshot_id = uuid.uuid4()
    await db.execute(
        insert(AssetListSnapshot.__table__).values(
            id=snapshot_id,
            source=source,
            asset_type=asset_type,
            asset_list=status,
            request_id=request_id,
            attempt_no=attempt_no,
            as_of=as_of,
            row_count=row_count,
        )
    )
    return snapshot_id


async def insert_rows(db: AsyncSession, snapshot_id: uuid.UUID, records: Sequence[AssetRecord]) -> int:
    """Insert the snapshot's rows exactly as served, in chunks.

    Args:
        db: The caller's session, inside its transaction.
        snapshot_id: The snapshot the rows belong to.
        records: The delivered assets, in payload order.

    Returns:
        int: How many rows were written.
    """
    table = AssetListRow.__table__
    for start in range(0, len(records), ROW_CHUNK):
        await db.execute(
            insert(table).values(
                [
                    {
                        'id': uuid.uuid4(),
                        'snapshot_id': snapshot_id,
                        'vendor_asset_id': record.vendor_asset_id,
                        'symbol': record.symbol,
                        'name': record.name,
                        'exchange': record.exchange,
                        'active': record.active,
                        'tradable': record.tradable,
                        'raw_payload': record.raw_payload,
                    }
                    for record in records[start : start + ROW_CHUNK]
                ]
            )
        )
    return len(records)


async def symbols_by_id(db: AsyncSession, snapshot_id: uuid.UUID) -> dict[str, str]:
    """The snapshot's vendor asset ids, each with the symbol it was listed under (the first row, if an id repeats).

    Args:
        db: A session.
        snapshot_id: The snapshot.

    Returns:
        dict[str, str]: Vendor asset id to symbol.
    """
    table = AssetListRow.__table__
    rows = await db.execute(
        select(table.c.vendor_asset_id, table.c.symbol)
        .where(table.c.snapshot_id == snapshot_id)
        .order_by(table.c.vendor_asset_id, table.c.id)
    )
    found: dict[str, str] = {}
    for vendor_asset_id, symbol in rows:
        found.setdefault(vendor_asset_id, symbol)
    return found


async def last_seen_in_list(
    db: AsyncSession, source: DataSource, status: AssetListStatus, vendor_asset_ids: Sequence[str], before: datetime
) -> dict[str, SeenInOther]:
    """For each given id, its latest sighting in a snapshot of the list taken strictly before an instant.

    Args:
        db: A session.
        source: The data source.
        status: The list to search (the OTHER list of the snapshot being diffed).
        vendor_asset_ids: The ids to look for.
        before: The snapshot instant of the snapshot being diffed.

    Returns:
        dict[str, SeenInOther]: Ids that were seen, with where and under which symbol; the others are absent.
    """
    if not vendor_asset_ids:
        return {}
    rows = AssetListRow.__table__
    snapshots = AssetListSnapshot.__table__
    seen: dict[str, SeenInOther] = {}
    for chunk in _chunks(vendor_asset_ids):
        # The latest sighting of each id: rank its sightings newest first and keep the first (portable, no DISTINCT ON).
        ranked = (
            select(
                rows.c.vendor_asset_id.label('vendor_asset_id'),
                snapshots.c.id.label('snapshot_id'),
                snapshots.c.as_of.label('as_of'),
                rows.c.symbol.label('symbol'),
                func.row_number()
                .over(
                    partition_by=rows.c.vendor_asset_id,
                    order_by=(snapshots.c.as_of.desc(), snapshots.c.received_at.desc(), rows.c.id),
                )
                .label('rank'),
            )
            .join(snapshots, snapshots.c.id == rows.c.snapshot_id)
            .where(
                rows.c.vendor_asset_id.in_(chunk),
                snapshots.c.source == source,
                snapshots.c.asset_list == status,
                snapshots.c.as_of < before,
            )
            .subquery()
        )
        for row in await db.execute(select(ranked).where(ranked.c.rank == 1)):
            seen[row.vendor_asset_id] = SeenInOther(snapshot_id=row.snapshot_id, as_of=row.as_of, symbol=row.symbol)
    return seen


async def insert_events(
    db: AsyncSession,
    *,
    source: DataSource,
    status: AssetListStatus,
    snapshot_id: uuid.UUID,
    as_of: datetime,
    events: Sequence[NewEvent],
) -> int:
    """Insert the derived events of one snapshot, in chunks.

    The unique index on (snapshot_id, vendor_asset_id, kind) keeps a replay from writing an event twice.

    Args:
        db: The caller's session, inside its transaction.
        source: The data source.
        status: The snapshot's list.
        snapshot_id: The snapshot that produced the events.
        as_of: That snapshot's instant.
        events: The events.

    Returns:
        int: How many were written.
    """
    table = AssetListEvent.__table__
    written = 0
    for start in range(0, len(events), ROW_CHUNK):
        result = await db.execute(
            insert(table)
            .values(
                [
                    {
                        'id': uuid.uuid4(),
                        'source': source,
                        'asset_list': status,
                        'kind': event.kind,
                        'vendor_asset_id': event.vendor_asset_id,
                        'symbol': event.symbol,
                        'previous_symbol': event.previous_symbol,
                        'snapshot_id': snapshot_id,
                        'previous_snapshot_id': event.previous_snapshot_id,
                        'as_of': as_of,
                        'previous_as_of': event.previous_as_of,
                    }
                    for event in events[start : start + ROW_CHUNK]
                ]
            )
            .on_conflict_do_nothing(index_elements=['snapshot_id', 'vendor_asset_id', 'kind'])
        )
        written += result.rowcount
    return written
