"""The error-id functions (TE-3b tj-3mk3u5.37.13 and TE-2 fu-1 tj-zxqn4r; ADR tj-fa1rpu D8 and its 16:22 addendum).

D8 ties an answer to the cause chain in the log by an id carried on both. new_error_id is the only spelling either
transport mints it in: the text of a uuid4, the format TE-3's 500 path used before it. Fresh on every call, because
an id that two failures share correlates nothing. That it stays standard-library only is
test_errors_stdlib_only.py's job, which already covers every module under common/errors.

THE OTHER TWO THIRDS OF THE MECHANISM MOVED HERE (validator, gating tj-zxqn4r), and until this file they were
reached by no test that named them. own_error_id reads an id back and has_cause_chain decides whether a traceback
is worth logging; both were private copies in common/rpc/errors.py and routers/common/errors.py, and the copies
HAD ALREADY DIVERGED on their fourth line -- one joined a sequence-valued id with ',' and the other with ', ' --
while the gRPC copy's comment claimed they read an id identically. Four suites exercised them, but only
indirectly, through a transport, which is exactly how a divergence in a branch neither suite drove went unseen
for two days. These are the direct cases.
"""

import uuid

import pytest

from common.errors.vocabulary import (
    METADATA_SEQUENCE_SEPARATOR,
    InvalidRequestError,
    Reason,
    has_cause_chain,
    new_error_id,
    own_error_id,
)


pytestmark = pytest.mark.common

# Enough calls that a function caching, counting from a fixed seed or truncating its value would repeat itself.
CALLS = 1000

# Any reason on the InvalidRequestError branch; which one is irrelevant to reading an id off the metadata.
ANY_REASON = Reason.NOT_FOUND


def error_with(error_id) -> InvalidRequestError:
    """An error carrying the given error_id metadata, or none at all when it is None.

    Args:
        error_id: The metadata value to carry, or None to carry no error_id key.

    Returns:
        InvalidRequestError: The error.
    """
    metadata = {} if error_id is None else {'error_id': error_id}
    return InvalidRequestError(ANY_REASON, 'the detail is irrelevant here', metadata=metadata)


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


# ---------------------------------------------------------------------------------------------------------------
# THE SEPARATOR. Part B of the tj-zxqn4r ruling: a sequence-valued error_id becomes one string by joining its
# items with a comma and nothing else.
# ---------------------------------------------------------------------------------------------------------------


def test_the_shared_separator_is_a_comma_and_nothing_else():
    """The literal, pinned once, because every other case here reads the constant rather than restating it.

    WITHOUT THIS CASE THE SEPARATOR IS UNPINNED. The cases below join with METADATA_SEQUENCE_SEPARATOR instead
    of with ',' deliberately -- what they are about is that one spelling reaches both edges, and restating the
    literal in each would make them a set of independent claims about a comma rather than one claim about a
    shared constant. The cost of reading the constant is that every one of them MOVES WITH IT: change the
    constant to ' | ' and they all stay green, because expectation and production are now the same symbol.
    This case is what makes that safe. It is the only place the literal appears, so the ruling's "it is ','"
    has exactly one witness and changing the separator is a deliberate act that reds here.

    WHY ',' AND NOT ', ': the comma join is the one that is WIRE-VISIBLE. common/rpc/errors.py's _wire_value
    writes every sequence-valued ErrorInfo metadata value that way and the TE-2 gate pins it, while the HTTP
    side's join only ever reached a log line. So the wire spelling wins and the HTTP log line moved to match,
    which is what makes the id in data_store's log, the id in ingest's log and the id on the gRPC wire one
    string for one error.
    """
    assert METADATA_SEQUENCE_SEPARATOR == ','


# ---------------------------------------------------------------------------------------------------------------
# own_error_id
# ---------------------------------------------------------------------------------------------------------------

# Every shape TE-1 lets an error_id metadata value take that NAMES NOTHING. All three are accepted by the
# constructor and all three must read as "this error has no id of its own", so the edge mints one.
NAMES_NOTHING = [
    pytest.param(None, id='no-error_id-key-at-all'),
    pytest.param('', id='the-empty-string'),
    pytest.param((), id='an-empty-sequence'),
    pytest.param(('',), id='a-sequence-of-one-empty-string'),
    pytest.param(('', ''), id='a-sequence-of-nothing-but-empty-strings'),
]


@pytest.mark.parametrize('error_id', NAMES_NOTHING)
def test_an_id_that_names_nothing_is_no_id(error_id):
    """None, not '' and not '(,)': the caller's test is `is None`, so anything falsy-but-not-None would break it.

    Both edges ask this function `is None` to decide three things at once -- whether to put an id in the answer,
    which id to name in the log line, and whether to log the cause chain. A return of '' would be falsy but not
    None, so an edge testing `is None` would treat the empty string as a real id, send it, name it, and skip the
    chain. The emptiness is collapsed HERE precisely so no caller has to remember to collapse it.

    Args:
        error_id: A metadata value that names nothing.
    """
    assert own_error_id(error_with(error_id)) is None


def test_a_string_id_is_returned_as_it_stands():
    """The ordinary case: a uuid4 from new_error_id, or a peer's id kept across a hop, comes back untouched."""
    assert own_error_id(error_with('peer-id-0001')) == 'peer-id-0001'


@pytest.mark.parametrize(
    ('parts', 'expected'),
    [
        pytest.param(('a1', 'b2'), 'a1,b2', id='every-part-names-one'),
        pytest.param(('a1', 'b2', 'c3'), 'a1,b2,c3', id='three-parts'),
        # Not every part need name something: one that does makes the whole value the error's own (387f169).
        pytest.param(('', 'b2'), ',b2', id='a-leading-empty-part-is-kept'),
        pytest.param(('a1', ''), 'a1,', id='a-trailing-empty-part-is-kept'),
        pytest.param(('solo',), 'solo', id='one-part-joins-to-itself'),
    ],
)
def test_a_sequence_id_is_joined_with_the_shared_separator(parts: tuple[str, ...], expected: str):
    """The join, including that an empty part inside a naming sequence is KEPT rather than dropped.

    THE EMPTY-PART CASES ARE THE INTERESTING ONES and they are not symmetry. ('', 'b2') names something, so it
    is an id -- the "names nothing" rule above needs EVERY part empty. A reading that filtered the empty parts
    out before joining would produce 'b2' here, which is a DIFFERENT STRING from the one the gRPC hop writes on
    the wire for the same value: _wire_value joins the items as they are. The two edges would then name
    different ids for one error, which is the exact failure this bead exists to remove, re-introduced one layer
    down. So the expectation is spelled out literally rather than recomputed with a join.

    Args:
        parts: The sequence-valued error_id.
        expected: The one string it must become.
    """
    assert own_error_id(error_with(parts)) == expected


def test_the_sequence_join_uses_the_shared_constant():
    """The join is the shared constant, shown by deriving the expectation from it rather than from a literal.

    The case above pins today's output against hand-written strings, which is what catches a filtered or
    reordered join. This one pins the RELATIONSHIP: whatever METADATA_SEQUENCE_SEPARATOR is, that is what
    own_error_id joins with. The two together are what let the gRPC hop's _SEQUENCE_SEPARATOR be an alias of
    this constant and have that mean something.
    """
    assert own_error_id(error_with(('a1', 'b2'))) == METADATA_SEQUENCE_SEPARATOR.join(('a1', 'b2'))


# ---------------------------------------------------------------------------------------------------------------
# has_cause_chain
# ---------------------------------------------------------------------------------------------------------------


def test_a_bare_exception_has_no_chain_to_log():
    """Nothing to show, so an edge logging exc_info would attach a traceback that explains nothing."""
    assert has_cause_chain(InvalidRequestError(ANY_REASON, 'raised on its own')) is False


def test_an_explicit_from_sets_a_chain():
    """`raise ... from e` is the deliberate spelling, and the one write_transaction uses for every conversion."""
    try:
        try:
            raise ValueError('the underlying failure')
        except ValueError as cause:
            raise InvalidRequestError(ANY_REASON, 'converted') from cause
    except InvalidRequestError as error:
        assert has_cause_chain(error) is True


def test_a_raise_inside_an_except_block_sets_a_chain_implicitly():
    """__context__, not __cause__: the chain exists even when nobody wrote `from`.

    This is the half a `__cause__`-only reading would miss, and it is the common one -- a handler that catches
    and raises its own error sets the context without thinking about it. The traceback is just as worth logging.
    """
    try:
        try:
            raise ValueError('the underlying failure')
        except ValueError:
            raise InvalidRequestError(ANY_REASON, 'raised inside the except')  # noqa: B904 -- the point of the case
    except InvalidRequestError as error:
        assert has_cause_chain(error) is True


def test_from_none_suppresses_the_chain():
    """`from None` is a deliberate statement that the context is noise, and it must be obeyed.

    The subtlest of the four: __context__ IS still set by the interpreter, so a reading that checked only
    whether it is None would log a chain the raise site explicitly suppressed. __suppress_context__ is the flag
    that distinguishes them, and this case is the only one that fails if it is dropped from the expression.
    """
    try:
        try:
            raise ValueError('deliberately hidden')
        except ValueError:
            raise InvalidRequestError(ANY_REASON, 'context suppressed') from None
    except InvalidRequestError as error:
        assert error.__context__ is not None, 'the interpreter did not set a context, so this case proves nothing'
        assert has_cause_chain(error) is False
