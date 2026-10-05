"""A REAL FetchDataset PEER, built from the contract's DESCRIPTORS and not from a generated stub.

WHY A DESCRIPTOR-DRIVEN PEER. TID251 bans ``trader_joe.proto`` everywhere but ``common/rpc``, and a test
is not that seam (architect, 05:11 UTC 2026-10-02): no per-file-ignore and no noqa is granted here. So
nothing in this package may import ``ingest_pb2`` or ``ingest_pb2_grpc``. Everything below is reached
instead through the public surface ``common.rpc.mapping`` exposes for exactly this purpose --
``CONTRACT_FILES``, the FileDescriptors -- plus ``google.protobuf.message_factory``, which turns a
descriptor into the message class protoc already registered. The service, its one method, that method's
wire path and its two message types are all read off the descriptor, so a rename on the .proto reaches
these tests as a failure rather than as a stale constant that still compiles.

WHY A REAL SERVER AND NOT A STUB DOUBLE. tj-3mk3u5.29 item 5 (b) requires the bad stream to be built AT
THE WIRE: the encoder in ``common/rpc/mapping/fetch_stream.py`` is the thing that CANNOT produce a
disagreeing page, so a test that reached past it to the client's decoder would be asserting about a
peer that cannot exist, and would say nothing about the peers that can -- a test double, a replay
backend (tj-r6vcgv) or a version-skewed server. These helpers let a test script any sequence of
``FetchDatasetResponse`` messages, valid or not, and send them down a real grpc.aio connection.

THE BOUNDARY IS ON BY DEFAULT because the production host installs it by default (tj-19r2z5): a peer
that raises answers INTERNAL with an error_id, which is what the client seam must then read.

Every server binds 127.0.0.1 on an ephemeral port and is stopped in a ``finally``: a grpc.aio server
still running when its event loop closes hangs the interpreter at exit instead of failing one test.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta

import grpc
from google.protobuf import message_factory
from google.protobuf.message import Message

from common.enums.data_stock import Feed
from common.errors.vocabulary import REASONS, Reason, TraderJoeError
from common.rpc.errors import ErrorBoundaryInterceptor
from common.rpc.mapping import CONTRACT_FILES, FetchStreamEncoder, done_to_proto, refused_response
from schemas.data_ingest import fetch_dataset as domain


LOOPBACK = '127.0.0.1'

# How long any network await in these helpers may take before the test fails instead of hanging.
GUARD_S = 5.0

_INGEST_FILE = next(file for file in CONTRACT_FILES if file.name.endswith('internal/ingest/v1/ingest.proto'))
_SERVICE = _INGEST_FILE.services_by_name['IngestService']
_METHOD = _SERVICE.methods_by_name['FetchDataset']

# The path a peer dials, read off the descriptor rather than written out: it is what the servicer
# registration (tj-3mk3u5.9) and the client seam must agree on, and it is the one string gRPC matches.
FETCH_PATH = f'/{_SERVICE.full_name}/{_METHOD.name}'

#: The streamed response type. It is the one contract message with NO domain twin -- pure transport, so
#: ``CONTRACT_MESSAGES`` does not carry it -- which is exactly why it is resolved from the descriptor.
Response = message_factory.GetMessageClass(_METHOD.output_type)
Request = message_factory.GetMessageClass(_METHOD.input_type)

#: What a peer behaviour is: grpc.aio hands it the decoded request and the servicer context, and it
#: yields the messages to send.
type Peer = Callable[[Message, grpc.aio.ServicerContext], AsyncIterator[Message]]


def message_class(name: str) -> type[Message]:
    """One contract message class by its SIMPLE name, resolved through the descriptors.

    Args:
        name: e.g. ``'BarPage'``, ``'FetchAccepted'``, ``'Bar'``.

    Returns:
        type[Message]: The generated class, from protoc's own registry.

    Raises:
        AssertionError: If no contract file declares a message by that name, which a rename causes.
    """
    for file in CONTRACT_FILES:
        descriptor = file.message_types_by_name.get(name)
        if descriptor is not None:
            return message_factory.GetMessageClass(descriptor)
    declared = sorted(name for file in CONTRACT_FILES for name in file.message_types_by_name)
    raise AssertionError(f'no contract message is named {name!r}; the contract declares {declared}')


def sending(*responses: Message) -> Peer:
    """A peer that sends exactly these messages, in this order, and then ends the call OK.

    Args:
        *responses: The ``FetchDatasetResponse`` messages to send.

    Returns:
        Peer: The behaviour, for ``peer_server``.
    """

    async def peer(request: Message, context: grpc.aio.ServicerContext) -> AsyncIterator[Message]:
        for response in responses:
            yield response

    return peer


def sending_then_raising(responses: Sequence[Message], error: BaseException) -> Peer:
    """A peer that sends these messages and then fails, so a mid-stream failure is reachable.

    Args:
        responses: What to send first.
        error: What to raise once they are sent.

    Returns:
        Peer: The behaviour, for ``peer_server``.
    """

    async def peer(request: Message, context: grpc.aio.ServicerContext) -> AsyncIterator[Message]:
        for response in responses:
            yield response
        raise error

    return peer


@contextlib.asynccontextmanager
async def peer_server(peer: Peer, *, with_boundary: bool = True) -> AsyncIterator[grpc.aio.Channel]:
    """Run a one-method IngestService peer and yield an open channel to it.

    Args:
        peer: What the method does with the request it is handed.
        with_boundary: Install ``ErrorBoundaryInterceptor``, as the production host does by default
            (tj-19r2z5). False shows what the boundary is holding back.

    Yields:
        grpc.aio.Channel: A channel to the running peer. Both are torn down on the way out.
    """
    server = grpc.aio.server(interceptors=[ErrorBoundaryInterceptor()] if with_boundary else [])
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                _SERVICE.full_name,
                {
                    _METHOD.name: grpc.unary_stream_rpc_method_handler(
                        peer,
                        request_deserializer=Request.FromString,
                        response_serializer=lambda response: response.SerializeToString(),
                    )
                },
            ),
        )
    )
    port = server.add_insecure_port(f'{LOOPBACK}:0')
    assert port != 0, 'the peer could not bind an ephemeral loopback port'
    await server.start()
    channel = grpc.aio.insecure_channel(f'{LOOPBACK}:{port}')
    try:
        yield channel
    finally:
        # Both, always. A grpc.aio server still running when the loop closes hangs the whole run.
        await channel.close()
        await server.stop(None)


# ---------------------------------------------------------------------------------------------
# THE DOMAIN SIDE OF ONE ORDINARY FETCH, so every test below states only what it varies.

#: The tape the ack settles in these tests, and the one that disagrees with it. Two real members, never
#: a sentinel: Feed has no "we do not know" member by design (tj-vhboky.1).
ACK_FEED = Feed.IEX
OTHER_FEED = Feed.SIP

WHEN = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def a_request(**overrides: object) -> domain.FetchDatasetRequest:
    """One ordinary fetch request.

    Args:
        **overrides: Fields to replace.

    Returns:
        domain.FetchDatasetRequest: The request.
    """
    return domain.FetchDatasetRequest(
        **{
            'owner': 'rebalancer',
            'source': 'ALPACA',
            'asset_symbol': 'VFV',
            'asset_type': 'stock',
            'data_types': ['market-activity'],
            'granularity': '1min',
            'start': WHEN,
            'end': WHEN + timedelta(hours=1),
            'update_type': 2,
            'feed': None,
            **overrides,
        }
    )


def a_bar(*, minute: int = 0, feed: Feed = ACK_FEED) -> domain.Bar:
    """One bar, distinct per minute so a page's bars are never interchangeable.

    Args:
        minute: How far into the window the bar opens.
        feed: The tape the READER resolved for this bar, which the encoder then compares against.

    Returns:
        domain.Bar: The bar.
    """
    return domain.Bar(
        bar_start=WHEN + timedelta(minutes=minute),
        open=100.0 + minute,
        high=101.0 + minute,
        low=99.0 + minute,
        close=100.5 + minute,
        volume=1000.0 + minute,
        trade_count=10 + minute,
        vwap=100.25 + minute,
        feed=feed,
    )


def a_done(*, bar_count: int = 2) -> domain.FetchDone:
    """The terminator of an accepted stream.

    Args:
        bar_count: How many bars crossed.

    Returns:
        domain.FetchDone: The done.
    """
    return domain.FetchDone(
        bar_count=bar_count,
        served_range=domain.ServedRange(start=WHEN, end=WHEN + timedelta(hours=1)),
        as_of=WHEN + timedelta(hours=1),
    )


def accepted_response(feed: Feed = ACK_FEED) -> Message:
    """The accepted ack, as the server-side encoder builds it.

    Args:
        feed: The resolved feed.

    Returns:
        Message: The FetchDatasetResponse.
    """
    return FetchStreamEncoder(feed).accepted()


def done_response(**overrides: object) -> Message:
    """The done event.

    Args:
        **overrides: Passed to ``a_done``.

    Returns:
        Message: The FetchDatasetResponse.
    """
    return Response(done=done_to_proto(a_done(**overrides)))


def refused(detail: str = 'this deployment serves IEX only', **metadata: str) -> Message:
    """A refused ack, built through the production helper so it mirrors what a servicer would send.

    Args:
        detail: The human detail.
        **metadata: Allowlisted context, e.g. ``feed='SIP'``.

    Returns:
        Message: The FetchDatasetResponse carrying the refusal.
    """
    reason = Reason.FEED_NOT_AVAILABLE
    error: TraderJoeError = REASONS[reason].branch(reason, detail, metadata=dict(metadata) or None)
    return refused_response(error)


async def collected(events: AsyncIterator[object]) -> tuple[list[object], BaseException | None]:
    """Drain a fetch, keeping what it yielded AND the error that ended it.

    A raise is the answer in most of these tests, and the events yielded BEFORE it are half of what is
    being asserted -- "the caller receives no pages from that stream" is a claim about both.

    Args:
        events: The iterator ``IngestFetchClient.fetch`` returns.

    Returns:
        tuple[list[object], BaseException | None]: What arrived, and what ended it.
    """
    received: list[object] = []

    async def drain() -> BaseException | None:
        try:
            async for event in events:
                received.append(event)
        except BaseException as error:
            return error
        return None

    return received, await asyncio.wait_for(drain(), GUARD_S)
