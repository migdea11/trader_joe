"""AlpacaRead against recorded vendor responses: paging, retries, offsets, empty, range, refusals.

Decision tj-j4wknb R3: the vendor parsing and paging are covered by in-process unit tests on the
REAL alpaca-py client (StockHistoricalDataClient inside AlpacaRead), answering its HTTP from
recorded bodies. Nothing here reaches the network: the harness (alpaca_recorded) mounts a requests
adapter on the client's session for every scheme and fails the test on anything it does not know.

The wire contract every assertion is written against is research tj-vhboky.57 (its FINAL SUMMARY
and the architect's 16:44 UTC review): limit=10000 on every request, page_token only from page 2,
4 attempts with 3 flat 3 s sleeps on 429 and 504, start and end sent with the query's own offset,
errors as APIError(status_code, message). The fixtures were RECORDED from Alpaca at the host
sitting (tj-irhy0a.3), except those that cannot be provoked; see fixtures/alpaca/README.md.

The range rule is decision tj-j4wknb ADDENDUM 5 (ruling on tj-irhy0a.15): get_bars serves
[start, end), half-open, on instants. Alpaca's end is inclusive, so AlpacaRead drops a bar stamped
exactly at end.

FAILURE CONTRACT: until PR 2, get_bars RAISES on a vendor failure and yields nothing of that fetch
(tj-j4wknb addendum 2 B, addendum 4 item 11). The tests assert the SDK's APIError propagating; PR
2's typed errors (tj-fa1rpu) replace it and tighten these tests.
"""

import copy
import math
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from itertools import pairwise

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from common.enums.data_select import AssetType
from common.enums.data_stock import Feed, Granularity
from data.ingest.app.brokers.alpaca.broker_codes import AlpacaGranularity
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.interface import Bar, BarsQuery, BarsResponse, BrokerUnsupportedError, Instrument
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.tests.alpaca_recorded import (
    ALPACA_DATA_HOST,
    BARS_PATH,
    RecordedResponse,
    always,
    by_page_token,
    in_order,
    load,
    paged,
    parse_rfc3339,
    record_sdk_sleeps,
    recorded_client,
)


pytestmark = pytest.mark.data_ingest

SYMBOL = 'AAPL'
MINUS_0500 = timezone(timedelta(hours=-5))
PLUS_0930 = timezone(timedelta(hours=9, minutes=30))
OFFSETS = pytest.mark.parametrize('offset', [UTC, MINUS_0500, PLUS_0930], ids=['Z', '-05:00', '+09:30'])

# The timeframe spelling on the wire for each platform granularity (tj-vhboky.57 item 5).
WIRE_TIMEFRAME = {
    Granularity.ONE_MINUTE: '1Min',
    Granularity.FIVE_MINUTES: '5Min',
    Granularity.THIRTY_MINUTES: '30Min',
    Granularity.ONE_HOUR: '1Hour',
    Granularity.ONE_DAY: '1Day',
    Granularity.ONE_WEEK: '1Week',
    Granularity.ONE_MONTH: '1Month',
}

# alpaca-py 0.44.0's retry facts (tj-vhboky.54 16:44 UTC addendum; tj-vhboky.57 item 6).
SDK_ATTEMPTS = 4
SDK_SLEEPS = [3, 3, 3]


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        yield pool


@pytest.fixture(autouse=True)
def iex_deployment(monkeypatch):
    """Pin the deployment's tape, so the feed asserted on the wire is not read from the host."""
    monkeypatch.delenv('ALPACA_SIP_ENABLED', raising=False)


def build_query(
    start: datetime,
    end: datetime | None,
    granularity: Granularity = Granularity.ONE_DAY,
    asset_type: AssetType = AssetType.STOCK,
    exchange: str | None = None,
    currency: str | None = None,
    adjustment: str = 'raw',
) -> BarsQuery:
    return BarsQuery(
        instrument=Instrument(SYMBOL, asset_type, exchange=exchange, currency=currency),
        granularity=granularity,
        start=start,
        end=end,
        adjustment=adjustment,
        priority=RequestPriority.BACKFILL,
    )


def build_reader(client, executor: ThreadPoolExecutor) -> AlpacaRead:
    return AlpacaRead(client=client, executor_provider=lambda: executor)


async def drain(response: BarsResponse) -> list[Bar]:
    return [bar async for bar in response.bars]


def stamps(bars: list[Bar]) -> list[datetime]:
    return [bar.timestamp for bar in bars]


# --------------------------------------------------------------------------------------------
# The harness itself: nothing can reach a socket.


def test_every_scheme_is_answered_by_the_recorded_transport_and_no_other_host_is_served():
    client, transport = recorded_client(always(load('bars_1Day')))

    for url in ('https://data.alpaca.markets/v2/stocks/bars', 'http://example.com/', 'https://example.com/'):
        assert client._session.get_adapter(url) is transport
    with pytest.raises(AssertionError, match='left the recorded vendor host'):
        client._session.get('https://example.com/')


# --------------------------------------------------------------------------------------------
# Paging (tj-vhboky.57 item 4 and the architect's correction: limit=10000 on every request).


@pytest.mark.asyncio
async def test_a_two_page_range_reassembles_with_no_gap_and_no_duplicate(executor):
    page1, page2 = load('bars_1Day_page1'), load('bars_1Day_page2')
    client, transport = recorded_client(paged(page1, page2))
    query = build_query(page1.timestamps[0], page2.timestamps[-1] + Granularity.ONE_DAY.offset)

    bars = await drain(await build_reader(client, executor).get_bars(query))

    assert stamps(bars) == page1.timestamps + page2.timestamps
    assert len(set(stamps(bars))) == len(bars)
    assert len(transport.sent) == 2
    # Every field survives the SDK's parse and the adapter's conversion, not only the timestamp.
    first = page1.bars[0]
    assert bars[0] == Bar(
        timestamp=parse_rfc3339(first['t']),
        open=first['o'],
        high=first['h'],
        low=first['l'],
        close=first['c'],
        volume=first['v'],
        trade_count=first['n'],
        vwap=first['vw'],
    )


@pytest.mark.asyncio
async def test_request_one_carries_limit_and_no_token_and_request_two_carries_the_token(executor):
    page1, page2 = load('bars_1Day_page1'), load('bars_1Day_page2')
    client, transport = recorded_client(paged(page1, page2))
    query = build_query(page1.timestamps[0], page2.timestamps[-1] + Granularity.ONE_DAY.offset)

    await drain(await build_reader(client, executor).get_bars(query))

    first, second = transport.sent
    for sent in (first, second):
        assert sent.method == 'GET'
        assert (sent.host, sent.path) == (ALPACA_DATA_HOST, BARS_PATH)
        assert sent.params['symbols'] == SYMBOL
        assert sent.params['timeframe'] == '1Day'
        assert sent.params['feed'] == 'iex'
        assert sent.params['limit'] == '10000'
        # Omitted or 'raw': Alpaca's default adjustment is raw, and raw is all we store.
        assert sent.params.get('adjustment', 'raw') == 'raw'
    assert 'page_token' not in first.params
    assert second.params['page_token'] == page1.next_page_token


# --------------------------------------------------------------------------------------------
# Every granularity the adapter maps parses to aware UTC, ascending, within bounds.


@pytest.mark.asyncio
@pytest.mark.parametrize('alpaca_granularity', list(AlpacaGranularity), ids=lambda g: g.name)
async def test_every_mapped_granularity_parses_to_aware_utc_ascending_bars_within_bounds(executor, alpaca_granularity):
    granularity = alpaca_granularity.granularity
    # Written out, not read back off the enum, so a wrong mapping cannot agree with itself. A
    # granularity mapped later without an entry here fails with KeyError until it is added.
    wire_timeframe = WIRE_TIMEFRAME[granularity]
    recorded = load(f'bars_{wire_timeframe}')
    client, transport = recorded_client(always(recorded))
    query = build_query(recorded.timestamps[0], recorded.timestamps[-1] + granularity.offset, granularity)

    bars = await drain(await build_reader(client, executor).get_bars(query))

    assert transport.sent[0].params['timeframe'] == wire_timeframe
    assert stamps(bars) == recorded.timestamps
    for bar in bars:
        assert bar.timestamp.tzinfo is not None
        assert bar.timestamp.utcoffset() == timedelta(0)
        assert query.start <= bar.timestamp < query.end
    assert all(earlier < later for earlier, later in pairwise(stamps(bars)))


# --------------------------------------------------------------------------------------------
# Non-UTC offsets serve the same instants as the Z form (tj-vhboky.59 16:44 UTC note, item 3).


@pytest.mark.asyncio
@pytest.mark.parametrize('offset', [MINUS_0500, PLUS_0930], ids=['-05:00', '+09:30'])
async def test_bounds_with_a_non_utc_offset_serve_the_same_instants_as_the_z_form(executor, offset):
    page1, page2 = load('bars_1Day_page1'), load('bars_1Day_page2')
    z_query = build_query(page1.timestamps[0], page2.timestamps[-1] + Granularity.ONE_DAY.offset)
    offset_query = build_query(z_query.start.astimezone(offset), z_query.end.astimezone(offset))

    z_client, z_transport = recorded_client(paged(page1, page2))
    z_bars = await drain(await build_reader(z_client, executor).get_bars(z_query))
    offset_client, offset_transport = recorded_client(paged(page1, page2))
    offset_bars = await drain(await build_reader(offset_client, executor).get_bars(offset_query))

    assert stamps(offset_bars) == stamps(z_bars) == page1.timestamps + page2.timestamps
    for sent in offset_transport.sent:
        assert parse_rfc3339(sent.params['start']) == z_query.start
        assert parse_rfc3339(sent.params['end']) == z_query.end
    assert len(offset_transport.sent) == len(z_transport.sent) == 2


# --------------------------------------------------------------------------------------------
# The range is half-open [start, end) on instants (tj-j4wknb ADDENDUM 5, tj-irhy0a.15).


@pytest.mark.asyncio
@OFFSETS
async def test_a_recorded_bar_at_start_is_served_and_one_exactly_at_end_is_dropped(executor, offset):
    recorded = load('bars_range_boundary')
    at_start, *_, at_end = recorded.timestamps
    client, transport = recorded_client(always(recorded))
    query = build_query(at_start.astimezone(offset), at_end.astimezone(offset), Granularity.ONE_HOUR)

    bars = await drain(await build_reader(client, executor).get_bars(query))

    # The vendor answered with both boundary bars (its end is inclusive); the adapter kept one.
    assert at_start in stamps(bars)
    assert at_end not in stamps(bars)
    assert stamps(bars) == recorded.timestamps[:-1]
    assert len(transport.sent) == 1


@pytest.mark.asyncio
async def test_an_open_ended_query_sends_no_end_and_serves_every_recorded_bar(executor):
    recorded = load('bars_range_boundary')
    client, transport = recorded_client(always(recorded))
    query = build_query(recorded.timestamps[0], None, Granularity.ONE_HOUR)

    bars = await drain(await build_reader(client, executor).get_bars(query))

    assert stamps(bars) == recorded.timestamps
    assert 'end' not in transport.sent[0].params


# --------------------------------------------------------------------------------------------
# EMPTY: both body shapes give zero bars (tj-vhboky.57 item 2 ruling: assert the outcome).


@pytest.mark.asyncio
@pytest.mark.parametrize('fixture', ['bars_empty_absent_symbol', 'bars_empty_list'], ids=['absent-key', 'empty-list'])
async def test_both_empty_body_shapes_give_a_response_with_zero_bars(executor, fixture):
    client, transport = recorded_client(always(load(fixture)))
    start = datetime(2022, 1, 8, 5, tzinfo=UTC)

    response = await build_reader(client, executor).get_bars(build_query(start, start + timedelta(days=2)))

    assert isinstance(response, BarsResponse)
    assert response.feed is Feed.IEX
    assert await drain(response) == []
    assert len(transport.sent) == 1


# --------------------------------------------------------------------------------------------
# trade_count is the int Bar declares (tj-j4wknb addendum 2 B, tj-irhy0a.17). alpaca-py parses it
# as a float, and Bar's dataclass equality cannot tell 772630 from 772630.0, so the type is
# asserted exactly.

# Every fixture that serves bars, each as the pages of one fetch.
SERVED_FIXTURES = [
    pytest.param(('bars_1Day',), id='1Day'),
    pytest.param(('bars_1Day_page1', 'bars_1Day_page2'), id='1Day-two-pages'),
    pytest.param(('bars_1Min',), id='1Min'),
    pytest.param(('bars_5Min',), id='5Min'),
    pytest.param(('bars_30Min',), id='30Min'),
    pytest.param(('bars_1Hour',), id='1Hour'),
    pytest.param(('bars_1Week',), id='1Week'),
    pytest.param(('bars_1Month',), id='1Month'),
    pytest.param(('bars_range_boundary',), id='range-boundary'),
]


def with_second_trade_count(recorded: RecordedResponse, trade_count: float) -> RecordedResponse:
    """The same response with the second bar's n replaced, for values a JSON fixture cannot hold."""
    body = copy.deepcopy(recorded.body)
    body['bars'][SYMBOL][1]['n'] = trade_count
    return RecordedResponse(name=f'{recorded.name}[n={trade_count!r}]', status=recorded.status, body=body)


@pytest.mark.asyncio
@pytest.mark.parametrize('names', SERVED_FIXTURES)
async def test_every_served_bar_carries_its_trade_count_as_exactly_int(executor, names):
    pages = [load(name) for name in names]
    client, _transport = recorded_client(paged(*pages))
    query = build_query(pages[0].timestamps[0], None)

    bars = await drain(await build_reader(client, executor).get_bars(query))

    assert [bar.trade_count for bar in bars] == [bar['n'] for page in pages for bar in page.bars]
    assert bars
    for bar in bars:
        assert type(bar.trade_count) is int, f'{bar.timestamp}: {bar.trade_count!r}'


@pytest.mark.asyncio
# None keeps the fixture's own 831423.5; NaN and infinity are not JSON, so they are swapped in.
@pytest.mark.parametrize('replacement', [None, math.nan, math.inf], ids=['fixture-831423.5', 'nan', 'inf'])
async def test_a_non_whole_trade_count_fails_get_bars_naming_the_symbol_and_instant(executor, replacement):
    recorded = load('bars_1Day_fractional_trade_count')
    if replacement is not None:
        recorded = with_second_trade_count(recorded, replacement)
    client, _transport = recorded_client(always(recorded))
    query = build_query(recorded.timestamps[0], None)
    corrupt_bar = recorded.timestamps[1]
    reader = build_reader(client, executor)

    # Raised by get_bars itself: the fetch fails whole, never truncated and never part-served.
    with pytest.raises(ValueError, match='trade_count') as raised:
        await reader.get_bars(query)

    assert SYMBOL in str(raised.value)
    assert corrupt_bar.isoformat() in str(raised.value)


@pytest.mark.asyncio
async def test_a_null_trade_count_is_served_as_none(executor):
    recorded = load('bars_1Day_null_trade_count')
    client, _transport = recorded_client(always(recorded))
    query = build_query(recorded.timestamps[0], None)

    bars = await drain(await build_reader(client, executor).get_bars(query))

    assert stamps(bars) == recorded.timestamps
    assert [bar.trade_count for bar in bars] == [772630, None, 846121]
    assert [type(bar.trade_count) for bar in bars] == [int, type(None), int]


# --------------------------------------------------------------------------------------------
# Retries: 4 attempts, 3 flat 3 s sleeps, codes 429 and 504, then get_bars raises.


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [429, 504])
async def test_a_retried_status_on_every_attempt_makes_four_attempts_then_get_bars_raises(
    executor, monkeypatch, status
):
    sleeps = record_sdk_sleeps(monkeypatch)
    client, transport = recorded_client(always(load(f'error_{status}')))
    recorded = load('bars_1Day')
    query = build_query(recorded.timestamps[0], recorded.timestamps[-1] + Granularity.ONE_DAY.offset)

    with pytest.raises(APIError) as raised:
        await build_reader(client, executor).get_bars(query)

    assert raised.value.status_code == status
    assert len(transport.sent) == SDK_ATTEMPTS
    assert sleeps == SDK_SLEEPS


@pytest.mark.asyncio
async def test_one_429_then_a_page_is_served_after_one_retry(executor, monkeypatch):
    sleeps = record_sdk_sleeps(monkeypatch)
    recorded = load('bars_1Day')
    client, transport = recorded_client(in_order([load('error_429'), recorded]))
    query = build_query(recorded.timestamps[0], recorded.timestamps[-1] + Granularity.ONE_DAY.offset)

    bars = await drain(await build_reader(client, executor).get_bars(query))

    assert stamps(bars) == recorded.timestamps
    assert len(transport.sent) == 2
    assert sleeps == SDK_SLEEPS[:1]


# --------------------------------------------------------------------------------------------
# A page-2 failure yields nothing of that fetch (tj-j4wknb addendum 2 B).


@pytest.mark.asyncio
async def test_page_two_answering_500_raises_and_yields_no_bar_from_page_one(executor, monkeypatch):
    sleeps = record_sdk_sleeps(monkeypatch)
    page1, page2 = load('bars_1Day_page1'), load('bars_1Day_page2')
    client, transport = recorded_client(by_page_token({None: page1, page1.next_page_token: load('error_500')}))
    query = build_query(page1.timestamps[0], page2.timestamps[-1] + Granularity.ONE_DAY.offset)
    reader = build_reader(client, executor)

    # get_bars itself raises, so no BarsResponse -- and no bar of page 1 -- ever reaches the
    # caller. An error deferred into iteration would hand back a response first, and fail here.
    with pytest.raises(APIError) as raised:
        await reader.get_bars(query)

    assert raised.value.status_code == 500
    # Page 1 was answered and page 2 was asked for, once: 500 is not a retried status.
    assert [sent.params.get('page_token') for sent in transport.sent] == [None, page1.next_page_token]
    assert sleeps == []


# --------------------------------------------------------------------------------------------
# An error body surfaces as APIError with status_code and a readable message, at the SDK layer.


def test_an_error_body_surfaces_from_the_sdk_as_api_error_with_status_and_message():
    recorded = load('error_400')
    client, transport = recorded_client(always(recorded))
    start = datetime(2022, 1, 3, 5, tzinfo=UTC)

    with pytest.raises(APIError) as raised:
        client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=SYMBOL, timeframe=TimeFrame.Day, start=start, end=start + timedelta(days=1)
            )
        )

    assert raised.value.status_code == 400
    assert isinstance(raised.value.message, str)
    assert raised.value.message
    assert len(transport.sent) == 1


@pytest.mark.asyncio
async def test_the_sdk_error_reaches_the_reader_caller_unchanged(executor):
    client, _transport = recorded_client(always(load('error_400')))
    start = datetime(2022, 1, 3, 5, tzinfo=UTC)

    with pytest.raises(APIError) as raised:
        await build_reader(client, executor).get_bars(build_query(start, start + timedelta(days=1)))

    assert raised.value.status_code == 400
    assert raised.value.message


# --------------------------------------------------------------------------------------------
# Refusals happen before any HTTP request (tj-j4wknb addendum 4 items 3 and 5).

REFUSED = [
    pytest.param({'asset_type': AssetType.CRYPTO}, id='asset_type-crypto'),
    pytest.param({'asset_type': AssetType.OPTION}, id='asset_type-option'),
    pytest.param({'adjustment': 'split'}, id='adjustment-split'),
    pytest.param({'adjustment': 'all'}, id='adjustment-all'),
    pytest.param({'exchange': 'XNAS'}, id='exchange-xnas'),
    pytest.param({'currency': 'CAD'}, id='currency-cad'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('refused', REFUSED)
async def test_what_alpaca_cannot_serve_is_refused_with_zero_http_requests(executor, refused):
    recorded = load('bars_1Day')
    client, transport = recorded_client(always(recorded))
    query = build_query(recorded.timestamps[0], recorded.timestamps[-1] + Granularity.ONE_DAY.offset, **refused)

    with pytest.raises(BrokerUnsupportedError):
        await build_reader(client, executor).get_bars(query)

    assert transport.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize('currency', [None, 'USD'])
async def test_a_servable_query_reaches_the_transport_so_the_zero_above_is_not_vacuous(executor, currency):
    recorded = load('bars_1Day')
    client, transport = recorded_client(always(recorded))
    query = build_query(recorded.timestamps[0], recorded.timestamps[-1] + Granularity.ONE_DAY.offset, currency=currency)

    bars = await drain(await build_reader(client, executor).get_bars(query))

    assert stamps(bars) == recorded.timestamps
    assert len(transport.sent) == 1
