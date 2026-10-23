"""build_infra pins for fake mode: the overlay, SYSTEM_COMPOSE, make system-launch, and the image that holds no fakes.

tj-vhboky.61 (FB-4a). Design: decision tj-j4wknb R4 -- no test instrumentation in production; a
test-only launcher in tests/fakes; a fake-mode compose overlay mounts the test tree read-only into
data_ingest and swaps its command; the prod image holds no fakes -- with the architect's 01:55 UTC
ruling on tj-irhy0a.8 (one worker, literal) and ADR tj-4rr0la addenda 3 (3) and 5 (no file loaded
after the overlay undoes it; tests/fakes is mounted :ro). The separate agent stack is retired (addenda
26-27; tj-iee6sn.27): EVERY file set allowed to load the overlay is enumerated, exactly, by
test_agent_stack_files_retired.py, and the MCP image's copy of the overlay is pinned beside the other
MCP pins (tools/agent_mcp/tests/test_dev_guards.py).

Docker-free, like every build_infra pin: the compose files are read with yaml.safe_load and merged by
compose_model, and make runs for real with `docker` stubbed first on PATH. What only a daemon can show
-- the launcher's WARNING banner in data_ingest's logs, the healthcheck under the swapped command, the
/code/tests mount point under read_only -- is the host sitting's (tj-irhy0a.3).
"""

import ast
import os
import shlex
from pathlib import Path, PurePosixPath

import pytest

from common.tests.compose_model import BASE_FILE, FAKE_FILE, interpolate, load, merge, prod_model, system_model, volume
from common.tests.test_ci_invariants import (
    _ACCEPTED_GUARDS,
    _REFUSED_GUARDS,
    MAKEFILE,
    REPO_ROOT,
    SERVER_ROOT,
    SYSTEM_GUARD,
    _compose_calls,
    _compose_projects,
    _dockerfile_copy_sources,
    _dockerfile_stages,
    _expanded_make_variable,
    _make_recipe,
    _run_make,
    _subprocess_env,
)
from common.tests.test_service_pythonpath import _launch_sets


pytestmark = pytest.mark.build_infra

SYSTEM_LAUNCH = 'system-launch'
FAKES_DIR = REPO_ROOT / 'tests' / 'fakes'
LAUNCHER = FAKES_DIR / 'ingest_launcher.py'
# SERVER_ROOT: data/ingest travels with the service trees (tj-iontkq.4). tests/fakes above does
# not -- it stays at the top of the repository, which is why the two take different roots.
BROKER_API = SERVER_ROOT / 'data' / 'ingest' / 'app' / 'brokers' / 'alpaca' / 'broker_api.py'
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
# The only keys the overlay may set on data_ingest's behaviour: the mount, the command, the blanked keys.
FAKE_KEYS = frozenset({'volumes', 'command', 'environment'})
# The test-mode images (ADR tj-4rr0la addendum 27 (2)): the overlay names the test pair :test, so
# every compose call through it builds and runs :test, never the :latest a deployment may run.
# Spelled out, not read from the overlay: the oracle must not be the file under test.
TEST_IMAGES = {'data_store': 'trader_joe_data_store:test', 'data_ingest': 'trader_joe_data_ingest:test'}
PROD_IMAGES = {'data_store': 'trader_joe_data_store:latest', 'data_ingest': 'trader_joe_data_ingest:latest'}
TEST_BUILD = 'test-build'
# The sets that must never run a fake: the prod, dev and tools stacks, and everything else the
# Makefile starts. SYSTEM_COMPOSE is the only Makefile assignment that names the overlay (SEED_DUMP_COMPOSE
# reaches it through $(SYSTEM_COMPOSE)); the retired agent-stack set was the other (tj-iee6sn.27).
NON_FAKE_VARIABLES = (
    'PROD_COMPOSE',
    'PROD_UP',
    'DEV_COMPOSE',
    'TOOLS_COMPOSE',
    'TEST_CLIENT_COMPOSE',
    'AGENT_COMPOSE',
    'AGENT_MCP_COMPOSE',
)
FAKE_LOADING_VARIABLES = frozenset({'SYSTEM_COMPOSE'})

DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_LOG"
exit 0
"""


# --- the overlay itself: data_ingest only, read-only fakes, the launcher, one worker, no keys -------


def _fake_ingest() -> dict:
    return load(FAKE_FILE)['services']['data_ingest']


def test_the_overlay_sets_the_test_images_and_only_data_ingest_mount_command_and_environment():
    """Item 1: 'for data_ingest ONLY ... Nothing else.' No network, port or other service.

    tj-iee6sn.19 re-pin (ADR tj-4rr0la addendum 27 (1)-(2)): the overlay is now the test-mode
    overlay, so it also names the test pair's images -- data_store gains an entry holding `image`
    and nothing else, and data_ingest's keys gain `image` -- each exactly its :test tag.
    """
    overlay = load(FAKE_FILE)
    assert set(overlay) == {'services'}, f'the fake overlay sets top-level {sorted(overlay)}'
    services = overlay['services']
    assert set(services) == set(TEST_IMAGES), f'the fake overlay touches {sorted(services)}'
    assert set(services['data_store']) == {'image'}, (
        f'the fake overlay sets data_store keys {sorted(services["data_store"])}'
    )
    assert set(_fake_ingest()) == FAKE_KEYS | {'image'}, (
        f'the fake overlay sets data_ingest keys {sorted(_fake_ingest())}'
    )
    assert {name: spec['image'] for name, spec in services.items()} == TEST_IMAGES, services


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


def test_the_system_stack_is_prod_but_for_the_test_tags_and_data_ingest_mount_command_and_keys():
    """Item 2: 'Nothing else differs from prod-launch' -- same build, networks, env files, healthcheck.

    tj-iee6sn.19 re-pin (ADR tj-4rr0la addendum 27 (2)): the one other difference is the tag. The
    test pair is the prod build -- the same build: section, prod_image target -- tagged :test, while
    prod keeps :latest. postgres is untouched.
    """
    system, prod = system_model(), prod_model()
    assert system['networks'] == prod['networks']
    assert set(system['services']) == set(prod['services'])
    for name, spec in prod['services'].items():
        if name not in TEST_IMAGES:
            assert system['services'][name] == spec, f'fake mode changes {name}'
    allowed = {'data_store': {'image'}, 'data_ingest': FAKE_KEYS | {'image'}}
    for name, keys in allowed.items():
        service, prod_service = system['services'][name], prod['services'][name]
        changed = {key for key in set(service) | set(prod_service) if service.get(key) != prod_service.get(key)}
        assert changed == keys, f'fake mode changes {name} {sorted(changed)}, not {sorted(keys)}'
        assert service['build'] == prod_service['build'] and service['build']['target'] == 'prod_image', name
        assert (service['image'], prod_service['image']) == (TEST_IMAGES[name], PROD_IMAGES[name]), name


def _fake_loading_sets() -> dict[str, tuple[Path, ...]]:
    """Every file set that loads the overlay, read from where each is spelled (test_service_pythonpath's reader)."""
    return {label: files for label, files in _launch_sets().items() if FAKE_FILE in files}


def test_every_set_that_loads_the_overlay_runs_the_fake():
    """Addendum 3 (3), re-pointed by tj-iee6sn.27 from the retired agent-stack model to every fake-loading set.

    No file in a set, before the overlay or after it (the test client, the MCP's build overlay), undoes the
    swap: the merged data_ingest runs the launcher, mounts the fakes read-only and has the broker keys
    blank, in make system-launch's, make seed-dump's, the MCP's test_up and test_build sets,
    run_migrations.sh's TEST definition and CI's. Which sets those are is enumerated exactly by
    test_agent_stack_files_retired.py; this asserts what each of them runs.
    """
    sets = _fake_loading_sets()
    assert len(sets) >= 4, f'found only {sorted(sets)}; a parsing slip must not empty this check'
    for label, files in sets.items():
        ingest = merge([load(path) for path in files])['services']['data_ingest']
        assert ingest['command'] == _fake_ingest()['command'], label
        assert FAKE_MOUNT in [volume(entry) for entry in ingest['volumes']], label
        keys = {name: ingest['environment'][name] for name in _credential_vars()}
        assert keys == dict.fromkeys(_credential_vars(), ''), label
        assert ingest['image'] == TEST_IMAGES['data_ingest'], label


# --- who loads the overlay ------------------------------------------------------------------------


def test_system_compose_is_the_base_file_then_the_overlay_in_the_default_project():
    """Item 2: SYSTEM_COMPOSE is docker-compose.yaml plus the fake overlay, nothing else.

    No -p: the default project, as TEST_CLIENT_COMPOSE has, so `make test-system` after `make
    system-launch` joins the fake stack's networks. Never the dev override, the test client, the
    MCP's files, or the tools file.
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


def test_only_system_compose_names_the_overlay_in_the_makefile():
    """Every Makefile line that names the file is the one assignment; any new one goes red.

    tj-iee6sn.27 re-pin: the second assignment, the retired agent-stack set, is gone.
    """
    naming = {line.split(':=')[0].strip() for line in _makefile_code_lines() if FAKE_FILE.name in line}
    assert naming == FAKE_LOADING_VARIABLES, f'Makefile lines naming {FAKE_FILE.name}: {naming}'


SEED_DUMP = 'seed-dump'
SEED_DUMP_COMPOSE = 'SEED_DUMP_COMPOSE'


def test_only_system_launch_seed_dump_and_test_build_reach_system_compose():
    """No other recipe (prod-launch, dev-launch, test-system...) reaches the fake stack's file list.

    Re-pinned by tj-irhy0a.22 item 3: make seed-dump runs the producer 'against the stack
    system-launch started (base + fake overlay + test-client file)', so SYSTEM_COMPOSE has exactly
    two users -- system-launch's recipe and the one SEED_DUMP_COMPOSE assignment -- and
    SEED_DUMP_COMPOSE has exactly one, seed-dump's recipe. Both recipes open with the shared guard.

    tj-iee6sn.19 re-pin (ADR tj-4rr0la addendum 27 (2)): a third user, test-build's one recipe line,
    which builds the :test images through the same file set. It is not behind the guard: a build
    starts nothing and writes no database (test_test_mode_images.py pins its recipe).
    """
    lines = [line.strip().lstrip('@-+') for line in _makefile_code_lines()]
    users = [line for line in lines if '$(SYSTEM_COMPOSE)' in line]
    launch = [line for line in _make_recipe(SYSTEM_LAUNCH) if '$(SYSTEM_COMPOSE)' in line]
    build = [line for line in _make_recipe(TEST_BUILD) if '$(SYSTEM_COMPOSE)' in line]
    seed_assignment = [line for line in users if line.split(':=')[0].strip() == SEED_DUMP_COMPOSE]
    assert len(launch) == len(build) == len(seed_assignment) == 1, f'lines using $(SYSTEM_COMPOSE): {users}'
    assert sorted(users) == sorted(launch + build + seed_assignment), f'lines using $(SYSTEM_COMPOSE): {users}'
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
    """Item 2, re-pinned by ADR tj-4rr0la addendum 26 (4) (tj-iee6sn.18) and tj-iee6sn.24: three docker calls.

    First the presence check of the two :test images (tj-iee6sn.24: one `docker image inspect` of both
    refs, so compose never builds an absent one unstamped). Then the project's down, by dev-up's own
    line -- TOOLS_COMPOSE's file set, a bare `down`, never -v or --remove-orphans -- so only one mode is
    ever up. Then the one up of the fake stack: SYSTEM_COMPOSE with PROD_UP's --wait, from the :test
    images. Nothing builds or pulls, and the only call that starts anything loads the fake overlay.
    """
    env, log = _stubbed(tmp_path, environment)
    result = _run_make(tmp_path, SYSTEM_LAUNCH, *arguments, env=env)
    assert result.returncode == 0 and 'REFUSED' not in result.stderr, f'{result.stdout}{result.stderr}'
    calls = _docker_calls(log)
    assert len(calls) == 3, f'system-launch ran docker {len(calls)} times: {calls}'
    assert calls[0].split() == ['image', 'inspect', 'trader_joe_data_store:test', 'trader_joe_data_ingest:test'], calls
    for call in calls[1:]:
        words = call.split()
        assert words[0] == 'compose' and not {'build', '--build', 'pull'} & set(words), calls
    [(tools_files, _)] = _compose_calls(_expanded_make_variable('TOOLS_COMPOSE', REPO_ROOT, _subprocess_env()))
    [(down_files, down)] = _compose_calls(f'docker {calls[1]}')
    assert down_files == tools_files and down == ['down'], calls
    assert FAKE_FILE.name not in down_files, calls
    [(files, rest)] = _compose_calls(f'docker {calls[2]}')
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
    outside the repository-root tests/ -- neither tests/ itself, nor anything under it, nor (tj-irhy0a.2,
    the architect's 08:35 UTC 2026-09-30 gap) an ANCESTOR of it: `COPY . /code` normalises to '.', which
    would carry tests/fakes into the image while naming no path under tests/. .dockerignore does not
    exclude the top-level tests/ (only the in-package test directories), so this pin, not the ignore
    file, is what keeps the fakes out of every image.
    """
    sources = _dockerfile_copy_sources()
    # Build-context spellings: the service trees are COPYed out of ./server (tj-iontkq.4).
    assert {'server/common', 'server/routers', 'server/schemas', 'server/data/ingest/app'} <= sources, sources
    test_tree = PurePosixPath('tests')
    offending = sorted(
        source
        for source in sources
        if PurePosixPath(source).is_relative_to(test_tree) or test_tree.is_relative_to(PurePosixPath(source))
    )
    assert offending == [], f'the Dockerfile copies the test tree, or a directory holding it: {offending}'


def test_the_system_test_image_copies_no_source():
    """tj-irhy0a.2 item 4: system_test_image adds no COPY or ADD of its own.

    compose bind-mounts its sources read-only (the Dockerfile's comment above the stage), so a test
    edit needs no rebuild and a stale image cannot run old tests. What it inherits from
    base_build_image (pyproject.toml and uv.lock) is that stage's, not this one's.
    """
    stages = _dockerfile_stages()
    assert 'system_test_image' in stages, sorted(stages)
    _, body = stages['system_test_image']
    copies = [line.strip() for line in body.splitlines() if line.strip().split(maxsplit=1)[:1] in (['COPY'], ['ADD'])]
    assert copies == [], f'system_test_image copies files into itself: {copies}'
