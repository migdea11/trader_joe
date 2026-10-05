"""The grpc.aio server host in common/rpc/server.py, driven through common/rpc's hand-written API.

ADR tj-8konfu D2 (grpc.aio), D6.5 (the standard grpc.health.v1 service) and the bind discipline
from tj-r6vcgv addendum 1 A5 and tj-glqs4r: never a wildcard address, host and port from the
environment. Nothing here imports common.rpc.generated, which TID251 bans outside common/rpc (D3):
Ping is reached through common.rpc.ping, and health through grpcio-health-checking's own stubs.

Every server binds 127.0.0.1 on an ephemeral port, and every network await is bounded by GUARD_S, so
a regression fails a test instead of hanging the suite.
"""

import asyncio
import socket

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

from common.rpc.channel import create_channel
from common.rpc.ping import SERVICE_NAME, ping, ping_service
from common.rpc.server import GRPC_HOST_ENV, GRPC_PORT_ENV, BindAddress, GrpcServerHost, refuse_wildcard


pytestmark = pytest.mark.common

LOOPBACK = '127.0.0.1'
GUARD_S = 10.0
SERVING = health_pb2.HealthCheckResponse.SERVING
NOT_SERVING = health_pb2.HealthCheckResponse.NOT_SERVING


def _loopback_host(stop_grace_s: float | None = None) -> GrpcServerHost:
    if stop_grace_s is None:
        return GrpcServerHost(BindAddress(LOOPBACK, 0), [ping_service()])
    return GrpcServerHost(BindAddress(LOOPBACK, 0), [ping_service()], stop_grace_s=stop_grace_s)


async def _status(stub: health_pb2_grpc.HealthStub, service: str) -> int:
    response = await asyncio.wait_for(stub.Check(health_pb2.HealthCheckRequest(service=service), timeout=5), GUARD_S)
    return response.status


# ---------------------------------------------------------------------------------------------------
# SERVING


@pytest.mark.asyncio
async def test_ping_round_trips_through_the_host_and_the_channel_factory():
    """The pipeline proof end to end: the committed generated code, GrpcServerHost and create_channel."""
    async with _loopback_host() as host:
        assert host.port != 0, 'port 0 asks the OS for a port; the host must report the one it got'
        channel = create_channel(f'{LOOPBACK}:{host.port}')
        try:
            assert await asyncio.wait_for(ping(channel, 'hello, grpc', timeout_s=5), GUARD_S) == 'hello, grpc'
        finally:
            await channel.close()


@pytest.mark.asyncio
async def test_health_reports_the_server_and_each_attached_service_serving():
    """D6.5: the standard health service answers for the server ('') and by fully qualified service name.

    The name is the versioned proto package's (proto/README.md), which is what a probe will ask for.
    An unregistered name is NOT_FOUND, so a SERVING answer is not just the default for any question.
    """
    assert SERVICE_NAME == 'trader_joe.ping.v1.PingService'
    async with _loopback_host() as host:
        channel = create_channel(f'{LOOPBACK}:{host.port}')
        try:
            stub = health_pb2_grpc.HealthStub(channel)
            assert await _status(stub, '') == SERVING
            assert await _status(stub, SERVICE_NAME) == SERVING
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await _status(stub, 'trader_joe.absent.v1.AbsentService')
            assert raised.value.code() == grpc.StatusCode.NOT_FOUND
        finally:
            await channel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('service', ['', SERVICE_NAME], ids=['server', 'service'])
async def test_health_turns_not_serving_the_moment_stop_begins(service: str):
    """A watcher sees NOT_SERVING while in-flight calls still drain, not only once the port is gone.

    The watch is itself an in-flight call, so it stays open through the grace period, which is
    shortened here to keep the test quick.
    """
    host = _loopback_host(stop_grace_s=0.2)
    await host.start()
    channel = create_channel(f'{LOOPBACK}:{host.port}')
    try:
        watch = health_pb2_grpc.HealthStub(channel).Watch(health_pb2.HealthCheckRequest(service=service))
        first = await asyncio.wait_for(watch.read(), GUARD_S)
        assert first.status == SERVING
        stopping = asyncio.create_task(host.stop())
        second = await asyncio.wait_for(watch.read(), GUARD_S)
        assert second is not grpc.aio.EOF, 'the watch ended without ever reporting NOT_SERVING'
        assert second.status == NOT_SERVING
        await asyncio.wait_for(stopping, GUARD_S)
    finally:
        await channel.close()
        await host.stop()


# ---------------------------------------------------------------------------------------------------
# LIFECYCLE


@pytest.mark.asyncio
async def test_a_host_starts_once_and_stops_idempotently():
    """stop() before start() and a second stop() are no-ops; a second start() is refused."""
    host = _loopback_host()
    await host.stop()
    with pytest.raises(RuntimeError, match='not been started'):
        _ = host.port
    await host.start()
    try:
        with pytest.raises(RuntimeError, match='starts once'):
            await host.start()
    finally:
        await host.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_leaving_the_context_stops_the_server_even_when_the_body_raises():
    """The lifespan shape: an exception out of the body still stops the server, so nothing answers after."""
    host = _loopback_host()
    try:
        with pytest.raises(LookupError, match='the body failed'):
            async with host:
                port = host.port
                raise LookupError('the body failed')
        channel = create_channel(f'{LOOPBACK}:{port}')
        try:
            # Any failure to reach the server will do. Which code it is belongs to the channel's wait
            # policy, pinned in test_rpc_channel.py, not to this test.
            with pytest.raises(grpc.aio.AioRpcError):
                await asyncio.wait_for(ping(channel, 'anyone there?', timeout_s=0.3), GUARD_S)
        finally:
            await channel.close()
    finally:
        # A no-op when the context did its job. When it did not, a grpc.aio server left running past
        # its event loop hangs the whole pytest run instead of failing this one test.
        await host.stop()


@pytest.mark.asyncio
async def test_a_port_that_cannot_be_bound_fails_start():
    """A bind failure surfaces from start(), rather than leaving a server that listens nowhere."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind((LOOPBACK, 0))
        holder.listen()
        host = GrpcServerHost(BindAddress(LOOPBACK, holder.getsockname()[1]), [ping_service()])
        with pytest.raises(RuntimeError, match='bind'):
            await host.start()
    with pytest.raises(RuntimeError, match='not been started'):
        _ = host.port


# ---------------------------------------------------------------------------------------------------
# PORT EXCLUSIVITY (ADR tj-8konfu addendum A2)
#
# grpcio sets SO_REUSEPORT on a listening socket by default, and Linux lets a second socket bind a port
# when both sockets set it: the bind succeeds and the kernel splits connections between the two. The
# test above cannot see that. Its holder is a plain socket without SO_REUSEPORT, which refuses the
# second bind whatever gRPC sets. Only two gRPC servers on one port show whether the option is off.


@pytest.mark.asyncio
async def test_a_second_host_on_a_bound_port_fails_start_instead_of_sharing_it():
    """A2: an extra worker's lifespan fails at the bind, loudly, instead of taking half the connections.

    The first host still answers afterwards: the failed start released only what it had built itself.
    """
    first = _loopback_host()
    await first.start()
    try:
        second = GrpcServerHost(BindAddress(LOOPBACK, first.port), [ping_service()])
        try:
            with pytest.raises(RuntimeError, match='bind'):
                await second.start()
            with pytest.raises(RuntimeError, match='not been started'):
                _ = second.port
        finally:
            # A no-op when the bind failed. When the port was shared instead, the second server is
            # running, and a grpc.aio server left running past its event loop hangs the pytest run.
            await second.stop()
        channel = create_channel(f'{LOOPBACK}:{first.port}')
        try:
            assert await asyncio.wait_for(ping(channel, 'still here', timeout_s=5), GUARD_S) == 'still here'
        finally:
            await channel.close()
    finally:
        await first.stop()


# ---------------------------------------------------------------------------------------------------
# NEVER A WILDCARD (tj-r6vcgv addendum 1 A5; tj-glqs4r)
#
# What is refused is what the host RESOLVES to, not how it is spelled: '0' and '0.0' are inet_aton
# shorthands for 0.0.0.0, and a check on the literal strings would let them through.
WILDCARD_HOSTS = ['0.0.0.0', '0', '0.0', '::', '[::]', '::0', '0:0:0:0:0:0:0:0']
SPECIFIC_HOSTS = ['127.0.0.1', 'localhost', '10.0.3.4', '::1', '[::1]', 'fd00::4']


@pytest.mark.asyncio
@pytest.mark.parametrize('host', WILDCARD_HOSTS)
async def test_refuse_wildcard_refuses_every_spelling_of_the_unspecified_address(host: str):
    with pytest.raises(ValueError, match='wildcard'):
        await refuse_wildcard(host)


@pytest.mark.asyncio
@pytest.mark.parametrize('host', SPECIFIC_HOSTS)
async def test_refuse_wildcard_accepts_a_specific_address(host: str):
    await refuse_wildcard(host)


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['', '   ', '[]'])
async def test_refuse_wildcard_refuses_a_blank_host(host: str):
    with pytest.raises(ValueError, match='empty'):
        await refuse_wildcard(host)


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['0.0.0.0', '0', '::'])
async def test_start_refuses_a_wildcard_bind_before_binding_anything(host: str):
    """The refusal is wired into start(), ahead of the bind: the host never gets a port."""
    server = GrpcServerHost(BindAddress(host, 0), [ping_service()])
    try:
        with pytest.raises(ValueError, match='wildcard'):
            await server.start()
        with pytest.raises(RuntimeError, match='not been started'):
            _ = server.port
    finally:
        await server.stop()


# ---------------------------------------------------------------------------------------------------
# THE BIND ADDRESS FROM THE ENVIRONMENT
#
# Neither variable has a default. An unset one fails at startup naming itself: a loopback default
# would let the container's own healthcheck pass while its peer could never connect.


def test_from_env_reads_the_host_and_port(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(GRPC_HOST_ENV, '  10.0.3.4 ')
    monkeypatch.setenv(GRPC_PORT_ENV, '50051')
    assert BindAddress.from_env() == BindAddress('10.0.3.4', 50051)


_FROM_ENV_REFUSED = {
    'host unset': (None, '50051', GRPC_HOST_ENV),
    'host blank': ('   ', '50051', GRPC_HOST_ENV),
    'port unset': ('10.0.3.4', None, GRPC_PORT_ENV),
    'port blank': ('10.0.3.4', ' ', GRPC_PORT_ENV),
    'port not an integer': ('10.0.3.4', 'fifty', GRPC_PORT_ENV),
    'port 0, ephemeral': ('10.0.3.4', '0', GRPC_PORT_ENV),
}


@pytest.mark.parametrize(('host', 'port', 'named'), _FROM_ENV_REFUSED.values(), ids=_FROM_ENV_REFUSED.keys())
def test_from_env_fails_naming_the_variable_at_fault(
    monkeypatch: pytest.MonkeyPatch, host: str | None, port: str | None, named: str
):
    for variable, value in ((GRPC_HOST_ENV, host), (GRPC_PORT_ENV, port)):
        if value is None:
            monkeypatch.delenv(variable, raising=False)
        else:
            monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=named):
        BindAddress.from_env()


@pytest.mark.parametrize('port', ['-1', '65536', '70000'])
def test_from_env_refuses_a_port_outside_the_tcp_range(monkeypatch: pytest.MonkeyPatch, port: str):
    monkeypatch.setenv(GRPC_HOST_ENV, '10.0.3.4')
    monkeypatch.setenv(GRPC_PORT_ENV, port)
    with pytest.raises(ValueError, match=port):
        BindAddress.from_env()


@pytest.mark.parametrize(('host', 'port'), [('', 50051), ('   ', 50051), ('10.0.3.4', -1), ('10.0.3.4', 65536)])
def test_bind_address_refuses_a_blank_host_or_a_port_outside_the_tcp_range(host: str, port: int):
    with pytest.raises(ValueError, match='gRPC bind'):
        BindAddress(host, port)


@pytest.mark.parametrize(
    ('host', 'target'),
    [
        ('10.0.3.4', '10.0.3.4:50051'),
        ('data_ingest', 'data_ingest:50051'),
        ('fd00::4', '[fd00::4]:50051'),
        ('[fd00::4]', '[fd00::4]:50051'),
    ],
)
def test_target_brackets_an_ipv6_literal_once(host: str, target: str):
    assert BindAddress(host, 50051).target == target
