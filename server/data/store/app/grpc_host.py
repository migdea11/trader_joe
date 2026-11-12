"""data_store's gRPC server: the one place its services are registered, and the host that serves them (tj-7zjov1.27).

StoreService is the result path: ingest calls DeliverAttempt and RegisterInstance here (owner ruling on tj-26jt9w,
2026-10-24: push). The lifespan (app_depends.lifespan) builds the host here and enters it around its yield, so a later
task adds a service by appending to registered_services() and never edits the lifespan. The standard health service is
served always, with StoreService reported under its own name, which the container healthcheck and the CI peer-reach
step (P34b) read.

The bind address is the environment's, read when called and never at import, with no default
(APP_INTERNAL_GRPC_HOST / APP_INTERNAL_GRPC_PORT, common/rpc/server.py): the compose file binds the data-store-grpc
alias on the ingest_store network, never a wildcard.

The servicer never holds a generated message: common/rpc/store.py owns that seam and takes a handler, and the handler
is the delivery intake.
"""

from common.rpc.server import BindAddress, GrpcServerHost, ServiceRegistration
from common.rpc.store import store_service
from data.store.app.ingest.delivery_intake import DeliveryIntake


def registered_services(intake: DeliveryIntake) -> list[ServiceRegistration]:
    """List the servicers data_store hosts, besides the health service the host always serves.

    Args:
        intake: The delivery intake that StoreService writes through.

    Returns:
        list[ServiceRegistration]: The servicers to attach before the server starts.
    """
    return [store_service(intake)]


def build_grpc_host(intake: DeliveryIntake | None = None) -> GrpcServerHost:
    """Build the gRPC host from the environment, without binding or starting anything.

    Args:
        intake: The delivery intake, or None for the default one.

    Returns:
        GrpcServerHost: The host. Enter it as an async context manager to start it and to stop it on every exit path.

    Raises:
        ValueError: If APP_INTERNAL_GRPC_HOST or APP_INTERNAL_GRPC_PORT is unset or empty, or the port is not an
            integer from 1 to 65535.
    """
    return GrpcServerHost(BindAddress.from_env(), registered_services(intake or DeliveryIntake()))
