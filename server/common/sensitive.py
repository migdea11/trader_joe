"""Redaction of sensitive values where they are RENDERED, not where they are stored or sent.

Design: tj-vhboky.41 ADDENDUM 1, D3. The user ruled against hiding detail wholesale (no engine
hide_parameters): requests, parameters and every non-sensitive value stay in the logs, and only
the fields on the sensitive list are kept out of the text that gets rendered. The list is the
user's answer on tj-vhboky.45: the dataset entry's `owner`, and nothing else for now. A field
joins the list by being declared with one of the aliases below, and a column by being declared
with SensitiveString (common/database/sql_alchemy_sensitive_string.py).

This module holds three layers:
- M1, the SQL-parameter half: REDACTED and RedactedStr. SensitiveString's bind processor wraps
  each bound value in a RedactedStr, so every repr()-based rendering of the parameters --
  SQLAlchemy's exception text ("[parameters: ...]"), engine echo, and uvicorn's traceback of an
  unhandled 500 -- shows the marker. The driver still sends and stores the real value: the
  asyncpg spike (tj-w6bpjm notes, host run 2026-09-29) showed the subclass reaches asyncpg,
  stores byte-identical to a plain str, matches in equality filters and ON CONFLICT.
- M2, the pydantic half: SensitiveStr and OptionalSensitiveStr.
- M3, the opaque value: OpaqueStr, a str whose repr, str and format all render the marker, with an
  explicit reveal() as the only sanctioned way out. For a value that is held and sent but must
  never be rendered by accident (a vendor credential), as opposed to M1's value that is stored.

WHAT M1 GUARDS: repr() of a bound parameter, which is how SQLAlchemy renders parameter lists.
WHAT M2 GUARDS: a model's repr() and str(), which is what an f-string or %-format of a model in a
log line renders (`log.debug(f'Validating request: {self}')` is one such site). The field is left
out of that text entirely.

WHAT M3 GUARDS: str, repr, format, f-strings with any conversion, str.format, percent-format,
string.Template, print, logging arguments including inside containers, and an exception
constructed with the value as its argument.

WHAT NONE GUARDS, so nobody assumes it does:
- Postgres's own DETAIL text on a constraint violation ("Key (...)=(...) already exists"). It is
  written by the server, before SQLAlchemy sees it (tj-vhboky.41's known residual).
- str(), format() or an f-string of the value itself, RedactedStr only: only repr() is
  overridden, so f'{value}' renders the plain value.
- model_dump() and model_dump_json(), UNCHANGED on purpose. The open GET returns owner and
  request bodies carry it, so the value must still serialise. Logging a dump logs the value.
- Validation errors. pydantic's ValidationError renders the rejected input, owner included.
- Anything that reads the attribute directly, e.g. f'{model.owner}'.
- OpaqueStr keeps its value through encode, json.dumps, pickle at protocol 2 and up and copy; concatenation, slicing,
  join and every str method return a plain str; a pydantic str field strips the subclass; httpx
  encodes the raw value.

HAZARD: str(x) used as a conversion, RedactedStr(x) or any other str-subclass constructor, and
pickle at protocol 0 or 1 yield the marker, not the value; SensitiveString refuses an OpaqueStr
with a TypeError. Unwrap with reveal() only where the value must be sent.

NOT SecretStr: it changes the type, forces get_secret_value() at every use and serialises as
asterisks, which would break the GET and the bodies above.
"""

from typing import Annotated

from pydantic import Field


REDACTED = '<redacted>'
"""The marker rendered in place of a sensitive value."""


class RedactedStr(str):
    """A str whose repr() is the REDACTED marker; everything else is the plain str's.

    Only __repr__ is overridden. __str__, __format__, equality, hashing, len() and the character
    data are str's own, so the driver encodes and stores the real value and comparisons behave
    exactly as for the plain value. Never override __str__ here: the driver and every f-string
    would then carry the marker instead of the value.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return REDACTED


class OpaqueStr(str):
    """A str whose repr(), str() and format() all render the REDACTED marker.

    Guards what M3 guards and nothing else (see the module docstring): the value stays in
    equality, hash, len, encode, json.dumps, copy, deepcopy and pickle at protocol 2 and up (the
    default, and what copy uses), and every str method returns a plain str that carries no
    protection. reveal() is the only sanctioned way to the value.

    Pickle at protocol 0 or 1 goes through copyreg, which takes str() of it: the round trip
    yields an OpaqueStr holding the marker. A documented hazard; it fails closed.

    Never pass one to RedactedStr or any other str-subclass constructor (it takes str() and
    holds the marker), nor to a pydantic str field (it strips the subclass and the value
    travels on unprotected). SensitiveString refuses one with a TypeError.
    """

    __slots__ = ()

    def __new__(cls, value: str) -> 'OpaqueStr':
        if isinstance(value, OpaqueStr):
            return value
        if not isinstance(value, str):
            raise TypeError(f'OpaqueStr needs a str, got {type(value).__name__}')
        return super().__new__(cls, str.__str__(value))

    def __repr__(self) -> str:
        return REDACTED

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, format_spec: str) -> str:
        return format(REDACTED, format_spec)

    def reveal(self) -> str:
        """Return the real value as an exact str.

        The result carries no protection: call it only where the value is sent.
        """
        return str.__str__(self)


# TWO aliases, not one plus `| None`. Verified by the architect on pydantic 2.13.5 (tj-vhboky.41,
# 03:23 UTC): `SensitiveStr | None` emits UnsupportedFieldAttributeWarning and the field STAYS in
# the repr -- the Field(repr=False) inside a union member is silently dropped. The optional form
# has to carry the marker at the top level of its own Annotated.
SensitiveStr = Annotated[str, Field(repr=False)]
"""A required str field whose value is left out of the model's repr() and str()."""

OptionalSensitiveStr = Annotated[str | None, Field(repr=False)]
"""An optional str field whose value is left out of the model's repr() and str(). Never spell
this as `SensitiveStr | None`: that form silently keeps the value in the repr."""
