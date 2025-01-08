from contextlib import asynccontextmanager

from fastapi import FastAPI

from common.app_lifecycle import startup_logs, teardown_logs
from common.environment import get_env_var
from common.kafka.kafka_config import ConsumerParams
from common.kafka.kafka_consumer import SharedKafkaConsumer
from common.kafka.topics import ConsumerGroup, StaticTopic
from common.logging import get_logger
from common.worker_pool import SharedWorkerPool
from data.store.app.db import database
from data.store.app.retrieve.data_action_request import \
    store_market_activity_worker
from routers.common import ping
from routers.data_store import (asset_market_activity_data,
                                store_dataset_request)

log = get_logger(__name__)


BROKER_NAME = get_env_var("BROKER_NAME")
BROKER_PORT = get_env_var("BROKER_PORT", cast_type=int)
BROKER_CONN_TIMEOUT = get_env_var("BROKER_CONN_TIMEOUT", cast_type=int)


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup_logs(app)
    SharedWorkerPool.worker_startup()

    # Setup Kafka for all topics
    clientParams = ConsumerParams(
        BROKER_NAME,
        BROKER_PORT,
        [StaticTopic.STOCK_MARKET_ACTIVITY],
        ConsumerGroup.DATA_STORE_GROUP,
        False,
        BROKER_CONN_TIMEOUT
    )
    SharedKafkaConsumer.wait_for_kafka(clientParams)

    # Setup Database
    await database.initialize()
    # Setup Workers
    store_market_activity_worker(
        host=clientParams.host,
        port=clientParams.port,
        timeout=clientParams.timeout
    )
    log.info("Data Store App Ready!!!")

    yield

    # cleanup tasks
    teardown_logs(app)
    SharedWorkerPool.worker_shutdown()
    SharedKafkaConsumer.shutdown()

    await database.shutdown()

app = FastAPI(lifespan=lifespan)
app.include_router(ping.router)
app.include_router(asset_market_activity_data.router)
app.include_router(store_dataset_request.router)
