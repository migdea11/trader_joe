"""migrate and migrate_status: ONE `make migrate` and ONE status, on whichever mode of the one project is up.

Design checked against: bead tj-iee6sn.10's 20:10 REWORK 2 note and its 20:50 amendment (ADR tj-4rr0la
addendum 27) and 10:00 note (use container_stamp_*), the owner's ruling on tj-iee6sn.1 (ONE migrate, make
migrate's semantics, on whichever mode is up), ADR tj-4rr0la addenda 26-27, and
server/data/store/run_migrations.sh with the Makefile's `migrate` recipe, whose semantics migrate mirrors.
dev_migrate, dev_migrate_check, dev_migrate_status and migrate_check are REMOVED; so are the agent-stack
alembic paths. Both verbs are PLAIN calls.

migrate, a refusal at any point running nothing after it and charging nothing:

    the mode guard FIRST (one `docker ps`; DEV or TEST; empty, PROD and mixed refuse) -> S6 stale copies
    -> D6 on the MODE's bind sources -> D5 env-file preflight -> the empty-versions guard (no step) -> the
    postgres lookup (exactly one) -> the data_store lookup (several refuse, none passes, as select_stack)
    -> `docker image inspect` of the mode's data_store image (:dev 'call build' / :test 'call
    test_build') -> TEST ONLY, the stamp gate: the data_ingest lookup, then -- when data_store or
    data_ingest runs -- the two trusted digests (a failure answers failed, uncharged) and one container
    stamp read per RUNNING service (stale, unstamped or unreadable: 'stale test stack: call test_build,
    then test_up') -> ONE budget charge -> the bases (the one-off `run` of data_store is flagged as
    building) -> the one-off `run --rm --no-deps data_store /code/.venv/bin/alembic upgrade head`:
        DEV   through up's fixed `sh -c` secret wrapper over the dev files, dev_pinned_step_env()
        TEST  UNWRAPPED over base + docker-compose.fake.yaml, the pinned env plus DATA_DIR=./volume_data/test

migrate_status: the mode guard (DEV or TEST), the data_store lookup, ONE exec of alembic current and
history in it. It never charges the budget and never runs upgrade.

Every verb call goes through the REAL AgentStack over the fake-docker rig. The secret tests run the REAL
wrapper under sh with the REAL tools/dev_instance_secret.sh, a FAKE env file and a fake docker. Expected
values are literals, or parsed from run_migrations.sh, the Makefile and the compose files.
"""

import asyncio
import inspect
import json
import os
import re
import shlex
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from common.tests.test_ci_invariants import _made_variable
from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import (
    REPO_ROOT,
    FakeDocker,
    Rig,
    default_response,
    is_dev_digest,
    is_dev_up_wrapper,
    make_rig,
)
from tools.agent_mcp.tests.test_dev_label_verbs import (
    BUDGET_REFUSAL,
    DEV_LINE,
    GUARD,
    GUARD_FAILED,
    GUARD_REFUSAL,
    MIXED_REFUSAL,
    PROD_LINE,
    TEST_LINE,
    symlink_refusal,
)
from tools.agent_mcp.tests.test_dev_up import (
    DEV_FILES,
    FAKE_DOCKER,
    FAKE_SECRET,
    PINNED_ENV,
    WRAPPER_SCRIPT,
    digest_argv,
    file_prefix,
    image_inspect,
    wrapper_head,
)


pytestmark = pytest.mark.build_infra

# Captured at import, before any rig forbids real subprocesses: the secret tests run the real wrapper.
REAL_CREATE_SUBPROCESS_EXEC = asyncio.create_subprocess_exec

DOCKER = '/usr/local/bin/docker'
PROJECT = 'trader_joe_dev'
TRUSTED = '/opt/agent_mcp/compose'
TEST_FILES = ('docker-compose.yaml', 'docker-compose.fake.yaml')
DEV_STORE_IMAGE = 'trader_joe_data_store:dev'
TEST_STORE_IMAGE = 'trader_joe_data_store:test'
ALEMBIC = '/code/.venv/bin/alembic'
MIGRATE_TAIL = ('run', '--rm', '--no-deps', 'data_store', ALEMBIC, 'upgrade', 'head')
STAMP_FORMAT = '{{ with .Config.Labels }}{{ index . "trader_joe.source.digest" }}{{ end }}'
STORE_DIGEST = '1' * 64
INGEST_DIGEST = '2' * 64
OTHER_DIGEST = 'e' * 64
POSTGRES_ID = 'aaaaaaaaaaa1'
STORE_ID = 'bbbbbbbbbbb2'
INGEST_ID = 'ccccccccccc3'
TEST_DATA_DIR = './volume_data/test'
VERSIONS = 'server/data/store/migrations/versions'
BUDGET = 12
STALE_PREFIX = 'the MCP image is stale: '
MODES = ('DEV', 'TEST')
LINES = {'DEV': DEV_LINE, 'TEST': TEST_LINE}

# The selection line each result starts with (owner ruling: 'prints which one it used', as make migrate does).
SELECTION = {
    'DEV': (
        'using the DEV definition (docker-compose.yaml + docker-compose.override.yaml) of project trader_joe_dev '
        'in DEV mode (revisions from the root checkout)'
    ),
    'TEST': (
        'using the TEST definition (docker-compose.yaml + docker-compose.fake.yaml) of project trader_joe_dev '
        'in TEST mode (the :test image; alembic.ini and revisions from the root checkout)'
    ),
}
NOTHING_UP = 'nothing is up: call up (your dev data) or test_up first'
EMPTY_VERSIONS = f'no revision files in {VERSIONS}: wrong checkout?'
# 183f296: the hint is mode-neutral (it used to say 'start the dev stack first', wrong under TEST).
NEUTRAL_HINT = 'start the project first: up (your dev data) or test_up (test mode)'
NO_POSTGRES = f'no running postgres container in the trader_joe_dev project: {NEUTRAL_HINT}'
# The stamp read of the :test data_store IMAGE, the image the TEST one-off runs (d6b942f).
IMAGE_STAMP_READ = (DOCKER, 'image', 'inspect', '--format', STAMP_FORMAT, TEST_STORE_IMAGE)
LOOKUP_FAILED = 'the container lookup failed (exit 1); see the steps'
IMAGE_REFUSAL = {
    'DEV': f'the image {DEV_STORE_IMAGE} is not on the daemon: call build first',
    'TEST': f'the image {TEST_STORE_IMAGE} is not on the daemon: call test_build first',
}
STALE_FIX = 'stale test stack: call test_build, then test_up'
DIGEST_FAILED = 'the source digest failed (data store); nothing was migrated'


def lookup(service: str) -> tuple[str, ...]:
    return (
        DOCKER,
        'ps',
        '--filter',
        'status=running',
        '--filter',
        f'label=com.docker.compose.project={PROJECT}',
        '--filter',
        f'label=com.docker.compose.service={service}',
        '--filter',
        'label=com.docker.compose.oneoff=False',
        '--format',
        '{{.ID}}',
    )


def container_stamp(container_id: str) -> tuple[str, ...]:
    return (DOCKER, 'inspect', '--type', 'container', '--format', STAMP_FORMAT, container_id)


def fakeset_prefix(host: Path) -> tuple[str, ...]:
    files = [word for name in TEST_FILES for word in ('-f', f'{TRUSTED}/{name}')]
    return (DOCKER, 'compose', '-p', PROJECT, '--project-directory', str(host), *files)


def dev_oneoff(host: Path) -> tuple[str, ...]:
    return (*wrapper_head(host), *file_prefix(host), *MIGRATE_TAIL)


def fakeset_oneoff(host: Path) -> tuple[str, ...]:
    return (*fakeset_prefix(host), *MIGRATE_TAIL)


ONEOFF = {'DEV': dev_oneoff, 'TEST': fakeset_oneoff}


def base_inspects() -> list[tuple[str, ...]]:
    return [image_inspect(ref) for ref in stack.BASE_IMAGES]


def admission(mode: str, host: Path, running: tuple[str, ...] = ('data_store', 'data_ingest')) -> list[tuple[str, ...]]:
    """Every step a migrate call runs ahead of the budget charge, in order."""
    steps = [GUARD, lookup('postgres'), lookup('data_store')]
    if mode == 'DEV':
        return [*steps, image_inspect(DEV_STORE_IMAGE)]
    # TEST (d6b942f): the two digests ALWAYS, the :test data_store IMAGE's stamp ALWAYS (the image the one-off
    # runs), then the data_ingest lookup and one container stamp per RUNNING service.
    steps += [
        image_inspect(TEST_STORE_IMAGE),
        digest_argv(host, 'store'),
        digest_argv(host, 'ingest'),
        IMAGE_STAMP_READ,
        lookup('data_ingest'),
    ]
    steps += [container_stamp({'data_store': STORE_ID, 'data_ingest': INGEST_ID}[name]) for name in running]
    return steps


def full_run(mode: str, host: Path, running: tuple[str, ...] = ('data_store', 'data_ingest')) -> list[tuple[str, ...]]:
    return [*admission(mode, host, running), *base_inspects(), ONEOFF[mode](host)]


def status_run() -> list[tuple[str, ...]]:
    return [
        GUARD,
        lookup('data_store'),
        (DOCKER, 'exec', '-w', '/code', STORE_ID, 'sh', '-c', stack.DEV_MIGRATE_STATUS_SCRIPT),
    ]


class Responder:
    """The fake daemon of a migrate call: each step's answer is a field the test can change."""

    def __init__(self, mode: str = 'DEV') -> None:
        self.guard: tuple[int, bytes] = (0, LINES[mode])
        self.lookups: dict[str, tuple[int, bytes]] = {
            'postgres': (0, f'{POSTGRES_ID}\n'.encode()),
            'data_store': (0, f'{STORE_ID}\n'.encode()),
            'data_ingest': (0, f'{INGEST_ID}\n'.encode()),
        }
        self.images: dict[str, int] = {DEV_STORE_IMAGE: 0, TEST_STORE_IMAGE: 0}
        self.digests: dict[str, tuple[int | None, bytes]] = {
            'store': (0, f'{STORE_DIGEST}\n'.encode()),
            'ingest': (0, f'{INGEST_DIGEST}\n'.encode()),
        }
        self.stamps: dict[str, tuple[int | None, bytes]] = {
            STORE_ID: (0, f'{STORE_DIGEST}\n'.encode()),
            INGEST_ID: (0, f'{INGEST_DIGEST}\n'.encode()),
        }
        self.image_stamp: tuple[int | None, bytes] = (0, f'{STORE_DIGEST}\n'.encode())
        self.alembic: tuple[int, bytes, bytes] = (0, b'', b'')
        self.exec: tuple[int, bytes, bytes] = (0, b'== alembic current ==\nd7e2a9c35b18 (head)\n', b'')

    def __call__(self, step: stack.Step) -> runner.ProcessResult:
        argv = step.argv
        if argv == GUARD:
            return runner.ProcessResult(self.guard[0], self.guard[1], b'')
        for service, answer in self.lookups.items():
            if argv == lookup(service):
                return runner.ProcessResult(answer[0], answer[1], b'')
        for image, exit_status in self.images.items():
            if argv == image_inspect(image):
                return runner.ProcessResult(exit_status, b'' if exit_status else b'sha256:abc\n', b'')
        if is_dev_digest(step):
            answer = self.digests[argv[-1]]
            return runner.ProcessResult(answer[0], answer[1], b'')
        if argv == IMAGE_STAMP_READ:
            return runner.ProcessResult(self.image_stamp[0], self.image_stamp[1], b'')
        for container_id, answer in self.stamps.items():
            if argv == container_stamp(container_id):
                return runner.ProcessResult(answer[0], answer[1], b'')
        if is_dev_up_wrapper(step) or argv[-len(MIGRATE_TAIL) :] == MIGRATE_TAIL:
            return runner.ProcessResult(*self.alembic)
        if argv[1:2] == ('exec',):
            return runner.ProcessResult(*self.exec)
        return default_response(step)


def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str = 'DEV') -> tuple[Rig, Responder]:
    responder = Responder(mode)
    return make_rig(tmp_path, monkeypatch, FakeDocker(respond=responder)), responder


def _argvs(rig: Rig) -> list[tuple[str, ...]]:
    return [step.argv for step in rig.docker.steps]


def _alembic_steps(rig: Rig) -> list[stack.Step]:
    """Every step that ran alembic in any form: the one-off (wrapped or not) or an exec."""
    return [step for step in rig.docker.steps if ALEMBIC in step.argv or is_dev_up_wrapper(step) or 'exec' in step.argv]


def _charges_left(rig: Rig) -> int:
    """How many more charges the budget takes before refusing (it is consumed by asking)."""
    for taken in range(BUDGET + 1):
        try:
            rig.agent.dev_budget.charge()
        except stack.Refused:
            return taken
    raise AssertionError('the budget never refused')


# ===============================================================================================
# migrate: THE CALL THAT WORKS, in each mode
# ===============================================================================================


@pytest.mark.parametrize('mode', MODES)
def test_migrate_runs_every_step_in_the_designed_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str):
    rig, _ = _rig(tmp_path, monkeypatch, mode)
    result = rig.call('migrate', {})
    assert result['status'] == 'ok', result
    assert result['message'] == f'{SELECTION[mode]}; alembic upgrade head exited 0'
    assert result['details'] == {'mode': mode, 'selection': SELECTION[mode]}
    assert _argvs(rig) == full_run(mode, rig.layout.repo_host_path)
    assert all(step.cwd == rig.layout.stack_dir for step in rig.docker.steps), rig.docker.steps
    assert 'job' not in json.dumps(result).lower(), 'a plain call, never a job'
    assert _charges_left(rig) == BUDGET - 1, 'an admitted call is charged exactly once'


@pytest.mark.parametrize('mode', MODES)
def test_the_mode_guard_is_the_first_step_of_every_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str):
    for verb in ('migrate', 'migrate_status'):
        rig, _ = _rig(tmp_path / verb, monkeypatch, mode)
        assert rig.call(verb, {})['status'] == 'ok'
        assert _argvs(rig)[0] == GUARD and _argvs(rig).count(GUARD) == 1, (verb, _argvs(rig))


@pytest.mark.parametrize('mode', MODES)
def test_already_at_head_answers_ok_and_never_says_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str):
    """Gate finding (2) of the old dev_migrate: no 'applied' wording; alembic's own output is in the step record."""
    rig, responder = _rig(tmp_path, monkeypatch, mode)
    responder.alembic = (0, b'', b'INFO  [alembic.runtime.migration] Will assume transactional DDL.\n')
    result = rig.call('migrate', {})
    assert (result['status'], result['exit_status']) == ('ok', 0), result
    assert result['message'].endswith('; alembic upgrade head exited 0') and 'applied' not in result['message']
    assert 'transactional DDL' in result['steps'][-1]['stderr']['text']


@pytest.mark.parametrize('mode', MODES)
def test_no_data_store_container_proceeds_as_select_stack_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
):
    """select_stack: a postgres of the mode with NO data_store is still that mode. Only several data_store refuse."""
    rig, responder = _rig(tmp_path, monkeypatch, mode)
    responder.lookups['data_store'] = (0, b'')
    assert rig.call('migrate', {})['status'] == 'ok'
    expected = full_run(mode, rig.layout.repo_host_path, ('data_ingest',))
    assert _argvs(rig) == expected


def test_dev_runs_the_wrapped_one_off_under_the_pinned_env_and_no_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """DEV: the owner's DATA_DIR (their .env) is never pinned; dev_pinned_step_env() exactly."""
    rig, _ = _rig(tmp_path, monkeypatch, 'DEV')
    assert rig.call('migrate', {})['status'] == 'ok'
    alembic = [step for step in rig.docker.steps if is_dev_up_wrapper(step)]
    assert len(alembic) == 1 and alembic[0].argv == dev_oneoff(rig.layout.repo_host_path)
    assert alembic[0].env == PINNED_ENV == stack.dev_pinned_step_env()
    env = dict(alembic[0].env)
    assert 'DATA_DIR' not in env, 'DATA_DIR pinned in DEV: the owner database would move'
    assert not [name for name in env if name.startswith('SOURCE_DIGEST')], env
    assert 'INSTANCE_WRITE_SECRET' not in env and 'POSTGRES_PASS' not in env and '/dev/null' not in env.values()
    assert all(step.env == () for step in rig.docker.steps if step is not alembic[0]), rig.docker.steps


def test_test_runs_the_unwrapped_one_off_over_base_and_fake_with_data_dir_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """TEST: no secret wrapper, the base and fake files only, DATA_DIR=./volume_data/test after the pinned env."""
    rig, _ = _rig(tmp_path, monkeypatch, 'TEST')
    assert rig.call('migrate', {})['status'] == 'ok'
    assert not [step for step in rig.docker.steps if is_dev_up_wrapper(step)], 'the secret wrapper ran in TEST'
    oneoff = rig.docker.steps[-1]
    assert oneoff.argv == fakeset_oneoff(rig.layout.repo_host_path)
    assert oneoff.env == (*PINNED_ENV, ('DATA_DIR', TEST_DATA_DIR)) == stack.testmode_pinned_step_env()
    assert dict(oneoff.env)['DATA_DIR'] == TEST_DATA_DIR
    assert 'INSTANCE_WRITE_SECRET' not in dict(oneoff.env) and 'POSTGRES_PASS' not in dict(oneoff.env)
    assert all(step.env == () for step in rig.docker.steps[:-1]), rig.docker.steps


@pytest.mark.parametrize('mode', MODES)
def test_only_the_one_off_is_flagged_as_building_and_the_bases_come_just_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
):
    rig, _ = _rig(tmp_path, monkeypatch, mode)
    assert rig.call('migrate', {})['status'] == 'ok'
    host = rig.layout.repo_host_path
    assert [step.argv for step in rig.docker.steps if step.builds] == [ONEOFF[mode](host)]
    assert _argvs(rig)[-1 - len(stack.BASE_IMAGES) : -1] == base_inspects()


@pytest.mark.parametrize('mode', MODES)
def test_no_digest_and_no_secret_word_in_a_recorded_command_or_the_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
):
    rig, _ = _rig(tmp_path, monkeypatch, mode)
    result = rig.call('migrate', {})
    commands = [record['command'] for record in result['steps']]
    assert commands[-1] == shlex.join(ONEOFF[mode](rig.layout.repo_host_path))
    # The wrapper script names INSTANCE_WRITE_SECRET by design (DEV); its VALUE is the secret test's below.
    for word in ('SOURCE_DIGEST', 'COMPOSE_', 'DATA_DIR', STORE_DIGEST, INGEST_DIGEST):
        assert not [command for command in commands if word in command], (word, commands)
    audit = '\n'.join(rig.audit_lines())
    assert STORE_DIGEST not in audit and INGEST_DIGEST not in audit
    assert json.loads(rig.audit_lines()[-1])['arguments'] == {}


@pytest.mark.parametrize('mode', MODES)
def test_no_steering_walk_and_no_generated_code_check_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str):
    """Every -f is given and nothing label-only runs; nothing builds, so stale generated code does not refuse."""
    rig, _ = _rig(tmp_path, monkeypatch, mode)
    (rig.layout.stack_dir / 'compose.yaml').write_text('services: {}\n')
    shutil.rmtree(rig.layout.repo / 'gen' / 'proto' / 'ts')
    assert rig.call('migrate', {})['status'] == 'ok'


# ===============================================================================================
# migrate: THE ORDER -- each refusal runs only the steps before it, runs no alembic and charges nothing
# ===============================================================================================


def _empty(rig: Rig, responder: Responder) -> None:
    responder.guard = (0, b'')


def _prod(rig: Rig, responder: Responder) -> None:
    responder.guard = (0, PROD_LINE)


def _prod_beside(rig: Rig, responder: Responder) -> None:
    responder.guard = (0, responder.guard[1] + PROD_LINE)


def _mixed(rig: Rig, responder: Responder) -> None:
    responder.guard = (0, DEV_LINE + TEST_LINE)


def _guard_failed(rig: Rig, responder: Responder) -> None:
    responder.guard = (1, b'')


def _stale_copy(rig: Rig, responder: Responder) -> None:
    path = rig.layout.trusted_dir / 'tools' / 'source_digest.sh'
    path.write_bytes(path.read_bytes() + b'# drift\n')


def _stale_fake_copy(rig: Rig, responder: Responder) -> None:
    path = rig.layout.trusted_dir / 'docker-compose.fake.yaml'
    path.write_bytes(path.read_bytes() + b'# drift\n')


def _symlinked_migrations(rig: Rig, responder: Responder) -> None:
    """The one-off mounts migrations/ itself: a symlink there is the D6 case that matters most, in both modes."""
    repo = rig.layout.repo
    shutil.move(str(repo / 'server' / 'data' / 'store' / 'migrations'), str(rig.layout.root / 'moved-migrations'))
    (repo / 'server' / 'data' / 'store' / 'migrations').symlink_to(rig.layout.root / 'moved-migrations')


def _env_file_a_symlink(rig: Rig, responder: Responder) -> None:
    outside = rig.layout.root / 'elsewhere.env'
    outside.write_text('INSTANCE_WRITE_SECRET=planted\n')
    (rig.layout.repo / '.env').symlink_to(outside)


def _env_file_a_directory(rig: Rig, responder: Responder) -> None:
    (rig.layout.repo / 'server' / 'data' / 'store' / '.env').mkdir(parents=True)


def _no_revision_files(rig: Rig, responder: Responder) -> None:
    for path in (rig.layout.repo / VERSIONS).glob('*.py'):
        path.unlink()
    (rig.layout.repo / VERSIONS / 'README').write_text('not a revision\n')


def _no_versions_directory(rig: Rig, responder: Responder) -> None:
    shutil.rmtree(rig.layout.repo / VERSIONS)


def _no_postgres(rig: Rig, responder: Responder) -> None:
    responder.lookups['postgres'] = (0, b'')


def _two_postgres(rig: Rig, responder: Responder) -> None:
    responder.lookups['postgres'] = (0, f'{POSTGRES_ID}\n{STORE_ID}\n'.encode())


def _postgres_lookup_failed(rig: Rig, responder: Responder) -> None:
    responder.lookups['postgres'] = (1, b'')


def _two_data_store(rig: Rig, responder: Responder) -> None:
    responder.lookups['data_store'] = (0, f'{STORE_ID}\n{POSTGRES_ID}\n'.encode())


def _data_store_lookup_failed(rig: Rig, responder: Responder) -> None:
    responder.lookups['data_store'] = (1, b'')


def _missing_image(rig: Rig, responder: Responder) -> None:
    responder.images = {DEV_STORE_IMAGE: 1, TEST_STORE_IMAGE: 1}


def _spent_budget(rig: Rig, responder: Responder) -> None:
    for _ in range(BUDGET):
        rig.agent.dev_budget.charge()


# name -> (break it, the refusal (None: checked by prefix below), how many steps ran (None: all of admission))
REFUSALS = {
    'an empty project': (_empty, NOTHING_UP, 1),
    'a prod container': (_prod, GUARD_REFUSAL, 1),
    'a prod container beside the mode': (_prod_beside, GUARD_REFUSAL, 1),
    'dev and test together': (_mixed, MIXED_REFUSAL, 1),
    'the guard could not list': (_guard_failed, GUARD_FAILED, 1),
    'S6: a stale baked copy': (_stale_copy, None, 1),
    'D6: a symlinked migrations bind source': (
        _symlinked_migrations,
        symlink_refusal('server/data/store/migrations', 'server/data/store/migrations'),
        1,
    ),
    'D5: an env file that is a symlink': (_env_file_a_symlink, symlink_refusal('.env', '.env'), 1),
    'D5: an env file that is a directory': (
        _env_file_a_directory,
        'server/data/store/.env is not a regular file: remove it, or start the stack yourself',
        1,
    ),
    'no revision file in versions/': (_no_revision_files, EMPTY_VERSIONS, 1),
    'no versions/ directory': (_no_versions_directory, EMPTY_VERSIONS, 1),
    'no running postgres': (_no_postgres, NO_POSTGRES, 2),
    'two running postgres': (_two_postgres, None, 2),
    'the postgres lookup failed': (_postgres_lookup_failed, LOOKUP_FAILED, 2),
    'two running data_store': (
        _two_data_store,
        '2 running data_store containers in the trader_joe_dev project; cannot tell which stack',
        3,
    ),
    'the data_store lookup failed': (_data_store_lookup_failed, LOOKUP_FAILED, 3),
    'no data_store image of the mode': (_missing_image, None, 4),
    'the budget spent': (_spent_budget, BUDGET_REFUSAL, None),
}


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize(('breaks', 'message', 'ran'), list(REFUSALS.values()), ids=list(REFUSALS))
def test_each_refusal_runs_only_the_steps_before_it_no_alembic_and_charges_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, breaks, message: str | None, ran: int | None
):
    rig, responder = _rig(tmp_path, monkeypatch, mode)
    breaks(rig, responder)
    result = rig.call('migrate', {})
    assert result['status'] == 'refused', result
    if breaks is _stale_copy:
        assert result['message'].startswith(f'{STALE_PREFIX}tools/source_digest.sh differs'), result
    elif breaks is _two_postgres:
        assert result['message'].startswith('2 running postgres containers in the trader_joe_dev project'), result
    elif breaks is _missing_image:
        assert result['message'] == IMAGE_REFUSAL[mode], result
    else:
        assert result['message'] == message, result
    host = rig.layout.repo_host_path
    expected = admission(mode, host) if ran is None else full_run(mode, host)[:ran]
    assert _argvs(rig) == expected
    assert not _alembic_steps(rig) and not [step for step in rig.docker.steps if step.builds], 'alembic ran'
    assert 'details' not in result, 'a refusal carries no selection detail'
    if breaks is not _spent_budget:
        assert _charges_left(rig) == BUDGET, 'a refused call was charged'


def test_a_stale_baked_fake_file_refuses_test_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """S6 covers the TEST set's own file: the one-off would load yesterday's docker-compose.fake.yaml."""
    rig, responder = _rig(tmp_path, monkeypatch, 'TEST')
    _stale_fake_copy(rig, responder)
    result = rig.call('migrate', {})
    assert result['status'] == 'refused' and result['message'].startswith(STALE_PREFIX), result
    assert 'docker-compose.fake.yaml' in result['message'] and _argvs(rig) == [GUARD]


@pytest.mark.parametrize(
    ('mode', 'source', 'refused'),
    [('DEV', 'server/common', True), ('TEST', 'server/common', False), ('TEST', 'tests/fakes', True)],
    ids=['dev: server/common', 'test: server/common is not a test bind', 'test: tests/fakes'],
)
def test_d6_checks_the_bind_sources_of_the_mode_that_is_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, source: str, refused: bool
):
    rig, _ = _rig(tmp_path, monkeypatch, mode)
    target = rig.layout.repo / source
    target.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(rig.layout.root / 'moved'))
    target.symlink_to(rig.layout.root / 'moved', target_is_directory=True)
    result = rig.call('migrate', {})
    if refused:
        assert (result['status'], result['message']) == ('refused', symlink_refusal(source, source)), result
        assert _argvs(rig) == [GUARD]
    else:
        assert result['status'] == 'ok', result


# Two things broken at once: the EARLIER check of the designed order must be the one that answers.
PAIRS = {
    'the guard before S6': (_prod, _stale_copy, 'project trader_joe_dev holds a container not started'),
    'empty before S6': (_empty, _stale_copy, NOTHING_UP),
    'S6 before D6': (_stale_copy, _symlinked_migrations, STALE_PREFIX),
    'D6 before D5': (_symlinked_migrations, _env_file_a_directory, 'a symlink at server/data/store/migrations'),
    'D5 before the versions guard': (_env_file_a_directory, _no_revision_files, 'is not a regular file'),
    'the versions guard before the lookups': (_no_revision_files, _no_postgres, 'no revision files'),
    'postgres before data_store': (_no_postgres, _two_data_store, 'no running postgres'),
    'data_store before the image': (_two_data_store, _missing_image, 'running data_store containers'),
    'the image before the budget': (_missing_image, _spent_budget, 'first'),
}


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize(('first', 'second', 'answer'), list(PAIRS.values()), ids=list(PAIRS))
def test_with_two_checks_failing_the_earlier_one_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, first, second, answer: str
):
    rig, responder = _rig(tmp_path, monkeypatch, mode)
    first(rig, responder)
    second(rig, responder)
    result = rig.call('migrate', {})
    assert result['status'] == 'refused' and answer in result['message'], result


# ===============================================================================================
# migrate TEST ONLY: the stamp gate over the RUNNING data_store and data_ingest containers
# ===============================================================================================


@pytest.mark.parametrize(
    'running',
    [('data_store', 'data_ingest'), ('data_store',), ('data_ingest',)],
    ids=['both', 'data_store only', 'data_ingest only'],
)
def test_each_running_service_has_its_stamp_read_and_a_missing_one_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, running: tuple[str, ...]
):
    rig, responder = _rig(tmp_path, monkeypatch, 'TEST')
    for service in ('data_store', 'data_ingest'):
        if service not in running:
            responder.lookups[service] = (0, b'')
    assert rig.call('migrate', {})['status'] == 'ok'
    assert _argvs(rig) == full_run('TEST', rig.layout.repo_host_path, running)
    stamped = {step.argv[-1] for step in rig.docker.steps if step.argv[:2] == (DOCKER, 'inspect')}
    assert stamped == {{'data_store': STORE_ID, 'data_ingest': INGEST_ID}[name] for name in running}


_SERVICE_STATES = {
    'both up': ('data_store', 'data_ingest'),
    'data_store only': ('data_store',),
    'data_ingest only': ('data_ingest',),
    'both down (postgres alone)': (),
}


def _set_running(responder: Responder, running: tuple[str, ...]) -> None:
    for service in ('data_store', 'data_ingest'):
        if service not in running:
            responder.lookups[service] = (0, b'')


@pytest.mark.parametrize('running', list(_SERVICE_STATES.values()), ids=list(_SERVICE_STATES))
def test_the_test_image_stamp_is_read_once_whatever_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, running: tuple[str, ...]
):
    """d6b942f closes the flagged gap: the :test image the one-off runs is stamp-checked ALWAYS, before the gate."""
    rig, responder = _rig(tmp_path, monkeypatch, 'TEST')
    _set_running(responder, running)
    assert rig.call('migrate', {})['status'] == 'ok'
    argvs = _argvs(rig)
    assert argvs.count(IMAGE_STAMP_READ) == 1 and argvs == full_run('TEST', rig.layout.repo_host_path, running)
    assert argvs.index(IMAGE_STAMP_READ) > argvs.index(image_inspect(TEST_STORE_IMAGE))
    assert argvs.index(IMAGE_STAMP_READ) < argvs.index(lookup('data_ingest'))


_BAD_IMAGE_STAMPS = {
    'the read fails': ((1, b''), f'the image {TEST_STORE_IMAGE} could not be read'),
    'unstamped': ((0, b''), f'CANNOT VERIFY: the image {TEST_STORE_IMAGE}'),
    'stale': ((0, f'{OTHER_DIGEST}\n'.encode()), f'STALE: {TEST_STORE_IMAGE} was built from different source'),
    "data_ingest's digest": ((0, f'{INGEST_DIGEST}\n'.encode()), f'STALE: {TEST_STORE_IMAGE}'),
}


@pytest.mark.parametrize('running', list(_SERVICE_STATES.values()), ids=list(_SERVICE_STATES))
@pytest.mark.parametrize(('answer', 'start'), list(_BAD_IMAGE_STAMPS.values()), ids=list(_BAD_IMAGE_STAMPS))
def test_a_bad_test_image_stamp_refuses_whatever_runs_with_no_alembic_and_no_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, running: tuple[str, ...], answer, start: str
):
    """Postgres alone up (the old gap) included: a stale :test image refuses, naming test_build."""
    rig, responder = _rig(tmp_path, monkeypatch, 'TEST')
    _set_running(responder, running)
    responder.image_stamp = answer
    result = rig.call('migrate', {})
    assert result['status'] == 'refused', result
    assert result['message'].startswith(start) and result['message'].endswith('call test_build'), result['message']
    assert _argvs(rig) == admission('TEST', rig.layout.repo_host_path)[:7], 'the gate stops at the image stamp'
    assert _argvs(rig)[-1] == IMAGE_STAMP_READ
    assert not _alembic_steps(rig) and _charges_left(rig) == BUDGET


def test_a_test_image_retagged_after_test_up_refuses_although_the_running_stamps_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """test_build at another checkout after test_up: the running containers match, the image the one-off runs does not."""
    rig, responder = _rig(tmp_path, monkeypatch, 'TEST')
    responder.image_stamp = (0, f'{OTHER_DIGEST}\n'.encode())
    result = rig.call('migrate', {})
    assert result['status'] == 'refused', result
    assert f'image stamp {OTHER_DIGEST}' in result['message'] and f'checkout digest {STORE_DIGEST}' in result['message']
    assert not [argv for argv in _argvs(rig) if argv[:2] == (DOCKER, 'inspect')], 'no container stamp after the refusal'
    assert not _alembic_steps(rig) and _charges_left(rig) == BUDGET


def test_with_postgres_alone_up_and_a_matching_image_the_migration_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The other half of the closed gap: no running service is not a refusal when the :test image matches."""
    rig, responder = _rig(tmp_path, monkeypatch, 'TEST')
    _set_running(responder, ())
    assert rig.call('migrate', {})['status'] == 'ok'
    argvs = _argvs(rig)
    assert IMAGE_STAMP_READ in argvs and [step for step in rig.docker.steps if is_dev_digest(step)]
    assert not [argv for argv in argvs if argv[:2] == (DOCKER, 'inspect')], 'no container to read'


def test_dev_mode_runs_no_stamp_gate_no_image_stamp_and_no_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """DEV binds the checkout's code over /code, so a stale image cannot run stale code: no gate, as make migrate."""
    rig, responder = _rig(tmp_path, monkeypatch, 'DEV')
    responder.stamps = {STORE_ID: (0, b''), INGEST_ID: (0, b'')}
    responder.image_stamp = (0, b'')
    assert rig.call('migrate', {})['status'] == 'ok'
    assert lookup('data_ingest') not in _argvs(rig)
    stamp_reads = [step for step in rig.docker.steps if 'trader_joe.source.digest' in ' '.join(step.argv)]
    assert not stamp_reads, 'DEV read a stamp'
    assert not [step for step in rig.docker.steps if is_dev_digest(step) or step.argv[:2] == (DOCKER, 'inspect')]


_BAD_STAMPS = {
    'the read fails': ((1, b''), 'container could not be read'),
    'the read is killed': ((None, b''), 'container could not be read'),
    'unstamped (empty)': ((0, b''), 'CANNOT VERIFY'),
    "docker's <no value>": ((0, b'<no value>\n'), 'CANNOT VERIFY'),
    'uppercase hex': ((0, f'{"A" * 64}\n'.encode()), 'CANNOT VERIFY'),
    'one digit short': ((0, f'{"1" * 63}\n'.encode()), 'CANNOT VERIFY'),
    'two stamps': ((0, f'{STORE_DIGEST}\n{STORE_DIGEST}\n'.encode()), 'CANNOT VERIFY'),
    'stale': ((0, f'{OTHER_DIGEST}\n'.encode()), 'STALE'),
    "the other service's stamp": ((0, f'{INGEST_DIGEST}\n'.encode()), 'STALE'),
}


@pytest.mark.parametrize('service', ['data_store', 'data_ingest'])
@pytest.mark.parametrize(('answer', 'kind'), list(_BAD_STAMPS.values()), ids=list(_BAD_STAMPS))
def test_a_bad_running_stamp_refuses_with_no_alembic_step_and_no_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: str, answer, kind: str
):
    rig, responder = _rig(tmp_path, monkeypatch, 'TEST')
    container_id = {'data_store': STORE_ID, 'data_ingest': INGEST_ID}[service]
    if service == 'data_ingest' and kind == 'STALE' and answer[1] == f'{INGEST_DIGEST}\n'.encode():
        answer = (0, f'{STORE_DIGEST}\n'.encode())
    responder.stamps[container_id] = answer
    result = rig.call('migrate', {})
    assert result['status'] == 'refused', result
    assert kind in result['message'] and f'running {service} container' in result['message'], result['message']
    assert result['message'].endswith(STALE_FIX), result['message']
    assert _argvs(rig)[-1] == container_stamp(container_id), 'the gate stops at the bad stamp'
    assert not _alembic_steps(rig) and not [step for step in rig.docker.steps if step.builds]
    assert _charges_left(rig) == BUDGET


def test_a_stale_stamp_names_both_digests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch, 'TEST')
    responder.stamps[STORE_ID] = (0, f'{OTHER_DIGEST}\n'.encode())
    message = rig.call('migrate', {})['message']
    assert f'container stamp {OTHER_DIGEST}' in message and f'checkout digest {STORE_DIGEST}' in message, message


@pytest.mark.parametrize('service_name', ['store', 'ingest'])
@pytest.mark.parametrize(
    'answer', [(1, b''), (None, b''), (0, b'not-a-digest\n')], ids=['exits 1', 'killed', 'malformed']
)
def test_a_failed_digest_answers_failed_before_any_stamp_and_charges_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service_name: str, answer
):
    rig, responder = _rig(tmp_path, monkeypatch, 'TEST')
    responder.digests[service_name] = answer
    result = rig.call('migrate', {})
    assert result['status'] == 'failed', result
    assert result['message'] == f'the source digest failed (data {service_name}); nothing was migrated'
    host = rig.layout.repo_host_path
    # guard, two lookups, the image inspect, then the digest(s): no image stamp, no container stamp read.
    assert _argvs(rig) == admission('TEST', host)[: 5 if service_name == 'store' else 6]
    assert IMAGE_STAMP_READ not in _argvs(rig)
    assert not _alembic_steps(rig)
    assert _charges_left(rig) == BUDGET


@pytest.mark.parametrize(
    ('answer', 'message'),
    [
        ((0, f'{INGEST_ID}\n{STORE_ID}\n'.encode()), '2 running data_ingest containers in the trader_joe_dev project'),
        ((1, b''), LOOKUP_FAILED),
        ((0, b'trader_joe-data_ingest-1\n'), 'the container lookup returned something that is not a container id'),
    ],
    ids=['two data_ingest', 'the lookup failed', 'a non-id'],
)
def test_the_data_ingest_lookup_refuses_several_a_failure_and_a_non_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer, message: str
):
    rig, responder = _rig(tmp_path, monkeypatch, 'TEST')
    responder.lookups['data_ingest'] = answer
    result = rig.call('migrate', {})
    assert result['status'] == 'refused' and result['message'].startswith(message), result
    assert _argvs(rig) == admission('TEST', rig.layout.repo_host_path)[:8]
    assert _charges_left(rig) == BUDGET


# ===============================================================================================
# migrate: FAILURES AFTER ADMISSION -- charged once, the exit status surfaced, the selection kept
# ===============================================================================================


@pytest.mark.parametrize('mode', MODES)
def test_a_migration_failing_midway_answers_failed_with_alembics_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
):
    rig, responder = _rig(tmp_path, monkeypatch, mode)
    responder.alembic = (1, b'', b'sqlalchemy.exc.ProgrammingError: column already exists\n')
    result = rig.call('migrate', {})
    assert (result['status'], result['exit_status']) == ('failed', 1), result
    assert result['message'] == (
        f'{SELECTION[mode]}; alembic upgrade head failed in the one-off data_store container; see the steps'
    )
    assert result['details'] == {'mode': mode, 'selection': SELECTION[mode]}
    assert 'column already exists' in result['steps'][-1]['stderr']['text']
    assert _argvs(rig) == full_run(mode, rig.layout.repo_host_path)
    assert _charges_left(rig) == BUDGET - 1


@pytest.mark.parametrize('mode', MODES)
def test_a_failed_base_pull_runs_no_alembic_and_is_charged_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
):
    rig, responder = _rig(tmp_path, monkeypatch, mode)
    ref = stack.BASE_IMAGES[0]

    def respond(step: stack.Step) -> runner.ProcessResult:
        if step.argv in (image_inspect(ref), (DOCKER, 'pull', ref)):
            return runner.ProcessResult(1, b'', b'pull access denied')
        return responder(step)

    rig.docker.respond = respond
    result = rig.call('migrate', {})
    assert result['status'] == 'failed' and result['message'].startswith('migrate stopped: base image'), result
    assert not _alembic_steps(rig)
    assert _charges_left(rig) == BUDGET - 1


def test_the_budget_is_charged_once_per_call_and_the_thirteenth_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, _ = _rig(tmp_path, monkeypatch, 'TEST')
    for _ in range(BUDGET):
        assert rig.call('migrate', {})['status'] == 'ok'
    rig.docker.steps.clear()
    result = rig.call('migrate', {})
    assert (result['status'], result['message']) == ('refused', BUDGET_REFUSAL), result
    assert _argvs(rig) == admission('TEST', rig.layout.repo_host_path), 'the charge comes after the last check'


# ===============================================================================================
# THE ARGVS: exact, built through their checks, and the checks refusing every neighbour
# ===============================================================================================


def _host() -> Path:
    return Path('/h/repo')


def test_the_dev_builder_spells_the_wrapped_one_off(tmp_path: Path):
    host = Path('/home/someone/repos/trader_joe')
    step = stack.dev_migrate_step(tmp_path, host)
    assert step.argv == dev_oneoff(host)
    assert step.argv[:6] == (
        'sh',
        '-c',
        WRAPPER_SCRIPT,
        'sh',
        f'{TRUSTED}/tools/dev_instance_secret.sh',
        f'{host}/.env',
    )
    assert step.argv[6:12] == (DOCKER, 'compose', '-p', PROJECT, '--project-directory', str(host))
    assert step.argv[12:22] == tuple(word for name in DEV_FILES for word in ('-f', f'{TRUSTED}/{name}'))
    assert step.argv[22:] == MIGRATE_TAIL == stack.DEV_MIGRATE_TAIL
    assert step.cwd == tmp_path and step.builds is True and step.env == PINNED_ENV


def test_the_test_builder_spells_the_unwrapped_one_off_over_base_and_fake(tmp_path: Path):
    host = Path('/home/someone/repos/trader_joe')
    step = stack.testmode_migrate_step(tmp_path, host)
    assert step.argv == (
        DOCKER,
        'compose',
        '-p',
        PROJECT,
        '--project-directory',
        str(host),
        '-f',
        f'{TRUSTED}/docker-compose.yaml',
        '-f',
        f'{TRUSTED}/docker-compose.fake.yaml',
        *MIGRATE_TAIL,
    )
    assert step.cwd == tmp_path and step.builds is True
    assert step.env == (*PINNED_ENV, ('DATA_DIR', TEST_DATA_DIR))


def test_each_builder_runs_its_argv_through_the_lifecycle_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[str, ...]] = []
    real = stack.check_dev_lifecycle_argv

    def spy(argv, repo_host_path=None):
        seen.append(tuple(argv))
        real(argv, repo_host_path)

    monkeypatch.setattr(stack, 'check_dev_lifecycle_argv', spy)
    dev = stack.dev_migrate_step(tmp_path, _host())
    test = stack.testmode_migrate_step(tmp_path, _host())
    assert seen == [dev.argv[6:], test.argv]


def test_the_image_and_lookup_steps_are_these_words(tmp_path: Path):
    for builder, image in (
        (stack.dev_data_store_image_step, DEV_STORE_IMAGE),
        (stack.testmode_data_store_image_step, TEST_STORE_IMAGE),
    ):
        step = builder(tmp_path)
        assert step.argv == image_inspect(image) and step.cwd == tmp_path and not step.builds and step.env == ()
    for service in ('postgres', 'data_store', 'data_ingest'):
        assert stack.dev_lookup_step(tmp_path, service).argv == lookup(service)
    assert stack.container_stamp_step(tmp_path, STORE_ID).argv == container_stamp(STORE_ID)


_DEV_GOOD = dev_oneoff(_host())
_TEST_GOOD = fakeset_oneoff(_host())
_TABLE = 'a dev lifecycle command is one of its fixed commands, with its fixed arguments, and no other'
_DEV_ROW = 'the dev migrate wrapper runs the fixed migrate command, and no other'
_DEV_HEAD = 'the dev migrate wrapper is its six fixed words and nothing else'
_TEST_ROW = 'the test migrate is the fixed `alembic upgrade head` command over the test set, and no other'
_FILES = 'a file-based dev lifecycle command names exactly the trusted dev compose files'


def _dev_tail(*tail: str) -> tuple[str, ...]:
    return (*wrapper_head(_host()), *file_prefix(_host()), *tail)


def _test_tail(*tail: str) -> tuple[str, ...]:
    return (*fakeset_prefix(_host()), *tail)


# name -> (which check, argv, the refusal it must start with)
_NEIGHBOURS = {
    'dev: upgrade to another revision': ('dev', _dev_tail(*MIGRATE_TAIL[:-1], 'd7e2a9c35b18'), _TABLE),
    'dev: upgrade heads': ('dev', _dev_tail(*MIGRATE_TAIL[:-1], 'heads'), _TABLE),
    'dev: upgrade head --sql': ('dev', _dev_tail(*MIGRATE_TAIL, '--sql'), _TABLE),
    'dev: downgrade base': ('dev', _dev_tail(*MIGRATE_TAIL[:-2], 'downgrade', 'base'), _TABLE),
    'dev: the removed check row': ('dev', _dev_tail(*MIGRATE_TAIL[:-2], 'check'), _TABLE),
    'dev: an extra -v': (
        'dev',
        _dev_tail(*MIGRATE_TAIL[:3], '-v', *MIGRATE_TAIL[3:]),
        'a dev lifecycle command removes',
    ),
    'dev: the up row': ('dev', _dev_tail(*stack.DEV_UP_TAIL), _DEV_ROW),
    'dev: no wrapper at all': ('dev', _DEV_GOOD[6:], _DEV_HEAD),
    'dev: the test one-off wrapped': ('dev', (*_DEV_GOOD[:6], *_TEST_GOOD), _DEV_ROW),
    'dev: another env file for the helper': (
        'dev',
        (*_DEV_GOOD[:5], '/h/repo/server/data/store/.env', *_DEV_GOOD[6:]),
        _DEV_HEAD,
    ),
    "dev: the checkout's compose file": (
        'dev',
        (*_DEV_GOOD[:13], '/h/repo/docker-compose.yaml', *_DEV_GOOD[14:]),
        _FILES,
    ),
    'test: any wrapper': ('test', (*wrapper_head(_host()), *_TEST_GOOD), 'a dev lifecycle verb runs only'),
    'test: the base file alone': (
        'test',
        (
            DOCKER,
            'compose',
            '-p',
            PROJECT,
            '--project-directory',
            str(_host()),
            '-f',
            f'{TRUSTED}/docker-compose.yaml',
            *MIGRATE_TAIL,
        ),
        _FILES,
    ),
    'test: the dev files': ('test', _DEV_GOOD[6:], _TEST_ROW),
    'test: the test build set': (
        'test',
        (*fakeset_prefix(_host()), '-f', f'{TRUSTED}/docker-compose.agent-test.yaml', *MIGRATE_TAIL),
        _TABLE,
    ),
    'test: upgrade heads': ('test', _test_tail(*MIGRATE_TAIL[:-1], 'heads'), _TABLE),
    'test: downgrade': ('test', _test_tail(*MIGRATE_TAIL[:-2], 'downgrade', '-1'), _TABLE),
    'test: --build': ('test', _test_tail('run', '--rm', '--no-deps', '--build', *MIGRATE_TAIL[3:]), _TABLE),
    'test: the test up row': ('test', _test_tail(*stack.TEST_UP_TAIL), _TEST_ROW),
    'test: another project': (
        'test',
        (*_TEST_GOOD[:3], 'trader_joe_agent_stack', *_TEST_GOOD[4:]),
        'a dev lifecycle verb runs only',
    ),
}
_CHECKS = {'dev': stack.check_dev_migrate_argv, 'test': stack.check_testmode_migrate_argv}


@pytest.mark.parametrize(('kind', 'argv', 'message'), list(_NEIGHBOURS.values()), ids=list(_NEIGHBOURS))
def test_the_migrate_checks_refuse_every_neighbour(kind: str, argv: tuple[str, ...], message: str):
    with pytest.raises(stack.Refused) as refused:
        _CHECKS[kind](argv, _host())
    assert str(refused.value).startswith(message), str(refused.value)


def test_the_migrate_checks_accept_their_own_argv_and_not_each_others():
    stack.check_dev_migrate_argv(_DEV_GOOD, _host())
    stack.check_testmode_migrate_argv(_TEST_GOOD, _host())
    with pytest.raises(stack.Refused):
        stack.check_dev_migrate_argv(_TEST_GOOD, _host())
    with pytest.raises(stack.Refused):
        stack.check_testmode_migrate_argv(_DEV_GOOD, _host())


def test_the_test_oneoff_table_holds_the_migrate_words_only():
    """TEST_ONEOFF_COMMANDS, consulted by check_dev_lifecycle_argv: one row, the migrate words, the TEST kind."""
    assert stack.TEST_ONEOFF_COMMANDS == {MIGRATE_TAIL: stack._TEST_FILE_BASED}
    stack.check_dev_lifecycle_argv(_TEST_GOOD, _host())
    stack.check_dev_lifecycle_argv(_DEV_GOOD[6:], _host())
    with pytest.raises(stack.Refused, match='one of its fixed commands'):
        stack.check_dev_lifecycle_argv((DOCKER, 'compose', '-p', PROJECT, *MIGRATE_TAIL), _host())


# ===============================================================================================
# THE SECRET: the REAL wrapper (DEV) and the real unwrapped one-off (TEST), a FAKE env file, a fake docker
# ===============================================================================================


def _secret_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> tuple[Rig, Path]:
    """A rig whose one-off really runs. DEV: word 4 becomes the repository's helper, word 6 a fake docker.

    TEST: word 0 (docker) becomes the fake docker; there is no wrapper and no helper to swap.
    """
    rig, _ = _rig(tmp_path, monkeypatch, mode)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', REAL_CREATE_SUBPROCESS_EXEC)
    fakebin = tmp_path / 'fakebin'
    fakebin.mkdir()
    fake = fakebin / 'docker'
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    helper = REPO_ROOT / 'tools' / 'dev_instance_secret.sh'
    docker = rig.docker
    host = rig.layout.repo_host_path

    async def run(step: stack.Step) -> runner.ProcessResult:
        if is_dev_up_wrapper(step):
            docker.steps.append(step)
            assert step.argv[4] == f'{TRUSTED}/tools/dev_instance_secret.sh' and step.argv[6] == DOCKER
            argv = (*step.argv[:4], str(helper), step.argv[5], str(fake), *step.argv[7:])
            return await runner.run_process(replace(step, argv=argv))
        if step.argv == fakeset_oneoff(host):
            docker.steps.append(step)
            return await runner.run_process(replace(step, argv=(str(fake), *step.argv[1:])))
        return await docker(step)

    rig.agent = runner.AgentStack(rig.layout.settings, run=run)
    return rig, fakebin


def _child_env(fakebin: Path) -> dict[str, str]:
    lines = (fakebin / 'env').read_text(encoding='utf-8').splitlines()
    return dict(line.split('=', 1) for line in lines if '=' in line)


def _clean(child: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in child.items() if key not in ('PWD', 'SHLVL', '_')}


@pytest.mark.parametrize('mode', MODES)
def test_the_secret_never_reaches_an_argv_the_audit_or_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
):
    rig, fakebin = _secret_rig(tmp_path, monkeypatch, mode)
    (rig.layout.repo / '.env').write_text(f'POSTGRES_PASS=fake-pg\nINSTANCE_WRITE_SECRET={FAKE_SECRET}\n')
    result = rig.call('migrate', {})
    assert result['status'] == 'ok', result
    child = _clean(_child_env(fakebin))
    host = rig.layout.repo_host_path
    argv_seen = (fakebin / 'argv').read_text(encoding='utf-8').splitlines()
    if mode == 'DEV':
        assert child == {**runner.DOCKER_ENV, **dict(PINNED_ENV), 'INSTANCE_WRITE_SECRET': FAKE_SECRET}
        assert argv_seen == [*file_prefix(host)[1:], *MIGRATE_TAIL]
    else:
        assert child == {**runner.DOCKER_ENV, **dict(PINNED_ENV), 'DATA_DIR': TEST_DATA_DIR}
        assert 'INSTANCE_WRITE_SECRET' not in child, 'TEST derives no secret: base + fake carry no :? guard on it'
        assert argv_seen == [*fakeset_prefix(host)[1:], *MIGRATE_TAIL]
    for step in rig.docker.steps:
        assert not [word for word in step.argv if FAKE_SECRET in word or 'fake-pg' in word], step.argv
        assert FAKE_SECRET not in dict(step.env).values()
    kept = json.dumps(result) + '\n'.join(rig.audit_lines())
    assert FAKE_SECRET not in kept and 'fake-pg' not in kept
    assert 'fake compose: up' in result['steps'][-1]['stdout']['text'], 'the output was recorded, without the value'
    assert FAKE_SECRET not in os.environ.values()


def test_an_empty_secret_gives_unused_and_the_dev_migration_still_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """As make migrate's compose would interpolate any value: the one-off data_store never reads it."""
    rig, fakebin = _secret_rig(tmp_path, monkeypatch, 'DEV')
    (rig.layout.repo / '.env').write_text('INSTANCE_WRITE_SECRET=\n')
    assert rig.call('migrate', {})['status'] == 'ok'
    assert _child_env(fakebin)['INSTANCE_WRITE_SECRET'] == 'unused'


# ===============================================================================================
# migrate_status: the mode guard, then the exec of the former dev_migrate_status, never charged
# ===============================================================================================


@pytest.mark.parametrize('mode', MODES)
def test_migrate_status_runs_the_guard_the_lookup_then_one_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
):
    rig, _ = _rig(tmp_path, monkeypatch, mode)
    result = rig.call('migrate_status', {})
    assert result['status'] == 'ok', result
    assert result['message'] == f'{SELECTION[mode]}; alembic current and history from the running data_store container'
    assert result['details'] == {'mode': mode, 'selection': SELECTION[mode]}
    assert _argvs(rig) == status_run()
    assert all(step.cwd == rig.layout.stack_dir and step.env == () and not step.builds for step in rig.docker.steps)
    audit = json.loads(rig.audit_lines()[-1])
    assert audit['verb'] == 'migrate_status' and audit['status'] == 'ok' and audit['arguments'] == {}, audit


@pytest.mark.parametrize('mode', MODES)
def test_migrate_status_never_charges_and_runs_with_the_budget_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
):
    rig, _ = _rig(tmp_path, monkeypatch, mode)
    assert rig.call('migrate_status', {})['status'] == 'ok'
    assert _charges_left(rig) == BUDGET, 'migrate_status charged the budget'
    assert rig.call('migrate_status', {})['status'] == 'ok', 'a spent budget refused a read-only verb'


@pytest.mark.parametrize('mode', MODES)
def test_migrate_status_never_runs_upgrade_or_a_one_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str):
    rig, _ = _rig(tmp_path, monkeypatch, mode)
    assert rig.call('migrate_status', {})['status'] == 'ok'
    for step in rig.docker.steps:
        words = ' '.join(step.argv)
        assert 'upgrade' not in words and 'compose' not in step.argv and not is_dev_up_wrapper(step), step.argv
    assert 'for sub in current history; do' in stack.DEV_MIGRATE_STATUS_SCRIPT
    assert 'upgrade' not in stack.DEV_MIGRATE_STATUS_SCRIPT


_STATUS_GUARD_REFUSALS = {
    'an empty project': ((0, b''), NOTHING_UP),
    'a prod container': ((0, PROD_LINE), GUARD_REFUSAL),
    'dev and test together': ((0, DEV_LINE + TEST_LINE), MIXED_REFUSAL),
    'the guard could not list': ((1, b''), GUARD_FAILED),
}


@pytest.mark.parametrize(('guard', 'message'), list(_STATUS_GUARD_REFUSALS.values()), ids=list(_STATUS_GUARD_REFUSALS))
def test_migrate_status_refuses_on_the_guard_with_nothing_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard, message: str
):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.guard = guard
    result = rig.call('migrate_status', {})
    assert (result['status'], result['message']) == ('refused', message), result
    assert _argvs(rig) == [GUARD]


_STATUS_LOOKUP_ANSWERS = {
    'no running container': ((0, b''), 'no running data_store container'),
    'blank lines': ((0, b'\n \n'), 'no running data_store container'),
    'two running containers': ((0, f'{STORE_ID}\n{INGEST_ID}\n'.encode()), '2 running data_store'),
    'a non-hex answer': ((0, b'trader_joe-data_store-1\n'), 'the container lookup returned something'),
    'the lookup failed': ((1, b''), 'the container lookup failed'),
}


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize(('answer', 'start'), list(_STATUS_LOOKUP_ANSWERS.values()), ids=list(_STATUS_LOOKUP_ANSWERS))
def test_migrate_status_refuses_without_exec_unless_the_lookup_found_exactly_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, answer, start: str
):
    """Zero, several, a non-id or a failed lookup: 'refused', ZERO exec; zero names db_tables."""
    rig, responder = _rig(tmp_path, monkeypatch, mode)
    responder.lookups['data_store'] = answer
    result = rig.call('migrate_status', {})
    assert result['status'] == 'refused' and result['message'].startswith(start), result
    assert _argvs(rig) == status_run()[:2], 'an exec ran'
    names_fallback = result['message'].endswith('; use db_tables, which works while data_store is down')
    assert names_fallback is start.startswith('no running'), result['message']
    assert STORE_ID not in result['message'] and _charges_left(rig) == BUDGET


@pytest.mark.parametrize('mode', MODES)
def test_a_failed_status_exec_is_failed_with_its_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
):
    rig, responder = _rig(tmp_path, monkeypatch, mode)
    responder.exec = (3, b'== alembic current ==\nboom\n', b'')
    result = rig.call('migrate_status', {})
    assert (result['status'], result['exit_status']) == ('failed', 3), result
    assert result['message'] == f'{SELECTION[mode]}; alembic exited 3 in the running data_store; see the steps'


# ===============================================================================================
# THE SURFACE: the registry, the tool list, the schemas, the timeouts, the descriptions
# ===============================================================================================

REMOVED = ('dev_migrate', 'dev_migrate_check', 'dev_migrate_status', 'migrate_check')


def test_the_removed_verbs_are_not_registered_anywhere():
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type] -- only the verb table is read
    for verb in REMOVED:
        assert verb not in agent.verbs, verb
        assert verb not in runner.VERB_SCHEMAS and verb not in runner.VERB_TIMEOUT_SECONDS, verb
    assert {'migrate', 'migrate_status'} <= set(agent.verbs)
    for name in ('_dev_migrate', '_dev_migrate_check', '_dev_migrate_status', '_migrate_check', '_alembic'):
        assert not hasattr(runner.AgentStack, name), name
    for name in ('dev_migrate_check_step', 'check_dev_migrate_check_argv', 'DEV_MIGRATE_CHECK_TAIL', 'alembic_steps'):
        assert not hasattr(stack, name), name


def test_the_tool_list_is_the_verb_table():
    """server.py lists one tool per AgentStack.verbs entry, so a verb out of the table is out of the tool list."""
    source = (REPO_ROOT / 'tools' / 'agent_mcp' / 'server.py').read_text(encoding='utf-8')
    assert 'for verb in agent_stack.verbs' in source and 'inputSchema=VERB_SCHEMAS[verb]' in source
    for verb in REMOVED:
        assert f"'{verb}'" not in source, verb


@pytest.mark.parametrize('verb', ['migrate', 'migrate_status'])
def test_the_schema_is_closed_and_takes_nothing(verb: str):
    assert runner.VERB_SCHEMAS[verb] == {
        'type': 'object',
        'properties': {},
        'required': [],
        'additionalProperties': False,
    }


@pytest.mark.parametrize('verb', ['migrate', 'migrate_status'])
@pytest.mark.parametrize(
    'arguments',
    [
        {'revision': 'base'},
        {'mode': 'PROD'},
        {'MIGRATE_CHECK_ALLOW_STALE_IMAGE': '1'},
        {'command': 'downgrade'},
        ['upgrade'],
        'x',
    ],
    ids=repr,
)
def test_any_argument_is_refused_with_no_step_and_no_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, arguments: Any
):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert rig.call(verb, arguments)['status'] == 'refused'
    assert rig.docker.steps == [] and _charges_left(rig) == BUDGET


def test_the_dead_empty_branch_is_gone_and_the_guard_alone_refuses_empty():
    """aa886b6: _migrate_mode is the guard call; the guard refuses an empty project with migrate's hint."""
    source = inspect.getsource(runner.AgentStack._migrate_mode)
    assert 'is None' not in source and source.count('raise') == 0, source
    with pytest.raises(stack.Refused) as refused:
        stack.check_mode_guard(0, b'', stack.MODES_DEV_OR_TEST, NOTHING_UP)
    assert str(refused.value) == NOTHING_UP
    assert None not in stack.MODES_DEV_OR_TEST


@pytest.mark.parametrize(
    ('verb', 'arguments', 'service', 'mode'),
    [
        ('migrate', {}, 'postgres', 'DEV'),
        ('migrate', {}, 'postgres', 'TEST'),
        ('migrate_status', {}, 'data_store', 'DEV'),
        ('migrate_status', {}, 'data_store', 'TEST'),
        ('db_tables', {}, 'postgres', 'DEV'),
        ('http_get', {'name': 'ping'}, 'data_store', 'DEV'),
    ],
)
def test_every_no_container_refusal_carries_the_mode_neutral_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, arguments: dict, service: str, mode: str
):
    """183f296: pick_dev_target names up AND test_up; 'start the dev stack first' is gone (wrong under TEST)."""
    rig, responder = _rig(tmp_path, monkeypatch, mode)
    responder.lookups[service] = (0, b'')
    result = rig.call(verb, arguments)
    assert result['status'] == 'refused', result
    assert result['message'].startswith(f'no running {service} container in the trader_joe_dev project: {NEUTRAL_HINT}')
    assert 'start the dev stack first' not in result['message']


def test_the_timeouts_are_600_and_300_inside_the_clients():
    assert runner.VERB_TIMEOUT_SECONDS['migrate'] == 600
    assert runner.VERB_TIMEOUT_SECONDS['migrate_status'] == 300
    config = json.loads((REPO_ROOT / '.mcp.json').read_text(encoding='utf-8'))
    timeout = config['mcpServers']['agent-stack']['timeout']
    assert timeout >= (max(runner.VERB_TIMEOUT_SECONDS.values()) + 30) * 1000


def test_migrate_says_it_writes_the_database_of_the_mode_that_is_up():
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type] -- only the verb table is read
    text = agent.describe('migrate')
    assert text.startswith('STATE-CHANGING, and it WRITES THE DATABASE OF THE MODE THAT IS UP'), text
    assert "the USER'S OWN dev database in DEV mode" in text and './volume_data/test' in text, text
    assert 'alembic upgrade head' in text and 'run --rm --no-deps' in text and 'no dry run' in text, text
    assert "owner's client setting" in text and 'call build or test_build' in text, text
    # aa886b6: the image AND each running container carry the stamp; several data_ingest refuse in TEST only.
    assert (
        'REFUSED unless the :test data_store image and each running data_store and data_ingest container carry '
        "a source stamp equal to this checkout's digest"
    ) in text, text
    assert 'several data_store containers run (in TEST also several data_ingest)' in text, text
    assert 'several data_store or data_ingest containers' not in text, text
    assert 'stale test stack: call test_build, then test_up' in text and 'Never touches prod' in text, text
    status = agent.describe('migrate_status')
    assert status.startswith('READ-ONLY: `alembic current` then `alembic history` on the mode that is up'), status
    assert 'charges no budget' in status and 'db_tables' in status, status


# ===============================================================================================
# MIRRORS: make migrate, run_migrations.sh's two definitions, the Makefile's test data dir, the files
# ===============================================================================================

SCRIPT = REPO_ROOT / 'server' / 'data' / 'store' / 'run_migrations.sh'


def _script() -> str:
    return SCRIPT.read_text(encoding='utf-8')


def _recipe(target: str) -> list[str]:
    text = (REPO_ROOT / 'Makefile').read_text(encoding='utf-8').replace('\\\n', ' ')
    match = re.search(rf'^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n)+)', text, re.MULTILINE)
    assert match is not None, f'the Makefile has no {target} target'
    return [line[1:].strip() for line in match.group(1).splitlines()]


def test_make_migrate_is_the_script_with_no_argument():
    assert _recipe('migrate') == ['./server/data/store/run_migrations.sh']


def test_the_tail_is_the_scripts_one_off_with_its_default_argument():
    """The script's last line runs `"${COMPOSE[@]}" run --rm --no-deps data_store <alembic> "${ALEMBIC_ARGS[@]}"`."""
    lines = [line for line in _script().splitlines() if line.startswith('"${COMPOSE[@]}" run ')]
    assert len(lines) == 1, lines
    words = shlex.split(lines[0], posix=False)
    assert words[0] == '"${COMPOSE[@]}"' and words[-1] == '"${ALEMBIC_ARGS[@]}"', words
    assert tuple(words[1:-1]) == MIGRATE_TAIL[:5]
    assert 'ALEMBIC_ARGS=(upgrade head)' in _script()
    assert MIGRATE_TAIL[5:] == ('upgrade', 'head')


def _definition(name: str) -> tuple[str, ...]:
    """The -f files of the script's COMPOSE=(...) line inside select_stack's NAME branch."""
    block = _script().split('select_stack() {', 1)[1]
    match = re.search(rf'\n\s*{name}\)\n\s*COMPOSE=\(docker compose ((?:-f \S+ ?)+)\)', block)
    assert match is not None, f'no {name} branch in select_stack'
    return tuple(match.group(1).split()[1::2])


def test_the_test_one_off_loads_exactly_the_scripts_test_definition():
    """TEST: base + fake, nothing else -- what run_migrations.sh selects for a fake-labelled postgres."""
    assert _definition('TEST') == TEST_FILES
    assert tuple(fakeset_oneoff(_host())[7:10:2]) == tuple(f'{TRUSTED}/{name}' for name in TEST_FILES)
    assert 'using the TEST definition (docker-compose.yaml + docker-compose.fake.yaml)' in _script()


def test_the_dev_one_off_loads_the_scripts_dev_definition_first():
    """DEV: the script loads base + override; the MCP adds the web files and the agent-dev overlay after them."""
    assert _definition('DEV') == DEV_FILES[:2]
    assert 'using the DEV definition (docker-compose.yaml + docker-compose.override.yaml)' in _script()


def test_the_selection_lines_name_the_scripts_definitions():
    for mode, files in (('DEV', DEV_FILES[:2]), ('TEST', TEST_FILES)):
        assert SELECTION[mode].startswith(f'using the {mode} definition ({" + ".join(files)})'), SELECTION[mode]
        assert stack.migrate_selection(mode) == SELECTION[mode]


def test_the_test_data_dir_is_the_makefiles():
    text = (REPO_ROOT / 'Makefile').read_text(encoding='utf-8')
    match = re.search(r'^TEST_DATA_DIR\s*:?=\s*(\S+)\s*$', text, re.MULTILINE)
    assert match is not None and match.group(1) == TEST_DATA_DIR, match
    # Re-pinned for tj-grna9p.161 (ADR tj-4rr0la addendum 29): the Makefile's test set now carries
    # -p $(DEV_PROJECT) (trader_joe_dev). This pins the Makefile side only; the MCP's own test-mode
    # project moves with tj-iee6sn.31, not here.
    assert (
        'SYSTEM_COMPOSE := DATA_DIR=$(TEST_DATA_DIR) docker compose -p $(DEV_PROJECT) -f docker-compose.yaml '
        '-f docker-compose.fake.yaml'
    ) in text
    # Read as make expands it, not by a regex on the line: the assignment is `override` since tj-grna9p.192.
    assert _made_variable('DEV_PROJECT') == 'trader_joe_dev', 'DEV_PROJECT is not trader_joe_dev'


def _services(name: str) -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / name).read_text(encoding='utf-8')).get('services') or {}


def test_base_and_fake_carry_no_instance_secret_guard_and_the_web_files_do():
    """Review (3): TEST needs no wrapper. The only `:?` guard in base + fake is POSTGRES_PASS, read by compose."""
    for name in TEST_FILES:
        text = (REPO_ROOT / name).read_text(encoding='utf-8')
        assert 'INSTANCE_WRITE_SECRET' not in text, name
        assert re.findall(r'\$\{(\w+):\?', text) == (['POSTGRES_PASS'] if name == 'docker-compose.yaml' else []), name
    assert '${INSTANCE_WRITE_SECRET:?' in (REPO_ROOT / 'docker-compose.web.yaml').read_text(encoding='utf-8')


def test_the_fake_file_runs_data_store_on_the_test_image_and_the_base_binds_the_revisions():
    assert _services('docker-compose.fake.yaml')['data_store']['image'] == TEST_STORE_IMAGE
    volumes = _services('docker-compose.yaml')['data_store']['volumes']
    assert './server/data/store/migrations:/code/migrations' in volumes
    assert './server/data/store/alembic.ini:/code/alembic.ini' in volumes


def test_the_extra_dev_files_change_nothing_the_data_store_one_off_runs():
    """The web files define only web; the MCP overlay only data_store's build.dockerfile."""
    assert set(_services('docker-compose.web.yaml')) == {'web'}
    assert set(_services('docker-compose.web.dev.yaml')) == {'web'}
    overlay = _services('docker-compose.agent-dev.yaml')
    assert overlay['data_store'] == {'build': {'dockerfile': '/opt/agent_mcp/compose/Dockerfile'}}
