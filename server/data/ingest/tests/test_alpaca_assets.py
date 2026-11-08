"""Alpaca's asset-list and asset-by-symbol reads (P12, tj-7zjov1.22), against builder-ingest's 35b436a.

Checked against the bead's design notes, which bind: the 17:38 UTC P27a s1 inputs (read the list RAW, never
through the alpaca-py Asset model, whose closed exchange enum has no TXSE; send asset_class=us_equity explicitly;
the field mapping; the trading host's own rate-limit headers; the trading host is configuration), as amended at
21:54 UTC (ONE vendor call per request, for the one status it names, never both and never absent; name filled as
given) and at 20:29 UTC (the by-symbol read sits on BrokerRead, paced on the trading-API scope, a vendor 404 its
own typed outcome, transient and rate failures typed with reset_at). Acceptance: the list paced on the
trading-API scope; the vendor asset id opaque data and never a call argument; nothing judged.

HOW IT IS DRIVEN. The REAL alpaca-py TradingClient over the recorded transport (alpaca_recorded.stub_trading_vendor)
on an invented trading host, with constructed bodies served byte for byte. Pacing is observed through
RecordingScheduler, which records the SCOPE of every acquire, so a read charged to the data scope is caught; the
deadline case uses a real Scheduler. No vendor key is read: the placeholder pair is the only credential here.
"""

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pydantic
import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Asset as SdkAsset

from common.enums.data_select import AssetType
from common.enums.data_stock import Feed
from common.errors.vocabulary import ExogenousError, InvalidRequestError, Reason
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.assets import asset_from_record, parse_asset, parse_asset_list
from data.ingest.app.brokers.alpaca.classify import RATE_LIMIT_RESET_HEADER
from data.ingest.app.brokers.alpaca.handle import ALPACA_DATA_SCOPE, ALPACA_TRADING_SCOPE
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.broker_errors import MissingCredentialsError
from data.ingest.app.brokers.interface import (
    Asset,
    AssetListFailure,
    AssetListQuery,
    AssetListResponse,
    AssetLookupFailure,
    AssetLookupQuery,
    AssetNotFound,
    AssetResponse,
    BrokerUnsupportedError,
    CallTally,
)
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler import scheduler as scheduler_module
from data.ingest.tests.alpaca_recorded import (
    ALPACA_TRADING_TEST_HOST,
    ALPACA_TRADING_TEST_URL,
    ASSETS_PATH,
    PLACEHOLDER_KEY_ID,
    PLACEHOLDER_SECRET,
    raw_answer,
    status_answer,
    stub_trading_vendor,
)
from data.ingest.tests.scheduler_doubles import ONE_PER_SECOND, Acquire, RecordingScheduler, real_scheduler
from schemas.data_ingest.submit import AssetListStatus


pytestmark = pytest.mark.data_ingest

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=5)

# Constructed records, shaped as the P27a s1 captures describe them (keys, a TXSE venue, an empty name, the borrow
# fields beside each other, no cusip). Invented ids and names: these are not recordings.
AAPL_ID = 'b0b6dd9d-8b9b-48a9-ba46-b9d54906e415'
OBK_ID = '7e0a1c1e-0d55-4b8b-9f43-2c1f7a6b0a01'
GONE_ID = '0c3c8d3a-7f0f-4b1c-8f4b-5b3a6f0e9e22'

# Each element is written as the bytes the vendor sent: odd spacing, an escaped and a literal non-ASCII
# character, and brackets and commas inside strings, so a re-serialisation or a naive split cannot match them.
AAPL_RAW = (
    b'{"id": "' + AAPL_ID.encode() + b'", "class": "us_equity", "exchange": "NASDAQ", "symbol": "AAPL",'
    b' "name": "Apple Inc. Common Stock [x], \\u00e9", "status": "active", "tradable": true,'
    b' "easy_to_borrow": true, "borrow_status": "easy", "attributes": ["fractional_eh_enabled", "has_options"]}'
)
OBK_RAW = (
    '{"id":"' + OBK_ID + '","class":"us_equity","exchange":"TXSE","symbol":"OBK",'
    '"name":"Origin Bancorp, Inc. Société Générale","status":"active","tradable":false,'
    '"marginable":true,"shortable":true,"easy_to_borrow":false,"fractionable":true}'
).encode('utf-8')
GONE_RAW = (
    b'{\n  "id": "' + GONE_ID.encode() + b'",\n  "class": "us_equity",\n  "exchange": "OTC",\n'
    b'  "symbol": "GONE",\n  "name": "",\n  "status": "inactive",\n  "tradable": false\n}'
)
LIST_BODY = b' [\n  ' + AAPL_RAW + b' ,\n' + OBK_RAW + b',' + GONE_RAW + b'\n] '


def record(**overrides: object) -> dict:
    """One decoded vendor record, every neutral field present unless overridden; _ABSENT removes a key, None is null."""
    fields_ = {
        'id': AAPL_ID,
        'class': 'us_equity',
        'exchange': 'NASDAQ',
        'symbol': 'AAPL',
        'name': 'Apple Inc.',
        'status': 'active',
        'tradable': True,
    }
    fields_.update(overrides)
    return {key: value for key, value in fields_.items() if value is not _ABSENT}


_ABSENT = object()


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No cached trading client, no remembered reset, and no credential or trading host in the environment."""
    for name in (*broker_api.ALPACA_CREDENTIAL_VARS, broker_api.ALPACA_TRADING_HOST_VAR):
        monkeypatch.delenv(name, raising=False)
    broker_api.set_trading_client(None)
    broker_api.forget_resets()
    yield
    broker_api.set_trading_client(None)
    broker_api.forget_resets()


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        yield pool


def reader(
    executor: ThreadPoolExecutor,
    trading_client: TradingClient | None = None,
    *,
    scheduler=None,
    trading_scope: str | None = ALPACA_TRADING_SCOPE,
    clock=lambda: NOW,
) -> AlpacaRead:
    """A real AlpacaRead on the production scope names, reading the given trading client."""
    return AlpacaRead(
        executor_provider=lambda: executor,
        clock=clock,
        scheduler=scheduler if scheduler is not None else RecordingScheduler(),
        scope=ALPACA_DATA_SCOPE,
        trading_client=trading_client,
        trading_scope=trading_scope,
        feed=Feed.IEX,
    )


def list_query(status: AssetListStatus = AssetListStatus.ACTIVE, **overrides) -> AssetListQuery:
    fields_ = {
        'asset_type': AssetType.STOCK,
        'status': status,
        'priority': RequestPriority.BACKFILL,
        'deadline': DEADLINE,
    }
    return AssetListQuery(**{**fields_, **overrides})


def lookup(symbol: str = 'AAPL') -> AssetLookupQuery:
    return AssetLookupQuery(symbol, priority=RequestPriority.INTERACTIVE, deadline=DEADLINE)


def list_failure(outcome) -> ExogenousError | InvalidRequestError:
    assert isinstance(outcome, AssetListFailure), f'expected an AssetListFailure, got {outcome!r}'
    return outcome.error


def lookup_failure(outcome) -> ExogenousError | InvalidRequestError:
    assert isinstance(outcome, AssetLookupFailure), f'expected an AssetLookupFailure, got {outcome!r}'
    return outcome.error


# ---------------------------------------------------------------------------------------------
# The shape of the interface: the vendor id is data a read returns, never an argument
# ---------------------------------------------------------------------------------------------


def test_the_lookup_takes_a_symbol_and_never_a_vendor_asset_id():
    """Acceptance: the handle reports the vendor asset id as opaque data and never takes it as a call argument."""
    assert [field.name for field in fields(AssetLookupQuery)] == ['symbol', 'priority', 'deadline']
    assert [field.name for field in fields(AssetListQuery)] == ['asset_type', 'status', 'priority', 'deadline', 'tally']


def test_an_asset_carries_the_neutral_fields_and_the_raw_payload_and_nothing_else():
    """Design input (4): easy_to_borrow, borrow_status and every other key stay in raw_payload only; no cusip."""
    assert [field.name for field in fields(Asset)] == [
        'vendor_asset_id',
        'symbol',
        'raw_payload',
        'active',
        'tradable',
        'exchange',
        'name',
    ]


# ---------------------------------------------------------------------------------------------
# Read raw: each element's own bytes, and the SDK model never touched
# ---------------------------------------------------------------------------------------------


def test_each_listed_asset_keeps_its_own_bytes_exactly_as_served_in_the_order_served():
    assets = parse_asset_list(LIST_BODY)

    assert [asset.raw_payload for asset in assets] == [AAPL_RAW, OBK_RAW, GONE_RAW]
    assert [asset.symbol for asset in assets] == ['AAPL', 'OBK', 'GONE']
    assert [asset.vendor_asset_id for asset in assets] == [AAPL_ID, OBK_ID, GONE_ID]


def test_the_neutral_fields_are_copies_of_what_the_vendor_said():
    aapl, obk, gone = parse_asset_list(LIST_BODY)

    assert aapl == Asset(
        vendor_asset_id=AAPL_ID,
        symbol='AAPL',
        raw_payload=AAPL_RAW,
        active=True,
        tradable=True,
        exchange='NASDAQ',
        name='Apple Inc. Common Stock [x], é',
    )
    # A TXSE venue is carried as the vendor's string; an untradable active asset is still listed, unjudged.
    assert (obk.exchange, obk.active, obk.tradable, obk.name) == (
        'TXSE',
        True,
        False,
        'Origin Bancorp, Inc. Société Générale',
    )
    # Design input (4): an empty vendor name stays empty; an inactive asset is listed like any other.
    assert (gone.exchange, gone.active, gone.tradable, gone.name) == ('OTC', False, False, '')


def test_the_sdk_asset_model_still_refuses_a_txse_row_which_is_why_the_list_is_read_raw():
    """Design input (1). If this goes green the hazard has moved, not gone: any new venue breaks a closed enum."""
    with pytest.raises(pydantic.ValidationError):
        SdkAsset(**json.loads(OBK_RAW))


def test_a_list_holding_a_txse_row_parses_whole():
    assert [asset.exchange for asset in parse_asset_list(LIST_BODY)] == ['NASDAQ', 'TXSE', 'OTC']


@pytest.mark.parametrize('body', [b'[]', b' [ \n\t] ', b'\n[\r\n]\n'])
def test_an_empty_array_is_an_empty_list(body):
    assert parse_asset_list(body) == []


@pytest.mark.parametrize(
    ('status', 'active'),
    [
        pytest.param('active', True, id='active'),
        pytest.param('inactive', False, id='inactive'),
        pytest.param(_ABSENT, None, id='absent'),
        pytest.param(None, None, id='null'),
        pytest.param(1, None, id='not-a-string'),
    ],
)
def test_active_is_the_status_equal_to_active_and_none_when_the_vendor_gave_none(status, active):
    raw = json.dumps(record(status=status)).encode()

    assert asset_from_record(json.loads(raw), raw).active is active


@pytest.mark.parametrize(
    ('tradable', 'expected'),
    [
        pytest.param(True, True, id='true'),
        pytest.param(False, False, id='false'),
        pytest.param(_ABSENT, None, id='absent'),
        pytest.param(None, None, id='null'),
        pytest.param('true', None, id='a-string'),
    ],
)
def test_tradable_is_the_vendors_flag_and_none_never_false_when_it_gave_none(tradable, expected):
    """The P31 note: leave active or tradable unset, never false, when the vendor gives no value."""
    raw = json.dumps(record(tradable=tradable)).encode()

    assert asset_from_record(json.loads(raw), raw).tradable is expected


@pytest.mark.parametrize('key', ['exchange', 'name'])
@pytest.mark.parametrize('value', [_ABSENT, None, 7], ids=['absent', 'null', 'not-a-string'])
def test_a_venue_or_name_the_vendor_did_not_give_as_a_string_is_empty(key, value):
    raw = json.dumps(record(**{key: value})).encode()

    assert getattr(asset_from_record(json.loads(raw), raw), key) == ''


@pytest.mark.parametrize('exchange', ['NASDAQ', 'NYSE', 'ARCA', 'BATS', 'OTC', 'AMEX', 'TXSE', 'NYSEARCA', 'NEWVENUE'])
def test_every_venue_string_is_carried_as_given(exchange):
    raw = json.dumps(record(exchange=exchange)).encode()

    assert asset_from_record(json.loads(raw), raw).exchange == exchange


def test_the_by_symbol_answer_keeps_the_whole_body_as_its_raw_payload():
    asset = parse_asset(GONE_RAW)

    assert asset.raw_payload == GONE_RAW
    assert (asset.vendor_asset_id, asset.symbol, asset.active, asset.name) == (GONE_ID, 'GONE', False, '')


SENTINEL = 'body-text-that-must-not-reach-a-message'


@pytest.mark.parametrize(
    'body',
    [
        pytest.param(b'\xff\xfe[]', id='not-utf8'),
        pytest.param(b'{"id": "x", "symbol": "' + SENTINEL.encode() + b'"}', id='an-object-not-an-array'),
        pytest.param(b'', id='empty'),
        pytest.param(b'[' + AAPL_RAW, id='unterminated'),
        pytest.param(b'[' + AAPL_RAW + b' ' + OBK_RAW + b']', id='missing-comma'),
        pytest.param(b'[' + AAPL_RAW + b',]', id='trailing-comma'),
        pytest.param(b'["' + SENTINEL.encode() + b'"]', id='an-element-not-an-object'),
        pytest.param(b'[{"symbol": "' + SENTINEL.encode() + b'"}]', id='no-id'),
        pytest.param(b'[{"id": "", "symbol": "AAPL"}]', id='empty-id'),
        pytest.param(b'[{"id": 7, "symbol": "AAPL"}]', id='id-not-a-string'),
        pytest.param(b'[{"id": "x"}]', id='no-symbol'),
        pytest.param(b'[{"id": "x", "symbol": ""}]', id='empty-symbol'),
        # tj-7zjov1.150: one array and only whitespace after its closing bracket, at both return points.
        pytest.param(b'[]' + SENTINEL.encode(), id='empty-array-then-junk'),
        pytest.param(b'[] []', id='empty-array-then-a-second-array'),
        pytest.param(b'[]\n["' + SENTINEL.encode() + b'"]', id='empty-array-then-a-second-array-with-a-body'),
        pytest.param(b'[' + AAPL_RAW + b'][' + OBK_RAW + b']', id='array-then-a-second-array'),
        pytest.param(b'[' + AAPL_RAW + b'] x', id='array-then-a-stray-character'),
        pytest.param(b'[' + AAPL_RAW + b'],', id='array-then-a-comma'),
        pytest.param(b'[' + AAPL_RAW + b']\n' + SENTINEL.encode(), id='array-then-junk'),
    ],
)
def test_a_list_that_is_not_an_array_of_asset_records_is_vendor_corruption_raised_loud(body):
    with pytest.raises(ValueError) as raised:
        parse_asset_list(body)

    assert SENTINEL not in str(raised.value), 'the vendor body reached the error message'


TRAILING_WHITESPACE = [
    pytest.param(b' ', id='space'),
    pytest.param(b'\t', id='tab'),
    pytest.param(b'\r', id='cr'),
    pytest.param(b'\n', id='lf'),
    pytest.param(b' \t\r\n \r\n', id='mixed'),
]


@pytest.mark.parametrize('tail', TRAILING_WHITESPACE)
def test_an_empty_array_followed_only_by_whitespace_is_an_empty_list(tail):
    """tj-7zjov1.150: the trailing-content check refuses content, never the whitespace a body may end with."""
    assert parse_asset_list(b'[]' + tail) == []


@pytest.mark.parametrize('tail', TRAILING_WHITESPACE)
def test_a_list_followed_only_by_whitespace_parses_whole_with_each_element_sliced_byte_for_byte(tail):
    assets = parse_asset_list(LIST_BODY + tail)

    assert [asset.raw_payload for asset in assets] == [AAPL_RAW, OBK_RAW, GONE_RAW]
    assert [asset.symbol for asset in assets] == ['AAPL', 'OBK', 'GONE']


@pytest.mark.parametrize(
    'body',
    [
        pytest.param(b'\xff', id='not-utf8'),
        pytest.param(b'{"id": ', id='not-json'),
        pytest.param(b'[' + AAPL_RAW + b']', id='a-list-not-a-record'),
        pytest.param(b'{"symbol": "AAPL"}', id='no-id'),
    ],
)
def test_a_by_symbol_answer_that_is_not_one_record_is_raised_loud(body):
    with pytest.raises(ValueError):
        parse_asset(body)


# ---------------------------------------------------------------------------------------------
# get_assets: one call, the one status, asset_class=us_equity, on the trading scope
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('status', list(AssetListStatus), ids=lambda status: status.value)
async def test_a_list_is_one_get_for_the_one_status_named_with_us_equity_on_the_trading_host(executor, status):
    """Design inputs (2) and (3) as amended at 21:54 UTC: one call, never both statuses, never absent."""
    vendor = stub_trading_vendor(raw_answer(LIST_BODY))

    outcome = await reader(executor, vendor.client).get_assets(list_query(status))

    assert isinstance(outcome, AssetListResponse)
    [sent] = vendor.sent
    assert sent.method == 'GET'
    assert (sent.host, sent.path) == (ALPACA_TRADING_TEST_HOST, ASSETS_PATH)
    assert sent.params == {'status': status.value, 'asset_class': 'us_equity'}
    assert sent.headers['APCA-API-KEY-ID'] == PLACEHOLDER_KEY_ID
    assert sent.headers['APCA-API-SECRET-KEY'] == PLACEHOLDER_SECRET


@pytest.mark.asyncio
async def test_a_list_answer_is_every_asset_raw_in_order_stamped_with_the_readers_clock(executor):
    vendor = stub_trading_vendor(raw_answer(LIST_BODY))

    outcome = await reader(executor, vendor.client).get_assets(list_query())

    assert outcome == AssetListResponse(assets=parse_asset_list(LIST_BODY), as_of=NOW)
    assert [asset.raw_payload for asset in outcome.assets] == [AAPL_RAW, OBK_RAW, GONE_RAW]


@pytest.mark.asyncio
async def test_an_empty_list_is_served_with_no_assets(executor):
    vendor = stub_trading_vendor(raw_answer(b'[]'))

    outcome = await reader(executor, vendor.client).get_assets(list_query())

    assert outcome == AssetListResponse(assets=[], as_of=NOW)


@pytest.mark.asyncio
async def test_a_list_is_paced_by_one_acquire_on_the_trading_scope_and_never_the_data_scope(executor):
    """Acceptance: paced on the trading-API scope. The priority and deadline are the query's own."""
    vendor = stub_trading_vendor(raw_answer(LIST_BODY))
    pacing = RecordingScheduler()

    await reader(executor, vendor.client, scheduler=pacing).get_assets(list_query())

    assert pacing.acquires == [Acquire(ALPACA_TRADING_SCOPE, RequestPriority.BACKFILL, DEADLINE)]


@pytest.mark.asyncio
async def test_a_list_counts_one_call_and_one_page_on_its_tally(executor):
    vendor = stub_trading_vendor(raw_answer(LIST_BODY))
    tally = CallTally()

    before = datetime.now(UTC)
    await reader(executor, vendor.client).get_assets(list_query(tally=tally))
    after = datetime.now(UTC)

    assert (tally.calls, tally.pages, tally.retries, tally.throttled_429) == (1, 1, 0, 0)
    assert before <= tally.first_call_at == tally.last_call_at <= after


@pytest.mark.asyncio
@pytest.mark.parametrize('asset_type', [kind for kind in AssetType if kind is not AssetType.STOCK])
async def test_a_list_of_a_kind_other_than_stock_is_refused_before_any_acquire_or_request(executor, asset_type):
    vendor = stub_trading_vendor()
    pacing = RecordingScheduler()

    outcome = await reader(executor, vendor.client, scheduler=pacing).get_assets(list_query(asset_type=asset_type))

    error = list_failure(outcome)
    assert type(error) is BrokerUnsupportedError
    assert error.reason is Reason.UNSUPPORTED_ASSET_TYPE
    assert vendor.sent == []
    assert pacing.acquires == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'reason'),
    [
        pytest.param(401, Reason.VENDOR_AUTH, id='401'),
        pytest.param(403, Reason.VENDOR_AUTH, id='403'),
        pytest.param(404, Reason.VENDOR_REJECTED, id='404'),
        pytest.param(422, Reason.VENDOR_REJECTED, id='422'),
        pytest.param(500, Reason.VENDOR_UNAVAILABLE, id='500'),
        pytest.param(503, Reason.VENDOR_UNAVAILABLE, id='503'),
    ],
)
async def test_a_list_the_vendor_refuses_is_a_typed_failure_with_its_cause(executor, status, reason):
    """A 404 on the LIST endpoint is not a not-found answer, and never an empty list.

    The list has no symbol to be missing: it is the vendor refusing the request.
    """
    vendor = stub_trading_vendor(status_answer(status))

    outcome = await reader(executor, vendor.client).get_assets(list_query())

    error = list_failure(outcome)
    assert error.reason is reason
    assert isinstance(error.__cause__, APIError)
    assert error.__cause__.status_code == status
    assert len(vendor.sent) == 1, 'a refused list was retried by the reader'


@pytest.mark.asyncio
async def test_a_list_rate_limited_is_returned_with_the_reset_the_trading_host_named_and_counted(executor):
    reset = NOW + timedelta(seconds=42)
    vendor = stub_trading_vendor(status_answer(429, headers={RATE_LIMIT_RESET_HEADER: str(reset.timestamp())}))
    tally = CallTally()

    outcome = await reader(executor, vendor.client).get_assets(list_query(tally=tally))

    error = list_failure(outcome)
    assert error.reason is Reason.VENDOR_RATE_LIMITED
    assert error.reset_at == reset
    assert (tally.calls, tally.pages, tally.throttled_429) == (1, 0, 1)
    assert len(vendor.sent) == 1


@pytest.mark.asyncio
async def test_a_header_less_429_waits_for_the_reset_the_last_trading_answer_named_and_the_data_scope_learns_nothing(
    executor,
):
    """Design input (5): the trading host sends its own rate-limit headers; they are remembered for ITS scope."""
    reset = NOW + timedelta(seconds=37)
    vendor = stub_trading_vendor(
        raw_answer(b'[]', headers={RATE_LIMIT_RESET_HEADER: str(reset.timestamp())}), status_answer(429)
    )
    read = reader(executor, vendor.client)

    await read.get_assets(list_query())
    outcome = await read.get_assets(list_query(AssetListStatus.INACTIVE))

    assert list_failure(outcome).reset_at == reset
    assert broker_api.last_reset(ALPACA_TRADING_SCOPE, NOW) == reset
    assert broker_api.last_reset(ALPACA_DATA_SCOPE, NOW) is None, 'a trading answer moved the data scope reset'


@pytest.mark.asyncio
async def test_a_header_less_429_with_nothing_remembered_waits_the_trading_scopes_declared_window(executor):
    vendor = stub_trading_vendor(status_answer(429))
    pacing = RecordingScheduler(declared_window_seconds=60.0)

    outcome = await reader(executor, vendor.client, scheduler=pacing).get_assets(list_query())

    assert list_failure(outcome).reset_at == NOW + timedelta(seconds=60)
    assert pacing.windows == [ALPACA_TRADING_SCOPE]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'cut',
    [
        pytest.param(requests.exceptions.ConnectionError('down'), id='connection'),
        pytest.param(requests.exceptions.ReadTimeout('slow'), id='timeout'),
        pytest.param(requests.exceptions.ChunkedEncodingError('cut'), id='cut'),
    ],
)
async def test_a_list_the_vendor_cannot_answer_is_vendor_unavailable_counted_as_one_call(executor, cut):
    vendor = stub_trading_vendor(cut)
    tally = CallTally()

    outcome = await reader(executor, vendor.client).get_assets(list_query(tally=tally))

    error = list_failure(outcome)
    assert error.reason is Reason.VENDOR_UNAVAILABLE
    assert error.__cause__ is cut
    assert (tally.calls, tally.pages) == (1, 0)


class Frozen:
    """A monotonic clock that never moves, so a scope's single token never refills."""

    def __call__(self) -> float:
        return 0.0


@pytest.fixture
def scheduler_never_sleeps(monkeypatch) -> None:
    async def never_sleep(seconds: float) -> None:
        raise AssertionError(f'the scheduler slept {seconds}s instead of refusing at once')

    monkeypatch.setattr(scheduler_module, 'asyncio', SimpleNamespace(sleep=never_sleep))


@pytest.mark.asyncio
async def test_a_list_whose_trading_token_misses_the_deadline_is_rate_budget_with_no_request(
    executor, scheduler_never_sleeps
):
    """The trading scope's own budget gates the call: the data scope is full and is never asked."""
    scheduler = real_scheduler(
        {ALPACA_DATA_SCOPE: ONE_PER_SECOND, ALPACA_TRADING_SCOPE: ONE_PER_SECOND},
        clock=lambda: NOW,
        budget_clock=Frozen(),
    )
    vendor = stub_trading_vendor(raw_answer(b'[]'))
    read = reader(executor, vendor.client, scheduler=scheduler)

    first = await read.get_assets(list_query(deadline=NOW + timedelta(milliseconds=500)))
    second = await read.get_assets(list_query(deadline=NOW + timedelta(milliseconds=500)))

    assert isinstance(first, AssetListResponse)
    error = list_failure(second)
    assert error.reason is Reason.RATE_BUDGET
    assert len(vendor.sent) == 1, 'the call was made without a token'


@pytest.mark.asyncio
async def test_without_credentials_a_list_is_vendor_auth_with_no_acquire_and_no_request(executor):
    pacing = RecordingScheduler()

    outcome = await reader(executor, None, scheduler=pacing).get_assets(list_query())

    error = list_failure(outcome)
    assert type(error) is MissingCredentialsError
    assert error.reason is Reason.VENDOR_AUTH
    assert pacing.acquires == []


@pytest.mark.asyncio
async def test_a_reader_built_without_a_trading_scope_raises_rather_than_charge_another(executor):
    vendor = stub_trading_vendor()

    with pytest.raises(RuntimeError, match='trading scope'):
        await reader(executor, vendor.client, trading_scope=None).get_assets(list_query())
    with pytest.raises(RuntimeError, match='trading scope'):
        await reader(executor, vendor.client, trading_scope=None).get_asset(lookup())
    assert vendor.sent == []


@pytest.mark.asyncio
async def test_a_list_body_that_is_not_an_array_raises_out_of_the_read(executor):
    vendor = stub_trading_vendor(raw_answer(b'{"message": "not a list"}'))

    with pytest.raises(ValueError, match='not a JSON array'):
        await reader(executor, vendor.client).get_assets(list_query())


# ---------------------------------------------------------------------------------------------
# get_asset: one call by symbol, a 404 is an answer, on the trading scope, counted nowhere
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lookup_is_one_get_by_symbol_with_no_parameters_and_keeps_the_body_raw(executor):
    vendor = stub_trading_vendor(raw_answer(AAPL_RAW))

    outcome = await reader(executor, vendor.client).get_asset(lookup('AAPL'))

    [sent] = vendor.sent
    assert sent.method == 'GET'
    assert (sent.host, sent.path) == (ALPACA_TRADING_TEST_HOST, f'{ASSETS_PATH}/AAPL')
    assert sent.params == {}
    assert outcome == AssetResponse(asset=parse_asset(AAPL_RAW), as_of=NOW)
    assert outcome.asset.raw_payload == AAPL_RAW
    assert outcome.asset.vendor_asset_id == AAPL_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('symbol', 'path'),
    [
        pytest.param('BRK.B', f'{ASSETS_PATH}/BRK.B', id='a-dot-stays'),
        pytest.param('BRK/B', f'{ASSETS_PATH}/BRK%2FB', id='a-slash-is-escaped'),
        pytest.param('A B', f'{ASSETS_PATH}/A%20B', id='a-space-is-escaped'),
    ],
)
async def test_the_symbol_is_one_escaped_path_segment(executor, symbol, path):
    vendor = stub_trading_vendor(raw_answer(AAPL_RAW))

    await reader(executor, vendor.client).get_asset(lookup(symbol))

    assert [sent.path for sent in vendor.sent] == [path]


@pytest.mark.asyncio
async def test_a_lookup_judges_nothing_an_inactive_untradable_record_is_answered_like_any_other(executor):
    vendor = stub_trading_vendor(raw_answer(GONE_RAW))

    outcome = await reader(executor, vendor.client).get_asset(lookup('GONE'))

    assert isinstance(outcome, AssetResponse)
    assert (outcome.asset.active, outcome.asset.tradable, outcome.asset.vendor_asset_id) == (False, False, GONE_ID)


@pytest.mark.asyncio
async def test_a_lookup_answers_the_vendors_own_symbol_even_when_it_differs_from_the_one_asked(executor):
    vendor = stub_trading_vendor(raw_answer(AAPL_RAW))

    outcome = await reader(executor, vendor.client).get_asset(lookup('aapl'))

    assert outcome.asset.symbol == 'AAPL'


@pytest.mark.asyncio
async def test_a_vendor_404_on_a_lookup_is_the_typed_not_found_answer_not_a_failure(executor):
    """21:54 and 20:29 UTC notes: the 404 is its own typed outcome; P12h reads it as the id having gone."""
    vendor = stub_trading_vendor(status_answer(404, 'asset not found for GONE'))

    outcome = await reader(executor, vendor.client).get_asset(lookup('GONE'))

    assert outcome == AssetNotFound(symbol='GONE', as_of=NOW)


@pytest.mark.asyncio
async def test_a_lookup_is_paced_by_one_acquire_on_the_trading_scope_under_its_priority_and_deadline(executor):
    vendor = stub_trading_vendor(raw_answer(AAPL_RAW), status_answer(404))
    pacing = RecordingScheduler()
    read = reader(executor, vendor.client, scheduler=pacing)

    await read.get_asset(lookup('AAPL'))
    await read.get_asset(lookup('GONE'))

    assert pacing.acquires == [Acquire(ALPACA_TRADING_SCOPE, RequestPriority.INTERACTIVE, DEADLINE)] * 2


@pytest.mark.asyncio
async def test_a_lookup_rate_limited_is_a_failure_carrying_the_reset(executor):
    reset = NOW + timedelta(seconds=19)
    vendor = stub_trading_vendor(status_answer(429, headers={RATE_LIMIT_RESET_HEADER: str(reset.timestamp())}))

    outcome = await reader(executor, vendor.client).get_asset(lookup())

    error = lookup_failure(outcome)
    assert error.reason is Reason.VENDOR_RATE_LIMITED
    assert error.reset_at == reset


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('answer', 'reason'),
    [
        pytest.param(status_answer(503), Reason.VENDOR_UNAVAILABLE, id='503'),
        pytest.param(requests.exceptions.ConnectionError('down'), Reason.VENDOR_UNAVAILABLE, id='connection'),
        pytest.param(status_answer(403), Reason.VENDOR_AUTH, id='403'),
        pytest.param(status_answer(422), Reason.VENDOR_REJECTED, id='422'),
    ],
)
async def test_a_lookup_that_is_neither_found_nor_not_found_is_a_typed_failure(executor, answer, reason):
    vendor = stub_trading_vendor(answer)

    outcome = await reader(executor, vendor.client).get_asset(lookup())

    assert lookup_failure(outcome).reason is reason


@pytest.mark.asyncio
@pytest.mark.parametrize('symbol', ['', '   '], ids=['empty', 'blank'])
async def test_a_lookup_with_no_symbol_is_refused_before_any_acquire_or_request(executor, symbol):
    vendor = stub_trading_vendor()
    pacing = RecordingScheduler()

    outcome = await reader(executor, vendor.client, scheduler=pacing).get_asset(lookup(symbol))

    error = lookup_failure(outcome)
    assert type(error) is InvalidRequestError
    assert error.reason is Reason.INVALID_REQUEST
    assert vendor.sent == []
    assert pacing.acquires == []


@pytest.mark.asyncio
async def test_without_credentials_a_lookup_is_vendor_auth(executor):
    outcome = await reader(executor, None).get_asset(lookup())

    assert type(lookup_failure(outcome)) is MissingCredentialsError


# ---------------------------------------------------------------------------------------------
# broker_api: the trading client is configuration, and request_raw makes one GET and keeps the bytes
# ---------------------------------------------------------------------------------------------


def test_the_trading_client_is_never_built_without_a_configured_host(monkeypatch):
    """Design input (6): the trading host is configuration, never a hard-coded paper or live host."""
    monkeypatch.setenv('ALPACA_API_KEY', PLACEHOLDER_KEY_ID)
    monkeypatch.setenv('ALPACA_API_SECRET', PLACEHOLDER_SECRET)

    with pytest.raises(MissingCredentialsError, match=broker_api.ALPACA_TRADING_HOST_VAR):
        broker_api.get_trading_client()


def test_the_trading_client_is_never_built_without_credentials(monkeypatch):
    monkeypatch.setenv(broker_api.ALPACA_TRADING_HOST_VAR, ALPACA_TRADING_TEST_URL)

    with pytest.raises(MissingCredentialsError):
        broker_api.get_trading_client()


def test_the_trading_client_is_built_once_on_the_configured_host_and_cleared_by_the_seam(monkeypatch):
    monkeypatch.setenv('ALPACA_API_KEY', PLACEHOLDER_KEY_ID)
    monkeypatch.setenv('ALPACA_API_SECRET', PLACEHOLDER_SECRET)
    monkeypatch.setenv(broker_api.ALPACA_TRADING_HOST_VAR, ALPACA_TRADING_TEST_URL + '/')

    client = broker_api.get_trading_client()

    assert client._base_url == ALPACA_TRADING_TEST_URL, 'the trailing slash would double the path separator'
    assert broker_api.get_trading_client() is client
    broker_api.set_trading_client(None)
    assert broker_api.get_trading_client() is not client


def test_request_raw_makes_one_get_and_keeps_the_body_bytes_and_the_headers():
    vendor = stub_trading_vendor(raw_answer(LIST_BODY, headers={RATE_LIMIT_RESET_HEADER: '123'}))

    page = broker_api.request_raw(vendor.client, broker_api.ASSETS_PATH, {'status': 'active'})

    [sent] = vendor.sent
    assert (sent.method, sent.path, sent.params) == ('GET', ASSETS_PATH, {'status': 'active'})
    assert page.content == LIST_BODY
    assert page.headers[RATE_LIMIT_RESET_HEADER] == '123'


@pytest.mark.parametrize('status', [404, 429, 503])
def test_request_raw_raises_the_sdks_api_error_carrying_the_response_and_never_retries(status):
    vendor = stub_trading_vendor(status_answer(status, 'no', headers={RATE_LIMIT_RESET_HEADER: '9'}))

    with pytest.raises(APIError) as raised:
        broker_api.request_raw(vendor.client, broker_api.ASSETS_PATH)

    assert raised.value.status_code == status
    assert raised.value.response.headers[RATE_LIMIT_RESET_HEADER] == '9'
    assert len(vendor.sent) == 1
