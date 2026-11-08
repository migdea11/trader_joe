from collections.abc import Sequence

from common.errors.vocabulary import ExogenousError, Reason


class MissingCredentialsError(ExogenousError):
    """Raised when a vendor client is built without the credentials that vendor needs.

    get_env_var() returns None for an unset variable rather than raising, so an adapter
    that hands those straight to its SDK gets the vendor's own generic complaint --
    alpaca-py says 'You must supply a method of authentication' and names nothing. This
    names the variables that are actually unset instead (tj-84jfb9).

    It lives at the brokers level, not inside one adapter, because every adapter reads
    credentials the same way and none of them should import another's module for an error.

    It is an ExogenousError with the reason VENDOR_AUTH (ADR tj-fa1rpu D5): expected, outside our
    control and signalled, like a vendor that refuses the credentials it was given. The detail
    names the variables that are unset and never holds a credential's value.

    Args:
        detail (str): One sentence for a human, naming the unset variables.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(Reason.VENDOR_AUTH, detail)


class MalformedCredentialsError(ExogenousError):
    """Raised when a configured credential is not non-empty visible ASCII, so it is refused before any request.

    A sibling leaf of MissingCredentialsError, not a subclass: a leaf subclasses exactly one branch directly and
    nothing subclasses a leaf (ADR tj-fa1rpu D5). Handlers that mean both name both classes.

    The HTTP layer's own rejection of such a value quotes it, which is what this keeps out of the logs. It takes
    the variable NAMES and builds the sentence itself, so no caller can pass a value in. The sentence always names
    every variable, whichever failed, and never holds a value, a length, a position or a character.

    Args:
        variables (Sequence[str]): Names of the credential variables that were checked.
    """

    def __init__(self, variables: Sequence[str]) -> None:
        super().__init__(
            Reason.VENDOR_AUTH,
            f'Vendor credentials are malformed: {" and ".join(variables)} must each be non-empty visible ASCII',
        )
