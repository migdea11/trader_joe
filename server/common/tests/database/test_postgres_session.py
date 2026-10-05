"""Pins for the context-managed session() accessor on both Postgres handles (bug tj-vhboky.76).

ADR tj-8z213c replaced a Task-scoped session registry, whose sessions nothing closed, with a
plain session factory behind a context manager. These tests drive the production initialize()
and session() against real SQLite pools (aiosqlite for the async handle, the stdlib driver for
the sync one), so a session that is not closed shows up as a connection still checked out of
the pool. Only the Postgres readiness probe and the engine URL are substituted; the engine
options and the session factory are the ones initialize() builds.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import create_engine as real_create_engine
from sqlalchemy import text
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_scoped_session
from sqlalchemy.ext.asyncio import create_async_engine as real_create_async_engine
from sqlalchemy.orm import Session, scoped_session


os.environ['POSTGRES_ASYNC'] = 'True'
os.environ['POSTGRES_SYNC'] = 'True'

from common.database.postgres_tools import PostgresSessionFactory


AsyncHandle = PostgresSessionFactory.AsyncSessionHandle
SyncHandle = PostgresSessionFactory.SyncSession


class _BlockError(Exception):
    """Raised inside a session() block to prove the original exception propagates."""


@pytest.fixture
def async_uri() -> URL:
    return AsyncHandle.create_uri(host='localhost', port=5432, database='leak_db', user='user', password='pw')


@pytest.fixture
def sync_uri() -> URL:
    return SyncHandle.create_uri(host='localhost', port=5432, database='leak_db', user='user', password='pw')


@pytest_asyncio.fixture(autouse=True)
async def _shutdown_after():
    yield
    await PostgresSessionFactory.shutdown()


@pytest.fixture
def sqlite_async_engines(tmp_path: Path) -> Iterator[list]:
    """Make initialize() build a real aiosqlite engine, with the production engine options.

    A file database, not :memory:, so SQLAlchemy gives it a real queue pool whose checkedout()
    count shows whether a session still holds a connection.
    """
    created = []

    def _create(_url: str, **kwargs):
        engine = real_create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "async.db"}', **kwargs)
        created.append(engine)
        return engine

    with (
        patch.object(AsyncHandle, 'wait_for_db', new_callable=AsyncMock, return_value=True),
        patch('common.database.postgres_tools.create_async_engine', side_effect=_create),
    ):
        yield created


@pytest.fixture
def sqlite_sync_engines(tmp_path: Path) -> Iterator[list]:
    """Make initialize() build a real SQLite engine, with the production engine options."""
    created = []

    def _create(_url: str, **kwargs):
        engine = real_create_engine(f'sqlite:///{tmp_path / "sync.db"}', **kwargs)
        created.append(engine)
        return engine

    with (
        patch.object(SyncHandle, 'wait_for_db', return_value=True),
        patch('common.database.postgres_tools.create_engine', side_effect=_create),
    ):
        yield created


# --- async handle -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_session_closes_on_normal_exit(sqlite_async_engines, async_uri: URL):
    await AsyncHandle.initialize(async_uri, timeout=0)
    pool = sqlite_async_engines[0].pool

    async with AsyncHandle.session(async_uri) as session:
        assert isinstance(session, AsyncSession)
        # A read autobegins and checks out a connection, exactly like search_entries.
        await session.execute(text('SELECT 1'))
        assert session.in_transaction()
        assert pool.checkedout() == 1

    # Nothing committed or rolled back in the block: only the close on exit can end the read.
    assert not session.in_transaction()
    assert pool.checkedout() == 0


@pytest.mark.asyncio
async def test_async_session_closes_and_reraises_when_block_raises(sqlite_async_engines, async_uri: URL):
    await AsyncHandle.initialize(async_uri, timeout=0)
    pool = sqlite_async_engines[0].pool
    raised = _BlockError('boom')

    with pytest.raises(_BlockError) as exc_info:
        async with AsyncHandle.session(async_uri) as session:
            await session.execute(text('SELECT 1'))
            assert pool.checkedout() == 1
            raise raised

    assert exc_info.value is raised
    assert not session.in_transaction()
    assert pool.checkedout() == 0


@pytest.mark.asyncio
async def test_async_sequential_sessions_are_distinct(sqlite_async_engines, async_uri: URL):
    await AsyncHandle.initialize(async_uri, timeout=0)

    async with AsyncHandle.session(async_uri) as first:
        pass
    async with AsyncHandle.session(async_uri) as second:
        pass

    # Same task, same uri: a registry would hand back the same object. A factory does not.
    assert first is not second


@pytest.mark.asyncio
async def test_async_session_before_initialize_raises(async_uri: URL):
    with pytest.raises(RuntimeError, match='not initialized'):
        async with AsyncHandle.session(async_uri):
            pass


@pytest.mark.asyncio
async def test_async_session_after_shutdown_raises(sqlite_async_engines, async_uri: URL):
    await AsyncHandle.initialize(async_uri, timeout=0)
    await PostgresSessionFactory.shutdown()

    with pytest.raises(RuntimeError, match='not initialized'):
        async with AsyncHandle.session(async_uri):
            pass


@pytest.mark.asyncio
async def test_async_second_initialize_raises_and_keeps_engine(sqlite_async_engines, async_uri: URL):
    await AsyncHandle.initialize(async_uri, timeout=0)
    original = dict(AsyncHandle._async_engines)

    # Bug item 7: the guard tested the display string against hash keys and never fired, so a
    # second initialize silently replaced the engine and orphaned the first pool.
    with pytest.raises(RuntimeError, match='already initialized'):
        await AsyncHandle.initialize(async_uri, timeout=0)

    assert len(sqlite_async_engines) == 1
    assert AsyncHandle._async_engines == original


# --- sync handle --------------------------------------------------------------------------------


def test_sync_session_closes_on_normal_exit(sqlite_sync_engines, sync_uri: URL):
    SyncHandle.initialize(sync_uri, timeout=0)
    pool = sqlite_sync_engines[0].pool

    with SyncHandle.session(sync_uri) as session:
        assert isinstance(session, Session)
        session.execute(text('SELECT 1'))
        assert session.in_transaction()
        assert pool.checkedout() == 1

    assert not session.in_transaction()
    assert pool.checkedout() == 0


def test_sync_session_closes_and_reraises_when_block_raises(sqlite_sync_engines, sync_uri: URL):
    SyncHandle.initialize(sync_uri, timeout=0)
    pool = sqlite_sync_engines[0].pool
    raised = _BlockError('boom')

    with pytest.raises(_BlockError) as exc_info, SyncHandle.session(sync_uri) as session:
        session.execute(text('SELECT 1'))
        assert pool.checkedout() == 1
        raise raised

    assert exc_info.value is raised
    assert not session.in_transaction()
    assert pool.checkedout() == 0


def test_sync_sequential_sessions_are_distinct(sqlite_sync_engines, sync_uri: URL):
    SyncHandle.initialize(sync_uri, timeout=0)

    with SyncHandle.session(sync_uri) as first:
        pass
    with SyncHandle.session(sync_uri) as second:
        pass

    # Same thread, same uri: a thread-local registry would hand back the same object.
    assert first is not second


def test_sync_session_before_initialize_raises(sync_uri: URL):
    with pytest.raises(RuntimeError, match='not initialized'), SyncHandle.session(sync_uri):
        pass


@pytest.mark.asyncio
async def test_sync_session_after_shutdown_raises(sqlite_sync_engines, sync_uri: URL):
    SyncHandle.initialize(sync_uri, timeout=0)
    await PostgresSessionFactory.shutdown()

    with pytest.raises(RuntimeError, match='not initialized'), SyncHandle.session(sync_uri):
        pass


def test_sync_second_initialize_raises_and_keeps_engine(sqlite_sync_engines, sync_uri: URL):
    SyncHandle.initialize(sync_uri, timeout=0)
    original = dict(SyncHandle._sync_engines)

    # Bug item 7, sync side: see the async test above.
    with pytest.raises(RuntimeError, match='already initialized'):
        SyncHandle.initialize(sync_uri, timeout=0)

    assert len(sqlite_sync_engines) == 1
    assert SyncHandle._sync_engines == original


# --- regression pins on ADR tj-8z213c decision item 3 -------------------------------------------


@pytest.mark.parametrize('handle', [AsyncHandle, SyncHandle], ids=['async', 'sync'])
def test_handle_has_no_get_session(handle):
    # get_session handed out a session that nobody owned, so nothing ever closed it: that
    # accessor is the defect in bug tj-vhboky.76 (one leaked pooled connection per read request,
    # pool exhausted after 15). session() is the only accessor; do not bring get_session back.
    assert not hasattr(handle, 'get_session')


@pytest.mark.asyncio
async def test_initialize_builds_no_scoped_registry(
    sqlite_async_engines, sqlite_sync_engines, async_uri: URL, sync_uri: URL
):
    # A scoped registry keeps every session (and, for the async one, its Task) alive until
    # someone calls remove(). ADR tj-8z213c removed both; nothing initialize() stores may be one.
    await AsyncHandle.initialize(async_uri, timeout=0)
    SyncHandle.initialize(sync_uri, timeout=0)

    for handle in (AsyncHandle, SyncHandle):
        for value in vars(handle).values():
            if isinstance(value, dict):
                for stored in value.values():
                    assert not isinstance(stored, (async_scoped_session, scoped_session)), handle
