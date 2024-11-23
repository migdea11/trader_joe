import os
from fastapi import FastAPI
from fastapi.concurrency import asynccontextmanager
from common.kafka.kafka_tools import wait_for_kafka
from routers import store_broker_data

BROKER_NAME = os.getenv("BROKER_NAME")
BROKER_PORT = int(os.getenv("BROKER_PORT"))
BROKER_CONN_TIMEOUT = int(os.getenv("BROKER_CONN_TIMEOUT"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_kafka(BROKER_NAME, BROKER_PORT, BROKER_CONN_TIMEOUT)
    # Other startup tasks
    yield
    # cleanup tasks

app = FastAPI()

app.include_router(store_broker_data.router)
