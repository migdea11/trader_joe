"""The expiry reaper and GET /ui/v1/reaper/pending, driven over real rows in SQLite (validator, gating tj-grna9p.23).

THE DESIGN is the architect's 2026-10-14 consolidated brief on tj-grna9p.23, which wins over the bead's body:
  1. the loop is common/scheduling.run_periodic with Every(REAPER_INTERVAL_SECONDS), started in the lifespan and
     cancelled and awaited at teardown;
  2. THE PENDING SET is `expiry <= now` (set and passed), whatever the update type: a retired DAILY/STREAM dataset
     is pending only once its expiry has passed. ONE query serves the pass, the route and DatasetSummary's flag;
  3. DRY-RUN BY DEFAULT: only an explicit `true` in REAPER_DELETES_ENABLED deletes. Dry-run logs a count, never an
     owner or a symbol, and deletes nothing. Enabled: each dataset in its own transaction, a dataset_deletion row
     (cause REAPER, actor `reaper`, bars counted), NO override-log row, DatasetChanged DELETED after each commit,
     one failure logged by exception type and the pass carries on;
  4. missing audit tables: an enabled pass deletes nothing (dry-run unaffected);
  5. a dataset with a RUNNING ingest is skipped this pass and retried on the next;
  6. the route: open, ReaperPendingPage of DatasetSummary (reaper_pending true), keyset (expiry, id), opaque cursor,
     deletes_enabled from the env, as_of = now;
  7. ReaperPendingChanged: when the pending ids differ from the previous pass's, after enabled-mode deletions, and
     once on the first pass after start, in dry-run too.
SUPERSEDED: "bars another entry holds are kept". Every bar belongs to one dataset and the delete cascades to that
dataset's bars only, so the pin here is that every OTHER dataset's bars are untouched.

WHAT TIER THIS IS. The real reaper pass, the real crud (list_pending, pending_ids, reaper_delete_entry) and the real
route over real tables in in-memory SQLite, with foreign keys on so the bars' ON DELETE CASCADE runs. The reaper's
own_session is pointed at the engine, its publish is recorded with the commit count at that moment (so "after the
commit" is evidence, not order of lines), and audit_tables_exist (Postgres's to_regclass) is replaced by a stand-in
answering migrated or not. Postgres itself: tests/system/test_reaper_pass.py.
"""

import asyncio
import base64
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from common.enums.data_stock import Granularity, UpdateType
from common.scheduling import Every
from data.store.app import app_depends
from data.store.app import reaper as reaper_module
from data.store.app.database.crud import dataset_override
from data.store.app.database.crud.dataset_override import OperatorOverrideInput
from data.store.app.database.crud.stock import dataset_catalog as catalog_repo
from data.store.app.database.crud.stock import store_dataset_entry as entry_repo
from data.store.app.database.crud.stock.store_dataset_entry import EntryNotFound
from data.store.app.database.database import async_db
from data.store.app.database.models.dataset_deletion import DatasetDeletion
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.events import DatasetChange, DatasetChanged, ReaperPendingChanged
from data.store.app.main import app
from data.store.app.reaper import Reaper
from data.store.tests.problem_body import problem
from data.store.tests.test_ui_dataset_writes_route import _ProbeSession
from data.store.tests.test_ui_datasets_route import (  # noqa: F401 (aware_sqlite_datetimes is a fixture)
    NOW,
    _sqlite_timezone,
    aware_sqlite_datetimes,
    bar,
    entry,
    ny_midnight,
    run_of,
)
from routers.data_store.ui_datasets import utc_now


pytestmark = pytest.mark.data_store

PENDING_PATH = '/ui/v1/reaper/pending'
# Distinctive, so their absence from a log line is evidence and not luck.
FOREIGN_OWNER = 'strategy-reaper-owner-8813'
SYMBOL_PREFIX = 'RPZ'


# ------------------------------------------------------------------------------------------------- the world


class World:
    """The engine, the seeded rows, the commit count of the reaper's sessions and every event with that count."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.commits = 0
        self.sessions = 0
        self.events: list[tuple[Any, int]] = []
        self.migrated = True
        self.rows: dict[str, StoreDatasetEntry] = {}

    def id(self, name: str) -> uuid.UUID:
        return self.rows[name].id

    async def all(self, model: Any) -> list[Any]:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            return list((await db.execute(select(model))).scalars())

    async def entry_ids(self) -> set[uuid.UUID]:
        return {row.id for row in await self.all(StoreDatasetEntry)}

    async def bars_by_dataset(self) -> dict[uuid.UUID, int]:
        counts: dict[uuid.UUID, int] = {}
        for row in await self.all(StockMarketActivity):
            counts[row.dataset_id] = counts.get(row.dataset_id, 0) + 1
        return counts

    async def set_expiry(self, name: str, expiry: datetime | None) -> None:
        async with AsyncSession(self.engine) as db:
            await db.execute(
                update(StoreDatasetEntry).where(StoreDatasetEntry.id == self.id(name)).values(expiry=expiry)
            )
            await db.commit()

    async def add(self, *rows: Any) -> None:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            db.add_all(rows)
            await db.commit()

    def reaper_events(self) -> list[int]:
        return [commits for event_, commits in self.events if isinstance(event_, ReaperPendingChanged)]

    def deleted_events(self) -> list[tuple[uuid.UUID, int]]:
        return [
            (event_.id, commits)
            for event_, commits in self.events
            if isinstance(event_, DatasetChanged) and event_.change is DatasetChange.DELETED
        ]


# The seed, judged at NOW (Tue 6 Oct 2026 22:00Z). PENDING: expired-static (a day ago, 2 bars, another owner's),
# exact-static (expiry == NOW: `<=` includes it), retired-daily (an hour ago, 1 bar), retired-stream (two days
# ago). NOT PENDING: future-static (tomorrow), retiring-daily (retired, expiry in an hour), live-stream (no expiry,
# 3 bars), forever-static (no expiry).
PENDING = ('expired-static', 'exact-static', 'retired-daily', 'retired-stream')
NOT_PENDING = ('future-static', 'retiring-daily', 'live-stream', 'forever-static')


def _seed() -> tuple[dict[str, StoreDatasetEntry], list[StockMarketActivity]]:
    rows = {
        'expired-static': entry(
            f'{SYMBOL_PREFIX}A', start=ny_midnight(1), end=ny_midnight(6), expiry=NOW - timedelta(days=1)
        ),
        'exact-static': entry(f'{SYMBOL_PREFIX}B', start=ny_midnight(1), end=ny_midnight(6), expiry=NOW),
        'retired-daily': entry(
            f'{SYMBOL_PREFIX}C', update_type=UpdateType.DAILY, expiry=NOW - timedelta(hours=1), collect=False
        ),
        'retired-stream': entry(
            f'{SYMBOL_PREFIX}D',
            granularity=Granularity.ONE_MINUTE,
            update_type=UpdateType.STREAM,
            expiry=NOW - timedelta(days=2),
            collect=False,
        ),
        'future-static': entry(
            f'{SYMBOL_PREFIX}E', start=ny_midnight(1), end=ny_midnight(6), expiry=NOW + timedelta(days=1)
        ),
        'retiring-daily': entry(
            f'{SYMBOL_PREFIX}F', update_type=UpdateType.DAILY, expiry=NOW + timedelta(hours=1), collect=False
        ),
        'live-stream': entry(f'{SYMBOL_PREFIX}G', granularity=Granularity.ONE_MINUTE, update_type=UpdateType.STREAM),
        'forever-static': entry(f'{SYMBOL_PREFIX}H', start=ny_midnight(1), end=ny_midnight(6)),
    }
    rows['expired-static'].owner = FOREIGN_OWNER
    bars = [
        bar(rows['expired-static'], ny_midnight(2)),
        bar(rows['expired-static'], ny_midnight(5)),
        bar(rows['retired-daily'], ny_midnight(5)),
        bar(rows['live-stream'], NOW - timedelta(minutes=3)),
        bar(rows['live-stream'], NOW - timedelta(minutes=2)),
        bar(rows['live-stream'], NOW - timedelta(minutes=1)),
    ]
    return rows, bars


@pytest_asyncio.fixture
async def world(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    request.getfixturevalue('aware_sqlite_datetimes')
    engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, 'connect')
    def register(dbapi_connection, _record):
        dbapi_connection.run_async(lambda c: c.create_function('timezone', 2, _sqlite_timezone))
        dbapi_connection.run_async(lambda c: c.create_function('gen_random_uuid', 0, lambda: uuid.uuid4().hex))
        dbapi_connection.run_async(lambda c: c.execute('PRAGMA case_sensitive_like = ON'))
        dbapi_connection.run_async(lambda c: c.execute('PRAGMA foreign_keys = ON'))

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: StoreDatasetEntry.metadata.create_all(
                sync,
                tables=[
                    StoreDatasetEntry.__table__,
                    StockMarketActivity.__table__,
                    IngestRun.__table__,
                    DatasetOverrideLog.__table__,
                    DatasetDeletion.__table__,
                ],
            )
        )
    state = World(engine)
    state.rows, bars = _seed()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        db.add_all(state.rows.values())
        await db.flush()
        db.add_all(bars)
        await db.commit()

    class CountingSession(AsyncSession):
        async def commit(self) -> None:
            await super().commit()
            state.commits += 1

    @asynccontextmanager
    async def own_session() -> AsyncIterator[AsyncSession]:
        state.sessions += 1
        async with CountingSession(engine, expire_on_commit=False) as db:
            yield db

    async def tables_exist(_db) -> bool:
        return state.migrated

    async def request_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db

    async def pinned_now() -> datetime:
        return NOW

    monkeypatch.delenv(reaper_module.DELETES_ENABLED_ENV, raising=False)
    monkeypatch.setattr(reaper_module, 'own_session', own_session)
    monkeypatch.setattr(reaper_module, 'audit_tables_exist', tables_exist)
    monkeypatch.setattr(reaper_module, 'publish', lambda e: state.events.append((e, state.commits)))
    app.dependency_overrides[async_db] = request_session
    app.dependency_overrides[utc_now] = pinned_now
    try:
        yield state
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(reaper_module.DELETES_ENABLED_ENV, 'true')


@pytest_asyncio.fixture
async def client(world: World) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://store') as http:
        yield http


def _names(world: World, ids: Any) -> set[str]:
    by_id = {row.id: name for name, row in world.rows.items()}
    return {by_id[i] for i in ids}


def _no_owner_or_symbol(records: list[logging.LogRecord]) -> None:
    """No line of the project's own loggers names an owner or a symbol (the SQLite driver's DEBUG trace is not ours).

    The reaper's own lines must be among them, or the check would hold vacuously.
    """
    ours = [r.getMessage() for r in records if r.name.startswith(('data.', 'common.', 'routers.'))]
    assert any(r.name == reaper_module.log.name for r in records), 'the reaper logged nothing'
    for line in ours:
        assert FOREIGN_OWNER not in line, f'an owner reached a log line: {line}'
        assert SYMBOL_PREFIX not in line, f'a symbol reached a log line: {line}'


# ------------------------------------------------------------------------------------------------- the pending set


@pytest.mark.asyncio
async def test_the_pending_set_is_exactly_the_datasets_whose_expiry_has_passed(world: World):
    """Pending means expiry <= NOW, whatever the update type; an unset or a future expiry never is."""
    async with AsyncSession(world.engine) as db:
        ids = await catalog_repo.pending_ids(db, NOW)
        listed = await catalog_repo.list_pending(db, NOW)

    assert _names(world, ids) == set(PENDING)
    assert [str(row.id) for row in listed] == _expected_order(world), 'list_pending is not ordered by (expiry, id)'
    assert {row.id for row in listed} == ids, 'the two pending reads disagree'


@pytest.mark.asyncio
async def test_a_retired_subscription_is_pending_only_once_its_expiry_has_passed(world: World):
    retiring = world.id('retiring-daily')
    async with AsyncSession(world.engine) as db:
        assert retiring not in await catalog_repo.pending_ids(db, NOW)
        assert retiring in await catalog_repo.pending_ids(db, NOW + timedelta(hours=1))


COLLECT_CASES = [
    pytest.param(UpdateType.DAILY, True, id='daily-collecting'),
    pytest.param(UpdateType.DAILY, False, id='daily-stopped'),
    pytest.param(UpdateType.STREAM, True, id='stream-collecting'),
    pytest.param(UpdateType.STREAM, False, id='stream-stopped'),
    pytest.param(UpdateType.STATIC, None, id='static-null'),
]


def _collect_entry(symbol: str, update_type: UpdateType, collect: bool | None, expiry: datetime) -> StoreDatasetEntry:
    span = {} if update_type is not UpdateType.STATIC else {'start': ny_midnight(1), 'end': ny_midnight(6)}
    granularity = Granularity.ONE_MINUTE if update_type is UpdateType.STREAM else Granularity.ONE_DAY
    return entry(symbol, update_type=update_type, granularity=granularity, expiry=expiry, collect=collect, **span)


@pytest.mark.asyncio
@pytest.mark.parametrize(('update_type', 'collect'), COLLECT_CASES)
async def test_an_expired_dataset_is_pending_and_deleted_whatever_collect_says(
    world: World, enabled, update_type, collect
):
    """Decision tj-hhy3dk: the reaper deletes at the expiry; collect (true, false or NULL) is never read."""
    expired = _collect_entry(f'{SYMBOL_PREFIX}X', update_type, collect, NOW - timedelta(minutes=1))
    await world.add(expired)
    async with AsyncSession(world.engine) as db:
        assert expired.id in await catalog_repo.pending_ids(db, NOW)
        assert expired.id in {row.id for row in await catalog_repo.list_pending(db, NOW)}

    deleted = await Reaper().run_pass(NOW)

    assert deleted == len(PENDING) + 1
    assert expired.id not in await world.entry_ids(), f'an expired {update_type} with collect {collect} was kept'


@pytest.mark.asyncio
@pytest.mark.parametrize(('update_type', 'collect'), COLLECT_CASES)
async def test_an_unexpired_dataset_is_never_pending_or_deleted_whatever_collect_says(
    world: World, enabled, update_type, collect
):
    """A stopped subscription is not deleted early: only its expiry, set and passed, makes it pending."""
    future = _collect_entry(f'{SYMBOL_PREFIX}Y', update_type, collect, NOW + timedelta(minutes=1))
    if update_type is UpdateType.STATIC:
        forever = entry(f'{SYMBOL_PREFIX}Z', start=ny_midnight(1), end=ny_midnight(6))
    else:
        forever = entry(f'{SYMBOL_PREFIX}Z', update_type=update_type, collect=collect)
    await world.add(future, forever)
    async with AsyncSession(world.engine) as db:
        assert not {future.id, forever.id} & set(await catalog_repo.pending_ids(db, NOW))

    assert await Reaper().run_pass(NOW) == len(PENDING)
    assert {future.id, forever.id} <= await world.entry_ids()


def test_the_reaper_and_its_crud_never_read_collect_or_retired():
    """Static half of the rule: the pending predicate and the reaper module name neither (the docstrings excepted)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(reaper_module))
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not {'collect', 'is_retired', 'is_collecting'} & names, sorted(names & {'collect', 'is_retired'})
    source = inspect.getsource(catalog_repo._past_expiry).split('"""')[-1]
    assert 'collect' not in source and 'update_type' not in source, source
    delete_source = inspect.getsource(entry_repo.reaper_delete_entry).split('"""')[-1]
    assert 'collect' not in delete_source and 'is_retired' not in delete_source, delete_source


@pytest.mark.asyncio
async def test_pending_ids_restricted_to_a_page_answers_only_those_and_an_empty_page_runs_no_statement(world: World):
    page = [world.id('expired-static'), world.id('future-static'), world.id('live-stream'), uuid.uuid4()]
    statements: list[str] = []
    event.listen(world.engine.sync_engine, 'before_cursor_execute', lambda *a: statements.append(a[2]))
    async with AsyncSession(world.engine) as db:
        assert await catalog_repo.pending_ids(db, NOW, page) == {world.id('expired-static')}
        before = len(statements)
        assert await catalog_repo.pending_ids(db, NOW, []) == frozenset()
        assert len(statements) == before, 'an empty page issued a statement'


@pytest.mark.asyncio
async def test_list_pending_keyset_resumes_strictly_after_the_key_including_a_tie_on_expiry(world: World):
    """Two datasets with the same expiry are told apart by id; paging by one visits each pending row exactly once."""
    twin = entry(f'{SYMBOL_PREFIX}T', start=ny_midnight(1), end=ny_midnight(6), expiry=NOW - timedelta(days=1))
    await world.add(twin)
    async with AsyncSession(world.engine) as db:
        everything = [row.id for row in await catalog_repo.list_pending(db, NOW)]
        seen: list[uuid.UUID] = []
        after = None
        # Bounded: a keyset that resumes AT its key instead of after it would return the same row forever.
        for _ in range(len(everything) + 2):
            page = await catalog_repo.list_pending(db, NOW, after=after, limit=1)
            if not page:
                break
            (row,) = page
            seen.append(row.id)
            after = (row.expiry, row.id)
    assert seen == everything
    assert len(seen) == len(PENDING) + 1


# ------------------------------------------------------------------------------------------------- dry-run


@pytest.mark.asyncio
@pytest.mark.parametrize('flag', [None, '', 'false', 'False', '1', 'yes', 'on', 'enabled', 'truee', 't'])
async def test_dry_run_deletes_nothing_and_logs_only_a_count(world: World, monkeypatch, caplog, flag):
    if flag is not None:
        monkeypatch.setenv(reaper_module.DELETES_ENABLED_ENV, flag)
    caplog.set_level(logging.DEBUG)
    entries_before, bars_before = await world.entry_ids(), await world.bars_by_dataset()

    deleted = await Reaper().run_pass(NOW)

    assert deleted == 0
    assert await world.entry_ids() == entries_before
    assert await world.bars_by_dataset() == bars_before
    assert await world.all(DatasetDeletion) == []
    assert await world.all(DatasetOverrideLog) == []
    assert world.commits == 0, 'a dry-run pass committed'
    assert world.deleted_events() == []
    counted = [r.getMessage() for r in caplog.records if r.name == reaper_module.log.name]
    assert any(f'{len(PENDING)} dataset(s)' in line and 'dry-run' in line for line in counted), counted
    _no_owner_or_symbol(caplog.records)


@pytest.mark.asyncio
async def test_dry_run_issues_no_write_statement(world: World):
    statements: list[str] = []
    event.listen(world.engine.sync_engine, 'before_cursor_execute', lambda *a: statements.append(a[2]))
    await Reaper().run_pass(NOW)
    assert statements
    writes = [s for s in statements if s.lstrip().split(None, 1)[0].upper() in {'UPDATE', 'DELETE', 'INSERT'}]
    assert writes == []


@pytest.mark.asyncio
async def test_dry_run_does_not_consult_the_audit_tables(world: World, monkeypatch):
    """Dry-run is unaffected by a missing audit table: it neither probes nor changes its answer."""
    world.migrated = False
    probes: list[Any] = []

    async def probe(_db) -> bool:
        probes.append(_db)
        return False

    monkeypatch.setattr(reaper_module, 'audit_tables_exist', probe)
    assert await Reaper().run_pass(NOW) == 0
    assert probes == []
    assert world.reaper_events() == [0]


# ------------------------------------------------------------------------------------------------- enabled


@pytest.mark.asyncio
@pytest.mark.parametrize('flag', ['true', 'TRUE', 'True', ' true '])
async def test_enabled_deletes_each_pending_dataset_and_its_bars_and_leaves_every_other_dataset(
    world: World, monkeypatch, caplog, flag
):
    monkeypatch.setenv(reaper_module.DELETES_ENABLED_ENV, flag)
    caplog.set_level(logging.DEBUG)
    bars_before = await world.bars_by_dataset()

    deleted = await Reaper().run_pass(NOW)

    assert deleted == len(PENDING)
    assert _names(world, await world.entry_ids()) == set(NOT_PENDING)
    bars_after = await world.bars_by_dataset()
    for name in PENDING:
        assert world.id(name) not in bars_after, f'{name} kept its bars'
    for name in NOT_PENDING:
        assert bars_after.get(world.id(name)) == bars_before.get(world.id(name)), f'{name} lost bars'
    assert bars_after[world.id('live-stream')] == 3
    _no_owner_or_symbol(caplog.records)


@pytest.mark.asyncio
async def test_enabled_writes_one_reaper_deletion_row_per_dataset_and_no_override_row(world: World, enabled):
    await Reaper().run_pass(NOW)

    assert await world.all(DatasetOverrideLog) == [], 'the reaper wrote an override row'
    deletions = {row.dataset_id: row for row in await world.all(DatasetDeletion)}
    assert _names(world, deletions) == set(PENDING)
    row = deletions[world.id('expired-static')]
    source = world.rows['expired-static']
    assert (
        row.owner,
        row.asset_symbol,
        row.source,
        row.granularity,
        row.update_type,
        row.range_start,
        row.range_end,
        row.bars_deleted,
        row.cause,
        row.actor,
    ) == (
        FOREIGN_OWNER,
        source.asset_symbol,
        source.source.name,
        'ONE_DAY',
        'STATIC',
        source.start,
        ny_midnight(6),
        2,
        DatasetDeletion.CAUSE_REAPER,
        'reaper',
    )
    assert DatasetDeletion.CAUSE_REAPER == 'REAPER' != DatasetDeletion.CAUSE_OPERATOR
    assert {r.cause for r in deletions.values()} == {'REAPER'}
    assert {r.actor for r in deletions.values()} == {'reaper'}
    assert deletions[world.id('retired-daily')].bars_deleted == 1
    assert deletions[world.id('retired-stream')].bars_deleted == 0
    assert deletions[world.id('retired-daily')].range_end is None, 'an open range recorded an end'


@pytest.mark.asyncio
async def test_each_dataset_is_its_own_transaction_and_announced_after_its_own_commit(world: World, enabled):
    await Reaper().run_pass(NOW)

    announced = world.deleted_events()
    assert _names(world, (dataset_id for dataset_id, _ in announced)) == set(PENDING)
    # One commit per dataset, and DELETED for the n-th dataset is published after exactly n commits.
    assert [commits for _, commits in announced] == list(range(1, len(PENDING) + 1))
    assert world.commits == len(PENDING)
    # The read session plus one session per deletion.
    assert world.sessions == 1 + len(PENDING)
    # ReaperPendingChanged strictly after every deletion's commit.
    assert world.reaper_events() == [len(PENDING)]


@pytest.mark.asyncio
async def test_a_retired_subscription_is_deleted_only_after_its_expiry(world: World, enabled):
    reaper = Reaper()
    await reaper.run_pass(NOW)
    assert world.id('retiring-daily') in await world.entry_ids(), 'a retired subscription went before its expiry'

    await reaper.run_pass(NOW + timedelta(hours=1))
    assert world.id('retiring-daily') not in await world.entry_ids()
    assert world.id('live-stream') in await world.entry_ids(), 'a subscription with no expiry was deleted'


@pytest.mark.asyncio
async def test_one_failure_is_logged_by_type_and_the_pass_carries_on(world: World, enabled, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    failing = world.id('expired-static')
    real = reaper_module.reaper_delete_entry

    async def flaky(db, dataset_id, now):
        if dataset_id == failing:
            raise RuntimeError(f'boom {FOREIGN_OWNER} {SYMBOL_PREFIX}A')
        return await real(db, dataset_id, now)

    monkeypatch.setattr(reaper_module, 'reaper_delete_entry', flaky)

    deleted = await Reaper().run_pass(NOW)

    assert deleted == len(PENDING) - 1
    assert failing in await world.entry_ids()
    assert _names(world, await world.entry_ids()) == set(NOT_PENDING) | {'expired-static'}
    errors = [r for r in caplog.records if r.name == reaper_module.log.name and r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert 'RuntimeError' in errors[0].getMessage()
    assert 'boom' not in caplog.text, 'the exception message reached the log'
    _no_owner_or_symbol(caplog.records)
    assert failing not in {dataset_id for dataset_id, _ in world.deleted_events()}


@pytest.mark.asyncio
async def test_a_failing_deletion_record_rolls_the_delete_back(world: World, enabled, monkeypatch):
    """One transaction: a dataset never goes without its deletion row."""

    async def broken(*_args, **_kwargs):
        raise RuntimeError('deletion insert failed')

    monkeypatch.setattr(entry_repo, 'append_deletion', broken)
    bars_before = await world.bars_by_dataset()

    assert await Reaper().run_pass(NOW) == 0

    assert _names(world, await world.entry_ids()) == set(PENDING) | set(NOT_PENDING)
    assert await world.bars_by_dataset() == bars_before
    assert world.commits == 0
    assert world.deleted_events() == []


@pytest.mark.asyncio
async def test_a_dataset_extended_since_the_listing_is_not_deleted(world: World):
    """The expiry is read again inside the delete's transaction."""
    target = world.id('expired-static')
    await world.set_expiry('expired-static', NOW + timedelta(days=30))

    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        assert await entry_repo.reaper_delete_entry(db, target, NOW) is False
    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        assert await entry_repo.reaper_delete_entry(db, world.id('live-stream'), NOW) is False

    assert target in await world.entry_ids()
    assert world.id('live-stream') in await world.entry_ids()
    assert await world.all(DatasetDeletion) == []


@pytest.mark.asyncio
async def test_the_pass_does_not_delete_a_dataset_extended_between_its_listing_and_its_delete(
    world: World, enabled, monkeypatch
):
    target = world.id('expired-static')
    real_pending_ids = catalog_repo.pending_ids

    async def then_extend(db, now, dataset_ids=None):
        listed = await real_pending_ids(db, now, dataset_ids)
        await world.set_expiry('expired-static', NOW + timedelta(days=30))
        return listed

    monkeypatch.setattr(reaper_module.repo, 'pending_ids', then_extend)

    deleted = await Reaper().run_pass(NOW)

    assert deleted == len(PENDING) - 1
    assert target in await world.entry_ids()
    assert target not in {row.dataset_id for row in await world.all(DatasetDeletion)}
    assert target not in {dataset_id for dataset_id, _ in world.deleted_events()}


@pytest.mark.asyncio
async def test_a_dataset_already_gone_raises_not_found(world: World):
    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        with pytest.raises(EntryNotFound):
            await entry_repo.reaper_delete_entry(db, uuid.uuid4(), NOW)


# ------------------------------------------------------------------------------------------------- the audit rule


@pytest.mark.asyncio
async def test_a_missing_audit_table_means_an_enabled_pass_deletes_nothing(world: World, enabled, caplog):
    world.migrated = False
    caplog.set_level(logging.DEBUG)
    statements: list[str] = []
    event.listen(world.engine.sync_engine, 'before_cursor_execute', lambda *a: statements.append(a[2]))
    bars_before = await world.bars_by_dataset()

    assert await Reaper().run_pass(NOW) == 0

    assert _names(world, await world.entry_ids()) == set(PENDING) | set(NOT_PENDING)
    assert await world.bars_by_dataset() == bars_before
    writes = [s for s in statements if s.lstrip().split(None, 1)[0].upper() in {'UPDATE', 'DELETE', 'INSERT'}]
    assert writes == []
    assert world.commits == 0
    assert world.deleted_events() == []
    # The pending set still announces itself on the first pass.
    assert world.reaper_events() == [0]
    assert 'audit tables missing' in caplog.text
    _no_owner_or_symbol(caplog.records)


@pytest.mark.asyncio
async def test_a_missing_audit_table_warns_once_through_the_real_probe(world: World, enabled, monkeypatch, caplog):
    monkeypatch.setattr(dataset_override, '_warned_missing', False)

    async def unmigrated(_db) -> bool:
        return await dataset_override.audit_tables_exist(_ProbeSession((True, False)))

    monkeypatch.setattr(reaper_module, 'audit_tables_exist', unmigrated)
    caplog.set_level(logging.WARNING, logger=dataset_override.log.name)

    reaper = Reaper()
    assert await reaper.run_pass(NOW) == 0
    assert await reaper.run_pass(NOW) == 0

    warnings = [r for r in caplog.records if r.name == dataset_override.log.name and 'make migrate' in r.getMessage()]
    assert len(warnings) == 1
    assert len(await world.entry_ids()) == len(PENDING) + len(NOT_PENDING)


# ------------------------------------------------------------------------------------------------- RUNNING


@pytest.mark.asyncio
async def test_a_dataset_with_a_running_ingest_is_skipped_and_retried_on_the_next_pass(world: World, enabled, caplog):
    caplog.set_level(logging.INFO)
    running = run_of(world.rows['retired-daily'], minutes_ago=1, state=IngestRunState.RUNNING)
    # A finished run on another pending dataset does not hold it back; a RUNNING run on a live one is irrelevant.
    await world.add(
        running,
        run_of(world.rows['expired-static'], minutes_ago=30),
        run_of(world.rows['live-stream'], minutes_ago=1, state=IngestRunState.RUNNING),
    )
    reaper = Reaper()

    assert await reaper.run_pass(NOW) == len(PENDING) - 1
    assert _names(world, await world.entry_ids()) == set(NOT_PENDING) | {'retired-daily'}
    assert '1 skipped for a running ingest' in caplog.text

    async with AsyncSession(world.engine) as db:
        await db.execute(update(IngestRun).where(IngestRun.id == running.id).values(state=IngestRunState.SUCCEEDED))
        await db.commit()

    assert await reaper.run_pass(NOW) == 1
    assert _names(world, await world.entry_ids()) == set(NOT_PENDING)


# ------------------------------------------------------------------------------------------------- ReaperPendingChanged


@pytest.mark.asyncio
@pytest.mark.parametrize('deleting', [False, True], ids=['dry-run', 'enabled'])
async def test_the_first_pass_always_publishes_once_even_with_nothing_pending(world: World, monkeypatch, deleting):
    if deleting:
        monkeypatch.setenv(reaper_module.DELETES_ENABLED_ENV, 'true')
    for name in PENDING:
        await world.set_expiry(name, None)
    reaper = Reaper()

    await reaper.run_pass(NOW)
    assert world.reaper_events() == [0]

    await reaper.run_pass(NOW)
    await reaper.run_pass(NOW + timedelta(minutes=5))
    assert world.reaper_events() == [0], 'an unchanged empty set was announced again'


@pytest.mark.asyncio
async def test_dry_run_publishes_on_change_only(world: World):
    reaper = Reaper()

    await reaper.run_pass(NOW)
    await reaper.run_pass(NOW)
    assert len(world.reaper_events()) == 1, 'an unchanged set was announced'

    # An expiry moved into the past adds one.
    await world.set_expiry('future-static', NOW - timedelta(minutes=1))
    await reaper.run_pass(NOW)
    assert len(world.reaper_events()) == 2
    await reaper.run_pass(NOW)
    assert len(world.reaper_events()) == 2

    # An expiry moved out of the past removes one.
    await world.set_expiry('expired-static', None)
    await reaper.run_pass(NOW)
    assert len(world.reaper_events()) == 3

    # Time passing makes the retired subscription pending.
    await reaper.run_pass(NOW + timedelta(hours=2))
    assert len(world.reaper_events()) == 4
    await reaper.run_pass(NOW + timedelta(hours=3))
    assert len(world.reaper_events()) == 4


@pytest.mark.asyncio
async def test_enabled_publishes_after_deletions_and_then_stays_quiet(world: World, enabled):
    reaper = Reaper()
    await reaper.run_pass(NOW)
    assert len(world.reaper_events()) == 1

    # The stored set is pending minus deleted, i.e. empty: a pass that finds nothing is no change.
    await reaper.run_pass(NOW)
    assert len(world.reaper_events()) == 1


@pytest.mark.asyncio
async def test_a_deletion_on_a_later_pass_publishes_even_though_the_listed_set_matches_the_last(
    world: World, monkeypatch
):
    """After a dry-run pass the set is S; an enabled pass that lists the same S and deletes from it still publishes."""
    reaper = Reaper()
    await reaper.run_pass(NOW)
    assert len(world.reaper_events()) == 1

    monkeypatch.setenv(reaper_module.DELETES_ENABLED_ENV, 'true')
    await reaper.run_pass(NOW)
    assert len(world.reaper_events()) == 2


@pytest.mark.asyncio
async def test_a_deletion_publishes_even_when_what_remains_equals_the_last_pass(world: World, enabled):
    """A deletion is announced even when what remains equals the stored set.

    A skipped RUNNING dataset stays and a newcomer is deleted, so the remaining set looks unchanged; the pending set
    still changed during the pass, so it is announced ('after enabled-mode deletions').
    """
    for name in PENDING:
        if name != 'retired-daily':
            await world.set_expiry(name, None)
    await world.add(run_of(world.rows['retired-daily'], minutes_ago=1, state=IngestRunState.RUNNING))
    reaper = Reaper()
    await reaper.run_pass(NOW)
    assert len(world.reaper_events()) == 1
    assert world.id('retired-daily') in await world.entry_ids()

    await world.set_expiry('future-static', NOW - timedelta(minutes=1))
    assert await reaper.run_pass(NOW) == 1
    assert world.id('future-static') not in await world.entry_ids()
    assert len(world.reaper_events()) == 2, 'a deletion went unannounced because the remaining set looked unchanged'


@pytest.mark.asyncio
async def test_a_skipped_running_dataset_does_not_republish_on_every_pass(world: World, enabled):
    await world.add(run_of(world.rows['retired-daily'], minutes_ago=1, state=IngestRunState.RUNNING))
    reaper = Reaper()
    await reaper.run_pass(NOW)
    events_after_first = len(world.reaper_events())

    await reaper.run_pass(NOW)
    await reaper.run_pass(NOW)
    assert len(world.reaper_events()) == events_after_first


@pytest.mark.asyncio
async def test_a_failed_pass_does_not_advance_the_previous_set(world: World, monkeypatch):
    """A pass whose read raises publishes nothing and leaves the first-pass announcement owed."""
    reaper = Reaper()

    async def down(*_a, **_k):
        raise ConnectionError('database down')

    real = reaper_module.repo.pending_ids
    monkeypatch.setattr(reaper_module.repo, 'pending_ids', down)
    with pytest.raises(ConnectionError):
        await reaper.run_pass(NOW)
    assert world.reaper_events() == []

    monkeypatch.setattr(reaper_module.repo, 'pending_ids', real)
    await reaper.run_pass(NOW)
    assert len(world.reaper_events()) == 1


# ------------------------------------------------------------------------------------------------- the route


async def _all_pages(client: httpx.AsyncClient, limit: int) -> list[dict]:
    pages: list[dict] = []
    params: dict[str, Any] = {'limit': limit}
    while True:
        response = await client.get(PENDING_PATH, params=params)
        assert response.status_code == 200, response.text
        pages.append(response.json())
        cursor = pages[-1].get('nextCursor', '')
        if not cursor:
            return pages
        params = {'limit': limit, 'cursor': cursor}
        assert len(pages) < 50, 'the pages never ended'


def _expected_order(world: World) -> list[str]:
    pending = [world.rows[name] for name in PENDING]
    return [str(row.id) for row in sorted(pending, key=lambda r: (r.expiry, r.id))]


@pytest.mark.asyncio
async def test_the_route_lists_the_pending_set_oldest_expiry_first_with_its_fields(world: World, client):
    response = await client.get(PENDING_PATH)

    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('application/json')
    body = response.json()
    assert [item['id'] for item in body['items']] == _expected_order(world)
    assert all(item['reaperPending'] is True for item in body['items'])
    assert body.get('nextCursor', '') == ''
    assert body['asOf'] == '2026-10-06T22:00:00Z'
    assert body.get('deletesEnabled', False) is False
    expired = next(item for item in body['items'] if item['id'] == str(world.id('expired-static')))
    assert (expired['expiry'], expired['barCount'], expired['owner']) == ('2026-10-05T22:00:00Z', '2', FOREIGN_OWNER)
    retired = next(item for item in body['items'] if item['id'] == str(world.id('retired-daily')))
    assert retired['state'] == 'DATASET_STATE_RETIRED'


@pytest.mark.asyncio
async def test_the_route_needs_no_secret(world: World, client):
    """Open like the other /ui/v1 GETs: no instance secret header is sent here."""
    assert (await client.get(PENDING_PATH)).status_code == 200


@pytest.mark.asyncio
async def test_the_route_reports_deletes_enabled_from_the_env(world: World, client, monkeypatch):
    monkeypatch.setenv(reaper_module.DELETES_ENABLED_ENV, 'TRUE')
    assert (await client.get(PENDING_PATH)).json()['deletesEnabled'] is True
    monkeypatch.setenv(reaper_module.DELETES_ENABLED_ENV, '1')
    assert (await client.get(PENDING_PATH)).json().get('deletesEnabled', False) is False


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [1, 2, 3])
async def test_paging_visits_every_pending_dataset_once_in_order(world: World, client, limit):
    twin = entry(f'{SYMBOL_PREFIX}T', start=ny_midnight(1), end=ny_midnight(6), expiry=NOW - timedelta(days=1))
    await world.add(twin)
    world.rows['twin'] = twin

    pages = await _all_pages(client, limit)

    ids = [item['id'] for page in pages for item in page['items']]
    pending = [world.rows[name] for name in (*PENDING, 'twin')]
    assert ids == [str(row.id) for row in sorted(pending, key=lambda r: (r.expiry, r.id))]
    assert all(len(page['items']) <= limit for page in pages)
    assert pages[-1].get('nextCursor', '') == ''


@pytest.mark.asyncio
async def test_the_route_lists_a_running_dataset_the_pass_would_skip(world: World, client):
    """The route answers the pending set; only the pass skips a RUNNING one (it is still pending)."""
    await world.add(run_of(world.rows['retired-daily'], minutes_ago=1, state=IngestRunState.RUNNING))
    ids = [item['id'] for item in (await client.get(PENDING_PATH)).json()['items']]
    assert str(world.id('retired-daily')) in ids


@pytest.mark.asyncio
async def test_the_route_answers_an_empty_page_when_nothing_is_pending(world: World, client):
    for name in PENDING:
        await world.set_expiry(name, None)
    body = (await client.get(PENDING_PATH)).json()
    assert body.get('items', []) == []
    assert body.get('nextCursor', '') == ''


@pytest.mark.asyncio
async def test_a_catalog_cursor_is_refused_by_the_reaper_read_and_the_reverse(world: World, client):
    """The cursor is bound to its read: one minted by the catalog list is a 422 here, and a reaper cursor there."""
    catalog = await client.get('/ui/v1/datasets', params={'limit': 1, 'sort': 'DATASET_SORT_EXPIRY'})
    if catalog.status_code != 200:
        catalog = await client.get('/ui/v1/datasets', params={'limit': 1})
    assert catalog.status_code == 200, catalog.text
    catalog_cursor = catalog.json()['nextCursor']
    problem(await client.get(PENDING_PATH, params={'cursor': catalog_cursor}), status=422, reason='INVALID_REQUEST')

    reaper_cursor = (await client.get(PENDING_PATH, params={'limit': 1})).json()['nextCursor']
    assert reaper_cursor
    problem(await client.get('/ui/v1/datasets', params={'cursor': reaper_cursor}), status=422, reason='INVALID_REQUEST')


@pytest.mark.asyncio
@pytest.mark.parametrize('cursor', ['not-base64-!!', 'e30', 'W10', 'eyJvIjoicmVhcGVyIiwiayI6WzEsIngiXX0'])
async def test_a_cursor_that_does_not_decode_is_422(world: World, client, cursor):
    # e30 = {}, W10 = [], the last = {"o":"reaper","k":[1,"x"]} (a key whose id is not a uuid).
    problem(await client.get(PENDING_PATH, params={'cursor': cursor}), status=422, reason='INVALID_REQUEST')


def _cursor(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')


@pytest.mark.asyncio
@pytest.mark.parametrize('tag', ['symbol', 'expiry', 'runs', None])
async def test_a_reaper_shaped_key_under_another_reads_tag_is_422(world: World, client, tag):
    """The binding is the tag, not only the shape.

    Today no other read's key has the reaper's shape, so a forged one is the only way to show that a cursor tagged
    for another read is refused even when its key would decode.
    """
    first = (await client.get(PENDING_PATH, params={'limit': 1})).json()['nextCursor']
    padded = first + '=' * (-len(first) % 4)
    key = json.loads(base64.urlsafe_b64decode(padded))['k']
    assert (await client.get(PENDING_PATH, params={'cursor': _cursor({'o': 'reaper', 'k': key})})).status_code == 200
    forged = {'k': key} if tag is None else {'o': tag, 'k': key}
    problem(await client.get(PENDING_PATH, params={'cursor': _cursor(forged)}), status=422, reason='INVALID_REQUEST')


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', ['0', '-1', '501', 'many'])
async def test_a_limit_out_of_bounds_is_422(world: World, client, limit):
    problem(await client.get(PENDING_PATH, params={'limit': limit}), status=422)


@pytest.mark.asyncio
async def test_the_route_records_no_usage_and_writes_nothing(world: World, client, monkeypatch):
    statements: list[str] = []
    event.listen(world.engine.sync_engine, 'before_cursor_execute', lambda *a: statements.append(a[2]))
    recorded: list[Any] = []
    from data.store.app import usage_recorder

    monkeypatch.setattr(usage_recorder.usage_recorder, 'record_read', lambda *a, **k: recorded.append(a))
    assert (await client.get(PENDING_PATH)).status_code == 200
    writes = [s for s in statements if s.lstrip().split(None, 1)[0].upper() in {'UPDATE', 'DELETE', 'INSERT'}]
    assert writes == []
    assert recorded == []


@pytest.mark.asyncio
async def test_the_catalog_list_and_detail_flag_exactly_the_reaper_pending_datasets(world: World, client):
    """tj-grna9p.85 flipped this pin: list and detail carry reaper_pending, true for the pending set and unset elsewhere."""
    body = (await client.get('/ui/v1/datasets', params={'limit': 100})).json()
    flagged = {item['id'] for item in body['items'] if item.get('reaperPending')}
    assert flagged == {str(world.id(name)) for name in PENDING}
    assert all(item.get('reaperPending', False) is False for item in body['items'] if item['id'] not in flagged)
    for name in PENDING:
        assert (await client.get(f'/ui/v1/datasets/{world.id(name)}')).json()['reaperPending'] is True, name
    not_pending = next(item['id'] for item in body['items'] if item['id'] not in flagged)
    assert 'reaperPending' not in (await client.get(f'/ui/v1/datasets/{not_pending}')).json()


# ------------------------------------------------------------------------------------------------- env


@pytest.mark.parametrize(
    ('value', 'expected'),
    [(None, 3600), ('120', 120), ('1', 1), ('0', 3600), ('-5', 3600), ('abc', 3600), ('1.5', 3600), ('', 3600)],
)
def test_the_interval_reads_the_env_and_falls_back_to_the_default(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(reaper_module.INTERVAL_ENV, raising=False)
    else:
        monkeypatch.setenv(reaper_module.INTERVAL_ENV, value)
    assert reaper_module.interval_seconds() == expected
    assert reaper_module.INTERVAL_ENV == 'REAPER_INTERVAL_SECONDS'
    assert reaper_module.DEFAULT_INTERVAL_SECONDS == 3600


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        (None, False),
        ('', False),
        ('false', False),
        ('0', False),
        ('1', False),
        ('yes', False),
        ('on', False),
        ('true!', False),
        ('true', True),
        ('TRUE', True),
        ('tRuE', True),
        ('  true\n', True),
    ],
)
def test_deletes_are_enabled_only_by_an_explicit_true(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(reaper_module.DELETES_ENABLED_ENV, raising=False)
    else:
        monkeypatch.setenv(reaper_module.DELETES_ENABLED_ENV, value)
    assert reaper_module.deletes_enabled() is expected
    assert reaper_module.DELETES_ENABLED_ENV == 'REAPER_DELETES_ENABLED'


# ------------------------------------------------------------------------------------------------- the loop


def test_the_loop_is_run_periodic_with_every_of_the_env_interval(monkeypatch):
    started: list[tuple[str, Any, Callable, dict]] = []

    def fake_run_periodic(name, schedule, job, **kwargs):
        started.append((name, schedule, job, kwargs))
        return 'task'

    monkeypatch.setattr(reaper_module, 'run_periodic', fake_run_periodic)
    monkeypatch.setenv(reaper_module.INTERVAL_ENV, '90')

    assert reaper_module.start_reaper_loop() == 'task'
    ((name, schedule, _job, kwargs),) = started
    assert name == 'expiry-reaper'
    assert schedule == Every(timedelta(seconds=90))
    assert kwargs.get('log') is reaper_module.log


@pytest.mark.asyncio
async def test_the_loop_runs_a_pass_at_once_and_stops_when_cancelled(monkeypatch):
    """Through the real run_periodic: the first pass runs at start, the next waits an interval, stop ends the task."""
    passes: list[datetime | None] = []

    class FakeReaper:
        async def run_pass(self, now=None):
            passes.append(now)
            return 0

    monkeypatch.setattr(reaper_module, 'reaper', FakeReaper())
    monkeypatch.setenv(reaper_module.INTERVAL_ENV, '3600')

    task = reaper_module.start_reaper_loop()
    try:
        for _ in range(50):
            if passes:
                break
            await asyncio.sleep(0)
        assert len(passes) == 1
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(passes) == 1, 'the second pass did not wait for the interval'
        assert not task.done()
    finally:
        await reaper_module.stop_reaper_loop(task)
    assert task.done()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_the_lifespan_starts_the_reaper_and_stops_it_before_the_database(monkeypatch):
    """The wiring in app_depends.lifespan: started on startup, cancelled and awaited at teardown, database last."""
    order: list[str] = []
    tasks: list[asyncio.Task] = []
    passes: list[Any] = []

    class FakeReaper:
        async def run_pass(self, now=None):
            passes.append(now)
            return 0

    class FakeDatabase:
        async def initialize(self) -> None:
            order.append('database.initialize')

        async def shutdown(self) -> None:
            order.append('database.shutdown')

    class FakeChannel:
        async def close(self) -> None:
            order.append('channel.close')

    class FakePool:
        @staticmethod
        def worker_startup() -> None: ...

        @staticmethod
        def worker_shutdown() -> None: ...

    async def nothing(*_a, **_k) -> None:
        return None

    real_start, real_stop = app_depends.start_reaper_loop, app_depends.stop_reaper_loop

    def start() -> asyncio.Task:
        order.append('reaper.start')
        task = real_start()
        tasks.append(task)
        return task

    async def stop(task: asyncio.Task) -> None:
        order.append('reaper.stop')
        await real_stop(task)

    monkeypatch.setattr(reaper_module, 'reaper', FakeReaper())
    # The lifespan also starts the interim DAILY refresh (tj-grna9p.22), on by default; it is not this test's subject
    # and would run a real pass against the uninitialised database module and the fake channel (tj-grna9p.92).
    monkeypatch.setenv('DAILY_REFRESH_ENABLED', 'false')
    monkeypatch.setattr(app_depends, 'start_reaper_loop', start)
    monkeypatch.setattr(app_depends, 'stop_reaper_loop', stop)
    monkeypatch.setattr(app_depends, 'database', FakeDatabase())
    monkeypatch.setattr(app_depends, 'sweep_interrupted_runs', nothing)
    monkeypatch.setattr(app_depends, 'start_usage_loops', lambda: [])
    monkeypatch.setattr(app_depends, 'stop_usage_loops', nothing)
    monkeypatch.setattr(app_depends, 'initialize_latency_client', lambda *a, **k: None)
    monkeypatch.setattr(app_depends, 'init_debugger', lambda: None)
    monkeypatch.setattr(app_depends, 'startup_logs', lambda _app: None)
    monkeypatch.setattr(app_depends, 'teardown_logs', lambda _app: None)
    monkeypatch.setattr(app_depends, 'SharedWorkerPool', FakePool)
    monkeypatch.setattr(app_depends, 'target_from_env', lambda _name: 'ingest:1')
    monkeypatch.setattr(app_depends, 'create_channel', lambda _target: FakeChannel())
    monkeypatch.setattr(app_depends, 'GrpcIngestFetchClient', lambda _channel: object())

    async with app_depends.lifespan(app):
        for _ in range(50):
            if passes:
                break
            await asyncio.sleep(0)
        (task,) = tasks
        assert not task.done(), 'the reaper loop is not running while the app is up'
        assert len(passes) == 1, 'the first pass did not run at startup'

    assert task.done() and task.cancelled()
    assert order.index('database.initialize') < order.index('reaper.start')
    assert order.index('reaper.stop') < order.index('database.shutdown')


# ------------------------------------------------------------------------------------------------- the da5c881 rework
# The architect's gate on tj-grna9p.23 (2026-10-14): the delete reads the row FOR UPDATE, re-checks the expiry AND the
# RUNNING ingest under that lock, shares one count-delete-record helper with the operator delete, and logs a dataset
# the operator deleted first at INFO as 'already gone'. The lock itself is Postgres's to show (SQLite ignores FOR
# UPDATE): tests/system/test_reaper_race.py. What does not need Postgres is pinned here.


@pytest.mark.asyncio
async def test_a_running_ingest_that_appears_after_the_pass_checked_is_honoured_inside_the_delete(
    world: World, enabled, monkeypatch, caplog
):
    """The pass's own RUNNING read sees nothing; a RUNNING row lands before the delete; the delete leaves it alone."""
    caplog.set_level(logging.DEBUG)
    target = world.rows['retired-daily']
    real_running = reaper_module.running_dataset_ids

    async def then_a_run_starts(db):
        seen = await real_running(db)
        assert target.id not in seen
        await world.add(run_of(target, minutes_ago=0, state=IngestRunState.RUNNING))
        return seen

    monkeypatch.setattr(reaper_module, 'running_dataset_ids', then_a_run_starts)

    deleted = await Reaper().run_pass(NOW)

    assert deleted == len(PENDING) - 1
    assert target.id in await world.entry_ids(), 'a dataset with a running ingest was deleted'
    assert target.id not in {row.dataset_id for row in await world.all(DatasetDeletion)}
    assert target.id not in {dataset_id for dataset_id, _ in world.deleted_events()}
    assert not [r for r in caplog.records if r.name == reaper_module.log.name and r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_the_delete_itself_refuses_a_dataset_with_a_running_ingest(world: World):
    target = world.rows['expired-static']
    await world.add(run_of(target, minutes_ago=1, state=IngestRunState.RUNNING))
    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        assert await entry_repo.reaper_delete_entry(db, target.id, NOW) is False
    assert target.id in await world.entry_ids()
    assert await world.all(DatasetDeletion) == []
    # A finished run does not hold it back.
    async with AsyncSession(world.engine) as db:
        await db.execute(update(IngestRun).values(state=IngestRunState.SUCCEEDED))
        await db.commit()
    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        assert await entry_repo.reaper_delete_entry(db, target.id, NOW) is True


_SAME_IN_BOTH = (
    'dataset_id',
    'owner',
    'asset_symbol',
    'source',
    'granularity',
    'update_type',
    'range_start',
    'range_end',
    'bars_deleted',
)


@pytest.mark.asyncio
async def test_the_operator_and_the_reaper_write_the_same_deletion_row_but_for_cause_and_actor(
    world: World, monkeypatch
):
    """One helper: the same dataset deleted once by each path gives rows equal in every column but cause and actor."""

    async def migrated(_db) -> bool:
        return True

    monkeypatch.setattr(entry_repo, 'audit_tables_exist', migrated)
    original = world.rows['expired-static']
    columns = {c.key: getattr(original, c.key) for c in StoreDatasetEntry.__mapper__.column_attrs}
    async with AsyncSession(world.engine) as db:
        stored = await db.execute(select(StockMarketActivity).where(StockMarketActivity.dataset_id == original.id))
        bar_rows = [
            {c.key: getattr(b, c.key) for c in StockMarketActivity.__mapper__.column_attrs if c.key != 'id'}
            for b in stored.scalars().all()
        ]
    assert len(bar_rows) == 2

    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        await entry_repo.operator_delete_entry(db, original.id, OperatorOverrideInput(reason='same', confirmed=True))
    # The same dataset again, same id and bars, then the reaper.
    await world.add(StoreDatasetEntry(**columns))
    await world.add(*(StockMarketActivity(**values) for values in bar_rows))
    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        assert await entry_repo.reaper_delete_entry(db, original.id, NOW) is True

    by_cause = {row.cause: row for row in await world.all(DatasetDeletion)}
    assert set(by_cause) == {DatasetDeletion.CAUSE_OPERATOR, DatasetDeletion.CAUSE_REAPER}
    operator, reaper = by_cause[DatasetDeletion.CAUSE_OPERATOR], by_cause[DatasetDeletion.CAUSE_REAPER]
    assert {name: getattr(operator, name) for name in _SAME_IN_BOTH} == {
        name: getattr(reaper, name) for name in _SAME_IN_BOTH
    }
    assert operator.bars_deleted == 2
    assert operator.owner == FOREIGN_OWNER
    assert (operator.actor, reaper.actor) == ('operator', 'reaper')
    # Every column is compared above, or is one of the two that must differ, or is generated by the database.
    assert {c.name for c in DatasetDeletion.__table__.columns} == {*_SAME_IN_BOTH, 'cause', 'actor', 'id', 'deleted_at'}


@pytest.mark.asyncio
async def test_a_dataset_already_gone_is_logged_at_info_and_counted_as_neither(
    world: World, enabled, monkeypatch, caplog
):
    """The operator deleted it between the listing and the reaper's turn: INFO, no owner, not a deletion, no error."""
    caplog.set_level(logging.DEBUG)
    gone = world.id('expired-static')
    real = reaper_module.reaper_delete_entry

    async def operator_got_there_first(db, dataset_id, now):
        if dataset_id == gone:
            async with AsyncSession(world.engine) as other:
                await other.execute(StoreDatasetEntry.__table__.delete().where(StoreDatasetEntry.id == gone))
                await other.commit()
        return await real(db, dataset_id, now)

    monkeypatch.setattr(reaper_module, 'reaper_delete_entry', operator_got_there_first)

    deleted = await Reaper().run_pass(NOW)

    assert deleted == len(PENDING) - 1
    assert gone not in {dataset_id for dataset_id, _ in world.deleted_events()}
    assert gone not in {row.dataset_id for row in await world.all(DatasetDeletion)}
    ours = [r for r in caplog.records if r.name == reaper_module.log.name]
    assert not [r for r in ours if r.levelno >= logging.ERROR], [r.getMessage() for r in ours]
    gone_lines = [r for r in ours if 'already gone' in r.getMessage()]
    assert len(gone_lines) == 1
    assert gone_lines[0].levelno == logging.INFO
    assert f'{len(PENDING) - 1} of {len(PENDING)}' in caplog.text
    _no_owner_or_symbol(caplog.records)


# ------------------------------------------------------------------------------------------------- the 138c009 rework
# The architect's round 2 gate on tj-grna9p.23 (2026-10-14), item A: an enabled pass's summary line counts EVERY
# outcome, counts only: deleted, skipped (RUNNING at listing), left alone (refused under the lock: expiry moved or an
# ingest started since listing), gone before their turn (the operator deleted it first), failed. The dry-run line is
# unchanged. Each scenario below makes exactly one pending dataset take one outcome; the other three are deleted.

SUMMARY = re.compile(
    r'Expiry reaper: (?P<deleted>\d+) of (?P<pending>\d+) dataset\(s\) past expiry deleted, '
    r'(?P<skipped>\d+) skipped for a running ingest, '
    r'(?P<left_alone>\d+) left alone \(expiry moved or ingest started since listing\), '
    r'(?P<gone>\d+) gone before their turn, (?P<failed>\d+) failed'
)
OUTCOMES = ('deleted', 'skipped', 'left_alone', 'gone', 'failed')


def _summary(caplog: pytest.LogCaptureFixture) -> tuple[str, dict[str, int]]:
    """The pass's one summary line, matched whole, and its counts."""
    lines = [r.getMessage() for r in caplog.records if r.name == reaper_module.log.name]
    summaries = [line for line in lines if line.startswith('Expiry reaper: ') and 'past expiry deleted' in line]
    assert len(summaries) == 1, lines
    match = SUMMARY.fullmatch(summaries[0])
    assert match is not None, f'the summary line does not read as specified: {summaries[0]}'
    return summaries[0], {name: int(value) for name, value in match.groupdict().items()}


async def _nothing_in_the_way(world: World, monkeypatch) -> None:
    return None


async def _a_running_ingest_at_listing(world: World, monkeypatch) -> None:
    await world.add(run_of(world.rows['retired-daily'], minutes_ago=1, state=IngestRunState.RUNNING))


async def _expiry_moved_after_listing(world: World, monkeypatch) -> None:
    real = catalog_repo.pending_ids

    async def then_extend(db, now, dataset_ids=None):
        listed = await real(db, now, dataset_ids)
        await world.set_expiry('expired-static', NOW + timedelta(days=30))
        return listed

    monkeypatch.setattr(reaper_module.repo, 'pending_ids', then_extend)


async def _ingest_started_after_listing(world: World, monkeypatch) -> None:
    real = reaper_module.running_dataset_ids

    async def then_a_run_starts(db):
        seen = await real(db)
        await world.add(run_of(world.rows['retired-daily'], minutes_ago=0, state=IngestRunState.RUNNING))
        return seen

    monkeypatch.setattr(reaper_module, 'running_dataset_ids', then_a_run_starts)


async def _operator_deleted_it_first(world: World, monkeypatch) -> None:
    gone = world.id('expired-static')
    real = reaper_module.reaper_delete_entry

    async def operator_got_there_first(db, dataset_id, now):
        if dataset_id == gone:
            async with AsyncSession(world.engine) as other:
                await other.execute(StoreDatasetEntry.__table__.delete().where(StoreDatasetEntry.id == gone))
                await other.commit()
        return await real(db, dataset_id, now)

    monkeypatch.setattr(reaper_module, 'reaper_delete_entry', operator_got_there_first)


async def _one_delete_fails(world: World, monkeypatch) -> None:
    failing = world.id('expired-static')
    real = reaper_module.reaper_delete_entry

    async def flaky(db, dataset_id, now):
        if dataset_id == failing:
            raise RuntimeError(f'boom {FOREIGN_OWNER} {SYMBOL_PREFIX}A {failing}')
        return await real(db, dataset_id, now)

    monkeypatch.setattr(reaper_module, 'reaper_delete_entry', flaky)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('arrange', 'outcome'),
    [
        pytest.param(_nothing_in_the_way, 'deleted', id='deleted'),
        pytest.param(_a_running_ingest_at_listing, 'skipped', id='skipped-running-at-listing'),
        pytest.param(_expiry_moved_after_listing, 'left_alone', id='left-alone-expiry-moved'),
        pytest.param(_ingest_started_after_listing, 'left_alone', id='left-alone-ingest-started'),
        pytest.param(_operator_deleted_it_first, 'gone', id='gone-before-its-turn'),
        pytest.param(_one_delete_fails, 'failed', id='failed'),
    ],
)
async def test_the_summary_line_counts_each_outcome_and_names_no_dataset(
    world: World, enabled, monkeypatch, caplog, arrange, outcome
):
    caplog.set_level(logging.DEBUG)
    await arrange(world, monkeypatch)

    deleted = await Reaper().run_pass(NOW)

    line, counts = _summary(caplog)
    expected = dict.fromkeys(OUTCOMES, 0)
    if outcome == 'deleted':
        expected['deleted'] = len(PENDING)
    else:
        expected['deleted'] = len(PENDING) - 1
        expected[outcome] = 1
    assert counts == {'pending': len(PENDING), **expected}, line
    assert deleted == expected['deleted']
    # Every pending dataset is accounted for exactly once.
    assert sum(counts[name] for name in OUTCOMES) == counts['pending']
    # Counts only: no owner, no symbol, no dataset id.
    assert FOREIGN_OWNER not in line and SYMBOL_PREFIX not in line
    for row in world.rows.values():
        assert str(row.id) not in line and row.id.hex not in line
    _no_owner_or_symbol(caplog.records)


@pytest.mark.asyncio
async def test_the_dry_run_line_is_unchanged_and_no_enabled_summary_is_logged(world: World, caplog):
    caplog.set_level(logging.DEBUG)

    await Reaper().run_pass(NOW)

    lines = [r.getMessage() for r in caplog.records if r.name == reaper_module.log.name]
    assert lines == [f'Expiry reaper dry-run: {len(PENDING)} dataset(s) are past expiry; nothing is deleted']
