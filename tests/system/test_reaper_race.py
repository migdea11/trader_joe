"""The reaper's delete against a concurrent expiry PATCH, two sessions on real Postgres (tj-grna9p.23, gate of da5c881).

THE DESIGN is the architect's 2026-10-14 gate on tj-grna9p.23: reaper_delete_entry reads the dataset FOR UPDATE as
the first statement of its transaction, then re-checks the expiry and the RUNNING ingest under that lock. Under READ
COMMITTED a PATCH extending the expiry (an UPDATE, which takes the same row lock) therefore either
  * comes SECOND: it waits on the reaper's lock, the reaper commits its delete, and the PATCH answers 404; or
  * comes FIRST: the reaper's locked read waits for the PATCH's commit, sees the new expiry, and leaves the dataset.
With the old unlocked read the first case lets the PATCH commit in between (no wait) and the second deletes a
dataset whose expiry was just extended. Both tests below were run red against that (with_for_update removed).

HOW THE SESSIONS ARE HELD. Each test pauses one side at a known point by wrapping a function that side calls while
it holds the row lock: the reaper's dataset_has_running_run (called after its locked read) or the PATCH's
append_override (called after its UPDATE). The other side is then started, and the WAIT is shown by Postgres itself:
pg_stat_activity names a backend of the scratch database waiting on a Lock, and the waiting side has not finished
while the other is held. That wait is the order; completion order is not asserted, because once the lock is released
the waiter may finish before the holder's task has closed its session.

THE PATCH is the real route, PATCH /ui/v1/datasets/{id}/expiry, driven in process (httpx's ASGI transport) with its
session pointed at the scratch database, so its crud, its audit probe (to_regclass) and its 404 are the shipped ones.
Every row lives in a scratch database this test creates and drops (test_usage_record's scratch_db, reused).

Run by make test-system and the agent-stack MCP's run_system_tests. FAIL, NEVER SKIP.
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType
from data.store.app import events
from data.store.app import reaper as reaper_module
from data.store.app.client_label import OPERATOR_PRINCIPAL
from data.store.app.database.crud.stock import store_dataset_entry as entry_repo
from data.store.app.database.database import async_db
from data.store.app.database.models.dataset_deletion import DatasetDeletion
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.main import app
from data.store.app.reaper import Reaper
from routers.common.instance_secret import INSTANCE_SECRET_ENV_VAR, INSTANCE_SECRET_HEADER
from tests.system import test_usage_record
from tests.system.conftest import BAR_TABLE, ENTRY_TABLE


pytestmark = pytest.mark.data_store

scratch_db = test_usage_record.scratch_db
scratch_async = test_usage_record.scratch_async
_head = test_usage_record._head

DELETION = DatasetDeletion.__table__
OVERRIDE = DatasetOverrideLog.__table__
RUN = IngestRun.__table__
SECRET = 'reaper-race-local-secret'
# Long enough for a lock wait to show up in pg_stat_activity; the tests never sleep this long when things work.
WAIT_SECONDS = 10.0


@pytest_asyncio.fixture
async def store(scratch_db, scratch_async: AsyncEngine, monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    """The scratch database at head, and the real app's routes with their sessions on it."""
    scratch_db.must('upgrade', _head())

    async def session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(scratch_async, expire_on_commit=False) as db:
            yield db

    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, SECRET)
    app.dependency_overrides[async_db] = session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://store') as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _expired_dataset(scratch_db, entry_values, bar_values, *, symbol: str, owner: str) -> sa.Row:
    now = datetime.now(UTC)
    with scratch_db.engine.begin() as conn:
        row = conn.execute(
            sa.insert(ENTRY_TABLE)
            .values(
                entry_values(
                    asset_symbol=symbol,
                    owner=owner,
                    expiry=now - timedelta(days=1),
                    update_type=UpdateType.STATIC.value,
                    expiry_type=ExpiryType.BULK.value,
                )
            )
            .returning(ENTRY_TABLE)
        ).one()
        for minute in range(2):
            conn.execute(sa.insert(BAR_TABLE).values(bar_values(row, timestamp=row.start + timedelta(minutes=minute))))
    return row


async def _a_backend_waits_on_a_lock(scratch_db) -> bool:
    """Whether Postgres reports a backend of the scratch database waiting on a lock, polled up to WAIT_SECONDS."""
    query = sa.text(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
    )
    deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        with scratch_db.engine.connect() as conn:
            if conn.execute(query).scalar_one() > 0:
                return True
        await asyncio.sleep(0.05)
    return False


def _held(real: Callable[..., Awaitable[Any]], reached: asyncio.Event, release: asyncio.Event, *, after: bool):
    """Wrap REAL so its caller stops at it until RELEASE: before the call, or after it when AFTER."""

    async def wrapper(*args, **kwargs):
        if not after:
            reached.set()
            await release.wait()
        result = await real(*args, **kwargs)
        if after:
            reached.set()
            await release.wait()
        return result

    return wrapper


def _patch(client: httpx.AsyncClient, dataset_id: uuid.UUID, body: dict) -> Awaitable[httpx.Response]:
    return client.patch(f'/ui/v1/datasets/{dataset_id}/expiry', json=body, headers={INSTANCE_SECRET_HEADER: SECRET})


async def _reaper_delete(scratch_async: AsyncEngine, dataset_id: uuid.UUID) -> bool:
    async with AsyncSession(scratch_async, expire_on_commit=False) as session:
        return await entry_repo.reaper_delete_entry(session, dataset_id, datetime.now(UTC))


def _finish(order: list[str], name: str, coroutine: Awaitable[Any]) -> asyncio.Task:
    async def run():
        try:
            return await coroutine
        finally:
            order.append(name)

    return asyncio.create_task(run())


@pytest.mark.asyncio
async def test_a_patch_after_the_reapers_locked_read_waits_and_then_404s(
    store, scratch_db, scratch_async, entry_values, bar_values, run_identity, monkeypatch
):
    dataset = _expired_dataset(
        scratch_db, entry_values, bar_values, symbol=run_identity.alt_symbol('racea'), owner=OPERATOR_PRINCIPAL
    )
    locked, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        entry_repo, 'dataset_has_running_run', _held(entry_repo.dataset_has_running_run, locked, release, after=False)
    )
    order: list[str] = []
    reaper = _finish(order, 'reaper', _reaper_delete(scratch_async, dataset.id))
    patch = None
    try:
        await asyncio.wait_for(locked.wait(), WAIT_SECONDS)
        patch = _finish(order, 'patch', _patch(store, dataset.id, {'expiry': '2031-01-01T00:00:00Z'}))

        assert await _a_backend_waits_on_a_lock(scratch_db), 'the PATCH did not wait on the reaper: no row lock'
        assert not patch.done(), 'the PATCH completed while the reaper held its transaction'
    finally:
        release.set()
        deleted = await reaper
        response = await patch if patch is not None else None

    # No completion-order assertion: the reaper's commit releases the lock, and the PATCH can finish its 404 before
    # the reaper's task closes its session (seen once on Postgres). The order that matters, the reaper's commit before
    # the PATCH's UPDATE, is shown by the PATCH waiting on the lock, unfinished, until the release above.
    assert sorted(order) == ['patch', 'reaper']
    assert deleted is True
    assert response is not None and response.status_code == 404, response.text if response is not None else None
    assert response.json()['reason'] == 'NOT_FOUND'
    with scratch_db.engine.connect() as conn:
        assert conn.execute(sa.select(ENTRY_TABLE.c.id).where(ENTRY_TABLE.c.id == dataset.id)).first() is None
        (record,) = conn.execute(sa.select(DELETION).where(DELETION.c.dataset_id == dataset.id)).all()
    assert (record.cause, record.actor, record.bars_deleted) == ('REAPER', 'reaper', 2)


@pytest.mark.asyncio
async def test_a_patch_that_commits_first_is_seen_by_the_locked_read_and_the_dataset_survives(
    store, scratch_db, scratch_async, entry_values, bar_values, run_identity, monkeypatch
):
    foreign = run_identity.alt_owner('race')
    dataset = _expired_dataset(
        scratch_db, entry_values, bar_values, symbol=run_identity.alt_symbol('raceb'), owner=foreign
    )
    updated, release = asyncio.Event(), asyncio.Event()
    # The PATCH stops after its UPDATE (and its override row), holding the row lock, before its commit.
    monkeypatch.setattr(entry_repo, 'append_override', _held(entry_repo.append_override, updated, release, after=True))
    order: list[str] = []
    patch = _finish(
        order,
        'patch',
        _patch(
            store, dataset.id, {'expiry': '2031-01-01T00:00:00Z', 'override': {'reason': 'keep', 'confirmed': True}}
        ),
    )
    reaper = None
    try:
        await asyncio.wait_for(updated.wait(), WAIT_SECONDS)
        reaper = _finish(order, 'reaper', _reaper_delete(scratch_async, dataset.id))

        assert await _a_backend_waits_on_a_lock(scratch_db), 'the reaper did not wait on the PATCH'
        assert not reaper.done()
    finally:
        release.set()
        response = await patch
        deleted = await reaper if reaper is not None else None

    # No completion-order assertion here: once the PATCH commits, the reaper's wait ends at once while the route
    # still re-reads the summary, so either task may finish first. The order that matters, the PATCH's commit
    # before the reaper's locked read, is shown by the reaper having waited on the lock until the release above.
    assert response.status_code == 200, response.text
    assert deleted is False, 'the reaper deleted a dataset whose expiry the PATCH had just extended'
    with scratch_db.engine.connect() as conn:
        survivor = conn.execute(sa.select(ENTRY_TABLE).where(ENTRY_TABLE.c.id == dataset.id)).one()
        assert survivor.expiry == datetime(2031, 1, 1, tzinfo=UTC)
        assert conn.execute(sa.select(sa.func.count()).select_from(DELETION)).scalar_one() == 0
        assert conn.execute(sa.select(sa.func.count()).select_from(BAR_TABLE)).scalar_one() == 2
        assert conn.execute(sa.select(sa.func.count()).select_from(OVERRIDE)).scalar_one() == 1


@pytest.mark.asyncio
async def test_on_postgres_a_run_acknowledged_after_the_pass_checked_keeps_its_dataset(
    store, scratch_db, scratch_async, entry_values, bar_values, run_identity, monkeypatch
):
    """The RUNNING re-check is inside the locked transaction: a run that appears after the pass's own read counts."""
    dataset = _expired_dataset(
        scratch_db, entry_values, bar_values, symbol=run_identity.alt_symbol('racec'), owner=OPERATOR_PRINCIPAL
    )

    @asynccontextmanager
    async def own_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(scratch_async, expire_on_commit=False) as session:
            yield session

    real_running = reaper_module.running_dataset_ids

    async def then_a_run_is_acknowledged(db):
        seen = await real_running(db)
        with scratch_db.engine.begin() as conn:
            conn.execute(
                sa.insert(RUN).values(
                    dataset_id=dataset.id,
                    trigger=IngestRunTrigger.REQUEST,
                    actor=run_identity.owner,
                    asset_symbol='RUN',
                    asset_type=AssetType.STOCK,
                    data_type=DataType.MARKET_ACTIVITY,
                    source=DataSource.ALPACA_API,
                    granularity=Granularity.ONE_MINUTE,
                    update_type=UpdateType.STATIC.value,
                    state=IngestRunState.RUNNING,
                )
            )
        return seen

    monkeypatch.setattr(reaper_module, 'own_session', own_session)
    monkeypatch.setattr(reaper_module, 'publish', lambda _event: None)
    monkeypatch.setattr(reaper_module, 'running_dataset_ids', then_a_run_is_acknowledged)
    monkeypatch.setenv(reaper_module.DELETES_ENABLED_ENV, 'true')

    assert await Reaper().run_pass(datetime.now(UTC)) == 0

    with scratch_db.engine.connect() as conn:
        assert conn.execute(sa.select(ENTRY_TABLE.c.id).where(ENTRY_TABLE.c.id == dataset.id)).first() is not None
        assert conn.execute(sa.select(sa.func.count()).select_from(DELETION)).scalar_one() == 0


async def _published_within(subscription: events.Subscription, seconds: float) -> list[Any]:
    """Every event the hub handed this subscription within SECONDS of asking."""
    seen: list[Any] = []
    with contextlib.suppress(TimeoutError):
        while True:
            seen.append(await asyncio.wait_for(subscription.get(), seconds))
    return seen


@pytest.mark.asyncio
async def test_an_overriding_patch_that_waited_on_the_reapers_delete_404s_with_no_override_row_and_no_event(
    store, scratch_db, scratch_async, entry_values, bar_values, run_identity, monkeypatch
):
    """tj-grna9p.23 round 2 (item B, 138c009): ANOTHER owner's dataset, a valid override, the PATCH waiting on the lock.

    Before the fix the waiting UPDATE matched no row, the override-log row (append-only, never removable) was written
    and committed for a dataset that no longer exists, DatasetChanged UPDATED was published, and only then a 404.
    """
    foreign = run_identity.alt_owner('raced')
    dataset = _expired_dataset(
        scratch_db, entry_values, bar_values, symbol=run_identity.alt_symbol('raced'), owner=foreign
    )
    survivor = _expired_dataset(
        scratch_db, entry_values, bar_values, symbol=run_identity.alt_symbol('racee'), owner=OPERATOR_PRINCIPAL
    )
    subscription = events.hub.subscribe()
    locked, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        entry_repo, 'dataset_has_running_run', _held(entry_repo.dataset_has_running_run, locked, release, after=False)
    )
    order: list[str] = []
    try:
        reaper = _finish(order, 'reaper', _reaper_delete(scratch_async, dataset.id))
        patch = None
        try:
            await asyncio.wait_for(locked.wait(), WAIT_SECONDS)
            patch = _finish(
                order,
                'patch',
                _patch(
                    store,
                    dataset.id,
                    {'expiry': '2031-01-01T00:00:00Z', 'override': {'reason': 'keep it', 'confirmed': True}},
                ),
            )

            assert await _a_backend_waits_on_a_lock(scratch_db), 'the PATCH did not wait on the reaper: no row lock'
            assert not patch.done(), 'the PATCH completed while the reaper held its transaction'
        finally:
            release.set()
            deleted = await reaper
            response = await patch if patch is not None else None

        # As in the first test: the lock wait while the reaper held its transaction is the order, not completion.
        assert sorted(order) == ['patch', 'reaper']
        assert deleted is True
        assert response is not None and response.status_code == 404, response.text if response is not None else None
        assert response.json()['reason'] == 'NOT_FOUND'
        assert foreign not in response.text
        with scratch_db.engine.connect() as conn:
            overrides = conn.execute(sa.select(OVERRIDE).where(OVERRIDE.c.dataset_id == dataset.id)).all()
            (record,) = conn.execute(sa.select(DELETION).where(DELETION.c.dataset_id == dataset.id)).all()
        published = await _published_within(subscription, 0.5)
        assert (overrides, published) == ([], []), 'a PATCH that updated nothing left an override row or an event'
        assert (record.cause, record.actor, record.owner, record.bars_deleted) == ('REAPER', 'reaper', foreign, 2)

        # The subscription is live: a PATCH that does land is announced on it, so the empty list above is evidence.
        landed = await _patch(store, survivor.id, {'expiry': '2031-01-01T00:00:00Z'})
        assert landed.status_code == 200, landed.text
        assert await _published_within(subscription, 0.5) == [
            events.DatasetChanged(survivor.id, events.DatasetChange.UPDATED)
        ]
    finally:
        subscription.close()
