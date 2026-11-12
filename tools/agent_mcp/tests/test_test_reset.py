"""test_reset: the TEST database cleared in place (tj-w0rddi).

Design: the bead's RECOMMENDED DESIGN. The verb acts on TEST mode alone, with the rails of run_system_tests
and seed_dump (only test mode up, the postgres data mount exactly <repo>/volume_data/test/postgres, every
running data_store and data_ingest source stamp equal to the root checkout's), then runs ONE `docker exec` as
user postgres in the postgres container that drops and recreates the public schema. Nothing is deleted, no
host path or bind mount is used, the stack stays up, no call is counted or limited by volume (ADR tj-4rr0la
addendum 33), and no secret passes through the server.

WHAT IS PINNED
    admitted      the rails in order, then the one exec, the response's database and revision
    refusals      dev mode, prod, mixed, nothing up, a wrong mount, a stale stamp, no data_store: each runs
                  no exec
    volume        CALL_VOLUME admitted calls in a row each run their one exec, and none is refused
    the exec      word for word, as user postgres with NO read-only option, bound to its one tail
    the SQL       RUN under sh with a fake psql: the exact statements, in order, with the container's own
                  user and database, and every fake secret value redacted on every path
    the result    a failed or unclean reset is 'failed'; no row, only names and counts, is ever reported

Expected values are literals, never read from stack.py: a test that read them from the module under test
would agree with any change to it. The one exception is the script text, which is pinned by running it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import (
    CALL_VOLUME,
    DEV_CONTAINER_ID,
    DEV_INGEST_CONTAINER_ID,
    OLD_BUDGET_REFUSAL,
    TEST_RESET_STDOUT,
    Rig,
    assert_no_budget_wording,
    assert_no_call_counter,
    default_response,
    is_base_step,
    is_client_step,
    is_container_stamp_read,
    is_dev_digest,
    is_dev_exec,
    is_dev_lookup,
    is_mode_guard,
    is_postgres_mount_read,
    make_rig,
    postgres_test_source,
    system_suite_response,
)
from tools.agent_mcp.tests.test_dev_label_verbs import DEV_LINE, GUARD_REFUSAL, MIXED_REFUSAL, PROD_LINE


pytestmark = pytest.mark.build_infra

DOCKER = '/usr/local/bin/docker'
MODE_REFUSAL = 'the system suite runs only in test mode: call test_up first; your dev data is not touched'
STALE_STACK = 'stale test stack: call test_build, then test_up'
NO_DATA_STORE = 'no running data_store container in the trader_joe_dev project: call test_up'
# The statements, from literals.
EXPECTED_RESET_SQL = (
    "SET lock_timeout = '15s'; "
    'DROP SCHEMA IF EXISTS public CASCADE; '
    'CREATE SCHEMA public AUTHORIZATION pg_database_owner; '
    "COMMENT ON SCHEMA public IS 'standard public schema'; "
    'GRANT USAGE ON SCHEMA public TO PUBLIC'
)
EXPECTED_VERIFY_SQL = (
    "SELECT 'database', current_database()::text "
    "UNION ALL SELECT 'schema_owner', pg_get_userbyid(nspowner)::text FROM pg_namespace WHERE nspname = 'public' "
    "UNION ALL SELECT 'alembic_version', CASE WHEN to_regclass('public.alembic_version') IS NULL "
    "THEN 'absent' ELSE 'present' END "
    "UNION ALL SELECT 'public_relations', count(*)::text FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'"
)
TEST_LINE = b'/r/docker-compose.yaml,/r/docker-compose.fake.yaml\n'


def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, respond=None) -> Rig:
    """A rig answering TEST mode and the test mount; RESPOND, when given, answers before that."""
    rig = make_rig(tmp_path, monkeypatch)
    admitted = system_suite_response(rig.layout)
    rig.docker.respond = admitted if respond is None else (lambda step: respond(step) or admitted(step))
    return rig


def _call(rig: Rig) -> dict:
    return rig.call('test_reset', {})


def _answer(predicate, result: runner.ProcessResult):
    return lambda step: result if predicate(step) else None


def _ok(stdout: bytes) -> runner.ProcessResult:
    return runner.ProcessResult(0, stdout, b'')


def _is_lookup_of(service: str):
    return lambda step: is_dev_lookup(step) and f'label=com.docker.compose.service={service}' in step.argv


def _execs(rig: Rig) -> list:
    return [step for step in rig.docker.steps if is_dev_exec(step)]


def _refused_with_no_exec(rig: Rig, result: dict, message: str | None = None) -> None:
    assert result['status'] == 'refused', result
    if message is not None:
        assert result['message'] == message or result['message'].endswith(message), result
    assert _execs(rig) == [], [step.argv for step in rig.docker.steps]
    assert not [step for step in rig.docker.steps if is_client_step(step) or is_base_step(step)]
    assert_no_call_counter(rig)
    assert not rig.layout.snapshot.exists(), 'test_reset reads no worktree and takes no snapshot'


# --- the admitted call -------------------------------------------------------------------------------


def test_an_admitted_call_runs_the_rails_in_order_then_one_exec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    result = _call(rig)
    assert result['status'] == 'ok', result

    def kind(step) -> str:
        if is_mode_guard(step):
            return 'guard'
        if is_postgres_mount_read(step):
            return 'mount'
        if is_dev_lookup(step):
            return (
                'lookup:'
                + next(w for w in step.argv if w.startswith('label=com.docker.compose.service=')).split('=')[-1]
            )
        if is_dev_digest(step):
            return 'digest'
        if is_container_stamp_read(step):
            return f'stamp:{step.argv[-1]}'
        if is_dev_exec(step):
            return 'exec'
        return f'other:{step.argv[:3]}'

    assert [kind(step) for step in rig.docker.steps] == [
        'guard',
        'lookup:postgres',
        'mount',
        'lookup:data_store',
        'digest',
        'digest',
        'lookup:data_ingest',
        f'stamp:{DEV_CONTAINER_ID}',
        f'stamp:{DEV_INGEST_CONTAINER_ID}',
        'exec',
    ]
    assert_no_call_counter(rig)
    assert not rig.layout.snapshot.exists() and rig.git.calls == [], 'no worktree is read'


def test_the_exec_is_one_plain_docker_exec_as_postgres_with_no_read_only_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    (step,) = _execs(rig)
    assert list(step.argv) == [DOCKER, 'exec', '-u', 'postgres', DEV_CONTAINER_ID, 'sh', '-c', step.argv[-1]]
    assert step.argv[-1] == stack.TEST_RESET_SCRIPT
    assert step.env == () and step.builds is False and step.stdout_cap is None
    assert step.cwd == rig.layout.stack_dir


def test_the_exec_targets_the_postgres_container_whose_mount_was_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The id the mount rail checked is the id the exec runs in, not a second lookup that could differ."""
    rig = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    (mount,) = [step for step in rig.docker.steps if is_postgres_mount_read(step)]
    (step,) = _execs(rig)
    assert step.argv[-4] == mount.argv[-1]
    assert len([s for s in rig.docker.steps if _is_lookup_of('postgres')(s)]) == 1


def test_the_response_names_the_database_and_the_revision_and_no_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    result = _call(rig)
    assert result['status'] == 'ok'
    assert result['message'] == (
        'test database postgres reset in place: the public schema is recreated and empty, '
        'alembic revision: none; the stack is still up: call migrate'
    )
    assert result['details'] == {'database': 'postgres', 'alembic_revision': None, 'public_relations': 0}
    assert result['exit_status'] == 0


def test_a_row_echoed_by_the_database_is_in_neither_the_message_nor_the_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Only the four named lines are parsed into the result; any other line stays in the redacted step output."""
    rows = TEST_RESET_STDOUT + b'dataset_read|ROWSENTINEL_9d41\nversion_num|ROWSENTINEL_9d41\n'
    rig = _rig(tmp_path, monkeypatch, _answer(is_dev_exec, _ok(rows)))
    result = _call(rig)
    assert result['status'] == 'ok'
    assert 'ROWSENTINEL_9d41' not in json.dumps({k: result[k] for k in ('message', 'details')})
    assert all('ROWSENTINEL_9d41' not in line for line in rig.audit_lines())


def test_the_call_is_audited_with_no_arguments_and_no_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    [line] = rig.audit_lines()
    audit = json.loads(line)
    assert audit['verb'] == 'test_reset' and audit['arguments'] == {} and audit['status'] == 'ok'
    assert 'schema' not in line.lower()


def test_the_verb_takes_no_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    result = rig.call('test_reset', {'database': 'postgres'})
    assert result['status'] == 'refused' and "no argument named 'database'" in result['message'], result
    assert rig.docker.steps == []
    assert_no_call_counter(rig)
    assert runner.VERB_SCHEMAS['test_reset'] == {
        'properties': {},
        'required': [],
        'type': 'object',
        'additionalProperties': False,
    }


def test_the_verb_is_a_handler_with_a_timeout_under_the_client_margin():
    assert runner.VERB_TIMEOUT_SECONDS['test_reset'] == 300
    description = runner.AgentStack(settings=None).describe('test_reset')  # type: ignore[arg-type]
    for fragment in (
        'ERASES THE TEST DATABASE',
        'never your dev data',
        'IN PLACE',
        'user postgres',
        'no file is deleted',
        'test mode',
        '<repo>/volume_data/test/postgres',
        'source stamp',
        'migrate',
        'redacted',
        'no row is ever printed',
        'permission for this verb is the owner',
    ):
        assert fragment in description, fragment
    assert_no_budget_wording(description)


# --- the rails: every refusal runs no exec ----------------------------------------

_MODES = {
    'nothing is up': (b'', MODE_REFUSAL),
    'dev mode': (DEV_LINE, MODE_REFUSAL),
    'a dev data_ingest': (b'/r/docker-compose.yaml,/r/docker-compose.override.yaml\n', MODE_REFUSAL),
    'prod': (PROD_LINE, GUARD_REFUSAL),
    'dev and test mixed': (DEV_LINE + TEST_LINE, MIXED_REFUSAL),
}


@pytest.mark.parametrize(('guard', 'message'), list(_MODES.values()), ids=list(_MODES))
def test_every_mode_but_test_refuses_at_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard: bytes, message: str
):
    rig = _rig(tmp_path, monkeypatch, _answer(is_mode_guard, _ok(guard)))
    result = _call(rig)
    _refused_with_no_exec(rig, result, message)
    assert [list(step.argv) for step in rig.docker.steps] == [list(rig.docker.steps[0].argv)], 'only the guard ran'


def test_a_failed_guard_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch, _answer(is_mode_guard, runner.ProcessResult(1, b'', b'daemon down')))
    _refused_with_no_exec(rig, _call(rig))


def _mounts(layout) -> dict[str, bytes]:
    expected = postgres_test_source(layout)
    host = str(layout.repo_host_path)
    return {
        'the dev data directory': f'{host}/volume_data/postgres\n'.encode(),
        'no such mount': b'',
        'a bind elsewhere': b'/srv/postgres\n',
        'the same directory spelled through the other path': f'{layout.repo}/volume_data/test/postgres\n'.encode(),
        'a dot-dot spelling': f'{host}/volume_data/test/../test/postgres\n'.encode(),
        'a trailing slash': f'{expected}/\n'.encode(),
        'a sub-directory': f'{expected}/sub\n'.encode(),
        'two mounts': f'{expected}\n{expected}\n'.encode(),
    }


@pytest.mark.parametrize('case', list(_mounts(type('L', (), {'repo_host_path': Path('/h'), 'repo': Path('/r')})())))
def test_any_mount_but_the_test_directory_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str):
    rig = _rig(tmp_path, monkeypatch)
    stdout = _mounts(rig.layout)[case]
    admitted = rig.docker.respond
    rig.docker.respond = lambda step: _ok(stdout) if is_postgres_mount_read(step) else admitted(step)
    result = _call(rig)
    _refused_with_no_exec(
        rig, result, f'the system suite writes only the postgres on {postgres_test_source(rig.layout)}: call test_up'
    )
    assert is_postgres_mount_read(rig.docker.steps[-1])


def test_a_failed_mount_read_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch, _answer(is_postgres_mount_read, runner.ProcessResult(1, b'', b'no container')))
    result = _call(rig)
    assert 'its data mount is unknown' in result['message'], result
    _refused_with_no_exec(rig, result)


@pytest.mark.parametrize(
    ('stdout', 'fragment'), [(b'', 'no running postgres'), (b'aaaaaaaaaaaa\nbbbbbbbbbbbb\n', '2 running postgres')]
)
def test_no_or_several_postgres_refuse_before_the_mount_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes, fragment: str
):
    rig = _rig(tmp_path, monkeypatch, _answer(_is_lookup_of('postgres'), _ok(stdout)))
    result = _call(rig)
    assert fragment in result['message'], result
    _refused_with_no_exec(rig, result)
    assert not [step for step in rig.docker.steps if is_postgres_mount_read(step)]


def test_no_running_data_store_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch, _answer(_is_lookup_of('data_store'), _ok(b'')))
    _refused_with_no_exec(rig, _call(rig), NO_DATA_STORE)


def _stamp_of(container: str, stamp: bytes, exit_status: int = 0):
    return _answer(
        lambda step: is_container_stamp_read(step) and step.argv[-1] == container,
        runner.ProcessResult(exit_status, stamp, b''),
    )


_STAMPS = {
    'data_store stale': (DEV_CONTAINER_ID, b'f' * 64 + b'\n', 0),
    'data_ingest stale': (DEV_INGEST_CONTAINER_ID, b'f' * 64 + b'\n', 0),
    'data_store unstamped': (DEV_CONTAINER_ID, b'\n', 0),
    'data_ingest unreadable': (DEV_INGEST_CONTAINER_ID, b'', 1),
}


@pytest.mark.parametrize(('container', 'stamp', 'exit_status'), list(_STAMPS.values()), ids=list(_STAMPS))
def test_a_stale_running_container_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container: str, stamp: bytes, exit_status: int
):
    rig = _rig(tmp_path, monkeypatch, _stamp_of(container, stamp, exit_status))
    result = _call(rig)
    _refused_with_no_exec(rig, result, STALE_STACK)
    assert is_container_stamp_read(rig.docker.steps[-1]) and rig.docker.steps[-1].argv[-1] == container


def test_a_failed_digest_fails_and_runs_nothing_after(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch, _answer(is_dev_digest, runner.ProcessResult(1, b'', b'boom')))
    result = _call(rig)
    assert result['status'] == 'failed' and result['message'].endswith('nothing was run'), result
    assert _execs(rig) == []
    assert_no_call_counter(rig)


def test_a_stale_mcp_image_refuses_before_any_docker_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    # The trusted copies are compared after the guard: change one so they differ from the checkout's.
    (rig.layout.repo / 'docker-compose.yaml').write_text('services: {}\n# changed after the image was baked\n')
    result = _call(rig)
    assert result['status'] == 'refused' and 'the MCP image is stale' in result['message'], result
    assert _execs(rig) == []
    assert_no_call_counter(rig)


def test_call_volume_admitted_calls_each_run_their_one_exec_and_none_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No volume limit (ADR tj-4rr0la addendum 33): past the withdrawn 12 an hour, every admitted call resets."""
    rig = _rig(tmp_path, monkeypatch)
    for count in range(1, CALL_VOLUME + 1):
        result = _call(rig)
        assert result['status'] == 'ok' and result['message'] != OLD_BUDGET_REFUSAL, (count, result)
        assert len(_execs(rig)) == count, 'every admitted call reached its exec'
    assert_no_call_counter(rig)


def test_every_call_runs_exactly_the_steps_of_one_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Calls are independent: the CALL_VOLUME-th call runs the same steps, word for word, as the first."""
    rig = _rig(tmp_path, monkeypatch)
    per_call = []
    for _ in range(CALL_VOLUME):
        before = len(rig.docker.steps)
        assert _call(rig)['status'] == 'ok'
        per_call.append([list(step.argv) for step in rig.docker.steps[before:]])
    assert all(steps == per_call[0] for steps in per_call), 'a later call ran different steps'
    assert_no_call_counter(rig)


# --- a failed or unclean reset -------------------------------------------------------------------------


def test_a_failed_exec_is_failed_and_leaves_nothing_that_affects_the_next_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    failing = {'exec': True}

    def respond(step):
        if is_dev_exec(step) and failing['exec']:
            return runner.ProcessResult(1, b'ERROR: deadlock\n', b'')
        return None

    rig = _rig(tmp_path, monkeypatch, respond)
    result = _call(rig)
    assert result['status'] == 'failed' and 'rolled back' in result['message'], result
    assert 'details' not in result
    assert_no_call_counter(rig)
    # The next call is admitted and runs its exec normally: the failure left no state behind.
    failing['exec'] = False
    first_call_steps = len(rig.docker.steps)
    result = _call(rig)
    assert result['status'] == 'ok', result
    assert [list(step.argv) for step in rig.docker.steps[first_call_steps:]] == [
        list(step.argv) for step in rig.docker.steps[:first_call_steps]
    ]


_UNCLEAN = {
    'alembic_version still there': b'database|postgres\nschema_owner|pg_database_owner\nalembic_version|present\npublic_relations|3\n',
    'relations left': b'database|postgres\nschema_owner|pg_database_owner\nalembic_version|absent\npublic_relations|2\n',
    'the wrong owner': b'database|postgres\nschema_owner|postgres\nalembic_version|absent\npublic_relations|0\n',
    'a line missing': b'database|postgres\nalembic_version|absent\npublic_relations|0\n',
    'nothing printed': b'',
    'not numeric': b'database|postgres\nschema_owner|pg_database_owner\nalembic_version|absent\npublic_relations|x\n',
    'a database name that is not an identifier': (
        b'database|post gres\nschema_owner|pg_database_owner\nalembic_version|absent\npublic_relations|0\n'
    ),
}


@pytest.mark.parametrize('stdout', list(_UNCLEAN.values()), ids=list(_UNCLEAN))
def test_a_verification_that_does_not_show_an_empty_schema_is_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes
):
    rig = _rig(tmp_path, monkeypatch, _answer(is_dev_exec, _ok(stdout)))
    result = _call(rig)
    assert result['status'] == 'failed' and 'did not show an empty public schema' in result['message'], result
    assert 'details' not in result


def test_parse_test_reset_reads_the_four_lines():
    state = stack.parse_test_reset(TEST_RESET_STDOUT)
    assert state == stack.ResetState('postgres', 'pg_database_owner', 'absent', 0) and state.clean


def test_parse_test_reset_is_not_clean_on_any_deviation():
    for stdout in _UNCLEAN.values():
        state = stack.parse_test_reset(stdout)
        assert state is None or not state.clean


# --- the exec is bound to its tail ---------------------------------------------------------------------

_WRITE = (DOCKER, 'exec', '-u', 'postgres', DEV_CONTAINER_ID, 'sh', '-c')


def test_the_reset_tail_is_accepted_with_exactly_its_option_set():
    stack.check_dev_exec_argv((*_WRITE, stack.TEST_RESET_SCRIPT))


_BAD_RESET_EXECS = {
    'with the read-only option': (
        DOCKER,
        'exec',
        '-u',
        'postgres',
        '-e',
        'PGOPTIONS=-c default_transaction_read_only=on',
        DEV_CONTAINER_ID,
        'sh',
        '-c',
        stack.TEST_RESET_SCRIPT,
    ),
    'as root': (DOCKER, 'exec', '-u', 'root', DEV_CONTAINER_ID, 'sh', '-c', stack.TEST_RESET_SCRIPT),
    'attached user': (DOCKER, 'exec', '-upostgres', DEV_CONTAINER_ID, 'sh', '-c', stack.TEST_RESET_SCRIPT),
    'no option': (DOCKER, 'exec', DEV_CONTAINER_ID, 'sh', '-c', stack.TEST_RESET_SCRIPT),
    'the code workdir': (DOCKER, 'exec', '-w', '/code', DEV_CONTAINER_ID, 'sh', '-c', stack.TEST_RESET_SCRIPT),
    'privileged': (
        DOCKER,
        'exec',
        '--privileged',
        '-u',
        'postgres',
        DEV_CONTAINER_ID,
        'sh',
        '-c',
        stack.TEST_RESET_SCRIPT,
    ),
    'a changed script': (*_WRITE[:4], DEV_CONTAINER_ID, 'sh', '-c', stack.TEST_RESET_SCRIPT + '\nrm -rf /'),
    'other SQL': (*_WRITE, 'psql -c "DROP DATABASE postgres"'),
    'a word after': (*_WRITE, stack.TEST_RESET_SCRIPT, 'extra'),
}


@pytest.mark.parametrize('argv', list(_BAD_RESET_EXECS.values()), ids=list(_BAD_RESET_EXECS))
def test_the_reset_exec_is_refused_in_any_other_shape(argv):
    with pytest.raises(stack.Refused):
        stack.check_dev_exec_argv(argv)


@pytest.mark.parametrize('script', [stack.DEV_DB_TABLES_SCRIPT, stack.DEV_MIGRATE_STATUS_SCRIPT])
def test_the_write_option_set_is_refused_with_every_other_tail(script: str):
    """The user-postgres-without-read-only set is test_reset's alone: db_tables' script cannot run under it."""
    with pytest.raises(stack.Refused):
        stack.check_dev_exec_argv((*_WRITE, script))


def test_the_builder_refuses_an_unlisted_tail():
    with pytest.raises(stack.Refused):
        stack.dev_exec_steps(Path('/stack'), DEV_CONTAINER_ID, ('sh', '-c', EXPECTED_RESET_SQL))


# --- the constants, and the SQL and script RUN ---------------------------------------------------------


def test_the_sql_constants_are_the_literals():
    assert stack.TEST_RESET_SQL == EXPECTED_RESET_SQL
    assert stack.TEST_RESET_VERIFY_SQL == EXPECTED_VERIFY_SQL
    assert stack.TEST_RESET_TAIL == ('sh', '-c', stack.TEST_RESET_SCRIPT)
    assert '@RESET@' not in stack.TEST_RESET_SCRIPT and '@VERIFY@' not in stack.TEST_RESET_SCRIPT


def test_the_sql_names_no_database_drop_and_no_file_or_program():
    """The reset is the schema, in place: the database the session is connected to is never dropped."""
    script = (stack.TEST_RESET_SQL + ' ' + stack.TEST_RESET_VERIFY_SQL).upper()
    for forbidden in ('DROP DATABASE', 'CREATE DATABASE', 'TRUNCATE', 'COPY ', 'PG_READ_FILE', 'LO_EXPORT', ' RM '):
        assert forbidden not in script, forbidden


_FAKE_PSQL = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_PSQL_LOG"
case "$*" in
*"DROP SCHEMA"*)
  [ "$FAKE_FAIL_ON" = reset ] && { printf 'ERROR: lock timeout leak INSTANCE_WRITE_SECRET=%s\n' "$INSTANCE_WRITE_SECRET"; exit 1; }
  printf 'leak POSTGRES_PASS=%s FAKE_DSN=%s\n' "$POSTGRES_PASS" "$FAKE_DSN"
  ;;
*)
  [ "$FAKE_FAIL_ON" = verify ] && { printf 'ERROR: verify leak INSTANCE_WRITE_SECRET=%s\n' "$INSTANCE_WRITE_SECRET"; exit 1; }
  printf 'database|dummy_db\nschema_owner|pg_database_owner\nalembic_version|absent\npublic_relations|0\n'
  printf 'leak INSTANCE_WRITE_SECRET=%s POSTGRES_PASSWORD=%s\n' "$INSTANCE_WRITE_SECRET" "$POSTGRES_PASSWORD"
  ;;
esac
"""


def _run_script(tmp_path: Path, secret: str, fail_on: str = '') -> tuple[subprocess.CompletedProcess, list[str]]:
    sh = shutil.which('sh')
    if sh is None:
        pytest.skip('no sh on PATH')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)
    psql = bin_dir / 'psql'
    psql.write_text(_FAKE_PSQL)
    psql.chmod(0o755)
    log = tmp_path / 'psql.log'
    env = {
        'PATH': f'{bin_dir}:/usr/bin:/bin',
        'FAKE_PSQL_LOG': str(log),
        'FAKE_FAIL_ON': fail_on,
        'POSTGRES_USER': 'dummy_user',
        'POSTGRES_DB': 'dummy_db',
        'INSTANCE_WRITE_SECRET': secret,
        'POSTGRES_PASS': 'dummy-pass-value',
        'POSTGRES_PASSWORD': 'dummy-password-value',
        'ALPACA_API_KEY': 'dummy-alpaca-key',
        'ALPACA_API_SECRET': 'dummy-alpaca-secret',
        'FAKE_DSN': 'postgresql://dummy_user:dummy-dsn-pw@db:5432/dummy_db',
    }
    completed = subprocess.run(
        [sh, '-c', stack.TEST_RESET_SCRIPT], env=env, capture_output=True, timeout=30, check=False
    )
    return completed, (log.read_text().splitlines() if log.exists() else [])


def test_the_script_issues_exactly_the_reset_and_then_the_verification(tmp_path: Path):
    """What psql actually receives: the container's own user and database and only the constant SQL, in order."""
    completed, calls = _run_script(tmp_path, 'dummy-not-a-secret')
    assert completed.returncode == 0, completed
    assert calls == [
        f'-X -q -At -v ON_ERROR_STOP=1 -U dummy_user -d dummy_db -c {EXPECTED_RESET_SQL}',
        f'-X -At -F | -v ON_ERROR_STOP=1 -U dummy_user -d dummy_db -c {EXPECTED_VERIFY_SQL}',
    ]
    printed = completed.stdout.decode()
    assert 'database|dummy_db\nschema_owner|pg_database_owner\nalembic_version|absent\npublic_relations|0\n' in printed


@pytest.mark.parametrize('fail_on', ['reset', 'verify'])
def test_a_failing_reset_exits_1_and_the_verification_does_not_run_after_a_failed_reset(tmp_path: Path, fail_on: str):
    completed, calls = _run_script(tmp_path, 'dummy-not-a-secret', fail_on)
    assert completed.returncode == 1, completed
    assert len(calls) == (1 if fail_on == 'reset' else 2), calls


_SECRETS = {
    'plain': 'dummy-write-secret-1',
    'shell specials': "dummy'$(id)`id`;|>",
    'awk specials': 'dummy"\\&\\\\%s&\\n',
    'spaces': '  dummy spaced  ',
}


@pytest.mark.parametrize('fail_on', ['', 'reset', 'verify'], ids=['success', 'reset fails', 'verify fails'])
@pytest.mark.parametrize('secret', list(_SECRETS.values()), ids=list(_SECRETS))
def test_no_fake_secret_value_survives_the_script(tmp_path: Path, secret: str, fail_on: str):
    """Every secret value and every postgres:// value is <redacted>, on the success path and on both error exits."""
    completed, _ = _run_script(tmp_path, secret, fail_on)
    printed = completed.stdout.decode('utf-8', errors='replace') + completed.stderr.decode('utf-8', errors='replace')
    leaked = [
        value
        for value in (secret, 'dummy-pass-value', 'dummy-password-value', 'dummy-dsn-pw')
        if value.strip() in printed
    ]
    assert leaked == [], f'the script printed fake secret values: {leaked}'
    assert '<redacted>' in printed, printed


def test_the_secret_is_on_no_command_line_env_output_or_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sentinel = 'WRITE_SECRET_SENTINEL_5c2e'
    rig = _rig(tmp_path, monkeypatch)
    (rig.layout.repo / '.env').write_text(f'INSTANCE_WRITE_SECRET={sentinel}\nPOSTGRES_PASS={sentinel}\n')
    monkeypatch.setenv('INSTANCE_WRITE_SECRET', sentinel)
    result = _call(rig)
    assert result['status'] == 'ok', result
    for step in rig.docker.steps:
        assert all(sentinel not in word for word in step.argv), step.argv
        assert all(sentinel not in value for _, value in step.env)
    assert sentinel not in json.dumps(result)
    assert all(sentinel not in line for line in rig.audit_lines())


def test_default_response_does_not_admit_test_reset_in_dev_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The rig's DEV default refuses it: the verb is test mode's alone."""
    rig = make_rig(tmp_path, monkeypatch)
    rig.docker.respond = default_response
    result = _call(rig)
    assert result['status'] == 'refused' and result['message'] == MODE_REFUSAL, result
