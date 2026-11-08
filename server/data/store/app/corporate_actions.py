"""The corporate-actions arm's mapping: a delivered record to the observation the store keeps (tj-7zjov1.31, P18).

THE RECORD IS KEPT AS PUBLISHED. The window of the request is a fetch parameter and never a filter: every record a
delivery carries is stored, whatever its dates or symbols. Nothing is interpreted, resolved or tied to a listing
(ADR tj-26jt9w addendum 12), and the payload bytes are stored untouched beside their SHA-256, which with the vendor id
is the identity of an observation.
"""

import hashlib

from data.store.app.database.crud.corporate_action import NewObservation
from data.store.app.database.crud.ingest_request import IngestRequestRecord
from schemas.data_ingest.delivery import AttemptDelivery, CorporateActionRecord, CorporateActionRecords


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
