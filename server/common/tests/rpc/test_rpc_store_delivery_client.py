"""THE GrpcStoreDeliveryClient SEAM against a real GrpcServerHost peer on loopback (tj-7zjov1.16).

The design (clients/store_delivery.py, store.proto, the owner's ack-gate ruling of 2026-10-24):

* ONE ATTEMPT IS ONE STREAM and the store's answer IS the acknowledgement: deliver() returns only after the
  handler has returned, and never on a part.
* THE CLIENT NEVER RETRIES. A failed call raises a typed TraderJoeError once; redelivery is the caller's, with
  the SAME AttemptDelivery, and the store answers the duplicate (request_id, attempt_no) from its stored row.
* An answer with the zero verdict is PEER_PROTOCOL_ERROR: the gate must not open on it.
* An attempt this client cannot encode is OUR bug, ProtoMappingError, raised before any call.
* One finite deadline per call with wait_for_ready: a dead peer fails at the deadline; a deadline that is not
  finite and positive is refused at construction.

The peer is the production servicer (``store_service``) on GrpcServerHost, or a scripted registration on the
same host where the production encoder cannot produce the answer under test. Nothing imports trader_joe.proto.
"""

import asyncio
import inspect
import math
import types
from collections.abc import AsyncIterator, Iterator

import grpc
import pytest
from google.protobuf.message import Message

import common.rpc.clients.store_delivery as store_delivery
from common.errors.vocabulary import Reason, TraderJoeError
from common.rpc.clients.store_delivery import (
    DEFAULT_DELIVERY_DEADLINE_S,
    DEFAULT_REGISTER_DEADLINE_S,
    GrpcStoreDeliveryClient,
    StoreDeliveryClient,
)
from common.rpc.mapping import ProtoMappingError, attempt_to_messages, response_to_proto
from common.tests.rpc.delivery_contract import (
    Handler,
    RegisterResponseMessage,
    ResponseMessage,
    a_header,
    a_registration,
    a_response,
    an_asset,
    an_attempt,
    hosted,
    records_of,
    scripted_registration,
    store_peer,
)
from common.tests.rpc.errors_harness import error_for
from common.tests.rpc.fetch_contract import GUARD_S
from schemas.data_ingest import delivery as domain


pytestmark = pytest.mark.common

UNREACHABLE = '127.0.0.1:1'


def _attempt() -> domain.AttemptDelivery:
    return an_attempt(domain.AssetRecords(records=[an_asset(index) for index in range(1500)]))


# ---------------------------------------------------------------------------------------------
# THE ACK IS THE ANSWER


@pytest.mark.asyncio
async def test_deliver_returns_the_stores_answer_and_the_handler_saw_the_whole_attempt():
    """The happy path by value: the split attempt is reassembled whole, and the ack comes back in domain terms."""
    handler = Handler(answer=a_response(domain.DeliveryVerdict.FINAL, count=4))
    attempt = _attempt()

    async with store_peer(handler) as channel:
        answer = await GrpcStoreDeliveryClient(channel, deadline_s=GUARD_S).deliver(attempt)

    assert answer == a_response(domain.DeliveryVerdict.FINAL, count=4)
    (received,) = handler.delivered
    assert (received.header, received.trailer) == (attempt.header, attempt.trailer)
    assert records_of(received) == records_of(attempt)


@pytest.mark.asyncio
async def test_deliver_returns_only_after_the_handler_returns():
    """The gate: while the handler holds its commit, deliver() has not returned."""
    gate = asyncio.Event()
    handler = Handler(gate=gate)

    async with store_peer(handler) as channel:
        task = asyncio.create_task(GrpcStoreDeliveryClient(channel, deadline_s=GUARD_S).deliver(_attempt()))
        await asyncio.wait_for(handler.entered.wait(), GUARD_S)
        await asyncio.sleep(0.2)
        assert not task.done(), 'deliver returned before the store answered'
        gate.set()
        answer = await asyncio.wait_for(task, GUARD_S)

    assert answer == a_response()


# ---------------------------------------------------------------------------------------------
# FAILURES ARE TYPED, AND RAISED ONCE


@pytest.mark.asyncio
async def test_a_peer_error_becomes_the_same_typed_error_and_the_call_is_made_once():
    """The store's reason arrives as itself; the client does not try again on its own."""
    handler = Handler(raising=error_for(Reason.VENDOR_UNAVAILABLE, 'the database is restarting'))

    async with store_peer(handler) as channel:
        with pytest.raises(TraderJoeError) as excinfo:
            await GrpcStoreDeliveryClient(channel, deadline_s=GUARD_S).deliver(_attempt())

    assert excinfo.value.reason is Reason.VENDOR_UNAVAILABLE
    assert isinstance(excinfo.value.__cause__, grpc.aio.AioRpcError)
    assert len(handler.delivered) == 1, 'the client retried; redelivery is the caller decision'


@pytest.mark.asyncio
async def test_a_peer_that_cuts_the_call_is_not_retried():
    """A transport-level failure (UNAVAILABLE from the peer) is raised once, not hidden behind a retry loop."""
    calls: list[int] = []

    async def unavailable(requests: AsyncIterator[Message], context: grpc.aio.ServicerContext) -> Message:
        calls.append(1)
        async for _ in requests:
            pass
        await context.abort(grpc.StatusCode.UNAVAILABLE, 'store going away')

    async with hosted(scripted_registration(unavailable)) as channel:
        with pytest.raises(TraderJoeError) as excinfo:
            await GrpcStoreDeliveryClient(channel, deadline_s=GUARD_S).deliver(_attempt())

    assert excinfo.value.reason is Reason.PEER_UNAVAILABLE
    assert calls == [1]


@pytest.mark.asyncio
async def test_an_answer_with_the_zero_verdict_is_a_peer_protocol_error():
    """An acknowledgement that says nothing: the mapping error is kept as the cause."""

    async def silent(requests: AsyncIterator[Message], context: grpc.aio.ServicerContext) -> Message:
        async for _ in requests:
            pass
        return ResponseMessage(verdict=0, attempt_count=3)

    async with hosted(scripted_registration(silent)) as channel:
        with pytest.raises(TraderJoeError) as excinfo:
            await GrpcStoreDeliveryClient(channel, deadline_s=GUARD_S).deliver(_attempt())

    assert excinfo.value.reason is Reason.PEER_PROTOCOL_ERROR
    assert isinstance(excinfo.value.__cause__, ProtoMappingError)


@pytest.mark.asyncio
async def test_a_valid_scripted_answer_decodes():
    """The scripted peer itself is sound: a real verdict through it is an ordinary answer."""

    async def answering(requests: AsyncIterator[Message], context: grpc.aio.ServicerContext) -> Message:
        async for _ in requests:
            pass
        return response_to_proto(a_response(domain.DeliveryVerdict.CONTINUE, count=9))

    async with hosted(scripted_registration(answering)) as channel:
        answer = await GrpcStoreDeliveryClient(channel, deadline_s=GUARD_S).deliver(_attempt())

    assert answer == a_response(domain.DeliveryVerdict.CONTINUE, count=9)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'unmappable',
    [
        lambda: an_attempt(header=a_header().model_copy(update={'attempt_no': 2**32})),
        lambda: an_attempt(domain.AssetRecords(records=[an_asset(0, payload_bytes=5 * 1024 * 1024)])),
    ],
    ids=['attempt_no past uint32', 'a record over the guard'],
)
async def test_an_unmappable_attempt_raises_proto_mapping_error_before_any_call(unmappable):
    """Our bug, not the peer's: never PEER_PROTOCOL_ERROR, and the store sees nothing.

    Args:
        unmappable: Builds an attempt the encoder refuses.
    """
    handler = Handler()

    async with store_peer(handler) as channel:
        with pytest.raises(ProtoMappingError) as excinfo:
            await GrpcStoreDeliveryClient(channel, deadline_s=GUARD_S).deliver(unmappable())

    assert not isinstance(excinfo.value, TraderJoeError)
    assert handler.delivered == []


@pytest.mark.asyncio
@pytest.mark.parametrize('call', ['deliver', 'register_instance'])
async def test_a_dead_peer_fails_at_the_deadline_rather_than_waiting_without_bound(call: str):
    """wait_for_ready plus a finite deadline: a store that never comes up ends as DEADLINE.

    Args:
        call: Which client method.
    """
    channel = grpc.aio.insecure_channel(UNREACHABLE)
    try:
        client = GrpcStoreDeliveryClient(channel, deadline_s=0.3, register_deadline_s=0.3)
        with pytest.raises(TraderJoeError) as excinfo:
            await asyncio.wait_for(
                client.deliver(_attempt()) if call == 'deliver' else client.register_instance(a_registration()), GUARD_S
            )
    finally:
        await channel.close()

    assert excinfo.value.reason is Reason.DEADLINE


# ---------------------------------------------------------------------------------------------
# REDELIVERY: the same attempt, the same key, the stored answer


@pytest.mark.asyncio
async def test_a_redelivered_attempt_is_answered_from_the_stored_row_and_writes_nothing():
    """The caller's recovery: the SAME AttemptDelivery again. Same messages, same key, the same answer."""
    handler = Handler(answer=a_response(domain.DeliveryVerdict.CONTINUE, count=3))
    attempt = _attempt()

    async with store_peer(handler) as channel:
        client = GrpcStoreDeliveryClient(channel, deadline_s=GUARD_S)
        first = await client.deliver(attempt)
        second = await client.deliver(attempt)

    assert first == second == a_response(domain.DeliveryVerdict.CONTINUE, count=3)
    assert len(handler.delivered) == 2
    assert handler.delivered[0] == handler.delivered[1]
    assert list(handler.written) == [('req-7', 3)], 'the duplicate was written as a new attempt'
    assert attempt_to_messages(attempt) == attempt_to_messages(attempt)


# ---------------------------------------------------------------------------------------------
# RegisterInstance


@pytest.mark.asyncio
async def test_register_instance_reaches_the_store_and_returns_the_empty_answer():
    """The announcement crosses in domain terms."""
    handler = Handler()

    async with store_peer(handler) as channel:
        answer = await GrpcStoreDeliveryClient(channel, deadline_s=GUARD_S).register_instance(a_registration())

    assert answer == domain.RegisterInstanceResponse()
    assert handler.registered == [a_registration()]


@pytest.mark.asyncio
async def test_a_register_error_from_the_store_arrives_typed():
    """The same reading of a failed call as deliver's."""
    handler = Handler(raising=error_for(Reason.VENDOR_UNAVAILABLE, 'down'))

    async with store_peer(handler) as channel:
        with pytest.raises(TraderJoeError) as excinfo:
            await GrpcStoreDeliveryClient(channel, deadline_s=GUARD_S).register_instance(a_registration())

    assert excinfo.value.reason is Reason.VENDOR_UNAVAILABLE
    assert len(handler.registered) == 1


# ---------------------------------------------------------------------------------------------
# THE DEADLINE RULE


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['deadline_s', 'register_deadline_s'])
@pytest.mark.parametrize('deadline', [0.0, -1.0, math.inf, math.nan], ids=['zero', 'negative', 'inf', 'nan'])
async def test_a_deadline_that_is_not_finite_and_positive_is_refused_at_construction(name: str, deadline: float):
    """Refused when the client is BUILT, not on the first call.

    Args:
        name: Which deadline.
        deadline: The rejected value.
    """
    channel = grpc.aio.insecure_channel(UNREACHABLE)
    try:
        with pytest.raises(ValueError, match='finite, positive'):
            GrpcStoreDeliveryClient(channel, **{name: deadline})
    finally:
        await channel.close()


class _Call:
    """Stands in for a grpc.aio stream-unary call: awaitable once, cancellable."""

    def __init__(self, response: Message) -> None:
        self._response = response
        self.cancelled = False

    def __await__(self):
        async def answer() -> Message:
            return self._response

        return answer().__await__()

    def cancel(self) -> bool:
        self.cancelled = True
        return True


class _RecordingStub:
    """Stands in for the generated StoreServiceStub, recording each call's options and requests."""

    def __init__(self, channel: grpc.aio.Channel) -> None:
        self.channel = channel
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.sent: list[Message] = []
        self.deliver_calls: list[_Call] = []

    def DeliverAttempt(self, requests: Iterator[Message], **options: object) -> _Call:
        self.calls.append(('DeliverAttempt', options))
        self.sent.extend(requests)
        self.deliver_calls.append(_Call(response_to_proto(a_response())))
        return self.deliver_calls[-1]

    async def RegisterInstance(self, message: Message, **options: object) -> Message:
        self.calls.append(('RegisterInstance', options))
        return RegisterResponseMessage()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('given', 'expected'),
    [
        ({}, (DEFAULT_DELIVERY_DEADLINE_S, DEFAULT_REGISTER_DEADLINE_S)),
        ({'deadline_s': 2.5, 'register_deadline_s': 1.5}, (2.5, 1.5)),
    ],
    ids=['default', 'set'],
)
async def test_each_call_carries_its_deadline_with_wait_for_ready(monkeypatch, given, expected):
    """The acceptance criterion on the wire call itself: a finite timeout and wait_for_ready, per call.

    Args:
        monkeypatch: Swaps the stub module the client module holds.
        given: The deadlines passed at construction.
        expected: (deliver timeout, register timeout).
    """
    stubs: list[_RecordingStub] = []

    def make_stub(channel: grpc.aio.Channel) -> _RecordingStub:
        stubs.append(_RecordingStub(channel))
        return stubs[-1]

    monkeypatch.setattr(store_delivery, 'store_pb2_grpc', types.SimpleNamespace(StoreServiceStub=make_stub))
    channel = grpc.aio.insecure_channel(UNREACHABLE)
    try:
        client = GrpcStoreDeliveryClient(channel, **given)
        await client.deliver(_attempt())
        await client.register_instance(a_registration())
    finally:
        await channel.close()

    (stub,) = stubs
    assert stub.channel is channel
    assert client.deadline_s == expected[0]
    assert stub.calls == [
        ('DeliverAttempt', {'timeout': expected[0], 'wait_for_ready': True}),
        ('RegisterInstance', {'timeout': expected[1], 'wait_for_ready': True}),
    ]
    assert stub.sent == attempt_to_messages(_attempt()), 'one call carried the whole attempt, in order'
    assert stub.deliver_calls[0].cancelled, 'the finished call is released'


def test_the_default_deadlines_are_finite_bounds():
    """A minute for the attempt report, the unary 10 s for the announcement."""
    assert DEFAULT_DELIVERY_DEADLINE_S == 60.0
    assert DEFAULT_REGISTER_DEADLINE_S == 10.0


@pytest.mark.parametrize('method', ['deliver', 'register_instance'])
def test_the_grpc_client_matches_the_interface_a_test_double_implements(method: str):
    """StoreDeliveryClient is a Protocol checked at runtime by nothing; the signatures are compared here.

    Args:
        method: The method compared.
    """
    declared = inspect.signature(getattr(StoreDeliveryClient, method))
    implemented = inspect.signature(getattr(GrpcStoreDeliveryClient, method))

    assert list(declared.parameters) == list(implemented.parameters)
    for name in declared.parameters:
        assert declared.parameters[name].annotation == implemented.parameters[name].annotation
    assert declared.return_annotation == implemented.return_annotation
    assert inspect.iscoroutinefunction(getattr(GrpcStoreDeliveryClient, method))
