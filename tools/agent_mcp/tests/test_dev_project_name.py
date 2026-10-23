"""The ONE project every MCP verb addresses is trader_joe_dev, and prod's default project is unreachable.

tj-iee6sn.31. Design: ADR tj-4rr0la addendum 29 and decision tj-oxxl8a option A: dev and test run as the
explicit compose project trader_joe_dev (the Makefile's DEV_PROJECT, tj-grna9p.161); PROD keeps compose's
default project, the checkout directory's name, trader_joe, and no MCP verb may ever name it.

What is pinned here, each across ALL fifteen verbs rather than per verb module:
  * every recorded `docker compose` argv carries exactly one `-p trader_joe_dev` (the secret wrapper's
    compose argv included), every `docker ps` filters on project=trader_joe_dev, and every pinned
    COMPOSE_PROJECT_NAME is trader_joe_dev; no argv word, filter or env value names the bare project;
  * the source of tools/agent_mcp (tests excluded) never spells the bare name as a project;
  * a fake daemon holding a PROD-project container beside the dev ones: no verb sees it or touches it,
    and the mode guard still refuses only on what sits INSIDE trader_joe_dev;
  * the MCP and the Makefile name the same project, read from the Makefile;
  * the tool descriptions name trader_joe_dev and never the bare project.

Expected values are literals, never read from stack.py: a test that took the project from the module
under test would agree with any change to it.
"""

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from common.tests.test_ci_invariants import _make_variable
from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import (
    DEV_CONTAINER_ID,
    DEV_INGEST_CONTAINER_ID,
    DEV_MODE_LINE,
    REPO_ROOT,
    SYSTEM_SUITE_VERBS,
    TEST_MODE_LINE,
    VERB_SAMPLES,
    FakeDocker,
    Rig,
    dev_up_compose_argv,
    is_dev_lookup,
    is_dev_up_wrapper,
    is_mode_guard,
    make_rig,
    response_for,
)


pytestmark = pytest.mark.build_infra

DOCKER = '/usr/local/bin/docker'
DEV_PROJECT = 'trader_joe_dev'
PROD_PROJECT = 'trader_joe'
PROJECT_LABEL = 'label=com.docker.compose.project='
# The fifteen verbs of ADR tj-4rr0la addenda 26 (5) and 27 (4), spelled out so a verb added or dropped
# is red here until this sweep is told about it.
ALL_VERBS = (
    'up',
    'build',
    'tools',
    'test_up',
    'test_build',
    'down',
    'restart',
    'ps',
    'logs',
    'db_tables',
    'http_get',
    'migrate',
    'migrate_status',
    'run_system_tests',
    'seed_dump',
)
# The bare name used AS A PROJECT: not followed by a character that would make it a longer identifier
# (trader_joe_dev, trader_joe_devnet, trader_joe_agent_mcp, trader_joe_data_store:dev), a dotted label
# key (trader_joe.source.digest), a hyphenated tag (trader_joe-seed/1) or a path segment.
BARE_PROJECT = re.compile(r'(?<![A-Za-z0-9_])trader_joe(?![A-Za-z0-9_./-])')
# The compose steps whose env is deliberately EMPTY: the read-only pair, which pass -p and inherit
# nothing (runner.DOCKER_ENV names no COMPOSE_ variable).
READ_ONLY_COMPOSE_TAILS = ('ps', 'logs')


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, respond=None) -> tuple[Rig, dict]:
    rig = make_rig(tmp_path, monkeypatch)
    rig.docker.respond = respond if respond is not None else response_for(verb, rig.layout)
    return rig, rig.call(verb, dict(VERB_SAMPLES[verb]))


def _compose_argv(step: stack.Step) -> tuple[str, ...] | None:
    """The compose argv of a step: its own, or the one the up secret wrapper execs; None if not compose."""
    argv = dev_up_compose_argv(step) if is_dev_up_wrapper(step) else tuple(step.argv)
    return argv if argv[:2] == (DOCKER, 'compose') else None


def _project_filters(argv) -> list[str]:
    return [word[len(PROJECT_LABEL) :] for word in argv if word.startswith(PROJECT_LABEL)]


# ===============================================================================================
# THE SWEEP OVER ALL FIFTEEN VERBS
# ===============================================================================================


def test_the_sweep_covers_exactly_the_verbs_the_server_offers():
    assert set(ALL_VERBS) == set(VERB_SAMPLES) == set(runner.VERB_SCHEMAS), sorted(VERB_SAMPLES)
    assert len(ALL_VERBS) == 15


@pytest.mark.parametrize('verb', ALL_VERBS)
def test_every_argv_filter_and_env_pin_of_the_verb_addresses_trader_joe_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig, result = _run(tmp_path, monkeypatch, verb)
    assert result['status'] == 'ok', result
    addressed = 0
    for step in rig.docker.steps:
        compose = _compose_argv(step)
        env = dict(step.env)
        if compose is not None:
            assert compose[2:4] == ('-p', DEV_PROJECT), f'{verb}: {compose}'
            assert compose.count('-p') == 1 and '--project-name' not in compose, f'{verb}: {compose}'
            assert not [word for word in compose if word.startswith('-p') and word != '-p'], compose
            tail = compose[4]
            if tail in READ_ONLY_COMPOSE_TAILS:
                assert step.env == (), f'{verb}: the read-only {tail} inherits an env: {step.env}'
            else:
                assert env.get('COMPOSE_PROJECT_NAME') == DEV_PROJECT, f'{verb}: {compose} pins {env}'
            addressed += 1
        if step.argv[:2] == (DOCKER, 'ps'):
            assert _project_filters(step.argv) == [DEV_PROJECT], f'{verb}: {step.argv}'
            addressed += 1
        if 'COMPOSE_PROJECT_NAME' in env:
            assert env['COMPOSE_PROJECT_NAME'] == DEV_PROJECT, f'{verb}: {env}'
        for word in (*step.argv, *env.values()):
            assert not BARE_PROJECT.search(word), f'{verb} names the bare project: {word!r}'
            assert word != PROD_PROJECT and word != f'{PROJECT_LABEL}{PROD_PROJECT}', f'{verb}: {word!r}'
    assert addressed, f'{verb} addressed no project at all, so the sweep pinned nothing'


# ===============================================================================================
# THE SOURCE: no bare project anywhere in tools/agent_mcp outside its tests
# ===============================================================================================


def _mcp_sources() -> list[Path]:
    root = REPO_ROOT / 'tools' / 'agent_mcp'
    return sorted(
        path
        for path in root.rglob('*')
        if path.is_file() and 'tests' not in path.relative_to(root).parts and '__pycache__' not in path.parts
    )


@pytest.mark.parametrize(
    'text',
    [
        "DEV_PROJECT = 'trader_joe'",
        '-p trader_joe down',
        'project=trader_joe\n',
        'project trader_joe, so',
        '"trader_joe"',
    ],
)
def test_the_bare_project_pattern_catches_each_spelling_of_the_project(text: str):
    """The grep pin below is only as good as its pattern: each way the old project was spelled matches."""
    assert BARE_PROJECT.search(text), text


@pytest.mark.parametrize(
    'text',
    [
        'trader_joe_dev',
        'trader_joe_devnet',
        'trader_joe_agent_mcp',
        'trader_joe_data_store:dev',
        'trader_joe.source.digest',
        'trader_joe-seed/1',
        '/proto/trader_joe/proto',
    ],
)
def test_the_bare_project_pattern_spares_every_other_name(text: str):
    assert not BARE_PROJECT.search(text), text


def test_no_mcp_source_outside_its_tests_names_the_bare_project():
    sources = _mcp_sources()
    names = {path.name for path in sources}
    assert {'stack.py', 'runner.py', '__init__.py', 'server.py', 'Dockerfile'} <= names, sorted(names)
    offenders = [
        f'{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}'
        for path in sources
        for number, line in enumerate(path.read_text(encoding='utf-8', errors='replace').splitlines(), 1)
        if BARE_PROJECT.search(line)
    ]
    assert not offenders, '\n'.join(offenders)


# ===============================================================================================
# A FAKE DAEMON WITH A PROD CONTAINER BESIDE THE DEV ONES
# ===============================================================================================

PROD_IDS = {
    'postgres': 'decaf0000001',
    'data_store': 'decaf0000002',
    'data_ingest': 'decaf0000003',
    'web': 'decaf0000004',
}
PROD_CONFIG_LINE = '/srv/trader_joe/docker-compose.yaml,/srv/trader_joe/docker-compose.web.yaml'


class TwoProjectDaemon:
    """Containers of TWO projects: the dev set in trader_joe_dev and prod's set in trader_joe.

    The mode guard and the label lookups are answered by FILTERING on the argv's project label, as the
    daemon does: a filter for one project lists that project's containers only, and NO project filter
    lists every container (the daemon's behaviour, and what a dropped filter would get). Every other
    step falls through to the harness's answer for the verb.
    """

    def __init__(self, base: Callable[[stack.Step], runner.ProcessResult], dev_line: str) -> None:
        self.base = base
        self.containers = [
            *((DEV_PROJECT, service, dev_line, self._dev_id(service)) for service in stack.DEV_SERVICES),
            *((PROD_PROJECT, service, PROD_CONFIG_LINE, PROD_IDS[service]) for service in stack.DEV_SERVICES),
        ]

    @staticmethod
    def _dev_id(service: str) -> str:
        return DEV_INGEST_CONTAINER_ID if service == 'data_ingest' else DEV_CONTAINER_ID

    def _selected(self, argv) -> list[tuple[str, str, str, str]]:
        projects = _project_filters(argv)
        services = [word.split('=', 2)[2] for word in argv if word.startswith('label=com.docker.compose.service=')]
        return [
            container
            for container in self.containers
            if all(container[0] == project for project in projects)
            and all(container[1] == service for service in services)
        ]

    def __call__(self, step: stack.Step) -> runner.ProcessResult:
        if is_mode_guard(step):
            lines = ''.join(f'{container[2]}\n' for container in self._selected(step.argv))
            return runner.ProcessResult(0, lines.encode(), b'')
        if is_dev_lookup(step):
            ids = ''.join(f'{container[3]}\n' for container in self._selected(step.argv))
            return runner.ProcessResult(0, ids.encode(), b'')
        return self.base(step)


def _two_project_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, dev_line: str | None = None):
    rig = make_rig(tmp_path, monkeypatch, FakeDocker())
    line = dev_line or (TEST_MODE_LINE if verb in SYSTEM_SUITE_VERBS else DEV_MODE_LINE).decode().rstrip('\n')
    daemon = TwoProjectDaemon(response_for(verb, rig.layout), line)
    rig.docker.respond = daemon
    return rig, daemon


@pytest.mark.parametrize('verb', ALL_VERBS)
def test_no_verb_sees_or_touches_a_container_of_the_prod_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """The prod containers are on the daemon, and the verb neither refuses over them nor reaches one."""
    rig, _ = _two_project_rig(tmp_path, monkeypatch, verb)
    result = rig.call(verb, dict(VERB_SAMPLES[verb]))
    assert result['status'] == 'ok', result
    for step in rig.docker.steps:
        assert not set(PROD_IDS.values()) & set(step.argv), f'{verb} reached a prod container: {step.argv}'
        compose = _compose_argv(step)
        if compose is not None:
            assert compose[2:4] == ('-p', DEV_PROJECT), compose
    assert PROD_CONFIG_LINE not in str(result), 'a prod container surfaced in the result'


@pytest.mark.parametrize('verb', ('down', 'restart', 'ps', 'logs'))
def test_the_label_only_verbs_act_on_the_dev_project_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    """No -f, so the project is ALL that says which containers compose acts on: it is trader_joe_dev."""
    rig, _ = _two_project_rig(tmp_path, monkeypatch, verb)
    assert rig.call(verb, dict(VERB_SAMPLES[verb]))['status'] == 'ok'
    composes = [_compose_argv(step) for step in rig.docker.steps if _compose_argv(step) is not None]
    assert len(composes) == 1, composes
    (compose,) = composes
    assert compose[:4] == (DOCKER, 'compose', '-p', DEV_PROJECT), compose
    assert not [word for word in compose[4:] if word.startswith(('-p', '--project', '-f', '--file'))], compose


GUARDED_VERBS = ('down', 'restart', 'up', 'tools', 'test_up', 'migrate', 'migrate_status')
PROD_REFUSAL = (
    'project trader_joe_dev holds a container not started from the dev or test files (a prod launch?): '
    'stop it with make dev-down; tj-oxxl8a'
)
MIXED_REFUSAL = (
    'project trader_joe_dev holds dev and test containers together, so which mode is up cannot be told: '
    'stop them with make dev-down'
)


@pytest.mark.parametrize('verb', GUARDED_VERBS)
def test_a_prod_labelled_container_inside_trader_joe_dev_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """Defence in depth: the mode guard keeps classifying by label, and names make dev-down, never prod-down."""
    rig, daemon = _two_project_rig(tmp_path, monkeypatch, verb)
    daemon.containers.append((DEV_PROJECT, 'web', PROD_CONFIG_LINE, 'feed00000001'))
    result = rig.call(verb, dict(VERB_SAMPLES[verb]))
    assert result['status'] == 'refused' and result['message'] == PROD_REFUSAL, result
    assert [step for step in rig.docker.steps if not is_mode_guard(step)] == [], 'something ran after the refusal'


@pytest.mark.parametrize('verb', GUARDED_VERBS)
def test_dev_and_test_together_inside_trader_joe_dev_are_refused_as_mixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig, daemon = _two_project_rig(tmp_path, monkeypatch, verb)
    daemon.containers.append((DEV_PROJECT, 'data_ingest', TEST_MODE_LINE.decode().rstrip('\n'), 'feed00000002'))
    result = rig.call(verb, dict(VERB_SAMPLES[verb]))
    assert result['status'] == 'refused' and result['message'] == MIXED_REFUSAL, result


@pytest.mark.parametrize('verb', GUARDED_VERBS)
def test_a_test_container_in_the_prod_project_does_not_make_the_dev_project_mixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """The mix is judged inside trader_joe_dev only: another project's containers are not this one's mode."""
    rig, daemon = _two_project_rig(tmp_path, monkeypatch, verb)
    daemon.containers.append((PROD_PROJECT, 'data_ingest', TEST_MODE_LINE.decode().rstrip('\n'), 'feed00000003'))
    assert rig.call(verb, dict(VERB_SAMPLES[verb]))['status'] == 'ok'


# ===============================================================================================
# THE MAKEFILE AND THE DESCRIPTIONS
# ===============================================================================================


def test_the_mcp_and_the_makefile_name_the_same_dev_project():
    """Read from the Makefile as make defines it: the two can never drift apart without this going red."""
    assert _make_variable('DEV_PROJECT') == DEV_PROJECT == stack.DEV_PROJECT
    assert stack.dev_compose_prefix() == [DOCKER, 'compose', '-p', DEV_PROJECT]
    # Prod keeps compose's default: its compose set passes no project, so it can never be trader_joe_dev.
    prod_words = _make_variable('PROD_COMPOSE').split()
    assert not [word for word in prod_words if word.partition('=')[0] in ('-p', '--project-name')], prod_words


# The descriptions that spell a compose command of the project: each names trader_joe_dev in it.
DESCRIBED_COMMANDS = {
    'down': '`docker compose -p trader_joe_dev down`',
    'restart': '`docker compose -p trader_joe_dev restart <service>`',
    'up': 'docker compose -p trader_joe_dev down',
    'test_up': 'docker compose -p trader_joe_dev down',
}


@pytest.mark.parametrize('verb', ALL_VERBS)
def test_no_tool_description_names_the_bare_project(verb: str):
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type] -- only the verb table is read
    text = agent.describe(verb)
    assert not BARE_PROJECT.search(text), f'{verb}: {text}'
    if verb in DESCRIBED_COMMANDS:
        assert DESCRIBED_COMMANDS[verb] in text, f'{verb}: {text}'


def test_the_module_documentation_names_trader_joe_dev_and_never_the_bare_project():
    import tools.agent_mcp as package

    doc = package.__doc__ or ''
    assert 'stack.DEV_PROJECT, trader_joe_dev' in doc and '`docker compose -p trader_joe_dev ps|logs`' in doc
    assert not BARE_PROJECT.search(doc)
