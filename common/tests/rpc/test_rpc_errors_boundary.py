"""The servicer boundary: every escaping exception becomes a reply (ADR tj-fa1rpu D1(b), D5, D6, D8).

ErrorBoundaryInterceptor is an interceptor and not a per-method decorator because D1(b) calls the
obligation absolute. The claim that buys is that a method NOBODY DECORATED is still covered, so these tests
drive four methods built by hand -- one per cardinality -- on a generic service with no .proto and no
ceremony of any kind (common/tests/rpc/error_boundary_harness.py). If the boundary ever stops covering a
cardinality, or stops covering a method that did nothing to opt in, this file goes red.

WHAT A BUG LOOKS LIKE ON THE WIRE (D5, D8): INTERNAL, a message carrying an error_id and nothing else, and
NO rich status -- an ErrorInfo asserts a (domain, reason) from our closed vocabulary, and a bug has none.
The traceback goes to the log under that id. The last test here shows what that is holding back: with no
interceptor installed, gRPC's own default puts the exception's text on the wire.
"""

import logging
import re

import grpc
import pytest

from common.errors.vocabulary import ERROR_DOMAIN, ExogenousError, InvalidRequestError, Reason

from .errors_harness import (
    CODELESS,
    METHODS,
    RENDERABLE,
    SERVED,
    STREAM_STREAM,
    UNARY_STREAM,
    Replied,
    boundary_server,
    error_for,
    invoke,
    nothing,
    raising,
    writing_stream_server,
)


pytestmark = pytest.mark.common

# The two response-streaming methods: only these can abort PART WAY through a reply.
STREAMING_METHODS = (UNARY_STREAM, STREAM_STREAM)

# A uuid4's text, which is what common/errors' new_error_id mints.
UUID4 = re.compile(r'\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z')

# The fixed one-line message a bug's INTERNAL status carries. Pinned HERE rather than imported from
# common/rpc/errors.py, because it is the one machine-readable thing a bug's reply has: the client half
# recovers the id by matching it, so a change to the template is a wire change and must be a red.
INTERNAL_MESSAGE = re.compile(r'\Ainternal error; error_id ([0-9a-f-]{36})\Z')


def _bug_id(replied: Replied) -> str:
    error = replied.failed
    assert error.code() is grpc.StatusCode.INTERNAL, f'a bug must answer INTERNAL, not {error.code()}'
    matched = INTERNAL_MESSAGE.fullmatch(error.details() or '')
    assert matched is not None, f"a bug's message must be the fixed template, not {error.details()!r}"
    assert UUID4.fullmatch(matched.group(1)), 'the id in the message must be a minted error_id'
    return matched.group(1)


# -----------------------------------------------------------------------------------------------------
# TOTALITY: EVERY CARDINALITY, AND A METHOD THAT DID NOTHING TO OPT IN
#
# The four methods below are plain callables handed to grpc.method_handlers_generic_handler. None of them
# is decorated, registered, or named anywhere in common/rpc. Covering them is the interceptor's whole
# argument over a decorator, and parametrising over METHODS is what keeps a fifth cardinality from being
# added to _HANDLER_FACTORIES untested.


@pytest.mark.asyncio
@pytest.mark.parametrize('method', METHODS)
async def test_the_boundary_covers_a_method_of_every_cardinality_that_did_nothing_to_opt_in(method: str):
    """D1(b): an undecorated method of any cardinality still answers INTERNAL rather than leaking a bug."""
    async with boundary_server(raising(ZeroDivisionError('division by zero'))) as channel:
        replied = await invoke(channel, method)
    assert replied.messages == []
    _bug_id(replied)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', METHODS)
async def test_a_method_that_does_not_fail_is_untouched_by_the_boundary(method: str):
    """The boundary wraps every handler, so the happy path has to keep working through the wrapper."""
    async with boundary_server(nothing) as channel:
        replied = await invoke(channel, method)
    assert replied.error is None, f'the boundary broke a successful call: {replied.error}'
    assert replied.messages == [SERVED]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'exc',
    [ZeroDivisionError('x'), KeyError('absent'), RuntimeError('boom'), TypeError('wrong'), ValueError('bad')],
    ids=lambda exc: type(exc).__name__,
)
async def test_any_exception_at_all_becomes_internal_with_an_error_id(exc: Exception):
    """D5: the boundary is total over Exception, not a list of the ones somebody thought of."""
    async with boundary_server(raising(exc)) as channel:
        replied = await invoke(channel, METHODS[0])
    _bug_id(replied)


@pytest.mark.asyncio
async def test_two_bugs_get_two_different_error_ids():
    """An id that identifies one log line, not a constant that identifies the template."""
    async with boundary_server(raising(RuntimeError('boom'))) as channel:
        first = _bug_id(await invoke(channel, METHODS[0]))
        second = _bug_id(await invoke(channel, METHODS[0]))
    assert first != second


# -----------------------------------------------------------------------------------------------------
# NOTHING OF THE BUG REACHES THE CALLER BUT THE ID (D8)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', METHODS)
async def test_a_bug_puts_no_exception_text_and_no_rich_status_on_the_wire(method: str):
    """D8: not the type, not the message, not the traceback, and no ErrorInfo -- a bug has no reason."""
    secret = 'postgresql://joe:hunter2@db.internal:5432/trader'
    async with boundary_server(raising(RuntimeError(f'connect failed: {secret}'))) as channel:
        replied = await invoke(channel, method)
    error = replied.failed
    wire = repr((error.details(), list(error.trailing_metadata() or ()), list(error.initial_metadata() or ())))
    for forbidden in (secret, 'hunter2', 'RuntimeError', 'connect failed', 'Traceback', 'error_boundary_harness'):
        assert forbidden not in wire, f'{forbidden!r} reached the caller: {wire}'
    assert ERROR_DOMAIN not in wire, 'a bug carries no ErrorInfo, so the domain cannot appear'
    assert not any(key == 'grpc-status-details-bin' for key, _ in error.trailing_metadata() or ())


@pytest.mark.asyncio
async def test_the_traceback_is_logged_under_the_same_id_the_caller_was_given(caplog: pytest.LogCaptureFixture):
    """The two halves of D8's correlation id: the caller quotes it, and the operator searches the log for it."""
    secret = 'hunter2-not-on-the-wire'
    cause = ConnectionResetError('vendor socket died')
    bug = RuntimeError(f'while reading {secret}')
    bug.__cause__ = cause
    with caplog.at_level(logging.DEBUG, logger='common.rpc.errors'):
        async with boundary_server(raising(bug)) as channel:
            replied = await invoke(channel, METHODS[0])
    error_id = _bug_id(replied)
    records = [record for record in caplog.records if error_id in record.getMessage()]
    assert len(records) == 1, f'a bug writes exactly one line naming its id, not {len(records)}'
    assert records[0].levelno == logging.ERROR, 'a bug is always an operational failure'
    assert records[0].exc_info is not None, 'the traceback belongs in the log, which is the point of the id'
    logged = caplog.text
    assert secret in logged and 'ConnectionResetError' in logged, 'the chain and its text must reach the log'


# -----------------------------------------------------------------------------------------------------
# MID-STREAM (bead item 2: 'an exception mid-stream ends the stream with that status')


@pytest.mark.asyncio
@pytest.mark.parametrize('method', STREAMING_METHODS)
async def test_a_bug_part_way_through_a_stream_ends_it_with_internal_after_what_was_already_sent(method: str):
    """The messages already on the wire still arrive; the stream then ends with the status, not with EOF."""
    async with boundary_server(raising(RuntimeError('boom')), before=3) as channel:
        replied = await invoke(channel, method)
    assert replied.messages == [b'0', b'1', b'2'], 'messages sent before the failure must still arrive'
    _bug_id(replied)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', STREAMING_METHODS)
async def test_a_typed_error_part_way_through_a_stream_ends_it_with_its_own_status(method: str):
    """A stream that fails for a REASON ends with that reason, not with a bug and not with a clean EOF."""
    failure = InvalidRequestError(Reason.NOT_FOUND, 'no such dataset')
    async with boundary_server(raising(failure), before=2) as channel:
        replied = await invoke(channel, method)
    assert replied.messages == [b'0', b'1']
    assert replied.failed.code() is grpc.StatusCode.NOT_FOUND


# -----------------------------------------------------------------------------------------------------
# THE OTHER LEGAL RESPONSE-STREAMING SHAPE
#
# grpc.aio accepts two: an async generator function, and a coroutine that writes through context.write().
# _guarded() branches on inspect.isasyncgenfunction and must wrap the behaviour as whichever it is -- a
# generator wrapped in a coroutine would be handed to gRPC un-awaited, as the single response. Every other
# streaming test above drives the first shape, so these two drive the branch that would otherwise be the
# untested one.


@pytest.mark.asyncio
async def test_a_streaming_coroutine_that_writes_through_the_context_is_covered_too():
    async with writing_stream_server(raising(RuntimeError('boom')), before=2) as channel:
        replied = await invoke(channel, UNARY_STREAM)
    assert replied.messages == [b'0', b'1'], 'what it wrote before failing still arrives'
    _bug_id(replied)


@pytest.mark.asyncio
async def test_a_streaming_coroutine_that_succeeds_still_delivers_everything_it_wrote():
    async with writing_stream_server(nothing, before=2) as channel:
        replied = await invoke(channel, UNARY_STREAM)
    assert replied.error is None, f'the boundary broke the writing shape: {replied.error}'
    assert replied.messages == [b'0', b'1', SERVED]


# -----------------------------------------------------------------------------------------------------
# NEVER SILENT, AND NEVER UNAVAILABLE (ADR tj-8konfu D6.4)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize(
    'exc',
    [
        RuntimeError('a bug'),
        InvalidRequestError(Reason.NOT_FOUND, 'typed'),
        ExogenousError(Reason.VENDOR_AUTH, 'typed'),
    ],
    ids=['bug', 'refused', 'not-ready'],
)
async def test_the_boundary_is_never_silent_and_never_answers_unavailable(method: str, exc: Exception):
    """Two absolutes together: a failed call always carries a status, and that status is never UNAVAILABLE.

    UNAVAILABLE is reserved for the transport saying the peer is down. A server that sends it deliberately
    makes PEER_UNAVAILABLE a lie on the other side of the hop.
    """
    async with boundary_server(raising(exc)) as channel:
        replied = await invoke(channel, method)
    error = replied.failed
    assert error.code() is not grpc.StatusCode.OK, 'a failed call is never reported as success'
    assert error.code() is not grpc.StatusCode.UNAVAILABLE, 'the server never deliberately returns UNAVAILABLE'
    assert error.code() is not grpc.StatusCode.UNKNOWN, "UNKNOWN is gRPC's default for a leak, never an answer"


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', RENDERABLE)
async def test_no_reason_reaches_the_wire_as_unavailable_through_the_boundary(reason: Reason):
    """Driven off the REASONS table, so a row added with grpc_code='UNAVAILABLE' is caught on the wire too.

    TE-1's ReasonSpec already refuses such a row at import. This is the second line: the check that holds
    even if that one is relaxed, made at the only place that matters, which is what the peer received.
    """
    async with boundary_server(raising(error_for(reason, 'rendered for the wire'))) as channel:
        replied = await invoke(channel, METHODS[0])
    assert replied.failed.code() is not grpc.StatusCode.UNAVAILABLE


# -----------------------------------------------------------------------------------------------------
# A REASON THAT NEVER RENDERS, RAISED OUT OF A SERVICER
#
# render() refuses a grpc_code-None reason, because asking for its status is a programming error. Raising
# one out of a servicer is the SAME programming error -- FEED_NOT_AVAILABLE belongs in the FetchDataset ack
# (tj-3mk3u5.22 Q5) -- and the boundary treats it as the bug it is rather than letting render()'s
# ValueError escape. That is what keeps the boundary total: it answers, always.


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', CODELESS)
async def test_a_reason_that_never_renders_raised_out_of_a_servicer_is_a_bug_not_an_escape(reason: Reason):
    """All seven of them, driven off the table: INTERNAL with an id, never a ValueError out of render()."""
    async with boundary_server(raising(error_for(reason, 'raised where it does not belong'))) as channel:
        replied = await invoke(channel, METHODS[0])
    _bug_id(replied)
    assert ERROR_DOMAIN not in repr(list(replied.failed.trailing_metadata() or ())), 'a bug carries no ErrorInfo'


# -----------------------------------------------------------------------------------------------------
# WHAT THE BOUNDARY IS HOLDING BACK
#
# This is the control. It installs no interceptor and shows gRPC's own default for an exception escaping a
# servicer: UNKNOWN, with the exception's text on the wire -- precisely the leak D8 forbids. It is also the
# measure of follow-up fu-2: a boundary nobody installs leaves the server in exactly this state.


@pytest.mark.asyncio
async def test_without_the_boundary_grpc_leaks_the_exception_text_as_unknown():
    """The default this interceptor exists to replace. If grpc.aio ever stops leaking, this is where to look."""
    secret = 'leaked-by-the-default'
    async with boundary_server(raising(RuntimeError(secret)), with_boundary=False) as channel:
        replied = await invoke(channel, METHODS[0])
    error = replied.failed
    assert error.code() is grpc.StatusCode.UNKNOWN
    assert secret in (error.details() or ''), 'the control is only meaningful while the default really leaks'
