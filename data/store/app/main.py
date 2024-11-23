import os
from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from common.postgres.postgres_tools import wait_for_db
from routers import stock_price_volume

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_CONN_TIMEOUT = os.getenv("DATABASE_CONN_TIMEOUT")

def run_migrations():
    print(f"connected to {DATABASE_URL}")
    if wait_for_db(DATABASE_URL, DATABASE_CONN_TIMEOUT):
        alembic_cfg = Config("/code/alembic.ini")
        alembic_cfg.set_main_option("script_location", "/code/migrations")
        alembic_cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
        print("Running migrations...")
        command.upgrade(alembic_cfg, "head")
    else:
        raise Exception("Database is not ready.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    # Other startup tasks
    yield
    # cleanup tasks

app = FastAPI(lifespan=lifespan)

app.include_router(stock_price_volume.router)