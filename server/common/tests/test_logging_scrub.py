"""The registered-value log scrub and the broker log sink in common/logging.py (tj-7zjov1.74, tests tj-7zjov1.75).

Design: tj-7zjov1.74 and its notes (exp1 where records land, exp2 the prototype); tj-7zjov1.67 note
ARCHITECT DESIGN ADDENDUM items 8 to 11; ADR tj-vhboky.41 ADDENDUM 2 (M5); owner rulings on
tj-7zjov1.78 (sink kept on stderr by default, INFO by default; our own broker-failure tracebacks stay
on the main log, scrubbed).

HOW THESE TESTS READ OUTPUT. Never through caplog: caplog sees records, not formatted text, so it
cannot see a scrub, and with propagate False on BROKER_LOGGERS whether caplog receives those loggers
depends on timing (pytest 9.1.1 attaches its handlers to loggers that are ALREADY non-propagating
when a test phase starts, and to no logger that becomes so during it), so an absence assertion
there can pass vacuously. Every assertion reads the
FORMATTED output of a real handler: a stream handler the test attaches, the sink's own stream, the
sink's file, or a fresh interpreter's stdout and stderr.

Logging state is process-wide, so every in-process test runs inside isolated_logging(), which puts
handlers, formatters, levels and propagate back and clears the once-per-process sink. The registry
has no reset: each test registers a fresh made-up value. Values are made up; the only network use is
a loopback socket that never answers.
"""

import base64
import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import uuid
from functools import lru_cache

import httpx
import pytest
import requests

import common.app_lifecycle as app_lifecycle
from common.logging import (
    BROKER_LOGGERS,
    ScrubFormatter,
    configure_broker_sink,
    get_logger,
    install_scrub,
    register_secret,
)
from common.sensitive import REDACTED
from common.tests.image_path import image_pythonpath
from common.tests.logging_isolation import Captured, isolated_logging
from common.tests.roots import SERVER_ROOT


pytestmark = pytest.mark.common

LOOPBACK = '127.0.0.1'
PLAIN = '%(message)s'
# The ingest scheduler's executor logger: get_logger(__name__) in data/ingest/app/scheduler/executor.py.
EXECUTOR = 'data.ingest.app.scheduler.executor'
SINK_PREFIX = 'BROKER '


def _made_up(tag: str) -> str:
    """A fresh made-up credential-shaped value; never a real one."""
    return f'PKMADEUP{tag}{uuid.uuid4().hex.upper()}'


def _scrubbing(fmt: str = PLAIN) -> ScrubFormatter:
    return ScrubFormatter(logging.Formatter(fmt))


@pytest.fixture
def clean(monkeypatch: pytest.MonkeyPatch):
    """Isolated logging state with LOG_LEVEL and BROKER_LOG_FILE unset."""
    monkeypatch.delenv('LOG_LEVEL', raising=False)
    monkeypatch.delenv('BROKER_LOG_FILE', raising=False)
    with isolated_logging():
        yield


@pytest.fixture
def plain_and_scrubbed(clean):
    """Two handlers on one logger, one plain and one ScrubFormatter over the same format.

    The plain one is the before-the-scrub view: the leak each test closes is shown in it first.
    """
    plain = Captured('tj75.shape', logging.Formatter(PLAIN))
    scrubbed = Captured('tj75.shape', _scrubbing())
    try:
        yield plain, scrubbed
    finally:
        plain.close()
        scrubbed.close()


def _requests_invalid_header(value: str) -> requests.exceptions.InvalidHeader:
    """The exception requests raises for a malformed header value, before any connection."""
    try:
        requests.Request('GET', f'http://{LOOPBACK}:9/', headers={'APCA-API-KEY-ID': value}).prepare()
    except requests.exceptions.InvalidHeader as exc:
        return exc
    raise AssertionError('requests accepted a malformed header value')


def _httpx_local_protocol_error(value: str) -> httpx.LocalProtocolError:
    """The exception httpx raises sending a malformed header value to a loopback listener."""
    with socket.socket() as listener:
        listener.bind((LOOPBACK, 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            with httpx.Client(timeout=5) as client:
                client.get(f'http://{LOOPBACK}:{port}/', headers={'APCA-API-SECRET-KEY': value})
        except httpx.LocalProtocolError as exc:
            return exc
    raise AssertionError('httpx sent a malformed header value')


# --- 1. message argument, message, traceback --------------------------------------------------


def test_a_registered_value_is_replaced_in_the_argument_the_message_and_the_traceback(clean):
    value = _made_up('ARG')
    captured = Captured('tj75.child', _scrubbing())
    log = logging.getLogger('tj75.child')
    try:

        def emit() -> None:
            log.error('argument %s', value)
            log.error(f'message {value}')
            try:
                raise ValueError(f'bad key {value}')
            except ValueError:
                log.exception('traceback follows')

        emit()
        before = captured.text
        register_secret(value)
        emit()
        after = captured.text[len(before) :]
    finally:
        captured.close()

    # Red before the scrub: the value is in all three places until it is registered.
    assert before.count(value) == 3, before
    assert value not in after, after
    lines = after.splitlines()
    assert f'argument {REDACTED}' in lines
    assert f'message {REDACTED}' in lines
    assert lines[-1] == f'ValueError: bad key {REDACTED}', 'the last line of the traceback kept the value'


def test_a_record_created_before_registration_but_formatted_after_it_is_scrubbed(clean):
    """The guard is at FORMAT time: a record held unformatted (a queue, a buffer) is still covered."""
    value = _made_up('LATE')
    record = logging.LogRecord('tj75.queue', logging.ERROR, __file__, 1, 'held %s', (value,), None)
    register_secret(value)
    assert _scrubbing().format(record) == f'held {REDACTED}'


# --- 2. the five forms ------------------------------------------------------------------------
#
# Each value is shaped so that ONLY the form under test matches its rendering: dropping that one form
# from register_secret leaves the rendering unchanged, so each case reds on its own form alone.

FORMS = {
    # The whole value, trailing LF included, becomes one marker (the stripped form alone leaves the LF).
    'raw': (lambda core: f'{core}\n', lambda v: f'key={v}|', f'key={REDACTED}|'),
    # A caller that strips before logging: no other form is a substring of the bare core.
    'stripped': (lambda core: f'{core}\n', lambda v: f'key={v.strip()}|', f'key={REDACTED}|'),
    # repr keeps the non-ASCII character and escapes the control character as \x01.
    'repr': (lambda core: f'{core}\x01é', lambda v: f'key={v!r}|', f"key='{REDACTED}'|"),
    # JSON escapes the control character as \u0001, which no other form produces.
    'json': (lambda core: f'{core}\x01', lambda v: f'key={json.dumps(v)}|', f'key="{REDACTED}"|'),
    # httpx and httpcore print bytes: the UTF-8 encoding shows as \xc3\xa9.
    'bytes': (lambda core: f'{core}é', lambda v: f'key={v.encode()!r}|', f"key=b'{REDACTED}'|"),
}


@pytest.mark.parametrize('form', list(FORMS))
def test_each_registered_form_is_replaced_and_only_that_form_matches_its_rendering(clean, form: str):
    make, render, expected = FORMS[form]
    value = make(_made_up(form.upper()))
    text = render(value)
    record = logging.LogRecord('tj75.form', logging.ERROR, __file__, 1, text, None, None)
    assert _scrubbing().format(record) == text, 'an unregistered value was changed'
    register_secret(value)
    assert _scrubbing().format(record) == expected


def test_the_requests_invalid_header_traceback_is_scrubbed(plain_and_scrubbed):
    plain, scrubbed = plain_and_scrubbed
    value = _made_up('REQ') + '\n'
    register_secret(value)
    logging.getLogger('tj75.shape').error('attempt raised', exc_info=_requests_invalid_header(value))

    assert f'header value: {value!r}' in plain.text, 'the leak this closes is not there to close'
    assert value.strip() not in scrubbed.text, scrubbed.text
    assert scrubbed.text.rstrip().endswith(f"header value: '{REDACTED}'"), scrubbed.text


def test_the_httpx_local_protocol_error_traceback_is_scrubbed(plain_and_scrubbed):
    plain, scrubbed = plain_and_scrubbed
    value = _made_up('HTTPX') + '\n'
    register_secret(value)
    logging.getLogger('tj75.shape').error('attempt raised', exc_info=_httpx_local_protocol_error(value))

    assert f'Illegal header value {value.encode()!r}' in plain.text, 'the leak this closes is not there'
    assert value.strip() not in scrubbed.text, scrubbed.text
    assert f"httpx.LocalProtocolError: Illegal header value b'{REDACTED}'" in scrubbed.text


def test_a_json_escaped_rendering_is_scrubbed(plain_and_scrubbed):
    plain, scrubbed = plain_and_scrubbed
    value = _made_up('JSON') + '\n'
    register_secret(value)
    logging.getLogger('tj75.shape').error('payload %s', json.dumps({'key': value}))

    assert json.dumps(value)[1:-1] in plain.text
    assert value.strip() not in scrubbed.text
    assert scrubbed.text == f'payload {{"key": "{REDACTED}"}}\n'


def test_the_httpcore_debug_trace_of_the_failed_header_send_is_scrubbed(clean):
    value = _made_up('TRACE') + '\n'
    register_secret(value)
    httpcore_logger = logging.getLogger('httpcore')
    httpcore_logger.setLevel(logging.DEBUG)  # restored by isolated_logging
    plain = Captured('httpcore', logging.Formatter(PLAIN))
    scrubbed = Captured('httpcore', _scrubbing())
    try:
        _httpx_local_protocol_error(value)
    finally:
        plain.close()
        scrubbed.close()

    def failed(text: str) -> str:
        return next(line for line in text.splitlines() if 'send_request_headers.failed' in line)

    assert value.strip() in failed(plain.text), 'httpcore no longer traces the value: the premise moved'
    assert value.strip() not in scrubbed.text
    assert REDACTED in failed(scrubbed.text)


# --- 3. coverage of handlers ------------------------------------------------------------------


def test_install_scrub_wraps_the_root_handler_and_a_child_logger_record_reaches_it_scrubbed(clean):
    value = _made_up('CHILD')
    own_format = logging.Formatter('%(levelname)s|%(name)s|%(message)s')
    root = Captured('', own_format)
    try:
        install_scrub()
        assert isinstance(root.handler.formatter, ScrubFormatter)
        assert root.handler.formatter.inner is own_format, 'the handler lost its own formatter'
        register_secret(value)
        logging.getLogger('tj75.deep.child.logger').error('key %s', value)
    finally:
        root.close()
    assert root.text == f'ERROR|tj75.deep.child.logger|key {REDACTED}\n'


def test_get_logger_installs_the_scrub(clean):
    root = Captured('', logging.Formatter(PLAIN))
    try:
        get_logger('tj75.get_logger')
        assert isinstance(root.handler.formatter, ScrubFormatter)
    finally:
        root.close()


def _noop_asgi(scope, receive, send):  # pragma: no cover - never served
    raise AssertionError('not served')


def test_under_uvicorns_own_logging_setup_its_handlers_are_wrapped_and_keep_their_format(clean):
    """The CLI of uvicorn builds Config (which applies its LOGGING_CONFIG) before importing the app."""
    import uvicorn
    from uvicorn.logging import AccessFormatter, DefaultFormatter

    uvicorn.Config(app=_noop_asgi)
    get_logger('tj75.app_module')
    (default,) = logging.getLogger('uvicorn').handlers
    (access,) = logging.getLogger('uvicorn.access').handlers
    assert isinstance(default.formatter, ScrubFormatter)
    assert isinstance(default.formatter.inner, DefaultFormatter)
    assert isinstance(access.formatter, ScrubFormatter)
    assert isinstance(access.formatter.inner, AccessFormatter)

    value = _made_up('ASGI') + '\n'
    register_secret(value)
    record = logging.getLogger('uvicorn.error').makeRecord(
        'uvicorn.error',
        logging.ERROR,
        __file__,
        1,
        'Exception in ASGI application\n',
        None,
        (requests.exceptions.InvalidHeader, _requests_invalid_header(value), None),
    )
    unscrubbed = default.formatter.inner.format(record)
    record.exc_text = None
    text = default.format(record)
    assert value.strip() in unscrubbed, 'the leak this closes is not there'
    assert text.startswith('ERROR:    Exception in ASGI application'), "uvicorn's own format was lost"
    assert value.strip() not in text
    assert text.rstrip().endswith(f"header value: '{REDACTED}'")


# --- 4. nothing else changes ------------------------------------------------------------------


def test_an_unregistered_value_and_ordinary_text_pass_through_unchanged(clean):
    register_secret(_made_up('OTHER'))
    inner = logging.Formatter('%(levelname)s %(name)s %(message)s')
    try:
        raise RuntimeError(f'ordinary failure with {_made_up("UNREG")}')
    except RuntimeError as exc:
        record = logging.LogRecord('tj75.plain', logging.ERROR, __file__, 1, 'ordinary %s', ('text',), None)
        record.exc_info = (RuntimeError, exc, exc.__traceback__)
    expected = inner.format(record)
    record.exc_text = None
    assert ScrubFormatter(inner).format(record) == expected


def test_a_handler_with_no_formatter_keeps_the_plain_message_once_wrapped(clean):
    """F4 on tj-7zjov1.74: wrapping keeps each handler's own format, the formatter-less one included.

    logging renders a handler with no formatter through its default Formatter: the bare message. The
    wrapped handler must write the same text, not BASIC_FORMAT's level and logger name.
    """
    import io

    name = 'tj75.no_formatter'
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    assert handler.formatter is None
    log = logging.getLogger(name)  # not in TOUCHED: its state is put back here
    saved = (log.level, log.propagate)
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        log.warning('plain %s', 'text')
        before = stream.getvalue()
        handler.setFormatter(ScrubFormatter(handler.formatter))
        log.warning('plain %s', 'text')
        after = stream.getvalue()[len(before) :]
    finally:
        log.removeHandler(handler)
        log.setLevel(saved[0])
        log.propagate = saved[1]
    assert before == 'plain text\n'
    assert after == before, 'wrapping changed the format of a handler that had none'


def test_install_scrub_on_a_root_handler_with_no_formatter_keeps_the_plain_message(clean):
    """F4 through install_scrub itself: a root handler with no formatter writes the same text after."""
    root = Captured('')
    root.handler.setFormatter(None)  # Captured supplies a format by default; this handler has none
    try:
        logging.getLogger('tj75.no_formatter.root').error('plain %s', 'text')
        before = root.text
        install_scrub()
        assert isinstance(root.handler.formatter, ScrubFormatter)
        logging.getLogger('tj75.no_formatter.root').error('plain %s', 'text')
        after = root.text[len(before) :]
    finally:
        root.close()
    assert before == 'plain text\n'
    assert after == before


@pytest.mark.parametrize(
    'value', ['', ' ', '   ', '\t', '\r\n', ' \t\n'], ids=['empty', 'space', 'spaces', 'tab', 'crlf', 'mixed']
)
def test_an_empty_or_whitespace_only_value_registers_nothing(clean, value: str):
    """tj-7zjov1.74 item 1: an empty or whitespace-only value registers nothing -- none of its forms.

    The tab, CR and LF cases are the ones the per-form filter alone missed (F1): their escaped
    forms (backslash-t, backslash-r, backslash-n text) are not whitespace, so only the early return
    on a blank value keeps them out of the registry.
    """
    register_secret(value)
    text = f'raw[{value}] repr[{value!r}] json[{json.dumps(value)}] bytes[{value.encode()!r}] words in a line'
    record = logging.LogRecord('tj75.blank', logging.ERROR, __file__, 1, text, None, None)
    assert _scrubbing().format(record) == text


# --- 5. documented non-guards, pinned ---------------------------------------------------------


def test_a_base64_form_of_a_registered_value_is_not_replaced(clean):
    """Documented: literal forms only. A Basic auth header carries key:secret base64-encoded and passes."""
    key, secret = _made_up('B64K'), _made_up('B64S')
    register_secret(key)
    register_secret(secret)
    encoded = base64.b64encode(f'{key}:{secret}'.encode()).decode()
    record = logging.LogRecord('tj75.b64', logging.ERROR, __file__, 1, 'Authorization: Basic %s', (encoded,), None)
    assert _scrubbing().format(record) == f'Authorization: Basic {encoded}'


def test_a_split_value_is_not_replaced(clean):
    """Documented: literal forms only. Half a registered value is not a registered form."""
    value = _made_up('SPLIT')
    register_secret(value)
    half = value[: len(value) // 2]
    record = logging.LogRecord('tj75.split', logging.ERROR, __file__, 1, 'first half %s', (half,), None)
    assert _scrubbing().format(record) == f'first half {half}'


def test_a_record_formatted_before_register_secret_ran_is_not_scrubbed(clean):
    """Documented: values registered before the record is formatted only. What was written stays written."""
    value = _made_up('EARLY')
    captured = Captured('tj75.early', _scrubbing())
    try:
        logging.getLogger('tj75.early').error('early %s', value)
        register_secret(value)
        logging.getLogger('tj75.early').error('late %s', value)
    finally:
        captured.close()
    assert captured.text == f'early {value}\nlate {REDACTED}\n'


def test_a_handler_added_after_install_scrub_is_not_wrapped(clean):
    """Documented: a later handler (a test capture handler included) is not covered."""
    value = _made_up('LATER')
    register_secret(value)
    install_scrub()
    later = Captured('', logging.Formatter(PLAIN))
    try:
        logging.getLogger('tj75.later').error('key %s', value)
    finally:
        later.close()
    assert not isinstance(later.handler.formatter, ScrubFormatter)
    assert later.text == f'key {value}\n'


def test_residual_an_interior_control_character_passes_the_httpcore_trace_double_escaped(clean):
    """Residual, NOT a documented form, pinned so it is seen.

    httpcore's trace prints repr() of the exception, whose text already holds the bytes repr, so every
    backslash is doubled. The five forms carry single
    backslashes. A trailing CR or LF is still covered by the stripped form (test above); an INTERIOR
    control character leaves both halves of the value in the trace. Only at LOG_LEVEL=DEBUG (the sink
    is INFO otherwise). get_client builds the requests-based Alpaca client, not an httpx sender, and
    refuses such a value before building it (tj-7zjov1.67). The only httpx sender that carries a
    credential is the dev vendor probe, which runs check_credential before it builds its client, so
    no such value reaches this trace (accepted residual, architect ruling F2 on tj-7zjov1.74).
    If this goes red, the form was added: invert it.
    """
    head, tail = _made_up('HEAD'), _made_up('TAIL')
    value = f'{head}\n{tail}'
    register_secret(value)
    logging.getLogger('httpcore').setLevel(logging.DEBUG)  # restored by isolated_logging
    scrubbed = Captured('httpcore', _scrubbing())
    try:
        _httpx_local_protocol_error(value)
    finally:
        scrubbed.close()
    failed = next(line for line in scrubbed.text.splitlines() if 'send_request_headers.failed' in line)
    assert head in failed
    assert tail in failed


# --- 6 and 7. sink routing and its boundary ---------------------------------------------------

SINK_ROUTED = ('alpaca', 'requests', 'urllib3.connectionpool', 'httpx', 'httpcore.http11')


@pytest.fixture
def sink_and_root(clean):
    """configure_broker_sink(None) with its stream redirected to a buffer, and a scrubbed root capture."""
    import io

    root = Captured('', logging.Formatter(PLAIN))
    sink = configure_broker_sink(None)
    sink_stream = io.StringIO()
    assert isinstance(sink, logging.StreamHandler)
    sink.setStream(sink_stream)
    install_scrub()
    try:
        yield sink, sink_stream, root
    finally:
        root.close()


def test_the_sink_without_a_path_is_one_stderr_stream_on_every_broker_logger(clean):
    sink = configure_broker_sink(None)
    assert type(sink) is logging.StreamHandler
    assert sink.stream is sys.stderr
    for name in BROKER_LOGGERS:
        broker_logger = logging.getLogger(name)
        # requests ships a NullHandler of its own; the sink is the one handler added.
        assert [h for h in broker_logger.handlers if not isinstance(h, logging.NullHandler)] == [sink], name
        assert broker_logger.propagate is False, name
        assert broker_logger.level == logging.NOTSET, f'{name}: the design sets no level on the loggers'
    assert isinstance(sink.formatter, ScrubFormatter)
    assert '%(name)s' in sink.formatter.inner._fmt and '%(asctime)s' in sink.formatter.inner._fmt


@pytest.mark.parametrize('name', SINK_ROUTED)
def test_a_broker_library_record_reaches_the_sink_scrubbed_and_not_the_root(sink_and_root, name: str):
    _, sink_stream, root = sink_and_root
    value = _made_up('SINK')
    register_secret(value)
    logging.getLogger(name).warning('library line %s', value)

    (line,) = sink_stream.getvalue().splitlines()
    assert line.startswith(SINK_PREFIX) and f' {name}  library line {REDACTED}' in line, line
    assert value not in sink_stream.getvalue()
    assert root.text == '', f'{name} also reached the root handler'
    assert logging.getLogger(name).handlers == [] or name in BROKER_LOGGERS, 'a child got a handler of its own'


def test_our_own_log_exception_of_a_broker_failure_stays_on_root_scrubbed_and_not_in_the_sink(sink_and_root):
    """R8 A on tj-7zjov1.78: our tracebacks of broker failures stay on the main log, scrubbed."""
    _, sink_stream, root = sink_and_root
    value = _made_up('OWN') + '\n'
    register_secret(value)
    logging.getLogger(EXECUTOR).error('Handle h: the attempt runner raised', exc_info=_requests_invalid_header(value))

    assert 'Handle h: the attempt runner raised' in root.text
    assert root.text.rstrip().endswith(f"header value: '{REDACTED}'")
    assert value.strip() not in root.text
    assert 'the attempt runner raised' not in sink_stream.getvalue()


# --- 8. file destination ----------------------------------------------------------------------


def test_a_path_gives_a_rotating_file_mode_0600_bounded_and_scrubbed(clean, tmp_path):
    path = tmp_path / 'broker.log'
    previous = os.umask(0)  # permissive, so the mode proves the code's own umask
    try:
        sink = configure_broker_sink(str(path))
    finally:
        restored = os.umask(previous)
    assert restored == 0, 'configure_broker_sink left its umask in place'
    assert type(sink) is logging.handlers.RotatingFileHandler
    assert sink.maxBytes == 10 * 1024 * 1024
    assert sink.backupCount == 5
    assert os.stat(path).st_mode & 0o777 == 0o600

    value = _made_up('FILE')
    register_secret(value)
    logging.getLogger('urllib3.connectionpool').warning('pool line %s', value)
    sink.flush()
    text = path.read_text(encoding='utf-8')
    assert text.startswith(SINK_PREFIX) and f'pool line {REDACTED}' in text
    assert value not in text


# --- 9. idempotence ---------------------------------------------------------------------------


def test_install_scrub_twice_wraps_once(clean):
    root = Captured('', logging.Formatter(PLAIN))
    try:
        install_scrub()
        first = root.handler.formatter
        install_scrub()
        assert root.handler.formatter is first
        assert not isinstance(first.inner, ScrubFormatter)
    finally:
        root.close()


def test_configure_broker_sink_twice_leaves_one_handler_per_logger_whatever_the_path(clean, tmp_path):
    first = configure_broker_sink(None)
    second = configure_broker_sink(str(tmp_path / 'ignored.log'))
    assert second is first
    assert not (tmp_path / 'ignored.log').exists()
    for name in BROKER_LOGGERS:
        handlers = logging.getLogger(name).handlers
        assert [h for h in handlers if not isinstance(h, logging.NullHandler)] == [first], name


# --- 10. startup_logs -------------------------------------------------------------------------


@pytest.mark.parametrize('configured', [None, 'broker.log'], ids=['unset', 'set'])
def test_startup_logs_installs_the_sink_and_the_scrub_before_it_logs(
    clean, monkeypatch: pytest.MonkeyPatch, tmp_path, configured: str | None
):
    from fastapi import FastAPI

    calls: list[tuple] = []
    expected_path = None
    if configured is not None:
        expected_path = str(tmp_path / configured)
        monkeypatch.setenv('BROKER_LOG_FILE', expected_path)
    monkeypatch.setattr(app_lifecycle, 'configure_broker_sink', lambda path: calls.append(('sink', path)))
    monkeypatch.setattr(app_lifecycle, 'install_scrub', lambda: calls.append(('scrub',)))

    class _Order(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            calls.append(('log', record.getMessage()))

    order = _Order(logging.DEBUG)
    lifecycle_logger = logging.getLogger('common.app_lifecycle')
    lifecycle_logger.addHandler(order)
    previous_level = lifecycle_logger.level
    lifecycle_logger.setLevel(logging.INFO)
    try:
        app_lifecycle.startup_logs(FastAPI())
    finally:
        lifecycle_logger.removeHandler(order)
        lifecycle_logger.setLevel(previous_level)
    assert calls[:3] == [('sink', expected_path), ('scrub',), ('log', 'Starting up app...')], calls[:3]


# --- the real service setup, end to end, in a fresh interpreter ---------------------------------
#
# The order uvicorn's CLI uses: Config (its LOGGING_CONFIG applied), then the app imported (get_logger
# runs basicConfig and install_scrub), then the lifespan (startup_logs), then a credential registered
# at its read site. Each site gets its own value so a leak is attributed to the site that leaked it.

_SERVICE_PROBE = """
import json, logging, sys

import uvicorn

uvicorn.Config(app=lambda scope, receive, send: None)

from fastapi import FastAPI

from common.app_lifecycle import startup_logs
from common.logging import get_logger, register_secret
from common.tests.test_logging_scrub import EXECUTOR, _httpx_local_protocol_error, _requests_invalid_header

log = get_logger(EXECUTOR)
startup_logs(FastAPI())
values = json.loads(sys.argv[2])
if sys.argv[1] == 'register':
    for value in values.values():
        register_secret(value)

log.error('Handle h: the attempt runner raised for request r1', exc_info=_requests_invalid_header(values['requests']))
log.error('Handle h: the attempt runner raised for request r2', exc_info=_httpx_local_protocol_error(values['httpx']))
logging.getLogger('uvicorn.error').error(
    'Exception in ASGI application\\n', exc_info=_requests_invalid_header(values['asgi'])
)
logging.getLogger('uvicorn.access').info(
    '%s - "%s %s HTTP/%s" %d', '127.0.0.1:50000', 'GET', '/probe?k=' + values['access'], '1.1', 500
)
"""


@lru_cache
def _service(register: bool, log_level: str | None) -> tuple[dict[str, str], str, str]:
    values = {site: _made_up(site.upper()) + '\n' for site in ('requests', 'httpx', 'asgi', 'access')}
    env = {k: v for k, v in os.environ.items() if k not in ('PYTEST_ADDOPTS', 'LOG_LEVEL', 'BROKER_LOG_FILE')}
    if log_level is not None:
        env['LOG_LEVEL'] = log_level
    env['PYTHONPATH'] = image_pythonpath(SERVER_ROOT)
    result = subprocess.run(
        [sys.executable, '-c', _SERVICE_PROBE, 'register' if register else 'none', json.dumps(values)],
        cwd=SERVER_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return values, result.stdout, result.stderr


def _trace(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if 'send_request_headers.failed' in line]


def test_the_service_setup_leaks_every_site_when_nothing_is_registered():
    """The premise: without registration each of the four sites prints its value. Proves the next test."""
    values, stdout, stderr = _service(False, 'DEBUG')
    assert f'header value: {values["requests"]!r}' in stderr
    assert f'Illegal header value {values["httpx"].encode()!r}' in stderr
    assert any(values['httpx'].strip() in line for line in _trace(stderr))
    assert f'header value: {values["asgi"]!r}' in stderr
    assert values['access'].strip() in stdout


def test_the_service_setup_scrubs_every_site_in_its_own_handler_and_format():
    values, stdout, stderr = _service(True, 'DEBUG')
    for site, value in values.items():
        assert value.strip() not in stderr + stdout, f'{site} leaked'

    lines = stderr.splitlines()
    # Root handler, basicConfig's format: our executor's tracebacks stay on the main log (R8 A).
    assert 'ERROR    [<string>]  Handle h: the attempt runner raised for request r1' in lines
    assert 'ERROR    [<string>]  Handle h: the attempt runner raised for request r2' in lines
    assert stderr.count(f"header value: '{REDACTED}'") == 2  # executor r1 and the ASGI record
    assert f"httpx.LocalProtocolError: Illegal header value b'{REDACTED}'" in lines
    # The library trace went to the sink, in the sink's format, not to root.
    (trace,) = _trace(stderr)
    assert trace.startswith(SINK_PREFIX) and ' httpcore.http11 ' in trace and REDACTED in trace, trace
    # uvicorn's own handlers, uvicorn's own formats.
    assert 'ERROR:    Exception in ASGI application' in lines
    assert stdout.startswith(f'INFO:     127.0.0.1:50000 - "GET /probe?k={REDACTED} HTTP/1.1" 500 '), stdout


def test_the_service_setup_at_the_default_level_drops_the_library_debug_trace():
    """Owner ruling on tj-7zjov1.78: the sink is INFO unless LOG_LEVEL=DEBUG (tj-7zjov1.86)."""
    values, stdout, stderr = _service(True, None)
    assert _trace(stderr) == []
    assert not [line for line in stderr.splitlines() if line.startswith(SINK_PREFIX) and ' DEBUG ' in line]
    for site, value in values.items():
        assert value.strip() not in stderr + stdout, f'{site} leaked'
