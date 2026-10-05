"""The uv environment make syncs, the marker that vouches for it, and the devcontainer's own copy.

tj-3t2axg. /workspace is the host checkout bind-mounted into the devcontainer, so host and
container once shared one .venv and one .venv_init marker beside it. Each side's uv pointed
bin/python at an interpreter only it has; the other side's next `uv run` rebuilt the venv with
default-groups only (no sqlalchemy, no asyncpg) while the marker still said "synced", and the
suite died on ModuleNotFoundError. Three things now stop that, and each is pinned here:

* the marker depends on the environment's interpreter, so a missing or dangling bin/python
  forces the full sync before any uv command gets to rebuild the venv on its own terms;
* the marker lives INSIDE the environment, so a venv uv deletes and recreates takes its marker
  with it -- the marker can never vouch for a different venv than the one it sits in;
* the devcontainer sets UV_PROJECT_ENVIRONMENT to its own directory, never .venv, so the two
  sides stop overwriting each other's interpreter in the first place.

These drive the real Makefile through GNU make in dry-run (-n) and question (-q) mode from a
scratch directory, so nothing is synced and no uv runs. They read what make WOULD do, which is
the behaviour under test, rather than matching the Makefile's text.
"""

import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest
import yaml

from common.tests.roots import REPO_ROOT


pytestmark = pytest.mark.build_infra

# THE TRUE REPOSITORY ROOT (tj-iontkq.2): the Makefile, .devcontainer/ and the git checkout this
# module runs `make` and `git check-ignore` in are all at the top of the repository. REPO_ROOT.
MAKEFILE = REPO_ROOT / 'Makefile'
DEVCONTAINER_COMPOSE = REPO_ROOT / '.devcontainer' / 'compose.yml'
DEVCONTAINER_SERVICE = 'agent'

# A non-default environment directory, so every test that sets it proves make follows
# UV_PROJECT_ENVIRONMENT rather than a hard-coded .venv.
CUSTOM_ENV = '.venv-under-test'

# The one command that makes the environment whole. Its presence in a dry run is the signal that
# make would re-sync; its absence, that make trusts the marker.
FULL_SYNC = 'uv sync --all-groups --no-group security'

# Old enough that the marker, written "now", is newer than every declaration and the interpreter.
_LONG_AGO = 1_000_000_000


def _make_env(uv_project_environment: str | None) -> dict[str, str]:
    """The caller's environment with UV_PROJECT_ENVIRONMENT set exactly as asked, or removed."""
    env = dict(os.environ)
    # Run under `make test`, these carry the outer make's flags and depth into the inner one.
    for inherited in ('MAKEFLAGS', 'MFLAGS', 'MAKELEVEL'):
        env.pop(inherited, None)
    if uv_project_environment is None:
        env.pop('UV_PROJECT_ENVIRONMENT', None)
    else:
        env['UV_PROJECT_ENVIRONMENT'] = uv_project_environment
    return env


def _make(cwd: Path, *arguments: str, uv_project_environment: str | None) -> subprocess.CompletedProcess:
    """Run the repository Makefile from `cwd`. Callers pass -n or -q: nothing is ever executed."""
    assert shutil.which('make'), 'make is not on PATH, so the Makefile cannot be exercised'
    command = ['make', '--no-print-directory', '-C', str(cwd), '-f', str(MAKEFILE), *arguments]
    return subprocess.run(command, capture_output=True, text=True, env=_make_env(uv_project_environment), check=False)


def _make_value(name: str, uv_project_environment: str | None, cwd: Path) -> str:
    """The value make gives a variable once the whole Makefile is parsed, under the given env."""
    result = _make(
        cwd,
        '-s',
        '--eval',
        'print-value-%: ; @printf "%s" "$($*)"',
        f'print-value-{name}',
        uv_project_environment=uv_project_environment,
    )
    assert result.returncode == 0, f'could not evaluate {name}: {result.stderr}'
    return result.stdout


def _synced_project(root: Path, env_dir: str, interpreter_target: Path | None) -> Path:
    """A scratch project whose environment make last synced: declarations, env, marker.

    `interpreter_target` is where bin/python points; a path that does not exist makes the link
    dangling, which is the tj-3t2axg shape: an interpreter only the OTHER side has. None leaves
    bin/python out altogether. Returns the marker's path.
    """
    for declaration in ('pyproject.toml', 'uv.lock'):
        path = root / declaration
        path.write_text('', encoding='utf-8')
        os.utime(path, (_LONG_AGO, _LONG_AGO))
    bin_dir = root / env_dir / 'bin'
    bin_dir.mkdir(parents=True)
    if interpreter_target is not None:
        (bin_dir / 'python').symlink_to(interpreter_target)
    marker = root / _make_value('VENV_MARKER', env_dir, root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('', encoding='utf-8')
    return marker


def _real_old_interpreter(root: Path) -> Path:
    """A file that exists and predates the marker, standing in for a working interpreter."""
    interpreter = root / 'interpreter-that-exists'
    interpreter.write_text('', encoding='utf-8')
    os.utime(interpreter, (_LONG_AGO, _LONG_AGO))
    return interpreter


def test_a_synced_environment_with_a_working_interpreter_is_up_to_date(tmp_path: Path) -> None:
    """The control for the test below: a healthy environment triggers no sync.

    Without it, a Makefile that re-synced on every run would pass the dangling-interpreter test
    for the wrong reason.
    """
    marker = _synced_project(tmp_path, CUSTOM_ENV, _real_old_interpreter(tmp_path))
    relative = str(marker.relative_to(tmp_path))

    question = _make(tmp_path, '-q', relative, uv_project_environment=CUSTOM_ENV)
    dry_run = _make(tmp_path, '-n', relative, uv_project_environment=CUSTOM_ENV)

    assert question.returncode == 0, (
        f'make considers a synced environment with a working interpreter out of date '
        f'(make -q exit {question.returncode}); every target would re-sync on every run. '
        f'dry run:\n{dry_run.stdout}{dry_run.stderr}'
    )
    assert FULL_SYNC not in dry_run.stdout, f'a healthy environment would be re-synced:\n{dry_run.stdout}'


@pytest.mark.parametrize('broken', ['dangling', 'missing'])
def test_a_broken_interpreter_forces_the_full_sync_despite_a_fresh_marker(tmp_path: Path, broken: str) -> None:
    """tj-3t2axg: the marker says "synced", but bin/python points at nothing here -- re-sync in full.

    This is the exact state the host left behind: an interpreter path only the other side has,
    and a marker newer than every declaration. Before the fix make trusted the marker, the first
    `uv run` rebuilt the venv with default-groups only, and the suite lost sqlalchemy and asyncpg.
    """
    target = tmp_path / 'opt' / 'uv' / 'python' / 'cpython-only-on-the-other-side' if broken == 'dangling' else None
    marker = _synced_project(tmp_path, CUSTOM_ENV, target)
    relative = str(marker.relative_to(tmp_path))

    dry_run = _make(tmp_path, '-n', relative, uv_project_environment=CUSTOM_ENV)

    assert dry_run.returncode == 0, f'make -n {relative} failed: {dry_run.stderr}'
    assert FULL_SYNC in dry_run.stdout, (
        f'with a {broken} {CUSTOM_ENV}/bin/python and a fresh marker, make would not re-sync: the '
        f"marker must depend on the environment's interpreter (tj-3t2axg).\n{dry_run.stdout}"
    )
    # The re-sync must re-stamp the SAME marker, or the next run re-syncs forever or never.
    assert f'touch {relative}' in dry_run.stdout, f'the sync does not re-stamp {relative}:\n{dry_run.stdout}'


@pytest.mark.parametrize('uv_project_environment', [None, CUSTOM_ENV], ids=['default', 'overridden'])
def test_the_marker_lives_inside_the_environment_uv_uses(tmp_path: Path, uv_project_environment: str | None) -> None:
    """The marker's directory IS the environment, so a venv uv recreates takes its marker with it.

    A marker beside the environment (the old .venv_init) outlives a venv that uv deleted and
    rebuilt with default-groups only, and goes on vouching for it. And the environment is the one
    uv itself uses -- UV_PROJECT_ENVIRONMENT when set, else .venv -- or make syncs one directory
    while `uv run` uses another.
    """
    expected_env = uv_project_environment or '.venv'

    env_dir = _make_value('VENV_DIR', uv_project_environment, tmp_path)
    marker = PurePosixPath(_make_value('VENV_MARKER', uv_project_environment, tmp_path))
    interpreter = _make_value('VENV_PYTHON', uv_project_environment, tmp_path)

    assert env_dir == expected_env, (
        f'VENV_DIR is {env_dir!r} with UV_PROJECT_ENVIRONMENT={uv_project_environment!r}; uv would use '
        f'{expected_env!r}, so make would sync a different environment from the one `uv run` runs in'
    )
    assert marker.parent == PurePosixPath(expected_env), (
        f'the sync marker {marker} is not directly inside the environment {expected_env}: when uv '
        f'recreates that environment the marker survives and vouches for the new, partial one'
    )
    assert interpreter == f'{expected_env}/bin/python', (
        f'the interpreter the marker depends on is {interpreter!r}, not the one in {expected_env}'
    )


def test_a_fresh_checkout_creates_and_syncs_the_environment_uv_uses(tmp_path: Path) -> None:
    """No environment at all: make creates it where uv will look, syncs it in full, stamps it."""
    for declaration in ('pyproject.toml', 'uv.lock'):
        (tmp_path / declaration).write_text('', encoding='utf-8')
    marker = _make_value('VENV_MARKER', CUSTOM_ENV, tmp_path)

    dry_run = _make(tmp_path, '-n', marker, uv_project_environment=CUSTOM_ENV)

    assert dry_run.returncode == 0, f'make -n {marker} failed: {dry_run.stderr}'
    assert f'uv venv {CUSTOM_ENV}' in dry_run.stdout, (
        f'the environment is not created in {CUSTOM_ENV}:\n{dry_run.stdout}'
    )
    assert FULL_SYNC in dry_run.stdout, f'a fresh environment is not synced in full:\n{dry_run.stdout}'
    assert f'touch {marker}' in dry_run.stdout, f'the fresh environment is not stamped:\n{dry_run.stdout}'


def _devcontainer_environment() -> dict[str, str | None]:
    """The devcontainer service's environment, list or mapping form, as a mapping."""
    document = yaml.safe_load(DEVCONTAINER_COMPOSE.read_text(encoding='utf-8'))
    service = document['services'][DEVCONTAINER_SERVICE]
    environment = service.get('environment') or {}
    if isinstance(environment, dict):
        return {str(key): None if value is None else str(value) for key, value in environment.items()}
    pairs = {}
    for entry in environment:
        name, separator, value = str(entry).partition('=')
        pairs[name] = value if separator else None
    return pairs


def test_the_devcontainer_keeps_its_own_uv_environment_apart_from_the_hosts() -> None:
    """tj-3t2axg: the container's uv must never write the host's .venv.

    /workspace is the host checkout, bind-mounted. Left on the default, the container's uv points
    .venv/bin/python at /opt/uv/python, which the host does not have, and the host's next `uv run`
    rebuilds the venv with default-groups only (and the reverse). A directory of the container's
    own ends that.

    Relative, because uv resolves a relative UV_PROJECT_ENVIRONMENT against the project root: each
    agent worktree (ADR tj-aov3ip) then keeps its own environment. An absolute path would put
    every worktree's sync into one directory, which is the shared-venv failure again.
    """
    environment = _devcontainer_environment()
    value = environment.get('UV_PROJECT_ENVIRONMENT')

    assert value, (
        f'{DEVCONTAINER_COMPOSE.relative_to(REPO_ROOT)} service {DEVCONTAINER_SERVICE!r} sets no '
        f"UV_PROJECT_ENVIRONMENT, so the container shares the host checkout's .venv"
    )
    normalised = PurePosixPath(value)
    assert not normalised.is_absolute(), (
        f'UV_PROJECT_ENVIRONMENT={value} is absolute: every agent worktree would share one environment'
    )
    assert normalised != PurePosixPath('.venv'), (
        f"UV_PROJECT_ENVIRONMENT={value} is the host's own default environment; the two sides still share it"
    )


def test_the_devcontainer_environment_is_ignored_by_git() -> None:
    """An environment git does not ignore floods `git status` and can be staged by accident."""
    value = _devcontainer_environment().get('UV_PROJECT_ENVIRONMENT')
    assert value, 'the devcontainer sets no UV_PROJECT_ENVIRONMENT (see the test above)'
    assert shutil.which('git'), 'git is not on PATH, so the ignore rules cannot be checked'

    probe = f'{value}/bin/python'
    result = subprocess.run(
        ['git', 'check-ignore', '--no-index', '-q', probe], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, (
        f'git does not ignore {probe} (git check-ignore exit {result.returncode}: {result.stderr.strip()}); '
        f'add {value}/ to .gitignore'
    )


# ---------------------------------------------------------------------------------------------
# UV_FROZEN: THE THIRD PATH TO AN ACCIDENTAL RE-LOCK (tj-d3396o)
#
# tj-3zh7ss closed two paths -- the Makefile exports UV_FROZEN=1, CI sets it at workflow level --
# and neither reaches a bare `uv run` or `uv sync` TYPED IN AN AGENT SHELL. That command
# re-resolves and rewrites uv.lock, and the damage surfaces far from its cause: a stale lock is
# what made `make security` fail with bandit missing, under four red test_security_make cases
# whose output named neither uv nor the lock.
#
# WHAT THESE PIN, AND WHAT THEY DELIBERATELY DO NOT. They pin the CONFIGURATION: the value is in
# the compose file, in the service the agent runs as, and `make lock` still strips it. They do NOT
# pin that a running shell HAS it, because that is a property of a container built after this
# commit, and no agent rebuilds its own container. At the time of writing this very shell has
# UV_PROJECT_ENVIRONMENT set and UV_FROZEN unset, which is exactly the pre-rebuild state -- so a
# runtime assertion would have to be skipped here and would then be skipped in CI too, where the
# image is also not this devcontainer. Delivery is the user's rebuild and is checked by running
# `env | grep UV_FROZEN` in a fresh agent shell, not by this file pretending to have done it.
#
# WHY compose.yml RATHER THAN devcontainer.json, verified rather than taken from the comment:
# the Makefile sets AGENT_COMPOSE := docker compose --env-file ... -f .devcontainer/compose.yml and agent-build
# runs `$(AGENT_COMPOSE) build`, so the make path never invokes the Dev Containers CLI and would
# never read containerEnv/remoteEnv. The IDE path does read devcontainer.json -- but that file
# delegates with dockerComposeFile: compose.yml and service: agent, so it arrives at this same
# block. The compose environment is therefore the ONE placement that covers both paths; a
# containerEnv entry would cover only the IDE one, which is the path agents do not use.


def test_the_devcontainer_shell_is_configured_to_refuse_an_accidental_relock() -> None:
    """tj-d3396o: UV_FROZEN=1 is set for the agent service, so a bare uv command cannot rewrite the lock.

    Asserted against the compose file rather than os.environ on purpose -- see the note above on
    configuration versus delivery.
    """
    value = _devcontainer_environment().get('UV_FROZEN')

    assert value == '1', (
        f'{DEVCONTAINER_COMPOSE.relative_to(REPO_ROOT)} service {DEVCONTAINER_SERVICE!r} sets '
        f'UV_FROZEN={value!r}, expected "1". Without it a bare `uv run` or `uv sync` typed in an '
        f'agent shell re-resolves and rewrites uv.lock, and the failure surfaces later as a sync '
        f'that silently drops a dependency group.'
    )


def test_the_deliberate_relock_still_strips_uv_frozen() -> None:
    """`make lock` is the one command that MUST re-resolve, so it unsets the variable for itself.

    This is the half that makes the freeze safe to set: without it, freezing the shell would also
    break the only supported way to update the lock, and the next person would turn the freeze off
    rather than reach for `make lock`. Read from what make WOULD run, not from the Makefile's text.
    """
    recipe = subprocess.run(
        ['make', '--no-print-directory', '-n', 'lock'], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )

    assert recipe.returncode == 0, f'make -n lock failed: {recipe.stderr.strip()}'
    assert 'uv lock' in recipe.stdout, f'make lock no longer runs uv lock; it runs: {recipe.stdout.strip()}'
    assert '-u UV_FROZEN' in recipe.stdout, (
        f'make lock does not strip UV_FROZEN, so the deliberate re-lock is frozen too and would '
        f'only validate the lock rather than update it. It runs: {recipe.stdout.strip()}'
    )
