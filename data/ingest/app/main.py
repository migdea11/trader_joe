import os
from fastapi import FastAPI
from fastapi.concurrency import asynccontextmanager
from common.app_lifecycle import startup_logs, teardown_logs
from common.kafka.kafka_tools import ProducerParams, wait_for_kafka
from common.worker_pool import worker_shutdown, worker_startup
from routers.common import ping
from routers.data_ingest import get_broker_data

BROKER_NAME = os.getenv("BROKER_NAME")
BROKER_PORT = int(os.getenv("BROKER_PORT"))
BROKER_CONN_TIMEOUT = int(os.getenv("BROKER_CONN_TIMEOUT"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup_logs(app)
    worker_startup()
    wait_for_kafka(ProducerParams(BROKER_NAME, BROKER_PORT, BROKER_CONN_TIMEOUT))
    yield
    teardown_logs(app)
    worker_shutdown()

app = FastAPI(lifespan=lifespan)
app.include_router(ping.router)
app.include_router(get_broker_data.router)
