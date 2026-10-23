"""The server's own settings, read once from its container environment (tj-c4mosr.4 sets them).

AGENT_MCP_STACK_DIR and AGENT_MCP_REPO_HOST_PATH are HOST absolute paths mounted at the same path (ADR
tj-4rr0la addendum 1 (d) and addendum 23), because the Docker daemon resolves the snapshot's bind sources,
and the dev and test projects', on the host. The other two are CONTAINER paths since addendum 11: AGENT_MCP_REPO_ROOT is /workspace, where
the repository is mounted read-only (R1), and AGENT_HOME_PATH is the share directory, /agent_mcp_share
(R2) -- the token file and seed output, never the user's Claude config. None has a default: a default
computed from $HOME inside the container would name a directory the daemon has never seen, so an unset
one stops the server at start. The containment checks below compare container paths, so for the two
container-path settings they no longer describe the real host layout; the Makefile's host-side path
check (agent-mcp-paths) covers that.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


# The token's file name under AGENT_HOME_PATH (tj-c4mosr.3 item 2).
TOKEN_FILE_NAME = 'agent_mcp_token'  # nosec B105 -- the file's NAME; the token itself is never in source

DEFAULT_PORT = 8765
# Every interface inside the container, on purpose: reach is bounded by the NETWORK, not the bind
# address (ADR tj-4rr0la addendum 1 (c)) -- the container joins only the internal trader_joe_agent_mcp
# and agent_mcp_docker networks and publishes no port. Loopback would make it unreachable from the
# devcontainer altogether.
DEFAULT_BIND = '0.0.0.0'  # nosec B104 -- network-bounded, see above
# The compose service name the devcontainer dials; the DNS-rebinding check accepts only this Host.
DEFAULT_HOSTNAME = 'agent_mcp'


class SettingsError(Exception):
    """A setting is missing or unusable; the server refuses to start."""


@dataclass(frozen=True)
class Settings:
    """Resolved settings. Every path is absolute and free of symlinks."""

    repo_root: Path
    repo_host_path: Path
    stack_dir: Path
    token_file: Path
    bind: str
    port: int
    hostname: str

    @property
    def agent_home(self) -> Path:
        """The AGENT_HOME_PATH setting, the share directory: the token file's directory, and seed_dump's output root."""
        return self.token_file.parent


def _absolute_dir(environ: Mapping[str, str], name: str) -> Path:
    raw = environ.get(name, '')
    if not raw:
        raise SettingsError(f'{name} is not set; it has no default (a host path, mounted at the same path)')
    if not os.path.isabs(raw):
        raise SettingsError(f'{name} must be an absolute path')
    resolved = Path(os.path.realpath(raw))
    if not resolved.is_dir():
        raise SettingsError(f'{name} is not a directory: {resolved}')
    return resolved


# The marker compared through both repository paths: git's HEAD, which every checkout has and which no
# two checkouts share.
_REPO_MARKER = Path('.git') / 'HEAD'


def _same_tree(first: Path, second: Path) -> bool:
    """True when both paths reach the same checkout: the marker file is one file (device and inode).

    A bind mount shares the inode, so the same host directory mounted at two paths passes, and a
    different checkout, a copy or a directory without the marker does not.
    """
    try:
        return os.path.samefile(first / _REPO_MARKER, second / _REPO_MARKER)
    except OSError:
        return False


def _is_within(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Read and check the settings.

    AGENT_MCP_REPO_ROOT  the repository's main checkout, mounted read-only; /workspace, a container
                         path (addendum 11 R1).
    AGENT_MCP_REPO_HOST_PATH
                         the same checkout at its HOST path, mounted read-only at that same path
                         (addendum 23): the dev verbs' compose --project-directory. Required, no
                         default, and refused unless it is the very tree AGENT_MCP_REPO_ROOT is
                         (the .git/HEAD file is one file through both paths), so a wrong path stops
                         the server at start instead of aiming compose at another directory.
    AGENT_MCP_STACK_DIR  the MCP's own working directory: the worktree snapshot (source/) and the
                         audit log. A HOST path mounted at the same path, outside the repository and
                         AGENT_HOME_PATH; its default is computed by the host make target, not here.
                         It holds no env file and no compose data: the one project's data lives under
                         the checkout's ./volume_data.
    AGENT_HOME_PATH      the share directory, /agent_mcp_share, a container path (addendum 11 R2):
                         the token file is AGENT_HOME_PATH/agent_mcp_token, and seed_dump writes under
                         AGENT_HOME_PATH/agent_mcp_seeds. The name is kept from before R2.
    AGENT_MCP_PORT, AGENT_MCP_BIND, AGENT_MCP_HOSTNAME  optional.

    The containment checks run on these container paths; the host-side check in the Makefile
    (agent-mcp-paths) is what covers the real host layout.
    """
    repo_root = _absolute_dir(environ, 'AGENT_MCP_REPO_ROOT')
    repo_host_path = _absolute_dir(environ, 'AGENT_MCP_REPO_HOST_PATH')
    if not _same_tree(repo_root, repo_host_path):
        raise SettingsError(
            'AGENT_MCP_REPO_HOST_PATH is not the same checkout as AGENT_MCP_REPO_ROOT '
            f'(.git/HEAD is not one file through {repo_host_path} and the repository root); it must be the '
            'repository root checkout, mounted read-only at its host path'
        )
    stack_dir = _absolute_dir(environ, 'AGENT_MCP_STACK_DIR')
    agent_home = _absolute_dir(environ, 'AGENT_HOME_PATH')
    # The snapshot's premise (ADR tj-4rr0la addendum 5): nothing but the MCP writes under the stack
    # directory. The devcontainer mounts the repository and AGENT_HOME_PATH read-write, so the stack
    # directory may lie inside neither, nor contain either.
    if _is_within(stack_dir, repo_root) or _is_within(repo_root, stack_dir):
        raise SettingsError('AGENT_MCP_STACK_DIR must lie outside the repository, and not contain it')
    if _is_within(stack_dir, repo_host_path) or _is_within(repo_host_path, stack_dir):
        raise SettingsError('AGENT_MCP_STACK_DIR must lie outside the repository, and not contain it')
    if _is_within(stack_dir, agent_home) or _is_within(agent_home, stack_dir):
        raise SettingsError('AGENT_MCP_STACK_DIR must lie outside AGENT_HOME_PATH, and not contain it')
    if _is_within(agent_home, repo_root):
        raise SettingsError('AGENT_HOME_PATH must lie outside the repository')
    raw_port = environ.get('AGENT_MCP_PORT', str(DEFAULT_PORT))
    if not raw_port.isdigit() or not 0 < int(raw_port) < 65536:
        raise SettingsError('AGENT_MCP_PORT must be a port number')
    return Settings(
        repo_root=repo_root,
        repo_host_path=repo_host_path,
        stack_dir=stack_dir,
        token_file=agent_home / TOKEN_FILE_NAME,
        bind=environ.get('AGENT_MCP_BIND', DEFAULT_BIND),
        port=int(raw_port),
        hostname=environ.get('AGENT_MCP_HOSTNAME', DEFAULT_HOSTNAME),
    )
