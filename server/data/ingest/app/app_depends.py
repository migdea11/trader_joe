import asyncio
import contextlib
from collections.abc import Mapping
from contextlib import AsyncExitStack

from fastapi import FastAPI
from fastapi.concurrency import asynccontextmanager

from common.app_lifecycle import init_debugger, startup_logs, teardown_logs
from common.enums.data_stock import DataSource
from common.logging import get_logger
from common.rpc.channel import DATA_STORE_GRPC_TARGET_ENV, create_channel, target_from_env
from common.rpc.clients.store_delivery import GrpcStoreDeliveryClient
from common.worker_pool import SharedWorkerPool
from data.ingest.app.brokers.handle import BrokerHandle
from data.ingest.app.grpc_host import NamedHealthReporter, build_grpc_host
from data.ingest.app.instance import announce_until_acknowledged, log_announcement_death, mint_instance
from routers.common.latency import initialize_latency_server
from routers.data_ingest.instance_handler import IngestInstanceHandler
from routers.data_ingest.submit_handler import IngestSubmitHandler
from routers.data_store.app_endpoints import APP_NAME as STORE_APP_NAME
from routers.data_store.app_endpoints import APP_PORT_INTERNAL as STORE_APP_PORT


log = get_logger(__name__)


def make_lifespan(handles: Mapping[DataSource, BrokerHandle] | None = None):
    """Build the app lifespan that serves requests through the given broker handles.

    A factory rather than a module-level lifespan, so the composition root (main.create_app)
    decides which handle serves each data source and this module knows nothing of which.

    Args:
        handles (Mapping[DataSource, BrokerHandle] | None): The broker handles that queue, attempt and report
            Submit requests, or None for an app that serves no Submit (Submit then answers
            UNIMPLEMENTED). With handles, the lifespan mints this process's instance id, opens the channel to the
            store (DATA_STORE_GRPC_TARGET, which must be set), starts every handle's workers, and stops them
            before it closes the channel.

    Returns:
        A lifespan context manager factory for FastAPI.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Built first, started later: an unset bind variable stops startup here, before any worker thread
        # exists for it to leave running.
        # The one place the instance id is minted: Submit answers, attempt reports, the probe and RegisterInstance
        # all carry this identity (tj-26jt9w addendum 5).
        identity = mint_instance()
        instance_id = identity.instance_id
        submit = IngestSubmitHandler(handles, instance_id) if handles else None
        # Without handles the call keeps its original shape: Submit and the probe are simply not registered.
        if submit is not None:
            grpc_host = build_grpc_host(submit=submit, instance=IngestInstanceHandler(identity))
        else:
            grpc_host = build_grpc_host()

        # Init Common Endpoints
        # Using different app for REST latency, ensuring test isn't affected by client and server are on the same thread.
        initialize_latency_server(app, STORE_APP_NAME, STORE_APP_PORT)
        init_debugger()

        startup_logs(app)

        # The worker pool starts threads, so the try opens on the very next line: nothing that can fail is
        # allowed to sit between a start and the teardown that undoes it (tj-3mk3u5.24 gate finding).
        SharedWorkerPool.worker_startup()
        try:
            # Entered before the gRPC host so the handles are running when the first Submit is answered, and
            # left after it: the host stops taking requests first, then the workers stop, then the channel closes.
            async with AsyncExitStack() as stack:
                store = None
                if handles:
                    channel = create_channel(target_from_env(DATA_STORE_GRPC_TARGET_ENV))
                    stack.push_async_callback(channel.close)
                    store = GrpcStoreDeliveryClient(channel)
                    # Closed after the handles stop (the stack unwinds last-in first-out), so a final status lands.
                    reporter = NamedHealthReporter(grpc_host)
                    stack.push_async_callback(reporter.aclose)
                    for handle in handles.values():
                        handle.start(store, instance_id, reporter.report)
                        stack.push_async_callback(handle.stop)
                # The gRPC server is hosted ONLY through 'async with' around the yield, never start() here and
                # stop() in a separate hook: a grpc.aio server still running when the loop closes can hang
                # process exit, and this stops it on every exit path. A failed start (port taken, wildcard
                # host) also lands in the finally, so the worker pool is shut down rather than left running.
                async with grpc_host:
                    # Announced once the probe is being served, in the background: a store that is down never
                    # holds ingest back from serving, and the announcement is retried until it is acknowledged.
                    announcement = (
                        asyncio.create_task(announce_until_acknowledged(store, identity), name='announce-instance')
                        if store is not None
                        else None
                    )
                    if announcement is not None:
                        announcement.add_done_callback(log_announcement_death)
                    try:
                        log.info('Data Ingest App Ready!!!')
                        yield
                    finally:
                        if announcement is not None:
                            announcement.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await announcement
        finally:
            teardown_logs(app)
            SharedWorkerPool.worker_shutdown()

    return lifespan
