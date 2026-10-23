"""TEST mode over the one compose project: test_build, test_up, the per-verb mode table, the stamp helpers.

Design checked against: bead tj-iee6sn.24's WHAT (1)-(4) and TESTS, the builder's 00:21/00:31 UTC notes
there (the deviations the orchestrator accepted: test_up has no secret wrapper and no devnet step;
down and restart still act on an empty project), and ADR tj-4rr0la addendum 26 (as amended)
and addendum 27 (1)-(3): test mode is make system-launch's set (base + docker-compose.fake.yaml) on the
:test images, built by make test-build, never :latest; the MCP refuses while any PROD container exists.

test_build (make test-build, 1800 s), a refusal running nothing after it and charging nothing:

    S6 stale copies (fake and test overlay included) -> generated-code freshness on gen/proto/python
    ONLY -> ONE budget charge -> the two trusted digests -> the bases -> ONE file-based build of
    data_store and data_ingest over base + fake + the MCP-only docker-compose.agent-test.yaml

test_up (make system-launch, 600 s):

    mode guard (any non-PROD state) -> S6 -> D6 on tests/fakes -> D5 env preflight -> steering walk
    -> one inspect per :test image ('call test_build') -> the two digests -> the stamp read of each
    :test image ('STALE: ... call test_build') -> ONE budget charge -> the bases -> the label-only down
    -> ONE plain (unwrapped) up -d --wait --wait-timeout 300 over base + fake, DATA_DIR=./volume_data/test

Every verb call goes through the REAL AgentStack over the fake-docker rig. Expected values are literals
or parsed from the Makefile and the compose files, never read from stack.py.
"""

import json
import shlex
import shutil
from pathlib import Path

import pytest

from common.tests import compose_model
from common.tests.test_ci_invariants import _compose_calls, _expanded_make_variable, _make_recipe, _subprocess_env
from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import (
    DEV_DIGESTS,
    REPO_ROOT,
    VERB_SAMPLES,
    FakeDocker,
    Rig,
    default_response,
    is_dev_digest,
    is_dev_up_wrapper,
    make_rig,
)
from tools.agent_mcp.tests.test_dev_guards import EXPECTED_PLACEHOLDER
from tools.agent_mcp.tests.test_dev_label_verbs import (
    BUDGET_REFUSAL,
    DEV_LINE,
    GUARD,
    GUARD_REFUSAL,
    MIXED_REFUSAL,
    PROD_LINE,
    STEERING_ENV,
    TEST_LINE,
    steering_refusal,
    symlink_refusal,
)
from tools.agent_mcp.tests.test_dev_up import PINNED_ENV


pytestmark = pytest.mark.build_infra

DOCKER = '/usr/local/bin/docker'
PROJECT = 'trader_joe_dev'
TRUSTED = '/opt/agent_mcp/compose'
BASH = '/usr/bin/bash'
DIGEST_SCRIPT = f'{TRUSTED}/tools/source_digest.sh'
TEST_FILES = ('docker-compose.yaml', 'docker-compose.fake.yaml')
TEST_BUILD_FILES = (*TEST_FILES, 'docker-compose.agent-test.yaml')
TEST_IMAGES = ('trader_joe_data_store:test', 'trader_joe_data_ingest:test')
TEST_DATA_DIR = './volume_data/test'
BUILD_TAIL = ('build', 'data_store', 'data_ingest')
UP_TAIL = ('up', '-d', '--wait', '--wait-timeout', '300')
STAMP_FORMAT = '{{ with .Config.Labels }}{{ index . "trader_joe.source.digest" }}{{ end }}'
STORE_DIGEST = DEV_DIGESTS[('data', 'store')]
INGEST_DIGEST = DEV_DIGESTS[('data', 'ingest')]
DIGESTS = {'trader_joe_data_store:test': STORE_DIGEST, 'trader_joe_data_ingest:test': INGEST_DIGEST}
DOWN = (DOCKER, 'compose', '-p', PROJECT, 'down')
BUDGET = 12
STALE_PREFIX = 'the MCP image is stale: '
TEST_GENERATE_HINT = 'run make proto in the root checkout'
TEST_MODE_HINT = 'test mode is up: call up first'
UP_OK = (
    'the trader_joe_dev project was stopped and test mode is up and healthy: postgres on ./volume_data/test, '
    'data_store and data_ingest from the :test images, data_ingest on the fake broker'
)
BUILD_OK = 'the :test images (data_store, data_ingest) are built and stamped; :latest was not touched'
# test_up's up env: the pinned env (compose reads the project's env files itself), then DATA_DIR.
UP_ENV = (*PINNED_ENV, ('DATA_DIR', TEST_DATA_DIR))
# test_build's build env: build's placeholders (no env file is read), DATA_DIR, the two digests.
BUILD_ENV = (
    *EXPECTED_PLACEHOLDER,
    ('DATA_DIR', TEST_DATA_DIR),
    ('SOURCE_DIGEST_DATA_STORE', STORE_DIGEST),
    ('SOURCE_DIGEST_DATA_INGEST', INGEST_DIGEST),
)


def trusted_prefix(host: Path, files: tuple[str, ...]) -> tuple[str, ...]:
    words = [word for name in files for word in ('-f', f'{TRUSTED}/{name}')]
    return (DOCKER, 'compose', '-p', PROJECT, '--project-directory', str(host), *words)


def build_argv(host: Path) -> tuple[str, ...]:
    return (*trusted_prefix(host, TEST_BUILD_FILES), *BUILD_TAIL)


def up_argv(host: Path) -> tuple[str, ...]:
    return (*trusted_prefix(host, TEST_FILES), *UP_TAIL)


def image_inspect(ref: str) -> tuple[str, ...]:
    return (DOCKER, 'image', 'inspect', '--format', '{{.Id}}', ref)


def stamp_read(ref: str) -> tuple[str, ...]:
    return (DOCKER, 'image', 'inspect', '--format', STAMP_FORMAT, ref)


def digest_argv(host: Path, service_name: str) -> tuple[str, ...]:
    return (BASH, DIGEST_SCRIPT, '--context', str(host), 'data', service_name)


def base_inspects() -> list[tuple[str, ...]]:
    return [image_inspect(ref) for ref in stack.BASE_IMAGES]


def up_run(host: Path) -> list[tuple[str, ...]]:
    """test_up's whole run, in the designed order."""
    return [
        GUARD,
        *(image_inspect(ref) for ref in TEST_IMAGES),
        digest_argv(host, 'store'),
        digest_argv(host, 'ingest'),
        *(stamp_read(ref) for ref in TEST_IMAGES),
        *base_inspects(),
        DOWN,
        up_argv(host),
    ]


def build_run(host: Path) -> list[tuple[str, ...]]:
    return [digest_argv(host, 'store'), digest_argv(host, 'ingest'), *base_inspects(), build_argv(host)]


class Responder:
    """The fake daemon of a test-mode call: each step's answer is a field the test can change."""

    def __init__(self) -> None:
        self.guard: tuple[int, bytes] = (0, b'')
        self.missing_images: set[str] = set()
        self.digests: dict[str, tuple[int | None, bytes]] = {
            'store': (0, f'{STORE_DIGEST}\n'.encode()),
            'ingest': (0, f'{INGEST_DIGEST}\n'.encode()),
        }
        self.stamps: dict[str, tuple[int, bytes]] = {ref: (0, f'{d}\n'.encode()) for ref, d in DIGESTS.items()}
        self.missing_bases = False
        self.pull_exit = 0
        self.down_exit = 0
        self.compose_exit = 0

    def __call__(self, step: stack.Step) -> runner.ProcessResult:
        argv = step.argv
        if argv == GUARD:
            return runner.ProcessResult(self.guard[0], self.guard[1], b'')
        if argv[1:3] == ('image', 'inspect') and argv[3:5] == ('--format', '{{.Id}}') and argv[-1] in TEST_IMAGES:
            missing = argv[-1] in self.missing_images
            return runner.ProcessResult(1 if missing else 0, b'' if missing else b'sha256:abc\n', b'')
        if argv[1:3] == ('image', 'inspect') and argv[3:5] == ('--format', STAMP_FORMAT):
            exit_status, stdout = self.stamps[argv[-1]]
            return runner.ProcessResult(exit_status, stdout, b'')
        if self.missing_bases and argv[1:3] == ('image', 'inspect') and argv[-1] in stack.BASE_IMAGES:
            return runner.ProcessResult(1, b'', b'Error: No such image')
        if argv[1:2] == ('pull',):
            return runner.ProcessResult(self.pull_exit, b'', b'')
        if is_dev_digest(step):
            exit_status, stdout = self.digests[argv[-1]]
            return runner.ProcessResult(exit_status, stdout, b'')
        if argv == DOWN:
            return runner.ProcessResult(self.down_exit, b'', b'down said no\n' if self.down_exit else b'')
        if argv[1:2] == ('compose',):
            return runner.ProcessResult(self.compose_exit, b'', b'compose said no\n' if self.compose_exit else b'')
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
# THE TOOL LIST
# ===============================================================================================


def test_both_verbs_take_nothing():
    for verb in ('test_build', 'test_up'):
        assert runner.VERB_SCHEMAS[verb] == {
            'type': 'object',
            'additionalProperties': False,
            'properties': {},
            'required': [],
        }


def test_the_timeouts_are_1800_and_600_seconds_under_the_client_timeout():
    """WHAT (3)/(4): test_build 1800 s, test_up 600 s; the .mcp.json client outlasts both with a margin."""
    assert runner.VERB_TIMEOUT_SECONDS['test_build'] == 1800
    assert runner.VERB_TIMEOUT_SECONDS['test_up'] == 600
    client_ms = json.loads((REPO_ROOT / '.mcp.json').read_text(encoding='utf-8'))['mcpServers']['agent-stack'][
        'timeout'
    ]
    assert client_ms >= (1800 + 30) * 1000, client_ms


def test_the_descriptions_say_what_each_verb_never_touches():
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type] -- only the verb table is read
    assert {'test_build', 'test_up'} <= set(agent.verbs)
    build = agent.describe('test_build')
    assert 'STATE-CHANGING' in build and 'never builds or tags :latest' in build, build
    assert 'reads no env file' in build and 'creates and starts no container' in build, build
    up = agent.describe('test_up')
    assert 'STATE-CHANGING' in up and '`make system-launch`' in up, up
    for phrase in (
        'dev mode included',
        './volume_data/test',
        'never your dev data and never :latest',
        'call test_build',
    ):
        assert phrase in up, (phrase, up)


# ===============================================================================================
# test_build
# ===============================================================================================


def test_test_build_runs_the_digests_the_bases_then_one_build_over_the_test_build_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, _ = _rig(tmp_path, monkeypatch)
    result = rig.call('test_build', {})
    assert result['status'] == 'ok', result
    assert result['message'] == BUILD_OK
    assert _argvs(rig) == build_run(rig.layout.repo_host_path)
    assert all(step.cwd == rig.layout.stack_dir for step in rig.docker.steps)
    assert _charges_left(rig) == BUDGET - 1


def test_test_build_runs_no_mode_guard_even_on_a_prod_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """WHAT (1): 'build, test_build: no guard (they build images only)'."""
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.guard = (0, PROD_LINE)
    assert rig.call('test_build', {})['status'] == 'ok'
    assert GUARD not in _argvs(rig)


def test_only_the_build_step_carries_the_placeholders_data_dir_and_the_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert rig.call('test_build', {})['status'] == 'ok'
    build = rig.docker.steps[-1]
    assert build.argv == build_argv(rig.layout.repo_host_path)
    assert build.env == BUILD_ENV, build.env
    assert dict(build.env)['COMPOSE_ENV_FILES'] == '/dev/null', 'D5: no env file is read'
    assert all(step.env == () for step in rig.docker.steps[:-1]), rig.docker.steps
    assert [step.argv for step in rig.docker.steps if step.builds] == [build.argv]


def test_no_argv_or_env_of_test_build_names_latest_or_a_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    result = rig.call('test_build', {})
    words = [word for step in rig.docker.steps for word in step.argv] + [
        v for step in rig.docker.steps for _, v in step.env
    ]
    assert not [word for word in words if ':latest' in word], words
    commands = [record['command'] for record in result['steps']]
    assert not [command for command in commands if STORE_DIGEST in command or INGEST_DIGEST in command]


def test_test_build_mirrors_make_test_build():
    """The build is make test-build's, with the MCP-only overlay last.

    SYSTEM_COMPOSE's files, the same tail, the same DATA_DIR, and WITH_SOURCE_STAMP's two digest variables.
    """
    [recipe] = _make_recipe('test-build')
    assert recipe == '$(WITH_SOURCE_STAMP) $(SYSTEM_COMPOSE) build data_store data_ingest', recipe
    system = _expanded_make_variable('SYSTEM_COMPOSE', REPO_ROOT, _subprocess_env())
    [(files, rest)] = _compose_calls(f'{system} {" ".join(BUILD_TAIL)}')
    host = Path('/home/someone/trader_joe')
    step = stack.testmode_build_step(Path('/stack'), host, [STORE_DIGEST, INGEST_DIGEST])
    assert tuple(files) == TEST_FILES and tuple(rest) == BUILD_TAIL
    assert step.argv == build_argv(host)
    assert step.argv[-len(BUILD_TAIL) :] == tuple(rest)
    assert system.split()[0] == f'DATA_DIR={dict(step.env)["DATA_DIR"]}'
    stamp = _expanded_make_variable('WITH_SOURCE_STAMP', REPO_ROOT, _subprocess_env())
    for name in ('SOURCE_DIGEST_DATA_STORE', 'SOURCE_DIGEST_DATA_INGEST'):
        assert name in stamp and name in dict(step.env), (name, stamp)
    assert step.builds is True and step.cwd == Path('/stack')


@pytest.mark.parametrize(
    'digests',
    [
        [],
        [STORE_DIGEST],
        [STORE_DIGEST, INGEST_DIGEST, STORE_DIGEST],
        [STORE_DIGEST, 'x' * 64],
        ['AB' * 32, INGEST_DIGEST],
    ],
    ids=['none', 'one', 'three', 'not hex', 'upper case'],
)
def test_the_build_step_builder_refuses_anything_but_two_valid_digests(digests):
    with pytest.raises(stack.Refused, match='test_build needs exactly one valid source digest per image'):
        stack.testmode_build_step(Path('/stack'), Path('/h'), digests)


def _stale_copy(name: str):
    def apply(rig: Rig, responder: Responder) -> None:
        path = rig.layout.trusted_dir / name
        path.write_bytes(path.read_bytes() + b'# drift\n')

    return apply


def _missing_python(rig: Rig, responder: Responder) -> None:
    shutil.rmtree(rig.layout.repo / 'gen' / 'proto' / 'python')


def _spent_budget(rig: Rig, responder: Responder) -> None:
    for _ in range(BUDGET):
        rig.agent.dev_budget.charge()


@pytest.mark.parametrize('name', ['docker-compose.fake.yaml', 'docker-compose.agent-test.yaml', 'Dockerfile'])
def test_test_build_refuses_a_stale_copy_of_its_own_files_with_no_step_and_no_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
):
    rig, responder = _rig(tmp_path, monkeypatch)
    _stale_copy(name)(rig, responder)
    result = rig.call('test_build', {})
    assert result['status'] == 'refused' and result['message'].startswith(f'{STALE_PREFIX}{name} differs'), result
    assert _argvs(rig) == [] and _charges_left(rig) == BUDGET


def test_test_build_refuses_a_missing_python_tree_with_no_step_and_no_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, responder = _rig(tmp_path, monkeypatch)
    _missing_python(rig, responder)
    result = rig.call('test_build', {})
    assert result['status'] == 'refused', result
    assert result['message'] == f'gen/proto/python is missing or empty: {TEST_GENERATE_HINT}'
    assert _argvs(rig) == [] and _charges_left(rig) == BUDGET


def test_test_build_does_not_need_the_typescript_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Make test-build runs `proto` and no gen-proto-ts: test mode has no web, so gen/proto/ts is not checked."""
    rig, _ = _rig(tmp_path, monkeypatch)
    shutil.rmtree(rig.layout.repo / 'gen' / 'proto' / 'ts')
    assert rig.call('test_build', {})['status'] == 'ok'
    # And build, which builds web, still needs it: the narrowing is test_build's alone.
    rig2, _ = _rig(tmp_path / 'dev', monkeypatch)
    shutil.rmtree(rig2.layout.repo / 'gen' / 'proto' / 'ts')
    assert rig2.call('build', {})['status'] == 'refused'


def test_test_build_refuses_on_a_spent_budget_before_any_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    _spent_budget(rig, responder)
    result = rig.call('test_build', {})
    assert result['status'] == 'refused' and result['message'] == BUDGET_REFUSAL, result
    assert _argvs(rig) == []


@pytest.mark.parametrize(
    ('first', 'second', 'answer'),
    [
        (_stale_copy('docker-compose.fake.yaml'), _missing_python, STALE_PREFIX),
        (_missing_python, _spent_budget, 'gen/proto/python is missing'),
    ],
    ids=['S6 before freshness', 'freshness before the budget'],
)
def test_test_build_with_two_checks_failing_the_earlier_one_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first, second, answer: str
):
    rig, responder = _rig(tmp_path, monkeypatch)
    first(rig, responder)
    second(rig, responder)
    result = rig.call('test_build', {})
    assert result['status'] == 'refused' and answer in result['message'], result


@pytest.mark.parametrize('service_name', ['store', 'ingest'])
def test_a_failed_digest_fails_test_build_before_any_base_or_build_and_is_charged_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service_name: str
):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.digests[service_name] = (1, b'')
    result = rig.call('test_build', {})
    assert result['status'] == 'failed', result
    assert result['message'] == f'the source digest failed (data {service_name}); nothing was built'
    host = rig.layout.repo_host_path
    assert _argvs(rig) == build_run(host)[: 1 if service_name == 'store' else 2]
    assert _charges_left(rig) == BUDGET - 1


def test_a_failed_build_answers_failed_and_is_charged_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.compose_exit = 1
    result = rig.call('test_build', {})
    assert result['status'] == 'failed', result
    assert result['message'] == 'compose build of the test images failed for the trader_joe_dev project'
    assert _charges_left(rig) == BUDGET - 1


# ===============================================================================================
# test_up: the call that works
# ===============================================================================================


@pytest.mark.parametrize(
    'guard', [b'', DEV_LINE, TEST_LINE, DEV_LINE + DEV_LINE], ids=['empty', 'dev mode', 'test mode', 'two dev']
)
def test_test_up_runs_every_step_in_the_designed_order_and_answers_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard: bytes
):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.guard = (0, guard)
    result = rig.call('test_up', {})
    assert result['status'] == 'ok', result
    assert result['message'] == UP_OK
    assert _argvs(rig) == up_run(rig.layout.repo_host_path)
    assert all(step.cwd == rig.layout.stack_dir for step in rig.docker.steps)
    assert _charges_left(rig) == BUDGET - 1
    assert 'job' not in json.dumps(result).lower()


def test_only_the_up_carries_data_dir_and_the_down_only_the_steering_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert rig.call('test_up', {})['status'] == 'ok'
    by_argv = {step.argv: step.env for step in rig.docker.steps}
    host = rig.layout.repo_host_path
    assert by_argv[up_argv(host)] == UP_ENV, by_argv[up_argv(host)]
    assert by_argv[DOWN] == STEERING_ENV
    others = {argv: env for argv, env in by_argv.items() if argv not in (up_argv(host), DOWN)}
    assert all(env == () for env in others.values()), others
    env = dict(by_argv[up_argv(host)])
    # The pinned env, never the placeholders: compose reads the owner's env files for POSTGRES_PASS.
    assert 'POSTGRES_PASS' not in env and 'INSTANCE_WRITE_SECRET' not in env and env['COMPOSE_ENV_FILES'] == ''


def test_test_up_runs_no_secret_wrapper_no_devnet_and_no_digest_in_any_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Deviation (1), accepted: the plain up is make system-launch's own.

    The base and fake files carry no INSTANCE_WRITE_SECRET guard and name no external network (pinned
    from the files below).
    """
    rig, _ = _rig(tmp_path, monkeypatch)
    result = rig.call('test_up', {})
    assert not [step for step in rig.docker.steps if is_dev_up_wrapper(step)]
    assert not [argv for argv in _argvs(rig) if argv[1:2] == ('network',)]
    commands = [record['command'] for record in result['steps']]
    assert not [c for c in commands if STORE_DIGEST in c or INGEST_DIGEST in c]
    assert not [word for argv in _argvs(rig) for word in argv if ':latest' in word]


def test_the_test_files_need_no_secret_and_join_no_external_network():
    """Why test_up has no wrapper and no devnet step: read from the two files it loads."""
    text = '\n'.join((REPO_ROOT / name).read_text(encoding='utf-8') for name in TEST_FILES)
    assert 'INSTANCE_WRITE_SECRET' not in text
    for name in TEST_FILES:
        for network in (compose_model.load(REPO_ROOT / name).get('networks') or {}).values():
            assert not (network or {}).get('external'), (name, network)
        for service in (compose_model.load(REPO_ROOT / name).get('services') or {}).values():
            assert 'devnet' not in (service.get('networks') or {}), (name, service)


def test_only_the_up_step_is_flagged_as_building_and_the_bases_come_before_the_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, _ = _rig(tmp_path, monkeypatch)
    assert rig.call('test_up', {})['status'] == 'ok'
    host = rig.layout.repo_host_path
    assert [step.argv for step in rig.docker.steps if step.builds] == [up_argv(host)]
    argvs = _argvs(rig)
    bases = [index for index, argv in enumerate(argvs) if argv in base_inspects()]
    assert len(bases) == len(stack.BASE_IMAGES)
    assert max(bases) < argvs.index(DOWN) and min(bases) > argvs.index(stamp_read(TEST_IMAGES[1]))


def test_test_up_mirrors_make_system_launchs_up():
    """The up is system-launch's last line: SYSTEM_COMPOSE's files, its tail, its DATA_DIR."""
    recipe = _make_recipe('system-launch')
    assert recipe[-1] == '$(SYSTEM_COMPOSE) up -d --wait --wait-timeout 300', recipe
    system = _expanded_make_variable('SYSTEM_COMPOSE', REPO_ROOT, _subprocess_env())
    [(files, rest)] = _compose_calls(f'{system} {recipe[-1].split(" ", 1)[1]}')
    host = Path('/home/someone/trader_joe')
    step = stack.testmode_up_step(Path('/stack'), host)
    assert step.argv == up_argv(host)
    assert tuple(files) == TEST_FILES and tuple(rest) == UP_TAIL == step.argv[-len(UP_TAIL) :]
    assert system.split()[0] == f'DATA_DIR={dict(step.env)["DATA_DIR"]}' == f'DATA_DIR={TEST_DATA_DIR}'
    assert step.env == UP_ENV and step.builds is True
    # The two images the Makefile checks are the two test_up checks.
    assert _expanded_make_variable('TEST_IMAGES', REPO_ROOT, _subprocess_env()).split() == list(TEST_IMAGES)


def test_data_dir_is_pinned_only_on_test_ups_up_and_test_builds_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Addendum 26 (3): DEV keeps the owner's DATA_DIR, never pinned by the MCP. Every verb, swept."""
    pinned = {}
    for index, verb in enumerate(sorted(VERB_SAMPLES)):
        rig = make_rig(tmp_path / str(index), monkeypatch)
        rig.call(verb, dict(VERB_SAMPLES[verb]))
        for step in rig.docker.steps:
            if 'DATA_DIR' in dict(step.env):
                pinned.setdefault(verb, []).append(step.argv[-len(UP_TAIL) :] if verb == 'test_up' else step.argv[-3:])
    assert pinned == {'test_up': [UP_TAIL], 'test_build': [BUILD_TAIL]}, pinned
    assert 'DATA_DIR' not in dict(stack.dev_pinned_step_env())
    assert 'DATA_DIR' not in dict(stack.dev_placeholder_step_env())


def _parsed_bind_sources(files: tuple[str, ...]) -> tuple[set[str], set[str]]:
    """(relative in-repo bind sources, every other bind source) over FILES, parsed."""
    relative, other = set(), set()
    for name in files:
        for service in (compose_model.load(REPO_ROOT / name).get('services') or {}).values():
            for entry in service.get('volumes') or []:
                mount = compose_model.volume(entry)
                if mount['type'] != 'bind':
                    continue
                source = mount['source']
                if source.startswith('./'):
                    relative.add(source[2:].rstrip('/'))
                else:
                    other.add(source)
    return relative, other


# The in-repo binds of base + fake: data_store's two alembic binds (docker-compose.yaml l.86-87) and the
# fakes (docker-compose.fake.yaml). The data dir is the residual D6 cannot see, as for dev.
EXPECTED_TEST_BIND_SOURCES = {'server/data/store/alembic.ini', 'server/data/store/migrations', 'tests/fakes'}
# Every compose file the dev and tools sets load (the D6 pin for DEV_BIND_SOURCES lives in test_dev_guards.py).
DEV_AND_TOOLS_FILES = (
    'docker-compose.yaml',
    'docker-compose.override.yaml',
    'docker-compose.web.yaml',
    'docker-compose.web.dev.yaml',
    'docker-compose.tools.yaml',
    'docker-compose.agent-dev.yaml',
)


def test_the_test_files_in_repo_binds_are_these_and_the_data_dir_is_the_residual():
    relative, other = _parsed_bind_sources(TEST_BUILD_FILES)
    assert relative == EXPECTED_TEST_BIND_SOURCES, relative
    assert other == {'${DATA_DIR:-./volume_data}/postgres'}, other
    # The MCP-only overlay test_build adds mounts nothing.
    assert _parsed_bind_sources(('docker-compose.agent-test.yaml',)) == (set(), set())


def test_the_test_bind_sources_are_the_relative_binds_parsed_from_the_test_files():
    """D6 for test_up: every in-repo bind source of base + fake, PARSED, so a new mount cannot slip past.

    tj-iee6sn.24's validator RE: the first list held tests/fakes alone and missed data_store's alembic binds.
    """
    relative, _ = _parsed_bind_sources(TEST_FILES)
    assert len(set(stack.TEST_BIND_SOURCES)) == len(stack.TEST_BIND_SOURCES), stack.TEST_BIND_SOURCES
    assert set(stack.TEST_BIND_SOURCES) == relative, sorted(relative ^ set(stack.TEST_BIND_SOURCES))


def test_every_in_repo_bind_of_every_compose_file_the_lifecycle_loads_is_checked_by_its_verbs():
    """The audit, as a pin: the dev and tools sets' binds are in DEV_BIND_SOURCES, the test sets' in TEST_BIND_SOURCES."""
    dev_relative, _ = _parsed_bind_sources(DEV_AND_TOOLS_FILES)
    assert dev_relative <= set(stack.DEV_BIND_SOURCES), sorted(dev_relative - set(stack.DEV_BIND_SOURCES))
    test_relative, _ = _parsed_bind_sources(TEST_BUILD_FILES)
    assert test_relative <= set(stack.TEST_BIND_SOURCES), sorted(test_relative - set(stack.TEST_BIND_SOURCES))


@pytest.mark.parametrize('source', ['server/data/store/migrations', 'server/data/store/alembic.ini'])
def test_a_symlinked_alembic_bind_source_refuses_test_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str):
    """The daemon resolves data_store's alembic binds at every start: a link planted there must refuse, as up's does."""
    rig, _ = _rig(tmp_path, monkeypatch)
    path = rig.layout.repo / source
    moved = rig.layout.root / 'moved'
    shutil.move(str(path), str(moved))
    path.symlink_to(moved, target_is_directory=moved.is_dir())
    result = rig.call('test_up', {})
    assert result['status'] == 'refused' and result['message'] == symlink_refusal(source, source), result
    assert _argvs(rig) == [GUARD] and _charges_left(rig) == BUDGET


# ===============================================================================================
# test_up: each refusal runs only the steps before it and charges nothing
# ===============================================================================================


def _guard(stdout: bytes):
    def apply(rig: Rig, responder: Responder) -> None:
        responder.guard = (0, stdout)

    return apply


def _symlinked_fakes(rig: Rig, responder: Responder) -> None:
    repo = rig.layout.repo
    shutil.move(str(repo / 'tests' / 'fakes'), str(rig.layout.root / 'moved-fakes'))
    (repo / 'tests' / 'fakes').symlink_to(rig.layout.root / 'moved-fakes', target_is_directory=True)


def _env_file_a_directory(rig: Rig, responder: Responder) -> None:
    (rig.layout.repo / 'server' / 'data' / 'ingest' / '.env').mkdir(parents=True)


def _steering_file(rig: Rig, responder: Responder) -> None:
    (rig.layout.stack_dir / 'compose.yaml').write_text('services: {}\n')


def _missing(index: int):
    def apply(rig: Rig, responder: Responder) -> None:
        responder.missing_images.add(TEST_IMAGES[index])

    return apply


def _stamp(index: int, answer: tuple[int, bytes]):
    def apply(rig: Rig, responder: Responder) -> None:
        responder.stamps[TEST_IMAGES[index]] = answer

    return apply


OTHER = '3' * 64


def _stale(index: int) -> str:
    image = TEST_IMAGES[index]
    return (
        f'STALE: {image} was built from different source than this checkout '
        f'(image stamp {OTHER}, checkout digest {DIGESTS[image]}): call test_build'
    )


def _unstamped(index: int) -> str:
    return f'CANNOT VERIFY: the image {TEST_IMAGES[index]} carries no usable source stamp (absent is not a match): call test_build'


def _unreadable(index: int) -> str:
    return f'the image {TEST_IMAGES[index]} could not be read, so there is nothing to compare against: call test_build'


# name -> (break it, the refusal message (None: checked by prefix), how many steps ran)
UP_REFUSALS = {
    'a prod container': (_guard(PROD_LINE), GUARD_REFUSAL, 1),
    'both files in one label': (
        _guard(b'/r/docker-compose.yaml,/r/docker-compose.override.yaml,/r/docker-compose.fake.yaml\n'),
        GUARD_REFUSAL,
        1,
    ),
    'dev and test together': (_guard(DEV_LINE + TEST_LINE), MIXED_REFUSAL, 1),
    'S6: a stale fake overlay': (_stale_copy('docker-compose.fake.yaml'), None, 1),
    'S6: a stale base file': (_stale_copy('docker-compose.yaml'), None, 1),
    'D6: tests/fakes is a symlink': (_symlinked_fakes, symlink_refusal('tests/fakes', 'tests/fakes'), 1),
    'D5: an env file that is a directory': (
        _env_file_a_directory,
        'server/data/ingest/.env is not a regular file: remove it, or start the stack yourself',
        1,
    ),
    'steering: a compose file in the stack directory': (_steering_file, None, 1),
    'no data_store:test image': (
        _missing(0),
        'the image trader_joe_data_store:test is not on the daemon: call test_build first',
        2,
    ),
    'no data_ingest:test image': (
        _missing(1),
        'the image trader_joe_data_ingest:test is not on the daemon: call test_build first',
        3,
    ),
    'data_store:test stale': (_stamp(0, (0, f'{OTHER}\n'.encode())), _stale(0), 6),
    'data_ingest:test stale': (_stamp(1, (0, f'{OTHER}\n'.encode())), _stale(1), 7),
    'data_store:test unstamped': (_stamp(0, (0, b'\n')), _unstamped(0), 6),
    'data_ingest:test stamp not hex': (_stamp(1, (0, b'<no value>\n')), _unstamped(1), 7),
    'data_store:test stamp unreadable': (_stamp(0, (1, b'')), _unreadable(0), 6),
    "data_store:test carries data_ingest's stamp": (_stamp(0, (0, f'{INGEST_DIGEST}\n'.encode())), None, 6),
    'the budget spent': (_spent_budget, BUDGET_REFUSAL, 7),
}


@pytest.mark.parametrize(('breaks', 'message', 'ran'), list(UP_REFUSALS.values()), ids=list(UP_REFUSALS))
def test_each_test_up_refusal_runs_only_the_steps_before_it_and_charges_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, breaks, message: str | None, ran: int
):
    rig, responder = _rig(tmp_path, monkeypatch)
    breaks(rig, responder)
    result = rig.call('test_up', {})
    assert result['status'] == 'refused', result
    if message is not None:
        assert result['message'] == message, result
    elif 'stale' in breaks.__qualname__:
        assert result['message'].startswith(STALE_PREFIX), result
    elif breaks is _steering_file:
        assert result['message'] == steering_refusal(rig.layout.stack_dir / 'compose.yaml'), result
    else:
        assert result['message'].startswith('STALE: trader_joe_data_store:test'), result
    assert _argvs(rig) == up_run(rig.layout.repo_host_path)[:ran]
    assert DOWN not in _argvs(rig) and not [argv for argv in _argvs(rig) if argv[-len(UP_TAIL) :] == UP_TAIL]
    if breaks is not _spent_budget:
        assert _charges_left(rig) == BUDGET, 'a refused call was charged'


def test_a_symlinked_dev_only_bind_source_does_not_refuse_test_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """D6 checks what the test set mounts: server/common is a dev bind, not a test one."""
    rig, _ = _rig(tmp_path, monkeypatch)
    repo = rig.layout.repo
    shutil.move(str(repo / 'server' / 'common'), str(rig.layout.root / 'moved-common'))
    (repo / 'server' / 'common').symlink_to(rig.layout.worktree / 'server' / 'common', target_is_directory=True)
    assert rig.call('test_up', {})['status'] == 'ok'


UP_PAIRS = {
    'the guard before S6': (
        _guard(PROD_LINE),
        _stale_copy('docker-compose.fake.yaml'),
        'not started from the dev or test',
    ),
    'S6 before D6': (_stale_copy('docker-compose.fake.yaml'), _symlinked_fakes, STALE_PREFIX),
    'D6 before D5': (_symlinked_fakes, _env_file_a_directory, 'a symlink at tests/fakes'),
    'D5 before steering': (_env_file_a_directory, _steering_file, 'is not a regular file'),
    'steering before the images': (_steering_file, _missing(0), 'would steer compose'),
    'the images before the stamps': (_missing(1), _stamp(0, (0, b'\n')), 'is not on the daemon'),
    'the stamps before the budget': (_stamp(0, (0, b'\n')), _spent_budget, 'CANNOT VERIFY'),
}


@pytest.mark.parametrize(('first', 'second', 'answer'), list(UP_PAIRS.values()), ids=list(UP_PAIRS))
def test_test_up_with_two_checks_failing_the_earlier_one_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first, second, answer: str
):
    rig, responder = _rig(tmp_path, monkeypatch)
    first(rig, responder)
    second(rig, responder)
    result = rig.call('test_up', {})
    assert result['status'] == 'refused' and answer in result['message'], result


def test_a_failed_guard_listing_runs_nothing_else(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.guard = (1, b'')
    _stale_copy('docker-compose.fake.yaml')(rig, responder)
    result = rig.call('test_up', {})
    assert result['message'] == 'could not list the containers of project trader_joe_dev, so nothing was changed'
    assert _argvs(rig) == [GUARD] and _charges_left(rig) == BUDGET


# ===============================================================================================
# test_up: failures after admission
# ===============================================================================================


@pytest.mark.parametrize('service_name', ['store', 'ingest'])
def test_a_failed_digest_returns_before_the_stamps_and_the_down_and_charges_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service_name: str
):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.digests[service_name] = (0, b'not-a-digest\n')
    result = rig.call('test_up', {})
    assert result['status'] == 'failed', result
    assert result['message'] == f'the source digest failed (data {service_name}); nothing was stopped or started'
    assert _argvs(rig) == up_run(rig.layout.repo_host_path)[: 4 if service_name == 'store' else 5]
    assert _charges_left(rig) == BUDGET


def test_a_failed_base_pull_stops_before_the_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.missing_bases, responder.pull_exit = True, 1
    result = rig.call('test_up', {})
    assert result['status'] == 'failed' and result['message'].startswith('test_up stopped: '), result
    assert DOWN not in _argvs(rig)
    assert _charges_left(rig) == BUDGET - 1


def test_a_failed_down_starts_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.down_exit = 1
    result = rig.call('test_up', {})
    assert result['status'] == 'failed', result
    assert result['message'] == 'compose down failed for the trader_joe_dev project; nothing was started'
    assert _argvs(rig)[-1] == DOWN


def test_a_failed_up_answers_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, responder = _rig(tmp_path, monkeypatch)
    responder.compose_exit = 1
    result = rig.call('test_up', {})
    assert result['status'] == 'failed', result
    assert (
        result['message']
        == 'compose up failed for the trader_joe_dev project in test mode; see the steps and call logs'
    )
    assert _charges_left(rig) == BUDGET - 1


# ===============================================================================================
# THE PER-VERB MODE TABLE (WHAT (1), as the orchestrator accepted the builder's deviation (2))
# ===============================================================================================

ANY = {'up', 'test_up', 'down', 'restart'}
DEV_OR_EMPTY = {'tools': TEST_MODE_HINT}
# tj-iee6sn.10: migrate and migrate_status act on whichever mode is UP, DEV or TEST; an empty project refuses.
MIGRATE_EMPTY_HINT = 'nothing is up: call up (your dev data) or test_up first'
DEV_OR_TEST = {'migrate': MIGRATE_EMPTY_HINT, 'migrate_status': MIGRATE_EMPTY_HINT}
NO_GUARD = {'build', 'test_build'}
STATES = {'empty': b'', 'dev': DEV_LINE, 'test': TEST_LINE, 'mixed': DEV_LINE + TEST_LINE, 'prod': PROD_LINE}


def _expected(verb: str, state: str) -> str | None:
    """None: the guard passes; else the refusal."""
    if state == 'prod':
        return GUARD_REFUSAL
    if state == 'mixed':
        return MIXED_REFUSAL
    if verb in DEV_OR_EMPTY and state == 'test':
        return DEV_OR_EMPTY[verb]
    if verb in DEV_OR_TEST and state == 'empty':
        return DEV_OR_TEST[verb]
    return None


class _GuardOnly:
    def __init__(self, stdout: bytes) -> None:
        self.stdout = stdout

    def __call__(self, step: stack.Step) -> runner.ProcessResult:
        if step.argv == GUARD:
            return runner.ProcessResult(0, self.stdout, b'')
        return default_response(step)


def test_the_mode_table_covers_every_state_changing_verb():
    """Every verb on the one project but the read-only four and the two test-suite verbs.

    The names carry no dev_ prefix to select by any more (tj-iee6sn.26), so the exclusions are spelled out.
    migrate_status is read-only but guarded (it acts on the mode that is up, tj-iee6sn.10), so it is here.
    """
    state_changing = set(VERB_SAMPLES) - {'ps', 'logs', 'db_tables', 'http_get', 'run_system_tests', 'seed_dump'}
    assert state_changing == {
        'up',
        'build',
        'tools',
        'down',
        'restart',
        'test_up',
        'test_build',
        'migrate',
        'migrate_status',
    }
    assert ANY | set(DEV_OR_EMPTY) | set(DEV_OR_TEST) | NO_GUARD == state_changing, state_changing


@pytest.mark.parametrize('state', list(STATES), ids=list(STATES))
@pytest.mark.parametrize('verb', sorted(ANY | set(DEV_OR_EMPTY) | set(DEV_OR_TEST)))
def test_each_guarded_verb_accepts_exactly_its_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, state: str
):
    rig = make_rig(tmp_path, monkeypatch, FakeDocker(respond=_GuardOnly(STATES[state])))
    result = rig.call(verb, dict(VERB_SAMPLES[verb]))
    refusal = _expected(verb, state)
    assert _argvs(rig)[0] == GUARD, _argvs(rig)
    if refusal is None:
        assert result['status'] == 'ok', result
        assert len(_argvs(rig)) > 1
    else:
        assert result['status'] == 'refused' and result['message'] == refusal, result
        assert _argvs(rig) == [GUARD], 'a refusal runs nothing after the guard'
        assert _charges_left(rig) == BUDGET


@pytest.mark.parametrize('verb', sorted(NO_GUARD))
def test_the_build_verbs_run_no_guard_in_any_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    for state, stdout in STATES.items():
        rig = make_rig(tmp_path / state, monkeypatch, FakeDocker(respond=_GuardOnly(stdout)))
        assert rig.call(verb, {})['status'] == 'ok', (verb, state)
        assert GUARD not in _argvs(rig), (verb, state)


# ===============================================================================================
# THE STAMP HELPERS, generalised (WHAT (2)): an image ref, or a running container by id
# ===============================================================================================

STAMPED = ('trader_joe_data_store:dev', 'trader_joe_data_ingest:dev', *TEST_IMAGES)


@pytest.mark.parametrize('image', STAMPED)
def test_the_stamp_read_of_each_stamped_image_is_one_fixed_inspect(image: str):
    assert stack.dev_stamp_argv(image) == stamp_read(image)
    stack.check_dev_stamp_argv(stamp_read(image), image)
    step = stack.dev_stamp_step(Path('/stack'), image)
    assert step.argv == stamp_read(image) and step.cwd == Path('/stack') and step.env == () and step.builds is False


def test_the_stamp_read_defaults_to_the_data_store_dev_image():
    assert stack.dev_stamp_argv() == stamp_read('trader_joe_data_store:dev')


@pytest.mark.parametrize(
    'image',
    [
        'trader_joe_web:dev',
        'trader_joe_data_store:latest',
        'trader_joe_data_ingest:latest',
        'trader_joe_data_store',
        'x:test',
    ],
)
def test_no_other_image_is_read(image: str):
    with pytest.raises(stack.Refused, match='the source stamp is read from one of'):
        stack.dev_stamp_argv(image)


def test_a_stamp_argv_for_another_image_is_refused():
    with pytest.raises(stack.Refused, match='the stamp read is'):
        stack.check_dev_stamp_argv(stamp_read(TEST_IMAGES[1]), TEST_IMAGES[0])
    with pytest.raises(stack.Refused):
        stack.check_dev_stamp_argv((*stamp_read(TEST_IMAGES[0]), '--size'), TEST_IMAGES[0])


CONTAINER_ID = 'c0ffee000001'


def test_the_container_stamp_read_is_one_typed_inspect_of_one_checked_id():
    expected = (DOCKER, 'inspect', '--type', 'container', '--format', STAMP_FORMAT, CONTAINER_ID)
    assert stack.container_stamp_argv(CONTAINER_ID) == expected
    stack.check_container_stamp_argv(expected)
    step = stack.container_stamp_step(Path('/stack'), CONTAINER_ID)
    assert step.argv == expected and step.env == () and step.builds is False


@pytest.mark.parametrize(
    'bad', ['', 'trader_joe_data_store:test', 'C0FFEE000001', 'c0ffee', 'c0ffee000001;rm', '-c0ffee000001']
)
def test_the_container_stamp_read_refuses_anything_but_a_container_id(bad: str):
    """`--type container` plus a hex id: an image name (which a container could share) never reaches it."""
    with pytest.raises(stack.Refused):
        stack.container_stamp_argv(bad)


_BAD_CONTAINER_ARGVS = {
    'no --type': (DOCKER, 'inspect', '--format', STAMP_FORMAT, CONTAINER_ID),
    'type image': (DOCKER, 'inspect', '--type', 'image', '--format', STAMP_FORMAT, CONTAINER_ID),
    'another format': (DOCKER, 'inspect', '--type', 'container', '--format', '{{.Id}}', CONTAINER_ID),
    'two ids': (DOCKER, 'inspect', '--type', 'container', '--format', STAMP_FORMAT, CONTAINER_ID, CONTAINER_ID),
    'an image ref': (DOCKER, 'inspect', '--type', 'container', '--format', STAMP_FORMAT, TEST_IMAGES[0]),
    'empty': (),
}


@pytest.mark.parametrize('argv', list(_BAD_CONTAINER_ARGVS.values()), ids=list(_BAD_CONTAINER_ARGVS))
def test_any_other_container_stamp_argv_is_refused(argv):
    with pytest.raises(stack.Refused):
        stack.check_container_stamp_argv(argv)


@pytest.mark.parametrize('image', TEST_IMAGES)
def test_a_test_images_stamp_check_names_test_build(image: str):
    digest = DIGESTS[image]
    stack.check_dev_image_stamp(0, f'{digest}\n'.encode(), digest, image)
    for exit_status, stdout, start in (
        (1, b'', f'the image {image} could not be read'),
        (0, b'\n', f'CANNOT VERIFY: the image {image}'),
        (0, ('AB' * 32).encode(), f'CANNOT VERIFY: the image {image}'),
        (0, f'{OTHER}\n'.encode(), f'STALE: {image}'),
    ):
        with pytest.raises(stack.Refused) as refused:
            stack.check_dev_image_stamp(exit_status, stdout, digest, image)
        assert str(refused.value).startswith(start) and str(refused.value).endswith(': call test_build'), refused.value


def test_a_dev_images_stamp_check_still_names_dev_build():
    image = 'trader_joe_data_store:dev'
    with pytest.raises(stack.Refused) as refused:
        stack.check_dev_image_stamp(0, f'{OTHER}\n'.encode(), STORE_DIGEST)
    assert str(refused.value).startswith(f'STALE IMAGE: {image}') and str(refused.value).endswith('call build')
    assert stack.stamp_image_fix(image) == 'call build'
    assert [stack.stamp_image_fix(ref) for ref in TEST_IMAGES] == ['call test_build'] * 2


def test_the_stamp_checks_docstring_names_both_fixes_and_the_unstamped_cases():
    """The architect's 09:30 note (A), loosely: the text names call test_build beside call build.

    And an unstamped image as one CI pulled and retagged, or one compose made implicitly.
    """
    doc = ' '.join((stack.check_dev_image_stamp.__doc__ or '').split())
    assert 'call test_build' in doc and 'call build' in doc, doc
    assert 'CI pulled and retagged' in doc and 'implicitly' in doc, doc
    assert 'make system-launch' not in doc, doc


def test_the_image_presence_steps_are_one_inspect_per_test_image():
    steps = stack.testmode_image_steps(Path('/stack'))
    assert [step.argv for step in steps] == [image_inspect(ref) for ref in TEST_IMAGES]
    assert all(step.builds is False and step.env == () for step in steps)
    assert stack.TEST_IMAGES == TEST_IMAGES


def test_the_test_images_are_the_refs_the_fake_overlay_names():
    services = compose_model.load(REPO_ROOT / 'docker-compose.fake.yaml')['services']
    assert (services['data_store']['image'], services['data_ingest']['image']) == TEST_IMAGES


def test_the_command_lines_are_recorded_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, _ = _rig(tmp_path, monkeypatch)
    result = rig.call('test_up', {})
    assert [record['command'] for record in result['steps']] == [
        shlex.join(argv) for argv in up_run(rig.layout.repo_host_path)
    ]
