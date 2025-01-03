from fastapi import FastAPI
from fastapi.concurrency import asynccontextmanager

from common.app_lifecycle import startup_logs, teardown_logs
from common.environment import get_env_var
from common.kafka.kafka_config import ProducerParams
from common.kafka.kafka_producer import SharedKafkaProducer
from common.worker_pool import SharedWorkerPool
from routers.common import ping
from routers.data_ingest import get_dataset_request

BROKER_NAME = get_env_var("BROKER_NAME")
BROKER_PORT = get_env_var("BROKER_PORT", is_num=True)
BROKER_CONN_TIMEOUT = get_env_var("BROKER_CONN_TIMEOUT", is_num=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup_logs(app)
    SharedWorkerPool.worker_startup()
    SharedKafkaProducer.wait_for_kafka(ProducerParams(BROKER_NAME, BROKER_PORT, BROKER_CONN_TIMEOUT))
    yield
    teardown_logs(app)
    SharedWorkerPool.worker_shutdown()
    SharedKafkaProducer.shutdown()

app = FastAPI(lifespan=lifespan)
app.include_router(ping.router)
app.include_router(get_dataset_request.router)
