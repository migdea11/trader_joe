"""Both Alpaca values are registered with the log scrub at every environment read, malformed ones included.

Bead tj-7zjov1.77 against builder-ingest's 4075d66 (tj-7zjov1.76). The design: tj-7zjov1.76's description and its
architect amendments of 17:17 UTC (read each variable exactly once) and 18:00 UTC (register the plain str, never the
OpaqueStr); the scrub's API and limits in common/logging.py (tj-7zjov1.74).

WHAT IS PINNED:
1. get_client registers both well-formed values: afterwards a record carrying either, formatted through
   ScrubFormatter, shows the marker and not the value (message, %r argument and traceback alike).
2. get_client registers a malformed value BEFORE the shape check: it raises MalformedCredentialsError, and a real
   requests InvalidHeader built with that value, logged with its traceback, is scrubbed.
3. An unset or empty value registers nothing (ordinary text formats unchanged) and the MissingCredentialsError names
   only the variable. Half-configured (one set, the other unset or empty): the set value, and only it, is already
   registered as a plain str when MissingCredentialsError is raised (architect ruling 18:31 UTC, fix a7b0b09).
4. get_client reads each variable exactly once, and the missing check, the registration, the check and the wrap all
   use the objects read (identity), on the well-formed and on the missing path.
5. What is registered is a plain str, never the OpaqueStr wrapper, so the word in the marker is never over-scrubbed.
6. StaticCredentialProvider.from_env registers both values under arbitrary variable names, before its blank check.
7. The registry never alters what is sent: the vendor still receives the real bytes in header mode.

The registry is process-wide and common.logging exposes no reset, so every test makes up fresh values and first
shows they are NOT scrubbed yet (the control that the registration came from the call under test). ScrubFormatter is
instantiated directly, so no logger or handler state is changed. Made-up values only; loopback only.
"""

import logging
import sys
import threading
from collections.abc import Iterator
from itertools import count
from unittest.mock import patch
from uuid import uuid4

import pytest
from requests.exceptions import InvalidHeader
from requests.utils import check_header_validity

from common import logging as common_logging
from common.logging import ScrubFormatter
from common.sensitive import REDACTED, OpaqueStr
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.broker_errors import MalformedCredentialsError, MissingCredentialsError
from data.ingest.app.scheduler import credentials as credentials_module
from data.ingest.app.scheduler.credentials import StaticCredentialProvider, check_credential_shape
from data.ingest.app.scheduler.limits import SchedulerConfigError
from data.ingest.tests.test_alpaca_opaque_credentials import LoopbackVendor, production_client


pytestmark = pytest.mark.data_ingest

KEY_VAR, SECRET_VAR = broker_api.ALPACA_CREDENTIAL_VARS
GUARD_S = 10.0
ORDINARY = 'an ordinary line: a b\tc\nd, redacted, PK, key, secret, 0123456789'
DAMAGE = {'trailing-lf': lambda v: v + '\n', 'trailing-cr': lambda v: v + '\r', 'leading-space': lambda v: ' ' + v}


def fresh_key() -> str:
    return f'PKSCRUB{uuid4().hex.upper()[:17]}'


def fresh_secret() -> str:
    return f'scrubMadeUp{uuid4().hex}'


def formatted(msg: str, *args: object, exc_info: object = None) -> str:
    """One record formatted through a freshly built ScrubFormatter, as every scrubbed handler formats it."""
    record = logging.LogRecord('test.scrub', logging.ERROR, __file__, 1, msg, args or None, exc_info)
    return ScrubFormatter(logging.Formatter('%(levelname)s %(name)s %(message)s')).format(record)


def assert_scrubbed(value: str) -> None:
    for text in (formatted('carrying %s here', value), formatted('carrying %r here', value), formatted(value)):
        assert REDACTED in text, 'a registered value formatted without the marker'
        assert value not in text and value.strip() not in text, 'a formatted line still carries the value'


def assert_not_scrubbed(value: str) -> None:
    assert value in formatted('carrying %s here', value), 'control: the value was already registered'


@pytest.fixture(autouse=True)
def clean_process_state(monkeypatch) -> Iterator[None]:
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.delenv(KEY_VAR, raising=False)
    monkeypatch.delenv(SECRET_VAR, raising=False)
    broker_api.set_client(None)
    yield
    broker_api.set_client(None)


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


@pytest.fixture
def registered() -> Iterator[list[object]]:
    """Every argument get_client and from_env pass to register_secret, passed on to the real one."""
    calls: list[object] = []

    def spy(value):
        calls.append(value)
        return common_logging.register_secret(value)

    with (
        patch.object(broker_api, 'register_secret', side_effect=spy),
        patch.object(credentials_module, 'register_secret', side_effect=spy),
    ):
        yield calls


# ---------------------------------------------------------------------------------------------
# 1. Well-formed values
# ---------------------------------------------------------------------------------------------


def test_get_client_registers_both_well_formed_values(monkeypatch):
    key, secret = fresh_key(), fresh_secret()
    monkeypatch.setenv(KEY_VAR, key)
    monkeypatch.setenv(SECRET_VAR, secret)
    assert_not_scrubbed(key)
    assert_not_scrubbed(secret)

    broker_api.get_client()

    assert_scrubbed(key)
    assert_scrubbed(secret)
    try:
        raise RuntimeError(f'the vendor said {key}:{secret}')
    except RuntimeError:
        text = formatted('failed', exc_info=sys.exc_info())
    assert 'RuntimeError' in text and 'Traceback' in text, 'the traceback was not formatted'
    assert key not in text and secret not in text, 'a traceback carrying the pair was not scrubbed'


# ---------------------------------------------------------------------------------------------
# 2. A malformed value is registered before the shape check refuses it
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize('which', ('key', 'secret'))
@pytest.mark.parametrize('shape', DAMAGE)
def test_a_malformed_value_is_refused_and_already_registered_so_an_invalid_header_traceback_is_scrubbed(
    shape, which, monkeypatch
):
    key, secret = fresh_key(), fresh_secret()
    key, secret = (DAMAGE[shape](key), secret) if which == 'key' else (key, DAMAGE[shape](secret))
    bad = key if which == 'key' else secret
    header = 'APCA-API-KEY-ID' if which == 'key' else 'APCA-API-SECRET-KEY'
    monkeypatch.setenv(KEY_VAR, key)
    monkeypatch.setenv(SECRET_VAR, secret)

    # The real requests refusal, built with the value: its text carries the repr of the value (the measured leak).
    with pytest.raises(InvalidHeader) as built:
        check_header_validity((header, bad))
    exc_info = (type(built.value), built.value, built.value.__traceback__)
    raw = logging.Formatter('%(message)s').format(
        logging.LogRecord('control', logging.ERROR, __file__, 1, 'refused', None, exc_info)
    )
    assert repr(bad) in raw, 'control: the InvalidHeader traceback no longer carries the value'
    assert_not_scrubbed(bad.strip())

    with pytest.raises(MalformedCredentialsError):
        broker_api.get_client()

    assert getattr(broker_api, '__CLIENT') is None
    text = formatted('the vendor call failed', exc_info=exc_info)
    assert 'InvalidHeader' in text and REDACTED in text
    for form in (bad, bad.strip(), repr(bad), repr(bad)[1:-1]):
        assert form not in text, 'the InvalidHeader traceback still carries the malformed value'
    # The well-formed partner was registered in the same call.
    assert_scrubbed(secret if which == 'key' else key)


# ---------------------------------------------------------------------------------------------
# 3. Unset or empty: nothing registered, the error names only the variable
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize('blank', ('unset', 'empty'))
@pytest.mark.parametrize('which', ('key', 'secret', 'both'))
def test_an_unset_or_empty_value_registers_nothing_blank_and_the_error_names_only_variables(
    which, blank, monkeypatch, registered
):
    key, secret = fresh_key(), fresh_secret()
    for var, value, is_blank in ((KEY_VAR, key, which in ('key', 'both')), (SECRET_VAR, secret, which != 'key')):
        if not is_blank:
            monkeypatch.setenv(var, value)
        elif blank == 'empty':
            monkeypatch.setenv(var, '')

    with pytest.raises(MissingCredentialsError) as raised:
        broker_api.get_client()

    names = {'key': [KEY_VAR], 'secret': [SECRET_VAR], 'both': [KEY_VAR, SECRET_VAR]}[which]
    assert raised.value.detail == f'Alpaca credentials are not configured: {", ".join(names)} unset'
    assert key not in str(raised.value) and secret not in str(raised.value)
    assert all(type(value) is str and value.strip() for value in registered), 'a blank value was registered'
    assert formatted(ORDINARY) == f'ERROR test.scrub {ORDINARY}', 'ordinary text no longer formats unchanged'


@pytest.mark.parametrize('blank', ('unset', 'empty'))
@pytest.mark.parametrize('present_which', ('key', 'secret'))
def test_half_configured_get_client_registers_the_set_value_before_raising_missing(
    present_which, blank, monkeypatch, registered
):
    # Architect ruling 18:31 UTC on tj-7zjov1.76 (fix a7b0b09): registration precedes the missing check, so with one
    # variable set and the other unset or empty the set value is already scrubbed when MissingCredentialsError raises.
    present = fresh_key() if present_which == 'key' else fresh_secret()
    present_var, blank_var = (KEY_VAR, SECRET_VAR) if present_which == 'key' else (SECRET_VAR, KEY_VAR)
    monkeypatch.setenv(present_var, present)
    if blank == 'empty':
        monkeypatch.setenv(blank_var, '')
    assert_not_scrubbed(present)
    count_before = len(registered)

    with pytest.raises(MissingCredentialsError) as raised:
        broker_api.get_client()

    assert raised.value.detail == f'Alpaca credentials are not configured: {blank_var} unset'
    assert present not in str(raised.value) and present not in repr(raised.value)
    assert getattr(broker_api, '__CLIENT') is None
    new = registered[count_before:]
    assert new == [present] and type(new[0]) is str, f'registered {new!r}, not exactly the set value as a plain str'
    assert_scrubbed(present)
    assert formatted(ORDINARY) == f'ERROR test.scrub {ORDINARY}', 'ordinary text no longer formats unchanged'


# ---------------------------------------------------------------------------------------------
# 4. Read once; registered, checked and wrapped are the objects read
# ---------------------------------------------------------------------------------------------


def test_get_client_reads_each_variable_once_and_registers_checks_and_wraps_exactly_those_objects(registered):
    reads: list[tuple[str, str]] = []
    checked: list[tuple[str, str]] = []
    wrapped: list[str] = []
    events: list[str] = []
    serial = count(1)
    makers = {KEY_VAR: fresh_key, SECRET_VAR: fresh_secret}

    def read(name, *args, **kwargs):
        value = f'{makers[name]()}x{next(serial)}'
        reads.append((name, value))
        events.append('read')
        return value

    def check(credential):
        events.append('check')
        checked.append((credential.key_id, credential.secret))
        return check_credential_shape(credential)

    def wrap(value):
        events.append('wrap')
        wrapped.append(value)
        return OpaqueStr(value)

    with (
        patch.object(broker_api, 'get_env_var', side_effect=read),
        patch.object(broker_api, 'check_credential_shape', side_effect=check),
        patch.object(broker_api, 'OpaqueStr', side_effect=wrap),
    ):
        count_before = len(registered)
        broker_api.get_client()

    assert [name for name, _ in reads] == [KEY_VAR, SECRET_VAR], f'the reads were {[n for n, _ in reads]}'
    (_, key), (_, secret) = reads
    assert len(registered) - count_before == 2
    assert registered[-2] is key and registered[-1] is secret, 'what was registered is not the object read'
    assert len(checked) == 1 and checked[0][0] is key and checked[0][1] is secret
    assert len(wrapped) == 2 and wrapped[0] is key and wrapped[1] is secret
    assert events == ['read', 'read', 'check', 'wrap', 'wrap']


@pytest.mark.parametrize('which', ('key', 'secret'))
def test_the_missing_path_reads_each_variable_once_and_names_only_what_it_read_as_blank(which, registered):
    reads: list[str] = []
    blank_var = KEY_VAR if which == 'key' else SECRET_VAR

    def read(name, *args, **kwargs):
        reads.append(name)
        return None if name == blank_var else fresh_key()

    with patch.object(broker_api, 'get_env_var', side_effect=read), pytest.raises(MissingCredentialsError) as raised:
        broker_api.get_client()

    assert reads == [KEY_VAR, SECRET_VAR], f'the reads were {reads}'
    assert raised.value.detail == f'Alpaca credentials are not configured: {blank_var} unset'


@pytest.mark.parametrize('which', ('key', 'secret'))
def test_a_value_that_changes_after_the_first_read_never_changes_the_outcome(which, registered):
    # A second read anywhere (the missing check, the registration) would see a different answer from the first.
    changing = KEY_VAR if which == 'key' else SECRET_VAR
    seen: dict[str, int] = {}
    first = {KEY_VAR: fresh_key(), SECRET_VAR: fresh_secret()}

    def blank_later(name, *args, **kwargs):
        seen[name] = seen.get(name, 0) + 1
        return first[name] if name != changing or seen[name] == 1 else ''

    with patch.object(broker_api, 'get_env_var', side_effect=blank_later):
        client = broker_api.get_client()
    assert client._api_key.reveal() == first[KEY_VAR] and client._secret_key.reveal() == first[SECRET_VAR]

    broker_api.set_client(None)
    seen.clear()

    def blank_first(name, *args, **kwargs):
        seen[name] = seen.get(name, 0) + 1
        return '' if name == changing and seen[name] == 1 else fresh_key()

    with patch.object(broker_api, 'get_env_var', side_effect=blank_first), pytest.raises(MissingCredentialsError) as e:
        broker_api.get_client()
    assert e.value.detail == f'Alpaca credentials are not configured: {changing} unset'


# ---------------------------------------------------------------------------------------------
# 5. The plain str is registered, never the wrapper
# ---------------------------------------------------------------------------------------------


def test_the_registered_values_are_plain_str_and_the_marker_word_is_not_over_scrubbed(monkeypatch, registered):
    key, secret = fresh_key(), fresh_secret() + '\n'
    monkeypatch.setenv(KEY_VAR, key)
    monkeypatch.setenv(SECRET_VAR, secret)

    with pytest.raises(MalformedCredentialsError):
        broker_api.get_client()
    monkeypatch.setenv(SECRET_VAR, secret.strip())
    broker_api.get_client()

    assert registered, 'nothing was registered'
    assert all(type(value) is str for value in registered), (
        f'registered {sorted({type(v).__name__ for v in registered})}, not plain str'
    )
    assert not any(isinstance(value, OpaqueStr) for value in registered)
    assert (
        formatted('the word redacted and <redacted> stay') == 'ERROR test.scrub the word redacted and <redacted> stay'
    )


# ---------------------------------------------------------------------------------------------
# 6. StaticCredentialProvider.from_env, under arbitrary names
# ---------------------------------------------------------------------------------------------


def test_from_env_registers_both_values_under_arbitrary_variable_names(monkeypatch, registered):
    key_var, secret_var = f'SCRUB_TEST_K_{uuid4().hex[:8]}', f'SCRUB_TEST_S_{uuid4().hex[:8]}'
    key, secret = fresh_key(), fresh_secret()
    monkeypatch.setenv(key_var, key)
    monkeypatch.setenv(secret_var, secret)
    assert_not_scrubbed(key)
    assert_not_scrubbed(secret)

    (credential,) = StaticCredentialProvider.from_env(key_var, secret_var).credentials()

    assert credential.key_id == key and credential.secret == secret, 'the provider no longer holds the real values'
    assert_scrubbed(key)
    assert_scrubbed(secret)
    assert registered[-2:] == [key, secret] and all(type(v) is str for v in registered[-2:])


@pytest.mark.parametrize('blank', ('unset', 'empty'))
@pytest.mark.parametrize('which', ('key', 'secret'))
def test_from_env_registers_the_value_it_read_before_refusing_the_blank_one(which, blank, monkeypatch, registered):
    key_var, secret_var = f'SCRUB_TEST_K_{uuid4().hex[:8]}', f'SCRUB_TEST_S_{uuid4().hex[:8]}'
    present = fresh_key() if which == 'secret' else fresh_secret()
    present_var, blank_var = (key_var, secret_var) if which == 'secret' else (secret_var, key_var)
    monkeypatch.setenv(present_var, present)
    if blank == 'empty':
        monkeypatch.setenv(blank_var, '')
    assert_not_scrubbed(present)

    with pytest.raises(SchedulerConfigError) as raised:
        StaticCredentialProvider.from_env(key_var, secret_var)

    assert present not in str(raised.value)
    assert key_var in str(raised.value) and secret_var in str(raised.value)
    assert_scrubbed(present)
    assert registered[-1:] == [present], 'the blank value was registered, or the read one was not'
    assert formatted(ORDINARY) == f'ERROR test.scrub {ORDINARY}'


# ---------------------------------------------------------------------------------------------
# 7. The registry changes nothing on the wire
# ---------------------------------------------------------------------------------------------


def test_the_vendor_still_receives_the_real_registered_bytes_in_header_mode(monkeypatch, vendor: LoopbackVendor):
    key, secret = fresh_key(), fresh_secret()
    monkeypatch.setenv(KEY_VAR, key)
    monkeypatch.setenv(SECRET_VAR, secret)
    client = production_client(vendor)
    assert_scrubbed(key)
    assert_scrubbed(secret)

    broker_api.request_page(client, broker_api.BARS_PATH, {'symbols': 'VFV', 'timeframe': '1Day'})

    assert len(vendor.requests) == 1
    assert vendor.header(0, 'APCA-API-KEY-ID') == [key.encode('ascii')]
    assert vendor.header(0, 'APCA-API-SECRET-KEY') == [secret.encode('ascii')]
    assert vendor.header(0, 'Authorization') == []
    assert REDACTED.encode() not in b'\n'.join(value for _, value in vendor.requests[0][1])
