"""THE IngestSubmitClient SEAM: Submit and Instance, in domain terms, over a real grpc.aio connection (tj-7zjov1.15).

The design is tj-7zjov1.15's acceptance -- the store calls Submit and Instance through one client with the
existing deadline rule, and the Submit answer maps accepted (resolved feed, quota key, instance id) or
refused -- plus the P07 gate's feed rule, which makes the client's one non-obvious duty visible:

* THE CLIENT ALWAYS PASSES THE REQUEST'S KIND to the answer's mapper. Without it the mapper defaults to bars,
  and every asset-list or corporate-actions acceptance (which carries the zero feed) would be refused. So a
  non-bars acceptance decoding cleanly is the proof the kind was passed.
* An answer that breaks the rule, or will not map, is PEER_PROTOCOL_ERROR with the mapping error as cause.
* A gRPC failure arrives as from_rpc_error reads it; an ingest that does not serve the methods answers
  UNIMPLEMENTED. Production ingest does that until P11 and P36 bind their handlers: IngestServicer built
  without a Submit or Instance handler aborts with UNIMPLEMENTED (pinned over the real servicer in
  test_rpc_ingest_submit_servicer.py).
* A finite, configurable deadline bounds both calls (unary_call_options: timeout plus wait_for_ready), and a
  bad one is refused at construction.
* A request this service cannot encode is OUR bug, raised as ProtoMappingError, and never reaches the peer.

The peer is built from the contract's descriptors (``fetch_contract.message_class``), never from a generated
stub: TID251 keeps ``trader_joe.proto`` out of tests. The call-options check swaps the stub module the client
module holds for a recording fake, so no generated code is imported here either.
"""

import contextlib
import inspect
import math
import types
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime

import grpc
import pytest
from google.protobuf.message import Message

import common.rpc.clients.ingest_submit as ingest_submit
from common.enums.data_stock import Feed
from common.errors.vocabulary import Reason, TraderJoeError
from common.rpc.clients.ingest_submit import DEFAULT_SUBMIT_DEADLINE_S, GrpcIngestSubmitClient, IngestSubmitClient
from common.rpc.errors import ErrorBoundaryInterceptor
from common.rpc.mapping import (
    CONTRACT_FILES,
    ProtoMappingError,
    instance_response_to_proto,
    submit_request_to_domain,
    submit_response_to_proto,
)
from common.tests.rpc.errors_harness import error_for
from common.tests.rpc.fetch_contract import GUARD_S, LOOPBACK, message_class
from common.tests.rpc.submit_contract import (
    KINDS,
    NON_BARS,
    SubmitResponseMessage,
    a_submit,
    accepted,
    feed_number,
    raw_accepted,
    raw_refused,
    refused,
)
from schemas.data_ingest.submit import InstanceResponse, RequestKind


pytestmark = pytest.mark.common

UNREACHABLE = '127.0.0.1:1'
STARTED = datetime(2026, 3, 2, 13, 0, tzinfo=UTC)

_SERVICE = next(file for file in CONTRACT_FILES if file.name.endswith('ingest/v1/ingest.proto')).services_by_name[
    'IngestService'
]
SubmitRequestMessage = message_class('SubmitRequest')
InstanceRequestMessage = message_class('InstanceRequest')
InstanceResponseMessage = message_class('InstanceResponse')

type UnaryPeer = Callable[[Message, grpc.aio.ServicerContext], Awaitable[Message]]


@contextlib.asynccontextmanager
async def ingest_server(
    *, submit: UnaryPeer | None = None, instance: UnaryPeer | None = None
) -> AsyncIterator[grpc.aio.Channel]:
    """An IngestService peer serving whichever of Submit and Instance is given, and nothing else.

    Neither given is a peer that registers no handler for either method, so gRPC itself answers
    UNIMPLEMENTED. That is the same status production ingest sends for a method it has no handler for,
    reached by a different route: there the method IS registered and IngestServicer aborts with it.

    Args:
        submit: What Submit does with the decoded request.
        instance: What Instance does with the decoded probe.

    Yields:
        grpc.aio.Channel: A channel to the running peer; both are torn down on the way out.
    """
    server = grpc.aio.server(interceptors=[ErrorBoundaryInterceptor()])
    handlers = {}
    if submit is not None:
        handlers['Submit'] = grpc.unary_unary_rpc_method_handler(
            submit,
            request_deserializer=SubmitRequestMessage.FromString,
            response_serializer=lambda response: response.SerializeToString(),
        )
    if instance is not None:
        handlers['Instance'] = grpc.unary_unary_rpc_method_handler(
            instance,
            request_deserializer=InstanceRequestMessage.FromString,
            response_serializer=lambda response: response.SerializeToString(),
        )
    if handlers:
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(_SERVICE.full_name, handlers),))
    port = server.add_insecure_port(f'{LOOPBACK}:0')
    assert port != 0
    await server.start()
    channel = grpc.aio.insecure_channel(f'{LOOPBACK}:{port}')
    try:
        yield channel
    finally:
        await channel.close()
        await server.stop(None)


def answering(response: Message, seen: list[Message] | None = None) -> UnaryPeer:
    """A peer that records what it was sent and answers with RESPONSE.

    Args:
        response: The wire answer, valid or not.
        seen: Where to record each request.

    Returns:
        UnaryPeer: The behaviour.
    """

    async def peer(request: Message, context: grpc.aio.ServicerContext) -> Message:
        if seen is not None:
            seen.append(request)
        return response

    return peer


def an_instance_answer() -> Message:
    """Returns: Message: A valid InstanceResponse, built by the production encoder."""
    return instance_response_to_proto(InstanceResponse(instance_id='ingest-1', started_at=STARTED))


async def _submit_failure(response: Message, kind: RequestKind = RequestKind.BARS) -> TraderJoeError:
    request = a_submit(**KINDS[kind])
    async with ingest_server(submit=answering(response)) as channel:
        with pytest.raises(TraderJoeError) as excinfo:
            await GrpcIngestSubmitClient(channel, deadline_s=GUARD_S).submit(request)
    return excinfo.value


# ---------------------------------------------------------------------------------------------
# SUBMIT: the happy paths, and the kind reaching the answer's mapper


@pytest.mark.asyncio
async def test_a_bars_submit_reaches_the_peer_whole_and_its_acceptance_comes_back_with_the_feed():
    """The happy path, by value both ways: the request decodes back to itself at the peer."""
    seen: list[Message] = []
    request = a_submit()

    async with ingest_server(submit=answering(submit_response_to_proto(accepted(Feed.SIP)), seen)) as channel:
        result = await GrpcIngestSubmitClient(channel, deadline_s=GUARD_S).submit(request)

    assert result == accepted(Feed.SIP)
    (sent,) = seen
    assert submit_request_to_domain(sent) == request


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', NON_BARS, ids=lambda kind: kind.value)
async def test_a_non_bars_acceptance_with_no_feed_decodes_because_the_client_passes_the_kind(kind: RequestKind):
    """THE KIND IS PASSED. Under the bars default this exact answer is refused for its zero feed.

    Args:
        kind: asset_list or corporate_actions.
    """
    seen: list[Message] = []
    request = a_submit(**KINDS[kind])

    async with ingest_server(submit=answering(raw_accepted(0), seen)) as channel:
        result = await GrpcIngestSubmitClient(channel, deadline_s=GUARD_S).submit(request)

    assert result == accepted(None)
    (sent,) = seen
    assert sent.WhichOneof('kind') == kind.value


@pytest.mark.asyncio
async def test_a_bars_refusal_is_returned_as_an_answer_and_not_raised():
    """Turning a refusal into an error is the caller's decision (the store's Submit caller), not the client's."""
    async with ingest_server(submit=answering(raw_refused())) as channel:
        result = await GrpcIngestSubmitClient(channel, deadline_s=GUARD_S).submit(a_submit())

    assert result == refused()


# ---------------------------------------------------------------------------------------------
# SUBMIT: answers that break the rule or will not map are the PEER's protocol error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('kind', 'response'),
    [
        (RequestKind.BARS, raw_accepted(0)),
        (RequestKind.ASSET_LIST, raw_accepted(feed_number(Feed.IEX))),
        (RequestKind.CORPORATE_ACTIONS, raw_accepted(feed_number(Feed.IEX))),
        (RequestKind.ASSET_LIST, raw_refused()),
        (RequestKind.CORPORATE_ACTIONS, raw_refused()),
        (RequestKind.BARS, SubmitResponseMessage(instance_id='ingest-1')),
    ],
    ids=[
        'bars-zero-feed',
        'asset_list-with-feed',
        'corporate_actions-with-feed',
        'asset_list-refused',
        'corporate_actions-refused',
        'no-ack-arm',
    ],
)
async def test_an_answer_that_breaks_the_feed_rule_is_a_peer_protocol_error(kind: RequestKind, response: Message):
    """The mapping error is kept as the cause and never escapes raw.

    Args:
        kind: The kind submitted.
        response: The rule-breaking answer.
    """
    error = await _submit_failure(response, kind)

    assert error.reason is Reason.PEER_PROTOCOL_ERROR
    assert isinstance(error.__cause__, ProtoMappingError)


@pytest.mark.asyncio
async def test_an_answer_with_an_empty_instance_id_is_a_peer_protocol_error():
    """The store learns of a restart from the id; an answer without one cannot be read."""
    response = raw_accepted(feed_number(Feed.IEX))
    response.instance_id = ''

    error = await _submit_failure(response)

    assert error.reason is Reason.PEER_PROTOCOL_ERROR
    assert isinstance(error.__cause__, ProtoMappingError)


# ---------------------------------------------------------------------------------------------
# gRPC FAILURES, for both calls


@pytest.mark.asyncio
@pytest.mark.parametrize('call', ['submit', 'instance'])
async def test_an_ingest_without_the_method_answers_a_typed_error_never_a_raw_grpc_one(call: str):
    """An ingest that serves neither method, as production ingest does until P11 and P36 bind their handlers.

    The peer here registers no handler at all; the real servicer's own UNIMPLEMENTED, through this same
    client, is pinned in test_rpc_ingest_submit_servicer.py.

    Args:
        call: Which client method.
    """
    async with ingest_server() as channel:
        client = GrpcIngestSubmitClient(channel, deadline_s=GUARD_S)
        with pytest.raises(TraderJoeError) as excinfo:
            await (client.submit(a_submit()) if call == 'submit' else client.instance())

    assert excinfo.value.reason is Reason.PEER_PROTOCOL_ERROR
    assert isinstance(excinfo.value.__cause__, grpc.aio.AioRpcError)
    assert excinfo.value.__cause__.code() is grpc.StatusCode.UNIMPLEMENTED


@pytest.mark.asyncio
@pytest.mark.parametrize('call', ['submit', 'instance'])
async def test_a_typed_error_from_the_peer_arrives_as_the_same_reason(call: str):
    """from_rpc_error reads the peer's ErrorInfo back, so the caller sees ingest's reason.

    Args:
        call: Which client method.
    """

    async def failing(request: Message, context: grpc.aio.ServicerContext) -> Message:
        raise error_for(Reason.VENDOR_UNAVAILABLE, 'the vendor is down')

    async with ingest_server(submit=failing, instance=failing) as channel:
        client = GrpcIngestSubmitClient(channel, deadline_s=GUARD_S)
        with pytest.raises(TraderJoeError) as excinfo:
            await (client.submit(a_submit()) if call == 'submit' else client.instance())

    assert excinfo.value.reason is Reason.VENDOR_UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize('call', ['submit', 'instance'])
async def test_an_unreachable_ingest_fails_at_the_deadline_rather_than_waiting_without_bound(call: str):
    """wait_for_ready plus a finite deadline: a peer that never comes up ends as DEADLINE.

    Args:
        call: Which client method.
    """
    channel = grpc.aio.insecure_channel(UNREACHABLE)
    try:
        client = GrpcIngestSubmitClient(channel, deadline_s=0.3)
        with pytest.raises(TraderJoeError) as excinfo:
            await (client.submit(a_submit()) if call == 'submit' else client.instance())
    finally:
        await channel.close()

    assert excinfo.value.reason is Reason.DEADLINE


# ---------------------------------------------------------------------------------------------
# INSTANCE


@pytest.mark.asyncio
async def test_the_instance_probe_sends_nothing_and_its_answer_crosses_in_domain_terms():
    """The probe is empty on the wire; the answer is the instance id and when it started, aware UTC."""
    seen: list[Message] = []

    async with ingest_server(instance=answering(an_instance_answer(), seen)) as channel:
        result = await GrpcIngestSubmitClient(channel, deadline_s=GUARD_S).instance()

    assert result == InstanceResponse(instance_id='ingest-1', started_at=STARTED)
    (sent,) = seen
    assert sent.ListFields() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('broken', ['started_at-unset', 'instance_id-empty'])
async def test_an_instance_answer_this_release_cannot_read_is_a_peer_protocol_error(broken: str):
    """Mapped like Submit's: the mapping error is the cause, PEER_PROTOCOL_ERROR the reason.

    Args:
        broken: What is wrong with the answer.
    """
    response = InstanceResponseMessage(instance_id='ingest-1') if broken == 'started_at-unset' else an_instance_answer()
    if broken == 'instance_id-empty':
        response.instance_id = ''

    async with ingest_server(instance=answering(response)) as channel:
        with pytest.raises(TraderJoeError) as excinfo:
            await GrpcIngestSubmitClient(channel, deadline_s=GUARD_S).instance()

    assert excinfo.value.reason is Reason.PEER_PROTOCOL_ERROR
    assert isinstance(excinfo.value.__cause__, ProtoMappingError)


# ---------------------------------------------------------------------------------------------
# THE DEADLINE RULE


@pytest.mark.asyncio
@pytest.mark.parametrize('deadline', [0.0, -1.0, math.inf, math.nan])
async def test_a_deadline_that_is_not_a_finite_positive_number_is_refused_at_construction(deadline: float):
    """Refused when the client is BUILT, not on the first call.

    Args:
        deadline: The rejected value.
    """
    channel = grpc.aio.insecure_channel(UNREACHABLE)
    try:
        with pytest.raises(ValueError, match='finite, positive'):
            GrpcIngestSubmitClient(channel, deadline_s=deadline)
    finally:
        await channel.close()


class _RecordingStub:
    """Stands in for the generated IngestServiceStub and records the options each call was made with."""

    def __init__(self, channel: grpc.aio.Channel) -> None:
        self.channel = channel
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def Submit(self, message: Message, **options: object) -> Message:
        self.calls.append(('Submit', options))
        return submit_response_to_proto(accepted(Feed.IEX))

    async def Instance(self, message: Message, **options: object) -> Message:
        self.calls.append(('Instance', options))
        return an_instance_answer()


@pytest.mark.asyncio
@pytest.mark.parametrize(('given', 'expected'), [(None, DEFAULT_SUBMIT_DEADLINE_S), (2.5, 2.5)], ids=['default', 'set'])
async def test_both_calls_carry_the_clients_deadline_with_wait_for_ready(monkeypatch, given, expected):
    """The existing deadline rule, on the wire call itself: timeout is the client's deadline, wait_for_ready on.

    The default is used when the builder sets none, and an override is what reaches the call -- not merely
    what the property reports.

    Args:
        monkeypatch: Swaps the stub module the client module holds.
        given: The deadline passed at construction, or None for the default.
        expected: The timeout each call must carry.
    """
    stubs: list[_RecordingStub] = []

    def make_stub(channel: grpc.aio.Channel) -> _RecordingStub:
        stubs.append(_RecordingStub(channel))
        return stubs[-1]

    monkeypatch.setattr(ingest_submit, 'ingest_pb2_grpc', types.SimpleNamespace(IngestServiceStub=make_stub))
    channel = grpc.aio.insecure_channel(UNREACHABLE)
    try:
        client = GrpcIngestSubmitClient(channel) if given is None else GrpcIngestSubmitClient(channel, deadline_s=given)
        await client.submit(a_submit())
        await client.instance()
    finally:
        await channel.close()

    (stub,) = stubs
    assert stub.channel is channel
    assert client.deadline_s == expected
    assert stub.calls == [
        ('Submit', {'timeout': expected, 'wait_for_ready': True}),
        ('Instance', {'timeout': expected, 'wait_for_ready': True}),
    ]


def test_the_default_deadline_is_a_short_finite_bound():
    """Both calls are answered from memory, so the default is the brief's 10 s, not the 300 s fetch bound."""
    assert DEFAULT_SUBMIT_DEADLINE_S == 10.0


# ---------------------------------------------------------------------------------------------
# OUR OWN BUGS, AND THE INTERFACE


@pytest.mark.asyncio
async def test_a_request_this_service_cannot_encode_is_our_bug_and_never_reaches_the_peer():
    """ProtoMappingError, deliberately: the peer never saw it, so PEER_PROTOCOL_ERROR would blame the wrong side."""
    seen: list[Message] = []
    unencodable = a_submit().model_copy(update={'attempt_count': -1})

    async with ingest_server(submit=answering(submit_response_to_proto(accepted(Feed.IEX)), seen)) as channel:
        with pytest.raises(ProtoMappingError) as excinfo:
            await GrpcIngestSubmitClient(channel, deadline_s=GUARD_S).submit(unencodable)

    assert not isinstance(excinfo.value, TraderJoeError)
    assert seen == [], 'nothing reached the peer'


@pytest.mark.parametrize('method', ['submit', 'instance'])
def test_the_grpc_client_matches_the_interface_a_test_double_implements(method: str):
    """IngestSubmitClient is a Protocol, checked at runtime by nothing; the signatures are compared here.

    Args:
        method: The method compared.
    """
    declared = inspect.signature(getattr(IngestSubmitClient, method))
    implemented = inspect.signature(getattr(GrpcIngestSubmitClient, method))

    assert list(declared.parameters) == list(implemented.parameters)
    for name in declared.parameters:
        assert declared.parameters[name].annotation == implemented.parameters[name].annotation
    assert declared.return_annotation == implemented.return_annotation
    assert inspect.iscoroutinefunction(getattr(GrpcIngestSubmitClient, method))
