"""Credential providers: which key a vendor call is made with.

PR 5 builds the interface, a static provider over today's configured key and the most-headroom choice. The
account and credential mapping that makes several credentials real is later (tj-grna9p.24, tj-6sdc8j).
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from common.environment import get_env_var
from data.ingest.app.scheduler.limits import SchedulerConfigError


@dataclass(frozen=True)
class Credential:
    """One vendor key pair.

    Attributes:
        key_id (str): Public key id. Safe to log.
        secret (str): The secret. Excluded from repr so it never reaches a log line or a traceback.
    """

    key_id: str
    secret: str = field(repr=False)


# Printable ASCII 0x21 to 0x7e: no space, control character or non-ASCII. Matched with fullmatch, so a trailing
# newline fails.
_VISIBLE_ASCII: Final = re.compile(r'[\x21-\x7e]+')


class CredentialShapeError(ValueError):
    """A key id or secret is empty or not wholly visible ASCII. Carries no value, no length and no position."""


def check_credential_shape(credential: Credential) -> None:
    """Refuse a key id or secret that is empty or holds a character outside 0x21 to 0x7e.

    A value with a stray CR, LF or space is rejected by the HTTP layer with an error whose text carries the value.
    Refusing it first keeps the value out of every traceback and log line. Never strips or repairs the value.

    Anything that sends a credential over HTTP calls this first. The scheduler's own provider does not, because the
    credential it holds only picks a quota and is never sent; when the account mapping (tj-grna9p.24) starts sending
    provider credentials, the sender must call this predicate.

    Args:
        credential (Credential): The key to check.

    Raises:
        CredentialShapeError: If either value is empty or holds a character outside 0x21 to 0x7e. The error has no
            message and no chained cause.
    """
    if not (_VISIBLE_ASCII.fullmatch(credential.key_id) and _VISIBLE_ASCII.fullmatch(credential.secret)):
        raise CredentialShapeError


class CredentialProvider(Protocol):
    """Supplies the credentials a scheduler may spend against its quota."""

    def credentials(self) -> Sequence[Credential]:
        """List every credential available right now, never empty.

        Returns:
            Sequence[Credential]: The credentials to choose among.
        """
        ...


class StaticCredentialProvider:
    """A provider over a fixed list of credentials, read once."""

    def __init__(self, *credentials: Credential) -> None:
        if not credentials:
            raise SchedulerConfigError('A static credential provider needs at least one credential')
        self.__credentials = tuple(credentials)

    @classmethod
    def from_env(cls, key_var: str, secret_var: str) -> 'StaticCredentialProvider':
        """Build a provider over the one key configured in the environment today.

        Args:
            key_var (str): Name of the variable holding the key id.
            secret_var (str): Name of the variable holding the secret.

        Returns:
            StaticCredentialProvider: Provider with that single credential.

        Raises:
            SchedulerConfigError: If either variable is unset or empty. The message names the variables, never a value.
        """
        key_id = get_env_var(key_var)
        secret = get_env_var(secret_var)
        if not key_id or not secret:
            raise SchedulerConfigError(f'Credentials are not configured: set {key_var} and {secret_var}')
        return cls(Credential(key_id=key_id, secret=secret))

    def credentials(self) -> Sequence[Credential]:
        """List the fixed credentials.

        Returns:
            Sequence[Credential]: Every credential given at construction.
        """
        return self.__credentials


def most_headroom(provider: CredentialProvider, headroom: Callable[[Credential], float]) -> Credential:
    """Choose the credential with the most quota left.

    Args:
        provider (CredentialProvider): Where the candidates come from.
        headroom (Callable[[Credential], float]): Quota left for a credential; higher is better.

    Returns:
        Credential: The one with the largest headroom; the earliest listed wins a tie.

    Raises:
        SchedulerConfigError: If the provider lists none.
    """
    candidates = provider.credentials()
    if not candidates:
        raise SchedulerConfigError('The credential provider listed no credentials')
    return max(candidates, key=headroom)
