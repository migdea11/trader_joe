import math

import grpc
from fastapi import APIRouter, Depends, FastAPI

from common.endpoints import get_endpoint_url
from common.environment import get_env_var
from common.logging import get_logger
from common.rpc.channel import DATA_INGEST_GRPC_TARGET_ENV, create_channel, target_from_env
from common.rpc.server import ServiceRegistration
from common.timer import Timer
from routers.common.app_endpoints import InterfaceRest
from schemas.common.latency import InternalLatencyRequest, LatencyRequest


log = get_logger(__name__)

LATENCY_TEST_ENABLED = get_env_var('LATENCY_TEST_ENABLED', default=False, cast_type=bool)
LATENCY_TEST_TIMEOUT = get_env_var('LATENCY_TEST_TIMEOUT', default=60, cast_type=int)
__GRPC_CLIENT = None
__REST_CLIENT = None
__APP_NAME = None
__APP_PORT = None


def get_latency_services() -> list[ServiceRegistration]:
    """The servicers the latency harness adds to a gRPC server: its gRPC arm, when the harness is on.

    The harness's one registration point, for data_ingest (data/ingest/app/grpc_host.py,
    registered_services). It replaces the get_latency_topics() shim that served the Kafka arm, which
    went with that arm's last caller. The server is built before the lifespan calls
    initialize_latency_server, so the servicer reaches it through that list rather than from here.

    Returns:
        list[ServiceRegistration]: The latency servicer, or nothing when LATENCY_TEST_ENABLED is off.
    """
    if not LATENCY_TEST_ENABLED:
        return []

    # Imported only when the harness is on. It loads the generated tree (trader_joe.proto), and both
    # services import this module at startup, so a top-level import would load it everywhere.
    from common.rpc.latency import latency_service

    return [latency_service()]


def _percentile(ordered: list[float], percentile: int) -> float:
    """The nearest-rank percentile: the smallest sample with at least that share of all samples at or below it.

    Always a duration that was observed, never an interpolation. Under 100 samples the 99th percentile is
    the slowest call, so p99 means more than 'the maximum' only over a run of 100 iterations or more.

    Args:
        ordered (list[float]): The samples, sorted ascending. At least one.
        percentile (int): From 1 to 100.

    Returns:
        float: The sample at that rank.
    """
    return ordered[math.ceil(percentile * len(ordered) / 100) - 1]


def initialize_latency_client(app: FastAPI, app_name: str, app_port: int):
    if not LATENCY_TEST_ENABLED:
        return

    log.debug('Initializing latency client...')
    global __APP_NAME, __APP_PORT
    if __APP_NAME is not None:
        log.error('Latency test already initialized.')
        return

    # Init gRPC Client, first: a missing or malformed DATA_INGEST_GRPC_TARGET stops startup here, naming
    # the variable, before anything else is built. One channel for the process, never closed (the lifetime
    # ruled on tj-3mk3u5.8), for the same reason as the REST client below: a channel per request would make
    # every sample pay its own connection setup, and the harness exists to compare transports. Nothing
    # closes it because this is a dev-only measurement process that exits with its container -- that is the
    # ruling, not an oversight. The arm is imported only now, for the reason get_latency_services() gives.
    from common.rpc.latency import LatencyProbeClient

    global __GRPC_CLIENT
    channel = create_channel(target_from_env(DATA_INGEST_GRPC_TARGET_ENV))
    __GRPC_CLIENT = LatencyProbeClient(channel, LATENCY_TEST_TIMEOUT)

    __APP_NAME = app_name
    __APP_PORT = app_port

    # Init REST Client. One client for the process, never closed, like the gRPC channel above (the lifetime
    # ruled on tj-3mk3u5.8): a client per request would make every sample pay its own TCP connect, and the
    # harness exists to compare transports, not connection setup.
    # Concurrency is uncapped (max_connections=None) so that iterations never queue inside the client on
    # httpx's default of 100, which would inflate p99 for a reason that is not the transport. Idle
    # keep-alive stays at httpx's default of 20 on measured evidence (tj-3mk3u5.61, 2026-10-03): retaining
    # a large idle pool costs client-side httpcore bookkeeping, about 6x on the second of three
    # back-to-back runs at 200 iterations, and the uvicorn 5 s keep-alive closes hoarded sockets
    # underneath the client anyway. Dev-only harness, and nowhere else.
    import httpx

    global __REST_CLIENT
    __REST_CLIENT = httpx.AsyncClient(
        timeout=LATENCY_TEST_TIMEOUT, limits=httpx.Limits(max_connections=None, max_keepalive_connections=20)
    )

    router = APIRouter()

    @router.get(InterfaceRest.LATENCY)
    async def latency(request: LatencyRequest = Depends()):
        import asyncio
        import os

        log.debug('Measuring latency...')
        inner_timer = Timer()
        payload = ''.join(os.urandom(request.payload_size * 1024).hex())

        # Get REST Client
        rest_client = __REST_CLIENT
        url = get_endpoint_url(__APP_NAME, __APP_PORT, InterfaceRest.INTERNAL_LATENCY.value)

        internal_request = InternalLatencyRequest(payload=payload)

        async def send_rest() -> bool:
            response = await rest_client.post(
                url, content=internal_request.model_dump_json(), headers={'Content-Type': 'application/json'}
            )
            return response.status_code == 200

        async def send_grpc() -> bool:
            try:
                await __GRPC_CLIENT.probe(payload)
            except grpc.aio.AioRpcError as e:
                log.warning(f'gRPC latency probe failed: {e.code().name}: {e.details()}')
                return False
            return True

        if request.latency_type is LatencyRequest.LatencyType.REST:
            log.debug(f'Sending REST request to {url}')
            send_type = send_rest
        elif request.latency_type is LatencyRequest.LatencyType.GRPC:
            send_type = send_grpc
        else:
            return {'success': False, 'error': 'Invalid latency type'}

        # Each sample times one AWAITED call: the clock starts and stops around the await, so it measures
        # the round trip. A synchronous wrapper around the coroutine function would instead stop its clock
        # when the coroutine object is created, before the call has run at all.
        async def timed_send() -> tuple[bool, float]:
            key = inner_timer.tick()
            success = await send_type()
            return success, inner_timer.tock(key)

        iterations = request.iterations or 1
        outer_timer = Timer()
        outer_timer.tick()
        results = await asyncio.gather(*[timed_send() for _ in range(iterations)])
        outer_timer.tock()
        if not all(success for success, _ in results):
            return {'success': False, 'error': 'Failed to send request'}

        # p50 and p99 per call, never a mean alone: a mean hides the tail the comparison is about.
        samples = sorted(duration for _, duration in results)
        return {
            'success': True,
            'p50_latency': _percentile(samples, 50),
            'p99_latency': _percentile(samples, 99),
            'samples': len(samples),
            'latency': inner_timer.total_time() / len(samples),
            'total_time': outer_timer.total_time(),
            'iterations': iterations,
        }

    app.include_router(router)


def initialize_latency_server(app: FastAPI, app_name: str, app_port: int):
    if not LATENCY_TEST_ENABLED:
        return

    log.debug('Initializing latency server...')
    global __APP_NAME, __APP_PORT
    if __APP_NAME is not None:
        log.error('Latency test already initialized.')
        return

    __APP_NAME = app_name
    __APP_PORT = app_port

    # Init REST Server
    router = APIRouter()

    @router.post(InterfaceRest.INTERNAL_LATENCY)
    async def latency_test(request: InternalLatencyRequest):
        return {'test': True}

    app.include_router(router)
