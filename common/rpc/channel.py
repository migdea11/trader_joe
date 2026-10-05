"""The grpc.aio channel factory: the client half of the shared transport policy.

grpc.aio only (ADR tj-8konfu D2): no sync channel, and no sync stub run in an executor.

HOW A CALLER WAITS FOR ITS PEER -- ADR tj-8konfu D6.5, ruled O1 (tj-3mk3u5.22 Q1). Nothing waits at
startup: there is no lifespan wait and no compose ordering on the peer. create_channel() connects
nothing; the first call connects, and the channel reconnects by itself after the peer restarts. Each
UNARY call instead waits for the peer itself, bounded by its own deadline, through
unary_call_options(): a call issued while the peer is starting or restarting waits for the channel to
become ready, and if the deadline passes first, that one request fails with DEADLINE_EXCEEDED. This
covers a restart as well as a cold start, and the failure lands on the request that needed the peer.

A server-streaming call must NOT use unary_call_options(). A stream has no deadline (D6.1), so
wait_for_ready would wait without bound. A stream that fails with UNAVAILABLE resubscribes with
backoff instead.
"""

import math
from typing import TypedDict

import grpc

from common.rpc.config import channel_options


class UnaryCallOptions(TypedDict):
    """Keyword arguments for one unary stub call, e.g. ``await stub.Method(request, **options)``."""

    timeout: float
    wait_for_ready: bool


def create_channel(target: str) -> grpc.aio.Channel:
    """Open a channel to a gRPC server with the shared options.

    Lazy: creating it connects nothing. Create it inside the running event loop (a lifespan, not module
    import), close it with ``await channel.close()``, and share one channel among every stub that
    calls the same peer.

    Args:
        target: The server's host:port, e.g. 'data_ingest:50051'.

    Returns:
        grpc.aio.Channel: The channel.
    """
    return grpc.aio.insecure_channel(target, options=channel_options())


def unary_call_options(timeout_s: float) -> UnaryCallOptions:
    """Per-call options for a unary call: wait for the peer, but never past the call's own deadline.

    Args:
        timeout_s: The call's deadline, in seconds from now. Finite and positive: wait_for_ready
            without a deadline would wait forever for a peer that is down.

    Returns:
        UnaryCallOptions: timeout and wait_for_ready=True.

    Raises:
        ValueError: If the deadline is not a finite, positive number of seconds.
    """
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError(f'a unary gRPC call needs a finite, positive deadline in seconds, got {timeout_s!r}')
    return {'timeout': timeout_s, 'wait_for_ready': True}
