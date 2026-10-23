"""run_system_tests and seed_dump on TEST mode of the one project (tj-iee6sn.20).

Design: ADR tj-4rr0la addenda 26 (3), (5), (6) and 27 (1), (3), with addendum 5 ruling 2 (a worktree
snapshot never supplies the Dockerfile); the architect's consolidated readiness note on the bead (11:00).

THE RAILS, in order, each refusal running nothing after it and charging nothing: the worktree name and
the paths (spelling, then git); the mode guard, TEST only; the S6 stale-copy check and the D5 env-file
preflight; the postgres data mount (one lookup, then ONE fixed inspect, accepted only when its source is
<repo host path>/volume_data/test/postgres); a running data_store (the builder's deviation 2), the two
digests and the stamp of every running data_store and data_ingest container; then the snapshot and its
checks, and ONE budget charge. THE CLIENT: up's secret wrapper around `docker compose -p trader_joe_dev
--project-directory <snapshot> --env-file <repo host path>/.env` and three trusted files, the third an
MCP-only overlay that sets test_client's build.dockerfile alone. check_testclient_argv admits only the
three client tails, and the dev lifecycle check refuses that prefix.

Expected values are literals: a test that read them from stack.py would agree with any change to it.
"""

import ast
import inspect
import json
import re
import textwrap
from pathlib import Path

import pytest
import yaml

from common.tests import compose_model
from common.tests.test_ci_invariants import _dockerignore_rules, _is_excluded_from_context
from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import (
    DEV_CONTAINER_ID,
    DEV_INGEST_CONTAINER_ID,
    LIVE_ENV_SENTINEL,
    REPO_ROOT,
    WORKTREE_NAME,
    Rig,
    budget_spent,
    client_prefix_words,
    client_tail,
    default_response,
    is_base_step,
    is_client_step,
    is_container_stamp_read,
    is_dev_digest,
    is_dev_lookup,
    is_mode_guard,
    is_postgres_mount_read,
    make_rig,
    plant_live_env_files,
    postgres_test_source,
    system_suite_response,
)
from tools.agent_mcp.tests.test_commands import _baked_into_trusted
from tools.agent_mcp.tests.test_dev_label_verbs import (
    BUDGET_REFUSAL,
    DEV_LINE,
    GUARD_REFUSAL,
    MIXED_REFUSAL,
    PROD_LINE,
    STEERING_ENV,
    TEST_LINE,
)


pytestmark = pytest.mark.build_infra

DOCKER = '/usr/local/bin/docker'
# Dev and test's explicit project (ADR tj-4rr0la addendum 29; tj-iee6sn.31); prod keeps the default.
PROJECT = 'trader_joe_dev'
PROD_PROJECT = 'trader_joe'
TRUSTED = '/opt/agent_mcp/compose'
MODE_REFUSAL = 'the system suite runs only in test mode: call test_up first; your dev data is not touched'
STALE_STACK = 'stale test stack: call test_build, then test_up'
NO_DATA_STORE = 'no running data_store container in the trader_joe_dev project: call test_up'
MOUNT_FORMAT = '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Source}}{{end}}{{end}}'
CLIENT_FILES = ('docker-compose.yaml', 'docker-compose.test-client.yaml', 'docker-compose.agent-client.yaml')
OVERLAY = REPO_ROOT / 'docker-compose.agent-client.yaml'
MCP_DOCKERIGNORE = REPO_ROOT / 'tools' / 'agent_mcp' / 'Dockerfile.dockerignore'
# The client steps' env: the three service env-file variables at /dev/null, then the steering pins.
CLIENT_ENV = (
    ('ROOT_ENV_FILE', '/dev/null'),
    ('STORE_ENV_FILE', '/dev/null'),
    ('INGEST_ENV_FILE', '/dev/null'),
    *STEERING_ENV,
)
RUN_HEAD = ['run', '--rm', '--no-deps', '--build', 'test_client']
PRODUCER = ['run', '--rm', '-T', '--entrypoint', '/code/.venv/bin/python', 'test_client', '-m', 'data.store.seeds']
VERBS = {
    'run_system_tests': {'worktree': WORKTREE_NAME, 'paths': ['tests/system/test_one.py']},
    'seed_dump': {'worktree': WORKTREE_NAME},
}


def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, respond=None) -> Rig:
    """A rig answering TEST mode and the test mount; RESPOND, when given, answers before that."""
    rig = make_rig(tmp_path, monkeypatch)
    admitted = system_suite_response(rig.layout)
    if respond is None:
        rig.docker.respond = admitted
    else:
        rig.docker.respond = lambda step: respond(step) or admitted(step)
    return rig


def _call(rig: Rig, verb: str) -> dict:
    return rig.call(verb, dict(VERBS[verb]))


def _ran_nothing_after(rig: Rig, predicate) -> None:
    """The last step is the first one PREDICATE matches; no client or base step ran; nothing charged."""
    steps = rig.docker.steps
    assert steps and predicate(steps[-1]), [step.argv for step in steps]
    assert len([step for step in steps if predicate(step)]) == 1, [step.argv for step in steps]
    assert not [step for step in steps if is_client_step(step) or is_base_step(step)]
    assert budget_spent(rig) == 0
    assert not rig.layout.snapshot.exists(), 'a refused rail took the snapshot'


def _answer(predicate, result: runner.ProcessResult):
    return lambda step: result if predicate(step) else None


def _ok(stdout: bytes) -> runner.ProcessResult:
    return runner.ProcessResult(0, stdout, b'')


# --- the admitted call ------------------------------------------------------------------------------


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_an_admitted_call_runs_the_rails_in_order_then_charges_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """The rails' kinds in order, then the bases and the client; the snapshot taken; exactly one charge."""
    rig = _rig(tmp_path, monkeypatch)
    result = _call(rig, verb)
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
        if is_base_step(step):
            return 'base'
        if is_client_step(step):
            return 'client:' + client_tail(step, rig.layout)[0]
        return f'other:{step.argv[:3]}'

    kinds = [kind(step) for step in rig.docker.steps]
    rails = [
        'guard',
        'lookup:postgres',
        'mount',
        'lookup:data_store',
        'digest',
        'digest',
        'lookup:data_ingest',
        f'stamp:{DEV_CONTAINER_ID}',
        f'stamp:{DEV_INGEST_CONTAINER_ID}',
    ]
    clients = ['client:run'] if verb == 'run_system_tests' else ['client:build', 'client:run']
    assert kinds == [*rails, *['base'] * len(stack.BASE_IMAGES), *clients], kinds
    assert budget_spent(rig) == 1
    assert rig.layout.snapshot.is_dir()


def test_the_mode_refusal_is_spelled_as_the_design_gives_it():
    assert stack.SYSTEM_SUITE_MODE_HINT == MODE_REFUSAL
    assert frozenset({'TEST'}) == stack.MODES_TEST


# --- the rails before the mode guard -----------------------------------------------------------------


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_a_bad_worktree_name_runs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig = _rig(tmp_path, monkeypatch)
    result = rig.call(verb, {**VERBS[verb], 'worktree': '../escape'})
    assert result['status'] == 'refused', result
    assert rig.git.calls == [] and rig.docker.steps == [] and budget_spent(rig) == 0


def test_a_badly_spelled_path_runs_no_git_and_no_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    result = rig.call('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': ['tests/fakes']})
    assert result['status'] == 'refused', result
    assert rig.git.calls == [] and rig.docker.steps == [] and budget_spent(rig) == 0


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_an_unknown_worktree_runs_git_and_no_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig = _rig(tmp_path, monkeypatch)
    result = rig.call(verb, {**VERBS[verb], 'worktree': 'wt-none'})
    assert result['status'] == 'refused' and 'no worktree named' in result['message'], result
    assert rig.git.list_calls == 1 and rig.docker.steps == [] and budget_spent(rig) == 0


# --- the mode guard: TEST only ----------------------------------------------------------------------

_MODES = {
    'empty project': (b'', MODE_REFUSAL),
    'dev mode': (DEV_LINE, MODE_REFUSAL),
    # A DEV data_ingest alone (dev-deps' base + override) is DEV mode: it is the real broker, so it refuses.
    'a dev data_ingest': (b'/r/docker-compose.yaml,/r/docker-compose.override.yaml\n', MODE_REFUSAL),
    'prod': (PROD_LINE, GUARD_REFUSAL),
    # Override AND fake together is no mode any target launches (addendum 27 (1)): PROD.
    'override and fake together': (
        b'/r/docker-compose.yaml,/r/docker-compose.override.yaml,/r/docker-compose.fake.yaml\n',
        GUARD_REFUSAL,
    ),
    'dev and test mixed': (DEV_LINE + TEST_LINE, MIXED_REFUSAL),
    'test beside a dev data_ingest': (
        TEST_LINE + b'/r/docker-compose.yaml,/r/docker-compose.override.yaml\n',
        MIXED_REFUSAL,
    ),
}


@pytest.mark.parametrize(('guard', 'message'), list(_MODES.values()), ids=list(_MODES))
@pytest.mark.parametrize('verb', sorted(VERBS))
def test_every_mode_but_test_refuses_at_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, guard: bytes, message: str
):
    rig = _rig(tmp_path, monkeypatch, _answer(is_mode_guard, _ok(guard)))
    result = _call(rig, verb)
    assert result == {**result, 'status': 'refused', 'message': message}, result
    _ran_nothing_after(rig, is_mode_guard)


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_a_failed_guard_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig = _rig(tmp_path, monkeypatch, _answer(is_mode_guard, runner.ProcessResult(1, b'', b'daemon down')))
    assert _call(rig, verb)['status'] == 'refused'
    _ran_nothing_after(rig, is_mode_guard)


def test_test_mode_with_several_test_containers_is_admitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch, _answer(is_mode_guard, _ok(TEST_LINE * 3)))
    assert _call(rig, 'run_system_tests')['status'] == 'ok'


# --- S6 and D5, after the guard ----------------------------------------------------------------------


@pytest.mark.parametrize('name', ['docker-compose.test-client.yaml', 'docker-compose.agent-client.yaml', 'Dockerfile'])
@pytest.mark.parametrize('verb', sorted(VERBS))
def test_a_stale_trusted_client_file_refuses_after_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, name: str
):
    rig = _rig(tmp_path, monkeypatch)
    trusted = rig.layout.trusted_dir / name
    trusted.write_bytes(trusted.read_bytes() + b'# drift\n')
    result = _call(rig, verb)
    assert result['status'] == 'refused' and result['message'].startswith(f'the MCP image is stale: {name} differs'), (
        result
    )
    _ran_nothing_after(rig, is_mode_guard)


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_a_symlinked_root_env_file_refuses_after_the_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig = _rig(tmp_path, monkeypatch)
    target = rig.layout.root / 'elsewhere.env'
    target.write_text('X=1\n')
    (rig.layout.repo / '.env').symlink_to(target)
    result = _call(rig, verb)
    assert result == {
        **result,
        'status': 'refused',
        'message': 'a symlink at .env: remove the link, or start the stack yourself',
    }
    _ran_nothing_after(rig, is_mode_guard)


# --- the postgres data mount ---------------------------------------------------------------------------


def _mounts(layout) -> dict[str, bytes]:
    expected = postgres_test_source(layout)
    host = str(layout.repo_host_path)
    return {
        'the dev data directory': f'{host}/volume_data/postgres\n'.encode(),
        'no such mount': b'',
        'no such mount, a bare newline': b'\n',
        'a bind elsewhere': b'/srv/postgres\n',
        # The same directory through the root checkout's own path (repo_host_path is a symlink to it in the
        # rig): only the exact spelling is accepted, never a resolution of it.
        'the same directory spelled through the other path': f'{layout.repo}/volume_data/test/postgres\n'.encode(),
        'a dot-dot spelling': f'{host}/volume_data/test/../test/postgres\n'.encode(),
        'a trailing slash': f'{expected}/\n'.encode(),
        'a sub-directory': f'{expected}/sub\n'.encode(),
        'a prefix of it': f'{expected[:-1]}\n'.encode(),
        'two mounts, run together': f'{expected}{expected}\n'.encode(),
        'two mounts, two lines': f'{expected}\n{expected}\n'.encode(),
        'two final newlines': f'{expected}\n\n'.encode(),
        'not utf-8': b'\xff\xfe\n',
    }


_MOUNT_IDS = list(_mounts(type('L', (), {'repo_host_path': Path('/h'), 'repo': Path('/r')})()))


@pytest.mark.parametrize('case', _MOUNT_IDS)
@pytest.mark.parametrize('verb', sorted(VERBS))
def test_any_mount_but_the_test_directory_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, case: str
):
    rig = _rig(tmp_path, monkeypatch)
    stdout = _mounts(rig.layout)[case]
    admitted = rig.docker.respond
    rig.docker.respond = lambda step: _ok(stdout) if is_postgres_mount_read(step) else admitted(step)
    result = _call(rig, verb)
    assert result['status'] == 'refused', result
    assert result['message'].endswith(
        f'the system suite writes only the postgres on {postgres_test_source(rig.layout)}: call test_up'
    ), result
    _ran_nothing_after(rig, is_postgres_mount_read)


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_a_failed_mount_read_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig = _rig(
        tmp_path, monkeypatch, _answer(is_postgres_mount_read, runner.ProcessResult(1, b'', b'no such container'))
    )
    result = _call(rig, verb)
    assert result['status'] == 'refused' and 'its data mount is unknown' in result['message'], result
    _ran_nothing_after(rig, is_postgres_mount_read)


def _is_lookup_of(service: str):
    return lambda step: is_dev_lookup(step) and f'label=com.docker.compose.service={service}' in step.argv


@pytest.mark.parametrize(
    ('stdout', 'fragment'), [(b'', 'no running postgres'), (b'aaaaaaaaaaaa\nbbbbbbbbbbbb\n', '2 running postgres')]
)
def test_no_or_several_postgres_refuse_before_the_mount_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes, fragment: str
):
    rig = _rig(tmp_path, monkeypatch, _answer(_is_lookup_of('postgres'), _ok(stdout)))
    result = _call(rig, 'run_system_tests')
    assert result['status'] == 'refused' and fragment in result['message'], result
    _ran_nothing_after(rig, _is_lookup_of('postgres'))


def test_the_mount_read_is_one_fixed_inspect_of_the_looked_up_id():
    assert stack.postgres_mount_argv('0123456789ab') == (
        DOCKER,
        'inspect',
        '--type',
        'container',
        '--format',
        MOUNT_FORMAT,
        '0123456789ab',
    )
    assert (
        stack.expected_test_postgres_mount(Path('/home/u/trader_joe')) == '/home/u/trader_joe/volume_data/test/postgres'
    )
    for bad in ('--format', 'ABCDEF012345', '0123', '0123456789ab; ls'):
        with pytest.raises(stack.Refused):
            stack.postgres_mount_argv(bad)


_BAD_MOUNT_ARGVS = {
    'another format': (DOCKER, 'inspect', '--type', 'container', '--format', '{{.Config.Env}}', '0123456789ab'),
    'no type': (DOCKER, 'inspect', '--format', MOUNT_FORMAT, '0123456789ab'),
    'an image': (DOCKER, 'inspect', '--type', 'image', '--format', MOUNT_FORMAT, '0123456789ab'),
    'two ids': (DOCKER, 'inspect', '--type', 'container', '--format', MOUNT_FORMAT, '0123456789ab', '0123456789ac'),
    'another docker': ('docker', 'inspect', '--type', 'container', '--format', MOUNT_FORMAT, '0123456789ab'),
}


@pytest.mark.parametrize('argv', list(_BAD_MOUNT_ARGVS.values()), ids=list(_BAD_MOUNT_ARGVS))
def test_check_postgres_mount_argv_refuses_every_other_inspect(argv):
    with pytest.raises(stack.Refused):
        stack.check_postgres_mount_argv(argv)


# --- a running data_store, the digests and the running stamps -----------------------------------------


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_no_running_data_store_refuses_before_the_digests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    """The builder's deviation 2: postgres alone up has no stamp to prove the stack current, so it refuses."""
    rig = _rig(tmp_path, monkeypatch, _answer(_is_lookup_of('data_store'), _ok(b'')))
    result = _call(rig, verb)
    assert result == {**result, 'status': 'refused', 'message': NO_DATA_STORE}, result
    _ran_nothing_after(rig, _is_lookup_of('data_store'))


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_several_data_store_containers_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig = _rig(tmp_path, monkeypatch, _answer(_is_lookup_of('data_store'), _ok(b'aaaaaaaaaaaa\nbbbbbbbbbbbb\n')))
    assert _call(rig, verb)['status'] == 'refused'
    _ran_nothing_after(rig, _is_lookup_of('data_store'))


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_a_failed_digest_fails_and_runs_nothing_after(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig = _rig(tmp_path, monkeypatch, _answer(is_dev_digest, runner.ProcessResult(1, b'', b'boom')))
    result = _call(rig, verb)
    assert result['status'] == 'failed' and result['message'].endswith('nothing was run'), result
    steps = rig.docker.steps
    assert is_dev_digest(steps[-1]) and len([s for s in steps if is_dev_digest(s)]) == 1
    assert not [step for step in steps if is_client_step(step) or is_base_step(step)] and budget_spent(rig) == 0


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
@pytest.mark.parametrize('verb', sorted(VERBS))
def test_a_stale_running_container_refuses_with_no_client_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, container: str, stamp: bytes, exit_status: int
):
    rig = _rig(tmp_path, monkeypatch, _stamp_of(container, stamp, exit_status))
    result = _call(rig, verb)
    assert result['status'] == 'refused' and result['message'].endswith(STALE_STACK), result
    steps = rig.docker.steps
    assert is_container_stamp_read(steps[-1]) and steps[-1].argv[-1] == container
    assert not [step for step in steps if is_client_step(step) or is_base_step(step)]
    assert budget_spent(rig) == 0 and not rig.layout.snapshot.exists()


def test_a_stopped_data_ingest_reads_only_the_data_store_stamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """EVERY RUNNING one: a data_ingest that is not running has no container to read, and is not required."""
    rig = _rig(tmp_path, monkeypatch, _answer(_is_lookup_of('data_ingest'), _ok(b'')))
    assert _call(rig, 'run_system_tests')['status'] == 'ok'
    assert [step.argv[-1] for step in rig.docker.steps if is_container_stamp_read(step)] == [DEV_CONTAINER_ID]


# --- after the rails: the snapshot, then one charge ----------------------------------------------------


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_an_exhausted_budget_refuses_after_the_snapshot_and_runs_no_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig = _rig(tmp_path, monkeypatch)
    for _ in range(12):
        rig.agent.dev_budget.charge()
    result = _call(rig, verb)
    assert result == {**result, 'status': 'refused', 'message': BUDGET_REFUSAL}, result
    assert not [step for step in rig.docker.steps if is_client_step(step) or is_base_step(step)]
    assert rig.layout.snapshot.is_dir() and budget_spent(rig) == 12


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_a_failed_client_still_costs_one_charge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig = _rig(tmp_path, monkeypatch, _answer(is_client_step, runner.ProcessResult(1, b'', b'')))
    assert _call(rig, verb)['status'] == 'failed'
    assert budget_spent(rig) == 1


# --- the client command -----------------------------------------------------------------------------------


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_every_client_step_runs_under_the_pinned_env_with_no_data_dir_and_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig = _rig(tmp_path, monkeypatch)
    assert _call(rig, verb)['status'] == 'ok'
    clients = [step for step in rig.docker.steps if is_client_step(step)]
    assert clients
    for step in clients:
        assert step.env == CLIENT_ENV, step.env
        names = {name for name, _ in step.env}
        assert not names & {'DATA_DIR', 'POSTGRES_PASS', 'INSTANCE_WRITE_SECRET', 'APP_INTERNAL_PORT'}, names
        assert step.cwd == rig.layout.stack_dir


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_no_live_env_value_reaches_an_argv_the_response_or_the_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """The wrapper reads the root env file in the CHILD: the server never opens it, so its values go nowhere here."""
    rig = _rig(tmp_path, monkeypatch)
    plant_live_env_files(rig.layout.repo)
    plant_live_env_files(rig.layout.worktree)
    result = _call(rig, verb)
    assert result['status'] == 'ok', result
    assert LIVE_ENV_SENTINEL not in json.dumps(result)
    assert all(LIVE_ENV_SENTINEL not in line for line in rig.audit_lines())
    for step in rig.docker.steps:
        assert all(LIVE_ENV_SENTINEL not in word for word in step.argv)
        assert all(LIVE_ENV_SENTINEL not in value for _, value in step.env)
    assert not list(rig.layout.snapshot.rglob('.env')), 'a live env file reached the snapshot'


def test_the_client_prefix_spelled_from_literals(tmp_path: Path):
    stack_dir, host = Path('/stack'), Path('/home/u/trader_joe')
    assert stack.testclient_prefix(stack_dir, host) == [
        DOCKER,
        'compose',
        '-p',
        PROJECT,
        '--project-directory',
        '/stack/source',
        '--env-file',
        '/home/u/trader_joe/.env',
        *(word for name in CLIENT_FILES for word in ('-f', f'{TRUSTED}/{name}')),
    ]
    assert stack.SYSTEM_CLIENT_COMPOSE_FILES == CLIENT_FILES


def test_seed_dump_with_a_date_passes_it_after_the_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    assert rig.call('seed_dump', {'worktree': WORKTREE_NAME, 'date': '2026-03-04'})['status'] == 'ok'
    tails = [client_tail(step, rig.layout) for step in rig.docker.steps if is_client_step(step)]
    assert tails == [['build', 'test_client'], [*PRODUCER, '--date', '2026-03-04']]


# --- check_testclient_argv: only the three shapes ----------------------------------------------------------

STACK_DIR, HOST = Path('/stack'), Path('/home/u/trader_joe')


def _client_argv(*tail: str, prefix: list[str] | None = None) -> list[str]:
    return [*(prefix if prefix is not None else stack.testclient_prefix(STACK_DIR, HOST)), *tail]


_ACCEPTED = {
    'build': ['build', 'test_client'],
    'run one path': [*RUN_HEAD, 'tests/system/test_one.py'],
    'run the suite': [*RUN_HEAD, 'tests/system'],
    'run fifty paths': [*RUN_HEAD, *(f'tests/system/test_{i}.py' for i in range(50))],
    'seed': PRODUCER,
    'seed with a date': [*PRODUCER, '--date', '2024-02-29'],
}


@pytest.mark.parametrize('tail', list(_ACCEPTED.values()), ids=list(_ACCEPTED))
def test_check_testclient_argv_accepts_the_three_client_shapes(tail):
    stack.check_testclient_argv(_client_argv(*tail), STACK_DIR, HOST)


def _prefix_with(files: tuple[str, ...], project_dir: str = '/stack/source', env_file: str = '/home/u/trader_joe/.env'):
    return [
        DOCKER,
        'compose',
        '-p',
        PROJECT,
        '--project-directory',
        project_dir,
        '--env-file',
        env_file,
        *(word for name in files for word in ('-f', f'{TRUSTED}/{name}')),
    ]


_REFUSED_TAILS = {
    'run without --no-deps': ['run', '--rm', '--build', 'test_client', 'tests/system'],
    'run without --build': ['run', '--rm', '--no-deps', 'test_client', 'tests/system'],
    'run without --rm': ['run', '--no-deps', '--build', 'test_client', 'tests/system'],
    'run with no path': RUN_HEAD,
    'run another service': ['run', '--rm', '--no-deps', '--build', 'data_store', 'tests/system'],
    'run with -v': [*RUN_HEAD, '-v', 'tests/system'],
    'run with a volume': [*RUN_HEAD, '--volume', '/:/host', 'tests/system'],
    'run with an entrypoint': [*RUN_HEAD, '--entrypoint', 'sh', 'tests/system'],
    'run with -e': ['run', '--rm', '--no-deps', '--build', '-e', 'X=1', 'test_client', 'tests/system'],
    'run a path outside': [*RUN_HEAD, 'tests/fakes'],
    'run a dot-dot path': [*RUN_HEAD, 'tests/system/../fakes'],
    'run an unnormalised path': [*RUN_HEAD, 'tests/system/./test_one.py'],
    'run an absolute path': [*RUN_HEAD, '/code/tests/system'],
    'run an option as a path': [*RUN_HEAD, '-k', 'x'],
    'run fifty-one paths': [*RUN_HEAD, *(f'tests/system/test_{i}.py' for i in range(51))],
    'build another service': ['build', 'data_store'],
    'build with an extra word': ['build', 'test_client', 'data_store'],
    'build with --pull': ['build', '--pull', 'test_client'],
    'up': ['up', '-d', 'test_client'],
    'down -v': ['down', '-v'],
    'seed with a bad date': [*PRODUCER, '--date', '2023-02-29'],
    'seed with --date and no value': [*PRODUCER, '--date'],
    'seed with an extra word': [*PRODUCER, '--date', '2024-02-29', 'x'],
    'seed with another flag': [*PRODUCER, '--out', '2024-02-29'],
    'seed with -v': [*PRODUCER, '-v'],
    'a second -f in the tail': ['-f', '/tmp/x.yaml', 'build', 'test_client'],
    'a second --env-file in the tail': ['--env-file', '/tmp/x', 'build', 'test_client'],
    'a project in the tail': ['-p', 'other', 'build', 'test_client'],
    'empty': [],
}


@pytest.mark.parametrize('tail', list(_REFUSED_TAILS.values()), ids=list(_REFUSED_TAILS))
def test_check_testclient_argv_refuses_every_other_tail(tail):
    with pytest.raises(stack.Refused):
        stack.check_testclient_argv(_client_argv(*tail), STACK_DIR, HOST)


_REFUSED_PREFIXES = {
    'no client overlay': _prefix_with(CLIENT_FILES[:2]),
    'the fake file added': _prefix_with((*CLIENT_FILES, 'docker-compose.fake.yaml')),
    'the overlay first': _prefix_with((CLIENT_FILES[2], *CLIENT_FILES[:2])),
    # The retired overlay's name (tj-iee6sn.27), which no longer exists, in place of the client overlay.
    'the retired agent-stack overlay': _prefix_with((*CLIENT_FILES[:2], 'docker-compose.agent-stack.yaml')),
    'the root checkout as project directory': _prefix_with(CLIENT_FILES, project_dir='/home/u/trader_joe'),
    'the worktree as project directory': _prefix_with(
        CLIENT_FILES, project_dir='/home/u/trader_joe/.claude/worktrees/wt'
    ),
    'another env file': _prefix_with(CLIENT_FILES, env_file='/stack/source/.env'),
    'the retired agent stack env': _prefix_with(CLIENT_FILES, env_file='/stack/agent_stack.env'),
    'no env file': [w for w in _prefix_with(CLIENT_FILES) if w not in ('--env-file', '/home/u/trader_joe/.env')],
    'the retired agent stack project': [
        '/usr/local/bin/docker',
        'compose',
        '-p',
        'trader_joe_agent_stack',
        *_prefix_with(CLIENT_FILES)[4:],
    ],
    # tj-iee6sn.31: prod's default project, the one name no client command may ever carry.
    "prod's default project": ['/usr/local/bin/docker', 'compose', '-p', PROD_PROJECT, *_prefix_with(CLIENT_FILES)[4:]],
}


@pytest.mark.parametrize('prefix', list(_REFUSED_PREFIXES.values()), ids=list(_REFUSED_PREFIXES))
def test_check_testclient_argv_refuses_every_other_prefix(prefix):
    with pytest.raises(stack.Refused):
        stack.check_testclient_argv(_client_argv('build', 'test_client', prefix=prefix), STACK_DIR, HOST)


WRAPPER_REFUSAL = 'a client command has no shell wrapper: the secret is read from the root env file by compose'


def _wrapper_head() -> list[str]:
    """The up verb's six wrapper words, spelled here: the form the client command must never take (5a38b7f)."""
    return ['sh', '-c', stack.DEV_UP_WRAPPER_SCRIPT, 'sh', f'{TRUSTED}/tools/dev_instance_secret.sh', f'{HOST}/.env']


def _wrapped_forms() -> dict[str, list[str]]:
    argv = _client_argv('build', 'test_client')
    run = _client_argv(*RUN_HEAD, 'tests/system')
    return {
        'the six-word head': [*_wrapper_head(), *argv],
        'the head on a run': [*_wrapper_head(), *run],
        'sh -c alone in front': ['sh', '-c', *argv],
        'the script word alone in front': [stack.DEV_UP_WRAPPER_SCRIPT, *argv],
        'sh as a tail word': [*argv, 'sh'],
        '-c as a tail word': [*argv, '-c'],
        'the script as a path': [*run[:-1], stack.DEV_UP_WRAPPER_SCRIPT],
        'sh inside the prefix': [*argv[:4], 'sh', *argv[4:]],
        'the head after the prefix': [*_client_argv(), *_wrapper_head(), 'build', 'test_client'],
    }


@pytest.mark.parametrize('form', list(_wrapped_forms()), ids=list(_wrapped_forms()))
def test_check_testclient_argv_refuses_the_wrapper_form_in_any_position(form: str):
    with pytest.raises(stack.Refused) as refused:
        stack.check_testclient_argv(_wrapped_forms()[form], STACK_DIR, HOST)
    assert str(refused.value) == WRAPPER_REFUSAL


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_the_client_command_is_compose_itself_with_no_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """Exact argv of every client step: the client prefix, then the tail; no shell word anywhere in the call."""
    rig = _rig(tmp_path, monkeypatch)
    assert _call(rig, verb)['status'] == 'ok'
    host, stack_dir = rig.layout.repo_host_path, rig.layout.stack_dir
    prefix = [
        DOCKER,
        'compose',
        '-p',
        PROJECT,
        '--project-directory',
        f'{stack_dir}/source',
        '--env-file',
        f'{host}/.env',
        *(word for name in CLIENT_FILES for word in ('-f', f'{TRUSTED}/{name}')),
    ]
    tails = (
        [[*RUN_HEAD, 'tests/system/test_one.py']]
        if verb == 'run_system_tests'
        else [['build', 'test_client'], PRODUCER]
    )
    assert [list(step.argv) for step in rig.docker.steps if is_client_step(step)] == [[*prefix, *t] for t in tails]
    for step in rig.docker.steps:
        assert step.argv[0] != 'sh' and stack.DEV_UP_WRAPPER_SCRIPT not in step.argv, step.argv
        assert not any('dev_instance_secret' in word for word in step.argv), step.argv


@pytest.mark.parametrize('tail', list(_ACCEPTED.values()), ids=list(_ACCEPTED))
def test_the_dev_lifecycle_check_refuses_the_client_prefix(tail):
    """--env-file is a word of the client prefix ONLY: the dev lifecycle check refuses the client command whole."""
    with pytest.raises(stack.Refused):
        stack.check_dev_lifecycle_argv(_client_argv(*tail), HOST)


def test_the_dev_lifecycle_check_refuses_an_env_file_after_any_dev_prefix():
    for prefix in (
        stack.dev_file_prefix(HOST),
        stack.testmode_prefix(HOST),
        stack.testmode_build_prefix(HOST),
        stack.dev_compose_prefix(),
    ):
        for spelled in (['--env-file', f'{HOST}/.env'], [f'--env-file={HOST}/.env']):
            with pytest.raises(stack.Refused):
                stack.check_dev_lifecycle_argv([*prefix, *spelled, 'down'], HOST)


# --- the trusted client overlay (addendum 5 ruling 2) ---------------------------------------------------------


def test_the_client_overlay_sets_only_test_clients_build_dockerfile_to_the_trusted_copy():
    model = yaml.safe_load(OVERLAY.read_text(encoding='utf-8'))
    assert model == {'services': {'test_client': {'build': {'dockerfile': f'{TRUSTED}/Dockerfile'}}}}, model
    dockerfile = Path(model['services']['test_client']['build']['dockerfile'])
    assert dockerfile.is_absolute() and dockerfile.parent == Path(TRUSTED)


def test_the_merged_client_build_is_the_snapshot_context_and_the_trusted_dockerfile():
    """Over the three client files, test_client builds context `.` (the snapshot) with the trusted Dockerfile.

    The target is system_test_image, and no service but test_client gains a key from the overlay.
    """
    models = [compose_model.load(REPO_ROOT / name) for name in CLIENT_FILES]
    merged = compose_model.merge(models)
    assert merged['services']['test_client']['build'] == {
        'context': '.',
        'target': 'system_test_image',
        'dockerfile': f'{TRUSTED}/Dockerfile',
    }
    without = compose_model.merge(models[:2])
    for service, spec in without['services'].items():
        if service != 'test_client':
            assert merged['services'][service] == spec, service


def test_the_snapshot_never_holds_a_dockerfile_compose_file_dockerignore_or_env_file():
    """Addendum 5 ruling 2, from the other side: nothing the snapshot copies can be a build or compose input."""
    for source in stack.SNAPSHOT_SOURCES:
        name = Path(source).name
        assert not name.startswith(('Dockerfile', 'docker-compose', 'compose.', '.env', '.dockerignore')), source


def test_the_mcp_image_bakes_the_client_overlay_into_the_trusted_directory():
    landed = _baked_into_trusted()
    assert landed['docker-compose.agent-client.yaml'] == f'{TRUSTED}/docker-compose.agent-client.yaml'
    assert landed['docker-compose.test-client.yaml'] == f'{TRUSTED}/docker-compose.test-client.yaml'
    lines = [line.strip() for line in MCP_DOCKERIGNORE.read_text(encoding='utf-8').splitlines()]
    assert '!docker-compose.agent-client.yaml' in lines
    assert not [
        line for line in lines if line.startswith('!') and 'agent-client' in line and re.search(r'[*?\[\]]', line)
    ]
    rules = _dockerignore_rules(MCP_DOCKERIGNORE.read_text(encoding='utf-8'))
    assert not _is_excluded_from_context('docker-compose.agent-client.yaml', rules)


def test_the_client_overlay_is_mcp_only():
    """No Makefile target and no CI step loads it: make test-system keeps the plain base + client pair."""
    assert 'agent-client' not in (REPO_ROOT / 'Makefile').read_text(encoding='utf-8')
    for workflow in (REPO_ROOT / '.github' / 'workflows').glob('*.yml'):
        assert 'docker-compose.agent-client.yaml' not in workflow.read_text(encoding='utf-8'), workflow


# --- schemas, timeouts and the removed agent-stack paths --------------------------------------------------------


def test_the_schemas_and_timeouts_are_unchanged_and_under_the_client_margin():
    assert runner.VERB_SCHEMAS['run_system_tests']['required'] == ['worktree', 'paths']
    assert set(runner.VERB_SCHEMAS['run_system_tests']['properties']) == {'worktree', 'paths'}
    assert set(runner.VERB_SCHEMAS['seed_dump']['properties']) == {'worktree', 'date'}
    assert runner.VERB_TIMEOUT_SECONDS['run_system_tests'] == runner.VERB_TIMEOUT_SECONDS['seed_dump'] == 1800
    client = json.loads((REPO_ROOT / '.mcp.json').read_text(encoding='utf-8'))
    [server] = [spec for spec in client['mcpServers'].values() if 'timeout' in spec]
    assert server['timeout'] == 1860 * 1000 and server['timeout'] > 1800 * 1000


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_the_descriptions_say_test_database_test_up_and_root_checkout(verb: str):
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type] -- only the verb table is read
    text = agent.describe(verb)
    for fragment in (
        'WRITES THE TEST DATABASE (./volume_data/test), never your dev data',
        'Needs test_up',
        "the root checkout's :test images",
        'snapshot of WORKTREE',
    ):
        assert fragment in text, (verb, fragment)


_REMOVED = {'_guarded_env', 'compose_prefix', 'ensure_env_files', '_steps', 'STACK_ENV_NAME', 'PROJECT'}


@pytest.mark.parametrize('handler', ['_run_system_tests', '_seed_dump', '_test_mode_rails'])
def test_the_pair_reaches_none_of_the_agent_stack_paths(handler: str):
    source = inspect.getsource(getattr(runner.AgentStack, handler))
    names = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(ast.parse(textwrap.dedent(source)))
        if isinstance(node, ast.Attribute | ast.Name)
    }
    assert not names & _REMOVED, names & _REMOVED


def test_the_client_builders_reach_none_of_the_agent_stack_paths():
    for builder in (stack.system_tests_steps, stack.seed_dump_steps, stack._testclient_steps, stack.testclient_prefix):
        source = inspect.getsource(builder)
        assert (
            'compose_prefix(' not in source.replace('testclient_prefix(', '')
            and '_steps(stack_dir, root_env' not in source
        )


def test_default_response_still_admits_no_system_suite_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The rig's DEV default: a sample refuses at the guard, so a sweep cannot pass these verbs by accident."""
    rig = make_rig(tmp_path, monkeypatch)
    rig.docker.respond = default_response
    for verb in VERBS:
        rig.docker.steps.clear()
        assert _call(rig, verb)['message'] == MODE_REFUSAL
    assert client_prefix_words(rig.layout)[-1] == f'{TRUSTED}/docker-compose.agent-client.yaml'


# --- the rework (5a38b7f, 5a18d7a): no secret anywhere, refusals audit their arguments -----------------------

SECRET_SENTINEL = 'WRITE_SECRET_SENTINEL_7f3a9c'


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_the_write_secret_is_on_no_command_line_env_output_or_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """Compose interpolates INSTANCE_WRITE_SECRET from the root env file itself; this server never holds it."""
    rig = _rig(tmp_path, monkeypatch)
    (rig.layout.repo / '.env').write_text(f'INSTANCE_WRITE_SECRET={SECRET_SENTINEL}\n')
    monkeypatch.setenv('INSTANCE_WRITE_SECRET', SECRET_SENTINEL)
    result = _call(rig, verb)
    assert result['status'] == 'ok', result
    for step in rig.docker.steps:
        assert all(SECRET_SENTINEL not in word for word in step.argv), step.argv
        assert 'INSTANCE_WRITE_SECRET' not in {name for name, _ in step.env}, step.env
        assert all(SECRET_SENTINEL not in value for _, value in step.env)
    assert SECRET_SENTINEL not in json.dumps(result)
    assert all(SECRET_SENTINEL not in line for line in rig.audit_lines())


def _refusals(layout) -> dict:
    return {
        'dev mode': _answer(is_mode_guard, _ok(DEV_LINE)),
        'the dev data mount': _answer(
            is_postgres_mount_read, _ok(f'{layout.repo_host_path}/volume_data/postgres\n'.encode())
        ),
        'a stale data_store': _stamp_of(DEV_CONTAINER_ID, b'f' * 64 + b'\n'),
    }


_REFUSAL_IDS = ['dev mode', 'the dev data mount', 'a stale data_store']
_AUDITED = {
    'run_system_tests': {'worktree': WORKTREE_NAME, 'paths': ['tests/system/test_one.py']},
    'seed_dump': {'worktree': WORKTREE_NAME},
}


@pytest.mark.parametrize('case', _REFUSAL_IDS)
@pytest.mark.parametrize('verb', sorted(VERBS))
def test_a_refused_call_audits_the_arguments_it_was_refused_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, case: str
):
    rig = make_rig(tmp_path, monkeypatch)
    answer, admitted = _refusals(rig.layout)[case], system_suite_response(rig.layout)
    rig.docker.respond = lambda step: answer(step) or admitted(step)
    assert _call(rig, verb)['status'] == 'refused'
    [audit] = rig.audit_lines()
    assert json.loads(audit)['arguments'] == _AUDITED[verb], audit


def test_a_refused_run_audits_the_normalised_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch, _answer(is_mode_guard, _ok(DEV_LINE)))
    paths = ['tests/system/./sub/../test_one.py']
    assert rig.call('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': paths})['status'] == 'refused'
    assert json.loads(rig.audit_lines()[0])['arguments'] == {
        'worktree': WORKTREE_NAME,
        'paths': ['tests/system/test_one.py'],
    }
