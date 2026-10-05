"""The agent-mcp dependency group lives in the MCP image alone; the MCP image is built as ruled.

tj-c4mosr.5 body bullet 9 (the agent-mcp group in no stage of the service Dockerfile, not in
default-groups, not in CI), the 01:37 ruling on the validator's tj-c4mosr.9 comment (CI's two test
installs and the Makefile's venv sync are ONE group set: every group but security and agent-mcp; the
pip-audit export in CI and in make security keeps agent-mcp and drops security), and item (12) (the
[tool.uv] conflict, the image's frozen --only-group sync, digest-pinned bases, a non-root USER, no
safe.directory, no docker group, the pyjwt floor). Design: ADR tj-4rr0la section 6, addenda 1 (a), 6 (D6, D7).

Steps are located by name, never by line number.
"""

import re
import shlex
import tomllib
from itertools import pairwise

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from common.tests.test_ci_invariants import (
    MAKEFILE,
    PYPROJECT,
    REPO_ROOT,
    TESTING_WORKFLOW,
    _load_yaml,
    _make_recipe,
    _run_lines,
    _workflow_files,
)


pytestmark = pytest.mark.build_infra

GROUP = 'agent-mcp'
MCP_DOCKERFILE = REPO_ROOT / 'tools' / 'agent_mcp' / 'Dockerfile'
SERVICE_DOCKERFILE = REPO_ROOT / 'Dockerfile'


def _target_lines(target: str) -> list[str]:
    """A target's recipe lines, read past the column-0 comment lines _make_recipe stops at."""
    lines = MAKEFILE.read_text(encoding='utf-8').replace('\\\n', ' ').splitlines()
    start = next(index for index, line in enumerate(lines) if re.match(rf'^{re.escape(target)}\s*:(?!=)', line))
    recipe = []
    for line in lines[start + 1 :]:
        if line.startswith('\t'):
            recipe.append(line.strip().lstrip('@-+').strip())
        elif line.strip() and not line.startswith('#'):
            break
    return recipe


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding='utf-8'))


def _all_groups() -> set[str]:
    return set(_pyproject()['dependency-groups'])


def _groups(words: list[str]) -> set[str] | None:
    """The dependency groups a `uv sync` / `uv export` argv installs, or None when it is not one."""
    for index, word in enumerate(words):
        if word == 'uv' and index + 1 < len(words) and words[index + 1] in ('sync', 'export'):
            flags = words[index + 2 :]
            break
    else:
        return None
    only = {value for flag, value in pairwise(flags) if flag == '--only-group'}
    dropped = {value for flag, value in pairwise(flags) if flag == '--no-group'}
    if only:
        return only
    if '--all-groups' in flags:
        return _all_groups() - dropped
    default = set(_pyproject()['tool']['uv']['default-groups'])
    added = {value for flag, value in pairwise(flags) if flag == '--group'}
    return (default | added) - dropped


def _workflow_step_groups() -> dict[str, set[str]]:
    found = {}
    for path in _workflow_files():
        for job_id, job in ((_load_yaml(path) or {}).get('jobs') or {}).items():
            for step in (job or {}).get('steps') or []:
                for line in _run_lines(step.get('run') or ''):
                    if not re.search(r'\buv\s+(sync|export)\b', line):
                        continue
                    groups = _groups(shlex.split(line.split('>')[0]))
                    if groups is not None:
                        found[f'{path.name} {job_id} {step.get("name")}'] = groups
    return found


def _step(job_name: str, step_name: str) -> list[str]:
    jobs = (_load_yaml(TESTING_WORKFLOW) or {}).get('jobs') or {}
    job = next(job for job in jobs.values() if (job or {}).get('name') == job_name)
    steps = [step for step in job['steps'] if step.get('name') == step_name]
    assert len(steps) == 1, f'{job_name}: {len(steps)} steps named {step_name!r}'
    return _run_lines(steps[0].get('run') or '')


def _one_uv(lines: list[str]) -> set[str]:
    sets = [groups for line in lines if (groups := _groups(shlex.split(line.split('>')[0]))) is not None]
    assert len(sets) == 1, f'expected one uv sync/export, found {lines}'
    return sets[0]


def test_the_venv_ci_test_installs_are_one_group_set_without_security_or_agent_mcp():
    """01:37: workflow 'Install dependencies' (testing and system jobs) == Makefile venv sync == all but security, agent-mcp."""
    expected = _all_groups() - {'security', GROUP}
    venv = [line for line in _target_lines('$(VENV_MARKER)') if line.startswith('uv sync')]
    assert [_groups(shlex.split(line)) for line in venv] == [expected], venv
    for job in ('Linting and Unit Testing', 'System Testing'):
        assert _one_uv(_step(job, 'Install dependencies')) == expected, f'{job} installs a different group set'


def test_the_audit_export_keeps_agent_mcp_and_drops_security_in_ci_and_make():
    """01:37 / ADR s6: pip-audit in make security covers the agent-mcp group (mcp, pyjwt>=2.15, CVE-2026-101918)."""
    ci = _one_uv(_step('Security Checks', 'Generate Requirements'))
    make = _one_uv([line for line in _make_recipe('security') if line.startswith('uv export')])
    assert ci == make, (ci, make)
    assert GROUP in ci and 'security' not in ci, ci


def test_no_ci_install_takes_the_agent_mcp_group():
    """Body bullet 9: not in CI -- every uv sync in every workflow leaves agent-mcp out."""
    installs = {
        where: groups for where, groups in _workflow_step_groups().items() if 'Generate Requirements' not in where
    }
    assert installs, 'no uv sync found in any workflow'
    assert not [where for where, groups in installs.items() if GROUP in groups], installs


def test_init_and_default_groups_leave_agent_mcp_out():
    assert GROUP not in _pyproject()['tool']['uv']['default-groups']
    init = [line for line in _target_lines('init') if line.startswith('uv sync')]
    assert [_groups(shlex.split(line)) for line in init] == [_all_groups() - {GROUP}], init


def test_agent_mcp_and_security_resolve_apart_and_pyjwt_has_its_floor():
    """(12), D7: the [tool.uv] conflict, and pyjwt>=2.15.0 in agent-mcp.

    The floor excludes every 2.14.x (CVE-2026-101918, fixed in 2.15.0; tj-0pobey.5) and with it
    every 2.13.x (the ten advisories fixed in 2.14.0). The agent-mcp group's locked pyjwt must
    meet that floor; the security group's fork, resolved apart, is semgrep's to cap.
    """
    conflicts = _pyproject()['tool']['uv']['conflicts']
    assert [{'group': GROUP}, {'group': 'security'}] in conflicts or [
        {'group': 'security'},
        {'group': GROUP},
    ] in conflicts
    requirements = [Requirement(item) for item in _pyproject()['dependency-groups'][GROUP] if isinstance(item, str)]
    pyjwt = [requirement for requirement in requirements if requirement.name.lower() == 'pyjwt']
    assert len(pyjwt) == 1, requirements
    specifier = pyjwt[0].specifier
    vulnerable = [
        version for version in ('2.13.0', '2.13.9', '2.14.0', '2.14.9') if specifier.contains(Version(version))
    ]
    assert not vulnerable, f'{pyjwt[0]} admits vulnerable pyjwt {vulnerable}'
    assert specifier.contains(Version('2.15.0')), pyjwt
    lock = tomllib.loads((REPO_ROOT / 'uv.lock').read_text(encoding='utf-8'))
    project = next(package for package in lock['package'] if package['name'] == 'trader-joe')
    locked = [dep['version'] for dep in project['dev-dependencies'][GROUP] if dep['name'] == 'pyjwt']
    assert len(locked) == 1 and specifier.contains(Version(locked[0])), (locked, str(specifier))


def test_no_stage_of_the_service_dockerfile_installs_agent_mcp():
    text = SERVICE_DOCKERFILE.read_text(encoding='utf-8').replace('\\\n', ' ')
    syncs = [line for line in text.splitlines() if 'uv sync' in line and not line.lstrip().startswith('#')]
    assert syncs, 'the service Dockerfile runs no uv sync, so this pin would guard nothing'
    for line in syncs:
        words = shlex.split(line[line.index('uv sync') :].split('&&')[0])
        groups = _groups(words)
        assert groups is not None and GROUP not in groups, line
    assert GROUP not in text


def _mcp_dockerfile_lines() -> list[str]:
    text = MCP_DOCKERFILE.read_text(encoding='utf-8').replace('\\\n', ' ')
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]


def test_the_mcp_image_syncs_only_agent_mcp_from_the_frozen_lock():
    syncs = [line for line in _mcp_dockerfile_lines() if 'uv sync' in line]
    assert syncs == ['RUN uv sync --only-group agent-mcp --frozen'], syncs


def test_every_base_image_of_the_mcp_image_is_pinned_by_digest():
    images = [line.split()[1] for line in _mcp_dockerfile_lines() if line.startswith('FROM ')]
    images += re.findall(r'COPY --from=(\S+/\S+)', '\n'.join(_mcp_dockerfile_lines()))
    assert images, 'no FROM found'
    unpinned = [image for image in images if not re.search(r'@sha256:[0-9a-f]{64}$', image)]
    assert not unpinned, f'unpinned base images: {unpinned}'


def test_the_mcp_image_runs_as_its_own_non_root_user_with_no_docker_group():
    """(12): USER non-root; no docker group (the daemon is reached over TCP via the proxy); no safe.directory."""
    lines = _mcp_dockerfile_lines()
    users = [line.split()[1] for line in lines if line.startswith('USER ')]
    assert users and users[-1] not in ('root', '0', '0:0'), users
    joined = '\n'.join(lines)
    assert 'safe.directory' not in joined
    assert not re.search(r'groupadd[^\n]*\bdocker\b|usermod|-G\s+\S*docker|\bgpasswd\b', joined), (
        'a docker group in the MCP image'
    )
    assert 'docker.sock' not in joined
