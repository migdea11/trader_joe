"""The shared transport policy as the RELATIONS between the two option sets the factories pass to gRPC.

ADR tj-8konfu D6.2 and D6.3. The behaviour behind most of these only shows on a timescale of
minutes: a server whose minimum ping interval is longer than the client's keepalive answers the
client's pings with GOAWAY ENHANCE_YOUR_CALM ("too_many_pings") after MAX_PING_STRIKES of them,
30 s apart at the configured values. tj-3mk3u5.23's notes reproduce it at scaled-down values. What
is pinned here is that the two sets returned by channel_options() and server_options(), the ones
create_channel and GrpcServerHost actually use, cannot produce it. Each test checks a relation the
ADR names, not a constant's current value, so a retuned interval passes and a broken pairing fails.

The keys are spelled out as gRPC's channel argument names (include/grpc/impl/channel_arg_names.h).
gRPC ignores an argument it does not recognise, so a misspelled key in config.py would be inert in
production; here it is a missing key and fails.

The message ceiling is pinned by behaviour in test_rpc_limits.py. It appears here only because an
explicit 4 MiB receive limit and an inherited one behave identically, and D6.3 asks for explicit.

One pin is a value rather than a relation: addendum A2 fixes the server's grpc.so_reuseport at 0,
and any other value reverses A2. Its behaviour, a second server failing to bind the port, is pinned
in test_rpc_server.py.
"""

import pytest

from common.rpc.config import ChannelOptions, channel_options, server_options


pytestmark = pytest.mark.common

CEILING = 4 * 1024 * 1024

KEEPALIVE_TIME = 'grpc.keepalive_time_ms'
KEEPALIVE_TIMEOUT = 'grpc.keepalive_timeout_ms'
PERMIT_WITHOUT_CALLS = 'grpc.keepalive_permit_without_calls'
MAX_PINGS_WITHOUT_DATA = 'grpc.http2.max_pings_without_data'
MIN_PING_INTERVAL_WITHOUT_DATA = 'grpc.http2.min_ping_interval_without_data_ms'
MAX_PING_STRIKES = 'grpc.http2.max_ping_strikes'
MAX_SEND = 'grpc.max_send_message_length'
MAX_RECEIVE = 'grpc.max_receive_message_length'
REUSEPORT = 'grpc.so_reuseport'

SIDES = {'channel': channel_options, 'server': server_options}


def _options(side: str) -> dict[str, int]:
    options: ChannelOptions = SIDES[side]()
    keys = [key for key, _ in options]
    assert len(keys) == len(set(keys)), f'{side} options name a key more than once: {sorted(keys)}'
    return dict(options)


@pytest.mark.parametrize('side', list(SIDES))
def test_both_sides_set_the_message_ceiling_explicitly_in_both_directions(side: str):
    """D6.3: max send AND max receive, on the channel AND the server; one side alone fails asymmetrically."""
    options = _options(side)
    assert (options.get(MAX_SEND), options.get(MAX_RECEIVE)) == (CEILING, CEILING)


@pytest.mark.parametrize('side', list(SIDES))
def test_both_sides_set_keepalive_explicitly(side: str):
    """D6.2: keepalive is configured on both sides, not inherited (the client's default is disabled)."""
    options = _options(side)
    missing = [key for key in (KEEPALIVE_TIME, KEEPALIVE_TIMEOUT, PERMIT_WITHOUT_CALLS) if key not in options]
    assert not missing, f'{side} options leave {missing} at gRPC defaults'
    assert options[KEEPALIVE_TIME] > 0
    assert options[KEEPALIVE_TIMEOUT] > 0


def test_the_server_accepts_the_client_keepalive_pings():
    """THE TRAP D6.2 names: the server's minimum ping interval must be shorter than the client's ping interval.

    Equal is not enough: a ping that lands a hair early is a strike, so this is strict.
    """
    client, server = _options('channel'), _options('server')
    assert MIN_PING_INTERVAL_WITHOUT_DATA in server, (
        f'the server leaves {MIN_PING_INTERVAL_WITHOUT_DATA} at its 300000 ms default, which is longer '
        f'than any useful client keepalive'
    )
    assert server[MIN_PING_INTERVAL_WITHOUT_DATA] < client[KEEPALIVE_TIME], (
        f'the server treats pings closer than {server[MIN_PING_INTERVAL_WITHOUT_DATA]} ms as abusive, '
        f'and the client pings every {client[KEEPALIVE_TIME]} ms: GOAWAY ENHANCE_YOUR_CALM'
    )
    assert MAX_PING_STRIKES in server, f'the server leaves {MAX_PING_STRIKES} at its default'


def test_a_client_that_pings_while_idle_is_allowed_to_by_the_server():
    """If the client pings with no call in flight, the server must permit that, or each such ping is a strike."""
    client, server = _options('channel'), _options('server')
    if client[PERMIT_WITHOUT_CALLS]:
        assert server.get(PERMIT_WITHOUT_CALLS), (
            'the client keeps pinging an idle connection and the server counts every such ping as a '
            'bad one: GOAWAY ENHANCE_YOUR_CALM on the first idle connection'
        )


def test_a_client_that_pings_while_idle_keeps_pinging():
    """Pinging while idle needs the cap on pings without data lifted, or it stops after two (gRPC's default)."""
    client = _options('channel')
    if client[PERMIT_WITHOUT_CALLS]:
        assert client.get(MAX_PINGS_WITHOUT_DATA) == 0, (
            f'the client permits idle pings but {MAX_PINGS_WITHOUT_DATA}={client.get(MAX_PINGS_WITHOUT_DATA)!r} '
            f'(unset means 2) stops them after that many, so a dead peer goes undetected on an idle channel'
        )


def test_the_server_turns_so_reuseport_off():
    """A2: a second bind on a server's port must fail, not share it, so SO_REUSEPORT is explicitly 0.

    gRPC's default is 1, so a missing key shares the port exactly as a 1 does. Only the server side
    is asserted: SO_REUSEPORT is an option on a listening socket, and a channel listens on nothing.
    """
    server = _options('server')
    assert server.get(REUSEPORT) == 0, (
        f'the server sets {REUSEPORT}={server.get(REUSEPORT)!r} (unset means 1): a second process binds '
        f'the same port and the kernel splits connections between the two'
    )
