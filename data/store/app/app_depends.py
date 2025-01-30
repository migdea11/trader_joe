from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from common.app_lifecycle import startup_logs, teardown_logs
from common.database.postgres_tools import PostgresSessionFactory
from common.kafka.kafka_config import get_consumer_params, get_rpc_params
from common.kafka.kafka_rpc_factory import KafkaRpcFactory
from common.kafka.messaging.kafka_consumer import KafkaConsumerFactory
from common.kafka.topics import ConsumerGroup, RpcEndpointTopic
from common.logging import get_logger
from common.worker_pool import SharedWorkerPool
from data.store.app.database import database
from routers.common.latency import get_latency_topics, initialize_latency_client
from routers.data_ingest.app_endpoints import (
    InterfaceRpc,
    APP_NAME as INGEST_APP_NAME,
    APP_PORT_INTERNAL as INGEST_APP_PORT
)

log = get_logger(__name__)

__RPC_CLIENTS = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init Common Endpoints
    # Using different app for REST latency, ensuring test isn't affected by client and server are on the same thread.
    initialize_latency_client(app, INGEST_APP_NAME, INGEST_APP_PORT, ConsumerGroup.COMMON_GROUP)

    startup_logs(app)
    SharedWorkerPool.worker_startup()

    # Setup Database
    await database.initialize()

    # Init Kafka
    consumer_params = get_consumer_params(
        [
            RpcEndpointTopic.STOCK_MARKET_ACTIVITY.request,
            *get_latency_topics()
        ],
        ConsumerGroup.DATA_STORE_GROUP
    )
    KafkaConsumerFactory.wait_for_kafka(consumer_params)

    # Init RPC Endpoints
    rpc = KafkaRpcFactory(get_rpc_params(ConsumerGroup.DATA_STORE_GROUP))
    rpc.add_client(InterfaceRpc.INGEST_DATASET)
    global __RPC_CLIENTS
    __RPC_CLIENTS = rpc.init_clients()

    log.info("Data Store App Ready!!!")
    yield

    # cleanup tasks
    teardown_logs(app)
    __RPC_CLIENTS.shutdown()
    await database.shutdown()
    SharedWorkerPool.worker_shutdown()

    await database.shutdown()


@asynccontextmanager
async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    session = PostgresSessionFactory.AsyncSessionHandle.get_session(database.DATABASE_ASYNC_URI)
    try:
        yield session
    finally:
        await session.close()


def get_rpc_clients() -> KafkaRpcFactory.RpcClients:
    return __RPC_CLIENTS
