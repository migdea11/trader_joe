"""build_infra pins for fake mode: the overlay, SYSTEM_COMPOSE, make system-launch, and the image that holds no fakes.

tj-vhboky.61 (FB-4a). Design: decision tj-j4wknb R4 -- no test instrumentation in production; a
test-only launcher in tests/fakes; a fake-mode compose overlay mounts the test tree read-only into
data_ingest and swaps its command; the prod image holds no fakes -- with the architect's 01:55 UTC
ruling on tj-irhy0a.8 (one worker, literal) and ADR tj-4rr0la addenda 3 (3) and 5 (the agent stack
loads the overlay LAST; tests/fakes is snapshot-sourced and mounted :ro). The agent stack's file list
and the MCP image's copy of the overlay are pinned beside the other MCP pins
(test_agent_stack_compose.py, tools/agent_mcp/tests/test_commands.py).

Docker-free, like every build_infra pin: the compose files are read with yaml.safe_load and merged by
compose_model, and make runs for real with `docker` stubbed first on PATH. What only a daemon can show
-- the launcher's WARNING banner in data_ingest's logs, the healthcheck under the swapped command, the
/code/tests mount point under read_only -- is the host sitting's (tj-irhy0a.3).
"""

import ast
import os
import shlex
from pathlib import Path

import pytest

from common.tests.compose_model import (
    BASE_FILE,
    FAKE_FILE,
    agent_stack_model,
    interpolate,
    load,
    prod_model,
    system_model,
    volume,
)
from common.tests.test_ci_invariants import (
    _ACCEPTED_GUARDS,
    _REFUSED_GUARDS,
    MAKEFILE,
    REPO_ROOT,
    SYSTEM_GUARD,
    _compose_calls,
    _compose_projects,
    _dockerfile_copy_sources,
    _expanded_make_variable,
    _make_recipe,
    _run_make,
    _subprocess_env,
)


pytestmark = pytest.mark.build_infra

SYSTEM_LAUNCH = 'system-launch'
FAKES_DIR = REPO_ROOT / 'tests' / 'fakes'
LAUNCHER = FAKES_DIR / 'ingest_launcher.py'
BROKER_API = REPO_ROOT / 'data' / 'ingest' / 'app' / 'brokers' / 'alpaca' / 'broker_api.py'
# Spelled out, not read from the overlay: the oracle must not be the file under test.
FAKE_MOUNT = {'type': 'bind', 'source': './tests/fakes', 'target': '/code/tests/fakes', 'read_only': True}
# The swapped command after compose's interpolation ($$ -> $), as the container's /bin/sh -c sees it:
# entrypoint.sh's host and port variable, the launcher's app, and ONE worker, literal -- FakeRead's
# FAILONCE_ memory is per process, so every extra worker would fail once more (ruling on tj-irhy0a.8).
FAKE_SHELL_WORDS = [
    'exec',
    'uvicorn',
    'tests.fakes.ingest_launcher:app',
    '--host',
    '0.0.0.0',
    '--port',
    '$APP_INTERNAL_PORT',
    '--workers',
    '1',
]
# The only keys the overlay may set on data_ingest: the mount, the command, the blanked keys.
FAKE_KEYS = frozenset({'volumes', 'command', 'environment'})
# The sets that must never run a fake: the prod, dev and tools stacks, and everything else the
# Makefile starts. SYSTEM_COMPOSE and AGENT_STACK_COMPOSE are the only two that load the overlay.
NON_FAKE_VARIABLES = (
    'PROD_COMPOSE',
    'PROD_UP',
    'DEV_COMPOSE',
    'TOOLS_COMPOSE',
    'TEST_CLIENT_COMPOSE',
    'AGENT_COMPOSE',
    'AGENT_MCP_COMPOSE',
)
FAKE_LOADING_VARIABLES = frozenset({'SYSTEM_COMPOSE', 'AGENT_STACK_COMPOSE'})

DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_LOG"
exit 0
"""


# --- the overlay itself: data_ingest only, read-only fakes, the launcher, one worker, no keys -------


def _fake_ingest() -> dict:
    return load(FAKE_FILE)['services']['data_ingest']


def test_the_overlay_touches_data_ingest_only_and_only_its_mount_command_and_environment():
    """Item 1: 'for data_ingest ONLY ... Nothing else.' No network, port, image or other service."""
    overlay = load(FAKE_FILE)
    assert set(overlay) == {'services'}, f'the fake overlay sets top-level {sorted(overlay)}'
    assert set(overlay['services']) == {'data_ingest'}, f'the fake overlay touches {sorted(overlay["services"])}'
    assert set(_fake_ingest()) == FAKE_KEYS, f'the fake overlay sets data_ingest keys {sorted(_fake_ingest())}'


def test_the_overlay_mounts_exactly_tests_fakes_read_only():
    """Item 1 and addendum 5: one bind, ./tests/fakes at /code/tests/fakes, :ro -- never writable."""
    mounts = [volume(entry) for entry in _fake_ingest()['volumes']]
    assert mounts == [FAKE_MOUNT], mounts
    assert FAKES_DIR.is_dir(), f'the mount source {FAKES_DIR} does not exist'


def test_the_command_runs_the_launcher_with_one_literal_worker():
    """Item 1 and the 01:55 UTC ruling: the launcher's app, entrypoint.sh's host and port, --workers 1."""
    command = _fake_ingest()['command']
    assert isinstance(command, list) and command[:2] == ['/bin/sh', '-c'] and len(command) == 3, command
    shell = interpolate(command[2], {})
    assert shlex.split(shell) == FAKE_SHELL_WORDS, shell
    assert 'SERVICE_WORKERS' not in command[2], 'the worker count must never come from an env file'


def test_the_launcher_the_command_names_exists_and_exposes_app():
    """The command's module:attribute resolves: tests/fakes/ingest_launcher.py assigns a module-level app."""
    module, _, attribute = FAKE_SHELL_WORDS[2].partition(':')
    assert REPO_ROOT.joinpath(*module.split('.')).with_suffix('.py') == LAUNCHER
    tree = ast.parse(LAUNCHER.read_text(encoding='utf-8'))
    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    assert attribute in assigned, f'{LAUNCHER.name} assigns no module-level {attribute!r}'


def test_tests_is_a_namespace_package_so_tests_fakes_resolves_in_the_image():
    """The builder's check, kept: /code/tests is a bare mount point, so `tests` must need no __init__.py.

    With PYTHONPATH=/code the image resolves tests.fakes only as a regular package under a namespace
    `tests`; a committed tests/__init__.py would make the host import path differ from the container's.
    """
    assert not (REPO_ROOT / 'tests' / '__init__.py').exists()
    assert (FAKES_DIR / '__init__.py').is_file()


def _credential_vars() -> set[str]:
    """ALPACA_CREDENTIAL_VARS from broker_api.py, by ast -- the names production reads its keys from."""
    for node in ast.parse(BROKER_API.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == 'ALPACA_CREDENTIAL_VARS' for target in node.targets
        ):
            return set(ast.literal_eval(node.value))
    raise AssertionError(f'{BROKER_API} defines no ALPACA_CREDENTIAL_VARS')


def test_the_overlay_blanks_exactly_the_broker_keys_production_reads():
    """Item 1 (ops hygiene, tj-0rpt9t): environment: outranks env_file:, so a host key never arrives.

    Empty, not absent, and exactly the names production reads -- a renamed variable would otherwise
    leave the new name unblanked while this file still said ALPACA_API_KEY.
    """
    environment = _fake_ingest()['environment']
    assert isinstance(environment, dict), environment
    assert environment == dict.fromkeys(_credential_vars(), ''), environment


# --- the merged stacks: fake mode changes data_ingest and nothing else ------------------------------


def test_the_system_stack_is_prod_but_for_data_ingest_mount_command_and_keys():
    """Item 2: 'Nothing else differs from prod-launch' -- same images, networks, env files, healthcheck."""
    system, prod = system_model(), prod_model()
    assert system['networks'] == prod['networks']
    assert set(system['services']) == set(prod['services'])
    for name, spec in prod['services'].items():
        if name != 'data_ingest':
            assert system['services'][name] == spec, f'fake mode changes {name}'
    ingest, prod_ingest = system['services']['data_ingest'], prod['services']['data_ingest']
    changed = {key for key in set(ingest) | set(prod_ingest) if ingest.get(key) != prod_ingest.get(key)}
    assert changed <= FAKE_KEYS, f'fake mode changes data_ingest {sorted(changed)}'
    assert ingest['build']['target'] == 'prod_image' and ingest['image'] == prod_ingest['image']


def test_the_agent_stack_runs_the_fake_because_the_overlay_is_last():
    """Addendum 3 (3): no earlier file can undo the swap -- the merged agent model runs the launcher."""
    ingest = agent_stack_model()['services']['data_ingest']
    assert ingest['command'] == _fake_ingest()['command']
    assert FAKE_MOUNT in [volume(entry) for entry in ingest['volumes']]
    assert {name: ingest['environment'][name] for name in _credential_vars()} == dict.fromkeys(_credential_vars(), '')


# --- who loads the overlay ------------------------------------------------------------------------


def test_system_compose_is_the_base_file_then_the_overlay_in_the_default_project():
    """Item 2: SYSTEM_COMPOSE is docker-compose.yaml plus the fake overlay, nothing else.

    No -p: the default project, as TEST_CLIENT_COMPOSE has, so `make test-system` after `make
    system-launch` joins the fake stack's networks. Never the dev override, the test client, the
    agent-stack or MCP files, or the tools file.
    """
    env = _subprocess_env()
    expanded = _expanded_make_variable('SYSTEM_COMPOSE', REPO_ROOT, env)
    assert _compose_calls(expanded) == [([BASE_FILE.name, FAKE_FILE.name], [])], expanded
    client = _expanded_make_variable('TEST_CLIENT_COMPOSE', REPO_ROOT, env)
    assert _compose_projects(expanded) == _compose_projects(client) == [None], (expanded, client)


@pytest.mark.parametrize('variable', NON_FAKE_VARIABLES)
def test_no_prod_dev_or_tools_set_loads_the_fake_overlay(variable: str):
    """R4 and the overlay's header: PROD_COMPOSE (and every other set) never runs the fake."""
    expanded = _expanded_make_variable(variable, REPO_ROOT, _subprocess_env())
    assert FAKE_FILE.name not in expanded, f'{variable} ({expanded}) loads {FAKE_FILE.name}'


def _makefile_code_lines() -> list[str]:
    text = MAKEFILE.read_text(encoding='utf-8').replace('\\\n', ' ')
    return [line for line in text.splitlines() if not line.lstrip().startswith('#')]


def test_only_system_compose_and_the_agent_stack_name_the_overlay_in_the_makefile():
    """Every Makefile line that names the file is one of the two assignments; any new one goes red."""
    naming = {line.split(':=')[0].strip() for line in _makefile_code_lines() if FAKE_FILE.name in line}
    assert naming == FAKE_LOADING_VARIABLES, f'Makefile lines naming {FAKE_FILE.name}: {naming}'


SEED_DUMP = 'seed-dump'
SEED_DUMP_COMPOSE = 'SEED_DUMP_COMPOSE'


def test_only_system_launch_and_seed_dump_reach_system_compose():
    """No other recipe (prod-launch, dev-launch, test-system...) reaches the fake stack's file list.

    Re-pinned by tj-irhy0a.22 item 3: make seed-dump runs the producer 'against the stack
    system-launch started (base + fake overlay + test-client file)', so SYSTEM_COMPOSE has exactly
    two users -- system-launch's recipe and the one SEED_DUMP_COMPOSE assignment -- and
    SEED_DUMP_COMPOSE has exactly one, seed-dump's recipe. Both recipes open with the shared guard.
    """
    lines = [line.strip().lstrip('@-+') for line in _makefile_code_lines()]
    users = [line for line in lines if '$(SYSTEM_COMPOSE)' in line]
    launch = [line for line in _make_recipe(SYSTEM_LAUNCH) if '$(SYSTEM_COMPOSE)' in line]
    seed_assignment = [line for line in users if line.split(':=')[0].strip() == SEED_DUMP_COMPOSE]
    assert len(launch) == 1 and len(seed_assignment) == 1, f'lines using $(SYSTEM_COMPOSE): {users}'
    assert sorted(users) == sorted(launch + seed_assignment), f'lines using $(SYSTEM_COMPOSE): {users}'
    seed_users = [line for line in lines if f'$({SEED_DUMP_COMPOSE})' in line]
    seed_recipe = [line for line in _make_recipe(SEED_DUMP) if f'$({SEED_DUMP_COMPOSE})' in line]
    assert len(seed_users) == 1 and seed_users == seed_recipe, f'lines using $({SEED_DUMP_COMPOSE}): {seed_users}'
    for target in (SYSTEM_LAUNCH, SEED_DUMP):
        assert _make_recipe(target)[0] == '$(SYSTEM_TEST_DISPOSABLE_GUARD)', (target, _make_recipe(target))


# --- make system-launch, run for real with docker stubbed ------------------------------------------


def _stubbed(tmp_path: Path, guard: str | None) -> tuple[dict[str, str], Path]:
    stubs, log = tmp_path / 'stubs', tmp_path / 'docker.log'
    stubs.mkdir()
    (stubs / 'docker').write_text(DOCKER_STUB)
    (stubs / 'docker').chmod(0o755)
    env = _subprocess_env(**{SYSTEM_GUARD: guard})
    env.update(PATH=f'{stubs}:{os.environ["PATH"]}', STUB_LOG=str(log))
    return env, log


def _docker_calls(log: Path) -> list[str]:
    return log.read_text(encoding='utf-8').splitlines() if log.exists() else []


def test_system_launch_reuses_the_test_system_guard_not_a_copy():
    """Item 2: 'behind the SAME disposable-database guard as test-system (reuse it, do not copy it)'."""
    for target in (SYSTEM_LAUNCH, 'test-system'):
        assert _make_recipe(target)[0] == '$(SYSTEM_TEST_DISPOSABLE_GUARD)', (target, _make_recipe(target))
    checks = [line for line in _makefile_code_lines() if f'$({SYSTEM_GUARD})' in line and '!=' in line]
    assert len(checks) == 1, f'the disposable-database check is spelled {len(checks)} times: {checks}'


@pytest.mark.parametrize(('arguments', 'environment'), list(_REFUSED_GUARDS.values()), ids=list(_REFUSED_GUARDS))
def test_system_launch_refuses_without_the_attestation_and_never_reaches_docker(
    tmp_path: Path, arguments: list[str], environment: str | None
):
    """Item 2 DONE WHEN: the refusal without SYSTEM_TEST_DISPOSABLE_DB=1 -- non-zero, the reason, no docker.

    On a host that runs production, the default project IS production's: an unguarded system-launch
    would recreate its data_ingest as the fake and write fake bars into its database.
    """
    env, log = _stubbed(tmp_path, environment)
    result = _run_make(tmp_path, SYSTEM_LAUNCH, *arguments, env=env)
    assert result.returncode != 0, f'make {SYSTEM_LAUNCH} {arguments} ran with the guard {environment!r}'
    for phrase in (
        f'make {SYSTEM_LAUNCH} REFUSED',
        'WRITES to the database it is pointed at',
        'production deployment',
        f'{SYSTEM_GUARD}=1',
    ):
        assert phrase in result.stderr, f'the refusal does not say {phrase!r}:\n{result.stderr}'
    assert _docker_calls(log) == [], f'docker ran before the guard refused: {_docker_calls(log)}'


@pytest.mark.parametrize(('arguments', 'environment'), list(_ACCEPTED_GUARDS.values()), ids=list(_ACCEPTED_GUARDS))
def test_system_launch_starts_exactly_the_fake_stack_and_never_builds(
    tmp_path: Path, arguments: list[str], environment: str | None
):
    """Item 2: one docker call -- SYSTEM_COMPOSE up with PROD_UP's --wait -- from the prod images, no build."""
    env, log = _stubbed(tmp_path, environment)
    result = _run_make(tmp_path, SYSTEM_LAUNCH, *arguments, env=env)
    assert result.returncode == 0 and 'REFUSED' not in result.stderr, f'{result.stdout}{result.stderr}'
    calls = _docker_calls(log)
    assert len(calls) == 1, f'system-launch ran docker {len(calls)} times: {calls}'
    words = calls[0].split()
    assert words[0] == 'compose' and not {'build', '--build', 'pull'} & set(words), calls
    [(files, rest)] = _compose_calls(f'docker {calls[0]}')
    assert files == [BASE_FILE.name, FAKE_FILE.name], calls
    [(_, prod_up)] = _compose_calls(_expanded_make_variable('PROD_UP', REPO_ROOT, _subprocess_env()))
    assert rest == prod_up == ['up', '-d', '--wait', '--wait-timeout', '300'], (rest, prod_up)


def test_system_launch_has_no_prerequisite_that_builds_or_syncs():
    """'No build ... No $(VENV_MARKER)': nothing runs before the guard, so a refusal costs nothing."""
    text = MAKEFILE.read_text(encoding='utf-8')
    header = next(line for line in text.splitlines() if line.startswith(f'{SYSTEM_LAUNCH}:'))
    prerequisites = header.split(':', 1)[1].split('#')[0].split()
    assert prerequisites == [], f'{SYSTEM_LAUNCH} depends on {prerequisites}'


def test_system_launch_is_phony():
    """Undeclared, a file named system-launch at the root would make the target a silent no-op."""
    phony = [line for line in MAKEFILE.read_text(encoding='utf-8').splitlines() if line.startswith('.PHONY:')]
    assert any(SYSTEM_LAUNCH in line.split(':', 1)[1].split() for line in phony)


# --- R4: the prod image holds no fakes ------------------------------------------------------------


def test_no_dockerfile_copy_reads_the_test_tree():
    """R4: the fakes reach a container only through the overlay's mount, never an image layer.

    Every context path the root Dockerfile COPYs or ADDs (build args expanded from compose) lies
    outside the repository-root tests/ -- neither tests/ itself nor anything under it.
    """
    sources = _dockerfile_copy_sources()
    assert {'common', 'routers', 'schemas', 'data/ingest/app'} <= sources, sources
    offending = sorted(source for source in sources if source == 'tests' or source.startswith('tests/'))
    assert offending == [], f'the Dockerfile copies the test tree: {offending}'
