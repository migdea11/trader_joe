"""Redaction of sensitive values where they are RENDERED, not where they are stored or sent.

Design: tj-vhboky.41 ADDENDUM 1, D3. The user ruled against hiding detail wholesale (no engine
hide_parameters): requests, parameters and every non-sensitive value stay in the logs, and only
the fields on the sensitive list are kept out of the text that gets rendered. The list is the
user's answer on tj-vhboky.45: the dataset entry's `owner`, and nothing else for now. A field
joins the list by being declared with one of the aliases below.

This module holds layer M2, the pydantic half. Layer M1 -- the str subclass that redacts SQL
bound parameters in exception text, engine echo and uvicorn's traceback -- is NOT here yet: it
waits on a spike against a real Postgres (tj-w6bpjm step 0) showing asyncpg stores such a
subclass as its plain value.

WHAT M2 GUARDS: a model's repr() and str(), which is what an f-string or %-format of a model in a
log line renders (`log.debug(f'Validating request: {self}')` is one such site). The field is left
out of that text entirely.

WHAT M2 DOES NOT GUARD, so nobody assumes it does:
- model_dump() and model_dump_json() are UNCHANGED, on purpose. The open GET returns owner and
  request bodies carry it, so the value must still serialise. Logging a dump logs the value.
- Validation errors. pydantic's ValidationError renders the rejected input, owner included.
- Anything that reads the attribute directly, e.g. f'{model.owner}'.
- SQL parameters and Postgres's own DETAIL text (see tj-vhboky.41's known residual).

NOT SecretStr: it changes the type, forces get_secret_value() at every use and serialises as
asterisks, which would break the GET and the bodies above.
"""

from typing import Annotated

from pydantic import Field


# TWO aliases, not one plus `| None`. Verified by the architect on pydantic 2.13.5 (tj-vhboky.41,
# 03:23 UTC): `SensitiveStr | None` emits UnsupportedFieldAttributeWarning and the field STAYS in
# the repr -- the Field(repr=False) inside a union member is silently dropped. The optional form
# has to carry the marker at the top level of its own Annotated.
SensitiveStr = Annotated[str, Field(repr=False)]
"""A required str field whose value is left out of the model's repr() and str()."""

OptionalSensitiveStr = Annotated[str | None, Field(repr=False)]
"""An optional str field whose value is left out of the model's repr() and str(). Never spell
this as `SensitiveStr | None`: that form silently keeps the value in the repr."""
