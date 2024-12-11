import os

from sqlalchemy.orm import Session

from common.postgres.postgres_tools import (
    wait_for_db as common_wait_for_db,
    get_instance as common_get_instance
)

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_CONN_TIMEOUT = os.getenv("DATABASE_CONN_TIMEOUT")


def wait_for_db() -> bool:
    return common_wait_for_db(DATABASE_URL, DATABASE_CONN_TIMEOUT)


def get_instance() -> Session:
    return common_get_instance(DATABASE_URL, DATABASE_CONN_TIMEOUT)