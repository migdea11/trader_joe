"""The corporate_action repository: append-only observations, written INSIDE A TRANSACTION THE CALLER OWNS.

NOTHING HERE COMMITS. The delivery intake's corporate-actions arm composes this with the attempt row and the request's
count in one write_transaction. There is no update and no delete: a vendor revision is a new row.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import DataSource
from data.store.app.database.models.corporate_action import CorporateAction


@dataclass(frozen=True)
class NewObservation:
    """One vendor record as delivered, with what delivered it. Built by app/corporate_actions.py."""

    source: DataSource
    vendor_action_id: str
    action_type: str
    ex_date: datetime | None
    record_date: datetime | None
    payable_date: datetime | None
    process_date: datetime | None
    effective_date: datetime | None
    identifiers: dict[str, str]
    raw_payload: bytes
    payload_hash: str
    request_id: uuid.UUID
    attempt_no: int
    requesting_symbol: str | None
    requested_start: datetime | None
    requested_end: datetime | None
    # When the vendor answered; None reads the transaction start.
    as_of: datetime | None


async def insert_observations(db: AsyncSession, observations: list[NewObservation]) -> int:
    """Insert the observations, keeping the first of any (source, vendor id, payload hash) already stored.

    The unique index on those three columns is the arbiter, so an observation the store already holds, from an earlier
    delivery or earlier in this one, writes nothing and is not counted. Rows are inserted one statement per row so the
    count is exact and a large payload never exceeds the driver's parameter limit.

    Args:
        db: The caller's session, inside its transaction, with the request row locked.
        observations: The records of one delivery, in payload order.

    Returns:
        int: How many were new.
    """
    table = CorporateAction.__table__
    written = 0
    for observation in observations:
        result = await db.execute(
            insert(table)
            .values(
                id=uuid.uuid4(),
                source=observation.source,
                vendor_action_id=observation.vendor_action_id,
                action_type=observation.action_type,
                ex_date=observation.ex_date,
                record_date=observation.record_date,
                payable_date=observation.payable_date,
                process_date=observation.process_date,
                effective_date=observation.effective_date,
                identifiers=observation.identifiers,
                raw_payload=observation.raw_payload,
                payload_hash=observation.payload_hash,
                request_id=observation.request_id,
                attempt_no=observation.attempt_no,
                requesting_symbol=observation.requesting_symbol,
                requested_start=observation.requested_start,
                requested_end=observation.requested_end,
                as_of=observation.as_of if observation.as_of is not None else func.now(),
            )
            .on_conflict_do_nothing(index_elements=['source', 'vendor_action_id', 'payload_hash'])
        )
        written += result.rowcount
    return written
