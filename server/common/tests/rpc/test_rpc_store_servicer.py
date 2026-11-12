"""THE StoreServicer SEAM: a client stream in, one AttemptDelivery to the handler, the answer out (tj-7zjov1.16).

The design (rpc/store.py, store.proto): the servicer assembles the stream, calls the handler ONCE, and encodes
what it returns. A stream that breaks the header-payload-trailer grammar is the sender's error, refused as
INVALID_REQUEST, and the handler is never called, so an incomplete attempt writes nothing. A TraderJoeError
the handler raises passes the error boundary as itself. RegisterInstance reaches the handler in domain terms.

Some cases drive the servicer directly, because the boundary hides raises and a test through a server could
not tell an InvalidRequestError from anything else that became a status; the rest run on a real
GrpcServerHost registered through the production ``store_service``.
"""

import inspect
from collections.abc import AsyncIterator, Sequence

import grpc
import pytest
from google.protobuf.message import Message

from common.errors.vocabulary import InvalidRequestError, Reason, TraderJoeError
from common.rpc.errors import from_rpc_error
from common.rpc.mapping import attempt_to_messages, register_instance_request_to_proto, response_to_domain
from common.rpc.store import SERVICE_NAME, DeliveryHandler, StoreServicer, store_service
from common.tests.rpc.delivery_contract import (
    DELIVER_PATH,
    REGISTER_PATH,
    STORE_SERVICE,
    Envelope,
    Handler,
    RegisterRequestMessage,
    RegisterResponseMessage,
    ResponseMessage,
    a_registration,
    a_response,
    an_asset,
    an_attempt,
    records_of,
    store_peer,
)
from common.tests.rpc.errors_harness import error_for
from common.tests.rpc.fetch_contract import GUARD_S
from schemas.data_ingest import delivery as domain


pytestmark = pytest.mark.common


async def _stream(messages: Sequence[Message]) -> AsyncIterator[Message]:
    for message in messages:
        yield message


async def _deliver_directly(handler: Handler, messages: Sequence[Message]) -> Message:
    return await StoreServicer(handler).DeliverAttempt(_stream(messages), None)  # type: ignore[arg-type]


def _assets_attempt() -> domain.AttemptDelivery:
    return an_attempt(domain.AssetRecords(records=[an_asset(index) for index in range(1001)]))


# ---------------------------------------------------------------------------------------------
# DIRECTLY: what the servicer raises and what it hands the handler


@pytest.mark.asyncio
async def test_a_whole_stream_reaches_the_handler_once_as_one_attempt_and_its_answer_is_encoded():
    """The handler sees the domain attempt, records in order across the split; the answer is the handler's."""
    handler = Handler(answer=a_response(domain.DeliveryVerdict.FINAL, count=4))
    attempt = _assets_attempt()

    answer = await _deliver_directly(handler, attempt_to_messages(attempt))

    (received,) = handler.delivered
    assert received.header == attempt.header and received.trailer == attempt.trailer
    assert records_of(received) == records_of(attempt)
    assert response_to_domain(answer) == a_response(domain.DeliveryVerdict.FINAL, count=4)


MALFORMED = {
    'no trailer': lambda messages: messages[:-1],
    'no header': lambda messages: messages[1:],
    'a second header': lambda messages: [messages[0], *messages],
    'after the trailer': lambda messages: [*messages, messages[1]],
    'an empty oneof': lambda messages: [messages[0], Envelope(), *messages[1:]],
    'empty stream': lambda messages: [],
}


@pytest.mark.asyncio
@pytest.mark.parametrize('break_it', list(MALFORMED.values()), ids=list(MALFORMED))
async def test_a_malformed_stream_is_invalid_request_and_the_handler_is_never_called(break_it):
    """The sender's error, typed as such, and nothing is written for it.

    Args:
        break_it: Turns a good stream into a bad one.
    """
    handler = Handler()

    with pytest.raises(InvalidRequestError) as excinfo:
        await _deliver_directly(handler, break_it(attempt_to_messages(_assets_attempt())))

    assert excinfo.value.reason is Reason.INVALID_REQUEST
    assert handler.delivered == []


@pytest.mark.asyncio
async def test_an_unreadable_registration_is_invalid_request_and_the_handler_is_never_called():
    """started_at unset: refused, not read as the epoch."""
    handler = Handler()

    with pytest.raises(InvalidRequestError):
        await StoreServicer(handler).RegisterInstance(RegisterRequestMessage(instance_id='ingest-1'), None)

    assert handler.registered == []


# ---------------------------------------------------------------------------------------------
# OVER A REAL GrpcServerHost, registered the production way


@pytest.mark.asyncio
async def test_a_malformed_stream_on_the_wire_is_invalid_request_to_the_caller():
    """At the wire the refusal is a status the client reads back as INVALID_REQUEST, not INTERNAL."""
    handler = Handler()
    good = attempt_to_messages(an_attempt())

    async with store_peer(handler) as channel:
        call = channel.stream_unary(
            DELIVER_PATH,
            request_serializer=lambda message: message.SerializeToString(),
            response_deserializer=ResponseMessage.FromString,
        )
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await call(iter(good[:-1]), timeout=GUARD_S)

    assert from_rpc_error(excinfo.value).reason is Reason.INVALID_REQUEST
    assert handler.delivered == []


@pytest.mark.asyncio
async def test_the_handlers_typed_error_passes_the_boundary_as_itself():
    """The store's reason reaches ingest; the boundary does not flatten it to INTERNAL."""
    handler = Handler(raising=error_for(Reason.VENDOR_UNAVAILABLE, 'the database is restarting'))

    async with store_peer(handler) as channel:
        call = channel.stream_unary(
            DELIVER_PATH,
            request_serializer=lambda message: message.SerializeToString(),
            response_deserializer=ResponseMessage.FromString,
        )
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await call(iter(attempt_to_messages(an_attempt())), timeout=GUARD_S)

    error = from_rpc_error(excinfo.value)
    assert isinstance(error, TraderJoeError)
    assert error.reason is Reason.VENDOR_UNAVAILABLE
    assert len(handler.delivered) == 1


@pytest.mark.asyncio
async def test_register_instance_reaches_the_handler_in_domain_terms():
    """The announcement arrives as the domain model, and the answer is the empty message."""
    handler = Handler()

    async with store_peer(handler) as channel:
        call = channel.unary_unary(
            REGISTER_PATH,
            request_serializer=lambda message: message.SerializeToString(),
            response_deserializer=RegisterResponseMessage.FromString,
        )
        answer = await call(register_instance_request_to_proto(a_registration()), timeout=GUARD_S)

    assert handler.registered == [a_registration()]
    assert answer.ListFields() == []


@pytest.mark.asyncio
async def test_an_unreadable_registration_on_the_wire_is_invalid_request():
    """The same refusal as DeliverAttempt's, at the wire."""
    handler = Handler()

    async with store_peer(handler) as channel:
        call = channel.unary_unary(
            REGISTER_PATH,
            request_serializer=lambda message: message.SerializeToString(),
            response_deserializer=RegisterResponseMessage.FromString,
        )
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await call(RegisterRequestMessage(instance_id='ingest-1'), timeout=GUARD_S)

    assert from_rpc_error(excinfo.value).reason is Reason.INVALID_REQUEST
    assert handler.registered == []


# ---------------------------------------------------------------------------------------------
# THE REGISTRATION AND THE SEAM'S SHAPE


def test_the_registration_names_the_service_the_descriptor_declares():
    """The health service reports under this name; it must be the contract's."""
    assert SERVICE_NAME == STORE_SERVICE.full_name == 'trader_joe.proto.internal.store.v1.StoreService'
    assert store_service(Handler()).name == SERVICE_NAME


@pytest.mark.parametrize('method', ['deliver', 'register_instance'])
def test_the_handler_double_matches_the_protocol_by_shape(method: str):
    """DeliveryHandler is structural; the double, which inherits nothing, has the same signature.

    Args:
        method: The method compared.
    """
    assert DeliveryHandler not in Handler.__mro__
    declared = inspect.signature(getattr(DeliveryHandler, method))
    implemented = inspect.signature(getattr(Handler, method))

    assert list(declared.parameters) == list(implemented.parameters)
    assert inspect.iscoroutinefunction(getattr(Handler, method))


def test_the_servicer_module_owns_the_mapper_calls_and_data_store_never_sees_a_message():
    """The handler's methods take and return domain models only."""
    hints = inspect.get_annotations(DeliveryHandler.deliver, eval_str=True)

    assert hints == {'attempt': domain.AttemptDelivery, 'return': domain.DeliverAttemptResponse}
    assert inspect.get_annotations(DeliveryHandler.register_instance, eval_str=True) == {
        'request': domain.RegisterInstanceRequest,
        'return': None,
    }
