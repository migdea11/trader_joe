from fastapi import FastAPI
from fastapi.concurrency import asynccontextmanager

from common.app_lifecycle import startup_logs, teardown_logs
from common.kafka.kafka_config import get_consumer_params
from common.kafka.messaging.kafka_consumer import KafkaConsumerFactory
from common.kafka.messaging.kafka_producer import KafkaProducerFactory
from common.kafka.topics import ConsumerGroup, RpcEndpointTopic
from common.logging import get_logger
from common.worker_pool import SharedWorkerPool
from routers.data_ingest import get_dataset_request

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup_logs(app)
    SharedWorkerPool.worker_startup()

    # Init Kafka
    consumer_params = get_consumer_params(
        [
            RpcEndpointTopic.STOCK_MARKET_ACTIVITY.request
        ],
        ConsumerGroup.DATA_INGEST_GROUP
    )
    KafkaConsumerFactory.wait_for_kafka(consumer_params)

    # Init RPC Endpoints
    rpc = get_dataset_request.rpc
    rpc_servers = rpc.init_servers()

    log.info("Data Ingest App Ready!!!")
    yield

    teardown_logs(app)
    rpc_servers.shutdown()
    SharedWorkerPool.worker_shutdown()
    KafkaProducerFactory.shutdown()
