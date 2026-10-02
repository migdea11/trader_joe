"""build_infra pins for `make security`'s requirements.txt: always removed, a finding still fails.

tj-0pobey.6. The recipe exported requirements.txt, ran pip-audit, then removed the file on a line of
its own -- which make never reached when pip-audit found something, so the file was left in the
working tree. The fix removes it whatever the export or pip-audit returns and re-raises that status.

Docker- and network-free. make runs the REAL security recipe from an empty temporary directory with
`uv` stubbed first on PATH, so bandit, semgrep, the export and pip-audit are all the stub: the recipe's
own shell -- the redirect, the status capture, the rm -- is what runs. The export and pip-audit
invocations are also pinned to the CI security job's, which the recipe's comment says runs them
identically.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from common.tests.test_ci_invariants import (
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
