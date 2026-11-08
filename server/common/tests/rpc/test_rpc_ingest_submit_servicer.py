"""THE IngestServicer's Submit AND Instance: two domain-typed handlers behind the generated messages (tj-7zjov1.48).

The design is tj-7zjov1.48's description and acceptance: IngestServicer serves Submit and Instance through two
STRUCTURAL Protocols in domain terms, the answer to a Submit is encoded under the REQUEST's kind, and a servicer
built without a handler answers UNIMPLEMENTED for that method exactly as before, so existing construction is
unchanged. Plus the P07-gate feed rule (architect note of 08:35 UTC on tj-7zjov1.15), which is what makes the
servicer's one non-obvious duty visible:

* THE SERVICER PASSES THE REQUEST'S KIND to the encoder. Under the bars default an accepted asset-list or
  corporate-actions answer, which carries no feed, fails to encode. So a non-bars acceptance with no feed
  crossing the wire cleanly is the proof the kind was passed.
* A handler answer that breaks the feed rule is OUR bug, raised as ProtoMappingError; the error boundary
  answers INTERNAL carrying an error_id and nothing else. Never a silent encode.
* A request with no kind cannot be decoded; it is answered the same way and never reaches the handler.
* A TraderJoeError the handler raises is rendered by the boundary as its own status.
* FetchDataset, the PR 4 pull stream, is NOT a method (P37, tj-7zjov1.280): the servicer has no such attribute,
  and a call to its path is answered UNIMPLEMENTED by gRPC itself, never INTERNAL.

EVERYTHING HERE RUNS OVER A REAL grpc.aio SERVER WITH THE ERROR BOUNDARY, registered through the production
``ingest_service()``, because GrpcServerHost installs the boundary in every server
(tj-19r2z5) and every claim below is about what reaches the wire. The doubles are plain classes and inherit
nothing (decision tj-tkm4tn D1): builder-ingest's handlers will conform by shape, so a double that subclassed
the Protocol would prove the opposite of the rule.

Nothing here imports ``trader_joe.proto`` (TID251): wire messages come from ``fetch_contract.message_class``.
"""

import contextlib
import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import grpc
import pytest
from google.protobuf.message import Message

from common.enums.data_stock import Feed
from common.errors.vocabulary import Reason, TraderJoeError
from common.rpc.clients.ingest_submit import GrpcIngestSubmitClient
from common.rpc.errors import ErrorBoundaryInterceptor
from common.rpc.ingest import SERVICE_NAME, IngestServicer, InstanceHandler, SubmitHandler, ingest_service
from common.rpc.mapping import submit_request_to_proto
from common.rpc.server import ServiceRegistration
from common.tests.rpc.errors_harness import error_for
from common.tests.rpc.fetch_contract import GUARD_S, LOOPBACK, message_class
from common.tests.rpc.submit_contract import KINDS, NON_BARS, SubmitResponseMessage, a_submit, accepted, refused
from schemas.data_ingest.submit import InstanceResponse, RequestKind, SubmitRequest, SubmitResponse


pytestmark = pytest.mark.common

STARTED = datetime(2026, 3, 2, 13, 0, tzinfo=UTC)

# The fixed message a bug's INTERNAL status carries, bar the id (common/rpc/errors.py). Spelled out here, not
# imported: the module keeps it private, and this file asserts the wire, not the module's own constant.
INTERNAL_PREFIX = 'internal error; error_id '

InstanceRequestMessage = message_class('InstanceRequest')
InstanceResponseMessage = message_class('InstanceResponse')

#: Per method: the request a raw call sends and the class its answer is read with.
RAW = {
    'Submit': (lambda: submit_request_to_proto(a_submit()), SubmitResponseMessage),
    'Instance': (InstanceRequestMessage, InstanceResponseMessage),
}


# ---------------------------------------------------------------------------------------------
# THE DOUBLES. Plain classes, no base: a SubmitHandler and an InstanceHandler by shape.


class _Submit:
    """A SubmitHandler by shape, recording each decoded request and answering as scripted."""

    def __init__(self, answer: SubmitResponse | None = None, *, raising: BaseException | None = None) -> None:
        self._answer = answer
        self._raising = raising
        self.seen: list[SubmitRequest] = []

    async def submit(self, request: SubmitRequest) -> SubmitResponse:
        """Record the request, then raise or answer.

        Args:
            request: The decoded request.

        Returns:
            SubmitResponse: The scripted answer.

        Raises:
            BaseException: The scripted error, if any.
        """
        self.seen.append(request)
        if self._raising is not None:
            raise self._raising
        assert self._answer is not None
        return self._answer


class _Instance:
    """An InstanceHandler by shape."""

    def __init__(self, answer: InstanceResponse | None = None, *, raising: BaseException | None = None) -> None:
        self._answer = answer
        self._raising = raising
        self.calls = 0

    async def instance(self) -> InstanceResponse:
        """Raise or answer.

        Returns:
            InstanceResponse: The scripted answer.

        Raises:
            BaseException: The scripted error, if any.
        """
        self.calls += 1
        if self._raising is not None:
            raise self._raising
        assert self._answer is not None
        return self._answer


def an_instance() -> InstanceResponse:
    """Returns: InstanceResponse: ingest-7, started at STARTED."""
    return InstanceResponse(instance_id='ingest-7', started_at=STARTED)


def an_answer_for(kind: RequestKind) -> SubmitResponse:
    """A legal acceptance for KIND: a feed on bars, none otherwise.

    Args:
        kind: The request's kind.

    Returns:
        SubmitResponse: The answer.
    """
    return accepted(Feed.IEX if kind is RequestKind.BARS else None)


@contextlib.asynccontextmanager
async def served(registration: ServiceRegistration) -> AsyncIterator[grpc.aio.Channel]:
    """Run a real server holding REGISTRATION behind the error boundary, and yield a channel to it.

    Args:
        registration: Built by the production ingest_service().

    Yields:
        grpc.aio.Channel: A channel to the server. Both are torn down on the way out.
    """
    server = grpc.aio.server(interceptors=[ErrorBoundaryInterceptor()])
    registration.add_to_server(server)
    port = server.add_insecure_port(f'{LOOPBACK}:0')
    assert port != 0, 'the servicer could not bind an ephemeral loopback port'
    await server.start()
    channel = grpc.aio.insecure_channel(f'{LOOPBACK}:{port}')
    try:
        yield channel
    finally:
        await channel.close()
        await server.stop(None)


async def raw_call(channel: grpc.aio.Channel, method: str, request: Message | None = None) -> Message:
    """Call METHOD with no client seam in the way, so the status itself is what a test sees.

    Args:
        channel: The channel to the servicer.
        method: Submit or Instance.
        request: The wire request; defaults to an ordinary one for the method.

    Returns:
        Message: The answer.

    Raises:
        grpc.aio.AioRpcError: The status, when the call failed.
    """
    build, answer_class = RAW[method]
    call = channel.unary_unary(
        f'/{SERVICE_NAME}/{method}',
        request_serializer=lambda message: message.SerializeToString(),
        response_deserializer=answer_class.FromString,
    )
    return await call(request if request is not None else build(), timeout=GUARD_S)


def assert_internal_with_an_error_id(error: grpc.aio.AioRpcError) -> None:
    """The boundary's answer to a bug: INTERNAL, an error_id, and nothing else.

    Args:
        error: The failed call.
    """
    assert error.code() is grpc.StatusCode.INTERNAL
    details = error.details() or ''
    assert details.startswith(INTERNAL_PREFIX), details
    assert details.removeprefix(INTERNAL_PREFIX).strip(), 'the INTERNAL answer must name an error_id'


# ---------------------------------------------------------------------------------------------
# NO HANDLER: UNIMPLEMENTED, for every way the servicer is built


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['Submit', 'Instance'])
@pytest.mark.parametrize(
    'build',
    [lambda: ingest_service(), lambda: ingest_service(submit=None, instance=None)],
    ids=['ingest_service', 'ingest_service-explicit-none'],
)
async def test_a_servicer_built_without_the_handler_answers_unimplemented(method: str, build):
    """A method whose handler is not bound is unserved, not broken.

    Over a real server WITH the boundary, because the boundary turns any escaping exception into INTERNAL;
    UNIMPLEMENTED reaching the caller is the claim that the method is unserved rather than broken. This is
    exactly what falling back to the generated base method got wrong: it sets UNIMPLEMENTED and then raises
    NotImplementedError, which the boundary answered INTERNAL with an error_id (measured at b92efb8 and its
    parent; fixed in f4dffcb by aborting instead).

    Args:
        method: Submit or Instance.
        build: How the registration is made.
    """
    async with served(build()) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await raw_call(channel, method)

    assert excinfo.value.code() is grpc.StatusCode.UNIMPLEMENTED


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['submit', 'instance'])
async def test_the_store_client_reads_a_missing_handler_as_unimplemented_not_as_the_peers_bug(method: str):
    """What the store sees through GrpcIngestSubmitClient against a servicer with no handler bound.

    PEER_PROTOCOL_ERROR with the UNIMPLEMENTED status as cause, never PEER_INTERNAL carrying an error_id.

    Args:
        method: Which client call.
    """
    async with served(ingest_service()) as channel:
        client = GrpcIngestSubmitClient(channel, deadline_s=GUARD_S)
        with pytest.raises(TraderJoeError) as excinfo:
            await (client.submit(a_submit()) if method == 'submit' else client.instance())

    assert excinfo.value.reason is Reason.PEER_PROTOCOL_ERROR
    assert isinstance(excinfo.value.__cause__, grpc.aio.AioRpcError)
    assert excinfo.value.__cause__.code() is grpc.StatusCode.UNIMPLEMENTED


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['Submit', 'Instance'])
async def test_a_servicer_without_the_handler_never_answers_ok(method: str):
    """No handler is a FAILED call, never an empty answer read as success, whatever the status.

    An empty SubmitResponse would decode as a protocol error, but an empty-looking success is still the wrong
    thing for an unserved method to send. The status itself is pinned by the UNIMPLEMENTED cases above.

    Args:
        method: Submit or Instance.
    """
    async with served(ingest_service()) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await raw_call(channel, method)

    assert excinfo.value.code() is not grpc.StatusCode.OK


@pytest.mark.asyncio
@pytest.mark.parametrize('bound', ['submit', 'instance'])
async def test_binding_one_handler_leaves_the_other_method_unimplemented(bound: str):
    """The two handlers are independent: Instance is not served because Submit is, nor the reverse.

    Args:
        bound: Which handler is given.
    """
    submit = _Submit(accepted(Feed.IEX)) if bound == 'submit' else None
    instance = _Instance(an_instance()) if bound == 'instance' else None
    other = 'Instance' if bound == 'submit' else 'Submit'

    async with served(ingest_service(submit=submit, instance=instance)) as channel:
        await raw_call(channel, 'Submit' if bound == 'submit' else 'Instance')
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await raw_call(channel, other)

    assert excinfo.value.code() is grpc.StatusCode.UNIMPLEMENTED


# ---------------------------------------------------------------------------------------------
# FETCHDATASET IS NOT A METHOD (P37, tj-7zjov1.280): the pull stream is gone; Submit and Instance are untouched


async def raw_fetch(channel: grpc.aio.Channel) -> list[bytes]:
    """Call the old FetchDataset path with an empty body and read the stream to its end, no client seam in the way.

    The path is spelled out, not read off the descriptor: the descriptor no longer declares the method, which is
    the point. A server that still registered a handler under it would answer something other than UNIMPLEMENTED.

    Args:
        channel: The channel to the servicer.

    Returns:
        list[bytes]: Every message, raw.

    Raises:
        grpc.aio.AioRpcError: The status, when the call failed.
    """
    call = channel.unary_stream(
        f'/{SERVICE_NAME}/FetchDataset', request_serializer=lambda body: body, response_deserializer=lambda body: body
    )
    return [message async for message in call(b'', timeout=GUARD_S)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'build',
    [
        lambda: ingest_service(),
        lambda: ingest_service(submit=_Submit(accepted(Feed.IEX)), instance=_Instance(an_instance())),
    ],
    ids=['ingest_service', 'ingest_service-submit-and-instance'],
)
async def test_the_old_fetch_dataset_path_is_answered_unimplemented_by_every_servicer(build):
    """UNIMPLEMENTED reaching the caller through the boundary, never INTERNAL and never an empty OK stream.

    With the method gone the servicer registers no handler under the path, so gRPC itself answers it: a peer still
    built against the PR 4 contract fails loudly and typed, not as the store's bug.

    Args:
        build: How the registration is made.
    """
    async with served(build()) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await raw_fetch(channel)

    assert excinfo.value.code() is grpc.StatusCode.UNIMPLEMENTED, excinfo.value.details()
    assert not (excinfo.value.details() or '').startswith(INTERNAL_PREFIX)


def test_fetch_dataset_is_not_a_method_of_the_servicer_nor_of_its_generated_base():
    """The servicer and the generated base it inherits declare Submit, Instance and Quote, and no FetchDataset.

    hasattr walks the MRO, so a method left on IngestServicer, or a FetchDataset regenerated into the base from a
    .proto that still declared the rpc, reds this. The descriptor pin is in test_proto_internal_contracts.py.
    """
    assert not hasattr(IngestServicer, 'FetchDataset')
    for method in ('Submit', 'Instance', 'Quote'):
        assert hasattr(IngestServicer, method), method


@pytest.mark.asyncio
async def test_submit_and_instance_are_served_with_both_handlers_bound():
    """The servicer ingest registers since tj-7zjov1.275, both handlers bound: both methods must work."""
    submit = _Submit(accepted(Feed.IEX))
    instance = _Instance(an_instance())

    async with served(ingest_service(submit=submit, instance=instance)) as channel:
        client = GrpcIngestSubmitClient(channel, deadline_s=GUARD_S)
        answer = await client.submit(a_submit())
        identity = await client.instance()

    assert answer == accepted(Feed.IEX)
    assert submit.seen == [a_submit()]
    assert identity == an_instance()
    assert instance.calls == 1


def test_an_ingest_servicer_constructs_with_no_handler_at_all():
    """IngestServicer() is legal: both handlers are optional, and neither is required to build the service."""
    servicer = IngestServicer()

    assert isinstance(servicer, IngestServicer)
    assert ingest_service().name == SERVICE_NAME


# ---------------------------------------------------------------------------------------------
# SUBMIT: decoded in, encoded under the request's kind out -- end to end with the real client


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', list(KINDS), ids=lambda kind: kind.value)
async def test_a_submit_of_each_kind_reaches_the_handler_decoded_and_its_answer_crosses_back(kind: RequestKind):
    """The end-to-end pair: GrpcIngestSubmitClient against IngestServicer, the handler a plain fake.

    The handler sees the DOMAIN request, equal to what the client sent. For the two non-bars kinds the answer
    is an acceptance with NO FEED, which fails to encode under the bars default -- so it arriving whole is the
    proof the servicer passed the request's kind to the encoder.

    Args:
        kind: The request's kind.
    """
    request = a_submit(**KINDS[kind])
    answer = an_answer_for(kind)
    handler = _Submit(answer)

    async with served(ingest_service(submit=handler)) as channel:
        result = await GrpcIngestSubmitClient(channel, deadline_s=GUARD_S).submit(request)

    assert result == answer
    (seen,) = handler.seen
    assert isinstance(seen, SubmitRequest)
    assert seen == request
    assert seen.kind is kind


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', NON_BARS, ids=lambda kind: kind.value)
async def test_a_non_bars_acceptance_goes_on_the_wire_with_the_zero_feed(kind: RequestKind):
    """The wire half of the kind proof, read raw: the ack is accepted, its feed is FEED_UNSPECIFIED (0).

    Args:
        kind: asset_list or corporate_actions.
    """
    handler = _Submit(accepted(None))

    async with served(ingest_service(submit=handler)) as channel:
        answer = await raw_call(channel, 'Submit', submit_request_to_proto(a_submit(**KINDS[kind])))

    assert answer.ack.WhichOneof('outcome') == 'accepted'
    assert answer.ack.accepted.feed == 0
    assert answer.ack.accepted.quota_key == 'alpaca-a'
    assert answer.instance_id == 'ingest-1'


@pytest.mark.asyncio
async def test_a_bars_refusal_crosses_as_an_answer_and_not_as_a_status():
    """A refused ack is a legal bars answer; the call ends OK and the client returns it."""
    handler = _Submit(refused())

    async with served(ingest_service(submit=handler)) as channel:
        result = await GrpcIngestSubmitClient(channel, deadline_s=GUARD_S).submit(a_submit())

    assert result == refused()


# ---------------------------------------------------------------------------------------------
# SUBMIT: what the servicer must NOT put on the wire


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('kind', 'answer'),
    [
        (RequestKind.BARS, accepted(None)),
        (RequestKind.ASSET_LIST, accepted(Feed.IEX)),
        (RequestKind.CORPORATE_ACTIONS, accepted(Feed.IEX)),
        (RequestKind.ASSET_LIST, refused()),
        (RequestKind.CORPORATE_ACTIONS, refused()),
    ],
    ids=['bars-no-feed', 'asset_list-with-feed', 'corporate_actions-with-feed', 'asset_list-refused', 'ca-refused'],
)
async def test_a_handler_answer_that_breaks_the_feed_rule_is_internal_with_an_error_id(
    kind: RequestKind, answer: SubmitResponse
):
    """Not a silent encode: the boundary answers INTERNAL with an error_id, and the client reads PEER_INTERNAL.

    The handler WAS called -- the failure is the answer's encoding, not the request's decoding.

    Args:
        kind: The request's kind.
        answer: The rule-breaking answer.
    """
    handler = _Submit(answer)
    request = a_submit(**KINDS[kind])

    async with served(ingest_service(submit=handler)) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await raw_call(channel, 'Submit', submit_request_to_proto(request))
        with pytest.raises(TraderJoeError) as through_client:
            await GrpcIngestSubmitClient(channel, deadline_s=GUARD_S).submit(request)

    assert_internal_with_an_error_id(excinfo.value)
    assert through_client.value.reason is Reason.PEER_INTERNAL
    assert through_client.value.metadata['error_id']
    assert handler.seen == [request, request]


@pytest.mark.asyncio
async def test_a_request_with_no_kind_is_refused_and_never_reaches_the_handler():
    """An unset oneof cannot be decoded; the boundary answers INTERNAL with an error_id and the handler is idle."""
    message = submit_request_to_proto(a_submit())
    message.ClearField('bars')
    assert message.WhichOneof('kind') is None
    handler = _Submit(accepted(Feed.IEX))

    async with served(ingest_service(submit=handler)) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await raw_call(channel, 'Submit', message)

    assert_internal_with_an_error_id(excinfo.value)
    assert handler.seen == []


# ---------------------------------------------------------------------------------------------
# THE HANDLER'S OWN ERRORS go through the usual path


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['submit', 'instance'])
@pytest.mark.parametrize('reason', [Reason.VENDOR_UNAVAILABLE, Reason.OVERRIDE_REQUIRED])
async def test_a_trader_joe_error_from_a_handler_reaches_the_caller_as_the_same_reason(method: str, reason: Reason):
    """The boundary renders a raised TraderJoeError as its status and the client reads the reason back.

    Args:
        method: Which handler raises.
        reason: The reason raised.
    """
    raised = error_for(reason, 'the handler failed')
    submit = _Submit(raising=raised)
    instance = _Instance(raising=raised)

    async with served(ingest_service(submit=submit, instance=instance)) as channel:
        client = GrpcIngestSubmitClient(channel, deadline_s=GUARD_S)
        with pytest.raises(TraderJoeError) as excinfo:
            await (client.submit(a_submit()) if method == 'submit' else client.instance())

    assert excinfo.value.reason is reason
    assert excinfo.value.reason is not Reason.PEER_INTERNAL


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['Submit', 'Instance'])
async def test_any_other_exception_from_a_handler_is_internal_with_an_error_id(method: str):
    """A bug in the handler is the boundary's INTERNAL, never a typed reason and never silence.

    Args:
        method: Which handler raises.
    """
    submit = _Submit(raising=RuntimeError('boom'))
    instance = _Instance(raising=RuntimeError('boom'))

    async with served(ingest_service(submit=submit, instance=instance)) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await raw_call(channel, method)

    assert_internal_with_an_error_id(excinfo.value)


# ---------------------------------------------------------------------------------------------
# INSTANCE


@pytest.mark.asyncio
async def test_the_instance_probe_round_trips_the_instance_id_and_started_at():
    """End to end through the real client: the handler's answer arrives equal, aware UTC."""
    handler = _Instance(an_instance())

    async with served(ingest_service(instance=handler)) as channel:
        result = await GrpcIngestSubmitClient(channel, deadline_s=GUARD_S).instance()

    assert result == an_instance()
    assert result.started_at.utcoffset() is not None
    assert handler.calls == 1


@pytest.mark.asyncio
async def test_the_instance_answer_is_on_the_wire_as_the_handler_gave_it():
    """Read raw, so a client that papered over a wrong field could not hide it."""
    async with served(ingest_service(instance=_Instance(an_instance()))) as channel:
        answer = await raw_call(channel, 'Instance')

    assert answer.instance_id == 'ingest-7'
    assert answer.started_at.ToDatetime(UTC) == STARTED


# ---------------------------------------------------------------------------------------------
# THE REGISTRATION HELPER


def test_the_registration_names_the_ingest_service():
    """The health service reports under SERVICE_NAME, which is the descriptor's own IngestService name."""
    registration = ingest_service()

    assert isinstance(registration, ServiceRegistration)
    assert registration.name == SERVICE_NAME
    assert SERVICE_NAME == 'trader_joe.proto.internal.ingest.v1.IngestService'


@pytest.mark.parametrize('target', [IngestServicer.__init__, ingest_service], ids=['IngestServicer', 'ingest_service'])
def test_the_two_handlers_are_the_only_arguments_and_are_optional_keyword_only(target):
    """Submit and Instance are the only handlers: the positional fetch handler is gone with FetchDataset (P37).

    A positional parameter left behind would let a caller still hand in a fetch handler that nothing serves.

    Args:
        target: The constructor or the helper.
    """
    parameters = inspect.signature(target).parameters

    assert [name for name in parameters if name != 'self'] == ['submit', 'instance']
    for name in ('submit', 'instance'):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameters[name].default is None, name


@pytest.mark.parametrize('method', ['Submit', 'Instance'])
def test_submit_and_instance_are_coroutines_so_the_boundary_guards_them_as_unary(method: str):
    """common/rpc/errors.py branches on inspect.isasyncgenfunction; a unary method must be a plain coroutine.

    Args:
        method: The servicer method.
    """
    behaviour = getattr(IngestServicer, method)

    assert inspect.iscoroutinefunction(behaviour)
    assert not inspect.isasyncgenfunction(behaviour)


def test_the_handler_seams_are_structural_and_in_domain_terms():
    """Conformance by shape (tj-tkm4tn D1, carried to tj-7zjov1.48), with domain types on both sides.

    The doubles above are served by real servers with no ancestor in common with the Protocols, which is the
    evidence; this pins it, and pins the Protocols as not runtime-checkable, so no isinstance gate appears.
    """
    for double, protocol in ((_Submit, SubmitHandler), (_Instance, InstanceHandler)):
        assert double.__mro__ == (double, object)
        with pytest.raises(TypeError):
            isinstance(double(), protocol)  # type: ignore[misc]

    submit = inspect.signature(SubmitHandler.submit)
    assert list(submit.parameters) == ['self', 'request']
    assert submit.parameters['request'].annotation is SubmitRequest
    assert submit.return_annotation is SubmitResponse
    assert inspect.iscoroutinefunction(SubmitHandler.submit)

    instance = inspect.signature(InstanceHandler.instance)
    assert list(instance.parameters) == ['self']
    assert instance.return_annotation is InstanceResponse
    assert inspect.iscoroutinefunction(InstanceHandler.instance)
