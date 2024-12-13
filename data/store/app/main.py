from contextlib import asynccontextmanager
import os

from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from common.app_lifecycle import startup_logs, teardown_logs
from common.logging import get_logger
from data.store.app.retrieve.data_action_request import store_market_activity_worker
from common.worker_pool import worker_shutdown, worker_startup
from routers.common import ping
from routers.data_store import stock_market_activity_data, store_broker_data
from .db.database import DATABASE_URI, get_instance, wait_for_db

log = get_logger(__name__)

BROKER_NAME = os.getenv("BROKER_NAME")
BROKER_PORT = int(os.getenv("BROKER_PORT"))
BROKER_CONN_TIMEOUT = int(os.getenv("BROKER_CONN_TIMEOUT"))


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
    worker_startup()
    # TODO fix migrations
    # run_migrations()

    # Setup Consumers
    store_market_activity_worker(
        host=BROKER_NAME,
        port=BROKER_PORT,
        timeout=BROKER_CONN_TIMEOUT,
        db=get_instance()
    )
    log.info("Data Store Ready!!!")
    yield
    teardown_logs(app)
    worker_shutdown()
    # cleanup tasks

app = FastAPI(lifespan=lifespan)
app.include_router(ping.router)
app.include_router(stock_market_activity_data.router)
app.include_router(store_broker_data.router)
