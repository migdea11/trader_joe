"""What the tests of common/rpc/errors.py share: a four-cardinality grpc.aio server, and the one error factory.

THE ERROR FACTORY, first, because it is what keeps these tests honest. Every case in this gate is driven
off the REASONS table rather than a hand-written list of reasons. A hand list is the failure the architect
flagged on tj-3mk3u5.29's _NAIVE_CASES: it passes forever while the table grows past it, and nothing goes
red when a Reason is added. RENDERABLE and CODELESS are computed from REASONS, so adding a row puts it in
this gate on the next run, and error_for() builds a valid error for whichever reason it is handed --
supplying reset_at exactly where the row requires it and withholding it exactly where the row refuses it.

A REAL SERVER WITH ONE METHOD OF EVERY CARDINALITY, second.

WHY A GENERIC SERVICE AND NOT A GENERATED ONE. The boundary's whole claim (ADR tj-fa1rpu D1(b)) is that it

WHY A GENERIC SERVICE AND NOT A GENERATED ONE. The boundary's whole claim (ADR tj-fa1rpu D1(b)) is that it
covers a method nobody wrote it for -- including a cardinality this repository does not serve today. A
generated stub can only offer the methods some .proto already declares, so a test built on one could never
show that. grpc.method_handlers_generic_handler takes handlers this module builds by hand, with identity
serialisers over bytes, so all four cardinalities exist on one server and no .proto is involved. Nothing
here imports trader_joe.proto, which TID251 bans outside common/rpc.

Every server binds 127.0.0.1 on an ephemeral port and is stopped in a finally, because a grpc.aio server
still running when its event loop closes hangs the interpreter at exit instead of failing one test (T1
finding N3). Every network await is bounded by GUARD_S for the same reason.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import grpc

from common.errors.vocabulary import REASONS, Outcome, Reason, TraderJoeError
from common.rpc.errors import ErrorBoundaryInterceptor, abort_with_error


# Every Reason whose row names a gRPC code, and every Reason whose row does not. Computed from the one
# table (D4), never listed by hand, so a row added tomorrow is in this gate tomorrow. Sorted so the
# parametrised ids are stable and a failure names the same case on every machine.
RENDERABLE: tuple[Reason, ...] = tuple(sorted(r for r, spec in REASONS.items() if spec.grpc_code is not None))
CODELESS: tuple[Reason, ...] = tuple(sorted(r for r, spec in REASONS.items() if spec.grpc_code is None))

# How far ahead a reset_at sits in these tests. Large enough that the derived retry_after is stable
# whatever the test machine is doing, small enough to be obviously a test value.
RESET_AFTER_S: int = 300


def error_for(
    reason: Reason,
    detail: str = 'the request did not succeed',
    *,
    metadata: Mapping[str, str | Sequence[str]] | None = None,
    reset_at: datetime | None = None,
    with_reset_at: bool = False,
) -> TraderJoeError:
    """Build a valid error for ANY Reason, on its own branch, obeying that row's reset_at rule.

    TE-1 requires reset_at on the two rows that set requires_reset_at and refuses it on every REFUSED
    row, so a test that swept the table with one fixed shape would raise from the constructor rather than
    exercise the renderer. This decides per row instead.

    Args:
        reason: Any member of Reason.
        detail: The human text, which becomes the status message.
        metadata: Allowlisted context, as the constructor takes it.
        reset_at: An explicit instant, overriding the default. Ignored where the row refuses one.
        with_reset_at: Ask for a reset_at even where the row only permits it, so the RetryInfo path can be
            driven on a reason that does not require one.

    Returns:
        TraderJoeError: An instance of REASONS[reason].branch, never of a leaf, whose constructor may differ.
    """
    spec = REASONS[reason]
    wanted = spec.requires_reset_at or with_reset_at or reset_at is not None
    chosen = None
    if wanted and spec.outcome is not Outcome.REFUSED:
        chosen = reset_at if reset_at is not None else datetime.now(UTC) + timedelta(seconds=RESET_AFTER_S)
    return spec.branch(reason, detail, metadata=metadata, reset_at=chosen)


SERVICE: str = 'trader_joe.test.boundary.v1.BoundaryService'
LOOPBACK: str = '127.0.0.1'
GUARD_S: float = 10.0

# One method per (request_streaming, response_streaming) pair, named for the pair. UNARY_STREAM is the
# shape data_ingest's FetchDataset uses; the other three are here so the boundary cannot be proved on the
# cardinalities we happen to serve and left unproved on the ones a later service adds.
UNARY_UNARY: str = 'UnaryUnary'
UNARY_STREAM: str = 'UnaryStream'
STREAM_UNARY: str = 'StreamUnary'
STREAM_STREAM: str = 'StreamStream'
METHODS: tuple[str, ...] = (UNARY_UNARY, UNARY_STREAM, STREAM_UNARY, STREAM_STREAM)

# What a method that reaches its end returns or yields, so a test can tell a completed call from an
# aborted one without guessing.
SERVED: bytes = b'served'

# An action is what the method body does once it has consumed its request: raise, abort, or nothing.
Action = Callable[[grpc.aio.ServicerContext], Awaitable[None]]


def path(method: str) -> str:
    """The full RPC path for one of METHODS, as a handler_call_details.method carries it."""
    return f'/{SERVICE}/{method}'


async def nothing(context: grpc.aio.ServicerContext) -> None:
    """An action that lets the method finish normally."""


def raising(exc: BaseException) -> Action:
    """An action that raises exc, with whatever __cause__ or __context__ the caller already gave it."""

    async def action(context: grpc.aio.ServicerContext) -> None:
        raise exc

    return action


def aborting(error: TraderJoeError, *, method: str | None = None) -> Action:
    """An action that RETURNS an error rather than raising it, by calling abort_with_error itself.

    This is tj-3mk3u5.9's servicer path: it converts a returned failure and sends it without ever raising.
    The amendment of 2026-10-02 requires it to get exactly what a raised error gets, so every test that
    compares the two paths drives this action against raising() on the same error.
    """

    async def action(context: grpc.aio.ServicerContext) -> None:
        await abort_with_error(context, error, method=method)

    return action


def _identity(payload: bytes) -> bytes:
    return payload


async def _one_request() -> AsyncIterator[bytes]:
    yield b'request'


def _behaviours(action: Action, before: int) -> dict[str, Callable[..., object]]:
    # `before` messages are yielded BEFORE the action runs, so a streaming test can show that an abort
    # part way through a stream ends it after whatever was already sent, rather than discarding it.

    async def unary_unary(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
        await action(context)
        return SERVED

    async def unary_stream(request: bytes, context: grpc.aio.ServicerContext) -> AsyncIterator[bytes]:
        for index in range(before):
            yield f'{index}'.encode()
        await action(context)
        yield SERVED

    async def stream_unary(requests: AsyncIterator[bytes], context: grpc.aio.ServicerContext) -> bytes:
        async for _ in requests:
            pass
        await action(context)
        return SERVED

    async def stream_stream(requests: AsyncIterator[bytes], context: grpc.aio.ServicerContext) -> AsyncIterator[bytes]:
        async for _ in requests:
            pass
        for index in range(before):
            yield f'{index}'.encode()
        await action(context)
        yield SERVED

    return {
        UNARY_UNARY: unary_unary,
        UNARY_STREAM: unary_stream,
        STREAM_UNARY: stream_unary,
        STREAM_STREAM: stream_stream,
    }


_FACTORIES: dict[str, Callable[..., grpc.RpcMethodHandler]] = {
    UNARY_UNARY: grpc.unary_unary_rpc_method_handler,
    UNARY_STREAM: grpc.unary_stream_rpc_method_handler,
    STREAM_UNARY: grpc.stream_unary_rpc_method_handler,
    STREAM_STREAM: grpc.stream_stream_rpc_method_handler,
}


@dataclass
class Replied:
    """What one call produced: the messages that arrived, and the error that ended it, if any.

    A streaming call keeps the messages it received before the abort, which is how the 'after whatever was
    already sent' half of the mid-stream rule is checked.
    """

    messages: list[bytes] = field(default_factory=list)
    error: grpc.aio.AioRpcError | None = None

    @property
    def failed(self) -> grpc.aio.AioRpcError:
        """The error, asserting there was one, so a test reads it without a None check at every site."""
        assert self.error is not None, f'the call succeeded, returning {self.messages!r}'
        return self.error


async def invoke(channel: grpc.aio.Channel, method: str, *, timeout_s: float = 5.0) -> Replied:
    """Call one of METHODS and collect everything it produced. Never raises AioRpcError: it is the answer.

    Args:
        channel: An open channel to a server from boundary_server().
        method: One of METHODS.
        timeout_s: The call's own deadline.

    Returns:
        Replied: The messages received, in order, and the error that ended the call, if any.
    """
    replied = Replied()
    target = path(method)
    try:
        if method == UNARY_UNARY:
            call = channel.unary_unary(target, _identity, _identity)
            replied.messages.append(await asyncio.wait_for(call(b'request', timeout=timeout_s), GUARD_S))
        elif method == STREAM_UNARY:
            call = channel.stream_unary(target, _identity, _identity)
            replied.messages.append(await asyncio.wait_for(call(_one_request(), timeout=timeout_s), GUARD_S))
        else:
            factory = channel.unary_stream if method == UNARY_STREAM else channel.stream_stream
            stream = factory(target, _identity, _identity)
            request = b'request' if method == UNARY_STREAM else _one_request()
            async for response in _bounded(stream(request, timeout=timeout_s)):
                replied.messages.append(response)
    except grpc.aio.AioRpcError as error:
        replied.error = error
    return replied


async def _bounded(call: grpc.aio.StreamStreamCall) -> AsyncIterator[bytes]:
    # A per-message guard, so a stream that stalls fails this test instead of hanging the suite.
    while (response := await asyncio.wait_for(call.read(), GUARD_S)) is not grpc.aio.EOF:
        yield response


@contextlib.asynccontextmanager
async def writing_stream_server(action: Action, *, before: int = 0) -> AsyncIterator[grpc.aio.Channel]:
    """A server whose one response-streaming method is a COROUTINE that writes through the context.

    grpc.aio accepts two shapes for a response-streaming behaviour: an async generator function, and a
    coroutine that calls context.write(). _guarded() branches on inspect.isasyncgenfunction and wraps the
    behaviour as whichever it is, because wrapping a generator in a coroutine would hand gRPC an un-awaited
    generator object as the single response. boundary_server() drives the first shape everywhere; this
    drives the second, so the branch that is not the obvious one is not the untested one.

    Yields:
        grpc.aio.Channel: A channel to the running server, with UNARY_STREAM the only method.
    """

    async def writes(request: bytes, context: grpc.aio.ServicerContext) -> None:
        for index in range(before):
            await context.write(f'{index}'.encode())
        await action(context)
        await context.write(SERVED)

    server = grpc.aio.server(interceptors=[ErrorBoundaryInterceptor()])
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                SERVICE,
                {
                    UNARY_STREAM: grpc.unary_stream_rpc_method_handler(
                        writes, request_deserializer=_identity, response_serializer=_identity
                    )
                },
            ),
        )
    )
    port = server.add_insecure_port(f'{LOOPBACK}:0')
    await server.start()
    channel = grpc.aio.insecure_channel(f'{LOOPBACK}:{port}')
    try:
        yield channel
    finally:
        await channel.close()
        await server.stop(None)


@contextlib.asynccontextmanager
async def boundary_server(
    action: Action = nothing, *, before: int = 0, with_boundary: bool = True
) -> AsyncIterator[grpc.aio.Channel]:
    """Run a server carrying all four cardinalities and yield an open channel to it.

    Args:
        action: What each method body does once it has consumed its request.
        before: Messages a response-streaming method yields before running the action.
        with_boundary: False installs no interceptor, which is how a test shows what the boundary is
            holding back -- gRPC's own default for an escaping exception.

    Yields:
        grpc.aio.Channel: A channel to the running server. Both are torn down on the way out.
    """
    interceptors = [ErrorBoundaryInterceptor()] if with_boundary else []
    server = grpc.aio.server(interceptors=interceptors)
    behaviours = _behaviours(action, before)
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                SERVICE,
                {
                    name: _FACTORIES[name](
                        behaviours[name], request_deserializer=_identity, response_serializer=_identity
                    )
                    for name in METHODS
                },
            ),
        )
    )
    port = server.add_insecure_port(f'{LOOPBACK}:0')
    assert port != 0, 'the server could not bind an ephemeral loopback port'
    await server.start()
    channel = grpc.aio.insecure_channel(f'{LOOPBACK}:{port}')
    try:
        yield channel
    finally:
        # Both, always. A grpc.aio server still running when the loop closes hangs the whole run.
        await channel.close()
        await server.stop(None)
