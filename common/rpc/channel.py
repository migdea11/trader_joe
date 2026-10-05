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

WHERE A CLIENT FINDS ITS PEER -- ADR tj-q9ae5u addendum 5. One environment variable holds the peer's
host:port, set by one entry in the client service's environment: in the base docker-compose.yaml, and
target_from_env() is the one reader. The host is the alias the server binds, never its hostname. The
value has no default in code: a missing one fails startup naming the variable, instead of surfacing
later as DEADLINE_EXCEEDED against a peer that is up.
"""

import ipaddress
import math
import os
from typing import Final, TypedDict

import grpc

from common.rpc.config import channel_options


# data_store's target for data_ingest's gRPC server: data-ingest-grpc, the alias data_ingest binds on
# ingest_store, and the server's own port expression (docker-compose.yaml, data_store environment:).
DATA_INGEST_GRPC_TARGET_ENV: Final = 'DATA_INGEST_GRPC_TARGET'

_MAX_PORT: Final = 65535


class UnaryCallOptions(TypedDict):
    """Keyword arguments for one unary stub call, e.g. ``await stub.Method(request, **options)``."""

    timeout: float
    wait_for_ready: bool


def target_from_env(name: str) -> str:
    """Read a peer's gRPC target, host:port, from an environment variable (ADR tj-q9ae5u addendum 5).

    Read when called, never at import (common/CLAUDE.md, pitfall 1), and with no default. The check is on
    the string only, with no DNS lookup: nothing resolves the peer at startup (ADR tj-8konfu D6.5, O1),
    so a name that does not resolve fails its first call instead. An IPv6 literal is bracketed, as gRPC
    requires: '[fd00::4]:50051'. The environment is read directly rather than through
    common.environment, which configures logging when it is imported.

    Args:
        name: The variable, e.g. DATA_INGEST_GRPC_TARGET_ENV.

    Returns:
        str: The target, stripped, ready for create_channel().

    Raises:
        ValueError: Naming the variable, if it is unset or blank, has no host or no ':port', has a port
            that is not an integer from 1 to 65535, or names a wildcard host (0.0.0.0, ::, [::]).
    """
    target = os.environ.get(name, '').strip()
    if not target:
        raise ValueError(f"{name} is not set; it must name the peer's gRPC server as host:port")

    if target.startswith('['):
        host, bracket, rest = target[1:].partition(']')
        if not bracket or not rest.startswith(':'):
            raise ValueError(f"{name}={target!r} has no ':port' after its bracketed host")
        port = rest[1:]
    else:
        host, colon, port = target.rpartition(':')
        if not colon:
            raise ValueError(f"{name}={target!r} has no ':port'")
        if ':' in host:
            raise ValueError(f'{name}={target!r} has an unbracketed IPv6 host; write it as [host]:port')
    if not host:
        raise ValueError(f'{name}={target!r} has no host')

    if not (port.isascii() and port.isdigit()) or not 1 <= int(port) <= _MAX_PORT:
        raise ValueError(f'{name}={target!r} has port {port!r}, which is not an integer from 1 to {_MAX_PORT}')

    try:
        wildcard = ipaddress.ip_address(host.split('%')[0]).is_unspecified
    except ValueError:
        wildcard = False  # A name, not an IP literal: whether it resolves is for the first call to find.
    if wildcard:
        raise ValueError(f'{name}={target!r} names the wildcard address {host}; name the interface the peer binds')
    return target


def create_channel(target: str) -> grpc.aio.Channel:
    """Open a channel to a gRPC server with the shared options.

    Lazy: creating it connects nothing. Create it inside the running event loop (a lifespan, not module
    import), close it with ``await channel.close()``, and share one channel among every stub that
    calls the same peer.

    Args:
        target: The server's host:port, e.g. 'data-ingest-grpc:50051', from target_from_env().

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
