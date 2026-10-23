"""down and restart, the label-only STATE-CHANGING dev verbs (bead tj-iee6sn.7).

Design checked against: the bead's 22:05 and 04:31 UTC notes (down is label-only `docker compose
-p trader_joe down`, now `-p trader_joe_dev down` (tj-iee6sn.31): never -v, --volumes, --rmi or --remove-orphans, no compose file, no env file),
its 14:59 UTC notes (1)-(5) (D6 on restart only; the F2 and F4 wording; the F8 steering guard),
owner rulings D1/D2/D6/D7 on tj-iee6sn.1 and ADR tj-4rr0la addenda 22-24.

Every call goes through the REAL AgentStack with the fake-docker rig, so the order is the order the
verbs run, and a refusal is shown to run nothing after it by the steps the fake recorded:

    D7 prod guard (one `docker ps`)  ->  D6 symlink check (restart only)
        ->  steering guard  ->  budget charge  ->  the ONE compose step

Expected values are literals, never read from stack.py.
"""

import asyncio
import json
import os
import shlex
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import REPO_ROOT, FakeDocker, Rig, default_response, make_rig


pytestmark = pytest.mark.build_infra

DOCKER = '/usr/local/bin/docker'
# Dev and test's explicit project (ADR tj-4rr0la addendum 29, decision tj-oxxl8a option A; tj-iee6sn.31).
PROJECT = 'trader_joe_dev'
# Prod's project, compose's default: no verb here may ever see or touch a container of it.
PROD_PROJECT = 'trader_joe'
APPS = ('data_store', 'data_ingest', 'web')
GUARD = (
    DOCKER,
    'ps',
    '-a',
    '--filter',
    f'label=com.docker.compose.project={PROJECT}',
    # ADR tj-4rr0la addendum 26 (2), tj-iee6sn.24: one-off containers (alembic runs, test_client) are
    # ignored by the mode classification, as the lookups already ignore them.
    '--filter',
    'label=com.docker.compose.oneoff=False',
    '--format',
    '{{.Label "com.docker.compose.project.config_files"}}',
)
DOWN = (DOCKER, 'compose', '-p', PROJECT, 'down')


def restart(service: str) -> tuple[str, ...]:
    return (DOCKER, 'compose', '-p', PROJECT, 'restart', service)


# The steering subset of the pinned step env: no env file named, only the COMPOSE_* variables that
# would otherwise redirect a label-only compose (the 14:59 note (4)).
STEERING_ENV = (
    ('COMPOSE_PROJECT_NAME', PROJECT),
    ('COMPOSE_FILE', ''),
    ('COMPOSE_PATH_SEPARATOR', ':'),
    ('COMPOSE_ENV_FILES', ''),
    ('COMPOSE_PROFILES', ''),
    ('COMPOSE_REMOVE_ORPHANS', 'false'),
    ('COMPOSE_IGNORE_ORPHANS', 'false'),
    # F7, tj-iee6sn.8's 17:25 note (C): pinned in the shared steering env, so here too.
    ('COMPOSE_BAKE', 'false'),
)
# docker compose's documented default file names (compose-go DefaultFileNames and
# DefaultOverrideFileNames), and the dotenv file compose reads from the project directory.
STEERING_NAMES = (
    'compose.yaml',
    'compose.yml',
    'docker-compose.yaml',
    'docker-compose.yml',
    'compose.override.yaml',
    'compose.override.yml',
    'docker-compose.override.yaml',
    'docker-compose.override.yml',
    '.env',
)
PROD_LINE = b'/r/docker-compose.yaml,/r/docker-compose.web.yaml\n'
DEV_LINE = b'/r/docker-compose.yaml,/r/docker-compose.override.yaml,/r/docker-compose.web.yaml\n'
# The mode guard's PROD refusal, exactly: tj-iee6sn.24's WHAT (1) as tj-iee6sn.31 rewords it. Prod no
# longer lives in this project, so a prod-labelled container here is a stray, and make dev-down (the
# down of this very project) is what removes it -- never make prod-down, which would stop real prod.
GUARD_REFUSAL = (
    'project trader_joe_dev holds a container not started from the dev or test files (a prod launch?): '
    'stop it with make dev-down; tj-oxxl8a'
)
# DEV and TEST containers together (tj-iee6sn.24): which mode is up cannot be told.
MIXED_REFUSAL = (
    'project trader_joe_dev holds dev and test containers together, so which mode is up cannot be told: '
    'stop them with make dev-down'
)
# What make system-launch's containers carry: the base file and the fake overlay (TEST mode).
TEST_LINE = b'/r/docker-compose.yaml,/r/docker-compose.fake.yaml\n'
GUARD_FAILED = 'could not list the containers of project trader_joe_dev, so nothing was changed'
BUDGET_REFUSAL = '12 state-changing dev verbs in the last hour is the budget; wait, or run the make target yourself'
SERVICE_REFUSAL = 'service must be one of: data_store, data_ingest, web'
BUDGET = 12

VERBS = {'down': {}, 'restart': {'service': 'data_store'}}
EXPECTED_STEP = {'down': DOWN, 'restart': restart('data_store')}


def steering_refusal(path: Path) -> str:
    return f'{path} would steer compose: remove it, or run the make target yourself'


def symlink_refusal(link: str, source: str) -> str:
    on_the_way = '' if link == source else f' (on the way to {source})'
    return f'a symlink at {link}{on_the_way}: remove the link, or start the stack yourself'


class Responder:
    """The guard answers GUARD_STDOUT with GUARD_EXIT; every other step exits STEP_EXIT."""

    def __init__(self) -> None:
        self.guard_stdout = b''
        self.guard_exit = 0
        self.step_exit = 0

    def __call__(self, step: stack.Step) -> runner.ProcessResult:
        if step.argv == GUARD:
            return runner.ProcessResult(self.guard_exit, self.guard_stdout, b'')
        if step.argv[:2] == (DOCKER, 'compose'):
            return runner.ProcessResult(self.step_exit, b'', b'compose said no\n' if self.step_exit else b'')
        return default_response(step)


def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Rig, Responder]:
    responder = Responder()
    return make_rig(tmp_path, monkeypatch, FakeDocker(respond=responder)), responder


def _argvs(rig: Rig) -> list[tuple[str, ...]]:
    return [step.argv for step in rig.docker.steps]


def _new_argvs(rig: Rig, before: int) -> list[tuple[str, ...]]:
    return _argvs(rig)[before:]


def _call(rig: Rig, verb: str, arguments: Any) -> tuple[dict[str, Any], list[tuple[str, ...]]]:
    """One call, and the argvs IT ran (the rig keeps every call's steps)."""
    before = len(rig.docker.steps)
    result = rig.call(verb, arguments)
    return result, _new_argvs(rig, before)


def _audit(rig: Rig) -> dict[str, Any]:
    return json.loads(rig.audit_lines()[-1])


def _symlink_a_bind_source(rig: Rig) -> None:
    """server/common in the checkout becomes a link to the worktree's identical copy."""
    repo = rig.layout.repo
    shutil.move(str(repo / 'server' / 'common'), str(rig.layout.root / 'moved-common'))
    (repo / 'server' / 'common').symlink_to(rig.layout.worktree / 'server' / 'common', target_is_directory=True)


def _unlink_the_bind_source(rig: Rig) -> None:
    repo = rig.layout.repo
    (repo / 'server' / 'common').unlink()
    shutil.move(str(rig.layout.root / 'moved-common'), str(repo / 'server' / 'common'))


SYMLINK_REFUSAL = symlink_refusal('server/common', 'server/common')


# ===============================================================================================
# THE TOOL LIST: closed schemas, descriptions, timeouts
# ===============================================================================================


def test_the_schemas_are_closed_and_restart_takes_only_the_three_apps():
    assert runner.VERB_SCHEMAS['down'] == {
        'type': 'object',
        'additionalProperties': False,
        'properties': {},
        'required': [],
    }
    assert runner.VERB_SCHEMAS['restart'] == {
        'type': 'object',
        'additionalProperties': False,
        'properties': {'service': {'type': 'string', 'enum': list(APPS)}},
        'required': ['service'],
    }
    assert tuple(stack.DEV_RESTART_SERVICES) == APPS
    assert 'postgres' not in runner.VERB_SCHEMAS['restart']['properties']['service']['enum']


def test_both_verbs_are_on_the_tool_list_with_a_description_of_what_they_never_do():
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type] -- only the verb table is read
    assert {'down', 'restart'} <= set(agent.verbs)
    down = agent.describe('down')
    assert 'STATE-CHANGING' in down and '`docker compose -p trader_joe_dev down`' in down, down
    assert 'never removes a volume or an image and never passes --remove-orphans' in down, down
    assert 'reads no compose file and no env file' in down, down
    again = agent.describe('restart')
    assert 'STATE-CHANGING' in again and '`docker compose -p trader_joe_dev restart <service>`' in again, again
    assert 'data_store, data_ingest, web (never postgres)' in again, again
    assert 'creates, removes and rebuilds nothing' in again, again


def test_both_timeouts_are_300_seconds_inside_the_client_timeout():
    """300 s each (the bead); the .mcp.json margin sweep holds the client above the longest verb."""
    assert runner.VERB_TIMEOUT_SECONDS['down'] == 300
    assert runner.VERB_TIMEOUT_SECONDS['restart'] == 300
    client_ms = json.loads((REPO_ROOT / '.mcp.json').read_text(encoding='utf-8'))['mcpServers']['agent-stack'][
        'timeout'
    ]
    assert client_ms >= 300 * 1000 + 30_000, client_ms


# ===============================================================================================
# THE EXACT ARGV
# ===============================================================================================


def test_dev_down_runs_the_guard_then_exactly_a_label_only_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """D2 and the 04:31 amendment: no -v/--volumes/--rmi/--remove-orphans, no -f, no env file, -p trader_joe_dev."""
    rig, _ = _rig(tmp_path, monkeypatch)
    result, argvs = _call(rig, 'down', {})
    assert result['status'] == 'ok', result
    assert argvs == [GUARD, DOWN]
    assert result['message'] == 'the trader_joe_dev compose project is down; no volume or image was removed'


@pytest.mark.parametrize('service', APPS)
def test_dev_restart_runs_the_guard_then_exactly_a_restart_of_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: str
):
    """D1: restart of ONE existing container -- no up, no create, no recreate, so never two instances."""
    rig, _ = _rig(tmp_path, monkeypatch)
    result, argvs = _call(rig, 'restart', {'service': service})
    assert result['status'] == 'ok', result
    assert argvs == [GUARD, restart(service)]
    assert result['message'] == f'{service} restarted in the trader_joe_dev project'


def test_the_step_builders_build_one_unbuilding_step_in_the_stack_dir_with_the_steering_env():
    stack_dir = Path('/stack')
    for steps, argv in (
        (stack.dev_down_steps(stack_dir), DOWN),
        *((stack.dev_restart_steps(stack_dir, service), restart(service)) for service in APPS),
    ):
        assert len(steps) == 1, steps
        (step,) = steps
        assert step.argv == argv and step.cwd == stack_dir and step.env == STEERING_ENV, step
        assert step.builds is False and step.stdout_cap is None, step


def test_the_label_only_step_env_is_exactly_the_steering_variables():
    """No env-file variable at all: a label-only verb names no env file, even an in-repo default."""
    assert stack.dev_label_only_step_env() == STEERING_ENV


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_each_step_runs_in_the_stack_dir_and_only_the_compose_step_carries_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """The guard's `docker ps` gets no env; the compose step gets STEERING_ENV, recorded nowhere."""
    rig, _ = _rig(tmp_path, monkeypatch)
    result, _ = _call(rig, verb, VERBS[verb])
    assert result['status'] == 'ok', result
    guard, step = rig.docker.steps
    assert guard.argv == GUARD and guard.env == () and guard.cwd == rig.layout.stack_dir
    assert step.argv == EXPECTED_STEP[verb] and step.env == STEERING_ENV and step.cwd == rig.layout.stack_dir
    assert [record['command'] for record in result['steps']] == [shlex.join(GUARD), shlex.join(EXPECTED_STEP[verb])]
    assert 'COMPOSE_' not in json.dumps(result['steps']) and 'COMPOSE_' not in rig.audit_lines()[-1]


def test_the_compose_child_gets_the_steering_env_over_the_fixed_one_and_nothing_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Through the real run_process: the server's own COMPOSE_FILE does not reach compose."""
    monkeypatch.setenv('COMPOSE_FILE', '/tmp/evil.yaml')
    monkeypatch.setenv('COMPOSE_PROFILES', 'evil')
    (step,) = stack.dev_down_steps(tmp_path)
    result = asyncio.run(runner.run_process(replace(step, argv=('/usr/bin/env',))))
    assert result.exit_status == 0
    child = dict(line.split('=', 1) for line in result.stdout.decode().splitlines())
    assert child == {**runner.DOCKER_ENV, **dict(STEERING_ENV)}, child


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_a_failed_compose_step_reports_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.step_exit = 1
    result, argvs = _call(rig, verb, VERBS[verb])
    assert result['status'] == 'failed' and result['exit_status'] == 1, result
    assert argvs == [GUARD, EXPECTED_STEP[verb]]
    expected = {
        'down': 'compose down failed for the trader_joe_dev project',
        'restart': 'compose restart of data_store failed for the trader_joe_dev project',
    }
    assert result['message'] == expected[verb]


# ===============================================================================================
# THE CLOSED SERVICE ENUM, at the schema check and at validate_dev_restart_service
# ===============================================================================================

BAD_SERVICES = {
    'postgres': 'postgres',
    'empty': '',
    'unknown': 'redis',
    'a pgAdmin service': 'pgadmin',
    'traversal': '../web',
    'traversal back in': 'web/../postgres',
    'a trailing space': 'web ',
    'upper case': 'WEB',
    'a container name': 'trader_joe-web-1',
    'an option': '--all',
    'a second word': 'web data_store',
    'a nul': 'web\0',
    'None': None,
    'an int': 5,
    'a bool': True,
    'a list': ['web'],
    'a dict': {'name': 'web'},
}


@pytest.mark.parametrize('service', list(BAD_SERVICES.values()), ids=list(BAD_SERVICES))
def test_validate_refuses_everything_outside_the_three_apps(service: object):
    with pytest.raises(stack.Refused) as refused:
        stack.validate_dev_restart_service(service)
    assert str(refused.value) == SERVICE_REFUSAL


@pytest.mark.parametrize('service', APPS)
def test_validate_returns_each_app(service: str):
    assert stack.validate_dev_restart_service(service) == service


def test_the_restart_builder_validates_again():
    with pytest.raises(stack.Refused, match='service must be one of'):
        stack.dev_restart_steps(Path('/stack'), 'postgres')


@pytest.mark.parametrize('service', list(BAD_SERVICES.values()), ids=list(BAD_SERVICES))
def test_a_bad_service_is_refused_before_any_subprocess_and_never_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: object
):
    rig, _ = _rig(tmp_path, monkeypatch)
    result, argvs = _call(rig, 'restart', {'service': service})
    assert result['status'] == 'refused' and result['message'] == SERVICE_REFUSAL, result
    assert argvs == []
    assert _audit(rig)['arguments'] is None


EXTRA_KEYS = {
    'down with a service': ('down', {'service': 'web'}, "down takes no argument named 'service'"),
    'down with volumes': ('down', {'volumes': True}, "down takes no argument named 'volumes'"),
    'restart with an extra key': ('restart', {'service': 'web', 'extra': 1}, "restart takes no argument named 'extra'"),
    'restart with a tail': ('restart', {'service': 'web', 'tail': 5}, "restart takes no argument named 'tail'"),
    'restart with no service': ('restart', {}, 'restart needs service'),
    'down positional': ('down', ['down', '-v'], 'arguments must be an object'),
}


@pytest.mark.parametrize(('verb', 'arguments', 'message'), list(EXTRA_KEYS.values()), ids=list(EXTRA_KEYS))
def test_extra_missing_or_positional_arguments_are_refused_before_any_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, arguments: Any, message: str
):
    rig, _ = _rig(tmp_path, monkeypatch)
    result, argvs = _call(rig, verb, arguments)
    assert result['status'] == 'refused' and result['message'] == message, result
    assert argvs == []
    assert _audit(rig)['arguments'] is None


# ===============================================================================================
# THE AUDIT LINE
# ===============================================================================================


@pytest.mark.parametrize('service', APPS)
def test_the_audit_line_carries_the_validated_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: str):
    rig, _ = _rig(tmp_path, monkeypatch)
    rig.call('restart', {'service': service})
    audit = _audit(rig)
    assert audit['verb'] == 'restart' and audit['known'] is True, audit
    assert audit['arguments'] == {'service': service} and audit['status'] == 'ok', audit


def test_the_audit_line_of_dev_down_carries_no_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    rig.call('down', {})
    audit = _audit(rig)
    assert audit['verb'] == 'down' and audit['arguments'] == {} and audit['status'] == 'ok', audit


def test_a_guard_refused_restart_is_audited_with_its_validated_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.guard_stdout = PROD_LINE
    rig.call('restart', {'service': 'web'})
    audit = _audit(rig)
    assert audit['arguments'] == {'service': 'web'} and audit['status'] == 'refused', audit


# ===============================================================================================
# THE D7 PROD GUARD, FIRST
# ===============================================================================================


@pytest.mark.parametrize('verb', sorted(VERBS))
@pytest.mark.parametrize(
    'stdout', [PROD_LINE, DEV_LINE + PROD_LINE, b'\n', DEV_LINE[:-1]], ids=['prod', 'mixed', 'unlabelled', 'no newline']
)
def test_a_project_with_a_non_dev_container_refuses_with_only_the_guard_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, stdout: bytes
):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.guard_stdout = stdout
    result, argvs = _call(rig, verb, VERBS[verb])
    assert result['status'] == 'refused' and result['message'] == GUARD_REFUSAL, result
    assert argvs == [GUARD]


@pytest.mark.parametrize('verb', sorted(VERBS))
@pytest.mark.parametrize('exit_status', [1, 125, -9])
def test_a_failed_guard_listing_refuses_with_only_the_guard_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, exit_status: int
):
    """Fail closed: an empty stdout from a FAILED `docker ps` must not read as an empty project."""
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.guard_exit = exit_status
    result, argvs = _call(rig, verb, VERBS[verb])
    assert result['status'] == 'refused' and result['message'] == GUARD_FAILED, result
    assert argvs == [GUARD]


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_an_all_dev_project_passes_the_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.guard_stdout = DEV_LINE + DEV_LINE
    result, argvs = _call(rig, verb, VERBS[verb])
    assert result['status'] == 'ok', result
    assert argvs == [GUARD, EXPECTED_STEP[verb]]


# ===============================================================================================
# THE D6 SYMLINK CHECK: restart only (it STARTS a container), never down
# ===============================================================================================


@pytest.mark.parametrize('service', APPS)
def test_a_symlinked_bind_source_refuses_dev_restart_after_only_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: str
):
    """The 14:59 note (1): with a symlinked bind source restart runs only the `docker ps`."""
    rig, _ = _rig(tmp_path, monkeypatch)
    _symlink_a_bind_source(rig)
    result, argvs = _call(rig, 'restart', {'service': service})
    assert result['status'] == 'refused' and result['message'] == SYMLINK_REFUSAL, result
    assert argvs == [GUARD]


def test_a_symlinked_parent_of_a_bind_source_refuses_naming_the_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    repo = rig.layout.repo
    shutil.move(str(repo / 'server'), str(rig.layout.root / 'moved-server'))
    (repo / 'server').symlink_to(rig.layout.worktree / 'server', target_is_directory=True)
    result, argvs = _call(rig, 'restart', {'service': 'web'})
    assert result['message'] == symlink_refusal('server', 'server/data/store/app'), result
    assert argvs == [GUARD]


def test_dev_down_ignores_a_symlinked_bind_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The down verb starts nothing, so the daemon resolves no bind source: D6 does not apply to it."""
    rig, _ = _rig(tmp_path, monkeypatch)
    _symlink_a_bind_source(rig)
    result, argvs = _call(rig, 'down', {})
    assert result['status'] == 'ok', result
    assert argvs == [GUARD, DOWN]


# ===============================================================================================
# THE STEERING GUARD (F8): nothing compose would find by searching the stack dir and its parents
# ===============================================================================================


def test_the_steering_names_are_compose_s_default_files_their_overrides_and_dotenv():
    assert set(stack._STEERING_NAMES) == set(STEERING_NAMES)
    assert len(stack._STEERING_NAMES) == len(STEERING_NAMES)


@pytest.mark.parametrize('verb', sorted(VERBS))
@pytest.mark.parametrize('where', ['stack dir', 'parent'])
@pytest.mark.parametrize('name', STEERING_NAMES)
def test_a_steering_file_refuses_with_only_the_guard_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, where: str, name: str
):
    rig, _ = _rig(tmp_path, monkeypatch)
    directory = rig.layout.stack_dir if where == 'stack dir' else rig.layout.root
    (directory / name).write_text('services: {}\n')
    result, argvs = _call(rig, verb, VERBS[verb])
    assert result['status'] == 'refused' and result['message'] == steering_refusal(directory / name), result
    assert argvs == [GUARD]


def _deep(tmp_path: Path) -> tuple[Path, list[Path]]:
    """tmp/a/b/c/stack, and every directory the walk covers below tmp."""
    stack_dir = tmp_path / 'a' / 'b' / 'c' / 'stack'
    stack_dir.mkdir(parents=True)
    return stack_dir, [stack_dir, stack_dir.parent, tmp_path / 'a' / 'b', tmp_path / 'a', tmp_path]


# What a stack dir and its parents normally hold, none of which compose would read.
NOISE = (
    'agent_stack.env',
    'agent_stack_store.env',
    '.env.default',
    '.env.local',
    'audit.log',
    'compose.yaml.bak',
    'docker-compose.tools.yaml',
    'docker-compose.agent-dev.yaml',
    '.dockerenv',
)


def test_a_clean_tree_passes(tmp_path: Path):
    stack_dir, directories = _deep(tmp_path)
    for directory in directories:
        for name in NOISE:
            (directory / name).write_text('x\n')
    (stack_dir / 'source').mkdir()
    (stack_dir / 'source' / 'docker-compose.yaml').write_text('services: {}\n')
    (stack_dir / 'source' / '.env').write_text('X=1\n')
    stack.check_dev_steering_files(stack_dir)


@pytest.mark.parametrize('depth', range(5), ids=['stack', 'parent', 'grandparent', 'great-grandparent', 'tmp'])
@pytest.mark.parametrize('name', STEERING_NAMES)
def test_a_steering_name_anywhere_up_the_walk_refuses_naming_it(tmp_path: Path, name: str, depth: int):
    stack_dir, directories = _deep(tmp_path)
    (directories[depth] / name).write_text('x\n')
    with pytest.raises(stack.Refused) as refused:
        stack.check_dev_steering_files(stack_dir)
    assert str(refused.value) == steering_refusal(directories[depth] / name)


def _dangling(path: Path) -> None:
    path.symlink_to(path.parent / 'nowhere')


def _directory(path: Path) -> None:
    path.mkdir()


def _link_to_a_file(path: Path) -> None:
    (path.parent / 'real').write_text('x\n')
    path.symlink_to(path.parent / 'real')


def _fifo(path: Path) -> None:
    os.mkfifo(path)


@pytest.mark.parametrize('form', [_dangling, _directory, _link_to_a_file, _fifo], ids=lambda f: f.__name__[1:])
@pytest.mark.parametrize('name', ['.env', 'compose.yaml', 'docker-compose.override.yml'])
def test_a_steering_name_in_any_form_refuses(tmp_path: Path, name: str, form):
    """A dangling link, a directory called .env, a link, a fifo: the NAME refuses, whatever it is."""
    stack_dir, directories = _deep(tmp_path)
    form(directories[1] / name)
    with pytest.raises(stack.Refused) as refused:
        stack.check_dev_steering_files(stack_dir)
    assert str(refused.value) == steering_refusal(directories[1] / name)


def test_the_nearest_directory_is_named_first(tmp_path: Path):
    stack_dir, directories = _deep(tmp_path)
    (directories[3] / 'compose.yaml').write_text('x\n')
    (stack_dir / '.env').write_text('x\n')
    with pytest.raises(stack.Refused) as refused:
        stack.check_dev_steering_files(stack_dir)
    assert str(refused.value) == steering_refusal(stack_dir / '.env')


def test_the_steering_guard_only_lstats_and_never_opens_or_follows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Lstat only: every way to open, stat-through or list a directory is made to fail."""
    stack_dir, directories = _deep(tmp_path)

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f'the steering guard did more than lstat: {args!r}')

    (directories[2] / '.env').write_text('SECRET=1\n')
    with monkeypatch.context() as patched:
        for target, name in (
            (os, 'open'),
            (os, 'stat'),
            (os, 'scandir'),
            (os, 'listdir'),
            (os, 'readlink'),
            (Path, 'open'),
            (Path, 'read_text'),
            (Path, 'read_bytes'),
            (Path, 'exists'),
            (Path, 'is_file'),
            (Path, 'resolve'),
        ):
            patched.setattr(target, name, refuse)
        patched.setattr('builtins.open', refuse)
        with pytest.raises(stack.Refused) as refused:
            stack.check_dev_steering_files(stack_dir)
    assert str(refused.value) == steering_refusal(directories[2] / '.env')


def test_an_lstat_error_other_than_not_found_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stack_dir, directories = _deep(tmp_path)
    real_lstat = os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if str(path) == str(directories[1] / 'compose.yml'):
            raise PermissionError(13, 'Permission denied', str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, 'lstat', lstat)
    with pytest.raises(stack.Refused) as refused:
        stack.check_dev_steering_files(stack_dir)
    assert str(refused.value) == f'{directories[1] / "compose.yml"} could not be checked: Permission denied'


def test_an_lstat_error_refuses_the_verb_with_only_the_guard_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    real_lstat = os.lstat
    target = str(rig.layout.stack_dir / '.env')

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if str(path) == target:
            raise OSError(5, 'Input/output error', target)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, 'lstat', lstat)
    result, argvs = _call(rig, 'down', {})
    assert result['status'] == 'refused' and result['message'] == f'{target} could not be checked: Input/output error'
    assert argvs == [GUARD]


def test_not_a_directory_reads_as_not_there(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """ENOTDIR, as in the D6 helper, names nothing: it is not refused."""
    stack_dir, _ = _deep(tmp_path)

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        raise NotADirectoryError(20, 'Not a directory', str(path))

    monkeypatch.setattr(os, 'lstat', lstat)
    stack.check_dev_steering_files(stack_dir)


# ===============================================================================================
# THE ORDER: guard -> symlink (restart) -> steering -> budget -> step
# ===============================================================================================


def _exhaust(rig: Rig) -> None:
    for index in range(BUDGET):
        verb = 'down' if index % 2 else 'restart'
        assert rig.call(verb, VERBS[verb])['status'] == 'ok'


def _charged(rig: Rig) -> int:
    return len(rig.agent.dev_budget._calls)


def test_the_guard_runs_before_the_symlink_steering_and_budget_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    _exhaust(rig)
    _symlink_a_bind_source(rig)
    (rig.layout.stack_dir / '.env').write_text('x\n')
    responder.guard_stdout = PROD_LINE
    for verb in VERBS:
        result, argvs = _call(rig, verb, VERBS[verb])
        assert result['message'] == GUARD_REFUSAL, result
        assert argvs == [GUARD]


def test_the_symlink_check_runs_before_the_steering_and_budget_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    _exhaust(rig)
    _symlink_a_bind_source(rig)
    (rig.layout.stack_dir / '.env').write_text('x\n')
    result, argvs = _call(rig, 'restart', {'service': 'web'})
    assert result['message'] == SYMLINK_REFUSAL, result
    assert argvs == [GUARD]


def test_the_steering_check_runs_before_the_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    _exhaust(rig)
    (rig.layout.stack_dir / '.env').write_text('x\n')
    for verb in VERBS:
        result, argvs = _call(rig, verb, VERBS[verb])
        assert result['message'] == steering_refusal(rig.layout.stack_dir / '.env'), result
        assert argvs == [GUARD]


# ===============================================================================================
# THE BUDGET (S10): 12 admitted state-changing calls an hour, both verbs, only admitted ones charged
# ===============================================================================================


@pytest.mark.parametrize('verb', sorted(VERBS))
def test_the_thirteenth_admitted_call_refuses_after_only_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """Both verbs draw on one budget: six of each, then either refuses."""
    rig, _ = _rig(tmp_path, monkeypatch)
    _exhaust(rig)
    result, argvs = _call(rig, verb, VERBS[verb])
    assert result['status'] == 'refused' and result['message'] == BUDGET_REFUSAL, result
    assert argvs == [GUARD]
    assert _charged(rig) == BUDGET


def test_twelve_of_one_verb_exhaust_the_other(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    for _ in range(BUDGET):
        assert rig.call('down', {})['status'] == 'ok'
    assert rig.call('restart', {'service': 'web'})['message'] == BUDGET_REFUSAL


def test_refused_calls_are_never_charged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Refusals at every check, then twelve admitted calls still fit, and the thirteenth refuses."""
    rig, responder = _rig(tmp_path, monkeypatch)
    for _ in range(3):
        responder.guard_stdout = PROD_LINE
        assert rig.call('down', {})['message'] == GUARD_REFUSAL
        responder.guard_stdout = b''
        responder.guard_exit = 1
        assert rig.call('restart', {'service': 'web'})['message'] == GUARD_FAILED
        responder.guard_exit = 0
        _symlink_a_bind_source(rig)
        assert rig.call('restart', {'service': 'web'})['message'] == SYMLINK_REFUSAL
        _unlink_the_bind_source(rig)
        (rig.layout.stack_dir / 'compose.yaml').write_text('x\n')
        assert rig.call('down', {})['status'] == 'refused'
        (rig.layout.stack_dir / 'compose.yaml').unlink()
        assert rig.call('restart', {'service': 'postgres'})['message'] == SERVICE_REFUSAL
        assert rig.call('down', {'volumes': True})['status'] == 'refused'
    assert _charged(rig) == 0
    _exhaust(rig)
    assert _charged(rig) == BUDGET
    assert rig.call('down', {})['message'] == BUDGET_REFUSAL
    assert _charged(rig) == BUDGET


def test_a_budget_refusal_is_not_charged_either(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    _exhaust(rig)
    for _ in range(5):
        assert rig.call('down', {})['message'] == BUDGET_REFUSAL
    assert _charged(rig) == BUDGET


def test_an_admitted_call_whose_compose_step_fails_is_charged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """It was admitted and its step ran (and may have changed state), so it counts."""
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.step_exit = 1
    assert rig.call('down', {})['status'] == 'failed'
    assert _charged(rig) == 1


def test_read_only_dev_verbs_are_not_charged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    for _ in range(BUDGET + 1):
        assert rig.call('ps', {})['status'] == 'ok'
    assert _charged(rig) == 0
    _exhaust(rig)
    assert rig.call('down', {})['message'] == BUDGET_REFUSAL
