"""POST /store's request path (P35, tj-7zjov1.28): write the request, submit it, wait for the verdict. Over SQLite.

THE DESIGN: the bead's acceptance (the daily loop and POST write the request row BEFORE sending, then submit; POST
passes the caller's ready_by or null; POST awaits the verdict up to the fetch deadline), the architect notes A (the
entry on the first delivered attempt), F (never QUEUED under a superseded instance) and H (POST writes trigger
REQUEST plus expiry and collect from the body, true, false and omitted as NULL; the entry takes them), ADR tj-26jt9w
addendum 14 (POST writes the body's expiry_type), and the store CLAUDE.md request-path line.

WHAT IS DRIVEN: data_action_request's own functions (new_bars_request, submit_request_of, submit_record, submit_bars,
store_market_activity_worker, the function POST /store calls and nothing else), through the real crud, against a
FakeSubmit standing in for ingest. Where ingest's report is needed, the test delivers it through the real
DeliveryIntake (request_path_harness.py), so the POST answers from what the intake wrote. The HTTP route itself is
the route tier's (test_store_dataset_entry_route.py, another validator's file).
"""

import functools
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import REASONS, ExogenousError, InvalidRequestError, Reason
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.crud.stock.store_dataset_entry import OwnOverlapConflict
from data.store.app.database.models.ingest_request import (
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRunTrigger
from data.store.app.ingest import data_action_request
from data.store.app.ingest.data_action_request import (
    StoredDataset,
    new_bars_request,
    store_market_activity_worker,
    submit_bars,
    submit_record,
    submit_request_of,
)
from data.store.tests.delivery_reports import report, with_trailer
from data.store.tests.request_path_harness import INSTANCE_A, INSTANCE_B, OWNER, START, FakeSubmit, Harness, make_engine
from schemas.data_ingest.delivery import DeliveryOutcome
from schemas.data_ingest.fetch_dataset import BarPage, ServedRange
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

PATH = StoreAssetDatasetPath(asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol='vfv')
READY_BY = START + timedelta(days=40, hours=2)
EXPIRY = START + timedelta(days=365, minutes=7)


def body(**overrides) -> StoreAssetDatasetBody:
    values = {
        'owner': OWNER,
        'source': DataSource.ALPACA_API,
        'granularity': Granularity.ONE_DAY,
        'start': START,
        'update_type': UpdateType.DAILY,
        'expiry_type': ExpiryType.ROLLING,
        'expiry': EXPIRY,
        'ready_by': READY_BY,
    }
    return StoreAssetDatasetBody(**{**values, **overrides})


@pytest_asyncio.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    engine = await make_engine(monkeypatch)
    try:
        yield Harness(engine, monkeypatch)
    finally:
        await engine.dispose()


async def _post(harness: Harness, client: FakeSubmit, payload: StoreAssetDatasetBody | None = None):
    async with AsyncSession(harness.engine, expire_on_commit=False) as db:
        return await submit_bars(
            PATH,
            payload or body(),
            db,
            client,
            trigger=IngestRunTrigger.REQUEST,
            client_label=(payload or body()).owner,
            ready_by=(payload or body()).ready_by,
        )


# --------------------------------------------------------------------------------------------- the request row


@pytest.mark.parametrize(
    ('collect', 'stored'), [(True, True), (False, False), (None, None)], ids=['true', 'false', 'omitted']
)
async def test_h_post_writes_the_request_row_before_sending_with_trigger_request_and_the_body_lifecycle_fields(
    harness, collect, stored
):
    """POST commits its request row before the Submit, with trigger REQUEST and the body's fields.

    The row is committed (PENDING) BEFORE the Submit is sent, carrying everything the first delivered attempt needs
    to write the entry: trigger REQUEST, the body's expiry, expiry_type and collect (omitted stored NULL), the
    caller's ready_by, no dataset_id.
    """
    client = FakeSubmit()
    seen_at_send: list = []

    async def look(request):
        seen_at_send.append(await harness.request_row(uuid.UUID(request.request_id)))

    client.on_submit = look

    record = await _post(harness, client, body(collect=collect))

    (row,) = seen_at_send
    assert row.state is IngestRequestState.PENDING, 'written and committed before the Submit was sent'
    assert (row.kind, row.trigger, row.dataset_id) == (IngestRequestKind.BARS, IngestRunTrigger.REQUEST, None)
    assert (row.expiry, row.expiry_type, row.collect) == (EXPIRY, ExpiryType.ROLLING.value, stored)
    assert (row.ready_by, row.client_label, row.best_effort) == (READY_BY, OWNER, False)
    assert (row.asset_symbol, row.asset_type, row.data_types) == (
        'VFV',
        AssetType.STOCK,
        [DataType.MARKET_ACTIVITY.value],
    )
    assert (row.granularity, row.update_type, row.requested_start, row.requested_end) == (
        Granularity.ONE_DAY,
        UpdateType.DAILY.value,
        START,
        None,
    )
    assert (record.state, record.instance_id) == (IngestRequestState.QUEUED, INSTANCE_A)


async def test_post_with_no_ready_by_writes_and_sends_null(harness):
    client = FakeSubmit()

    record = await _post(harness, client, body(ready_by=None))

    assert (await harness.request_row(record.id)).ready_by is None
    (sent,) = client.sent
    assert sent.ready_by is None


@pytest.mark.parametrize('expiry_type', [ExpiryType.BULK, ExpiryType.BUFFER_1K, ExpiryType.ROLLING])
async def test_post_writes_the_bodys_own_expiry_type_including_its_default(harness, expiry_type):
    """A STATIC body may carry any type, BULK (the schema's default) included: the declared value is stored."""
    payload = body(update_type=UpdateType.STATIC, expiry_type=expiry_type, end=START + timedelta(days=9))
    record = await _post(harness, FakeSubmit(), payload)
    assert (await harness.request_row(record.id)).expiry_type == expiry_type.value


async def test_new_bars_request_for_a_refresh_carries_no_lifecycle_field():
    """The DAILY loop's row names its dataset and never a lifecycle field, whatever its body holds."""
    dataset_id = uuid.uuid4()
    narrowed = START + timedelta(days=20)

    row = new_bars_request(
        PATH,
        body(collect=False),
        trigger=IngestRunTrigger.SCHEDULE,
        client_label='scheduler',
        start=narrowed,
        ready_by=READY_BY,
        dataset_id=dataset_id,
        carries_lifecycle=False,
    )

    assert (row.trigger, row.dataset_id, row.client_label) == (IngestRunTrigger.SCHEDULE, dataset_id, 'scheduler')
    assert (row.expiry, row.expiry_type, row.collect) == (None, None, None)
    assert (row.requested_start, row.ready_by) == (narrowed, READY_BY)


# --------------------------------------------------------------------------------------------- the Submit


async def test_the_submit_carries_the_stored_request_its_id_and_its_count(harness):
    client = FakeSubmit()

    record = await _post(harness, client)

    (sent,) = client.sent
    assert sent.request_id == str(record.id)
    assert (sent.owner, sent.ready_by, sent.recovered, sent.attempt_count) == (OWNER, READY_BY, False, 0)
    assert (sent.bars.asset_symbol, sent.bars.start, sent.bars.end, sent.bars.update_type) == (
        'VFV',
        START,
        None,
        UpdateType.DAILY,
    )


async def test_submit_request_of_sends_the_durable_count_and_the_recovered_flag(harness):
    record = await _post(harness, FakeSubmit())
    await harness.set_request(record.id, attempts=4)
    async with AsyncSession(harness.engine) as db:
        stored = await request_crud.get_request(db, record.id)

    sent = submit_request_of(stored, recovered=True)

    assert (sent.recovered, sent.attempt_count) == (True, 4)


async def test_submit_request_of_refuses_a_non_bars_request_or_a_row_missing_a_field(harness):
    async with AsyncSession(harness.engine) as db:
        asset_list = await request_crud.create_request(
            db,
            request_crud.NewIngestRequest(
                kind=IngestRequestKind.ASSET_LIST, source=DataSource.ALPACA_API, client_label=OWNER
            ),
        )
    with pytest.raises(ValueError):
        submit_request_of(asset_list)
    record = await _post(harness, FakeSubmit())
    await harness.set_request(record.id, granularity=None)
    async with AsyncSession(harness.engine) as db:
        broken = await request_crud.get_request(db, record.id)
    with pytest.raises(ValueError):
        submit_request_of(broken)


async def test_an_accepted_submit_notes_the_instance_and_queues_the_request_under_it(harness):
    record = await _post(harness, FakeSubmit(INSTANCE_B))

    assert (await harness.instance(INSTANCE_B)) is not None
    row = await harness.request_row(record.id)
    assert (row.state, row.instance_id) == (IngestRequestState.QUEUED, INSTANCE_B)


async def test_f_an_answer_from_an_instance_already_superseded_leaves_the_request_for_the_watcher(harness):
    """An answer from an instance already superseded queues nothing.

    Note F: the submit path must not write QUEUED under an instance that is already superseded (nothing would ever
    work on it); the request stays PENDING and the instance watcher re-sends it.
    """
    await harness.register(INSTANCE_A)
    await harness.register(INSTANCE_B)

    record = await _post(harness, FakeSubmit(INSTANCE_A))

    row = await harness.request_row(record.id)
    assert (row.state, row.instance_id) == (IngestRequestState.PENDING, None)
    assert record.state is IngestRequestState.PENDING


async def test_a_refused_feed_fails_the_request_permanent_and_raises_feed_not_available(harness):
    client = FakeSubmit()
    client.refuse_with = Reason.FEED_NOT_AVAILABLE.value

    with pytest.raises(InvalidRequestError) as caught:
        await _post(harness, client, body(feed=Feed.SIP))

    assert caught.value.reason is Reason.FEED_NOT_AVAILABLE
    assert REASONS[Reason.FEED_NOT_AVAILABLE].http_status == 422
    (row,) = await harness.requests()
    assert (row.state, row.failure_cause) == (IngestRequestState.FAILED, IngestRequestFailureCause.PERMANENT.value)
    assert row.failure_detail == 'the feed asked for is not served' and row.finished_at is not None


async def test_a_refusal_with_an_unknown_reason_is_a_peer_protocol_error_and_still_fails_the_request(harness):
    client = FakeSubmit()
    client.refuse_with = 'A_REASON_FROM_THE_FUTURE'

    with pytest.raises(ExogenousError) as caught:
        await _post(harness, client)

    assert caught.value.reason is Reason.PEER_PROTOCOL_ERROR
    (row,) = await harness.requests()
    assert row.state is IngestRequestState.FAILED


async def test_a_submit_call_that_fails_leaves_the_request_pending_for_the_watcher(harness):
    client = FakeSubmit()
    client.raises = ExogenousError(Reason.PEER_UNAVAILABLE, 'ingest is down')

    with pytest.raises(ExogenousError):
        await _post(harness, client)

    (row,) = await harness.requests()
    assert (row.state, row.instance_id, row.finished_at) == (IngestRequestState.PENDING, None, None)


async def test_submit_record_returns_the_row_unchanged_when_the_request_moved_on(harness):
    """A late or repeated answer for a finished request never reopens it."""
    record = await _post(harness, FakeSubmit())
    await harness.set_request(record.id, state=IngestRequestState.SUCCEEDED)
    async with AsyncSession(harness.engine) as db:
        finished = await request_crud.get_request(db, record.id)

    again = await submit_record(finished, FakeSubmit(INSTANCE_B))

    assert again == finished
    assert (await harness.request_row(record.id)).state is IngestRequestState.SUCCEEDED


# --------------------------------------------------------------------------------------------- the own overlap


async def test_an_own_overlap_is_refused_before_anything_is_written_or_sent(harness):
    await harness.entry(owner=OWNER, start=START - timedelta(days=5))
    client = FakeSubmit()

    with pytest.raises(OwnOverlapConflict):
        await _post(harness, client)

    assert client.sent == []
    assert await harness.requests() == []


# --------------------------------------------------------------------------------------------- the whole POST


class IngestPlays:
    """Ingest's reports, delivered inside the waiter's sleep.

    Ingest's reports, delivered INSIDE the waiter's sleep, so no two sessions interleave on the one SQLite
    connection (StaticPool shares it, and an interleaved session would end the other's transaction). Each submit
    queues the reports the test scripts for that request; the waiter's next sleep delivers them, in order.
    """

    def __init__(self, harness: Harness, monkeypatch: pytest.MonkeyPatch, *scripts) -> None:
        self.harness = harness
        self.queued: list = []
        self.sleeps = 0
        self.client = FakeSubmit()

        async def on_submit(request):
            self.queued.extend(script(request.request_id) for script in scripts)

        async def sleep(_seconds: float) -> None:
            self.sleeps += 1
            while self.queued:
                await harness.deliver(self.queued.pop(0))

        self.client.on_submit = on_submit
        monkeypatch.setattr(
            data_action_request, 'await_verdict', functools.partial(data_action_request.await_verdict, sleep=sleep)
        )

    async def post(self, payload: StoreAssetDatasetBody) -> StoredDataset:
        async with AsyncSession(self.harness.engine, expire_on_commit=False) as db:
            return await store_market_activity_worker(PATH, payload, db, self.client, deadline_s=10)


async def test_post_end_to_end_answers_from_the_finishing_attempt_row_and_the_entry_takes_the_body(
    harness, monkeypatch
):
    """A whole POST: write, submit, ingest reports, and the answer from the rows.

    Write, submit, ingest reports (attempt 1 failed, attempt 2 succeeded), POST answers from the rows: the bars the
    finishing attempt wrote and ITS served range, which is neither the request's window nor attempt 1's.
    """
    served = ServedRange(start=START + timedelta(days=1, hours=5), end=START + timedelta(days=33, minutes=9))
    plays = IngestPlays(
        harness,
        monkeypatch,
        lambda request_id: report(request_id, DeliveryOutcome.FAILED),
        lambda request_id: with_trailer(report(request_id, attempt_no=2), served_range=served),
    )

    stored = await plays.post(body(collect=False))

    assert stored == StoredDataset(data_points=2, served_range=served)
    (sent,) = plays.client.sent
    assert sent.ready_by == READY_BY, "POST passes the caller's ready_by"
    assert plays.sleeps >= 1, 'the POST waited for the reports'
    (row,) = await harness.requests()
    assert (row.state, row.attempts, row.trigger) == (IngestRequestState.SUCCEEDED, 2, IngestRunTrigger.REQUEST)
    (entry,) = await harness.entries()
    assert row.dataset_id == entry.id
    assert (entry.expiry, entry.expiry_type, entry.collect, entry.feed) == (EXPIRY, ExpiryType.ROLLING, False, Feed.IEX)
    assert [run.trigger for run in await harness.runs(row.id)] == [IngestRunTrigger.REQUEST, IngestRunTrigger.RETRY]


async def test_post_end_to_end_of_an_empty_answer_is_zero_points_and_the_attempts_range(harness, monkeypatch):
    plays = IngestPlays(harness, monkeypatch, lambda request_id: report(request_id, parts=[BarPage(bars=[])]))

    stored = await plays.post(body())

    assert stored.data_points == 0
    assert stored.served_range == report(uuid.uuid4()).trailer.served_range
    (row,) = await harness.requests()
    assert row.state is IngestRequestState.EMPTY


async def test_post_whose_request_expires_on_its_ready_by_returns_the_deadline_failure(harness, monkeypatch):
    """The owner's R4 ruling: a POST's ready_by is the caller's deadline; ingest reports EXPIRED, the POST fails."""
    plays = IngestPlays(harness, monkeypatch, lambda request_id: report(request_id, DeliveryOutcome.EXPIRED))

    with pytest.raises(ExogenousError) as caught:
        await plays.post(body())

    assert caught.value.reason is Reason.DEADLINE
    assert await harness.entries() == [], 'nothing was fetched, so no entry'


async def test_post_that_outlives_its_deadline_answers_the_deadline_error_and_the_request_keeps_running(
    harness, monkeypatch
):
    monkeypatch.setenv('STORE_VERDICT_POLL_SECONDS', '0.01')
    async with AsyncSession(harness.engine, expire_on_commit=False) as db:
        with pytest.raises(ExogenousError) as caught:
            await store_market_activity_worker(PATH, body(), db, FakeSubmit(), deadline_s=0.05)

    assert caught.value.reason is Reason.DEADLINE
    (row,) = await harness.requests()
    assert (row.state, row.finished_at) == (IngestRequestState.QUEUED, None), 'left running, never failed'


async def test_the_post_path_reads_its_deadline_from_the_setting_when_given_none(harness, monkeypatch):
    seen: list = []

    async def spy(request_id, *, deadline_s=None, **kwargs):
        seen.append((request_id, deadline_s))
        return StoredDataset(data_points=0, served_range=ServedRange(start=START, end=START + timedelta(days=1)))

    monkeypatch.setattr(data_action_request, 'await_verdict', spy)
    async with AsyncSession(harness.engine, expire_on_commit=False) as db:
        await store_market_activity_worker(PATH, body(), db, FakeSubmit())

    ((request_id, deadline),) = seen
    assert deadline is None, 'None reads STORE_VERDICT_DEADLINE_SECONDS (test_await_verdict.py pins the reading)'
    (row,) = await harness.requests()
    assert request_id == row.id
