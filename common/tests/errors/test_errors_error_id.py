"""The one error-id function (TE-3b tj-3mk3u5.37.13; ADR tj-fa1rpu D8 as its 16:22 UTC 2026-10-02 addendum reads it).

D8 ties an answer to the cause chain in the log by an id carried on both. new_error_id is the only spelling either
transport mints it in: the text of a uuid4, the format TE-3's 500 path used before it. Fresh on every call, because
an id that two failures share correlates nothing. That it stays standard-library only is
test_errors_stdlib_only.py's job, which already covers every module under common/errors.
"""

import uuid

import pytest

from common.errors.vocabulary import new_error_id


pytestmark = pytest.mark.common

# Enough calls that a function caching, counting from a fixed seed or truncating its value would repeat itself.
CALLS = 1000


def test_an_error_id_is_the_canonical_text_of_a_uuid4():
    """A version-4, RFC 4122 uuid, written the way str(uuid) writes it: lower-case hex, hyphenated, 36 characters."""
    error_id = new_error_id()
    assert isinstance(error_id, str)
    parsed = uuid.UUID(error_id)
    assert parsed.version == 4
    assert parsed.variant == uuid.RFC_4122
    assert str(parsed) == error_id


def test_every_call_returns_a_fresh_error_id():
    """No two calls ever return the same id."""
    assert len({new_error_id() for _ in range(CALLS)}) == CALLS
