"""THE IngestQuoteClient SEAM: one unary Quote, in domain terms, over a real grpc.aio connection (tj-grna9p.44).

The design is tj-grna9p.44's architect re-baseline of 2026-10-11, items 2 and 8b, and its acceptance:

* the quote client against an ingest WITHOUT Quote raises a typed TraderJoeError, never a raw grpc error
  -- that is every call until tj-grna9p.51 serves Quote, so it is the behaviour production sees first;
* a served answer crosses as QuoteResult with every scope, an unset reset_at staying None;
* a finite, configurable deadline bounds the call, refused at construction when it is not;
* a request this service cannot encode is OUR bug, raised as ProtoMappingError and not blamed on the peer.

The peer is built from the contract's descriptors (``fetch_contract.message_class``), never from a generated
stub: TID251 keeps ``trader_joe.proto`` out of tests.
"""

import contextlib
import inspect
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime

import grpc
import pytest
from google.protobuf.message import Message

from common.errors.vocabulary import Reason, TraderJoeError
from common.rpc.clients.ingest_quote import DEFAULT_QUOTE_DEADLINE_S, GrpcIngestQuoteClient, IngestQuoteClient
from common.rpc.errors import ErrorBoundaryInterceptor
from common.rpc.mapping import CONTRACT_FILES, ProtoMappingError, request_to_domain
from common.tests.rpc.errors_harness import error_for
from common.tests.rpc.fetch_contract import GUARD_S, LOOPBACK, a_request, message_class
from schemas.data_ingest.fetch_dataset import FetchDatasetRequest
from schemas.data_ingest.quote import QuoteResult, ScopeCapacity


pytestmark = pytest.mark.common

UNREACHABLE = '127.0.0.1:1'
RESET = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)

_SERVICE = next(file for file in CONTRACT_FILES if file.name.endswith('ingest/v1/ingest.proto')).services_by_name[
    'IngestService'
]
QuoteRequest = message_class('QuoteRequest')
QuoteResponse = message_class('QuoteResponse')
ScopeCapacityMessage = message_class('ScopeCapacity')

type QuotePeer = Callable[[Message, grpc.aio.ServicerContext], Awaitable[Message]]


@contextlib.asynccontextmanager
async def quote_server(peer: QuotePeer | None) -> AsyncIterator[grpc.aio.Channel]:
    """An IngestService peer serving Quote with PEER, or serving nothing at all when PEER is None.

    None is today's data_ingest: Quote is declared on the service and served by no one until
    tj-grna9p.51, so the call reaches a server that answers UNIMPLEMENTED.

    Args:
        peer: What Quote does with the decoded request, or None for an ingest without Quote.

    Yields:
        grpc.aio.Channel: A channel to the running peer; both are torn down on the way out.
    """
    server = grpc.aio.server(interceptors=[ErrorBoundaryInterceptor()])
    if peer is not None:
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    _SERVICE.full_name,
                    {
                        'Quote': grpc.unary_unary_rpc_method_handler(
                            peer,
                            request_deserializer=QuoteRequest.FromString,
                            response_serializer=lambda response: response.SerializeToString(),
                        )
                    },
                ),
            )
        )
    port = server.add_insecure_port(f'{LOOPBACK}:0')
    assert port != 0
    await server.start()
    channel = grpc.aio.insecure_channel(f'{LOOPBACK}:{port}')
    try:
        yield channel
    finally:
        await channel.close()
        await server.stop(None)


def _answer(seen: list[Message]) -> QuotePeer:
    async def peer(request: Message, context: grpc.aio.ServicerContext) -> Message:
        seen.append(request)
        response = QuoteResponse(estimated_vendor_calls=7, quota_key='alpaca-b')
        response.scopes.append(ScopeCapacityMessage(quota_key='alpaca-a', cap=200, remaining=0, window_seconds=60))
        with_reset = ScopeCapacityMessage(quota_key='alpaca-b', cap=200, remaining=150, window_seconds=60)
        with_reset.reset_at.FromDatetime(RESET)
        response.scopes.append(with_reset)
        return response

    return peer


@pytest.mark.asyncio
async def test_a_served_quote_crosses_as_a_domain_result_with_every_scope():
    """The happy path, by value: the fetch reached the peer whole, and every scope came back."""
    seen: list[Message] = []
    request = a_request(request_id='req-1', ready_by=RESET, best_effort=True)

    async with quote_server(_answer(seen)) as channel:
        result = await GrpcIngestQuoteClient(channel, deadline_s=GUARD_S).quote(request)

    assert result == QuoteResult(
        estimated_vendor_calls=7,
        quota_key='alpaca-b',
        scopes=[
            ScopeCapacity(quota_key='alpaca-a', cap=200, remaining=0, window_seconds=60, reset_at=None),
            ScopeCapacity(quota_key='alpaca-b', cap=200, remaining=150, window_seconds=60, reset_at=RESET),
        ],
    )
    (sent,) = seen
    assert [field.name for field, _ in sent.ListFields()] == ['fetch'], 'a quote wraps one fetch and nothing else'
    assert request_to_domain(sent.fetch) == request


@pytest.mark.asyncio
async def test_an_ingest_without_quote_answers_a_typed_error_never_a_raw_grpc_one():
    """Acceptance (.78): until tj-grna9p.51, every call is UNIMPLEMENTED and becomes PEER_PROTOCOL_ERROR."""
    async with quote_server(None) as channel:
        with pytest.raises(TraderJoeError) as excinfo:
            await GrpcIngestQuoteClient(channel, deadline_s=GUARD_S).quote(a_request())

    assert excinfo.value.reason is Reason.PEER_PROTOCOL_ERROR
    assert isinstance(excinfo.value.__cause__, grpc.aio.AioRpcError)
    assert excinfo.value.__cause__.code() is grpc.StatusCode.UNIMPLEMENTED


@pytest.mark.asyncio
async def test_a_typed_error_from_the_peer_arrives_as_the_same_reason():
    """from_rpc_error reads the peer's ErrorInfo back, so the caller sees ingest's reason, not a generic one."""

    async def failing(request: Message, context: grpc.aio.ServicerContext) -> Message:
        raise error_for(Reason.VENDOR_UNAVAILABLE, 'the vendor is down')

    async with quote_server(failing) as channel:
        with pytest.raises(TraderJoeError) as excinfo:
            await GrpcIngestQuoteClient(channel, deadline_s=GUARD_S).quote(a_request())

    assert excinfo.value.reason is Reason.VENDOR_UNAVAILABLE


@pytest.mark.asyncio
async def test_an_answer_this_release_cannot_read_is_a_protocol_error_and_not_a_mapping_error():
    """A valid wire message the mapper refuses -- a sub-microsecond reset_at -- is the PEER's fault.

    The client must turn a ProtoMappingError on the ANSWER into PEER_PROTOCOL_ERROR, keeping the mapping
    error as the cause, and never let it escape raw.
    """

    async def unreadable(request: Message, context: grpc.aio.ServicerContext) -> Message:
        response = QuoteResponse(estimated_vendor_calls=1, quota_key='alpaca-a')
        bad = ScopeCapacityMessage(quota_key='alpaca-a')
        bad.reset_at.seconds = int(RESET.timestamp())
        bad.reset_at.nanos = 1  # sub-microsecond: the mapper refuses it rather than rounding
        response.scopes.append(bad)
        return response

    async with quote_server(unreadable) as channel:
        with pytest.raises(TraderJoeError) as excinfo:
            await GrpcIngestQuoteClient(channel, deadline_s=GUARD_S).quote(a_request())

    assert excinfo.value.reason is Reason.PEER_PROTOCOL_ERROR
    assert isinstance(excinfo.value.__cause__, ProtoMappingError)


@pytest.mark.asyncio
async def test_an_unreachable_ingest_fails_at_the_deadline_rather_than_waiting_without_bound():
    """wait_for_ready plus a finite deadline: a peer that never comes up ends as DEADLINE."""
    channel = grpc.aio.insecure_channel(UNREACHABLE)
    try:
        with pytest.raises(TraderJoeError) as excinfo:
            await GrpcIngestQuoteClient(channel, deadline_s=0.3).quote(a_request())
    finally:
        await channel.close()

    assert excinfo.value.reason is Reason.DEADLINE


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
            GrpcIngestQuoteClient(channel, deadline_s=deadline)
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_the_deadline_is_configurable_and_defaults_to_a_short_finite_bound():
    """A quote reads counters and makes no vendor call, so its default is short -- and an override is kept."""
    assert math.isfinite(DEFAULT_QUOTE_DEADLINE_S) and 0 < DEFAULT_QUOTE_DEADLINE_S <= 30
    channel = grpc.aio.insecure_channel(UNREACHABLE)
    try:
        assert GrpcIngestQuoteClient(channel).deadline_s == DEFAULT_QUOTE_DEADLINE_S
        assert GrpcIngestQuoteClient(channel, deadline_s=2.5).deadline_s == 2.5
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_a_request_this_service_cannot_encode_is_our_bug_and_is_not_dressed_up_as_the_peers():
    """ProtoMappingError, deliberately: the peer never saw it, so PEER_PROTOCOL_ERROR would blame the wrong side."""
    seen: list[Message] = []
    good = a_request()
    unencodable = FetchDatasetRequest.model_construct(**good.model_dump() | {'start': good.start.replace(tzinfo=None)})

    async with quote_server(_answer(seen)) as channel:
        with pytest.raises(ProtoMappingError) as excinfo:
            await GrpcIngestQuoteClient(channel, deadline_s=GUARD_S).quote(unencodable)

    assert not isinstance(excinfo.value, TraderJoeError)
    assert seen == [], 'nothing reached the peer'


def test_the_grpc_client_matches_the_interface_a_test_double_implements():
    """IngestQuoteClient is a Protocol, checked at runtime by nothing; the signature is compared here."""
    declared = inspect.signature(IngestQuoteClient.quote)
    implemented = inspect.signature(GrpcIngestQuoteClient.quote)

    assert list(declared.parameters) == list(implemented.parameters)
    assert declared.parameters['request'].annotation == implemented.parameters['request'].annotation
    assert inspect.iscoroutinefunction(GrpcIngestQuoteClient.quote)
