class MissingCredentialsError(RuntimeError):
    """Raised when a vendor client is built without the credentials that vendor needs.

    get_env_var() returns None for an unset variable rather than raising, so an adapter
    that hands those straight to its SDK gets the vendor's own generic complaint --
    alpaca-py says 'You must supply a method of authentication' and names nothing. This
    names the variables that are actually unset instead (tj-84jfb9).

    It lives at the brokers level, not inside one adapter, because every adapter reads
    credentials the same way and none of them should import another's module for an error.
    """
