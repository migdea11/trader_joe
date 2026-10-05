"""GrpcServerHost installs the error boundary itself: the PRODUCTION host, not a hand-built server.

ADR tj-fa1rpu D1(b) calls the servicer's obligation absolute -- every escaping exception becomes a reply --
and D8 forbids the exception text on the wire. common/tests/rpc/test_rpc_errors_boundary.py proves
ErrorBoundaryInterceptor keeps both promises when something installs it. This file proves something does:
every server GrpcServerHost builds carries it, no caller opts in, and no caller can opt out.

WHY A SECOND FILE RATHER THAN MORE CASES IN THE TE-2 GATE. The TE-2 gate builds its own grpc.aio.server and
passes the interceptor in; it would stay green with common/rpc/server.py reverted to
grpc.aio.server(options=server_options()), which is exactly the state that bead tj-19r2z5 exists to repair.
Every server below is a real GrpcServerHost, started on 127.0.0.1:0 and stopped in a finally, so these are
the tests that go red if the installation is ever removed, reordered behind a caller's interceptor, or made
replaceable by one.

WHAT EACH SECTION PINS:

    THE LEAK          a servicer that did nothing to opt in answers INTERNAL with an error_id, never gRPC's
                      own UNKNOWN with the exception text. A canary string in the exception is absent from
                      the details, the trailers and the initial metadata, and present in the log under the
                      id the caller was given.
    THE TYPED PATH    installing the boundary did not change what a raised TraderJoeError does: it still
                      arrives as its reason's own status with its ErrorInfo.
    HEALTH            Check and Watch are dispatched through the same chain. Health's own deliberate abort
                      for a service name it does not know still surfaces as NOT_FOUND, not as an INTERNAL
                      bug -- that abort raises grpc.aio.AbortError, which the boundary re-raises untouched.
    NO OFF SWITCH     a caller's own interceptors run AND the boundary is still there, still FIRST. First
                      means outermost, so the boundary guards the handler a caller's interceptor returned.

A plain grpc.aio.insecure_channel is used throughout rather than common.rpc.channel.create_channel: what is
under test is the server's interceptor chain, and the production channel's retry and wait policy would put
a second variable between the raise and the assertion. create_channel against this host is pinned in
common/tests/rpc/test_rpc_server.py.
"""

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

from common.errors.vocabulary import ERROR_DOMAIN, REASONS, Reason
from common.rpc.errors import from_rpc_error
from common.rpc.server import BindAddress, GrpcServerHost, ServiceRegistration

from .errors_harness import (
    GUARD_S,
    LOOPBACK,
    SERVED,
    SERVICE,
    UNARY_STREAM,
    UNARY_UNARY,
    Action,
    Replied,
    error_for,
    invoke,
    nothing,
    path,
    raising,
)


pytestmark = pytest.mark.common

# What grpc.aio hands an interceptor to reach the next one, spelled as common/rpc/errors.py spells it.
_Continuation = Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler | None]]

SERVING = health_pb2.HealthCheckResponse.SERVING
NOT_SERVING = health_pb2.HealthCheckResponse.NOT_SERVING

# The two cardinalities this file drives through the host. All four are driven against the interceptor in
# the TE-2 gate; here one unary and one streaming method are enough to show the HOST installs what that
# gate proved, without starting four servers per case.
HOSTED_METHODS = (UNARY_UNARY, UNARY_STREAM)

# The fixed one-line message a bug's INTERNAL status carries, spelled out here for the same reason the TE-2
# gate spells it out: it is the only machine-readable thing a bug's reply has, so a change to it is a wire
# change and must be a red rather than an import that quietly follows along.
INTERNAL_MESSAGE = re.compile(r'\Ainternal error; error_id ([0-9a-f-]{36})\Z')

# What a bug must never put on the wire. Not a realistic secret -- a string that could only have come from
# the exception, so its presence anywhere in the reply is proof the host served a servicer unguarded.
CANARY = 'ACCOUNT-12345-SECRET-CANARY'

# Three reasons from three different rows of the REASONS table: a refused request, a refused lookup, and a
# not-ready condition that carries a reset_at and therefore a RetryInfo. The whole table is swept against
# render() and the interceptor in the TE-2 gate; these three are here to show the typed path still reaches
# the wire through the production host, which is a different claim.
TYPED_REASONS = (Reason.INVALID_REQUEST, Reason.NOT_FOUND, Reason.RATE_BUDGET)

# How long a host under test lets in-flight calls drain. Short, because the Watch test stops a server while
# a call is open and every other test wants teardown to be quick.
STOP_GRACE_S = 0.2


def _identity(payload: bytes) -> bytes:
    return payload


def _hosted_service(action: Action) -> ServiceRegistration:
    # A ServiceRegistration is all GrpcServerHost knows about a servicer: a name for the health service and
    # a callable that attaches it. Building the handlers by hand, with identity serialisers over bytes and
    # no .proto anywhere, is the same device the TE-2 harness uses, and for the same reason -- the boundary's
    # claim is about a method nobody wrote it for. The method names are the harness's, so invoke() drives
    # these handlers unchanged.

    async def unary_unary(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
        await action(context)
        return SERVED

    async def unary_stream(request: bytes, context: grpc.aio.ServicerContext) -> AsyncIterator[bytes]:
        await action(context)
        yield SERVED

    handlers: Mapping[str, grpc.RpcMethodHandler] = {
        UNARY_UNARY: grpc.unary_unary_rpc_method_handler(
            unary_unary, request_deserializer=_identity, response_serializer=_identity
        ),
        UNARY_STREAM: grpc.unary_stream_rpc_method_handler(
            unary_stream, request_deserializer=_identity, response_serializer=_identity
        ),
    }

    def add_to_server(server: grpc.aio.Server) -> None:
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(SERVICE, dict(handlers)),))

    return ServiceRegistration(name=SERVICE, add_to_server=add_to_server)


@contextlib.asynccontextmanager
async def _hosted(
    action: Action = nothing, *, interceptors: Sequence[grpc.aio.ServerInterceptor] = ()
) -> AsyncIterator[tuple[GrpcServerHost, grpc.aio.Channel]]:
    """Run a REAL GrpcServerHost on an ephemeral loopback port and yield it with an open channel.

    Args:
        action: What the hosted method does once it has consumed its request.
        interceptors: What the caller supplies IN ADDITION to the boundary. Nothing here ever asks for the
            boundary: that is the whole point -- the host installs it.

    Yields:
        tuple[GrpcServerHost, grpc.aio.Channel]: The running host and a channel to it.
    """
    host = GrpcServerHost(
        BindAddress(LOOPBACK, 0), [_hosted_service(action)], stop_grace_s=STOP_GRACE_S, interceptors=interceptors
    )
    await host.start()
    channel = grpc.aio.insecure_channel(f'{LOOPBACK}:{host.port}')
    try:
        yield host, channel
    finally:
        # Both, always: a grpc.aio server still running when the event loop closes hangs the interpreter at
        # exit instead of failing one test (T1 finding N3).
        await channel.close()
        await host.stop()


def _bug_id(replied: Replied) -> str:
    # The error_id out of a bug's reply, asserting the reply is a bug's reply at all. The explicit UNKNOWN
    # message is the point of this file: UNKNOWN is what an uninstalled boundary answers.
    error = replied.failed
    code = error.code()
    assert code is not grpc.StatusCode.UNKNOWN, (
        'UNKNOWN is the gRPC default for an exception escaping a servicer, so the host served this servicer '
        f'with no error boundary installed; details were {error.details()!r}'
    )
    assert code is grpc.StatusCode.INTERNAL, f'a bug must answer INTERNAL, not {code}'
    matched = INTERNAL_MESSAGE.fullmatch(error.details() or '')
    assert matched is not None, f"a bug's message must be the fixed template, not {error.details()!r}"
    return matched.group(1)


async def _check(stub: health_pb2_grpc.HealthStub, service: str) -> int:
    response = await asyncio.wait_for(stub.Check(health_pb2.HealthCheckRequest(service=service), timeout=5), GUARD_S)
    return response.status


# -----------------------------------------------------------------------------------------------------
# THE TEST THAT WOULD HAVE BEEN RED
#
# Before 77ae383, common/rpc/server.py built grpc.aio.server(options=server_options()) with no interceptors
# and no caller passed any, so every exception escaping a servicer answered UNKNOWN with the exception text
# in the details. These two tests are that defect, measured against the production host.


@pytest.mark.asyncio
@pytest.mark.parametrize('method', HOSTED_METHODS)
async def test_a_servicer_the_host_serves_answers_internal_rather_than_grpcs_unknown(method: str):
    """D1(b) and D5: an undecorated servicer attached to a plain GrpcServerHost is guarded on the day it is attached.

    Nothing in _hosted_service() mentions the boundary, imports common/rpc/errors.py or opts in in any way.
    That is the claim: the one object that builds every server in this repository carries the obligation.
    """
    async with _hosted(raising(RuntimeError(f'boom: {CANARY}'))) as (_, channel):
        replied = await invoke(channel, method)
    assert replied.messages == [], 'a method that raised before yielding must send nothing'
    _bug_id(replied)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', HOSTED_METHODS)
async def test_nothing_of_the_exception_reaches_the_caller_through_the_host(method: str):
    """D8: not the canary, not the type, not the traceback, and no ErrorInfo -- a bug has no reason.

    Details AND trailers AND initial metadata, because a leak that moved from the message into a trailer
    would still be the leak, and because the rich status rides in a trailer.
    """
    async with _hosted(raising(RuntimeError(f'boom: {CANARY}'))) as (_, channel):
        replied = await invoke(channel, method)
    error = replied.failed
    wire = repr((error.details(), list(error.trailing_metadata() or ()), list(error.initial_metadata() or ())))
    for forbidden in (CANARY, 'RuntimeError', 'boom', 'Traceback', ERROR_DOMAIN):
        assert forbidden not in wire, f'{forbidden!r} reached the caller: {wire}'
    assert not any(key == 'grpc-status-details-bin' for key, _ in error.trailing_metadata() or ()), (
        'a bug carries no rich status: an ErrorInfo would assert a reason from the closed vocabulary'
    )


@pytest.mark.asyncio
async def test_the_canary_the_wire_never_saw_is_in_the_log_under_the_id_the_caller_got(
    caplog: pytest.LogCaptureFixture,
):
    """The two halves of D8's correlation id, through the production host: the caller quotes it, the operator searches it."""
    with caplog.at_level(logging.DEBUG, logger='common.rpc.errors'):
        async with _hosted(raising(RuntimeError(f'boom: {CANARY}'))) as (_, channel):
            replied = await invoke(channel, UNARY_UNARY)
    error_id = _bug_id(replied)
    records = [record for record in caplog.records if error_id in record.getMessage()]
    assert len(records) == 1, f'a bug writes exactly one line naming its id, not {len(records)}'
    assert records[0].levelno == logging.ERROR, 'a bug the host hid from the caller is an operational failure'
    assert records[0].exc_info is not None, 'the traceback belongs in the log, which is what the id is for'
    assert CANARY in caplog.text, 'the text withheld from the caller must be somewhere, and the log is where'


@pytest.mark.asyncio
@pytest.mark.parametrize('method', HOSTED_METHODS)
async def test_a_method_that_succeeds_is_untouched_by_the_installed_boundary(method: str):
    """The host wraps every handler it serves, so the happy path has to keep working through the wrapper."""
    async with _hosted() as (_, channel):
        replied = await invoke(channel, method)
    assert replied.error is None, f'the installed boundary broke a successful call: {replied.error}'
    assert replied.messages == [SERVED]


# -----------------------------------------------------------------------------------------------------
# THE TYPED PATH IS UNCHANGED
#
# Installing the boundary must not turn a typed error into a bug. The reasons below come from three
# different rows of the one table; the table itself is swept in the TE-2 gate.


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', TYPED_REASONS, ids=lambda reason: reason.value)
async def test_a_typed_error_raised_under_the_host_arrives_as_its_own_status(reason: Reason):
    """A TraderJoeError keeps its reason's code and its ErrorInfo: the host installed a boundary, not a muzzle.

    Read back with from_rpc_error, which is what the peer actually does (ADR tj-8konfu D6.4: clients branch
    on the reason, not on the status).
    """
    spec = REASONS[reason]
    assert spec.grpc_code is not None, f'{reason} no longer renders as a status; pick another row for this case'
    async with _hosted(raising(error_for(reason, 'the request did not succeed', with_reset_at=True))) as (_, channel):
        replied = await invoke(channel, UNARY_UNARY)
    error = replied.failed
    assert error.code() is grpc.StatusCode[spec.grpc_code], f'{reason} must keep its own code through the host'
    assert error.details() == 'the request did not succeed'
    recovered = from_rpc_error(error)
    assert recovered.reason is reason, 'the ErrorInfo must survive the hop, or the peer cannot branch on it'
    assert recovered.metadata.get('error_id'), 'every typed status carries an error_id (tj-fa1rpu Tier 4 addendum)'


# -----------------------------------------------------------------------------------------------------
# HEALTH GOES THROUGH THE BOUNDARY TOO, AND THAT COSTS HEALTH NOTHING
#
# The health servicer is attached to the same server, so the chain wraps Check and Watch like anything else.
# Two things could have broken and did not: health's own deliberate abort, which must stay NOT_FOUND rather
# than becoming an INTERNAL bug, and Watch, which is an async generator rather than a coroutine.


@pytest.mark.asyncio
async def test_health_check_answers_through_the_boundary_and_its_own_not_found_stays_not_found(
    caplog: pytest.LogCaptureFixture,
):
    """The abort health raises on purpose is grpc.aio.AbortError, which the boundary re-raises untouched.

    If the boundary ever treated it as an escaping exception, an unknown service name would answer INTERNAL
    and every probe asking about a service that has not registered yet would read as a server bug.
    """
    with caplog.at_level(logging.ERROR, logger='common.rpc.errors'):
        async with _hosted() as (_, channel):
            stub = health_pb2_grpc.HealthStub(channel)
            assert await _check(stub, '') == SERVING, 'the overall status must answer through the boundary'
            assert await _check(stub, SERVICE) == SERVING, 'an attached service must answer through the boundary'
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await _check(stub, 'trader_joe.proto.absent.v1.AbsentService')
    assert raised.value.code() is grpc.StatusCode.NOT_FOUND, (
        f"health's own abort must stay NOT_FOUND, not become {raised.value.code()}"
    )
    assert 'internal error; error_id' not in (raised.value.details() or '')
    assert caplog.records == [], f'a deliberate abort is not a bug and must log nothing: {caplog.text}'


@pytest.mark.asyncio
@pytest.mark.parametrize('service', ['', SERVICE], ids=['server', 'service'])
async def test_health_watch_streams_through_the_boundary_from_serving_to_not_serving(service: str):
    """Watch is an async generator, the branch _guarded has to wrap as one; a stream that still works proves it did.

    The watch is itself an in-flight call, so it stays open across stop() and sees the transition rather
    than just ending.
    """
    async with _hosted() as (host, channel):
        watch = health_pb2_grpc.HealthStub(channel).Watch(health_pb2.HealthCheckRequest(service=service))
        first = await asyncio.wait_for(watch.read(), GUARD_S)
        assert first.status == SERVING, 'Watch must deliver its first message through the boundary'
        stopping = asyncio.create_task(host.stop())
        second = await asyncio.wait_for(watch.read(), GUARD_S)
        assert second is not grpc.aio.EOF, 'the wrapped stream ended instead of reporting NOT_SERVING'
        assert second.status == NOT_SERVING
        await asyncio.wait_for(stopping, GUARD_S)


# -----------------------------------------------------------------------------------------------------
# THERE IS NO OFF SWITCH, AND THE BOUNDARY IS FIRST
#
# The constructor takes interceptors a caller wants IN ADDITION. Two ways that could have been written
# wrong: the caller's list could REPLACE the boundary (an off switch by another name), or the boundary
# could be appended LAST, which would leave everything a caller's interceptor does outside the guarantee.


class _RecordingInterceptor(grpc.aio.ServerInterceptor):
    """A caller's own interceptor: it records every method dispatched through it and changes nothing."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def intercept_service(
        self, continuation: _Continuation, handler_call_details: grpc.HandlerCallDetails
    ) -> grpc.RpcMethodHandler | None:
        """Record the method and hand the call on unchanged.

        Args:
            continuation: Resolves the next interceptor, or the handler lookup.
            handler_call_details: The call being dispatched.

        Returns:
            grpc.RpcMethodHandler | None: Exactly what the rest of the chain resolved.
        """
        self.seen.append(getattr(handler_call_details, 'method', ''))
        return await continuation(handler_call_details)


class _WrappingInterceptor(grpc.aio.ServerInterceptor):
    """A caller's interceptor that REPLACES the handler's behaviour with one that raises.

    This is how 'the boundary is first' is measured without reading a private attribute. First means
    outermost: the boundary wraps whatever handler the rest of the chain resolved, so it guards this
    interceptor's behaviour. Appended last it would wrap the real handler instead -- which this interceptor
    then discards -- and the raise would escape to gRPC's UNKNOWN default, canary and all.
    """

    def __init__(self, exc: BaseException, method: str) -> None:
        self._exc = exc
        self._method = method

    async def intercept_service(
        self, continuation: _Continuation, handler_call_details: grpc.HandlerCallDetails
    ) -> grpc.RpcMethodHandler | None:
        """Return a handler whose behaviour raises, for the one method under test.

        Args:
            continuation: Resolves the next interceptor, or the handler lookup.
            handler_call_details: The call being dispatched.

        Returns:
            grpc.RpcMethodHandler | None: A raising handler for the method under test, and whatever the
            chain resolved for anything else.
        """
        handler = await continuation(handler_call_details)
        # Only the method under test: health's Check is unary_unary too, and wrapping it would prove
        # nothing while breaking every other assertion on this server.
        if handler is None or getattr(handler_call_details, 'method', None) != self._method:
            return handler

        async def raises(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
            raise self._exc

        return grpc.unary_unary_rpc_method_handler(
            raises, request_deserializer=handler.request_deserializer, response_serializer=handler.response_serializer
        )


@pytest.mark.asyncio
async def test_a_callers_interceptor_runs_and_does_not_displace_the_boundary():
    """Supplying interceptors adds to the chain; it never substitutes for the boundary.

    A constructor argument that replaced the boundary rather than prepending to it would be the off switch
    bead tj-19r2z5 item 3 forbids, reached by passing any list at all.
    """
    recorder = _RecordingInterceptor()
    async with _hosted(raising(RuntimeError(f'boom: {CANARY}')), interceptors=[recorder]) as (_, channel):
        replied = await invoke(channel, UNARY_UNARY)
    assert path(UNARY_UNARY) in recorder.seen, "the caller's own interceptor never ran"
    _bug_id(replied)
    assert CANARY not in repr((replied.failed.details(), list(replied.failed.trailing_metadata() or ())))


@pytest.mark.asyncio
async def test_the_boundary_is_outermost_so_it_guards_what_a_callers_interceptor_returned():
    """Bead item 2: an auth or logging interceptor added later is INSIDE the guarantee, not outside it.

    The exception here is raised by the caller's interceptor's own handler, not by any servicer. It still
    becomes INTERNAL with an error_id and no text, which is only true while the boundary is first.
    """
    wrapper = _WrappingInterceptor(RuntimeError(f'boom: {CANARY}'), path(UNARY_UNARY))
    async with _hosted(interceptors=[wrapper]) as (_, channel):
        replied = await invoke(channel, UNARY_UNARY)
    _bug_id(replied)
    wire = repr((replied.failed.details(), list(replied.failed.trailing_metadata() or ())))
    assert CANARY not in wire, f'an interceptor behind the boundary leaked its exception text: {wire}'


@pytest.mark.asyncio
async def test_several_caller_interceptors_all_run_and_the_boundary_still_answers():
    """More than one addition, to show the chain is built rather than one slot that the last writer wins."""
    first, second = _RecordingInterceptor(), _RecordingInterceptor()
    async with _hosted(raising(ValueError(f'boom: {CANARY}')), interceptors=[first, second]) as (_, channel):
        replied = await invoke(channel, UNARY_UNARY)
    assert path(UNARY_UNARY) in first.seen and path(UNARY_UNARY) in second.seen
    _bug_id(replied)


@pytest.mark.asyncio
async def test_an_empty_interceptor_list_is_not_a_way_to_ask_for_no_boundary():
    """The default and an explicit empty list are the same thing: guarded. Neither is an opt-out."""
    async with _hosted(raising(RuntimeError(f'boom: {CANARY}')), interceptors=[]) as (_, channel):
        replied = await invoke(channel, UNARY_UNARY)
    _bug_id(replied)
