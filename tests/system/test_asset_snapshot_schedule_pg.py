"""The asset-list snapshot schedule (P47, tj-7zjov1.138) against the real, migrated Postgres tables.

WRITTEN, NOT YET RUN where it was written: it runs with the rest of the suite (make test-system, or the agent-stack
run_system_tests after test_up) on a stack migrated to the head.

THE DESIGN: P47's description (items 1 to 4), the architect's P47 note of 23:33 (the row names its list through
new_asset_list_request; the age reads the newest NON-EMPTY snapshot) and ADR tj-26jt9w addendum 17. The unit tier
(server/data/store/tests/test_asset_snapshot_schedule.py) pins the rules over SQLite; WHAT THIS PROVES that it cannot:
  AS1 the due rule reads the real asset_list_snapshot through newest_snapshot (the row_count > 0 filter and the
      ordering on Postgres): no snapshot asks both lists; a non-empty snapshot at the due time asks nothing; a newer
      EMPTY snapshot over a stale non-empty one still asks the active list;
  AS2 the in-flight guard through the real open_asset_list_request_exists on the asset_list_status enum: an open
      ACTIVE request suppresses ACTIVE and not INACTIVE, and a second pass (a store restart) stacks nothing;
  AS3 an ingest-start trigger writes a real request row with asset_list_status 'active' (the Postgres enum label),
      trigger SCHEDULE, client_label scheduler, NULL ready_by, QUEUED by the real submit path; the seed reads the real
      live_instance_id.

ISOLATION. The schedule runs here for the source IB_API only (a cadence map of one source), which the live data_store
never schedules (its map is the calendar map: ALPACA). IB_API's asset-list state is also test_asset_list_pg.py's, which
like this file clears it at the start; the suite runs serially. Every test deletes what it wrote, failed or not.
THE INSTANCE: submit_record notes the instance that answers; the fake Submit answers with the LIVE instance id the store
already knows, so no instance is created and the running stack's ingest is never superseded. Only when the database
knows no instance is one minted, and then deleted.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource, Feed
from data.store.app import asset_snapshot_schedule
from data.store.app.asset_snapshot_schedule import AssetSnapshotSchedule, SnapshotCadence
from data.store.app.daily_refresh import SCHEDULER_ACTOR
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.crud.ingest_instance import live_instance_id
from data.store.app.database.models.asset_list import AssetListEvent, AssetListRow, AssetListSnapshot
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestKind, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRun, IngestRunTrigger
from data.store.app.ingest import data_action_request
from data.store.app.trading_calendar import calendar_for_source
from schemas.data_ingest.submit import (
    AssetListStatus,
    InstanceResponse,
    SubmitAccepted,
    SubmitAck,
    SubmitRequest,
    SubmitResponse,
)


# A first sighting supersedes the stack's live ingest too; the fixture makes it live again (tj-7zjov1.233).
pytestmark = [pytest.mark.data_store, pytest.mark.asyncio, pytest.mark.usefixtures('live_instances_kept')]

REQUESTS = IngestRequest.__table__
RUNS = IngestRun.__table__
INSTANCES = IngestInstance.__table__
SNAPSHOTS, ROWS, EVENTS = AssetListSnapshot.__table__, AssetListRow.__table__, AssetListEvent.__table__
SOURCE = DataSource.IB_API
ACTIVE, INACTIVE = AssetListStatus.ACTIVE, AssetListStatus.INACTIVE
# Wednesday 2026-02-04 on XNYS: close 21:00 UTC, so with a 30-minute offset the last due time at NOW is 21:30 UTC.
OFFSET = timedelta(minutes=30)
DUE = datetime(2026, 2, 4, 21, 30, tzinfo=UTC)
NOW = datetime(2026, 2, 4, 22, tzinfo=UTC)
CALENDAR = calendar_for_source(DataSource.ALPACA_API)


def _clear(conn) -> None:
    """SOURCE's asset-list snapshots and requests (this file's and test_asset_list_pg.py's, run serially)."""
    mine = sa.select(SNAPSHOTS.c.id).where(SNAPSHOTS.c.source == SOURCE)
    conn.execute(sa.delete(EVENTS).where(EVENTS.c.snapshot_id.in_(mine)))
    conn.execute(sa.delete(ROWS).where(ROWS.c.snapshot_id.in_(mine)))
    conn.execute(sa.delete(SNAPSHOTS).where(SNAPSHOTS.c.source == SOURCE))
    stale = sa.select(REQUESTS.c.id).where(REQUESTS.c.source == SOURCE, REQUESTS.c.kind == IngestRequestKind.ASSET_LIST)
    conn.execute(sa.delete(RUNS).where(RUNS.c.request_id.in_(stale)))
    conn.execute(
        sa.delete(REQUESTS).where(REQUESTS.c.source == SOURCE, REQUESTS.c.kind == IngestRequestKind.ASSET_LIST)
    )


class Minted:
    instances: list[str]

    def __init__(self) -> None:
        self.instances = []


@pytest.fixture
def minted(pg_engine: Engine) -> Iterator[Minted]:
    with pg_engine.begin() as conn:
        _clear(conn)
    ids = Minted()
    yield ids
    with pg_engine.begin() as conn:
        _clear(conn)
        if ids.instances:
            conn.execute(sa.delete(INSTANCES).where(INSTANCES.c.instance_id.in_(ids.instances)))


@pytest_asyncio.fixture
async def sessions(pg_async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> AsyncEngine:
    @asynccontextmanager
    async def own_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(pg_async_engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(asset_snapshot_schedule, 'own_session', own_session)
    monkeypatch.setattr(data_action_request, 'own_session', own_session)
    return pg_async_engine


class LiveSubmit:
    """The IngestSubmitClient interface: records each request and accepts it under the given (live) instance id."""

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.sent: list[SubmitRequest] = []

    async def submit(self, request: SubmitRequest) -> SubmitResponse:
        self.sent.append(request)
        return SubmitResponse(
            ack=SubmitAck(accepted=SubmitAccepted(feed=Feed.IEX, quota_key='system-test-asset-snapshot')),
            instance_id=self.instance_id,
        )

    async def instance(self) -> InstanceResponse:
        return InstanceResponse(instance_id=self.instance_id, started_at=NOW)


@pytest_asyncio.fixture
async def client(sessions: AsyncEngine, minted: Minted, run_identity) -> LiveSubmit:
    async with AsyncSession(sessions) as db:
        live = await live_instance_id(db)
    if live is None:
        live = f'system-test-{run_identity.run_id}-asset-snapshot-{uuid.uuid4().hex[:6]}'
        minted.instances.append(live)
    return LiveSubmit(live)


def _schedule(client: LiveSubmit, *, handled: str | None = None) -> AssetSnapshotSchedule:
    schedule = AssetSnapshotSchedule(
        client, {SOURCE: SnapshotCadence(daily_offset=OFFSET)}, {SOURCE: CALENDAR}, clock=lambda: NOW
    )
    schedule._handled_instance = handled
    return schedule


async def _snapshot(engine: AsyncEngine, as_of: datetime, *, status=ACTIVE, rows: int = 3) -> None:
    async with AsyncSession(engine) as db:
        await db.execute(
            sa.insert(SNAPSHOTS).values(
                source=SOURCE,
                asset_type=AssetType.STOCK,
                asset_list=status,
                request_id=uuid.uuid4(),
                attempt_no=1,
                as_of=as_of,
                row_count=rows,
            )
        )
        await db.commit()


async def _requests(engine: AsyncEngine) -> list[sa.Row]:
    """SOURCE's asset-list rows, with the list as Postgres stores its enum label."""
    async with engine.connect() as conn:
        statement = (
            sa.select(
                REQUESTS,
                sa.cast(REQUESTS.c.asset_list_status, sa.Text).label('label'),
                sa.cast(REQUESTS.c.trigger, sa.Text).label('trigger_label'),
            )
            .where(REQUESTS.c.source == SOURCE, REQUESTS.c.kind == IngestRequestKind.ASSET_LIST)
            .order_by(REQUESTS.c.created_at, REQUESTS.c.asset_list_status)
        )
        return list((await conn.execute(statement)).all())


# --------------------------------------------------------------------------------------------- AS1 the due rule


async def test_as1_no_snapshot_requests_both_lists(sessions, client):
    await _schedule(client).run_daily(SOURCE)

    rows = await _requests(sessions)
    assert [row.label for row in rows] == ['active', 'inactive']
    assert [request.asset_list.status for request in client.sent] == [ACTIVE, INACTIVE]


async def test_as1_a_snapshot_at_the_due_time_is_current_and_nothing_is_requested(sessions, client):
    await _snapshot(sessions, DUE)
    await _snapshot(sessions, NOW - timedelta(days=1), status=INACTIVE)

    await _schedule(client).run_daily(SOURCE)

    assert await _requests(sessions) == []
    assert client.sent == []


async def test_as1_a_newer_empty_snapshot_does_not_make_a_stale_list_current(sessions, client):
    await _snapshot(sessions, DUE - timedelta(seconds=1))
    await _snapshot(sessions, NOW - timedelta(minutes=1), rows=0)
    await _snapshot(sessions, NOW - timedelta(days=1), status=INACTIVE)

    await _schedule(client).run_daily(SOURCE)

    assert [row.label for row in await _requests(sessions)] == ['active']


# --------------------------------------------------------------------------------------------- AS2 the in-flight guard


async def test_as2_an_open_active_request_suppresses_active_and_not_inactive_and_a_restart_stacks_nothing(
    sessions, client
):
    async with AsyncSession(sessions, expire_on_commit=False) as db:
        await request_crud.create_request(
            db,
            data_action_request.new_asset_list_request(
                SOURCE, AssetType.STOCK, ACTIVE, trigger=IngestRunTrigger.SCHEDULE, client_label=SCHEDULER_ACTOR
            ),
        )

    await _schedule(client).run_daily(SOURCE)
    assert sorted(row.label for row in await _requests(sessions)) == ['active', 'inactive']
    assert [request.asset_list.status for request in client.sent] == [INACTIVE]

    restarted = LiveSubmit(client.instance_id)
    await _schedule(restarted).run_daily(SOURCE)
    assert len(await _requests(sessions)) == 2, 'a second pass with both lists open stacks nothing'
    assert restarted.sent == []

    async with AsyncSession(sessions) as db:
        assert await request_crud.open_asset_list_request_exists(db, SOURCE, ACTIVE) is True
        assert await request_crud.open_asset_list_request_exists(db, SOURCE, INACTIVE) is True
        assert await request_crud.open_asset_list_request_exists(db, DataSource.MANUAL_ENTRY, ACTIVE) is False


# --------------------------------------------------------------------------------------------- AS3 ingest start


async def test_as3_an_ingest_start_writes_a_real_active_request_row(sessions, client):
    await _snapshot(sessions, NOW - timedelta(hours=1))
    await _snapshot(sessions, NOW - timedelta(days=1), status=INACTIVE)
    schedule = _schedule(client)
    await schedule.seed()
    async with AsyncSession(sessions) as db:
        assert schedule._handled_instance == await live_instance_id(db)

    await schedule.on_instance_seen(f'{client.instance_id}-restarted')

    (row,) = await _requests(sessions)
    assert row.label == 'active'
    assert row.trigger_label == IngestRunTrigger.SCHEDULE.value
    assert row.client_label == SCHEDULER_ACTOR
    assert row.ready_by is None
    assert row.asset_type is AssetType.STOCK
    assert row.state is IngestRequestState.QUEUED
    assert row.instance_id == client.instance_id
    (sent,) = client.sent
    assert sent.request_id == str(row.id) and sent.asset_list.status is ACTIVE and sent.ready_by is None


async def test_as3_the_handled_instance_seen_again_writes_nothing(sessions, client):
    schedule = _schedule(client)
    await schedule.seed()
    # A database that knows no instance seeds none; the minted id stands in for the live one then.
    schedule._handled_instance = schedule._handled_instance or client.instance_id

    await schedule.on_instance_seen(client.instance_id)

    assert await _requests(sessions) == []
    assert client.sent == []
