"""The error vocabulary on the gRPC hop: google.rpc.Status with ErrorInfo and RetryInfo, both ways.

ADR tj-fa1rpu D1(b), D5, D6, D8 and Tier 4, with its 2026-09-30 addendum (the (domain, reason) pair travels
as google.rpc.Status + ErrorInfo here) and its 16:22 UTC 2026-10-02 addendum (every typed status carries an
error_id). ADR tj-8konfu D6.4 fixes the mapping and two rules this module obeys absolutely: THE SERVER NEVER
DELIBERATELY RETURNS UNAVAILABLE, and CLIENTS BRANCH ON THE ErrorInfo REASON, NOT ON THE STATUS.

This is the gRPC twin of routers/common/errors.py. Both read common/errors, which knows no protocol, and
neither knows the other. Where they must agree -- that every typed answer carries an error_id, that the one
log line names it, and that the line carries the cause chain only when the edge minted the id -- they agree
BY CALLING THE SAME CODE: own_error_id and has_cause_chain live in common/errors beside new_error_id, where
both edges read them. Neither needs a transport to answer its question, so the standard-library-only pin on
that package is no obstacle, and two copies of a judgement cannot be kept in step by a comment -- these two
were written as copies and had diverged on the sequence join within two days (tj-zxqn4r).

THE THREE PIECES, in the order a failure meets them:

    render()                    a TraderJoeError -> google.rpc.Status. PURE: it logs nothing and sends
                                nothing, like TE-3's render(). Code from REASONS, message from detail,
                                an ErrorInfo detail carrying (reason, domain) and the allowlisted metadata,
                                and a RetryInfo detail when the error knows when it clears.
    abort_with_error()          THE ONE FUNCTION THAT SENDS A TYPED ERROR AS A STATUS. It settles the
                                error_id, writes the one log line, and aborts the call. The boundary below
                                calls it for a RAISED error and tj-3mk3u5.9's servicer calls it for a
                                RETURNED one, so the two get identical treatment.
    ErrorBoundaryInterceptor    D1(b): EVERY ESCAPING EXCEPTION BECOMES A REPLY. An interceptor rather than
                                a decorator because the obligation is absolute and a decorator can be
                                forgotten; it wraps every method of every servicer attached to the server,
                                unary and streaming alike.

And on the other side of the hop:

    from_rpc_error()            an AioRpcError -> a TraderJoeError, preserving the reason so the caller can
                                branch on it (D6.4) and the error_id so the id a human quotes is the one in
                                the PEER's log.

WHAT A BUG LOOKS LIKE ON THE WIRE (D5, D8). gRPC's own default for an exception escaping a servicer is
UNKNOWN with the traceback in the status details -- precisely the leak D8 forbids. The boundary answers
INTERNAL instead, with a message that carries an error_id and nothing else, and NO ErrorInfo: an ErrorInfo
asserts a (domain, reason) pair from our closed vocabulary, and a bug has no reason. The traceback goes to
the log under the same id. Because the id is then the only machine-readable thing the reply has, the message
is a FIXED TEMPLATE both halves of this module share (_INTERNAL_MESSAGE_PREFIX), and from_rpc_error() reads
the id back out of it. That is not parsing a human detail (D8): it is reading our own one-line format.

WHAT NEVER COMES THROUGH HERE. FEED_NOT_AVAILABLE is refused in-band on the FetchDataset ack, never as a
status (user ruling tj-3mk3u5.22 Q5; tj-3mk3u5.27). It is one of the reasons whose REASONS row has no
grpc_code, and render() refuses every one of them.

SEQUENCE-VALUED METADATA, THE ONE STATED ENCODING. common/errors lets an allowlisted key hold a str or a
sequence of str, while an AIP-193 ErrorInfo metadata value is a string. On this hop a sequence is written as
its items joined by a comma and nothing else, and a value read back is always a str, never re-split: a
comma inside one item is indistinguishable from the separator, so splitting would invent a tuple. Nothing
carries a sequence across this hop in PR 2 -- colliding_ids arises in data_store, which answers over HTTP --
so the encoding is stated here rather than chosen under pressure later.
"""

import functools
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final, NoReturn

import grpc
from google.protobuf import message
from google.rpc import error_details_pb2, status_pb2
from grpc_status import rpc_status

from common.errors.vocabulary import (
    ERROR_DOMAIN,
    METADATA_SEQUENCE_SEPARATOR,
    REASONS,
    Disposition,
    ExogenousError,
    Reason,
    TraderJoeError,
    has_cause_chain,
    new_error_id,
    own_error_id,
)
from common.logging import get_logger


log = get_logger(__name__)

# Where a rich google.rpc.Status rides on a gRPC response, fixed by the status-mapping convention every
# gRPC implementation shares. Named here because both halves of this module need it and grpcio-status
# exports it only from a private module; _check_module() below proves the two spellings agree.
_STATUS_DETAILS_TRAILER: Final = 'grpc-status-details-bin'

# The METADATA_KEYS key D8's correlation id travels under, on the error and on the wire. Spelled as in
# routers/common/errors.py, because it is the same key on both transports.
_ERROR_ID_KEY: Final = 'error_id'

# Allowlisted keys that common/errors derives from the reset_at attribute and refuses in the metadata
# argument (TE-1). Written from the attribute on the way out and lifted back out of the metadata on the way
# in, never passed through. retry_after is not written at all: RetryInfo is its gRPC-native carrier, and a
# second copy could only go stale on the way.
_RESET_AT_KEY: Final = 'reset_at'
_DERIVED_METADATA_KEYS: Final = frozenset({_RESET_AT_KEY, 'retry_after'})

# The one encoding for a sequence-valued metadata item on this hop (see the module docstring). It is the
# shared constant itself, not a second spelling of it, so the join this hop writes on the wire and the join
# own_error_id puts in a log line cannot be changed apart.
_SEQUENCE_SEPARATOR: Final = METADATA_SEQUENCE_SEPARATOR

# The whole message a bug's INTERNAL status carries, bar the id itself. One constant, used to write the
# message and to read it back, so the two can never drift apart.
_INTERNAL_MESSAGE_PREFIX: Final = 'internal error; error_id '

# How loudly the edge logs a typed error it sends, from the reason's disposition (D4). As TE-3 does at the
# HTTP edge: one line, naming the error_id, carrying the cause chain only under an id minted here.
_LOG_LEVELS: Final[Mapping[Disposition, int]] = MappingProxyType(
    {Disposition.PAGE: logging.ERROR, Disposition.RECORD: logging.WARNING, Disposition.CLIENT_FIX: logging.INFO}
)

# Which RpcMethodHandler member holds the behaviour, and which factory rebuilds the handler around a new
# one, keyed by (request_streaming, response_streaming). All four, so no cardinality can slip past the
# boundary unguarded -- the two this repository serves today and the two it does not.
_HANDLER_FACTORIES: Final[Mapping[tuple[bool, bool], tuple[str, Callable[..., grpc.RpcMethodHandler]]]] = (
    MappingProxyType(
        {
            (False, False): ('unary_unary', grpc.unary_unary_rpc_method_handler),
            (False, True): ('unary_stream', grpc.unary_stream_rpc_method_handler),
            (True, False): ('stream_unary', grpc.stream_unary_rpc_method_handler),
            (True, True): ('stream_stream', grpc.stream_stream_rpc_method_handler),
        }
    )
)

# What the log line calls a call whose method nobody passed.
_UNNAMED_METHOD: Final = 'a gRPC call'


def _check_module() -> None:
    """Refuse at import a module that could not do its job as written, so no build can ship one.

    Raises:
        ValueError: If grpcio-status writes the rich status under a trailer other than the one named here,
            or if a Disposition has no log level.
    """
    written = {key for key, _ in rpc_status.to_status(status_pb2.Status()).trailing_metadata}
    if written != {_STATUS_DETAILS_TRAILER}:
        raise ValueError(f'grpcio-status writes the rich status under {sorted(written)}, not {_STATUS_DETAILS_TRAILER}')
    unlevelled = sorted(disposition.value for disposition in Disposition if disposition not in _LOG_LEVELS)
    if unlevelled:
        raise ValueError(f'_LOG_LEVELS has no level for {unlevelled}')


_check_module()


def _wire_value(value: str | tuple[str, ...]) -> str:
    return value if isinstance(value, str) else _SEQUENCE_SEPARATOR.join(value)


def render(error: TraderJoeError, *, error_id: str | None = None) -> status_pb2.Status:
    """Render a TraderJoeError as the google.rpc.Status its reason's row names. Pure: nothing is logged or sent.

    The code is REASONS[reason].grpc_code and the message is the error's detail. One ErrorInfo detail carries
    the reason, ERROR_DOMAIN and every metadata key the error holds, each as a string; reset_at joins them in
    RFC 3339, and error_id is always present. When the error knows when it clears, a second RetryInfo detail
    carries the derived delay in whole seconds.

    The error_id is the error's own when its metadata holds one, and otherwise the error_id argument. A
    metadata error_id that names nothing ('', or a sequence with no non-empty str) is none. Without an
    argument, an id is minted here, so the status carries one all the same; a caller that means to LOG under
    the id settles it first and passes it in, which is what abort_with_error does.

    Args:
        error: The error to render.
        error_id: The id to carry when the error has none of its own.

    Returns:
        status_pb2.Status: The rich status, ready for abort_with_error or rpc_status.to_status.

    Raises:
        ValueError: If the reason is never rendered as a gRPC status, or -- unreachably, since TE-1's table
            refuses such a row at import -- if its code is UNAVAILABLE.
    """
    spec = REASONS[error.reason]
    if spec.grpc_code is None:
        raise ValueError(
            f'{error.reason} is never rendered as a gRPC status: REASONS[{error.reason}].grpc_code is None. '
            'FEED_NOT_AVAILABLE travels in the FetchDataset ack (tj-3mk3u5.22 Q5), the PEER_* reasons and '
            'DEADLINE are made on the client side of a hop, and the DATABASE_* reasons answer over HTTP'
        )
    code = grpc.StatusCode[spec.grpc_code]
    if code is grpc.StatusCode.UNAVAILABLE:
        raise ValueError(
            f'{error.reason} would render as UNAVAILABLE, which the server never returns deliberately '
            '(ADR tj-8konfu D6.4); it is the transport saying the peer is down'
        )

    metadata = {key: _wire_value(value) for key, value in error.metadata.items()}
    if own_error_id(error) is None:
        metadata[_ERROR_ID_KEY] = error_id or new_error_id()
    # Read once each: the two are derived from one reset_at, and retry_after is re-derived from the clock on
    # every read, so a second read could name a different number than the one already written.
    reset_at, retry_after = error.reset_at, error.retry_after
    if reset_at is not None:
        metadata[_RESET_AT_KEY] = reset_at.isoformat()

    status = status_pb2.Status(code=code.value[0], message=error.detail)
    status.details.add().Pack(
        error_details_pb2.ErrorInfo(reason=error.reason.value, domain=ERROR_DOMAIN, metadata=metadata)
    )
    if retry_after is not None:
        retry_info = error_details_pb2.RetryInfo()
        retry_info.retry_delay.FromSeconds(retry_after)
        status.details.add().Pack(retry_info)
    return status


async def abort_with_error(
    context: grpc.aio.ServicerContext, error: TraderJoeError, *, method: str | None = None
) -> NoReturn:
    """Send a TraderJoeError as this call's status: the one way a typed error leaves a servicer.

    D8 at the edge, as its 16:22 UTC 2026-10-02 addendum reads it through: the status carries the error's own
    error_id, or one minted here, and the same id names the one log line. That line is written at the level
    the reason's disposition sets, and carries the cause chain only under an id minted here -- an error that
    arrived with an id was logged, chain and all, by whoever set it.

    Both paths into a status come here, so a RETURNED error (tj-3mk3u5.9's servicer, converting a
    BarsFailure) gets exactly what a RAISED one gets from the boundary below.

    Args:
        context: The servicer context for the call being answered.
        error: The error to send. Its reason must be one that renders as a status.
        method: The RPC path for the log line, e.g. '/trader_joe.proto.internal.ingest.v1.IngestService/
            FetchDataset'. The boundary passes it; a servicer calling this directly may.

    Raises:
        BaseException: Always. context.abort raises to terminate the call, which is how the status is sent.
        ValueError: If the reason is never rendered as a gRPC status, as render() raises. The boundary below
            never lets that escape: it treats such a reason as the programming error it is.
    """
    own_id = own_error_id(error)
    error_id = new_error_id() if own_id is None else own_id
    status = render(error, error_id=error_id)
    log.log(
        _LOG_LEVELS[REASONS[error.reason].disposition],
        f'{method or _UNNAMED_METHOD} -> {REASONS[error.reason].grpc_code} {error.reason}: {error.detail}; '
        f'error_id {error_id}',
        exc_info=error if own_id is None and has_cause_chain(error) else None,
    )
    rich = rpc_status.to_status(status)
    await context.abort(rich.code, rich.details, rich.trailing_metadata)


async def _abort_with_bug(context: grpc.aio.ServicerContext, exc: BaseException, method: str | None) -> NoReturn:
    # A bug (D5). The caller gets an id and nothing else -- no ErrorInfo, because a bug has no reason from
    # our vocabulary; the operator gets the traceback under the same id.
    error_id = new_error_id()
    log.error(f'Unhandled {type(exc).__name__} on {method or _UNNAMED_METHOD}; error_id {error_id}', exc_info=exc)
    await context.abort(grpc.StatusCode.INTERNAL, f'{_INTERNAL_MESSAGE_PREFIX}{error_id}')


async def _abort_for(context: grpc.aio.ServicerContext, exc: Exception, method: str | None) -> NoReturn:
    if isinstance(exc, TraderJoeError) and REASONS[exc.reason].grpc_code is not None:
        await abort_with_error(context, exc, method=method)
    # Everything else is a bug, INCLUDING a TraderJoeError whose reason never renders as a status: raising
    # FEED_NOT_AVAILABLE out of a servicer is a programming error, since it belongs in the ack. Treating it
    # as a bug rather than letting render() raise keeps this boundary total -- it answers, always.
    await _abort_with_bug(context, exc, method)


def _guarded(behaviour: Callable[..., object], method: str | None) -> Callable[..., object]:
    # grpc.aio lets a response-streaming behaviour be either an async generator or a coroutine that writes
    # through the context, so the wrapper has to be whichever the behaviour is: wrapping a generator in a
    # coroutine would hand gRPC an un-awaited generator object as the single response.
    if inspect.isasyncgenfunction(behaviour):

        @functools.wraps(behaviour)
        async def streamed(request: object, context: grpc.aio.ServicerContext) -> object:
            try:
                async for response in behaviour(request, context):
                    yield response
            except grpc.aio.AbortError:
                # The servicer aborted on purpose; its status is already set. Not silence.
                raise
            except Exception as exc:
                # Mid-stream too: abort ends the stream with this status, after whatever was already sent.
                await _abort_for(context, exc, method)

        return streamed

    @functools.wraps(behaviour)
    async def answered(request: object, context: grpc.aio.ServicerContext) -> object:
        try:
            return await behaviour(request, context)
        except grpc.aio.AbortError:
            raise
        except Exception as exc:
            await _abort_for(context, exc, method)

    return answered


def _guarded_handler(handler: grpc.RpcMethodHandler, method: str | None) -> grpc.RpcMethodHandler:
    name, factory = _HANDLER_FACTORIES[(bool(handler.request_streaming), bool(handler.response_streaming))]
    return factory(
        _guarded(getattr(handler, name), method),
        request_deserializer=handler.request_deserializer,
        response_serializer=handler.response_serializer,
    )


class ErrorBoundaryInterceptor(grpc.aio.ServerInterceptor):
    """The servicer boundary (ADR tj-fa1rpu D1(b), D6): every escaping exception becomes a reply.

    Pass it to grpc.aio.server(interceptors=[ErrorBoundaryInterceptor()]) and every method of every servicer
    on that server is covered, unary and server-streaming alike, including one added later by someone who
    never read this module. That is why it is an interceptor and not a decorator: D1(b) calls the obligation
    absolute, and a per-method decorator is exactly the kind of thing a new method forgets.

    A TraderJoeError becomes its status through abort_with_error. Anything else is a bug: INTERNAL carrying
    only an error_id, with the traceback logged under that id and never put on the wire (D5, D8). It is never
    silent and never UNAVAILABLE. An exception raised part way through a stream ends that stream with the
    status, after the messages already sent.

    A cancellation is not an exception this boundary answers: asyncio.CancelledError is a BaseException, so
    it passes through to the runtime, as does the AbortError a servicer raises by aborting on purpose.
    """

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler | None]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler | None:
        """Wrap the handler the rest of the chain resolved, so its behaviour cannot escape without replying.

        Args:
            continuation: Resolves the next interceptor, or the handler lookup.
            handler_call_details: The call being dispatched; its method names the RPC path for the log line.

        Returns:
            grpc.RpcMethodHandler | None: The guarded handler, or None when nothing serves this method -- an
            unknown method is gRPC's UNIMPLEMENTED to answer, not this boundary's.
        """
        handler = await continuation(handler_call_details)
        if handler is None:
            return None
        return _guarded_handler(handler, getattr(handler_call_details, 'method', None))


def _rich_status(error: grpc.aio.AioRpcError) -> status_pb2.Status | None:
    for key, value in error.trailing_metadata() or ():
        if key == _STATUS_DETAILS_TRAILER and isinstance(value, bytes):
            try:
                return status_pb2.Status.FromString(value)
            except message.DecodeError:
                return None
    return None


def _unpacked(status: status_pb2.Status, prototype: message.Message) -> message.Message | None:
    for detail in status.details:
        if detail.Is(prototype.DESCRIPTOR):
            try:
                detail.Unpack(prototype)
            except message.DecodeError:
                return None
            return prototype
    return None


def _bug_error_id(detail: str) -> str | None:
    # The id out of the fixed message a bug's INTERNAL status carries. A peer that is not ours, or a gRPC
    # runtime that answered INTERNAL itself, does not match the template and names no id.
    if not detail.startswith(_INTERNAL_MESSAGE_PREFIX):
        return None
    return detail.removeprefix(_INTERNAL_MESSAGE_PREFIX).strip() or None


def _protocol_error(code: grpc.StatusCode | None, detail: str, why: str) -> ExogenousError:
    # C8: a reply that will not map is its own reason, never a hang and never an escaping ValueError. The
    # raw text goes in the detail, where a human reads it and nothing parses it.
    name = code.name if code is not None else 'an unknown code'
    return ExogenousError(Reason.PEER_PROTOCOL_ERROR, f'the peer answered {name} and {why}; its message was {detail!r}')


def _rebuilt(
    reason: Reason, detail: str, info: error_details_pb2.ErrorInfo, status: status_pb2.Status
) -> TraderJoeError:
    # Built on the reason's own branch, never on a leaf, whose constructor may take different arguments.
    branch = REASONS[reason].branch
    metadata = {key: value for key, value in info.metadata.items() if key not in _DERIVED_METADATA_KEYS}
    written = info.metadata.get(_RESET_AT_KEY)
    if written:
        return branch(reason, detail, metadata=metadata, reset_at=datetime.fromisoformat(written))
    retry_info = _unpacked(status, error_details_pb2.RetryInfo())
    if retry_info is not None and retry_info.HasField('retry_delay'):
        # Relative to now, as RetryInfo means it, so a delay does not go stale on the way here.
        delay = retry_info.retry_delay.ToTimedelta().total_seconds()
        return branch.from_retry_after(reason, detail, delay, metadata=metadata)
    return branch(reason, detail, metadata=metadata)


def from_rpc_error(error: grpc.aio.AioRpcError) -> TraderJoeError:
    """Turn a failed gRPC call into the TraderJoeError it stands for, preserving the reason and the error_id.

    THE REASON IS PRESERVED, because clients branch on it and not on the status (ADR tj-8konfu D6.4). So is
    the error_id, so that data_store's HTTP edge renders the id from INGEST's log and the id a caller quotes
    is the one an operator searches for.

    The three transport outcomes are read from the code, before any detail is unpacked, because they are the
    transport speaking rather than a peer's vocabulary:

        UNAVAILABLE         PEER_UNAVAILABLE. The server never sends it deliberately, so it means the peer
                            is down or restarting.
        DEADLINE_EXCEEDED   DEADLINE.
        INTERNAL            PEER_INTERNAL, keeping the error_id out of the peer's fixed bug message.

    Anything else is expected to carry a google.rpc.Status with our ErrorInfo. A reply that will not map is
    PEER_PROTOCOL_ERROR carrying the raw text in its detail (tj-fa1rpu C8): no rich status, no ErrorInfo, a
    foreign domain, a reason this release does not know, or a payload common/errors refuses to rebuild --
    a REFUSED reason that arrived with a reset_at, say, or a metadata key outside the allowlist. Both
    services ship from one release, so an unknown reason here is deploy skew, not D10's SDK case.

    This function raises nothing: a decoding failure is a reason, not an exception.

    Args:
        error: The error grpc.aio raised for the failed call.

    Returns:
        TraderJoeError: The error, on the branch its reason belongs to.
    """
    code = error.code()
    detail = error.details() or ''
    if code is grpc.StatusCode.UNAVAILABLE:
        return ExogenousError(Reason.PEER_UNAVAILABLE, detail or 'the peer could not be reached')
    if code is grpc.StatusCode.DEADLINE_EXCEEDED:
        return ExogenousError(Reason.DEADLINE, detail or 'the call did not finish within its deadline')
    if code is grpc.StatusCode.INTERNAL:
        peer_id = _bug_error_id(detail)
        return ExogenousError(
            Reason.PEER_INTERNAL,
            detail or 'the peer failed with an internal error',
            metadata={_ERROR_ID_KEY: peer_id} if peer_id is not None else None,
        )

    status = _rich_status(error)
    if status is None:
        return _protocol_error(code, detail, 'it carries no google.rpc.Status this release could read')
    info = _unpacked(status, error_details_pb2.ErrorInfo())
    if info is None:
        return _protocol_error(code, detail, 'its google.rpc.Status carries no ErrorInfo')
    if info.domain != ERROR_DOMAIN:
        return _protocol_error(code, detail, f'its ErrorInfo names the domain {info.domain!r}, not {ERROR_DOMAIN!r}')
    if info.reason not in Reason.__members__:
        return _protocol_error(code, detail, f'its ErrorInfo reason {info.reason!r} is not one this release knows')
    try:
        return _rebuilt(Reason(info.reason), detail, info, status)
    except (TypeError, ValueError) as e:
        return _protocol_error(code, detail, f'its ErrorInfo reason {info.reason} will not rebuild here: {e}')
