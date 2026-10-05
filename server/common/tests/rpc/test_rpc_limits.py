"""The message ceiling, ADR tj-8konfu D6.3: explicit, 4 MiB, on both sides and in both directions.

Each case puts the side under test (the channel factory or the server host) opposite a peer with NO
limits at all, so a refusal can only have come from the side under test. A raw-bytes method stands
in for a contract: with no serializer a gRPC message is exactly its bytes, so the size the limit sees
is exactly the size the test chose, and no generated code is involved (TID251, D3).

CEILING is written out here rather than imported from common.rpc.config, so that raising or lowering
the constant there turns these tests red instead of moving them along with it.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc
import pytest

from common.rpc.channel import create_channel
from common.rpc.server import BindAddress, GrpcServerHost, ServiceRegistration


pytestmark = pytest.mark.common

CEILING = 4 * 1024 * 1024
LOOPBACK = '127.0.0.1'
GUARD_S = 15.0
SERVICE = 'validator.Blob'
UNLIMITED = (('grpc.max_send_message_length', -1), ('grpc.max_receive_message_length', -1))


async def _count(request: bytes, _context: grpc.aio.ServicerContext) -> bytes:
    """Reply with how many bytes arrived: a large request, a small reply."""
    return str(len(request)).encode()


async def _make(request: bytes, _context: grpc.aio.ServicerContext) -> bytes:
    """Reply with as many bytes as the request asks for: a small request, a large reply."""
    return b'\0' * int(request)


def _blob_handler() -> grpc.GenericRpcHandler:
    return grpc.method_handlers_generic_handler(
        SERVICE,
        {'Count': grpc.unary_unary_rpc_method_handler(_count), 'Make': grpc.unary_unary_rpc_method_handler(_make)},
    )


@asynccontextmanager
async def _channel_factory_against_an_unlimited_server() -> AsyncIterator[grpc.aio.Channel]:
    server = grpc.aio.server(options=UNLIMITED)
    server.add_generic_rpc_handlers((_blob_handler(),))
    port = server.add_insecure_port(f'{LOOPBACK}:0')
    await server.start()
    channel = create_channel(f'{LOOPBACK}:{port}')
    try:
        yield channel
    finally:
        await channel.close()
        await server.stop(None)


@asynccontextmanager
async def _server_host_against_an_unlimited_channel() -> AsyncIterator[grpc.aio.Channel]:
    blob = ServiceRegistration(
        name=SERVICE, add_to_server=lambda server: server.add_generic_rpc_handlers((_blob_handler(),))
    )
    async with GrpcServerHost(BindAddress(LOOPBACK, 0), [blob]) as host:
        channel = grpc.aio.insecure_channel(f'{LOOPBACK}:{host.port}', options=UNLIMITED)
        try:
            yield channel
        finally:
            await channel.close()


# direction -> (the pairing that puts the side under test opposite an unlimited peer, the method that
# makes the large message travel in that direction)
DIRECTIONS = {
    'channel sends': (_channel_factory_against_an_unlimited_server, 'Count'),
    'channel receives': (_channel_factory_against_an_unlimited_server, 'Make'),
    'server receives': (_server_host_against_an_unlimited_channel, 'Count'),
    'server sends': (_server_host_against_an_unlimited_channel, 'Make'),
}


async def _transfer(direction: str, size: int) -> grpc.StatusCode:
    """Move one message of `size` bytes in `direction`; OK only if it arrived whole."""
    pairing, method = DIRECTIONS[direction]
    request, expected = (b'\0' * size, str(size).encode()) if method == 'Count' else (str(size).encode(), b'\0' * size)
    async with pairing() as channel:
        try:
            reply = await asyncio.wait_for(channel.unary_unary(f'/{SERVICE}/{method}')(request, timeout=10), GUARD_S)
        except grpc.aio.AioRpcError as error:
            return error.code()
    assert reply == expected, f'{direction}: the {size}-byte message arrived as {len(reply)} bytes'
    return grpc.StatusCode.OK


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', list(DIRECTIONS))
async def test_a_message_of_exactly_the_ceiling_crosses(direction: str):
    """The ceiling is not set lower than 4 MiB, on either side, in either direction."""
    assert await _transfer(direction, CEILING) == grpc.StatusCode.OK


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', list(DIRECTIONS))
async def test_one_byte_over_the_ceiling_is_refused(direction: str):
    """Not inherited and not removed: each side enforces 4 MiB itself, sending as well as receiving.

    gRPC's inherited send limit is unlimited, so the two send directions fail only because the
    shared options set it.
    """
    assert await _transfer(direction, CEILING + 1) == grpc.StatusCode.RESOURCE_EXHAUSTED
