from sqlalchemy import URL
from sqlalchemy.orm import Session

from common.environment import get_env_var
from common.logging import get_logger
from common.postgres.postgres_tools import SharedPostgresSession

log = get_logger(__name__)

DATABASE_HOST = get_env_var("DATABASE_NAME")
DATABASE_PORT = get_env_var("DATABASE_PORT", is_num=True)
DATABASE_DB_NAME = get_env_var("POSTGRES_DB_NAME")
DATABASE_USER = get_env_var("POSTGRES_USER")
DATABASE_PASS = get_env_var("POSTGRES_PASS")
DATABASE_CONN_TIMEOUT = get_env_var("DATABASE_CONN_TIMEOUT", is_num=True)

DATABASE_URI = URL.create(
    "postgresql",
    username=DATABASE_USER,
    password=DATABASE_PASS,
    host=DATABASE_HOST,
    port=DATABASE_PORT,
    database=DATABASE_DB_NAME
)


def wait_for_db() -> bool:
    return SharedPostgresSession.wait_for_db(DATABASE_URI, DATABASE_CONN_TIMEOUT)


def get_instance() -> Session:
    return SharedPostgresSession.get_instance(DATABASE_URI, DATABASE_CONN_TIMEOUT)
