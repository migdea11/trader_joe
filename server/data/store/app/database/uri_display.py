from sqlalchemy.engine import make_url
from sqlalchemy.engine.url import URL
from sqlalchemy.exc import ArgumentError


_UNSET = '<unset>'
_UNPARSEABLE = '<unparseable>'
_REDACTED = '***'

# libpq (and so psycopg2/asyncpg) accepts a password handed in the query string exactly as it
# accepts one in the `://user:password@` form -- e.g. the unix-socket form
# `?host=/var/run/postgresql&password=...`. SQLAlchemy's own `render_as_string(hide_password=True)`
# only touches the latter, so this key has to be masked separately or it survives untouched.
# libpq itself only recognises the lowercase key, but an operator can still type 'Password' or
# 'PASSWORD' and get a DSN that fails to connect while still carrying a real value -- masked on
# a case-folded match so a typo doesn't turn into a leak.
_QUERY_KEY_TO_REDACT = 'password'


def is_ambiguous_database_uri(uri: str) -> bool:
    """True if `uri`'s authority can't be split into userinfo and host unambiguously.

    A DSN has exactly one '@' when every reserved character in the password has been
    percent-encoded, since '@' is what separates userinfo from host. A raw, unescaped '@'
    inside the password -- reachable because docker-compose.yaml interpolates POSTGRES_PASS
    unencoded -- adds (at least) one more, and there is no reliable way to tell after the fact
    which '@' was the real separator: `make_url` picks the first one, folding the rest of the
    password into what it parses as host/path/query. Checked on the RAW string rather than the
    parsed URL, because the fallout doesn't reliably show up in one place to check for -- a
    password like 'HEAD@MID/TAIL' or 'HEAD@MID?TAIL' spreads the leaked remainder across host,
    path or query in a way a single parsed-field check (e.g. "does the host contain '@'") can
    miss, where a raw count cannot.

    Args:
        uri (str): The raw DSN.

    Returns:
        bool: True if `uri` contains more than one '@'.
    """
    return uri.count('@') > 1


def _redact_query_password(url: URL) -> URL:
    """Return `url` with any query key that case-folds to 'password' masked."""
    return url.set(
        query={key: (_REDACTED if key.lower() == _QUERY_KEY_TO_REDACT else value) for key, value in url.query.items()}
    )


def mask_database_uri(uri: str | None) -> str:
    """Render a database DSN with its password hidden, safe to log.

    tj-zb1di4: migrations/env.py used to log DATABASE_URI verbatim, which put the prod
    password in plain text in `make migrate` output and in the public repo's CI logs. This
    is the only thing that may reach a log line in its place. It never returns the input
    unchanged, and never returns anything derived from an input it cannot fully account for --
    an unset, malformed, or ambiguous DSN can still carry a password, so all three fall back to
    a fixed placeholder rather than echoing anything from `uri`.

    Two escapes beyond SQLAlchemy's own `hide_password`:
      * A password can arrive in the query string instead of the `user:password@` form (the
        unix-socket DSN, `?host=/var/run/postgresql&password=...`, is the reachable case here --
        `hide_password` never looks at the query string at all, so that key is masked separately.
      * `is_ambiguous_database_uri()` -- see its docstring -- catches a raw, unescaped '@' in the
        password before any parsing happens.

    Args:
        uri (str | None): The raw DSN, e.g. the value of DATABASE_URI. May be unset, malformed,
            or ambiguous in the ways described above.

    Returns:
        str: `uri` with its password (`user:password@` form and `?password=` query form alike)
            replaced by asterisks, or a fixed placeholder if `uri` is empty, ambiguous, or
            cannot be parsed as a URL.
    """
    if not uri:
        return _UNSET
    if is_ambiguous_database_uri(uri):
        return _UNPARSEABLE
    try:
        url = make_url(uri)
    except (ArgumentError, ValueError):
        # ArgumentError: not a URL at all. ValueError: a component parsed but failed to cast,
        # e.g. a non-numeric port -- still no password rendered, so still safe to place here.
        return _UNPARSEABLE

    return _redact_query_password(url).render_as_string(hide_password=True)
