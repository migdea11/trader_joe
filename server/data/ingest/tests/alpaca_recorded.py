"""Recorded-response harness: answers alpaca-py's HTTP from fixture files, in-process.

Decision tj-j4wknb R3: the vendor parsing and paging a transport fake would have exercised are
covered by unit tests on the REAL alpaca-py client, answering its HTTP from recorded bodies. This
module is that mechanism, kept free of test functions so the contract suite (FAKES-3, tj-irhy0a.10)
can import it without importing a test module.

The transport is a requests adapter mounted on the client's own session for BOTH http:// and
https://, so every URL the SDK can build reaches it and nothing reaches a socket. It fails the test
-- rather than answering -- on any host other than the one it serves (Alpaca's data host, or the invented
trading host of stub_trading_vendor for the asset reads, tj-7zjov1.22), on a page token no fixture
knows, and past a request cap, so a paging loop that never ends turns red instead of hanging.

Fixture files live in fixtures/alpaca/ as JSON: {"provenance": {...}, "status": <int>, "body": ...}.
Most were RECORDED from Alpaca at the host sitting (tj-irhy0a.3; FAKES-4 tj-irhy0a.13); the few
that cannot be provoked stay DOCUMENTED from research tj-vhboky.57. fixtures/alpaca/README.md says
which is which.

RESPONSE HEADERS (TE-5 tj-3mk3u5.37.6). A response can carry headers beside its Content-Type, so a
429 can be answered with or without X-RateLimit-Reset or Retry-After. No fixture records any: the
recorder writes no header (README, Re-recording), and which ones a real 429 sends is unknown. A
test that needs one attaches it with with_headers(), and says so.

STUB VENDORS (tj-7zjov1.56). Since the reader owns the bars page loop, it reads the client's private
_session, _base_url, _api_version and _get_default_headers (broker_api.request_page), so a Mock client
standing in for get_stock_bars no longer reaches anything. Tests that only need a few bars, or an
exception from the HTTP session, use stub_vendor(): the REAL client over this same transport, answering
scripted answers in turn, with constructed bodies (wire_bar, bars_answer, status_answer) whose
provenance says 'constructed'. So every such test also exercises the private attributes for real.
"""

import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http import HTTPStatus
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests
from alpaca.common import rest as alpaca_rest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from data.ingest.app.brokers.alpaca import broker_api


FIXTURES_DIR = Path(__file__).parent / 'fixtures' / 'alpaca'

# The host and path alpaca-py 0.44.0 calls for stock bars (research tj-vhboky.57 item 5).
ALPACA_DATA_HOST = 'data.alpaca.markets'
BARS_PATH = '/v2/stocks/bars'

# Placeholders, not credentials: the SDK refuses to build without a key pair, and the transport
# never sends them anywhere.
PLACEHOLDER_KEY_ID = 'placeholder-key-id'
PLACEHOLDER_SECRET = 'placeholder-secret'

# Whole seconds, an optional fraction of any length, and a mandatory offset: a naive value is
# refused, because every instant compared here must be aware.
RFC3339_FRACTION = re.compile(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?([+-]\d{2}:\d{2})')

# Far above any scenario here (the retry case makes 4), low enough that a loop fails fast.
DEFAULT_REQUEST_CAP = 20


@dataclass(frozen=True)
class RecordedResponse:
    """One vendor answer: an HTTP status and its JSON body.

    Attributes:
        name (str): Fixture file stem, for failure messages.
        status (int): HTTP status code.
        body (Any): Decoded JSON body.
        provenance (Mapping[str, Any]): Where the body came from ('documented' or 'recorded').
        headers (Mapping[str, str]): Response headers beside Content-Type, such as a 429's
            X-RateLimit-Reset or Retry-After. None recorded; see with_headers().
    """

    name: str
    status: int
    body: Any
    provenance: Mapping[str, Any] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def bars(self) -> list[dict]:
        """Every bar in the body, across every symbol, in body order."""
        bars = self.body.get('bars') or {}
        return [bar for symbol_bars in bars.values() for bar in symbol_bars if bar is not None]

    @property
    def timestamps(self) -> list[datetime]:
        """The instants of every bar in the body, parsed from their RFC 3339 't' fields."""
        return [parse_rfc3339(bar['t']) for bar in self.bars]

    @property
    def next_page_token(self) -> str | None:
        """The body's next_page_token, or None on the last page."""
        return self.body.get('next_page_token')


@dataclass(frozen=True)
class SentRequest:
    """What reached the transport: the request and the send arguments requests passed with it.

    Attributes:
        method (str): HTTP method.
        url (str): Full URL, query string included.
        headers (Mapping[str, str]): Request headers.
        timeout (Any): The timeout requests handed the adapter (None when the caller set none).
    """

    method: str
    url: str
    headers: Mapping[str, str]
    timeout: Any

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    @property
    def host(self) -> str | None:
        return urlsplit(self.url).hostname

    @property
    def params(self) -> dict[str, str]:
        """The query parameters, one value each (the SDK never repeats one)."""
        parsed = parse_qs(urlsplit(self.url).query, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}


Responder = Callable[[SentRequest], RecordedResponse]


def parse_rfc3339(value: str) -> datetime:
    """Parse an RFC 3339 timestamp, including a Z suffix and nanosecond fractions."""
    match = RFC3339_FRACTION.fullmatch(value.replace('Z', '+00:00'))
    if match is None:
        raise ValueError(f'not an RFC 3339 timestamp with an offset: {value!r}')
    whole, fraction, offset = match.groups()
    # datetime holds microseconds; the vendor may send nanoseconds (tj-vhboky.57 item 1).
    micros = f'.{fraction[:6].ljust(6, "0")}' if fraction else ''
    return datetime.fromisoformat(f'{whole}{micros}{offset}')


def load(name: str) -> RecordedResponse:
    """Load one fixture from fixtures/alpaca/<name>.json.

    Args:
        name (str): File stem.

    Returns:
        RecordedResponse: The fixture.
    """
    raw = json.loads((FIXTURES_DIR / f'{name}.json').read_text())
    return RecordedResponse(
        name=name,
        status=raw['status'],
        body=raw['body'],
        provenance=raw.get('provenance', {}),
        headers=raw.get('headers', {}),
    )


def with_headers(response: RecordedResponse, headers: Mapping[str, str]) -> RecordedResponse:
    """The same response, answering with these headers too: constructed, never recorded."""
    return replace(
        response, name=f'{response.name}[headers={sorted(headers)}]', headers={**response.headers, **headers}
    )


def always(response: RecordedResponse) -> Responder:
    """Answer every request with the same response."""
    return lambda _sent: response


def in_order(responses: Sequence[RecordedResponse]) -> Responder:
    """Answer the n-th request with the n-th response; a request past the end fails the test."""
    remaining = list(responses)

    def respond(sent: SentRequest) -> RecordedResponse:
        if not remaining:
            raise AssertionError(f'unscripted request past the last recorded response: {sent.url}')
        return remaining.pop(0)

    return respond


def by_page_token(pages: Mapping[str | None, RecordedResponse]) -> Responder:
    """Answer by the request's page_token, as the vendor does; None keys the first page.

    An unknown token fails the test, so a client that invents or mangles a token is caught.
    """

    def respond(sent: SentRequest) -> RecordedResponse:
        token = sent.params.get('page_token')
        if token not in pages:
            raise AssertionError(f'no recorded page for page_token={token!r}: {sent.url}')
        return pages[token]

    return respond


def paged(*responses: RecordedResponse) -> Responder:
    """Chain pages through their own next_page_token values: page n+1 answers page n's token."""
    pages: dict[str | None, RecordedResponse] = {None: responses[0]}
    for current, following in pairwise(responses):
        token = current.next_page_token
        if token is None:
            raise AssertionError(f'{current.name} has no next_page_token, so nothing can reach {following.name}')
        pages[token] = following
    return by_page_token(pages)


class RecordedTransport(BaseAdapter):
    """A requests adapter that answers from recorded responses and records what it was sent.

    Args:
        responder (Responder): Chooses the response for each request.
        request_cap (int): Requests allowed before the transport fails the test.
    """

    def __init__(
        self, responder: Responder, request_cap: int = DEFAULT_REQUEST_CAP, host: str = ALPACA_DATA_HOST
    ) -> None:
        super().__init__()
        self.responder = responder
        self.request_cap = request_cap
        self.host = host
        self.sent: list[SentRequest] = []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        sent = SentRequest(method=request.method, url=request.url, headers=dict(request.headers), timeout=timeout)
        self.sent.append(sent)
        if len(self.sent) > self.request_cap:
            raise AssertionError(f'more than {self.request_cap} requests reached the transport; is the client looping?')
        if sent.host != self.host:
            raise AssertionError(f'request left the recorded vendor host: {sent.url}')
        recorded = self.responder(sent)

        response = requests.Response()
        response.status_code = recorded.status
        response.reason = HTTPStatus(recorded.status).phrase
        # A bytes body is served exactly as given (the asset reads slice it byte for byte); anything else as JSON.
        response._content = recorded.body if isinstance(recorded.body, bytes) else json.dumps(recorded.body).encode()
        response.headers = CaseInsensitiveDict({'Content-Type': 'application/json', **recorded.headers})
        response.encoding = 'utf-8'
        response.url = request.url
        response.request = request
        return response

    def close(self) -> None:
        pass


# RETIRED ON tj-7zjov1.56: record_sdk_sleeps.
#
# It replaced the clock alpaca-py's own retry loop sleeps on (common/rest.py RESTClient._request). Since
# tj-7zjov1.56 the reader owns the bars page loop and makes every request through broker_api.request_page,
# which never enters that loop, so the clock it patched is dead: a test asserting its recorder stayed empty
# asserted nothing. Its live successor is record_page_retry_sleeps below, the wait between the 504 retries the
# reader now makes itself. A reader that slid back onto the SDK's loop is caught by request counts instead
# (a 429 would reach the transport four times, not once), and by sdk_retry_is_never_entered.


def record_page_retry_sleeps(monkeypatch) -> list[float]:
    """Replace the wait broker_api's bars page loop sleeps before a 504 retry with a recorder, for one test.

    The loop waits with asyncio.sleep(BARS_PAGE_RETRY_WAIT_S), 3 s (tj-7zjov1.56). Only that module's asyncio
    reference is replaced, and get_running_loop is the real one, so the scheduler's own waits and the worker pool
    are untouched. The recorded sleep returns at once, so the gate stays fast and each wait is assertable.

    Args:
        monkeypatch (pytest.MonkeyPatch): The test's monkeypatch fixture, which undoes this.

    Returns:
        list[float]: Every wait the page loop asked for, in order.
    """
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(broker_api, 'asyncio', SimpleNamespace(sleep=sleep, get_running_loop=asyncio.get_running_loop))
    return sleeps


def sdk_retry_is_never_entered(monkeypatch) -> list[str]:
    """Make alpaca-py's own request loop record every entry, for one test.

    RESTClient._request is where the SDK sleeps and retries a 429 or 504 by itself. Since tj-7zjov1.56 nothing on
    the bars path may reach it (the bead's requirement 3: no HTTP request outside the scheduler). The real method
    still runs, so a regression is observed and not masked.

    Args:
        monkeypatch (pytest.MonkeyPatch): The test's monkeypatch fixture, which undoes this.

    Returns:
        list[str]: The path of every request that went through the SDK's own loop.
    """
    entered: list[str] = []
    sdk_request = alpaca_rest.RESTClient._request

    def recording(self, method, path, *args, **kwargs):
        entered.append(path)
        return sdk_request(self, method, path, *args, **kwargs)

    monkeypatch.setattr(alpaca_rest.RESTClient, '_request', recording)
    return entered


# Constructed bodies, for tests that need a bar set no fixture holds. Never written to fixtures/: these are
# not recordings, and their provenance says so.


def wire_bar(timestamp: datetime, close: float = 10.0) -> dict[str, Any]:
    """One bar as the vendor's JSON carries it (tj-vhboky.57 item 1), at an aware instant."""
    if timestamp.utcoffset() is None:
        raise ValueError(f'a wire bar needs an aware instant: {timestamp!r}')
    stamp = timestamp.astimezone(UTC).isoformat().replace('+00:00', 'Z')
    return {'t': stamp, 'o': 1.0, 'h': 2.0, 'l': 0.5, 'c': close, 'v': 10, 'n': 3, 'vw': 1.5}


def bars_answer(
    bars_by_symbol: Mapping[str, Sequence[dict[str, Any]]],
    next_page_token: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> RecordedResponse:
    """A constructed 200 bars page: its bars by symbol, its next_page_token, and any response headers."""
    return RecordedResponse(
        name=f'constructed_bars[{next_page_token}]',
        status=200,
        body={
            'bars': {symbol: list(bars) for symbol, bars in bars_by_symbol.items()},
            'next_page_token': next_page_token,
        },
        provenance={'kind': 'constructed'},
        headers=dict(headers or {}),
    )


def status_answer(status: int, message: str = 'no', headers: Mapping[str, str] | None = None) -> RecordedResponse:
    """A constructed error answer, shaped as Alpaca's: {"message": ...}, with no code field."""
    return RecordedResponse(
        name=f'constructed_{status}',
        status=status,
        body={'message': message},
        provenance={'kind': 'constructed'},
        headers=dict(headers or {}),
    )


Answer = RecordedResponse | BaseException


def scripted(*answers: Answer) -> Responder:
    """Answer the n-th request with the n-th answer, raising it when it is an exception.

    An exception is raised by the transport, inside the HTTP session, which is where requests raises a
    connection error, a timeout or a cut body. A request past the last answer fails the test.
    """
    remaining = list(answers)

    def respond(sent: SentRequest) -> RecordedResponse:
        if not remaining:
            raise AssertionError(f'unscripted request past the last scripted answer: {sent.url}')
        answer = remaining.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return respond


@dataclass
class StubVendor:
    """A real alpaca-py client over a scripted RecordedTransport, and what reached it.

    Attributes:
        client (StockHistoricalDataClient): The client to inject.
        transport (RecordedTransport): Its transport.
    """

    client: StockHistoricalDataClient
    transport: RecordedTransport

    @property
    def sent(self) -> list[SentRequest]:
        """Every HTTP request that reached the vendor, in order."""
        return self.transport.sent


def stub_vendor(*answers: Answer, request_cap: int = DEFAULT_REQUEST_CAP) -> StubVendor:
    """A real client answering each request in turn with `answers` (see scripted)."""
    client, transport = recorded_client(scripted(*answers), request_cap)
    return StubVendor(client, transport)


# The trading host the asset-read tests configure. Invented and unresolvable on purpose: the real host is
# deployment configuration (ALPACA_TRADING_BASE_URL), never a default, and nothing here may reach a network.
ALPACA_TRADING_TEST_HOST = 'trading.alpaca.test.invalid'
ALPACA_TRADING_TEST_URL = f'https://{ALPACA_TRADING_TEST_HOST}'
# The path alpaca-py 0.44.0's TradingClient prefixes (api_version v2) to the asset endpoint.
ASSETS_PATH = '/v2/assets'


@dataclass
class StubTradingVendor:
    """A real alpaca-py TradingClient over a scripted RecordedTransport, and what reached it.

    Attributes:
        client (TradingClient): The client to inject.
        transport (RecordedTransport): Its transport.
    """

    client: TradingClient
    transport: RecordedTransport

    @property
    def sent(self) -> list[SentRequest]:
        """Every HTTP request that reached the vendor, in order."""
        return self.transport.sent


def stub_trading_vendor(*answers: Answer, request_cap: int = DEFAULT_REQUEST_CAP) -> StubTradingVendor:
    """A real TradingClient on ALPACA_TRADING_TEST_URL, answering each request in turn with `answers` (see scripted).

    Built exactly as broker_api.get_trading_client builds one (url_override, header auth), with the placeholder pair.
    """
    client = TradingClient(PLACEHOLDER_KEY_ID, PLACEHOLDER_SECRET, url_override=ALPACA_TRADING_TEST_URL)
    transport = RecordedTransport(scripted(*answers), request_cap, host=ALPACA_TRADING_TEST_HOST)
    session = client._session
    session.mount('https://', transport)
    session.mount('http://', transport)
    if set(session.adapters) != {'https://', 'http://'} or any(a is not transport for a in session.adapters.values()):
        raise AssertionError(f'a real adapter is still mounted: {dict(session.adapters)}')
    return StubTradingVendor(client, transport)


def raw_answer(body: bytes, status: int = 200, headers: Mapping[str, str] | None = None) -> RecordedResponse:
    """A constructed answer whose body is served byte for byte, never re-encoded."""
    return RecordedResponse(
        name=f'constructed_raw_{status}',
        status=status,
        body=body,
        provenance={'kind': 'constructed'},
        headers=dict(headers or {}),
    )


def asset_answer(symbol: str, vendor_asset_id: str) -> RecordedResponse:
    """A constructed 200 asset-by-symbol answer: one trading-API asset record, invented, not a recording."""
    body = (
        f'{{"id": "{vendor_asset_id}", "class": "us_equity", "exchange": "NASDAQ", "symbol": "{symbol}",'
        f' "name": "Constructed {symbol}", "status": "active", "tradable": true}}'
    )
    return raw_answer(body.encode('utf-8'))


def recorded_client(
    responder: Responder, request_cap: int = DEFAULT_REQUEST_CAP
) -> tuple[StockHistoricalDataClient, RecordedTransport]:
    """Build a real alpaca-py client whose every HTTP request is answered by a RecordedTransport.

    The client keeps alpaca-py's own defaults (retry count, wait, codes); a test that must not
    sleep patches the SDK's clock rather than the client's configuration.

    Args:
        responder (Responder): Chooses the response for each request.
        request_cap (int): Requests allowed before the transport fails the test.

    Returns:
        tuple[StockHistoricalDataClient, RecordedTransport]: The client and its transport.
    """
    client = StockHistoricalDataClient(PLACEHOLDER_KEY_ID, PLACEHOLDER_SECRET)
    transport = RecordedTransport(responder, request_cap)
    # RESTClient keeps a plain requests.Session in the private _session (alpaca-py 0.44.0,
    # common/rest.py:69; research tj-vhboky.57 item 8). Mounting on both schemes replaces the only
    # two adapters a Session carries, so no URL can reach the real HTTPAdapter.
    session = client._session
    session.mount('https://', transport)
    session.mount('http://', transport)
    if set(session.adapters) != {'https://', 'http://'} or any(a is not transport for a in session.adapters.values()):
        raise AssertionError(f'a real adapter is still mounted: {dict(session.adapters)}')
    return client, transport
