"""The expiry reaper on real Postgres: what the SQLite tier (data/store/tests/test_reaper.py) cannot prove (tj-grna9p.23).

The design is the architect's 2026-10-14 consolidated brief on tj-grna9p.23: the pending set is `expiry <= now`
whatever the update type; dry-run unless REAPER_DELETES_ENABLED is exactly `true`; enabled, each dataset is deleted
in its own transaction with a dataset_deletion row (cause REAPER, actor `reaper`, bars counted) and no override-log
row; a missing audit table means an enabled pass deletes nothing; a dataset with a RUNNING ingest is skipped.

What only Postgres shows, and is pinned here:
  * the pending predicate compares timestamptz INSTANTS through asyncpg (SQLite compares wall-clock text);
  * the delete cascades through the real ON DELETE CASCADE foreign key and the record counts the bars it took;
  * the deletion row's SensitiveString owner, gen_random_uuid() id and now() timestamp round-trip;
  * the real audit_tables_exist (to_regclass) answers False before revision d7e2a9c35b18, and the enabled pass then
    deletes nothing;
  * the running ingest_run partial-index read finds the RUNNING dataset.
And, over real HTTP against the running stack: GET /ui/v1/reaper/pending lists an expired dataset this run created,
flagged reaperPending, and the stack (default env) reports deletesEnabled false.

WHY A SCRATCH DATABASE. An enabled pass deletes EVERY pending dataset it finds, and the stack's database holds rows
this suite does not own (the dev seed's STATIC rows with a long-past expiry among them). So every pass here runs
against a database this test creates, migrates and drops (test_usage_record's scratch_db, reused). The HTTP check
only reads; the stack's own reaper is in its default dry-run.

Run by make test-system and the agent-stack MCP's run_system_tests. FAIL, NEVER SKIP.
"""

import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType
from data.store.app import reaper as reaper_module
from data.store.app.database.crud.stock import dataset_catalog as catalog_repo
from data.store.app.database.models.dataset_deletion import DatasetDeletion
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.events import DatasetChanged, ReaperPendingChanged
from data.store.app.reaper import Reaper
from routers.data_store.app_endpoints import UiReaperInterface
from tests.system import test_usage_record
from tests.system.conftest import BAR_TABLE, ENTRY_TABLE


pytestmark = pytest.mark.data_store

# Reused, not copied: a scratch database this test creates and drops, and an asyncpg engine on it.
scratch_db = test_usage_record.scratch_db
scratch_async = test_usage_record.scratch_async
_head = test_usage_record._head

DELETION = DatasetDeletion.__table__
OVERRIDE = DatasetOverrideLog.__table__
RUN = IngestRun.__table__
PENDING_PATH = UiReaperInterface.GET_UI_REAPER_PENDING.value


class Seeded:
    """The scratch rows, by name, and the instant the pass judges at."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.ids: dict[str, uuid.UUID] = {}
        self.bars: dict[str, int] = {}

    def name_of(self, dataset_id: uuid.UUID) -> str:
        return next(name for name, i in self.ids.items() if i == dataset_id)


# Judged at `now`: PENDING are an expired STATIC with 3 bars, a STATIC whose expiry is EXACTLY now in another
# offset (+05:00, the same instant), a retired DAILY an hour past, and a retired DAILY with a RUNNING ingest. NOT
# PENDING: a retired DAILY an hour ahead, a STATIC with no expiry holding 2 bars.
PENDING = ('expired-static', 'exact-static', 'retired-daily', 'running-daily')
NOT_PENDING = ('retiring-daily', 'forever-static')


def _seed(engine: sa.Engine, entry_values, bar_values, run_identity) -> Seeded:
    now = datetime.now(UTC).replace(microsecond=0)
    seeded = Seeded(now)
    plus_five = now.astimezone(ZoneInfo('Asia/Karachi'))  # +05:00, no DST: the same instant as now
    static = {'update_type': UpdateType.STATIC.value, 'expiry_type': ExpiryType.BULK.value}
    daily = {'update_type': UpdateType.DAILY.value, 'expiry_type': ExpiryType.ROLLING.value}
    plan: dict[str, tuple[dict[str, Any], int]] = {
        'expired-static': ({**static, 'expiry': now - timedelta(days=1)}, 3),
        'exact-static': ({**static, 'expiry': plus_five}, 0),
        'retired-daily': ({**daily, 'expiry': now - timedelta(hours=1)}, 1),
        'running-daily': ({**daily, 'expiry': now - timedelta(hours=2)}, 0),
        'retiring-daily': ({**daily, 'expiry': now + timedelta(hours=1)}, 0),
        'forever-static': ({**static, 'expiry': None}, 2),
    }
    with engine.begin() as conn:
        for name, (columns, bar_count) in plan.items():
            row = conn.execute(
                sa.insert(ENTRY_TABLE)
                .values(entry_values(asset_symbol=run_identity.alt_symbol(name.replace('-', '')), **columns))
                .returning(ENTRY_TABLE)
            ).one()
            seeded.ids[name] = row.id
            seeded.bars[name] = bar_count
            for minute in range(bar_count):
                conn.execute(
                    sa.insert(BAR_TABLE).values(bar_values(row, timestamp=row.start + timedelta(minutes=minute)))
                )
        conn.execute(
            sa.insert(RUN).values(
                dataset_id=seeded.ids['running-daily'],
                trigger=IngestRunTrigger.REQUEST,
                actor=run_identity.owner,
                asset_symbol='RUN',
                asset_type=AssetType.STOCK,
                data_type=DataType.MARKET_ACTIVITY,
                source=DataSource.ALPACA_API,
                granularity=Granularity.ONE_MINUTE,
                update_type=UpdateType.DAILY.value,
                state=IngestRunState.RUNNING,
            )
        )
    return seeded


@pytest.fixture
def point_reaper(monkeypatch: pytest.MonkeyPatch, scratch_async: AsyncEngine) -> list[Any]:
    """The reaper module's own_session on the scratch database; returns the events it publishes."""

    @asynccontextmanager
    async def own_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(scratch_async, expire_on_commit=False) as session:
            yield session

    published: list[Any] = []
    monkeypatch.setattr(reaper_module, 'own_session', own_session)
    monkeypatch.setattr(reaper_module, 'publish', published.append)
    monkeypatch.delenv(reaper_module.DELETES_ENABLED_ENV, raising=False)
    return published


def _entry_ids(engine: sa.Engine) -> set[uuid.UUID]:
    with engine.connect() as conn:
        return set(conn.execute(sa.select(ENTRY_TABLE.c.id)).scalars())


def _bar_counts(engine: sa.Engine) -> dict[uuid.UUID, int]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(BAR_TABLE.c.dataset_id, sa.func.count()).group_by(BAR_TABLE.c.dataset_id))
        return dict(rows.tuples().all())


@pytest.mark.asyncio
async def test_dry_run_on_postgres_lists_exactly_the_expired_set_and_deletes_nothing(
    scratch_db, scratch_async, point_reaper, entry_values, bar_values, run_identity
):
    scratch_db.must('upgrade', _head())
    seeded = _seed(scratch_db.engine, entry_values, bar_values, run_identity)
    bars_before = _bar_counts(scratch_db.engine)

    async with AsyncSession(scratch_async) as session:
        pending = await catalog_repo.pending_ids(session, seeded.now)
        listed = await catalog_repo.list_pending(session, seeded.now)
    assert {seeded.name_of(i) for i in pending} == set(PENDING)
    assert [row.id for row in listed] == [row.id for row in sorted(listed, key=lambda r: (r.expiry, r.id))], (
        'list_pending is not ordered by (expiry, id) on Postgres'
    )
    assert {row.id for row in listed} == pending

    assert await Reaper().run_pass(seeded.now) == 0

    assert _entry_ids(scratch_db.engine) == set(seeded.ids.values())
    assert _bar_counts(scratch_db.engine) == bars_before
    with scratch_db.engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(DELETION)).scalar_one() == 0
    assert point_reaper == [ReaperPendingChanged()]


@pytest.mark.asyncio
async def test_enabled_on_postgres_deletes_and_records_each_pending_dataset_and_skips_the_running_one(
    scratch_db, point_reaper, entry_values, bar_values, run_identity, monkeypatch
):
    scratch_db.must('upgrade', _head())
    seeded = _seed(scratch_db.engine, entry_values, bar_values, run_identity)
    monkeypatch.setenv(reaper_module.DELETES_ENABLED_ENV, 'true')
    deleted_names = set(PENDING) - {'running-daily'}

    assert await Reaper().run_pass(seeded.now) == len(deleted_names)

    remaining = {seeded.name_of(i) for i in _entry_ids(scratch_db.engine)}
    assert remaining == set(NOT_PENDING) | {'running-daily'}
    bars = _bar_counts(scratch_db.engine)
    for name in deleted_names:
        assert seeded.ids[name] not in bars, f'{name} kept bars: the cascade did not run'
    assert bars[seeded.ids['forever-static']] == 2, 'another dataset lost bars'

    with scratch_db.engine.connect() as conn:
        rows = conn.execute(sa.select(DELETION)).all()
        overrides = conn.execute(sa.select(sa.func.count()).select_from(OVERRIDE)).scalar_one()
    assert overrides == 0, 'the reaper wrote an override row'
    by_name = {seeded.name_of(row.dataset_id): row for row in rows}
    assert set(by_name) == deleted_names
    for name, row in by_name.items():
        assert (row.cause, row.actor, row.bars_deleted) == ('REAPER', 'reaper', seeded.bars[name]), name
        assert row.owner == run_identity.owner
        assert row.id is not None and row.deleted_at is not None

    deleted_events = [e.id for e in point_reaper if isinstance(e, DatasetChanged)]
    assert {seeded.name_of(i) for i in deleted_events} == deleted_names
    assert point_reaper[-1] == ReaperPendingChanged()


@pytest.mark.asyncio
async def test_enabled_on_postgres_before_the_audit_revision_deletes_nothing(
    scratch_db, point_reaper, entry_values, bar_values, run_identity, monkeypatch
):
    """The REAL to_regclass probe answers False when the audit tables are absent, so nothing is deleted unrecorded.

    THE ABSENCE IS MADE AT HEAD, NOT BY STOPPING AT b3d9e1a47c62 (tj-grna9p.176). This test used to migrate only to
    PRE_AUDIT_REVISION, which worked while every revision after it left store_dataset_entry alone. e5c8b1d4f703 adds
    store_dataset_entry.collect, and the head's reaper reads the whole entry row (and the seed's RETURNING names
    every model column), so a database below that revision is no longer one the head's code can run on at all. The
    audit tables are renamed away and back instead: to_regclass sees exactly what it saw below d7e2a9c35b18, on an
    entry table the head's code matches.
    """
    scratch_db.must('upgrade', _head())
    seeded = _seed(scratch_db.engine, entry_values, bar_values, run_identity)
    monkeypatch.setenv(reaper_module.DELETES_ENABLED_ENV, 'true')
    _rename_audit_tables(scratch_db.engine, hidden=True)
    bars_before = _bar_counts(scratch_db.engine)

    assert await Reaper().run_pass(seeded.now) == 0

    assert _entry_ids(scratch_db.engine) == set(seeded.ids.values())
    assert _bar_counts(scratch_db.engine) == bars_before
    assert not [e for e in point_reaper if isinstance(e, DatasetChanged)]

    # Migrated, the next pass deletes.
    _rename_audit_tables(scratch_db.engine, hidden=False)
    assert await Reaper().run_pass(seeded.now) == len(PENDING) - 1


HIDDEN_SUFFIX = '_hidden_by_test'


def _rename_audit_tables(engine: sa.Engine, *, hidden: bool) -> None:
    """Rename both audit tables away (hidden) or back, so the probe answers as on a database without them."""
    with engine.begin() as conn:
        for table in (DELETION.name, OVERRIDE.name):
            old, new = (table, f'{table}{HIDDEN_SUFFIX}') if hidden else (f'{table}{HIDDEN_SUFFIX}', table)
            conn.execute(sa.text(f'ALTER TABLE public.{old} RENAME TO {new}'))  # nosec B608 -- model table names


@pytest.fixture
def expired_entry(insert_entry: Callable[..., sa.Row], run_identity) -> Iterator[sa.Row]:
    """One STATIC entry on the STACK's database whose expiry passed a day ago; insert_entry's registry deletes it."""
    yield insert_entry(
        asset_symbol=run_identity.alt_symbol('rpr'),
        expiry=datetime.now(UTC) - timedelta(days=1),
        update_type=UpdateType.STATIC.value,
        expiry_type=ExpiryType.BULK.value,
    )


def test_the_running_stack_lists_an_expired_dataset_and_reports_dry_run(data_store, expired_entry):
    """Over real HTTP: the pending read is open, flags the dataset, and the stack's default is dry-run."""
    found: dict | None = None
    params: dict[str, Any] = {'limit': 500}
    for _ in range(200):
        response = data_store.get(PENDING_PATH, params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body.get('deletesEnabled', False) is False, 'the stack is not in dry-run'
        assert body['asOf']
        found = next((item for item in body.get('items', []) if item['id'] == str(expired_entry.id)), None)
        cursor = body.get('nextCursor', '')
        if found is not None or not cursor:
            break
        params = {'limit': 500, 'cursor': cursor}
    assert found is not None, 'the expired dataset is not in the pending read'
    assert found['reaperPending'] is True
    assert found['owner'] == expired_entry.owner
