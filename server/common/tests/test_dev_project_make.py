"""Dev and test run as compose project trader_joe_dev; prod keeps its default project (tj-grna9p.161).

Design: ADR tj-4rr0la addendum 29, the owner's ruling on tj-oxxl8a (option A). Every Makefile compose
set that is not prod's carries -p $(DEV_PROJECT), DEV_PROJECT is trader_joe_dev, and PROD_COMPOSE --
with every target built on it -- carries no -p, so prod keeps compose's default project and today's
launch. A dev/test teardown therefore reaches only trader_joe_dev. CI's System Testing job sets
COMPOSE_PROJECT_NAME: trader_joe_dev at job level so its direct compose calls address the project
make system-launch starts. No compose file names a project, and .env.default sets none.

The recipe pins read what make itself prints (`make -n`), not the variables' source text, so a recipe
that spells its own compose line, or a set that loses its -p, is red here even where the variable
pins elsewhere stay green.
"""

import re
from pathlib import Path

import pytest

from common.tests.compose_model import AGENT_MCP_FILE, load
from common.tests.test_ci_invariants import (
    DEV_PROJECT,
    ENV_DEFAULT_FILE,
    MAKEFILE,
    REPO_ROOT,
    SYSTEM_GUARD,
    SYSTEM_JOB_NAME,
    TESTING_WORKFLOW,
    _compose_calls,
    _compose_projects,
    _expanded_make_variable,
    _load_yaml,
    _run_make,
    _step_lines,
    _subprocess_env,
    _system_job,
    _system_steps,
)


pytestmark = pytest.mark.build_infra

PROJECT_OPTIONS = ('-p', '--project-name')
# Every target whose recipe reaches a dev or test compose set, and so must address trader_joe_dev only.
DEV_TARGETS = (
    'dev-build',
    'leave-fake-mode',
    'dev-deps',
    'dev-tools',
    'dev-launch',
    'dev-up',
    'dev-logs',
    'dev-down',
    'test-build',
    'system-launch',
    'test-system',
    'seed-dump',
)
# The dev/test targets that tear something down: each down must be there AND reach trader_joe_dev.
TEARDOWN_TARGETS = ('leave-fake-mode', 'dev-up', 'dev-down', 'system-launch')
# Every target built on PROD_COMPOSE (or PROD_UP, which is PROD_COMPOSE plus an up).
PROD_TARGETS = ('prod-build', 'prod-build-clean', 'prod-deps', 'prod-launch', 'prod-logs', 'prod-down')
# The devcontainer's and the MCP's own compose targets: neither prod's project nor the dev/test one.
AGENT_TARGETS = ('agent-build', 'agent-up', 'agent-down', 'agent-attach', 'agent-mcp-up', 'agent-mcp-rebuild')
# The compose sets that are prod's, and the agent sets that keep their own project handling.
PROD_SETS = frozenset({'PROD_COMPOSE', 'PROD_UP'})
AGENT_SETS = frozenset({'AGENT_COMPOSE', 'AGENT_MCP_COMPOSE'})
# Every other Makefile *_COMPOSE set is a dev or test set.
DEV_SETS = frozenset({'DEV_COMPOSE', 'TOOLS_COMPOSE', 'TEST_CLIENT_COMPOSE', 'SYSTEM_COMPOSE', 'SEED_DUMP_COMPOSE'})
# The one compose file that names a project of its own, and the name it gives.
NAMED_COMPOSE = {AGENT_MCP_FILE.name: 'trader_joe_agent_mcp'}
_SET_REFERENCE = re.compile(r'\$\((\w+_COMPOSE|PROD_UP)\)')
_TARGET_HEADER = re.compile(r'^([A-Za-z0-9_.-]+)\s*:(?![=:])')
_SET_ASSIGNMENT = re.compile(r'^(\w+_COMPOSE|PROD_UP)\s*:?=', re.MULTILINE)


def _make_n(tmp_path: Path, target: str) -> str:
    """`make -n TARGET` with the attestation given, run from an empty directory, continuations folded.

    proto and gen-proto-ts are treated as up to date, so nothing but the target's compose lines and
    their prerequisites' print; COMPOSE_PROJECT_NAME is removed so the caller's shell cannot lend a
    project to a line that names none.
    """
    env = _subprocess_env(**{SYSTEM_GUARD: None, 'COMPOSE_PROJECT_NAME': None})
    result = _run_make(tmp_path, '-n', '-o', 'proto', '-o', 'gen-proto-ts', target, f'{SYSTEM_GUARD}=1', env=env)
    assert result.returncode == 0, f'make -n {target} failed: {result.stderr}'
    return re.sub(r'\\\n\t?', ' ', result.stdout)


def _projects(text: str) -> list[str | None]:
    return [project for line in text.splitlines() for project in _compose_projects(line)]


def _recipes() -> dict[str, list[str]]:
    """Every target's recipe lines, continuations folded, as the Makefile spells them."""
    recipes, target = {}, None
    for line in MAKEFILE.read_text(encoding='utf-8').replace('\\\n', ' ').splitlines():
        header = _TARGET_HEADER.match(line)
        if header and not line.startswith('\t'):
            target = header.group(1)
            recipes.setdefault(target, [])
        elif line.startswith('\t') and target is not None:
            recipes[target].append(line)
        elif line.strip() and not line.startswith('#'):
            target = None
    return recipes


# --- the variable ---------------------------------------------------------------------------------


def test_dev_project_is_trader_joe_dev():
    assert _expanded_make_variable('DEV_PROJECT', REPO_ROOT, _subprocess_env()) == DEV_PROJECT


def test_every_makefile_compose_set_is_classified():
    """A new *_COMPOSE set must be added to one class here, so it cannot dodge the project pins."""
    sets = set(_SET_ASSIGNMENT.findall(MAKEFILE.read_text(encoding='utf-8')))
    assert sets == PROD_SETS | AGENT_SETS | DEV_SETS, sorted(sets ^ (PROD_SETS | AGENT_SETS | DEV_SETS))


@pytest.mark.parametrize('variable', sorted(DEV_SETS))
def test_every_dev_and_test_set_carries_exactly_the_dev_project(variable: str):
    expanded = _expanded_make_variable(variable, REPO_ROOT, _subprocess_env())
    assert _compose_projects(expanded) == [DEV_PROJECT], expanded
    words = expanded.split()
    assert sum(word.partition('=')[0] in PROJECT_OPTIONS for word in words) == 1, expanded


@pytest.mark.parametrize('variable', sorted(PROD_SETS))
def test_no_prod_set_carries_a_project_or_names_the_dev_project(variable: str):
    """Addendum 29 (1): PROD_COMPOSE stays without -p, byte-identical in effect to today's."""
    expanded = _expanded_make_variable(variable, REPO_ROOT, _subprocess_env())
    assert _compose_projects(expanded) == [None], expanded
    assert DEV_PROJECT not in expanded and 'DEV_PROJECT' not in expanded, expanded
    assert not {'--project-name', '--project-directory', '-p'} & set(expanded.split()), expanded


# --- the recipes, as make prints them --------------------------------------------------------------


def test_every_target_reaching_a_compose_set_is_classified():
    """Every target whose recipe names a compose set is a dev, prod or agent target listed above."""
    reaching = {target for target, lines in _recipes().items() if any(_SET_REFERENCE.search(line) for line in lines)}
    assert reaching == set(DEV_TARGETS) | set(PROD_TARGETS) | set(AGENT_TARGETS), sorted(
        reaching ^ (set(DEV_TARGETS) | set(PROD_TARGETS) | set(AGENT_TARGETS))
    )


@pytest.mark.parametrize('target', DEV_TARGETS)
def test_every_compose_call_of_a_dev_or_test_target_addresses_only_the_dev_project(tmp_path: Path, target: str):
    projects = _projects(_make_n(tmp_path, target))
    assert projects, f'make -n {target} printed no compose call, so this pin looked at nothing'
    assert set(projects) == {DEV_PROJECT}, (target, projects)


@pytest.mark.parametrize('target', TEARDOWN_TARGETS)
def test_every_dev_and_test_teardown_reaches_only_the_dev_project(tmp_path: Path, target: str):
    """Addendum 29 (2): a dev/test down can no longer reach prod by name, because it names trader_joe_dev."""
    text = _make_n(tmp_path, target)
    downs = [
        project
        for line in text.splitlines()
        for (_, rest), project in zip(_compose_calls(line), _compose_projects(line), strict=True)
        if rest and rest[0].rstrip(';') == 'down'  # leave-fake-mode's down ends a shell list: `down;`
    ]
    assert downs, f'make -n {target} printed no compose down'
    assert downs == [DEV_PROJECT] * len(downs), (target, downs)


@pytest.mark.parametrize('target', PROD_TARGETS)
def test_no_prod_target_carries_a_project_or_mentions_the_dev_project(tmp_path: Path, target: str):
    text = _make_n(tmp_path, target)
    projects = _projects(text)
    assert projects, f'make -n {target} printed no compose call, so this pin looked at nothing'
    assert projects == [None] * len(projects), (target, projects)
    assert DEV_PROJECT not in text, text


# --- the hazard comment ----------------------------------------------------------------------------


def test_the_hazard_comment_sits_above_prod_compose():
    """The block above PROD_COMPOSE says prod keeps no -p, and that a shared host fails loudly, safely."""
    lines = MAKEFILE.read_text(encoding='utf-8').splitlines()
    index = next(number for number, line in enumerate(lines) if line.startswith('PROD_COMPOSE :='))
    block = []
    for line in reversed(lines[:index]):
        if not line.strip():
            break
        block.append(line)
    text = ' '.join(line.lstrip('# ') for line in reversed(block))
    assert 'DEV_PROJECT := trader_joe_dev' in text, text
    for phrase in ('addendum 29', 'tj-oxxl8a', 'PROD_COMPOSE carries NO -p', 'THE HAZARD THAT REMAINS', 'fails LOUDLY'):
        assert phrase in text, (phrase, text)
    assert 'no target here tears down the other project' in text, text


# --- the guard -------------------------------------------------------------------------------------


@pytest.mark.parametrize('target', ['test-system', 'system-launch', 'seed-dump'])
def test_the_guard_still_refuses_and_names_the_dev_project_beside_the_production_deployment(
    tmp_path: Path, target: str
):
    """Addendum 29 (4): SYSTEM_TEST_DISPOSABLE_DB=1 stays; the message no longer says prod shares the project."""
    env = _subprocess_env(**{SYSTEM_GUARD: None})
    result = _run_make(tmp_path, target, env=env)
    assert result.returncode != 0, result.stdout
    message = ' '.join(result.stderr.split())
    assert f'make {target} REFUSED' in message and f'{SYSTEM_GUARD}=1' in message, message
    assert 'production deployment' in message and f'({DEV_PROJECT})' in message, message
    assert 'same compose project' not in message, message


# --- CI ---------------------------------------------------------------------------------------------


def test_the_system_job_sets_the_dev_project_at_job_level():
    """The bead's choice: one job-level COMPOSE_PROJECT_NAME every direct compose call inherits."""
    env = _system_job().get('env') or {}
    assert env.get('COMPOSE_PROJECT_NAME') == DEV_PROJECT, f'{SYSTEM_JOB_NAME} job env: {env}'


def test_no_system_step_overrides_the_project():
    """No step env, run line or direct compose call moves a call off trader_joe_dev."""
    for step in _system_steps():
        assert 'COMPOSE_PROJECT_NAME' not in (step.get('env') or {}), step.get('name')
        for line in _step_lines(step):
            assert 'COMPOSE_PROJECT_NAME' not in line, (step.get('name'), line)
            assert '--project-directory' not in line, (step.get('name'), line)
            for project in _compose_projects(line):
                assert project in (None, DEV_PROJECT), (step.get('name'), line)


def test_only_the_system_job_sets_a_compose_project_name():
    """Not at workflow level and not in another job: Image Build's renders stay project-free by design."""
    workflow = _load_yaml(TESTING_WORKFLOW) or {}
    assert 'COMPOSE_PROJECT_NAME' not in (workflow.get('env') or {}), workflow.get('env')
    setters = [
        job.get('name')
        for job in (workflow.get('jobs') or {}).values()
        if 'COMPOSE_PROJECT_NAME' in ((job or {}).get('env') or {})
    ]
    assert setters == [SYSTEM_JOB_NAME], setters


# --- compose files and .env.default ----------------------------------------------------------------


def test_no_compose_file_names_a_project_but_the_mcps_own():
    """No top-level name: in any repo-root compose file, so neither prod's default nor -p is overridden."""
    files = sorted(REPO_ROOT.glob('docker-compose*.yaml'))
    assert len(files) > len(NAMED_COMPOSE), [path.name for path in files]
    names = {path.name: load(path).get('name') for path in files}
    assert {name: value for name, value in names.items() if value is not None} == NAMED_COMPOSE, names


def test_env_default_sets_no_compose_project_name():
    assert 'COMPOSE_PROJECT_NAME' not in ENV_DEFAULT_FILE.read_text(encoding='utf-8')
