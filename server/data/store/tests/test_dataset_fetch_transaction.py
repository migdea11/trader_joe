"""THE POST'S REQUEST PATH: the order of its writes around the Submit, pinned (validator, tj-7zjov1.28, P35).

WHAT THIS FILE PINNED BEFORE, AND WHY IT WAS REWRITTEN. Until P35 this file was "the order and the one transaction"
of the FetchDataset worker (tj-3mk3u5.10, tj-hywf7w): own-overlap SELECT, then the stream, the entry upsert on the
ack, each page written before the next was pulled, one commit after the done, a rollback on every failure. THAT
WHOLE BEHAVIOUR IS GONE from the POST: store_market_activity_worker no longer holds a stream and writes no entry and
no bar. Its properties live on in two places, and each old case is accounted for below rather than deleted silently:

  - THE ONE TRANSACTION, the page-by-page write, the entry written from the resolved tape and never from the body,
    the collect lock and flip, and "a failure writes nothing" are now the DELIVERY INTAKE's: one transaction per
    delivered attempt (lock the request, write the entry on the first delivered attempt, the bars page by page, the
    attempt row, the request's outcome) - test_delivery_intake.py (validator part B of tj-7zjov1.28), which drives
    the real intake over SQLite, and test_bar_batch_chunking.py for the per-page chunk size;
  - THE ORDER ON THE POST'S SIDE is new and is this file's subject. The cases that pinned the request's outward
    shape (the body's tape preference travels outward; a body naming none sends none; the caller's principal is
    the one ingest acts for) are kept, re-pointed at the SubmitRequest.

THE ORDER, as data_action_request.py's module docstring states it and as built in 12768ab:
    1. refuse an own-overlap (a POST only) before anything is written or sent: one local SELECT;
    2. WRITE THE REQUEST ROW (PENDING) and COMMIT it, BEFORE sending, so a store restart finds what was in flight;
    3. SUBMIT; an accepted answer notes the instance and moves the request to QUEUED under it, never under an
       instance already superseded; a refusal finishes the request FAILED (PERMANENT); a call that fails leaves it
       PENDING for the instance watcher;
    4. (the POST then waits for the verdict: the waiter's tests are part B's.)
Each of those clauses is about WHEN something happens relative to something else, so the evidence is ONE JOURNAL:
the SQL statements and commits the engine saw, and the Submit the double received, in the order they happened.

WHAT TIER THIS IS. The real submit path over real tables in a SQLite file (post_path.py), with the Submit seam a
double. That Postgres commits what SQLite does is the system tier's.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import ERROR_DOMAIN, ExogenousError, InvalidRequestError, Reason
from data.store.app.database.crud.ingest_request import get_request
from data.store.app.database.crud.stock.store_dataset_entry import OwnOverlapConflict
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestFailureCause, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRunTrigger
from data.store.app.ingest import data_action_request
from data.store.tests.delivery_reports import INSTANCE_A, INSTANCE_B
from data.store.tests.post_path import PostStore, SubmitDouble
from data.store.tests.post_path import post_store as _post_store  # noqa: F401 (a fixture)
from data.store.tests.test_ui_datasets_route import aware_sqlite_datetimes, entry  # noqa: F401 (a fixture)
from schemas.data_ingest import fetch_dataset
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

PRINCIPAL = 'strategy-that-asked'
REQUEST_PATH = StoreAssetDatasetPath(
    asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol='AAPL'
)
READY_BY = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
REQUEST_BODY = StoreAssetDatasetBody(
    owner=PRINCIPAL,
    source=DataSource.ALPACA_API,
    granularity=Granularity.ONE_DAY,
    start=datetime(2026, 1, 1, tzinfo=UTC),
    ready_by=READY_BY,
)


@pytest.fixture
def store(_post_store: PostStore) -> PostStore:  # noqa: F811 (the imported fixture, under its own name here)
    """post_path.py's SQLite world."""
    return _post_store


async def _submit(store: PostStore, client: SubmitDouble, body: StoreAssetDatasetBody = REQUEST_BODY):
    """The POST's half before the wait, with the POST's own arguments (store_market_activity_worker's first call)."""
    async with store.session() as db:
        return await data_action_request.submit_bars(
            REQUEST_PATH,
            body,
            db,
            client,
            trigger=IngestRunTrigger.REQUEST,
            client_label=body.owner,
            ready_by=body.ready_by,
        )


async def test_the_request_is_committed_before_the_submit_and_queued_after_its_answer(store: PostStore):
    """Steps 1 to 3 on one timeline: overlap SELECT, the request INSERT and its COMMIT, the Submit, then QUEUED.

    A request written AFTER the Submit (or inside a transaction still open when the Submit goes out) would be lost by
    a store restart in between, with ingest holding work the store has no record of: the restart-safety the request
    record exists for. A QUEUED mark written BEFORE the answer would claim an instance that may never have taken it.
    The POST writes no entry and no bar of its own: those are the delivery's.
    """
    client = SubmitDouble.silent(store)

    queued = await _submit(store, client)

    journal = store.journal
    insert = journal.index('INSERT ingest_request')
    submit = journal.index('submit')
    queue = journal.index('UPDATE ingest_request')
    assert journal.index('SELECT store_dataset_entry') < insert, f'the overlap was not checked first: {journal}'
    assert insert < journal.index('COMMIT', insert) < submit, (
        f'the request was not committed before the Submit: {journal}'
    )
    assert submit < queue, f'the request was marked QUEUED before ingest answered: {journal}'
    assert queue < journal.index('COMMIT', queue), f'the QUEUED mark was never committed: {journal}'
    assert not [
        step
        for step in journal
        if step.endswith(('store_dataset_entry', 'stock_market_activity')) and not step.startswith('SELECT')
    ], f'the POST wrote an entry or a bar itself: {journal}'
    assert queued.state is IngestRequestState.QUEUED
    assert queued.instance_id == INSTANCE_A
    (stored,) = await store.requests()
    assert IngestRequestState(stored.state) is IngestRequestState.QUEUED
    assert str(stored.id) == client.submitted[0].request_id == str(queued.id)


async def test_an_own_overlap_is_refused_before_anything_is_written_or_sent(store: PostStore):
    """One local SELECT decides it: no request row, no Submit, so no vendor call and no queue slot is spent on a 409."""
    await store.add(entry('AAPL', start=datetime(2025, 12, 1, tzinfo=UTC)))
    clash = REQUEST_BODY.model_copy(update={'owner': 'operator'})
    client = SubmitDouble(store)

    store.journal.clear()

    with pytest.raises(OwnOverlapConflict):
        await _submit(store, client, clash)

    journal = list(store.journal)
    assert client.submitted == [], 'ingest was asked for a request the store already knew it would refuse'
    assert await store.requests() == []
    assert not [step for step in journal if step.startswith(('INSERT', 'UPDATE'))], journal


async def test_a_refused_answer_finishes_the_request_failed_and_raises_the_refusal(store: PostStore):
    """Ingest refused the request for good (a tape it cannot serve): FAILED, cause PERMANENT, never QUEUED."""
    refused = fetch_dataset.FetchRefused(
        reason=Reason.FEED_NOT_AVAILABLE.value, domain=ERROR_DOMAIN, detail='not this tape', metadata={'feed': 'sip'}
    )
    client = SubmitDouble(store, refused=refused)

    with pytest.raises(InvalidRequestError) as raised:
        await _submit(store, client, REQUEST_BODY.model_copy(update={'feed': Feed.SIP}))

    assert raised.value.reason is Reason.FEED_NOT_AVAILABLE
    (stored,) = await store.requests()
    assert IngestRequestState(stored.state) is IngestRequestState.FAILED
    assert stored.failure_cause == IngestRequestFailureCause.PERMANENT
    assert stored.failure_detail == 'not this tape'
    assert stored.finished_at is not None
    assert 'UPDATE ingest_request' in store.journal[store.journal.index('submit') :]


async def test_a_failed_call_leaves_the_request_pending_for_the_watcher(store: PostStore):
    """A call that fails writes nothing after the request row: PENDING, no instance; what the call raised propagates."""
    error = ExogenousError(Reason.PEER_UNAVAILABLE, 'ingest is not there')
    client = SubmitDouble(store, raises=error)

    with pytest.raises(ExogenousError) as raised:
        await _submit(store, client)

    journal = list(store.journal)
    assert raised.value is error
    (stored,) = await store.requests()
    assert IngestRequestState(stored.state) is IngestRequestState.PENDING
    assert stored.instance_id is None
    assert journal[-1] == 'submit', f'something was written after a failed call: {journal}'


async def test_an_answer_from_a_superseded_instance_never_queues_the_request(store: PostStore):
    """Note F on tj-7zjov1.28: the submit path must not write QUEUED under an instance that is already superseded.

    Nothing would ever work on such a request (the sighting that superseded the instance marked only the rows QUEUED
    in its own snapshot), so it is left PENDING for the watcher. INSTANCE_A answers here, already superseded by
    INSTANCE_B. The interleaving on Postgres (the supersession committing between the answer and the mark) is the
    system tier's; this pins the guard's own decision.
    """
    now = datetime.now(UTC)
    await store.add(
        IngestInstance(instance_id=INSTANCE_A, first_seen_at=now, last_seen_at=now, superseded_at=now),
        IngestInstance(instance_id=INSTANCE_B, first_seen_at=now, last_seen_at=now, superseded_at=None),
    )
    client = SubmitDouble.silent(store)

    left = await _submit(store, client)

    assert left.state is IngestRequestState.PENDING
    (stored,) = await store.requests()
    assert IngestRequestState(stored.state) is IngestRequestState.PENDING
    assert stored.instance_id is None


async def test_a_live_instance_answer_after_a_not_delivered_mark_queues_it_again(store: PostStore):
    """The positive that keeps the guard above honest: NOT_DELIVERED re-submitted and answered by the live instance."""
    client = SubmitDouble.silent(store)
    first = await _submit(store, client)
    async with store.session() as db:
        await db.execute(
            update(IngestRequest).where(IngestRequest.id == first.id).values(state=IngestRequestState.NOT_DELIVERED)
        )
        await db.commit()
    async with store.session() as db:
        record = await get_request(db, first.id)
    resend = SubmitDouble.silent(store)

    again = await data_action_request.submit_record(record, resend, recovered=True)

    assert again.state is IngestRequestState.QUEUED
    assert again.instance_id == INSTANCE_A
    (submitted,) = resend.submitted
    assert submitted.recovered is True
    assert submitted.request_id == str(first.id)


# --------------------------------------------------------------------------------------------- the outward request


async def test_the_bodys_preference_travels_outward_on_the_submit(store: PostStore):
    """A named tape is ingest's to CHECK (never to substitute), so it must reach the Submit and the request row as sent."""
    client = SubmitDouble.silent(store)

    await _submit(store, client, REQUEST_BODY.model_copy(update={'feed': Feed.SIP}))

    (submitted,) = client.submitted
    assert submitted.bars.feed is Feed.SIP
    (stored,) = await store.requests()
    assert stored.feed is Feed.SIP


async def test_a_body_naming_no_tape_sends_none_outward(store: PostStore):
    """An absent feed means "the deployment decides"; a POST that named one would turn an ordinary request into a check."""
    client = SubmitDouble.silent(store)

    await _submit(store, client)

    (submitted,) = client.submitted
    assert submitted.bars.feed is None
    (stored,) = await store.requests()
    assert stored.feed is None


async def test_the_submit_and_the_request_row_agree_with_what_the_caller_sent(store: PostStore):
    """The caller's principal, window, deadline and identity fields reach the Submit and the row, by VALUE.

    The principal: owner is identity on the entry the first delivered attempt writes (from the row's client_label),
    and it is the principal ingest acts for (the Submit's owner); a substituted value would write the dataset under
    somebody else and fetch for them. The rest are the fields the entry and the fetch must not disagree on.
    """
    client = SubmitDouble.silent(store)

    await _submit(store, client)

    (submitted,) = client.submitted
    (stored,) = await store.requests()
    assert submitted.owner == stored.client_label == PRINCIPAL
    assert submitted.ready_by == stored.ready_by == READY_BY
    assert submitted.attempt_count == 0 and submitted.recovered is False
    assert submitted.best_effort is stored.best_effort is False
    bars = submitted.bars
    assert (bars.asset_symbol, stored.asset_symbol) == ('AAPL', 'AAPL')
    assert bars.asset_type is stored.asset_type is AssetType.STOCK
    assert bars.source is stored.source is DataSource.ALPACA_API
    assert bars.granularity is stored.granularity is Granularity.ONE_DAY
    assert bars.start == stored.requested_start == REQUEST_BODY.start
    assert bars.end is None and stored.requested_end is None
    assert bars.update_type is UpdateType.STATIC and stored.update_type == UpdateType.STATIC.value
    assert bars.data_types == [REQUEST_PATH.data_type] and list(stored.data_types) == [REQUEST_PATH.data_type.value]
    assert stored.trigger is IngestRunTrigger.REQUEST
    assert stored.dataset_id is None, 'a POST names no dataset: its first delivered attempt writes the entry'


@pytest.mark.parametrize('collect', [None, False, True], ids=['omitted', 'false', 'true'])
async def test_a_post_records_its_lifecycle_fields_from_the_body(store: PostStore, collect):
    """Note H on tj-7zjov1.28, the POST writer: expiry, expiry type and collect as sent; an omitted collect is NULL.

    The first delivered attempt writes the entry from these columns, so a POST that dropped one would create an entry
    with a lifecycle the caller never asked for; an omitted collect stored as true or false would let an SDK retry
    undo an operator's Stop on a repeat.
    """
    expiry = datetime(2026, 3, 1, tzinfo=UTC)
    body = REQUEST_BODY.model_copy(
        update={
            'update_type': UpdateType.DAILY,
            'expiry_type': ExpiryType.ROLLING,
            'expiry': expiry,
            'collect': collect,
        }
    )
    client = SubmitDouble.silent(store)

    await _submit(store, client, body)

    (stored,) = await store.requests()
    assert stored.trigger is IngestRunTrigger.REQUEST
    assert stored.expiry == expiry
    assert stored.expiry_type == ExpiryType.ROLLING.value
    assert stored.collect is collect


async def test_a_static_post_records_its_bulk_expiry_type(store: PostStore):
    """The body's own declared expiry type, its default included (BULK for STATIC): the entry's identity needs it."""
    client = SubmitDouble.silent(store)

    await _submit(store, client)

    (stored,) = await store.requests()
    assert stored.expiry_type == ExpiryType.BULK.value
    assert (stored.collect, stored.expiry) == (None, REQUEST_BODY.expiry)


async def test_the_post_waits_for_the_verdict_up_to_its_deadline_and_the_request_continues(store: PostStore):
    """Owner ruling R4 (a): a POST that outlives its deadline answers DEADLINE and its request is left running.

    The waiter's own cases are part B's; this is the POST's composition of the two halves, end to end. Since P51 the
    one deadline covers the clamp's query too, so it is long enough for the (answered) query and runs out on the
    silent bars request.
    """
    client = SubmitDouble.silent(store)

    with pytest.raises(ExogenousError) as raised:
        async with store.session() as db:
            await data_action_request.store_market_activity_worker(
                REQUEST_PATH, REQUEST_BODY, db, client, deadline_s=0.5
            )

    assert raised.value.reason is Reason.DEADLINE
    (stored,) = await store.bars_requests()
    assert IngestRequestState(stored.state) is IngestRequestState.QUEUED, 'the request was abandoned at the deadline'


async def test_a_delivered_answer_is_what_the_post_returns(store: PostStore):
    """The other end of the composition: ingest delivers, the waiter answers from the attempt row's served window."""
    client = SubmitDouble(store)

    async with store.session() as db:
        stored = await data_action_request.store_market_activity_worker(
            REQUEST_PATH, REQUEST_BODY, db, client, deadline_s=5
        )

    assert stored.data_points == 0
    assert stored.served_range == client.delivery.served
    assert stored.served_range.end != REQUEST_BODY.end, 'the served window was echoed from the request'
    (request,) = await store.bars_requests()
    assert request.dataset_id == store.entry_id, 'the first delivered attempt did not give the request its entry'
    assert timedelta(0) <= (request.finished_at - request.created_at) < timedelta(seconds=5)
    assert uuid.UUID(client.submitted[0].request_id) == request.id
