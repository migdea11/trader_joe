"""The channel factory in common/rpc/channel.py, and how a unary caller waits for its peer.

ADR tj-8konfu D6.5, ruled O1 (tj-3mk3u5.22 Q1): a lazy channel, no wait at startup, and each unary
call waiting for the peer with wait_for_ready, bounded by its own deadline. That is what lets a call
issued while the peer is starting or restarting succeed, and what makes a peer that never comes fail
THAT request with DEADLINE_EXCEEDED rather than at once with UNAVAILABLE.

The client's reconnect backoff cap in common/rpc/config.py is pinned here too, by behaviour: it is
what keeps O1 true after a long outage (measured in tj-3mk3u5.23's notes), and nothing else in the
suite would notice it go.

Calls go through common.rpc.ping, never through common.rpc.generated (TID251, D3). Every server
binds 127.0.0.1, and every network await is bounded by GUARD_S.
"""

import asyncio
import contextlib
import math
import time
from itertools import pairwise

import grpc
import pytest

from common.rpc.channel import create_channel, unary_call_options
from common.rpc.ping import ping, ping_service
from common.rpc.server import BindAddress, GrpcServerHost


pytestmark = pytest.mark.common

LOOPBACK = '127.0.0.1'
GUARD_S = 15.0
# The deadline of a call that is expected to wait for a peer that never comes.
SHORT_DEADLINE_S = 0.5
# The deadline of a call that is expected to be served: far longer than any wait below, so a pass
# never depends on timing.
LONG_DEADLINE_S = 8.0
# How long a call is left in flight with nothing listening before the server is started.
PEER_DOWN_S = 0.3


async def _vacated_port() -> int:
    """A loopback port a server bound and released, so nothing listens on it now."""
    async with GrpcServerHost(BindAddress(LOOPBACK, 0), [ping_service()]) as host:
        return host.port


# ---------------------------------------------------------------------------------------------------
# PER-CALL OPTIONS


def test_unary_call_options_wait_for_ready_bounded_by_the_deadline():
    assert unary_call_options(2.5) == {'timeout': 2.5, 'wait_for_ready': True}


@pytest.mark.parametrize('timeout_s', [0, 0.0, -1.0, math.inf, -math.inf, math.nan])
def test_unary_call_options_refuses_a_deadline_that_is_not_finite_and_positive(timeout_s: float):
    """wait_for_ready with no finite deadline would wait forever for a peer that is down."""
    with pytest.raises(ValueError, match='finite, positive deadline'):
        unary_call_options(timeout_s)


# ---------------------------------------------------------------------------------------------------
# D6.5 = O1


@pytest.mark.asyncio
async def test_a_call_to_a_peer_that_never_comes_fails_deadline_exceeded_at_its_deadline():
    """The channel connects nothing until called; the call then waits out its deadline, not less.

    Without wait_for_ready the same call fails UNAVAILABLE immediately, which is the behaviour O1
    replaces.
    """
    channel = create_channel(f'{LOOPBACK}:{await _vacated_port()}')
    try:
        assert channel.get_state() == grpc.ChannelConnectivity.IDLE, 'create_channel must not connect'
        started = time.monotonic()
        with pytest.raises(grpc.aio.AioRpcError) as raised:
            await asyncio.wait_for(ping(channel, 'anyone there?', timeout_s=SHORT_DEADLINE_S), GUARD_S)
        waited = time.monotonic() - started
    finally:
        await channel.close()
    assert raised.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    assert waited >= SHORT_DEADLINE_S * 0.9, f'the call gave up after {waited:.2f}s, before its deadline'


@pytest.mark.asyncio
async def test_a_call_issued_before_the_peer_starts_is_served_once_it_does():
    """Cold start: the call is in flight with nothing listening, then the server starts and answers it."""
    port = await _vacated_port()
    channel = create_channel(f'{LOOPBACK}:{port}')
    late = GrpcServerHost(BindAddress(LOOPBACK, port), [ping_service()])
    try:
        call = asyncio.create_task(ping(channel, 'cold start', timeout_s=LONG_DEADLINE_S))
        await asyncio.sleep(PEER_DOWN_S)
        assert not call.done(), f'the call ended while the peer was down: {call.exception()!r}'
        await late.start()
        assert await asyncio.wait_for(call, GUARD_S) == 'cold start'
    finally:
        await late.stop()
        await channel.close()


@pytest.mark.asyncio
async def test_a_call_issued_while_the_peer_restarts_is_served_after_the_restart():
    """Restart, the case O1 was chosen for: one channel, a server stopped and a new one started on its port."""
    first = GrpcServerHost(BindAddress(LOOPBACK, 0), [ping_service()])
    await first.start()
    port = first.port
    channel = create_channel(f'{LOOPBACK}:{port}')
    second = GrpcServerHost(BindAddress(LOOPBACK, port), [ping_service()])
    try:
        assert await asyncio.wait_for(ping(channel, 'before', timeout_s=LONG_DEADLINE_S), GUARD_S) == 'before'
        await first.stop()
        call = asyncio.create_task(ping(channel, 'after the restart', timeout_s=LONG_DEADLINE_S))
        await asyncio.sleep(PEER_DOWN_S)
        assert not call.done(), f'the call ended while the peer was down: {call.exception()!r}'
        await second.start()
        assert await asyncio.wait_for(call, GUARD_S) == 'after the restart'
    finally:
        await first.stop()
        await second.stop()
        await channel.close()


# ---------------------------------------------------------------------------------------------------
# THE RECONNECT BACKOFF CAP
#
# While its peer is down a channel retries the connection with exponential backoff: 1 s, then x1.6
# per attempt, each delay jittered by +-20 %, by default up to 120 s (doc/connection-backoff.md at the
# pinned grpcio). Uncapped, the fourth delay is 4.096 s, never less than 3.28 s after jitter, and the
# delays only grow from there. config.py caps them at 2 s, at most 2.4 s after jitter. MAX_GAP_S sits
# between the two, with room for scheduling on a loaded runner.
#
# The peer here accepts each connection and closes it at once, so every attempt is visible and none
# succeeds. A wait_for_ready call keeps the channel trying. Five attempts take about 6.6 s with the
# cap, which is the cost of watching the fourth delay: the first three cannot tell capped from not.
RECONNECT_ATTEMPTS = 5
MAX_GAP_S = 3.0


@pytest.mark.asyncio
async def test_the_channel_retries_a_down_peer_at_least_every_few_seconds():
    attempts: list[float] = []
    enough = asyncio.Event()

    async def accept_and_close(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        attempts.append(time.monotonic())
        writer.close()
        if len(attempts) >= RECONNECT_ATTEMPTS:
            enough.set()

    listener = await asyncio.start_server(accept_and_close, LOOPBACK, 0)
    channel = create_channel(f'{LOOPBACK}:{listener.sockets[0].getsockname()[1]}')
    call = asyncio.create_task(ping(channel, 'never answered', timeout_s=GUARD_S * 2))
    try:
        await asyncio.wait_for(enough.wait(), GUARD_S)
    finally:
        call.cancel()
        with contextlib.suppress(asyncio.CancelledError, grpc.aio.AioRpcError):
            await call
        await channel.close()
        listener.close()
        await listener.wait_closed()
    gaps = [round(later - earlier, 2) for earlier, later in pairwise(attempts)]
    assert max(gaps) <= MAX_GAP_S, (
        f'the channel waited up to {max(gaps)}s between connection attempts (gaps {gaps}). Without '
        f'the client reconnect backoff cap a peer that is back goes unnoticed for that long, and a '
        f'wait_for_ready call with a shorter deadline fails although the peer is up (ADR tj-8konfu D6.5)'
    )
