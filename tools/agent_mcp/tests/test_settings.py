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

VARIABLES = ('AGENT_MCP_REPO_ROOT', 'AGENT_MCP_REPO_HOST_PATH', 'AGENT_MCP_STACK_DIR', 'AGENT_HOME_PATH')
HOST_PATH = 'AGENT_MCP_REPO_HOST_PATH'
NOT_THE_SAME_CHECKOUT = 'AGENT_MCP_REPO_HOST_PATH is not the same checkout as AGENT_MCP_REPO_ROOT'


def _checkout(path: Path) -> Path:
    """A directory holding .git/HEAD, the marker settings compares through both repository paths."""
    (path / '.git').mkdir(parents=True)
    (path / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')
    return path


def _bind_stand_in(repo: Path, path: Path) -> Path:
    """PATH as a second view of REPO's tree: its .git/HEAD is REPO's own file (a hard link).

    In agent_mcp the host path is a bind mount of the same checkout, so both paths differ as strings
    and as realpaths while every file is one inode. A hard link is the one unprivileged way to get
    exactly that in a test; a symlink would collapse into repo_root under realpath.
    """
    (path / '.git').mkdir(parents=True)
    os.link(repo / '.git' / 'HEAD', path / '.git' / 'HEAD')
    return path


def _dirs(tmp_path: Path) -> dict[str, Path]:
    root = Path(os.path.realpath(tmp_path))
    paths = {
        'AGENT_MCP_REPO_ROOT': _checkout(root / 'repo'),
        'AGENT_MCP_STACK_DIR': root / 'stack',
        'AGENT_HOME_PATH': root / 'home',
    }
    for name in ('AGENT_MCP_STACK_DIR', 'AGENT_HOME_PATH'):
        paths[name].mkdir()
    paths[HOST_PATH] = _bind_stand_in(paths['AGENT_MCP_REPO_ROOT'], root / 'host' / 'repo')
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
    assert settings.repo_host_path == paths[HOST_PATH] != settings.repo_root
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
    """No default for any of the four: a path computed inside the container names nothing the daemon sees."""
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
    """Recreate INNER's directory inside OUTER's (a moved repository keeps a host path onto its own tree)."""
    nested = dict(paths)
    nested[inner] = paths[outer] / 'nested'
    if inner == 'AGENT_MCP_REPO_ROOT':
        _checkout(nested[inner])
        nested[HOST_PATH] = _bind_stand_in(nested[inner], paths[HOST_PATH].parent / 'moved')
    elif inner == HOST_PATH:
        _bind_stand_in(paths['AGENT_MCP_REPO_ROOT'], nested[inner])
    else:
        nested[inner].mkdir()
    return nested


_PREMISE_CASES = {
    'stack dir inside the repository': ('AGENT_MCP_STACK_DIR', 'AGENT_MCP_REPO_ROOT', 'outside the repository'),
    'stack dir containing the repository': ('AGENT_MCP_REPO_ROOT', 'AGENT_MCP_STACK_DIR', 'outside the repository'),
    # The HOST path's own clause (tj-iee6sn.5): in agent_mcp it is a second mount of the checkout, a
    # different path from /workspace, so the repository clauses above never see it.
    'stack dir inside the repository host path': ('AGENT_MCP_STACK_DIR', HOST_PATH, 'outside the repository'),
    'stack dir containing the repository host path': (HOST_PATH, 'AGENT_MCP_STACK_DIR', 'outside the repository'),
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


@pytest.mark.parametrize('same_as', ['AGENT_MCP_REPO_ROOT', HOST_PATH, 'AGENT_HOME_PATH'])
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


# --- AGENT_MCP_REPO_HOST_PATH is the very tree AGENT_MCP_REPO_ROOT is (tj-iee6sn.5; addendum 23) -----
#
# The dev verbs hand this path to compose as --project-directory, so the daemon binds whatever it
# names into the user's dev containers. A wrong path must stop the server at start, not aim compose at
# another directory.


def _host_path_variants(paths: dict[str, Path]) -> dict[str, Path]:
    """Directories that are NOT the repository's tree, each a way the host path could be wrong."""
    repo = paths['AGENT_MCP_REPO_ROOT']
    root = repo.parent
    copy = _checkout(root / 'a_copy')
    copy.joinpath('.git', 'HEAD').write_text(repo.joinpath('.git', 'HEAD').read_text())
    unmarked = root / 'unmarked'
    unmarked.mkdir()
    # An agent worktree of this very repository: git puts a .git FILE there, never .git/HEAD.
    worktree = repo / '.claude' / 'worktrees' / 'wt'
    worktree.mkdir(parents=True)
    worktree.joinpath('.git').write_text(f'gitdir: {repo}/.git/worktrees/wt\n')
    # The repository's parent and a subdirectory of it: neither holds the marker.
    subdirectory = repo / 'server'
    subdirectory.mkdir()
    return {
        'another checkout with an identical HEAD': copy,
        'a directory without .git/HEAD': unmarked,
        'an agent worktree of the repository': worktree,
        'the parent of the repository': root,
        'a subdirectory of the repository': subdirectory,
    }


_WRONG_TREES = (
    'another checkout with an identical HEAD',
    'a directory without .git/HEAD',
    'an agent worktree of the repository',
    'the parent of the repository',
    'a subdirectory of the repository',
)


@pytest.mark.parametrize('variant', _WRONG_TREES)
def test_a_host_path_that_is_not_the_same_tree_is_refused(tmp_path: Path, variant: str):
    """Compared by identity (.git/HEAD one file through both paths), so equal CONTENT is not enough."""
    paths = _dirs(tmp_path)
    wrong = _host_path_variants(paths)[variant]
    with pytest.raises(SettingsError, match=NOT_THE_SAME_CHECKOUT):
        load_settings(_environ(paths, **{HOST_PATH: str(wrong)}))


def test_the_refusal_names_the_host_path_it_rejected(tmp_path: Path):
    paths = _dirs(tmp_path)
    wrong = _host_path_variants(paths)['a directory without .git/HEAD']
    with pytest.raises(SettingsError) as raised:
        load_settings(_environ(paths, **{HOST_PATH: str(wrong)}))
    assert str(wrong) in str(raised.value), raised.value


def test_a_symlink_alias_of_the_repository_passes_and_resolves_to_it(tmp_path: Path):
    """An alias of the same tree is the same tree; Settings keeps the resolved path, free of symlinks."""
    paths = _dirs(tmp_path)
    alias = tmp_path / 'alias'
    alias.symlink_to(paths['AGENT_MCP_REPO_ROOT'], target_is_directory=True)
    settings = load_settings(_environ(paths, **{HOST_PATH: str(alias)}))
    assert settings.repo_host_path == paths['AGENT_MCP_REPO_ROOT'] == settings.repo_root


def test_the_repository_root_itself_passes_as_its_own_host_path(tmp_path: Path):
    """The devcontainer-less case where both strings agree: still one tree, still accepted."""
    paths = _dirs(tmp_path)
    settings = load_settings(_environ(paths, **{HOST_PATH: str(paths['AGENT_MCP_REPO_ROOT'])}))
    assert settings.repo_host_path == settings.repo_root
