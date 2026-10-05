from contextlib import asynccontextmanager

from fastapi import FastAPI

from common.app_lifecycle import init_debugger, startup_logs, teardown_logs
from common.logging import get_logger
from common.rpc.channel import DATA_INGEST_GRPC_TARGET_ENV, create_channel, target_from_env
from common.rpc.clients.ingest_fetch import GrpcIngestFetchClient, IngestFetchClient
from common.worker_pool import SharedWorkerPool
from data.store.app.database import database
from routers.common.latency import initialize_latency_client
from routers.data_ingest.app_endpoints import APP_NAME as INGEST_APP_NAME
from routers.data_ingest.app_endpoints import APP_PORT_INTERNAL as INGEST_APP_PORT


log = get_logger(__name__)

__INGEST_CHANNEL = None
__INGEST_FETCH_CLIENT = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init Common Endpoints
    # Using different app for REST latency, ensuring test isn't affected by client and server are on the same thread.
    initialize_latency_client(app, INGEST_APP_NAME, INGEST_APP_PORT)
    init_debugger()

    startup_logs(app)
    SharedWorkerPool.worker_startup()

    # Setup Database
    await database.initialize()

    # THE DATASET PATH'S PEER. One channel to data_ingest's gRPC server, shared by every stub that
    # dials it, created inside the running loop and closed on teardown (ADR tj-q9ae5u addendum 5:
    # the caller resolves the target and owns the channel; the seam reads no environment variable
    # of its own).
    #
    # NO START GATE, and none is wanted (ADR tj-8konfu D6.5 = O1, ruled on tj-3mk3u5.22 Q1).
    # create_channel connects nothing: the first fetch connects, waiting for the peer through
    # wait_for_ready bounded by that call's own deadline, which covers a cold start and a restart
    # alike. There is nothing here to wait for the peer to come up.
    #
    # UNCONDITIONAL, unlike the latency harness's target: a missing DATA_INGEST_GRPC_TARGET fails
    # startup naming the variable, rather than surfacing later as DEADLINE_EXCEEDED against a peer
    # that is perfectly healthy.
    global __INGEST_CHANNEL, __INGEST_FETCH_CLIENT
    __INGEST_CHANNEL = create_channel(target_from_env(DATA_INGEST_GRPC_TARGET_ENV))
    __INGEST_FETCH_CLIENT = GrpcIngestFetchClient(__INGEST_CHANNEL)

    log.info('Data Store App Ready!!!')
    yield

    # cleanup tasks
    teardown_logs(app)
    await __INGEST_CHANNEL.close()
    SharedWorkerPool.worker_shutdown()

    await database.shutdown()


def get_ingest_fetch_client() -> IngestFetchClient:
    """FastAPI dependency: the client the dataset path fetches through.

    Typed as the INTERFACE, not as GrpcIngestFetchClient: ADR tj-8konfu D3's seam is what a
    backtest replay backend (tj-r6vcgv) and a test double implement, so nothing downstream may
    depend on the gRPC implementation. A route overrides this dependency rather than reaching for
    the module global.

    Returns:
        IngestFetchClient: The client built by the lifespan, or None outside one -- there is no
        client to hand out before startup has run.
    """
    return __INGEST_FETCH_CLIENT
