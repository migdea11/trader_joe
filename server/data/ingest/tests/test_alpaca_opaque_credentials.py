"""get_client wraps the CHECKED Alpaca key and secret in OpaqueStr, and the vendor still receives the real bytes.

Bead tj-7zjov1.73 against builder-ingest's 486216c (tj-7zjov1.72). The design: tj-7zjov1.72's description, the
ARCHITECT DESIGN item 4 on tj-7zjov1.67, and the owner rulings on tj-7zjov1.69 (R1 A OpaqueStr, R2 A get_client
only, header mode pinned by a test). OpaqueStr's contract: common/sensitive.py, layer M3.

WHAT IS PINNED, AND WHY EACH ONE MATTERS ON A CREDENTIAL PATH:
1. The client stores OpaqueStr for both values, and every rendering of them (repr, str, format, an f-string, a log
   record, the auth-header dict) shows the marker. Without the wrap these are plain str and render the secret.
2. The bytes on the wire are the REAL values, exactly, in both APCA headers, and there is no Authorization header.
   Driven through a real alpaca-py client built by the unpatched production get_client, against a loopback server,
   both through request_page (the paged bars read) and through the SDK's own request path (the latest-bar call).
   A str() or format of the wrapper anywhere on the way would send the marker; use_basic_auth would send a Basic
   header built from the marker.
3. Order: read, check, then wrap, from exactly the checked objects. Wrapping first would hand the check a value
   whose str() is the marker, which is how a malformed value could be hidden from it.
4. A malformed value raises MalformedCredentialsError from None, with no client built and nothing wrapped.
5. Static scans of the ingest source: no use_basic_auth anywhere, no reveal anywhere.
6. Neither an HTTP error from the vendor, nor its traceback, nor a repr of the client or its prepared request
   headers, puts the value into a log line or an exception text.

Made-up key and secret; the only network is a loopback server started in the test.
"""

import ast
import contextlib
import logging
import threading
import traceback
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
from pathlib import Path
from unittest.mock import patch

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestBarRequest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import Feed, Granularity
from common.sensitive import REDACTED, OpaqueStr
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.broker_errors import MalformedCredentialsError
from data.ingest.app.brokers.interface import BarsQuery, BarsResponse, Instrument
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler.credentials import CredentialShapeError, check_credential_shape
from data.ingest.tests.scheduler_doubles import RecordingScheduler, alpaca_reader


pytestmark = pytest.mark.data_ingest

KEY = 'PKOPAQUEVALIDATORKEY5M8T'
SECRET = 'opaqueValidatorMadeUpSecret3Jq7Vd'
KEY_VAR, SECRET_VAR = 'ALPACA_API_KEY', 'ALPACA_API_SECRET'
GUARD_S = 10.0
START = datetime(2024, 1, 2, tzinfo=UTC)
END = datetime(2024, 1, 3, tzinfo=UTC)
LATEST_BAR_BODY = b'{"bars":{"VFV":{"t":"2024-01-02T14:30:00Z","o":1.0,"h":1.0,"l":1.0,"c":1.0,"v":1,"n":1,"vw":1.0}}}'
BARS_BODY = b'{"bars":{},"next_page_token":null}'

INGEST_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = (INGEST_ROOT / 'app', INGEST_ROOT.parents[1] / 'routers' / 'data_ingest')


def leaked_forms() -> list[str]:
    """Every rendering of the pair whose presence in text is a leak."""
    forms = []
    for value in (KEY, SECRET):
        forms += [value, repr(value), repr(value.encode()), value.encode().hex()]
    return forms


def assert_no_leak(text: str, where: str) -> None:
    for form in leaked_forms():
        assert form not in text, f'{where} carries a credential ({len(form)}-character form)'


# ---------------------------------------------------------------------------------------------
# The loopback vendor
# ---------------------------------------------------------------------------------------------


class LoopbackVendor(ThreadingHTTPServer):
    """A 127.0.0.1 server recording every request's path and full header list as the raw bytes on the wire."""

    daemon_threads = True

    def __init__(self) -> None:
        self.requests: list[tuple[str, list[tuple[str, bytes]]]] = []
        self.status = 200
        self.lock = threading.Lock()
        super().__init__(('127.0.0.1', 0), _Handler)

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.server_address[1]}'

    def header(self, index: int, name: str) -> list[bytes]:
        return [value for key, value in self.requests[index][1] if key.lower() == name.lower()]


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        # http.server decodes header values as latin-1, so encoding back gives the exact bytes on the wire.
        headers = [(name, value.encode('latin-1')) for name, value in self.headers.items()]
        with self.server.lock:
            self.server.requests.append((self.path, headers))
        if self.server.status != 200:
            body = b'{"message":"forbidden by the loopback"}'
        elif self.path.startswith('/v2/stocks/bars/latest'):
            body = LATEST_BAR_BODY
        else:
            body = BARS_BODY
        self.send_response(self.server.status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def vendor() -> Iterator[LoopbackVendor]:
    server = LoopbackVendor()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(GUARD_S)


@pytest.fixture(autouse=True)
def clean_process_state(monkeypatch) -> Iterator[None]:
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.delenv('ALPACA_SIP_ENABLED', raising=False)
    monkeypatch.setenv(KEY_VAR, KEY)
    monkeypatch.setenv(SECRET_VAR, SECRET)
    broker_api.set_client(None)
    yield
    broker_api.set_client(None)


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield executor


def production_client(vendor: LoopbackVendor) -> StockHistoricalDataClient:
    """The client the UNPATCHED get_client builds and caches, pointed at the loopback after construction.

    Nothing about the constructor call is substituted, so the arguments get_client passes are the ones in use.
    """
    client = broker_api.get_client()
    assert type(client) is StockHistoricalDataClient
    client._base_url = vendor.url
    return client


def bars_query() -> BarsQuery:
    return BarsQuery(
        instrument=Instrument('VFV', AssetType.STOCK),
        granularity=Granularity.ONE_DAY,
        start=START,
        end=END,
        priority=RequestPriority.INTERACTIVE,
    )


# ---------------------------------------------------------------------------------------------
# Log capture
# ---------------------------------------------------------------------------------------------


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        # The handler sits on every logger, so a propagating record reaches it more than once.
        if all(seen is not record for seen in self.records):
            self.records.append(record)


def record_text(record: logging.LogRecord) -> str:
    parts = [record.name, str(record.msg), repr(record.args)]
    with contextlib.suppress(Exception):
        parts.append(record.getMessage())
    with contextlib.suppress(Exception):
        parts.append(logging.Formatter('%(message)s').format(record))
    if record.exc_info and record.exc_info[1] is not None:
        parts.append(''.join(traceback.format_exception(*record.exc_info)))
    if record.exc_text:
        parts.append(record.exc_text)
    return '\n'.join(parts)


@pytest.fixture
def all_logs() -> Iterator[_Collect]:
    """A handler on the root and on every existing logger, all at DEBUG and enabled, restored afterwards."""
    handler = _Collect()
    for name in ('requests', 'urllib3', 'urllib3.connectionpool', 'alpaca', 'test.opaque'):
        logging.getLogger(name)
    loggers = [logging.getLogger()] + [
        logger for logger in list(logging.root.manager.loggerDict.values()) if isinstance(logger, logging.Logger)
    ]
    saved = [(logger, logger.level, logger.disabled) for logger in loggers]
    saved_disable = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
        logger.disabled = False
        logger.addHandler(handler)
    try:
        yield handler
    finally:
        for logger, level, disabled in saved:
            logger.removeHandler(handler)
            logger.setLevel(level)
            logger.disabled = disabled
        logging.disable(saved_disable)


def assert_logs_clean(logs: _Collect) -> None:
    for record in logs.records:
        assert_no_leak(record_text(record), where=f'a {record.levelname} record from {record.name!r}')


# ---------------------------------------------------------------------------------------------
# 1. What the client stores, and how it renders
# ---------------------------------------------------------------------------------------------


def test_get_client_stores_both_credentials_as_opaque_str_holding_the_real_values():
    client = broker_api.get_client()

    for stored, real in ((client._api_key, KEY), (client._secret_key, SECRET)):
        assert type(stored) is OpaqueStr, f'the client holds a {type(stored).__name__}, not an OpaqueStr'
        assert stored.reveal() == real
        assert type(stored.reveal()) is str
        assert stored == real
        assert repr(stored) == REDACTED
        assert str(stored) == REDACTED
        assert format(stored) == REDACTED
        assert f'{stored}|{stored!r}|{stored!s}|{stored:>40}'.count(REDACTED) == 4
        assert '%s %r' % (stored, stored) == f'{REDACTED} {REDACTED}'  # noqa: UP031 - percent-format is the case


def test_the_auth_headers_the_client_builds_carry_the_wrapper_and_render_the_marker(all_logs: _Collect):
    client = broker_api.get_client()
    headers = client._get_default_headers()

    assert type(headers['APCA-API-KEY-ID']) is OpaqueStr
    assert type(headers['APCA-API-SECRET-KEY']) is OpaqueStr
    assert 'Authorization' not in headers
    rendered = '\n'.join([repr(headers), str(headers), f'{headers}', repr(vars(client)), repr(client)])
    assert_no_leak(rendered, where='a rendering of the client or its auth headers')
    assert rendered.count(REDACTED) >= 4

    log = logging.getLogger('test.opaque')
    log.info('headers %s', headers)
    log.info('client %r and its state %r', client, vars(client))
    log.info(f'f-string headers {headers}')
    assert len(all_logs.records) == 3
    assert all(REDACTED in record.getMessage() for record in all_logs.records)
    assert_logs_clean(all_logs)


# ---------------------------------------------------------------------------------------------
# 2. The bytes on the wire are the real ones, header mode only
# ---------------------------------------------------------------------------------------------


def assert_real_bytes_in_header_mode(vendor: LoopbackVendor, index: int) -> None:
    assert vendor.header(index, 'APCA-API-KEY-ID') == [KEY.encode('ascii')], 'the key on the wire is not the real one'
    assert vendor.header(index, 'APCA-API-SECRET-KEY') == [SECRET.encode('ascii')], (
        'the secret on the wire is not the real one'
    )
    assert vendor.header(index, 'Authorization') == [], 'an Authorization header was sent: not header mode'
    sent = b'\n'.join(value for _, value in vendor.requests[index][1])
    assert REDACTED.encode() not in sent, 'the marker went on the wire'


def test_request_page_sends_the_exact_real_bytes_in_both_credential_headers(vendor: LoopbackVendor):
    client = production_client(vendor)

    page = broker_api.request_page(client, broker_api.BARS_PATH, {'symbols': 'VFV', 'timeframe': '1Day'})

    assert page.body == {'bars': {}, 'next_page_token': None}
    assert len(vendor.requests) == 1
    assert vendor.requests[0][0].startswith('/v2/stocks/bars?')
    assert_real_bytes_in_header_mode(vendor, 0)


@pytest.mark.asyncio
async def test_the_production_bars_read_with_no_injected_client_sends_the_real_bytes(vendor: LoopbackVendor, pool):
    production_client(vendor)
    scheduler = RecordingScheduler()

    outcome = await alpaca_reader(None, pool, scheduler=scheduler).get_bars(bars_query())

    assert isinstance(outcome, BarsResponse), f'the read failed: {outcome!r}'
    assert len(vendor.requests) == 1
    assert_real_bytes_in_header_mode(vendor, 0)


@pytest.mark.asyncio
async def test_the_sdk_request_path_sends_the_exact_real_bytes(vendor: LoopbackVendor, pool):
    # The non-paged data types still go through alpaca-py's own _request (fetch_data_type's second branch).
    production_client(vendor)
    scheduler = RecordingScheduler()

    latest = await broker_api.fetch_data_type(
        pool,
        bars_query(),
        DataType.MARKET_ACTIVITY,
        True,
        {'symbol_or_symbols': 'VFV'},
        Feed.IEX,
        None,
        scheduler=scheduler,
        scope='S',
    )

    assert set(latest) == {'VFV'}
    assert len(vendor.requests) == 1
    assert vendor.requests[0][0].startswith('/v2/stocks/bars/latest')
    assert_real_bytes_in_header_mode(vendor, 0)


# ---------------------------------------------------------------------------------------------
# 3. Read, check, then wrap, from exactly the checked objects
# ---------------------------------------------------------------------------------------------


def test_get_client_checks_plain_values_then_wraps_exactly_those_and_builds_from_the_wrappers():
    # Every read answers a distinct well-formed value from one counter, so a wrap or a build from any read other than
    # the checked one is visible. Identity, not equality, ties the wrap to the checked objects: OpaqueStr compares
    # equal to its plain value, so equality could not tell a second read of the same text apart.
    events: list[tuple] = []
    base = {KEY_VAR: KEY, SECRET_VAR: SECRET}
    reads = count(1)
    wrapped: list[OpaqueStr] = []
    built: list[tuple[tuple, dict]] = []

    def read(name, *args, **kwargs):
        value = f'{base[name]}x{next(reads)}' if name in base else None
        events.append(('read', name))
        return value

    def check(credential):
        events.append(('check', credential.key_id, credential.secret))
        return check_credential_shape(credential)

    def wrap(value):
        events.append(('wrap', value))
        result = OpaqueStr(value)
        wrapped.append(result)
        return result

    def build(*args, **kwargs):
        events.append(('build',))
        built.append((args, kwargs))
        return StockHistoricalDataClient(*args, **kwargs)

    with (
        patch.object(broker_api, 'get_env_var', side_effect=read),
        patch.object(broker_api, 'check_credential_shape', side_effect=check),
        patch.object(broker_api, 'OpaqueStr', side_effect=wrap),
        patch.object(broker_api, 'StockHistoricalDataClient', side_effect=build),
    ):
        client = broker_api.get_client()

    kinds = [event[0] for event in events]
    assert kinds.count('check') == 1
    assert kinds.count('wrap') == 2
    assert kinds.count('build') == 1
    check_at = kinds.index('check')
    assert all(kind == 'read' for kind in kinds[:check_at]), 'something ran before the check other than the reads'
    assert kinds[check_at + 1 :] == ['wrap', 'wrap', 'build'], f'the order after the check is {kinds[check_at + 1 :]}'

    _, checked_key, checked_secret = events[check_at]
    assert type(checked_key) is str and type(checked_secret) is str, 'the check was handed a wrapper, not the value'
    assert events[check_at + 1][1] is checked_key, 'the key wrapped is not the object that was checked'
    assert events[check_at + 2][1] is checked_secret, 'the secret wrapped is not the object that was checked'

    ((args, kwargs),) = built
    assert args == tuple(wrapped) and all(a is w for a, w in zip(args, wrapped, strict=True)), (
        'the client was built from something other than the two wrappers'
    )
    assert kwargs == {}, f'get_client passed keyword arguments to the client: {sorted(kwargs)}'
    assert client._api_key.reveal() == checked_key
    assert client._secret_key.reveal() == checked_secret


# ---------------------------------------------------------------------------------------------
# 4. A malformed value: refused, from None, nothing built, nothing wrapped
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize('which', ('key', 'secret'))
@pytest.mark.parametrize(
    'damage',
    [lambda v: v + '\r', lambda v: ' ' + v, lambda v: v[:4] + '\x7f' + v[4:]],
    ids=['trailing-cr', 'leading-space', 'del-0x7f'],
)
def test_a_malformed_value_is_refused_from_none_before_anything_is_wrapped_or_built(which, damage, monkeypatch):
    key_id, secret = (damage(KEY), SECRET) if which == 'key' else (KEY, damage(SECRET))
    monkeypatch.setenv(KEY_VAR, key_id)
    monkeypatch.setenv(SECRET_VAR, secret)
    wraps: list[str] = []
    builds: list[tuple] = []

    with (
        patch.object(broker_api, 'OpaqueStr', side_effect=lambda v: wraps.append(v) or OpaqueStr(v)),
        patch.object(broker_api, 'StockHistoricalDataClient', side_effect=lambda *a, **k: builds.append(a)),
        pytest.raises(MalformedCredentialsError) as raised,
    ):
        broker_api.get_client()

    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert type(raised.value.__context__) is CredentialShapeError
    assert wraps == [], 'a malformed value was wrapped'
    assert builds == [], 'a client was built from a malformed value'
    assert getattr(broker_api, '__CLIENT') is None
    text = '\n'.join([str(raised.value), repr(raised.value), ''.join(traceback.format_exception(raised.value))])
    for value in (key_id, secret):
        assert value not in text and repr(value) not in text


# ---------------------------------------------------------------------------------------------
# 5. Static scans of the ingest source
# ---------------------------------------------------------------------------------------------


def _ingest_sources() -> list[Path]:
    files = sorted(path for root in SOURCE_DIRS for path in root.rglob('*.py'))
    return [path for path in files if 'tests' not in path.relative_to(INGEST_ROOT.parents[1]).parts]


def _basic_auth_sites(tree: ast.AST) -> list[int]:
    """Lines naming use_basic_auth in code: a keyword argument, a name, an attribute or a string (a **{} splat)."""
    names = ('use_basic_auth', '_use_basic_auth')
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == 'use_basic_auth':
            sites.append(node.value.lineno)
        elif (
            (isinstance(node, ast.Name) and node.id in names)
            or (isinstance(node, ast.Attribute) and node.attr in names)
            or (isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip() in names)
        ):
            sites.append(node.lineno)
    return sites


def _reveal_sites(tree: ast.AST) -> list[int]:
    """Lines that reach reveal in code: an attribute, a bare name, or a getattr string."""
    sites = []
    for node in ast.walk(tree):
        if (
            (isinstance(node, ast.Attribute) and node.attr == 'reveal')
            or (isinstance(node, ast.Name) and node.id == 'reveal')
            or (isinstance(node, ast.Constant) and node.value == 'reveal')
        ):
            sites.append(node.lineno)
    return sites


def test_the_scans_are_live_on_planted_source():
    planted_basic = ast.parse(
        'C(k, s, use_basic_auth=True)\nC(k, s, **{"use_basic_auth": False})\nc._use_basic_auth = True\n'
    )
    planted_reveal = ast.parse('C(k.reveal(), s)\nf = s.reveal\ngetattr(s, "reveal")()\n')

    assert sorted(set(_basic_auth_sites(planted_basic))) == [1, 2, 3]
    assert sorted(_reveal_sites(planted_reveal)) == [1, 2, 3]
    sources = _ingest_sources()
    assert INGEST_ROOT / 'app' / 'brokers' / 'alpaca' / 'broker_api.py' in sources
    assert any(path.parent.name == 'data_ingest' for path in sources), 'the router sources were not scanned'
    assert not any('tests' in path.parts[-3:] for path in sources)


def test_no_ingest_source_names_use_basic_auth():
    found = {
        str(path.relative_to(INGEST_ROOT.parents[1])): lines
        for path in _ingest_sources()
        if (lines := _basic_auth_sites(ast.parse(path.read_text(), filename=str(path))))
    }

    assert found == {}, f'use_basic_auth formats the key into a string and would send the marker: {found}'


def test_no_ingest_source_calls_reveal():
    found = {
        str(path.relative_to(INGEST_ROOT.parents[1])): lines
        for path in _ingest_sources()
        if (lines := _reveal_sites(ast.parse(path.read_text(), filename=str(path))))
    }

    assert found == {}, f'reveal() unwraps the credential where nothing needs it: {found}'


def test_the_built_client_is_in_header_mode():
    client = broker_api.get_client()

    assert client._use_basic_auth is False
    assert client._oauth_token is None


# ---------------------------------------------------------------------------------------------
# 6. An HTTP error, raised and logged, never renders the value
# ---------------------------------------------------------------------------------------------


def _error_text(error: BaseException) -> str:
    response = getattr(error, 'response', None)
    request = getattr(response, 'request', None)
    return '\n'.join(
        [
            str(error),
            repr(error),
            repr(error.args),
            ''.join(traceback.format_exception(error)),
            repr(getattr(request, 'headers', None)),
            str(getattr(request, 'headers', None)),
        ]
    )


def test_a_vendor_error_through_request_page_logged_with_its_traceback_renders_no_value(
    vendor: LoopbackVendor, all_logs: _Collect
):
    vendor.status = 403
    client = production_client(vendor)
    log = logging.getLogger('test.opaque')

    with pytest.raises(APIError) as raised:
        broker_api.request_page(client, broker_api.BARS_PATH, {'symbols': 'VFV'})
    try:
        raise raised.value
    except APIError:
        log.exception('the vendor refused: %r', raised.value)

    # The request that failed still carried the real bytes: the wrapper changed nothing on the wire.
    assert_real_bytes_in_header_mode(vendor, 0)
    request_headers = raised.value.response.request.headers
    assert type(request_headers['APCA-API-SECRET-KEY']) is OpaqueStr
    assert_no_leak(_error_text(raised.value), where='the APIError, its traceback or its prepared request headers')
    assert any(record.exc_info for record in all_logs.records), 'the error was not logged with its traceback'
    assert_logs_clean(all_logs)


def test_a_vendor_error_through_the_sdk_path_logged_with_its_traceback_renders_no_value(
    vendor: LoopbackVendor, all_logs: _Collect
):
    vendor.status = 403
    client = production_client(vendor)
    log = logging.getLogger('test.opaque')

    with pytest.raises(APIError) as raised:
        client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols='VFV'))
    try:
        raise raised.value
    except APIError:
        log.exception('the vendor refused: %s', raised.value)
    log.error('client was %r with %r', client, vars(client))

    assert len(vendor.requests) == 1
    assert_real_bytes_in_header_mode(vendor, 0)
    assert_no_leak(_error_text(raised.value), where='the SDK APIError, its traceback or its prepared request headers')
    assert_logs_clean(all_logs)


def test_the_loopback_capture_is_live():
    # The control: a plain-str header DOES render the value, so the clean results above are not a dead check.
    plain = {'APCA-API-SECRET-KEY': SECRET}

    with pytest.raises(AssertionError):
        assert_no_leak(repr(plain), where='control')
