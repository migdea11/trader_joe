"""settings.load_settings: every path set, absolute, a directory, and the snapshot's premise held.

tj-c4mosr.5 items (11) and S7 (ADR tj-4rr0la addendum 5, ruling 1, PREMISE: nothing but the MCP
writes under the stack directory, so it may lie inside neither the repository nor AGENT_HOME_PATH,
nor contain either). Since addendum 11 R1 the server sees the repository at /workspace, so for the
real host layout these checks compare a host path with a container path; the Makefile's
agent-mcp-paths target carries the host-side check (pinned in common/tests). These stay pinned for
the paths the server does see.
"""

import os
from pathlib import Path

import pytest

from tools.agent_mcp.settings import (
    DEFAULT_BIND,
    DEFAULT_HOSTNAME,
    DEFAULT_PORT,
    TOKEN_FILE_NAME,
    SettingsError,
    load_settings,
)


pytestmark = pytest.mark.build_infra

VARIABLES = ('AGENT_MCP_REPO_ROOT', 'AGENT_MCP_STACK_DIR', 'AGENT_HOME_PATH')


def _dirs(tmp_path: Path) -> dict[str, Path]:
    root = Path(os.path.realpath(tmp_path))
    paths = {
        'AGENT_MCP_REPO_ROOT': root / 'repo',
        'AGENT_MCP_STACK_DIR': root / 'stack',
        'AGENT_HOME_PATH': root / 'home',
    }
    for path in paths.values():
        path.mkdir()
    return paths


def _environ(paths: dict[str, Path], **overrides: str | None) -> dict[str, str]:
    environ = {name: str(path) for name, path in paths.items()}
    for name, value in overrides.items():
        if value is None:
            environ.pop(name, None)
        else:
            environ[name] = value
    return environ


def test_a_sane_layout_loads_with_the_documented_defaults(tmp_path: Path):
    paths = _dirs(tmp_path)
    settings = load_settings(_environ(paths))
    assert settings.repo_root == paths['AGENT_MCP_REPO_ROOT']
    assert settings.stack_dir == paths['AGENT_MCP_STACK_DIR']
    assert (
        settings.token_file
        == paths['AGENT_HOME_PATH'] / TOKEN_FILE_NAME
        == paths['AGENT_HOME_PATH'] / 'agent_mcp_token'
    )
    assert (settings.port, settings.bind, settings.hostname) == (DEFAULT_PORT, DEFAULT_BIND, DEFAULT_HOSTNAME)
    assert (DEFAULT_PORT, DEFAULT_HOSTNAME) == (8765, 'agent_mcp')


def test_agent_home_is_the_agent_home_path_setting_the_seed_output_root(tmp_path: Path):
    """tj-irhy0a.22: seed_dump writes under Settings.agent_home, which is AGENT_HOME_PATH itself.

    Not the token file and not its grandparent: a property that drifted from the setting would put
    the seeds somewhere the devcontainer does not mount at /agent_mcp_share.
    """
    paths = _dirs(tmp_path)
    settings = load_settings(_environ(paths))
    assert settings.agent_home == paths['AGENT_HOME_PATH']


@pytest.mark.parametrize('variable', VARIABLES)
@pytest.mark.parametrize('spelling', ['unset', 'empty', 'relative', 'missing', 'a file'])
def test_each_path_must_be_set_absolute_and_a_directory(tmp_path: Path, variable: str, spelling: str):
    """No default for any of the three: a path computed inside the container names nothing the daemon sees."""
    paths = _dirs(tmp_path)
    a_file = tmp_path / 'a_file'
    a_file.write_text('x')
    value = {
        'unset': None,
        'empty': '',
        'relative': os.path.relpath(paths[variable]),
        'missing': str(tmp_path / 'nowhere'),
        'a file': str(a_file),
    }[spelling]
    with pytest.raises(SettingsError, match=variable):
        load_settings(_environ(paths, **{variable: value}))


def _nest(paths: dict[str, Path], inner: str, outer: str) -> dict[str, Path]:
    """Recreate INNER's directory inside OUTER's."""
    nested = dict(paths)
    nested[inner] = paths[outer] / 'nested'
    nested[inner].mkdir()
    return nested


_PREMISE_CASES = {
    'stack dir inside the repository': ('AGENT_MCP_STACK_DIR', 'AGENT_MCP_REPO_ROOT', 'outside the repository'),
    'stack dir containing the repository': ('AGENT_MCP_REPO_ROOT', 'AGENT_MCP_STACK_DIR', 'outside the repository'),
    'stack dir inside AGENT_HOME_PATH': ('AGENT_MCP_STACK_DIR', 'AGENT_HOME_PATH', 'outside AGENT_HOME_PATH'),
    'stack dir containing AGENT_HOME_PATH': ('AGENT_HOME_PATH', 'AGENT_MCP_STACK_DIR', 'outside AGENT_HOME_PATH'),
    'AGENT_HOME_PATH inside the repository': ('AGENT_HOME_PATH', 'AGENT_MCP_REPO_ROOT', 'AGENT_HOME_PATH must lie'),
}


@pytest.mark.parametrize(('inner', 'outer', 'message'), list(_PREMISE_CASES.values()), ids=list(_PREMISE_CASES))
def test_the_snapshot_premise_refuses_overlapping_paths(tmp_path: Path, inner: str, outer: str, message: str):
    """S7 and (11): the stack directory apart from the repository and AGENT_HOME_PATH, both ways."""
    paths = _nest(_dirs(tmp_path), inner, outer)
    with pytest.raises(SettingsError, match=message):
        load_settings(_environ(paths))


@pytest.mark.parametrize('same_as', ['AGENT_MCP_REPO_ROOT', 'AGENT_HOME_PATH'])
def test_the_stack_dir_may_not_be_the_repository_or_the_home_itself(tmp_path: Path, same_as: str):
    paths = _dirs(tmp_path)
    with pytest.raises(SettingsError, match='AGENT_MCP_STACK_DIR'):
        load_settings(_environ(paths, AGENT_MCP_STACK_DIR=str(paths[same_as])))


def test_a_symlinked_stack_dir_is_judged_where_it_resolves(tmp_path: Path):
    """A link outside the repository that resolves inside it is still inside it."""
    paths = _dirs(tmp_path)
    inside = paths['AGENT_MCP_REPO_ROOT'] / 'stack_inside'
    inside.mkdir()
    link = tmp_path / 'innocent_looking'
    link.symlink_to(inside)
    with pytest.raises(SettingsError, match='outside the repository'):
        load_settings(_environ(paths, AGENT_MCP_STACK_DIR=str(link)))


@pytest.mark.parametrize('port', ['abc', '0', '65536', '-1', '80.5', ''])
def test_the_port_must_be_a_port_number(tmp_path: Path, port: str):
    with pytest.raises(SettingsError, match='AGENT_MCP_PORT'):
        load_settings(_environ(_dirs(tmp_path), AGENT_MCP_PORT=port))
