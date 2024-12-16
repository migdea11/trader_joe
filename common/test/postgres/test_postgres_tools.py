import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy.engine import Engine
from psycopg2 import OperationalError
from sqlalchemy.engine.url import URL
from common.postgres.postgres_tools import SharedPostgresSession


@pytest.fixture
def mock_uri():
    return URL.create(
        drivername="postgresql",
        username="user",
        password="password",
        host="localhost",
        port=5432,
        database="test_db"
    )


@pytest.fixture(autouse=True)
def cleanup():
    yield
    SharedPostgresSession.shutdown()


@patch("common.postgres.postgres_tools.psycopg2.connect")
@patch("common.postgres.postgres_tools.create_engine")
@patch("common.postgres.postgres_tools.sessionmaker")
def test_successful_get_instance(mock_sessionmaker, mock_create_engine, mock_psycopg_connect, mock_uri, cleanup):
    # Mock psycopg2.connect to simulate successful connection
    mock_psycopg_connect.return_value = MagicMock()

    # Mock create_engine and sessionmaker
    mock_engine = MagicMock(spec=Engine)
    mock_create_engine.return_value = mock_engine

    mock_session_maker_instance = MagicMock()
    mock_sessionmaker.return_value = mock_session_maker_instance

    session = SharedPostgresSession.get_instance(mock_uri, timeout=0)

    # Assert that connection and engine creation occurred
    mock_psycopg_connect.assert_called_once_with(mock_uri.render_as_string(hide_password=False))
    mock_create_engine.assert_called_once_with(mock_uri)
    mock_sessionmaker.assert_called_once_with(autocommit=False, autoflush=False, bind=mock_engine)

    # Assert that a session was returned
    assert session is mock_session_maker_instance()


@patch("common.postgres.postgres_tools.psycopg2.connect")
def test_wait_for_db_timeout(mock_psycopg_connect, mock_uri, cleanup):
    # Simulate psycopg2.connect raising OperationalError
    mock_psycopg_connect.side_effect = OperationalError

    # Test timeout scenario
    result = SharedPostgresSession.wait_for_db(mock_uri, timeout=1)

    # Assert that the method retried and eventually returned False
    assert result is False
    print(mock_psycopg_connect.call_count)
    assert mock_psycopg_connect.call_count > 1


@patch("common.postgres.postgres_tools.psycopg2.connect")
@patch("common.postgres.postgres_tools.create_engine")
@patch("common.postgres.postgres_tools.sessionmaker")
def test_reuse_existing_instance(mock_sessionmaker, mock_create_engine, mock_psycopg_connect, mock_uri, cleanup):
    # Mock psycopg2.connect and engine creation
    mock_psycopg_connect.return_value = MagicMock()
    mock_engine = MagicMock(spec=Engine)
    mock_create_engine.return_value = mock_engine

    # Mock sessionmaker
    mock_session_maker_instance = MagicMock()
    mock_sessionmaker.return_value = mock_session_maker_instance

    # Call get_instance twice with the same URI
    SharedPostgresSession.get_instance(mock_uri, timeout=0)
    SharedPostgresSession.get_instance(mock_uri, timeout=0)

    # Assert connection and engine creation occurred only once
    mock_psycopg_connect.assert_called_once()
    mock_create_engine.assert_called_once_with(mock_uri)
    mock_sessionmaker.assert_called_once_with(autocommit=False, autoflush=False, bind=mock_engine)


@patch("common.postgres.postgres_tools.psycopg2.connect")
def test_get_instance_db_unavailable(mock_psycopg_connect, mock_uri, cleanup):
    # Simulate psycopg2.connect raising OperationalError
    mock_psycopg_connect.side_effect = OperationalError

    # Test that an exception is raised when DB is unavailable
    with pytest.raises(ConnectionError, match="Database startup timed out."):
        SharedPostgresSession.get_instance(mock_uri, timeout=0)
