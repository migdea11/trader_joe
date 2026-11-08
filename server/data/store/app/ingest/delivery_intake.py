"""THE DELIVERY INTAKE: the store's side of the result path, StoreService behind common/rpc/store.py (tj-7zjov1.27, P34).

Ingest reports every attempt, success or failure, and does nothing more for that request until this module has
ANSWERED. The answer is therefore the acknowledgement, and the one rule that matters is that it is sent only after
everything it vouches for is durable.

ONE TRANSACTION PER DELIVERED ATTEMPT. Inside a single write_transaction, in this order:

    1. lock the request row (FOR UPDATE). Everything below assumes the lock.
    2. answer a duplicate from what is stored, writing nothing (below).
    3. refuse a report that breaks a cross-field rule, writing nothing (below).
    4. write the data, through the kind's arm.
    5. write the attempt row: quota key, spend, verdict (failed attempts included).
    6. add the attempt to the request's durable count and its spend, and move its state.
    COMMIT, and only then return the answer. A failure anywhere rolls all of it back, the error leaves as a typed
    status, and ingest redelivers the same attempt number: not a broker retry, no vendor call, no double count.

IDEMPOTENCY. The key is (request_id, attempt_no) in the header. The request lock serialises two deliveries of one
request, so the lookup in step 2 always sees the committed outcome of the one before it; the partial unique index on
ingest_run is the backstop and insert_attempt names it as the arbiter. A report whose attempt row exists is answered
from that row (its stored verdict, the request's durable count) and writes nothing, not even last_seen_at. The same
holds for an EXPIRED or EXHAUSTED report, which has no attempt row: it is a duplicate when the request is already
finished, and is answered FINAL from the request. A report that reuses a number another INSTANCE already recorded is
not the same report: from a superseded instance it is answered FINAL and dropped (the live instance's record stands
and the old process stops); from the live instance it is REFUSED, loudly, because answering it from the other
instance's row would leave a request nothing is working on. THE COLLISION IS PREVENTED UPSTREAM: P35 waits out the
delivery deadline (60 s) plus a margin before it resubmits a request, so a late report from the old instance has
landed or died first. Both branches are therefore GUARDS, and each logs at ERROR (ids only). See DeliveryIntake.deliver.

ORDERING. The instance sighting (header.instance_id) commits BEFORE the delivery transaction, on its own: the
marker is true whether or not this delivery succeeds, and a first sighting is what makes an older instance
superseded, which step 6 reads. A crash between the two leaves a noticed instance and an undelivered attempt, and the
redelivery repairs the second. Within a request, attempts are ordered by the lock; across requests nothing is
ordered and nothing needs to be.

WHAT A REPORT FROM A SUPERSEDED INSTANCE DOES. Its attempt happened and cost what it cost, so it is written (row,
data, count, spend), and it is answered FINAL so that process stops. It finishes the request only when it is
conclusive by itself: a success, or a permanent refusal. A failure that would have been retried leaves the request
as it was, for the live instance to carry on, and a last_attempt flag on it is ignored (the live instance holds the
budget and reads the durable count we return).

THE ARMS (bars, asset list, corporate actions) ARE SEPARATE FUNCTIONS, called inside the one transaction at step 4,
so the beads that fill them in add to a function and never to the shape of the transaction: P16 adds the write-time
clip and the coverage write to the bars arm, P17 the asset-list snapshot, P18 and P44 the corporate-action rows.
UNTIL AN ARM LANDS, ITS PAYLOAD IS REFUSED (INVALID_REQUEST), NEVER ACKNOWLEDGED UNWRITTEN: an asset list cannot be
fetched again, so an acknowledgement that wrote nothing would lose it for good (CLAUDE.md, what may cross the wire).
For the same reason no attempt of those kinds is recorded yet: ingest_run's label columns are NOT NULL and a
request of those kinds has no symbol, granularity or data type to supply them. Only EXPIRED and EXHAUSTED, which
write no attempt row, are accepted for them.

THE VENDOR ASSET ID. The header's vendor_asset_id is stored as received, opaque, in ingest_run.vendor_asset_id on the
attempt row of every attempt that writes one. An empty id is stored NULL, never an empty string; a request of another
kind writes no attempt row yet. Nothing here compares it, looks anything up or names a listing: the store only stores
it (the stop rule that reads it is a later bead). It is reproducible by an asset lookup, so the wire rule is not engaged.

THE LABEL (client_label) AND THE QUOTA KEY ARE NEVER LOGGED beyond what a log line below names: ids, numbers, enums.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import DataType
from common.environment import get_env_var
from common.errors.vocabulary import InvalidRequestError, Reason
from common.logging import get_logger
from data.store.app.database.crud.ingest_delivery import (
    AttemptLabel,
    NewAttempt,
    RequestOutcome,
    apply_outcome,
    find_attempt,
    insert_attempt,
    instance_is_superseded,
    lock_dataset_entry,
    lock_request,
)
from data.store.app.database.crud.ingest_instance import note_instance_seen
from data.store.app.database.crud.ingest_request import IngestRequestRecord
from data.store.app.database.crud.ingest_run import IngestRunRecord
from data.store.app.database.crud.stock.asset_market_activity import write_market_activity_in_transaction
from data.store.app.database.database import own_session
from data.store.app.database.models.ingest_request import (
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRunState, IngestRunTrigger
from data.store.app.database.transaction import write_transaction
from data.store.app.events import DatasetChange, DatasetChanged, IngestRunChanged, publish
from schemas.data_ingest.delivery import (
    AssetRecords,
    AttemptDelivery,
    CorporateActionRecords,
    DeliverAttemptResponse,
    DeliveryErrorClass,
    DeliveryOutcome,
    DeliveryPayload,
    DeliveryVerdict,
    RegisterInstanceRequest,
)
from schemas.data_ingest.fetch_dataset import BarPage
from schemas.data_store.stock.market_activity_data import (
    BatchStockDataMarketActivityCreate,
    StockDataMarketActivityData,
)


log = get_logger(__name__)

# The kind each payload message type belongs to (rule f).
_PAYLOAD_KIND: dict[type, IngestRequestKind] = {
    BarPage: IngestRequestKind.BARS,
    AssetRecords: IngestRequestKind.ASSET_LIST,
    CorporateActionRecords: IngestRequestKind.CORPORATE_ACTIONS,
}

# The outcomes that made no vendor call: no attempt row, no counted attempt, no spend.
_NO_CALL = frozenset({DeliveryOutcome.EXPIRED, DeliveryOutcome.EXHAUSTED})


def _refuse(detail: str) -> InvalidRequestError:
    """The refusal of a report the store will not write. The detail is a fixed sentence, never a value."""
    return InvalidRequestError(Reason.INVALID_REQUEST, detail)


# ---------------------------------------------------------------------------------------------
# THE CROSS-FIELD RULES the carrying layer leaves to the store (architect, P32 gate and P36 gate notes on tj-7zjov1.27)


def validate_report(attempt: AttemptDelivery, kind: IngestRequestKind) -> None:
    """Refuse a report whose fields contradict each other or the request it names. Writes nothing.

    The rules: (a) an EXPIRED or EXHAUSTED report carries no stats, as_of, served range, payload, quota key or vendor
    asset id; (b) error_class is set on FAILED and only there, and a FAILED report has no payload; (c) last_attempt only
    on FAILED; (d) served_range only on SUCCEEDED; (g) a SUCCEEDED or FAILED report names a quota key; (h) a SUCCEEDED
    report carries vendor stats (a FAILED one may not: the spend is then unknown, see _deliver_in_transaction); (e) the
    feed follows the request kind; (f) the payload is of the request's kind.

    Args:
        attempt: The assembled report.
        kind: The kind of the request the header names, from the request record.

    Raises:
        InvalidRequestError: INVALID_REQUEST, naming the rule and never a value.
    """
    trailer, header, outcome = attempt.trailer, attempt.header, attempt.trailer.outcome
    if outcome in _NO_CALL:
        # (a) An expired or exhausted report made no vendor call, so it carries nothing a call would produce.
        if trailer.vendor_stats is not None:
            raise _refuse(f'{outcome} report carries vendor stats, but it made no vendor call')
        if trailer.as_of is not None:
            raise _refuse(f'{outcome} report carries an as_of, but it made no vendor call')
        if trailer.served_range is not None:
            raise _refuse(f'{outcome} report carries a served range, but it made no vendor call')
        if attempt.parts:
            raise _refuse(f'{outcome} report carries a payload, but it made no vendor call')
        if header.quota_key:
            raise _refuse(f'{outcome} report names a quota key, but it was charged to no quota')
        if header.vendor_asset_id:
            raise _refuse(f'{outcome} report names a vendor asset id, but it made no vendor call')
    # (b) error_class belongs to FAILED, and a FAILED report must say how it failed.
    if outcome is DeliveryOutcome.FAILED:
        if trailer.error_class is None:
            raise _refuse('FAILED report carries no error class')
        # store.proto: a failed attempt carries no payload.
        if attempt.parts:
            raise _refuse('FAILED report carries a payload')
    elif trailer.error_class is not None:
        raise _refuse(f'{outcome} report carries an error class, which belongs to FAILED only')
    # (c) last_attempt is meaningful on FAILED only.
    if trailer.last_attempt and outcome is not DeliveryOutcome.FAILED:
        raise _refuse(f'{outcome} report carries last_attempt, which belongs to FAILED only')
    # (d) A served range is the window a SUCCEEDED attempt answered for.
    if trailer.served_range is not None and outcome is not DeliveryOutcome.SUCCEEDED:
        raise _refuse(f'{outcome} report carries a served range, which belongs to SUCCEEDED only')
    # (g) A counted attempt was charged to a quota, and the charge needs its name.
    if outcome not in _NO_CALL and not header.quota_key:
        raise _refuse('a counted attempt names no quota key')
    # (h) A success reports what it cost. A FAILED report without stats is ACCEPTED (the attempt runner's crash path
    # sends a permanent failure with none, and refusing it would stall that failure for ever); its spend is unknown.
    if outcome is DeliveryOutcome.SUCCEEDED and trailer.vendor_stats is None:
        raise _refuse('a SUCCEEDED report carries no vendor stats')
    # (e) THE FEED RULE, judged against the request's kind because only the store holds it for every outcome: a bars
    # report names its resolved tape whatever the outcome (an expired or exhausted one included); the other kinds
    # have no tape.
    if kind is IngestRequestKind.BARS:
        if header.feed is None:
            raise _refuse('a bars report carries no resolved feed')
    elif header.feed is not None:
        raise _refuse(f'{kind} report carries a feed, but that kind has no tape')
    # (f) The payload is of the request's kind.
    for part in attempt.parts:
        if _PAYLOAD_KIND[type(part)] is not kind:
            raise _refuse(f'the payload does not match the {kind} request it names')


# ---------------------------------------------------------------------------------------------
# THE ARMS: what is written for a SUCCEEDED attempt of each kind. Each runs inside the delivery transaction, before
# the attempt row and the request update, and returns how many records it wrote.


@dataclass(frozen=True)
class ArmContext:
    """What an arm is given: the session inside the delivery transaction, the locked request and the report."""

    db: AsyncSession
    request: IngestRequestRecord
    attempt: AttemptDelivery
    chunk_size: int | None


Arm = Callable[[ArmContext], Awaitable[int]]


async def write_bars(context: ArmContext) -> int:
    """The bars arm: upsert every bar of every page under the request's dataset, through the existing bar writer.

    P16 adds the write-time clip against the listing and the dataset_coverage write HERE, at this seam.

    Args:
        context: The delivery in progress.

    Returns:
        int: How many bars were written.

    Raises:
        InvalidRequestError: INVALID_REQUEST, if the request has no dataset to write under, a bar names another
            tape than the header's, or a bar cannot be stored; DUPLICATE_BAR_TIMESTAMP, if one page carries a
            timestamp twice. Nothing is kept: the transaction rolls back.
    """
    request, header = context.request, context.attempt.header
    pages = [part for part in context.attempt.parts if isinstance(part, BarPage)]
    if not any(page.bars for page in pages):
        return 0
    if request.dataset_id is None:
        # Bars are keyed on a dataset. Until the submit path (P35) gives a bars request its dataset, there is none,
        # and acknowledging bars that were not stored would be a lie.
        raise _refuse('the request has no dataset to store these bars under')
    if request.asset_symbol is None or request.granularity is None:
        raise _refuse('the request does not name the symbol and granularity these bars belong to')
    # NO ORPHAN BARS: bars carry a foreign key to their dataset (ON DELETE CASCADE), so the database itself never keeps
    # a bar whose entry is gone, and that key stays the backstop. The entry is locked FOR UPDATE anyway so that a
    # delivery for a deleted entry is refused cleanly below instead of surfacing a foreign-key violation as a database
    # error, so the delivery serialises with the operator's delete and the reaper (they lock the same row), and so
    # P35's drop rule (gone or retired) has one place to judge under the lock. A dataset that is merely stopped is
    # P35's rule, not this arm's.
    if not await lock_dataset_entry(context.db, request.dataset_id):
        raise _refuse('the dataset this request writes to no longer exists')
    written = 0
    for page in pages:
        batch = BatchStockDataMarketActivityCreate(
            dataset_id=request.dataset_id,
            asset_symbol=request.asset_symbol,
            source=request.source,
            granularity=request.granularity,
            # The header's tape: rule (e) made it present, and every bar below is held to it.
            feed=header.feed,
            dataset={},
        )
        for bar in page.bars:
            if bar.feed != header.feed:
                raise _refuse('a bar names another tape than the report header')
            try:
                data = StockDataMarketActivityData(
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    trade_count=bar.trade_count,
                )
            except ValidationError as error:
                # Parity with the POST path: a bar the stored shape cannot hold (no trade count, a fractional volume)
                # is refused, not given a made-up value. The error text carries values and is not kept.
                raise _refuse('a bar cannot be stored: its volume or trade count is not a whole number') from error
            # bar_start, the instant the bar OPENS, is the stored timestamp.
            batch.append_data(DataType.MARKET_ACTIVITY, data, bar.bar_start)
        written += await write_market_activity_in_transaction(
            context.db, batch, requested_chunk_size=context.chunk_size
        )
    return written


async def write_asset_list(context: ArmContext) -> int:
    """The asset-list arm. NOT BUILT (P17): refuses, because an asset list cannot be fetched again.

    Raises:
        InvalidRequestError: INVALID_REQUEST, always.
    """
    raise _refuse('asset-list intake is not built yet; refused rather than acknowledged unwritten')


async def write_corporate_actions(context: ArmContext) -> int:
    """The corporate-actions arm. NOT BUILT (P18, P44): refuses any payload; a success with none writes nothing.

    Args:
        context: The delivery in progress.

    Returns:
        int: Zero, for a success that carries no records.

    Raises:
        InvalidRequestError: INVALID_REQUEST, if the report carries a payload message.
    """
    if context.attempt.parts:
        raise _refuse('corporate-action intake is not built yet; refused rather than acknowledged unwritten')
    return 0


ARMS: dict[IngestRequestKind, Arm] = {
    IngestRequestKind.BARS: write_bars,
    IngestRequestKind.ASSET_LIST: write_asset_list,
    IngestRequestKind.CORPORATE_ACTIONS: write_corporate_actions,
}


def _label_for(request: IngestRequestRecord) -> AttemptLabel:
    """The NOT NULL label columns of the attempt row, from the request. Only a bars request has them.

    Raises:
        InvalidRequestError: INVALID_REQUEST, for a request of another kind or one missing a label field. No
            sentinel stands in for a value the store does not know.
    """
    if request.kind is not IngestRequestKind.BARS:
        raise _refuse(f'an attempt of a {request.kind} request cannot be recorded yet')
    if (
        request.asset_symbol is None
        or request.asset_type is None
        or not request.data_types
        or request.granularity is None
        or request.update_type is None
    ):
        raise _refuse('the request does not carry the labels an attempt row needs')
    try:
        data_type = DataType(request.data_types[0])
    except ValueError as error:
        raise _refuse('the request names a data type the store does not know') from error
    return AttemptLabel(
        asset_symbol=request.asset_symbol,
        asset_type=request.asset_type,
        data_type=data_type,
        granularity=request.granularity,
        update_type=request.update_type,
    )


# ---------------------------------------------------------------------------------------------
# THE VERDICT


@dataclass(frozen=True)
class Decision:
    """What a report means for its request: the answer, and the terminal state it moves an OPEN request to."""

    verdict: DeliveryVerdict
    # None leaves the request's state alone (a retry is still wanted, or the report is not conclusive).
    state: IngestRequestState | None
    cause: IngestRequestFailureCause | None


def decide(attempt: AttemptDelivery, *, bars_written: int, has_records: bool, superseded: bool) -> Decision:
    """The verdict rule: FINAL on a permanent refusal, the last attempt, a superseded request or instance; else CONTINUE.

    An expired report is FINAL with cause EXPIRED, an exhausted one FINAL with cause ATTEMPTS_EXHAUSTED; neither is a
    vendor failure. A success is FINAL and EMPTY when it carried no record (the vendor answered and held none).

    Args:
        attempt: The validated report.
        bars_written: Bars the bars arm wrote.
        has_records: Whether the success carried any record at all (a bar, an asset, an action).
        superseded: Whether the reporting instance has been superseded by a newer one.

    Returns:
        Decision: The verdict and the state change for an open request.
    """
    trailer = attempt.trailer
    match trailer.outcome:
        case DeliveryOutcome.EXPIRED:
            return Decision(DeliveryVerdict.FINAL, IngestRequestState.FAILED, IngestRequestFailureCause.EXPIRED)
        case DeliveryOutcome.EXHAUSTED:
            return Decision(
                DeliveryVerdict.FINAL, IngestRequestState.FAILED, IngestRequestFailureCause.ATTEMPTS_EXHAUSTED
            )
        case DeliveryOutcome.SUCCEEDED:
            state = IngestRequestState.SUCCEEDED if (bars_written > 0 or has_records) else IngestRequestState.EMPTY
            return Decision(DeliveryVerdict.FINAL, state, None)
        case DeliveryOutcome.FAILED:
            if trailer.error_class is DeliveryErrorClass.PERMANENT:
                return Decision(DeliveryVerdict.FINAL, IngestRequestState.FAILED, IngestRequestFailureCause.PERMANENT)
            if superseded:
                # The old process stops; the live instance carries the request on from the durable count.
                return Decision(DeliveryVerdict.FINAL, None, None)
            if trailer.last_attempt:
                return Decision(
                    DeliveryVerdict.FINAL, IngestRequestState.FAILED, IngestRequestFailureCause.ATTEMPTS_EXHAUSTED
                )
            return Decision(DeliveryVerdict.CONTINUE, None, None)
    raise AssertionError(f'unhandled outcome {trailer.outcome}')  # pragma: no cover


# ---------------------------------------------------------------------------------------------
# THE HANDLER


@dataclass(frozen=True)
class _Delivered:
    """The result of the delivery transaction, carried out of it for the answer and the after-commit events."""

    response: DeliverAttemptResponse
    run: IngestRunRecord | None = None
    dataset_id: uuid.UUID | None = None


class DeliveryIntake:
    """The store's DeliveryHandler: writes each delivered attempt in one transaction, then answers.

    Structural conformance to common.rpc.store.DeliveryHandler; it does not import it.
    """

    def __init__(self, *, chunk_size: int | None = None) -> None:
        """Bind the intake.

        Args:
            chunk_size: Rows per bar statement. None reads MARKET_ACTIVITY_BATCH_SIZE, as the POST path does; the
                bar writer clamps an unset or oversized value to what the driver accepts.
        """
        self._chunk_size = (
            chunk_size if chunk_size is not None else get_env_var('MARKET_ACTIVITY_BATCH_SIZE', cast_type=int)
        )

    async def register_instance(self, request: RegisterInstanceRequest) -> None:
        """Record the instance ingest announced. Idempotent: an id already held only moves last_seen_at.

        A new id supersedes the older instances and marks their QUEUED requests NOT_DELIVERED, in the marker's own
        transaction; the re-submission of those is the watcher's (P35), which reads them from the request table.

        Args:
            request: The instance id and when that process started.
        """
        async with own_session() as db:
            sighting = await note_instance_seen(db, request.instance_id)
        if sighting.is_new:
            log.info(
                f'Ingest instance registered: {len(sighting.superseded)} older instance(s) superseded, '
                f'{len(sighting.not_delivered)} request(s) marked not delivered'
            )

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        """Write one reported attempt durably, then answer. The answer is the acknowledgement.

        Args:
            attempt: The assembled report.

        Returns:
            DeliverAttemptResponse: CONTINUE or FINAL with the durable attempt count, after the commit.

        Raises:
            InvalidRequestError: INVALID_REQUEST for a report the store will not write (nothing is written and the
                sender must not treat it as acknowledged), NOT_FOUND for a request the store does not hold.
            ExogenousError: DATABASE_* if the transaction could not commit; nothing is kept, redeliver.
        """
        try:
            request_id = uuid.UUID(attempt.header.request_id)
        except ValueError as error:
            raise _refuse('the request id is not a request id') from error

        # Recorded first and on its own commit (module docstring, ORDERING).
        async with own_session() as db:
            await note_instance_seen(db, attempt.header.instance_id)

        async with own_session() as db, write_transaction(db, 'store delivered attempt'):
            delivered = await self._deliver_in_transaction(db, attempt, request_id)
        # AFTER THE COMMIT, like every publish: notifications, not data.
        if delivered.run is not None:
            publish(IngestRunChanged(delivered.run))
        if delivered.dataset_id is not None:
            publish(DatasetChanged(delivered.dataset_id, DatasetChange.UPDATED))
        log.info(
            f'Delivery of attempt {attempt.header.attempt_no} of request {request_id} '
            f'({attempt.trailer.outcome}) answered {delivered.response.verdict} at count {delivered.response.attempt_count}'
        )
        return delivered.response

    async def _deliver_in_transaction(
        self, db: AsyncSession, attempt: AttemptDelivery, request_id: uuid.UUID
    ) -> _Delivered:
        """Steps 1 to 6 of the module docstring, on the session inside the delivery transaction. Never commits."""
        header, trailer = attempt.header, attempt.trailer
        # 1. Lock. A request the store does not hold has nowhere to put the report.
        request = await lock_request(db, request_id)
        if request is None:
            raise InvalidRequestError(Reason.NOT_FOUND, 'No ingest request found with that request id')

        # 2. A duplicate is answered from the stored row and writes nothing.
        stored = await find_attempt(db, request_id, header.attempt_no)
        superseded = await instance_is_superseded(db, header.instance_id)
        if stored is not None:
            if stored.instance_id in (None, header.instance_id):
                return _Delivered(self._stored_answer(stored.verdict, request))
            # Another instance already recorded this number. P35 prevents this (it waits out the delivery deadline
            # before it resubmits), so reaching it is a defect to see: a guard, logged at ERROR with ids only.
            log.error(
                f'Attempt {header.attempt_no} of request {request_id} was already recorded by instance '
                f'{stored.instance_id}; reported by instance {header.instance_id} '
                f'({"superseded, dropped FINAL" if superseded else "live, refused"})'
            )
            if superseded:
                return _Delivered(DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=request.attempts))
            raise _refuse('this attempt number was already recorded by another ingest instance')
        if trailer.outcome in _NO_CALL and not request.state.is_open:
            # No attempt row exists for these, so a finished request is the stored answer.
            return _Delivered(DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=request.attempts))

        # 3. Cross-field rules, judged against the request's kind. Still nothing written.
        validate_report(attempt, request.kind)
        if trailer.outcome is DeliveryOutcome.FAILED and trailer.vendor_stats is None:
            # Accepted (rule h), but the spend is unknown, never zero: the attempt row keeps its vendor columns NULL
            # and the request's sums add 0. P34c adds the counter that says so.
            log.warning(
                f'Attempt {header.attempt_no} of request {request_id} failed with no vendor stats: spend unknown'
            )

        # 4. The data, through the kind's arm. Only a success carries any.
        bars_written = 0
        has_records = False
        if trailer.outcome is DeliveryOutcome.SUCCEEDED:
            bars_written = await ARMS[request.kind](ArmContext(db, request, attempt, self._chunk_size))
            has_records = _carries_records(attempt.parts)

        decision = decide(attempt, bars_written=bars_written, has_records=has_records, superseded=superseded)
        counted = trailer.outcome not in _NO_CALL

        # 5. The attempt row, for an attempt that made a vendor call.
        run: IngestRunRecord | None = None
        if counted:
            run = await insert_attempt(db, self._attempt_row(request, attempt, decision, bars_written))
            if run is None:
                # The arbiter index held the attempt although the lookup above did not: not reachable under the lock.
                # Roll everything back rather than count an attempt twice; the redelivery is answered as a duplicate.
                raise _refuse('this attempt was recorded concurrently')

        # 6. Count, spend and state, in one UPDATE. A terminal request keeps its first verdict; the count and the
        # spend are added regardless, because the report happened.
        stats = trailer.vendor_stats
        is_final_for_open = request.state.is_open and decision.state is not None
        outcome = RequestOutcome(
            attempts=1 if counted else 0,
            calls=stats.calls if stats else 0,
            pages=stats.pages if stats else 0,
            throttled_429=stats.throttled_429 if stats else 0,
            retries=stats.retries if stats else 0,
            state=decision.state if is_final_for_open else None,
            failure_cause=decision.cause if is_final_for_open else None,
            failure_detail=(trailer.detail or None) if is_final_for_open and decision.cause is not None else None,
            queued_by=(
                header.instance_id if request.state.is_open and decision.state is None and not superseded else None
            ),
        )
        updated = await apply_outcome(db, request_id, outcome)
        # A terminal request answers FINAL whatever the late report said: it is finished.
        verdict = decision.verdict if request.state.is_open else DeliveryVerdict.FINAL
        return _Delivered(
            DeliverAttemptResponse(verdict=verdict, attempt_count=updated.attempts),
            run=run,
            dataset_id=request.dataset_id if bars_written > 0 else None,
        )

    @staticmethod
    def _stored_answer(verdict: str | None, request: IngestRequestRecord) -> DeliverAttemptResponse:
        """Repeat the answer the stored attempt row recorded, with the request's durable count."""
        if verdict is None:
            # An attempt row without a verdict was not written by this intake. Never guess an acknowledgement.
            raise RuntimeError('the stored attempt carries no verdict')
        return DeliverAttemptResponse(verdict=DeliveryVerdict(verdict), attempt_count=request.attempts)

    @staticmethod
    def _attempt_row(
        request: IngestRequestRecord, attempt: AttemptDelivery, decision: Decision, bars_written: int
    ) -> NewAttempt:
        """The attempt row for a SUCCEEDED or FAILED report.

        The verdict stored is the one the request's state allows: a terminal request answers FINAL.
        """
        header, trailer = attempt.header, attempt.trailer
        stats = trailer.vendor_stats
        verdict = decision.verdict if request.state.is_open else DeliveryVerdict.FINAL
        if trailer.outcome is DeliveryOutcome.FAILED:
            state = IngestRunState.FAILED
        else:
            state = (
                IngestRunState.SUCCEEDED
                if bars_written > 0 or _carries_records(attempt.parts)
                else IngestRunState.EMPTY
            )
        return NewAttempt(
            request=request,
            attempt_no=header.attempt_no,
            instance_id=header.instance_id,
            quota_key=header.quota_key or None,
            verdict=verdict.value,
            state=state,
            trigger=IngestRunTrigger.REQUEST if request.attempts == 0 else IngestRunTrigger.RETRY,
            bars_written=bars_written,
            feed=header.feed,
            stats=stats,
            started_at=stats.first_call_at if stats else None,
            finished_at=trailer.as_of,
            # The human sentence of a failure, as the report gave it; ingest guarantees it carries no secret.
            error_message=(trailer.detail or None) if state is IngestRunState.FAILED else None,
            label=_label_for(request),
            vendor_asset_id=header.vendor_asset_id or None,
        )


def _carries_records(parts: list[DeliveryPayload]) -> bool:
    """Whether any payload message holds a record."""
    for part in parts:
        if isinstance(part, BarPage) and part.bars:
            return True
        if isinstance(part, AssetRecords | CorporateActionRecords) and part.records:
            return True
    return False


__all__ = ['ARMS', 'ArmContext', 'Decision', 'DeliveryIntake', 'decide', 'validate_report']
