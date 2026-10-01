"""Recorded-response harness: answers alpaca-py's HTTP from fixture files, in-process.

Decision tj-j4wknb R3: the vendor parsing and paging a transport fake would have exercised are
covered by unit tests on the REAL alpaca-py client, answering its HTTP from recorded bodies. This
module is that mechanism, kept free of test functions so the contract suite (FAKES-3, tj-irhy0a.10)
can import it without importing a test module.

The transport is a requests adapter mounted on the client's own session for BOTH http:// and
https://, so every URL the SDK can build reaches it and nothing reaches a socket. It fails the test
-- rather than answering -- on any host other than Alpaca's data host, on a page token no fixture
knows, and past a request cap, so a paging loop that never ends turns red instead of hanging.

Fixture files live in fixtures/alpaca/ as JSON: {"provenance": {...}, "status": <int>, "body": ...}.
Most were RECORDED from Alpaca at the host sitting (tj-irhy0a.3; FAKES-4 tj-irhy0a.13); the few
that cannot be provoked stay DOCUMENTED from research tj-vhboky.57. fixtures/alpaca/README.md says
which is which.
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests
from alpaca.common import rest as alpaca_rest
from alpaca.data.historical import StockHistoricalDataClient
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict


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
    """

    name: str
    status: int
    body: Any
    provenance: Mapping[str, Any] = field(default_factory=dict)

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
    return RecordedResponse(name=name, status=raw['status'], body=raw['body'], provenance=raw.get('provenance', {}))


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

    def __init__(self, responder: Responder, request_cap: int = DEFAULT_REQUEST_CAP) -> None:
        super().__init__()
        self.responder = responder
        self.request_cap = request_cap
        self.sent: list[SentRequest] = []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        sent = SentRequest(method=request.method, url=request.url, headers=dict(request.headers), timeout=timeout)
        self.sent.append(sent)
        if len(self.sent) > self.request_cap:
            raise AssertionError(f'more than {self.request_cap} requests reached the transport; is the client looping?')
        if sent.host != ALPACA_DATA_HOST:
            raise AssertionError(f'request left the recorded vendor host: {sent.url}')
        recorded = self.responder(sent)

        response = requests.Response()
        response.status_code = recorded.status
        response.reason = HTTPStatus(recorded.status).phrase
        response._content = json.dumps(recorded.body).encode()
        response.headers = CaseInsensitiveDict({'Content-Type': 'application/json'})
        response.encoding = 'utf-8'
        response.url = request.url
        response.request = request
        return response

    def close(self) -> None:
        pass


def record_sdk_sleeps(monkeypatch) -> list[float]:
    """Replace the clock alpaca-py's retry loop sleeps on with a recorder, for one test.

    alpaca-py cannot be built with a zero retry wait: RESTClient ignores retry_wait_seconds=0
    ('if retry_wait_seconds and retry_wait_seconds > 0', common/rest.py:79) and keeps its 3 s
    default. Patching the module's time reference instead keeps the production defaults, keeps
    the gate fast, and makes each wait the SDK asked for assertable.

    Args:
        monkeypatch (pytest.MonkeyPatch): The test's monkeypatch fixture, which undoes this.

    Returns:
        list[float]: Every wait the SDK requested, in order.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(alpaca_rest, 'time', SimpleNamespace(sleep=sleeps.append))
    return sleeps


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
