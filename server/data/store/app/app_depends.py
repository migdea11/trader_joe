from contextlib import asynccontextmanager

from fastapi import FastAPI

from common.app_lifecycle import init_debugger, startup_logs, teardown_logs
from common.logging import get_logger
from common.rpc.channel import DATA_INGEST_GRPC_TARGET_ENV, create_channel, target_from_env
from common.rpc.clients.ingest_submit import GrpcIngestSubmitClient, IngestSubmitClient
from common.worker_pool import SharedWorkerPool
from data.store.app.asset_snapshot_schedule import start_asset_snapshot_loops, stop_asset_snapshot_loops
from data.store.app.daily_refresh import start_daily_refresh_loops, stop_daily_refresh_loops
from data.store.app.database import database
from data.store.app.grpc_host import build_grpc_host
from data.store.app.ingest.instance_watch import start_instance_watch_loop, stop_instance_watch_loop
from data.store.app.ingest.run_recording import sweep_interrupted_runs
from data.store.app.panel_manager import start_panel_manager_loop, stop_panel_manager_loop
from data.store.app.reaper import start_reaper_loop, stop_reaper_loop
from data.store.app.usage_recorder import start_usage_loops, stop_usage_loops
from routers.common.latency import initialize_latency_client
from routers.data_ingest.app_endpoints import APP_NAME as INGEST_APP_NAME
from routers.data_ingest.app_endpoints import APP_PORT_INTERNAL as INGEST_APP_PORT


log = get_logger(__name__)

__INGEST_CHANNEL = None
__INGEST_SUBMIT_CLIENT = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # THE RESULT PATH'S SERVER (tj-7zjov1.27), BUILT FIRST and entered last. Building binds nothing; it reads
    # APP_INTERNAL_GRPC_HOST and APP_INTERNAL_GRPC_PORT, so a missing one fails startup naming the variable before
    # anything else has started.
    grpc_host = build_grpc_host()

    # Init Common Endpoints
    # Using different app for REST latency, ensuring test isn't affected by client and server are on the same thread.
    initialize_latency_client(app, INGEST_APP_NAME, INGEST_APP_PORT)
    init_debugger()

    startup_logs(app)
    SharedWorkerPool.worker_startup()

    # Setup Database
    await database.initialize()
    # A run a previous process left RUNNING will never finish: this process holds none yet. Safe
    # only because data_store runs ONE process (see the store CLAUDE.md). Since the push path nothing writes a RUNNING
    # row, so this only fails the legacy ones an earlier release left behind; an open REQUEST is not a run and is
    # never swept: the instance watcher re-sends it.
    await sweep_interrupted_runs()
    # The usage recorder's flush and rollup loops (tj-grna9p.46); stopped, and flushed once more, on teardown.
    usage_tasks = start_usage_loops()
    # The expiry reaper's pass (tj-grna9p.23): dry-run unless REAPER_DELETES_ENABLED is true. One worker only.
    reaper_task = start_reaper_loop()

    # THE DATASET PATH'S PEER. One channel to data_ingest's gRPC server, shared by every stub that
    # dials it, created inside the running loop and closed on teardown (ADR tj-q9ae5u addendum 5:
    # the caller resolves the target and owns the channel; the seam reads no environment variable
    # of its own).
    #
    # NO START GATE, and none is wanted (ADR tj-8konfu D6.5 = O1, ruled on tj-3mk3u5.22 Q1).
    # create_channel connects nothing: the first Submit connects, waiting for the peer through
    # wait_for_ready bounded by that call's own deadline, which covers a cold start and a restart
    # alike. There is nothing here to wait for the peer to come up.
    #
    # UNCONDITIONAL, unlike the latency harness's target: a missing DATA_INGEST_GRPC_TARGET fails
    # startup naming the variable, rather than surfacing later as DEADLINE_EXCEEDED against a peer
    # that is perfectly healthy.
    global __INGEST_CHANNEL, __INGEST_SUBMIT_CLIENT
    __INGEST_CHANNEL = create_channel(target_from_env(DATA_INGEST_GRPC_TARGET_ENV))
    __INGEST_SUBMIT_CLIENT = GrpcIngestSubmitClient(__INGEST_CHANNEL)
    # The interim DAILY refresh (tj-grna9p.22): needs the client, so it starts after it. One worker only.
    refresh_tasks = await start_daily_refresh_loops(__INGEST_SUBMIT_CLIENT)
    # The instance watcher (tj-7zjov1.28): re-sends PENDING requests and re-submits the ones a superseded instance held.
    # The asset-list snapshot schedule (tj-7zjov1.138): its own daily loops, and the hook the watcher calls when its probe
    # names a new ingest instance. Started first, so the hook exists when the watcher's first probe runs.
    snapshot_loops = await start_asset_snapshot_loops(__INGEST_SUBMIT_CLIENT)
    watch_task = start_instance_watch_loop(
        __INGEST_SUBMIT_CLIENT, on_instance=snapshot_loops.schedule.on_instance_seen if snapshot_loops else None
    )
    # The panel manager (tj-7zjov1.34): one 5 minute tick; what is due is derived from the rows. Needs the Submit client and
    # the database. Every panel is off until a settings version enables it.
    panel_task = await start_panel_manager_loop(__INGEST_SUBMIT_CLIENT)

    # StoreService (DeliverAttempt, RegisterInstance) serves from here to teardown. It is entered last, so every
    # dependency of a delivery (the database above all) is up before ingest can reach it, and it exits FIRST on
    # teardown, so an in-flight delivery commits or rolls back whole against a live database: the host lets calls
    # finish within its grace and then cancels them.
    async with grpc_host:
        log.info('Data Store App Ready!!!')
        yield

    # cleanup tasks
    # Before the channel it fetches through closes.
    await stop_panel_manager_loop(panel_task)
    await stop_instance_watch_loop(watch_task)
    await stop_asset_snapshot_loops(snapshot_loops)
    await stop_daily_refresh_loops(refresh_tasks)
    teardown_logs(app)
    await __INGEST_CHANNEL.close()
    SharedWorkerPool.worker_shutdown()

    # Before the database goes: the last flush needs it.
    await stop_reaper_loop(reaper_task)
    await stop_usage_loops(usage_tasks)
    await database.shutdown()


def get_ingest_submit_client() -> IngestSubmitClient:
    """FastAPI dependency: the client the dataset path submits its requests through.

    Typed as the INTERFACE, not as GrpcIngestSubmitClient: ADR tj-8konfu D3's seam is what a
    backtest replay backend (tj-r6vcgv) and a test double implement, so nothing downstream may
    depend on the gRPC implementation. A route overrides this dependency rather than reaching for
    the module global.

    Returns:
        IngestSubmitClient: The client built by the lifespan, or None outside one -- there is no
        client to hand out before startup has run.
    """
    return __INGEST_SUBMIT_CLIENT
