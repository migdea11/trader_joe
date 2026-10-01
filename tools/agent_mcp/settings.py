"""The server's own settings, read once from its container environment (tj-c4mosr.4 sets them).

Every path here is a HOST absolute path that the MCP container mounts at the same path (ADR tj-4rr0la
addendum 1 (d)), because the Docker daemon resolves bind-mount sources on the host. A default computed
from $HOME inside the container would name a directory the daemon has never seen, so the paths have
none: an unset one stops the server at start.
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
    stack_dir: Path
    token_file: Path
    bind: str
    port: int
    hostname: str


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


def _is_within(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Read and check the settings.

    AGENT_MCP_REPO_ROOT  the repository's main checkout (mounted read-only).
    AGENT_MCP_STACK_DIR  the agent stack's own directory (env files, data, snapshot, audit log),
                         outside the repository and AGENT_HOME_PATH; default on the host
                         ${XDG_DATA_HOME:-$HOME/.local/share}/trader_joe_agent_stack, computed by the
                         host make target, not here.
    AGENT_HOME_PATH      the agent home directory; the token file is AGENT_HOME_PATH/agent_mcp_token.
    AGENT_MCP_PORT, AGENT_MCP_BIND, AGENT_MCP_HOSTNAME  optional.
    """
    repo_root = _absolute_dir(environ, 'AGENT_MCP_REPO_ROOT')
    stack_dir = _absolute_dir(environ, 'AGENT_MCP_STACK_DIR')
    agent_home = _absolute_dir(environ, 'AGENT_HOME_PATH')
    # The snapshot's premise (ADR tj-4rr0la addendum 5): nothing but the MCP writes under the stack
    # directory. The devcontainer mounts the repository and AGENT_HOME_PATH read-write, so the stack
    # directory may lie inside neither, nor contain either.
    if _is_within(stack_dir, repo_root) or _is_within(repo_root, stack_dir):
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
        stack_dir=stack_dir,
        token_file=agent_home / TOKEN_FILE_NAME,
        bind=environ.get('AGENT_MCP_BIND', DEFAULT_BIND),
        port=int(raw_port),
        hostname=environ.get('AGENT_MCP_HOSTNAME', DEFAULT_HOSTNAME),
    )
