"""No log record carries the write secret: tj-3mk3u5.47 (SYS-REDACT; tj-3mk3u5.41 finding F5).

Design: the architect's ruling on tj-3mk3u5.47. Redact at record creation, with a session-wide
log-record factory in conftest.py that shares one redaction, SecretRedaction, with
DataStoreHttp.redact.

WHY. common/logging.py puts the root logger at DEBUG, so httpcore's trace reaches both of
pytest's captured sections, and its receive_response_headers line prints every raw response
header value. When test_http_write_secret.py's leak line caught a header echo, that same failure
report printed the secret in full.

WHAT IS PROVEN HERE is the INSTALLED redaction against the deployment's REAL secret, not a copy
built for the test. SecretLogProbe (conftest) puts the secret into a record on the logger httpcore
really uses. The record is read back from pytest's own capture and from a handler on the root
logger, which a child logger's record reaches only by propagation. The tests that need a secret
with a quote, a backslash or a non-ASCII character build the redaction with a SYNTHETIC one,
through the redaction_kit fixture.

HYGIENE: no test takes the deployment's secret, and every assertion about a captured log is on a
plain bool, never on caplog.text or on any expression pytest would expand into the failure
message. Nothing here needs a running data_store.
"""

import logging
from collections.abc import Iterator

import pytest


pytestmark = pytest.mark.data_store

REDACTED = '<redacted>'

# Where httpcore 1.0.9 creates its trace records (httpcore/_sync and _async).
HTTPCORE_LOGGERS = ('httpcore.connection', 'httpcore.http11', 'httpcore.http2', 'httpcore.proxy', 'httpcore.socks')
# Loggers that no list names: httpx's own, one an httpcore upgrade might add, and one outside both.
OTHER_LOGGERS = ('httpx', 'httpcore.an_emitter_added_later', 'tests.system')
LEVELS = (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL)

# What the probe's header trace prints before and after the echoed value.
TRACE_HEAD = (
    "receive_response_headers.complete return_value=(b'HTTP/1.1', 401, b'Unauthorized', "
    "[(b'content-length', b'53'), (b'www-authenticate', b'"
)
TRACE_REDACTED = f"{TRACE_HEAD}{REDACTED}')])"


class Collect(logging.Handler):
    """A handler for the ROOT logger, keeping everything it formats, by the logger that made it."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter('%(levelname)s %(name)s %(message)s'))
        self.formatted: list[tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.formatted.append((record.name, self.format(record)))

    @property
    def text(self) -> str:
        """Everything it received, from any logger: what a leak check reads."""
        return ''.join(f'{line}\n' for _, line in self.formatted)

    def lines_from(self, logger_name: str) -> list[str]:
        return [line for name, line in self.formatted if name == logger_name]


def messages_from(caplog: pytest.LogCaptureFixture, logger_name: str) -> list[str]:
    """The captured messages of one logger, so a stray record from another cannot unsettle a test."""
    return [record.getMessage() for record in caplog.records if record.name == logger_name]


@pytest.fixture
def root_handler() -> Iterator[Collect]:
    handler = Collect()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)


def test_an_httpcore_header_trace_reaches_caplog_redacted_and_still_prints(caplog, secret_log_probe):
    caplog.set_level(logging.DEBUG, logger='httpcore.http11')

    secret_log_probe.emit_header_trace('httpcore.http11')

    leaked = secret_log_probe.carries_secret(caplog.text)
    # The record itself too: its raw msg and args, which a handler could read without formatting.
    leaked_raw = any(secret_log_probe.carries_secret(f'{record.msg} {record.args!r}') for record in caplog.records)
    printed_redacted = messages_from(caplog, 'httpcore.http11') == [TRACE_REDACTED]
    assert not leaked, 'the captured log carries the write secret (value withheld)'
    assert not leaked_raw, "a captured record's msg or args carry the write secret (value withheld)"
    assert printed_redacted, 'the trace line did not print once, whole, with the header value as <redacted>'


@pytest.mark.parametrize('logger_name', [*HTTPCORE_LOGGERS, *OTHER_LOGGERS])
def test_a_record_on_any_logger_at_any_level_reaches_every_handler_redacted(
    caplog, root_handler, secret_log_probe, logger_name
):
    caplog.set_level(logging.DEBUG, logger=logger_name)

    for level in LEVELS:
        secret_log_probe.emit_header_trace(logger_name, level)

    leaked = secret_log_probe.carries_secret(caplog.text) or secret_log_probe.carries_secret(root_handler.text)
    expected = [f'{logging.getLevelName(level)} {logger_name} {TRACE_REDACTED}' for level in LEVELS]
    propagated_redacted = root_handler.lines_from(logger_name) == expected
    captured_redacted = messages_from(caplog, logger_name) == [TRACE_REDACTED] * len(LEVELS)
    assert not leaked, f'{logger_name}: a handler received the write secret (value withheld)'
    assert propagated_redacted, f'{logger_name}: the root handler did not get every level, redacted'
    assert captured_redacted, f'{logger_name}: caplog did not get every level, redacted'


def test_a_traceback_attached_to_a_record_is_redacted_and_still_prints(caplog, root_handler, secret_log_probe):
    caplog.set_level(logging.DEBUG, logger='httpcore.http11')

    secret_log_probe.emit_failure_with_traceback('httpcore.http11')

    leaked = secret_log_probe.carries_secret(caplog.text) or secret_log_probe.carries_secret(root_handler.text)
    exception_line = f'ConnectionError: the peer sent back {REDACTED}'
    records = [record for record in caplog.records if record.name == 'httpcore.http11']
    printed = all(
        text.count('Traceback (most recent call last):') == 1 and text.rstrip().endswith(exception_line)
        for text in (caplog.text, *root_handler.lines_from('httpcore.http11'))
    )
    no_live_exception = [record.exc_info for record in records] == [None]
    assert not leaked, 'a captured traceback carries the write secret (value withheld)'
    assert printed, 'the traceback did not print once, ending in the redacted exception line'
    assert no_live_exception, 'a record still carries the live exception a formatter could render raw'


@pytest.mark.parametrize(
    'secret',
    [
        'a' * 16 + '0123456789abcdef',
        "single'quote",
        'double"quote',
        'both\'and"quotes',
        'back\\slash',
        'ends-in-a-backslash\\',
        'nön-ascii',
        'tab\there',
    ],
    ids=['hex', 'single-quote', 'double-quote', 'both-quotes', 'backslash', 'trailing-backslash', 'non-ascii', 'tab'],
)
def test_the_shared_redaction_replaces_the_secret_as_text_and_as_a_bytes_literal(redaction_kit, secret):
    # A SYNTHETIC secret, so this test may hold it and its assertions may print it.
    literal = repr(secret.encode())
    text = f'header={literal} plain={secret} again={literal}'

    redacted = redaction_kit.redaction(secret)(text)

    quote = literal[-1]
    assert redacted == f'header=b{quote}{REDACTED}{quote} plain={REDACTED} again=b{quote}{REDACTED}{quote}'


def test_data_store_http_redacts_through_the_same_function_as_the_log_factory(redaction_kit, monkeypatch, caplog):
    # A SYNTHETIC, non-hex secret: R2's widening reaches describe() and the transport-failure
    # message through DataStoreHttp.redact.
    secret = 'synthetic-nön-hex'
    client = redaction_kit.data_store_http('http://127.0.0.1:9', secret)
    try:
        widened = client.redact(f'echoed {secret.encode()!r} and {secret}')
        # One implementation: wrap the shared one, and both of its callers change.
        shared = redaction_kit.redaction.__call__
        monkeypatch.setattr(redaction_kit.redaction, '__call__', lambda self, text: 'shared:' + shared(self, text))
        caplog.set_level(logging.DEBUG, logger='tests.system')
        logging.getLogger('tests.system').debug('a record')
        through_client = client.redact('a text')
    finally:
        client.close()

    through_factory = messages_from(caplog, 'tests.system') == ['shared:a record']
    assert widened == f"echoed b'{REDACTED}' and {REDACTED}"
    assert through_client == 'shared:a text'
    assert through_factory, 'the installed log-record factory does not call the shared redaction'


def test_an_install_chains_over_the_current_factory_and_puts_it_back(redaction_kit, caplog, secret_log_probe):
    caplog.set_level(logging.DEBUG, logger='tests.system')
    synthetic = 'synthetic-inner-secret'
    before = logging.getLogRecordFactory()

    with redaction_kit.records_redacted(redaction_kit.redaction(synthetic)):
        during = logging.getLogRecordFactory()
        logging.getLogger('tests.system').debug(f'inner {synthetic}')
        secret_log_probe.emit_header_trace('tests.system')
    after = logging.getLogRecordFactory()

    # Bools only: one of these messages carries the deployment's secret if the redaction fails.
    session_secret_redacted = not secret_log_probe.carries_secret(caplog.text)
    both_redacted = messages_from(caplog, 'tests.system') == [f'inner {REDACTED}', TRACE_REDACTED]
    assert session_secret_redacted, 'the chained-over session factory stopped redacting (value withheld)'
    assert both_redacted, 'the inner and the chained-over session redaction did not both apply'
    assert during is not before
    assert after is before
