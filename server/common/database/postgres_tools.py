import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import ClassVar

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from common.environment import get_env_var
from common.logging import get_logger


log = get_logger(__name__)

_POSTGRES_ASYNC_ENABLED = get_env_var('POSTGRES_ASYNC', default=False, cast_type=bool)
_POSTGRES_SYNC_ENABLED = get_env_var('POSTGRES_SYNC', default=False, cast_type=bool)
if _POSTGRES_ASYNC_ENABLED is True:
    log.info('Postgres async is enabled.')
    import asyncio

    import asyncpg
if _POSTGRES_SYNC_ENABLED is True:
    log.info('Postgres sync is enabled.')
    import psycopg2


class PostgresSessionFactory:
    """Creates handle to create and manage Postgres database async sessions."""

    _active_db_uris: ClassVar[set[str]] = set()

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

        _async_engines: ClassVar[dict[int, AsyncEngine]] = {}
        _async_session_makers: ClassVar[dict[int, async_sessionmaker[AsyncSession]]] = {}

        @staticmethod
        def create_uri(host: str, port: int, database: str, user: str, password: str) -> URL:
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
                'postgresql+asyncpg', username=user, password=password, host=host, port=port, database=database
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
                raise RuntimeError('Postgres async is not enabled.')

            start_time = time.time()
            # asyncpg doesn't support the SQLAlchemy's 'postgresql+asyncpg' drivername
            internal_uri = uri.set(drivername='postgresql')
            uri_str = PostgresSessionFactory._get_display_uri(internal_uri)
            while True:
                try:
                    conn = await asyncpg.connect(internal_uri.render_as_string(hide_password=False))
                    await conn.close()
                    log.info(f'Postgres is ready for {uri_str}!')
                    return True
                except (asyncpg.CannotConnectNowError, asyncpg.PostgresError) as e:
                    elapsed_time = time.time() - start_time
                    if elapsed_time >= timeout:
                        log.error(f'Failed to connect to Postgres {uri_str} after {timeout} seconds: {e}')
                        return False
                    log.debug(f'Waiting for Postgres {uri_str} to be ready...')
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
            # The dicts are keyed by the db hash, so the guard must test the hash.
            db_hash = PostgresSessionFactory._get_db_hash(uri)
            if db_hash in cls._async_engines:
                raise RuntimeError(f'Session factory already initialized for {uri_str}.')

            if not await cls.wait_for_db(uri, timeout):
                raise ConnectionError(f'Database startup timed out for {uri_str}.')

            # Initialize async engine and session
            async_engine = create_async_engine(uri.render_as_string(hide_password=False), pool_pre_ping=True)
            cls._async_engines[db_hash] = async_engine
            cls._async_session_makers[db_hash] = async_sessionmaker(async_engine, autocommit=False, autoflush=False)

        @classmethod
        @asynccontextmanager
        async def session(cls, uri: URL) -> AsyncIterator[AsyncSession]:
            """Open an async session for the given database URI, closed when the block exits.

            The session comes from a plain async_sessionmaker, not a registry. Nothing here
            commits and nothing here rolls back: transaction boundaries belong to the caller
            (data_store's write_transaction). On exit, AsyncSession.__aexit__ closes the
            session, which rolls back any transaction still open -- that is how a read ends --
            and returns the connection to the pool.

            There is deliberately no scoped registry behind this. A Task-keyed registry keeps
            every session and its Task alive until someone calls remove(), and a caller that
            forgets leaks a pooled connection per request (ADR tj-8z213c). The context manager
            makes the owner of the session explicit instead.

            expire_on_commit is left at its default (True) deliberately (ADR tj-8z213c,
            Addendum 1 (i)): no code reads ORM attributes after a commit, and the one caller
            that returns an ORM object after committing refreshes it explicitly.

            Args:
                uri (URL): Postgres URI.

            Raises:
                RuntimeError: Session Handle not initialized.

            Yields:
                AsyncSession: Postgres async session, closed when the block exits.
            """
            db_hash = PostgresSessionFactory._get_db_hash(uri)
            if db_hash not in cls._async_session_makers:
                raise RuntimeError(
                    f'Session handle not initialized for {PostgresSessionFactory._get_display_uri(uri)}.'
                )

            async with cls._async_session_makers[db_hash]() as session:
                yield session

    class SyncSession:
        """Creates and manages a sync Postgres engine and a plain sessionmaker.

        Exposed only through session() (ADR tj-8z213c).
        """

        _sync_engines: ClassVar[dict[int, Engine]] = {}
        _sync_session_makers: ClassVar[dict[int, sessionmaker[Session]]] = {}

        @staticmethod
        def create_uri(host: str, port: int, database: str, user: str, password: str) -> URL:
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
                'postgresql+psycopg2', username=user, password=password, host=host, port=port, database=database
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
                raise RuntimeError('Postgres sync is not enabled.')

            start_time = time.time()
            uri_str = PostgresSessionFactory._get_display_uri(uri)
            while True:
                try:
                    conn = psycopg2.connect(uri.render_as_string(hide_password=False))
                    conn.close()
                    log.info(f'Postgres is ready for {uri_str}!')
                    return True
                except psycopg2.OperationalError as e:
                    elapsed_time = time.time() - start_time
                    if elapsed_time >= timeout:
                        log.error(f'Failed to connect to Postgres {uri_str} after {timeout} seconds: {e}')
                        return False
                    log.debug(f'Waiting for Postgres {uri_str} to be ready...')
                    time.sleep(retry)

        @classmethod
        def initialize(cls, uri: URL, timeout: int, retry: int = 1):
            """Initialize sync engines and session factories (synchronous)."""
            uri_str = PostgresSessionFactory._get_display_uri(uri)
            # The dicts are keyed by the db hash, so the guard must test the hash.
            db_hash = PostgresSessionFactory._get_db_hash(uri)
            if db_hash in cls._sync_engines:
                raise RuntimeError(f'Session factory already initialized for {uri_str}.')

            if not cls.wait_for_db(uri, timeout, retry):
                raise ConnectionError(f'Database startup timed out for {uri_str}.')

            # Initialize sync engine and session factory. session() is the only accessor.
            sync_engine = create_engine(uri.render_as_string(hide_password=False), pool_pre_ping=True)
            session_maker = sessionmaker(sync_engine, autocommit=False, autoflush=False)
            cls._sync_engines[db_hash] = sync_engine
            cls._sync_session_makers[db_hash] = session_maker
            log.info(f'Postgres sync session factory initialized for {uri_str}.')

        @classmethod
        @contextmanager
        def session(cls, uri: URL) -> Iterator[Session]:
            """Open a sync session for the given database URI, closed when the block exits.

            The session comes from a plain sessionmaker, not a registry. Nothing here commits
            and nothing here rolls back: transaction boundaries belong to the caller. On exit,
            Session.__exit__ closes the session, which rolls back any transaction still open
            and returns the connection to the pool.

            There is deliberately no scoped registry behind this: a registry keeps every
            session alive until someone calls remove(), and a caller that forgets leaks a
            pooled connection (ADR tj-8z213c). The context manager makes the owner explicit.

            expire_on_commit is left at its default (True) deliberately (ADR tj-8z213c,
            Addendum 1 (i)): no code reads ORM attributes after a commit, and the one caller
            that returns an ORM object after committing refreshes it explicitly.

            Args:
                uri (URL): Postgres URI.

            Raises:
                RuntimeError: Session Handle not initialized.

            Yields:
                Session: Postgres sync session, closed when the block exits.
            """
            db_hash = PostgresSessionFactory._get_db_hash(uri)
            if db_hash not in cls._sync_session_makers:
                raise RuntimeError(
                    f'Session handle not initialized for {PostgresSessionFactory._get_display_uri(uri)}.'
                )

            with cls._sync_session_makers[db_hash]() as session:
                yield session

    @classmethod
    async def shutdown(cls):
        """Clean up engines and sessions."""
        for async_engine in cls.AsyncSessionHandle._async_engines.values():
            await async_engine.dispose()
        for sync_engine in cls.SyncSession._sync_engines.values():
            sync_engine.dispose()

        cls.AsyncSessionHandle._async_engines.clear()
        cls.AsyncSessionHandle._async_session_makers.clear()
        cls.SyncSession._sync_engines.clear()
        cls.SyncSession._sync_session_makers.clear()
        log.info('Postgres session factory shut down.')
