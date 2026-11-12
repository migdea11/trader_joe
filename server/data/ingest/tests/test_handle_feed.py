"""The Alpaca feed is decided per request inside the Alpaca handle: SIP by default, IEX when named, no setting.

Against builder-ingest's 8f5ee32 and ab08d4d, checked against the owner rulings of 2026-11-06 (Q tj-7zjov1.341: "I
don't think this should be config at all. Default to SIP and IEX is only if explicitly requested"), ADR tj-xn3qa6
addendum 3 (supersedes "one handle, one feed") and tj-7zjov1.342: broker_api.resolve_feed maps a request naming no feed
to ALPACA_DEFAULT_FEED (SIP), SIP to SIP, IEX to IEX, and any other feed (NOT_APPLICABLE) to None, refused
FEED_NOT_AVAILABLE. The generic BrokerHandle takes that rule as its feed policy, calls it once per bars Submit and
decides no feed itself; the resolved feed rides on the ack, the queued BarsWork and ScheduledRequest, the vendor's feed
parameter, the response and the report the store receives. Nothing reads ALPACA_SIP_ENABLED: setting it changes
nothing. A kind with no tape still gets none, so unspecified is never replaced by a guess.

WHAT IS REAL. The production AlpacaHandle (its Scheduler, its AckGatedExecutor, a real AlpacaRead) and the production
BrokerHandle; both vendors are the real alpaca-py clients over recorded transports, and the store is a double that
answers every report FINAL. The report's feed is judged on the wire form (feed_rule). No vendor key is read and no
network is reached. START lies far behind the SIP recent-data edge, so the edge (test_alpaca_sip_edge) never decides
an outcome here.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import InvalidRequestError, Reason
from common.tests.roots import REPO_ROOT, SERVER_ROOT
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

# The retired setting. Named here only to prove nothing reads it.
RETIRED_VAR = 'ALPACA_SIP_ENABLED'
SYMBOL = 'VFV'
VFV_ID = '5b0c7a3e-1d2f-4e6a-9b8c-0d1e2f3a4b5c'
START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
END = START + timedelta(hours=1)
MAX_ATTEMPTS = 5
GUARD_S = 5.0
INSTANCE = 'instance-under-test'

# The feed a request names and the feed it must be served on (owner ruling 2026-11-06).
SERVED = pytest.mark.parametrize(
    ('named', 'expected'),
    [
        pytest.param(None, Feed.SIP, id='unnamed-sip'),
        pytest.param(Feed.SIP, Feed.SIP, id='sip-sip'),
        pytest.param(Feed.IEX, Feed.IEX, id='iex-iex'),
    ],
)

# Values of the retired setting, each of which once moved the tape. None of them may move it now.
RETIRED_SETTINGS = pytest.mark.parametrize('setting', [None, 'false', 'true'], ids=['unset', 'false', 'true'])


# ---------------------------------------------------------------------------------------------
# Doubles and harness
# ---------------------------------------------------------------------------------------------


class AnsweringStore:
    """A StoreDeliveryClient that answers every delivery FINAL at once and records it."""

    def __init__(self) -> None:
        self.received: list[AttemptDelivery] = []

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        self.received.append(attempt)
        return DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=attempt.header.attempt_no)

    async def register_instance(self, request):
        raise AssertionError('the executor never registers an instance')

    async def report_for(self, request_id: str) -> AttemptDelivery:
        """The first report of the given request, waiting for it within the guard."""

        async def wait() -> AttemptDelivery:
            while True:
                for attempt in self.received:
                    if attempt.header.request_id == request_id:
                        return attempt
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(wait(), GUARD_S)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """The retired setting unset, no credential, no trading host, no rate-limit override, no cached client or reset."""
    monkeypatch.delenv(RETIRED_VAR, raising=False)
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


def set_retired(monkeypatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(RETIRED_VAR, raising=False)
    else:
        monkeypatch.setenv(RETIRED_VAR, value)


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
    # The ceiling: several vendor calls per test, and pacing is not what this file is about.
    return AlpacaHandle(settings=settings, calls_per_minute=200)


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


def vendors(pages: int):
    """Install a bars vendor answering `pages` one-bar pages and a trading vendor for the asset lookups."""
    vendor = stub_vendor(*(one_bar_page() for _ in range(pages)))
    broker_api.set_client(vendor.client)
    broker_api.set_trading_client(stub_trading_vendor(*(asset_answer(SYMBOL, VFV_ID) for _ in range(pages))).client)
    return vendor


async def served(outcome: BarsResponse | BarsFailure) -> BarsResponse:
    """Assert the read served, and drain it so the vendor call it streams is complete."""
    assert isinstance(outcome, BarsResponse), f'expected a BarsResponse, got {outcome!r}'
    assert [bar async for bar in outcome.bars], 'the vendor served nothing'
    return outcome


async def run_bars_requests(handle: AlpacaHandle, *named: Feed | None):
    """Submit one bars request per named feed to a started handle; return the acks and the store's reports."""
    store = AnsweringStore()
    handle.start(store, INSTANCE)
    try:
        acks, reports = [], []
        for index, feed in enumerate(named):
            request_id = f'req-{index}'
            acks.append(
                await handle.submit(SubmitRequest(request_id=request_id, owner='rebalancer', bars=bars_request(feed)))
            )
            reports.append(await store.report_for(request_id))
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)
    return acks, reports


# ---------------------------------------------------------------------------------------------
# resolve_feed: the vendor rule, exact
# ---------------------------------------------------------------------------------------------


def test_the_default_feed_is_sip():
    assert broker_api.ALPACA_DEFAULT_FEED is Feed.SIP


@pytest.mark.parametrize(
    ('requested', 'expected'),
    [
        pytest.param(None, Feed.SIP, id='none-sip'),
        pytest.param(Feed.SIP, Feed.SIP, id='sip-sip'),
        pytest.param(Feed.IEX, Feed.IEX, id='iex-iex'),
        pytest.param(Feed.NOT_APPLICABLE, None, id='not_applicable-refused'),
    ],
)
def test_resolve_feed_table(requested, expected):
    assert broker_api.resolve_feed(requested) is expected


def test_resolve_feed_serves_only_sip_and_iex_of_every_feed():
    # A new Feed member must be decided here on purpose, not served by accident.
    assert {feed: broker_api.resolve_feed(feed) for feed in Feed} == {
        feed: (feed if feed in (Feed.SIP, Feed.IEX) else None) for feed in Feed
    }


@RETIRED_SETTINGS
def test_resolve_feed_ignores_the_retired_setting(monkeypatch, setting):
    set_retired(monkeypatch, setting)
    assert [broker_api.resolve_feed(feed) for feed in (None, Feed.SIP, Feed.IEX, Feed.NOT_APPLICABLE)] == [
        Feed.SIP,
        Feed.SIP,
        Feed.IEX,
        None,
    ]


def test_broker_api_no_longer_has_sip_enabled():
    assert not hasattr(broker_api, 'sip_enabled')


# ---------------------------------------------------------------------------------------------
# Through the handle: every leg names the feed resolved for THAT request
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@SERVED
async def test_a_bars_submit_is_served_on_its_resolved_feed_on_every_leg(named, expected):
    vendor = vendors(2)
    handle = alpaca_handle()
    queued = queued_by(handle)

    [ack], [report] = await run_bars_requests(handle, named)
    # A direct read through the same reader resolves the same way.
    response = await served(await handle.reader.get_bars(bars_query(named)))

    assert ack.accepted is not None, f'refused: {ack.refused!r}'
    assert ack.accepted.feed is expected, 'the ack names a tape other than the one resolved for the request'
    assert ack.accepted.quota_key == ALPACA_DATA_SCOPE
    [request] = queued
    assert isinstance(request.payload, BarsWork)
    assert request.payload.feed is expected, 'the queued BarsWork carries another tape'
    assert request.feed is expected, 'the ScheduledRequest carries another tape'
    assert [sent.params['feed'] for sent in vendor.sent] == [expected.value.lower()] * 2, (
        'the vendor was asked for another tape'
    )
    assert response.feed is expected
    assert report.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert report.header.feed is expected
    assert_a_bars_report_names_the_tape(report.header, expected)


@pytest.mark.asyncio
async def test_one_handle_serves_requests_naming_different_feeds_each_on_its_own():
    # Per request, not per account: the same handle serves SIP, IEX and the unnamed default side by side.
    vendor = vendors(3)
    handle = alpaca_handle()
    queued = queued_by(handle)

    acks, reports = await run_bars_requests(handle, Feed.IEX, None, Feed.SIP)

    expected = [Feed.IEX, Feed.SIP, Feed.SIP]
    assert [ack.accepted.feed for ack in acks] == expected
    assert [request.payload.feed for request in queued] == expected
    assert [request.feed for request in queued] == expected
    assert [sent.params['feed'] for sent in vendor.sent] == [feed.value.lower() for feed in expected]
    assert [report.header.feed for report in reports] == expected
    assert [report.trailer.outcome for report in reports] == [DeliveryOutcome.SUCCEEDED] * 3


@pytest.mark.asyncio
@RETIRED_SETTINGS
async def test_the_retired_setting_changes_no_submit_and_no_get_bars(monkeypatch, setting):
    # Set before the handle is built AND flipped after it: no value, read at either time, moves a tape.
    set_retired(monkeypatch, setting)
    vendor = vendors(4)
    handle = alpaca_handle()
    set_retired(monkeypatch, 'true' if setting != 'true' else 'false')

    acks, reports = await run_bars_requests(handle, None, Feed.IEX)
    unnamed = await served(await handle.reader.get_bars(bars_query()))
    iex = await served(await handle.reader.get_bars(bars_query(Feed.IEX)))

    assert [ack.accepted.feed for ack in acks] == [Feed.SIP, Feed.IEX]
    assert [report.header.feed for report in reports] == [Feed.SIP, Feed.IEX]
    assert (unnamed.feed, iex.feed) == (Feed.SIP, Feed.IEX)
    assert [sent.params['feed'] for sent in vendor.sent] == ['sip', 'iex', 'sip', 'iex']


def test_no_production_file_names_the_retired_setting():
    # The owner ruled the feed is not config at all. Every production source and deployment file is scanned; the test
    # trees are not (they name it to prove it is dead), nor tools/agent_mcp, whose stack tool keeps it on a deny list.
    candidates = [
        *(path for path in SERVER_ROOT.rglob('*.py') if 'tests' not in path.relative_to(SERVER_ROOT).parts),
        *SERVER_ROOT.rglob('.env.default'),
        *REPO_ROOT.glob('docker-compose*.yaml'),
        *REPO_ROOT.glob('.env.default'),
        *(REPO_ROOT / '.github').rglob('*.yml'),
        REPO_ROOT / 'Dockerfile',
        REPO_ROOT / 'Makefile',
        REPO_ROOT / 'entrypoint.sh',
    ]
    scanned = [path for path in candidates if path.is_file()]
    assert len(scanned) > 50, f'the scan found too few files to mean anything: {len(scanned)}'
    naming = [str(path.relative_to(REPO_ROOT)) for path in scanned if RETIRED_VAR in path.read_text(errors='replace')]
    assert naming == [], f'{RETIRED_VAR} is named again: {naming}'


def test_no_sip_default_outside_the_alpaca_handle():
    # SIP is Alpaca's default and nobody else's: no production module outside brokers/alpaca names Feed.SIP (the enum's
    # own definition aside), so server/common, server/schemas, data_store and the generic handle decide no feed.
    alpaca = SERVER_ROOT / 'data' / 'ingest' / 'app' / 'brokers' / 'alpaca'
    naming = [
        str(path.relative_to(REPO_ROOT))
        for path in SERVER_ROOT.rglob('*.py')
        if 'tests' not in path.relative_to(SERVER_ROOT).parts
        and alpaca not in path.parents
        and 'Feed.SIP' in path.read_text(errors='replace')
    ]
    assert naming == []


# ---------------------------------------------------------------------------------------------
# NOT_APPLICABLE: the one feed Alpaca cannot serve, refused at both sites before any vendor work
# ---------------------------------------------------------------------------------------------

UNSERVABLE_DETAIL = 'This broker cannot serve the requested feed: feed=NOT_APPLICABLE'


@pytest.mark.asyncio
async def test_a_submit_naming_not_applicable_is_refused_feed_not_available_and_queues_nothing():
    vendor = vendors(0)
    handle = alpaca_handle()
    queued = queued_by(handle)

    ack = await handle.submit(SubmitRequest(request_id='na', owner='o', bars=bars_request(Feed.NOT_APPLICABLE)))

    assert ack.accepted is None
    assert ack.refused is not None
    assert ack.refused.reason == Reason.FEED_NOT_AVAILABLE.value
    assert ack.refused.detail == UNSERVABLE_DETAIL
    assert dict(ack.refused.metadata) == {'feed': Feed.NOT_APPLICABLE.value}
    assert queued == []
    assert vendor.sent == []


@pytest.mark.asyncio
async def test_a_get_bars_naming_not_applicable_is_refused_before_any_vendor_call_or_token():
    vendor = stub_vendor(one_bar_page())
    scheduler = RecordingScheduler()
    reader = alpaca_reader(vendor.client, scheduler=scheduler)

    outcome = await reader.get_bars(bars_query(Feed.NOT_APPLICABLE))

    assert isinstance(outcome, BarsFailure), f'expected a refusal, got {outcome!r}'
    assert isinstance(outcome.error, InvalidRequestError)
    assert outcome.error.reason is Reason.FEED_NOT_AVAILABLE
    assert outcome.error.detail == UNSERVABLE_DETAIL
    assert dict(outcome.error.metadata) == {'feed': Feed.NOT_APPLICABLE.value}
    assert vendor.sent == [], 'the vendor was called before the feed refusal'
    assert scheduler.acquires == [], 'a rate token was spent before the feed refusal'


@pytest.mark.asyncio
@SERVED
async def test_a_bare_reader_serves_the_feed_each_query_names(named, expected):
    # The reader holds no feed of its own: built once, it serves whatever each query resolves to.
    vendor = stub_vendor(one_bar_page())
    reader = alpaca_reader(vendor.client)

    response = await served(await reader.get_bars(bars_query(named)))

    assert response.feed is expected
    assert [sent.params['feed'] for sent in vendor.sent] == [expected.value.lower()]


# ---------------------------------------------------------------------------------------------
# A kind with no tape gets none, whatever feed the bars requests resolve to
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
async def test_a_no_tape_kind_carries_no_feed_on_the_ack_the_queue_or_the_report(body, scope):
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
    assert ack.accepted.feed is None, 'the default tape was guessed onto a kind with no tape'
    assert ack.accepted.quota_key == scope
    assert exhausted.accepted.feed is None
    assert queued[0].feed is None, 'the queued request carries a tape'
    assert report.header.request_id == 'spent'
    assert report.trailer.outcome is DeliveryOutcome.EXHAUSTED
    assert report.header.feed is None
    assert_a_no_tape_report_is_unspecified(report.header)


# ---------------------------------------------------------------------------------------------
# BrokerHandle: a feed policy, never an account feed
# ---------------------------------------------------------------------------------------------


def broker_handle(**feed_kwargs) -> BrokerHandle:
    return BrokerHandle(
        source=DataSource.ALPACA_API,
        scope='TEST_DATA',
        scheduler=Mock(name='scheduler'),
        executor=Mock(name='executor'),
        reader=Mock(name='reader'),
        **feed_kwargs,
    )


def test_broker_handle_requires_a_feed_policy_and_has_no_default():
    with pytest.raises(TypeError):
        broker_handle()


@pytest.mark.parametrize('feed', [Feed.IEX, Feed.SIP], ids=['iex', 'sip'])
def test_broker_handle_takes_no_account_feed_and_exposes_none(feed):
    with pytest.raises(TypeError):
        broker_handle(feed=feed)
    assert not hasattr(broker_handle(feed_policy=broker_api.resolve_feed), 'feed')


def test_the_alpaca_handle_exposes_no_account_feed():
    assert not hasattr(alpaca_handle(), 'feed')
