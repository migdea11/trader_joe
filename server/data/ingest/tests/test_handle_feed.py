"""The broker handle owns the default feed per account: one resolution site, read once at build (P38, tj-7zjov1.97).

Against builder-ingest's 4454c83, checked against the bead's design (the owner rulings on tj-7zjov1.91, ADR tj-xn3qa6
addendum 1, ADR tj-j4wknb addendum 4): BrokerHandle takes the account's feed as a value at construction and exposes
it as `feed`; AlpacaHandle reads ALPACA_SIP_ENABLED once, when it is built (unset or false is IEX, true is SIP), and
hands that feed to its handle and to AlpacaRead; AlpacaRead reads no environment, serves that feed and refuses a named
one that differs with FEED_NOT_AVAILABLE before any vendor call; a kind with no tape still gets none, so unspecified is
never replaced by a guess. The ack, the queued BarsWork, the vendor's feed parameter, the response and the report the
store receives all name the one feed.

WHAT IS REAL. The production AlpacaHandle (its Scheduler, its AckGatedExecutor, a real AlpacaRead) and the production
BrokerHandle; both vendors are the real alpaca-py clients over recorded transports, and the store is a double that
answers every report FINAL. The report's feed is judged on the wire form (feed_rule). Pass-through spies on the
handle's own scheduler and on broker_api's environment read only observe. No vendor key is read and no network is
reached.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import InvalidRequestError, Reason
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.handle import ALPACA_DATA_SCOPE, ALPACA_TRADING_SCOPE, AlpacaHandle
from data.ingest.app.brokers.bars_attempt import BarsWork
from data.ingest.app.brokers.handle import BrokerHandle
from data.ingest.app.brokers.interface import BarsFailure, BarsQuery, BarsResponse, Instrument
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler.executor import ExecutorSettings
from data.ingest.app.scheduler.queue import ScheduledRequest
from data.ingest.app.scheduler.retry import RetryPolicy
from data.ingest.tests.alpaca_recorded import asset_answer, bars_answer, stub_trading_vendor, stub_vendor, wire_bar
from data.ingest.tests.feed_rule import assert_a_bars_report_names_the_tape, assert_a_no_tape_report_is_unspecified
from data.ingest.tests.scheduler_doubles import RecordingScheduler, alpaca_reader
from schemas.data_ingest.delivery import AttemptDelivery, DeliverAttemptResponse, DeliveryOutcome, DeliveryVerdict
from schemas.data_ingest.submit import (
    AssetListRequest,
    AssetListStatus,
    BarsRequest,
    CorporateActionsRequest,
    SubmitRequest,
)


pytestmark = pytest.mark.data_ingest

SIP_VAR = 'ALPACA_SIP_ENABLED'
SYMBOL = 'VFV'
VFV_ID = '5b0c7a3e-1d2f-4e6a-9b8c-0d1e2f3a4b5c'
START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
END = START + timedelta(hours=1)
MAX_ATTEMPTS = 5
GUARD_S = 5.0
INSTANCE = 'instance-under-test'

# The setting's values and the feed the handle must resolve from each. Unset and false are IEX: no deployment moves
# tape (the bead's acceptance).
SETTINGS = pytest.mark.parametrize(
    ('setting', 'expected'),
    [
        pytest.param(None, Feed.IEX, id='unset-iex'),
        pytest.param('false', Feed.IEX, id='false-iex'),
        pytest.param('true', Feed.SIP, id='true-sip'),
    ],
)
# Each feed, with the setting's value that names the OTHER tape.
BUILT_THEN_FLIPPED = pytest.mark.parametrize(
    ('built_with', 'expected', 'flipped_to'),
    [
        pytest.param('false', Feed.IEX, 'true', id='iex-then-true'),
        pytest.param('true', Feed.SIP, 'false', id='sip-then-false'),
    ],
)


# ---------------------------------------------------------------------------------------------
# Doubles and harness
# ---------------------------------------------------------------------------------------------


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

    async def report_for(self, request_id: str) -> AttemptDelivery:
        """The first report of the given request, waiting for it within the guard."""

        async def wait() -> AttemptDelivery:
            while True:
                for attempt in self.received:
                    if attempt.header.request_id == request_id:
                        return attempt
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(wait(), GUARD_S)


class SettingReads:
    """A pass-through on broker_api's environment read, counting each read of ALPACA_SIP_ENABLED.

    broker_api.sip_enabled looks get_env_var up on its module at call time, so this sees every read of the setting
    made through it, whoever calls it.
    """

    def __init__(self, monkeypatch) -> None:
        self.count = 0
        real = broker_api.get_env_var

        def counted(name, *args, **kwargs):
            if name == SIP_VAR:
                self.count += 1
            return real(name, *args, **kwargs)

        monkeypatch.setattr(broker_api, 'get_env_var', counted)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """The setting unset, no credential, no trading host, no rate-limit override, no cached client or reset."""
    monkeypatch.delenv(SIP_VAR, raising=False)
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


def set_setting(monkeypatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(SIP_VAR, raising=False)
    else:
        monkeypatch.setenv(SIP_VAR, value)


def alpaca_handle() -> AlpacaHandle:
    settings = ExecutorSettings(
        buffer_size=8,
        workers=1,
        stall_timeout_s=300.0,
        stall_check_interval_s=3600.0,
        redelivery_base_s=1.0,
        redelivery_max_s=1.5,
        retry=RetryPolicy(max_attempts=MAX_ATTEMPTS, base_delay_s=1.0, max_delay_s=60.0, factor=2.0),
    )
    return AlpacaHandle(settings=settings, calls_per_minute=60)


def queued_by(handle: BrokerHandle) -> list[ScheduledRequest]:
    """Record every request the handle queues, passing each through to its own scheduler unchanged."""
    queued: list[ScheduledRequest] = []
    submit = handle.scheduler.submit

    async def recorded(request: ScheduledRequest) -> None:
        queued.append(request)
        await submit(request)

    handle.scheduler.submit = recorded
    return queued


def bars_request(feed: Feed | None = None) -> BarsRequest:
    return BarsRequest(
        source=DataSource.ALPACA_API,
        asset_symbol=SYMBOL,
        asset_type=AssetType.STOCK,
        data_types=[DataType.MARKET_ACTIVITY],
        granularity=Granularity.THIRTY_MINUTES,
        start=START,
        end=END,
        update_type=UpdateType.STATIC,
        feed=feed,
    )


def bars_query(feed: Feed | None = None) -> BarsQuery:
    return BarsQuery(
        instrument=Instrument(SYMBOL, AssetType.STOCK),
        granularity=Granularity.THIRTY_MINUTES,
        start=START,
        end=END,
        priority=RequestPriority.INTERACTIVE,
        feed=feed,
    )


def one_bar_page():
    return bars_answer({SYMBOL: [wire_bar(START)]})


async def served(outcome: BarsResponse | BarsFailure) -> BarsResponse:
    """Assert the read served, and drain it so the vendor call it streams is complete."""
    assert isinstance(outcome, BarsResponse), f'expected a BarsResponse, got {outcome!r}'
    assert [bar async for bar in outcome.bars], 'the vendor served nothing'
    return outcome


async def run_one_bars_request(handle: AlpacaHandle, request_id: str = 'req-1'):
    """Submit one bars request (no feed named) to a started handle and return its ack and the store's report."""
    store = AnsweringStore()
    handle.start(store, INSTANCE)
    try:
        ack = await handle.submit(SubmitRequest(request_id=request_id, owner='rebalancer', bars=bars_request()))
        report = await store.first()
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)
    return ack, report


# ---------------------------------------------------------------------------------------------
# The setting decides the handle's feed, and every leg agrees with it
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@SETTINGS
async def test_the_setting_gives_the_handles_feed_and_the_ack_work_vendor_response_and_report_agree(
    monkeypatch, setting, expected
):
    set_setting(monkeypatch, setting)
    vendor = stub_vendor(one_bar_page(), one_bar_page())
    broker_api.set_client(vendor.client)
    broker_api.set_trading_client(stub_trading_vendor(asset_answer(SYMBOL, VFV_ID)).client)

    handle = alpaca_handle()
    queued = queued_by(handle)
    assert handle.feed is expected

    ack, report = await run_one_bars_request(handle)
    # The FetchDataset path reads through the same reader.
    response = await served(await handle.reader.get_bars(bars_query()))

    assert ack.accepted is not None
    assert ack.accepted.feed is expected, 'the ack names a tape other than the handle feed'
    [request] = queued
    assert isinstance(request.payload, BarsWork)
    assert request.payload.feed is expected, 'the queued BarsWork carries a tape other than the handle feed'
    assert request.feed is expected
    assert [sent.params['feed'] for sent in vendor.sent] == [expected.value.lower()] * 2, (
        'the vendor was asked for another tape'
    )
    assert response.feed is expected
    assert report.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert report.header.feed is expected
    assert_a_bars_report_names_the_tape(report.header, expected)


# ---------------------------------------------------------------------------------------------
# Read once, at build: changing the setting afterwards changes nothing
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@SETTINGS
async def test_the_handle_reads_the_setting_exactly_once_when_built_and_never_per_request(
    monkeypatch, setting, expected
):
    set_setting(monkeypatch, setting)
    reads = SettingReads(monkeypatch)
    broker_api.set_client(stub_vendor(one_bar_page(), one_bar_page()).client)
    broker_api.set_trading_client(stub_trading_vendor(asset_answer(SYMBOL, VFV_ID)).client)

    handle = alpaca_handle()
    assert reads.count == 1, 'the handle did not read the setting exactly once at build'

    await run_one_bars_request(handle)
    await served(await handle.reader.get_bars(bars_query()))
    await handle.submit(SubmitRequest(request_id='named', owner='rebalancer', bars=bars_request(feed=expected)))

    assert reads.count == 1, 'the setting was read again after build'


@pytest.mark.asyncio
@BUILT_THEN_FLIPPED
async def test_changing_the_setting_after_build_changes_no_submit_and_no_get_bars(
    monkeypatch, built_with, expected, flipped_to
):
    monkeypatch.setenv(SIP_VAR, built_with)
    vendor = stub_vendor(one_bar_page(), one_bar_page())
    broker_api.set_client(vendor.client)
    broker_api.set_trading_client(stub_trading_vendor(asset_answer(SYMBOL, VFV_ID)).client)
    handle = alpaca_handle()
    queued = queued_by(handle)

    monkeypatch.setenv(SIP_VAR, flipped_to)
    ack, report = await run_one_bars_request(handle)
    response = await served(await handle.reader.get_bars(bars_query()))
    # A request naming the built feed is still honoured; one naming the flipped-to feed is refused.
    honoured = await handle.submit(SubmitRequest(request_id='same', owner='o', bars=bars_request(feed=expected)))
    other = Feed.SIP if expected is Feed.IEX else Feed.IEX
    refused = await handle.submit(SubmitRequest(request_id='other', owner='o', bars=bars_request(feed=other)))

    assert handle.feed is expected
    assert ack.accepted.feed is expected, 'a submit after the change followed the new setting'
    assert queued[0].payload.feed is expected
    assert report.header.feed is expected
    assert_a_bars_report_names_the_tape(report.header, expected)
    assert response.feed is expected, 'a get_bars after the change followed the new setting'
    assert [sent.params['feed'] for sent in vendor.sent] == [expected.value.lower()] * 2
    assert honoured.accepted is not None
    assert honoured.accepted.feed is expected
    assert refused.refused is not None
    assert refused.refused.reason == Reason.FEED_NOT_AVAILABLE.value


# ---------------------------------------------------------------------------------------------
# AlpacaRead: serves its own feed, refuses a differing named one before any vendor call
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sip_reader_refuses_a_query_naming_iex_before_any_vendor_call_or_token(monkeypatch):
    # The setting names IEX, so a reader that consulted it would serve the query rather than refuse it.
    monkeypatch.setenv(SIP_VAR, 'false')
    vendor = stub_vendor(one_bar_page())
    scheduler = RecordingScheduler()
    reader = alpaca_reader(vendor.client, scheduler=scheduler, feed=Feed.SIP)

    outcome = await reader.get_bars(bars_query(feed=Feed.IEX))

    assert isinstance(outcome, BarsFailure), f'expected a refusal, got {outcome!r}'
    assert isinstance(outcome.error, InvalidRequestError)
    assert outcome.error.reason is Reason.FEED_NOT_AVAILABLE
    assert dict(outcome.error.metadata) == {'feed': Feed.IEX.value}
    assert vendor.sent == [], 'the vendor was called before the feed refusal'
    assert scheduler.acquires == [], 'a rate token was spent before the feed refusal'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('feed', 'setting'), [pytest.param(Feed.SIP, 'false', id='sip'), pytest.param(Feed.IEX, 'true', id='iex')]
)
async def test_a_query_naming_the_readers_own_feed_is_honoured_whatever_the_setting(monkeypatch, feed, setting):
    # The setting names the other tape: the reader reads no environment, so it serves its own.
    monkeypatch.setenv(SIP_VAR, setting)
    vendor = stub_vendor(one_bar_page(), one_bar_page())
    reader = alpaca_reader(vendor.client, feed=feed)

    named = await served(await reader.get_bars(bars_query(feed=feed)))
    unnamed = await served(await reader.get_bars(bars_query()))

    assert named.feed is feed
    assert unnamed.feed is feed
    assert [sent.params['feed'] for sent in vendor.sent] == [feed.value.lower()] * 2


# ---------------------------------------------------------------------------------------------
# The refusal's wording names the account (tj-7zjov1.156), at both sites
# ---------------------------------------------------------------------------------------------

# The handle's feed, the setting that builds it, and the other tape a request names.
ACCOUNT_REFUSES_OTHER = pytest.mark.parametrize(
    ('own', 'setting', 'other'),
    [
        pytest.param(Feed.SIP, 'true', Feed.IEX, id='sip-account-refuses-iex'),
        pytest.param(Feed.IEX, 'false', Feed.SIP, id='iex-account-refuses-sip'),
    ],
)


def account_refusal_detail(feed: Feed) -> str:
    return f'This account cannot serve the requested feed: feed={feed.value}'


@pytest.mark.asyncio
@ACCOUNT_REFUSES_OTHER
async def test_the_submit_refusal_detail_names_the_account_with_the_feed_suffix(monkeypatch, own, setting, other):
    monkeypatch.setenv(SIP_VAR, setting)
    handle = alpaca_handle()
    assert handle.feed is own

    ack = await handle.submit(SubmitRequest(request_id='named-other', owner='o', bars=bars_request(feed=other)))

    assert ack.accepted is None
    assert ack.refused is not None
    assert ack.refused.reason == Reason.FEED_NOT_AVAILABLE.value
    assert ack.refused.detail == account_refusal_detail(other)
    assert dict(ack.refused.metadata) == {'feed': other.value}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('own', 'other'),
    [
        pytest.param(Feed.SIP, Feed.IEX, id='sip-reader-refuses-iex'),
        pytest.param(Feed.IEX, Feed.SIP, id='iex-reader-refuses-sip'),
    ],
)
async def test_the_get_bars_refusal_detail_names_the_account_with_the_feed_suffix(own, other):
    vendor = stub_vendor(one_bar_page())
    reader = alpaca_reader(vendor.client, feed=own)

    outcome = await reader.get_bars(bars_query(feed=other))

    assert isinstance(outcome, BarsFailure), f'expected a refusal, got {outcome!r}'
    assert outcome.error.reason is Reason.FEED_NOT_AVAILABLE
    assert outcome.error.detail == account_refusal_detail(other)
    assert dict(outcome.error.metadata) == {'feed': other.value}
    assert vendor.sent == []


# ---------------------------------------------------------------------------------------------
# A kind with no tape gets none, whatever the handle's feed
# ---------------------------------------------------------------------------------------------


LISTING = AssetListRequest(source=DataSource.ALPACA_API, asset_type=AssetType.STOCK, status=AssetListStatus.ACTIVE)
ACTIONS = CorporateActionsRequest(
    source=DataSource.ALPACA_API,
    asset_symbol=SYMBOL,
    start=datetime(2026, 1, 1, tzinfo=UTC),
    end=datetime(2026, 2, 1, tzinfo=UTC),
)
NO_TAPE_KINDS = pytest.mark.parametrize(
    ('body', 'scope'),
    [
        pytest.param({'asset_list': LISTING}, ALPACA_TRADING_SCOPE, id='asset_list'),
        pytest.param({'corporate_actions': ACTIONS}, ALPACA_DATA_SCOPE, id='corporate_actions'),
    ],
)


@pytest.mark.asyncio
@NO_TAPE_KINDS
@pytest.mark.parametrize('setting', ['true', 'false'], ids=['sip-handle', 'iex-handle'])
async def test_a_no_tape_kind_carries_no_feed_on_the_ack_the_queue_or_the_report(monkeypatch, body, scope, setting):
    monkeypatch.setenv(SIP_VAR, setting)
    handle = alpaca_handle()
    queued = queued_by(handle)
    store = AnsweringStore()
    handle.start(store, INSTANCE)
    try:
        ack = await handle.submit(SubmitRequest(request_id='queued', owner='world-manager', **body))
        # At the budget: reported EXHAUSTED at once with no vendor call, so the report is the kind rule alone.
        exhausted = await handle.submit(
            SubmitRequest(request_id='spent', owner='world-manager', attempt_count=MAX_ATTEMPTS, **body)
        )
        report = await store.report_for('spent')
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)

    assert ack.accepted is not None
    assert ack.accepted.feed is None, "the handle's feed was guessed onto a kind with no tape"
    assert ack.accepted.quota_key == scope
    assert exhausted.accepted.feed is None
    assert queued[0].feed is None, "the queued request carries the handle's feed"
    assert report.header.request_id == 'spent'
    assert report.trailer.outcome is DeliveryOutcome.EXHAUSTED
    assert report.header.feed is None
    assert_a_no_tape_report_is_unspecified(report.header)


# ---------------------------------------------------------------------------------------------
# BrokerHandle.feed
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize('feed', [Feed.IEX, Feed.SIP], ids=['iex', 'sip'])
def test_broker_handle_feed_is_the_value_it_was_built_with_and_is_read_only(feed):
    handle = BrokerHandle(
        source=DataSource.ALPACA_API,
        scope='TEST_DATA',
        scheduler=Mock(name='scheduler'),
        executor=Mock(name='executor'),
        reader=Mock(name='reader'),
        feed=feed,
    )

    assert handle.feed is feed
    with pytest.raises(AttributeError):
        handle.feed = Feed.IEX if feed is Feed.SIP else Feed.SIP
    assert handle.feed is feed


def test_broker_handle_takes_no_feed_resolver():
    # The account's feed is a value passed at construction; a per-request resolver is no longer accepted.
    with pytest.raises(TypeError):
        BrokerHandle(
            source=DataSource.ALPACA_API,
            scope='TEST_DATA',
            scheduler=Mock(),
            executor=Mock(),
            reader=Mock(),
            resolve_feed=lambda: Feed.IEX,
        )
