"""up, the file-based STATE-CHANGING `make dev-up` over the user's dev project (bead tj-iee6sn.9).

Design checked against: the architect's 17:02 UTC consolidated note on tj-iee6sn.9 (it wins over the
description and the earlier notes, restating the 04:31 amendment and the 14:59 F9 wrapper), owner
rulings D1, D3, D5 and D8 on tj-iee6sn.1, and ADR tj-4rr0la addenda 22-24 (addendum 23 (3): the secret).
up is a PLAIN call (D8, 600 s, no job). Order, a refusal at any point running nothing after it and
charging nothing, and a failure before the down never stopping anything:

    D7 guard -> S6 stale copies -> D6 bind-source symlinks -> D5 env-file preflight -> the steering
    walk (the label-only down passes no -f, so compose searches the stack directory and its parents)
    -> generated-code freshness -> one `docker image inspect` per :dev image (missing: 'call build')
    -> ONE budget charge -> the two trusted digests (a failure returns BEFORE the down) -> the
    label-only `docker compose -p trader_joe_dev down` -> devnet inspect, else create, else inspect again
    -> the bases (the up can build) -> the file-based up of postgres and the three apps, run by the
    fixed `sh -c` wrapper that derives INSTANCE_WRITE_SECRET inside the child.

Every verb call goes through the REAL AgentStack over the fake-docker rig. The secret tests run the
REAL wrapper script under sh, with the REAL tools/dev_instance_secret.sh reading a FAKE env file in a
tmp layout and a fake `docker` recording what it was given. Expected values are literals or parsed
from the Makefile, never read from stack.py (the script constant is pinned by a literal copy).
"""

import asyncio
import itertools
import json
import os
import re
import shlex
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from common.tests.test_ci_invariants import _make_variable
from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import (
    REPO_ROOT,
    FakeDocker,
    Rig,
    default_response,
    dev_up_compose_argv,
    is_dev_digest,
    is_dev_up_wrapper,
    make_rig,
)
from tools.agent_mcp.tests.test_dev_label_verbs import (
    GUARD,
    GUARD_REFUSAL,
    PROD_LINE,
    STEERING_ENV,
    steering_refusal,
    symlink_refusal,
)


pytestmark = pytest.mark.build_infra

# Captured at import, before any rig forbids real subprocesses: the secret tests run the real wrapper.
REAL_CREATE_SUBPROCESS_EXEC = asyncio.create_subprocess_exec

DOCKER = '/usr/local/bin/docker'
PROJECT = 'trader_joe_dev'
# The Makefile's DEV_PROJECT since tj-grna9p.161 (ADR tj-4rr0la addendum 29), spelled out so the
# oracle is not the file under test. PROJECT above is the MCP's, which tj-iee6sn.31 moved to this.
MAKEFILE_DEV_PROJECT = 'trader_joe_dev'
TRUSTED = '/opt/agent_mcp/compose'
HELPER = f'{TRUSTED}/tools/dev_instance_secret.sh'
BASH = '/usr/bin/bash'
DIGEST_SCRIPT = f'{TRUSTED}/tools/source_digest.sh'
DEV_FILES = (
    'docker-compose.yaml',
    'docker-compose.override.yaml',
    'docker-compose.web.yaml',
    'docker-compose.web.dev.yaml',
    'docker-compose.agent-dev.yaml',
)
UP_TAIL = ('up', '-d', '--wait', '--wait-timeout', '300', 'postgres', 'data_store', 'data_ingest', 'web')
# THE WRAPPER SCRIPT, a literal copy (the 14:59 / 17:02 notes: 'the script constant is pinned'). Reads
# the helper and the env file from $1 and $2, exports the helper's stdout, shifts them off, execs the
# rest. No formatting, no interpolation: the compose argv is never script text.
WRAPPER_SCRIPT = 'INSTANCE_WRITE_SECRET="$(sh "$1" "$2")" || exit 1\nexport INSTANCE_WRITE_SECRET\nshift 2\nexec "$@"'
DEV_IMAGES = ('trader_joe_data_store:dev', 'trader_joe_data_ingest:dev', 'trader_joe_web:dev')
NETWORK = 'trader_joe_devnet'
DOWN = (DOCKER, 'compose', '-p', PROJECT, 'down')
NET_INSPECT = (DOCKER, 'network', 'inspect', '--format', '{{.Name}}', NETWORK)
NET_CREATE = (DOCKER, 'network', 'create', '--driver', 'bridge', NETWORK)
STORE_DIGEST = '1' * 64
INGEST_DIGEST = '2' * 64
# compose reads the project's env files itself (D5/R2): the in-repo defaults pinned, the steering
# variables pinned, NEVER the placeholder env (no POSTGRES_PASS, no INSTANCE_WRITE_SECRET, nothing at /dev/null).
PINNED_ENV = (
    ('ROOT_ENV_FILE', '.env'),
    ('STORE_ENV_FILE', './server/data/store/.env'),
    ('INGEST_ENV_FILE', './server/data/ingest/.env'),
    *STEERING_ENV,
)
UP_ENV = (*PINNED_ENV, ('SOURCE_DIGEST_DATA_STORE', STORE_DIGEST), ('SOURCE_DIGEST_DATA_INGEST', INGEST_DIGEST))
OK_MESSAGE = 'the trader_joe_dev project was stopped and postgres, data_store, data_ingest and web are up and healthy'
DOWN_FAILED = 'compose down failed for the trader_joe_dev project; nothing was started'
NETWORK_FAILED = 'the trader_joe_devnet network could not be created; the project is down'
UP_FAILED = 'compose up failed for the trader_joe_dev project; see the steps and call logs'
BUDGET_REFUSAL = '12 state-changing dev verbs in the last hour is the budget; wait, or run the make target yourself'
STALE_PREFIX = 'the MCP image is stale: '
GENERATE_HINT = 'run make proto gen-proto-ts in the root checkout'
BUDGET = 12
# A FAKE secret in a FAKE env file in a tmp layout: never a real value.
FAKE_SECRET = 'fake-instance-secret-0d5c9e71'


def file_prefix(host: Path) -> tuple[str, ...]:
    files = [word for name in DEV_FILES for word in ('-f', f'{TRUSTED}/{name}')]
    return (DOCKER, 'compose', '-p', PROJECT, '--project-directory', str(host), *files)


def wrapper_head(host: Path) -> tuple[str, ...]:
    return ('sh', '-c', WRAPPER_SCRIPT, 'sh', HELPER, f'{host}/.env')


def up_argv(host: Path) -> tuple[str, ...]:
    return (*wrapper_head(host), *file_prefix(host), *UP_TAIL)


def image_inspect(ref: str) -> tuple[str, ...]:
    return (DOCKER, 'image', 'inspect', '--format', '{{.Id}}', ref)


def digest_argv(host: Path, service_name: str) -> tuple[str, ...]:
    return (BASH, DIGEST_SCRIPT, '--context', str(host), 'data', service_name)


def base_inspects() -> list[tuple[str, ...]]:
    return [image_inspect(ref) for ref in stack.BASE_IMAGES]


def before_bases(host: Path) -> list[tuple[str, ...]]:
    """Everything a call runs ahead of the base images: guard, the three image inspects, the two digests."""
    return [GUARD, *(image_inspect(ref) for ref in DEV_IMAGES), digest_argv(host, 'store'), digest_argv(host, 'ingest')]


def before_down(host: Path) -> list[tuple[str, ...]]:
    """Everything a call runs ahead of the down: the above, then the base inspects (17:33 gate ruling F-A)."""
    return [*before_bases(host), *base_inspects()]


def full_run(host: Path) -> list[tuple[str, ...]]:
    return [*before_down(host), DOWN, NET_INSPECT, up_argv(host)]


class Responder:
    """The fake daemon of an up call: each step's answer is a field the test can change."""

    def __init__(self) -> None:
        self.guard: tuple[int, bytes] = (0, b'')
        self.missing_images: set[str] = set()
        self.digests: dict[str, tuple[int | None, bytes]] = {
            'store': (0, f'{STORE_DIGEST}\n'.encode()),
            'ingest': (0, f'{INGEST_DIGEST}\n'.encode()),
        }
        self.down_exit = 0
        # Exit statuses for successive network inspects; exhausted, an inspect exits 0.
        self.network_inspects: list[int] = []
        self.create_exit = 0
        self.up_exit = 0

    def __call__(self, step: stack.Step) -> runner.ProcessResult:
        argv = step.argv
        if argv == GUARD:
            return runner.ProcessResult(self.guard[0], self.guard[1], b'')
        if argv[1:3] == ('image', 'inspect') and argv[-1] in DEV_IMAGES:
            missing = argv[-1] in self.missing_images
            return runner.ProcessResult(1 if missing else 0, b'' if missing else b'sha256:abc\n', b'')
        if is_dev_digest(step):
            exit_status, stdout = self.digests[argv[-1]]
            return runner.ProcessResult(exit_status, stdout, b'')
        if argv == DOWN:
            return runner.ProcessResult(self.down_exit, b'', b'down said no\n' if self.down_exit else b'')
        if argv == NET_INSPECT:
            exit_status = self.network_inspects.pop(0) if self.network_inspects else 0
            return runner.ProcessResult(exit_status, b'' if exit_status else f'{NETWORK}\n'.encode(), b'')
        if argv == NET_CREATE:
            return runner.ProcessResult(self.create_exit, b'', b'')
        if is_dev_up_wrapper(step):
            return runner.ProcessResult(self.up_exit, b'', b'up said no\n' if self.up_exit else b'')
        return default_response(step)


def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Rig, Responder]:
    responder = Responder()
    return make_rig(tmp_path, monkeypatch, FakeDocker(respond=responder)), responder


def _call(rig: Rig, arguments: Any = None) -> dict[str, Any]:
    return rig.call('up', {} if arguments is None else arguments)


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


def test_dev_up_runs_every_step_in_the_designed_order_and_answers_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    result = _call(rig)
    assert result['status'] == 'ok', result
    assert result['message'] == OK_MESSAGE
    assert _argvs(rig) == full_run(rig.layout.repo_host_path)
    assert all(step.cwd == rig.layout.stack_dir for step in rig.docker.steps), rig.docker.steps
    assert 'job' not in json.dumps(result).lower(), 'D8: a plain call, never a job'


def test_postgres_appears_in_exactly_one_argv_the_ups_service_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The 04:31 ACCEPTANCE: postgres is in the up's explicit list, and in no other argv of the call."""
    rig, _ = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    with_postgres = [argv for argv in _argvs(rig) if 'postgres' in argv]
    assert with_postgres == [up_argv(rig.layout.repo_host_path)]
    compose = dev_up_compose_argv(rig.docker.steps[-1])
    assert compose[-len(UP_TAIL) :] == UP_TAIL
    assert '--no-deps' not in compose and 'pgadmin' not in compose


def test_a_missing_postgres_container_proceeds_to_the_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """D3 (ruled): the up CREATES postgres; nothing looks a postgres container up, nothing refuses its absence.

    The guard's listing is empty (no container at all, postgres included) and the call still ends ok.
    """
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.guard = (0, b'')
    result = _call(rig)
    assert result['status'] == 'ok', result
    up = up_argv(rig.layout.repo_host_path)
    looked_up = [
        argv
        for argv in _argvs(rig)
        if argv != up and any(word == 'postgres' or word.endswith('service=postgres') for word in argv)
    ]
    assert looked_up == [], looked_up
    assert _argvs(rig)[-1] == up


def test_only_the_up_carries_the_pinned_env_and_the_digests_and_the_down_only_the_steering_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    by_argv = {step.argv: step.env for step in rig.docker.steps}
    host = rig.layout.repo_host_path
    assert by_argv[up_argv(host)] == UP_ENV, by_argv[up_argv(host)]
    assert by_argv[DOWN] == STEERING_ENV
    others = {argv: env for argv, env in by_argv.items() if argv not in (up_argv(host), DOWN)}
    assert all(env == () for env in others.values()), others
    assert stack.dev_pinned_step_env() == PINNED_ENV


def test_the_up_env_names_no_secret_and_no_placeholder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The secret is derived in the child (addendum 23 (3)); the build's placeholder env is never used here."""
    rig, _ = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    env = dict(rig.docker.steps[-1].env)
    assert 'INSTANCE_WRITE_SECRET' not in env and 'POSTGRES_PASS' not in env and 'APP_INTERNAL_PORT' not in env
    assert '/dev/null' not in env.values(), env
    assert env['COMPOSE_ENV_FILES'] == ''


def test_no_digest_appears_in_a_recorded_command_or_the_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    result = _call(rig)
    commands = [record['command'] for record in result['steps']]
    assert commands[-1] == shlex.join(up_argv(rig.layout.repo_host_path))
    # (The wrapper's script word names INSTANCE_WRITE_SECRET; the value is the secret tests' subject below.)
    for word in ('SOURCE_DIGEST', 'COMPOSE_'):
        assert not [command for command in commands if word in command], (word, commands)
    for digest in (STORE_DIGEST, INGEST_DIGEST):
        assert not [line for line in rig.audit_lines() if digest in line]
        # The digest steps' own stdout is the digest; no command line names one.
        assert not [command for command in commands if digest in command]


def test_only_the_up_step_is_flagged_as_building(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The up builds (compose builds a missing image on `up`), so the bases come first; nothing else can.

    17:33 gate ruling F-A: the bases are ensured after the digests and BEFORE the down, once; the up's
    own implicit ensure (builds=True, the backstop) then adds nothing because bases_present is set.
    """
    rig, _ = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    assert [step.argv for step in rig.docker.steps if step.builds] == [up_argv(rig.layout.repo_host_path)]
    argvs = _argvs(rig)
    bases = [index for index, argv in enumerate(argvs) if argv in base_inspects()]
    assert len(bases) == len(stack.BASE_IMAGES), 'each base is inspected exactly once per call'
    assert max(bases) < argvs.index(DOWN), 'the bases run before anything is stopped'
    assert min(bases) > argvs.index(digest_argv(rig.layout.repo_host_path, 'ingest')), 'the bases follow the digests'
    assert argvs[-2] == NET_INSPECT, 'no base step runs between the devnet and the up'


# ===============================================================================================
# THE ORDER: each refusal runs only the steps before it and charges nothing
# ===============================================================================================


def _prod_container(rig: Rig, responder: Responder) -> None:
    responder.guard = (0, PROD_LINE)


def _stale_copy(rig: Rig, responder: Responder) -> None:
    path = rig.layout.trusted_dir / 'tools' / 'dev_instance_secret.sh'
    path.write_bytes(path.read_bytes() + b'# drift\n')


def _symlinked_bind_source(rig: Rig, responder: Responder) -> None:
    repo = rig.layout.repo
    shutil.move(str(repo / 'server' / 'common'), str(rig.layout.root / 'moved-common'))
    (repo / 'server' / 'common').symlink_to(rig.layout.worktree / 'server' / 'common', target_is_directory=True)


def _env_file_a_directory(rig: Rig, responder: Responder) -> None:
    (rig.layout.repo / 'server' / 'data' / 'store' / '.env').mkdir(parents=True)


def _env_file_a_symlink(rig: Rig, responder: Responder) -> None:
    outside = rig.layout.root / 'elsewhere.env'
    outside.write_text('INSTANCE_WRITE_SECRET=planted\n')
    (rig.layout.repo / '.env').symlink_to(outside)


def _steering_file(rig: Rig, responder: Responder) -> None:
    (rig.layout.stack_dir / 'compose.yaml').write_text('services: {}\n')


def _stale_generated(rig: Rig, responder: Responder) -> None:
    shutil.rmtree(rig.layout.repo / 'gen' / 'proto' / 'ts')


def _spent_budget(rig: Rig, responder: Responder) -> None:
    for _ in range(BUDGET):
        rig.agent.dev_budget.charge()


def _missing_image(index: int):
    def apply(rig: Rig, responder: Responder) -> None:
        responder.missing_images.add(DEV_IMAGES[index])

    return apply


def _image_refusal(index: int) -> str:
    return f'the image {DEV_IMAGES[index]} is not on the daemon: call build first'


# name -> (break it, the refusal message, how many steps ran: the guard plus the inspects up to it)
REFUSALS = {
    'D7: a prod container in the project': (_prod_container, GUARD_REFUSAL, 1),
    'S6: a stale baked copy': (_stale_copy, None, 1),
    'D6: a symlinked bind source': (_symlinked_bind_source, symlink_refusal('server/common', 'server/common'), 1),
    'D5: an env file that is a directory': (
        _env_file_a_directory,
        'server/data/store/.env is not a regular file: remove it, or start the stack yourself',
        1,
    ),
    'D5: an env file that is a symlink': (_env_file_a_symlink, symlink_refusal('.env', '.env'), 1),
    'steering: a compose file in the stack directory': (_steering_file, None, 1),
    'stale generated code': (_stale_generated, f'gen/proto/ts is missing or empty: {GENERATE_HINT}', 1),
    'no data_store:dev image': (_missing_image(0), _image_refusal(0), 2),
    'no data_ingest:dev image': (_missing_image(1), _image_refusal(1), 3),
    'no web:dev image': (_missing_image(2), _image_refusal(2), 4),
    'the budget spent': (_spent_budget, BUDGET_REFUSAL, 4),
}


@pytest.mark.parametrize(('breaks', 'message', 'ran'), list(REFUSALS.values()), ids=list(REFUSALS))
def test_each_refusal_runs_only_the_steps_before_it_and_charges_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, breaks, message: str | None, ran: int
):
    rig, responder = _rig(tmp_path, monkeypatch)
    breaks(rig, responder)
    result = _call(rig)
    assert result['status'] == 'refused', result
    if breaks is _stale_copy:
        assert result['message'].startswith(f'{STALE_PREFIX}tools/dev_instance_secret.sh differs'), result
    elif breaks is _steering_file:
        assert result['message'] == steering_refusal(rig.layout.stack_dir / 'compose.yaml'), result
    else:
        assert result['message'] == message, result
    assert _argvs(rig) == full_run(rig.layout.repo_host_path)[:ran]
    assert DOWN not in _argvs(rig)
    if breaks is not _spent_budget:
        assert _charges_left(rig) == BUDGET, 'a refused call was charged'


# Two things broken at once: the EARLIER check of the designed order must be the one that answers.
PAIRS = {
    'S6 before D6': (_stale_copy, _symlinked_bind_source, STALE_PREFIX),
    'D6 before D5': (_symlinked_bind_source, _env_file_a_directory, 'a symlink at server/common'),
    'D5 before steering': (_env_file_a_directory, _steering_file, 'server/data/store/.env is not a regular file'),
    'steering before freshness': (_steering_file, _stale_generated, 'would steer compose'),
    'freshness before the images': (_stale_generated, _missing_image(0), 'gen/proto/ts is missing or empty'),
    'the images before the budget': (_missing_image(0), _spent_budget, 'the image trader_joe_data_store:dev'),
    'D7 before S6': (_prod_container, _stale_copy, 'project trader_joe_dev holds a container not started'),
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


def test_the_guard_runs_first_even_with_every_later_check_failing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.guard = (1, b'')
    for breaks in (_stale_copy, _symlinked_bind_source, _steering_file, _stale_generated):
        breaks(rig, responder)
    result = _call(rig)
    assert result['message'] == 'could not list the containers of project trader_joe_dev, so nothing was changed'
    assert _argvs(rig) == [GUARD]


# ===============================================================================================
# FAILURES AFTER ADMISSION: charged once; nothing before the down stops anything
# ===============================================================================================

_BAD_DIGESTS = {
    'store exits 1': ('store', (1, b'')),
    'ingest exits 1': ('ingest', (1, b'')),
    'store killed': ('store', (None, b'')),
    'ingest malformed': ('ingest', (0, b'not-a-digest\n')),
    'store two lines': ('store', (0, f'{STORE_DIGEST}\n{STORE_DIGEST}\n'.encode())),
}


@pytest.mark.parametrize(('service_name', 'answer'), list(_BAD_DIGESTS.values()), ids=list(_BAD_DIGESTS))
def test_a_failed_digest_returns_before_the_down_and_is_charged_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service_name: str, answer
):
    """The 17:02 note: a failed or malformed digest returns failed BEFORE the down, so it never leaves the stack down."""
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.digests[service_name] = answer
    result = _call(rig)
    assert result['status'] == 'failed', result
    assert result['message'] == f'the source digest failed (data {service_name}); nothing was stopped or started'
    host = rig.layout.repo_host_path
    ran = before_bases(host)[: 5 if service_name == 'store' else 6]
    assert _argvs(rig) == ran
    assert DOWN not in _argvs(rig) and not [argv for argv in _argvs(rig) if argv[1] == 'network']
    assert not [argv for argv in _argvs(rig) if argv in base_inspects()], 'a failed digest pulls no base'
    assert _charges_left(rig) == BUDGET - 1


def test_a_failed_down_starts_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.down_exit = 1
    result = _call(rig)
    assert (result['status'], result['message']) == ('failed', DOWN_FAILED), result
    assert _argvs(rig) == [*before_down(rig.layout.repo_host_path), DOWN]
    assert _charges_left(rig) == BUDGET - 1


def test_a_missing_network_is_created_then_looked_up_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The chain of make dev-network: inspect, else create, else inspect; here the create made it."""
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.network_inspects = [1, 0]
    assert _call(rig)['status'] == 'ok'
    host = rig.layout.repo_host_path
    assert _argvs(rig) == [*before_down(host), DOWN, NET_INSPECT, NET_CREATE, NET_INSPECT, up_argv(host)]


def test_a_create_that_lost_a_race_still_finds_the_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Another creator won: the create fails, the second inspect finds it, the up runs."""
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.network_inspects = [1, 0]
    responder.create_exit = 1
    assert _call(rig)['status'] == 'ok'
    host = rig.layout.repo_host_path
    assert _argvs(rig) == [*before_down(host), DOWN, NET_INSPECT, NET_CREATE, NET_INSPECT, up_argv(host)]


def test_no_network_after_the_create_fails_with_the_project_left_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.network_inspects = [1, 1]
    result = _call(rig)
    assert (result['status'], result['message']) == ('failed', NETWORK_FAILED), result
    assert _argvs(rig) == [*before_down(rig.layout.repo_host_path), DOWN, NET_INSPECT, NET_CREATE, NET_INSPECT]
    assert not [step for step in rig.docker.steps if step.builds or is_dev_up_wrapper(step)]


def test_a_network_that_exists_is_never_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An existing devnet is used as it is (compose declares it external), whatever created it."""
    rig, _ = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    assert NET_CREATE not in _argvs(rig) and _argvs(rig).count(NET_INSPECT) == 1


def test_a_failed_up_answers_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.up_exit = 1
    result = _call(rig)
    assert (result['status'], result['message'], result['exit_status']) == ('failed', UP_FAILED, 1), result
    assert _argvs(rig) == full_run(rig.layout.repo_host_path)


@pytest.mark.parametrize('failing', range(len(stack.BASE_IMAGES)))
def test_a_failed_base_pull_stops_nothing_and_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: int
):
    """17:33 gate ruling F-A: an absent base whose pull fails is found BEFORE anything is ended.

    The call stops after the digests and the base steps: no down (the project was not stopped), no
    network step, no up. BasePullFailed's 'nothing was built' is then the whole truth, and the call
    was admitted, so it is charged once.
    """
    rig, responder = _rig(tmp_path, monkeypatch)
    ref = stack.BASE_IMAGES[failing]

    def respond(step: stack.Step) -> runner.ProcessResult:
        if step.argv in (image_inspect(ref), (DOCKER, 'pull', ref)):
            return runner.ProcessResult(1, b'', b'pull access denied')
        return responder(step)

    rig.docker.respond = respond
    result = _call(rig)
    assert result['status'] == 'failed', result
    assert result['message'] == (
        f'up stopped: base image {ref} is not on the daemon and could not be pulled; nothing was built'
    )
    host = rig.layout.repo_host_path
    assert _argvs(rig) == [*before_bases(host), *base_inspects()[:failing], image_inspect(ref), (DOCKER, 'pull', ref)]
    assert DOWN not in _argvs(rig), 'a failed pull stopped the project'
    assert not [argv for argv in _argvs(rig) if argv[1] == 'network']
    assert not [step for step in rig.docker.steps if is_dev_up_wrapper(step) or step.builds]
    assert _charges_left(rig) == BUDGET - 1


def test_an_absent_base_is_pulled_before_the_down_and_never_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The pull that succeeds also runs before the down; the up's backstop ensure then runs no step."""
    rig, responder = _rig(tmp_path, monkeypatch)
    ref = stack.BASE_IMAGES[0]

    def respond(step: stack.Step) -> runner.ProcessResult:
        if step.argv == image_inspect(ref):
            return runner.ProcessResult(1, b'', b'Error: No such image')
        return responder(step)

    rig.docker.respond = respond
    assert _call(rig)['status'] == 'ok'
    host = rig.layout.repo_host_path
    assert _argvs(rig) == [
        *before_bases(host),
        image_inspect(ref),
        (DOCKER, 'pull', ref),
        *base_inspects()[1:],
        DOWN,
        NET_INSPECT,
        up_argv(host),
    ]


# ===============================================================================================
# THE WRAPPER: six fixed words, a constant script, the compose argv as positional arguments
# ===============================================================================================


def test_the_wrapper_script_is_the_pinned_constant():
    assert stack.DEV_UP_WRAPPER_SCRIPT == WRAPPER_SCRIPT


def test_the_wrapper_head_is_the_six_fixed_words(tmp_path: Path):
    host = Path('/home/someone/repos/trader_joe')
    assert stack.dev_up_wrapper_head(host) == wrapper_head(host)
    assert Path(HELPER) == stack.TRUSTED_DEV_INSTANCE_SECRET


def test_the_up_step_builder_spells_the_wrapped_up_and_flags_it(tmp_path: Path):
    host = Path('/home/someone/repos/trader_joe')
    step = stack.dev_up_step(tmp_path, host, [STORE_DIGEST, INGEST_DIGEST])
    assert step.argv == up_argv(host)
    assert step.cwd == tmp_path and step.builds is True and step.env == UP_ENV


@pytest.mark.parametrize('host', ['/home/someone/repos/trader_joe', '/srv/other checkout/with "quotes" $(x)'])
def test_no_word_of_the_compose_argv_is_ever_script_text(tmp_path: Path, host: str):
    """F9: the script word is the same constant for any host path; the argv only follows it, as arguments."""
    step = stack.dev_up_step(tmp_path, Path(host), [STORE_DIGEST, INGEST_DIGEST])
    assert step.argv[2] == WRAPPER_SCRIPT
    assert host not in step.argv[2] and 'compose' not in step.argv[2] and 'docker' not in step.argv[2]
    assert step.argv[6:] == (*file_prefix(Path(host)), *UP_TAIL)


@pytest.mark.parametrize(
    'digests',
    [[], [STORE_DIGEST], [STORE_DIGEST, INGEST_DIGEST, STORE_DIGEST], [STORE_DIGEST, 'abc'], [STORE_DIGEST, 'A' * 64]],
    ids=['none', 'one', 'three', 'short', 'uppercase'],
)
def test_the_up_step_builder_refuses_anything_but_two_valid_digests(tmp_path: Path, digests: list[str]):
    with pytest.raises(stack.Refused):
        stack.dev_up_step(tmp_path, Path('/h'), digests)


def test_the_up_step_builder_runs_its_argv_through_the_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[str, ...]] = []
    real = stack.check_dev_up_argv

    def spy(argv, repo_host_path):
        seen.append(tuple(argv))
        real(argv, repo_host_path)

    monkeypatch.setattr(stack, 'check_dev_up_argv', spy)
    host = Path('/h/repo')
    step = stack.dev_up_step(tmp_path, host, [STORE_DIGEST, INGEST_DIGEST])
    assert seen == [step.argv]


def test_the_check_runs_the_lifecycle_check_on_exactly_the_words_after_the_six(monkeypatch: pytest.MonkeyPatch):
    """F9 (14:59): check_dev_lifecycle_argv runs on compose_argv, the remainder, as built."""
    seen: list[tuple[str, ...]] = []
    real = stack.check_dev_lifecycle_argv

    def spy(argv, repo_host_path=None):
        seen.append(tuple(argv))
        real(argv, repo_host_path)

    monkeypatch.setattr(stack, 'check_dev_lifecycle_argv', spy)
    host = Path('/h/repo')
    stack.check_dev_up_argv(up_argv(host), host)
    assert seen == [(*file_prefix(host), *UP_TAIL)]


def test_the_real_argv_is_accepted():
    host = Path('/h/repo')
    stack.check_dev_up_argv(up_argv(host), host)


def _with(argv: tuple[str, ...], index: int, word: str) -> tuple[str, ...]:
    return (*argv[:index], word, *argv[index + 1 :])


def _host() -> Path:
    return Path('/h/repo')


_HEAD = len(wrapper_head(_host()))
_GOOD = up_argv(_host())
_FORBIDDEN_MESSAGE = (
    'a dev lifecycle command removes no volume, image or orphan and names no compose file, project or env file'
)
_TABLE_MESSAGE = 'a dev lifecycle command is one of its fixed commands, with its fixed arguments, and no other'
_HEAD_MESSAGE = 'the dev up wrapper is its six fixed words and nothing else'
_ROW_MESSAGE = 'the dev up wrapper runs the fixed up command, and no other'
_PREFIX_MESSAGE = 'a file-based dev lifecycle command names exactly the trusted dev compose files'

# name -> (argv, the refusal message it must start with)
_REFUSED = {
    'a forbidden word appended: --remove-orphans': (
        (*_GOOD, '--remove-orphans'),
        f'{_FORBIDDEN_MESSAGE}: --remove-orphans',
    ),
    'a forbidden word appended: -v': ((*_GOOD, '-v'), f'{_FORBIDDEN_MESSAGE}: -v'),
    'a forbidden word: -V renew anon volumes': ((*_GOOD[:-4], '-V', *_GOOD[-4:]), f'{_FORBIDDEN_MESSAGE}: -V'),
    'a forbidden word: a second -p': ((*_GOOD, '-p', 'other'), f'{_FORBIDDEN_MESSAGE}: -p'),
    'a forbidden word: --env-file=': ((*_GOOD, '--env-file=/x'), f'{_FORBIDDEN_MESSAGE}: --env-file=/x'),
    'an extra word: --build': ((*_GOOD[: -len(UP_TAIL) + 1], '--build', *_GOOD[-len(UP_TAIL) + 1 :]), _TABLE_MESSAGE),
    'an extra service: pgadmin': ((*_GOOD, 'pgadmin'), _TABLE_MESSAGE),
    'postgres missing': (tuple(word for word in _GOOD if word != 'postgres'), _TABLE_MESSAGE),
    '--no-deps present': ((*_GOOD[:-4], '--no-deps', *_GOOD[-4:]), _TABLE_MESSAGE),
    'a different wait timeout': (tuple('30' if word == '300' else word for word in _GOOD), _TABLE_MESSAGE),
    'the build row under the wrapper': (
        (*wrapper_head(_host()), *file_prefix(_host()), 'build', 'data_store', 'data_ingest', 'web'),
        _ROW_MESSAGE,
    ),
    'the label-only down under the wrapper': ((*wrapper_head(_host()), *DOWN), _ROW_MESSAGE),
    'the migrate row under the wrapper': (
        (
            *wrapper_head(_host()),
            *file_prefix(_host()),
            'run',
            '--rm',
            '--no-deps',
            'data_store',
            '/code/.venv/bin/alembic',
            'upgrade',
            'head',
        ),
        _ROW_MESSAGE,
    ),
    'another project directory': (_with(_GOOD, _HEAD + 5, '/elsewhere'), _PREFIX_MESSAGE),
    "a checkout's compose file": (_with(_GOOD, _HEAD + 7, '/h/repo/docker-compose.yaml'), _PREFIX_MESSAGE),
    'another project name': (_with(_GOOD, _HEAD + 3, 'trader_joe_agent_stack'), 'a dev lifecycle verb runs only'),
    'not docker after the head': (_with(_GOOD, _HEAD, '/bin/sh'), 'a dev lifecycle verb runs only'),
    'an empty remainder': (wrapper_head(_host()), 'a dev lifecycle verb runs only'),
    'the argv spliced into the script': (
        _with(_GOOD, 2, WRAPPER_SCRIPT + ' ' + shlex.join(_GOOD[_HEAD:])),
        _HEAD_MESSAGE,
    ),
    'a script that runs its own command': (_with(_GOOD, 2, 'exec "$@"; rm -rf /'), _HEAD_MESSAGE),
    'bash, not sh': (_with(_GOOD, 0, 'bash'), _HEAD_MESSAGE),
    '/bin/sh by path': (_with(_GOOD, 0, '/bin/sh'), _HEAD_MESSAGE),
    'no $0 word': ((*_GOOD[:3], *_GOOD[4:]), _HEAD_MESSAGE),
    "the checkout's helper": (_with(_GOOD, 4, '/h/repo/tools/dev_instance_secret.sh'), _HEAD_MESSAGE),
    'another env file': (_with(_GOOD, 5, '/h/repo/server/data/store/.env'), _HEAD_MESSAGE),
    'another host for the env file': (_with(_GOOD, 5, '/elsewhere/.env'), _HEAD_MESSAGE),
    'no wrapper at all': (_GOOD[_HEAD:], _HEAD_MESSAGE),
}


@pytest.mark.parametrize(('argv', 'message'), list(_REFUSED.values()), ids=list(_REFUSED))
def test_check_dev_up_argv_refuses_every_other_argv(argv: tuple[str, ...], message: str):
    with pytest.raises(stack.Refused) as refused:
        stack.check_dev_up_argv(argv, _host())
    assert str(refused.value).startswith(message), str(refused.value)


# ===============================================================================================
# THE SECRET: the REAL wrapper under sh, the REAL helper, a FAKE env file and a fake docker
# ===============================================================================================

FAKE_DOCKER = """#!/bin/sh
here="$(dirname "$0")"
printf '%s\\n' "$@" > "$here/argv"
env > "$here/env"
echo "fake compose: up"
echo "fake compose: progress" >&2
"""


def _secret_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper: Path | None = None) -> tuple[Rig, Path]:
    """A rig whose up step really runs: the wrapper argv as built, with the helper and docker words swapped.

    Word 4 (the baked helper) becomes the repository's tools/dev_instance_secret.sh, the file the S6
    check holds the baked copy equal to; word 6 (docker) becomes a fake that records its argv and env.
    Every other step goes to the fake daemon.
    """
    rig, _ = _rig(tmp_path, monkeypatch)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', REAL_CREATE_SUBPROCESS_EXEC)
    fakebin = tmp_path / 'fakebin'
    fakebin.mkdir()
    fake = fakebin / 'docker'
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    real_helper = helper or REPO_ROOT / 'tools' / 'dev_instance_secret.sh'

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


def test_the_child_compose_gets_the_derived_secret_and_the_server_keeps_none_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, fakebin = _secret_rig(tmp_path, monkeypatch)
    _write_root_env(rig, f'POSTGRES_PASS=fake-pg\nINSTANCE_WRITE_SECRET={FAKE_SECRET}\n')
    result = _call(rig)
    assert result['status'] == 'ok', result
    child = _child_env(fakebin)
    assert child['INSTANCE_WRITE_SECRET'] == FAKE_SECRET
    # The rest of the child's env is the runner's fixed one plus the up step's own, nothing else.
    expected = {**runner.DOCKER_ENV, **dict(UP_ENV), 'INSTANCE_WRITE_SECRET': FAKE_SECRET}
    assert {key: value for key, value in child.items() if key not in ('PWD', 'SHLVL', '_')} == expected
    # compose received exactly the file-based up, as positional arguments.
    host = rig.layout.repo_host_path
    assert (fakebin / 'argv').read_text(encoding='utf-8').splitlines() == [*file_prefix(host), *UP_TAIL][1:]
    # The value is in no command, no output, no audit line, and not in this process's environment.
    kept = _everything_the_server_kept(rig, result)
    assert FAKE_SECRET not in kept
    assert 'fake compose: up' in result['steps'][-1]['stdout']['text'], 'the output was recorded, without the value'
    assert FAKE_SECRET not in os.environ.values()


def test_an_empty_secret_gives_unused_as_the_helper_does_and_the_up_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """As make dev-up does: reads work, data_store refuses writes. Not a refusal (see the 17:02 FALLBACK)."""
    rig, fakebin = _secret_rig(tmp_path, monkeypatch)
    _write_root_env(rig, 'INSTANCE_WRITE_SECRET=\n')
    assert _call(rig)['status'] == 'ok'
    assert _child_env(fakebin)['INSTANCE_WRITE_SECRET'] == 'unused'


def test_no_root_env_file_gives_unused_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, fakebin = _secret_rig(tmp_path, monkeypatch)
    assert not (rig.layout.repo / '.env').exists()
    assert _call(rig)['status'] == 'ok'
    assert _child_env(fakebin)['INSTANCE_WRITE_SECRET'] == 'unused'


def test_an_inherited_secret_never_reaches_the_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The helper prefers its own environment's value; the runner's fixed env means that is never the server's."""
    monkeypatch.setenv('INSTANCE_WRITE_SECRET', 'inherited-not-wanted')
    rig, fakebin = _secret_rig(tmp_path, monkeypatch)
    _write_root_env(rig, f'INSTANCE_WRITE_SECRET={FAKE_SECRET}\n')
    assert _call(rig)['status'] == 'ok'
    assert _child_env(fakebin)['INSTANCE_WRITE_SECRET'] == FAKE_SECRET


def test_a_value_full_of_shell_is_passed_literally_and_never_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, fakebin = _secret_rig(tmp_path, monkeypatch)
    marker = tmp_path / 'ran'
    value = f'a b $(touch {marker}) "q"; `touch {marker}` \\$x'
    _write_root_env(rig, f"INSTANCE_WRITE_SECRET='{value}'\n")
    result = _call(rig)
    assert result['status'] == 'ok', result
    assert _child_env(fakebin)['INSTANCE_WRITE_SECRET'] == value
    assert not marker.exists(), 'a word of the value was run'
    assert value not in _everything_the_server_kept(rig, result)


def test_a_failing_helper_stops_the_wrapper_before_compose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`|| exit 1`: no secret, no compose. The project was already down; the call says the up failed."""
    rig, fakebin = _secret_rig(tmp_path, monkeypatch, helper=tmp_path / 'no-such-helper.sh')
    _write_root_env(rig, f'INSTANCE_WRITE_SECRET={FAKE_SECRET}\n')
    result = _call(rig)
    assert (result['status'], result['message'], result['exit_status']) == ('failed', UP_FAILED, 1), result
    assert not (fakebin / 'argv').exists(), 'compose ran without the secret'
    assert DOWN in [step.argv for step in rig.docker.steps], 'the down ran before the up, as designed'
    assert FAKE_SECRET not in _everything_the_server_kept(rig, result)


# ===============================================================================================
# ONE VERB AT A TIME
# ===============================================================================================


def test_a_second_dev_up_while_one_runs_is_told_busy_and_runs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    release = asyncio.Event()
    entered = asyncio.Event()

    async def hold(step: stack.Step) -> None:
        if is_dev_up_wrapper(step):
            entered.set()
            await release.wait()

    rig, _ = _rig(tmp_path, monkeypatch)
    rig.docker.hook = hold

    async def scenario() -> tuple[dict, dict, dict]:
        first = asyncio.create_task(rig.agent.call('up', {}))
        await asyncio.wait_for(entered.wait(), 10)
        again = await rig.agent.call('up', {})
        down = await rig.agent.call('down', {})
        release.set()
        return await first, again, down

    first, again, down = asyncio.run(scenario())
    assert first['status'] == 'ok', first
    for other in (again, down):
        assert other['status'] == 'busy' and 'up is running' in other['message'], other
        assert other['steps'] == []
    assert _argvs(rig).count(DOWN) == 1 and _argvs(rig).count(GUARD) == 1
    assert _charges_left(rig) == BUDGET - 1


# ===============================================================================================
# THE SURFACE: schema, timeout, verb table, description
# ===============================================================================================


def test_the_schema_is_closed_and_takes_nothing():
    assert runner.VERB_SCHEMAS['up'] == {
        'type': 'object',
        'properties': {},
        'required': [],
        'additionalProperties': False,
    }


@pytest.mark.parametrize(
    'arguments', [{'service': 'web'}, {'services': ['postgres']}, {'no_deps': True}, ['up'], 'up'], ids=repr
)
def test_any_argument_is_refused_with_no_step_and_no_charge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert _call(rig, arguments)['status'] == 'refused'
    assert rig.docker.steps == [] and _charges_left(rig) == BUDGET


def test_the_timeout_is_ten_minutes_inside_the_clients():
    assert runner.VERB_TIMEOUT_SECONDS['up'] == 600
    config = json.loads((REPO_ROOT / '.mcp.json').read_text(encoding='utf-8'))
    assert config['mcpServers']['agent-stack']['timeout'] >= (600 + 30) * 1000


def test_dev_up_is_on_the_tool_list_saying_what_it_stops_and_that_it_never_builds():
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type] -- only the verb table is read
    assert 'up' in agent.verbs
    text = agent.describe('up')
    assert 'STATE-CHANGING' in text and 'docker compose -p trader_joe_dev down' in text, text
    assert 'never builds' in text and 'call build' in text, text
    assert 'never removes a volume or an image' in text, text


# ===============================================================================================
# MIRRORS: make dev-up and make dev-network
# ===============================================================================================


def _recipe(target: str) -> tuple[list[str], list[str]]:
    """(prerequisites, recipe lines) of one Makefile target, continuation lines joined."""
    text = (REPO_ROOT / 'Makefile').read_text(encoding='utf-8').replace('\\\n', ' ')
    match = re.search(rf'^{re.escape(target)}:([^\n]*)\n((?:\t[^\n]*\n)+)', text, re.MULTILINE)
    assert match is not None, f'the Makefile has no {target} target'
    prerequisites = match.group(1).split('##', 1)[0].split()
    return prerequisites, [line[1:].strip() for line in match.group(2).splitlines()]


def test_the_up_is_make_dev_ups_up_line_exactly():
    """MIRROR (b): make dev-up's up line, flags and service list, parsed from the Makefile, not restated."""
    _, lines = _recipe('dev-up')
    assert len(lines) == 2, lines
    words = shlex.split(lines[1])
    assert words[0] == '$(DEV_COMPOSE)', words
    assert tuple(words[1:]) == UP_TAIL == stack.DEV_UP_TAIL
    # up's compose files are make's DEV_COMPOSE list plus the MCP-only overlay, last.
    make_files = [
        value for flag, value in itertools.pairwise(shlex.split(_make_variable('DEV_COMPOSE'))) if flag == '-f'
    ]
    assert (*make_files, 'docker-compose.agent-dev.yaml') == DEV_FILES


def test_both_downs_remove_no_volume_image_or_orphan_and_name_the_ups_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """MIRROR (a), not word for word: no -v/--volumes/--rmi/--remove-orphans, and the up's own project.

    make: the down goes through $(TOOLS_COMPOSE), which is $(DEV_COMPOSE) plus one -f and no project
    option of its own, so both lines name DEV_COMPOSE's one project: -p $(DEV_PROJECT), trader_joe_dev
    since tj-grna9p.161 (ADR tj-4rr0la addendum 29). The MCP: -p trader_joe_dev on both since
    tj-iee6sn.31, so make's down and the MCP's down stop the SAME project, read from the Makefile.
    """
    _, lines = _recipe('dev-up')
    down = shlex.split(lines[0])
    assert down[-2:] == ['$(TOOLS_COMPOSE)', 'down'], down
    assert all(re.fullmatch(r'[A-Z_]+=\S*', word) for word in down[:-2]), down
    destructive = {'-v', '--volumes', '--rmi', '--remove-orphans'}
    tools = shlex.split(_make_variable('TOOLS_COMPOSE'))
    assert tools[0] == '$(DEV_COMPOSE)' and tools[1:] == ['-f', 'docker-compose.tools.yaml'], tools
    dev_compose = shlex.split(_make_variable('DEV_COMPOSE'))
    project_options = {'-p', '--project-name', '--project-directory'}
    for words in (down, tools, dev_compose):
        assert not destructive & set(words), words
    for words in (down, tools):
        assert not project_options & set(words), words
    assert [word for word in dev_compose if word.partition('=')[0] in project_options] == ['-p'], dev_compose
    assert dev_compose[dev_compose.index('-p') + 1] == '$(DEV_PROJECT)', dev_compose
    assert _make_variable('DEV_PROJECT') == MAKEFILE_DEV_PROJECT
    rig, _ = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    compose_down = [argv for argv in _argvs(rig) if argv[:2] == (DOCKER, 'compose')]
    assert compose_down == [DOWN]
    up = dev_up_compose_argv(rig.docker.steps[-1])
    for argv in (DOWN, up):
        assert argv[2:4] == ('-p', PROJECT) and argv.count('-p') == 1, argv
        assert not destructive & set(argv), argv
    # One project on both sides (tj-iee6sn.31): the MCP's argv word equals make's own DEV_PROJECT.
    assert PROJECT == MAKEFILE_DEV_PROJECT == _make_variable('DEV_PROJECT')


def test_the_network_steps_mirror_make_dev_network():
    """dev-up depends on dev-network: inspect, else create --driver bridge, else inspect, of DEV_NETWORK."""
    prerequisites, _ = _recipe('dev-up')
    assert 'dev-network' in prerequisites, prerequisites
    _, lines = _recipe('dev-network')
    chain = ' '.join(lines)
    network = _make_variable('DEV_NETWORK')
    assert network == NETWORK
    assert 'docker network create --driver bridge $(DEV_NETWORK)' in chain, chain
    assert chain.count('docker network inspect $(DEV_NETWORK)') == 2, chain
    assert stack.dev_network_argv(True) == NET_CREATE and stack.dev_network_argv(False) == NET_INSPECT


@pytest.mark.parametrize(
    'argv',
    [
        (DOCKER, 'network', 'rm', NETWORK),
        (DOCKER, 'network', 'create', NETWORK),
        (DOCKER, 'network', 'inspect', NETWORK),
        (*NET_CREATE, '--internal'),
        (DOCKER, 'network', 'create', '--driver', 'bridge', 'other'),
    ],
    ids=shlex.join,
)
def test_the_network_check_refuses_anything_but_the_two_commands(argv: tuple[str, ...]):
    with pytest.raises(stack.Refused):
        stack.check_dev_network_argv(argv)


def test_the_network_builder_checks_what_it_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Each built argv passes through check_dev_network_argv (its accepted set is the two literals above)."""
    seen: list[tuple[str, ...]] = []
    real = stack.check_dev_network_argv

    def spy(argv):
        seen.append(tuple(argv))
        real(argv)

    monkeypatch.setattr(stack, 'check_dev_network_argv', spy)
    inspect, create = stack.dev_network_steps(tmp_path)
    assert (inspect.argv, create.argv) == (NET_INSPECT, NET_CREATE)
    assert seen == [NET_INSPECT, NET_CREATE]
    assert inspect.cwd == create.cwd == tmp_path and not inspect.builds and not create.builds


def test_the_image_steps_inspect_exactly_the_three_dev_images(tmp_path: Path):
    steps = stack.dev_image_steps(tmp_path)
    assert [step.argv for step in steps] == [image_inspect(ref) for ref in DEV_IMAGES]
    assert all(step.cwd == tmp_path and not step.builds and step.env == () for step in steps)
