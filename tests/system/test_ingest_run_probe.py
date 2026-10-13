"""The startup sweep's table probe on real Postgres over asyncpg (tj-s6r3rt.5).

DESIGN: tj-s6r3rt.5. Before its UPDATE, sweep_interrupted_runs asks crud.ingest_run_table_exists whether ingest_run
exists, by SELECT to_regclass(:name) IS NOT NULL -- a read that never enters write_transaction. On a never-migrated
database it answers False and the sweep logs one warning instead of write_transaction's traceback.

The offline suite (server/data/store/tests/test_run_recording.py) runs that statement on SQLite against a stand-in
for to_regclass, so it cannot show the one thing only Postgres can: that asyncpg binds the table name as an argument
to_regclass accepts, and that a missing relation reads NULL rather than raising. Were it to raise here, the sweep
would fall through to the UPDATE and the traceback would be back, silently. This module is that half.

NOTHING IS WRITTEN. The stack is migrated, so the table exists; the missing case is made by narrowing the search
path to pg_catalog with SET LOCAL inside one transaction that is rolled back -- to_regclass resolves an unqualified
name on the search path, exactly as the sweep's session does.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from data.store.app.database.crud.ingest_run import ingest_run_table_exists


pytestmark = pytest.mark.data_store


@pytest.mark.asyncio
async def test_the_probe_finds_ingest_run_on_a_migrated_database(pg_async_engine: AsyncEngine):
    async with AsyncSession(pg_async_engine) as session:
        assert await ingest_run_table_exists(session) is True
        await session.rollback()


@pytest.mark.asyncio
async def test_the_probe_answers_false_without_raising_when_the_table_cannot_be_found(pg_async_engine: AsyncEngine):
    async with AsyncSession(pg_async_engine) as session:
        await session.execute(sa.text('SET LOCAL search_path TO pg_catalog'))
        assert await ingest_run_table_exists(session) is False
        await session.rollback()
