import time
from typing import Dict

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_scoped_session, create_async_engine
)
from sqlalchemy.orm import Session, scoped_session, sessionmaker

from common.environment import get_env_var
from common.logging import get_logger

log = get_logger(__name__)

_POSTGRES_ASYNC_ENABLED = get_env_var("POSTGRES_ASYNC", default=False, cast_type=bool)
_POSTGRES_SYNC_ENABLED = get_env_var("POSTGRES_SYNC", default=False, cast_type=bool)
if _POSTGRES_ASYNC_ENABLED is True:
    log.info("Postgres async is enabled.")
    print("Postgres async is enabled.")
    import asyncio
    import asyncpg
if _POSTGRES_SYNC_ENABLED is True:
    log.info("Postgres sync is enabled.")
    print("Postgres sync is enabled.")
    import psycopg2


class PostgresSessionFactory:
    """Creates handle to create and manage Postgres database async sessions."""
    _active_db_uris = set()

    @staticmethod
    def _get_display_uri(uri: URL) -> str:
        """Get a display URI for logging purposes.

        Args:
            uri (URL): URI to be formatted.

        Returns:
            str: formatted URI.
        """
        return uri.render_as_string(hide_password=True)

    @classmethod
    def _get_db_hash(cls, uri: URL) -> int:
        """Hashes the database URI for internal use.

        Args:
            uri (URL): URI to be hashed.

        Returns:
            int: hashed URI.
        """
        return hash(cls._get_display_uri(uri))

    class AsyncSessionHandle:
        """Postgres session handle."""
        _async_engines: Dict[str, AsyncEngine] = {}
        _async_sessions: Dict[str, async_scoped_session] = {}

        @staticmethod
        def create_uri(
            host: str, port: int, database: str, user: str, password: str
        ) -> URL:
            """Creates Postgres URI.

            Args:
                host (str): Postgres host.
                port (int): Postgres port.
                database (str): Postgres database name.
                user (str): database user.
                password (str): database password.

            Returns:
                URL: Postgres URI.
            """
            return URL.create(
                "postgresql+asyncpg",
                username=user,
                password=password,
                host=host,
                port=port,
                database=database
            )

        @classmethod
        async def wait_for_db(cls, uri: URL, timeout: int, retry: int = 1) -> bool:
            """Wait for the database to be ready (asynchronous) using asyncpg.

            Args:
                uri (URL): Postgres URI.
                timeout (int): Connection timeout.
                retry (int, optional): Attempts to connect. Defaults to 1.

            Raises:
                RuntimeError: Attempting to connect when async is not enabled.

            Returns:
                bool: Flag indicating if the database is ready.
            """
            if _POSTGRES_ASYNC_ENABLED is False:
                raise RuntimeError("Postgres async is not enabled.")

            start_time = time.time()
            # asyncpg doesn't support the SQLAlchemy's 'postgresql+asyncpg' drivername
            internal_uri = uri.set(drivername="postgresql")
            uri_str = PostgresSessionFactory._get_display_uri(internal_uri)
            while True:
                try:
                    conn = await asyncpg.connect(internal_uri.render_as_string(hide_password=False))
                    await conn.close()
                    log.info(f"Postgres is ready for {uri_str}!")
                    return True
                except (asyncpg.CannotConnectNowError, asyncpg.PostgresError) as e:
                    elapsed_time = time.time() - start_time
                    if elapsed_time >= timeout:
                        log.error(f"Failed to connect to Postgres {uri_str} after {timeout} seconds: {e}")
                        return False
                    log.debug(f"Waiting for Postgres {uri_str} to be ready...")
                    await asyncio.sleep(retry)

        @classmethod
        async def initialize(cls, uri: URL, timeout: int):
            """Initialize async session handle.

            Args:
                uri (URL): Postgres URI.
                timeout (int): Connection timeout.

            Raises:
                RuntimeError: Connection already initialized.
                ConnectionError: Failed to connect to the database.
            """
            uri_str = PostgresSessionFactory._get_display_uri(uri)
            if uri_str in cls._async_engines:
                raise RuntimeError(f"Session factory already initialized for {uri_str}.")

            if not await cls.wait_for_db(uri, timeout):
                raise ConnectionError(f"Database startup timed out for {uri_str}.")

            # Initialize async engine and session
            async_engine = create_async_engine(uri.render_as_string(hide_password=False), pool_pre_ping=True)
            db_hash = PostgresSessionFactory._get_db_hash(uri)
            cls._async_engines[db_hash] = async_engine
            cls._async_sessions[db_hash] = async_scoped_session(
                sessionmaker(
                    async_engine, class_=AsyncSession, autocommit=False, autoflush=False
                ),
                scopefunc=asyncio.current_task
            )

        @classmethod
        def get_session(cls, uri: URL) -> AsyncSession:
            """Get an async session for the given database URI.

            Args:
                uri (URL): Postgres URI.

            Raises:
                RuntimeError: Session Handle not initialized.

            Returns:
                AsyncSession: Postgres async session.
            """
            db_hash = PostgresSessionFactory._get_db_hash(uri)
            if db_hash not in cls._async_sessions:
                raise RuntimeError(
                    f"Session handle not initialized for {PostgresSessionFactory._get_display_uri(uri)}."
                )

            temp = cls._async_sessions[db_hash]()
            log.debug(f"Session[{type(temp)}]: {temp}")
            return temp

    class SyncSession:
        """Creates handle to create and manage Postgres database async sessions."""
        _sync_engines: Dict[str, Engine] = {}
        _sync_sessions: Dict[str, scoped_session] = {}

        @staticmethod
        def create_uri(
            host: str, port: int, database: str, user: str, password: str
        ) -> URL:
            """Create Postgres URI.

            Args:
                host (str): Postgres host.
                port (int): Postgres port.
                database (str): Postgres database name.
                user (str): database user.
                password (str): database password.

            Returns:
                URL: Postgres URI.
            """
            return URL.create(
                "postgresql+psycopg2",
                username=user,
                password=password,
                host=host,
                port=port,
                database=database
            )

        @classmethod
        def wait_for_db(cls, uri: URL, timeout: int, retry: int = 1) -> bool:
            """Wait for the database to be ready (synchronous) using psycopg2.

            Args:
                uri (URL): Postgres URI.
                timeout (int): Connection timeout.
                retry (int, optional): Attempts to connect. Defaults to 1.

            Raises:
                RuntimeError: Attempting to connect when sync is not enabled.

            Returns:
                bool: Flag indicating if the database is ready.
            """
            if _POSTGRES_SYNC_ENABLED is False:
                raise RuntimeError("Postgres sync is not enabled.")

            start_time = time.time()
            uri_str = PostgresSessionFactory._get_display_uri(uri)
            while True:
                try:
                    conn = psycopg2.connect(uri.render_as_string(hide_password=False))
                    conn.close()
                    log.info(f"Postgres is ready for {uri_str}!")
                    return True
                except psycopg2.OperationalError as e:
                    elapsed_time = time.time() - start_time
                    if elapsed_time >= timeout:
                        log.error(f"Failed to connect to Postgres {uri_str} after {timeout} seconds: {e}")
                        return False
                    log.debug(f"Waiting for Postgres {uri_str} to be ready...")
                    time.sleep(retry)

        @classmethod
        def initialize(cls, uri: URL, timeout: int, retry: int = 1):
            """
            Initialize sync engines and session factories (synchronous).
            """
            uri_str = PostgresSessionFactory._get_display_uri(uri)
            if uri_str in cls._sync_engines:
                raise RuntimeError(f"Session factory already initialized for {uri_str}.")

            if not cls.wait_for_db(uri, timeout, retry):
                raise ConnectionError(f"Database startup timed out for {uri_str}.")

            # Initialize sync engine and session
            sync_engine = create_engine(uri.render_as_string(hide_password=False), pool_pre_ping=True)
            db_hash = PostgresSessionFactory._get_db_hash(uri)
            cls._sync_engines[db_hash] = sync_engine
            cls._sync_sessions[db_hash] = scoped_session(
                sessionmaker(sync_engine, autocommit=False, autoflush=False)
            )
            log.info(f"Postgres sync session factory initialized for {uri_str}.")

        @classmethod
        def get_session(cls, uri: URL) -> Session:
            """Get a sync session for the given database URI.

            Args:
                uri (URL): Postgres URI.

            Raises:
                RuntimeError: Session Handle not initialized.

            Returns:
                Session: Postgres sync session.
            """
            db_hash = PostgresSessionFactory._get_db_hash(uri)
            if db_hash not in cls._sync_sessions:
                raise RuntimeError(
                    f"Session handle not initialized for {PostgresSessionFactory._get_display_uri(uri)}."
                )

            return cls._sync_sessions[db_hash]()

    @classmethod
    async def shutdown(cls):
        """Clean up engines and sessions."""
        for async_engine in cls.AsyncSessionHandle._async_engines.values():
            await async_engine.dispose()
        for sync_engine in cls.SyncSession._sync_engines.values():
            sync_engine.dispose()

        cls.AsyncSessionHandle._async_engines.clear()
        cls.AsyncSessionHandle._async_sessions.clear()
        cls.SyncSession._sync_engines.clear()
        cls.SyncSession._sync_sessions.clear()
        log.info("Postgres session factory shut down.")
