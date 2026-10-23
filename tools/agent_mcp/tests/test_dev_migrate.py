"""dev_migrate and dev_migrate_check: `make migrate` and `make migrate-check` on the user's dev project (tj-iee6sn.10).

Design checked against: the architect's notes on tj-iee6sn.10 (20:58 dev_migrate_status removed, 22:05 plan
revision 2: dev_migrate is exactly make migrate, a plain call with timeout 600; 22:45 R1: dev_migrate_check
mirrors make migrate-check with its stamp refusal and NO MIGRATE_CHECK_ALLOW_STALE_IMAGE), owner rulings
D4, D5 and D7 on tj-iee6sn.1, ADR tj-4rr0la addenda 22-24, and server/data/store/run_migrations.sh with the
Makefile's migrate / migrate-check recipes, whose semantics the verbs mirror. Both are PLAIN calls. Order,
a refusal at any point running nothing after it and charging nothing:

    D7 guard -> S6 stale copies -> D6 bind-source symlinks -> D5 env-file preflight -> the empty-versions
    guard (no step) -> the postgres lookup (exactly one running) -> the data_store lookup (several refuse,
    none passes, as in select_stack) -> `docker image inspect` of trader_joe_data_store:dev (missing:
    'call dev_build first') -> [dev_migrate_check only: the trusted data/store digest (a failure answers
    failed, uncharged), the stamp read, check_dev_image_stamp] -> ONE budget charge -> the bases (the
    one-off `run` of data_store is flagged as building) -> the one-off alembic run, carried by dev_up's
    fixed `sh -c` secret wrapper under dev_pinned_step_env().

No steering walk runs (every -f is given, nothing label-only runs) and no generated-code check runs (no
build). Every verb call goes through the REAL AgentStack over the fake-docker rig. The secret tests run
the REAL wrapper under sh with the REAL tools/dev_instance_secret.sh, a FAKE env file and a fake docker.
Expected values are literals, or parsed from run_migrations.sh, the Makefile and the compose files.
"""

import asyncio
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
from tools.agent_mcp.tests.test_dev_label_verbs import GUARD, GUARD_REFUSAL, PROD_LINE, symlink_refusal
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
PROJECT = 'trader_joe'
STORE_IMAGE = 'trader_joe_data_store:dev'
ALEMBIC = '/code/.venv/bin/alembic'
MIGRATE_TAIL = ('run', '--rm', '--no-deps', 'data_store', ALEMBIC, 'upgrade', 'head')
CHECK_TAIL = ('run', '--rm', '--no-deps', 'data_store', ALEMBIC, 'check')
STAMP_FORMAT = '{{ with .Config.Labels }}{{ index . "trader_joe.source.digest" }}{{ end }}'
STAMP_READ = (DOCKER, 'image', 'inspect', '--format', STAMP_FORMAT, STORE_IMAGE)
STORE_DIGEST = '1' * 64
POSTGRES_ID = b'aaaaaaaaaaa1'
STORE_ID = b'bbbbbbbbbbb2'
HATCH = 'MIGRATE_CHECK_ALLOW_STALE_IMAGE'
VERSIONS = 'server/data/store/migrations/versions'
BUDGET = 12
BUDGET_REFUSAL = '12 state-changing dev verbs in the last hour is the budget; wait, or run the make target yourself'
STALE_PREFIX = 'the MCP image is stale: '
SELECTION = (
    'using the DEV definition (docker-compose.yaml + docker-compose.override.yaml): '
    'every container of the project was started with docker-compose.override.yaml'
)
MIGRATE_OK = f'alembic upgrade head applied to the running dev database ({SELECTION})'
MIGRATE_FAILED = 'alembic upgrade head failed in the one-off data_store container; see the steps'
CHECK_OK = (
    'no drift between the verified data_store :dev image and the dev schema; '
    'enum labels and server defaults were not compared'
)
EMPTY_VERSIONS = f'no revision files in {VERSIONS}: wrong checkout?'
IMAGE_REFUSAL = f'the image {STORE_IMAGE} is not on the daemon: call dev_build first'
DIGEST_FAILED = 'the source digest failed (data store); nothing was checked'
NO_POSTGRES = 'no running postgres container in the trader_joe project: start the dev stack first'
LOOKUP_FAILED = 'the container lookup failed (exit 1); see the steps'
VERBS = ('dev_migrate', 'dev_migrate_check')
TAILS = {'dev_migrate': MIGRATE_TAIL, 'dev_migrate_check': CHECK_TAIL}


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


def wrapped(host: Path, tail: tuple[str, ...]) -> tuple[str, ...]:
    return (*wrapper_head(host), *file_prefix(host), *tail)


def base_inspects() -> list[tuple[str, ...]]:
    return [image_inspect(ref) for ref in stack.BASE_IMAGES]


def admission(verb: str, host: Path) -> list[tuple[str, ...]]:
    """Every step a call runs ahead of the budget charge, in order."""
    steps = [GUARD, lookup('postgres'), lookup('data_store'), image_inspect(STORE_IMAGE)]
    if verb == 'dev_migrate_check':
        steps += [digest_argv(host, 'store'), STAMP_READ]
    return steps


def full_run(verb: str, host: Path) -> list[tuple[str, ...]]:
    return [*admission(verb, host), *base_inspects(), wrapped(host, TAILS[verb])]


class Responder:
    """The fake daemon of a migrate call: each step's answer is a field the test can change."""

    def __init__(self) -> None:
        self.guard: tuple[int, bytes] = (0, b'')
        self.lookups: dict[str, tuple[int, bytes]] = {
            'postgres': (0, POSTGRES_ID + b'\n'),
            'data_store': (0, STORE_ID + b'\n'),
        }
        self.image_exit = 0
        self.digest: tuple[int | None, bytes] = (0, f'{STORE_DIGEST}\n'.encode())
        self.stamp: tuple[int | None, bytes] = (0, f'{STORE_DIGEST}\n'.encode())
        self.alembic: tuple[int, bytes, bytes] = (0, b'', b'')

    def __call__(self, step: stack.Step) -> runner.ProcessResult:
        argv = step.argv
        if argv == GUARD:
            return runner.ProcessResult(self.guard[0], self.guard[1], b'')
        for service, answer in self.lookups.items():
            if argv == lookup(service):
                return runner.ProcessResult(answer[0], answer[1], b'')
        if argv == image_inspect(STORE_IMAGE):
            return runner.ProcessResult(self.image_exit, b'' if self.image_exit else b'sha256:abc\n', b'')
        if is_dev_digest(step):
            return runner.ProcessResult(self.digest[0], self.digest[1], b'')
        if argv == STAMP_READ:
            return runner.ProcessResult(self.stamp[0], self.stamp[1], b'')
        if is_dev_up_wrapper(step):
            return runner.ProcessResult(*self.alembic)
        return default_response(step)


def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Rig, Responder]:
    responder = Responder()
    return make_rig(tmp_path, monkeypatch, FakeDocker(respond=responder)), responder


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


@pytest.mark.parametrize('verb', VERBS)
def test_each_verb_runs_every_step_in_the_designed_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig, _ = _rig(tmp_path, monkeypatch)
    result = rig.call(verb, {})
    assert result['status'] == 'ok', result
    assert result['message'] == (MIGRATE_OK if verb == 'dev_migrate' else CHECK_OK)
    assert _argvs(rig) == full_run(verb, rig.layout.repo_host_path)
    assert all(step.cwd == rig.layout.stack_dir for step in rig.docker.steps), rig.docker.steps
    assert 'job' not in json.dumps(result).lower(), 'a plain call, never a job'
    assert _charges_left(rig) == BUDGET - 1, 'an admitted call is charged exactly once'


def test_dev_migrate_ok_carries_the_dev_selection_line_run_migrations_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """D4 ruled: 'prints which one it used'. The definition and the files named are run_migrations.sh's own."""
    rig, _ = _rig(tmp_path, monkeypatch)
    message = rig.call('dev_migrate', {})['message']
    assert SELECTION in message, message
    script = (REPO_ROOT / 'server' / 'data' / 'store' / 'run_migrations.sh').read_text(encoding='utf-8')
    assert 'using the DEV definition (docker-compose.yaml + docker-compose.override.yaml)' in script


def test_dev_migrate_check_returns_what_it_compared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    result = rig.call('dev_migrate_check', {})
    assert result['details'] == {
        'compared': (
            'the models of trader_joe_data_store:dev, whose source stamp matches this checkout, against the live '
            'schema of the running dev database'
        ),
        'not_compared': 'enum labels and server defaults, which autogenerate does not read',
    }


def test_a_database_already_at_head_answers_ok_with_alembics_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An upgrade head over a database at head is alembic's exit 0; its output is in the step record."""
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.alembic = (0, b'', b'INFO  [alembic.runtime.migration] Will assume transactional DDL.\n')
    result = rig.call('dev_migrate', {})
    assert (result['status'], result['exit_status']) == ('ok', 0), result
    assert 'transactional DDL' in result['steps'][-1]['stderr']['text']


def test_no_data_store_container_proceeds_as_select_stack_does(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """select_stack: a dev postgres with NO data_store is still dev. Only several data_store refuse."""
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.lookups['data_store'] = (0, b'')
    for verb in VERBS:
        assert rig.call(verb, {})['status'] == 'ok'


@pytest.mark.parametrize('verb', VERBS)
def test_only_the_alembic_step_carries_an_env_and_it_is_the_pinned_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """dev_pinned_step_env(), exactly: no SOURCE_DIGEST_* (nothing builds), no secret, no placeholder, no hatch."""
    rig, _ = _rig(tmp_path, monkeypatch)
    assert rig.call(verb, {})['status'] == 'ok'
    alembic = [step for step in rig.docker.steps if is_dev_up_wrapper(step)]
    assert len(alembic) == 1
    assert alembic[0].env == PINNED_ENV == stack.dev_pinned_step_env()
    assert all(step.env == () for step in rig.docker.steps if not is_dev_up_wrapper(step)), rig.docker.steps
    env = dict(alembic[0].env)
    assert not [name for name in env if name.startswith('SOURCE_DIGEST')], env
    assert 'INSTANCE_WRITE_SECRET' not in env and 'POSTGRES_PASS' not in env and '/dev/null' not in env.values()


@pytest.mark.parametrize('verb', VERBS)
def test_the_escape_hatch_is_in_no_argv_no_env_and_no_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """R1: MIGRATE_CHECK_ALLOW_STALE_IMAGE is never offered, even with the server's own env carrying it."""
    monkeypatch.setenv(HATCH, '1')
    rig, _ = _rig(tmp_path, monkeypatch)
    assert rig.call(verb, {})['status'] == 'ok'
    for step in rig.docker.steps:
        assert not [word for word in step.argv if HATCH in word], step.argv
        assert HATCH not in dict(step.env), step.env
    assert HATCH not in runner.DOCKER_ENV
    assert runner.VERB_SCHEMAS[verb]['properties'] == {}


@pytest.mark.parametrize('verb', VERBS)
def test_only_the_alembic_run_is_flagged_as_building_and_the_bases_come_just_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert rig.call(verb, {})['status'] == 'ok'
    host = rig.layout.repo_host_path
    assert [step.argv for step in rig.docker.steps if step.builds] == [wrapped(host, TAILS[verb])]
    assert _argvs(rig)[-1 - len(stack.BASE_IMAGES) : -1] == base_inspects()


@pytest.mark.parametrize('verb', VERBS)
def test_no_digest_and_no_secret_word_in_a_recorded_command_or_the_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig, _ = _rig(tmp_path, monkeypatch)
    result = rig.call(verb, {})
    commands = [record['command'] for record in result['steps']]
    assert commands[-1] == shlex.join(wrapped(rig.layout.repo_host_path, TAILS[verb]))
    for word in ('SOURCE_DIGEST', 'COMPOSE_', STORE_DIGEST, HATCH):
        assert not [command for command in commands if word in command], (word, commands)
    assert not [line for line in rig.audit_lines() if STORE_DIGEST in line]
    assert json.loads(rig.audit_lines()[-1])['arguments'] == {}


# ===============================================================================================
# THE ORDER: each refusal runs only the steps before it and charges nothing
# ===============================================================================================


def _prod_container(rig: Rig, responder: Responder) -> None:
    responder.guard = (0, PROD_LINE)


def _guard_failed(rig: Rig, responder: Responder) -> None:
    responder.guard = (1, b'')


def _stale_copy(rig: Rig, responder: Responder) -> None:
    path = rig.layout.trusted_dir / 'tools' / 'source_digest.sh'
    path.write_bytes(path.read_bytes() + b'# drift\n')


def _symlinked_bind_source(rig: Rig, responder: Responder) -> None:
    """The one-off mounts migrations/ itself: a symlink there is the D6 case that matters most here."""
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
    responder.lookups['postgres'] = (0, POSTGRES_ID + b'\n' + STORE_ID + b'\n')


def _postgres_lookup_failed(rig: Rig, responder: Responder) -> None:
    responder.lookups['postgres'] = (1, b'')


def _two_data_store(rig: Rig, responder: Responder) -> None:
    responder.lookups['data_store'] = (0, STORE_ID + b'\n' + POSTGRES_ID + b'\n')


def _data_store_lookup_failed(rig: Rig, responder: Responder) -> None:
    responder.lookups['data_store'] = (1, b'')


def _missing_image(rig: Rig, responder: Responder) -> None:
    responder.image_exit = 1


def _spent_budget(rig: Rig, responder: Responder) -> None:
    for _ in range(BUDGET):
        rig.agent.dev_budget.charge()


# name -> (break it, the refusal message (None: checked by prefix below), how many steps ran)
REFUSALS = {
    'D7: a prod container in the project': (_prod_container, GUARD_REFUSAL, 1),
    'D7: the guard could not list': (
        _guard_failed,
        'could not list the containers of project trader_joe, so nothing was changed',
        1,
    ),
    'S6: a stale baked copy': (_stale_copy, None, 1),
    'D6: a symlinked migrations bind source': (
        _symlinked_bind_source,
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
        '2 running data_store containers in the trader_joe project; cannot tell which stack',
        3,
    ),
    'the data_store lookup failed': (_data_store_lookup_failed, LOOKUP_FAILED, 3),
    'no data_store:dev image': (_missing_image, IMAGE_REFUSAL, 4),
    'the budget spent': (_spent_budget, BUDGET_REFUSAL, None),
}


@pytest.mark.parametrize('verb', VERBS)
@pytest.mark.parametrize(('breaks', 'message', 'ran'), list(REFUSALS.values()), ids=list(REFUSALS))
def test_each_refusal_runs_only_the_steps_before_it_and_charges_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, breaks, message: str | None, ran: int | None
):
    rig, responder = _rig(tmp_path, monkeypatch)
    breaks(rig, responder)
    result = rig.call(verb, {})
    assert result['status'] == 'refused', result
    if breaks is _stale_copy:
        assert result['message'].startswith(f'{STALE_PREFIX}tools/source_digest.sh differs'), result
    elif breaks is _two_postgres:
        assert result['message'].startswith('2 running postgres containers in the trader_joe project'), result
    else:
        assert result['message'] == message, result
    host = rig.layout.repo_host_path
    expected = admission(verb, host) if ran is None else full_run(verb, host)[:ran]
    assert _argvs(rig) == expected
    assert not [step for step in rig.docker.steps if is_dev_up_wrapper(step) or step.builds]
    if breaks is not _spent_budget:
        assert _charges_left(rig) == BUDGET, 'a refused call was charged'


# Two things broken at once: the EARLIER check of the designed order must be the one that answers.
PAIRS = {
    'D7 before S6': (_prod_container, _stale_copy, 'project trader_joe has containers not started'),
    'S6 before D6': (_stale_copy, _symlinked_bind_source, STALE_PREFIX),
    'D6 before D5': (_symlinked_bind_source, _env_file_a_directory, 'a symlink at server/data/store/migrations'),
    'D5 before the versions guard': (_env_file_a_directory, _no_revision_files, 'is not a regular file'),
    'the versions guard before the lookups': (_no_revision_files, _no_postgres, 'no revision files'),
    'postgres before data_store': (_no_postgres, _two_data_store, 'no running postgres'),
    'data_store before the image': (_two_data_store, _missing_image, 'running data_store containers'),
    'the image before the budget': (_missing_image, _spent_budget, 'call dev_build first'),
}


@pytest.mark.parametrize('verb', VERBS)
@pytest.mark.parametrize(('first', 'second', 'answer'), list(PAIRS.values()), ids=list(PAIRS))
def test_with_two_checks_failing_the_earlier_one_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, first, second, answer: str
):
    rig, responder = _rig(tmp_path, monkeypatch)
    first(rig, responder)
    second(rig, responder)
    result = rig.call(verb, {})
    assert result['status'] == 'refused' and answer in result['message'], result


@pytest.mark.parametrize('verb', VERBS)
def test_no_steering_walk_and_no_generated_code_check_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    """Review (1): neither the steering walk nor the generated-code check runs.

    Every -f is given and nothing label-only runs, so a compose file in the stack directory steers
    nothing; and nothing builds, so stale generated code does not refuse a migration (make migrate does not).
    """
    rig, _ = _rig(tmp_path, monkeypatch)
    (rig.layout.stack_dir / 'compose.yaml').write_text('services: {}\n')
    shutil.rmtree(rig.layout.repo / 'gen' / 'proto' / 'ts')
    assert rig.call(verb, {})['status'] == 'ok'


# ===============================================================================================
# dev_migrate_check ONLY: the digest, the stamp read, the stamp comparison
# ===============================================================================================

_BAD_DIGESTS = {
    'exits 1': (1, b''),
    'killed': (None, b''),
    'malformed': (0, b'not-a-digest\n'),
    'two lines': (0, f'{STORE_DIGEST}\n{STORE_DIGEST}\n'.encode()),
}


@pytest.mark.parametrize('answer', list(_BAD_DIGESTS.values()), ids=list(_BAD_DIGESTS))
def test_a_failed_digest_answers_failed_before_the_stamp_read_and_charges_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer
):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.digest = answer
    result = rig.call('dev_migrate_check', {})
    assert (result['status'], result['message']) == ('failed', DIGEST_FAILED), result
    assert _argvs(rig) == admission('dev_migrate_check', rig.layout.repo_host_path)[:5]
    assert _charges_left(rig) == BUDGET


def _cannot_verify() -> str:
    return 'CANNOT VERIFY: the image trader_joe_data_store:dev carries no usable source stamp'


_BAD_STAMPS = {
    'the read fails': ((1, b''), 'the image trader_joe_data_store:dev could not be read'),
    'the read is killed': ((None, b''), 'the image trader_joe_data_store:dev could not be read'),
    'absent (empty)': ((0, b''), _cannot_verify()),
    'absent (newline only)': ((0, b'\n'), _cannot_verify()),
    "docker's <no value>": ((0, b'<no value>\n'), _cannot_verify()),
    'uppercase hex': ((0, f'{"A" * 64}\n'.encode()), _cannot_verify()),
    'one digit short': ((0, f'{"1" * 63}\n'.encode()), _cannot_verify()),
    'one digit long': ((0, f'{"1" * 65}\n'.encode()), _cannot_verify()),
    'two stamps': ((0, f'{STORE_DIGEST}\n{STORE_DIGEST}\n'.encode()), _cannot_verify()),
    'a stamp and a word': ((0, f'{STORE_DIGEST} x\n'.encode()), _cannot_verify()),
    'mismatched': ((0, f'{"2" * 64}\n'.encode()), 'STALE IMAGE: trader_joe_data_store:dev was built from different'),
}


@pytest.mark.parametrize(('answer', 'start'), list(_BAD_STAMPS.values()), ids=list(_BAD_STAMPS))
def test_a_bad_stamp_refuses_naming_dev_build_and_runs_no_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer, start: str
):
    """verify_comparison_image without its hatch: never a pass, always a refusal, uncharged, naming dev_build."""
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.stamp = answer
    result = rig.call('dev_migrate_check', {})
    assert result['status'] == 'refused', result
    assert result['message'].startswith(start), result['message']
    assert result['message'].endswith('call dev_build'), result['message']
    assert _argvs(rig) == admission('dev_migrate_check', rig.layout.repo_host_path)
    assert _charges_left(rig) == BUDGET


def test_a_stale_stamp_names_both_digests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.stamp = (0, f'{"2" * 64}\n'.encode())
    message = rig.call('dev_migrate_check', {})['message']
    assert f'image stamp {"2" * 64}' in message and f'checkout digest {STORE_DIGEST}' in message, message


def test_dev_migrate_reads_no_stamp_and_runs_no_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """run_migrations.sh guards `check` only: an unstamped image does not refuse `upgrade head`, the remedy."""
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.stamp = (0, b'')
    assert rig.call('dev_migrate', {})['status'] == 'ok'
    assert STAMP_READ not in _argvs(rig)
    assert not [step for step in rig.docker.steps if is_dev_digest(step)]


@pytest.mark.parametrize(
    ('exit_status', 'stdout'),
    [(0, f'{STORE_DIGEST}\n'.encode()), (0, STORE_DIGEST.encode()), (0, f'  {STORE_DIGEST}  \n'.encode())],
    ids=['newline', 'bare', 'padded'],
)
def test_check_dev_image_stamp_accepts_exactly_the_matching_stamp(exit_status: int, stdout: bytes):
    stack.check_dev_image_stamp(exit_status, stdout, STORE_DIGEST)


@pytest.mark.parametrize(('answer', 'start'), list(_BAD_STAMPS.values()), ids=list(_BAD_STAMPS))
def test_check_dev_image_stamp_refuses_everything_else(answer, start: str):
    with pytest.raises(stack.Refused) as refused:
        stack.check_dev_image_stamp(answer[0], answer[1], STORE_DIGEST)
    assert str(refused.value).startswith(start), str(refused.value)


# ===============================================================================================
# FAILURES AFTER ADMISSION: charged once, the exit status surfaced
# ===============================================================================================


def test_a_migration_failing_midway_answers_failed_with_alembics_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.alembic = (1, b'', b'sqlalchemy.exc.ProgrammingError: column already exists\n')
    result = rig.call('dev_migrate', {})
    assert (result['status'], result['message'], result['exit_status']) == ('failed', MIGRATE_FAILED, 1), result
    assert 'column already exists' in result['steps'][-1]['stderr']['text']
    assert _argvs(rig) == full_run('dev_migrate', rig.layout.repo_host_path)
    assert _charges_left(rig) == BUDGET - 1


def test_a_check_that_finds_drift_answers_failed_naming_both_causes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.alembic = (255, b'', b'FAILED: New upgrade operations detected\n')
    result = rig.call('dev_migrate_check', {})
    assert (result['status'], result['exit_status']) == ('failed', 255), result
    assert result['message'].startswith('alembic check exited 255: either the schema has drifted'), result
    assert 'dev_migrate fixes' in result['message']
    assert _charges_left(rig) == BUDGET - 1


@pytest.mark.parametrize('verb', VERBS)
def test_a_failed_base_pull_runs_no_alembic_and_is_charged_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig, responder = _rig(tmp_path, monkeypatch)
    ref = stack.BASE_IMAGES[0]

    def respond(step: stack.Step) -> runner.ProcessResult:
        if step.argv in (image_inspect(ref), (DOCKER, 'pull', ref)):
            return runner.ProcessResult(1, b'', b'pull access denied')
        return responder(step)

    rig.docker.respond = respond
    result = rig.call(verb, {})
    assert result['status'] == 'failed' and result['message'].startswith(f'{verb} stopped: base image'), result
    assert not [step for step in rig.docker.steps if is_dev_up_wrapper(step)]
    assert _charges_left(rig) == BUDGET - 1


@pytest.mark.parametrize('verb', VERBS)
def test_the_budget_is_charged_once_per_call_and_the_thirteenth_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig, _ = _rig(tmp_path, monkeypatch)
    for _ in range(BUDGET):
        assert rig.call(verb, {})['status'] == 'ok'
    rig.docker.steps.clear()
    result = rig.call(verb, {})
    assert (result['status'], result['message']) == ('refused', BUDGET_REFUSAL), result
    assert _argvs(rig) == admission(verb, rig.layout.repo_host_path)


def test_the_two_verbs_share_one_budget_with_the_other_dev_verbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    for _ in range(BUDGET - 2):
        rig.agent.dev_budget.charge()
    assert rig.call('dev_migrate', {})['status'] == 'ok'
    assert rig.call('dev_migrate_check', {})['status'] == 'ok'
    assert rig.call('dev_migrate', {})['message'] == BUDGET_REFUSAL


# ===============================================================================================
# THE ARGVS: exact, built through their checks, and the checks refusing every neighbour
# ===============================================================================================


def _host() -> Path:
    return Path('/h/repo')


@pytest.mark.parametrize(
    ('builder', 'tail'),
    [(stack.dev_migrate_step, MIGRATE_TAIL), (stack.dev_migrate_check_step, CHECK_TAIL)],
    ids=['migrate', 'check'],
)
def test_the_alembic_step_builders_spell_the_wrapped_one_off(tmp_path: Path, builder, tail):
    host = Path('/home/someone/repos/trader_joe')
    step = builder(tmp_path, host)
    assert step.argv == wrapped(host, tail)
    assert step.argv[:6] == (
        'sh',
        '-c',
        WRAPPER_SCRIPT,
        'sh',
        '/opt/agent_mcp/compose/tools/dev_instance_secret.sh',
        f'{host}/.env',
    )
    assert step.argv[6:12] == (DOCKER, 'compose', '-p', PROJECT, '--project-directory', str(host))
    assert step.argv[12:22] == tuple(word for name in DEV_FILES for word in ('-f', f'/opt/agent_mcp/compose/{name}'))
    assert step.argv[22:] == tail
    assert step.cwd == tmp_path and step.builds is True and step.env == PINNED_ENV
    assert stack.DEV_MIGRATE_TAIL == MIGRATE_TAIL and stack.DEV_MIGRATE_CHECK_TAIL == CHECK_TAIL


@pytest.mark.parametrize(
    ('builder', 'check_name'),
    [
        (stack.dev_migrate_step, 'check_dev_migrate_argv'),
        (stack.dev_migrate_check_step, 'check_dev_migrate_check_argv'),
    ],
    ids=['migrate', 'check'],
)
def test_each_builder_runs_its_argv_through_its_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, builder, check_name: str
):
    """The builders hold their check by reference at definition time, so the spy goes on the shared one."""
    seen: list[tuple[str, ...]] = []
    real = stack.check_dev_lifecycle_argv

    def spy(argv, repo_host_path=None):
        seen.append(tuple(argv))
        real(argv, repo_host_path)

    monkeypatch.setattr(stack, 'check_dev_lifecycle_argv', spy)
    step = builder(tmp_path, _host())
    assert seen == [step.argv[6:]]
    getattr(stack, check_name)(step.argv, _host())


def test_the_stamp_read_and_the_image_inspect_are_these_words(tmp_path: Path):
    stamp = stack.dev_stamp_step(tmp_path)
    assert stamp.argv == STAMP_READ and stamp.cwd == tmp_path and not stamp.builds and stamp.env == ()
    image = stack.dev_data_store_image_step(tmp_path)
    assert image.argv == image_inspect(STORE_IMAGE) and image.cwd == tmp_path and not image.builds
    stack.check_dev_stamp_argv(STAMP_READ)


@pytest.mark.parametrize(
    'argv',
    [
        (*STAMP_READ[:-1], 'trader_joe_data_store:latest'),
        (*STAMP_READ[:4], '{{ index .Config.Labels "trader_joe.source.digest" }}', STORE_IMAGE),
        (*STAMP_READ, 'trader_joe_data_ingest:dev'),
        (DOCKER, 'image', 'inspect', STORE_IMAGE),
    ],
    ids=['another image', 'another format', 'a second image', 'no format'],
)
def test_the_stamp_check_refuses_any_other_read(argv):
    with pytest.raises(stack.Refused):
        stack.check_dev_stamp_argv(argv)


_GOOD = {'migrate': wrapped(_host(), MIGRATE_TAIL), 'check': wrapped(_host(), CHECK_TAIL)}
_CHECKS = {'migrate': stack.check_dev_migrate_argv, 'check': stack.check_dev_migrate_check_argv}
_TABLE = 'a dev lifecycle command is one of its fixed commands, with its fixed arguments, and no other'
_FORBIDDEN = 'a dev lifecycle command removes no volume, image or orphan'
_ROW = {
    'migrate': 'the dev migrate wrapper runs the fixed migrate command, and no other',
    'check': 'the dev migrate check wrapper runs the fixed migrate check command, and no other',
}
_HEAD = {
    'migrate': 'the dev migrate wrapper is its six fixed words and nothing else',
    'check': 'the dev migrate check wrapper is its six fixed words and nothing else',
}


def _wrapped_tail(*tail: str) -> tuple[str, ...]:
    return (*wrapper_head(_host()), *file_prefix(_host()), *tail)


# name -> (kind, argv, the refusal it must start with)
_NEIGHBOURS = {
    'upgrade to another revision': ('migrate', _wrapped_tail(*MIGRATE_TAIL[:-1], 'd7e2a9c35b18'), _TABLE),
    'upgrade +1': ('migrate', _wrapped_tail(*MIGRATE_TAIL[:-1], '+1'), _TABLE),
    'upgrade heads': ('migrate', _wrapped_tail(*MIGRATE_TAIL[:-1], 'heads'), _TABLE),
    'upgrade head --sql': ('migrate', _wrapped_tail(*MIGRATE_TAIL, '--sql'), _TABLE),
    'stamp head': ('migrate', _wrapped_tail(*MIGRATE_TAIL[:-2], 'stamp', 'head'), _TABLE),
    'downgrade -1': ('migrate', _wrapped_tail(*MIGRATE_TAIL[:-2], 'downgrade', '-1'), _TABLE),
    'downgrade base': ('migrate', _wrapped_tail(*MIGRATE_TAIL[:-2], 'downgrade', 'base'), _TABLE),
    'an extra -v in the tail': ('migrate', _wrapped_tail(*MIGRATE_TAIL[:3], '-v', *MIGRATE_TAIL[3:]), _FORBIDDEN),
    'an extra -e in the run': (
        'migrate',
        _wrapped_tail(*MIGRATE_TAIL[:3], '-e', f'{HATCH}=1', *MIGRATE_TAIL[3:]),
        _TABLE,
    ),
    'the check row under migrate': ('migrate', _GOOD['check'], _ROW['migrate']),
    'the up row under migrate': ('migrate', _wrapped_tail(*stack.DEV_UP_TAIL), _ROW['migrate']),
    'check with --autogenerate': ('check', _wrapped_tail(*CHECK_TAIL, '--autogenerate'), _TABLE),
    'check with an extra word': ('check', _wrapped_tail(*CHECK_TAIL, 'head'), _TABLE),
    'current instead of check': ('check', _wrapped_tail(*CHECK_TAIL[:-1], 'current'), _TABLE),
    'heads instead of check': ('check', _wrapped_tail(*CHECK_TAIL[:-1], 'heads'), _TABLE),
    'the migrate row under check': ('check', _GOOD['migrate'], _ROW['check']),
    'no wrapper at all (migrate)': ('migrate', _GOOD['migrate'][6:], _HEAD['migrate']),
    'no wrapper at all (check)': ('check', _GOOD['check'][6:], _HEAD['check']),
    'another env file for the helper': (
        'migrate',
        (*_GOOD['migrate'][:5], '/h/repo/server/data/store/.env', *_GOOD['migrate'][6:]),
        _HEAD['migrate'],
    ),
    'another project': (
        'check',
        (*_GOOD['check'][:9], 'trader_joe_agent_stack', *_GOOD['check'][10:]),
        'a dev lifecycle verb runs only',
    ),
    "the checkout's compose file": (
        'migrate',
        (*_GOOD['migrate'][:13], '/h/repo/docker-compose.yaml', *_GOOD['migrate'][14:]),
        'a file-based dev lifecycle command names exactly the trusted dev compose files',
    ),
}


@pytest.mark.parametrize(('kind', 'argv', 'message'), list(_NEIGHBOURS.values()), ids=list(_NEIGHBOURS))
def test_the_migrate_checks_refuse_every_neighbour(kind: str, argv: tuple[str, ...], message: str):
    with pytest.raises(stack.Refused) as refused:
        _CHECKS[kind](argv, _host())
    assert str(refused.value).startswith(message), str(refused.value)


@pytest.mark.parametrize('kind', ['migrate', 'check'])
def test_the_migrate_checks_accept_their_own_argv(kind: str):
    _CHECKS[kind](_GOOD[kind], _host())


@pytest.mark.parametrize('tail', [MIGRATE_TAIL, CHECK_TAIL], ids=['migrate', 'check'])
def test_the_up_check_still_refuses_the_migrate_rows_with_its_own_words(tail):
    """The shared _check_dev_wrapped_argv kept check_dev_up_argv's messages word for word."""
    with pytest.raises(stack.Refused) as refused:
        stack.check_dev_up_argv(_wrapped_tail(*tail), _host())
    assert str(refused.value) == 'the dev up wrapper runs the fixed up command, and no other'
    with pytest.raises(stack.Refused) as refused:
        stack.check_dev_up_argv(_wrapped_tail(*tail)[6:], _host())
    assert str(refused.value) == 'the dev up wrapper is its six fixed words and nothing else'


def test_both_rows_are_file_based_in_the_lifecycle_table():
    assert stack.DEV_LIFECYCLE_COMMANDS[MIGRATE_TAIL] == stack._DEV_FILE_BASED
    assert stack.DEV_LIFECYCLE_COMMANDS[CHECK_TAIL] == stack._DEV_FILE_BASED
    stack.check_dev_lifecycle_argv((*file_prefix(_host()), *MIGRATE_TAIL), _host())
    stack.check_dev_lifecycle_argv((*file_prefix(_host()), *CHECK_TAIL), _host())
    for tail in (MIGRATE_TAIL, CHECK_TAIL):
        with pytest.raises(stack.Refused, match='one of its fixed commands'):
            stack.check_dev_lifecycle_argv((DOCKER, 'compose', '-p', PROJECT, *tail), _host())


# ===============================================================================================
# THE SECRET: the REAL wrapper under sh, the REAL helper, a FAKE env file and a fake docker
# ===============================================================================================


def _secret_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Rig, Path]:
    """A rig whose alembic step really runs: word 4 becomes the repository's helper, word 6 a fake docker."""
    rig, _ = _rig(tmp_path, monkeypatch)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', REAL_CREATE_SUBPROCESS_EXEC)
    fakebin = tmp_path / 'fakebin'
    fakebin.mkdir()
    fake = fakebin / 'docker'
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    helper = REPO_ROOT / 'tools' / 'dev_instance_secret.sh'
    docker = rig.docker

    async def run(step: stack.Step) -> runner.ProcessResult:
        if is_dev_up_wrapper(step):
            docker.steps.append(step)
            assert step.argv[4] == '/opt/agent_mcp/compose/tools/dev_instance_secret.sh' and step.argv[6] == DOCKER
            argv = (*step.argv[:4], str(helper), step.argv[5], str(fake), *step.argv[7:])
            return await runner.run_process(replace(step, argv=argv))
        return await docker(step)

    rig.agent = runner.AgentStack(rig.layout.settings, run=run)
    return rig, fakebin


def _child_env(fakebin: Path) -> dict[str, str]:
    lines = (fakebin / 'env').read_text(encoding='utf-8').splitlines()
    return dict(line.split('=', 1) for line in lines if '=' in line)


@pytest.mark.parametrize('verb', VERBS)
def test_the_child_gets_the_derived_secret_and_the_server_keeps_none_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    monkeypatch.setenv(HATCH, '1')
    rig, fakebin = _secret_rig(tmp_path, monkeypatch)
    (rig.layout.repo / '.env').write_text(f'POSTGRES_PASS=fake-pg\nINSTANCE_WRITE_SECRET={FAKE_SECRET}\n')
    result = rig.call(verb, {})
    assert result['status'] == 'ok', result
    child = _child_env(fakebin)
    assert child['INSTANCE_WRITE_SECRET'] == FAKE_SECRET
    expected = {**runner.DOCKER_ENV, **dict(PINNED_ENV), 'INSTANCE_WRITE_SECRET': FAKE_SECRET}
    assert {key: value for key, value in child.items() if key not in ('PWD', 'SHLVL', '_')} == expected
    assert HATCH not in child, 'the escape hatch reached the one-off'
    host = rig.layout.repo_host_path
    assert (fakebin / 'argv').read_text(encoding='utf-8').splitlines() == [*file_prefix(host)[1:], *TAILS[verb]]
    kept = json.dumps(result) + '\n'.join(rig.audit_lines())
    assert FAKE_SECRET not in kept and 'fake-pg' not in kept
    assert 'fake compose: up' in result['steps'][-1]['stdout']['text'], 'the output was recorded, without the value'
    assert FAKE_SECRET not in os.environ.values()


def test_an_empty_secret_gives_unused_and_the_migration_still_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """As make migrate's compose would interpolate any value: the one-off data_store never reads it."""
    rig, fakebin = _secret_rig(tmp_path, monkeypatch)
    (rig.layout.repo / '.env').write_text('INSTANCE_WRITE_SECRET=\n')
    assert rig.call('dev_migrate', {})['status'] == 'ok'
    assert _child_env(fakebin)['INSTANCE_WRITE_SECRET'] == 'unused'


# ===============================================================================================
# THE SURFACE: schema, timeout, verb table, descriptions
# ===============================================================================================


@pytest.mark.parametrize('verb', VERBS)
def test_the_schema_is_closed_and_takes_nothing(verb: str):
    assert runner.VERB_SCHEMAS[verb] == {
        'type': 'object',
        'properties': {},
        'required': [],
        'additionalProperties': False,
    }


@pytest.mark.parametrize('verb', VERBS)
@pytest.mark.parametrize(
    'arguments',
    [{'revision': 'base'}, {HATCH: '1'}, {'allow_stale_image': True}, {'command': 'downgrade'}, ['upgrade'], 'check'],
    ids=repr,
)
def test_any_argument_is_refused_with_no_step_and_no_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, arguments
):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert rig.call(verb, arguments)['status'] == 'refused'
    assert rig.docker.steps == [] and _charges_left(rig) == BUDGET


@pytest.mark.parametrize('verb', VERBS)
def test_the_timeout_is_ten_minutes_inside_the_clients(verb: str):
    assert runner.VERB_TIMEOUT_SECONDS[verb] == 600
    config = json.loads((REPO_ROOT / '.mcp.json').read_text(encoding='utf-8'))
    assert config['mcpServers']['agent-stack']['timeout'] >= (600 + 30) * 1000
    assert max(runner.VERB_TIMEOUT_SECONDS.values()) == 1800
    assert config['mcpServers']['agent-stack']['timeout'] >= (1800 + 30) * 1000


def test_dev_migrate_says_it_writes_the_dev_database_and_the_permission_is_meant_to_be_ask():
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type] -- only the verb table is read
    assert set(VERBS) <= set(agent.verbs)
    text = agent.describe('dev_migrate')
    assert 'STATE-CHANGING' in text and "WRITES THE USER'S DEV DATABASE" in text, text
    assert 'alembic upgrade head' in text and 'run --rm --no-deps' in text, text
    assert 'meant to be ask' in text and 'call dev_build' in text, text


def test_dev_migrate_check_says_it_writes_alembic_version_and_has_no_escape_hatch():
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type] -- only the verb table is read
    text = agent.describe('dev_migrate_check')
    assert 'STATE-CHANGING' in text and 'alembic check' in text, text
    assert 'WRITES an empty alembic_version table when the dev database was never migrated' in text, text
    assert 'NO escape hatch' in text and 'dev_build' in text, text
    assert HATCH not in text, 'the description names the variable it never offers'


# ===============================================================================================
# MIRRORS: make migrate / make migrate-check, run_migrations.sh, and the compose files
# ===============================================================================================

SCRIPT = REPO_ROOT / 'server' / 'data' / 'store' / 'run_migrations.sh'


def _recipe(target: str) -> list[str]:
    text = (REPO_ROOT / 'Makefile').read_text(encoding='utf-8').replace('\\\n', ' ')
    match = re.search(rf'^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n)+)', text, re.MULTILINE)
    assert match is not None, f'the Makefile has no {target} target'
    return [line[1:].strip() for line in match.group(1).splitlines()]


def _script() -> str:
    return SCRIPT.read_text(encoding='utf-8')


def test_the_recipes_are_the_script_with_no_argument_and_with_check():
    assert _recipe('migrate') == ['./server/data/store/run_migrations.sh']
    assert _recipe('migrate-check') == ['./server/data/store/run_migrations.sh check']


def test_the_tails_are_the_scripts_one_off_with_its_default_and_check_arguments():
    """The script's last line runs `"${COMPOSE[@]}" run --rm --no-deps data_store <alembic> "${ALEMBIC_ARGS[@]}"`."""
    lines = [line for line in _script().splitlines() if line.startswith('"${COMPOSE[@]}" run ')]
    assert len(lines) == 1, lines
    words = shlex.split(lines[0], posix=False)
    assert words[0] == '"${COMPOSE[@]}"' and words[-1] == '"${ALEMBIC_ARGS[@]}"', words
    assert tuple(words[1:-1]) == MIGRATE_TAIL[:5] == CHECK_TAIL[:5]
    assert 'ALEMBIC_ARGS=(upgrade head)' in _script()
    assert MIGRATE_TAIL[5:] == ('upgrade', 'head') and CHECK_TAIL[5:] == ('check',)


def test_the_dev_selection_loads_the_first_two_of_the_mcps_files():
    """The script's dev selection loads base + override; the MCP's prefix adds only web, web.dev and agent-dev after them."""
    assert 'COMPOSE=(docker compose -f docker-compose.yaml -f docker-compose.override.yaml)' in _script()
    assert DEV_FILES[:2] == ('docker-compose.yaml', 'docker-compose.override.yaml')


def _services(name: str) -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / name).read_text(encoding='utf-8')).get('services') or {}


def test_the_extra_files_change_nothing_the_data_store_one_off_runs():
    """Review (2): the web files define only web; the MCP overlay only data_store's build.dockerfile."""
    assert set(_services('docker-compose.web.yaml')) == {'web'}
    assert set(_services('docker-compose.web.dev.yaml')) == {'web'}
    overlay = _services('docker-compose.agent-dev.yaml')
    assert overlay['data_store'] == {'build': {'dockerfile': '/opt/agent_mcp/compose/Dockerfile'}}
    web_text = (REPO_ROOT / 'docker-compose.web.yaml').read_text(encoding='utf-8')
    assert '${INSTANCE_WRITE_SECRET:?' in web_text, 'the :? guard the wrapper exists for'


def test_the_one_off_reaches_the_running_postgres_by_name_on_a_shared_network():
    """Review (3): DATABASE_URI names ${DATABASE_NAME}, postgres's container_name, on store_db and devnet."""
    base, override = _services('docker-compose.yaml'), _services('docker-compose.override.yaml')
    assert base['postgres']['container_name'] == '${DATABASE_NAME}'
    uri = [entry for entry in base['data_store']['environment'] if entry.startswith('DATABASE_URI=')]
    assert uri == [
        'DATABASE_URI=postgresql://${POSTGRES_USER}:${POSTGRES_PASS}@${DATABASE_NAME}:5432/${POSTGRES_DB_NAME}'
    ]
    postgres_networks = set(base['postgres']['networks']) | set(override['postgres']['networks'])
    store_networks = set(base['data_store']['networks']) | set(override['data_store']['networks'])
    assert {'store_db', 'devnet'} <= postgres_networks & store_networks
    assert './server/data/store/migrations:/code/migrations' in base['data_store']['volumes']


def test_the_stamp_read_and_its_rule_are_the_scripts():
    """verify_comparison_image: the same label, the same `with` guard, the same 64-hex rule, the data/store digest."""
    script = _script()
    assert "SOURCE_STAMP_LABEL='trader_joe.source.digest'" in script
    assert '{{ with .Config.Labels }}{{ index . \\"$SOURCE_STAMP_LABEL\\" }}{{ end }}' in script
    assert STAMP_FORMAT == '{{ with .Config.Labels }}{{ index . "trader_joe.source.digest" }}{{ end }}'
    assert '[[ $image_stamp =~ ^[0-9a-f]{64}$ ]]' in script
    assert 'SERVICE_PATH=data' in script and 'SERVICE_NAME=store' in script
    assert '"$REPO_ROOT/tools/source_digest.sh" "$SERVICE_PATH" "$SERVICE_NAME"' in script


def test_no_recipe_sets_the_escape_hatch():
    recipes = [
        line for line in (REPO_ROOT / 'Makefile').read_text(encoding='utf-8').splitlines() if line.startswith('\t')
    ]
    assert not [line for line in recipes if HATCH in line], 'a recipe sets the hatch the MCP must mirror the absence of'


@pytest.mark.parametrize('verb', VERBS)
def test_the_unwrapped_compose_argv_is_the_file_based_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert rig.call(verb, {})['status'] == 'ok'
    compose = dev_up_compose_argv(rig.docker.steps[-1])
    assert compose == (*file_prefix(rig.layout.repo_host_path), *TAILS[verb])
    assert compose[2:4] == ('-p', PROJECT) and compose.count('-p') == 1
