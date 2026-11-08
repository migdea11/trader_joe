"""The asset-list snapshot (revision a3f8d2c6e914, tj-7zjov1.30 P17) on the real, migrated Postgres.

WRITTEN, NOT YET RUN where it was written: it runs with the rest of the suite (make test-system, or the agent-stack
run_system_tests after test_up) on a stack migrated to the head.

THE DESIGN: the architect's ruling of 23:31 UTC on tj-7zjov1.30 (ADR tj-26jt9w addendum 17) and the bead's
description and 14:31 and 21:56 UTC notes. WHAT THIS PROVES that server/data/store/tests cannot, because SQLite has no
Postgres enum types, no advisory locks, one writer, and evaluates the CHECK in its own dialect:
  AL1 the CHECK ck_ingest_request_asset_list_status refuses an ASSET_LIST row with NULL list and a BARS row with a
      list (23514 naming the constraint), and admits both right combinations; the enum type asset_list_status holds
      exactly AssetListStatus's values, the kind type the model's;
  AL2 create_request then get_request round-trips each list on asyncpg, and open_asset_list_request_exists answers
      from the real enum comparison;
  AL3 the arm on Postgres, end to end from the store's own request row: submit_request_of -> the REAL
      AssetListAttemptRunner (ingest) over a stub reader -> the report as the executor assembles it -> DeliveryIntake.
      The list ingest was asked for is the list the snapshot is filed under; the attempt row has NULL labels, no served
      range and no vendor asset id; the second snapshot derives the expected events; a replay writes nothing twice;
  AL4 the transaction advisory lock: a delivery of a (source, list) waits behind another transaction holding that
      list's lock, and a delivery of the OTHER list does not.

P17f (tj-7zjov1.164, ADR tj-26jt9w addendum 18), on the same real tables:
  AL5 a LATE snapshot (18 d): a superseded instance's report landing after the recovered attempt, its as_of earlier
      than or EQUAL to the newest non-empty snapshot's, is stored raw, derives no event, answers FINAL and logs one
      WARNING with the request id and both instants and no symbol;
  AL6 a MOVE between lists (18 b), in both arrival orders: a vanished on the list it left plus a status_flip on the
      list it reached, the same two events whichever list arrives first;
  AL7 the INSTANCE WATCHER sends asset lists (18 g): a PENDING ASSET_LIST row past the resend delay is re-sent with
      its list, an abandoned one (held by a superseded instance) is re-submitted recovered with its durable count
      after the delivery deadline plus the margin, and once the next attempt is delivered through the real intake
      open_asset_list_request_exists is False for that list. The watcher's two reads are scoped to this test's
      requests, as in test_resubmit_races_pg.py.

ISOLATION. Snapshots are append-only for the service and the diff base is the newest non-empty snapshot of a (source,
list), shared by every writer, and open_asset_list_request_exists reads every open request of a (source, list). These
tests use the source IB_API, which no service and no other system test writes asset lists for (MANUAL_ENTRY is
test_asset_list_submit_pg.py's), delete exactly what they wrote at the end of each test, failed or not, and first
clear any IB_API asset-list state a killed earlier run of this file left behind, so a leftover can never be a base or
an open request here.
"""

import asyncio
import logging
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
from common.enums.data_stock import DataSource
from data.ingest.app.brokers.alpaca.asset_attempt import AssetListAttemptRunner, AssetListWork
from data.ingest.app.brokers.interface import Asset, AssetListResponse
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler.queue import ScheduledRequest
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.models.asset_list import (
    AssetListEvent,
    AssetListEventKind,
    AssetListRow,
    AssetListSnapshot,
)
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestKind, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.ingest import data_action_request, delivery_intake, instance_watch
from data.store.app.ingest.data_action_request import new_asset_list_request, submit_request_of
from data.store.app.ingest.delivery_intake import DeliveryIntake
from data.store.app.ingest.instance_watch import InstanceWatcher, WatchResult, resubmit_delay
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    DeliverAttemptResponse,
    DeliveryHeader,
    DeliveryVerdict,
    RegisterInstanceRequest,
)
from schemas.data_ingest.submit import (
    AssetListStatus,
    InstanceResponse,
    SubmitAccepted,
    SubmitAck,
    SubmitRequest,
    SubmitResponse,
)


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

REQUESTS = IngestRequest.__table__
RUNS = IngestRun.__table__
INSTANCES = IngestInstance.__table__
SNAPSHOTS, ROWS, EVENTS = AssetListSnapshot.__table__, AssetListRow.__table__, AssetListEvent.__table__
SOURCE = DataSource.IB_API
SQLSTATE_CHECK_VIOLATION = '23514'
QUOTA_SCOPE = 'system-test-trading-scope'
DAY_0 = datetime(2026, 4, 1, 14, 0, tzinfo=UTC)
LOCK_WAIT_S = 10.0
ACTIVE, INACTIVE = AssetListStatus.ACTIVE, AssetListStatus.INACTIVE


# --------------------------------------------------------------------------------------------- fixtures


class Created:
    """The ids one test wrote, deleted at its end: nothing this run did not write is deleted."""

    def __init__(self) -> None:
        self.requests: list[uuid.UUID] = []
        self.instances: list[str] = []


@pytest.fixture
def created(pg_engine: Engine) -> Iterator[Created]:
    with pg_engine.begin() as conn:
        # Leftovers of a killed earlier run of this file: SOURCE's asset-list state belongs to this file alone.
        mine = sa.select(SNAPSHOTS.c.id).where(SNAPSHOTS.c.source == SOURCE)
        conn.execute(sa.delete(EVENTS).where(EVENTS.c.snapshot_id.in_(mine)))
        conn.execute(sa.delete(ROWS).where(ROWS.c.snapshot_id.in_(mine)))
        conn.execute(sa.delete(SNAPSHOTS).where(SNAPSHOTS.c.source == SOURCE))
        stale = sa.select(REQUESTS.c.id).where(
            REQUESTS.c.source == SOURCE, REQUESTS.c.kind == IngestRequestKind.ASSET_LIST
        )
        conn.execute(sa.delete(RUNS).where(RUNS.c.request_id.in_(stale)))
        conn.execute(
            sa.delete(REQUESTS).where(REQUESTS.c.source == SOURCE, REQUESTS.c.kind == IngestRequestKind.ASSET_LIST)
        )
    ids = Created()
    yield ids
    with pg_engine.begin() as conn:
        if ids.requests:
            snapshots = sa.select(SNAPSHOTS.c.id).where(SNAPSHOTS.c.request_id.in_(ids.requests))
            conn.execute(sa.delete(EVENTS).where(EVENTS.c.snapshot_id.in_(snapshots)))
            conn.execute(sa.delete(ROWS).where(ROWS.c.snapshot_id.in_(snapshots)))
            conn.execute(sa.delete(SNAPSHOTS).where(SNAPSHOTS.c.request_id.in_(ids.requests)))
            conn.execute(sa.delete(RUNS).where(RUNS.c.request_id.in_(ids.requests)))
            conn.execute(sa.delete(REQUESTS).where(REQUESTS.c.id.in_(ids.requests)))
        if ids.instances:
            conn.execute(sa.delete(INSTANCES).where(INSTANCES.c.instance_id.in_(ids.instances)))


@pytest_asyncio.fixture
async def intake(pg_async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> DeliveryIntake:
    @asynccontextmanager
    async def own_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(pg_async_engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(delivery_intake, 'own_session', own_session)
    monkeypatch.setattr(delivery_intake, 'publish', lambda event: None)
    return DeliveryIntake()


@pytest.fixture
def instance_id(run_identity, created: Created) -> str:
    minted = f'system-test-{run_identity.run_id}-asset-list-{uuid.uuid4().hex[:6]}'
    created.instances.append(minted)
    return minted


class StubReader:
    """A BrokerRead that serves one fixed asset list, counting its one call on the query's tally."""

    def __init__(self, assets: list[Asset], as_of: datetime) -> None:
        self.assets, self.as_of = assets, as_of
        self.queries: list = []

    async def get_assets(self, query):
        self.queries.append(query)
        if query.tally is not None:
            query.tally.record(self.as_of)
        return AssetListResponse(assets=self.assets, as_of=self.as_of)


def asset(vendor_asset_id: str, symbol: str, active: bool | None = True) -> Asset:
    raw = f'{{"id":"{vendor_asset_id}","symbol":"{symbol}"}}'.encode()
    return Asset(vendor_asset_id=vendor_asset_id, symbol=symbol, raw_payload=raw, active=active, tradable=active)


async def _stored_request(engine: AsyncEngine, created: Created, run_identity, status: AssetListStatus):
    """The row the store writes for an asset list, by the store's own writer, QUEUED as the submit path leaves it."""
    async with AsyncSession(engine, expire_on_commit=False) as db:
        record = await request_crud.create_request(
            db,
            new_asset_list_request(
                SOURCE, AssetType.STOCK, status, trigger=IngestRunTrigger.SCHEDULE, client_label=run_identity.owner
            ),
        )
    created.requests.append(record.id)
    return record


async def _deliver_through_the_runner(
    intake: DeliveryIntake, engine: AsyncEngine, record, instance: str, reader: StubReader
) -> tuple[DeliverAttemptResponse, AttemptDelivery]:
    """Send the stored row as the store sends it, run the REAL runner on what ingest received, deliver its report."""
    sent = submit_request_of(record)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        assert await request_crud.mark_queued(db, record.id, instance) is not None
    work = AssetListWork(request=sent.asset_list, priority=RequestPriority.BACKFILL)
    scheduled = ScheduledRequest(request_id=sent.request_id, quota_scope=QUOTA_SCOPE, payload=work)
    result = await AssetListAttemptRunner(reader)(scheduled, 1)
    # The executor's own assembly (scheduler/executor.py, __delivery): the identity on the runner's result.
    attempt = AttemptDelivery(
        header=DeliveryHeader(
            request_id=sent.request_id,
            instance_id=instance,
            attempt_no=1,
            quota_key=result.quota_key,
            feed=result.feed,
            vendor_asset_id=result.vendor_asset_id,
        ),
        parts=result.parts,
        trailer=result.trailer,
    )
    return await intake.deliver(attempt), attempt


async def _one(engine: AsyncEngine, statement) -> sa.Row:
    async with engine.connect() as conn:
        return (await conn.execute(statement)).one()


async def _all(engine: AsyncEngine, statement) -> list[sa.Row]:
    async with engine.connect() as conn:
        return list((await conn.execute(statement)).all())


# --------------------------------------------------------------------------------------------- AL1 the CHECK and types


def _request_values(run_identity, created: Created, **values) -> dict:
    request_id = uuid.uuid4()
    created.requests.append(request_id)
    return {
        'id': request_id,
        'source': SOURCE,
        'client_label': run_identity.owner,
        'state': IngestRequestState.PENDING,
        'attempts': 0,
        **values,
    }


@pytest.mark.parametrize(
    ('kind', 'status'),
    [
        (IngestRequestKind.ASSET_LIST, None),
        (IngestRequestKind.BARS, ACTIVE),
        (IngestRequestKind.CORPORATE_ACTIONS, INACTIVE),
    ],
    ids=['asset-list-without-a-list', 'bars-with-a-list', 'corporate-actions-with-a-list'],
)
async def test_al1_the_check_refuses_both_wrong_combinations(attempt, run_identity, created, kind, status):
    error = attempt(
        sa.insert(REQUESTS).values(_request_values(run_identity, created, kind=kind, asset_list_status=status))
    )

    assert error is not None, f'the CHECK admitted {kind} with list {status}'
    assert error.orig.pgcode == SQLSTATE_CHECK_VIOLATION, error.orig
    assert error.orig.diag.constraint_name == IngestRequest.ASSET_LIST_STATUS_CONSTRAINT


@pytest.mark.parametrize(
    ('kind', 'status'),
    [(IngestRequestKind.ASSET_LIST, ACTIVE), (IngestRequestKind.ASSET_LIST, INACTIVE), (IngestRequestKind.BARS, None)],
    ids=['asset-list-active', 'asset-list-inactive', 'bars-without-a-list'],
)
async def test_al1_the_check_admits_the_right_combinations(attempt, run_identity, created, kind, status):
    assert (
        attempt(sa.insert(REQUESTS).values(_request_values(run_identity, created, kind=kind, asset_list_status=status)))
        is None
    )


async def test_al1_the_enum_types_hold_the_domains_values_and_the_check_is_the_designs(pg_conn):
    status = pg_conn.execute(sa.text('SELECT enum_range(NULL::asset_list_status)::text[]')).scalar_one()
    kind = pg_conn.execute(sa.text('SELECT enum_range(NULL::asset_list_event_kind)::text[]')).scalar_one()
    definition = pg_conn.execute(
        sa.text('SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name'),
        {'name': IngestRequest.ASSET_LIST_STATUS_CONSTRAINT},
    ).scalar_one()

    assert status == [member.value for member in AssetListStatus] == ['active', 'inactive']
    assert kind == [member.value for member in AssetListEventKind]
    assert definition == "CHECK (((kind = 'ASSET_LIST'::ingest_request_kind) = (asset_list_status IS NOT NULL)))"
    columns = pg_conn.execute(
        sa.text(
            "SELECT table_name, udt_name, is_nullable FROM information_schema.columns WHERE table_schema = 'public' "
            "AND column_name IN ('asset_list_status', 'asset_list') ORDER BY table_name"
        )
    ).all()
    assert [tuple(row) for row in columns] == [
        ('asset_list_event', 'asset_list_status', 'NO'),
        ('asset_list_snapshot', 'asset_list_status', 'NO'),
        ('ingest_request', 'asset_list_status', 'YES'),
    ], 'one shared type, nullable on the request only'


# --------------------------------------------------------------------------------------------- AL2 the request's list


@pytest.mark.parametrize('status', list(AssetListStatus), ids=lambda status: status.value)
async def test_al2_each_list_round_trips_and_the_open_read_answers_for_it(
    pg_async_engine, run_identity, created, status
):
    record = await _stored_request(pg_async_engine, created, run_identity, status)

    async with AsyncSession(pg_async_engine) as db:
        assert (await request_crud.get_request(db, record.id)).asset_list_status is status
        assert await request_crud.open_asset_list_request_exists(db, SOURCE, status) is True
    async with AsyncSession(pg_async_engine) as db:
        await request_crud.finish_request(db, record.id, state=IngestRequestState.EMPTY)
    async with AsyncSession(pg_async_engine) as db:
        assert await request_crud.open_asset_list_request_exists(db, SOURCE, status) is False


# --------------------------------------------------------------------------------------------- AL3 the arm, end to end


async def test_al3_the_list_asked_for_is_the_list_filed_and_the_second_snapshot_derives_its_events(
    intake, pg_async_engine, run_identity, created, instance_id
):
    await intake.register_instance(RegisterInstanceRequest(instance_id=instance_id, started_at=DAY_0))
    first = await _stored_request(pg_async_engine, created, run_identity, INACTIVE)
    reader = StubReader([asset('pg-a', 'ZZPGA', False), asset('pg-b', 'ZZPGB', None)], DAY_0)
    answer, _ = await _deliver_through_the_runner(intake, pg_async_engine, first, instance_id, reader)

    assert answer == DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=1)
    assert reader.queries[0].status is INACTIVE, 'ingest was asked for the stored list'
    snapshot = await _one(pg_async_engine, sa.select(SNAPSHOTS).where(SNAPSHOTS.c.request_id == first.id))
    assert (snapshot.asset_list, snapshot.source, snapshot.row_count, snapshot.as_of) == (INACTIVE, SOURCE, 2, DAY_0)
    rows = await _all(pg_async_engine, sa.select(ROWS).where(ROWS.c.snapshot_id == snapshot.id).order_by(ROWS.c.symbol))
    assert [(row.vendor_asset_id, row.active, bytes(row.raw_payload)) for row in rows] == [
        ('pg-a', False, b'{"id":"pg-a","symbol":"ZZPGA"}'),
        ('pg-b', None, b'{"id":"pg-b","symbol":"ZZPGB"}'),
    ]
    (run,) = await _all(pg_async_engine, sa.select(RUNS).where(RUNS.c.request_id == first.id))
    assert run.state is IngestRunState.SUCCEEDED and run.quota_key == QUOTA_SCOPE
    assert (run.asset_symbol, run.asset_type, run.data_type, run.granularity, run.update_type) == (None,) * 5
    assert (run.served_start, run.served_end, run.vendor_asset_id, run.feed) == (None,) * 4

    second = await _stored_request(pg_async_engine, created, run_identity, INACTIVE)
    later = DAY_0 + timedelta(days=7)
    reader = StubReader([asset('pg-a', 'ZZPGA', False), asset('pg-c', 'ZZPGC', False)], later)
    _, replayed = await _deliver_through_the_runner(intake, pg_async_engine, second, instance_id, reader)
    current = await _one(pg_async_engine, sa.select(SNAPSHOTS).where(SNAPSHOTS.c.request_id == second.id))
    events = await _all(pg_async_engine, sa.select(EVENTS).where(EVENTS.c.snapshot_id == current.id))

    assert sorted((event.kind, event.vendor_asset_id, event.symbol) for event in events) == [
        (AssetListEventKind.APPEARED, 'pg-c', 'ZZPGC'),
        (AssetListEventKind.VANISHED, 'pg-b', 'ZZPGB'),
    ]
    assert {(event.previous_snapshot_id, event.previous_as_of, event.as_of) for event in events} == {
        (snapshot.id, DAY_0, later)
    }

    # A replay of the same attempt writes nothing twice.
    before = (
        await _all(
            pg_async_engine, sa.select(sa.func.count()).select_from(ROWS).where(ROWS.c.snapshot_id == current.id)
        ),
        await _all(pg_async_engine, sa.select(sa.func.count()).select_from(EVENTS)),
        await _all(pg_async_engine, sa.select(sa.func.count()).select_from(SNAPSHOTS)),
    )
    again = await intake.deliver(replayed)
    after = (
        await _all(
            pg_async_engine, sa.select(sa.func.count()).select_from(ROWS).where(ROWS.c.snapshot_id == current.id)
        ),
        await _all(pg_async_engine, sa.select(sa.func.count()).select_from(EVENTS)),
        await _all(pg_async_engine, sa.select(sa.func.count()).select_from(SNAPSHOTS)),
    )
    assert again.verdict is DeliveryVerdict.FINAL and after == before


async def test_al3_an_empty_list_served_by_the_runner_is_an_empty_snapshot_of_the_requests_list(
    intake, pg_async_engine, run_identity, created, instance_id
):
    """The runner sends SUCCEEDED with no part for a served empty list; the store files it under the request's list."""
    await intake.register_instance(RegisterInstanceRequest(instance_id=instance_id, started_at=DAY_0))
    record = await _stored_request(pg_async_engine, created, run_identity, INACTIVE)

    _, attempt = await _deliver_through_the_runner(intake, pg_async_engine, record, instance_id, StubReader([], DAY_0))

    assert attempt.parts == []
    snapshot = await _one(pg_async_engine, sa.select(SNAPSHOTS).where(SNAPSHOTS.c.request_id == record.id))
    assert (snapshot.asset_list, snapshot.row_count) == (INACTIVE, 0)
    (run,) = await _all(pg_async_engine, sa.select(RUNS).where(RUNS.c.request_id == record.id))
    assert run.state is IngestRunState.EMPTY


# --------------------------------------------------------------------------------------------- AL4 the list lock


async def _blocked_by(engine: AsyncEngine, pid: int) -> bool:
    query = sa.text('SELECT count(*) FROM pg_stat_activity WHERE CAST(:pid AS integer) = ANY(pg_blocking_pids(pid))')
    async with engine.connect() as conn:
        return (await conn.execute(query, {'pid': pid})).scalar_one() > 0


@pytest.mark.parametrize(('held', 'delivered', 'waits'), [(ACTIVE, ACTIVE, True), (INACTIVE, ACTIVE, False)])
async def test_al4_a_delivery_waits_for_another_transaction_holding_its_lists_lock_and_only_that_list(
    intake, pg_async_engine, run_identity, created, instance_id, held, delivered, waits
):
    """The key the arm locks is the (source, list) one: held for the same list it blocks, for the other it does not."""
    await intake.register_instance(RegisterInstanceRequest(instance_id=instance_id, started_at=DAY_0))
    record = await _stored_request(pg_async_engine, created, run_identity, delivered)
    holder = await pg_async_engine.connect()
    try:
        transaction = await holder.begin()
        await holder.execute(
            sa.text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': f'asset_list:{SOURCE}:{held}'}
        )
        pid = (await holder.execute(sa.text('SELECT pg_backend_pid()'))).scalar_one()
        delivery = asyncio.create_task(
            _deliver_through_the_runner(
                intake, pg_async_engine, record, instance_id, StubReader([asset('pg-l', 'ZZPGL')], DAY_0)
            )
        )
        deadline = asyncio.get_running_loop().time() + (LOCK_WAIT_S if waits else 2.0)
        blocked = False
        while asyncio.get_running_loop().time() < deadline and not delivery.done():
            if await _blocked_by(pg_async_engine, pid):
                blocked = True
                break
            await asyncio.sleep(0.05)
        if waits:
            assert blocked and not delivery.done(), 'the delivery did not wait for the list lock'
        else:
            await asyncio.wait_for(delivery, LOCK_WAIT_S)
            assert not blocked, 'the delivery waited for the other list'
        await transaction.rollback()
    finally:
        await holder.close()
    answer, _ = await asyncio.wait_for(delivery, LOCK_WAIT_S)
    assert answer.attempt_count == 1


# --------------------------------------------------------------------------------------------- P17f helpers


async def _runner_attempt(record, instance: str, reader: StubReader, *, attempt_no: int) -> AttemptDelivery:
    """The REAL runner's report for that attempt number and instance, as the executor assembles it; not delivered."""
    sent = submit_request_of(record)
    work = AssetListWork(request=sent.asset_list, priority=RequestPriority.BACKFILL)
    scheduled = ScheduledRequest(request_id=sent.request_id, quota_scope=QUOTA_SCOPE, payload=work)
    result = await AssetListAttemptRunner(reader)(scheduled, attempt_no)
    return AttemptDelivery(
        header=DeliveryHeader(
            request_id=sent.request_id,
            instance_id=instance,
            attempt_no=attempt_no,
            quota_key=result.quota_key,
            feed=result.feed,
            vendor_asset_id=result.vendor_asset_id,
        ),
        parts=result.parts,
        trailer=result.trailer,
    )


async def _snapshot_events(engine: AsyncEngine, request_id: uuid.UUID) -> list[tuple]:
    """(kind, vendor asset id, previous snapshot id) of every event the request's snapshots derived."""
    snapshots = sa.select(SNAPSHOTS.c.id).where(SNAPSHOTS.c.request_id == request_id)
    rows = await _all(engine, sa.select(EVENTS).where(EVENTS.c.snapshot_id.in_(snapshots)))
    return sorted((AssetListEventKind(row.kind), row.vendor_asset_id, row.previous_snapshot_id) for row in rows)


def _mint(run_identity, created: Created, name: str) -> str:
    minted = f'system-test-{run_identity.run_id}-{name}-{uuid.uuid4().hex[:6]}'
    created.instances.append(minted)
    return minted


# --------------------------------------------------------------------------------------------- AL5 a late snapshot


@pytest.mark.parametrize('behind', [timedelta(days=1), timedelta(0)], ids=['earlier', 'equal'])
async def test_al5_a_superseded_instances_late_report_is_stored_raw_derives_nothing_and_warns_once(
    intake, pg_async_engine, run_identity, created, caplog, behind
):
    caplog.set_level(logging.WARNING)
    old, new = _mint(run_identity, created, 'al5-old'), _mint(run_identity, created, 'al5-new')
    await intake.register_instance(RegisterInstanceRequest(instance_id=old, started_at=DAY_0))
    base = await _stored_request(pg_async_engine, created, run_identity, ACTIVE)
    await _deliver_through_the_runner(
        intake, pg_async_engine, base, old, StubReader([asset('pg-a', 'ZZPGA'), asset('pg-b', 'ZZPGB')], DAY_0)
    )
    record = await _stored_request(pg_async_engine, created, run_identity, ACTIVE)
    async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
        assert await request_crud.mark_queued(db, record.id, old) is not None
    await intake.register_instance(RegisterInstanceRequest(instance_id=new, started_at=DAY_0))
    # The watcher's recovered re-submission, answered by the new instance: QUEUED under it.
    async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
        assert await request_crud.mark_queued(db, record.id, new) is not None
    newest = DAY_0 + timedelta(days=2)
    recovered = await _runner_attempt(
        record, new, StubReader([asset('pg-a', 'ZZPGA'), asset('pg-c', 'ZZPGC')], newest), attempt_no=2
    )
    await intake.deliver(recovered)
    caplog.clear()
    stale = await _runner_attempt(record, old, StubReader([asset('pg-s', 'ZZPGSECRET')], newest - behind), attempt_no=1)

    answer = await intake.deliver(stale)

    assert answer.verdict is DeliveryVerdict.FINAL
    stored = {
        row.attempt_no: row
        for row in await _all(pg_async_engine, sa.select(SNAPSHOTS).where(SNAPSHOTS.c.request_id == record.id))
    }
    assert (stored[1].row_count, stored[1].as_of) == (1, newest - behind)
    (row,) = await _all(pg_async_engine, sa.select(ROWS).where(ROWS.c.snapshot_id == stored[1].id))
    assert (row.vendor_asset_id, row.symbol) == ('pg-s', 'ZZPGSECRET')
    base_id = (await _one(pg_async_engine, sa.select(SNAPSHOTS).where(SNAPSHOTS.c.request_id == base.id))).id
    assert await _snapshot_events(pg_async_engine, record.id) == [
        (AssetListEventKind.APPEARED, 'pg-c', base_id),
        (AssetListEventKind.VANISHED, 'pg-b', base_id),
    ], 'only the recovered attempt derived events'
    assert await _all(pg_async_engine, sa.select(EVENTS).where(EVENTS.c.snapshot_id == stored[1].id)) == []
    (warning,) = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and 'is not newer than the newest snapshot of its list' in r.getMessage()
    ]
    message = warning.getMessage()
    assert str(record.id) in message
    assert f'as_of {(newest - behind).isoformat()}' in message and f'newest as_of {newest.isoformat()}' in message
    assert 'ZZPG' not in message and 'pg-s' not in message


# --------------------------------------------------------------------------------------------- AL6 a move, both orders


@pytest.mark.parametrize('first_arrival', [ACTIVE, INACTIVE], ids=['active-first', 'inactive-first'])
async def test_al6_a_move_between_lists_is_a_vanished_plus_a_status_flip_in_either_arrival_order(
    intake, pg_async_engine, run_identity, created, instance_id, first_arrival
):
    await intake.register_instance(RegisterInstanceRequest(instance_id=instance_id, started_at=DAY_0))

    async def deliver(status: AssetListStatus, assets: list[Asset], as_of: datetime) -> uuid.UUID:
        record = await _stored_request(pg_async_engine, created, run_identity, status)
        await _deliver_through_the_runner(intake, pg_async_engine, record, instance_id, StubReader(assets, as_of))
        return record.id

    active_1 = await deliver(ACTIVE, [asset('pg-p', 'ZZPGP'), asset('pg-q', 'ZZPGQ')], DAY_0)
    await deliver(INACTIVE, [asset('pg-i', 'ZZPGI', False)], DAY_0 + timedelta(hours=1))
    after_the_move = {
        ACTIVE: [asset('pg-p', 'ZZPGP')],
        INACTIVE: [asset('pg-i', 'ZZPGI', False), asset('pg-q', 'ZZPGQ', False)],
    }
    second = INACTIVE if first_arrival is ACTIVE else ACTIVE
    arrived = {first_arrival: await deliver(first_arrival, after_the_move[first_arrival], DAY_0 + timedelta(days=1))}
    arrived[second] = await deliver(second, after_the_move[second], DAY_0 + timedelta(days=1, hours=1))

    base_id = (await _one(pg_async_engine, sa.select(SNAPSHOTS).where(SNAPSHOTS.c.request_id == active_1))).id
    assert await _snapshot_events(pg_async_engine, arrived[ACTIVE]) == [(AssetListEventKind.VANISHED, 'pg-q', base_id)]
    assert await _snapshot_events(pg_async_engine, arrived[INACTIVE]) == [
        (AssetListEventKind.STATUS_FLIP, 'pg-q', base_id)
    ]


# --------------------------------------------------------------------------------------------- AL7 the watcher


class FakeSubmit:
    """The IngestSubmitClient interface, answering accepted from one instance id; records every request."""

    def __init__(self, instance: str) -> None:
        self.instance_id = instance
        self.sent: list[SubmitRequest] = []

    async def submit(self, request: SubmitRequest) -> SubmitResponse:
        self.sent.append(request)
        accepted = SubmitAck(accepted=SubmitAccepted(quota_key=QUOTA_SCOPE))
        return SubmitResponse(ack=accepted, instance_id=self.instance_id)

    async def instance(self) -> InstanceResponse:
        return InstanceResponse(instance_id=self.instance_id, started_at=DAY_0)


@pytest.fixture
def scoped_watch(pg_async_engine: AsyncEngine, created: Created, monkeypatch: pytest.MonkeyPatch) -> None:
    """The watcher and the submit path on this Postgres, their two reads scoped to this test's requests."""

    @asynccontextmanager
    async def own_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(pg_async_engine, expire_on_commit=False) as session:
            yield session

    for module in (data_action_request, instance_watch):
        monkeypatch.setattr(module, 'own_session', own_session)
    real_pending, real_abandoned = instance_watch.list_pending_before, instance_watch.list_abandoned

    async def mine_pending(db, cutoff):
        return [record for record in await real_pending(db, cutoff) if record.id in created.requests]

    async def mine_abandoned(db, cutoff):
        return [request_id for request_id in await real_abandoned(db, cutoff) if request_id in created.requests]

    monkeypatch.setattr(instance_watch, 'list_pending_before', mine_pending)
    monkeypatch.setattr(instance_watch, 'list_abandoned', mine_abandoned)


async def _open(engine: AsyncEngine, status: AssetListStatus) -> bool:
    async with AsyncSession(engine) as db:
        return await request_crud.open_asset_list_request_exists(db, SOURCE, status)


async def _request_row(engine: AsyncEngine, request_id: uuid.UUID) -> sa.Row:
    return await _one(engine, sa.select(REQUESTS).where(REQUESTS.c.id == request_id))


@pytest.mark.parametrize('status', list(AssetListStatus), ids=lambda status: status.value)
async def test_al7_a_pending_asset_list_past_the_resend_delay_is_re_sent_with_its_list_and_then_closes(
    intake, scoped_watch, pg_async_engine, run_identity, created, status
):
    live = _mint(run_identity, created, 'al7-live')
    await intake.register_instance(RegisterInstanceRequest(instance_id=live, started_at=DAY_0))
    record = await _stored_request(pg_async_engine, created, run_identity, status)
    async with AsyncSession(pg_async_engine) as db:
        await db.execute(
            sa.update(REQUESTS)
            .where(REQUESTS.c.id == record.id)
            .values(state=IngestRequestState.PENDING, instance_id=None, created_at=DAY_0)
        )
        await db.commit()
    client = FakeSubmit(live)

    early = await InstanceWatcher(client, clock=lambda: DAY_0 + timedelta(seconds=29)).run_pass()
    assert early == WatchResult() and client.sent == []
    result = await InstanceWatcher(client, clock=lambda: DAY_0 + timedelta(seconds=31)).run_pass()

    assert result == WatchResult(resent=1)
    (sent,) = client.sent
    assert (sent.request_id, sent.recovered, sent.attempt_count) == (str(record.id), False, 0)
    assert sent.asset_list.status is status
    row = await _request_row(pg_async_engine, record.id)
    assert (row.state, row.instance_id) == (IngestRequestState.QUEUED, live)
    assert await _open(pg_async_engine, status) is True

    await intake.deliver(
        await _runner_attempt(record, live, StubReader([asset('pg-w', 'ZZPGW', None)], DAY_0), attempt_no=1)
    )

    assert (await _request_row(pg_async_engine, record.id)).state is IngestRequestState.SUCCEEDED
    assert await _open(pg_async_engine, status) is False


async def test_al7_an_abandoned_asset_list_is_re_submitted_recovered_with_its_count_after_the_delay_and_closes(
    intake, scoped_watch, pg_async_engine, run_identity, created
):
    old, new = _mint(run_identity, created, 'al7-old'), _mint(run_identity, created, 'al7-new')
    await intake.register_instance(RegisterInstanceRequest(instance_id=old, started_at=DAY_0))
    record = await _stored_request(pg_async_engine, created, run_identity, INACTIVE)
    async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
        assert await request_crud.mark_queued(db, record.id, old) is not None
    # The old instance's first attempt was counted: the durable count is 1.
    async with AsyncSession(pg_async_engine) as db:
        await db.execute(sa.update(REQUESTS).where(REQUESTS.c.id == record.id).values(attempts=1))
        await db.commit()
    await intake.register_instance(RegisterInstanceRequest(instance_id=new, started_at=DAY_0))
    superseded_at = (
        await _one(pg_async_engine, sa.select(INSTANCES.c.superseded_at).where(INSTANCES.c.instance_id == old))
    ).superseded_at
    assert superseded_at is not None
    client = FakeSubmit(new)
    due = superseded_at + resubmit_delay()

    early = await InstanceWatcher(client, clock=lambda: due - timedelta(seconds=1)).run_pass()
    assert early == WatchResult() and client.sent == []
    result = await InstanceWatcher(client, clock=lambda: due + timedelta(seconds=1)).run_pass()

    assert result == WatchResult(resubmitted=1)
    (sent,) = client.sent
    assert (sent.request_id, sent.recovered, sent.attempt_count) == (str(record.id), True, 1)
    assert sent.asset_list.status is INACTIVE
    row = await _request_row(pg_async_engine, record.id)
    assert (row.state, row.instance_id, row.attempts) == (IngestRequestState.QUEUED, new, 1)
    assert await _open(pg_async_engine, INACTIVE) is True

    await intake.deliver(
        await _runner_attempt(record, new, StubReader([asset('pg-r', 'ZZPGR', False)], DAY_0), attempt_no=2)
    )

    row = await _request_row(pg_async_engine, record.id)
    assert (row.state, row.attempts) == (IngestRequestState.SUCCEEDED, 2)
    assert await _open(pg_async_engine, INACTIVE) is False
