"""The asset-list arm's logic: file a delivery as a dated snapshot and derive its events (tj-7zjov1.30, P17).

THE WIRE RULE. The asset list is the non-reproducible capture on this hop (the vendor purges some inactive assets), so
it is written at receipt, inside the delivery transaction, before the answer. Nothing here commits.

THE LIST COMES FROM THE REQUEST ROW ONLY (ADR tj-26jt9w addendum 17): every delivery, empty or not, is filed under
ingest_request.asset_list_status. The records' own active flag never chooses the list. A record that contradicts its list
is stored raw and counted as a member; the arm logs one WARNING with the count and refuses nothing, because a refused asset
list is redelivered forever and lost at an ingest restart. A record whose flag is None is a member with no warning.

THE SNAPSHOT is one header (asset_list_snapshot) and its raw rows (asset_list_row), append-only. An EMPTY delivery (the
runner sends a success with no part for a served empty list) is recorded as a snapshot with zero rows and derives NO event;
the diff base is the newest NON-EMPTY snapshot of the same (source, list), so a vendor glitch never reads as every listing
vanishing and then appearing.

THE EVENTS, derived by vendor asset id against that base, in the same transaction:
  appeared     an id in this snapshot and not in the base, and in no earlier snapshot of the other list.
  status_flip  an id in this snapshot and not in the base that an earlier snapshot of the OTHER list held (active to
               inactive or back). A delisting that also renames on the same id is one status_flip: the event carries both
               symbols.
  vanished     an id in the base and not in this snapshot, always (ADR tj-26jt9w addendum 18). A move between lists is
               a vanished on the list it left and a status_flip on the list it reached, whichever list arrives first.
A LATE snapshot (its as_of is not after the base's: a superseded instance's report after the recovered attempt, or a
late attempt of a finished request) is stored raw, derives NO event and logs one WARNING.
A list's FIRST non-empty snapshot has no base and derives no event. Nothing else is derived: no rename or reuse events and
no listing tables (addenda 12 to 17).
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from common.logging import get_logger
from data.store.app.database.crud import asset_list as crud
from data.store.app.database.crud.asset_list import NewEvent, SeenInOther
from data.store.app.database.crud.ingest_request import IngestRequestRecord
from data.store.app.database.models.asset_list import AssetListEventKind
from schemas.data_ingest.delivery import AssetRecord, AssetRecords, AttemptDelivery
from schemas.data_ingest.submit import AssetListStatus


log = get_logger(__name__)


def other_list(status: AssetListStatus) -> AssetListStatus:
    """The vendor's other list."""
    return AssetListStatus.INACTIVE if status is AssetListStatus.ACTIVE else AssetListStatus.ACTIVE


def records_of(attempt: AttemptDelivery) -> list[AssetRecord]:
    """Every record of every payload message of the attempt, in order; none for an empty list."""
    return [record for part in attempt.parts if isinstance(part, AssetRecords) for record in part.records]


def contradicting(records: list[AssetRecord], status: AssetListStatus) -> int:
    """How many records carry a flag that contradicts the list they were filed under.

    Args:
        records: The delivered assets.
        status: The request's list.

    Returns:
        int: Records with active False on the active list, or active True on the inactive one. None never counts.
    """
    wrong = status is not AssetListStatus.ACTIVE
    return sum(1 for record in records if record.active is wrong)


def derive_events(
    current: dict[str, str],
    base_symbols: dict[str, str],
    *,
    base_snapshot_id: uuid.UUID,
    base_as_of: datetime,
    seen_in_other: dict[str, SeenInOther],
) -> list[NewEvent]:
    """The events of one snapshot against its base. A pure function: no clock, no database.

    Args:
        current: This snapshot's vendor asset ids with their symbols.
        base_symbols: The base's ids with their symbols.
        base_snapshot_id: The base snapshot.
        base_as_of: The base snapshot's instant.
        seen_in_other: For the ids new to this list, their latest sighting in the other list before this snapshot.

    Returns:
        list[NewEvent]: Appearances and flips for new ids, then vanishes, each group in id order.
    """
    events: list[NewEvent] = []
    for vendor_asset_id in sorted(current.keys() - base_symbols.keys()):
        other = seen_in_other.get(vendor_asset_id)
        if other is not None:
            events.append(
                NewEvent(
                    AssetListEventKind.STATUS_FLIP,
                    vendor_asset_id,
                    current[vendor_asset_id],
                    other.symbol,
                    other.snapshot_id,
                    other.as_of,
                )
            )
        else:
            events.append(
                NewEvent(
                    AssetListEventKind.APPEARED,
                    vendor_asset_id,
                    current[vendor_asset_id],
                    None,
                    base_snapshot_id,
                    base_as_of,
                )
            )
    for vendor_asset_id in sorted(base_symbols.keys() - current.keys()):
        events.append(
            NewEvent(
                AssetListEventKind.VANISHED,
                vendor_asset_id,
                base_symbols[vendor_asset_id],
                None,
                base_snapshot_id,
                base_as_of,
            )
        )
    return events


async def record_snapshot(
    db: AsyncSession, request: IngestRequestRecord, attempt: AttemptDelivery, status: AssetListStatus
) -> int:
    """File the delivery as a snapshot of the request's list and derive its events, in the caller's transaction.

    Args:
        db: The delivery transaction's session, with the request row locked.
        request: The request the delivery answers (source and asset type).
        attempt: The report; its parts hold the assets, none for an empty list.
        status: The request's list (never read from the records).

    Returns:
        int: How many asset rows were written (zero for an empty snapshot).
    """
    records = records_of(attempt)
    await crud.lock_list(db, request.source, status)
    as_of = attempt.trailer.as_of or await crud.transaction_now(db)
    # The base is read BEFORE this snapshot is inserted, under the list lock; an empty snapshot derives nothing.
    base = await crud.newest_snapshot(db, request.source, status) if records else None
    snapshot_id = await crud.insert_snapshot(
        db,
        source=request.source,
        asset_type=request.asset_type,
        status=status,
        request_id=request.id,
        attempt_no=attempt.header.attempt_no,
        as_of=as_of,
        row_count=len(records),
    )
    await crud.insert_rows(db, snapshot_id, records)
    wrong = contradicting(records, status)
    if wrong:
        log.warning(
            f'Asset-list delivery for request {request.id} filed under the {status} list holds {wrong} record(s) '
            'whose own active flag contradicts it; stored raw and counted as members'
        )
    if base is None:
        return len(records)
    if base.as_of >= as_of:
        log.warning(
            f'Asset-list delivery for request {request.id} is not newer than the newest snapshot of its list; '
            f'stored raw, no event derived (as_of {as_of.isoformat()}, newest as_of {base.as_of.isoformat()})'
        )
        return len(records)

    current: dict[str, str] = {}
    for record in records:
        current.setdefault(record.vendor_asset_id, record.symbol)
    base_symbols = await crud.symbols_by_id(db, base.id)
    new_ids = sorted(current.keys() - base_symbols.keys())
    other = other_list(status)
    seen = await crud.last_seen_in_list(db, request.source, other, new_ids, as_of)
    events = derive_events(current, base_symbols, base_snapshot_id=base.id, base_as_of=base.as_of, seen_in_other=seen)
    await crud.insert_events(
        db, source=request.source, status=status, snapshot_id=snapshot_id, as_of=as_of, events=events
    )
    return len(records)
