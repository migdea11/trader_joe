
from fastapi import APIRouter, Depends, FastAPI

from common.endpoints import get_endpoint_url
from common.environment import get_env_var
from common.kafka.kafka_config import get_rpc_params
from common.kafka.kafka_rpc_factory import KafkaRpcFactory
from common.kafka.rpc.kafka_rpc_base import BaseRpcAck
from common.kafka.topics import ConsumerGroup, RpcEndpointTopic
from common.logging import get_logger
from common.timer import Timer, timeit
from routers.common.app_endpoints import InterfaceRest, InterfaceRpc
from schemas.common.latency import InternalLatencyRequest, LatencyRequest


log = get_logger(__name__)

LATENCY_TEST_ENABLED = get_env_var("LATENCY_TEST_ENABLED", default=False, cast_type=bool)
LATENCY_TEST_TIMEOUT = get_env_var("LATENCY_TEST_TIMEOUT", default=30, cast_type=int)
__PRC_CLIENTS = None
__RPC_SERVERS = None
__APP_NAME = None
__APP_PORT = None


def get_latency_topics():
    if not LATENCY_TEST_ENABLED:
        return tuple()

    return (
        RpcEndpointTopic.LATENCY_TEST.request,
        RpcEndpointTopic.LATENCY_TEST.response
    )


def initialize_latency_client(app: FastAPI, app_name: str, app_port: int, client_group: ConsumerGroup):
    if not LATENCY_TEST_ENABLED:
        return

    log.debug("Initializing latency client...")
    global __APP_NAME, __APP_PORT
    if __APP_NAME is not None:
        log.error("Latency test already initialized.")
        return

    __APP_NAME = app_name
    __APP_PORT = app_port

    # Init RPC Client
    rpc_client = KafkaRpcFactory(get_rpc_params(client_group))
    rpc_client.add_client(InterfaceRpc.LATENCY, LATENCY_TEST_TIMEOUT)

    global __PRC_CLIENTS
    __PRC_CLIENTS = rpc_client.init_clients()

    # Init REST Client
    router = APIRouter()

    @router.get(InterfaceRest.LATENCY)
    async def latency(request: LatencyRequest = Depends()):
        import httpx
        import os
        import asyncio

        from typing import List

        log.debug("Measuring latency...")
        inner_timer = Timer()
        payload = ''.join(os.urandom(request.payload_size * 1024).hex())

        # Get REST Client
        rest_client = httpx.AsyncClient(timeout=LATENCY_TEST_TIMEOUT)
        url = get_endpoint_url(__APP_NAME, __APP_PORT, InterfaceRest.INTERNAL_LATENCY.value)

        # Get RPC Client
        rpc_client = __PRC_CLIENTS.get_client(InterfaceRpc.LATENCY)
        internal_request = InternalLatencyRequest(payload=payload)

        @timeit(inner_timer)
        async def send_rest() -> bool:
            response = await rest_client.post(url, data=internal_request.model_dump_json())
            return response.status_code == 200

        @timeit(inner_timer)
        async def send_rpc_kafka():
            response = await rpc_client.send_request(internal_request)
            return response.success is BaseRpcAck.Success.SUCCESS

        if request.latency_type is LatencyRequest.LatencyType.RPC_KAFKA:
            send_type = send_rpc_kafka
        elif request.latency_type is LatencyRequest.LatencyType.REST:
            log.debug(f"Sending REST request to {url}")
            send_type = send_rest
        else:
            return {"success": False, "error": "Invalid latency type"}

        tasks = []
        outer_timer = Timer()
        outer_timer.tick()
        for _ in range(request.iterations or 1):
            tasks.append(send_type())

        results: List[BaseRpcAck] = await asyncio.gather(*tasks)
        outer_timer.tock()
        for result in results:
            if result is False:
                return {"success": False, "error": "Failed to send request"}
        total_time = inner_timer.total_time()
        latency = total_time / (request.iterations or 1)
        return {
            "success": True,
            "latency": latency,
            "mean_latency": total_time,
            "total_time": outer_timer.total_time(),
            "iterations": request.iterations or 1
        }

    app.include_router(router)


def initialize_latency_server(app: FastAPI, app_name: str, app_port: int, server_group: ConsumerGroup):
    if not LATENCY_TEST_ENABLED:
        return

    log.debug("Initializing latency server...")
    global __APP_NAME, __APP_PORT
    if __APP_NAME is not None:
        log.error("Latency test already initialized.")
        return

    __APP_NAME = app_name
    __APP_PORT = app_port

    # Init RPC Server
    rpc_server = KafkaRpcFactory(get_rpc_params(server_group))

    @rpc_server.add_server(InterfaceRpc.LATENCY)
    async def server_callback(request: InternalLatencyRequest) -> BaseRpcAck:
        return BaseRpcAck()

    global __RPC_SERVERS
    __RPC_SERVERS = rpc_server.init_servers()

    # Init REST Server
    router = APIRouter()

    @router.post(InterfaceRest.INTERNAL_LATENCY)
    async def latency_test(request: InternalLatencyRequest):
        return {"test": True}

    app.include_router(router)
