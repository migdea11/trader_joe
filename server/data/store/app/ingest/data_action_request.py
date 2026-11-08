"""THE REQUEST PATH: write the request, submit it to ingest, and (for a POST) wait for the verdict (tj-7zjov1.28, P35).

THE RESULT IS PUSHED, NOT STREAMED. The old path held one FetchDataset stream open, wrote the entry on the ack and the
bars page by page, and committed at its end. That stream is gone from this path. Ingest now answers the Submit at once
(held or refused) and reports every attempt, with its data, to the store's own StoreService (app/ingest/
delivery_intake.py), which writes it in one transaction before it acknowledges. What is left here is the front half and
the wait:

    1. refuse an own-overlap (a POST only), before anything is written or sent. One local SELECT.
    2. WRITE THE REQUEST ROW (PENDING) and commit it, BEFORE sending, so a store restart finds what was in flight. The
       row holds everything the first delivered attempt needs to write the dataset entry (owner, window, expiry,
       expiry type, collect) and the trigger that started it (REQUEST for a POST, SCHEDULE for the DAILY loop).
    3. SUBMIT. An accepted answer notes the instance and moves the request to QUEUED under it; a refusal (a feed
       ingest cannot serve) finishes the request FAILED. A call that fails leaves the request PENDING: the instance
       watcher (app/ingest/instance_watch.py) re-sends it, Submit being idempotent on the request id.
    4. A POST WAITS (await_verdict) for the request to finish, up to the fetch deadline; the DAILY loop does not.

THE WAITER NEVER JUDGES BY AN ANSWER INGEST RECEIVED. It reads the REQUEST ROW after the delivery committed (its
state, and the finishing attempt row for the data points and the served range), so a late redelivery of an earlier
attempt, answered from its stored row, cannot change what the caller is told.

A POST THAT OUTLIVES THE DEADLINE returns the existing deadline error and the request continues, its outcome in the
run history (owner ruling R4, option a). A POST's ready_by is the caller's deadline: ingest checks it when it takes the
request off its queue and reports the request expired without a vendor call, which the waiter returns as a failure.

THE OWNER IS NEVER LOGGED (D8): the client label rides the request row and the wire, and no line below formats it.
"""

import asyncio
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import DataType
from common.environment import get_env_var
from common.errors.vocabulary import ERROR_DOMAIN, REASONS, ExogenousError, InvalidRequestError, Reason, TraderJoeError
from common.logging import get_logger
from common.rpc.clients.ingest_fetch import DEFAULT_FETCH_DEADLINE_S
from common.rpc.clients.ingest_submit import IngestSubmitClient
from data.store.app.database.crud.ingest_instance import note_instance_seen
from data.store.app.database.crud.ingest_request import (
    IngestRequestRecord,
    NewIngestRequest,
    create_request,
    finish_request,
    get_request,
    mark_queued,
)
from data.store.app.database.crud.ingest_run import finishing_attempt
from data.store.app.database.crud.stock.store_dataset_entry import OverlapKey, check_own_overlap
from data.store.app.database.database import own_session
from data.store.app.database.models.ingest_request import (
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRunTrigger
from data.store.app.database.transaction import write_transaction
from schemas.data_ingest import fetch_dataset
from schemas.data_ingest.submit import BarsRequest, SubmitRequest, SubmitResponse
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath


log = get_logger(__name__)

VERDICT_DEADLINE_ENV: Final = 'STORE_VERDICT_DEADLINE_SECONDS'
VERDICT_POLL_ENV: Final = 'STORE_VERDICT_POLL_SECONDS'
DEFAULT_VERDICT_POLL_S: Final = 0.25

# Fixed sentences for the failures a finished request maps to. The vendor's own sentence rides along (it carries no
# secret: ingest guarantees that) but is never the whole message.
_EXPIRED_MESSAGE: Final = 'The request waited past its ready_by and expired before it was run; nothing was fetched.'
_PERMANENT_MESSAGE: Final = 'The market-data vendor refused the request for good; it will not be retried.'
_EXHAUSTED_MESSAGE: Final = 'The request failed on every attempt ingest was allowed; try again later.'


@dataclass(frozen=True)
class StoredDataset:
    """What one finished request wrote, and the window it was answered for.

    THE served_range IS THE FINISHING ATTEMPT ROW'S (served_start, served_end), copied as it was stored: never rebuilt
    from the request and never clamped again here, because the clamp is ingest's (end = min(requested end, as_of)) and
    a store that recomputed it would publish its own guess as the vendor's answer.

    Attributes:
        data_points: How many bars the finishing attempt wrote.
        served_range: The window the finishing attempt was answered for.
    """

    data_points: int
    served_range: fetch_dataset.ServedRange


def verdict_deadline_seconds() -> float:
    """How long a POST waits for its request to finish; a non-positive or malformed value is the fetch deadline."""
    value = get_env_var(VERDICT_DEADLINE_ENV, default=DEFAULT_FETCH_DEADLINE_S, cast_type=float)
    return value if math.isfinite(value) and value > 0 else DEFAULT_FETCH_DEADLINE_S


def verdict_poll_seconds() -> float:
    """How often the waiter reads the request row; a non-positive or malformed value is the default."""
    value = get_env_var(VERDICT_POLL_ENV, default=DEFAULT_VERDICT_POLL_S, cast_type=float)
    return value if math.isfinite(value) and value > 0 else DEFAULT_VERDICT_POLL_S


def new_bars_request(
    path: StoreAssetDatasetPath,
    body: StoreAssetDatasetBody,
    *,
    trigger: IngestRunTrigger,
    client_label: str,
    start: datetime | None = None,
    ready_by: datetime | None = None,
    dataset_id: uuid.UUID | None = None,
    carries_lifecycle: bool = True,
) -> NewIngestRequest:
    """The request row a bars request is written as, before it is sent.

    A POST (carries_lifecycle) records the body's expiry, expiry type and collect, which the first delivered attempt
    writes onto the dataset entry; the expiry type is the body's own declared value, its default included. The DAILY
    loop carries none of the three: it names its dataset (dataset_id) and the refresh never writes a lifecycle field.

    Args:
        path: The asset and data type.
        body: What the caller asked for (the entry's identity fields for a POST, the entry's own for the refresh).
        trigger: REQUEST for a POST, SCHEDULE for the DAILY loop.
        client_label: The row's label: the POST's owner, the refresh's actor.
        start: The window start to ask ingest for, when it narrows the body's (the refresh's missing tail).
        ready_by: The caller's deadline, or the next market open for the refresh; None is no deadline.
        dataset_id: The entry a refresh names; None for a POST, whose first delivered attempt creates it.
        carries_lifecycle: Whether the body's expiry, expiry type and collect are recorded.

    Returns:
        NewIngestRequest: The row, with its id left for the insert to mint.
    """
    return NewIngestRequest(
        kind=IngestRequestKind.BARS,
        source=body.source,
        client_label=client_label,
        dataset_id=dataset_id,
        asset_symbol=path.asset_symbol,
        asset_type=path.asset_type,
        data_types=(path.data_type.value,),
        granularity=body.granularity,
        update_type=body.update_type,
        feed=body.feed,
        requested_start=body.start if start is None else start,
        requested_end=body.end,
        ready_by=ready_by,
        best_effort=body.best_effort,
        trigger=trigger,
        expiry=body.expiry if carries_lifecycle else None,
        collect=body.collect if carries_lifecycle else None,
        expiry_type=body.expiry_type if carries_lifecycle else None,
    )


def submit_request_of(record: IngestRequestRecord, *, recovered: bool = False) -> SubmitRequest:
    """The Submit a stored bars request is sent as, with its durable attempt count.

    Used for the first send, for the watcher's re-send of a PENDING row and for the recovered re-submission of a
    superseded instance's request, which carries the count the old instance's reports left.

    Args:
        record: The request row.
        recovered: True when the store re-submits a request an earlier ingest instance held.

    Returns:
        SubmitRequest: The domain request.

    Raises:
        ValueError: If the request is not a bars request, or the row lacks a field the request needs.
    """
    if record.kind is not IngestRequestKind.BARS:
        raise ValueError(f'a {record.kind} request cannot be submitted by this release')
    if (
        record.asset_symbol is None
        or record.asset_type is None
        or not record.data_types
        or record.granularity is None
        or record.update_type is None
        or record.requested_start is None
    ):
        raise ValueError('the request row does not carry the fields a bars request needs')
    return SubmitRequest(
        request_id=str(record.id),
        owner=record.client_label,
        ready_by=record.ready_by,
        best_effort=record.best_effort,
        recovered=recovered,
        attempt_count=record.attempts,
        bars=BarsRequest(
            source=record.source,
            asset_symbol=record.asset_symbol,
            asset_type=record.asset_type,
            data_types=[DataType(value) for value in record.data_types],
            granularity=record.granularity,
            start=record.requested_start,
            end=record.requested_end,
            update_type=record.update_type,
            feed=record.feed,
        ),
    )


def _refusal_error(refused: fetch_dataset.FetchRefused) -> TraderJoeError:
    """The error a refused Submit stands for, rebuilt as the FetchDataset seam rebuilt its refusal."""
    if refused.domain != ERROR_DOMAIN or refused.reason not in Reason.__members__:
        return ExogenousError(
            Reason.PEER_PROTOCOL_ERROR, 'the Submit was refused for a reason this release cannot read'
        )
    reason = Reason(refused.reason)
    try:
        return REASONS[reason].branch(reason, refused.detail, metadata=dict(refused.metadata))
    except (TypeError, ValueError):
        return ExogenousError(
            Reason.PEER_PROTOCOL_ERROR, 'the Submit was refused with a payload this release cannot read'
        )


async def submit_record(
    record: IngestRequestRecord, client: IngestSubmitClient, *, recovered: bool = False
) -> IngestRequestRecord:
    """Send one stored request to ingest and record the answer.

    ACCEPTED: the answering instance is noted (a first sighting supersedes older instances), then the request moves to
    QUEUED under it, unless that instance is already superseded, when it is left as it was for the watcher. REFUSED
    (a feed ingest cannot serve): the request is finished FAILED (PERMANENT) and the refusal is raised as its error.

    Each write is its own transaction on its own session. A call that fails raises and writes nothing: the request
    stays as it was (PENDING, or NOT_DELIVERED) for the watcher.

    Args:
        record: The request row as stored.
        client: The Submit seam, as the interface.
        recovered: True for the re-submission of a superseded instance's request.

    Returns:
        IngestRequestRecord: The row after the answer (QUEUED), or as it was when the instance was superseded.

    Raises:
        TraderJoeError: The refusal, or what the failed call raised.
    """
    response: SubmitResponse = await client.submit(submit_request_of(record, recovered=recovered))
    async with own_session() as db:
        await note_instance_seen(db, response.instance_id)
    refused = response.ack.refused
    if refused is not None:
        async with own_session() as db:
            await finish_request(
                db,
                record.id,
                state=IngestRequestState.FAILED,
                failure_cause=IngestRequestFailureCause.PERMANENT,
                failure_detail=refused.detail,
            )
        raise _refusal_error(refused)
    async with own_session() as db:
        queued = await mark_queued(db, record.id, response.instance_id)
    if queued is None:
        log.info(
            f'Request {record.id} answered by an instance that is superseded or a request that moved on; left as is'
        )
        return record
    return queued


async def _check_overlap(db: AsyncSession, path: StoreAssetDatasetPath, body: StoreAssetDatasetBody) -> None:
    """Refuse a POST that overlaps one of the same owner's datasets, before the request is written or sent."""
    key = OverlapKey(
        owner=body.owner,
        asset_type=path.asset_type,
        asset_symbol=path.asset_symbol,
        data_type=path.data_type,
        source=body.source,
        granularity=body.granularity,
        expiry_type=body.expiry_type,
        update_type=body.update_type,
        start=body.start,
        end=body.end,
    )
    async with write_transaction(db, 'check own overlap'):
        await check_own_overlap(db, key)


async def submit_bars(
    path: StoreAssetDatasetPath,
    body: StoreAssetDatasetBody,
    db: AsyncSession,
    client: IngestSubmitClient,
    *,
    trigger: IngestRunTrigger,
    client_label: str,
    start: datetime | None = None,
    ready_by: datetime | None = None,
    dataset_id: uuid.UUID | None = None,
) -> IngestRequestRecord:
    """Write one bars request, then submit it. The DAILY loop's whole job, and a POST's first half.

    A request with no dataset_id is a POST: its own-overlap is refused first and the body's lifecycle fields are
    recorded. One with a dataset_id is a refresh of that entry.

    Args:
        path: The asset and data type.
        body: What is asked for.
        db: The caller's session, for the overlap read and the request row's insert.
        client: The Submit seam, as the interface.
        trigger: REQUEST or SCHEDULE.
        client_label: The row's label.
        start: The window start to ask for, when narrower than the body's.
        ready_by: The deadline, or None.
        dataset_id: The entry a refresh names.

    Returns:
        IngestRequestRecord: The request after the answer. PENDING when the call failed would have raised instead.

    Raises:
        OwnOverlapConflict: A POST overlaps one of the same owner's datasets; nothing is written or sent.
        TraderJoeError: The refusal, or what the failed call raised; the request row stays for the watcher.
    """
    if dataset_id is None:
        await _check_overlap(db, path, body)
    record = await create_request(
        db,
        new_bars_request(
            path,
            body,
            trigger=trigger,
            client_label=client_label,
            start=start,
            ready_by=ready_by,
            dataset_id=dataset_id,
            carries_lifecycle=dataset_id is None,
        ),
    )
    return await submit_record(record, client)


async def await_verdict(
    request_id: uuid.UUID,
    *,
    deadline_s: float | None = None,
    poll_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> StoredDataset:
    """Wait for a request to finish and answer from its rows.

    Reads the REQUEST ROW (state) after each delivery commit, and for a request that finished with data, the finishing
    attempt row (bars written, served range). An answer ingest received is never the evidence.

    Args:
        request_id: The request.
        deadline_s: How long to wait; None reads STORE_VERDICT_DEADLINE_SECONDS (default the fetch deadline).
        poll_s: How often to read; None reads STORE_VERDICT_POLL_SECONDS.
        clock: A monotonic clock in seconds; injected so a test does not wait.
        sleep: Sleeps for seconds; injected likewise.

    Returns:
        StoredDataset: Bars written and the served window, for a SUCCEEDED or EMPTY request.

    Raises:
        ExogenousError: DEADLINE, when the request is still open at the deadline (it continues) or expired on its
            ready_by; VENDOR_REJECTED for a permanent refusal; VENDOR_UNAVAILABLE for exhausted attempts;
            PEER_PROTOCOL_ERROR for a finished request whose attempt carries no served range.
        InvalidRequestError: NOT_FOUND, when the request row is gone.
    """
    wait = verdict_deadline_seconds() if deadline_s is None else deadline_s
    interval = verdict_poll_seconds() if poll_s is None else poll_s
    deadline = clock() + wait
    while True:
        async with own_session() as db:
            record = await get_request(db, request_id)
            if record is None:
                raise InvalidRequestError(Reason.NOT_FOUND, 'No ingest request found with that request id')
            if not record.state.is_open:
                return await _outcome(db, record)
        remaining = deadline - clock()
        if remaining <= 0:
            raise ExogenousError(
                Reason.DEADLINE,
                'The request did not finish within the fetch deadline; it keeps running and its outcome is in the run history.',
            )
        await sleep(min(interval, remaining))


async def _outcome(db: AsyncSession, record: IngestRequestRecord) -> StoredDataset:
    """What a finished request is to its waiter: the data it wrote, or the failure it ended in."""
    if record.state is IngestRequestState.FAILED:
        detail = f' ({record.failure_detail})' if record.failure_detail else ''
        match record.failure_cause:
            case IngestRequestFailureCause.EXPIRED:
                raise ExogenousError(Reason.DEADLINE, _EXPIRED_MESSAGE)
            case IngestRequestFailureCause.PERMANENT:
                raise ExogenousError(Reason.VENDOR_REJECTED, _PERMANENT_MESSAGE + detail)
            case _:
                raise ExogenousError(Reason.VENDOR_UNAVAILABLE, _EXHAUSTED_MESSAGE + detail)
    attempt = await finishing_attempt(db, record.id)
    if attempt is None or attempt.served_start is None or attempt.served_end is None:
        raise ExogenousError(
            Reason.PEER_PROTOCOL_ERROR, 'The request finished but its attempt row carries no served range.'
        )
    return StoredDataset(
        data_points=attempt.bars_written,
        served_range=fetch_dataset.ServedRange(start=attempt.served_start, end=attempt.served_end),
    )


async def store_market_activity_worker(
    path: StoreAssetDatasetPath,
    body: StoreAssetDatasetBody,
    db: AsyncSession,
    client: IngestSubmitClient,
    *,
    deadline_s: float | None = None,
) -> StoredDataset:
    """POST /store: write the request, submit it, wait for its verdict.

    Args:
        path: The asset and data type.
        body: What the caller asked for; also the entry's identity fields.
        db: The request's session.
        client: The Submit seam, as the interface.
        deadline_s: How long to wait for the verdict; None reads the setting.

    Returns:
        StoredDataset: The bars written and the served window of the finishing attempt.

    Raises:
        OwnOverlapConflict: The entry overlaps one of the same owner's datasets (409 at the edge); nothing is written
            or sent.
        TraderJoeError: A refused feed, a failed call, the request's failure, or the deadline. Deliberately not caught
            here or in the route (D6).
    """
    record = await submit_bars(
        path, body, db, client, trigger=IngestRunTrigger.REQUEST, client_label=body.owner, ready_by=body.ready_by
    )
    return await await_verdict(record.id, deadline_s=deadline_s)
