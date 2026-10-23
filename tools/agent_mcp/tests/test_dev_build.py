"""build, the file-based STATE-CHANGING build verb over the user's dev project (bead tj-iee6sn.8).

Design checked against: the architect's 17:25 UTC note on tj-iee6sn.8 (it wins over the description), its
14:59 note, the 04:08 validator finding, owner rulings D5 and D8 on tj-iee6sn.1 and ADR tj-4rr0la addenda
22-24. build is a PLAIN call (D8, timeout 1800 s, no job): it returns ok or failed itself. It reads
no env file (D5): its build step runs in the placeholder env with COMPOSE_ENV_FILES=/dev/null. It has no
prod guard, no D6 check and no steering walk (it starts nothing; every -f is given). Order, a refusal at
any point running nothing after it:

    S6 stale-copy check -> generated-code freshness -> ONE budget charge
        -> the two TRUSTED digests -> the bases (runner) -> ONE file-based build

Every call goes through the REAL AgentStack with the fake-docker rig (harness.make_rig points the real S6
check at a tmp trusted directory). Expected values are literals or parsed from the files they mirror
(the Makefile, the compose files, the MCP Dockerfile), never read from stack.py.
"""

import asyncio
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess  # nosec B404 -- the real digest script, run by the end-to-end symlink test
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from common.tests.test_ci_invariants import _make_variable
from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import REPO_ROOT, FakeDocker, Rig, default_response, is_dev_digest, make_rig
from tools.agent_mcp.tests.test_commands import _mcp_dockerfile_copies


pytestmark = pytest.mark.build_infra

DOCKER = '/usr/local/bin/docker'
PROJECT = 'trader_joe_dev'
TRUSTED = '/opt/agent_mcp/compose'
BASH = '/usr/bin/bash'
DIGEST_SCRIPT = f'{TRUSTED}/tools/source_digest.sh'
DEV_FILES = (
    'docker-compose.yaml',
    'docker-compose.override.yaml',
    'docker-compose.web.yaml',
    'docker-compose.web.dev.yaml',
    'docker-compose.agent-dev.yaml',
)
BUILD_TAIL = ('build', 'data_store', 'data_ingest', 'web')
STORE_DIGEST = '1' * 64
INGEST_DIGEST = '2' * 64
PLACEHOLDER_ENV = (
    ('POSTGRES_PASS', 'unused'),
    ('INSTANCE_WRITE_SECRET', 'unused'),
    ('APP_INTERNAL_PORT', '80'),
    ('DATABASE_NAME', 'db'),
    ('DATABASE_PORT', '5432'),
    ('DATA_STORE_PORT', '8080'),
    ('DATA_INGEST_PORT', '8081'),
    ('POSTGRES_USER', 'postgres'),
    ('POSTGRES_DB_NAME', 'postgres'),
    ('ROOT_ENV_FILE', '/dev/null'),
    ('STORE_ENV_FILE', '/dev/null'),
    ('INGEST_ENV_FILE', '/dev/null'),
    ('COMPOSE_PROJECT_NAME', PROJECT),
    ('COMPOSE_FILE', ''),
    ('COMPOSE_PATH_SEPARATOR', ':'),
    ('COMPOSE_ENV_FILES', '/dev/null'),
    ('COMPOSE_PROFILES', ''),
    ('COMPOSE_REMOVE_ORPHANS', 'false'),
    ('COMPOSE_IGNORE_ORPHANS', 'false'),
    ('COMPOSE_BAKE', 'false'),
)
BUILD_ENV = (*PLACEHOLDER_ENV, ('SOURCE_DIGEST_DATA_STORE', STORE_DIGEST), ('SOURCE_DIGEST_DATA_INGEST', INGEST_DIGEST))
OK_MESSAGE = 'the trader_joe_dev :dev images (data_store, data_ingest, web) are built and stamped'
BUILD_FAILED = 'compose build failed for the trader_joe_dev project'
BUDGET_REFUSAL = '12 state-changing dev verbs in the last hour is the budget; wait, or run the make target yourself'
STALE_PREFIX = 'the MCP image is stale: '
GENERATE_HINT = 'run make proto gen-proto-ts in the root checkout'
BUDGET = 12


def file_prefix(host: Path) -> tuple[str, ...]:
    files = [word for name in DEV_FILES for word in ('-f', f'{TRUSTED}/{name}')]
    return (DOCKER, 'compose', '-p', PROJECT, '--project-directory', str(host), *files)


def digest_argv(host: Path, service_name: str) -> tuple[str, ...]:
    return (BASH, DIGEST_SCRIPT, '--context', str(host), 'data', service_name)


def inspects() -> list[tuple[str, ...]]:
    return [(DOCKER, 'image', 'inspect', '--format', '{{.Id}}', ref) for ref in stack.BASE_IMAGES]


class Responder:
    """Each digest answers from DIGESTS; compose exits BUILD_EXIT; the rest as the harness default."""

    def __init__(self) -> None:
        self.digests: dict[str, tuple[int | None, bytes]] = {
            'store': (0, f'{STORE_DIGEST}\n'.encode()),
            'ingest': (0, f'{INGEST_DIGEST}\n'.encode()),
        }
        self.build_exit = 0

    def __call__(self, step: stack.Step) -> runner.ProcessResult:
        if is_dev_digest(step):
            exit_status, stdout = self.digests[step.argv[-1]]
            return runner.ProcessResult(exit_status, stdout, b'digest said no\n' if exit_status else b'')
        if step.argv[:2] == (DOCKER, 'compose'):
            return runner.ProcessResult(self.build_exit, b'', b'compose said no\n' if self.build_exit else b'')
        return default_response(step)


def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Rig, Responder]:
    responder = Responder()
    return make_rig(tmp_path, monkeypatch, FakeDocker(respond=responder)), responder


def _call(rig: Rig, arguments: Any = None) -> dict[str, Any]:
    return rig.call('build', {} if arguments is None else arguments)


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


def test_dev_build_runs_the_two_trusted_digests_then_the_bases_then_one_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, _ = _rig(tmp_path, monkeypatch)
    result = _call(rig)
    assert result['status'] == 'ok', result
    assert result['message'] == OK_MESSAGE
    host = rig.layout.repo_host_path
    assert [step.argv for step in rig.docker.steps] == [
        digest_argv(host, 'store'),
        digest_argv(host, 'ingest'),
        *inspects(),
        (*file_prefix(host), *BUILD_TAIL),
    ]
    assert all(step.cwd == rig.layout.stack_dir for step in rig.docker.steps), rig.docker.steps


def test_the_digests_reach_only_the_build_steps_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The placeholder env plus the two stamps, on the build alone; the digest and base steps get none."""
    rig, _ = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    *before, build = rig.docker.steps
    assert build.env == BUILD_ENV, build.env
    assert all(step.env == () for step in before), [step.env for step in before]


def test_no_digest_and_no_placeholder_appears_in_a_recorded_command_or_the_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, _ = _rig(tmp_path, monkeypatch)
    result = _call(rig)
    commands = [record['command'] for record in result['steps']]
    host = rig.layout.repo_host_path
    assert commands[-1] == shlex.join((*file_prefix(host), *BUILD_TAIL))
    for word in (STORE_DIGEST, INGEST_DIGEST, 'SOURCE_DIGEST', 'INSTANCE_WRITE_SECRET', 'COMPOSE_'):
        assert not [command for command in commands if word in command], (word, commands)
        assert not [line for line in rig.audit_lines() if word in line], word


def test_the_digest_steps_build_nothing_and_the_build_step_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    flags = {step.argv: step.builds for step in rig.docker.steps}
    host = rig.layout.repo_host_path
    assert flags[digest_argv(host, 'store')] is False and flags[digest_argv(host, 'ingest')] is False
    assert flags[(*file_prefix(host), *BUILD_TAIL)] is True


def test_dev_build_returns_its_outcome_itself_and_starts_no_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """D8: a plain call. ok or failed, never 'started', and no job id to poll."""
    rig, responder = _rig(tmp_path, monkeypatch)
    ok = _call(rig)
    responder.build_exit = 1
    failed = _call(rig)
    assert (ok['status'], failed['status']) == ('ok', 'failed'), (ok, failed)
    assert failed['message'] == BUILD_FAILED
    for result in (ok, failed):
        assert 'job' not in json.dumps(result).lower(), result
    assert [step.argv[-4:] for step in rig.docker.steps if step.argv[:2] == (DOCKER, 'compose')] == [BUILD_TAIL] * 2


def test_the_build_step_builder_spells_the_file_based_build_and_flags_it(tmp_path: Path):
    host = Path('/home/someone/repos/trader_joe')
    step = stack.dev_build_step(tmp_path, host, [STORE_DIGEST, INGEST_DIGEST])
    assert step.argv == (*file_prefix(host), *BUILD_TAIL)
    assert step.cwd == tmp_path and step.builds is True and step.stdout_cap is None
    assert step.env == BUILD_ENV
    assert not {'--pull', '--no-cache', '--progress', '--push', '-q'} & set(step.argv)


@pytest.mark.parametrize(
    'digests',
    [
        [],
        [STORE_DIGEST],
        [STORE_DIGEST, INGEST_DIGEST, STORE_DIGEST],
        [STORE_DIGEST, 'A' * 64],
        [STORE_DIGEST, '2' * 63],
        [STORE_DIGEST, '2' * 65],
        [STORE_DIGEST, f'{INGEST_DIGEST}\n'],
        [STORE_DIGEST, ''],
    ],
    ids=['none', 'one', 'three', 'uppercase', '63-hex', '65-hex', 'newline', 'empty'],
)
def test_the_build_step_builder_refuses_anything_but_two_valid_digests(tmp_path: Path, digests: list[str]):
    with pytest.raises(stack.Refused, match='exactly one valid source digest per image'):
        stack.dev_build_step(tmp_path, Path('/h'), digests)


# ===============================================================================================
# THE DIGEST STEPS
# ===============================================================================================


def test_the_digest_steps_are_the_trusted_script_over_the_host_path(tmp_path: Path):
    host = Path('/home/someone/repos/trader_joe')
    steps = stack.dev_digest_steps(tmp_path, host)
    assert [step.argv for step in steps] == [digest_argv(host, 'store'), digest_argv(host, 'ingest')]
    assert all(step.cwd == tmp_path and step.env == () and step.builds is False for step in steps), steps


def test_each_digest_argv_is_accepted_word_for_word():
    host = Path('/h/trader_joe')
    for service in ('store', 'ingest'):
        stack.check_dev_digest_argv(digest_argv(host, service), host)
        stack.check_dev_digest_argv(list(digest_argv(host, service)), host)


_TAMPERED = {
    "the checkout's script": (
        BASH,
        '/h/trader_joe/tools/source_digest.sh',
        '--context',
        '/h/trader_joe',
        'data',
        'store',
    ),
    'sh, not bash': ('/bin/sh', DIGEST_SCRIPT, '--context', '/h/trader_joe', 'data', 'store'),
    'bash from PATH': ('bash', DIGEST_SCRIPT, '--context', '/h/trader_joe', 'data', 'store'),
    'no --context': (BASH, DIGEST_SCRIPT, 'data', 'store'),
    'another context': (BASH, DIGEST_SCRIPT, '--context', '/h/elsewhere', 'data', 'store'),
    'a parent context': (BASH, DIGEST_SCRIPT, '--context', '/h', 'data', 'store'),
    'a list mode': (BASH, DIGEST_SCRIPT, '--context', '/h/trader_joe', '--list-files', 'data', 'store'),
    'another service': (BASH, DIGEST_SCRIPT, '--context', '/h/trader_joe', 'data', 'web'),
    'swapped operands': (BASH, DIGEST_SCRIPT, '--context', '/h/trader_joe', 'store', 'data'),
    'an extra word': (BASH, DIGEST_SCRIPT, '--context', '/h/trader_joe', 'data', 'store', 'x'),
    'a word short': (BASH, DIGEST_SCRIPT, '--context', '/h/trader_joe', 'data'),
    'bash -c': (BASH, '-c', 'echo 0', DIGEST_SCRIPT, '--context', '/h/trader_joe', 'data', 'store'),
    'empty': (),
}


@pytest.mark.parametrize('argv', list(_TAMPERED.values()), ids=list(_TAMPERED))
def test_a_tampered_digest_argv_is_refused(argv: tuple[str, ...]):
    with pytest.raises(
        stack.Refused, match=re.escape('the trusted source_digest.sh over the repository, and nothing else')
    ):
        stack.check_dev_digest_argv(argv, Path('/h/trader_joe'))


def test_the_digest_builder_runs_its_argv_through_the_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """dev_digest_steps calls check_dev_digest_argv on each argv it returns.

    The check's allowed set is built from dev_digest_argv itself, so the exact-argv pins above, from
    literals, are what hold the words; this holds the wiring.
    """
    checked = []
    monkeypatch.setattr(stack, 'check_dev_digest_argv', lambda argv, host: checked.append((tuple(argv), host)))
    host = Path('/h/trader_joe')
    stack.dev_digest_steps(tmp_path, host)
    assert checked == [(digest_argv(host, 'store'), host), (digest_argv(host, 'ingest'), host)]


_GOOD = f'{STORE_DIGEST}\n'.encode()
_BAD_DIGESTS = {
    'non-zero exit': (1, _GOOD),
    'exit 2 with a digest': (2, _GOOD),
    'killed at the timeout': (None, _GOOD),
    'empty': (0, b''),
    '63 hex': (0, b'a' * 63 + b'\n'),
    '65 hex': (0, b'a' * 65 + b'\n'),
    'uppercase hex': (0, b'A' * 64 + b'\n'),
    'not hex': (0, b'g' * 64 + b'\n'),
    'no trailing newline': (0, b'a' * 64),
    'CRLF': (0, b'a' * 64 + b'\r\n'),
    'two newlines': (0, b'a' * 64 + b'\n\n'),
    'a leading newline': (0, b'\n' + b'a' * 64 + b'\n'),
    'a leading space': (0, b' ' + b'a' * 64 + b'\n'),
    'extra output after': (0, b'a' * 64 + b'\nextra\n'),
    'extra output before': (0, b'note\n' + b'a' * 64 + b'\n'),
    'two digests': (0, b'a' * 64 + b'\n' + b'b' * 64 + b'\n'),
    'not ascii': (0, 'á'.encode() * 32 + b'\n'),
}


@pytest.mark.parametrize(('exit_status', 'stdout'), list(_BAD_DIGESTS.values()), ids=list(_BAD_DIGESTS))
def test_parse_source_digest_takes_exactly_one_line_of_64_lowercase_hex(exit_status, stdout: bytes):
    assert stack.parse_source_digest(exit_status, stdout) is None


def test_parse_source_digest_returns_the_digest_without_its_newline():
    assert stack.parse_source_digest(0, _GOOD) == STORE_DIGEST
    assert stack.parse_source_digest(0, ('0123456789abcdef' * 4 + '\n').encode()) == '0123456789abcdef' * 4


@pytest.mark.parametrize(('exit_status', 'stdout'), list(_BAD_DIGESTS.values()), ids=list(_BAD_DIGESTS))
@pytest.mark.parametrize('failing', ['store', 'ingest'])
def test_a_bad_digest_fails_the_call_before_any_base_or_build_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str, exit_status, stdout: bytes
):
    """tj-yb1bxj: an unstamped image is the quiet failure. Only the digest steps up to the bad one ran."""
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.digests[failing] = (exit_status, stdout)
    result = _call(rig)
    assert result['status'] == 'failed', result
    assert result['message'] == f'the source digest failed (data {failing}); nothing was built'
    host = rig.layout.repo_host_path
    ran = [digest_argv(host, 'store')] + ([digest_argv(host, 'ingest')] if failing == 'ingest' else [])
    assert [step.argv for step in rig.docker.steps] == ran
    assert [record['command'] for record in result['steps']] == [shlex.join(argv) for argv in ran]


def test_the_real_digest_argv_run_by_the_runner_gives_the_in_tree_digest(tmp_path: Path):
    """The argv the MCP runs, through the real runner, stamps what make dev-build stamps.

    runner.run_process (DOCKER_ENV, no shell), with the trusted script and Dockerfile copied to a tmp
    trusted layout in place of /opt/agent_mcp/compose.
    """
    trusted = tmp_path / 'compose'
    (trusted / 'tools').mkdir(parents=True)
    shutil.copy2(REPO_ROOT / 'tools' / 'source_digest.sh', trusted / 'tools' / 'source_digest.sh')
    shutil.copyfile(REPO_ROOT / 'Dockerfile', trusted / 'Dockerfile')
    for step in stack.dev_digest_steps(tmp_path, REPO_ROOT):
        argv = (step.argv[0], str(trusted / 'tools' / 'source_digest.sh'), *step.argv[2:])
        mine = asyncio.run(runner.run_process(stack.Step(argv, step.cwd)))
        in_tree = asyncio.run(
            runner.run_process(stack.Step((str(REPO_ROOT / 'tools' / 'source_digest.sh'), *step.argv[-2:]), tmp_path))
        )
        assert mine.exit_status == 0, mine.stderr
        assert stack.parse_source_digest(mine.exit_status, mine.stdout) == stack.parse_source_digest(
            in_tree.exit_status, in_tree.stdout
        )
        assert stack.parse_source_digest(mine.exit_status, mine.stdout) is not None


# tj-iee6sn.15 TESTS (v): build end to end over a checkout with a symlinked COPY-source component.
# The digest steps run the REAL script (a trusted tmp copy standing in for /opt/agent_mcp/compose)
# with the argv build built, over the rig's root checkout at its host path; every docker step is
# the fake. make_rig forbids real subprocesses by patching the module attributes, so the real run is
# taken from here, before any rig exists.
_REAL_SUBPROCESS_RUN = subprocess.run


class _RealDigestResponder(Responder):
    """Responder, except that each digest step runs the real trusted script with build's argv."""

    def __init__(self, trusted_script: Path) -> None:
        super().__init__()
        self.trusted_script = trusted_script

    def __call__(self, step: stack.Step) -> runner.ProcessResult:
        if not is_dev_digest(step):
            return super().__call__(step)
        argv = (step.argv[0], str(self.trusted_script), *step.argv[2:])
        done = _REAL_SUBPROCESS_RUN(  # nosec B603 -- the repository's own script, argument list, no shell
            argv, cwd=step.cwd, env=dict(runner.DOCKER_ENV), capture_output=True, check=False, timeout=120
        )
        return runner.ProcessResult(done.returncode, done.stdout, done.stderr)


def _real_digest_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    trusted = tmp_path / 'compose'
    (trusted / 'tools').mkdir(parents=True)
    shutil.copy2(REPO_ROOT / 'tools' / 'source_digest.sh', trusted / 'tools' / 'source_digest.sh')
    shutil.copyfile(REPO_ROOT / 'Dockerfile', trusted / 'Dockerfile')
    rig = make_rig(
        tmp_path / 'rig', monkeypatch, FakeDocker(respond=_RealDigestResponder(trusted / 'tools' / 'source_digest.sh'))
    )
    shutil.copyfile(REPO_ROOT / '.dockerignore', rig.layout.repo / '.dockerignore')
    return rig


def test_dev_build_over_a_clean_checkout_with_the_real_digest_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Non-vacuity for the test below: the same rig with no link gets two real digests and one build."""
    rig = _real_digest_rig(tmp_path, monkeypatch)
    result = _call(rig)
    assert result['status'] == 'ok', result
    assert [step.argv[:2] for step in rig.docker.steps][-1] == (DOCKER, 'compose')


@pytest.mark.parametrize('component', ['server', 'server/data/store/app', 'uv.lock'])
def test_dev_build_over_a_symlinked_copy_source_fails_and_runs_no_base_or_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
):
    rig = _real_digest_rig(tmp_path, monkeypatch)
    repo = rig.layout.repo
    moved = tmp_path / 'elsewhere' / component
    moved.parent.mkdir(parents=True)
    shutil.move(repo / component, moved)
    (repo / component).symlink_to(moved, target_is_directory=moved.is_dir())
    result = _call(rig)
    assert result['status'] == 'failed', result
    assert result['message'] == 'the source digest failed (data store); nothing was built'
    host = rig.layout.repo_host_path
    assert [step.argv for step in rig.docker.steps] == [digest_argv(host, 'store')], 'a base or build step ran'
    (record,) = result['steps']
    assert record['exit_status'] == 4 and record['stdout']['text'] == '', record
    assert f"passes through a symlink at '{component}'" in record['stderr']['text'], record


# ===============================================================================================
# THE ORDER: S6, then freshness, then the budget; a refusal runs and charges nothing
# ===============================================================================================


def _stale_trusted_copy(rig: Rig) -> None:
    path = rig.layout.trusted_dir / 'docker-compose.web.dev.yaml'
    path.write_bytes(path.read_bytes() + b'# drift\n')


def test_a_stale_trusted_copy_refuses_with_no_step_and_no_charge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    _stale_trusted_copy(rig)
    result = _call(rig)
    assert result['status'] == 'refused', result
    assert result['message'].startswith(f'{STALE_PREFIX}docker-compose.web.dev.yaml differs'), result
    assert 'make agent-mcp-rebuild' in result['message']
    assert rig.docker.steps == [] and _charges_left(rig) == BUDGET


@pytest.mark.parametrize('name', [checkout for checkout, _ in stack.DEV_TRUSTED_COPIES])
def test_every_compared_copy_is_checked_by_dev_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str):
    rig, _ = _rig(tmp_path, monkeypatch)
    (rig.layout.repo / name).write_bytes(b'# changed in the checkout\n')
    result = _call(rig)
    assert result['status'] == 'refused' and result['message'].startswith(f'{STALE_PREFIX}{name} differs'), result
    assert rig.docker.steps == []


def test_the_stale_copy_check_comes_before_freshness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    _stale_trusted_copy(rig)
    shutil.rmtree(rig.layout.repo / 'gen' / 'proto' / 'ts')
    assert _call(rig)['message'].startswith(STALE_PREFIX)


def _set_tree_mtime(root: Path, when: float) -> None:
    for directory, _, names in os.walk(root):
        for name in names:
            os.utime(Path(directory) / name, (when, when), follow_symlinks=False)


def _now() -> float:
    return time.time()


def _missing_python(repo: Path) -> None:
    shutil.rmtree(repo / 'gen' / 'proto' / 'python')


def _missing_ts(repo: Path) -> None:
    shutil.rmtree(repo / 'gen' / 'proto' / 'ts')


def _empty_python(repo: Path) -> None:
    shutil.rmtree(repo / 'gen' / 'proto' / 'python')
    (repo / 'gen' / 'proto' / 'python' / 'trader_joe' / 'proto').mkdir(parents=True)


def _empty_ts(repo: Path) -> None:
    shutil.rmtree(repo / 'gen' / 'proto' / 'ts')
    (repo / 'gen' / 'proto' / 'ts').mkdir()


def _python_a_file_not_a_dir(repo: Path) -> None:
    shutil.rmtree(repo / 'gen' / 'proto' / 'python')
    (repo / 'gen' / 'proto' / 'python').write_text('not a tree\n')


def _python_older(repo: Path) -> None:
    _set_tree_mtime(repo / 'gen' / 'proto' / 'python', _now() - 7200)


def _ts_older(repo: Path) -> None:
    _set_tree_mtime(repo / 'gen' / 'proto' / 'ts', _now() - 7200)


def _a_deep_proto_newer(repo: Path) -> None:
    deep = repo / 'proto' / 'trader_joe' / 'proto' / 'ui' / 'v1' / 'data.proto'
    deep.parent.mkdir(parents=True)
    deep.write_text('syntax = "proto3";\n')
    os.utime(deep, (_now() + 3600, _now() + 3600))


def _stale_ts_with_a_fresh_symlink(repo: Path, outside: Path) -> None:
    _ts_older(repo)
    outside.write_text('fresh\n')
    (repo / 'gen' / 'proto' / 'ts' / 'planted.ts').symlink_to(outside)


_STALE_GENERATED = {
    'gen/proto/python missing': (_missing_python, 'gen/proto/python is missing or empty'),
    'gen/proto/ts missing (a fresh checkout)': (_missing_ts, 'gen/proto/ts is missing or empty'),
    'gen/proto/python empty': (_empty_python, 'gen/proto/python is missing or empty'),
    'gen/proto/ts empty': (_empty_ts, 'gen/proto/ts is missing or empty'),
    'gen/proto/python a file': (_python_a_file_not_a_dir, 'gen/proto/python is missing or empty'),
    'gen/proto/python older than the proto': (_python_older, 'gen/proto/python is older than the newest .proto file'),
    'gen/proto/ts older than the proto': (_ts_older, 'gen/proto/ts is older than the newest .proto file'),
    'a nested proto newer than both': (_a_deep_proto_newer, 'gen/proto/python is older than the newest .proto file'),
}


@pytest.mark.parametrize(('make_stale', 'reason'), list(_STALE_GENERATED.values()), ids=list(_STALE_GENERATED))
def test_missing_or_old_generated_code_refuses_naming_the_fix_with_no_step_and_no_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_stale, reason: str
):
    rig, _ = _rig(tmp_path, monkeypatch)
    make_stale(rig.layout.repo)
    result = _call(rig)
    assert result['status'] == 'refused', result
    assert result['message'] == f'{reason}: {GENERATE_HINT}'
    assert rig.docker.steps == [] and _charges_left(rig) == BUDGET


def test_a_symlink_to_a_fresh_file_does_not_make_a_stale_tree_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    _stale_ts_with_a_fresh_symlink(rig.layout.repo, tmp_path / 'outside.ts')
    result = _call(rig)
    assert result['message'] == f'gen/proto/ts is older than the newest .proto file: {GENERATE_HINT}', result
    assert rig.docker.steps == []


def _no_proto_at_all(repo: Path) -> None:
    shutil.rmtree(repo / 'proto')


def _same_second(repo: Path) -> None:
    when = _now() - 60
    for tree in ('gen/proto/python', 'gen/proto/ts', 'proto'):
        _set_tree_mtime(repo / tree, when)


def _a_newer_file_that_is_not_a_proto(repo: Path) -> None:
    other = repo / 'proto' / 'buf.yaml'
    other.write_text('version: v2\n')
    os.utime(other, (_now() + 3600, _now() + 3600))


def _only_a_nested_generated_file(repo: Path) -> None:
    shutil.rmtree(repo / 'gen' / 'proto' / 'ts')
    nested = repo / 'gen' / 'proto' / 'ts' / 'trader_joe' / 'proto' / 'ui' / 'v1' / 'data_pb.ts'
    nested.parent.mkdir(parents=True)
    nested.write_text('export {};\n')


_FRESH_ENOUGH = {
    'no proto at all': _no_proto_at_all,
    'generated in the same second': _same_second,
    'a newer non-proto file in proto/': _a_newer_file_that_is_not_a_proto,
    'one nested generated file': _only_a_nested_generated_file,
}


@pytest.mark.parametrize('make_fresh', list(_FRESH_ENOUGH.values()), ids=list(_FRESH_ENOUGH))
def test_generated_code_no_older_than_the_protos_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fresh
):
    """No false refusal in normal use: equal mtimes, no protos, unrelated files, a deep tree."""
    rig, _ = _rig(tmp_path, monkeypatch)
    make_fresh(rig.layout.repo)
    assert _call(rig)['status'] == 'ok'


def test_the_freshness_check_runs_before_the_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """With the budget spent, a stale tree is still refused for being stale: the budget comes after."""
    rig, _ = _rig(tmp_path, monkeypatch)
    for _ in range(BUDGET):
        rig.agent.dev_budget.charge()
    _missing_ts(rig.layout.repo)
    assert _call(rig)['message'] == f'gen/proto/ts is missing or empty: {GENERATE_HINT}'


def test_a_spent_budget_refuses_with_no_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    for _ in range(BUDGET):
        rig.agent.dev_budget.charge()
    result = _call(rig)
    assert result['status'] == 'refused' and result['message'] == BUDGET_REFUSAL, result
    assert rig.docker.steps == []


def test_an_admitted_call_is_charged_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert _call(rig)['status'] == 'ok'
    assert _charges_left(rig) == BUDGET - 1


def test_a_failed_digest_is_charged_once_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The budget is charged once admitted, before the digests, so a failing digest counts.

    A looping agent whose digest fails is still stopped after twelve calls (the 17:25 note: 'budget
    charged only once admitted').
    """
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.digests['store'] = (1, b'')
    assert _call(rig)['status'] == 'failed'
    assert _charges_left(rig) == BUDGET - 1


def test_twelve_builds_an_hour_and_the_thirteenth_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert [_call(rig)['status'] for _ in range(BUDGET)] == ['ok'] * BUDGET
    steps = len(rig.docker.steps)
    assert _call(rig)['message'] == BUDGET_REFUSAL
    assert len(rig.docker.steps) == steps


def test_the_budget_is_shared_with_the_label_only_verbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    for _ in range(BUDGET):
        assert rig.call('down', {})['status'] == 'ok'
    assert _call(rig)['message'] == BUDGET_REFUSAL


def test_a_failed_base_pull_runs_no_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)

    def respond(step: stack.Step) -> runner.ProcessResult:
        if step.argv[1:3] in (('image', 'inspect'), ('pull', stack.BASE_IMAGES[0])):
            return runner.ProcessResult(1, b'', b'no')
        return responder(step)

    rig.docker.respond = respond
    result = _call(rig)
    assert result['status'] != 'ok', result
    assert not [step for step in rig.docker.steps if step.argv[:2] == (DOCKER, 'compose')]


# ===============================================================================================
# THE SURFACE: schema, timeout, verb table
# ===============================================================================================


def test_the_schema_is_closed_and_takes_nothing():
    assert runner.VERB_SCHEMAS['build'] == {
        'type': 'object',
        'properties': {},
        'required': [],
        'additionalProperties': False,
    }


@pytest.mark.parametrize(
    'arguments', [{'service': 'web'}, {'no_cache': True}, {'progress': 'plain'}, ['build'], 'build'], ids=repr
)
def test_any_argument_is_refused_with_no_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert _call(rig, arguments)['status'] == 'refused'
    assert rig.docker.steps == [] and _charges_left(rig) == BUDGET


def test_the_timeout_is_thirty_minutes_inside_the_clients():
    """D8 (slice 0, tj-iee6sn.4): the client's per-server timeout outlasts the verb's, with a margin."""
    assert runner.VERB_TIMEOUT_SECONDS['build'] == 1800
    config = json.loads((REPO_ROOT / '.mcp.json').read_text(encoding='utf-8'))
    client_ms = config['mcpServers']['agent-stack']['timeout']
    assert client_ms >= (1800 + 30) * 1000, client_ms
    assert max(runner.VERB_TIMEOUT_SECONDS.values()) == 1800, runner.VERB_TIMEOUT_SECONDS


def test_dev_build_is_a_verb_of_the_server():
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type] -- only the verb table is read
    assert 'build' in agent.verbs


# ===============================================================================================
# MIRRORS: make dev-build, the compose files, the MCP image
# ===============================================================================================


def _with_source_stamp() -> str:
    text = (REPO_ROOT / 'Makefile').read_text(encoding='utf-8')
    match = re.search(r'^define WITH_SOURCE_STAMP\n(.*?)^endef$', text, re.MULTILINE | re.DOTALL)
    assert match is not None, 'the Makefile defines no WITH_SOURCE_STAMP'
    return match.group(1)


def _makefile_stamps() -> list[tuple[str, tuple[str, str]]]:
    """(variable, (service path, service name)) for each digest WITH_SOURCE_STAMP computes, in order."""
    found = re.findall(r'(\w+)="\$\$\(\./tools/source_digest\.sh (\S+) (\S+)\)"', _with_source_stamp())
    return [(name, (path, service)) for name, path, service in found]


def test_dev_build_stamps_the_variables_make_dev_build_stamps_from_the_same_services():
    """MIRROR: WITH_SOURCE_STAMP's assignments are the MCP's digest pairs, variable for variable."""
    stamps = _makefile_stamps()
    assert stamps == [
        ('SOURCE_DIGEST_DATA_STORE', ('data', 'store')),
        ('SOURCE_DIGEST_DATA_INGEST', ('data', 'ingest')),
    ]
    assert stamps == list(zip(stack.DEV_DIGEST_VARIABLES, stack.DEV_DIGEST_SERVICES, strict=True))
    export = re.search(r'^\s*export (.*?);?\s*$', _with_source_stamp(), re.MULTILINE)
    assert export is not None and export.group(1).split() == [name for name, _ in stamps]


def test_make_dev_build_builds_exactly_the_services_dev_build_names():
    """MIRROR: make dev-build and build build the same three services.

    `$(WITH_SOURCE_STAMP) $(DEV_COMPOSE) build` names no service, so it builds every service with a
    build: key in DEV_COMPOSE's files; build names them.
    """
    text = (REPO_ROOT / 'Makefile').read_text(encoding='utf-8')
    recipe = re.search(r'^dev-build:.*\n\t(.*)$', text, re.MULTILINE)
    assert recipe is not None and recipe.group(1).strip() == '$(WITH_SOURCE_STAMP) $(DEV_COMPOSE) build', recipe
    words = shlex.split(_make_variable('DEV_COMPOSE'))
    files = [value for flag, value in itertools.pairwise(words) if flag == '-f']
    built = set()
    for name in files:
        services = yaml.safe_load((REPO_ROOT / name).read_text(encoding='utf-8')).get('services') or {}
        built |= {service for service, spec in services.items() if isinstance(spec, dict) and 'build' in spec}
    assert built == {'data_store', 'data_ingest', 'web'}, built
    assert set(stack.DEV_BUILD_TAIL[1:]) == built and stack.DEV_BUILD_TAIL[0] == 'build'
    # build's -f list is make's plus the MCP-only overlay that points build.dockerfile at the trusted copy.
    assert (*files, 'docker-compose.agent-dev.yaml') == stack.DEV_COMPOSE_FILES == DEV_FILES


def test_each_stamp_variable_is_the_one_compose_passes_to_that_services_build():
    """MIRROR: docker-compose.yaml hands SOURCE_DIGEST_<X> to the service whose SERVICE_PATH/NAME the digest names."""
    services = yaml.safe_load((REPO_ROOT / 'docker-compose.yaml').read_text(encoding='utf-8'))['services']
    by_pair = {}
    for spec in services.values():
        args = (spec.get('build') or {}).get('args') or {} if isinstance(spec, dict) else {}
        if 'SOURCE_DIGEST' in args:
            by_pair[(args['SERVICE_PATH'], args['SERVICE_NAME'])] = args['SOURCE_DIGEST']
    assert by_pair == {
        pair: f'${{{variable}:-}}'
        for variable, pair in zip(stack.DEV_DIGEST_VARIABLES, stack.DEV_DIGEST_SERVICES, strict=True)
    }, by_pair


def test_make_dev_build_regenerates_what_dev_build_requires_fresh():
    """MIRROR: the refusal's fix, `make proto gen-proto-ts`, is make dev-build's own prerequisites."""
    text = (REPO_ROOT / 'Makefile').read_text(encoding='utf-8')
    prerequisites = re.search(r'^dev-build:([^#\n]*)', text, re.MULTILINE)
    assert prerequisites is not None
    assert {'proto', 'gen-proto-ts'} <= set(prerequisites.group(1).split()), prerequisites.group(1)
    assert stack.DEV_GENERATED_DIRS == ('gen/proto/python', 'gen/proto/ts')
    assert _make_variable('PROTO_GEN') == 'gen/proto/python'


def test_the_mcp_image_bakes_no_dockerignore_beside_the_trusted_dockerfile():
    """(v) No <Dockerfile>.dockerignore is baked, so the build honours the checkout's, as the digest does.

    BuildKit reads <Dockerfile>.dockerignore beside the Dockerfile before the context's .dockerignore.
    None is baked beside /opt/agent_mcp/compose/Dockerfile, and every dev service builds from that
    Dockerfile (the agent-dev overlay).
    """
    landed = set()
    for sources, destination in _mcp_dockerfile_copies():
        for source in sources:
            landed.add(f'{destination.rstrip("/")}/{Path(source).name}' if destination.endswith('/') else destination)
    assert f'{TRUSTED}/Dockerfile' in landed, landed
    assert not [path for path in landed if path.startswith(TRUSTED) and path.endswith('.dockerignore')], landed
    overlay = yaml.safe_load((REPO_ROOT / 'docker-compose.agent-dev.yaml').read_text(encoding='utf-8'))['services']
    assert {service: spec['build']['dockerfile'] for service, spec in overlay.items()} == dict.fromkeys(
        BUILD_TAIL[1:], f'{TRUSTED}/Dockerfile'
    )


# ===============================================================================================
# D5: the dev files must LOAD under the placeholder env alone
# ===============================================================================================

_REFERENCE = re.compile(r'\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)')


class _Unset(Exception):
    pass


def _interpolate(value: str, env: dict[str, str]) -> str:
    """Compose's interpolation for the forms the dev files use: $$, ${X}, ${X:-d}, ${X-d}, ${X:?m}, ${X?m}, $X."""

    def one(match: re.Match) -> str:
        if match.group(0) == '$$':
            return '$'
        name = match.group(1) or match.group(4)
        operator, word = match.group(2), match.group(3) or ''
        present, value_set = name in env, env.get(name, '')
        if operator == ':-':
            return value_set if value_set else word
        if operator == '-':
            return value_set if present else word
        if operator in (':?', '?') and (not present or (operator == ':?' and not value_set)):
            raise _Unset(f'{name}: {word}')
        return value_set

    return _REFERENCE.sub(one, value)


def _walk(node: Any, env: dict[str, str]) -> Any:
    if isinstance(node, str):
        return _interpolate(node, env)
    if isinstance(node, list):
        return [_walk(item, env) for item in node]
    if isinstance(node, dict):
        return {key: _walk(item, env) for key, item in node.items()}
    return node


def _short_port_container_part(spec: str) -> str:
    """The container port of a short-syntax ports entry ([ip:][host:]container[/proto])."""
    return spec.split(':')[-1].split('/')[0]


def test_the_dev_files_interpolate_to_loadable_ports_under_the_placeholder_env():
    """D5: with no env file read, every port of the dev project must still parse.

    compose loads the WHOLE dev project from the placeholder env alone -- every service, the ones it
    does not build included -- and parses every short-syntax port. An empty container port (`127.0.0.1:${DATA_STORE_PORT}:${APP_INTERNAL_PORT}` with neither set)
    is refused by compose's port parser (docker/go-connections nat.ParsePortSpec, 'no port
    specified'), so the build would fail before it began. The `:?` guards are covered by the
    placeholders (test_dev_guards.py); this is the rest of the interpolation.
    """
    env = {**runner.DOCKER_ENV, **dict(stack.dev_placeholder_step_env())}
    empty = []
    for name in DEV_FILES:
        services = _walk(yaml.safe_load((REPO_ROOT / name).read_text(encoding='utf-8')), env).get('services') or {}
        for service, spec in services.items():
            for port in (spec or {}).get('ports') or []:
                if isinstance(port, str) and not _short_port_container_part(port):
                    empty.append(f'{name}: {service}: {port!r}')
    assert empty == [], f'ports with an empty container port under the placeholder env: {empty}'


_PORT = re.compile(r'^\d+(?:-\d+)?$')
_NAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')
# compose-spec's own pattern for container_name, the one the live refusal quoted (tj-iee6sn.30): `+`,
# so a one-character name is refused too.
_CONTAINER_NAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]+$')
_IMAGE = re.compile(r'^[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*(?::[\w][\w.-]{0,127})?$')
_IP = re.compile(r'^(?:\d{1,3}\.){3}\d{1,3}$')
# Rendered EMPTY under the build step's env in a position compose reads structurally, each argued safe.
# EMPTY ON PURPOSE. It held ('docker-compose.yaml', 'postgres', 'container_name') on the argument that ''
# is the Go zero value; the tj-iee6sn.11 sitting refuted it: compose validates the file against its
# schema first and refuses '' ("does not match pattern '[a-zA-Z0-9][a-zA-Z0-9_.-]+'"), tj-iee6sn.30.
# An entry here needs live compose evidence, not an argument.
_ACCEPTED_EMPTY: set[tuple[str, str, str]] = set()


def _short_port_problems(spec: str) -> list[str]:
    """[ip:][host:]container[/proto]: the container port is required; host and ip may be empty."""
    parts = spec.split('/')[0].split(':')
    if len(parts) > 3 or not _PORT.match(parts[-1]):
        return [f'container port {parts[-1]!r}']
    problems = []
    if len(parts) >= 2 and parts[-2] and not _PORT.match(parts[-2]):
        problems.append(f'host port {parts[-2]!r}')
    if len(parts) == 3 and parts[0] and not _IP.match(parts[0]):
        problems.append(f'host ip {parts[0]!r}')
    return problems


def _short_volume_problems(spec: str) -> list[str]:
    parts = spec.split(':')
    if len(parts) < 2 or not parts[0] or not parts[1].startswith('/'):
        return [f'volume {spec!r}']
    return []


def _service_problems(spec: dict[str, Any]) -> list[tuple[str, str]]:
    """(key, problem) for every structural value of one rendered service compose could not take."""
    found: list[tuple[str, str]] = []
    for key in ('image', 'container_name', 'hostname'):
        if key in spec:
            value = str(spec[key])
            pattern = {'image': _IMAGE, 'container_name': _CONTAINER_NAME}.get(key, _NAME)
            if not pattern.match(value):
                found.append((key, f'{value!r}'))
    for port in spec.get('ports') or []:
        if isinstance(port, dict):
            if not _PORT.match(str(port.get('target', ''))):
                found.append(('ports', f'target {port!r}'))
        else:
            found += [('ports', f'{port!r}: {problem}') for problem in _short_port_problems(str(port))]
    for port in spec.get('expose') or []:
        if not _PORT.match(str(port).split('/')[0]):
            found.append(('expose', f'{port!r}'))
    for volume in spec.get('volumes') or []:
        if isinstance(volume, dict):
            if not str(volume.get('target', '')).startswith('/'):
                found.append(('volumes', f'target {volume!r}'))
        else:
            found += [('volumes', problem) for problem in _short_volume_problems(str(volume))]
    for entry in [spec['env_file']] if isinstance(spec.get('env_file'), str) else spec.get('env_file') or []:
        path = entry.get('path', '') if isinstance(entry, dict) else entry
        if not path:
            found.append(('env_file', f'{entry!r}'))
    build = spec.get('build')
    if isinstance(build, dict):
        for key in ('context', 'dockerfile'):
            if key in build and not build[key]:
                found.append((f'build.{key}', 'empty'))
    for name, limit in (spec.get('ulimits') or {}).items():
        values = limit.values() if isinstance(limit, dict) else [limit]
        if not all(re.match(r'^-?\d+$', str(value)) for value in values):
            found.append(('ulimits', f'{name}: {limit!r}'))
    return found


def _unread(text: str) -> bool:
    """A `$` the stand-in does not read: one left over after its forms, or one inside a default word."""
    words = [match.group(3) or '' for match in _REFERENCE.finditer(text)]
    return '$' in _REFERENCE.sub('', text) or any('$' in word for word in words)


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [text for item in node for text in _strings(item)]
    if isinstance(node, dict):
        return [text for key, item in node.items() for text in (*_strings(key), *_strings(item))]
    return []


def test_every_structural_value_of_the_dev_files_is_loadable_under_the_build_steps_env():
    """D5, the whole audit: every reference in the five files build loads, rendered as compose would.

    The env is the build step's (DOCKER_ENV, stack's placeholder env, the two digests). A `:?` that
    fires, a reference the stand-in cannot read (left as `${`), and an empty or malformed value in a
    position compose parses -- image, container_name, hostname, ports (short and long), expose,
    volumes (short and long), env_file, build.context/dockerfile, ulimits, top-level network names --
    are each a load failure before anything builds. docker-compose.tools.yaml is not loaded by
    build (the -f list is pinned above), so it is not audited here.
    """
    # stack's own placeholder env (not the PLACEHOLDER_ENV literal), so a change to it is audited here.
    env = {**runner.DOCKER_ENV, **dict(stack.dev_placeholder_step_env()), **dict(BUILD_ENV[-2:])}
    assert set(dict(BUILD_ENV[-2:])) == set(stack.DEV_DIGEST_VARIABLES)
    problems, accepted = [], set()
    for name in DEV_FILES:
        raw = yaml.safe_load((REPO_ROOT / name).read_text(encoding='utf-8')) or {}
        # `$$` and every form the stand-in reads removed, a `$` left over is one it cannot read.
        problems += [f'{name}: unread reference {text!r}' for text in _strings(raw) if _unread(text)]
        try:
            rendered = _walk(raw, env)
        except _Unset as unset:
            problems.append(f'{name}: a :? guard fired: {unset}')
            continue
        for service, spec in (rendered.get('services') or {}).items():
            for key, problem in _service_problems(spec or {}):
                if (name, service, key) in _ACCEPTED_EMPTY and problem == "''":
                    accepted.add((name, service, key))
                else:
                    problems.append(f'{name}: {service}: {key}: {problem}')
        for network, spec in (rendered.get('networks') or {}).items():
            if isinstance(spec, dict) and 'name' in spec and not _NAME.match(str(spec['name'])):
                problems.append(f'{name}: network {network}: name {spec["name"]!r}')
    assert problems == [], problems
    # Non-vacuity, and a drift alarm both ways: the accepted empty is still there and still the only one.
    assert accepted == _ACCEPTED_EMPTY, f'no longer empty, drop it from _ACCEPTED_EMPTY: {_ACCEPTED_EMPTY - accepted}'


def test_the_structural_checks_refuse_what_compose_refuses():
    """Non-vacuity for the audit: each check is red on the shape it exists to catch."""
    assert _short_port_problems('127.0.0.1::') == ["container port ''"]
    assert _short_port_problems('127.0.0.1::80') == []
    assert _short_port_problems('127.0.0.1:x:80') == ["host port 'x'"]
    assert _short_volume_problems('/postgres:/var/lib/postgresql/data') == []
    assert _short_volume_problems(':/data') and _short_volume_problems('./x:') and _short_volume_problems('./x:rel')
    # tj-iee6sn.30: an empty or one-character container_name is what compose refused live.
    assert _service_problems({'container_name': ''}) == [('container_name', "''")]
    assert _service_problems({'container_name': 'd'}) == [('container_name', "'d'")]
    assert _service_problems({'container_name': 'db'}) == []
    assert _service_problems({'image': ':dev', 'hostname': '', 'env_file': [''], 'expose': ['']}) == [
        ('image', "':dev'"),
        ('hostname', "''"),
        ('expose', "''"),
        ('env_file', "''"),
    ]
    assert _service_problems(
        {'image': 'postgres:17.11-trixie', 'ports': [{'target': ''}], 'build': {'context': ''}}
    ) == [('ports', "target {'target': ''}"), ('build.context', 'empty')]
    assert _service_problems({'ulimits': {'nofile': {'soft': '', 'hard': '2'}}}) == [
        ('ulimits', "nofile: {'soft': '', 'hard': '2'}")
    ]
    # A nested default is a form the stand-in does not read; the audit must name it, not pass it.
    assert _unread('${A:-${B}}') and _unread('${A:-$B}') and _unread('$ {A}')
    assert not _unread('$${X} ${Y:-d} ${Z?m} $W')


def test_the_interpolator_reads_the_forms_the_files_use():
    """Non-vacuity for the test above: the stand-in interpolates as compose does."""
    env = {'SET': 'v', 'EMPTY': ''}
    assert _interpolate('${SET}|${UNSET}|$$x|$SET', env) == 'v||$x|v'
    assert _interpolate('${EMPTY:-d}|${EMPTY-d}|${UNSET-d}|${SET:-d}', env) == 'd||d|v'
    with pytest.raises(_Unset):
        _interpolate('${EMPTY:?m}', env)
    assert _short_port_container_part('127.0.0.1::') == ''
    assert _short_port_container_part('127.0.0.1::5432') == '5432'
    assert _short_port_container_part('127.0.0.1:8088:8080/tcp') == '8080'
