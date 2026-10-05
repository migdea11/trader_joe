from collections.abc import Mapping

from fastapi import FastAPI
from fastapi.concurrency import asynccontextmanager

from common.app_lifecycle import init_debugger, startup_logs, teardown_logs
from common.enums.data_stock import DataSource
from common.logging import get_logger
from common.worker_pool import SharedWorkerPool
from data.ingest.app.brokers.interface import BrokerRead
from data.ingest.app.grpc_host import build_grpc_host
from routers.common.latency import initialize_latency_server
from routers.data_store.app_endpoints import APP_NAME as STORE_APP_NAME
from routers.data_store.app_endpoints import APP_PORT_INTERNAL as STORE_APP_PORT


log = get_logger(__name__)


def make_lifespan(readers: Mapping[DataSource, BrokerRead]):
    """Build the app lifespan that serves requests through the given broker handles.

    A factory rather than a module-level lifespan, so the composition root (main.create_app)
    decides which handle serves each data source and this module knows nothing of which.

    Args:
        readers (Mapping[DataSource, BrokerRead]): Handle serving each data source.

    Returns:
        A lifespan context manager factory for FastAPI.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Built first, started later: an unset bind variable stops startup here, before any worker thread
        # exists for it to leave running.
        grpc_host = build_grpc_host(readers)

        # Init Common Endpoints
        # Using different app for REST latency, ensuring test isn't affected by client and server are on the same thread.
        initialize_latency_server(app, STORE_APP_NAME, STORE_APP_PORT)
        init_debugger()

        startup_logs(app)

        # The worker pool starts threads, so the try opens on the very next line: nothing that can fail is
        # allowed to sit between a start and the teardown that undoes it (tj-3mk3u5.24 gate finding).
        SharedWorkerPool.worker_startup()
        try:
            # The gRPC server is hosted ONLY through 'async with' around the yield, never start() here and
            # stop() in a separate hook: a grpc.aio server still running when the loop closes can hang
            # process exit, and this stops it on every exit path. A failed start (port taken, wildcard
            # host) also lands in the finally, so the worker pool is shut down rather than left running.
            async with grpc_host:
                log.info('Data Ingest App Ready!!!')
                yield
        finally:
            teardown_logs(app)
            SharedWorkerPool.worker_shutdown()

    return lifespan
