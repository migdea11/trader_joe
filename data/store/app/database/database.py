from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from common.database.postgres_tools import PostgresSessionFactory
from common.environment import get_env_var
from common.logging import get_logger

log = get_logger(__name__)

DATABASE_HOST = get_env_var("DATABASE_NAME")
DATABASE_PORT = get_env_var("DATABASE_PORT", cast_type=int)
DATABASE_DB_NAME = get_env_var("POSTGRES_DB_NAME")
DATABASE_USER = get_env_var("POSTGRES_USER")
DATABASE_PASS = get_env_var("POSTGRES_PASS")
DATABASE_CONN_TIMEOUT = get_env_var("DATABASE_CONN_TIMEOUT", cast_type=int)

DATABASE_ASYNC_URI = PostgresSessionFactory.AsyncSessionHandle.create_uri(
    DATABASE_HOST, DATABASE_PORT, DATABASE_DB_NAME, DATABASE_USER, DATABASE_PASS
)


async def initialize():
    await PostgresSessionFactory.AsyncSessionHandle.initialize(DATABASE_ASYNC_URI, DATABASE_CONN_TIMEOUT)


async def shutdown():
    await PostgresSessionFactory.shutdown()


# def get_db() -> Generator[Session, None, None]:
#     yield PostgresSessionFactory.SyncSession.get_session(DATABASE_URI)


async def async_db() -> AsyncGenerator[AsyncSession, None]:
    yield PostgresSessionFactory.AsyncSessionHandle.get_session(DATABASE_ASYNC_URI)
