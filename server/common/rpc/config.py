"""The shared gRPC transport policy: keepalive and message limits, for both sides, in one place.

ADR tj-8konfu D6.2 and D6.3, plus the client's reconnect backoff, which D6.5 = O1 depends on, and the
server's exclusive bind, addendum A2. The server host (common.rpc.server) and the channel factory
(common.rpc.channel) take their options from here and from nowhere else, so the client and server
halves of each setting are one decision rather than two defaults that happen to meet.

What does NOT live here: no error reason, no domain and no status table. The error vocabulary is
canonical in common/errors (ADR tj-fa1rpu U1), and rendering it as a gRPC status is its own module.

The defaults these values replace were read from gRPC's own documentation at the v1.81.1 tag, the
pinned grpcio: doc/keepalive.md, doc/connection-backoff.md and include/grpc/impl/channel_arg_names.h.
Client keepalive is disabled by default (INT_MAX). The server pings every 2 hours, accepts a client
ping no more often than every 5 minutes and sends GOAWAY after 2 bad pings. Neither side pings
without a call in flight, and neither sends more than 2 pings without data. The receive limit
defaults to 4 MiB and the send limit to unlimited. Reconnect backoff grows to 120 s. A server's
listening socket allows SO_REUSEPORT (grpc.so_reuseport defaults to 1).
"""

from typing import Final


type ChannelOptions = tuple[tuple[str, int], ...]

# ---------------------------------------------------------------------------------------------------
# MESSAGE SIZE (D6.3) -- explicit, on both sides, for both directions.
#
# Neither inherited nor removed. The inherited receive limit happens to be 4 MiB too, but the
# inherited SEND limit is unlimited, and a limit set on one side only fails asymmetrically: the sender
# serialises a message the receiver then refuses. The ceiling is a bug guard, not a working limit:
# contracts that carry bulk data page it in chunks well below it (D6.3 targets about 1 MiB), so a
# message that reaches the ceiling is a defect to surface, not a load to accommodate.
MAX_MESSAGE_BYTES: Final = 4 * 1024 * 1024

# ---------------------------------------------------------------------------------------------------
# KEEPALIVE (D6.2) -- what detects a dead peer. A deadline does not: a Subscribe stream has none.
#
# Both sides ping every KEEPALIVE_TIME_MS and drop the connection when a ping goes unacknowledged for
# KEEPALIVE_TIMEOUT_MS, so a peer that vanished without closing its socket (a container killed or
# restarted on the compose network) is noticed within about 40 s, on idle connections as well as busy
# ones.
KEEPALIVE_TIME_MS: Final = 30_000
KEEPALIVE_TIMEOUT_MS: Final = 10_000

# Ping with no call in flight, so an idle channel finds a dead server before its next unary call does
# (that call would otherwise wait out its whole deadline on a dead connection). This is ONE setting for
# both sides on purpose: a server left at the default rejects such pings as bad pings, and after
# MAX_PING_STRIKES of them it closes the connection with GOAWAY ENHANCE_YOUR_CALM.
KEEPALIVE_PERMIT_WITHOUT_CALLS: Final = True

# 0 lifts the default cap of 2 pings while no data flows. Without it an idle connection stops pinging
# after two pings, which defeats KEEPALIVE_PERMIT_WITHOUT_CALLS.
MAX_PINGS_WITHOUT_DATA: Final = 0

# SERVER ONLY: the shortest gap between client pings the server accepts while it sends no data. A
# shorter gap is a bad ping, and MAX_PING_STRIKES bad pings get GOAWAY ENHANCE_YOUR_CALM
# ("too_many_pings"). It MUST stay below the client's KEEPALIVE_TIME_MS, or a conforming client is
# treated as abusive and its streams die for no reason it can see -- the trap D6.2 names. It is
# derived from KEEPALIVE_TIME_MS rather than set beside it so the two cannot drift apart, and it is
# half that interval so timer jitter cannot turn an on-time ping into a strike.
MIN_RECV_PING_INTERVAL_WITHOUT_DATA_MS: Final = KEEPALIVE_TIME_MS // 2

# SERVER ONLY: the default, restated so it is a decision rather than an inheritance.
MAX_PING_STRIKES: Final = 2

# ---------------------------------------------------------------------------------------------------
# RECONNECT BACKOFF -- CLIENT ONLY. What lets D6.5 = O1 cover a restart, not only a cold start.
#
# While its peer is down, a channel retries the connection with exponential backoff, by default up to
# 120 s between attempts (doc/connection-backoff.md, v1.81.1). A wait_for_ready call does not cut
# that wait short. MEASURED on grpcio 1.81.1 (tj-3mk3u5.23 notes): after a 40 s outage, a call issued
# once the server was already back failed with DEADLINE_EXCEEDED at its 3 s deadline, because the
# channel's next attempt was further off than that. The cap bounds how long a peer that has come back
# goes unnoticed to about 2.4 s (the cap plus gRPC's 20 % jitter). A unary deadline shorter than
# that can still miss a peer in the moment it returns, which D6.1's deadlines should allow for.
# A connection attempt every 2 s against a peer that is down costs nothing worth weighing on one
# compose network.
MAX_RECONNECT_BACKOFF_MS: Final = 2_000

# ---------------------------------------------------------------------------------------------------
# PORT EXCLUSIVITY -- SERVER ONLY (ADR tj-8konfu addendum A2). A second bind on a gRPC port fails.
#
# grpcio allows SO_REUSEPORT on a server's listening socket by default ('grpc.so_reuseport', default
# 1, in include/grpc/impl/channel_arg_names.h at v1.81.1), so a second process that binds a port
# already in use succeeds silently and the kernel splits incoming connections between the two.
# Sharing a port is never intended here. entrypoint.sh starts uvicorn --workers $SERVICE_WORKERS and
# every worker runs the lifespan that binds, while data_ingest is one process by design: its
# RateBudget and SingleFlight are per-process state that an extra worker would duplicate, defeating
# both. With the option off, the extra bind fails and GrpcServerHost.start() raises. Serving one port
# from several processes would be a new decision reversing A2, not a change to this value.
ALLOW_REUSEPORT: Final = False


def _shared_options() -> ChannelOptions:
    return (
        ('grpc.max_send_message_length', MAX_MESSAGE_BYTES),
        ('grpc.max_receive_message_length', MAX_MESSAGE_BYTES),
        ('grpc.keepalive_time_ms', KEEPALIVE_TIME_MS),
        ('grpc.keepalive_timeout_ms', KEEPALIVE_TIMEOUT_MS),
        ('grpc.keepalive_permit_without_calls', int(KEEPALIVE_PERMIT_WITHOUT_CALLS)),
        ('grpc.http2.max_pings_without_data', MAX_PINGS_WITHOUT_DATA),
    )


def channel_options() -> ChannelOptions:
    """Options for every grpc.aio channel this repository opens.

    The shared options plus the client's reconnect backoff cap.

    Returns:
        ChannelOptions: The (key, value) channel arguments.
    """
    return (*_shared_options(), ('grpc.max_reconnect_backoff_ms', MAX_RECONNECT_BACKOFF_MS))


def server_options() -> ChannelOptions:
    """Options for every grpc.aio server this repository hosts.

    The shared options plus the server's ping policy, which the client's keepalive must satisfy, and
    SO_REUSEPORT off, so a second bind on the same port fails instead of sharing it.

    Returns:
        ChannelOptions: The (key, value) channel arguments.
    """
    return (
        *_shared_options(),
        ('grpc.http2.min_ping_interval_without_data_ms', MIN_RECV_PING_INTERVAL_WITHOUT_DATA_MS),
        ('grpc.http2.max_ping_strikes', MAX_PING_STRIKES),
        ('grpc.so_reuseport', int(ALLOW_REUSEPORT)),
    )
