import os
import time
import psycopg2
from fastapi import FastAPI
from alembic.config import Config
from alembic import command
from contextlib import asynccontextmanager

from routers import items

# TODO move to config
CONNECTION_ATTEMPTS = 5
CONNECTION_DELAY = 5

def wait_for_db(url):
    """Wait for the database to become available."""
    attempt = 0
    while attempt < CONNECTION_ATTEMPTS:
        try:
            conn = psycopg2.connect(url)
            conn.close()
            print("Database is ready!")
            return True
        except psycopg2.OperationalError:
            print(f"Database not ready, waiting for {CONNECTION_DELAY} seconds...")
            time.sleep(CONNECTION_DELAY)
            attempt += 1
    print("Database failed to start within the allocated time.")
    return False

def run_migrations():
    URL = os.getenv("DATABASE_URL")
    print(f"connected to {URL}")
    if wait_for_db(URL):
        alembic_cfg = Config("/code/alembic.ini")
        alembic_cfg.set_main_option("script_location", "/code/migrations")
        alembic_cfg.set_main_option("sqlalchemy.url", URL)
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

app.include_router(items.router)