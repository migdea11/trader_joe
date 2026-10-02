from collections.abc import Mapping

from fastapi import FastAPI
from fastapi.concurrency import asynccontextmanager

from common.app_lifecycle import init_debugger, startup_logs, teardown_logs
from common.enums.data_stock import DataSource
from common.kafka.kafka_config import get_consumer_params
from common.kafka.messaging.kafka_consumer import KafkaConsumerFactory
from common.kafka.messaging.kafka_producer import KafkaProducerFactory
from common.kafka.topics import ConsumerGroup, RpcEndpointTopic
from common.logging import get_logger
from common.worker_pool import SharedWorkerPool
from data.ingest.app import ingest_control
from data.ingest.app.brokers.interface import BrokerRead
from routers.common.latency import get_latency_topics, initialize_latency_server
from routers.data_ingest import get_dataset_request
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
        # Init Common Endpoints
        # Using different app for REST latency, ensuring test isn't affected by client and server are on the same thread.
        initialize_latency_server(app, STORE_APP_NAME, STORE_APP_PORT, ConsumerGroup.COMMON_GROUP)
        init_debugger()

        startup_logs(app)
        SharedWorkerPool.worker_startup()

        # Init Kafka
        consumer_params = get_consumer_params(
            [RpcEndpointTopic.STOCK_MARKET_ACTIVITY.request, *get_latency_topics()], ConsumerGroup.DATA_INGEST_GROUP
        )
        KafkaConsumerFactory.wait_for_kafka(consumer_params)

        # Handles are installed BEFORE the RPC servers start, so no request can arrive unserved.
        ingest_control.install_readers(readers)

        # Init RPC Endpoints
        rpc = get_dataset_request.rpc
        rpc_servers = rpc.init_servers()

        log.info('Data Ingest App Ready!!!')
        yield

        teardown_logs(app)
        rpc_servers.shutdown()
        ingest_control.clear_readers()
        SharedWorkerPool.worker_shutdown()
        KafkaProducerFactory.shutdown()

    return lifespan
