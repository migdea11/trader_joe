"""A bars attempt's vendor asset id, looked up by symbol after the bars (P12h, tj-7zjov1.128).

Against builder-ingest's 84239bd, checked against the bead's design (ADR tj-26jt9w addendum (10) item 4): after the
bars are drained the runner looks the request's symbol up and puts the id it returns, opaque, on the SUCCEEDED
report; a vendor 404 is FAILED PERMANENT with no parts and the bars discarded; a transient or rate failure of the
lookup is FAILED with that class (a rate limit with its reset), no parts; the lookup is paced on the trading scope
and never counted in vendor_stats; nothing about the asset is judged; a FAILED bars attempt keeps an empty id.

WHAT IS REAL. The runner tests drive the production BarsAttemptRunner over a scripted BrokerRead that logs every
step, so the order of the bars and the lookup is observable down to the last bar drained. The handle tests build the
production AlpacaHandle (its Scheduler, its AckGatedExecutor, a real AlpacaRead), with only the store a double and
both vendors the real alpaca-py clients over recorded transports; the header is read as the wire carries it, through
the production proto mapping. No vendor key is read.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import ExogenousError, InvalidRequestError, Reason
from common.rpc.mapping.delivery import header_to_proto
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.classify import RATE_LIMIT_RESET_HEADER
from data.ingest.app.brokers.alpaca.handle import ALPACA_DATA_SCOPE, AlpacaHandle
from data.ingest.app.brokers.bars_attempt import BarsAttemptRunner, BarsWork
from data.ingest.app.brokers.interface import (
    Asset,
    AssetLookupFailure,
    AssetLookupQuery,
    AssetNotFound,
    AssetResponse,
    Bar,
    BarsFailure,
    BarsQuery,
    BarsResponse,
    ServedRange,
)
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler import ScheduledRequest
from data.ingest.app.scheduler.executor import ExecutorSettings
from data.ingest.app.scheduler.retry import RetryPolicy
from data.ingest.tests.alpaca_recorded import (
    ASSETS_PATH,
    asset_answer,
    bars_answer,
    load,
    status_answer,
    stub_trading_vendor,
    stub_vendor,
    wire_bar,
)
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    DeliverAttemptResponse,
    DeliveryErrorClass,
    DeliveryOutcome,
    DeliveryVerdict,
)
from schemas.data_ingest.fetch_dataset import BarPage
from schemas.data_ingest.submit import BarsRequest, SubmitRequest


pytestmark = pytest.mark.data_ingest

SYMBOL = 'VFV'
START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
END = START + timedelta(hours=1)
# When the double's one bars call was made, and when its vendor answered: fixed, so the stats are exact.
BARS_CALL_AT = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
AS_OF = BARS_CALL_AT + timedelta(seconds=1)
# A ready_by no default could produce, so a lookup handed any other deadline is caught.
READY_BY = datetime(2026, 10, 26, 15, 7, 13, tzinfo=UTC)
RESET_AT = datetime(2026, 10, 26, 15, 3, 21, tzinfo=UTC)
# Opaque: mixed case, spaces and separators, so any normalisation of the id shows.
ODD_ID = ' Vendor-ID/With:Odd Case 42 '
GUARD_S = 5.0


def bar(at: datetime) -> Bar:
    return Bar(timestamp=at, open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0, trade_count=3, vwap=1.2)


BARS = [bar(START), bar(START + timedelta(minutes=30))]


def an_asset(vendor_asset_id: str = ODD_ID, **overrides) -> Asset:
    fields = {'vendor_asset_id': vendor_asset_id, 'symbol': SYMBOL, 'raw_payload': b'{"id": "raw"}'}
    return Asset(**{**fields, **overrides})


def found(asset: Asset | None = None) -> AssetResponse:
    return AssetResponse(asset=asset if asset is not None else an_asset(), as_of=AS_OF)


# ---------------------------------------------------------------------------------------------
# The runner, over a BrokerRead that logs every step
# ---------------------------------------------------------------------------------------------


class ScriptedRead:
    """A BrokerRead that serves scripted bars and a scripted lookup, logging every step.

    get_bars counts one call (and one page when it serves) on the query's tally. Every step lands in `events`, the
    last bar drained included.
    """

    def __init__(
        self, lookup: AssetResponse | AssetNotFound | AssetLookupFailure, bars: list[Bar] | BarsFailure = BARS
    ) -> None:
        self.events: list[str] = []
        self.lookups: list[AssetLookupQuery] = []
        self.bars_queries: list[BarsQuery] = []
        self.__lookup = lookup
        self.__bars = bars

    async def get_bars(self, query: BarsQuery) -> BarsResponse | BarsFailure:
        self.events.append('get_bars')
        self.bars_queries.append(query)
        query.tally.record(BARS_CALL_AT)
        if isinstance(self.__bars, BarsFailure):
            return self.__bars
        query.tally.record_page()
        return BarsResponse(feed=Feed.IEX, bars=self.__stream(), served_range=ServedRange(START, END), as_of=AS_OF)

    async def __stream(self) -> AsyncIterator[Bar]:
        for item in self.__bars:
            self.events.append('bar')
            yield item
        self.events.append('bars drained')

    async def get_asset(self, query: AssetLookupQuery) -> AssetResponse | AssetNotFound | AssetLookupFailure:
        self.events.append('get_asset')
        self.lookups.append(query)
        return self.__lookup

    async def get_assets(self, query):
        raise AssertionError('a bars attempt never reads an asset list')


def bars_request(**overrides) -> BarsRequest:
    fields = {
        'source': DataSource.ALPACA_API,
        'asset_symbol': SYMBOL,
        'asset_type': AssetType.STOCK,
        'data_types': [DataType.MARKET_ACTIVITY],
        'granularity': Granularity.THIRTY_MINUTES,
        'start': START,
        'end': END,
        'update_type': UpdateType.STATIC,
    }
    return BarsRequest(**{**fields, **overrides})


def scheduled(priority: RequestPriority = RequestPriority.BACKFILL, ready_by: datetime | None = READY_BY):
    work = BarsWork(request=bars_request(), priority=priority, feed=Feed.SIP)
    return ScheduledRequest(request_id='r1', quota_scope=ALPACA_DATA_SCOPE, payload=work, ready_by=ready_by)


def assert_stats_are_the_bars_call_alone(result) -> None:
    """One bars call, one page, at BARS_CALL_AT: the lookup added nothing, not a call and not a timestamp."""
    stats = result.trailer.vendor_stats
    assert (stats.calls, stats.pages, stats.retries, stats.throttled_429) == (1, 1, 0, 0)
    assert stats.first_call_at == stats.last_call_at == BARS_CALL_AT


@pytest.mark.asyncio
async def test_a_succeeded_attempt_carries_the_looked_up_vendor_id_untouched():
    read = ScriptedRead(found())

    result = await BarsAttemptRunner(read)(scheduled(), 1)

    assert result.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert result.vendor_asset_id == ODD_ID
    [page] = result.parts
    assert isinstance(page, BarPage)
    assert [item.bar_start for item in page.bars] == [START, START + timedelta(minutes=30)]
    assert result.trailer.as_of == AS_OF


@pytest.mark.asyncio
async def test_the_lookup_comes_after_the_last_bar_is_drained_and_is_made_once():
    read = ScriptedRead(found())

    await BarsAttemptRunner(read)(scheduled(), 1)

    assert read.events == ['get_bars', 'bar', 'bar', 'bars drained', 'get_asset']


@pytest.mark.asyncio
@pytest.mark.parametrize('priority', [RequestPriority.BACKFILL, RequestPriority.INTERACTIVE], ids=lambda p: p.name)
async def test_the_lookup_asks_for_the_requests_symbol_under_its_priority_and_ready_by(priority):
    read = ScriptedRead(found())

    await BarsAttemptRunner(read)(scheduled(priority=priority), 1)

    assert read.lookups == [AssetLookupQuery(SYMBOL, priority=priority, deadline=READY_BY)]
    # The same bound the bars call waited under.
    assert read.bars_queries[0].deadline == READY_BY


@pytest.mark.asyncio
async def test_a_request_with_no_ready_by_looks_up_with_no_deadline():
    read = ScriptedRead(found())

    await BarsAttemptRunner(read)(scheduled(ready_by=None), 1)

    assert read.lookups == [AssetLookupQuery(SYMBOL, priority=RequestPriority.BACKFILL, deadline=None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'lookup',
    [
        pytest.param(found(), id='found'),
        pytest.param(AssetNotFound(symbol=SYMBOL, as_of=AS_OF), id='not-found'),
        pytest.param(AssetLookupFailure(ExogenousError(Reason.VENDOR_UNAVAILABLE, 'down')), id='transient'),
        pytest.param(
            AssetLookupFailure(ExogenousError(Reason.VENDOR_RATE_LIMITED, 'slow', reset_at=RESET_AT)), id='rate'
        ),
    ],
)
async def test_the_lookup_is_never_counted_in_vendor_stats_whatever_it_answers(lookup):
    result = await BarsAttemptRunner(ScriptedRead(lookup))(scheduled(), 1)

    assert_stats_are_the_bars_call_alone(result)
    assert result.quota_key == ALPACA_DATA_SCOPE


@pytest.mark.asyncio
async def test_not_found_fails_permanent_naming_the_symbol_with_the_bars_discarded():
    read = ScriptedRead(AssetNotFound(symbol=SYMBOL, as_of=AS_OF))

    result = await BarsAttemptRunner(read)(scheduled(), 1)

    assert read.events[-1] == 'get_asset', 'the not-found answer came from a lookup made before the bars ended'
    assert result.trailer.outcome is DeliveryOutcome.FAILED
    assert result.trailer.error_class is DeliveryErrorClass.PERMANENT
    assert result.parts == [], 'the drained bars were delivered from a not-found attempt'
    assert SYMBOL in result.trailer.detail
    assert 'not found' in result.trailer.detail
    assert result.vendor_asset_id == ''
    assert result.reset_at is None
    assert result.trailer.served_range is None
    assert result.trailer.as_of is None
    # A FAILED report carries the tape of the ack, the work's, since it served nothing.
    assert result.feed is Feed.SIP


@pytest.mark.asyncio
async def test_a_transient_lookup_failure_fails_transient_with_no_parts_and_no_reset():
    read = ScriptedRead(AssetLookupFailure(ExogenousError(Reason.VENDOR_UNAVAILABLE, 'the trading API is down')))

    result = await BarsAttemptRunner(read)(scheduled(), 1)

    assert result.trailer.outcome is DeliveryOutcome.FAILED
    assert result.trailer.error_class is DeliveryErrorClass.TRANSIENT
    assert result.trailer.detail == 'the trading API is down'
    assert result.parts == []
    assert result.vendor_asset_id == ''
    assert result.reset_at is None


@pytest.mark.asyncio
async def test_a_rate_limited_lookup_fails_rate_limited_with_its_reset_and_no_parts():
    read = ScriptedRead(AssetLookupFailure(ExogenousError(Reason.VENDOR_RATE_LIMITED, 'slow', reset_at=RESET_AT)))

    result = await BarsAttemptRunner(read)(scheduled(), 1)

    assert result.trailer.outcome is DeliveryOutcome.FAILED
    assert result.trailer.error_class is DeliveryErrorClass.RATE_LIMITED
    assert result.reset_at == RESET_AT
    assert result.parts == []
    assert result.vendor_asset_id == ''


@pytest.mark.asyncio
async def test_a_failed_bars_call_makes_no_lookup_and_keeps_an_empty_vendor_id():
    failure = BarsFailure(ExogenousError(Reason.VENDOR_UNAVAILABLE, 'the data API is down'))
    read = ScriptedRead(found(), bars=failure)

    result = await BarsAttemptRunner(read)(scheduled(), 1)

    assert result.trailer.outcome is DeliveryOutcome.FAILED
    assert read.lookups == []
    assert read.events == ['get_bars']
    assert result.vendor_asset_id == ''


@pytest.mark.asyncio
async def test_a_zero_bar_attempt_still_looks_up_after_the_drain_and_carries_the_vendor_id():
    # The store finishes a zero-bar SUCCEEDED attempt EMPTY, and ADR tj-26jt9w addenda (12) item 3 and (13) make an
    # EMPTY attempt's id load-bearing: without it every dataset would stop at its first weekend or holiday refresh.
    read = ScriptedRead(found(), bars=[])

    result = await BarsAttemptRunner(read)(scheduled(), 1)

    assert read.events == ['get_bars', 'bars drained', 'get_asset']
    assert result.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert result.parts == []
    assert result.vendor_asset_id == ODD_ID
    assert result.trailer.served_range is not None
    assert result.trailer.as_of == AS_OF
    assert_stats_are_the_bars_call_alone(result)
    assert result.quota_key == ALPACA_DATA_SCOPE


@pytest.mark.asyncio
async def test_nothing_about_the_asset_is_judged_an_inactive_untradable_venue_less_record_still_succeeds():
    asset = an_asset(active=False, tradable=False, exchange='', name='', symbol='SOMETHING-ELSE')

    result = await BarsAttemptRunner(ScriptedRead(found(asset)))(scheduled(), 1)

    assert result.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert result.vendor_asset_id == ODD_ID
    assert len(result.parts) == 1


# ---------------------------------------------------------------------------------------------
# The typed reason on the trailer (tj-7zjov1.346, design tj-7zjov1.236 option C): a FAILED trailer built from a
# TraderJoeError carries that error's Reason; a trailer with no typed error behind it carries none.
# ---------------------------------------------------------------------------------------------


TYPED_BARS_FAILURES = [
    pytest.param(ExogenousError(Reason.RANGE_TOO_RECENT, 'the SIP edge', reset_at=RESET_AT), id='range-too-recent'),
    pytest.param(ExogenousError(Reason.VENDOR_RATE_LIMITED, 'slow', reset_at=RESET_AT), id='vendor-rate-limited'),
    pytest.param(ExogenousError(Reason.RATE_BUDGET, 'no token in time', reset_at=RESET_AT), id='rate-budget'),
    pytest.param(ExogenousError(Reason.VENDOR_UNAVAILABLE, 'down'), id='vendor-unavailable'),
    pytest.param(InvalidRequestError(Reason.FEED_NOT_AVAILABLE, 'no such tape'), id='feed-not-available'),
    pytest.param(InvalidRequestError(Reason.RANGE_IN_FUTURE, 'starts after now'), id='range-in-future'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('error', TYPED_BARS_FAILURES)
async def test_a_failed_bars_call_carries_the_reason_of_its_typed_error(error):
    result = await BarsAttemptRunner(ScriptedRead(found(), bars=BarsFailure(error)))(scheduled(), 1)

    assert result.trailer.outcome is DeliveryOutcome.FAILED
    assert result.trailer.reason is error.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error',
    [
        pytest.param(ExogenousError(Reason.VENDOR_UNAVAILABLE, 'down'), id='vendor-unavailable'),
        pytest.param(ExogenousError(Reason.VENDOR_RATE_LIMITED, 'slow', reset_at=RESET_AT), id='vendor-rate-limited'),
    ],
)
async def test_a_failed_lookup_carries_the_reason_of_its_typed_error(error):
    result = await BarsAttemptRunner(ScriptedRead(AssetLookupFailure(error)))(scheduled(), 1)

    assert result.trailer.outcome is DeliveryOutcome.FAILED
    assert result.trailer.reason is error.reason


@pytest.mark.asyncio
async def test_the_asset_not_found_failure_carries_no_reason():
    # AssetNotFound is an answer, not a TraderJoeError, and ASSET_NOT_FOUND is deferred (tj-7zjov1.324): no reason is
    # guessed for it.
    result = await BarsAttemptRunner(ScriptedRead(AssetNotFound(symbol=SYMBOL, as_of=AS_OF)))(scheduled(), 1)

    assert result.trailer.outcome is DeliveryOutcome.FAILED
    assert result.trailer.reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize('bars', [pytest.param(BARS, id='bars'), pytest.param([], id='no-bars')])
async def test_a_succeeded_bars_attempt_carries_no_reason(bars):
    result = await BarsAttemptRunner(ScriptedRead(found(), bars=bars))(scheduled(), 1)

    assert result.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert result.trailer.reason is None


# ---------------------------------------------------------------------------------------------
# Through the production AlpacaHandle and its executor: the header as the wire carries it
# ---------------------------------------------------------------------------------------------


VFV_ID = '5b0c7a3e-1d2f-4e6a-9b8c-0d1e2f3a4b5c'


class AnsweringStore:
    """A StoreDeliveryClient that answers every delivery FINAL at once and records it."""

    def __init__(self) -> None:
        self.received: list[AttemptDelivery] = []
        self.__arrived = asyncio.Event()

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        self.received.append(attempt)
        self.__arrived.set()
        return DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=attempt.header.attempt_no)

    async def register_instance(self, request):
        raise AssertionError('the executor never registers an instance')

    async def first(self) -> AttemptDelivery:
        await asyncio.wait_for(self.__arrived.wait(), GUARD_S)
        return self.received[0]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """An IEX deployment, no credential, no trading host, no cached client and no remembered reset."""
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


def alpaca_handle() -> AlpacaHandle:
    settings = ExecutorSettings(
        buffer_size=8,
        workers=1,
        stall_timeout_s=300.0,
        stall_check_interval_s=3600.0,
        redelivery_base_s=1.0,
        redelivery_max_s=1.5,
        retry=RetryPolicy(max_attempts=5, base_delay_s=1.0, max_delay_s=60.0, factor=2.0),
    )
    return AlpacaHandle(settings=settings, calls_per_minute=60)


async def one_report(*trading_answers, bars_page=None) -> tuple[AttemptDelivery, list]:
    """Submit one bars request to the production handle, with bars served and the lookup answered as scripted.

    bars_page replaces the default one-bar page. Returns the first report the store received and every request that
    reached the trading vendor.
    """
    page = bars_page if bars_page is not None else bars_answer({SYMBOL: [wire_bar(START)]})
    broker_api.set_client(stub_vendor(page).client)
    trading = stub_trading_vendor(*trading_answers)
    broker_api.set_trading_client(trading.client)
    handle = alpaca_handle()
    store = AnsweringStore()
    handle.start(store, 'instance-under-test')
    try:
        await handle.submit(SubmitRequest(request_id='req-1', owner='rebalancer', bars=bars_request()))
        report = await store.first()
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)
    return report, trading.sent


@pytest.mark.asyncio
async def test_through_the_handle_a_succeeded_report_carries_the_vendor_id_on_the_wire_header():
    report, sent = await one_report(asset_answer(SYMBOL, VFV_ID))

    assert report.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert header_to_proto(report.header).vendor_asset_id == VFV_ID
    assert [request.path for request in sent] == [f'{ASSETS_PATH}/{SYMBOL}']
    # The lookup is not the bars' spend: one data call, one page.
    assert (report.trailer.vendor_stats.calls, report.trailer.vendor_stats.pages) == (1, 1)
    assert report.header.quota_key == ALPACA_DATA_SCOPE
    assert len(report.parts) == 1


@pytest.mark.asyncio
async def test_through_the_handle_a_zero_bar_report_still_carries_the_vendor_id_on_the_wire_header():
    # The real empty-range body Alpaca returns (recorded): the store finishes this EMPTY, and its id is the P50 baseline.
    report, sent = await one_report(asset_answer(SYMBOL, VFV_ID), bars_page=load('bars_empty_absent_symbol'))

    assert report.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert report.parts == []
    assert header_to_proto(report.header).vendor_asset_id == VFV_ID
    assert [request.path for request in sent] == [f'{ASSETS_PATH}/{SYMBOL}']
    assert report.trailer.vendor_stats.calls == 1
    assert report.header.quota_key == ALPACA_DATA_SCOPE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('answer', 'klass'),
    [
        pytest.param(status_answer(404, 'asset not found'), DeliveryErrorClass.PERMANENT, id='404-not-found'),
        pytest.param(status_answer(500), DeliveryErrorClass.TRANSIENT, id='500-transient'),
        pytest.param(
            status_answer(429, headers={RATE_LIMIT_RESET_HEADER: str(RESET_AT.timestamp())}),
            DeliveryErrorClass.RATE_LIMITED,
            id='429-rate-limited',
        ),
    ],
)
async def test_through_the_handle_a_failed_lookup_reports_failed_with_no_parts_and_an_empty_wire_id(answer, klass):
    report, sent = await one_report(answer)

    assert len(sent) == 1
    assert report.trailer.outcome is DeliveryOutcome.FAILED
    assert report.trailer.error_class is klass
    assert report.parts == []
    assert header_to_proto(report.header).vendor_asset_id == ''
    assert (report.trailer.vendor_stats.calls, report.trailer.vendor_stats.pages) == (1, 1)
    # No feed named: SIP, the default (tj-7zjov1.343).
    assert report.header.feed is Feed.SIP


@pytest.mark.asyncio
async def test_through_the_handle_a_failed_bars_call_makes_no_lookup_and_the_wire_id_is_empty():
    broker_api.set_client(stub_vendor(status_answer(500)).client)
    trading = stub_trading_vendor()
    broker_api.set_trading_client(trading.client)
    handle = alpaca_handle()
    store = AnsweringStore()
    handle.start(store, 'instance-under-test')
    try:
        await handle.submit(SubmitRequest(request_id='req-1', owner='rebalancer', bars=bars_request()))
        report = await store.first()
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)

    assert report.trailer.outcome is DeliveryOutcome.FAILED
    assert trading.sent == []
    assert header_to_proto(report.header).vendor_asset_id == ''
