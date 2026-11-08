"""The two re-submission races of P35 (tj-7zjov1.28), forced on the real, migrated Postgres.

WRITTEN, NOT YET RUN where it was written: it needs the test stack (make test-system, or agent-stack
run_system_tests after test_up) migrated to the head. The architect does not close P35 on these until they have run.

THE DESIGN: architect ruling C and note F on tj-7zjov1.28, ADR tj-26jt9w addendum 9 (d), the store CLAUDE.md watcher
line. Both are about the idempotency key (request_id, attempt_no): a new instance numbers its attempts from the count
it is handed.

  R1  THE RE-SUBMIT DELAY RACE (ruling C). An old, superseded instance's last report is still in flight when the
      watcher re-submits its request. The watcher waits DEFAULT_DELIVERY_DEADLINE_S plus the margin from the
      supersession and reads the count UNDER THE REQUEST LOCK, so the late report has committed (and is in the count
      handed to the new instance) or is refused for ever; the new instance then never reuses its attempt number.
      Forced: the old instance's delivery is paused inside its transaction, holding the request lock, while the
      watcher's pass runs; the pass is seen blocked behind it; the delivery commits; the pass hands the new instance
      the count INCLUDING it; the new instance's next attempt is written, not refused. Before the delay, nothing is
      re-submitted at all.
  R2  QUEUED UNDER AN INSTANCE SUPERSEDED JUST BEFORE (note F). The instance sighting marks only the rows QUEUED in
      its own snapshot, and the intake reads `superseded` without a lock. Forced: a PENDING request's transient
      report from instance A reads A as live and pauses; B is first seen meanwhile (the request is PENDING, so the
      sighting marks nothing); the report commits QUEUED under A, a superseded instance. mark_queued refuses A (the
      submit path's guard), and the watcher treats the row exactly like NOT_DELIVERED: not before the delay, and
      after it re-submitted recovered with the durable count. And the submit path's own window: A's Submit answer
      lands after B was first seen, so mark_queued leaves the request PENDING for the watcher to re-send.

HOW. DeliveryIntake, the submit path and InstanceWatcher run in this process on the test stack's Postgres over
asyncpg, every own_session pointed at it; ingest is FakeSubmit, a double of the IngestSubmitClient interface. A first
sighting of an instance is GLOBAL by design (it supersedes every older instance and marks their QUEUED requests): safe
on the disposable stack this suite requires, as in test_delivery_intake_pg.py. The watcher's two reads are wrapped to
return only this test's requests, so a pass never re-sends a row another test or run left behind.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.rpc.clients.store_delivery import DEFAULT_DELIVERY_DEADLINE_S
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestKind, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRun
from data.store.app.ingest import data_action_request, delivery_intake, instance_watch
from data.store.app.ingest.data_action_request import submit_record
from data.store.app.ingest.delivery_intake import DeliveryIntake
from data.store.app.ingest.instance_watch import InstanceWatcher, WatchResult, resubmit_delay
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    DeliverAttemptResponse,
    DeliveryErrorClass,
    DeliveryHeader,
    DeliveryOutcome,
    DeliveryTrailer,
    DeliveryVerdict,
    RegisterInstanceRequest,
    VendorCallStats,
)
from schemas.data_ingest.fetch_dataset import Bar, BarPage, ServedRange
from schemas.data_ingest.submit import InstanceResponse, SubmitAccepted, SubmitAck, SubmitRequest, SubmitResponse


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

RUNS = IngestRun.__table__
REQUESTS = IngestRequest.__table__
INSTANCES = IngestInstance.__table__
START = datetime(2026, 2, 2, tzinfo=UTC)
BAR_START = datetime(2026, 2, 2, 5, tzinfo=UTC)
FIRST_CALL = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
QUOTA_KEY = 'system-test-quota-key'
FINAL, CONTINUE = DeliveryVerdict.FINAL, DeliveryVerdict.CONTINUE
LOCK_WAIT_S = 10.0


# --------------------------------------------------------------------------------------------- reports and ingest
# Built here: the test_client container mounts tests/system and the store's app, never the store's unit tests.


def report(
    request_id: uuid.UUID,
    outcome: DeliveryOutcome = DeliveryOutcome.SUCCEEDED,
    *,
    instance_id: str,
    attempt_no: int = 1,
) -> AttemptDelivery:
    header = DeliveryHeader(
        request_id=str(request_id),
        instance_id=instance_id,
        attempt_no=attempt_no,
        quota_key=QUOTA_KEY,
        feed=Feed.IEX,
        vendor_asset_id='vendor-asset-1',
    )
    stats = VendorCallStats(
        calls=1, pages=1, throttled_429=0, retries=0, first_call_at=FIRST_CALL, last_call_at=FIRST_CALL
    )
    if outcome is DeliveryOutcome.SUCCEEDED:
        bars = [
            Bar(bar_start=BAR_START, open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0, trade_count=3, feed=Feed.IEX)
        ]
        trailer = DeliveryTrailer(
            outcome=outcome,
            vendor_stats=stats,
            served_range=ServedRange(start=BAR_START, end=BAR_START + timedelta(days=1)),
            as_of=FIRST_CALL,
        )
        return AttemptDelivery(header=header, parts=[BarPage(bars=bars)], trailer=trailer)
    trailer = DeliveryTrailer(
        outcome=DeliveryOutcome.FAILED,
        error_class=DeliveryErrorClass.TRANSIENT,
        vendor_stats=stats,
        as_of=FIRST_CALL,
        detail='the vendor answered 503',
    )
    return AttemptDelivery(header=header, parts=[], trailer=trailer)


class FakeSubmit:
    """The IngestSubmitClient interface, answering accepted from one instance id; records every request."""

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.sent: list[SubmitRequest] = []

    async def submit(self, request: SubmitRequest) -> SubmitResponse:
        self.sent.append(request)
        accepted = SubmitAck(accepted=SubmitAccepted(feed=Feed.IEX, quota_key=QUOTA_KEY))
        return SubmitResponse(ack=accepted, instance_id=self.instance_id)

    async def instance(self) -> InstanceResponse:
        return InstanceResponse(instance_id=self.instance_id, started_at=START)


# --------------------------------------------------------------------------------------------- fixtures


class Created:
    def __init__(self) -> None:
        self.requests: list[uuid.UUID] = []
        self.instances: list[str] = []


@pytest.fixture
def created(pg_engine: Engine) -> Iterator[Created]:
    ids = Created()
    yield ids
    with pg_engine.begin() as conn:
        if ids.requests:
            conn.execute(sa.delete(RUNS).where(RUNS.c.request_id.in_(ids.requests)))
            conn.execute(sa.delete(REQUESTS).where(REQUESTS.c.id.in_(ids.requests)))
        if ids.instances:
            conn.execute(sa.delete(INSTANCES).where(INSTANCES.c.instance_id.in_(ids.instances)))


@pytest.fixture
def instance_id(run_identity, created: Created) -> Callable[[str], str]:
    def _mint(name: str) -> str:
        minted = f'system-test-{run_identity.run_id}-{name}-{uuid.uuid4().hex[:6]}'
        created.instances.append(minted)
        return minted

    return _mint


class Store:
    """The intake, the submit path and the watcher on the test stack's Postgres."""

    def __init__(self, engine: AsyncEngine, created: Created, monkeypatch: pytest.MonkeyPatch) -> None:
        self.engine = engine

        @asynccontextmanager
        async def own_session() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session

        for module in (delivery_intake, data_action_request, instance_watch):
            monkeypatch.setattr(module, 'own_session', own_session)
        monkeypatch.setattr(delivery_intake, 'publish', lambda event: None)

        # The watcher's reads, scoped to this test's requests (the reads themselves are the real ones).
        real_pending, real_abandoned = instance_watch.list_pending_before, instance_watch.list_abandoned

        async def mine_pending(db, cutoff):
            return [record for record in await real_pending(db, cutoff) if record.id in created.requests]

        async def mine_abandoned(db, cutoff):
            return [request_id for request_id in await real_abandoned(db, cutoff) if request_id in created.requests]

        monkeypatch.setattr(instance_watch, 'list_pending_before', mine_pending)
        monkeypatch.setattr(instance_watch, 'list_abandoned', mine_abandoned)
        self.intake = DeliveryIntake(chunk_size=10)
        self.session = own_session

    async def register(self, instance: str) -> None:
        await self.intake.register_instance(RegisterInstanceRequest(instance_id=instance, started_at=START))

    async def request_row(self, request_id: uuid.UUID) -> sa.Row:
        async with self.engine.connect() as conn:
            return (await conn.execute(sa.select(REQUESTS).where(REQUESTS.c.id == request_id))).one()

    async def runs(self, request_id: uuid.UUID) -> list[sa.Row]:
        async with self.engine.connect() as conn:
            statement = sa.select(RUNS).where(RUNS.c.request_id == request_id).order_by(RUNS.c.attempt_no)
            return list((await conn.execute(statement)).all())

    async def superseded_at(self, instance: str) -> datetime:
        async with self.engine.connect() as conn:
            at = (
                await conn.execute(sa.select(INSTANCES.c.superseded_at).where(INSTANCES.c.instance_id == instance))
            ).scalar_one()
        assert at is not None, f'{instance} is not superseded'
        return at

    async def wait_until_blocked_by(self, blocker_pid: int) -> None:
        deadline = asyncio.get_running_loop().time() + LOCK_WAIT_S
        query = sa.text(
            'SELECT count(*) FROM pg_stat_activity WHERE CAST(:pid AS integer) = ANY(pg_blocking_pids(pid))'
        )
        while True:
            async with self.engine.connect() as conn:
                if (await conn.execute(query, {'pid': blocker_pid})).scalar_one() > 0:
                    return
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail(f'no session blocked behind backend {blocker_pid} within {LOCK_WAIT_S}s')
            await asyncio.sleep(0.05)


@pytest_asyncio.fixture
async def store(pg_async_engine: AsyncEngine, created: Created, monkeypatch: pytest.MonkeyPatch) -> Store:
    return Store(pg_async_engine, created, monkeypatch)


@pytest.fixture
def bars_entry(insert_entry, own_symbol: str) -> sa.Row:
    return insert_entry(asset_symbol=own_symbol, granularity=Granularity.ONE_DAY, feed=Feed.IEX)


async def _request(
    store: Store, created: Created, run_identity, entry: sa.Row, *, state: IngestRequestState, holder: str | None
) -> uuid.UUID:
    request_id = uuid.uuid4()
    created.requests.append(request_id)
    async with AsyncSession(store.engine, expire_on_commit=False) as db:
        await request_crud.create_request(
            db,
            request_crud.NewIngestRequest(
                id=request_id,
                kind=IngestRequestKind.BARS,
                source=DataSource.ALPACA_API,
                client_label=run_identity.owner,
                dataset_id=entry.id,
                asset_symbol=entry.asset_symbol,
                asset_type=AssetType.STOCK,
                data_types=(DataType.MARKET_ACTIVITY.value,),
                granularity=Granularity.ONE_DAY,
                update_type=UpdateType.DAILY,
                feed=Feed.IEX,
                requested_start=START,
            ),
        )
        await db.execute(sa.update(REQUESTS).where(REQUESTS.c.id == request_id).values(state=state, instance_id=holder))
        await db.commit()
    return request_id


def _watcher(client: FakeSubmit, at: datetime) -> InstanceWatcher:
    return InstanceWatcher(client, clock=lambda: at)


# --------------------------------------------------------------------------------------------- R1


async def test_r1_the_watcher_does_not_re_submit_before_the_delivery_deadline_plus_the_margin(
    store, created, run_identity, bars_entry, instance_id
):
    old, new = instance_id('r1a-old'), instance_id('r1a-new')
    await store.register(old)
    request_id = await _request(store, created, run_identity, bars_entry, state=IngestRequestState.QUEUED, holder=old)
    await store.register(new)
    superseded = await store.superseded_at(old)
    assert resubmit_delay() == timedelta(seconds=DEFAULT_DELIVERY_DEADLINE_S + 15)
    client = FakeSubmit(new)

    result = await _watcher(client, superseded + resubmit_delay() - timedelta(seconds=1)).run_pass()

    assert result == WatchResult() and client.sent == []
    assert (await store.request_row(request_id)).state is IngestRequestState.NOT_DELIVERED


async def test_r1_an_old_instances_late_report_lands_in_the_count_the_new_instance_is_handed(
    store, created, run_identity, bars_entry, instance_id, monkeypatch
):
    old, new = instance_id('r1-old'), instance_id('r1-new')
    await store.register(old)
    request_id = await _request(store, created, run_identity, bars_entry, state=IngestRequestState.QUEUED, holder=old)
    await store.register(new)
    assert (await store.request_row(request_id)).state is IngestRequestState.NOT_DELIVERED
    superseded = await store.superseded_at(old)

    # The old instance's late report: paused INSIDE its delivery transaction, holding the request lock, before the
    # count moves.
    real_apply = delivery_intake.apply_outcome
    paused, release = asyncio.Event(), asyncio.Event()
    holder_pid: list[int] = []

    async def apply_once_paused(db, rid, outcome):
        if not paused.is_set():
            holder_pid.append((await db.execute(sa.text('SELECT pg_backend_pid()'))).scalar_one())
            paused.set()
            await asyncio.wait_for(release.wait(), LOCK_WAIT_S)
        return await real_apply(db, rid, outcome)

    monkeypatch.setattr(delivery_intake, 'apply_outcome', apply_once_paused)
    late = asyncio.create_task(store.intake.deliver(report(request_id, DeliveryOutcome.FAILED, instance_id=old)))
    await asyncio.wait_for(paused.wait(), LOCK_WAIT_S)

    # The watcher's pass, after the delay: it claims the request under its lock, so it waits behind the report.
    client = FakeSubmit(new)
    passing = asyncio.create_task(_watcher(client, superseded + resubmit_delay() + timedelta(seconds=1)).run_pass())
    await store.wait_until_blocked_by(holder_pid[0])
    assert client.sent == [], 'nothing is re-submitted while the old report is in flight'
    release.set()

    assert await asyncio.wait_for(late, LOCK_WAIT_S) == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert await asyncio.wait_for(passing, LOCK_WAIT_S) == WatchResult(resubmitted=1)

    (sent,) = client.sent
    assert (sent.request_id, sent.recovered, sent.attempt_count) == (str(request_id), True, 1), (
        'the count read under the lock includes the late report'
    )
    row = await store.request_row(request_id)
    assert (row.state, row.instance_id, row.attempts) == (IngestRequestState.QUEUED, new, 1)

    # The new instance numbers from the count it was handed: attempt 2 is written, never refused as a collision.
    answer = await store.intake.deliver(report(request_id, attempt_no=sent.attempt_count + 1, instance_id=new))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=2)
    assert [(run.attempt_no, run.instance_id) for run in await store.runs(request_id)] == [(1, old), (2, new)]
    assert (await store.request_row(request_id)).state is IngestRequestState.SUCCEEDED


# --------------------------------------------------------------------------------------------- R2


async def test_r2_a_report_read_live_just_before_a_supersession_strands_it_queued_and_the_watcher_recovers_it(
    store, created, run_identity, bars_entry, instance_id, monkeypatch
):
    a, b = instance_id('r2-a'), instance_id('r2-b')
    await store.register(a)
    request_id = await _request(store, created, run_identity, bars_entry, state=IngestRequestState.PENDING, holder=None)

    # A's transient report reads `superseded` (False, A is live), then pauses holding the request lock.
    real_superseded = delivery_intake.instance_is_superseded
    read, release = asyncio.Event(), asyncio.Event()

    async def read_then_wait(db, instance):
        result = await real_superseded(db, instance)
        if not read.is_set():
            read.set()
            await asyncio.wait_for(release.wait(), LOCK_WAIT_S)
        return result

    monkeypatch.setattr(delivery_intake, 'instance_is_superseded', read_then_wait)
    delivering = asyncio.create_task(store.intake.deliver(report(request_id, DeliveryOutcome.FAILED, instance_id=a)))
    await asyncio.wait_for(read.wait(), LOCK_WAIT_S)

    # B is first seen now. The request is PENDING in the sighting's snapshot, so it is not marked, and the sighting
    # does not wait for the delivery's lock.
    await asyncio.wait_for(store.register(b), LOCK_WAIT_S)
    release.set()

    assert await asyncio.wait_for(delivering, LOCK_WAIT_S) == DeliverAttemptResponse(verdict=CONTINUE, attempt_count=1)
    row = await store.request_row(request_id)
    superseded = await store.superseded_at(a)
    assert (row.state, row.instance_id, row.attempts) == (IngestRequestState.QUEUED, a, 1), (
        'the stranded state note F names: QUEUED under an instance that is already superseded'
    )

    # The submit path never writes QUEUED under the superseded instance.
    async with AsyncSession(store.engine, expire_on_commit=False) as db:
        assert await request_crud.mark_queued(db, request_id, a) is None

    # The watcher treats it exactly like NOT_DELIVERED: not before the delay ...
    client = FakeSubmit(b)
    early = await _watcher(client, superseded + resubmit_delay() - timedelta(seconds=1)).run_pass()
    assert early == WatchResult() and client.sent == []

    # ... and after it, re-submitted recovered with the durable count, then QUEUED under the live instance.
    late = await _watcher(client, superseded + resubmit_delay() + timedelta(seconds=1)).run_pass()

    assert late == WatchResult(resubmitted=1)
    (sent,) = client.sent
    assert (sent.request_id, sent.recovered, sent.attempt_count) == (str(request_id), True, 1)
    row = await store.request_row(request_id)
    assert (row.state, row.instance_id, row.attempts) == (IngestRequestState.QUEUED, b, 1)


async def test_r2_a_submit_answer_from_an_instance_superseded_before_it_is_recorded_leaves_the_request_pending(
    store, created, run_identity, bars_entry, instance_id, monkeypatch
):
    """The submit path's window: A answered the Submit, B was first seen before the answer was recorded."""
    a, b = instance_id('r2s-a'), instance_id('r2s-b')
    request_id = await _request(store, created, run_identity, bars_entry, state=IngestRequestState.PENDING, holder=None)
    real_mark = data_action_request.mark_queued

    async def b_first_seen_then_mark(db, rid, instance):
        await store.register(b)
        return await real_mark(db, rid, instance)

    monkeypatch.setattr(data_action_request, 'mark_queued', b_first_seen_then_mark)
    async with store.session() as db:
        record = await request_crud.get_request(db, request_id)

    await submit_record(record, FakeSubmit(a))

    row = await store.request_row(request_id)
    assert (row.state, row.instance_id) == (IngestRequestState.PENDING, None), 'never QUEUED under superseded A'

    # The watcher re-sends it as it is (PENDING: not recovered) once it is older than the PENDING age.
    monkeypatch.setattr(data_action_request, 'mark_queued', real_mark)
    client = FakeSubmit(b)
    result = await _watcher(client, row.created_at + timedelta(seconds=31)).run_pass()

    assert result == WatchResult(resent=1)
    (sent,) = client.sent
    assert (sent.request_id, sent.recovered) == (str(request_id), False)
    row = await store.request_row(request_id)
    assert (row.state, row.instance_id) == (IngestRequestState.QUEUED, b)
