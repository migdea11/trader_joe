"""RedactedStr and REDACTED: layer M1's str subclass (tj-w6bpjm; design tj-vhboky.41 Addendum 1, D3).

The contract is asymmetric on purpose: repr() -- how SQLAlchemy renders a parameter list, and how
engine echo and uvicorn's traceback show it -- is the marker, while EVERY other way the value is
read is the plain str's. The second half matters as much as the first: the driver encodes the
value through str's own machinery, and a subclass that changed str(), equality or hashing would
store, match or look up the wrong thing. Each pin below therefore compares against the plain value
it was built from.

The pydantic half (SensitiveStr, OptionalSensitiveStr) is pinned in
schemas/tests/test_sensitive_fields.py, not here.
"""

import operator

import pytest

from common.sensitive import REDACTED, RedactedStr


pytestmark = pytest.mark.common

PLAIN = 'owner-7f3a-not-the-marker'


@pytest.fixture
def redacted() -> RedactedStr:
    value = RedactedStr(PLAIN)
    # The value must be unmistakable for the marker, or every "absent" assertion below is vacuous.
    assert PLAIN not in REDACTED and REDACTED not in PLAIN
    return value


def test_the_marker_is_a_non_empty_str() -> None:
    assert type(REDACTED) is str
    assert REDACTED.strip()


def test_it_is_a_str(redacted: RedactedStr) -> None:
    assert isinstance(redacted, str)


def test_repr_is_the_marker(redacted: RedactedStr) -> None:
    assert repr(redacted) == REDACTED
    assert PLAIN not in repr(redacted)


def test_repr_of_a_parameter_tuple_shows_the_marker_and_the_neighbour(redacted: RedactedStr) -> None:
    """A tuple's repr is its items' reprs: the shape of SQLAlchemy's "[parameters: (...)]" text."""
    rendered = repr((redacted, 'visible-neighbour', 1))
    assert rendered == f"({REDACTED}, 'visible-neighbour', 1)"
    assert PLAIN not in rendered


def test_repr_format_conversions_show_the_marker(redacted: RedactedStr) -> None:
    assert f'{redacted!r}' == REDACTED
    # %-style is what logging applies to its arguments: log.debug('%r', value).
    assert '%r' % (redacted,) == REDACTED  # noqa: UP031


def test_str_is_the_plain_value(redacted: RedactedStr) -> None:
    assert str(redacted) == PLAIN
    # An exact str, not the subclass: SensitiveString's read path relies on this to unwrap.
    assert type(str(redacted)) is str


def test_f_string_format_and_percent_s_give_the_plain_value(redacted: RedactedStr) -> None:
    assert f'{redacted}' == PLAIN
    assert format(redacted) == PLAIN
    assert str.format('{}', redacted) == PLAIN
    # %-style is what logging applies to its arguments: log.debug('%s', value).
    assert '%s' % (redacted,) == PLAIN  # noqa: UP031


def test_equality_with_the_plain_value(redacted: RedactedStr) -> None:
    assert redacted == PLAIN
    assert operator.eq(PLAIN, redacted), 'equality must hold with the plain value on the left too'
    assert operator.ne(redacted, PLAIN) is False
    assert redacted != REDACTED


def test_hash_is_the_plain_values(redacted: RedactedStr) -> None:
    assert hash(redacted) == hash(PLAIN)
    assert {PLAIN: 'found'}[redacted] == 'found'
    assert {redacted: 'found'}[PLAIN] == 'found'


def test_len_and_character_data_are_the_plain_values(redacted: RedactedStr) -> None:
    assert len(redacted) == len(PLAIN)
    assert redacted.encode() == PLAIN.encode()
    assert redacted.encode('utf-8') == PLAIN.encode('utf-8')
    assert list(redacted) == list(PLAIN)
    assert str.__str__(redacted) == PLAIN


def test_non_ascii_character_data_round_trips() -> None:
    plain = 'propriétaire-所有者'
    value = RedactedStr(plain)
    assert value.encode() == plain.encode()
    assert len(value) == len(plain)
    assert repr(value) == REDACTED
