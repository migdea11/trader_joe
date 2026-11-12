"""The corporate-actions arm's mapping: a delivered record to the observation the store keeps (tj-7zjov1.31, P18).

THE RECORD IS KEPT AS PUBLISHED. The window of the request is a fetch parameter and never a filter: every record a
delivery carries is stored, whatever its dates or symbols. Nothing is interpreted, resolved or tied to a listing
(ADR tj-26jt9w addendum 12), and the payload bytes are stored untouched beside their SHA-256, which with the vendor id
is the identity of an observation.
"""

import hashlib
from datetime import datetime

from common.logging import get_logger
from data.store.app.database.crud.corporate_action import NameChange, NewObservation
from data.store.app.database.crud.ingest_request import IngestRequestRecord
from schemas.data_ingest.delivery import AttemptDelivery, CorporateActionRecord, CorporateActionRecords


log = get_logger(__name__)


def payload_hash(raw_payload: bytes) -> str:
    """The SHA-256 of the vendor's bytes, in hex.

    Args:
        raw_payload: The record exactly as the vendor published it.

    Returns:
        str: 64 lower-case hex characters.
    """
    return hashlib.sha256(raw_payload).hexdigest()


def observation_of(
    record: CorporateActionRecord, request: IngestRequestRecord, attempt: AttemptDelivery
) -> NewObservation:
    """The observation a delivered record becomes, with the request and attempt that delivered it.

    Args:
        record: One vendor record.
        request: The request the delivery answers (source, requesting symbol and window).
        attempt: The report (attempt number and the vendor's answer time).

    Returns:
        NewObservation: The row to append.
    """
    return NewObservation(
        source=request.source,
        vendor_action_id=record.vendor_action_id,
        action_type=record.action_type,
        ex_date=record.ex_date,
        record_date=record.record_date,
        payable_date=record.payable_date,
        process_date=record.process_date,
        effective_date=record.effective_date,
        identifiers=dict(record.identifiers),
        raw_payload=record.raw_payload,
        payload_hash=payload_hash(record.raw_payload),
        request_id=request.id,
        attempt_no=attempt.header.attempt_no,
        requesting_symbol=request.asset_symbol,
        requested_start=request.requested_start,
        requested_end=request.requested_end,
        as_of=attempt.trailer.as_of,
    )


def observations_of(request: IngestRequestRecord, attempt: AttemptDelivery) -> list[NewObservation]:
    """Every record of every payload message of the attempt, in order. None is filtered.

    Args:
        request: The request the delivery answers.
        attempt: The report.

    Returns:
        list[NewObservation]: One per delivered record, repeats included (the insert drops the ones already stored).
    """
    return [
        observation_of(record, request, attempt)
        for part in attempt.parts
        if isinstance(part, CorporateActionRecords)
        for record in part.records
    ]


def name_change_clamp(changes: list[NameChange], symbol: str) -> datetime | None:
    """The process date the first request for a symbol is clamped to, or None when it is not clamped (P51).

    A candidate is a name_change whose old symbol is the symbol: the vendor served an earlier holder of the ticker up
    to it. A candidate is dropped when a name_change making the symbol a NEW symbol has a STRICTLY later process date:
    the symbol came back, so its current holder's history starts after the rename away, not at it (META: METV on
    2022-01-31, then FB to META on 2022-06-09). A record with no process date cannot be placed in time and is ignored
    on both sides, counted in one WARNING. The answer is the latest remaining candidate's process date.

    Args:
        changes: The stored name_change records naming the symbol, one per vendor action id.
        symbol: The requested symbol.

    Returns:
        datetime | None: The latest remaining process date, or None when no candidate remains.
    """
    undated = sum(change.process_date is None for change in changes)
    if undated:
        log.warning(f'{undated} name_change record(s) naming a requested symbol carry no process date; ignored')
    dated = [change for change in changes if change.process_date is not None]
    arrivals = [change.process_date for change in dated if change.new_symbol == symbol]
    remaining = [
        change.process_date
        for change in dated
        if change.old_symbol == symbol and not any(arrived > change.process_date for arrived in arrivals)
    ]
    return max(remaining) if remaining else None
