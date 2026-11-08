"""The asset-list request kind: its attempt runner and the Alpaca handle's Submit of it (P12, tj-7zjov1.22).

Against builder-ingest's 35b436a, checked against the bead's design notes: the 14:49 UTC P36c feed rule (an
ASSET_LIST report is always FEED_UNSPECIFIED; the submit path that queues an asset list leaves
ScheduledRequest.feed None and never copies a deployment tape onto it; through the handle's submit, an asset list
that arrives exhausted and one that expires in the queue each report FEED_UNSPECIFIED on the wire with the no-call
shape otherwise unchanged), the 17:38 UTC inputs as amended at 21:54 UTC (one vendor call per request, stocks
only, nothing judged), and the acceptance (paced on the trading-API scope).

WHAT IS REAL. AssetListAttemptRunner is driven by a scripted BrokerRead double, so its report is checked for
each answer the interface allows. The handle tests build the production AlpacaHandle with its own Scheduler and
AckGatedExecutor; only the store is a double, and the vendor, where one is reached, is the real TradingClient
over the recorded transport. No vendor key is read.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import ExogenousError, InvalidRequestError, Reason
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.asset_attempt import AssetListAttemptRunner, AssetListWork
from data.ingest.app.brokers.alpaca.handle import (
    ALPACA_BARS_PAGE_SIZE,
    ALPACA_DATA_SCOPE,
    ALPACA_TRADING_SCOPE,
    AlpacaHandle,
    estimate_bars_calls,
    estimate_request_calls,
)
from data.ingest.app.brokers.bars_attempt import BarsWork
from data.ingest.app.brokers.interface import (
    Asset,
    AssetListFailure,
    AssetListQuery,
    AssetListResponse,
    BrokerUnsupportedError,
    CallTally,
)
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler import ScheduledRequest
from data.ingest.app.scheduler.executor import ExecutorSettings
from data.ingest.app.scheduler.retry import RetryPolicy
from data.ingest.tests.alpaca_recorded import raw_answer, stub_trading_vendor
from data.ingest.tests.feed_rule import assert_a_no_tape_report_is_unspecified
from schemas.data_ingest.delivery import (
    AssetRecord,
    AssetRecords,
    AttemptDelivery,
    DeliverAttemptResponse,
    DeliveryErrorClass,
    DeliveryOutcome,
    DeliveryVerdict,
)
from schemas.data_ingest.submit import AssetListRequest, AssetListStatus, BarsRequest, SubmitRequest


pytestmark = pytest.mark.data_ingest

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
MAX_ATTEMPTS = 5
# Every wait for the code under test is bounded by this, so a regression fails rather than hangs.
GUARD_S = 5.0

LISTING = AssetListRequest(source=DataSource.ALPACA_API, asset_type=AssetType.STOCK, status=AssetListStatus.ACTIVE)

ASSETS = [
    Asset(
        vendor_asset_id='id-aapl',
        symbol='AAPL',
        raw_payload=b'{"id":"id-aapl"}',
        active=True,
        tradable=True,
        exchange='NASDAQ',
        name='Apple',
    ),
    # The vendor said nothing of active or tradable: None, carried as None, never False.
    Asset(vendor_asset_id='id-obk', symbol='OBK', raw_payload=b'{"id":"id-obk"}', exchange='TXSE'),
    Asset(
        vendor_asset_id='id-gone',
        symbol='GONE',
        raw_payload=b'{"id":"id-gone", "name": ""}',
        active=False,
        tradable=False,
        exchange='OTC',
        name='',
    ),
]


# ---------------------------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------------------------


class ScriptedAssets:
    """A BrokerRead whose get_assets answers from a script and records each query; a call counts on its tally."""

    def __init__(self, answer: AssetListResponse | AssetListFailure) -> None:
        self.queries: list[AssetListQuery] = []
        self.__answer = answer

    async def get_assets(self, query: AssetListQuery) -> AssetListResponse | AssetListFailure:
        self.queries.append(query)
        if query.tally is not None:
            query.tally.record(NOW)
            if isinstance(self.__answer, AssetListResponse):
                query.tally.record_page()
        return self.__answer

    async def get_asset(self, query):
        raise AssertionError('an asset-list attempt never looks a symbol up')

    async def get_bars(self, query):
        raise AssertionError('an asset-list attempt never reads bars')


class AnsweringStore:
    """A StoreDeliveryClient that answers every delivery at once with one verdict and records it."""

    def __init__(self, verdict: DeliveryVerdict = DeliveryVerdict.FINAL) -> None:
        self.received: list[AttemptDelivery] = []
        self.__verdict = verdict
        self.__arrived = asyncio.Event()

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        self.received.append(attempt)
        self.__arrived.set()
        return DeliverAttemptResponse(verdict=self.__verdict, attempt_count=attempt.header.attempt_no)

    async def register_instance(self, request):
        raise AssertionError('the executor never registers an instance')

    async def first(self) -> AttemptDelivery:
        await asyncio.wait_for(self.__arrived.wait(), GUARD_S)
        return self.received[0]


async def settle() -> None:
    """Let every task that can run, run."""
    for _ in range(200):
        await asyncio.sleep(0)


def scheduled_listing(ready_by: datetime | None = NOW + timedelta(minutes=5)) -> ScheduledRequest:
    return ScheduledRequest(
        request_id='list-1',
        quota_scope=ALPACA_TRADING_SCOPE,
        payload=AssetListWork(request=LISTING, priority=RequestPriority.BACKFILL),
        ready_by=ready_by,
    )


def settings() -> ExecutorSettings:
    return ExecutorSettings(
        buffer_size=8,
        workers=1,
        stall_timeout_s=300.0,
        stall_check_interval_s=3600.0,
        redelivery_base_s=1.0,
        redelivery_max_s=1.5,
        retry=RetryPolicy(max_attempts=MAX_ATTEMPTS, base_delay_s=1.0, max_delay_s=60.0, factor=2.0),
    )


def alpaca_handle() -> AlpacaHandle:
    return AlpacaHandle(settings=settings(), calls_per_minute=60)


def asset_list_submit(request_id: str = 'list-1', **overrides) -> SubmitRequest:
    fields = {'request_id': request_id, 'owner': 'world-manager', 'asset_list': LISTING}
    return SubmitRequest(**{**fields, **overrides})


def bars_submit(request_id: str = 'bars-1') -> SubmitRequest:
    bars = BarsRequest(
        source=DataSource.ALPACA_API,
        asset_symbol='VFV',
        asset_type=AssetType.STOCK,
        data_types=[DataType.MARKET_ACTIVITY],
        granularity=Granularity.ONE_DAY,
        start=datetime(2026, 1, 2, tzinfo=UTC),
        end=datetime(2026, 1, 9, tzinfo=UTC),
        update_type=UpdateType.STATIC,
    )
    return SubmitRequest(request_id=request_id, owner='rebalancer', bars=bars)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """An IEX deployment (so a copied tape would be visible), no credential, no trading host, no cached client."""
    monkeypatch.delenv('ALPACA_SIP_ENABLED', raising=False)
    monkeypatch.delenv('ALPACA_RATE_LIMIT_PER_MINUTE', raising=False)
    for name in (*broker_api.ALPACA_CREDENTIAL_VARS, broker_api.ALPACA_TRADING_HOST_VAR):
        monkeypatch.delenv(name, raising=False)
    broker_api.set_client(None)
    broker_api.set_trading_client(None)
    broker_api.forget_resets()
    yield
    broker_api.set_client(None)
    broker_api.set_trading_client(None)
    broker_api.forget_resets()


# ---------------------------------------------------------------------------------------------
# The attempt runner
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_attempt_asks_for_the_list_the_request_names_under_its_priority_and_ready_by():
    reader = ScriptedAssets(AssetListResponse(assets=[], as_of=NOW))
    ready_by = NOW + timedelta(minutes=9)

    await AssetListAttemptRunner(reader)(scheduled_listing(ready_by), 1)

    [query] = reader.queries
    assert (query.asset_type, query.status, query.priority, query.deadline) == (
        AssetType.STOCK,
        AssetListStatus.ACTIVE,
        RequestPriority.BACKFILL,
        ready_by,
    )
    assert isinstance(query.tally, CallTally), 'the attempt cannot report its vendor calls without a tally'


@pytest.mark.asyncio
async def test_a_served_list_succeeds_with_every_asset_carried_raw_in_one_records_part():
    reader = ScriptedAssets(AssetListResponse(assets=ASSETS, as_of=NOW))

    result = await AssetListAttemptRunner(reader)(scheduled_listing(), 1)

    assert result.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert result.trailer.as_of == NOW
    assert result.trailer.error_class is None
    assert result.trailer.served_range is None
    assert result.parts == [
        AssetRecords(
            records=[
                AssetRecord(
                    vendor_asset_id='id-aapl',
                    symbol='AAPL',
                    raw_payload=b'{"id":"id-aapl"}',
                    active=True,
                    tradable=True,
                    exchange='NASDAQ',
                    name='Apple',
                ),
                AssetRecord(vendor_asset_id='id-obk', symbol='OBK', raw_payload=b'{"id":"id-obk"}', exchange='TXSE'),
                AssetRecord(
                    vendor_asset_id='id-gone',
                    symbol='GONE',
                    raw_payload=b'{"id":"id-gone", "name": ""}',
                    active=False,
                    tradable=False,
                    exchange='OTC',
                    name='',
                ),
            ]
        )
    ]
    # None stays None: the vendor said nothing, which is never read as inactive or untradable.
    obk = result.parts[0].records[1]
    assert (obk.active, obk.tradable) == (None, None)


@pytest.mark.asyncio
async def test_a_succeeded_list_names_no_tape_no_instrument_and_the_trading_scope():
    reader = ScriptedAssets(AssetListResponse(assets=ASSETS, as_of=NOW))

    result = await AssetListAttemptRunner(reader)(scheduled_listing(), 1)

    assert result.feed is None
    assert result.vendor_asset_id == ''
    assert result.quota_key == ALPACA_TRADING_SCOPE
    assert result.reset_at is None
    stats = result.trailer.vendor_stats
    assert (stats.calls, stats.pages, stats.retries, stats.throttled_429) == (1, 1, 0, 0)
    assert stats.first_call_at == stats.last_call_at == NOW


@pytest.mark.asyncio
async def test_an_empty_list_succeeds_with_no_part():
    reader = ScriptedAssets(AssetListResponse(assets=[], as_of=NOW))

    result = await AssetListAttemptRunner(reader)(scheduled_listing(), 1)

    assert result.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert result.parts == []


RESET = NOW + timedelta(seconds=30)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('error', 'klass', 'reset_at'),
    [
        pytest.param(
            ExogenousError(Reason.VENDOR_UNAVAILABLE, 'the vendor failed'),
            DeliveryErrorClass.TRANSIENT,
            None,
            id='unavailable-transient',
        ),
        pytest.param(
            ExogenousError(Reason.VENDOR_RATE_LIMITED, 'slow down', reset_at=RESET, clock=lambda: NOW),
            DeliveryErrorClass.RATE_LIMITED,
            RESET,
            id='rate-limited-with-its-reset',
        ),
        pytest.param(
            BrokerUnsupportedError(Reason.UNSUPPORTED_ASSET_TYPE, 'stocks only'),
            DeliveryErrorClass.PERMANENT,
            None,
            id='unsupported-permanent',
        ),
        pytest.param(
            InvalidRequestError(Reason.VENDOR_INVALID_REQUEST, 'bad parameter'),
            DeliveryErrorClass.PERMANENT,
            None,
            id='invalid-permanent',
        ),
    ],
)
async def test_a_failed_list_reports_its_class_its_detail_and_no_part(error, klass, reset_at):
    reader = ScriptedAssets(AssetListFailure(error))

    result = await AssetListAttemptRunner(reader)(scheduled_listing(), 1)

    assert result.trailer.outcome is DeliveryOutcome.FAILED
    assert result.trailer.error_class is klass
    assert result.trailer.detail == error.detail
    assert result.reset_at == reset_at
    assert result.parts == []
    assert result.feed is None
    assert result.quota_key == ALPACA_TRADING_SCOPE
    assert (result.trailer.vendor_stats.calls, result.trailer.vendor_stats.pages) == (1, 0)


# ---------------------------------------------------------------------------------------------
# The cost estimate
# ---------------------------------------------------------------------------------------------


def test_an_asset_list_is_estimated_at_one_call_and_bars_as_their_pages():
    """P27a s1: the whole list is one HTTP request, no pagination."""
    bars = ScheduledRequest(
        request_id='b',
        quota_scope=ALPACA_DATA_SCOPE,
        payload=BarsWork(request=bars_submit().bars, priority=RequestPriority.BACKFILL, feed=Feed.IEX),
    )

    assert estimate_request_calls(scheduled_listing(), ALPACA_BARS_PAGE_SIZE) == 1
    assert estimate_request_calls(scheduled_listing(), 1) == 1, 'a list does not page, whatever the page size'
    assert estimate_request_calls(bars, 2) == estimate_bars_calls(bars, 2) == 4


# ---------------------------------------------------------------------------------------------
# The handle's Submit of an asset list
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_asset_list_is_accepted_with_no_tape_on_the_trading_scope_and_queued_with_no_tape():
    """The 14:49 UTC feed rule: the queued request's feed stays None; it never takes the deployment tape."""
    handle = alpaca_handle()
    ready_by = NOW + timedelta(days=3650)

    ack = await handle.submit(asset_list_submit(ready_by=ready_by, attempt_count=2, client='world'))

    assert ack.refused is None
    assert ack.accepted.feed is None
    assert ack.accepted.quota_key == ALPACA_TRADING_SCOPE
    assert handle.scheduler.queued == 1
    queued = await asyncio.wait_for(handle.scheduler.take(), GUARD_S)
    assert queued.feed is None, 'the queued asset list carries a tape'
    assert queued.quota_scope == ALPACA_TRADING_SCOPE
    assert queued.payload == AssetListWork(request=LISTING, priority=RequestPriority.BACKFILL)
    assert (queued.request_id, queued.ready_by, queued.attempt_count, queued.client_label) == (
        'list-1',
        ready_by,
        2,
        'world',
    )


@pytest.mark.asyncio
async def test_an_asset_list_submit_is_idempotent_on_request_id_until_it_finishes():
    handle = alpaca_handle()

    first = await handle.submit(asset_list_submit())
    again = await handle.submit(asset_list_submit())

    assert again == first
    assert handle.scheduler.queued == 1, 'a re-sent asset list was queued twice'
    assert handle.active == 1

    handle.finished(ScheduledRequest(request_id='list-1', quota_scope=ALPACA_TRADING_SCOPE, payload=None))
    assert handle.active == 0
    await handle.submit(asset_list_submit())
    assert handle.scheduler.queued == 2


@pytest.mark.asyncio
async def test_active_counts_held_requests_of_both_kinds_and_finished_forgets_each():
    handle = alpaca_handle()

    await handle.submit(asset_list_submit('list-1'))
    await handle.submit(bars_submit('bars-1'))

    assert handle.active == 2
    handle.finished(ScheduledRequest(request_id='bars-1', quota_scope=ALPACA_DATA_SCOPE, payload=None))
    assert handle.active == 1
    handle.finished(ScheduledRequest(request_id='list-1', quota_scope=ALPACA_TRADING_SCOPE, payload=None))
    assert handle.active == 0


@pytest.mark.asyncio
async def test_a_bars_submit_still_goes_through_the_base_handle_with_its_tape_on_the_data_scope():
    handle = alpaca_handle()

    ack = await handle.submit(bars_submit())

    assert (ack.accepted.feed, ack.accepted.quota_key) == (Feed.IEX, ALPACA_DATA_SCOPE)
    queued = await asyncio.wait_for(handle.scheduler.take(), GUARD_S)
    assert (queued.quota_scope, queued.feed) == (ALPACA_DATA_SCOPE, Feed.IEX)


@pytest.mark.asyncio
@pytest.mark.parametrize('asset_type', [kind for kind in AssetType if kind is not AssetType.STOCK])
async def test_an_asset_list_of_a_kind_other_than_stock_is_refused_and_never_held(asset_type):
    handle = alpaca_handle()
    listing = AssetListRequest(source=DataSource.ALPACA_API, asset_type=asset_type, status=AssetListStatus.ACTIVE)

    with pytest.raises(InvalidRequestError) as raised:
        await handle.submit(asset_list_submit(asset_list=listing))

    assert raised.value.reason is Reason.UNSUPPORTED_ASSET_TYPE
    assert handle.scheduler.queued == 0
    assert handle.active == 0


# Corporate actions still falling through to BrokerHandle.submit's refusal is pinned by
# test_ack_gated_retry.test_the_handle_itself_refuses_a_kind_not_served_yet.


class ChargeSpy:
    """Wraps the handle's real scheduler's charge_restart and submit, logging each in order."""

    def __init__(self, handle: AlpacaHandle, monkeypatch) -> None:
        self.log: list[tuple[str, str, int | None]] = []
        scheduler = handle.scheduler
        charge, submit = scheduler.charge_restart, scheduler.submit

        def charge_restart(request: ScheduledRequest) -> int:
            charged = charge(request)
            self.log.append(('charge_restart', request.quota_scope, charged))
            return charged

        async def logged_submit(request: ScheduledRequest) -> None:
            self.log.append(('submit', request.quota_scope, None))
            await submit(request)

        monkeypatch.setattr(scheduler, 'charge_restart', charge_restart)
        monkeypatch.setattr(scheduler, 'submit', logged_submit)


@pytest.mark.asyncio
async def test_a_recovered_asset_list_is_charged_its_one_call_to_the_trading_scope_before_it_is_queued(monkeypatch):
    handle = alpaca_handle()
    spy = ChargeSpy(handle, monkeypatch)

    await handle.submit(asset_list_submit(attempt_count=1, recovered=True))

    assert spy.log == [('charge_restart', ALPACA_TRADING_SCOPE, 1), ('submit', ALPACA_TRADING_SCOPE, None)]


@pytest.mark.asyncio
async def test_an_asset_list_not_flagged_recovered_is_never_charged(monkeypatch):
    handle = alpaca_handle()
    spy = ChargeSpy(handle, monkeypatch)

    await handle.submit(asset_list_submit(attempt_count=1))

    assert spy.log == [('submit', ALPACA_TRADING_SCOPE, None)]


# ---------------------------------------------------------------------------------------------
# Through the handle and its executor: the no-call reports name no tape on the wire
# ---------------------------------------------------------------------------------------------


def assert_shaped_as_a_no_call_report(attempt: AttemptDelivery) -> None:
    """store.proto DELIVERY_OUTCOME_EXPIRED and EXHAUSTED: nothing of an attempt."""
    assert attempt.trailer.vendor_stats is None
    assert attempt.trailer.served_range is None
    assert attempt.trailer.as_of is None
    assert attempt.trailer.error_class is None
    assert attempt.trailer.last_attempt is False
    assert attempt.header.quota_key == ''
    assert attempt.header.vendor_asset_id == ''
    assert attempt.parts == []


@pytest.mark.asyncio
@pytest.mark.parametrize('recovered', [True, False], ids=['recovered', 'not-recovered'])
@pytest.mark.parametrize('attempt_count', [MAX_ATTEMPTS, MAX_ATTEMPTS + 2], ids=['at-max', 'past-max'])
async def test_an_asset_list_arriving_exhausted_reports_feed_unspecified_with_no_call(
    monkeypatch, recovered, attempt_count
):
    """The 14:49 UTC validator item: through the handle's submit, an asset list that arrives exhausted."""
    handle = alpaca_handle()
    spy = ChargeSpy(handle, monkeypatch)
    store = AnsweringStore()
    handle.start(store, 'instance-under-test')
    try:
        ack = await handle.submit(asset_list_submit(attempt_count=attempt_count, recovered=recovered))
        report = await store.first()
        await settle()
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)

    assert ack.accepted.feed is None
    assert report.trailer.outcome is DeliveryOutcome.EXHAUSTED
    assert report.header.request_id == 'list-1'
    assert report.header.attempt_no == attempt_count + 1
    assert report.header.feed is None
    assert_a_no_tape_report_is_unspecified(report.header)
    assert_shaped_as_a_no_call_report(report)
    assert len(store.received) == 1
    assert [entry[0] for entry in spy.log] == (['charge_restart'] if recovered else []), 'queued, or charged wrongly'
    assert handle.active == 0, 'the exhausted asset list was not forgotten after the acknowledgement'


@pytest.mark.asyncio
async def test_an_asset_list_expiring_in_the_queue_reports_feed_unspecified_with_no_call():
    """The 14:49 UTC validator item: through the handle's submit, an asset list that expires in the queue."""
    handle = alpaca_handle()
    store = AnsweringStore()
    handle.start(store, 'instance-under-test')
    try:
        ack = await handle.submit(asset_list_submit(ready_by=datetime.now(UTC) - timedelta(seconds=1)))
        report = await store.first()
        await settle()
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)

    assert ack.accepted.feed is None
    assert report.trailer.outcome is DeliveryOutcome.EXPIRED
    assert report.header.attempt_no == 1
    assert report.header.feed is None
    assert_a_no_tape_report_is_unspecified(report.header)
    assert_shaped_as_a_no_call_report(report)
    assert handle.active == 0


# ---------------------------------------------------------------------------------------------
# Through the handle end to end: one paced call on the trading scope, the list on the wire
# ---------------------------------------------------------------------------------------------

LIST_BODY = b'[{"id":"id-aapl","symbol":"AAPL","status":"active","tradable":true,"exchange":"NASDAQ","name":"Apple"}]'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', list(AssetListStatus), ids=lambda status: status.value)
async def test_a_submitted_asset_list_is_read_once_on_the_trading_scope_and_delivered_with_no_tape(monkeypatch, status):
    vendor = stub_trading_vendor(raw_answer(LIST_BODY))
    broker_api.set_trading_client(vendor.client)
    handle = alpaca_handle()
    acquired: list[str] = []
    acquire = handle.scheduler.acquire

    async def recording_acquire(scope, *args, **kwargs):
        acquired.append(scope)
        await acquire(scope, *args, **kwargs)

    monkeypatch.setattr(handle.scheduler, 'acquire', recording_acquire)
    store = AnsweringStore()
    handle.start(store, 'instance-under-test')
    try:
        listing = AssetListRequest(source=DataSource.ALPACA_API, asset_type=AssetType.STOCK, status=status)
        await handle.submit(asset_list_submit(asset_list=listing, ready_by=datetime.now(UTC) + timedelta(minutes=5)))
        report = await store.first()
        await settle()
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)

    assert report.trailer.outcome is DeliveryOutcome.SUCCEEDED, report.trailer.detail
    assert acquired == [ALPACA_TRADING_SCOPE], 'the list was not paced by exactly one trading-scope token'
    assert [sent.params for sent in vendor.sent] == [{'status': status.value, 'asset_class': 'us_equity'}]
    assert report.header.feed is None
    assert_a_no_tape_report_is_unspecified(report.header)
    assert report.header.quota_key == ALPACA_TRADING_SCOPE
    assert report.header.vendor_asset_id == ''
    assert report.trailer.vendor_stats.calls == 1
    [part] = report.parts
    assert [(record.vendor_asset_id, record.raw_payload) for record in part.records] == [('id-aapl', LIST_BODY[1:-1])]
