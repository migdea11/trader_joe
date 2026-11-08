"""The delivery intake on the real, migrated Postgres: PG1 to PG8 of tj-7zjov1.27 (P34), the essential cases.

WRITTEN, NOT YET RUN: there was no Postgres where it was written (the run needs the dev take-down the owner
approves). It runs with the rest of the suite (make test-system, or agent-stack run_system_tests after test_up) on a
stack migrated to the head (f2b7d4a91c36 or later). The architect does not close P34 on these until they have run.

THE DESIGN: the architect's validator case list on tj-7zjov1.27 (20:17 UTC, 2026-10-25), ADR tj-26jt9w addenda (4)
(5) (6) (9), and the store CLAUDE.md delivery section. WHAT THIS PROVES that server/data/store/tests cannot, because
SQLite has no partial index, no FOR UPDATE, one writer and no Postgres enum or ARRAY types:
  PG1 insert_attempt's ON CONFLICT names the partial unique index WITH its predicate and Postgres accepts it as the
      arbiter: a second insert of (request_id, attempt_no) returns None, raising nothing;
  PG2 a SUCCEEDED bars delivery commits everything in one transaction, through the real bar upsert, and the enums
      and the integer update_type round-trip;
  PG3 the same delivery again changes nothing: no second row, the bars and their updated_at untouched, the spend;
  PG4 two identical deliveries racing on two sessions leave exactly one attempt row and a count of 1 (the request
      lock FOR UPDATE serialises them; the second is answered as a duplicate);
  PG5 a refusal after page 1 was upserted (page 2 holds one timestamp twice; or names another tape) rolls back
      everything: no bars, no attempt row, count, spend and state unchanged;
  PG6 EXPIRED and EXHAUSTED fail the request with their own cause, no attempt row, the count and spend unchanged,
      FINAL; a repeat is FINAL and writes nothing;
  PG7 C1, as P35 (tj-7zjov1.28) changed it: a dataset_id naming no entry is DROPPED (nothing stored, the attempt
      row EMPTY, the request EMPTY with cause DISCARDED, FINAL; a refusal would be redelivered for ever), and an
      entry deleted by a concurrent transaction that holds its row lock is seen as gone once that transaction
      commits, and dropped the same way;
  PG8 RegisterInstance A then B supersedes A and marks its QUEUED request NOT_DELIVERED; A's later transient
      FAILED is still written (row, count, spend), leaves the state and is answered FINAL; A's SUCCEEDED then
      finishes the request.
  PG9 P49 (tj-7zjov1.143): the bars header's vendor_asset_id is stored as received in ingest_run.vendor_asset_id
      (PG2 reads it back, PG3 shows a repeat leaves it), and an empty header id is stored NULL, never ''.
  PG10 P35 ruling A and note H, through the REAL entry upsert (ON CONFLICT ON CONSTRAINT, which SQLite cannot run):
      a request with no dataset_id creates its entry on the first delivered attempt from the request row (expiry,
      expiry type, collect) and the header feed; a repeat of the identity matches it and a collect flip publishes
      RETIRED; a NULL expiry type is refused with nothing written, never BULK.
  PG11 a delivery for a Stopped subscription (collect false) is dropped like a gone one.
  PG12 P18 (tj-7zjov1.31): the corporate-actions arm on the real table (bytea, JSONB, the unique index as the dedupe
      arbiter, a revision as a new row), the attempt row's NULL labels and NULL vendor asset id, and the rollback.

HOW. DeliveryIntake and crud/ingest_delivery.py are driven directly on the test stack's Postgres over asyncpg
(pg_async_engine), with the intake's own_session pointed at that engine and its publish journalled. The ingest_request
rows and the dataset entries are inserted by the test (the entries through insert_entry, whose cleanup cascades to
their bars). A first sighting of an instance is GLOBAL by design (it supersedes every older instance and marks
their QUEUED requests NOT_DELIVERED): safe on the disposable stack this suite requires, as in
test_request_record_schema.py; the assertions read only this run's rows, and each test mints its own instance ids
and registers them before creating the requests that name them.
"""

import asyncio
import contextlib
import hashlib
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
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import InvalidRequestError, Reason
from data.store.app.database.crud import ingest_delivery
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.models.corporate_action import CorporateAction
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import (
    IngestRequest,
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.events import DatasetChange, DatasetChanged
from data.store.app.ingest import delivery_intake
from data.store.app.ingest.delivery_intake import DeliveryIntake
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    CorporateActionRecord,
    CorporateActionRecords,
    DeliverAttemptResponse,
    DeliveryErrorClass,
    DeliveryHeader,
    DeliveryOutcome,
    DeliveryTrailer,
    DeliveryVerdict,
    RegisterInstanceRequest,
    VendorCallStats,
    VendorReported,
)
from schemas.data_ingest.fetch_dataset import Bar, BarPage, ServedRange


# A first sighting supersedes the stack's live ingest too; the fixture makes it live again (tj-7zjov1.233).
pytestmark = [pytest.mark.data_store, pytest.mark.asyncio, pytest.mark.usefixtures('live_instances_kept')]

RUNS = IngestRun.__table__
REQUESTS = IngestRequest.__table__
INSTANCES = IngestInstance.__table__
BARS = StockMarketActivity.__table__
ACTIONS = CorporateAction.__table__
ENTRIES_NAME = 'store_dataset_entry'
START = datetime(2026, 2, 2, tzinfo=UTC)
FINAL, CONTINUE = DeliveryVerdict.FINAL, DeliveryVerdict.CONTINUE
# How long a test waits for a racing transaction to be seen blocked on a row lock.
LOCK_WAIT_S = 10.0
# How long a delivery waits for the other at the forced interleaving point in PG4.
RACE_WINDOW_S = 1.0
QUOTA_KEY = 'system-test-quota-key'
VENDOR_ASSET_ID = 'vendor-asset-1'
FIRST_CALL = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
AS_OF = FIRST_CALL + timedelta(seconds=4)
BAR_START = datetime(2026, 2, 2, 5, tzinfo=UTC)


# --------------------------------------------------------------------------------------------- reports
# Built here, not imported from server/data/store/tests/delivery_reports.py: the test_client container mounts
# tests/system and the store's app, never the store's unit-test directory (docker-compose.test-client.yaml).


def stats() -> VendorCallStats:
    return VendorCallStats(
        calls=3,
        pages=2,
        throttled_429=1,
        retries=1,
        first_call_at=FIRST_CALL,
        last_call_at=FIRST_CALL + timedelta(seconds=3),
        vendor_reported=VendorReported(limit=200, remaining=150, reset_at=AS_OF + timedelta(minutes=1)),
    )


def bar(offset_days: int, feed: Feed = Feed.IEX) -> Bar:
    return Bar(
        bar_start=BAR_START + timedelta(days=offset_days),
        open=10.0,
        high=12.0,
        low=9.0,
        close=11.0,
        volume=1000.0,
        trade_count=42,
        feed=feed,
    )


def bar_page(*offsets: int) -> BarPage:
    return BarPage(bars=[bar(offset) for offset in offsets])


def report(
    request_id: uuid.UUID,
    outcome: DeliveryOutcome = DeliveryOutcome.SUCCEEDED,
    *,
    instance_id: str,
    attempt_no: int = 1,
    parts: list | None = None,
    error_class: DeliveryErrorClass = DeliveryErrorClass.TRANSIENT,
    last_attempt: bool = False,
) -> AttemptDelivery:
    """A bars report the intake accepts (rules a to h), for the outcome given."""
    counted = outcome in (DeliveryOutcome.SUCCEEDED, DeliveryOutcome.FAILED)
    header = DeliveryHeader(
        request_id=str(request_id),
        instance_id=instance_id,
        attempt_no=attempt_no,
        quota_key=QUOTA_KEY if counted else '',
        feed=Feed.IEX,
        vendor_asset_id=VENDOR_ASSET_ID if counted else '',
    )
    if outcome is DeliveryOutcome.SUCCEEDED:
        trailer = DeliveryTrailer(
            outcome=outcome,
            vendor_stats=stats(),
            served_range=ServedRange(start=BAR_START, end=BAR_START + timedelta(days=10)),
            as_of=AS_OF,
        )
        parts = [bar_page(0, 1)] if parts is None else parts
    elif outcome is DeliveryOutcome.FAILED:
        trailer = DeliveryTrailer(
            outcome=outcome,
            error_class=error_class,
            vendor_stats=stats(),
            as_of=AS_OF,
            last_attempt=last_attempt,
            detail='the vendor answered 503',
        )
        parts = []
    else:
        trailer = DeliveryTrailer(outcome=outcome)
        parts = []
    return AttemptDelivery(header=header, parts=parts, trailer=trailer)


# --------------------------------------------------------------------------------------------- fixtures


class Created:
    """The ids one test wrote, deleted at its end: nothing this run did not write is deleted."""

    def __init__(self) -> None:
        self.requests: list[uuid.UUID] = []
        self.instances: list[str] = []


@pytest.fixture
def created(pg_engine: Engine) -> Iterator[Created]:
    ids = Created()
    yield ids
    with pg_engine.begin() as conn:
        if ids.requests:
            # corporate_action is append-only for the service; the test removes only the observations it delivered.
            conn.execute(sa.delete(ACTIONS).where(ACTIONS.c.request_id.in_(ids.requests)))
            conn.execute(sa.delete(RUNS).where(RUNS.c.request_id.in_(ids.requests)))
            conn.execute(sa.delete(REQUESTS).where(REQUESTS.c.id.in_(ids.requests)))
        if ids.instances:
            conn.execute(sa.delete(INSTANCES).where(INSTANCES.c.instance_id.in_(ids.instances)))


class Intake:
    """DeliveryIntake on the test stack's Postgres, with its published events journalled."""

    def __init__(self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch):
        self.engine = engine
        self.published: list[object] = []

        @asynccontextmanager
        async def own_session() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session

        monkeypatch.setattr(delivery_intake, 'own_session', own_session)
        monkeypatch.setattr(delivery_intake, 'publish', self.published.append)
        # A small chunk, so a page of bars is written in more than one statement inside the one transaction.
        self.intake = DeliveryIntake(chunk_size=2)

    async def deliver(self, attempt) -> DeliverAttemptResponse:
        return await self.intake.deliver(attempt)

    async def register(self, instance_id: str) -> None:
        await self.intake.register_instance(RegisterInstanceRequest(instance_id=instance_id, started_at=START))


@pytest_asyncio.fixture
async def intake(pg_async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Intake:
    return Intake(pg_async_engine, monkeypatch)


@pytest.fixture
def instance_id(run_identity, created: Created) -> Callable[[str], str]:
    def _mint(name: str) -> str:
        minted = f'system-test-{run_identity.run_id}-{name}-{uuid.uuid4().hex[:6]}'
        created.instances.append(minted)
        return minted

    return _mint


@pytest.fixture
def bars_entry(insert_entry, own_symbol: str) -> sa.Row:
    """The dataset entry a bars request writes under: this test's own symbol, 1day, IEX."""
    return insert_entry(asset_symbol=own_symbol, granularity=Granularity.ONE_DAY, feed=Feed.IEX)


async def _request(
    engine: AsyncEngine,
    created: Created,
    run_identity,
    symbol: str,
    *,
    holder: str,
    dataset_id: uuid.UUID | None,
    state: IngestRequestState = IngestRequestState.QUEUED,
    **values,
) -> uuid.UUID:
    """A bars request queued under `holder`, as the submit path leaves it. `values` override its row fields."""
    request_id = uuid.uuid4()
    created.requests.append(request_id)
    fields = {
        'id': request_id,
        'kind': IngestRequestKind.BARS,
        'source': DataSource.ALPACA_API,
        'client_label': run_identity.owner,
        'dataset_id': dataset_id,
        'asset_symbol': symbol,
        'asset_type': AssetType.STOCK,
        'data_types': (DataType.MARKET_ACTIVITY.value,),
        'granularity': Granularity.ONE_DAY,
        'update_type': UpdateType.DAILY,
        'feed': Feed.IEX,
        'requested_start': START,
        'requested_end': START + timedelta(days=30),
        **values,
    }
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await request_crud.create_request(db, request_crud.NewIngestRequest(**fields))
        await db.execute(sa.update(REQUESTS).where(REQUESTS.c.id == request_id).values(state=state, instance_id=holder))
        await db.commit()
    return request_id


async def _request_row(engine: AsyncEngine, request_id: uuid.UUID) -> sa.Row:
    async with engine.connect() as conn:
        return (await conn.execute(sa.select(REQUESTS).where(REQUESTS.c.id == request_id))).one()


async def _runs(engine: AsyncEngine, request_id: uuid.UUID) -> list[sa.Row]:
    async with engine.connect() as conn:
        return list(
            (
                await conn.execute(sa.select(RUNS).where(RUNS.c.request_id == request_id).order_by(RUNS.c.attempt_no))
            ).all()
        )


async def _bars(engine: AsyncEngine, dataset_id: uuid.UUID) -> list[sa.Row]:
    async with engine.connect() as conn:
        return list(
            (
                await conn.execute(sa.select(BARS).where(BARS.c.dataset_id == dataset_id).order_by(BARS.c.timestamp))
            ).all()
        )


async def _snapshot(engine: AsyncEngine, request_id: uuid.UUID, dataset_id: uuid.UUID | None) -> tuple:
    request = tuple(await _request_row(engine, request_id))
    runs = [tuple(row) for row in await _runs(engine, request_id)]
    bars = [tuple(row) for row in await _bars(engine, dataset_id)] if dataset_id is not None else []
    return request, runs, bars


async def _wait_until_blocked_by(engine: AsyncEngine, blocker_pid: int) -> None:
    """Wait until some session is blocked on a lock the backend `blocker_pid` holds, whatever the statement."""
    deadline = asyncio.get_running_loop().time() + LOCK_WAIT_S
    query = sa.text('SELECT count(*) FROM pg_stat_activity WHERE CAST(:pid AS integer) = ANY(pg_blocking_pids(pid))')
    while True:
        async with engine.connect() as conn:
            if (await conn.execute(query, {'pid': blocker_pid})).scalar_one() > 0:
                return
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f'no session blocked behind backend {blocker_pid} within {LOCK_WAIT_S}s')
        await asyncio.sleep(0.05)


# --------------------------------------------------------------------------------------------- PG1


async def test_pg1_insert_attempt_names_the_partial_index_and_a_repeat_returns_none(
    intake, created, run_identity, own_symbol, instance_id
):
    holder = instance_id('pg1')
    request_id = await _request(intake.engine, created, run_identity, own_symbol, holder=holder, dataset_id=None)
    async with AsyncSession(intake.engine, expire_on_commit=False) as db:
        request = await ingest_delivery.lock_request(db, request_id)
        attempt = ingest_delivery.NewAttempt(
            request=request,
            attempt_no=1,
            instance_id=holder,
            quota_key=QUOTA_KEY,
            verdict='CONTINUE',
            state=IngestRunState.FAILED,
            trigger=IngestRunTrigger.REQUEST,
            bars_written=0,
            feed=Feed.IEX,
            stats=stats(),
            started_at=FIRST_CALL,
            finished_at=AS_OF,
            error_message=None,
            label=ingest_delivery.AttemptLabel(
                own_symbol, AssetType.STOCK, DataType.MARKET_ACTIVITY, Granularity.ONE_DAY, UpdateType.DAILY
            ),
        )
        first = await ingest_delivery.insert_attempt(db, attempt)
        await db.commit()

    async with AsyncSession(intake.engine, expire_on_commit=False) as db:
        again = await ingest_delivery.insert_attempt(db, attempt)
        await db.commit()

    assert first is not None and first.request_id == request_id
    assert again is None, 'the arbiter index held the attempt, so nothing was inserted and nothing raised'
    (row,) = await _runs(intake.engine, request_id)
    assert row.verdict == 'CONTINUE'


# --------------------------------------------------------------------------------------------- PG2 and PG3


async def test_pg2_a_succeeded_bars_delivery_commits_everything_and_pg3_a_repeat_changes_nothing(
    intake, created, run_identity, bars_entry, instance_id
):
    holder = instance_id('pg2')
    await intake.register(holder)
    request_id = await _request(
        intake.engine, created, run_identity, bars_entry.asset_symbol, holder=holder, dataset_id=bars_entry.id
    )
    attempt = report(request_id, parts=[bar_page(0, 1, 2), bar_page(3)], instance_id=holder)

    answer = await intake.deliver(attempt)

    # PG2.
    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    bars = await _bars(intake.engine, bars_entry.id)
    assert [row.timestamp for row in bars] == [BAR_START + timedelta(days=d) for d in range(4)]
    assert {(row.feed, row.asset_symbol, row.granularity) for row in bars} == {
        (Feed.IEX, bars_entry.asset_symbol, Granularity.ONE_DAY)
    }
    (run,) = await _runs(intake.engine, request_id)
    assert (run.attempt_no, run.instance_id, run.quota_key, run.verdict) == (1, holder, QUOTA_KEY, 'FINAL')
    assert (run.state, run.trigger, run.bars_written, run.dataset_id) == (
        IngestRunState.SUCCEEDED,
        IngestRunTrigger.REQUEST,
        4,
        bars_entry.id,
    )
    assert (run.asset_symbol, run.asset_type, run.data_type, run.granularity) == (
        bars_entry.asset_symbol,
        AssetType.STOCK,
        DataType.MARKET_ACTIVITY,
        Granularity.ONE_DAY,
    )
    assert (run.vendor_calls, run.vendor_pages, run.vendor_throttled_429, run.vendor_retries) == (3, 2, 1, 1)
    assert run.vendor_reported_limit == 200 and run.vendor_first_call_at == FIRST_CALL
    assert run.vendor_asset_id == VENDOR_ASSET_ID, 'PG9: the header id, as received, on the attempt row'
    row = await _request_row(intake.engine, request_id)
    assert (row.attempts, row.spent_calls, row.spent_pages, row.spent_throttled_429, row.spent_retries) == (
        1,
        3,
        2,
        1,
        1,
    )
    assert row.state is IngestRequestState.SUCCEEDED and row.finished_at is not None
    # The stored labels, as Postgres holds them: the enum labels and the integer update_type.
    async with intake.engine.connect() as conn:
        raw = (
            await conn.execute(
                sa.text(
                    'SELECT r.state::text AS state, r.failure_cause, run.feed::text AS feed, run.state::text AS '
                    'run_state, run.trigger::text AS trigger, run.update_type, run.vendor_asset_id FROM ingest_request '
                    'r JOIN ingest_run run ON run.request_id = r.id WHERE r.id = :id'
                ),
                {'id': request_id},
            )
        ).one()
    assert (raw.state, raw.failure_cause, raw.feed) == ('SUCCEEDED', None, Feed.IEX.value)
    assert (raw.trigger, raw.update_type) == (IngestRunTrigger.REQUEST.value, UpdateType.DAILY.value)
    assert raw.run_state == IngestRunState.SUCCEEDED.value
    assert raw.vendor_asset_id == VENDOR_ASSET_ID

    # PG3: the same delivery again.
    before = await _snapshot(intake.engine, request_id, bars_entry.id)
    published = len(intake.published)

    again = await intake.deliver(attempt)

    assert again == answer
    assert await _snapshot(intake.engine, request_id, bars_entry.id) == before, (
        'a repeat changed the request, the attempt rows or a bar (updated_at included)'
    )
    assert len(intake.published) == published


# --------------------------------------------------------------------------------------------- PG4


async def test_pg4_two_identical_deliveries_racing_leave_one_attempt_row_and_a_count_of_one(
    intake, created, run_identity, bars_entry, instance_id, monkeypatch
):
    holder = instance_id('pg4')
    # Registered first, as ingest does at start, so the two deliveries race on the request lock and not on a first
    # sighting of the instance.
    await intake.register(holder)
    request_id = await _request(
        intake.engine, created, run_identity, bars_entry.asset_symbol, holder=holder, dataset_id=bars_entry.id
    )
    attempt = report(request_id, parts=[bar_page(0, 1)], instance_id=holder)

    # FORCE THE INTERLEAVING. Each delivery, once it has looked for a stored attempt, waits for the other to have
    # looked too (up to RACE_WINDOW_S). With the request row locked FOR UPDATE the second cannot look until the first
    # has committed, so the first waits out the window alone and the second FINDS the attempt. Without the lock both
    # look, both see none, and both go on to insert: the race this case exists to rule out.
    real_find = delivery_intake.find_attempt
    looked: list[object] = []
    both_looked = asyncio.Event()

    async def find_then_wait(db, request_id, attempt_no):
        found = await real_find(db, request_id, attempt_no)
        looked.append(found)
        if len(looked) >= 2:
            both_looked.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(both_looked.wait(), RACE_WINDOW_S)
        return found

    monkeypatch.setattr(delivery_intake, 'find_attempt', find_then_wait)
    first, second = await asyncio.wait_for(
        asyncio.gather(intake.deliver(attempt), intake.deliver(attempt), return_exceptions=True), LOCK_WAIT_S
    )

    assert len(looked) == 2 and looked[0] is None
    assert looked[1] is not None, 'the second delivery read before the first committed: the request lock did not hold'
    assert first == second == DeliverAttemptResponse(verdict=FINAL, attempt_count=1), (first, second)
    runs = await _runs(intake.engine, request_id)
    assert len(runs) == 1, f'{len(runs)} attempt rows for one (request_id, attempt_no)'
    row = await _request_row(intake.engine, request_id)
    assert (row.attempts, row.spent_calls) == (1, 3), 'the attempt was counted once and its spend added once'
    assert len(await _bars(intake.engine, bars_entry.id)) == 2


# --------------------------------------------------------------------------------------------- PG5


@pytest.mark.parametrize(
    ('second_page', 'reason'),
    [
        (BarPage(bars=[bar(5), bar(5)]), Reason.DUPLICATE_BAR_TIMESTAMP),
        (BarPage(bars=[bar(5, feed=Feed.SIP)]), Reason.INVALID_REQUEST),
    ],
    ids=['duplicate-timestamp', 'another-tape'],
)
async def test_pg5_a_refusal_after_page_one_was_upserted_rolls_back_everything(
    intake, created, run_identity, bars_entry, instance_id, second_page, reason
):
    holder = instance_id('pg5')
    await intake.register(holder)
    request_id = await _request(
        intake.engine, created, run_identity, bars_entry.asset_symbol, holder=holder, dataset_id=bars_entry.id
    )
    before = await _snapshot(intake.engine, request_id, bars_entry.id)

    with pytest.raises(InvalidRequestError) as caught:
        await intake.deliver(report(request_id, parts=[bar_page(0, 1, 2), second_page], instance_id=holder))

    assert caught.value.reason is reason
    assert await _snapshot(intake.engine, request_id, bars_entry.id) == before
    assert await _bars(intake.engine, bars_entry.id) == [], 'page 1 was upserted and must have been rolled back'
    assert intake.published == []


# --------------------------------------------------------------------------------------------- PG6


@pytest.mark.parametrize(
    ('outcome', 'cause'), [(DeliveryOutcome.EXPIRED, 'EXPIRED'), (DeliveryOutcome.EXHAUSTED, 'ATTEMPTS_EXHAUSTED')]
)
async def test_pg6_expired_and_exhausted_fail_the_request_with_no_attempt_row_and_the_count_unchanged(
    intake, created, run_identity, own_symbol, instance_id, outcome, cause
):
    holder = instance_id('pg6')
    await intake.register(holder)
    request_id = await _request(intake.engine, created, run_identity, own_symbol, holder=holder, dataset_id=None)
    async with intake.engine.begin() as conn:
        await conn.execute(
            sa.update(REQUESTS).where(REQUESTS.c.id == request_id).values(attempts=2, spent_calls=7, spent_pages=3)
        )
    attempt = report(request_id, outcome, attempt_no=3, instance_id=holder)

    answer = await intake.deliver(attempt)

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=2)
    assert await _runs(intake.engine, request_id) == []
    row = await _request_row(intake.engine, request_id)
    assert (row.state, row.failure_cause) == (IngestRequestState.FAILED, cause)
    assert row.finished_at is not None
    assert (row.attempts, row.spent_calls, row.spent_pages, row.spent_throttled_429, row.spent_retries) == (
        2,
        7,
        3,
        0,
        0,
    )

    before = await _snapshot(intake.engine, request_id, None)
    again = await intake.deliver(attempt)
    assert again == answer
    assert await _snapshot(intake.engine, request_id, None) == before


# --------------------------------------------------------------------------------------------- PG7


async def _assert_dropped(intake: Intake, request_id: uuid.UUID, dataset_id: uuid.UUID, answer) -> None:
    """The drop rule (P35): FINAL, nothing stored, the attempt row EMPTY, the request EMPTY with cause DISCARDED."""
    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    assert await _bars(intake.engine, dataset_id) == [], 'no bar was written under the gone or stopped entry'
    (run,) = await _runs(intake.engine, request_id)
    assert (run.state, run.bars_written, run.verdict) == (IngestRunState.EMPTY, 0, 'FINAL')
    row = await _request_row(intake.engine, request_id)
    assert (row.state, row.failure_cause) == (IngestRequestState.EMPTY, IngestRequestFailureCause.DISCARDED.value)
    assert row.finished_at is not None and row.attempts == 1
    assert not [event for event in intake.published if isinstance(event, DatasetChanged)], 'a drop announces no dataset'


async def test_pg7_bars_for_a_dataset_id_naming_no_entry_are_dropped_and_nothing_stored(
    intake, created, run_identity, own_symbol, instance_id
):
    """P35 turned P34's C1 refusal into the drop: a refusal would be redelivered for ever."""
    holder = instance_id('pg7a')
    await intake.register(holder)
    missing = uuid.uuid4()
    request_id = await _request(intake.engine, created, run_identity, own_symbol, holder=holder, dataset_id=missing)

    answer = await intake.deliver(report(request_id, instance_id=holder))

    await _assert_dropped(intake, request_id, missing, answer)


async def test_pg7_an_entry_deleted_by_a_concurrent_transaction_is_seen_as_gone_once_it_commits_and_dropped(
    intake, created, run_identity, bars_entry, instance_id
):
    """A concurrent delete holding the entry lock: the delivery waits, then drops cleanly.

    Bars carry a foreign key to their entry (ON DELETE CASCADE), the backstop; the arm locks the entry FOR UPDATE
    first, so the delivery waits for the delete, then sees the entry gone and drops cleanly, no FK violation.
    """
    holder = instance_id('pg7b')
    await intake.register(holder)
    request_id = await _request(
        intake.engine, created, run_identity, bars_entry.asset_symbol, holder=holder, dataset_id=bars_entry.id
    )

    # The operator's delete (or the reaper's) holds the entry's row lock, uncommitted, while the delivery arrives.
    async with AsyncSession(intake.engine) as deleter:
        deleter_pid = (await deleter.execute(sa.text('SELECT pg_backend_pid()'))).scalar_one()
        await deleter.execute(sa.text(f'DELETE FROM {ENTRIES_NAME} WHERE id = :id'), {'id': bars_entry.id})  # nosec B608
        delivering = asyncio.create_task(intake.deliver(report(request_id, instance_id=holder)))
        # Blocked behind the delete: the entry lock.
        await _wait_until_blocked_by(intake.engine, deleter_pid)
        await deleter.commit()

    answer = await asyncio.wait_for(delivering, LOCK_WAIT_S)

    await _assert_dropped(intake, request_id, bars_entry.id, answer)


async def test_pg11_a_delivery_for_a_stopped_subscription_is_dropped(
    intake, created, run_identity, insert_entry, own_symbol, instance_id
):
    stopped = insert_entry(
        asset_symbol=own_symbol,
        granularity=Granularity.ONE_DAY,
        feed=Feed.IEX,
        update_type=UpdateType.DAILY.value,
        expiry_type=ExpiryType.ROLLING.value,
        collect=False,
    )
    holder = instance_id('pg11')
    await intake.register(holder)
    request_id = await _request(intake.engine, created, run_identity, own_symbol, holder=holder, dataset_id=stopped.id)

    answer = await intake.deliver(report(request_id, instance_id=holder))

    await _assert_dropped(intake, request_id, stopped.id, answer)


# --------------------------------------------------------------------------------------------- PG10


async def _entry_of_request(intake: Intake, request_id: uuid.UUID) -> sa.Row:
    row = await _request_row(intake.engine, request_id)
    assert row.dataset_id is not None, 'the first delivered attempt set the request dataset_id'
    async with intake.engine.connect() as conn:
        return (
            await conn.execute(sa.text(f'SELECT * FROM {ENTRIES_NAME} WHERE id = :id'), {'id': row.dataset_id})  # nosec B608
        ).one()


async def test_pg10_the_first_delivered_attempt_creates_the_entry_through_the_real_upsert(
    intake, created, run_identity, own_symbol, instance_id, entry_registry
):
    """The first delivered attempt creates the entry through the real upsert; a repeat matches it.

    P35 ruling A and note H: the entry is upserted from the request row (expiry, expiry type, collect) and the
    header's resolved feed, in the delivery transaction, and the bars land under it; a repeat POST of the same
    identity matches it, sets expiry from the request and collect only when explicit, and a flip publishes RETIRED.
    """
    holder = instance_id('pg10')
    await intake.register(holder)
    expiry = START + timedelta(days=400)
    first = await _request(
        intake.engine,
        created,
        run_identity,
        own_symbol,
        holder=holder,
        dataset_id=None,
        requested_end=None,
        expiry=expiry,
        expiry_type=ExpiryType.ROLLING,
        collect=None,
        trigger=IngestRunTrigger.REQUEST,
    )

    answer = await intake.deliver(report(request_id=first, instance_id=holder))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    entry = await _entry_of_request(intake, first)
    entry_registry.add(entry.id)
    assert (entry.owner, entry.asset_symbol, entry.feed) == (run_identity.owner, own_symbol, Feed.IEX.value)
    assert (entry.expiry_type, entry.update_type, entry.collect) == (
        ExpiryType.ROLLING.value,
        UpdateType.DAILY.value,
        True,
    ), 'collect omitted is true for a subscription'
    assert entry.expiry == expiry
    assert len(await _bars(intake.engine, entry.id)) == 2
    (run,) = await _runs(intake.engine, first)
    assert run.dataset_id == entry.id

    # The same identity again, collect false: the entry is matched, stopped, and the flip is RETIRED.
    later_expiry = expiry + timedelta(days=30)
    again = await _request(
        intake.engine,
        created,
        run_identity,
        own_symbol,
        holder=holder,
        dataset_id=None,
        requested_end=None,
        expiry=later_expiry,
        expiry_type=ExpiryType.ROLLING,
        collect=False,
        trigger=IngestRunTrigger.REQUEST,
    )
    await intake.deliver(report(request_id=again, instance_id=holder, parts=[BarPage(bars=[])]))

    matched = await _entry_of_request(intake, again)
    assert matched.id == entry.id
    assert (matched.collect, matched.expiry) == (False, later_expiry)
    assert DatasetChanged(entry.id, DatasetChange.RETIRED) in intake.published


async def test_pg10_a_request_with_no_dataset_and_no_expiry_type_is_refused_and_nothing_written(
    intake, created, run_identity, own_symbol, instance_id
):
    holder = instance_id('pg10b')
    await intake.register(holder)
    request_id = await _request(
        intake.engine,
        created,
        run_identity,
        own_symbol,
        holder=holder,
        dataset_id=None,
        update_type=UpdateType.STATIC,
        expiry_type=None,
    )
    before = await _snapshot(intake.engine, request_id, None)

    with pytest.raises(InvalidRequestError) as caught:
        await intake.deliver(report(request_id, instance_id=holder))

    assert caught.value.reason is Reason.INVALID_REQUEST and 'expiry type' in caught.value.detail
    assert await _snapshot(intake.engine, request_id, None) == before
    async with intake.engine.connect() as conn:
        count = (
            await conn.execute(
                sa.text(f'SELECT count(*) FROM {ENTRIES_NAME} WHERE asset_symbol = :symbol'),  # nosec B608
                {'symbol': own_symbol},
            )
        ).scalar_one()
    assert count == 0, 'no entry, BULK or otherwise'


# --------------------------------------------------------------------------------------------- PG8


async def test_pg8_a_new_instance_supersedes_the_old_and_the_old_instances_later_reports_are_still_written(
    intake, created, run_identity, bars_entry, instance_id
):
    old, new = instance_id('pg8-old'), instance_id('pg8-new')
    await intake.register(old)
    request_id = await _request(
        intake.engine, created, run_identity, bars_entry.asset_symbol, holder=old, dataset_id=bars_entry.id
    )

    await intake.register(new)

    async with intake.engine.connect() as conn:
        old_row = (await conn.execute(sa.select(INSTANCES).where(INSTANCES.c.instance_id == old))).one()
        new_row = (await conn.execute(sa.select(INSTANCES).where(INSTANCES.c.instance_id == new))).one()
    assert old_row.superseded_at is not None and new_row.superseded_at is None
    assert (await _request_row(intake.engine, request_id)).state is IngestRequestState.NOT_DELIVERED

    # The old instance's transient failure: it happened and cost what it cost, so it is written, and FINAL.
    failed = await intake.deliver(
        report(
            request_id,
            DeliveryOutcome.FAILED,
            error_class=DeliveryErrorClass.TRANSIENT,
            last_attempt=True,
            instance_id=old,
        )
    )

    assert failed == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    row = await _request_row(intake.engine, request_id)
    assert (row.state, row.instance_id, row.finished_at) == (IngestRequestState.NOT_DELIVERED, old, None)
    assert (row.attempts, row.spent_calls) == (1, 3)
    (run,) = await _runs(intake.engine, request_id)
    assert (run.instance_id, run.verdict, run.state) == (old, 'FINAL', IngestRunState.FAILED)

    # The old instance's success is conclusive by itself: it finishes the request.
    succeeded = await intake.deliver(report(request_id, attempt_no=2, instance_id=old))

    assert succeeded == DeliverAttemptResponse(verdict=FINAL, attempt_count=2)
    row = await _request_row(intake.engine, request_id)
    assert row.state is IngestRequestState.SUCCEEDED and row.finished_at is not None
    assert len(await _bars(intake.engine, bars_entry.id)) == 2
    assert [run.trigger for run in await _runs(intake.engine, request_id)] == [
        IngestRunTrigger.REQUEST,
        IngestRunTrigger.RETRY,
    ]


# --------------------------------------------------------------------------------------------- PG9


async def test_pg9_an_empty_header_vendor_asset_id_is_stored_null_never_an_empty_string(
    intake, created, run_identity, bars_entry, instance_id
):
    holder = instance_id('pg9')
    await intake.register(holder)
    request_id = await _request(
        intake.engine, created, run_identity, bars_entry.asset_symbol, holder=holder, dataset_id=bars_entry.id
    )
    attempt = report(request_id, DeliveryOutcome.FAILED, instance_id=holder)
    attempt = attempt.model_copy(update={'header': attempt.header.model_copy(update={'vendor_asset_id': ''})})

    await intake.deliver(attempt)

    (run,) = await _runs(intake.engine, request_id)
    assert run.vendor_asset_id is None
    # As Postgres holds it: NULL, not the empty string.
    async with intake.engine.connect() as conn:
        is_null = (
            await conn.execute(
                sa.text('SELECT vendor_asset_id IS NULL FROM ingest_run WHERE request_id = :id'), {'id': request_id}
            )
        ).scalar_one()
    assert is_null is True


# --------------------------------------------------------------------------------------------- PG12
# tj-7zjov1.31 (P18), the corporate-actions arm on the real table: bytea and JSONB round trips, the real unique index
# as the dedupe arbiter (ON CONFLICT DO NOTHING on it), a vendor revision as a new row, the attempt row's NULL labels
# and NULL vendor asset id on Postgres (the architect's 11:50 UTC note asks for exactly this pin), and the observations
# rolled back with a refused delivery. Vendor action ids carry this run's id: the index is global and the table
# append-only, so a fixed id would meet an earlier run's row; the created fixture deletes this test's observations.


def ca_report(
    request_id: uuid.UUID,
    records: list[CorporateActionRecord],
    *,
    instance_id: str,
    attempt_no: int = 1,
    as_of: datetime = AS_OF,
    vendor_asset_id: str = '',
) -> AttemptDelivery:
    """A SUCCEEDED corporate-actions report: no feed, the served range, the records in one payload message."""
    return AttemptDelivery(
        header=DeliveryHeader(
            request_id=str(request_id),
            instance_id=instance_id,
            attempt_no=attempt_no,
            quota_key=QUOTA_KEY,
            feed=None,
            vendor_asset_id=vendor_asset_id,
        ),
        parts=[CorporateActionRecords(records=records)] if records else [],
        trailer=DeliveryTrailer(
            outcome=DeliveryOutcome.SUCCEEDED,
            vendor_stats=stats(),
            served_range=ServedRange(start=START, end=START + timedelta(days=30)),
            as_of=as_of,
        ),
    )


async def _ca_request(engine, created, run_identity, *, holder: str, symbol: str | None = 'XLNX') -> uuid.UUID:
    return await _request(
        engine,
        created,
        run_identity,
        symbol,
        holder=holder,
        dataset_id=None,
        kind=IngestRequestKind.CORPORATE_ACTIONS,
        asset_type=None,
        data_types=None,
        granularity=None,
        update_type=None,
        feed=None,
    )


async def _observations(engine: AsyncEngine, *request_ids: uuid.UUID) -> list[sa.Row]:
    async with engine.connect() as conn:
        statement = (
            sa.select(ACTIONS)
            .where(ACTIONS.c.request_id.in_(request_ids))
            .order_by(ACTIONS.c.received_at, ACTIONS.c.vendor_action_id, ACTIONS.c.as_of)
        )
        return list((await conn.execute(statement)).all())


async def test_pg12_a_corporate_actions_delivery_stores_raw_bytes_and_identifiers_and_null_labels(
    intake, created, run_identity, instance_id
):
    holder = instance_id('pg12')
    await intake.register(holder)
    request_id = await _ca_request(intake.engine, created, run_identity, holder=holder)
    vendor_id = f'ca-{run_identity.run_id}-{uuid.uuid4().hex[:8]}'
    raw = b'\x00\xff{"id":"' + vendor_id.encode() + b'","note":"bytes, not text"}'
    record = CorporateActionRecord(
        vendor_action_id=vendor_id,
        action_type='stock_merger',
        process_date=datetime(2022, 2, 14, tzinfo=UTC),
        effective_date=datetime(2022, 2, 14, tzinfo=UTC),
        identifiers={'acquirer_symbol': 'AMD', 'target_symbol': 'XLNX'},
        raw_payload=raw,
    )

    # The header names a vendor asset id: a non-bars attempt row stores NULL whatever the header carries.
    answer = await intake.deliver(ca_report(request_id, [record], instance_id=holder, vendor_asset_id=VENDOR_ASSET_ID))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    (row,) = await _observations(intake.engine, request_id)
    assert (row.vendor_action_id, row.action_type, row.raw_payload) == (vendor_id, 'stock_merger', raw)
    assert row.payload_hash == hashlib.sha256(raw).hexdigest()
    assert row.identifiers == {'acquirer_symbol': 'AMD', 'target_symbol': 'XLNX'}
    assert (row.process_date, row.effective_date, row.ex_date) == (record.process_date, record.effective_date, None)
    assert (row.request_id, row.attempt_no, row.requesting_symbol, row.as_of) == (request_id, 1, 'XLNX', AS_OF)
    assert row.source is DataSource.ALPACA_API
    (run,) = await _runs(intake.engine, request_id)
    assert (run.state, run.bars_written, run.feed) == (IngestRunState.SUCCEEDED, 0, None)
    assert (run.asset_symbol, run.asset_type, run.data_type, run.granularity, run.update_type) == (
        'XLNX',
        None,
        None,
        None,
        None,
    )
    assert run.served_start == START
    async with intake.engine.connect() as conn:
        raw_row = (
            await conn.execute(
                sa.text(
                    'SELECT run.vendor_asset_id IS NULL AS no_vendor_id, run.granularity IS NULL AS no_width, '
                    "ca.identifiers->>'acquirer_symbol' AS acquirer, octet_length(ca.raw_payload) AS size "
                    'FROM ingest_run run JOIN corporate_action ca ON ca.request_id = run.request_id '
                    'WHERE run.request_id = :id'
                ),
                {'id': request_id},
            )
        ).one()
    assert (raw_row.no_vendor_id, raw_row.no_width, raw_row.acquirer, raw_row.size) == (True, True, 'AMD', len(raw))


async def test_pg12_a_corporate_actions_request_with_no_symbol_writes_every_label_null(
    intake, created, run_identity, instance_id
):
    holder = instance_id('pg12-nosym')
    await intake.register(holder)
    request_id = await _ca_request(intake.engine, created, run_identity, holder=holder, symbol=None)

    await intake.deliver(ca_report(request_id, [], instance_id=holder))

    (run,) = await _runs(intake.engine, request_id)
    assert (run.asset_symbol, run.asset_type, run.data_type, run.granularity, run.update_type) == (None,) * 5
    assert run.state is IngestRunState.EMPTY


async def test_pg12_the_unique_index_keeps_a_repeat_once_and_a_revision_is_a_new_row(
    intake, created, run_identity, instance_id
):
    holder = instance_id('pg12-dedupe')
    await intake.register(holder)
    first = await _ca_request(intake.engine, created, run_identity, holder=holder)
    second = await _ca_request(intake.engine, created, run_identity, holder=holder)
    vendor_id = f'ca-{run_identity.run_id}-{uuid.uuid4().hex[:8]}'
    original = CorporateActionRecord(vendor_action_id=vendor_id, action_type='split', raw_payload=b'{"r":"1:2"}')
    revised = CorporateActionRecord(vendor_action_id=vendor_id, action_type='split', raw_payload=b'{"r":"1:3"}')
    later = AS_OF + timedelta(hours=6)

    await intake.deliver(ca_report(first, [original, original], instance_id=holder))
    await intake.deliver(ca_report(second, [original, revised], instance_id=holder, as_of=later))

    rows = await _observations(intake.engine, first, second)
    assert [(row.request_id, row.raw_payload, row.as_of) for row in rows] == [
        (first, original.raw_payload, AS_OF),
        (second, revised.raw_payload, later),
    ]


async def test_pg12_a_refused_corporate_actions_delivery_leaves_no_observation(
    intake, created, run_identity, instance_id, monkeypatch
):
    """The arm writes inside the delivery transaction: a failure after it rolls the observations back."""
    holder = instance_id('pg12-rollback')
    await intake.register(holder)
    request_id = await _ca_request(intake.engine, created, run_identity, holder=holder)
    record = CorporateActionRecord(
        vendor_action_id=f'ca-{run_identity.run_id}-{uuid.uuid4().hex[:8]}', action_type='split', raw_payload=b'{}'
    )

    async def failing_insert_attempt(db, attempt):
        raise InvalidRequestError(Reason.INVALID_REQUEST, 'the attempt row could not be written')

    monkeypatch.setattr(delivery_intake, 'insert_attempt', failing_insert_attempt)

    with pytest.raises(InvalidRequestError):
        await intake.deliver(ca_report(request_id, [record], instance_id=holder))

    assert await _observations(intake.engine, request_id) == []
    assert await _runs(intake.engine, request_id) == []
