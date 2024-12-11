from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from common.app_lifecycle import startup_logs, teardown_logs
from common.logging import get_logger
from routers.data_store import stock_market_activity_data, store_broker_data
from .db.database import DATABASE_URL, wait_for_db

log = get_logger(__name__)


def run_migrations():
    log.debug(f"connected to {DATABASE_URL}")
    if wait_for_db():
        alembic_cfg = Config("/code/alembic.ini")
        alembic_cfg.set_main_option("script_location", "/code/migrations")
        alembic_cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
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
    # TODO fix migrations
    # run_migrations()
    yield
    teardown_logs(app)
    # cleanup tasks

app = FastAPI(lifespan=lifespan)
app.include_router(stock_market_activity_data.router)
app.include_router(store_broker_data.router)
