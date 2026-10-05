"""build_infra pins for System Testing in fake mode and the head-seed artifact (tj-irhy0a.2).

The steps tj-irhy0a.1 added to the System Testing job of .github/workflows/trader_joe_testing.yml.
Design: decision tj-j4wknb R4 (the fake-mode overlay swaps data_ingest's command; no mode variable;
the prod image holds no fakes), decision tj-0rpt9t (CI runs the prod images and the prod compose file,
with no live credential), decision tj-vhboky.55 (SEED PRODUCTION: make seed-dump after the suite,
uploaded as a workflow artifact) and ADR tj-4rr0la addendum 10 (the producer runs in test_client).

  * Start System starts the stack through `make system-launch`, the developer's own target.
  * Check Fake Broker fails the job unless data_ingest's logs carry the launcher's WARNING banner.
    Exercised for real: the step's own script runs under bash with `docker` stubbed, against the
    banner line as the launcher logs it (its BANNER constant through common/logging.py's format).
  * Seed Dump runs `make seed-dump` into a job-local directory under the git-ignored output/.
  * Upload Head Seed uploads exactly that directory with the upload-artifact SHA the file already
    pins, failing when there is nothing to upload.
  * Every upload-artifact step sets overwrite: true under a name with no run_attempt, so a re-run
    re-uploads instead of failing on the fixed name (tj-irhy0a.27, pinned by tj-irhy0a.28).
  * Only the fake-mode overlay names the launcher, and no variable selects fake versus real.

Placement and attestation -- Check Fake Broker directly after Start System, Seed Dump and Upload Head
Seed after System Tests and before Instance Secret Lifecycle, SYSTEM_TEST_DISPOSABLE_DB on exactly
the three guarded-target steps, every `up` carrying the overlay -- are pinned with the rest of the
job in test_ci_invariants.py. The overlay, SYSTEM_COMPOSE, make system-launch and make seed-dump
themselves are pinned in test_fake_overlay.py and test_seed_dump_make.py.

What only a runner can show -- the job going green in fake mode, the artifact appearing -- is the
PR's CI run (tj-0pobey.3); nothing here needs Docker.
"""

import ast
import logging
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from common.tests.test_ci_invariants import (
    COMPOSE_FILE,
    FAKE_CHECK_STEP,
    MAKEFILE,
    REPO_ROOT,
    SEED_DUMP_STEP,
    SERVER_ROOT,
    START_STEP,
    UPLOAD_SEED_STEP,
    _compose_calls,
    _compose_service,
    _image_build_job,
    _load_yaml,
    _step_lines,
    _system_step,
    _walk_scalars,
    _workflow_files,
)


pytestmark = pytest.mark.build_infra

LAUNCHER = REPO_ROOT / 'tests' / 'fakes' / 'ingest_launcher.py'
# SERVER_ROOT: common/ travels with the service trees (tj-iontkq.4), unlike tests/fakes above.
LOGGING_MODULE = SERVER_ROOT / 'common' / 'logging.py'
FAKE_OVERLAY = REPO_ROOT / 'docker-compose.fake.yaml'
UPLOAD_ACTION = 'actions/upload-artifact'
_PINNED_ACTION = re.compile(r'^actions/upload-artifact@[0-9a-f]{40}$')
SEED_OUT_ROOT = 'output'
SEED_ARTIFACT_PREFIX = 'head-seed-'
# "Short retention" (tj-irhy0a.1 item 3): the artifact only moves the seed off the runner; the kept
# copy is the committed file (ruling on tj-vhboky.56). Two weeks is the ceiling this pin allows.
MAX_SEED_RETENTION_DAYS = 14
# What names the launcher, as a module path: the overlay's uvicorn target and nothing else.
LAUNCHER_NAMES = ('ingest_launcher', 'tests.fakes')
# A variable that would select fake versus real (tj-j4wknb R4 dropped INGEST_BROKER_MODE).
_MODE_VARIABLE = re.compile(r'BROKER_MODE|FAKE', re.IGNORECASE)
_ASSIGNED_NAME = re.compile(r'^\s*(?:export\s+)?([A-Za-z_]\w*)\s*[:?+]?=')

DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_LOG"
case " $* " in
  *" logs "*) cat "$STUB_LOGS"; exit "${STUB_STATUS:-0}" ;;
esac
exit 0
"""


def _commands(step_name: str, program: str) -> list[list[str]]:
    """The simple commands of a step's run script that start with `program`."""
    return [words for line in _step_lines(_system_step(step_name)) if (words := shlex.split(line))[:1] == [program]]


def _errexit_before(step_name: str, program: str) -> bool:
    lines = _step_lines(_system_step(step_name))
    first = next(index for index, line in enumerate(lines) if shlex.split(line)[:1] == [program])
    return any(re.match(r'^set\s+-\w*e', line) for line in lines[:first])


# --- Start System ----------------------------------------------------------------------------------


def test_start_system_starts_the_stack_through_make_system_launch():
    """tj-irhy0a.1 item 1: `make system-launch` (base file + fake overlay), the developer's own target.

    Exactly one make command, the bare target -- the attestation comes from the step's env, never
    the command line -- under errexit, and no compose `up` of the step's own beside it.
    """
    assert _commands(START_STEP, 'make') == [['make', 'system-launch']], _step_lines(_system_step(START_STEP))
    assert _errexit_before(START_STEP, 'make'), f'{START_STEP} does not set errexit before make system-launch'
    ups = [
        line
        for line in _step_lines(_system_step(START_STEP))
        for _, rest in _compose_calls(line)
        if rest[:1] in (['up'], ['create'])
    ]
    assert ups == [], f'{START_STEP} starts containers outside make system-launch: {ups}'


# --- Check Fake Broker -----------------------------------------------------------------------------


def _module_constant(path: Path, name: str) -> str:
    """A module-level string constant, read with ast so the module is never imported."""
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f'{path.name} assigns no module-level {name}')


def _log_format() -> str:
    """The format common/logging.py hands logging.basicConfig -- the format every service logs in."""
    for node in ast.walk(ast.parse(LOGGING_MODULE.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Call) and getattr(node.func, 'attr', None) == 'basicConfig':
            for keyword in node.keywords:
                if keyword.arg == 'format':
                    return ast.literal_eval(keyword.value)
    raise AssertionError(f'{LOGGING_MODULE.name} passes no format to logging.basicConfig')


def _logged(level: int, filename: str, message: str) -> str:
    """One line as data_ingest's logging would print it."""
    record = logging.LogRecord('test', level, f'/code/{filename}', 1, message, None, None)
    return logging.Formatter(_log_format()).format(record)


def _banner_message() -> str:
    """The launcher's banner as it logs it: BANNER, then its delay (tests/fakes/ingest_launcher.py)."""
    return f'{_module_constant(LAUNCHER, "BANNER")} SLOW_ delay: 2.0 s.'


PRODUCTION_LOGS = [
    (logging.INFO, 'data/ingest/app/main.py', 'Starting Kafka RPC servers'),
    (logging.WARNING, 'common/kafka/kafka_tools.py', 'broker not yet available, retrying'),
]


def _run_fake_check(tmp_path: Path, logs: str, status: int = 0) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run Check Fake Broker's own script as GitHub runs a `run:` with no shell (bash -e), docker stubbed."""
    bash = shutil.which('bash')
    assert bash, 'bash is not on PATH, so the step cannot be exercised'
    stub_dir = tmp_path / 'bin'
    stub_dir.mkdir()
    stub = stub_dir / 'docker'
    stub.write_text(DOCKER_STUB, encoding='utf-8')
    stub.chmod(0o755)
    (tmp_path / 'logs.txt').write_text(logs, encoding='utf-8')
    script = tmp_path / 'step.sh'
    script.write_text(_system_step(FAKE_CHECK_STEP)['run'], encoding='utf-8')
    env = {
        'PATH': f'{stub_dir}{os.pathsep}{os.environ.get("PATH", "")}',
        'STUB_LOG': str(tmp_path / 'docker.log'),
        'STUB_LOGS': str(tmp_path / 'logs.txt'),
        'STUB_STATUS': str(status),
    }
    result = subprocess.run(
        [bash, '--noprofile', '--norc', '-e', str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    log = tmp_path / 'docker.log'
    return result, log.read_text(encoding='utf-8').splitlines() if log.exists() else []


def test_check_fake_broker_reads_data_ingest_logs_from_the_stack():
    """FAKES-1 (tj-irhy0a.1 item 2): the logs of data_ingest, on the stack spelling, nothing else."""
    calls = [call for line in _step_lines(_system_step(FAKE_CHECK_STEP)) for call in _compose_calls(line)]
    assert len(calls) == 1, f'{FAKE_CHECK_STEP} must run one compose invocation, found {calls}'
    files, rest = calls[0]
    service, _, _ = _compose_service([word.rstrip(')"') for word in rest])
    assert files == [COMPOSE_FILE.name] and rest[:1] == ['logs'] and service == 'data_ingest', calls


def test_check_fake_broker_passes_on_the_launchers_banner(tmp_path: Path):
    """The banner, among production-shaped lines, as the launcher logs it at import: the step passes."""
    lines = [_logged(*PRODUCTION_LOGS[0]), _logged(logging.WARNING, 'ingest_launcher.py', _banner_message())]
    result, docker = _run_fake_check(tmp_path, '\n'.join([*lines, _logged(*PRODUCTION_LOGS[1])]) + '\n')
    assert result.returncode == 0, f'stdout={result.stdout!r} stderr={result.stderr!r}'
    assert 'FAKE BROKER:' in result.stdout, result.stdout
    assert any(' logs ' in f' {call} ' and call.endswith('data_ingest') for call in docker), docker


_REFUSED_LOGS = {
    # The production entrypoint: no banner at all -- an overlay dropped from Start System.
    'production-entrypoint': ('\n'.join(_logged(*entry) for entry in PRODUCTION_LOGS) + '\n', 0),
    'no-logs': ('', 0),
    # The banner text, but not the launcher's WARNING: something else printing the words.
    'banner-below-warning': (_logged(logging.INFO, 'ingest_launcher.py', _banner_message()) + '\n', 0),
    # `docker compose logs` itself failing must fail the step, even if it printed the banner first.
    'logs-command-fails': (_logged(logging.WARNING, 'ingest_launcher.py', _banner_message()) + '\n', 1),
}


@pytest.mark.parametrize(('logs', 'status'), list(_REFUSED_LOGS.values()), ids=list(_REFUSED_LOGS))
def test_check_fake_broker_fails_without_the_launchers_warning(tmp_path: Path, logs: str, status: int):
    """No launcher WARNING, or no logs to read: the step exits non-zero, before anything talks to the stack."""
    result, _ = _run_fake_check(tmp_path, logs, status)
    assert result.returncode != 0, f'{FAKE_CHECK_STEP} passed on {logs!r}: stdout={result.stdout!r}'


# --- Seed Dump and Upload Head Seed ----------------------------------------------------------------


def _seed_out() -> str:
    """The SEED_OUT Seed Dump hands make seed-dump."""
    commands = _commands(SEED_DUMP_STEP, 'make')
    assert len(commands) == 1 and commands[0][:2] == ['make', 'seed-dump'], (
        f'{SEED_DUMP_STEP} must run make seed-dump once: {commands}'
    )
    arguments = commands[0][2:]
    assert len(arguments) == 1 and arguments[0].startswith('SEED_OUT='), (
        f'{SEED_DUMP_STEP} must pass SEED_OUT and nothing else (the attestation is the step env): {arguments}'
    )
    return arguments[0].removeprefix('SEED_OUT=')


def test_seed_dump_runs_make_seed_dump_into_a_job_local_dir_under_output():
    """tj-irhy0a.1 item 3: the developer's target, into the checkout's git-ignored output/, under errexit.

    Job-local: relative, inside the checkout, under output/ (git-ignored, so a dump can never be
    committed by accident) and so never under tests/ -- where a reviewed seed is COPIED by a person
    (tj-vhboky.56). The step never runs the producer or the writer itself: make seed-dump is the one
    invocation (ADR tj-4rr0la addendum 10 (4)), and its status rule is the make target's.
    """
    seed_out = PurePosixPath(_seed_out())
    assert not seed_out.is_absolute() and '..' not in seed_out.parts, seed_out
    assert seed_out.parts[:1] == (SEED_OUT_ROOT,) and len(seed_out.parts) > 1, (
        f'SEED_OUT must be a directory under {SEED_OUT_ROOT}/: {seed_out}'
    )
    ignored = subprocess.run(
        ['git', 'check-ignore', '-q', '--no-index', f'{seed_out}/head.sql'], cwd=REPO_ROOT, check=False
    )
    assert ignored.returncode == 0, f'{seed_out} is not git-ignored, so a dumped seed could be committed'
    assert _errexit_before(SEED_DUMP_STEP, 'make'), f'{SEED_DUMP_STEP} does not set errexit before make seed-dump'
    direct = [line for line in _step_lines(_system_step(SEED_DUMP_STEP)) if 'data.store.seeds' in line]
    assert direct == [], f'{SEED_DUMP_STEP} runs the producer or writer outside make seed-dump: {direct}'


def _pinned_upload_action() -> str:
    """The upload-artifact reference Image Build's Push Pipeline Configs already uses."""
    uses = {
        step.get('uses')
        for step in _image_build_job().get('steps') or []
        if str(step.get('uses') or '').startswith(f'{UPLOAD_ACTION}@')
    }
    assert len(uses) == 1, f'Image Build must pin one {UPLOAD_ACTION} reference, found {uses}'
    (action,) = uses
    assert _PINNED_ACTION.match(action), f'{action} is not pinned to a full commit SHA'
    return action


def test_upload_head_seed_uploads_the_seed_dir_with_the_pinned_action():
    """tj-irhy0a.1 item 3 / tj-irhy0a.2 item 6: the SHA already pinned, the dump's directory, never empty.

    `if-no-files-found: error`, so a dump that wrote nothing turns the job red rather than uploading
    an empty artifact. Short retention: the artifact only moves the seed off the runner.
    """
    step = _system_step(UPLOAD_SEED_STEP)
    assert step.get('uses') == _pinned_upload_action(), step.get('uses')
    assert 'run' not in step, f'{UPLOAD_SEED_STEP} is an action step, not a script'
    options = step.get('with') or {}
    assert options.get('if-no-files-found') == 'error', options
    assert os.path.normpath(str(options.get('path') or '')) == os.path.normpath(_seed_out()), (
        f'{UPLOAD_SEED_STEP} uploads {options.get("path")!r}, not the directory {SEED_DUMP_STEP} wrote'
    )
    assert str(options.get('name') or '').startswith(SEED_ARTIFACT_PREFIX), options.get('name')
    retention = options.get('retention-days')
    assert isinstance(retention, int) and 1 <= retention <= MAX_SEED_RETENTION_DAYS, (
        f'retention-days must be set and short (1..{MAX_SEED_RETENTION_DAYS}): {retention!r}'
    )
    # tj-irhy0a.27 item 1: a re-run attempt replaces the earlier attempt's seed, the newer head.
    assert options.get('overwrite') is True, (
        f'{UPLOAD_SEED_STEP} must set overwrite: true (boolean), or a re-run fails on the fixed name: '
        f'{options.get("overwrite")!r}'
    )


def _upload_steps() -> list[tuple[str, str, dict]]:
    """Every upload-artifact step in .github/workflows/, as (file, step name, step)."""
    found = []
    for path in _workflow_files():
        jobs = (_load_yaml(path) or {}).get('jobs') or {}
        for job in jobs.values():
            for step in (job or {}).get('steps') or []:
                if str(step.get('uses') or '').startswith(f'{UPLOAD_ACTION}@'):
                    found.append((path.name, str(step.get('name') or step.get('uses')), step))
    return found


def test_every_upload_artifact_step_overwrites_under_a_name_without_the_run_attempt():
    """tj-irhy0a.27 item 1: re-run safety for every upload, not only the two there today.

    upload-artifact v4 refuses a second artifact of one name in one run, so each upload sets
    `overwrite: true` -- the boolean, not the string -- and a re-run attempt replaces the earlier
    attempt's artifact. The name carries no github.run_attempt: System Testing's Pull Pipeline Configs
    downloads pipeline-configs by its exact name, which a suffix would break on a re-run of that job
    alone (the alternative the architect rejected on tj-irhy0a.1, N2).
    """
    steps = _upload_steps()
    names = {name for _, name, _ in steps}
    assert {'Push Pipeline Configs', UPLOAD_SEED_STEP} <= names, f'upload-artifact steps found: {sorted(names)}'
    for workflow, name, step in steps:
        options = step.get('with') or {}
        assert options.get('overwrite') is True, (
            f'{workflow}: {name} must set overwrite: true (boolean): {options.get("overwrite")!r}'
        )
        assert 'run_attempt' not in str(options.get('name') or ''), (
            f'{workflow}: {name} names its artifact per attempt, which the exact-name download misses: '
            f'{options.get("name")!r}'
        )


# --- R4: only the overlay names the launcher; no mode variable --------------------------------------


def _uncommented(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip() and not line.strip().startswith('#')]


def _build_surfaces() -> dict[str, list[str]]:
    """Every compose file, env default, workflow and the Makefile, as (comment-free) text lines."""
    surfaces: dict[str, list[str]] = {}
    for path in sorted(REPO_ROOT.glob('docker-compose*.yaml')):
        surfaces[path.name] = [line for scalar in _walk_scalars(_load_yaml(path)) for line in _uncommented(scalar)]
    env_defaults = [REPO_ROOT / '.env.default', *sorted(REPO_ROOT.glob('data/*/.env.default'))]
    for path in env_defaults:
        surfaces[str(path.relative_to(REPO_ROOT))] = _uncommented(path.read_text(encoding='utf-8'))
    for path in _workflow_files():
        surfaces[path.name] = [line for scalar in _walk_scalars(_load_yaml(path)) for line in _uncommented(scalar)]
    surfaces[MAKEFILE.name] = _uncommented(MAKEFILE.read_text(encoding='utf-8'))
    return surfaces


def test_only_the_fake_overlay_names_the_launcher():
    """tj-irhy0a.2 item 5 (tj-j4wknb R4): the launcher is chosen in docker-compose.fake.yaml and nowhere else.

    No other compose file, env default (entrypoint.sh reads APP_MODULE from the environment, so an
    env default could select the launcher too), workflow or Makefile line names it. Mounting the
    tests/fakes DIRECTORY is not naming it: test_client mounts it for the producer (tj-irhy0a.22).
    """
    surfaces = _build_surfaces()
    assert FAKE_OVERLAY.name in surfaces and len(surfaces) > 5, sorted(surfaces)
    assert any('tests.fakes.ingest_launcher' in line for line in surfaces[FAKE_OVERLAY.name]), (
        f'{FAKE_OVERLAY.name} no longer names the launcher, so this pin would pass on nothing'
    )
    naming = [
        f'{where}: {line.strip()}'
        for where, lines in surfaces.items()
        if where != FAKE_OVERLAY.name
        for line in lines
        if any(name in line for name in LAUNCHER_NAMES)
    ]
    assert naming == [], f'the launcher is named outside {FAKE_OVERLAY.name}: {naming}'


def _variable_names(lines: list[str], document: object) -> set[str]:
    names = {match.group(1) for line in lines if (match := _ASSIGNED_NAME.match(line))}
    if isinstance(document, dict):
        for node in [document, *(document.get('jobs') or {}).values(), *(document.get('services') or {}).values()]:
            names |= {str(key) for key in ((node or {}).get('env') or {})}
            environment = (node or {}).get('environment') or {}
            names |= {str(item).split('=', 1)[0] for item in environment} if isinstance(environment, list) else set()
            names |= {str(key) for key in environment} if isinstance(environment, dict) else set()
            for step in (node or {}).get('steps') or []:
                names |= {str(key) for key in (step.get('env') or {})}
    return names


def test_no_variable_selects_fake_versus_real():
    """tj-j4wknb R4: there is no mode variable -- INGEST_BROKER_MODE was dropped with its guard.

    No compose file other than the overlay, no env default, no workflow env and no Makefile
    assignment declares a variable whose name says FAKE or BROKER_MODE. (Production modules are
    HANDLES-2's: data/ingest/tests/test_no_production_test_imports.py.)
    """
    offenders = []
    for path in [*sorted(REPO_ROOT.glob('docker-compose*.yaml')), *_workflow_files()]:
        if path == FAKE_OVERLAY:
            continue
        document = _load_yaml(path)
        names = _variable_names([], document)
        offenders += [f'{path.name}: {name}' for name in sorted(names) if _MODE_VARIABLE.search(name)]
    for path in [REPO_ROOT / '.env.default', *sorted(REPO_ROOT.glob('data/*/.env.default')), MAKEFILE]:
        names = _variable_names(_uncommented(path.read_text(encoding='utf-8')), None)
        offenders += [f'{path.relative_to(REPO_ROOT)}: {name}' for name in sorted(names) if _MODE_VARIABLE.search(name)]
    assert offenders == [], f'a variable names a fake-versus-real mode: {offenders}'
