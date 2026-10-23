"""The instance secret and the database password never reach output, an exception or a traceback (validator).

Both are fakes with canaries (doubles.SECRET, doubles.DB_PASSWORD). Each test drives a path, success or failure, with
stdout and stderr captured as well as the Reporter, and asserts that no canary appears anywhere. The failure doubles
quote the credential in their own exception text, the way a library might, so 'reported by type name only' is
what keeps it out.

tj-grna9p.148: the reset is a database path like degrade, so it is driven here too; seed runs with --yes-reset (a
reset, then the plan) or --no-reset where the test is about its POSTs.
"""

import traceback

import pytest

from tools.dev_datasets import request as tool
from tools.dev_datasets.tests.doubles import (
    DB_PASSWORD,
    GOOD_ENV,
    NO_RESET,
    SECRET,
    WRITING_MODES,
    FakeDb,
    assert_no_canary,
    exploding_connect,
    run_tool,
    store_for,
)


pytestmark = pytest.mark.build_infra


class QuotingHttp:
    """A transport that fails the way a library might: quoting the headers it was handed, secret included."""

    def __init__(self, fail_on: str):
        self.fail_on = fail_on
        self.inner = store_for(present={'r4', 'r7', 'r11'})

    def request(self, method, path, *, headers=None, body=None, timeout=60):
        if method == self.fail_on or path.startswith(self.fail_on):
            raise ValueError(f'invalid header value {headers!r} for {path} with {GOOD_ENV!r}')
        return self.inner.request(method, path, headers=headers, body=body, timeout=timeout)


@pytest.mark.parametrize('mode', [*WRITING_MODES, 'reset', 'check'])
@pytest.mark.parametrize('dry', [False, True])
def test_no_mode_shows_a_credential_on_success(mode, dry):
    db = FakeDb.with_plan_entries()
    confirm = ['--yes-reset'] if mode in ('seed', 'reset') and not dry else []
    argv = [mode, '--dry-run'] if dry else [mode, *confirm]
    outcome = run_tool(argv, http=store_for(present={'r4', 'r7', 'r11'}), connect=db.connect)
    assert outcome.status == 0, outcome.text
    assert_no_canary(outcome.text)


@pytest.mark.parametrize('mode', ['seed', 'repair'])
def test_a_failing_post_that_quotes_its_headers_shows_the_type_only(mode):
    outcome = run_tool([mode, NO_RESET] if mode == 'seed' else [mode], http=QuotingHttp('POST'))
    assert outcome.status == 1
    assert outcome.lines[-1] == 'failed (ValueError)'
    assert_no_canary(outcome.text)


@pytest.mark.parametrize('mode', ['seed', 'reset', 'reads', 'degrade', 'repair', 'check'])
def test_a_failing_get_that_quotes_the_environment_shows_the_type_only(mode):
    argv = [mode, '--yes-reset'] if mode in ('seed', 'reset') else [mode]
    outcome = run_tool(argv, http=QuotingHttp('/ui/v1/config'), connect=FakeDb.with_plan_entries().connect)
    assert outcome.status == 1
    assert_no_canary(outcome.text)


@pytest.mark.parametrize('status', [401, 403, 409, 422, 429, 500, 503])
def test_a_refusal_whose_body_echoes_the_secret_does_not_print_it(status):
    store = store_for()
    store.post_answers = {
        'r1': tool.Response(status, {'reason': 'X', 'error_id': 'e', 'detail': SECRET, 'headers': {'x': SECRET}})
    }
    outcome = run_tool(['seed', NO_RESET], http=store)
    assert outcome.status == 1
    assert_no_canary(outcome.text)


DB_ARGVS = [['degrade'], ['reset', '--yes-reset'], ['reset'], ['seed', '--yes-reset'], ['seed'], ['seed', '--dry-run']]


@pytest.mark.parametrize('argv', DB_ARGVS, ids=lambda argv: '-'.join(argv))
def test_a_database_failure_that_quotes_the_password_shows_the_type_only(argv):
    db = FakeDb.with_plan_entries()
    db.connect_error = RuntimeError(f'FATAL: password authentication failed: password={DB_PASSWORD}')
    outcome = run_tool(argv, http=store_for(), connect=db.connect)
    assert outcome.status == 1
    assert 'the database connection failed (RuntimeError)' in outcome.text
    assert_no_canary(outcome.text)
    db = FakeDb.with_plan_entries()
    db.execute_error = RuntimeError(f'dsn=postgresql://postgres:{DB_PASSWORD}@db:5432/postgres')
    outcome = run_tool(argv, http=store_for(), connect=db.connect)
    assert outcome.status == 1
    assert '(RuntimeError)' in outcome.lines[-1]
    assert_no_canary(outcome.text)


@pytest.mark.parametrize('failing', ['lock_timeout', "state = 'RUNNING'", 'count(*)', 'TRUNCATE'])
def test_a_reset_failing_at_any_statement_with_the_password_in_its_text_shows_the_type_only(failing):
    db = FakeDb.with_plan_entries()
    db.execute_error = RuntimeError(f'password={DB_PASSWORD} dsn=postgresql://postgres:{DB_PASSWORD}@db/postgres')
    db.fail_on = failing
    outcome = run_tool(['reset', '--yes-reset'], http=store_for(), connect=db.connect)
    assert outcome.status == 1
    assert outcome.lines[-1] == 'the reset transaction failed and was rolled back (RuntimeError)'
    assert_no_canary(outcome.text)


# ---- the quiet-stack paths (tj-grna9p.150 (5)) ------------------------------------------------------------------


@pytest.mark.parametrize('argv', [['reset', '--yes-reset'], ['seed', '--yes-reset']])
def test_the_running_refusal_shows_no_credential(argv):
    db = FakeDb.with_plan_entries()
    db.running = 1
    outcome = run_tool(argv, http=store_for(), connect=db.connect)
    assert outcome.status == 1
    assert 'RUNNING' in outcome.lines[-1]
    assert_no_canary(outcome.text)


@pytest.mark.parametrize('argv', [['reset', '--yes-reset'], ['seed', '--yes-reset']])
def test_not_clean_shows_no_credential(argv):
    db = FakeDb.with_plan_entries()
    db.written_while_waiting = {'ingest_run': 1, 'dataset_read': 1}
    outcome = run_tool(argv, http=store_for(), connect=db.connect)
    assert outcome.status == 1
    assert outcome.lines[-1].startswith('NOT CLEAN: ')
    assert_no_canary(outcome.text)


def test_a_recount_whose_connection_fails_quoting_the_password_shows_the_type_only():
    db = FakeDb.with_plan_entries()

    def stack_goes_away(_seconds):
        db.connect_error = RuntimeError(f'FATAL: password authentication failed: password={DB_PASSWORD}')

    outcome = run_tool(['reset', '--yes-reset'], http=store_for(), connect=db.connect, sleep=stack_goes_away)
    assert outcome.status == 1
    assert outcome.lines[-1] == 'the database connection failed (RuntimeError)'
    assert not any('verified clean' in line for line in outcome.lines)
    assert_no_canary(outcome.text)


def test_a_recount_whose_count_fails_quoting_the_password_shows_the_type_only():
    db = FakeDb.with_plan_entries()

    def count_fails(_seconds):
        db.execute_error = RuntimeError(f'dsn=postgresql://postgres:{DB_PASSWORD}@db/postgres')

    outcome = run_tool(['reset', '--yes-reset'], http=store_for(), connect=db.connect, sleep=count_fails)
    assert outcome.status == 1
    assert outcome.lines[-1] == 'the post-reset recount failed (RuntimeError)'
    assert not any('verified clean' in line for line in outcome.lines)
    # The recount only read; it rolls its connection back and closes it.
    assert db.events[-2:] == [('rollback',), ('close',)]
    assert_no_canary(outcome.text)


def test_the_real_connect_failing_shows_the_type_only(monkeypatch):
    import sys
    import types

    fake = types.ModuleType('psycopg2')

    def connect(**kwargs):
        raise RuntimeError(f'connection to {kwargs!r} failed')

    fake.connect = connect
    monkeypatch.setitem(sys.modules, 'psycopg2', fake)
    outcome = run_tool(['degrade'], http=store_for())
    assert outcome.status == 1
    assert 'the database connection failed (RuntimeError)' in outcome.text
    assert_no_canary(outcome.text)


def test_an_unexpected_error_anywhere_is_reported_by_type_only(monkeypatch):
    def boom(*args, **kwargs):
        raise KeyError(f'{SECRET} {DB_PASSWORD}')

    monkeypatch.setattr(tool, 'run_seed', boom)
    outcome = run_tool(['seed', NO_RESET], http=store_for())
    assert outcome.lines[-1] == 'failed (KeyError)'
    assert_no_canary(outcome.text)


def test_the_credential_wrappers_are_masked_in_every_rendering():
    env = tool.load_environment(GOOD_ENV)
    for rendering in (
        repr(env),
        str(env),
        f'{env}',
        repr(env.secret),
        str(env.secret),
        f'{env.db_password}',
        repr(vars(env)) if hasattr(env, '__dict__') else '',
    ):
        assert_no_canary(rendering)
    assert env.secret.reveal() == SECRET
    assert env.db_password.reveal() == DB_PASSWORD


def test_check_reports_set_without_the_values():
    outcome = run_tool(['check'], http=store_for())
    assert outcome.lines[:2] == ['secret: set', 'db password: set']
    assert_no_canary(outcome.text)


def test_a_transport_failure_of_the_real_client_carries_neither_value_nor_context():
    # Port 9 on loopback (discard) is closed in the test container, so the connect is refused at once.
    client = tool.HttpClient('http://127.0.0.1:9')
    with pytest.raises(tool.DevDatasetsError) as raised:
        client.request('POST', '/store/x', headers={'X-Instance-Secret': SECRET}, body={'owner': 'o'}, timeout=5)
    rendered = ''.join(traceback.format_exception(raised.value))
    assert 'failed below HTTP' in str(raised.value)
    assert_no_canary(rendered)
    assert raised.value.__suppress_context__ is True


def test_a_header_value_http_client_would_quote_never_reaches_it():
    # http.client puts a malformed header value in its ValueError; the tool refuses the value before the call.
    store = store_for()
    env = {**GOOD_ENV, 'INSTANCE_WRITE_SECRET': SECRET + '\r\nX-Injected: 1'}
    outcome = run_tool(['seed', NO_RESET, '--row', 'r1'], env, http=store, connect=exploding_connect)
    assert outcome.status == 1
    assert_no_canary(outcome.text)
    assert store.posts() == []
