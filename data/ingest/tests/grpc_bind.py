"""Bind data_ingest's gRPC host to loopback for a test that enters the real lifespan (tj-3mk3u5.24).

The lifespan reads APP_INTERNAL_GRPC_HOST and APP_INTERNAL_GRPC_PORT when it builds its gRPC host,
with no default (common/rpc/server.py), so a test that enters it sets both. 127.0.0.1, never a
wildcard, and a port the OS has just reported free, never a fixed one two runs could share.

The lifespan must stop its host on every exit path (bead tj-3mk3u5.24, note N3: a grpc.aio server
left running past its event loop can hang process exit). LoopbackGrpc therefore records every host
the lifespan builds and, on the way out, FAILS the test if any of them still answers gRPC, then
stops each one. The check comes before the stop, so the safety net cannot hide the defect it exists
for, and the stop means a regression fails the test that caught it instead of hanging the run.
"""

import asyncio
import os
import socket
from contextlib import ExitStack
from typing import Final, Self
from unittest.mock import patch

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc

from common.rpc.channel import create_channel
from common.rpc.server import GRPC_HOST_ENV, GRPC_PORT_ENV, GrpcServerHost
from data.ingest.app import app_depends


LOOPBACK: Final = '127.0.0.1'
# Every network await in these tests is bounded by this, so a regression fails rather than hangs.
GUARD_S: Final = 10.0


def free_loopback_port() -> int:
    """A port on 127.0.0.1 that was free a moment ago.

    Returns:
        int: The port.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


def accepts_connections(port: int) -> bool:
    """Whether anything on 127.0.0.1 accepts a TCP connection on the port right now.

    Synchronous on purpose, so a stub called synchronously from inside the lifespan can ask. The
    kernel completes the handshake from the listen backlog, so a server on the same event loop
    need not run for the answer to be yes.

    Args:
        port (int): The port to try.

    Returns:
        bool: True if a connection was accepted, False if it was refused.
    """
    try:
        with socket.create_connection((LOOPBACK, port), timeout=GUARD_S):
            return True
    except ConnectionRefusedError:
        return False


async def still_serving(host: GrpcServerHost) -> bool:
    """Whether a host that once started still answers a health check.

    Asked over gRPC rather than TCP, so a plain socket holding the port (a bind-failure test's
    holder) does not read as a running server.

    Args:
        host (GrpcServerHost): The host to ask.

    Returns:
        bool: True if its health service answered; False if it never started or nothing answers.
    """
    try:
        port = host.port
    except RuntimeError:
        return False
    channel = create_channel(f'{LOOPBACK}:{port}')
    try:
        stub = health_pb2_grpc.HealthStub(channel)
        await asyncio.wait_for(stub.Check(health_pb2.HealthCheckRequest(), timeout=0.5), GUARD_S)
    except grpc.aio.AioRpcError:
        return False
    finally:
        await channel.close()
    return True


class LoopbackGrpc:
    """Point the lifespan's gRPC host at 127.0.0.1 and a free port; fail if one outlives it; stop all.

    Use as ``async with LoopbackGrpc() as grpc_bind:`` around entering the lifespan, so it exits
    after the lifespan does. The host is the production one: app_depends.build_grpc_host is wrapped
    only to record what it returns, and is still the function that reads the environment and builds
    the host.

    Args:
        host (str): The bind host to configure.
        port (int | None): The bind port to configure; a free loopback port when None.
        unset (tuple[str, ...]): Variables to remove for the duration, after the two are set.
    """

    def __init__(self, host: str = LOOPBACK, port: int | None = None, unset: tuple[str, ...] = ()):
        self.host = host
        self.port = free_loopback_port() if port is None else port
        self.hosts: list[GrpcServerHost] = []
        self.__unset = unset
        self.__stack = ExitStack()

    async def __aenter__(self) -> Self:
        """Set the environment and start recording built hosts.

        Returns:
            Self: This binding.
        """
        # patch.dict restores the whole environment it found, so the removals below are undone too.
        self.__stack.enter_context(patch.dict(os.environ, {GRPC_HOST_ENV: self.host, GRPC_PORT_ENV: str(self.port)}))
        for name in self.__unset:
            os.environ.pop(name, None)
        build = app_depends.build_grpc_host

        def recording_build(readers):
            built = build(readers)
            self.hosts.append(built)
            return built

        self.__stack.enter_context(patch.object(app_depends, 'build_grpc_host', side_effect=recording_build))
        return self

    async def __aexit__(self, *exc_info) -> None:
        """Fail if a host the lifespan built still serves, stop every one, then restore the environment.

        Raises:
            AssertionError: If a host was still serving once the code under test had finished with it.
        """
        try:
            left_serving = [built.port for built in self.hosts if await still_serving(built)]
            for built in self.hosts:
                await asyncio.wait_for(built.stop(), GUARD_S)
        finally:
            self.__stack.close()
        if left_serving:
            raise AssertionError(
                f'a gRPC host was still serving on port(s) {left_serving} after the lifespan ended; '
                'it must stop on every exit path (tj-3mk3u5.24 note N3)'
            )
