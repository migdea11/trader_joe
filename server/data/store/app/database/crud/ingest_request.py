"""The ingest_request repository: the durable request record's writes and reads (tj-7zjov1.25, P33).

EVERY WRITE COMMITS ON ITS OWN, through write_transaction, on the session the caller hands in, exactly as
crud/ingest_run.py does. The record is written and committed BEFORE the request is sent to ingest, so a store
restart finds what was in flight. The delivery intake (a later bead) needs the attempt, its data, the count and the
verdict in one transaction; it composes statements of its own for that and does not call these.

RESULTS ARE DETACHED SNAPSHOTS (IngestRequestRecord), never ORM rows.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from datetime import datetime

from sqlalchemy import Row, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import (
    OPEN_STATES,
    IngestRequest,
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRunTrigger
from data.store.app.database.transaction import write_transaction
from schemas.data_ingest.submit import AssetListStatus


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
    # What started the request; None (a row written before the column) reads as REQUEST.
    trigger: IngestRunTrigger | None = None
    # The POST body's lifecycle fields, None where the request carries none.
    expiry: datetime | None = None
    collect: bool | None = None
    # Counted attempts that reported no vendor stats: the spend sums above leave them out.
    spend_unknown_attempts: int = 0
    # The POST body's expiry type, None where the request carries none.
    expiry_type: ExpiryType | None = None
    # The list an ASSET_LIST request asks for; None for every other kind (the table's CHECK).
    asset_list_status: AssetListStatus | None = None

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
            trigger=IngestRunTrigger(row.trigger) if row.trigger is not None else None,
            expiry=row.expiry,
            collect=row.collect,
            spend_unknown_attempts=row.spend_unknown_attempts,
            expiry_type=ExpiryType(row.expiry_type) if row.expiry_type is not None else None,
            asset_list_status=AssetListStatus(row.asset_list_status) if row.asset_list_status is not None else None,
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
    # REQUEST for a POST, SCHEDULE for the DAILY loop; None is stored NULL and reads as REQUEST.
    trigger: IngestRunTrigger | None = None
    # The POST body's lifecycle fields, stored as given.
    expiry: datetime | None = None
    collect: bool | None = None
    # The POST body's expiry type, stored as its integer value; None is stored NULL.
    expiry_type: ExpiryType | None = None
    # The list an ASSET_LIST request asks for: required for that kind, forbidden for the others (the table's CHECK).
    asset_list_status: AssetListStatus | None = None

    def __post_init__(self) -> None:
        """Refuse the combinations the table's CHECK refuses, before the insert.

        Raises:
            ValueError: If an ASSET_LIST request names no list, or another kind names one.
        """
        is_asset_list = self.kind is IngestRequestKind.ASSET_LIST
        if is_asset_list and self.asset_list_status is None:
            raise ValueError('an ASSET_LIST request must name its list (asset_list_status)')
        if not is_asset_list and self.asset_list_status is not None:
            raise ValueError(f'a {self.kind} request cannot name an asset list')


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
                    trigger=request.trigger,
                    expiry=request.expiry,
                    collect=request.collect,
                    expiry_type=request.expiry_type.value if request.expiry_type is not None else None,
                    asset_list_status=request.asset_list_status,
                    state=IngestRequestState.PENDING,
                )
                .returning(*IngestRequest.__table__.c)
            )
        ).one()
        return IngestRequestRecord.from_row(row)


async def requests_in_window(db: AsyncSession, source: DataSource, client_label: str, since: datetime) -> int:
    """How many requests a client label wrote for a source at or after an instant, of every kind and in every state.

    The panel manager's request-rate window (request_cap_per_hour): the caller passes the reserved panel owner and now
    minus 60 minutes. Counted from the rows, so a restart resets nothing. created_at is database time; the read is
    served by ix_ingest_request_source_created_at. A request the DAILY refresh wrote carries another label and is not
    counted.

    Args:
        db: A session.
        source: The data source.
        client_label: The label the requests carry (the panel owner).
        since: The window start, timezone-aware, inclusive.

    Returns:
        int: The count.

    Raises:
        ValueError: since is naive.
    """
    if since.tzinfo is None or since.utcoffset() is None:
        raise ValueError('since must be timezone-aware')
    table = IngestRequest.__table__
    return (
        await db.execute(
            select(func.count())
            .select_from(table)
            .where(table.c.source == source, table.c.created_at >= since, table.c.client_label == client_label)
        )
    ).scalar_one()


async def symbols_with_open_requests(db: AsyncSession, source: DataSource, client_label: str) -> set[str]:
    """The symbols a client label has an open request for: a BARS request with no dataset yet, or corporate actions.

    The panel manager's in-flight check: a symbol whose creation (its clamp or its first bars request) is still on its
    way must not be asked for again. A BARS request that already names a dataset is a refresh and is not counted.

    Args:
        db: A session.
        source: The data source.
        client_label: The label the requests carry (the panel owner).

    Returns:
        set[str]: The symbols.
    """
    table = IngestRequest.__table__
    rows = await db.execute(
        select(table.c.asset_symbol)
        .where(
            table.c.source == source,
            table.c.client_label == client_label,
            table.c.state.in_(list(OPEN_STATES)),
            table.c.asset_symbol.is_not(None),
            or_(
                table.c.kind == IngestRequestKind.CORPORATE_ACTIONS,
                (table.c.kind == IngestRequestKind.BARS) & table.c.dataset_id.is_(None),
            ),
        )
        .distinct()
    )
    return set(rows.scalars())


async def requests_by_ids(db: AsyncSession, request_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, IngestRequestRecord]:
    """The requests with these ids, by id; an id with no row is absent from the result."""
    found: dict[uuid.UUID, IngestRequestRecord] = {}
    ids = list(request_ids)
    for start in range(0, len(ids), 5000):
        stmt = select(IngestRequest).where(IngestRequest.id.in_(ids[start : start + 5000]))
        for row in (await db.execute(stmt)).scalars():
            found[row.id] = IngestRequestRecord.from_row(row)
    return found


async def fetch_finished_since(db: AsyncSession, dataset_id: uuid.UUID, since: datetime) -> bool:
    """Whether a bars request for the dataset, written at or after an instant, finished with an answer.

    SUCCEEDED (bars written) or EMPTY (the vendor held none) both count: the drain rule (ADR tj-26jt9w, L1) ends when a
    fetch made after the delisting was seen covers the last session, or comes back empty. A request written after the
    instant asks up to the moment it was made.

    Args:
        db: A session.
        dataset_id: The dataset.
        since: Aware; only requests created at or after it count.

    Returns:
        bool: True when such a request finished SUCCEEDED or EMPTY.
    """
    table = IngestRequest.__table__
    found = (
        await db.execute(
            select(table.c.id)
            .where(
                table.c.kind == IngestRequestKind.BARS,
                table.c.dataset_id == dataset_id,
                table.c.created_at >= since,
                table.c.state.in_([IngestRequestState.SUCCEEDED, IngestRequestState.EMPTY]),
            )
            .limit(1)
        )
    ).first()
    return found is not None


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


async def open_asset_list_request_exists(db: AsyncSession, source: DataSource, status: AssetListStatus) -> bool:
    """Whether an ASSET_LIST request of this source and list is in an open state (PENDING, QUEUED or NOT_DELIVERED).

    P47 asks it before writing another request for the list, so one list is never asked for twice at once.

    Args:
        db: A session.
        source: The data source.
        status: The list.

    Returns:
        bool: True when such a request is open.
    """
    table = IngestRequest.__table__
    found = (
        await db.execute(
            select(table.c.id)
            .where(
                table.c.kind == IngestRequestKind.ASSET_LIST,
                table.c.source == source,
                table.c.asset_list_status == status,
                table.c.state.in_(list(OPEN_STATES)),
            )
            .limit(1)
        )
    ).first()
    return found is not None


async def mark_queued(db: AsyncSession, request_id: uuid.UUID, instance_id: str) -> IngestRequestRecord | None:
    """Record that ingest answered: PENDING or NOT_DELIVERED becomes QUEUED with the answering instance.

    A request already past those states is left alone, so a late or repeated answer cannot reopen a finished
    request or undo a NOT_DELIVERED mark made for a newer instance. NEVER QUEUED UNDER A SUPERSEDED INSTANCE: when the
    answering instance has already been superseded the request is left as it was, because nothing would ever work on
    it (the instance sighting marks only the rows QUEUED in its own snapshot); the watcher re-submits it.

    Args:
        db: The caller's own session.
        request_id: The request.
        instance_id: The ingest instance that answered the Submit.

    Returns:
        IngestRequestRecord | None: The row after the update, or None when the request was not PENDING or
            NOT_DELIVERED (or does not exist), or the answering instance is superseded.
    """
    table = IngestRequest.__table__
    instances = IngestInstance.__table__
    async with write_transaction(db, 'record ingest request queued'):
        stmt = (
            update(table)
            .where(
                table.c.id == request_id,
                table.c.state.in_([IngestRequestState.PENDING, IngestRequestState.NOT_DELIVERED]),
                ~exists().where(instances.c.instance_id == instance_id, instances.c.superseded_at.is_not(None)),
            )
            .values(state=IngestRequestState.QUEUED, instance_id=instance_id)
            .returning(*table.c)
        )
        row = (await db.execute(stmt)).one_or_none()
        return IngestRequestRecord.from_row(row) if row is not None else None


async def list_pending_before(db: AsyncSession, cutoff: datetime) -> list[IngestRequestRecord]:
    """The PENDING requests written before an instant, oldest first: written, and not answered by ingest in time.

    Args:
        db: A session.
        cutoff: Aware; requests created at or after it are still being submitted by their own caller.

    Returns:
        list[IngestRequestRecord]: The rows, by created_at then id.
    """
    stmt = (
        select(IngestRequest)
        .where(IngestRequest.state == IngestRequestState.PENDING, IngestRequest.created_at < cutoff)
        .order_by(IngestRequest.created_at, IngestRequest.id)
    )
    return [IngestRequestRecord.from_row(row) for row in (await db.execute(stmt)).scalars()]


async def list_abandoned(db: AsyncSession, superseded_before: datetime) -> list[uuid.UUID]:
    """The ids of open requests that an instance superseded before an instant still holds, oldest first.

    NOT_DELIVERED requests and QUEUED ones whose instance is superseded are the same thing for the watcher: nothing
    works on them. A QUEUED row can be left under a superseded instance (the sighting marks only the rows QUEUED in
    its snapshot), so both are read.

    Args:
        db: A session.
        superseded_before: Aware; only instances superseded at or before it count, so the old process's last report
            has landed or died.

    Returns:
        list[uuid.UUID]: The request ids.
    """
    requests = IngestRequest.__table__
    instances = IngestInstance.__table__
    old_instance = exists().where(
        instances.c.instance_id == requests.c.instance_id, instances.c.superseded_at <= superseded_before
    )
    stmt = (
        select(requests.c.id)
        .where(
            requests.c.state.in_([IngestRequestState.NOT_DELIVERED, IngestRequestState.QUEUED]),
            or_(
                old_instance,
                (requests.c.instance_id.is_(None)) & (requests.c.state == IngestRequestState.NOT_DELIVERED),
            ),
        )
        .order_by(requests.c.created_at, requests.c.id)
    )
    return list((await db.execute(stmt)).scalars())


async def claim_abandoned(
    db: AsyncSession, request_id: uuid.UUID, superseded_before: datetime
) -> IngestRequestRecord | None:
    """Lock an abandoned request and return it as NOT_DELIVERED with its durable attempt count, or None.

    THE COUNT IS READ UNDER THE REQUEST LOCK (architect ruling (C), tj-7zjov1.28): the old instance's last in-flight
    report has either committed (and is in the count) or is refused for ever, so the new instance is never handed a
    count it will collide with. A QUEUED row under a superseded instance is moved to NOT_DELIVERED here, so the rest
    of the re-submission treats both alike. A row that is no longer abandoned (finished, or already taken by a newer
    instance) yields None.

    Args:
        db: The caller's own session.
        request_id: The request.
        superseded_before: Aware; the same cutoff list_abandoned used.

    Returns:
        IngestRequestRecord | None: The row after the claim, or None when it is not abandoned any more.
    """
    table = IngestRequest.__table__
    instances = IngestInstance.__table__
    async with write_transaction(db, 'claim abandoned ingest request'):
        row = (
            await db.execute(select(IngestRequest).where(IngestRequest.id == request_id).with_for_update())
        ).scalar_one_or_none()
        if row is None or row.state not in (IngestRequestState.NOT_DELIVERED, IngestRequestState.QUEUED):
            return None
        if row.instance_id is not None:
            superseded_at = (
                await db.execute(select(instances.c.superseded_at).where(instances.c.instance_id == row.instance_id))
            ).scalar_one_or_none()
            if superseded_at is None or superseded_at > superseded_before:
                return None
        elif row.state is not IngestRequestState.NOT_DELIVERED:
            return None
        record = IngestRequestRecord.from_row(row)
        if record.state is IngestRequestState.QUEUED:
            await db.execute(
                update(table).where(table.c.id == request_id).values(state=IngestRequestState.NOT_DELIVERED)
            )
        return replace(record, state=IngestRequestState.NOT_DELIVERED)


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
