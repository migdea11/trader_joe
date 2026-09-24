"""tj-zb1di4: mask_database_uri() must never let a password reach its return value.

migrations/env.py used to do `log.debug(f'Setting up postgres URL: {database_uri}')`, which put
the postgres password in plain text in `make migrate` output -- and the same migrate step runs
in this public repo's CI. The fix moved the masking into mask_database_uri()
(data/store/app/database/uri_display.py) so env.py's log line can only ever contain its output.

This file tests the helper as a unit. It does NOT prove env.py actually calls it correctly --
a helper-only suite stayed green when env.py's log line was reverted to raw interpolation
during review, since the helper itself was still fine. That coverage now lives in
test_env_uri_masking.py, which executes the real env.py.
"""

from urllib.parse import quote

import pytest

from data.store.app.database.uri_display import is_ambiguous_database_uri, mask_database_uri


SENTINEL_PASSWORD = 'S3ntinel-Tr0ub4dor-Password'  # fixture value, not a real credential
# Contains a raw '@', which is what makes the unescaped-@ cases below reachable.
SENTINEL_AT_PASSWORD = 'S3ntinel@Tr0ub4dor-Password'  # fixture value, not a real credential
# The fragment that survives BOTH the decoded form ('S3ntinel@Tr0ub4dor-Password') and the
# percent-encoded form ('S3ntinel%40Tr0ub4dor-Password') unchanged -- only the '@' differs
# between them, so this is what a leak check has to look for to catch either.
SENTINEL_AT_PASSWORD_FRAGMENT = 'Tr0ub4dor-Password'


@pytest.fixture
def database_uri() -> str:
    return f'postgresql://joe:{SENTINEL_PASSWORD}@db:5432/market_data'


def test_mask_database_uri_hides_the_password(database_uri: str):
    masked = mask_database_uri(database_uri)
    assert SENTINEL_PASSWORD not in masked
    # Everything else about the DSN stays legible -- this is meant to be read, not redacted whole.
    assert 'db:5432/market_data' in masked
    assert 'joe' in masked


def test_mask_database_uri_handles_unset():
    for empty in (None, ''):
        assert SENTINEL_PASSWORD not in mask_database_uri(empty)


def test_mask_database_uri_handles_malformed_input_without_leaking():
    # A value that still carries the password but fails to parse as a URL -- e.g. a copy-paste
    # that lost its scheme. Must not raise, and must not echo the input back.
    malformed = f'not-a-uri::{SENTINEL_PASSWORD}'
    masked = mask_database_uri(malformed)
    assert SENTINEL_PASSWORD not in masked
    assert malformed not in masked


def test_mask_database_uri_hides_a_password_carried_in_the_query_string():
    # libpq accepts `?password=...` exactly as it accepts `user:password@` -- SQLAlchemy's own
    # hide_password only touches the latter, so this key needs its own masking.
    uri = f'postgresql://joe@db:5432/market_data?password={SENTINEL_PASSWORD}'
    masked = mask_database_uri(uri)
    assert SENTINEL_PASSWORD not in masked
    assert 'joe' in masked


def test_mask_database_uri_hides_a_query_string_password_in_the_socket_form():
    # The unix-socket DSN form: host arrives as a query param too, alongside password.
    uri = f'postgresql://joe@/market_data?host=/var/run/postgresql&password={SENTINEL_PASSWORD}'
    masked = mask_database_uri(uri)
    assert SENTINEL_PASSWORD not in masked
    assert 'joe' in masked


def test_mask_database_uri_hides_a_query_string_password_key_in_any_case():
    # libpq itself only honours the lowercase key, so 'Password'/'PASSWORD' never connects --
    # but the value an operator typed is still a real credential, and still worth masking.
    uri = f'postgresql://joe@db:5432/market_data?Password={SENTINEL_PASSWORD}'
    masked = mask_database_uri(uri)
    assert SENTINEL_PASSWORD not in masked


def test_mask_database_uri_handles_a_non_numeric_port_without_raising():
    # make_url raises ValueError (not ArgumentError) when a component parses but fails to cast,
    # e.g. a non-numeric port -- the helper's docstring promises a placeholder either way.
    uri = f'postgresql://joe:{SENTINEL_PASSWORD}@db:notaport/market_data'
    masked = mask_database_uri(uri)
    assert SENTINEL_PASSWORD not in masked


def test_mask_database_uri_falls_back_to_the_placeholder_for_an_unescaped_at_in_the_password():
    # docker-compose.yaml interpolates POSTGRES_PASS unencoded, so a password containing a raw,
    # unescaped '@' is reachable. make_url mis-splits on the first '@', folding the remainder of
    # the password into what it thinks is the host -- which then survives hide_password=True
    # because it was never parsed as the password component at all.
    uri = f'postgresql://joe:{SENTINEL_AT_PASSWORD}@db:5432/market_data'
    masked = mask_database_uri(uri)
    assert SENTINEL_AT_PASSWORD_FRAGMENT not in masked


def test_mask_database_uri_hides_a_correctly_percent_encoded_at_in_the_password():
    # The correctly-escaped counterpart of the case above: '@' encoded as %40. This must parse
    # unambiguously and mask cleanly -- it's env.py's config.set_main_option() that chokes on
    # the literal '%', not this helper, which is covered separately in test_env_uri_masking.py.
    uri = f'postgresql://joe:{quote(SENTINEL_AT_PASSWORD, safe="")}@db:5432/market_data'
    masked = mask_database_uri(uri)
    assert SENTINEL_AT_PASSWORD_FRAGMENT not in masked
    assert 'joe' in masked
    assert 'db:5432/market_data' in masked


@pytest.mark.parametrize(
    'raw_password',
    [
        pytest.param(SENTINEL_AT_PASSWORD, id='at-then-host'),
        pytest.param(f'{SENTINEL_AT_PASSWORD}/tail-segment', id='at-then-slash'),
        pytest.param(f'{SENTINEL_AT_PASSWORD}?tail-param', id='at-then-question-mark'),
    ],
)
def test_mask_database_uri_falls_back_on_any_dsn_with_more_than_one_at(raw_password: str):
    # A parsed-field check (e.g. "does url.host contain '@'") only catches SOME of these -- a
    # raw '@' combined with '/' or '?' spreads the leaked remainder across host, path or query
    # instead. The raw string always has exactly one '@' when the password is correctly
    # encoded, so counting on the raw string catches all three uniformly.
    uri = f'postgresql://joe:{raw_password}@db:5432/market_data'
    assert is_ambiguous_database_uri(uri)
    masked = mask_database_uri(uri)
    assert SENTINEL_AT_PASSWORD_FRAGMENT not in masked


def test_is_ambiguous_database_uri_accepts_a_correctly_encoded_dsn():
    uri = f'postgresql://joe:{quote(SENTINEL_AT_PASSWORD, safe="")}@db:5432/market_data'
    assert not is_ambiguous_database_uri(uri)
