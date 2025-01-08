import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession
from psycopg2 import OperationalError
from asyncpg import CannotConnectNowError
from sqlalchemy.engine.url import URL
from common.database.postgres_tools import PostgresSessionFactory


@pytest.fixture
def mock_sync_uri() -> URL:
    return PostgresSessionFactory.SyncSession.create_uri(
        host="localhost",
        port=5432,
        database="test_db",
        user="user",
        password="password"
    )


@pytest.fixture
def mock_async_uri() -> URL:
    return PostgresSessionFactory.AsyncSession.create_uri(
        host="localhost",
        port=5432,
        database="test_db",
        user="user",
        password="password"
    )


@pytest.fixture(autouse=True)
def cleanup():
    yield
    PostgresSessionFactory.shutdown()


@patch("common.database.postgres_tools.psycopg2.connect")
@patch("common.database.postgres_tools.create_engine")
@patch("common.database.postgres_tools.sessionmaker")
@patch("common.environment.get_env_var", return_value=True)  # Mock sync enabled
def test_sync_initialize(mock_get_env_var, mock_sessionmaker, mock_create_engine, mock_psycopg_connect, mock_sync_uri: URL, cleanup):
    # Mock psycopg2.connect to simulate successful connection
    mock_psycopg_connect.return_value = MagicMock()

    # Mock create_engine and sessionmaker
    mock_engine = MagicMock(spec=Engine)
    mock_create_engine.return_value = mock_engine

    PostgresSessionFactory.SyncSession.initialize(mock_sync_uri, timeout=0)

    # Assert that connection and engine creation occurred
    mock_psycopg_connect.assert_called_once_with(mock_sync_uri.render_as_string(hide_password=False))
    mock_create_engine.assert_called_once_with(mock_sync_uri.render_as_string(hide_password=False), pool_pre_ping=True)
    mock_sessionmaker.assert_called_once_with(autocommit=False, autoflush=False, bind=mock_engine)


@patch("common.environment.get_env_var", return_value=False)  # Mock sync disabled
def test_sync_disabled(mock_get_env_var, mock_sync_uri: URL, cleanup):
    with pytest.raises(RuntimeError, match="Postgres sync is not enabled."):
        PostgresSessionFactory.SyncSession.wait_for_db(mock_sync_uri, timeout=0)


@patch("common.database.postgres_tools.psycopg2.connect")
@patch("common.environment.get_env_var", return_value=True)  # Mock sync enabled
def test_sync_wait_for_db_timeout(mock_get_env_var, mock_psycopg_connect, mock_sync_uri: URL, cleanup):
    # Simulate psycopg2.connect raising OperationalError
    mock_psycopg_connect.side_effect = OperationalError

    # Test timeout scenario
    result = PostgresSessionFactory.SyncSession.wait_for_db(mock_sync_uri, timeout=1)

    # Assert that the method retried and eventually returned False
    assert result is False
    assert mock_psycopg_connect.call_count > 1


@patch("common.database.postgres_tools.asyncpg.connect", new_callable=AsyncMock)
@patch("common.database.postgres_tools.create_async_engine")
@patch("common.database.postgres_tools.sessionmaker")
@patch("common.environment.get_env_var", return_value=True)  # Mock async enabled
def test_async_initialize(
    mock_get_env_var, mock_sessionmaker, mock_create_async_engine, mock_asyncpg_connect, mock_async_uri: URL, cleanup
):
    # Mock asyncpg.connect to simulate successful connection
    mock_asyncpg_connect.return_value = AsyncMock()

    # Mock create_async_engine and sessionmaker
    mock_engine = MagicMock(spec=AsyncEngine)
    mock_create_async_engine.return_value = mock_engine

    async def async_test():
        await PostgresSessionFactory.AsyncSession.initialize(mock_async_uri, timeout=0)

        # Assert that connection and engine creation occurred
        mock_asyncpg_connect.assert_called_once_with(mock_async_uri.render_as_string(hide_password=False))
        mock_create_async_engine.assert_called_once_with(
            mock_async_uri.render_as_string(hide_password=False), pool_pre_ping=True
        )
        mock_sessionmaker.assert_called_once_with(
            bind=mock_engine, class_=AsyncSession, autocommit=False, autoflush=False
        )

    pytest.run(async_test())


@patch("common.environment.get_env_var", return_value=False)  # Mock async disabled
def test_async_disabled(mock_get_env_var, mock_async_uri: URL, cleanup):
    with pytest.raises(RuntimeError, match="Postgres async is not enabled."):
        async def async_test():
            await PostgresSessionFactory.AsyncSession.wait_for_db(mock_async_uri, timeout=0)

        pytest.run(async_test())


@patch("common.database.postgres_tools.asyncpg.connect", new_callable=AsyncMock)
@patch("common.environment.get_env_var", return_value=True)  # Mock async enabled
def test_async_wait_for_db_timeout(mock_get_env_var, mock_asyncpg_connect, mock_async_uri: URL, cleanup):
    # Simulate asyncpg.connect raising CannotConnectNowError
    mock_asyncpg_connect.side_effect = CannotConnectNowError

    async def async_test():
        result = await PostgresSessionFactory.AsyncSession.wait_for_db(mock_async_uri, timeout=1)

        # Assert that the method retried and eventually returned False
        assert result is False
        assert mock_asyncpg_connect.call_count > 1

    pytest.run(async_test())
