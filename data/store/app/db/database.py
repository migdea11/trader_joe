import os

from sqlalchemy.orm import Session
from sqlalchemy import URL

from common.logging import get_logger
from common.postgres.postgres_tools import (
    wait_for_db as common_wait_for_db,
    get_instance as common_get_instance
)

log = get_logger(__name__)

DATABASE_HOST = os.getenv("DATABASE_NAME")
DATABASE_PORT = int(os.getenv("DATABASE_PORT"))
DATABASE_DB_NAME = os.getenv("POSTGRES_DB_NAME")
DATABASE_USER = os.getenv("POSTGRES_USER")
DATABASE_PASS = os.getenv("POSTGRES_PASS")

DATABASE_URI = URL.create(
    "postgresql",
    username=DATABASE_USER,
    password=DATABASE_PASS,
    host=DATABASE_HOST,
    port=DATABASE_PORT,
    database=DATABASE_DB_NAME
)
DATABASE_CONN_TIMEOUT = int(os.getenv("DATABASE_CONN_TIMEOUT"))


def wait_for_db() -> bool:
    return common_wait_for_db(DATABASE_URI, DATABASE_CONN_TIMEOUT)


def get_instance() -> Session:
    uri_str = DATABASE_URI.render_as_string(hide_password=False)
    log.debug(f"WAITING2... {uri_str}")
    return common_get_instance(DATABASE_URI, DATABASE_CONN_TIMEOUT)
