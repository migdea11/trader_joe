"""async_db against a REAL connection pool: every request returns its connection and leaves no session behind.

WHY THIS FILE EXISTS (validator, tj-vhboky.76.5, gating tj-vhboky.76.2). Bug tj-vhboky.76: async_db
yielded a session from a Task-scoped registry and never closed it, so every read request pinned a
pooled connection until the pool (5 + 10) was exhausted and every later request timed out. fca381f
switched async_db to PostgresSessionFactory.AsyncSessionHandle.session(), a context manager over a
plain async_sessionmaker (ADR tj-8z213c). Every other data_store test overrides async_db with a fake
session, so before this file nothing in the gate ran async_db itself, and nothing held a pool that
could run out.

THE HARNESS, and why each part matters:
  * A real AsyncEngine on aiosqlite, pool_size=1, max_overflow=0, pool_timeout=1, with
    poolclass=AsyncAdaptedQueuePool NAMED EXPLICITLY. SQLite's default pool is not a QueuePool, and
    a StaticPool or NullPool never runs out, which would make every assertion below vacuous.
    test_the_harness_pool_really_runs_out proves the pool can be exhausted at all.
  * The REAL data.store.app.database.database.initialize() and the REAL async_db, with no
    dependency_overrides. Only two seams are patched: wait_for_db (there is no Postgres to wait
    for) and create_async_engine inside postgres_tools (so initialize builds the engine above).
  * httpx.AsyncClient over ASGITransport, in the SAME event loop as the engine. Starlette's
    TestClient runs the app on its own loop in a thread, and an aiosqlite engine created on the
    test's loop must not be used there.

THE FOUR PINS:
  1. More sequential reads than the pool holds; each is 200 and leaves checkedout() == 0.
  2. A route that raises -- HTTPException(409) and an unhandled RuntimeError (500) -- still
     releases its connection, and the next read succeeds.
  3. write_transaction's commit path and its rollback path both release.
  4. RETENTION: the request's session is weakly referenced dead after the request and a
     gc.collect(). This is what rejected option (a) of the ADR would fail -- a registry that keeps
     the session alive even though close() released its connection -- and pins 1-3 cannot see it.

MUTATION PROOF (recorded on tj-vhboky.76.5): the original `yield ...get_session(...)` in async_db
turns pins 1, 2 and 4 red (pin 3 stays green: commit and rollback release on their own); async_db with its `async with` intact but also appending the session to a
module-level list (a fake registry) turns ONLY pin 4 red.
"""

import gc
import weakref
from collections.abc import AsyncIterator
from typing import Annotated
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
import sqlalchemy.exc
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from common.database.postgres_tools import PostgresSessionFactory
from data.store.app.database import database
from data.store.app.database.database import async_db
from data.store.app.database.transaction import write_transaction


pytestmark = pytest.mark.data_store

POOL_SIZE = 1
POOL_TIMEOUT_SECONDS = 1
READS_BEYOND_THE_POOL = 5  # more than pool_size + max_overflow = 1

RequestSession = Annotated[AsyncSession, Depends(async_db)]


class _DomainRejection(Exception):
    """Stands in for EntryNotFound and its siblings: a non-database exception inside a write."""


def _build_app(session_refs: list[weakref.ref]) -> FastAPI:
    """A minimal app whose routes depend on the REAL async_db."""
    app = FastAPI()

    @app.get('/read')
    async def read(db: RequestSession) -> dict[str, int]:
        return {'value': (await db.execute(text('SELECT 1'))).scalar_one()}

    @app.get('/read-then-409')
    async def read_then_409(db: RequestSession) -> None:
        await db.execute(text('SELECT 1'))
        raise HTTPException(status_code=409, detail='refused after a read')

    @app.get('/read-then-crash')
    async def read_then_crash(db: RequestSession) -> None:
        await db.execute(text('SELECT 1'))
        raise RuntimeError('unhandled, after a read')

    @app.get('/write-commits')
    async def write_commits(db: RequestSession) -> dict[str, int]:
        async with write_transaction(db, 'pool test commit'):
            value = (await db.execute(text('SELECT 1'))).scalar_one()
        return {'value': value}

    @app.get('/write-rolls-back')
    async def write_rolls_back(db: RequestSession) -> None:
        try:
            async with write_transaction(db, 'pool test rollback'):
                await db.execute(text('SELECT 1'))
                raise _DomainRejection('rejected inside the transaction')
        except _DomainRejection as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.get('/read-and-remember')
    async def read_and_remember(db: RequestSession) -> dict[str, int]:
        session_refs.append(weakref.ref(db))
        return {'value': (await db.execute(text('SELECT 1'))).scalar_one()}

    return app


class _Harness:
    def __init__(self, engine: AsyncEngine, client: httpx.AsyncClient, session_refs: list[weakref.ref]):
        self.engine = engine
        self.client = client
        self.session_refs = session_refs

    def checked_out(self) -> int:
        return self.engine.pool.checkedout()


@pytest_asyncio.fixture
async def harness() -> AsyncIterator[_Harness]:
    engines: list[AsyncEngine] = []

    def _real_pool_engine(*_args, **_kwargs) -> AsyncEngine:
        engine = create_async_engine(
            'sqlite+aiosqlite:///:memory:',
            poolclass=AsyncAdaptedQueuePool,
            pool_size=POOL_SIZE,
            max_overflow=0,
            pool_timeout=POOL_TIMEOUT_SECONDS,
        )
        engines.append(engine)
        return engine

    with (
        patch.object(PostgresSessionFactory.AsyncSessionHandle, 'wait_for_db', new=AsyncMock(return_value=True)),
        patch('common.database.postgres_tools.create_async_engine', new=_real_pool_engine),
    ):
        await database.initialize()
    try:
        assert len(engines) == 1, f'initialize() built {len(engines)} engines, expected exactly one'
        session_refs: list[weakref.ref] = []
        transport = httpx.ASGITransport(app=_build_app(session_refs), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url='http://pool-test') as client:
            yield _Harness(engines[0], client, session_refs)
    finally:
        await PostgresSessionFactory.shutdown()


@pytest.mark.asyncio
async def test_the_harness_pool_really_runs_out(harness: _Harness):
    """Guard against a vacuous harness: with one connection held, a second checkout times out."""
    assert isinstance(harness.engine.pool, AsyncAdaptedQueuePool), type(harness.engine.pool)
    assert harness.engine.pool.size() == POOL_SIZE
    async with harness.engine.connect() as held:
        await held.execute(text('SELECT 1'))
        assert harness.checked_out() == 1
        with pytest.raises(sqlalchemy.exc.TimeoutError):
            async with harness.engine.connect():
                pass
    assert harness.checked_out() == 0


@pytest.mark.asyncio
async def test_reads_beyond_the_pool_size_each_return_their_connection(harness: _Harness):
    """Pin 1, the defect itself: before the fix, request 2 timed out waiting for the one connection."""
    for attempt in range(1, READS_BEYOND_THE_POOL + 1):
        response = await harness.client.get('/read')
        assert response.status_code == 200, f'read {attempt}: {response.status_code} {response.text}'
        assert response.json() == {'value': 1}
        assert harness.checked_out() == 0, f'read {attempt} left {harness.checked_out()} connection(s) checked out'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('path', 'status'), [('/read-then-409', 409), ('/read-then-crash', 500)], ids=['http-exception', 'unhandled-error']
)
async def test_a_route_that_raises_after_a_read_still_returns_its_connection(harness: _Harness, path: str, status: int):
    """Pin 2: async_db's teardown runs with the exception thrown in, and still closes the session."""
    for attempt in range(1, READS_BEYOND_THE_POOL + 1):
        response = await harness.client.get(path)
        assert response.status_code == status, f'{path} attempt {attempt}: {response.status_code} {response.text}'
        assert harness.checked_out() == 0, f'{path} attempt {attempt} left {harness.checked_out()} checked out'

    following = await harness.client.get('/read')
    assert following.status_code == 200, f'read after {path}: {following.status_code} {following.text}'
    assert harness.checked_out() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('path', 'status'), [('/write-commits', 200), ('/write-rolls-back', 409)], ids=['commit', 'rollback']
)
async def test_write_transaction_paths_return_their_connection(harness: _Harness, path: str, status: int):
    """Pin 3: write_transaction's commit and rollback paths, inside a real request session."""
    for attempt in range(1, READS_BEYOND_THE_POOL + 1):
        response = await harness.client.get(path)
        assert response.status_code == status, f'{path} attempt {attempt}: {response.status_code} {response.text}'
        assert harness.checked_out() == 0, f'{path} attempt {attempt} left {harness.checked_out()} checked out'

    following = await harness.client.get('/read')
    assert following.status_code == 200, f'read after {path}: {following.status_code} {following.text}'


@pytest.mark.asyncio
async def test_nothing_retains_the_request_session_after_the_request(harness: _Harness):
    """Pin 4, retention: ADR tj-8z213c option (a) -- close() plus a registry -- passes pins 1-3 and fails this."""
    for attempt in range(1, READS_BEYOND_THE_POOL + 1):
        response = await harness.client.get('/read-and-remember')
        assert response.status_code == 200, f'request {attempt}: {response.status_code} {response.text}'

    assert len(harness.session_refs) == READS_BEYOND_THE_POOL
    gc.collect()
    alive = [index for index, ref in enumerate(harness.session_refs, start=1) if ref() is not None]
    assert alive == [], f'request sessions still referenced after the request and gc.collect(): {alive}'
