"""data_ingest's gRPC server: the one place its services are registered, and the host that serves them.

The lifespan (app_depends.make_lifespan) builds the host here and enters it around its yield, so a later
task adds a service by appending to registered_services() and never edits the lifespan. The standard
health service is served always, and Submit and the Instance probe when their handlers are given; the latency
arm (tj-3mk3u5.8) registers into this list only when its harness is on. The FetchDataset pull stream is no
longer served (owner ruling 2026-11-02).

A servicer here never imports a broker class (decision tj-j4wknb): it receives the handlers create_app
injected, and calls through their interfaces. grpc.aio runs every handler on the event loop, so
a handler that reaches the blocking alpaca-py client goes through SharedWorkerPool or asyncio.to_thread,
never inline (ADR tj-8konfu D2).
"""

import asyncio
import contextlib
from typing import Protocol

from common.rpc.ingest import InstanceHandler, SubmitHandler, ingest_service
from common.rpc.server import BindAddress, GrpcServerHost, ServiceRegistration
from routers.common.latency import get_latency_services


class _NamedHealthSetter(Protocol):
    async def set_service_health(self, service: str, serving: bool) -> None: ...


class NamedHealthReporter:
    """Applies a component's serving flags to named health services on the host, from synchronous code.

    The executor reports its stalled state from a synchronous method, and the host's setter is async. Each report
    records the newest flag for its service and a single task per service applies it, so the last report always
    wins and never overtakes an earlier one. It can only name a service: the host refuses the overall '' status.
    """

    def __init__(self, host: _NamedHealthSetter) -> None:
        """Bind to the host whose health service is set.

        Args:
            host (_NamedHealthSetter): The gRPC host, started or not (a status set before start is kept).
        """
        self.__host = host
        self.__latest: dict[str, bool] = {}
        self.__tasks: dict[str, asyncio.Task[None]] = {}

    def report(self, service: str, serving: bool) -> None:
        """Record the flag for a named service and apply it. Needs a running event loop.

        Args:
            service (str): The named health service.
            serving (bool): True for SERVING, False for NOT_SERVING.
        """
        self.__latest[service] = serving
        if service not in self.__tasks:
            self.__tasks[service] = asyncio.get_running_loop().create_task(
                self.__apply(service), name=f'health-{service}'
            )

    async def __apply(self, service: str) -> None:
        try:
            while service in self.__latest:
                serving = self.__latest.pop(service)
                await self.__host.set_service_health(service, serving)
        finally:
            del self.__tasks[service]

    async def aclose(self) -> None:
        """Let the pending statuses land, then stop. Nothing is reported after this."""
        tasks = list(self.__tasks.values())
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def registered_services(
    *, submit: SubmitHandler | None = None, instance: InstanceHandler | None = None
) -> list[ServiceRegistration]:
    """List the servicers data_ingest hosts, besides the health service the host always serves.

    The single registration point: append a ServiceRegistration here, built by the hand-written code
    in common/rpc (generated modules are private to it, ADR tj-8konfu D3).

    Args:
        submit (SubmitHandler | None): The Submit implementation, or None, in which case Submit answers
            UNIMPLEMENTED.
        instance (InstanceHandler | None): The Instance probe implementation, or None, in which case the probe
            answers UNIMPLEMENTED.

    Returns:
        list[ServiceRegistration]: The servicers to attach before the server starts: Submit and the probe
            when a handler is given, plus the latency harness when enabled.
    """
    return [ingest_service(submit=submit, instance=instance), *get_latency_services()]


def build_grpc_host(*, submit: SubmitHandler | None = None, instance: InstanceHandler | None = None) -> GrpcServerHost:
    """Build the gRPC host from the environment, without binding or starting anything.

    The bind address is read here, when called, never at import, and has no default: an unset
    APP_INTERNAL_GRPC_HOST or APP_INTERNAL_GRPC_PORT is a misconfiguration (common/rpc/server.py).

    Args:
        submit (SubmitHandler | None): The Submit implementation, or None.
        instance (InstanceHandler | None): The Instance probe implementation, or None.

    Returns:
        GrpcServerHost: The host. Enter it as an async context manager to start it and to stop it on
            every exit path.

    Raises:
        ValueError: If either variable is unset or empty, or the port is not an integer from 1 to 65535.
    """
    # Without a Submit handler the call keeps its original shape, so a double of registered_services needs no new argument.
    if submit is None and instance is None:
        services = registered_services()
    elif instance is None:
        services = registered_services(submit=submit)
    else:
        services = registered_services(submit=submit, instance=instance)
    return GrpcServerHost(BindAddress.from_env(), services)
