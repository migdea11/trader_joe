"""A connection cut while Alpaca's answer is read comes back VENDOR_UNAVAILABLE, on a real socket.

TE-4 follow-up tj-3mk3u5.37.14, checked against TE-4 tj-3mk3u5.37.5 item 4 ('a connection error or
timeout from the HTTP session -> VENDOR_UNAVAILABLE'; a connection cut mid-body is one) and ADR
tj-fa1rpu D5, D6 and D8, including its 16:22 UTC 2026-10-02 addendum item 5: the caught exception
becomes the returned error's __cause__.

WHY A REAL SOCKET AND NOT A STUB. requests raises ChunkedEncodingError, wrapping urllib3's
ProtocolError, when a connection ends before the body it announced. It builds that exception inside
its own body read, and a stub that simply raises ChunkedEncodingError skips that read. Here a one-shot
server on 127.0.0.1 answers alpaca-py's real request, sent through the SDK's own requests.Session. The
server promises the recorded bars body, sends its first bytes and hangs up. There are two framings: a
Content-Length longer than what arrives, and a chunk cut part-way. The same server sending the whole
body gets a SERVED answer, so a red here comes from the cut and never from the harness.

DETERMINISTIC AND FAST. The server reads the whole request head before it answers, so hanging up
sends a FIN and never a reset that could race the response head. It writes its answer in one go and
closes, so the client never waits on it. The SDK's retry clock is recorded and must stay untouched,
because a cut is not retried. The session ignores proxy variables, so the request reaches 127.0.0.1
whatever the environment says.
"""

import json
import socket
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
import requests
from alpaca.data.historical import StockHistoricalDataClient
from urllib3.exceptions import ProtocolError

from common.enums.data_select import AssetType
from common.enums.data_stock import Granularity
from common.errors.vocabulary import REASONS, ExogenousError, Outcome, Reason
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.classify import classify_vendor_error
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.interface import BarsFailure, BarsQuery, BarsResponse, Instrument
from data.ingest.app.brokers.rate_budget import RateBudget, RequestPriority
from data.ingest.tests.alpaca_recorded import BARS_PATH, PLACEHOLDER_KEY_ID, PLACEHOLDER_SECRET, load, record_sdk_sleeps


pytestmark = pytest.mark.data_ingest

# The reader's clock, fixed, well after the recorded bars.
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

# bars_1Day is RECORDED: three AAPL daily bars, 2022-01-03 to 2022-01-05, one page.
RECORDED = load('bars_1Day')
WHOLE_BODY = json.dumps(RECORDED.body).encode()
START = datetime(2022, 1, 3, tzinfo=UTC)
END = datetime(2022, 1, 6, tzinfo=UTC)

# How much of the promised body arrives before the hang-up.
SENT_BEFORE_THE_CUT = 10

# Long enough for a loaded CI runner, and only ever reached when the harness itself is broken.
SOCKET_TIMEOUT_SECONDS = 5.0

_HEAD = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n'


def content_length_cut() -> bytes:
    """Announce the whole recorded body by Content-Length, send its first bytes, then hang up."""
    return _HEAD + f'Content-Length: {len(WHOLE_BODY)}\r\n\r\n'.encode() + WHOLE_BODY[:SENT_BEFORE_THE_CUT]


def chunk_cut() -> bytes:
    """Announce the whole recorded body as one chunk, send its first bytes, then hang up."""
    return (
        _HEAD
        + b'Transfer-Encoding: chunked\r\n\r\n'
        + f'{len(WHOLE_BODY):x}\r\n'.encode()
        + WHOLE_BODY[:SENT_BEFORE_THE_CUT]
    )


def whole_answer() -> bytes:
    """The same recorded body, sent in full: the control that proves the harness serves."""
    return _HEAD + f'Content-Length: {len(WHOLE_BODY)}\r\n\r\n'.encode() + WHOLE_BODY


@dataclass
class OneShot:
    """What the one-shot server saw.

    Attributes:
        port (int): The port it listens on, at 127.0.0.1.
        request_head (bytes): The request line and headers it read before answering.
        error (BaseException | None): What broke the server itself, if anything did.
    """

    port: int
    request_head: bytes = b''
    error: BaseException | None = field(default=None, repr=False)


@contextmanager
def one_shot_server(answer: bytes) -> Iterator[OneShot]:
    """Serve one connection on 127.0.0.1: read the request head, write `answer`, hang up.

    The head is read in full first, so the receive buffer is empty when the socket closes and the
    hang-up is a clean FIN after the answer, never a reset. Anything that breaks the server fails
    the test once the body of the `with` is done.
    """
    listener = socket.create_server(('127.0.0.1', 0))
    listener.settimeout(SOCKET_TIMEOUT_SECONDS)
    seen = OneShot(port=listener.getsockname()[1])

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(SOCKET_TIMEOUT_SECONDS)
                while b'\r\n\r\n' not in seen.request_head:
                    received = connection.recv(65536)
                    if not received:
                        break
                    seen.request_head += received
                connection.sendall(answer)
                connection.shutdown(socket.SHUT_WR)
        except OSError as error:
            # Carried to the test's own thread, which fails on it below.
            seen.error = error

    server = threading.Thread(target=serve, name='one-shot-alpaca', daemon=True)
    server.start()
    try:
        yield seen
    finally:
        server.join(SOCKET_TIMEOUT_SECONDS)
        listener.close()
    assert not server.is_alive(), 'the one-shot server never finished'
    if seen.error is not None:
        raise AssertionError('the one-shot server itself failed') from seen.error


class CountingBudget:
    """A rate budget that grants every token and counts the acquires."""

    full_refill_seconds = 1.0

    def __init__(self) -> None:
        self.acquires = 0

    async def acquire(self, priority: RequestPriority = RequestPriority.INTERACTIVE, deadline=None) -> None:
        self.acquires += 1


@dataclass
class SpiedClient:
    """A real alpaca-py client pointed at the one-shot server, and every exception its bars call raised."""

    client: StockHistoricalDataClient
    raised: list[BaseException]


def real_client(port: int) -> SpiedClient:
    """Build the real SDK client against 127.0.0.1:<port>, recording what get_stock_bars raises.

    The spy only records: it re-raises the very instance, so the reader sees exactly what the SDK
    raised, and the test can then say which instance became the cause.
    """
    client = StockHistoricalDataClient(PLACEHOLDER_KEY_ID, PLACEHOLDER_SECRET, url_override=f'http://127.0.0.1:{port}')
    # No HTTP(S)_PROXY or NO_PROXY from the environment: the request goes to the one-shot server.
    client._session.trust_env = False
    raised: list[BaseException] = []
    sdk_get_stock_bars = client.get_stock_bars

    def get_stock_bars(request):
        try:
            return sdk_get_stock_bars(request)
        except BaseException as error:
            raised.append(error)
            raise

    client.get_stock_bars = get_stock_bars
    return SpiedClient(client, raised)


@pytest.fixture(autouse=True)
def iex_deployment_and_no_cached_client(monkeypatch):
    monkeypatch.delenv('ALPACA_SIP_ENABLED', raising=False)
    broker_api.set_client(None)
    yield
    broker_api.set_client(None)


@pytest.fixture
def budget(monkeypatch) -> CountingBudget:
    counting = CountingBudget()
    monkeypatch.setattr(broker_api, '__RATE_BUDGET', counting)
    return counting


@pytest.fixture
def sdk_sleeps(monkeypatch) -> list[float]:
    return record_sdk_sleeps(monkeypatch)


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=1) as pool:
        yield pool


def query() -> BarsQuery:
    return BarsQuery(
        instrument=Instrument('AAPL', AssetType.STOCK),
        granularity=Granularity.ONE_DAY,
        start=START,
        end=END,
        priority=RequestPriority.INTERACTIVE,
    )


async def get_bars_through(answer: bytes, executor: ThreadPoolExecutor) -> tuple[object, OneShot, SpiedClient]:
    with one_shot_server(answer) as seen:
        spied = real_client(seen.port)
        outcome = await AlpacaRead(client=spied.client, executor_provider=lambda: executor, clock=lambda: NOW).get_bars(
            query()
        )
    return outcome, seen, spied


def connection_error_classified() -> ExogenousError:
    """What the classifier makes of a plain connection error: the shape a cut body must share."""
    return classify_vendor_error(
        requests.exceptions.ConnectionError('refused'),
        rate_budget=RateBudget('ALPACA', rate_per_sec=3.0, burst=3.0),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'answer', [pytest.param(content_length_cut(), id='content-length'), pytest.param(chunk_cut(), id='chunked')]
)
async def test_a_connection_cut_mid_body_is_vendor_unavailable_with_the_sdks_exception_as_its_cause(
    executor, budget, sdk_sleeps, answer
):
    outcome, seen, spied = await get_bars_through(answer, executor)

    # The real SDK reached the server, for the bars endpoint, and its call raised exactly once.
    assert seen.request_head.startswith(f'GET {BARS_PATH}?'.encode()), seen.request_head[:80]
    assert len(spied.raised) == 1
    cut = spied.raised[0]
    assert type(cut) is requests.exceptions.ChunkedEncodingError
    assert isinstance(cut.args[0], ProtocolError), 'not a body cut as requests reports one'

    assert isinstance(outcome, BarsFailure), f'expected a BarsFailure, got {outcome!r}'
    error = outcome.error
    assert type(error) is ExogenousError
    assert error.reason is Reason.VENDOR_UNAVAILABLE
    assert REASONS[error.reason].outcome is Outcome.NOT_READY
    # The returned form of 'raise ... from e': the very instance the SDK raised (D8, item 5).
    assert error.__cause__ is cut
    # Built as a connection error is: the same sentence and the same metadata, and no delay.
    reference = connection_error_classified()
    assert error.detail == reference.detail
    assert dict(error.metadata) == dict(reference.metadata) == {'vendor': 'ALPACA'}
    assert error.reset_at is None
    # D8: nothing of the partial body or of the exception text reaches the caller.
    assert WHOLE_BODY[:SENT_BEFORE_THE_CUT].decode() not in error.detail
    assert str(cut) not in error.detail
    # One call reached the vendor and spent one token; the SDK did not retry the cut.
    assert budget.acquires == 1
    assert sdk_sleeps == []


@pytest.mark.asyncio
async def test_the_same_server_sending_the_whole_body_is_served(executor, budget, sdk_sleeps):
    """The control: the harness answers a real SDK request, so a red above is the cut and nothing else."""
    outcome, seen, spied = await get_bars_through(whole_answer(), executor)

    assert seen.request_head.startswith(f'GET {BARS_PATH}?'.encode())
    assert spied.raised == []
    assert isinstance(outcome, BarsResponse), f'expected SERVED, got {outcome!r}'
    closes = [bar.close async for bar in outcome.bars]
    assert closes == [bar['c'] for bar in RECORDED.bars]
    assert len(closes) == 3
    assert budget.acquires == 1
    assert sdk_sleeps == []
