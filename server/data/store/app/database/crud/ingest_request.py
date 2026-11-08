"""The ingest_request repository: the durable request record's writes and reads (tj-7zjov1.25, P33).

EVERY WRITE COMMITS ON ITS OWN, through write_transaction, on the session the caller hands in, exactly as
crud/ingest_run.py does. The record is written and committed BEFORE the request is sent to ingest, so a store
restart finds what was in flight. The delivery intake (a later bead) needs the attempt, its data, the count and the
verdict in one transaction; it composes statements of its own for that and does not call these.

RESULTS ARE DETACHED SNAPSHOTS (IngestRequestRecord), never ORM rows.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Row, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from data.store.app.database.models.ingest_request import (
    OPEN_STATES,
    IngestRequest,
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.transaction import write_transaction


@dataclass(frozen=True)
class IngestRequestRecord:
    """One ingest_request row, detached.

    client_label is repr=False: it is the caller's label and is never logged.
    """

    id: uuid.UUID
    kind: IngestRequestKind
    source: DataSource
    dataset_id: uuid.UUID | None
    asset_symbol: str | None
    asset_type: AssetType | None
    data_types: tuple[str, ...] | None
    granularity: Granularity | None
    update_type: UpdateType | None
    feed: Feed | None
    requested_start: datetime | None
    requested_end: datetime | None
    ready_by: datetime | None
    best_effort: bool
    client_label: str = field(repr=False)
    state: IngestRequestState
    instance_id: str | None
    attempts: int
    spent_calls: int
    spent_pages: int
    spent_throttled_429: int
    spent_retries: int
    failure_cause: str | None
    failure_detail: str | None
    created_at: datetime
    finished_at: datetime | None

    @classmethod
    def from_row(cls, row: IngestRequest | Row) -> 'IngestRequestRecord':
        """Snapshot an ORM row, or a RETURNING row of the table's columns (same attribute names).

        Call it before the transaction that loaded the row commits.
        """
        return cls(
            id=row.id,
            kind=IngestRequestKind(row.kind),
            source=row.source,
            dataset_id=row.dataset_id,
            asset_symbol=row.asset_symbol,
            asset_type=row.asset_type,
            data_types=tuple(row.data_types) if row.data_types is not None else None,
            granularity=row.granularity,
            update_type=UpdateType(row.update_type) if row.update_type is not None else None,
            feed=row.feed,
            requested_start=row.requested_start,
            requested_end=row.requested_end,
            ready_by=row.ready_by,
            best_effort=row.best_effort,
            client_label=row.client_label,
            state=IngestRequestState(row.state),
            instance_id=row.instance_id,
            attempts=row.attempts,
            spent_calls=row.spent_calls,
            spent_pages=row.spent_pages,
            spent_throttled_429=row.spent_throttled_429,
            spent_retries=row.spent_retries,
            failure_cause=row.failure_cause,
            failure_detail=row.failure_detail,
            created_at=row.created_at,
            finished_at=row.finished_at,
        )


@dataclass(frozen=True)
class NewIngestRequest:
    """What a request record is created from: its kind, its parameters and its deadline.

    The parameters a kind does not have stay None. id is minted by the caller when it needs the request id before
    the insert (it goes on the wire); otherwise the insert mints one.
    """

    kind: IngestRequestKind
    source: DataSource
    client_label: str = field(repr=False)
    id: uuid.UUID | None = None
    dataset_id: uuid.UUID | None = None
    asset_symbol: str | None = None
    asset_type: AssetType | None = None
    data_types: tuple[str, ...] | None = None
    granularity: Granularity | None = None
    update_type: UpdateType | None = None
    feed: Feed | None = None
    requested_start: datetime | None = None
    requested_end: datetime | None = None
    ready_by: datetime | None = None
    best_effort: bool = False


async def create_request(db: AsyncSession, request: NewIngestRequest) -> IngestRequestRecord:
    """Insert a PENDING row and commit it, before the request is sent.

    Args:
        db: The caller's own session.
        request: The request's kind, parameters and deadline.

    Returns:
        IngestRequestRecord: The committed row, with its id and created_at.
    """
    async with write_transaction(db, 'record ingest request'):
        row = (
            await db.execute(
                IngestRequest.__table__.insert()
                .values(
                    id=request.id or uuid.uuid4(),
                    kind=request.kind,
                    source=request.source,
                    dataset_id=request.dataset_id,
                    asset_symbol=request.asset_symbol,
                    asset_type=request.asset_type,
                    data_types=list(request.data_types) if request.data_types is not None else None,
                    granularity=request.granularity,
                    update_type=request.update_type.value if request.update_type is not None else None,
                    feed=request.feed,
                    requested_start=request.requested_start,
                    requested_end=request.requested_end,
                    ready_by=request.ready_by,
                    best_effort=request.best_effort,
                    client_label=request.client_label,
                    state=IngestRequestState.PENDING,
                )
                .returning(*IngestRequest.__table__.c)
            )
        ).one()
        return IngestRequestRecord.from_row(row)


async def get_request(db: AsyncSession, request_id: uuid.UUID) -> IngestRequestRecord | None:
    """One request by id.

    Args:
        db: A session.
        request_id: The request's id.

    Returns:
        IngestRequestRecord | None: The row, or None when no such request exists.
    """
    row = (await db.execute(select(IngestRequest).where(IngestRequest.id == request_id))).scalar_one_or_none()
    return IngestRequestRecord.from_row(row) if row is not None else None


async def list_requests_in_states(
    db: AsyncSession, states: Collection[IngestRequestState] = OPEN_STATES
) -> list[IngestRequestRecord]:
    """The requests in the given states (the open ones by default), oldest first.

    The store's restart re-send reads the PENDING ones and the instance watcher the NOT_DELIVERED ones; the
    default form is served by the partial index on the open states.

    Args:
        db: A session.
        states: The states to read.

    Returns:
        list[IngestRequestRecord]: The rows, by created_at then id.
    """
    stmt = (
        select(IngestRequest)
        .where(IngestRequest.state.in_(list(states)))
        .order_by(IngestRequest.created_at, IngestRequest.id)
    )
    return [IngestRequestRecord.from_row(row) for row in (await db.execute(stmt)).scalars()]


async def mark_queued(db: AsyncSession, request_id: uuid.UUID, instance_id: str) -> IngestRequestRecord | None:
    """Record that ingest answered: PENDING or NOT_DELIVERED becomes QUEUED with the answering instance.

    A request already past those states is left alone, so a late or repeated answer cannot reopen a finished
    request or undo a NOT_DELIVERED mark made for a newer instance.

    Args:
        db: The caller's own session.
        request_id: The request.
        instance_id: The ingest instance that answered the Submit.

    Returns:
        IngestRequestRecord | None: The row after the update, or None when the request was not PENDING or
            NOT_DELIVERED (or does not exist).
    """
    table = IngestRequest.__table__
    async with write_transaction(db, 'record ingest request queued'):
        stmt = (
            update(table)
            .where(
                table.c.id == request_id,
                table.c.state.in_([IngestRequestState.PENDING, IngestRequestState.NOT_DELIVERED]),
            )
            .values(state=IngestRequestState.QUEUED, instance_id=instance_id)
            .returning(*table.c)
        )
        row = (await db.execute(stmt)).one_or_none()
        return IngestRequestRecord.from_row(row) if row is not None else None


async def finish_request(
    db: AsyncSession,
    request_id: uuid.UUID,
    *,
    state: IngestRequestState,
    failure_cause: IngestRequestFailureCause | None = None,
    failure_detail: str | None = None,
) -> IngestRequestRecord | None:
    """Move an open request to a terminal state and commit it.

    A request that is already terminal is left as it is (the first verdict stands). Counts and spend are written
    by the delivery transaction, not here.

    Args:
        db: The caller's own session.
        request_id: The request.
        state: SUCCEEDED, EMPTY or FAILED.
        failure_cause: Why, for FAILED only.
        failure_detail: A sentence for a human, for FAILED only; never str() of an exception.

    Returns:
        IngestRequestRecord | None: The row after the update, or None when the request was not open.

    Raises:
        ValueError: If state is not terminal, or a failure is given with a state other than FAILED.
    """
    if state.is_open:
        raise ValueError(f'finish_request needs a terminal state, got {state}')
    if state is not IngestRequestState.FAILED and (failure_cause is not None or failure_detail is not None):
        raise ValueError('a failure cause and detail belong to FAILED only')
    table = IngestRequest.__table__
    async with write_transaction(db, 'record ingest request finish'):
        stmt = (
            update(table)
            .where(table.c.id == request_id, table.c.state.in_(list(OPEN_STATES)))
            .values(
                state=state,
                failure_cause=failure_cause.value if failure_cause is not None else None,
                failure_detail=failure_detail,
                finished_at=func.now(),
            )
            .returning(*table.c)
        )
        row = (await db.execute(stmt)).one_or_none()
        return IngestRequestRecord.from_row(row) if row is not None else None
