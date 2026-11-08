"""The interim DAILY refresh on real Postgres, its requests answered through the real delivery intake (validator).

The design is the architect's ruling on tj-utqd77 Q1 and its notes on tj-grna9p.22, as the push path (tj-7zjov1.28,
P35) changed it: each pass derives its work from the database, asks only for the MISSING TAIL of each behind,
non-retired DAILY dataset, and now WRITES a request (trigger SCHEDULE, the entry's dataset_id, actor `scheduler`,
ready_by the next market open) and SUBMITS it without waiting; ingest pushes the result to the store's delivery
intake, which writes the bars under the named entry in one transaction.

REPAIRED FOR THE PUSH PATH (validator, tj-7zjov1.28). Until P35 this file hosted ingest's FetchDataset servicer around
FakeRead and the refresh fetched through GrpcIngestFetchClient; that stream and client are gone (P37). The Submit and
the delivery have their own gRPC tier (test_delivery_intake.py, test_delivery_intake_pg.py); what only THIS file shows
is the refresh's own end on Postgres, so ingest is now a double that answers each Submit as ingest does (a tape it
cannot serve is a refused answer) and then DELIVERS, through the real DeliveryIntake on the scratch database, exactly
the bars FakeRead serves (tests.fakes.market_data.fake_bar), as of the pass's clock. Pinned here:
  * the narrowed request (the tail) lands, through the real bar INSERT ... ON CONFLICT, on the SAME entry: one entry,
    the same id, the declared range unchanged; the new bars are stored on it and the old ones are untouched, and a
    second pass asks only from the new last bar;
  * a refused Submit (the stored tape is one this deployment cannot serve) leaves no second entry and no bar, and its
    request is FAILED with cause PERMANENT; the refresh's request rows round-trip trigger SCHEDULE, the actor, the
    dataset id, ready_by and the narrowed requested_start, and the attempt row takes the trigger;
  * an operator Stop committed before the delivery drops it (the request EMPTY, cause DISCARDED), one committed while
    the delivery holds the entry's row lock waits for it and is kept, and an expiry PATCH drops nothing;
  * (tj-grna9p.167) a newest bar written before its session closed is asked for again INCLUSIVELY, and the real
    ON CONFLICT DO UPDATE rewrites that row with an updated_at past the close, so it then reads FRESH.

First run on Postgres 2026-10-26: four tests failed on the TEST, not the store. The double stepped 24 hours from the
requested start instead of serving FakeRead's UTC-midnight grid, the expected tail was the last bar plus 24 hours
instead of the local midnight after its session (_after_bar), and the provisional bar was stamped after its own
session's close, so it was written before its stamp and fell under the lost-anchor rule instead. Repaired by the
validator (tj-7zjov1.28); re-run on the stack pending. It runs with the rest of the suite (make test-system, or
agent-stack run_system_tests after test_up) on a stack migrated to the head.

WHY A SCRATCH DATABASE: a pass refreshes EVERY behind DAILY dataset it finds, and the stack's database holds rows
this suite does not own. So every pass here runs against a database this test creates, migrates and drops.

Run by make test-system and the agent-stack MCP's run_system_tests. FAIL, NEVER SKIP.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.database.sql_alchemy_nullable_datetime import NullableDateTime
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import ERROR_DOMAIN, Reason
from data.store.app import daily_refresh as refresh_module
from data.store.app.client_label import OPERATOR_PRINCIPAL
from data.store.app.daily_refresh import SCHEDULER_ACTOR, DailyRefresher, PassResult
from data.store.app.database.crud.stock.dataset_catalog import get_catalog_row
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestFailureCause, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.events import DatasetChange, DatasetChanged
from data.store.app.health import DatasetHealthStatus, evaluate_dataset
from data.store.app.ingest import data_action_request, delivery_intake
from data.store.app.trading_calendar import calendar_for_source
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    DeliveryHeader,
    DeliveryOutcome,
    DeliveryTrailer,
    VendorCallStats,
)
from schemas.data_ingest.fetch_dataset import Bar, BarPage, FetchRefused, ServedRange
from schemas.data_ingest.submit import SubmitAccepted, SubmitAck, SubmitRequest, SubmitResponse
from tests.fakes.market_data import FEED as FAKE_FEED
from tests.fakes.market_data import fake_bar, grid_timestamps
from tests.system import test_reaper_race as race
from tests.system import test_usage_record
from tests.system.conftest import BAR_TABLE, ENTRY_TABLE


pytestmark = pytest.mark.data_store

scratch_db = test_usage_record.scratch_db
scratch_async = test_usage_record.scratch_async
_head = test_usage_record._head
# The scratch database at head with the real app's routes on it, reused: the race tests PATCH through it.
store = race.store

RUN = IngestRun.__table__
REQUEST = IngestRequest.__table__

# Tue 6 Oct 2026 22:00Z: the 6th's XNYS session closed at 20:00Z. FakeRead stamps a daily bar at UTC midnight, which
# is the previous evening in New York, so a bar at 2 Oct 00:00Z belongs to the 1st's session and the dataset is behind.
NOW = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
DAY = Granularity.ONE_DAY.offset
START = datetime(2026, 9, 28, tzinfo=UTC)
SEEDED = [START + DAY * k for k in range(5)]  # 28 Sep .. 2 Oct, 00:00Z
UNSERVABLE_FEED = Feed.SIP
assert UNSERVABLE_FEED is not FAKE_FEED
# The DAILY loop's ready_by at NOW: the open of Wed 7 Oct, 09:30 New York (EDT).
NEXT_OPEN = datetime(2026, 10, 7, 13, 30, tzinfo=UTC)
# Where the tail starts after the newest stored 1day bar (CLAUDE.md DAILY refresh, _after_bar): the LOCAL midnight
# after that bar's session, never the bar plus 24 hours. The bar at 2 Oct 00:00Z is the 1st's session (8 pm New
# York), so the tail starts at the 2nd's local midnight, 04:00Z under EDT. Written as a literal on purpose: the
# expectation does not come from the code under test.
TAIL_START = datetime(2026, 10, 2, 4, 0, tzinfo=UTC)
NY = ZoneInfo('America/New_York')


def grid_stamps(start: datetime, end: datetime) -> list[datetime]:
    """FakeRead's 1day stamps in [start, end): UTC midnights, the first at or after start (tests.fakes.market_data)."""
    return list(grid_timestamps(Granularity.ONE_DAY, start, end))


def vendor_stamps(start: datetime, end: datetime) -> list[datetime]:
    """A real vendor's 1day stamps in [start, end): each New York local midnight, the first at or after start."""
    day = start.astimezone(NY).date()
    stamps = []
    while True:
        stamp = datetime.combine(day, datetime.min.time(), tzinfo=NY).astimezone(UTC)
        if stamp >= end:
            return stamps
        if stamp >= start:
            stamps.append(stamp)
        day += timedelta(days=1)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeIngest:
    """Ingest as the refresh sees it: answers each Submit, then (deliver_all) reports the one attempt it made.

    A request naming a tape other than FakeRead's is REFUSED in the answer, as ingest refuses a feed it cannot serve.
    An accepted request is delivered as one SUCCEEDED attempt carrying every bar FakeRead serves for the symbol: its
    grid (UTC midnights) from the first point at or after the requested start while before the clock, its served
    window [start, clock). `stamps` replaces the grid where a test needs a real vendor's local-midnight stamps.
    """

    def __init__(
        self,
        clock: Clock,
        intake: delivery_intake.DeliveryIntake,
        stamps: Callable[[datetime, datetime], list[datetime]] = grid_stamps,
    ) -> None:
        self.clock = clock
        self.intake = intake
        self.stamps = stamps
        self.instance_id = f'drp-{uuid.uuid4()}'
        self.submitted: list[SubmitRequest] = []
        self.queued: list[SubmitRequest] = []

    def asked(self) -> list[tuple[Feed | None, datetime]]:
        return sorted((request.bars.feed, request.bars.start) for request in self.submitted)

    async def submit(self, request: SubmitRequest) -> SubmitResponse:
        self.submitted.append(request)
        if request.bars.feed not in (None, FAKE_FEED):
            refused = FetchRefused(
                reason=Reason.FEED_NOT_AVAILABLE.value, domain=ERROR_DOMAIN, detail='not this tape', metadata={}
            )
            return SubmitResponse(ack=SubmitAck(refused=refused), instance_id=self.instance_id)
        self.queued.append(request)
        accepted = SubmitAccepted(feed=FAKE_FEED, quota_key='drp-quota')
        return SubmitResponse(ack=SubmitAck(accepted=accepted), instance_id=self.instance_id)

    async def instance(self) -> Any:
        raise AssertionError('the refresh never probes the instance')

    def _report(self, request: SubmitRequest) -> AttemptDelivery:
        now = self.clock.now
        bars = []
        for stamp in self.stamps(request.bars.start, now):
            served = fake_bar(request.bars.asset_symbol, stamp)
            bars.append(
                Bar(
                    bar_start=served.timestamp,
                    open=served.open,
                    high=served.high,
                    low=served.low,
                    close=served.close,
                    volume=float(served.volume),
                    trade_count=served.trade_count,
                    feed=FAKE_FEED,
                )
            )
        called = now - timedelta(seconds=5)
        return AttemptDelivery(
            header=DeliveryHeader(
                request_id=request.request_id,
                instance_id=self.instance_id,
                attempt_no=1,
                quota_key='drp-quota',
                feed=FAKE_FEED,
                vendor_asset_id='',
            ),
            parts=[BarPage(bars=bars)] if bars else [],
            trailer=DeliveryTrailer(
                outcome=DeliveryOutcome.SUCCEEDED,
                vendor_stats=VendorCallStats(
                    calls=1, pages=1, throttled_429=0, retries=0, first_call_at=called, last_call_at=called
                ),
                served_range=ServedRange(start=request.bars.start, end=now),
                as_of=now,
            ),
        )

    async def deliver_all(self) -> None:
        """Deliver every accepted request, in the order it was submitted."""
        queued, self.queued = self.queued, []
        for request in queued:
            await self.intake.deliver(self._report(request))


def _seed(engine: sa.Engine, entry_values, bar_values, run_identity) -> dict[str, uuid.UUID]:
    daily = {
        'update_type': UpdateType.DAILY.value,
        'expiry_type': ExpiryType.ROLLING.value,
        'granularity': Granularity.ONE_DAY,
        'start': START,
        'end': NullableDateTime.EPOCH,
    }
    plan = {
        'behind': {**daily},
        'refused': {**daily, 'feed': UNSERVABLE_FEED},
        # Stopped by its collect intent (decision tj-hhy3dk): an expiry would retire nothing.
        'retired': {**daily, 'collect': False},
    }
    ids: dict[str, uuid.UUID] = {}
    with engine.begin() as conn:
        for name, columns in plan.items():
            row = conn.execute(
                sa.insert(ENTRY_TABLE)
                .values(entry_values(asset_symbol=run_identity.alt_symbol(f'dr{name}'), **columns))
                .returning(ENTRY_TABLE)
            ).one()
            ids[name] = row.id
            for timestamp in SEEDED:
                conn.execute(sa.insert(BAR_TABLE).values(bar_values(row, timestamp=timestamp)))
    return ids


def _entries(engine: sa.Engine) -> dict[uuid.UUID, sa.Row]:
    with engine.connect() as conn:
        return {row.id: row for row in conn.execute(sa.select(ENTRY_TABLE))}


def _bar_times(engine: sa.Engine, dataset_id: uuid.UUID) -> list[datetime]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(BAR_TABLE.c.timestamp)
                .where(BAR_TABLE.c.dataset_id == dataset_id)
                .order_by(BAR_TABLE.c.timestamp)
            ).scalars()
        )


def _runs(engine: sa.Engine) -> list[sa.Row]:
    with engine.connect() as conn:
        return list(conn.execute(sa.select(RUN).order_by(RUN.c.started_at)))


def _requests(engine: sa.Engine) -> dict[uuid.UUID | None, sa.Row]:
    """The request rows, by the dataset they name."""
    with engine.connect() as conn:
        return {row.dataset_id: row for row in conn.execute(sa.select(REQUEST))}


@pytest.fixture
def point_refresh(monkeypatch: pytest.MonkeyPatch, scratch_async: AsyncEngine) -> list[Any]:
    """The refresh's, the submit path's and the intake's own_session on the scratch database; the events published."""

    @asynccontextmanager
    async def own_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(scratch_async, expire_on_commit=False) as session:
            yield session

    published: list[Any] = []
    monkeypatch.setattr(refresh_module, 'own_session', own_session)
    monkeypatch.setattr(data_action_request, 'own_session', own_session)
    monkeypatch.setattr(delivery_intake, 'own_session', own_session)
    monkeypatch.setattr(delivery_intake, 'publish', published.append)
    monkeypatch.delenv(refresh_module.MAX_PER_PASS_ENV, raising=False)
    return published


def _ingest(clock: Clock, stamps: Callable[[datetime, datetime], list[datetime]] = grid_stamps) -> FakeIngest:
    return FakeIngest(clock, delivery_intake.DeliveryIntake(), stamps)


@pytest.mark.asyncio
async def test_a_refresh_extends_the_same_entry_with_only_the_missing_tail_on_postgres(
    scratch_db, point_refresh, entry_values, bar_values, run_identity
):
    scratch_db.must('upgrade', _head())
    ids = _seed(scratch_db.engine, entry_values, bar_values, run_identity)
    before = _entries(scratch_db.engine)
    clock = Clock(NOW)
    ingest = _ingest(clock)

    result = await DailyRefresher(ingest, clock=clock).run_pass(DataSource.ALPACA_API)

    # FEED_NOT_AVAILABLE is REFUSED in the vocabulary: a permanent refusal, not a transient failure.
    assert result == PassResult(behind=2, refreshed=1, failed=0, deferred=0, refused=1)
    # Retired is never asked for. Each request names the stored tape and starts at the missing tail.
    assert ingest.asked() == sorted([(FAKE_FEED, TAIL_START), (UNSERVABLE_FEED, TAIL_START)])
    requests = _requests(scratch_db.engine)
    assert set(requests) == {ids['behind'], ids['refused']}
    for request in requests.values():
        assert request.trigger == IngestRunTrigger.SCHEDULE
        assert request.client_label == SCHEDULER_ACTOR
        assert request.requested_start == TAIL_START
        assert request.requested_end is None
        assert request.ready_by == NEXT_OPEN
        assert (request.expiry, request.collect, request.expiry_type) == (None, None, None)
    refused = requests[ids['refused']]
    assert (refused.state, refused.failure_cause) == (IngestRequestState.FAILED, IngestRequestFailureCause.PERMANENT)
    assert requests[ids['behind']].state == IngestRequestState.QUEUED, 'the pass waited, or the Submit was lost'

    await ingest.deliver_all()

    after = _entries(scratch_db.engine)
    assert set(after) == set(before), 'the refresh created or removed an entry'
    behind = after[ids['behind']]
    assert (behind.start, behind.end, behind.feed, behind.expiry) == (START, NullableDateTime.EPOCH, FAKE_FEED, None), (
        'the entry is not the declared one any more'
    )
    # FakeRead's grid from TAIL_START: 3 Oct .. 6 Oct 00:00Z, all before NOW.
    new_bars = [SEEDED[-1] + DAY * k for k in range(1, 5)]
    assert _bar_times(scratch_db.engine, ids['behind']) == SEEDED + new_bars
    for name in ('refused', 'retired'):
        assert _bar_times(scratch_db.engine, ids[name]) == SEEDED, f'{name} changed'
        assert after[ids[name]] == before[ids[name]]

    (attempt,) = _runs(scratch_db.engine)
    assert (attempt.dataset_id, attempt.state, attempt.bars_written) == (ids['behind'], IngestRunState.SUCCEEDED, 4)
    assert (attempt.trigger, attempt.actor, attempt.requested_start) == (
        IngestRunTrigger.SCHEDULE,
        SCHEDULER_ACTOR,
        TAIL_START,
    )
    assert _requests(scratch_db.engine)[ids['behind']].state == IngestRequestState.SUCCEEDED
    assert [e for e in point_refresh if isinstance(e, DatasetChanged)] == [
        DatasetChanged(ids['behind'], DatasetChange.UPDATED)
    ]

    # The next day's pass asks only after the new last bar, and still extends the same entry. That bar (6 Oct 00:00Z)
    # is the 5th's session, so the tail starts at the 6th's local midnight, 04:00Z.
    clock.now = NOW + timedelta(days=1)
    ingest.submitted.clear()
    await DailyRefresher(ingest, clock=clock).run_pass(DataSource.ALPACA_API)
    await ingest.deliver_all()

    second_tail = datetime(2026, 10, 6, 4, 0, tzinfo=UTC)
    assert [start for feed, start in ingest.asked() if feed == FAKE_FEED] == [second_tail]
    assert set(_entries(scratch_db.engine)) == set(before)
    assert _bar_times(scratch_db.engine, ids['behind']) == [*SEEDED, *new_bars, new_bars[-1] + DAY]


@pytest.mark.asyncio
async def test_a_refused_tape_is_skipped_by_the_next_pass_before_the_close_on_postgres(
    scratch_db, point_refresh, entry_values, bar_values, run_identity
):
    """The refused tape's request asked for the same tail and finished after the last close: the next pass skips it.

    The refusal writes no attempt row (no vendor call was made); finished_scheduled_runs reads the FAILED SCHEDULE
    request itself into the history (tj-7zjov1.160), over Postgres's ARRAY data_types. The SQLite twin is
    test_daily_refresh.py test_a_submit_refused_for_good_is_skipped_until_the_next_close.
    """
    scratch_db.must('upgrade', _head())
    ids = _seed(scratch_db.engine, entry_values, bar_values, run_identity)
    clock = Clock(NOW)
    ingest = _ingest(clock)
    await DailyRefresher(ingest, clock=clock).run_pass(DataSource.ALPACA_API)
    await ingest.deliver_all()

    clock.now = NOW + timedelta(hours=1)
    ingest.submitted.clear()
    second = await DailyRefresher(ingest, clock=clock).run_pass(DataSource.ALPACA_API)

    assert [feed for feed, _start in ingest.asked()] == [FAKE_FEED]
    assert second.skipped == 1
    refused_id = _requests(scratch_db.engine)[ids['refused']].id
    with scratch_db.engine.connect() as conn:
        refused_requests = conn.execute(
            sa.select(sa.func.count()).select_from(REQUEST).where(REQUEST.c.dataset_id == ids['refused'])
        ).scalar_one()
        attempts = conn.execute(
            sa.select(sa.func.count()).select_from(RUN).where(RUN.c.request_id == refused_id)
        ).scalar_one()
    assert (refused_requests, attempts) == (1, 0), 'the refused tape was re-sent, or its refusal wrote an attempt row'


# ---------------------------------------------------------------------------------------------
# Round 2 (fix 0b6d9d8), on the push path: an operator Stop racing the scheduled write. The delivery intake locks the
# entry FOR UPDATE (lock_active_dataset_entry) and judges it under that lock; a stopped or deleted entry DROPS the
# delivery. Its writes never touch expiry or collect. Since tj-grna9p.175 the Stop is PATCH .../collecting
# {collect: false}; an expiry PATCH is the deletion date only and drops nothing. Only Postgres has the row lock, so
# only here does the second case (a PATCH while the delivery holds the entry waits for its commit) mean anything. The
# PATCH is the real operator route (test_reaper_race's in-process app on the scratch database); the dataset is the
# operator's own, so the PATCH writes no override row and any override row would be the system actor's.

RETIRE_AT = datetime(2026, 10, 10, tzinfo=UTC)
STOP = {'collect': False}


def _stop(client: Any, dataset_id: uuid.UUID) -> Any:
    """The operator's retire: PATCH .../collecting with collect false (the dataset is the operator's own)."""
    return client.patch(
        f'/ui/v1/datasets/{dataset_id}/collecting', json=STOP, headers={race.INSTANCE_SECRET_HEADER: race.SECRET}
    )


def _operator_daily(engine: sa.Engine, entry_values, bar_values, run_identity) -> uuid.UUID:
    with engine.begin() as conn:
        row = conn.execute(
            sa.insert(ENTRY_TABLE)
            .values(
                entry_values(
                    asset_symbol=run_identity.alt_symbol('drrace'),
                    owner=OPERATOR_PRINCIPAL,
                    update_type=UpdateType.DAILY.value,
                    expiry_type=ExpiryType.ROLLING.value,
                    granularity=Granularity.ONE_DAY,
                    start=START,
                    end=NullableDateTime.EPOCH,
                )
            )
            .returning(ENTRY_TABLE)
        ).one()
        for timestamp in SEEDED:
            conn.execute(sa.insert(BAR_TABLE).values(bar_values(row, timestamp=timestamp)))
    return row.id


def _override_rows(engine: sa.Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(DatasetOverrideLog.__table__)).scalar_one()


@pytest.mark.asyncio
async def test_a_stop_that_commits_before_the_delivery_drops_the_answer_on_postgres(
    store, scratch_db, point_refresh, entry_values, bar_values, run_identity
):
    dataset_id = _operator_daily(scratch_db.engine, entry_values, bar_values, run_identity)
    clock = Clock(NOW)
    ingest = _ingest(clock)

    result = await DailyRefresher(ingest, clock=clock).run_pass(DataSource.ALPACA_API)
    # The operator's PATCH commits while the request is out, before ingest delivers.
    response = await _stop(store, dataset_id)
    await ingest.deliver_all()

    assert response.status_code == 200, response.text
    assert result == PassResult(behind=1, refreshed=1)
    entry = _entries(scratch_db.engine)[dataset_id]
    assert entry.collect is False, 'the system actor undid the operator retire'
    assert _bar_times(scratch_db.engine, dataset_id) == SEEDED, 'bars were written for a retired dataset'
    (run,) = _runs(scratch_db.engine)
    assert (run.state, run.bars_written, run.error_reason) == (IngestRunState.EMPTY, 0, None)
    request = _requests(scratch_db.engine)[dataset_id]
    assert (request.state, request.failure_cause) == (IngestRequestState.EMPTY, IngestRequestFailureCause.DISCARDED)
    assert _override_rows(scratch_db.engine) == 0
    assert not [e for e in point_refresh if isinstance(e, DatasetChanged)]


@pytest.mark.asyncio
async def test_a_stop_while_the_delivery_holds_the_entry_waits_for_it_and_is_kept_on_postgres(
    store, scratch_db, point_refresh, entry_values, bar_values, run_identity, monkeypatch
):
    dataset_id = _operator_daily(scratch_db.engine, entry_values, bar_values, run_identity)
    clock = Clock(NOW)
    ingest = _ingest(clock)
    locked, release = asyncio.Event(), asyncio.Event()
    # The delivery stops right after its locked judgement of the entry (touch_entry runs under the row lock), before
    # any bar, holding the lock.
    monkeypatch.setattr(
        delivery_intake, 'touch_entry', race._held(delivery_intake.touch_entry, locked, release, after=True)
    )

    result = await DailyRefresher(ingest, clock=clock).run_pass(DataSource.ALPACA_API)
    delivery = asyncio.create_task(ingest.deliver_all())
    patch = None
    try:
        await asyncio.wait_for(locked.wait(), race.WAIT_SECONDS)
        patch = asyncio.create_task(_stop(store, dataset_id))
        assert await race._a_backend_waits_on_a_lock(scratch_db), 'the PATCH did not wait on the delivery lock'
        assert not patch.done(), 'the PATCH completed while the delivery held the entry'
    finally:
        release.set()
        await delivery
        response = await patch if patch is not None else None

    assert response is not None and response.status_code == 200, response
    assert result == PassResult(behind=1, refreshed=1)
    entry = _entries(scratch_db.engine)[dataset_id]
    assert entry.collect is False, 'the stop that waited for the delivery was lost'
    assert _bar_times(scratch_db.engine, dataset_id) == SEEDED + [SEEDED[-1] + DAY * k for k in range(1, 5)]
    assert _override_rows(scratch_db.engine) == 0


@pytest.mark.asyncio
async def test_a_deletion_date_set_before_the_delivery_drops_nothing_on_postgres(
    store, scratch_db, point_refresh, entry_values, bar_values, run_identity
):
    """Decision tj-hhy3dk: an expiry PATCH while the request is out is the deletion date only; the answer is stored."""
    dataset_id = _operator_daily(scratch_db.engine, entry_values, bar_values, run_identity)
    clock = Clock(NOW)
    ingest = _ingest(clock)

    result = await DailyRefresher(ingest, clock=clock).run_pass(DataSource.ALPACA_API)
    response = await race._patch(store, dataset_id, {'expiry': RETIRE_AT.isoformat()})
    await ingest.deliver_all()

    assert response.status_code == 200, response.text
    assert result == PassResult(behind=1, refreshed=1)
    entry = _entries(scratch_db.engine)[dataset_id]
    assert (entry.expiry, entry.collect) == (RETIRE_AT, True), 'the refresh undid the expiry or an expiry stopped it'
    assert _bar_times(scratch_db.engine, dataset_id) == SEEDED + [SEEDED[-1] + DAY * k for k in range(1, 5)]


# ---------------------------------------------------------------------------------------------
# tj-grna9p.167: a newest bar written before its period closed is the vendor's part-way bar. The pass re-requests FROM
# that bar (inclusive), and the delivery's real INSERT ... ON CONFLICT DO UPDATE rewrites it: new OHLCV and an
# updated_at past the close, so the catalog's write time (the crud's newest-bar subquery, here on Postgres) then reads
# it final. Only this tier runs the upsert: on SQLite it is a recorder, so nothing there shows updated_at really moving.
#
# The bars here are stamped as the real vendor stamps a daily bar, at the session's LOCAL midnight (04:00Z under EDT),
# as the SQLite twin (server/data/store/tests/test_provisional_bar.py) does, and the double delivers the same
# stamps (vendor_stamps). FakeRead's UTC-midnight grid cannot carry this case: a bar at 7 Oct 00:00Z is the 6th's
# session but stamped AFTER its close, so a write before that close is a write before its own stamp, which the
# refresh judges by the lost-anchor rule (decision tj-7zjov1.120 addendum 1: the tail starts at the local midnight of
# the write time's period), not by the provisional reach-back this test pins.
#
# 6 Oct 04:00Z is the 6th's session (close 20:00Z). At 7 Oct 01:00Z the expected session is the 6th, the newest bar
# IS the 6th's, and it was written at 16:16Z, after its stamp and mid-session: LATE, not FRESH. The tail reaches back
# to that bar inclusively; the double serves nothing past its clock, so the tail is that one bar.

PROVISIONAL_NOW = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)
PROVISIONAL_BAR = datetime(2026, 10, 6, 4, 0, tzinfo=UTC)
MID_SESSION = datetime(2026, 10, 6, 16, 16, tzinfo=UTC)
OCT6_CLOSE = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)
assert PROVISIONAL_BAR < MID_SESSION < OCT6_CLOSE, 'the provisional bar must be written after its stamp, before close'


def _bar_row(engine: sa.Engine, dataset_id: uuid.UUID, timestamp: datetime) -> sa.Row:
    with engine.connect() as conn:
        return conn.execute(
            sa.select(BAR_TABLE).where(BAR_TABLE.c.dataset_id == dataset_id, BAR_TABLE.c.timestamp == timestamp)
        ).one()


async def _health(scratch_async: AsyncEngine, dataset_id: uuid.UUID, now: datetime):
    async with AsyncSession(scratch_async) as session:
        row = await get_catalog_row(session, dataset_id)
    assert row is not None
    health = evaluate_dataset(
        update_type=row.update_type,
        granularity=row.granularity,
        collect=row.collect,
        start=row.start,
        end=row.end or datetime(2100, 1, 1, tzinfo=UTC),
        last_bar=row.last_bar,
        covered=None,
        calendar=calendar_for_source(DataSource.ALPACA_API),
        now=now,
        last_bar_written_at=row.last_bar_written_at,
    )
    return row, health


@pytest.mark.asyncio
async def test_a_provisional_newest_bar_is_re_requested_and_rewritten_final_on_postgres(
    scratch_db, scratch_async, point_refresh, entry_values, bar_values, run_identity
):
    scratch_db.must('upgrade', _head())
    times = vendor_stamps(START, PROVISIONAL_BAR + DAY)  # 28 Sep .. 6 Oct 04:00Z, New York local midnights
    assert (len(times), times[-1]) == (9, PROVISIONAL_BAR)
    with scratch_db.engine.begin() as conn:
        row = conn.execute(
            sa.insert(ENTRY_TABLE)
            .values(
                entry_values(
                    asset_symbol=run_identity.alt_symbol('drprov'),
                    update_type=UpdateType.DAILY.value,
                    expiry_type=ExpiryType.ROLLING.value,
                    granularity=Granularity.ONE_DAY,
                    start=START,
                    end=NullableDateTime.EPOCH,
                )
            )
            .returning(ENTRY_TABLE)
        ).one()
        for timestamp in times[:-1]:
            conn.execute(sa.insert(BAR_TABLE).values(bar_values(row, timestamp=timestamp)))
        # The part-way bar: written mid-session, with a volume the vendor's final bar never has (FakeRead's >= 1000).
        conn.execute(
            sa.insert(BAR_TABLE).values(
                bar_values(row, timestamp=PROVISIONAL_BAR, volume=1, created_at=MID_SESSION, updated_at=MID_SESSION)
            )
        )
    dataset_id = row.id
    before = _bar_row(scratch_db.engine, dataset_id, PROVISIONAL_BAR)

    # The real Postgres subquery reads the newest bar's write time, and the dataset is LATE, not FRESH.
    catalog_row, health = await _health(scratch_async, dataset_id, PROVISIONAL_NOW)
    assert (catalog_row.last_bar, catalog_row.last_bar_written_at) == (PROVISIONAL_BAR, MID_SESSION)
    assert health.status is DatasetHealthStatus.LATE

    clock = Clock(PROVISIONAL_NOW)
    ingest = _ingest(clock, vendor_stamps)
    result = await DailyRefresher(ingest, clock=clock).run_pass(DataSource.ALPACA_API)
    assert result == PassResult(behind=1, refreshed=1)
    assert ingest.asked() == [(FAKE_FEED, PROVISIONAL_BAR)], 'the tail did not start at the provisional bar'
    await ingest.deliver_all()

    # Rewritten in place by ON CONFLICT DO UPDATE: the same row, the vendor's values, updated_at moved past the close
    # (the database's now(), the real wall clock), created_at untouched. No second bar.
    after = _bar_row(scratch_db.engine, dataset_id, PROVISIONAL_BAR)
    served = fake_bar(row.asset_symbol, PROVISIONAL_BAR)
    assert after.id == before.id
    assert (after.volume, after.close) == (served.volume, served.close)
    assert after.created_at == MID_SESSION
    assert after.updated_at > OCT6_CLOSE
    assert after.updated_at > before.updated_at
    assert _bar_times(scratch_db.engine, dataset_id) == times
    (run,) = _runs(scratch_db.engine)
    assert (run.requested_start, run.bars_written) == (PROVISIONAL_BAR, 1)

    # Final now: FRESH, and the next pass at the same clock asks for nothing.
    catalog_row, health = await _health(scratch_async, dataset_id, PROVISIONAL_NOW)
    assert catalog_row.last_bar_written_at == after.updated_at
    assert health.status is DatasetHealthStatus.FRESH
    ingest.submitted.clear()
    second = await DailyRefresher(ingest, clock=clock).run_pass(DataSource.ALPACA_API)

    assert second == PassResult()
    assert ingest.submitted == []
