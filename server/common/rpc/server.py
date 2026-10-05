"""The grpc.aio server host: attach servicers, serve the standard health service, start and stop.

grpc.aio only (ADR tj-8konfu D2): there is no sync server here and no thread-pool bridge to one. A
service hosts the server from its own lifespan by entering a GrpcServerHost as an async context
manager. Every option comes from common.rpc.config, which also configures the channel factory, so
the two sides of each setting cannot disagree.

THE BIND ADDRESS IS NEVER A WILDCARD. An app bound to 0.0.0.0 listens on every network it is
attached to, and network membership is part of how the services are kept apart (tj-r6vcgv addendum
1 A5; tj-glqs4r made this mistake once already). start() resolves the configured host and refuses it
if any address it resolves to is unspecified: 0.0.0.0, ::, or a name or shorthand ('0') that maps
to one. The host and port come from the environment with NO default. An unset variable is a
misconfiguration and fails at startup, naming the variable. A loopback default would be worse than
none: the container's own healthcheck would pass while the peer could not connect.

The health service is the standard grpc.health.v1.Health from grpcio-health-checking, not a bespoke
ping (ADR tj-8konfu D6.5). The overall status ('') and each attached service read SERVING while the
server runs, and every status reads NOT_SERVING from the moment stop() begins.

EVERY SERVER BUILT HERE CARRIES THE ERROR BOUNDARY, and there is no way to turn it off. ADR
tj-fa1rpu D1(b) calls the servicer's obligation absolute -- every escaping exception becomes a reply
-- and an interceptor was chosen over a per-method decorator because a decorator is what a new
method forgets. A host that only installed the boundary when asked would put that same forgettable
opt-in one level up, so this one object, which builds every server in the repository, carries it
instead. A test that wants gRPC's unguarded default builds its own grpc.aio.server.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Final, Self

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from common.environment import get_env_var
from common.logging import get_logger
from common.rpc.config import server_options
from common.rpc.errors import ErrorBoundaryInterceptor


log = get_logger(__name__)

GRPC_HOST_ENV: Final = 'APP_INTERNAL_GRPC_HOST'
GRPC_PORT_ENV: Final = 'APP_INTERNAL_GRPC_PORT'

# How long stop() lets in-flight calls finish before it cancels them.
DEFAULT_STOP_GRACE_S: Final = 5.0

_MAX_PORT: Final = 65535


@dataclass(frozen=True)
class ServiceRegistration:
    """One servicer, ready to attach to a server.

    Built only inside common/rpc, the one package that may import generated code (ADR tj-8konfu D3).
    Everything outside it receives a ready-made registration.

    Attributes:
        name: The fully qualified service name, e.g. 'trader_joe.proto.ping.v1.PingService'. The health
            service reports this service under that name.
        add_to_server: Attaches the servicer: the generated add_*_to_server function with the servicer
            already bound to it.
    """

    name: str
    add_to_server: Callable[[grpc.aio.Server], None]


@dataclass(frozen=True)
class BindAddress:
    """Where a server listens: one specific interface, never a wildcard.

    Port 0 asks the OS for a free port, which only a caller that reads GrpcServerHost.port back can
    use. from_env() refuses it for that reason.

    Attributes:
        host: A hostname or an IP literal. An IPv6 literal may be given with or without brackets.
        port: The TCP port, 0 to 65535.
    """

    host: str
    port: int

    def __post_init__(self) -> None:
        """Reject a blank host or a port outside 0-65535.

        Raises:
            ValueError: If the host is blank or the port is out of range.
        """
        if not self.host.strip():
            raise ValueError('gRPC bind host is empty; it must name the interface the service is reached on')
        if not 0 <= self.port <= _MAX_PORT:
            raise ValueError(f'gRPC bind port {self.port} is outside 0-{_MAX_PORT}')

    @classmethod
    def from_env(cls) -> Self:
        """Read the bind address from APP_INTERNAL_GRPC_HOST and APP_INTERNAL_GRPC_PORT.

        Read when called, never at import (common/CLAUDE.md, pitfall 1). Neither variable has a
        default.

        Returns:
            Self: The configured bind address.

        Raises:
            ValueError: If either variable is unset or empty, or the port is not an integer from 1 to
                65535.
        """
        host = get_env_var(GRPC_HOST_ENV)
        if host is None or not host.strip():
            raise ValueError(f'{GRPC_HOST_ENV} is not set; it must name the interface the gRPC server is reached on')
        port = get_env_var(GRPC_PORT_ENV)
        if port is None or not port.strip():
            raise ValueError(f'{GRPC_PORT_ENV} is not set')
        try:
            port_number = int(port)
        except ValueError:
            raise ValueError(f'{GRPC_PORT_ENV} is not an integer: {port!r}') from None
        if port_number == 0:
            raise ValueError(f'{GRPC_PORT_ENV} is 0, an ephemeral port no peer could be configured to reach')
        return cls(host=host.strip(), port=port_number)

    @property
    def target(self) -> str:
        """The host:port string gRPC binds, with an IPv6 literal bracketed.

        Returns:
            str: The address, e.g. '10.0.3.4:50051' or '[fd00::4]:50051'.
        """
        host = self.host.strip()
        if ':' in host and not host.startswith('['):
            host = f'[{host}]'
        return f'{host}:{self.port}'


async def refuse_wildcard(host: str) -> None:
    """Raise unless the host resolves only to specific addresses.

    The address gRPC would bind is what gets checked, not the spelling: '0', '0.0.0.0', '::' and a
    name that resolves to any of them are all refused. The lookup is awaited, never blocking.

    Args:
        host: The configured bind host.

    Raises:
        ValueError: If the host is blank, does not resolve, or resolves to an unspecified address.
    """
    name = host.strip().removeprefix('[').removesuffix(']')
    if not name:
        raise ValueError('gRPC bind host is empty; it must name the interface the service is reached on')
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(name, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f'gRPC bind host {host!r} does not resolve: {e}') from e
    # An IPv6 sockaddr may carry a %zone suffix, which ip_address() does not parse.
    wildcards = sorted(
        {info[4][0] for info in infos if ipaddress.ip_address(str(info[4][0]).split('%')[0]).is_unspecified}
    )
    if wildcards:
        raise ValueError(
            f'gRPC bind host {host!r} resolves to the wildcard address {", ".join(wildcards)}, which listens on '
            'every network the container is attached to; bind the interface the service is reached on instead'
        )


class GrpcServerHost:
    """A grpc.aio server with the error boundary, the health service and a fixed set of servicers.

    Use it as an async context manager, typically from a FastAPI lifespan::

        async with GrpcServerHost(BindAddress.from_env(), [registration]):
            yield

    Every option comes from common.rpc.config. A host starts once and stops once; build a new one to
    serve again.

    THE ERROR BOUNDARY IS INSTALLED BY DEFAULT AND CANNOT BE REMOVED (ADR tj-fa1rpu D1(b)). No caller
    has to know ErrorBoundaryInterceptor exists, and a servicer attached later by someone who never
    read common/rpc/errors.py is covered on the day it is attached: an exception escaping it answers
    INTERNAL carrying only an error_id, never gRPC's UNKNOWN with the exception text on the wire
    (D5, D8).

    THE BOUNDARY IS FIRST IN THE INTERCEPTOR CHAIN, ahead of anything a caller supplies. grpc.aio
    dispatches the chain in order, so first means outermost: the boundary guards whatever handler the
    rest of the chain resolved, which is the handler an inner interceptor has already wrapped. An
    auth or logging interceptor added later is therefore inside the guarantee rather than outside it
    -- an exception from its wrapped behaviour becomes a reply exactly as a servicer's would. (An
    interceptor that instead raises while RESOLVING the handler, out of its own intercept_service,
    fails the call before any handler exists; that is gRPC's dispatch to answer, not this boundary's.)

    THE HEALTH SERVICE IS INSIDE THE BOUNDARY TOO, and that is intended: Check and Watch are
    dispatched through the same chain as every other method. It costs health nothing. Its own
    deliberate context.abort -- NOT_FOUND for a service name it does not know -- raises
    grpc.aio.AbortError, which the boundary re-raises untouched, and Watch is an async generator,
    which the boundary wraps as one.
    """

    def __init__(
        self,
        bind: BindAddress,
        services: Sequence[ServiceRegistration],
        stop_grace_s: float = DEFAULT_STOP_GRACE_S,
        interceptors: Sequence[grpc.aio.ServerInterceptor] = (),
    ) -> None:
        """Configure the host. Nothing is bound and nothing is resolved until start().

        Args:
            bind: Where to listen.
            services: The servicers to attach, in addition to the health service.
            stop_grace_s: Seconds stop() lets in-flight calls finish before cancelling them.
            interceptors: Interceptors to run in ADDITION to the error boundary, in the order given.
                The boundary is always first, for the reason in the class docstring. There is no
                argument that removes it.
        """
        self._bind = bind
        self._services = tuple(services)
        self._stop_grace_s = stop_grace_s
        self._interceptors: tuple[grpc.aio.ServerInterceptor, ...] = (ErrorBoundaryInterceptor(), *interceptors)
        self._server: grpc.aio.Server | None = None
        self._health: health.aio.HealthServicer | None = None
        self._port: int | None = None
        self._started = False

    @property
    def port(self) -> int:
        """The port actually bound, which differs from the configured one when that was 0.

        Returns:
            int: The bound port.

        Raises:
            RuntimeError: If the server has not been started.
        """
        if self._port is None:
            raise RuntimeError('the gRPC server has not been started')
        return self._port

    async def start(self) -> None:
        """Bind, attach the health service and every servicer, start serving, then report SERVING.

        The server is built with the error boundary first in its interceptor chain, so every method
        it goes on to serve -- health included -- is guarded before the first call arrives.

        Raises:
            RuntimeError: If this host was already started, or the bind fails.
            ValueError: If the bind host is a wildcard or does not resolve.
        """
        if self._started:
            raise RuntimeError('a GrpcServerHost starts once; build a new one to serve again')
        self._started = True
        await refuse_wildcard(self._bind.host)

        server = grpc.aio.server(interceptors=self._interceptors, options=server_options())
        health_servicer = health.aio.HealthServicer()
        try:
            health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
            for service in self._services:
                service.add_to_server(server)
            # Raises RuntimeError when the address cannot be bound.
            port = server.add_insecure_port(self._bind.target)
            await server.start()
        except BaseException:
            # Release whatever the half-built server holds; the original error is the one raised.
            await server.stop(None)
            raise
        self._server, self._health, self._port = server, health_servicer, port

        for service in self._services:
            await health_servicer.set(service.name, health_pb2.HealthCheckResponse.SERVING)
        names = ', '.join(service.name for service in self._services) or 'none'
        log.info(f'gRPC server listening on {self._bind.host}:{port}; services: {names}')

    async def stop(self) -> None:
        """Report NOT_SERVING, then stop, letting in-flight calls finish within the grace period.

        Safe to call when the server never started or has already stopped.
        """
        if self._server is None or self._health is None:
            return
        server, health_servicer = self._server, self._health
        self._server = self._health = None
        await health_servicer.enter_graceful_shutdown()
        await server.stop(self._stop_grace_s)
        log.info(f'gRPC server on {self._bind.host}:{self._port} stopped')

    async def __aenter__(self) -> Self:
        """Start the server.

        Returns:
            Self: This host, started.
        """
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        """Stop the server, whether or not the body raised."""
        await self.stop()
