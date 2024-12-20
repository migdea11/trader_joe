from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from common.app_lifecycle import startup_logs, teardown_logs
from common.environment import get_env_var
from common.kafka.kafka_config import ConsumerParams
from common.kafka.kafka_consumer import SharedKafkaConsumer
from common.kafka.topics import ConsumerGroup, StaticTopic
from common.logging import get_logger
from common.worker_pool import SharedWorkerPool
from data.store.app.retrieve.data_action_request import \
    store_market_activity_worker
from routers.common import ping
from routers.data_store import stock_market_activity_data, store_broker_data

from .db.database import DATABASE_URI, get_instance, wait_for_db

log = get_logger(__name__)


BROKER_NAME = get_env_var("BROKER_NAME")
BROKER_PORT = get_env_var("BROKER_PORT", is_num=True)
BROKER_CONN_TIMEOUT = get_env_var("BROKER_CONN_TIMEOUT", is_num=True)


def run_migrations():
    log.debug(f"connected to {DATABASE_URI}")
    if wait_for_db():
        alembic_cfg = Config("/code/alembic.ini")
        alembic_cfg.set_main_option("script_location", "/code/migrations")
        alembic_cfg.set_main_option("sqlalchemy.url", DATABASE_URI.render_as_string(hide_password=False))
        log.info("Running migrations...")
        try:
            command.upgrade(alembic_cfg, "head")
        except Exception as e:
            log.error(f"Error running migrations: {e}")
            raise e
    else:
        raise Exception("Database is not ready.")


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

    # Setup Workers
    store_market_activity_worker(
        host=clientParams.host,
        port=clientParams.port,
        timeout=clientParams.timeout,
        db=get_instance()
    )
    log.info("Data Store App Ready!!!")

    yield

    # cleanup tasks
    teardown_logs(app)
    SharedWorkerPool.worker_shutdown()
    SharedKafkaConsumer.shutdown()

app = FastAPI(lifespan=lifespan)
app.include_router(ping.router)
app.include_router(stock_market_activity_data.router)
app.include_router(store_broker_data.router)
