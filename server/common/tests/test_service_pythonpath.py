"""build_infra pins for the service containers' runtime PYTHONPATH (tj-3mk3u5.56; decision tj-3mk3u5.42 addendum F1-A).

THE FINDING (tj-3mk3u5.44). The committed root env file, .env.default, ships a legacy PYTHONPATH=./. Every
service loads the root env file through env_file:, and compose ranks environment: over env_file: over the
image's ENV. So the services ran with PYTHONPATH=./, /code/gen/proto/python was off their path, and every
static pin of the Dockerfile stayed green, because the Dockerfile is not where the value is lost. The fix
(option A) restates the image's value, literally, in docker-compose.yaml's environment:.

The bead's validator gate, one section each:

  V1. For every launch set and every service built from the Dockerfile, the EFFECTIVE container PYTHONPATH --
      environment: merged across the set's files, else the env files' value (each read as its committed
      template, so the root one is .env.default with its PYTHONPATH=./), else the image stage's ENV -- EQUALS
      the Dockerfile's ENV PYTHONPATH for the stage the service builds, and so names /code/gen/proto/python.
      The services are derived from the compose files' build: sections, never listed, so a new service is
      covered the day it appears. The launch sets are read from where they are spelled: the Makefile's
      *_COMPOSE variables as make expands them, tools/agent_mcp/stack.py COMPOSE_FILES, every `docker
      compose` in the workflows, and data/store/run_migrations.sh. test_client is a derived service, so
      its PYTHONPATH is held EQUAL to the Dockerfile's, tightening tj-3mk3u5.44 gate item 7 (its
      working_dir form stays pinned by test_ci_invariants.py test_the_client_hands_the_suite_the_env_contract).
  V2. The Dockerfile's ENV PYTHONPATH values are equal. Already pinned:
      test_ci_invariants.py test_every_image_runs_with_the_pythonpath_the_suite_mirrors.
  V3. The value is literal -- no '$', no pass-through -- and the effective value does not move whatever the
      interpolation environment holds.
  V4. Non-vacuity. The same checks, run on the committed files with one change made in memory, see every
      way the path is lost: the entry dropped (the value then comes out as './', the env file's, so the
      env-file branch is live, not vacuous), the generated root dropped, the value interpolated, and the
      Dockerfile changed alone.
  V5. System Testing's Check Generated Code Import step, its own script run under bash with `docker`
      stubbed (import_path_docker_stub.py, which runs each exec'd -c in a fresh interpreter whose import
      path is the container's PYTHONPATH). Every running service built from the Dockerfile imports
      common.rpc.ping under the container environment the V1 model computes for the stack the job starts.
      When the path is lost the import really fails, and the step exits non-zero naming the service and
      the PYTHONPATH it saw. Its place in the job, after the stack is up, and its compose spelling are
      pinned with the rest of the job in test_ci_invariants.py (IMPORT_CHECK_STEP).

What only Docker can show -- the image build, compose's real render and precedence, the step against the
live stack -- is CI System Testing's run, and for the agent stack the user's batched MCP rebuild
(tj-3mk3u5.52, check H). CI runs this suite as root, so nothing here depends on mode bits being enforced:
the shim is made executable and that is all.
"""

import copy
import functools
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

import pytest

from common.tests import import_path_docker_stub as stub
from common.tests.compose_model import BASE_FILE, interpolate, load, merge
from common.tests.image_path import IMAGE_CODE_ROOT, IMAGE_PYTHONPATH_ENTRIES, image_pythonpath
from common.tests.test_ci_invariants import (
    ENV_DEFAULT_FILE,
    IMPORT_CHECK_STEP,
    MAKEFILE,
    MIGRATIONS_SCRIPT,
    PROJECT_DOCKERFILE,
    REPO_ROOT,
    SERVER_ROOT,
    _compose_calls,
    _container_env,
    _dockerfile_stages,
    _env_file_values,
    _every_workflow_step,
    _image_env,
    _image_pythonpath_model,
    _run_lines,
    _runtime_path_entries,
    _system_step,
)
from common.tests.test_grpc_bind_network import _COMPOSE_SET, _environment, _rendered, _set_files
from tools.agent_mcp import stack


pytestmark = pytest.mark.build_infra

VARIABLE = 'PYTHONPATH'
GENERATED_ROOT = str(IMAGE_CODE_ROOT / IMAGE_PYTHONPATH_ENTRIES[-1])
# What a container gets without the entry: the committed root env file's legacy value (tj-3mk3u5.44).
LEGACY_VALUE = './'
# Floors, so a parsing slip cannot shrink a derived set to nothing. Never the list itself: the services
# and the sets checked are whatever the files say.
KNOWN_SERVICES = frozenset({'data_store', 'data_ingest', 'test_client'})
KNOWN_MAKEFILE_SETS = frozenset(
    {
        'PROD_COMPOSE',
        'DEV_COMPOSE',
        'TOOLS_COMPOSE',
        'TEST_CLIENT_COMPOSE',
        'AGENT_STACK_COMPOSE',
        'SYSTEM_COMPOSE',
        'SEED_DUMP_COMPOSE',
    }
)
MAKEFILE_SOURCE = 'Makefile'
STACK_SOURCE = 'tools/agent_mcp/stack.py COMPOSE_FILES'
MIGRATIONS_SOURCE = 'server/data/store/run_migrations.sh'
WORKFLOW_SOURCE = 'workflow'
# compose's own default when an invocation names no -f: the base file, then the override beside it.
DEFAULT_COMPOSE_FILES = ('docker-compose.yaml', 'docker-compose.override.yaml')
# The stack System Testing starts: Start System runs make system-launch, which runs $(SYSTEM_COMPOSE) up.
SYSTEM_STACK_VARIABLE = 'SYSTEM_COMPOSE'
GUARD_S = 120.0
DOCKER_SHIM = '#!/bin/sh\nexec "$STUB_PYTHON" -P "$STUB_IMPL" "$@"\n'


# --- The model ---------------------------------------------------------------------------------------


def _resolved(files: list[str]) -> tuple[Path, ...]:
    """The checkout's compose files an invocation names, compose's default pair when it names none."""
    names = files or list(DEFAULT_COMPOSE_FILES)
    return tuple(REPO_ROOT / (PurePosixPath(name).name if name.startswith('/') else name) for name in names)


@functools.cache
def _launch_sets() -> dict[str, tuple[Path, ...]]:
    """Every compose file set a launch loads, keyed by where it is spelled, read from the four places."""
    sets: dict[str, tuple[Path, ...]] = {}
    for variable in sorted(set(_COMPOSE_SET.findall(MAKEFILE.read_text(encoding='utf-8')))):
        sets[f'{MAKEFILE_SOURCE} {variable}'] = _set_files(variable)
    sets[STACK_SOURCE] = tuple(REPO_ROOT / name for name in stack.COMPOSE_FILES)
    for line in _run_lines(MIGRATIONS_SCRIPT.read_text(encoding='utf-8')):
        if line.startswith('COMPOSE=(') and line.endswith(')'):
            for files, _ in _compose_calls(line[len('COMPOSE=(') : -1]):
                sets[f'{MIGRATIONS_SOURCE} -f {" -f ".join(files)}'] = _resolved(files)
    for _, step in _every_workflow_step():
        for line in _run_lines(step.get('run') or ''):
            for files, _ in _compose_calls(line):
                sets.setdefault(f'{WORKFLOW_SOURCE} -f {" -f ".join(files)}', _resolved(files))
    return sets


def _committed_root_env() -> dict[str, str]:
    """What compose interpolates from: the project directory's root env file, as committed."""
    return _env_file_values(ENV_DEFAULT_FILE)


def _image_target(service: Mapping, project_dir: Path) -> str | None:
    """The Dockerfile stage a service's image is built from; None when it is not built from the repository's Dockerfile.

    The agent stack builds the same file through the MCP image's own copy of it (stack.TRUSTED_DOCKERFILE).
    With no target, compose builds the Dockerfile's last stage.
    """
    build = service.get('build')
    if build is None:
        return None
    build = {'context': build} if isinstance(build, str) else dict(build)
    context = (project_dir / interpolate(str(build.get('context', '.')), {})).resolve()
    dockerfile = str(build.get('dockerfile', 'Dockerfile'))
    if dockerfile != str(stack.TRUSTED_DOCKERFILE) and (context / dockerfile).resolve() != PROJECT_DOCKERFILE.resolve():
        return None
    stages = list(_dockerfile_stages())
    target = str(build.get('target') or stages[-1])
    assert target in stages, (
        f'a service builds the Dockerfile target {target!r}, which is not one of its stages {stages}'
    )
    return target


def _built_services(model: dict, project_dir: Path) -> dict[str, str]:
    """{service: the stage it builds} for every service of a merged model built from the repository's Dockerfile."""
    built = {}
    for name, service in (model.get('services') or {}).items():
        target = _image_target(service or {}, project_dir)
        if target is not None:
            built[name] = target
    return built


def _effective_env(
    service: Mapping,
    target: str,
    project_dir: Path,
    *,
    image_of: Callable[[str], str | None] | None = None,
    interpolation: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """A container's environment as compose builds it, under the committed root env file.

    test_ci_invariants.py's _container_env ranks the three sources (image ENV < each env file's committed
    template, in order < environment:); this hands it environment: interpolated as compose interpolates
    it, from the project env file, so an interpolated value is read as the container would get it. A
    pass-through entry (a bare name) is left to the env files, as compose leaves it when the shell does
    not set it.
    """
    interpolation = _committed_root_env() if interpolation is None else interpolation
    image = (image_of or _dockerfile_pythonpath)(target)
    environment = {
        name: _rendered(value, interpolation)
        for name, value in _environment(service.get('environment')).items()
        if value is not None
    }
    return _container_env({**service, 'environment': environment}, {VARIABLE: image or ''}, project_dir)


def _dockerfile_pythonpath(target: str) -> str | None:
    return _image_env(target, VARIABLE)


def _offenders(
    label: str,
    documents: list[dict],
    project_dir: Path,
    *,
    image_of: Callable[[str], str | None] | None = None,
    interpolation: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """(offenders, {service: effective PYTHONPATH}) for one launch set's documents."""
    image_of = image_of or _dockerfile_pythonpath
    model = merge(documents)
    offenders, effective = [], {}
    for name, target in _built_services(model, project_dir).items():
        image = image_of(target)
        value = _effective_env(
            model['services'][name], target, project_dir, image_of=image_of, interpolation=interpolation
        ).get(VARIABLE, '')
        effective[name] = value
        if value != image or GENERATED_ROOT not in _runtime_path_entries(value):
            offenders.append(
                f'{label}: {name} (built from the Dockerfile stage {target}) runs with PYTHONPATH={value!r}, '
                f'but the stage sets {image!r}'
            )
    return offenders, effective


def _documents(files: tuple[Path, ...], replaced: Mapping[Path, dict] | None = None) -> list[dict]:
    replaced = replaced or {}
    return [replaced[path] if path in replaced else load(path) for path in files]


def _set_entry(service: dict, value: str | None) -> None:
    """Set PYTHONPATH in a service's environment:, keeping the block's form; None drops the entry."""
    environment = service.get('environment')
    if isinstance(environment, dict):
        environment.pop(VARIABLE, None)
        if value is not None:
            environment[VARIABLE] = value
        return
    kept = [entry for entry in environment or [] if str(entry).partition('=')[0].strip() != VARIABLE]
    service['environment'] = kept if value is None else [*kept, f'{VARIABLE}={value}']


def _base_built_services() -> dict[str, str]:
    return _built_services(merge([load(BASE_FILE)]), REPO_ROOT)


# --- V1: the effective PYTHONPATH, every launch set, every service built from the Dockerfile ---------


def test_the_launch_sets_are_read_from_every_place_one_is_spelled():
    """Non-vacuity for V1's sets: each of the four sources yields a set, and today's Makefile sets are among them."""
    sets = _launch_sets()
    sources = {MAKEFILE_SOURCE, STACK_SOURCE, MIGRATIONS_SOURCE, WORKFLOW_SOURCE}
    found = {source for source in sources if any(label.startswith(source) for label in sets)}
    assert found == sources, f'no launch set read from {sorted(sources - found)}: {sorted(sets)}'
    makefile = {label.split(' ', 1)[1] for label in sets if label.startswith(f'{MAKEFILE_SOURCE} ')}
    assert makefile >= KNOWN_MAKEFILE_SETS, f'the Makefile sets read are {sorted(makefile)}'
    missing = sorted(f'{label}: {path.name}' for label, files in sets.items() for path in files if not path.is_file())
    assert not missing, f'launch sets name compose files the checkout does not hold: {missing}'


def test_the_services_are_derived_from_the_dockerfile_builds():
    """Non-vacuity for V1's services: the derivation finds today's three, each at the stage its files name."""
    targets: dict[str, set[str]] = {}
    for files in _launch_sets().values():
        for name, target in _built_services(merge(_documents(files)), files[0].parent).items():
            targets.setdefault(name, set()).add(target)
    assert set(targets) >= KNOWN_SERVICES, f'services derived as built from the Dockerfile: {sorted(targets)}'
    assert targets['data_store'] >= {'prod_image', 'dev_image'}, f'data_store builds {sorted(targets["data_store"])}'
    assert targets['test_client'] == {'system_test_image'}, f'test_client builds {sorted(targets["test_client"])}'


def test_every_container_built_from_the_dockerfile_runs_with_the_images_pythonpath():
    """V1: under compose's precedence, with the committed root env file, every such container gets the stage's ENV.

    Equality, not 'contains': a value that names the generated root but differs from the image -- an extra
    entry, another order -- is a second definition that drifts, and the image is the one definition.
    """
    offenders, checked = [], 0
    for label, files in _launch_sets().items():
        found, effective = _offenders(label, _documents(files), files[0].parent)
        offenders += found
        checked += len(effective)
    assert checked, 'no launch set runs a service built from the Dockerfile, so nothing was checked'
    assert not offenders, '\n'.join(offenders)


# --- V3: literal, and no interpolation environment moves it -------------------------------------------


def test_the_path_is_written_literally_so_no_env_file_can_move_it():
    """V3: wherever a compose file sets PYTHONPATH for a service built from the Dockerfile, it is a literal.

    No '$' (compose interpolates from the project env file, which carries PYTHONPATH=./), and no bare name
    (a pass-through from the shell). Then the effective value is the same under an empty interpolation
    environment, the committed root env file, and one that sets PYTHONPATH to something else.
    """
    written, offenders = {}, []
    for label, files in _launch_sets().items():
        built = _built_services(merge(_documents(files)), files[0].parent)
        for path in files:
            for name, service in (load(path).get('services') or {}).items():
                environment = _environment((service or {}).get('environment'))
                if name not in built or VARIABLE not in environment:
                    continue
                value = environment[VARIABLE]
                written[f'{path.name}:{name}'] = value
                if value is None or '$' in value:
                    offenders.append(f'{label}: {path.name} sets {name} {VARIABLE} to {value!r}, not a literal')
        envs = ({}, _committed_root_env(), {**_committed_root_env(), VARIABLE: '/somewhere/else'})
        rendered = [_offenders(label, _documents(files), files[0].parent, interpolation=env)[1] for env in envs]
        if any(values != rendered[0] for values in rendered):
            offenders.append(f'{label}: the effective PYTHONPATH moves with the interpolation environment: {rendered}')
    assert {key.split(':', 1)[1] for key in written} >= KNOWN_SERVICES, f'PYTHONPATH assignments found: {written}'
    assert not offenders, '\n'.join(offenders)


# --- V4: the checks see each way the path is lost ------------------------------------------------------


# The value each mutation writes in place of the literal; None drops the entry.
_MUTATIONS = {
    'entry dropped': None,
    'generated root dropped': str(IMAGE_CODE_ROOT),
    'interpolated': f'${{{VARIABLE}:-{_image_pythonpath_model()}}}',
}


def test_the_committed_root_env_file_still_carries_the_legacy_value():
    """The finding's input, read from the file: without the compose entry, this is what a container gets."""
    assert _committed_root_env().get(VARIABLE) == LEGACY_VALUE, (
        f'{ENV_DEFAULT_FILE.name} sets {VARIABLE}={_committed_root_env().get(VARIABLE)!r}. If the legacy line changed '
        f'or went, re-read decision tj-3mk3u5.42 addendum F1-A: it keeps the line, host-side only.'
    )


@pytest.mark.parametrize('mutation', list(_MUTATIONS))
def test_the_model_sees_the_compose_entry_lost(mutation: str):
    """V4: one base-file service's entry changed in memory reds that service in every launch set that runs it.

    Each service the base file builds from the Dockerfile in turn, so both forms of environment: are
    exercised (data_store's list, data_ingest's mapping). With the entry dropped or interpolated the
    computed value is exactly the env file's './': the env-file branch is live, not vacuous.
    """
    base = load(BASE_FILE)
    services = _base_built_services()
    forms = {type(base['services'][name].get('environment')).__name__ for name in services}
    assert forms == {'list', 'dict'}, f'the base file environment: forms exercised: {forms}'
    for name in services:
        mutated = copy.deepcopy(base)
        _set_entry(mutated['services'][name], _MUTATIONS[mutation])
        reddened = []
        for label, files in _launch_sets().items():
            if BASE_FILE not in files:
                continue
            offenders, effective = _offenders(label, _documents(files, {BASE_FILE: mutated}), files[0].parent)
            if name not in effective:
                continue
            assert any(f': {name} (' in offender for offender in offenders), (
                f'{mutation} on {name}: {label} still reads as correct, with {effective[name]!r}'
            )
            if _MUTATIONS[mutation] is None or '$' in str(_MUTATIONS[mutation]):
                assert effective[name] == LEGACY_VALUE, (
                    f'{mutation} on {name}: {label} computes {effective[name]!r}, not the env file value'
                )
            reddened.append(label)
        assert reddened, f'{mutation} on {name}: no launch set that loads {BASE_FILE.name} runs it'


def test_the_model_sees_the_dockerfile_changed_alone():
    """V4: the Dockerfile's ENV changed without compose (in memory) reds every service built from it, in every set."""

    def changed(target: str) -> str:
        return f'{_dockerfile_pythonpath(target)}:/code/elsewhere'

    seen = set()
    for label, files in _launch_sets().items():
        offenders, effective = _offenders(label, _documents(files), files[0].parent, image_of=changed)
        assert len(offenders) == len(effective), f'{label}: only {offenders} of {sorted(effective)} read as wrong'
        seen |= set(effective)
    assert seen >= KNOWN_SERVICES, f'services checked against the changed Dockerfile: {sorted(seen)}'


# --- V5: Check Generated Code Import, its own script under bash ---------------------------------------


def _stack_containers(replaced: Mapping[Path, dict] | None = None) -> dict[str, dict[str, str]]:
    """Each running service of the stack the job starts that is built from the Dockerfile, with its container env."""
    files = _set_files(SYSTEM_STACK_VARIABLE)
    model = merge(_documents(files, replaced))
    return {
        name: _effective_env(model['services'][name], target, files[0].parent)
        for name, target in _built_services(model, files[0].parent).items()
    }


def _run_step(tmp_path: Path, containers: Mapping[str, Mapping[str, str]]) -> tuple[subprocess.CompletedProcess, list]:
    """The step's script as GitHub runs a `run:` with no shell (bash -e), docker stubbed; (result, docker calls)."""
    bash = shutil.which('bash')
    assert bash, 'bash is not on PATH, so the step cannot be exercised'
    script = _system_step(IMPORT_CHECK_STEP).get('run') or ''
    assert '${{' not in script, f'{IMPORT_CHECK_STEP} now uses a workflow expression, which this test cannot evaluate'
    shim = tmp_path / 'bin' / 'docker'
    shim.parent.mkdir(parents=True)
    shim.write_text(DOCKER_SHIM, encoding='utf-8')
    shim.chmod(0o755)
    (tmp_path / 'scenario.json').write_text(json.dumps({'containers': containers}), encoding='utf-8')
    (tmp_path / 'step.sh').write_text(script, encoding='utf-8')
    environment = {
        'PATH': f'{shim.parent}{os.pathsep}{os.environ.get("PATH", "")}',
        # THE STUB PROCESS's own path, not the modelled container's. It imports common.tests.*, so
        # it needs the host mirror of the first-party import path; the bare repository root stopped
        # being enough when the service trees moved under server/ (tj-iontkq.4). It cannot reach the
        # child the stub starts, which runs -E -P with the container environment and nothing else.
        'PYTHONPATH': image_pythonpath(),
        'STUB_PYTHON': sys.executable,
        'STUB_IMPL': stub.__file__,
        'STUB_SCENARIO': str(tmp_path / 'scenario.json'),
        # The two host directories the one image /code stands in for (tj-iontkq.4): the service
        # trees under the server root, gen/proto/python under the repository root.
        'STUB_CODE_ROOT': str(SERVER_ROOT),
        'STUB_CONTEXT_ROOT': str(REPO_ROOT),
        'STUB_LOG': str(tmp_path / 'docker.log'),
        'STUB_UNMODELLED_LOG': str(tmp_path / 'unmodelled.log'),
    }
    result = subprocess.run(
        [bash, '--noprofile', '--norc', '-e', str(tmp_path / 'step.sh')],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=GUARD_S,
        check=False,
    )
    unmodelled = tmp_path / 'unmodelled.log'
    assert not unmodelled.exists(), f'the step made calls the stand-in does not model: {unmodelled.read_text()}'
    log = tmp_path / 'docker.log'
    calls = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()] if log.exists() else []
    return result, calls


def _report(result: subprocess.CompletedProcess) -> str:
    return f'exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'


def _imports(calls: list, service: str) -> list:
    """The step's import calls into SERVICE: an exec of the image interpreter whose code imports common.rpc.ping."""
    prefix = ['compose', '-f', 'docker-compose.yaml', 'exec', '-T', service, stub.VENV_PYTHON, '-c']
    return [call for call in calls if call[: len(prefix)] == prefix and 'common.rpc.ping' in call[len(prefix)]]


def _line(result: subprocess.CompletedProcess, start: str) -> str:
    lines = [line for line in result.stdout.splitlines() if line.startswith(start)]
    assert len(lines) == 1, f'expected one line starting {start!r}:\n{_report(result)}'
    return lines[0]


def test_the_import_check_passes_on_the_stack_the_job_starts(tmp_path: Path):
    """Every running service built from the Dockerfile imports the generated code, from the checkout's gen tree."""
    containers = _stack_containers()
    assert set(containers) >= {'data_store', 'data_ingest'}, f'the system stack builds {sorted(containers)}'
    result, calls = _run_step(tmp_path, containers)
    assert result.returncode == 0, _report(result)
    services = {call[5] for call in calls if call[:5] == ['compose', '-f', 'docker-compose.yaml', 'exec', '-T']}
    assert services == set(containers), f'the step execs into {sorted(services)}, the stack runs {sorted(containers)}'
    generated_tree = f'{REPO_ROOT / IMAGE_PYTHONPATH_ENTRIES[-1]}/'
    for service, environment in containers.items():
        assert len(_imports(calls, service)) == 1, f'expected one import into {service}: {calls}'
        line = _line(result, f'{service}:')
        assert generated_tree in line, f'{service} did not load the generated code from {generated_tree}: {line}'
        assert f'{VARIABLE}={environment[VARIABLE]}' in line, f'{service} does not print the path it saw: {line}'


def test_the_import_check_fails_on_the_stack_without_the_compose_entry(tmp_path: Path):
    """V4 at runtime: the entry dropped from every base-file service, the env file's './' wins, and the step is red."""
    base = load(BASE_FILE)
    for name in _base_built_services():
        _set_entry(base['services'][name], None)
    containers = _stack_containers({BASE_FILE: base})
    assert {name: env.get(VARIABLE) for name, env in containers.items()} == dict.fromkeys(containers, LEGACY_VALUE)
    result, _ = _run_step(tmp_path, containers)
    assert result.returncode == 1, _report(result)
    for service in containers:
        error = _line(result, f'::error::{service} ')
        assert f'{VARIABLE}={LEGACY_VALUE}' in error, error
    assert result.stderr.count("No module named 'trader_joe'") == len(containers), _report(result)


@pytest.mark.parametrize(
    'pythonpath', [LEGACY_VALUE, str(IMAGE_CODE_ROOT), None], ids=['the env file value', 'no generated root', 'unset']
)
def test_the_import_check_fails_naming_the_service_and_the_path_it_saw(tmp_path: Path, pythonpath: str | None):
    """One service's path lost, each service in turn: exit 1, the service and its path named; the rest still checked."""
    healthy = _stack_containers()
    for index, service in enumerate(healthy):
        containers = copy.deepcopy(healthy)
        containers[service].pop(VARIABLE)
        if pythonpath is not None:
            containers[service][VARIABLE] = pythonpath
        result, calls = _run_step(tmp_path / str(index), containers)
        assert result.returncode == 1, _report(result)
        error = _line(result, f'::error::{service} ')
        assert f'{VARIABLE}={pythonpath or "<unset or empty>"}' in error, error
        assert "No module named 'trader_joe'" in result.stderr, _report(result)
        for other in set(healthy) - {service}:
            assert len(_imports(calls, other)) == 1, f'{other} was not checked after {service} failed: {calls}'
            _line(result, f'{other}: common.rpc.ping imports')


def test_the_import_check_fails_when_a_service_is_not_running(tmp_path: Path):
    """An exec that fails before the import (the container is gone) is red too, never an unproven pass."""
    healthy = _stack_containers()
    for index, service in enumerate(healthy):
        containers = {name: env for name, env in healthy.items() if name != service}
        result, calls = _run_step(tmp_path / str(index), containers)
        assert result.returncode == 1, _report(result)
        _line(result, f'::error::could not read {VARIABLE} inside {service}')
        for other in containers:
            assert len(_imports(calls, other)) == 1, f'{other} was not checked after {service} failed: {calls}'
