"""build_infra pins for `make security`'s requirements.txt: always removed, a finding still fails.

tj-0pobey.6. The recipe exported requirements.txt, ran pip-audit, then removed the file on a line of
its own -- which make never reached when pip-audit found something, so the file was left in the
working tree. The fix removes it whatever the export or pip-audit returns and re-raises that status.

Docker- and network-free. make runs the REAL security recipe from an empty temporary directory with
`uv` stubbed first on PATH, so bandit, semgrep, the export and pip-audit are all the stub: the recipe's
own shell -- the redirect, the status capture, the rm -- is what runs. The export and pip-audit
invocations are also pinned to the CI security job's, which the recipe's comment says runs them
identically.

The colour tests at the end (tj-3mk3u5.43) are the one exception: canned text can never carry a colour
code, so their stub hands `export` to the real uv, offline, against this repository's lock.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from common.tests.test_ci_invariants import (
    REPO_ROOT,
    TESTING_WORKFLOW,
    _commands,
    _load_yaml,
    _make_recipe,
    _run_lines,
    _run_make,
    _subprocess_env,
)


pytestmark = pytest.mark.build_infra

SECURITY = 'security'
REQUIREMENTS = 'requirements.txt'
EXPORT_LINE = 'pinned-package==1.2.3'

# Logs every call; `uv export` prints EXPORT_LINE and exits EXPORT_EXIT; `uv run pip-audit` records
# what requirements.txt held when it ran (or MISSING) and exits AUDIT_EXIT; anything else exits 0.
UV_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$UV_LOG"
if [ "$1" = export ]; then
    printf '%s\\n' "$EXPORT_LINE"
    exit "${EXPORT_EXIT:-0}"
fi
if [ "$1" = run ] && [ "$2" = pip-audit ]; then
    if [ -f requirements.txt ]; then cat requirements.txt >> "$AUDIT_SAW"; else echo MISSING >> "$AUDIT_SAW"; fi
    exit "${AUDIT_EXIT:-0}"
fi
exit 0
"""


def _security(tmp_path: Path, **values: str) -> tuple[subprocess.CompletedProcess, Path, dict[str, Path]]:
    """Run the real security recipe from an empty directory with `uv` stubbed; returns (result, work, logs)."""
    work = tmp_path / 'work'
    work.mkdir()
    stubs = tmp_path / 'stubs'
    stubs.mkdir()
    (stubs / 'uv').write_text(UV_STUB)
    (stubs / 'uv').chmod(0o755)
    logs = {'uv': tmp_path / 'uv.log', 'audit': tmp_path / 'audit.saw'}
    env = _subprocess_env()
    env.update(
        PATH=f'{stubs}:{os.environ["PATH"]}',
        UV_LOG=str(logs['uv']),
        AUDIT_SAW=str(logs['audit']),
        EXPORT_LINE=EXPORT_LINE,
        **values,
    )
    return _run_make(work, SECURITY, env=env), work, logs


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding='utf-8').splitlines() if path.exists() else []


def _recipe_status(result: subprocess.CompletedProcess) -> int:
    """The failing recipe line's own status: make exits 2 on any failure and names the status in 'Error N'."""
    if result.returncode == 0:
        return 0
    found = re.findall(rf'\[[^\]]*{re.escape(SECURITY)}\] Error (\d+)', result.stderr)
    assert found, f'make failed without naming the recipe status:\n{result.stderr}'
    return int(found[-1])


def _ran(logs: dict[str, Path], *words: str) -> bool:
    return any(line.split()[: len(words)] == list(words) for line in _lines(logs['uv']))


# --- The cleanup: requirements.txt never survives the target, and the status is the command's -----


@pytest.mark.parametrize('audit_status', ['1', '3'])
def test_a_pip_audit_finding_fails_the_target_and_still_removes_requirements_txt(tmp_path: Path, audit_status: str):
    """The bug: pip-audit non-zero -> the target fails with pip-audit's status, and the file is gone."""
    result, work, logs = _security(tmp_path, AUDIT_EXIT=audit_status)
    assert result.returncode != 0, f'a pip-audit finding did not fail make security:\n{result.stdout}'
    assert _recipe_status(result) == int(audit_status), result.stderr
    assert _lines(logs['audit']) == [EXPORT_LINE], 'pip-audit did not audit the exported requirements'
    assert not (work / REQUIREMENTS).exists(), f'{REQUIREMENTS} was left behind after a pip-audit finding'


def test_a_clean_audit_passes_and_removes_requirements_txt(tmp_path: Path):
    """The converse: exit 0, every scanner ran, pip-audit saw the export, and the file is gone."""
    result, work, logs = _security(tmp_path)
    assert result.returncode == 0, f'{result.stdout}{result.stderr}'
    for words in (('run', 'bandit'), ('run', 'semgrep'), ('export',), ('run', 'pip-audit')):
        assert _ran(logs, *words), f'uv {" ".join(words)} never ran: {_lines(logs["uv"])}'
    assert _lines(logs['audit']) == [EXPORT_LINE]
    assert not (work / REQUIREMENTS).exists(), f'{REQUIREMENTS} was left behind after a clean audit'


def test_a_failed_export_fails_the_target_removes_the_redirected_file_and_skips_the_audit(tmp_path: Path):
    """The redirect creates requirements.txt before a --locked export can fail; the target removes it."""
    result, work, logs = _security(tmp_path, EXPORT_EXIT='2')
    assert result.returncode != 0, f'a failed export did not fail make security:\n{result.stdout}'
    assert _recipe_status(result) == 2, result.stderr
    assert not _ran(logs, 'run', 'pip-audit'), 'pip-audit ran after the export failed'
    assert not (work / REQUIREMENTS).exists(), f'{REQUIREMENTS} was left behind after a failed export'


# --- CI parity: the cleanup is appended, the invocations stay the CI job's -------------------------


def _ci_security_step(step_name: str) -> list[str]:
    jobs = (_load_yaml(TESTING_WORKFLOW) or {}).get('jobs') or {}
    job = next(job for job in jobs.values() if (job or {}).get('name') == 'Security Checks')
    steps = [step for step in job['steps'] if step.get('name') == step_name]
    assert len(steps) == 1, f'Security Checks: {len(steps)} steps named {step_name!r}'
    return _run_lines(steps[0].get('run') or '')


@pytest.mark.parametrize(
    ('step_name', 'prefix'),
    [('Generate Requirements', 'uv export'), ('Run Pip-Audit', 'uv run pip-audit')],
    ids=['export', 'pip-audit'],
)
def test_the_recipes_first_command_is_the_ci_steps_one_command(step_name: str, prefix: str):
    """'The CI security job runs the identical line': CI's one command == the recipe line's first command."""
    [ci_line] = _ci_security_step(step_name)
    [make_line] = [line for line in _make_recipe(SECURITY) if line.startswith(prefix)]
    [ci_command] = _commands(ci_line)
    assert _commands(make_line)[0] == ci_command, (make_line, ci_line)


# --- Colour: the export is plain text whatever colour the caller forces (tj-3mk3u5.43) --------------
#
# uv colours its output when the CALLER's environment forces colour, even into a redirect. Under
# FORCE_COLOR=3, which agent shells set, requirements.txt began '\x1b[32m# This file was autogenerated'
# and pip-audit failed at line 1 after bandit and semgrep had passed. CI forces no colour, and its
# export is pinned to the recipe's above, so the recipe is where this is pinned.
#
# The real export reads only pyproject.toml and uv.lock and takes milliseconds. UV_OFFLINE makes uv
# fail rather than reach the network.

ESC = b'\x1b'
# Either variable on its own makes uv colour an export into a file.
COLOUR_FORCING = {'FORCE_COLOR': '3', 'CLICOLOR_FORCE': '1'}

# `uv export` is the real uv's; pip-audit keeps a copy of the requirements.txt it would have read;
# bandit, semgrep and anything else exit 0.
REAL_EXPORT_STUB = """#!/bin/sh
if [ "$1" = export ]; then exec "$REAL_UV" "$@"; fi
if [ "$1" = run ] && [ "$2" = pip-audit ]; then cp requirements.txt "$AUDIT_SAW"; fi
exit 0
"""


def _real_uv() -> str:
    uv = shutil.which('uv')
    assert uv, 'uv is not on PATH, so the real export cannot run'
    return uv


def _colour_forced_env(forcing: str, **values: str) -> dict[str, str]:
    """This environment with colour forced by `forcing` alone, and uv offline on this repository's project."""
    env = _subprocess_env(NO_COLOR=None, **dict.fromkeys(COLOUR_FORCING))
    env.update({forcing: COLOUR_FORCING[forcing], 'UV_OFFLINE': '1', 'UV_PROJECT': str(REPO_ROOT)}, **values)
    return env


@pytest.mark.parametrize('forcing', COLOUR_FORCING)
def test_the_recipes_export_is_plain_text_when_the_caller_forces_colour(tmp_path: Path, forcing: str):
    """The file pip-audit reads is the real export of the lock, and not one escape byte is in it."""
    work = tmp_path / 'work'
    work.mkdir()
    stubs = tmp_path / 'stubs'
    stubs.mkdir()
    (stubs / 'uv').write_text(REAL_EXPORT_STUB)
    (stubs / 'uv').chmod(0o755)
    saw = tmp_path / 'audit.saw'
    env = _colour_forced_env(forcing, PATH=f'{stubs}:{os.environ["PATH"]}', REAL_UV=_real_uv(), AUDIT_SAW=str(saw))
    result = _run_make(work, SECURITY, env=env)
    assert result.returncode == 0, f'{result.stdout}{result.stderr}'
    assert saw.exists(), f'pip-audit never saw {REQUIREMENTS}:\n{result.stdout}'
    exported = saw.read_bytes()
    # Pinned requirements with their hashes: what uv exports from the lock, not a stub's canned line.
    assert re.search(rb'^[A-Za-z0-9._-]+==\S', exported, re.MULTILINE), f'not an export of the lock: {exported[:200]!r}'
    assert b'--hash=sha256:' in exported, f'not the real export of the lock: {exported[:200]!r}'
    assert ESC not in exported, f'{forcing} coloured the {REQUIREMENTS} pip-audit reads: {exported[:80]!r}'


@pytest.mark.parametrize('forcing', COLOUR_FORCING)
def test_the_forcing_variable_does_colour_an_export_that_names_no_colour(tmp_path: Path, forcing: str):
    """The premise of the test above. Without it, a uv that ignored `forcing` would leave that test asserting nothing.

    If this fails, uv no longer colours a file under `forcing`, and the test above passes whether or
    not the recipe passes --color never.
    """
    exported = tmp_path / REQUIREMENTS
    with exported.open('wb') as out:
        result = subprocess.run(
            [_real_uv(), 'export', '--locked', '--format', 'requirements-txt'],
            stdout=out,
            stderr=subprocess.PIPE,
            cwd=tmp_path,
            env=_colour_forced_env(forcing),
            check=False,
        )
    assert result.returncode == 0, result.stderr
    assert ESC in exported.read_bytes(), f'{forcing} did not colour an export into a file'
