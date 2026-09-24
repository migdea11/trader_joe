"""tj-zb1di4: run the real migrations/env.py and prove the password never gets out.

test_uri_display.py tests mask_database_uri() and is_ambiguous_database_uri() as units, but a
unit test can't catch a regression IN env.py itself -- e.g. reverting env.py's log.debug call to
interpolate DATABASE_URI directly leaves a helper-only test suite green, because the helper is
still correct, it's just no longer called where it matters. This file closes that gap by
actually executing migrations/env.py.

env.py can't be imported the normal way: it reads `context.config` and calls
`context.is_offline_mode()` at module scope, both of which are attributes of alembic's
context-local proxy that only exist inside a live MigrationContext (unlike alembic.op, whose
attributes existing tests patch on an already-imported migration module -- op isn't touched
until a function runs, but context.config is touched at import time). So this fakes the
alembic.context proxy well enough for env.py's module-scope code and run_migrations_offline()
to complete without a database:
  * context.config is a REAL alembic.config.Config() with no ini file, so config_file_name is
    None (fileConfig() is skipped) and set_main_option()/get_main_option() do real configparser
    round-tripping -- which is exactly what exposed the '%' interpolation bug (AC1).
  * context.is_offline_mode() returns True, which sends env.py through
    run_migrations_offline() -- context.configure()/begin_transaction()/run_migrations() are
    then just recorded MagicMock calls, no engine, no connection, no Docker.

dotenv.load_dotenv is stubbed to a no-op so a real .env file is never read regardless of cwd.

caplog is set to DEBUG explicitly in every test that inspects it. Without that, the root logger
already has handlers installed by the time pytest runs, so common.logging.get_logger's own
`logging.basicConfig(level=logging.DEBUG)` is a no-op (basicConfig only configures a logger with
no handlers), root stays at its default WARNING, and env.py's DEBUG line is dropped before caplog
or capfd ever see it -- a test that doesn't set the level can look like it's checking the log
line while actually checking nothing. Each test that expects a log line also asserts one was
captured at all, so a test that goes blind like this fails loudly instead of passing vacuously.

Two sentinel passwords are used, not one: most DSN forms carry a password with no special
characters, but AC1 (percent-encoded '%') and the '@'-ambiguity forms are only reachable with a
password that itself contains '@' -- that's the whole reason those forms exist, so a password
without one would test nothing. The leak check for those forms is a FRAGMENT
('Tr0ub4dor-Password'), not the whole sentinel: the percent-encoded DSN's raw log text contains
'S3ntinel%40Tr0ub4dor-Password', not the decoded 'S3ntinel@Tr0ub4dor-Password', so a whole-string
check against the decoded sentinel passes vacuously on the encoded form. The fragment is
identical in both, so it catches a leak either way -- including a partial tail leak, which a
whole-sentinel check on the ambiguous forms would also miss.
"""

import logging
import runpy
import sys
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import quote

import alembic
import pytest
from alembic.config import Config


SENTINEL_PASSWORD = 'S3ntinel-Tr0ub4dor-Password'  # fixture value, not a real credential
SENTINEL_AT_PASSWORD = 'S3ntinel@Tr0ub4dor-Password'  # fixture value, not a real credential
SENTINEL_AT_PASSWORD_FRAGMENT = 'Tr0ub4dor-Password'

ENV_PY = Path(__file__).resolve().parents[1] / 'migrations' / 'env.py'

# name -> (dsn, the fragment that must not survive into any log/stdout/stderr/exception text)
WELL_FORMED_DSN_FORMS = {
    'plain': (f'postgresql://joe:{SENTINEL_PASSWORD}@db:5432/market_data', SENTINEL_PASSWORD),
    'query-password': (f'postgresql://joe@db:5432/market_data?password={SENTINEL_PASSWORD}', SENTINEL_PASSWORD),
    'unix-socket': (
        f'postgresql://joe@/market_data?host=/var/run/postgresql&password={SENTINEL_PASSWORD}',
        SENTINEL_PASSWORD,
    ),
    # AC1: the correct, escaped way to put '@' in a password. Literal '%' in the DSN is exactly
    # what used to make config.set_main_option() raise with the raw URI in the message.
    'percent-encoded': (
        f'postgresql://joe:{quote(SENTINEL_AT_PASSWORD, safe="")}@db:5432/market_data',
        SENTINEL_AT_PASSWORD_FRAGMENT,
    ),
}

# Same idea, but the password's '@' is left unescaped -- env.py must refuse these outright
# rather than try to mask and continue, since the ambiguous DSN still reaches psycopg2 in
# run_migrations_online() and psycopg2's own error names whatever it mis-parsed as the host.
AMBIGUOUS_DSN_FORMS = {
    'raw-at-then-host': (f'postgresql://joe:{SENTINEL_AT_PASSWORD}@db:5432/market_data', SENTINEL_AT_PASSWORD_FRAGMENT),
    'raw-at-then-slash': (
        f'postgresql://joe:{SENTINEL_AT_PASSWORD}/tail-segment@db:5432/market_data',
        SENTINEL_AT_PASSWORD_FRAGMENT,
    ),
    'raw-at-then-question-mark': (
        f'postgresql://joe:{SENTINEL_AT_PASSWORD}?tail-param@db:5432/market_data',
        SENTINEL_AT_PASSWORD_FRAGMENT,
    ),
}


@pytest.fixture
def fake_alembic_context(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace alembic.context with a MagicMock backed by a real, file-less Config()."""
    mock_context = MagicMock(name='alembic.context')
    mock_context.config = Config()
    mock_context.is_offline_mode.return_value = True

    monkeypatch.setitem(sys.modules, 'alembic.context', mock_context)
    monkeypatch.setattr(alembic, 'context', mock_context, raising=False)
    return mock_context


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch: pytest.MonkeyPatch):
    """env.py calls load_dotenv('.env') at import time -- never touch a real file in tests."""
    import dotenv

    monkeypatch.setattr(dotenv, 'load_dotenv', lambda *args, **kwargs: None)


def _run_env_py(database_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('DATABASE_URI', database_uri)
    runpy.run_path(str(ENV_PY), run_name='__env_py_under_test__')


@pytest.mark.parametrize('form', sorted(WELL_FORMED_DSN_FORMS))
def test_env_py_logs_the_url_without_leaking_the_password(
    form: str,
    fake_alembic_context: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture,
):
    database_uri, fragment = WELL_FORMED_DSN_FORMS[form]
    caplog.set_level(logging.DEBUG)

    try:
        _run_env_py(database_uri, monkeypatch)
    except BaseException as exc:  # want to inspect the message even on an unexpected failure
        assert fragment not in str(exc), f'exception leaked the password: {exc}'
        raise

    # The test must actually have observed the line it's checking -- otherwise a dropped
    # record (see module docstring) makes every assertion below pass vacuously.
    assert any('Setting up postgres URL' in record.getMessage() for record in caplog.records), (
        f'expected a "Setting up postgres URL" record, got: {[r.getMessage() for r in caplog.records]}'
    )

    for record in caplog.records:
        assert fragment not in record.getMessage(), f'log record leaked the password: {record.getMessage()}'

    captured = capfd.readouterr()
    assert fragment not in captured.out, f'stdout leaked the password: {captured.out}'
    assert fragment not in captured.err, f'stderr leaked the password: {captured.err}'


@pytest.mark.parametrize('form', sorted(AMBIGUOUS_DSN_FORMS))
def test_env_py_refuses_an_ambiguous_database_uri(
    form: str,
    fake_alembic_context: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture,
):
    database_uri, fragment = AMBIGUOUS_DSN_FORMS[form]
    caplog.set_level(logging.DEBUG)

    with pytest.raises(RuntimeError) as excinfo:
        _run_env_py(database_uri, monkeypatch)

    assert fragment not in str(excinfo.value), f'exception leaked the password: {excinfo.value}'
    assert 'ambiguous' in str(excinfo.value).lower()

    # Refused before config.set_main_option() / any engine is touched -- config never even
    # got the URL, so there's nothing downstream (e.g. psycopg2) left to leak it either.
    assert fake_alembic_context.config.get_main_option('sqlalchemy.url', None) is None
    fake_alembic_context.run_migrations.assert_not_called()

    for record in caplog.records:
        assert fragment not in record.getMessage(), f'log record leaked the password: {record.getMessage()}'

    captured = capfd.readouterr()
    assert fragment not in captured.out, f'stdout leaked the password: {captured.out}'
    assert fragment not in captured.err, f'stderr leaked the password: {captured.err}'


def test_env_py_still_configures_the_real_url_and_runs_offline_migrations(
    fake_alembic_context: MagicMock, monkeypatch: pytest.MonkeyPatch
):
    """Masking the log line must not touch the URL alembic actually connects with."""
    database_uri, _ = WELL_FORMED_DSN_FORMS['plain']
    _run_env_py(database_uri, monkeypatch)

    config = fake_alembic_context.config
    assert config.get_main_option('sqlalchemy.url') == database_uri

    fake_alembic_context.run_migrations.assert_called_once()


def test_env_py_configures_the_percent_encoded_url_without_raising(
    fake_alembic_context: MagicMock, monkeypatch: pytest.MonkeyPatch
):
    """AC1: a percent-encoded password must reach configparser without exploding on '%'."""
    database_uri, _ = WELL_FORMED_DSN_FORMS['percent-encoded']
    _run_env_py(database_uri, monkeypatch)

    config = fake_alembic_context.config
    assert config.get_main_option('sqlalchemy.url') == database_uri
