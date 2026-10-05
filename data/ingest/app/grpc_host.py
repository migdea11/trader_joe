"""data_ingest's gRPC server: the one place its services are registered, and the host that serves them.

The lifespan (app_depends.make_lifespan) builds the host here and enters it around its yield, so a later
task adds a service by appending to registered_services() and never edits the lifespan. Only the
standard health service is served today (ADR tj-8konfu D6.5); the latency arm (tj-3mk3u5.8) and the
dataset fetch (tj-3mk3u5.9) register into this list.

A servicer here never imports a broker class (decision tj-j4wknb): it receives the readers create_app
injected, and calls through the BrokerRead interface. grpc.aio runs every handler on the event loop, so
a handler that reaches the blocking alpaca-py client goes through SharedWorkerPool or asyncio.to_thread,
never inline (ADR tj-8konfu D2).
"""

from collections.abc import Mapping

from common.enums.data_stock import DataSource
from common.rpc.server import BindAddress, GrpcServerHost, ServiceRegistration
from data.ingest.app.brokers.interface import BrokerRead


def registered_services(readers: Mapping[DataSource, BrokerRead]) -> list[ServiceRegistration]:
    """List the servicers data_ingest hosts, besides the health service the host always serves.

    The single registration point: append a ServiceRegistration here, built by the hand-written code
    in common/rpc (generated modules are private to it, ADR tj-8konfu D3).

    Args:
        readers (Mapping[DataSource, BrokerRead]): Handle serving each data source, for the servicers
            that read through one. None does yet.

    Returns:
        list[ServiceRegistration]: The servicers to attach before the server starts. Empty today.
    """
    return []


def build_grpc_host(readers: Mapping[DataSource, BrokerRead]) -> GrpcServerHost:
    """Build the gRPC host from the environment, without binding or starting anything.

    The bind address is read here, when called, never at import, and has no default: an unset
    APP_INTERNAL_GRPC_HOST or APP_INTERNAL_GRPC_PORT is a misconfiguration (common/rpc/server.py).

    Args:
        readers (Mapping[DataSource, BrokerRead]): Handle serving each data source.

    Returns:
        GrpcServerHost: The host. Enter it as an async context manager to start it and to stop it on
            every exit path.

    Raises:
        ValueError: If either variable is unset or empty, or the port is not an integer from 1 to 65535.
    """
    return GrpcServerHost(BindAddress.from_env(), registered_services(readers))
