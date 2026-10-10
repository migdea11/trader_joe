"""What the UI shell is told about this deployment (tj-grna9p.45, served by GET /ui/v1/config).

Nothing here is a secret and nothing may become one: the config is read by the browser, so no credential,
key, token or address material is read, derived or returned. It is built from three things only.

    allowed groups     SIMULATION ONLY in PR 4 (ADR tj-grna9p.10). A constant, not a build flag and not derived
                       from credentials: the UI learns the groups from the server (tj-0rpt9t), and the server
                       derives more than SIMULATION only once PR 5 gives it accounts to claim.
    deployment label   DEPLOYMENT_LABEL from the environment, for DISPLAY ONLY. Nothing may branch on it
                       (tj-0rpt9t consequence 2); unset reads as the empty string.
    server version     SERVER_VERSION from the environment when a build sets it, else the installed distribution's
                       version, else 'unknown'.
"""

from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata

from common.environment import get_env_var


DEPLOYMENT_LABEL_VAR = 'DEPLOYMENT_LABEL'
SERVER_VERSION_VAR = 'SERVER_VERSION'
_DISTRIBUTION = 'trader-joe'
UNKNOWN_VERSION = 'unknown'


class AccountGroup(StrEnum):
    """The account groups a deployment may serve. Members are the proto AccountGroup names without the prefix."""

    SIMULATION = 'SIMULATION'
    PAPER = 'PAPER'
    LIVE = 'LIVE'


# Constant until PR 5 (ADR tj-grna9p.10). A tuple so a caller cannot grow it.
ALLOWED_GROUPS: tuple[AccountGroup, ...] = (AccountGroup.SIMULATION,)


@dataclass(frozen=True)
class UiConfigView:
    """The shell's configuration.

    Attributes:
        allowed_groups: The groups this deployment serves.
        deployment_label: A display-only label; empty when none is configured.
        server_version: The server's version, or 'unknown'.
    """

    allowed_groups: tuple[AccountGroup, ...]
    deployment_label: str
    server_version: str


def _server_version() -> str:
    configured = get_env_var(SERVER_VERSION_VAR, default='')
    if configured:
        return configured
    try:
        return metadata.version(_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return UNKNOWN_VERSION


def ui_config() -> UiConfigView:
    """Build the shell's configuration from the server's own state.

    Returns:
        UiConfigView: The allowed groups (SIMULATION only), the deployment label and the server version.
    """
    return UiConfigView(
        allowed_groups=ALLOWED_GROUPS,
        deployment_label=get_env_var(DEPLOYMENT_LABEL_VAR, default='').strip(),
        server_version=_server_version(),
    )
