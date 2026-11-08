"""A malformed Alpaca credential is refused in get_client, before the SDK client exists, and never logged.

Bead tj-7zjov1.68 against builder-ingest's d640a91 (tj-7zjov1.67). The design: tj-7zjov1.67's description and
acceptance, the architect ruling of 15:55 UTC on it (MalformedCredentialsError is a sibling ExogenousError leaf with
reason VENDOR_AUTH, built from variable NAMES, raised from None; read.py catches both leaves by name; the
attempt-level retry is kept and costs nothing) and its addendum 1 of 16:30 UTC (validate only, no RedactedStr).

THE LEAK THIS GUARDS. Before d640a91 get_client built StockHistoricalDataClient straight from the environment. A key
or secret with a trailing CR, a trailing LF or a leading space made requests raise InvalidHeader whose text ends in
the repr of the value; InvalidHeader is a bug to classify.py (ADR tj-fa1rpu D6), so AlpacaRead re-raised it and the
executor's log.exception printed the secret at ERROR. Every other malformed shape went on the wire.

HOW IT IS DRIVEN. The vendor is a loopback HTTP server started here, which counts every TCP connection it accepts
and records the raw credential header bytes of every request. The client is alpaca-py's REAL
StockHistoricalDataClient, built by the production get_client: the only substitution is the name get_client
constructs, replaced by a recorder that builds the real class with url_override pointed at the loopback server. So
"no connection" is observed at the socket, and "never built" is observed at the constructor. Logs are captured with
a handler attached to the root logger and to every logger that exists (requests, urllib3, alpaca and the ingest
loggers named explicitly), each set to DEBUG, so a record dropped by a level or a non-propagating logger still lands.

A value is LEAKED when its distinctive core appears in a record's message, args, formatted exc_info, exc_text or
stack, raw, as a repr, or as hex bytes. Every malformed value here is built around a made-up core, so the core is the
secret: an escaped or bytes rendering of the value still contains it.
"""

import asyncio
import contextlib
import logging
import os
import threading
import traceback
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
from unittest.mock import patch

import pytest
from alpaca.data.historical import StockHistoricalDataClient
from requests.exceptions import InvalidHeader
from requests.utils import check_header_validity

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import ExogenousError, Reason, has_cause_chain
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.handle import ALPACA_DATA_SCOPE
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.bars_attempt import BarsAttemptRunner, BarsWork, error_class
from data.ingest.app.brokers.broker_errors import MalformedCredentialsError, MissingCredentialsError
from data.ingest.app.brokers.interface import BarsFailure, BarsQuery, BarsResponse, Instrument
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler import (
    AckGatedExecutor,
    Credential,
    ExecutorSettings,
    Limit,
    RetryPolicy,
    ScheduledRequest,
    SchedulerBuilder,
    StaticCredentialProvider,
)
from data.ingest.app.scheduler.credentials import CredentialShapeError, check_credential_shape
from data.ingest.tests.scheduler_doubles import RecordingScheduler, alpaca_reader
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    DeliverAttemptResponse,
    DeliveryErrorClass,
    DeliveryOutcome,
    DeliveryVerdict,
)
from schemas.data_ingest.submit import BarsRequest


pytestmark = pytest.mark.data_ingest

# Made-up, distinctive cores. Neither is a real credential; each is long enough that a chance match is impossible.
KEY = 'PKVALIDATORMADEUPKEY7Q3Z'
SECRET = 'validatorMadeUpSecret9w4Rk2Lx'
KEY_VAR, SECRET_VAR = 'ALPACA_API_KEY', 'ALPACA_API_SECRET'
MESSAGE = 'Vendor credentials are malformed: ALPACA_API_KEY and ALPACA_API_SECRET must each be non-empty visible ASCII'
START = datetime(2024, 1, 2, tzinfo=UTC)
END = datetime(2024, 1, 3, tzinfo=UTC)
# Every wait for the code under test is bounded, so a regression fails rather than hangs.
GUARD_S = 10.0
# The executor's stall-alarm interval; the sleep double parks exactly this length so the alarm never runs.
WATCH_S = 7777.0
LOGGERS = (
    '',
    'requests',
    'urllib3',
    'urllib3.connectionpool',
    'alpaca',
    'data.ingest.app.brokers.alpaca.read',
    'data.ingest.app.brokers.alpaca.broker_api',
    'data.ingest.app.scheduler.executor',
    'data.ingest.app.scheduler.scheduler',
)


def _insert(value: str, char: str) -> str:
    return value[:4] + char + value[4:]


# Each malformed shape, as a function of the well-formed core it damages. ENV_SHAPES can be carried by the
# environment; NUL cannot (os.environ refuses it), and an empty value is a MISSING credential to get_client, so both
# are driven at the predicate and, for NUL, through get_client with the read patched.
ENV_SHAPES: dict[str, Callable[[str], str]] = {
    'trailing-cr': lambda v: v + '\r',
    'trailing-lf': lambda v: v + '\n',
    'crlf-plus-header': lambda v: v + '\r\nX-Injected: 1',
    'leading-space': lambda v: ' ' + v,
    'trailing-space': lambda v: v + ' ',
    'inner-space': lambda v: _insert(v, ' '),
    'tab': lambda v: _insert(v, '\t'),
    'control-0x01': lambda v: _insert(v, '\x01'),
    'del-0x7f': lambda v: _insert(v, '\x7f'),
    'non-ascii-letter': lambda v: _insert(v, 'é'),
}
ALL_SHAPES: dict[str, Callable[[str], str]] = {**ENV_SHAPES, 'empty': lambda v: '', 'nul': lambda v: _insert(v, '\x00')}
# The three shapes requests' header check refuses with the value in its text: the original leak.
LEAKY_SHAPES = ('trailing-cr', 'trailing-lf', 'leading-space')
WHICH = ('key', 'secret')


def malformed_pair(shape: str, which: str) -> tuple[str, str]:
    """A (key, secret) pair with exactly one of the two damaged by the named shape."""
    damage = ALL_SHAPES[shape]
    return (damage(KEY), SECRET) if which == 'key' else (KEY, damage(SECRET))


def leaked_forms(*values: str) -> list[str]:
    """Every rendering of a credential whose presence in log text is a leak: the cores, raw, repr'd and as hex."""
    forms = [KEY, SECRET, KEY.encode().hex(), SECRET.encode().hex()]
    for value in values:
        if value:
            forms += [value, repr(value), repr(value.encode('utf-8')), value.encode('utf-8').hex()]
    return forms


def assert_no_leak(text: str, *values: str, where: str) -> None:
    for form in leaked_forms(*values):
        assert form not in text, f'{where} carries a credential ({len(form)}-character form)'


# ---------------------------------------------------------------------------------------------
# The loopback vendor
# ---------------------------------------------------------------------------------------------


class LoopbackVendor(ThreadingHTTPServer):
    """An HTTP server on 127.0.0.1 that counts every TCP connection and records each request's credential headers."""

    daemon_threads = True

    def __init__(self) -> None:
        self.connections = 0
        self.requests: list[tuple[bytes | None, bytes | None]] = []
        self.lock = threading.Lock()
        super().__init__(('127.0.0.1', 0), _VendorHandler)

    def verify_request(self, request, client_address) -> bool:
        # Called once per accepted connection, before any byte of the request is parsed.
        with self.lock:
            self.connections += 1
        return True

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.server_address[1]}'


class _VendorHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        def raw(name: str) -> bytes | None:
            # http.server decodes header values as latin-1, so encoding back gives the bytes on the wire.
            value = self.headers.get(name)
            return None if value is None else value.encode('latin-1')

        with self.server.lock:
            self.server.requests.append((raw('APCA-API-KEY-ID'), raw('APCA-API-SECRET-KEY')))
        body = b'{"bars":{},"next_page_token":null}'
        self.send_response(200)
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


@dataclass
class ClientRecorder:
    """Stands in for the NAME get_client constructs, and builds the real alpaca-py client against the loopback."""

    url: str | None
    built: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, *args, **kwargs) -> StockHistoricalDataClient:
        self.built.append(args)
        if self.url is None:
            raise AssertionError('the SDK client was constructed')
        return StockHistoricalDataClient(*args, **kwargs, url_override=self.url)


@pytest.fixture(autouse=True)
def clean_process_state(monkeypatch) -> Iterator[None]:
    """No cached client either side of a test, no proxy between the client and the loopback, an IEX deployment."""
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.delenv('ALPACA_SIP_ENABLED', raising=False)
    monkeypatch.delenv(KEY_VAR, raising=False)
    monkeypatch.delenv(SECRET_VAR, raising=False)
    broker_api.set_client(None)
    yield
    broker_api.set_client(None)


@pytest.fixture
def recorder(vendor: LoopbackVendor) -> Iterator[ClientRecorder]:
    rec = ClientRecorder(vendor.url)
    with patch.object(broker_api, 'StockHistoricalDataClient', rec):
        yield rec


def cached_client():
    # Module-level double-underscore names are not mangled; test_broker_api.py reads __SINGLE_FLIGHT the same way.
    return getattr(broker_api, '__CLIENT')


# ---------------------------------------------------------------------------------------------
# Log capture at every level on every logger
# ---------------------------------------------------------------------------------------------


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.__seen: set[int] = set()

    def emit(self, record: logging.LogRecord) -> None:
        if id(record) not in self.__seen:
            self.__seen.add(id(record))
            self.records.append(record)


def record_text(record: logging.LogRecord) -> str:
    parts = [record.name, str(record.msg), repr(record.args)]
    # A record whose format fails is still searched by its parts.
    with contextlib.suppress(Exception):
        parts.append(record.getMessage())
    if record.exc_info and record.exc_info[1] is not None:
        parts.append(''.join(traceback.format_exception(*record.exc_info)))
    if record.exc_text:
        parts.append(record.exc_text)
    if record.stack_info:
        parts.append(record.stack_info)
    return '\n'.join(parts)


@pytest.fixture
def all_logs() -> Iterator[_Collect]:
    handler = _Collect()
    manager = logging.root.manager
    for name in LOGGERS:
        logging.getLogger(name)
    loggers = [logging.getLogger()] + [
        logger for logger in list(manager.loggerDict.values()) if isinstance(logger, logging.Logger)
    ]
    saved = [(logger, logger.level, logger.disabled) for logger in loggers]
    saved_disable = manager.disable
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


def assert_logs_clean(logs: _Collect, *values: str) -> None:
    for record in logs.records:
        assert_no_leak(record_text(record), *values, where=f'a {record.levelname} record from {record.name!r}')


def assert_error_clean(error: BaseException, *values: str) -> None:
    text = '\n'.join(
        [
            str(error),
            repr(error),
            repr(error.args),
            ''.join(traceback.format_exception(error)),
            repr(error.__context__),
            repr(getattr(error.__context__, 'args', None)),
        ]
    )
    assert_no_leak(text, *values, where='the error, its args, repr, traceback or context')


def assert_is_the_malformed_refusal(error: BaseException) -> None:
    """The 15:55 ruling, items 1 to 3: the class, the reason, the one sentence, and no cause chain."""
    assert type(error) is MalformedCredentialsError
    assert isinstance(error, ExogenousError)
    assert not isinstance(error, MissingCredentialsError), 'a malformed credential was reported as a missing one'
    assert error.reason is Reason.VENDOR_AUTH
    assert error.detail == MESSAGE
    assert str(error) == f'VENDOR_AUTH: {MESSAGE}'
    assert error.args == (Reason.VENDOR_AUTH, MESSAGE)
    assert not error.metadata
    assert error.reset_at is None
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert has_cause_chain(error) is False
    # The context is the predicate's refusal, which carries nothing.
    assert type(error.__context__) is CredentialShapeError
    assert error.__context__.args == ()


def bars_query() -> BarsQuery:
    return BarsQuery(
        instrument=Instrument('VFV', AssetType.STOCK),
        granularity=Granularity.ONE_DAY,
        start=START,
        end=END,
        priority=RequestPriority.INTERACTIVE,
    )


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield executor


# ---------------------------------------------------------------------------------------------
# 1. The predicate
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize('value', [KEY, SECRET, '!', '~', '!~', ''.join(chr(c) for c in range(0x21, 0x7F))])
def test_the_predicate_accepts_visible_ascii_including_its_two_ends(value: str):
    check_credential_shape(Credential(key_id=value, secret=value))


@pytest.mark.parametrize('which', WHICH)
@pytest.mark.parametrize('shape', ALL_SHAPES)
def test_the_predicate_refuses_each_malformed_shape_on_either_value_with_an_error_carrying_nothing(shape, which):
    key_id, secret = malformed_pair(shape, which)

    with pytest.raises(CredentialShapeError) as raised:
        check_credential_shape(Credential(key_id=key_id, secret=secret))

    assert raised.value.args == ()
    assert str(raised.value) == ''
    assert raised.value.__cause__ is None
    assert_error_clean(raised.value, key_id, secret)


def test_the_predicate_admits_exactly_0x21_to_0x7e_as_single_characters():
    accepted = []
    for code in [*range(0x00, 0x250), 0x2028, 0xFEFF, 0x10FFFF]:
        try:
            check_credential_shape(Credential(key_id=chr(code), secret='x'))
        except CredentialShapeError:
            continue
        accepted.append(code)

    assert accepted == list(range(0x21, 0x7F))


def _requests_refuses(value: str) -> bool:
    try:
        check_header_validity(('APCA-API-SECRET-KEY', value))
    except InvalidHeader:
        return True
    return False


def _candidates() -> list[str]:
    chars = [chr(c) for c in [*range(0x00, 0x250), 0x2028, 0x2029, 0x0085, 0xFEFF]]
    values = ['']
    for char in chars:
        values += [char, KEY + char, char + KEY, _insert(KEY, char), KEY + '\r\n' + char]
    return values


def test_the_predicate_refuses_everything_requests_header_check_refuses_and_more():
    """Addendum 1 (a): the property that makes a RedactedStr wrapper dead code behind the check.

    The refused set is DERIVED from the installed requests' own check_header_validity at test time, not written out,
    so a requests upgrade that refuses more is held to it automatically. Every value requests refuses (and would
    quote in its InvalidHeader text) must be refused by the predicate first.
    """
    candidates = _candidates()
    requests_refused = [value for value in candidates if _requests_refuses(value)]

    # The derivation is live: the three leaky shapes are among what requests refuses.
    for shape in LEAKY_SHAPES:
        assert _requests_refuses(ENV_SHAPES[shape](KEY)), f'requests no longer refuses {shape}; re-derive the leak'
    assert len(requests_refused) > 100

    let_through = []
    for value in requests_refused:
        for key_id, secret in ((value, SECRET), (KEY, value)):
            try:
                check_credential_shape(Credential(key_id=key_id, secret=secret))
            except CredentialShapeError:
                continue
            let_through.append(repr(value))
    assert let_through == [], 'the predicate admits a value requests would refuse and quote'


def test_the_predicate_is_strictly_stricter_than_requests_an_inner_space_is_the_witness():
    inner_space = _insert(SECRET, ' ')

    assert not _requests_refuses(inner_space), 'requests now refuses an inner space; the witness must change'
    with pytest.raises(CredentialShapeError):
        check_credential_shape(Credential(key_id=KEY, secret=inner_space))


# ---------------------------------------------------------------------------------------------
# 2. get_client
# ---------------------------------------------------------------------------------------------


def test_malformed_credentials_error_is_a_d5_leaf_directly_under_exogenous_error():
    """ADR tj-fa1rpu D5 as enforced at class definition: a leaf of the branch, a sibling of the missing leaf."""
    assert MalformedCredentialsError.__bases__ == (ExogenousError,)
    assert not issubclass(MalformedCredentialsError, MissingCredentialsError)
    assert not issubclass(MissingCredentialsError, MalformedCredentialsError)
    # Nothing subclasses a leaf: the vocabulary refuses it, which proves the class is registered as one.
    with pytest.raises(TypeError, match='D5'):
        type('UnderTheLeaf', (MalformedCredentialsError,), {})


def test_the_error_takes_names_and_builds_one_sentence_from_them():
    error = MalformedCredentialsError(('FIRST_VAR', 'SECOND_VAR'))

    assert (
        error.detail
        == 'Vendor credentials are malformed: FIRST_VAR and SECOND_VAR must each be non-empty visible ASCII'
    )


@pytest.mark.parametrize('which', WHICH)
@pytest.mark.parametrize('shape', ENV_SHAPES)
def test_get_client_refuses_a_malformed_environment_value_before_the_client_is_built(
    shape, which, monkeypatch, recorder: ClientRecorder
):
    key_id, secret = malformed_pair(shape, which)
    monkeypatch.setenv(KEY_VAR, key_id)
    monkeypatch.setenv(SECRET_VAR, secret)

    with pytest.raises(MalformedCredentialsError) as raised:
        broker_api.get_client()

    assert_is_the_malformed_refusal(raised.value)
    assert_error_clean(raised.value, key_id, secret)
    assert recorder.built == [], 'the SDK client was constructed from a malformed value'
    assert cached_client() is None

    # The refusal caches nothing: the corrected value builds on the next call, from exactly the values read.
    monkeypatch.setenv(KEY_VAR, KEY)
    monkeypatch.setenv(SECRET_VAR, SECRET)
    client = broker_api.get_client()
    assert recorder.built == [(KEY, SECRET)]
    assert cached_client() is client


@pytest.mark.parametrize('which', WHICH)
def test_get_client_refuses_a_nul_which_no_environment_can_carry(which, recorder: ClientRecorder):
    key_id, secret = malformed_pair('nul', which)
    values = {KEY_VAR: key_id, SECRET_VAR: secret}

    with (
        patch.object(broker_api, 'get_env_var', side_effect=lambda name, *a, **k: values.get(name)),
        pytest.raises(MalformedCredentialsError) as raised,
    ):
        broker_api.get_client()

    assert_is_the_malformed_refusal(raised.value)
    assert_error_clean(raised.value, key_id, secret)
    assert recorder.built == []


def test_get_client_builds_the_client_from_exactly_the_pair_it_checked(recorder: ClientRecorder):
    # Check-then-use (ruling of 15:55 UTC on tj-7zjov1.67, item 3; architect re-gate of 15:40 UTC on tj-7zjov1.68).
    # Every environment read answers a distinct well-formed value (the base plus a suffix from one shared counter), so
    # any two reads differ. A client built from a read other than the one checked, whether that read comes before
    # or after the check, then differs from the checked pair. This holds whatever the read count, so the missing
    # check may read the environment as often as it likes.
    checked: list[tuple[str, str]] = []
    base = {KEY_VAR: KEY, SECRET_VAR: SECRET}
    reads = count(1)

    def check(credential):
        checked.append((credential.key_id, credential.secret))
        return check_credential_shape(credential)

    def read(name, *args, **kwargs):
        return f'{base[name]}x{next(reads)}' if name in base else None

    with (
        patch.object(broker_api, 'get_env_var', side_effect=read),
        patch.object(broker_api, 'check_credential_shape', side_effect=check),
    ):
        client = broker_api.get_client()

    assert len(checked) == 1, 'the shape check did not run exactly once'
    assert recorder.built == checked, 'the client was built from a read other than the pair that was checked'
    assert cached_client() is client


@pytest.mark.parametrize('which', WHICH)
def test_an_empty_value_is_still_a_missing_credential_naming_that_variable(which, monkeypatch, recorder):
    monkeypatch.setenv(KEY_VAR, '' if which == 'key' else KEY)
    monkeypatch.setenv(SECRET_VAR, '' if which == 'secret' else SECRET)

    with pytest.raises(MissingCredentialsError) as raised:
        broker_api.get_client()

    assert type(raised.value) is MissingCredentialsError
    assert (KEY_VAR if which == 'key' else SECRET_VAR) in raised.value.detail
    assert recorder.built == []


def test_a_missing_value_is_reported_before_the_other_is_shape_checked(monkeypatch, recorder):
    # A malformed key beside an unset secret: the operator is told the secret is unset, and the key never appears.
    monkeypatch.setenv(KEY_VAR, KEY + '\r')

    with pytest.raises(MissingCredentialsError) as raised:
        broker_api.get_client()

    assert SECRET_VAR in raised.value.detail
    assert_error_clean(raised.value, KEY + '\r')
    assert recorder.built == []


@pytest.mark.asyncio
@pytest.mark.parametrize('which', WHICH)
async def test_fetch_data_types_default_client_path_refuses_before_any_token(which, monkeypatch, recorder, pool):
    """The second get_client call site (fetch_data_type with no client) refuses before scheduler.acquire."""
    key_id, secret = malformed_pair('trailing-cr', which)
    monkeypatch.setenv(KEY_VAR, key_id)
    monkeypatch.setenv(SECRET_VAR, secret)
    scheduler = RecordingScheduler()

    with pytest.raises(MalformedCredentialsError) as raised:
        await broker_api.fetch_data_type(
            pool, bars_query(), DataType.MARKET_ACTIVITY, False, {}, Feed.IEX, None, scheduler=scheduler, scope='S'
        )

    assert_is_the_malformed_refusal(raised.value)
    assert scheduler.acquires == []
    assert recorder.built == []


# ---------------------------------------------------------------------------------------------
# 3 and 4. End to end through the production AlpacaRead and the real alpaca-py client
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_a_well_formed_pair_reaches_the_vendor_byte_identical_and_the_capture_is_live(
    monkeypatch, vendor: LoopbackVendor, recorder: ClientRecorder, all_logs: _Collect, pool
):
    monkeypatch.setenv(KEY_VAR, KEY)
    monkeypatch.setenv(SECRET_VAR, SECRET)
    scheduler = RecordingScheduler()

    outcome = await alpaca_reader(None, pool, scheduler=scheduler).get_bars(bars_query())
    assert isinstance(outcome, BarsResponse), f'the control failed: {outcome!r}'
    assert [bar async for bar in outcome.bars] == []

    assert vendor.connections == 1
    assert vendor.requests == [(KEY.encode('ascii'), SECRET.encode('ascii'))]
    assert recorder.built == [(KEY, SECRET)]
    assert len(scheduler.acquires) == 1
    # The capture saw the HTTP layer's own DEBUG lines, so a silence in the malformed cases is not a dead handler.
    assert any(record.name.startswith('urllib3') for record in all_logs.records), 'urllib3 DEBUG was not captured'
    assert_logs_clean(all_logs)


@pytest.mark.asyncio
@pytest.mark.parametrize('which', WHICH)
@pytest.mark.parametrize('shape', ENV_SHAPES)
async def test_alpaca_read_returns_the_typed_refusal_with_no_connection_no_token_and_no_log_of_the_value(
    shape, which, monkeypatch, vendor: LoopbackVendor, recorder: ClientRecorder, all_logs: _Collect, pool
):
    key_id, secret = malformed_pair(shape, which)
    monkeypatch.setenv(KEY_VAR, key_id)
    monkeypatch.setenv(SECRET_VAR, secret)
    scheduler = RecordingScheduler()

    outcome = await alpaca_reader(None, pool, scheduler=scheduler).get_bars(bars_query())

    assert isinstance(outcome, BarsFailure), f'a malformed credential produced {type(outcome).__name__}'
    assert_is_the_malformed_refusal(outcome.error)
    assert_error_clean(outcome.error, key_id, secret)
    assert vendor.connections == 0, 'the vendor saw a connection made with a malformed credential'
    assert vendor.requests == []
    assert recorder.built == []
    assert scheduler.acquires == [], 'a scheduler token was spent on a credential that cannot be sent'
    assert_logs_clean(all_logs, key_id, secret)


@pytest.mark.asyncio
async def test_alpaca_read_still_returns_a_missing_credential_typed(monkeypatch, vendor, recorder, pool):
    monkeypatch.setenv(KEY_VAR, KEY)
    scheduler = RecordingScheduler()

    outcome = await alpaca_reader(None, pool, scheduler=scheduler).get_bars(bars_query())

    assert isinstance(outcome, BarsFailure)
    assert type(outcome.error) is MissingCredentialsError
    assert outcome.error.reason is Reason.VENDOR_AUTH
    assert SECRET_VAR in outcome.error.detail
    assert scheduler.acquires == []
    assert vendor.connections == 0


def test_the_attempt_gate_classes_the_refusal_transient_with_no_cause_chain():
    """Ruling item 6: VENDOR_AUTH is NOT_READY, so the attempt is TRANSIENT; no edge logs exc_info for it."""
    error = MalformedCredentialsError(broker_api.ALPACA_CREDENTIAL_VARS)

    assert error_class(error) is DeliveryErrorClass.TRANSIENT
    assert has_cause_chain(error) is False


# ---------------------------------------------------------------------------------------------
# The executor: every retry refused before a token or a call, the budget still spent
# ---------------------------------------------------------------------------------------------


class _AckingStore:
    """A StoreDeliveryClient that answers every report CONTINUE with the attempt's own number at once."""

    def __init__(self) -> None:
        self.received: list[AttemptDelivery] = []

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        self.received.append(attempt)
        return DeliverAttemptResponse(verdict=DeliveryVerdict.CONTINUE, attempt_count=attempt.header.attempt_no)

    async def register_instance(self, request):
        raise AssertionError('the executor never registers an instance')


async def _no_wait(seconds: float) -> None:
    if seconds == WATCH_S:
        await asyncio.Event().wait()
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize('which', WHICH)
@pytest.mark.parametrize('shape', LEAKY_SHAPES)
async def test_the_executor_retries_to_the_budget_with_no_token_no_call_and_no_traceback_of_the_value(
    shape, which, monkeypatch, vendor: LoopbackVendor, recorder: ClientRecorder, all_logs: _Collect, pool
):
    """The layer that leaked: the executor's log.exception for a runner that raised printed the InvalidHeader text.

    Driven through the production AckGatedExecutor on a real Scheduler, the production BarsAttemptRunner and the
    production AlpacaRead with no injected client. Ruling item 6, accepted retry behaviour: each attempt re-enters
    get_client and is refused before scheduler.acquire and before any HTTP, the attempt budget is still spent, and
    last_attempt is set on the last.
    """
    key_id, secret = malformed_pair(shape, which)
    monkeypatch.setenv(KEY_VAR, key_id)
    monkeypatch.setenv(SECRET_VAR, secret)
    finished: list[str] = []
    done = asyncio.Event()

    def on_finished(request: ScheduledRequest) -> None:
        finished.append(request.request_id)
        done.set()

    minute = timedelta(minutes=1)
    scheduler = (
        SchedulerBuilder()
        .declare_scope(ALPACA_DATA_SCOPE, [Limit(minute, 6000)])
        .set_limit(minute, 6000, scope=ALPACA_DATA_SCOPE)
        .cost_function(lambda _request, _page: 1, page_size=1)
        .credential_provider(StaticCredentialProvider(Credential(key_id='k', secret='s')))
        .on_expired(lambda _expired: None)
        .build()
    )
    reader = AlpacaRead(executor_provider=lambda: pool, scheduler=scheduler, scope=ALPACA_DATA_SCOPE, feed=Feed.IEX)
    executor = AckGatedExecutor(
        name='MALFORMED_TEST',
        scheduler=scheduler,
        run_attempt=BarsAttemptRunner(reader),
        settings=ExecutorSettings(buffer_size=4, workers=1, stall_check_interval_s=WATCH_S, retry=RetryPolicy(3)),
        on_finished=on_finished,
        sleep=_no_wait,
    )
    store = _AckingStore()
    request = BarsRequest(
        source=DataSource.ALPACA_API,
        asset_symbol='VFV',
        asset_type=AssetType.STOCK,
        data_types=[DataType.MARKET_ACTIVITY],
        granularity=Granularity.ONE_DAY,
        start=START,
        end=END,
        update_type=UpdateType.STATIC,
    )

    with patch.object(scheduler, 'acquire', wraps=scheduler.acquire) as acquire:
        executor.start(store, 'instance-under-test')
        try:
            await scheduler.submit(
                ScheduledRequest(
                    request_id='r1',
                    quota_scope=ALPACA_DATA_SCOPE,
                    payload=BarsWork(request=request, priority=RequestPriority.BACKFILL, feed=Feed.IEX),
                    feed=Feed.IEX,
                )
            )
            await asyncio.wait_for(done.wait(), GUARD_S)
        finally:
            await asyncio.wait_for(executor.stop(), GUARD_S)

    # The leak first, so a regression names it rather than a downstream symptom.
    assert_logs_clean(all_logs, key_id, secret)
    assert not [r for r in all_logs.records if r.exc_info], 'a traceback was logged for a refused credential'
    assert finished == ['r1']
    attempts = [(a.header.attempt_no, a.trailer.outcome, a.trailer.error_class) for a in store.received]
    assert attempts == [(n, DeliveryOutcome.FAILED, DeliveryErrorClass.TRANSIENT) for n in (1, 2, 3)]
    assert [a.trailer.last_attempt for a in store.received] == [False, False, True]
    assert all(a.trailer.detail == MESSAGE for a in store.received)
    assert all(a.trailer.vendor_stats.calls == 0 for a in store.received)
    assert acquire.await_count == 0, 'a retry spent a scheduler token'
    assert vendor.connections == 0
    assert recorder.built == []
    for attempt in store.received:
        assert_no_leak(attempt.model_dump_json(), key_id, secret, where='the report sent to the store')
    # The environment is untouched: the test left the value where it found it.
    assert os.environ[KEY_VAR] == key_id
