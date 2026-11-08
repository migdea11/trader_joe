"""The statements of one delivered attempt, run INSIDE A TRANSACTION THE CALLER OWNS (tj-7zjov1.27, P34).

NOTHING HERE COMMITS OR ROLLS BACK. The delivery intake (app/ingest/delivery_intake.py) opens one
write_transaction and composes these with the bar writer, so the attempt row, the data, the durable attempt count,
the summed spend and the verdict commit together or not at all. That is the reason this module exists beside
crud/ingest_request.py and crud/ingest_run.py, whose functions each commit on their own and so cannot be composed.

THE REQUEST ROW IS LOCKED FIRST (lock_request, FOR UPDATE) and every other statement here assumes the lock is held.
The lock is what makes the rest correct: two deliveries of one request, a redelivery racing the original or a late
report from a superseded instance racing the new instance's, are serialised on it, so the duplicate lookup that
follows always sees the committed outcome of the one before. The unique index on (request_id, attempt_no) is the
backstop, not the mechanism: insert_attempt names it as the ON CONFLICT arbiter, WITH its predicate (Postgres
refuses a partial unique index as arbiter without it), and returns None on a collision instead of raising.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import Feed, Granularity, UpdateType
from data.store.app.database.crud.ingest_request import IngestRequestRecord
from data.store.app.database.crud.ingest_run import IngestRunRecord
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestFailureCause, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.database.models.store_dataset_entry import DatasetStopCause, StoreDatasetEntry
from schemas.data_ingest.delivery import VendorCallStats


# The predicate of the partial unique index uq_ingest_run_request_id_attempt_no (models/ingest_run.py).
_ATTEMPT_INDEX_PREDICATE: Final = text('request_id IS NOT NULL AND attempt_no IS NOT NULL')


@dataclass(frozen=True)
class StoredAttempt:
    """What the attempt row of (request_id, attempt_no) says, enough to answer a redelivery from it."""

    instance_id: str | None
    # CONTINUE or FINAL as the store answered the first time.
    verdict: str | None


@dataclass(frozen=True)
class AttemptLabel:
    """The label columns of an ingest_run row, None where the request has none (stored NULL).

    A bars request supplies all five; a corporate-actions request only the symbol it asked for.
    """

    asset_symbol: str | None
    asset_type: AssetType | None
    data_type: DataType | None
    granularity: Granularity | None
    update_type: UpdateType | None


@dataclass(frozen=True)
class NewAttempt:
    """One reported attempt, as its ingest_run row is written.

    The label columns of the row (symbol, asset type, data type, granularity, update type, source) come from the
    request record, never from the report: the report names the request and the store holds what it asked for.
    actor is repr=False like every actor: it is the request's client label and is never logged.
    """

    request: IngestRequestRecord
    attempt_no: int
    instance_id: str
    quota_key: str | None
    verdict: str
    state: IngestRunState
    trigger: IngestRunTrigger
    bars_written: int
    feed: Feed | None
    stats: VendorCallStats | None
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    # The label of the row, resolved by the caller from the request (a request that cannot supply it is refused
    # before this is built).
    label: AttemptLabel
    # The header's vendor asset id, stored as received. None when the header named none (never an empty string).
    vendor_asset_id: str | None = None
    # The window a SUCCEEDED attempt answered for (the trailer's served range); None on any other attempt.
    served_start: datetime | None = None
    served_end: datetime | None = None
    # The typed (domain, reason) of a FAILED attempt: the vocabulary's ERROR_DOMAIN and the Reason the trailer carried.
    # Or, for a FAILED attempt the store itself refused, the run-record marker VENDOR_ASSET_CHANGED_REASON with
    # error_domain NULL. None for every other attempt, and for a FAILED one whose trailer named no reason.
    error_domain: str | None = None
    error_reason: str | None = None


@dataclass(frozen=True)
class RequestOutcome:
    """What a delivery does to its request row, applied in one UPDATE.

    attempts and the spend are DELTAS, added in SQL so the statement never overwrites a value it did not read. The
    CALLER decides the state change, from the row it read under the lock: state is set only for a request that was
    still open (a terminal request keeps its first verdict), and queued_by only for an open, non-final report.
    """

    attempts: int = 0
    calls: int = 0
    pages: int = 0
    throttled_429: int = 0
    retries: int = 0
    # Counted attempts that carried no vendor stats (their cost is in none of the sums above).
    spend_unknown: int = 0
    # A terminal state to move the request to, with its cause and detail for FAILED. None leaves the state alone.
    state: IngestRequestState | None = None
    failure_cause: IngestRequestFailureCause | None = None
    failure_detail: str | None = None
    # Move the request to QUEUED under this instance (a non-final report from the live instance). None leaves it.
    queued_by: str | None = None


async def lock_request(db: AsyncSession, request_id: uuid.UUID) -> IngestRequestRecord | None:
    """Read the request row and hold it FOR UPDATE until the caller's transaction ends.

    Args:
        db: The caller's session, inside its transaction.
        request_id: The request the report names.

    Returns:
        IngestRequestRecord | None: The row as it stands under the lock, or None when no such request exists.
    """
    row = (
        await db.execute(select(IngestRequest).where(IngestRequest.id == request_id).with_for_update())
    ).scalar_one_or_none()
    return IngestRequestRecord.from_row(row) if row is not None else None


async def find_attempt(db: AsyncSession, request_id: uuid.UUID, attempt_no: int) -> StoredAttempt | None:
    """The attempt row already stored for (request_id, attempt_no), if any.

    Args:
        db: The caller's session, inside its transaction, with the request row locked.
        request_id: The request.
        attempt_no: The attempt number from the header.

    Returns:
        StoredAttempt | None: The stored instance and verdict, or None when this attempt was never recorded.
    """
    table = IngestRun.__table__
    row = (
        await db.execute(
            select(table.c.instance_id, table.c.verdict).where(
                table.c.request_id == request_id, table.c.attempt_no == attempt_no
            )
        )
    ).one_or_none()
    return StoredAttempt(instance_id=row.instance_id, verdict=row.verdict) if row is not None else None


async def instance_is_superseded(db: AsyncSession, instance_id: str) -> bool:
    """Whether a newer ingest instance has been seen since this one (ingest_instance.superseded_at is set).

    An id the marker does not hold is not superseded: the intake records the sighting first, so every id it asks
    about is held.

    Args:
        db: The caller's session.
        instance_id: The instance named by the report's header.

    Returns:
        bool: True when the instance has been superseded.
    """
    table = IngestInstance.__table__
    superseded_at = (
        await db.execute(select(table.c.superseded_at).where(table.c.instance_id == instance_id))
    ).scalar_one_or_none()
    return superseded_at is not None


async def lock_dataset_entry(db: AsyncSession, dataset_id: uuid.UUID) -> bool:
    """Whether the dataset entry exists, holding its row FOR UPDATE until the caller's transaction ends.

    Bars carry a foreign key to their dataset (ON DELETE CASCADE), which stays the backstop, so the bars arm asks first
    for three reasons: a delivery for a deleted entry is refused cleanly instead of surfacing a foreign-key violation
    as a database error; the reaper and the operator's delete lock the same row, which serialises them against this
    write; and P35's drop rule (gone or retired) has one place to judge under the lock. An entry deleted before the
    lock is seen as gone.

    Args:
        db: The caller's session, inside its transaction.
        dataset_id: The dataset the request writes to.

    Returns:
        bool: True when the entry exists and is now locked.
    """
    table = StoreDatasetEntry.__table__
    found = (await db.execute(select(table.c.id).where(table.c.id == dataset_id).with_for_update())).first()
    return found is not None


async def insert_attempt(db: AsyncSession, attempt: NewAttempt) -> IngestRunRecord | None:
    """Insert the attempt row, finished, idempotently.

    Args:
        db: The caller's session, inside its transaction, with the request row locked.
        attempt: The attempt, its verdict and its spend.

    Returns:
        IngestRunRecord | None: The stored row, or None when the arbiter index already held the attempt (the caller
            treats that as a duplicate it did not see, and writes nothing more).
    """
    table = IngestRun.__table__
    stats = attempt.stats
    request = attempt.request
    values: dict = {
        'id': uuid.uuid4(),
        'dataset_id': request.dataset_id,
        'request_id': request.id,
        'attempt_no': attempt.attempt_no,
        'instance_id': attempt.instance_id,
        'quota_key': attempt.quota_key,
        'verdict': attempt.verdict,
        'trigger': attempt.trigger,
        'actor': request.client_label,
        'asset_symbol': attempt.label.asset_symbol,
        'asset_type': attempt.label.asset_type,
        'data_type': attempt.label.data_type,
        'source': request.source,
        'granularity': attempt.label.granularity,
        'update_type': attempt.label.update_type.value if attempt.label.update_type is not None else None,
        'feed': attempt.feed,
        'requested_start': request.requested_start,
        'requested_end': request.requested_end,
        'state': attempt.state,
        'bars_written': attempt.bars_written,
        'error_domain': attempt.error_domain,
        'error_reason': attempt.error_reason,
        'error_message': attempt.error_message,
        'started_at': attempt.started_at if attempt.started_at is not None else func.now(),
        'finished_at': attempt.finished_at if attempt.finished_at is not None else func.now(),
        'vendor_asset_id': attempt.vendor_asset_id,
        'served_start': attempt.served_start,
        'served_end': attempt.served_end,
    }
    if stats is not None:
        values.update(
            vendor_calls=stats.calls,
            vendor_pages=stats.pages,
            vendor_throttled_429=stats.throttled_429,
            vendor_retries=stats.retries,
            vendor_first_call_at=stats.first_call_at,
            vendor_last_call_at=stats.last_call_at,
        )
        if stats.vendor_reported is not None:
            values.update(
                vendor_reported_limit=stats.vendor_reported.limit,
                vendor_reported_remaining=stats.vendor_reported.remaining,
                vendor_reported_reset_at=stats.vendor_reported.reset_at,
            )
    row = (
        await db.execute(
            insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=['request_id', 'attempt_no'], index_where=_ATTEMPT_INDEX_PREDICATE)
            .returning(*table.c)
        )
    ).one_or_none()
    return IngestRunRecord.from_row(row) if row is not None else None


async def apply_outcome(db: AsyncSession, request_id: uuid.UUID, outcome: RequestOutcome) -> IngestRequestRecord:
    """Add the delivery's count and spend to the request row and move its state, in one UPDATE.

    Args:
        db: The caller's session, inside its transaction, with the request row locked.
        request_id: The request.
        outcome: The deltas and the state change.

    Returns:
        IngestRequestRecord: The row after the update.
    """
    table = IngestRequest.__table__
    values: dict = {
        'attempts': table.c.attempts + outcome.attempts,
        'spent_calls': table.c.spent_calls + outcome.calls,
        'spent_pages': table.c.spent_pages + outcome.pages,
        'spent_throttled_429': table.c.spent_throttled_429 + outcome.throttled_429,
        'spent_retries': table.c.spent_retries + outcome.retries,
        'spend_unknown_attempts': table.c.spend_unknown_attempts + outcome.spend_unknown,
    }
    if outcome.state is not None:
        values.update(
            state=outcome.state,
            failure_cause=outcome.failure_cause.value if outcome.failure_cause is not None else None,
            failure_detail=outcome.failure_detail,
            finished_at=func.now(),
        )
    elif outcome.queued_by is not None:
        values.update(state=IngestRequestState.QUEUED, instance_id=outcome.queued_by)
    row = (await db.execute(update(table).where(table.c.id == request_id).values(**values).returning(*table.c))).one()
    return IngestRequestRecord.from_row(row)


async def lock_active_dataset_entry(db: AsyncSession, dataset_id: uuid.UUID) -> bool:
    """Whether the entry exists AND is not stopped, holding its row FOR UPDATE until the caller's transaction ends.

    The bars arm's drop rule (P35): a delivery for a deleted entry, or for a DAILY or STREAM entry whose collect is
    false (the operator's Stop), is dropped, never stored. The retired rule is the one the UI judges by
    (app/dataset_catalog.is_retired), spelled here because that module imports this layer: collect false on a
    subscription; the expiry is never read, and a STATIC entry has no intent. The lock is the one the reaper and the
    operator's delete take, so the judgement and the write that follows it are one step against them.

    Args:
        db: The caller's session, inside its transaction.
        dataset_id: The dataset the request writes to.

    Returns:
        bool: True when the entry exists, is not stopped, and is now locked.
    """
    table = StoreDatasetEntry.__table__
    found = (
        await db.execute(select(table.c.collect, table.c.update_type).where(table.c.id == dataset_id).with_for_update())
    ).first()
    if found is None:
        return False
    return not (found.collect is False and UpdateType(found.update_type) in (UpdateType.DAILY, UpdateType.STREAM))


async def set_request_dataset(db: AsyncSession, request_id: uuid.UUID, dataset_id: uuid.UUID) -> None:
    """Record on the request the dataset its first delivered attempt created or matched.

    Args:
        db: The caller's session, inside its transaction, with the request row locked.
        request_id: The request.
        dataset_id: The entry the delivery transaction wrote.
    """
    table = IngestRequest.__table__
    await db.execute(update(table).where(table.c.id == request_id).values(dataset_id=dataset_id))


async def touch_entry(db: AsyncSession, dataset_id: uuid.UUID) -> None:
    """Set the entry's updated_at to the transaction start and change nothing else: the refresh's only entry write.

    The refresh never writes a lifecycle field (expiry, collect, stopped_cause), so an operator's choice is never
    undone; updated_at is the dataset's last-written instant.

    Args:
        db: The caller's session, inside its transaction, with the entry locked.
        dataset_id: The entry.
    """
    table = StoreDatasetEntry.__table__
    await db.execute(update(table).where(table.c.id == dataset_id).values(updated_at=func.now()))


async def stop_entry(db: AsyncSession, dataset_id: uuid.UUID, cause: DatasetStopCause) -> bool:
    """Stop a subscription: collect false, the store's cause and updated_at, in one UPDATE (the stop rule, P50).

    Runs in the delivery transaction under the entry lock the bars arm already holds. Health, the status group, the
    refresh and the drop rule then treat the entry as Stopped through is_retired. A STATIC entry has no collect intent
    (collect is NULL by CHECK, and a cause needs collect false), so it is left as it is and False is returned.

    Args:
        db: The caller's session, inside its transaction, with the entry locked.
        dataset_id: The entry.
        cause: Why the store stopped it.

    Returns:
        bool: True when the entry was stopped, False when it is STATIC (or gone).
    """
    table = StoreDatasetEntry.__table__
    result = await db.execute(
        update(table)
        .where(table.c.id == dataset_id, table.c.collect.is_not(None))
        .values(collect=False, stopped_cause=cause.value, updated_at=func.now())
    )
    return result.rowcount > 0
