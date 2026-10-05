"""Boundary (a) returns the typed result: AlpacaRead's outcomes and the types they are built from (TE-4).

TE-4 tj-3mk3u5.37.5, gated against ADR tj-fa1rpu D1(a), D2, D3 (note a; note b as amended by the
2026-10-02 Q-EMPTY addendum), D5, D6, D8 (its 16:22 UTC addendum item 5) and U4 = A; decision
tj-j4wknb addendum 2 B and addendum 4 items 5 and 8; the TE-1 gate rulings on tj-3mk3u5.37.3.

WHAT THIS FILE PINS, at AlpacaRead and the interface, with a stub vendor client:
* the pre-vendor refusals (a named feed this deployment cannot serve, a range starting at or after
  the reader's clock) are RETURNED before any vendor call AND before any rate token;
* a named feed this deployment can serve is honoured, all the way to the vendor's feed parameter;
* a missing credential is RETURNED, as VENDOR_AUTH, before any vendor call;
* SERVED carries served_range and as_of from the reader's injectable clock, with the end clamped to
  as_of (an open end IS as_of), and a range ending in the future is served, not refused;
* only the named exceptions are converted (D6): a vendor failure keeps its exception as __cause__, the
  rate budget's RATE_BUDGET comes back as a failure with no cause, and a bug still raises (D5);
* the re-parented errors and the new result types;
* the crypto and option stubs and create_app's problem+json handlers;
* (TE-4 follow-up tj-3mk3u5.37.14) a connection cut mid-body (ChunkedEncodingError) is VENDOR_UNAVAILABLE
  with the caught exception as its cause, while requests' other, non-transport exceptions still escape
  as bugs; and match_client_request raises UNSUPPORTED_ASSET_TYPE for any triple it does not map. The
  cut on a real socket, through alpaca-py's own session, is in test_alpaca_cut_connection.py.

The recorded-vendor half (the real alpaca-py client, its retries, the recorded bodies, served-empty,
the bars:null pin) is in test_alpaca_read_recorded.py; the classifier's own table in
test_alpaca_classify.py; the rate budget's deadline in test_rate_budget.py; the Kafka edge's {} in
test_read_seam.py.
"""

import dataclasses
import inspect
import itertools
import os
import typing
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestBarRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
    StockQuotesRequest,
    StockTradesRequest,
)
from fastapi.testclient import TestClient

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity
from common.errors.vocabulary import REASONS, ExogenousError, InvalidRequestError, Outcome, Reason, TraderJoeError
from data.ingest.app import main
from data.ingest.app.brokers import rate_budget as rate_budget_module
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.broker_errors import MissingCredentialsError
from data.ingest.app.brokers.interface import (
    BarsFailure,
    BarsQuery,
    BarsResponse,
    BrokerRead,
    BrokerUnsupportedError,
    Instrument,
    ServedRange,
)
from data.ingest.app.brokers.rate_budget import RateBudget, RequestPriority
from routers.common.errors import PROBLEM_RESPONSES


pytestmark = pytest.mark.data_ingest

# The reader's clock, fixed.
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
END = START + timedelta(hours=1)
PLUS_FIVE = timezone(timedelta(hours=5))
MICROSECOND = timedelta(microseconds=1)

# The three reasons a BrokerUnsupportedError may carry (TE-4 item 3).
UNSUPPORTED_REASONS = {Reason.UNSUPPORTED_INSTRUMENT, Reason.UNSUPPORTED_ASSET_TYPE, Reason.FEED_NOT_AVAILABLE}


# ---------------------------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------------------------


def vendor_bar(timestamp: datetime) -> SimpleNamespace:
    """One alpaca-py bar, with every attribute AlpacaRead reads."""
    return SimpleNamespace(
        open=1.0, high=2.0, low=0.5, close=10.0, volume=10, trade_count=3, vwap=1.5, timestamp=timestamp
    )


class StubBarSet:
    """The slice of alpaca-py's BarSet that AlpacaRead's conversion touches."""

    def __init__(self, bars_by_symbol: dict[str, list[SimpleNamespace]]):
        self.data = bars_by_symbol

    def __getitem__(self, symbol: str) -> list[SimpleNamespace]:
        return self.data[symbol]


def stub_client(*timestamps: datetime, symbol: str = 'VFV') -> Mock:
    client = Mock()
    client.get_stock_bars.return_value = StubBarSet({symbol: [vendor_bar(t) for t in timestamps]})
    return client


def failing_client(error: BaseException) -> Mock:
    client = Mock()
    client.get_stock_bars.side_effect = error
    return client


class CountingBudget:
    """A rate budget that grants every token and records what each acquire was asked."""

    full_refill_seconds = 1.0

    def __init__(self) -> None:
        self.acquires: list[tuple[RequestPriority, datetime | None]] = []

    async def acquire(self, priority: RequestPriority = RequestPriority.INTERACTIVE, deadline=None) -> None:
        self.acquires.append((priority, deadline))


def api_error(status: int, message: str) -> APIError:
    """An APIError as alpaca-py builds one: a real response, raise_for_status, APIError(text, http_error)."""
    response = requests.Response()
    response.status_code = status
    response._content = f'{{"message": "{message}"}}'.encode()
    response.encoding = 'utf-8'
    try:
        response.raise_for_status()
    except requests.HTTPError as http_error:
        return APIError(response.text, http_error)
    raise AssertionError(f'{status} is not an error status')


@pytest.fixture(autouse=True)
def iex_deployment_and_no_cached_client(monkeypatch):
    """An IEX deployment unless a test says otherwise, and no client cached across tests."""
    monkeypatch.delenv('ALPACA_SIP_ENABLED', raising=False)
    broker_api.set_client(None)
    yield
    broker_api.set_client(None)


@pytest.fixture
def budget(monkeypatch) -> CountingBudget:
    """Stand in for Alpaca's rate budget, so a token taken (or not) is observable."""
    counting = CountingBudget()
    monkeypatch.setattr(broker_api, '__RATE_BUDGET', counting)
    return counting


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=1) as pool:
        yield pool


def reader(client: Mock | None, executor: ThreadPoolExecutor, clock=lambda: NOW) -> AlpacaRead:
    return AlpacaRead(client=client, executor_provider=lambda: executor, clock=clock)


def query(**overrides) -> BarsQuery:
    fields = {
        'instrument': Instrument('VFV', AssetType.STOCK),
        'granularity': Granularity.THIRTY_MINUTES,
        'start': START,
        'end': END,
        'priority': RequestPriority.INTERACTIVE,
    }
    return BarsQuery(**{**fields, **overrides})


def failure_of(outcome: BarsResponse | BarsFailure) -> TraderJoeError:
    assert isinstance(outcome, BarsFailure), f'expected a BarsFailure, got {outcome!r}'
    return outcome.error


# ---------------------------------------------------------------------------------------------
# The named feed (TE-4 item 2): honoured when this deployment serves it, else refused before the vendor
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('sip_enabled', 'named'),
    [
        pytest.param('false', Feed.SIP, id='sip-on-an-iex-deployment'),
        pytest.param('true', Feed.IEX, id='iex-on-a-sip-deployment'),
        pytest.param('false', Feed.NOT_APPLICABLE, id='not-applicable'),
    ],
)
async def test_a_named_feed_the_deployment_cannot_serve_is_refused_before_any_call_or_token(
    monkeypatch, executor, budget, sip_enabled, named
):
    monkeypatch.setenv('ALPACA_SIP_ENABLED', sip_enabled)
    client = stub_client(START)

    error = failure_of(await reader(client, executor).get_bars(query(feed=named)))

    assert type(error) is BrokerUnsupportedError
    assert isinstance(error, InvalidRequestError)
    assert error.reason is Reason.FEED_NOT_AVAILABLE
    assert REASONS[error.reason].outcome is Outcome.REFUSED
    assert named.value in error.detail
    assert dict(error.metadata) == {'feed': named.value}
    assert error.__cause__ is None
    assert client.mock_calls == [], 'the vendor was called before the feed refusal'
    assert budget.acquires == [], 'a rate token was spent before the feed refusal'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('sip_enabled', 'named'), [pytest.param('false', Feed.IEX, id='iex'), pytest.param('true', Feed.SIP, id='sip')]
)
async def test_a_named_feed_the_deployment_serves_is_honoured_up_to_the_vendors_feed_parameter(
    monkeypatch, executor, budget, sip_enabled, named
):
    monkeypatch.setenv('ALPACA_SIP_ENABLED', sip_enabled)
    client = stub_client(START)

    response = await reader(client, executor).get_bars(query(feed=named))

    assert isinstance(response, BarsResponse)
    assert response.feed is named
    assert client.get_stock_bars.call_args.args[0].feed == DataFeed(named.value.lower())
    assert len(budget.acquires) == 1


@pytest.mark.asyncio
async def test_a_feed_refusal_comes_before_the_credential_is_resolved(executor, budget):
    # No client injected and no credentials: the feed refusal still wins over VENDOR_AUTH.
    with patch.dict(os.environ, {'ALPACA_SIP_ENABLED': 'false'}, clear=True):
        error = failure_of(await reader(None, executor).get_bars(query(feed=Feed.SIP)))

    assert error.reason is Reason.FEED_NOT_AVAILABLE


# ---------------------------------------------------------------------------------------------
# The future-range pre-check (TE-4 item 4a): on the reader's clock, before any call or token
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'start',
    [
        pytest.param(NOW, id='exactly-now'),
        pytest.param(NOW.astimezone(PLUS_FIVE), id='now-spelled-at-plus-five'),
        pytest.param(NOW + MICROSECOND, id='just-after-now'),
        pytest.param(NOW + timedelta(days=30), id='next-month'),
    ],
)
async def test_a_range_starting_at_or_after_the_readers_clock_is_refused_before_any_call_or_token(
    executor, budget, start
):
    client = stub_client(START)

    error = failure_of(await reader(client, executor).get_bars(query(start=start, end=start + timedelta(days=1))))

    assert type(error) is InvalidRequestError
    assert error.reason is Reason.RANGE_IN_FUTURE
    assert REASONS[error.reason].outcome is Outcome.REFUSED
    assert error.reset_at is None
    assert error.__cause__ is None
    assert dict(error.metadata) == {'range_start': start.isoformat()}
    assert client.mock_calls == [], 'the vendor was called for a range in the future'
    assert budget.acquires == [], 'a rate token was spent on a range in the future'


@pytest.mark.asyncio
async def test_a_range_starting_just_before_the_clock_is_fetched_not_refused(executor, budget):
    client = stub_client()

    response = await reader(client, executor).get_bars(query(start=NOW - MICROSECOND, end=None))

    assert isinstance(response, BarsResponse)
    client.get_stock_bars.assert_called_once()


@pytest.mark.asyncio
async def test_the_pre_check_reads_the_injected_clock_not_the_wall_clock(executor, budget):
    # A start a year in the past is in the FUTURE of a clock set two years back.
    past_clock = NOW - timedelta(days=730)

    error = failure_of(await reader(stub_client(START), executor, clock=lambda: past_clock).get_bars(query()))

    assert error.reason is Reason.RANGE_IN_FUTURE


@pytest.mark.asyncio
async def test_a_future_range_is_refused_before_the_credential_is_resolved(executor, budget):
    with patch.dict(os.environ, {}, clear=True):
        error = failure_of(await reader(None, executor).get_bars(query(start=NOW, end=None)))

    assert error.reason is Reason.RANGE_IN_FUTURE


# ---------------------------------------------------------------------------------------------
# A missing credential is RETURNED (VENDOR_AUTH), before any call or token
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_missing_credential_is_returned_as_vendor_auth_before_any_token(executor, budget):
    with patch.dict(os.environ, {'ALPACA_API_KEY': 'not-a-real-key'}, clear=True):
        error = failure_of(await reader(None, executor).get_bars(query()))

    assert type(error) is MissingCredentialsError
    assert error.reason is Reason.VENDOR_AUTH
    assert 'ALPACA_API_SECRET' in error.detail
    assert 'not-a-real-key' not in error.detail
    assert budget.acquires == []


# ---------------------------------------------------------------------------------------------
# SERVED: served_range and as_of (D2, D3 note a; Q-EMPTY addendum item 1)
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_past_range_is_served_as_asked_with_as_of_from_the_readers_clock(executor, budget):
    response = await reader(stub_client(START), executor).get_bars(query())

    assert response.served_range == ServedRange(START, END)
    assert type(response.served_range) is ServedRange
    assert response.as_of == NOW
    assert response.feed is Feed.IEX


@pytest.mark.asyncio
async def test_an_open_end_is_served_up_to_as_of(executor, budget):
    response = await reader(stub_client(START), executor).get_bars(query(end=None))

    assert response.served_range == ServedRange(START, NOW)
    assert response.as_of == NOW


@pytest.mark.asyncio
async def test_a_range_that_starts_in_the_past_and_ends_in_the_future_is_served_up_to_as_of(executor, budget):
    """Q-EMPTY addendum (3)(b): NOT refused. Fetched, and served_range ends at as_of, never at the asked end."""
    future_end = NOW + timedelta(days=3)
    client = stub_client(START)

    response = await reader(client, executor).get_bars(query(end=future_end))

    assert isinstance(response, BarsResponse)
    assert response.served_range == ServedRange(START, NOW)
    client.get_stock_bars.assert_called_once()


@pytest.mark.asyncio
async def test_an_end_exactly_at_the_clock_is_kept(executor, budget):
    response = await reader(stub_client(START), executor).get_bars(query(end=NOW))

    assert response.served_range == ServedRange(START, NOW)


@pytest.mark.asyncio
async def test_as_of_is_utc_whatever_offset_the_clock_answers_in(executor, budget):
    response = await reader(stub_client(START), executor, clock=lambda: NOW.astimezone(PLUS_FIVE)).get_bars(
        query(end=None)
    )

    assert response.as_of == NOW
    assert response.as_of.utcoffset() == timedelta(0)
    assert response.served_range.end == NOW


@pytest.mark.asyncio
async def test_as_of_is_read_after_the_vendor_answered(executor, budget):
    # Two reads of the clock: the pre-check before the call, as_of after it.
    readings = iter([NOW - timedelta(seconds=5), NOW])

    response = await reader(stub_client(START), executor, clock=lambda: next(readings)).get_bars(query(end=None))

    assert response.as_of == NOW
    assert response.served_range.end == NOW


# ---------------------------------------------------------------------------------------------
# What get_bars converts, and what it never converts (D5, D6)
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('raised', 'reason'),
    [
        pytest.param(api_error(400, 'end should not be before start'), Reason.VENDOR_INVALID_REQUEST, id='400'),
        pytest.param(api_error(403, 'forbidden'), Reason.VENDOR_AUTH, id='403'),
        pytest.param(api_error(404, 'not found'), Reason.VENDOR_REJECTED, id='404'),
        pytest.param(api_error(500, 'internal'), Reason.VENDOR_UNAVAILABLE, id='500'),
        pytest.param(requests.exceptions.ConnectionError('refused'), Reason.VENDOR_UNAVAILABLE, id='connection'),
        pytest.param(requests.exceptions.ReadTimeout('slow'), Reason.VENDOR_UNAVAILABLE, id='read-timeout'),
        # tj-3mk3u5.37.14: the connection cut while the body is read.
        pytest.param(requests.exceptions.ChunkedEncodingError('cut'), Reason.VENDOR_UNAVAILABLE, id='cut-mid-body'),
    ],
)
async def test_a_vendor_failure_is_returned_with_the_caught_exception_as_its_cause(executor, budget, raised, reason):
    """D8 via the 16:22 UTC addendum item 5: the returned form of 'raise ... from e'."""
    response = await reader(failing_client(raised), executor).get_bars(query())

    error = failure_of(response)
    assert error.reason is reason
    assert error.__cause__ is raised
    # The call reached the vendor, so it spent its token.
    assert len(budget.acquires) == 1


@pytest.mark.asyncio
async def test_a_vendor_429_is_returned_rate_limited_with_reset_at_on_the_readers_clock(executor, monkeypatch):
    monkeypatch.setattr(broker_api, '__RATE_BUDGET', RateBudget('ALPACA', rate_per_sec=2.0, burst=4.0))
    raised = api_error(429, 'rate limit exceeded')

    error = failure_of(await reader(failing_client(raised), executor).get_bars(query()))

    assert error.reason is Reason.VENDOR_RATE_LIMITED
    # No header on this response, so the budget's refill from empty: 4 / 2 = 2 s after the reader's now.
    assert error.reset_at == NOW + timedelta(seconds=2)
    assert error.__cause__ is raised


@pytest.mark.asyncio
async def test_an_api_error_with_no_status_is_re_raised_as_a_bug(executor, budget):
    raised = APIError('{"message": "no status"}')

    with pytest.raises(APIError) as caught:
        await reader(failing_client(raised), executor).get_bars(query())

    assert caught.value is raised


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'raised',
    [
        pytest.param(AttributeError("'NoneType' object has no attribute 'items'"), id='attribute-error'),
        pytest.param(TypeError('a bug'), id='type-error'),
        pytest.param(KeyError('bars'), id='key-error'),
        # requests' exceptions that are not transport failures stay bugs (tj-3mk3u5.37.14; D5, D6). Until
        # that bead this list held ChunkedEncodingError, the cut body, pinned as built so the fix would red it.
        pytest.param(requests.exceptions.RequestException('ambiguous'), id='bare-request-exception'),
        pytest.param(requests.exceptions.InvalidURL('bad url'), id='invalid-url'),
        pytest.param(requests.exceptions.TooManyRedirects('loop'), id='too-many-redirects'),
        pytest.param(requests.exceptions.InvalidHeader('bad header'), id='invalid-header'),
        pytest.param(requests.exceptions.ContentDecodingError('bad gzip'), id='content-decoding-error'),
        pytest.param(requests.exceptions.RetryError('retries'), id='retry-error'),
        pytest.param(requests.exceptions.HTTPError('unwrapped status'), id='unwrapped-http-error'),
    ],
)
async def test_anything_else_the_vendor_call_raises_propagates_unconverted(executor, budget, raised):
    """D6: no broad except around the SDK call. A bug escapes get_bars as itself (D5)."""
    with pytest.raises(type(raised)) as caught:
        await reader(failing_client(raised), executor).get_bars(query())

    assert caught.value is raised


@pytest.mark.asyncio
async def test_the_rate_budgets_refusal_is_returned_with_no_cause_and_no_vendor_call(executor, monkeypatch):
    """U4 = A through the reader: a deadline the budget cannot meet is a RATE_BUDGET failure, at once."""

    class Monotonic:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    async def never_sleep(seconds: float) -> None:
        # Fail fast means no sleep at all. A budget that slept here would wait on a clock that never
        # moves, so the refusal is asserted by failing the sleep rather than by hanging the run.
        raise AssertionError(f'the rate budget slept {seconds}s instead of refusing at once')

    monkeypatch.setattr(rate_budget_module, 'asyncio', SimpleNamespace(sleep=never_sleep))
    rate_budget = RateBudget('ALPACA', rate_per_sec=1.0, burst=1.0, clock=Monotonic(), wall_clock=lambda: NOW)
    assert rate_budget.try_acquire(RequestPriority.LIVE), 'the only token, spent before the query'
    monkeypatch.setattr(broker_api, '__RATE_BUDGET', rate_budget)
    client = stub_client(START)

    error = failure_of(await reader(client, executor).get_bars(query(deadline=NOW + timedelta(milliseconds=500))))

    assert type(error) is ExogenousError
    assert error.reason is Reason.RATE_BUDGET
    assert REASONS[error.reason].outcome is Outcome.NOT_READY
    assert error.reset_at == NOW + timedelta(seconds=1)
    assert error.__cause__ is None
    assert client.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'deadline', [pytest.param(None, id='no-deadline'), pytest.param(NOW + timedelta(seconds=5), id='a-deadline')]
)
async def test_the_querys_priority_and_deadline_reach_the_rate_budget(executor, budget, deadline):
    await reader(stub_client(START), executor).get_bars(query(priority=RequestPriority.BACKFILL, deadline=deadline))

    assert budget.acquires == [(RequestPriority.BACKFILL, deadline)]


# ---------------------------------------------------------------------------------------------
# The re-parented errors (D5; TE-4 item 3)
# ---------------------------------------------------------------------------------------------


def test_broker_unsupported_error_is_an_invalid_request_error_leaf():
    assert BrokerUnsupportedError.__bases__ == (InvalidRequestError,)
    assert not issubclass(BrokerUnsupportedError, ExogenousError)


@pytest.mark.parametrize('reason', sorted(UNSUPPORTED_REASONS))
def test_broker_unsupported_error_carries_each_of_its_three_reasons(reason):
    error = BrokerUnsupportedError(reason, 'Alpaca serves USD only: currency=CAD', metadata={'feed': 'SIP'})

    assert error.reason is reason
    assert error.detail == 'Alpaca serves USD only: currency=CAD'
    assert dict(error.metadata) == {'feed': 'SIP'}
    assert error.reset_at is None


@pytest.mark.parametrize('reason', [reason for reason in Reason if reason not in UNSUPPORTED_REASONS])
def test_broker_unsupported_error_refuses_every_other_reason(reason):
    with pytest.raises(ValueError, match='BrokerUnsupportedError carries'):
        BrokerUnsupportedError(reason, 'anything')


def test_missing_credentials_error_is_an_exogenous_vendor_auth_and_no_longer_a_runtime_error():
    error = MissingCredentialsError('Alpaca credentials are not configured: ALPACA_API_SECRET unset')

    assert MissingCredentialsError.__bases__ == (ExogenousError,)
    assert not isinstance(error, RuntimeError)
    assert error.reason is Reason.VENDOR_AUTH
    assert REASONS[error.reason].outcome is Outcome.NOT_READY
    assert error.detail == 'Alpaca credentials are not configured: ALPACA_API_SECRET unset'
    assert list(inspect.signature(MissingCredentialsError).parameters) == ['detail']


# ---------------------------------------------------------------------------------------------
# The result types (TE-4 items 1 and 2)
# ---------------------------------------------------------------------------------------------


def test_bars_failure_holds_the_error_alone_and_states_no_outcome_of_its_own():
    """The outcome class is READ from REASONS[error.reason], never stated a second time (TE-4 item 1)."""
    assert [field.name for field in dataclasses.fields(BarsFailure)] == ['error']
    failure = BarsFailure(InvalidRequestError(Reason.RANGE_IN_FUTURE, 'starts after now'))
    with pytest.raises(dataclasses.FrozenInstanceError):
        failure.error = None  # type: ignore[misc]


def test_bars_response_requires_served_range_and_as_of():
    assert [field.name for field in dataclasses.fields(BarsResponse)] == ['feed', 'bars', 'served_range', 'as_of']
    with pytest.raises(TypeError, match='served_range'):
        BarsResponse(feed=Feed.IEX, bars=None)  # type: ignore[call-arg]


def test_served_range_is_a_start_and_an_end():
    served = ServedRange(START, END)

    assert served == (START, END)
    assert (served.start, served.end) == (START, END)


def test_bars_query_feed_and_deadline_are_keyword_only_and_default_to_none():
    built = query()

    assert (built.feed, built.deadline) == (None, None)
    by_name = {field.name: field for field in dataclasses.fields(BarsQuery)}
    assert by_name['feed'].kw_only and by_name['deadline'].kw_only


def test_broker_read_get_bars_returns_the_typed_result():
    hints = typing.get_type_hints(BrokerRead.get_bars)

    assert hints['return'] == BarsResponse | BarsFailure


# ---------------------------------------------------------------------------------------------
# The unsupported-path stubs, match_client_request's fallback, and create_app (TE-4 items 3 and 7;
# tj-3mk3u5.37.14)
# ---------------------------------------------------------------------------------------------


# RETIRED ON tj-3mk3u5.32:
# test_the_crypto_and_option_stubs_are_coroutines_that_refuse_with_unsupported_asset_type.
#
# It parametrized directly over ingest_control.store_retrieve_crypto and store_retrieve_option,
# which tj-3mk3u5.11 deletes with the rest of the Kafka edge -- so the module would not have
# imported, let alone run. The coroutine half of it was about how the KAFKA handler awaited those
# stubs; there is no such caller left to satisfy.
#
# The UNSUPPORTED_ASSET_TYPE half survives on the gRPC path, where the refusal is the handler's
# rather than a dispatch stub's: test_fetch_dataset_handler.py's crypto and option cases of
# test_an_unservable_request_is_refused_before_the_ack_and_before_the_vendor_is_called, with
# test_a_non_stock_asset_type_is_refused_even_by_a_reader_that_would_have_served_it proving the
# refusal comes from the handler and not from whichever reader happens to be installed.


# Every triple match_client_request maps, and what it maps each to: the six stock endpoints alpaca-py has.
MAPPED_TRIPLES = {
    (AssetType.STOCK, DataType.MARKET_ACTIVITY, False): ('get_stock_bars', StockBarsRequest),
    (AssetType.STOCK, DataType.MARKET_ACTIVITY, True): ('get_stock_latest_bar', StockLatestBarRequest),
    (AssetType.STOCK, DataType.QUOTE, False): ('get_stock_quotes', StockQuotesRequest),
    (AssetType.STOCK, DataType.QUOTE, True): ('get_stock_latest_quote', StockLatestQuoteRequest),
    (AssetType.STOCK, DataType.TRADE, False): ('get_stock_trades', StockTradesRequest),
    (AssetType.STOCK, DataType.TRADE, True): ('get_stock_latest_trade', StockLatestTradeRequest),
}

# Every other (asset_type, data_type, latest) the enums can spell, a member added later included.
UNMAPPED_TRIPLES = [
    triple for triple in itertools.product(AssetType, DataType, (False, True)) if triple not in MAPPED_TRIPLES
]


def triple_id(triple: tuple[AssetType, DataType, bool]) -> str:
    asset_type, data_type, latest = triple
    return f'{asset_type.value}-{data_type.value}-{"latest" if latest else "range"}'


@pytest.mark.parametrize(
    ('asset_type', 'data_type', 'latest'), [pytest.param(*triple, id=triple_id(triple)) for triple in UNMAPPED_TRIPLES]
)
def test_an_unmapped_triple_is_refused_with_unsupported_asset_type(asset_type, data_type, latest):
    """tj-3mk3u5.37.14: the fallback raises, as fetch_data_type's docstring says, and never returns None.

    The dead fallback used to be 'case (_, _)', which cannot match a 3-tuple subject, so an unmapped triple
    fell off the match and returned None.
    """
    client = Mock()

    with pytest.raises(InvalidRequestError) as raised:
        broker_api.match_client_request(client, asset_type, data_type, latest)

    error = raised.value
    assert type(error) is InvalidRequestError
    assert error.reason is Reason.UNSUPPORTED_ASSET_TYPE
    assert REASONS[error.reason].outcome is Outcome.REFUSED
    assert f'asset_type={asset_type.value}' in error.detail
    assert f'data_type={data_type.value}' in error.detail
    assert error.__cause__ is None
    assert client.mock_calls == []


@pytest.mark.parametrize(
    ('asset_type', 'data_type', 'latest', 'method', 'request_type'),
    [pytest.param(*triple, *mapped, id=triple_id(triple)) for triple, mapped in MAPPED_TRIPLES.items()],
)
def test_each_stock_triple_still_maps_to_its_sdk_method_and_request(
    asset_type, data_type, latest, method, request_type
):
    client = Mock()

    mapped = broker_api.match_client_request(client, asset_type, data_type, latest)

    assert mapped == (getattr(client, method), request_type)
    assert mapped[0] is getattr(client, method)
    assert client.mock_calls == [], 'mapping a request must not call the vendor'


def test_create_app_installs_the_problem_handlers_and_declares_their_responses():
    app = main.create_app({DataSource.ALPACA_API: Mock()})

    async def refuse() -> None:
        raise InvalidRequestError(Reason.RANGE_IN_FUTURE, 'starts after now')

    app.add_api_route('/probe-refusal', refuse)
    # No `with`: the lifespan (Kafka, gRPC) is not entered; the handlers are the app's own.
    client = TestClient(app, raise_server_exceptions=False)

    refused = client.get('/probe-refusal')
    assert refused.status_code == 422
    assert refused.headers['content-type'].startswith('application/problem+json')
    assert refused.json()['reason'] == 'RANGE_IN_FUTURE'
    # The /ping surface keeps its status.
    assert client.get('/ping').status_code == 200
    assert app.router.responses == PROBLEM_RESPONSES
