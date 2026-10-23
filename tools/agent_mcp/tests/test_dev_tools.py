"""tools, the file-based STATE-CHANGING `make dev-tools` over the user's dev project (bead tj-iee6sn.14).

Design checked against: the bead's description and the architect's 18:37 UTC readiness note on
tj-iee6sn.14 (the mirror is `up -d pgadmin`; --wait is kept only as the ONE named deviation; the file set
is the wrapper plus the dev prefix plus the trusted docker-compose.tools.yaml; one new lifecycle row;
PGADMIN_* never in a step env, argv or audit), the 14:59 note (S6 covers the tools file), owner rulings
D5-D7 and R2 on tj-iee6sn.1, and ADR tj-4rr0la addenda 22-24. tools is a PLAIN call (300 s, no job).
Order, a refusal at any point running nothing after it and charging nothing:

    D7 guard -> S6 stale copies (the tools file included) -> D6 bind-source symlinks -> D5 env-file
    preflight -> ONE budget charge -> devnet inspect, else create, else inspect again -> the file-based
    `up -d --wait pgadmin` over the dev files with the tools file inserted before the MCP-only overlay,
    run by up's fixed `sh -c` wrapper that derives INSTANCE_WRITE_SECRET inside the child.

No steering walk (every -f is given), no generated-code check, no :dev image inspect, no digest and no
base: pgadmin and postgres are image-only (test_bases.py::test_the_dev_tools_up_can_build_nothing).

Every verb call goes through the REAL AgentStack over the fake-docker rig. The credential tests run the
REAL wrapper under sh, with the REAL tools/dev_instance_secret.sh reading a FIXTURE env file holding FAKE
values, and a fake `docker` recording what it was given. Expected values are literals or parsed from the
Makefile and the compose files, never read from stack.py.
"""

import asyncio
import itertools
import json
import os
import re
import shlex
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from common.tests.test_ci_invariants import _make_variable
from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import (
    REPO_ROOT,
    FakeDocker,
    Rig,
    default_response,
    dev_up_compose_argv,
    is_dev_up_wrapper,
    make_rig,
)
from tools.agent_mcp.tests.test_dev_label_verbs import GUARD, GUARD_REFUSAL, PROD_LINE, symlink_refusal
from tools.agent_mcp.tests.test_dev_up import (
    DEV_FILES,
    DOCKER,
    HELPER,
    NET_CREATE,
    NET_INSPECT,
    PINNED_ENV,
    PROJECT,
    TRUSTED,
    UP_TAIL,
    WRAPPER_SCRIPT,
    _recipe,
    file_prefix,
    wrapper_head,
)


pytestmark = pytest.mark.build_infra

# Captured at import, before any rig forbids real subprocesses: the credential tests run the real wrapper.
REAL_CREATE_SUBPROCESS_EXEC = asyncio.create_subprocess_exec

TOOLS_FILE = 'docker-compose.tools.yaml'
# make's TOOLS_COMPOSE (the first four dev files, then the tools file), then the MCP-only overlay LAST.
TOOLS_FILES = (*DEV_FILES[:4], TOOLS_FILE, DEV_FILES[4])
# make dev-tools' `up -d pgadmin`, plus --wait: the one documented deviation from the recipe.
TOOLS_TAIL = ('up', '-d', '--wait', 'pgadmin')
OK_MESSAGE = 'pgAdmin is running in the trader_joe project; stop it with the down verb'
UP_FAILED = (
    'compose up of pgadmin failed for the trader_joe project; if the step output names PGADMIN_EMAIL or '
    'PGADMIN_PASS, set it in the root env file (the pgAdmin password needs 6+ characters); see the steps and call logs'
)
NETWORK_FAILED = 'the trader_joe_devnet network could not be created; pgAdmin was not started'
BUDGET_REFUSAL = '12 state-changing dev verbs in the last hour is the budget; wait, or run the make target yourself'
STALE_PREFIX = 'the MCP image is stale: '
BUDGET = 12
# FAKE values in a FIXTURE env file in a tmp layout: never a real credential.
FAKE_EMAIL = 'dummy-not-a-secret-pgadmin@example.invalid'
FAKE_PASS = 'dummy-not-a-secret-pgadmin-pass'
FAKE_SECRET = 'dummy-not-a-secret-instance-write'


def tools_prefix(host: Path) -> tuple[str, ...]:
    files = [word for name in TOOLS_FILES for word in ('-f', f'{TRUSTED}/{name}')]
    return (DOCKER, 'compose', '-p', PROJECT, '--project-directory', str(host), *files)


def tools_argv(host: Path) -> tuple[str, ...]:
    return (*wrapper_head(host), *tools_prefix(host), *TOOLS_TAIL)


def full_run(host: Path) -> list[tuple[str, ...]]:
    return [GUARD, NET_INSPECT, tools_argv(host)]


class Responder:
    """The fake daemon of a tools: each step's answer is a field the test can change."""

    def __init__(self) -> None:
        self.guard: tuple[int, bytes] = (0, b'')
        # Exit statuses for successive network inspects; exhausted, an inspect exits 0.
        self.network_inspects: list[int] = []
        self.create_exit = 0
        self.up_exit = 0

    def __call__(self, step: stack.Step) -> runner.ProcessResult:
        argv = step.argv
        if argv == GUARD:
            return runner.ProcessResult(self.guard[0], self.guard[1], b'')
        if argv == NET_INSPECT:
            exit_status = self.network_inspects.pop(0) if self.network_inspects else 0
            return runner.ProcessResult(exit_status, b'' if exit_status else b'trader_joe_devnet\n', b'')
        if argv == NET_CREATE:
            return runner.ProcessResult(self.create_exit, b'', b'')
        if is_dev_up_wrapper(step):
            return runner.ProcessResult(self.up_exit, b'', b'up said no\n' if self.up_exit else b'')
        return default_response(step)


def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Rig, Responder]:
    responder = Responder()
    return make_rig(tmp_path, monkeypatch, FakeDocker(respond=responder)), responder


def _call(rig: Rig, arguments: Any = None) -> dict[str, Any]:
    return rig.call('tools', {} if arguments is None else arguments)


def _argvs(rig: Rig) -> list[tuple[str, ...]]:
    return [step.argv for step in rig.docker.steps]


def _charges_left(rig: Rig) -> int:
    """How many more charges the budget takes before refusing (it is consumed by asking)."""
    for taken in range(BUDGET + 1):
        try:
            rig.agent.dev_budget.charge()
        except stack.Refused:
            return taken
    raise AssertionError('the budget never refused')


# ===============================================================================================
# THE CALL THAT WORKS
# ===============================================================================================


def test_dev_tools_runs_the_guard_the_network_and_the_wrapped_up_and_answers_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, _ = _rig(tmp_path, monkeypatch)
    result = _call(rig)
    assert (result['status'], result['message']) == ('ok', OK_MESSAGE), result
    assert _argvs(rig) == full_run(rig.layout.repo_host_path)
    assert all(step.cwd == rig.layout.stack_dir for step in rig.docker.steps), rig.docker.steps
    assert 'job' not in json.dumps(result).lower(), 'a plain call, never a job'
    assert _charges_left(rig) == BUDGET - 1, 'charged exactly once'


def test_the_up_is_the_six_wrapper_words_the_tools_prefix_and_up_d_wait_pgadmin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    host = rig.layout.repo_host_path
    up = rig.docker.steps[-1]
    assert up.argv[:6] == ('sh', '-c', WRAPPER_SCRIPT, 'sh', HELPER, f'{host}/.env')
    compose = dev_up_compose_argv(up)
    assert compose[:6] == (DOCKER, 'compose', '-p', PROJECT, '--project-directory', str(host))
    files = [value for flag, value in itertools.pairwise(compose[6:-4]) if flag == '-f']
    assert files == [f'{TRUSTED}/{name}' for name in TOOLS_FILES]
    assert files.index(f'{TRUSTED}/{TOOLS_FILE}') == len(files) - 2, 'the tools file sits just before the overlay'
    assert files[-1] == f'{TRUSTED}/docker-compose.agent-dev.yaml'
    assert compose[-4:] == TOOLS_TAIL


def test_postgres_is_named_by_no_argv_and_nothing_but_pgadmin_is_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Compose starts postgres through pgadmin's depends_on, as make does; the MCP never names it."""
    rig, _ = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    assert not [argv for argv in _argvs(rig) if 'postgres' in argv], _argvs(rig)
    compose = dev_up_compose_argv(rig.docker.steps[-1])
    assert '--no-deps' not in compose and compose.count('pgadmin') == 1
    for word in ('data_store', 'data_ingest', 'web', '--build', '--force-recreate', '--remove-orphans', '-v'):
        assert word not in compose, word


def test_no_steering_walk_no_generated_code_no_image_no_digest_and_no_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A compose file in the stack directory, missing generated code and missing :dev images change nothing."""
    rig, responder = _rig(tmp_path, monkeypatch)
    (rig.layout.stack_dir / 'compose.yaml').write_text('services: {}\n')
    (rig.layout.stack_dir / '.env').write_text('PGADMIN_PASS=planted\n')
    for generated in (rig.layout.repo / 'gen' / 'proto' / 'ts').rglob('*'):
        if generated.is_file():
            generated.unlink()

    def respond(step: stack.Step) -> runner.ProcessResult:
        if step.argv[1:3] == ('image', 'inspect'):
            return runner.ProcessResult(1, b'', b'Error: No such image')
        return responder(step)

    rig.docker.respond = respond
    result = _call(rig)
    assert result['status'] == 'ok', result
    assert _argvs(rig) == full_run(rig.layout.repo_host_path)
    assert not [argv for argv in _argvs(rig) if argv[1] in ('image', 'pull') or argv[0] == '/usr/bin/bash']


def test_no_step_is_flagged_as_building(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    assert [step for step in rig.docker.steps if step.builds] == []
    step = stack.dev_tools_step(tmp_path, Path('/h/repo'))
    assert step.builds is False and step.cwd == tmp_path and step.argv == tools_argv(Path('/h/repo'))


def test_only_the_up_carries_an_env_and_it_is_exactly_the_pinned_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    up, others = rig.docker.steps[-1], rig.docker.steps[:-1]
    assert up.env == PINNED_ENV == stack.dev_pinned_step_env()
    assert all(step.env == () for step in others), others
    assert stack.dev_tools_step(tmp_path, Path('/h/repo')).env == PINNED_ENV


def test_no_pgadmin_key_or_credential_word_is_in_any_argv_env_command_or_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, _ = _rig(tmp_path, monkeypatch)
    result = _call(rig)
    words = [word for step in rig.docker.steps for word in step.argv]
    env = [part for step in rig.docker.steps for pair in step.env for part in pair]
    commands = [record['command'] for record in result['steps']]
    # The variables, by name (the service word `pgadmin` is in the tail, and the tmp path says it too).
    for text in [*words, *env, *commands, *rig.audit_lines()]:
        assert 'PGADMIN_' not in text, text
    assert words.count('pgadmin') == 1, 'the service name, once, as the up target'
    assert commands == [shlex.join(argv) for argv in full_run(rig.layout.repo_host_path)]


# ===============================================================================================
# THE ORDER: each refusal runs only the steps before it and charges nothing
# ===============================================================================================


def _prod_container(rig: Rig, responder: Responder) -> None:
    responder.guard = (0, PROD_LINE)


def _stale_script(rig: Rig, responder: Responder) -> None:
    path = rig.layout.trusted_dir / 'tools' / 'dev_instance_secret.sh'
    path.write_bytes(path.read_bytes() + b'# drift\n')


def _stale_tools_file(rig: Rig, responder: Responder) -> None:
    """The 14:59 note: S6 covers docker-compose.tools.yaml, the one file only tools loads."""
    path = rig.layout.repo / TOOLS_FILE
    path.write_bytes(path.read_bytes() + b'# the checkout moved on\n')


def _symlinked_bind_source(rig: Rig, responder: Responder) -> None:
    repo = rig.layout.repo
    (repo / 'server' / 'common').rename(rig.layout.root / 'moved-common')
    (repo / 'server' / 'common').symlink_to(rig.layout.worktree / 'server' / 'common', target_is_directory=True)


def _env_file_a_directory(rig: Rig, responder: Responder) -> None:
    (rig.layout.repo / 'server' / 'data' / 'store' / '.env').mkdir(parents=True)


def _env_file_a_symlink(rig: Rig, responder: Responder) -> None:
    outside = rig.layout.root / 'elsewhere.env'
    outside.write_text(f'PGADMIN_PASS={FAKE_PASS}\n')
    (rig.layout.repo / '.env').symlink_to(outside)


def _spent_budget(rig: Rig, responder: Responder) -> None:
    for _ in range(BUDGET):
        rig.agent.dev_budget.charge()


REFUSALS = {
    'D7: a prod container in the project': (_prod_container, GUARD_REFUSAL),
    'S6: a stale baked script': (_stale_script, f'{STALE_PREFIX}tools/dev_instance_secret.sh differs'),
    'S6: a stale tools file': (_stale_tools_file, f'{STALE_PREFIX}{TOOLS_FILE} differs'),
    'D6: a symlinked bind source': (_symlinked_bind_source, symlink_refusal('server/common', 'server/common')),
    'D5: an env file that is a directory': (
        _env_file_a_directory,
        'server/data/store/.env is not a regular file: remove it, or start the stack yourself',
    ),
    'D5: an env file that is a symlink': (_env_file_a_symlink, symlink_refusal('.env', '.env')),
    'the budget spent': (_spent_budget, BUDGET_REFUSAL),
}


@pytest.mark.parametrize(('breaks', 'message'), list(REFUSALS.values()), ids=list(REFUSALS))
def test_each_refusal_runs_only_the_guard_and_charges_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, breaks, message: str
):
    rig, responder = _rig(tmp_path, monkeypatch)
    breaks(rig, responder)
    result = _call(rig)
    assert result['status'] == 'refused', result
    assert result['message'].startswith(message), result
    assert _argvs(rig) == [GUARD]
    if breaks is not _spent_budget:
        assert _charges_left(rig) == BUDGET, 'a refused call was charged'


PAIRS = {
    'D7 before S6': (_prod_container, _stale_tools_file, 'project trader_joe has containers not started'),
    'S6 before D6': (_stale_tools_file, _symlinked_bind_source, STALE_PREFIX),
    'D6 before D5': (_symlinked_bind_source, _env_file_a_directory, 'a symlink at server/common'),
    'D5 before the budget': (_env_file_a_directory, _spent_budget, 'server/data/store/.env is not a regular file'),
}


@pytest.mark.parametrize(('first', 'second', 'answer'), list(PAIRS.values()), ids=list(PAIRS))
def test_with_two_checks_failing_the_earlier_one_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first, second, answer: str
):
    rig, responder = _rig(tmp_path, monkeypatch)
    first(rig, responder)
    second(rig, responder)
    result = _call(rig)
    assert result['status'] == 'refused' and answer in result['message'], result


def test_a_failed_guard_listing_runs_nothing_else(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.guard = (1, b'')
    _stale_tools_file(rig, responder)
    result = _call(rig)
    assert result['message'] == 'could not list the containers of project trader_joe, so nothing was changed'
    assert _argvs(rig) == [GUARD] and _charges_left(rig) == BUDGET


def test_a_project_with_a_dev_tools_pgadmin_in_it_passes_the_next_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The container tools made labels itself with the tools -f list, which loads the override: DEV."""
    rig, responder = _rig(tmp_path, monkeypatch)
    label = ','.join(f'{TRUSTED}/{name}' for name in TOOLS_FILES)
    responder.guard = (0, f'{label}\n'.encode())
    assert _call(rig)['status'] == 'ok'


# ===============================================================================================
# FAILURES AFTER ADMISSION: charged once
# ===============================================================================================


def test_a_missing_network_is_created_then_looked_up_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.network_inspects = [1, 0]
    assert _call(rig)['status'] == 'ok'
    assert _argvs(rig) == [GUARD, NET_INSPECT, NET_CREATE, NET_INSPECT, tools_argv(rig.layout.repo_host_path)]


def test_no_network_after_the_create_fails_and_pgadmin_is_not_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.network_inspects = [1, 1]
    responder.create_exit = 1
    result = _call(rig)
    assert (result['status'], result['message']) == ('failed', NETWORK_FAILED), result
    assert _argvs(rig) == [GUARD, NET_INSPECT, NET_CREATE, NET_INSPECT]
    assert _charges_left(rig) == BUDGET - 1


def test_a_failed_up_answers_failed_naming_the_two_variables_and_is_charged_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.up_exit = 1
    result = _call(rig)
    assert (result['status'], result['message'], result['exit_status']) == ('failed', UP_FAILED, 1), result
    assert _argvs(rig) == full_run(rig.layout.repo_host_path)
    assert _charges_left(rig) == BUDGET - 1


# ===============================================================================================
# THE CHECK: check_dev_tools_argv accepts the one argv and nothing near it
# ===============================================================================================

_HOST = Path('/h/repo')
_GOOD = tools_argv(_HOST)
_HEAD = len(wrapper_head(_HOST))
_FORBIDDEN_MESSAGE = (
    'a dev lifecycle command removes no volume, image or orphan and names no compose file, project or env file'
)
_TABLE_MESSAGE = 'a dev lifecycle command is one of its fixed commands, with its fixed arguments, and no other'
_HEAD_MESSAGE = 'the dev tools wrapper is its six fixed words and nothing else'
_ROW_MESSAGE = 'the dev tools wrapper runs the fixed tools command, and no other'
_PREFIX_MESSAGE = 'a file-based dev lifecycle command names exactly the trusted dev compose files'
_TOOLS_AT = _GOOD.index(f'{TRUSTED}/{TOOLS_FILE}')


def _with(argv: tuple[str, ...], index: int, word: str) -> tuple[str, ...]:
    return (*argv[:index], word, *argv[index + 1 :])


def test_the_real_argv_is_accepted():
    stack.check_dev_tools_argv(_GOOD, _HOST)


_REFUSED = {
    'the pgadmin tail without the tools file': (
        (*wrapper_head(_HOST), *file_prefix(_HOST), *TOOLS_TAIL),
        _TABLE_MESSAGE,
    ),
    'the tools file on the up row': ((*wrapper_head(_HOST), *tools_prefix(_HOST), *UP_TAIL), _TABLE_MESSAGE),
    'the up argv itself': ((*wrapper_head(_HOST), *file_prefix(_HOST), *UP_TAIL), _ROW_MESSAGE),
    'the tools file after the overlay': (
        # The dev prefix then matches whole, and the moved -f is a forbidden word of the tail.
        (*_GOOD[: _TOOLS_AT - 1], *_GOOD[_TOOLS_AT + 1 : -4], '-f', _GOOD[_TOOLS_AT], *TOOLS_TAIL),
        f'{_FORBIDDEN_MESSAGE}: -f',
    ),
    "the checkout's tools file": (_with(_GOOD, _TOOLS_AT, f'{_HOST}/{TOOLS_FILE}'), _PREFIX_MESSAGE),
    'an extra service: postgres': ((*_GOOD, 'postgres'), _TABLE_MESSAGE),
    'an extra word: --build': ((*_GOOD[:-1], '--build', _GOOD[-1]), _TABLE_MESSAGE),
    '--no-deps present': ((*_GOOD[:-1], '--no-deps', _GOOD[-1]), _TABLE_MESSAGE),
    'no --wait (the recipe verbatim)': (tuple(word for word in _GOOD if word != '--wait'), _TABLE_MESSAGE),
    'a wait timeout added': ((*_GOOD[:-1], '--wait-timeout', '300', _GOOD[-1]), _TABLE_MESSAGE),
    'a forbidden word appended: -v': ((*_GOOD, '-v'), f'{_FORBIDDEN_MESSAGE}: -v'),
    'a forbidden word appended: --remove-orphans': (
        (*_GOOD, '--remove-orphans'),
        f'{_FORBIDDEN_MESSAGE}: --remove-orphans',
    ),
    'a forbidden word: -V': ((*_GOOD[:-1], '-V', _GOOD[-1]), f'{_FORBIDDEN_MESSAGE}: -V'),
    'a forbidden word: --env-file=': ((*_GOOD, '--env-file=/x'), f'{_FORBIDDEN_MESSAGE}: --env-file=/x'),
    'a forbidden word: a second -p': ((*_GOOD, '-p', 'other'), f'{_FORBIDDEN_MESSAGE}: -p'),
    'a forbidden word: --profile': ((*_GOOD[:-1], '--profile', 'tools', _GOOD[-1]), f'{_FORBIDDEN_MESSAGE}: --profile'),
    'a third -f': ((*_GOOD[:-4], '-f', '/tmp/x.yaml', *TOOLS_TAIL), f'{_FORBIDDEN_MESSAGE}: -f'),
    'another project directory': (_with(_GOOD, _HEAD + 5, '/elsewhere'), _PREFIX_MESSAGE),
    'another project name': (_with(_GOOD, _HEAD + 3, 'trader_joe_agent_stack'), 'a dev lifecycle verb runs only'),
    'the label-only down under the wrapper': (
        (*wrapper_head(_HOST), DOCKER, 'compose', '-p', PROJECT, 'down'),
        _ROW_MESSAGE,
    ),
    'a script that runs its own command': (_with(_GOOD, 2, 'exec "$@"; rm -rf /'), _HEAD_MESSAGE),
    "the checkout's helper": (_with(_GOOD, 4, '/h/repo/tools/dev_instance_secret.sh'), _HEAD_MESSAGE),
    'another env file': (_with(_GOOD, 5, '/h/repo/server/data/store/.env'), _HEAD_MESSAGE),
    'no wrapper at all': (_GOOD[_HEAD:], _HEAD_MESSAGE),
}


@pytest.mark.parametrize(('argv', 'message'), list(_REFUSED.values()), ids=list(_REFUSED))
def test_check_dev_tools_argv_refuses_every_other_argv(argv: tuple[str, ...], message: str):
    with pytest.raises(stack.Refused) as refused:
        stack.check_dev_tools_argv(argv, _HOST)
    assert str(refused.value).startswith(message), str(refused.value)


def test_the_dev_up_check_refuses_the_dev_tools_argv():
    """The two wrapped checks do not overlap: up's never accepts the pgAdmin up."""
    with pytest.raises(stack.Refused, match='the dev up wrapper runs the fixed up command'):
        stack.check_dev_up_argv(_GOOD, _HOST)


def test_the_step_builder_runs_its_argv_through_the_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[str, ...]] = []
    real = stack.check_dev_tools_argv

    def spy(argv, repo_host_path):
        seen.append(tuple(argv))
        real(argv, repo_host_path)

    monkeypatch.setattr(stack, 'check_dev_tools_argv', spy)
    step = stack.dev_tools_step(tmp_path, _HOST)
    assert seen == [step.argv]


def test_the_tools_file_list_is_the_first_four_dev_files_the_tools_file_and_the_overlay_last():
    assert stack.DEV_TOOLS_COMPOSE_FILES == TOOLS_FILES
    assert stack.DEV_TOOLS_COMPOSE_FILES[:4] == stack.DEV_COMPOSE_FILES[:4]
    assert stack.dev_tools_prefix(_HOST) == list(tools_prefix(_HOST))
    assert stack.DEV_TOOLS_TAIL == TOOLS_TAIL


# ===============================================================================================
# THE CREDENTIALS: the REAL wrapper under sh, a FIXTURE env file with FAKE values, a fake docker
# ===============================================================================================

# Records its argv and env, then does what compose's `:?` does for the two pgAdmin variables: it reads
# the project directory's root env file and refuses, naming the variable and the guard's message only,
# when one is unset or empty. (A stand-in for compose: the real refusal is the host sitting's to see.)
FAKE_DOCKER = """#!/bin/sh
here="$(dirname "$0")"
printf '%s\\n' "$@" > "$here/argv"
env > "$here/env"
dir=""
prev=""
for word in "$@"; do
  [ "$prev" = "--project-directory" ] && dir="$word"
  prev="$word"
done
for name in PGADMIN_EMAIL PGADMIN_PASS; do
  if ! grep -q "^$name=." "$dir/.env" 2>/dev/null; then
    echo "error while interpolating services.pgadmin.environment: required variable $name is missing a value: set $name in .env" >&2
    exit 1
  fi
done
echo "fake compose: container pgadmin Started"
"""


def _credential_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Rig, Path]:
    """A rig whose up step really runs: the wrapper argv as built, with the helper and docker words swapped."""
    rig, _ = _rig(tmp_path, monkeypatch)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', REAL_CREATE_SUBPROCESS_EXEC)
    fakebin = tmp_path / 'fakebin'
    fakebin.mkdir()
    fake = fakebin / 'docker'
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    real_helper = REPO_ROOT / 'tools' / 'dev_instance_secret.sh'

    async def run(step: stack.Step) -> runner.ProcessResult:
        if is_dev_up_wrapper(step):
            assert step.argv[4] == HELPER and step.argv[6] == DOCKER, step.argv
            argv = (*step.argv[:4], str(real_helper), step.argv[5], str(fake), *step.argv[7:])
            return await runner.run_process(replace(step, argv=argv))
        return await rig.docker(step)

    rig.agent = runner.AgentStack(rig.layout.settings, run=run)
    return rig, fakebin


def _child_env(fakebin: Path) -> dict[str, str]:
    lines = (fakebin / 'env').read_text(encoding='utf-8').splitlines()
    return dict(line.split('=', 1) for line in lines if '=' in line)


def _write_root_env(rig: Rig, text: str) -> None:
    (rig.layout.repo / '.env').write_text(text, encoding='utf-8')


def _everything_the_server_kept(rig: Rig, result: dict[str, Any]) -> str:
    return json.dumps(result) + '\n'.join(rig.audit_lines())


def test_compose_alone_reads_the_credentials_and_the_server_keeps_none_of_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, fakebin = _credential_rig(tmp_path, monkeypatch)
    _write_root_env(rig, f'INSTANCE_WRITE_SECRET={FAKE_SECRET}\nPGADMIN_EMAIL={FAKE_EMAIL}\nPGADMIN_PASS={FAKE_PASS}\n')
    result = _call(rig)
    assert (result['status'], result['message']) == ('ok', OK_MESSAGE), result
    child = _child_env(fakebin)
    # The child's env is the runner's fixed one, the pinned env and the derived secret: no PGADMIN key.
    expected = {**runner.DOCKER_ENV, **dict(PINNED_ENV), 'INSTANCE_WRITE_SECRET': FAKE_SECRET}
    assert {key: value for key, value in child.items() if key not in ('PWD', 'SHLVL', '_')} == expected
    host = rig.layout.repo_host_path
    assert (fakebin / 'argv').read_text(encoding='utf-8').splitlines() == [*tools_prefix(host), *TOOLS_TAIL][1:]
    kept = _everything_the_server_kept(rig, result)
    for value in (FAKE_EMAIL, FAKE_PASS, FAKE_SECRET):
        assert value not in kept, value
        assert value not in os.environ.values(), value
        assert not [word for word in rig.docker.steps[-1].argv if value in word]
    assert 'fake compose: container pgadmin Started' in result['steps'][-1]['stdout']['text']


def test_an_inherited_pgadmin_value_never_reaches_the_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Compose prefers its environment over the env file; the runner's fixed env means it is never the server's."""
    monkeypatch.setenv('PGADMIN_EMAIL', 'inherited-not-wanted@example.invalid')
    monkeypatch.setenv('PGADMIN_PASS', 'inherited-not-wanted')
    rig, fakebin = _credential_rig(tmp_path, monkeypatch)
    _write_root_env(rig, f'PGADMIN_EMAIL={FAKE_EMAIL}\nPGADMIN_PASS={FAKE_PASS}\n')
    assert _call(rig)['status'] == 'ok'
    assert not [key for key in _child_env(fakebin) if key.startswith('PGADMIN')]


@pytest.mark.parametrize(
    ('env_text', 'missing'),
    [
        (f'PGADMIN_PASS={FAKE_PASS}\n', 'PGADMIN_EMAIL'),
        (f'PGADMIN_EMAIL={FAKE_EMAIL}\nPGADMIN_PASS=\n', 'PGADMIN_PASS'),
        ('PGADMIN_EMAIL=\nPGADMIN_PASS=\n', 'PGADMIN_EMAIL'),
    ],
    ids=['email unset', 'password empty', 'both empty (.env.default as copied)'],
)
def test_an_unset_credential_is_composes_refusal_surfaced_as_failed_naming_the_variable_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_text: str, missing: str
):
    rig, _ = _credential_rig(tmp_path, monkeypatch)
    _write_root_env(rig, env_text)
    result = _call(rig)
    assert (result['status'], result['message'], result['exit_status']) == ('failed', UP_FAILED, 1), result
    stderr = result['steps'][-1]['stderr']['text']
    assert f'required variable {missing} is missing a value' in stderr, stderr
    kept = _everything_the_server_kept(rig, result)
    assert FAKE_EMAIL not in kept and FAKE_PASS not in kept
    assert _charges_left(rig) == BUDGET - 1


def test_the_real_tools_files_guards_name_the_variable_and_never_a_value():
    """What compose echoes on the `:?` refusal is the variable name and the guard's own words: read them."""
    text = (REPO_ROOT / TOOLS_FILE).read_text(encoding='utf-8')
    guards = dict(re.findall(r'\$\{(PGADMIN_[A-Z]+):\?([^}]*)\}', text))
    assert set(guards) == {'PGADMIN_EMAIL', 'PGADMIN_PASS'}, guards
    for name, message in guards.items():
        assert message.startswith(f'set {name} in .env'), message
        assert '$' not in message, 'a guard message that interpolates could echo a value'


# ===============================================================================================
# THE TOOLS FILE UNDER THIS VERB'S ENV (the APP_INTERNAL_PORT bug class)
# ===============================================================================================

_REFERENCE = re.compile(r'\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)')


def _references(text: str) -> dict[str, str | None]:
    """Name -> its operator (':-', '-', ':?', '?') or None, for every reference in TEXT."""
    found: dict[str, str | None] = {}
    for match in _REFERENCE.finditer(text):
        if match.group(0) != '$$':
            found[match.group(1) or match.group(4)] = match.group(2)
    return found


def _env_default_names() -> set[str]:
    text = (REPO_ROOT / '.env.default').read_text(encoding='utf-8')
    return set(re.findall(r'^([A-Z_][A-Z0-9_]*)=', text, re.MULTILINE))


def test_every_variable_the_tools_file_reads_comes_from_the_root_env_file_or_has_a_default():
    """Every reference of the tools file is sourced under tools' own env.

    tools passes no value of its own: compose has the pinned env and the root env file, nothing make
    adds. So every reference of the tools file is guarded, defaulted, or a key of .env.default (which the
    user's root env file is copied from). A reference only make supplies -- APP_INTERNAL_PORT's bug class --
    would render empty here, and none may exist.
    """
    references = _references((REPO_ROOT / TOOLS_FILE).read_text(encoding='utf-8'))
    assert set(references) == {'POSTGRES_UI_PORT', 'PGADMIN_EMAIL', 'PGADMIN_PASS', 'DATA_DIR'}, references
    pinned = dict(stack.dev_pinned_step_env())
    assert not set(references) & set(pinned), 'the pinned env must not shadow a tools-file value'
    unsourced = [
        name
        for name, operator in references.items()
        if operator not in (':-', '-') and name not in _env_default_names()
    ]
    assert unsourced == [], unsourced


def test_the_pgadmin_port_is_published_on_loopback_only():
    """Review (4): a browser UI holding the database login, bound to 127.0.0.1 on the host and nowhere else."""
    services = yaml.safe_load((REPO_ROOT / TOOLS_FILE).read_text(encoding='utf-8'))['services']
    assert set(services) == {'pgadmin'}
    assert services['pgadmin']['ports'] == ['127.0.0.1:${POSTGRES_UI_PORT}:80']
    assert 'build' not in services['pgadmin']


# ===============================================================================================
# THE SURFACE: schema, timeout, description
# ===============================================================================================


def test_the_schema_is_closed_and_takes_nothing():
    assert runner.VERB_SCHEMAS['tools'] == {
        'type': 'object',
        'properties': {},
        'required': [],
        'additionalProperties': False,
    }


@pytest.mark.parametrize(
    'arguments', [{'service': 'pgadmin'}, {'email': FAKE_EMAIL}, {'password': FAKE_PASS}, ['up'], 'up'], ids=repr
)
def test_any_argument_is_refused_with_no_step_and_no_charge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments):
    rig, _ = _rig(tmp_path, monkeypatch)
    result = _call(rig, arguments)
    assert result['status'] == 'refused'
    assert rig.docker.steps == [] and _charges_left(rig) == BUDGET
    assert FAKE_PASS not in '\n'.join(rig.audit_lines())


def test_the_timeout_is_five_minutes_inside_the_clients():
    assert runner.VERB_TIMEOUT_SECONDS['tools'] == 300
    config = json.loads((REPO_ROOT / '.mcp.json').read_text(encoding='utf-8'))
    assert config['mcpServers']['agent-stack']['timeout'] >= (300 + 30) * 1000


def test_dev_tools_is_on_the_tool_list_saying_what_it_starts_and_what_it_never_touches():
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type] -- only the verb table is read
    assert 'tools' in agent.verbs
    text = agent.describe('tools')
    assert text.startswith('STATE-CHANGING: `make dev-tools`'), text
    assert 'starts pgAdmin' in text and '127.0.0.1' in text and 'POSTGRES_UI_PORT' in text, text
    assert 'never builds or stops anything' in text, text
    assert 'PGADMIN_EMAIL and PGADMIN_PASS' in text and 'never reads, passes or prints them' in text, text
    assert 'down removes pgAdmin' in text and 'Up to 5 minutes' in text, text


# ===============================================================================================
# MIRRORS: make dev-tools and TOOLS_COMPOSE
# ===============================================================================================


def test_the_up_is_make_dev_tools_up_apart_from_the_one_documented_wait():
    """MIRROR: the recipe is `$(TOOLS_COMPOSE) up -d pgadmin`; the MCP adds --wait and nothing else."""
    prerequisites, lines = _recipe('dev-tools')
    assert 'dev-network' in prerequisites, prerequisites
    assert len(lines) == 1, lines
    words = shlex.split(lines[0])
    assert words[0] == '$(TOOLS_COMPOSE)', words
    assert tuple(words[1:]) == ('up', '-d', 'pgadmin')
    assert tuple(word for word in TOOLS_TAIL if word != '--wait') == tuple(words[1:])
    assert stack.DEV_TOOLS_TAIL == TOOLS_TAIL


def test_the_file_list_is_make_tools_compose_plus_the_overlay_last():
    tools = shlex.split(_make_variable('TOOLS_COMPOSE'))
    assert tools[0] == '$(DEV_COMPOSE)', tools
    dev = [value for flag, value in itertools.pairwise(shlex.split(_make_variable('DEV_COMPOSE'))) if flag == '-f']
    extra = [value for flag, value in itertools.pairwise(tools[1:]) if flag == '-f']
    assert (*dev, *extra, 'docker-compose.agent-dev.yaml') == TOOLS_FILES == stack.DEV_TOOLS_COMPOSE_FILES
    host = Path('/h/repo')
    prefix = stack.dev_tools_prefix(host)
    assert [value for flag, value in itertools.pairwise(prefix) if flag == '-f'] == [
        f'{TRUSTED}/{name}' for name in (*dev, *extra, 'docker-compose.agent-dev.yaml')
    ]
