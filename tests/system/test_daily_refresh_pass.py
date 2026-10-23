"""The interim DAILY refresh on real Postgres, fetching from FakeRead over real gRPC (validator, gating tj-grna9p.22).

The design is the architect's ruling on tj-utqd77 Q1 and its notes on tj-grna9p.22: the store triggers and ingest
sees an ORDINARY FetchDataset request; each pass derives its work from the database and asks only for the MISSING
TAIL of each behind, non-retired DAILY dataset, through the existing worker and run recorder (trigger SCHEDULE, actor
`scheduler`).

What only this tier shows, and is pinned here (data/store/tests/test_daily_refresh.py is the SQLite tier, where the
entry upsert and the bar write are Postgres statements that cannot run):
  * the narrowed request (fetch_start = last bar + one bar) resolves, through the real INSERT ... ON CONFLICT, to the
    SAME entry: one entry, the same id, the declared range unchanged, and the own-overlap check passes it as an exact
    repeat (no 409);
  * the new bars are stored on that entry and the old ones are untouched, and a second pass asks only from the new
    last bar (a restart re-requests only what is missing);
  * a refused ack (the stored tape is one this deployment cannot serve) leaves no second entry and no bar, and its
    run is FAILED with FEED_NOT_AVAILABLE;
  * the recorded runs round-trip trigger SCHEDULE, actor `scheduler` and the narrowed requested_start.

THE FAKE BROKER. This client container is not on the ingest_store network, so it cannot dial the stack's
data-ingest-grpc. The same thing is built here instead: data_ingest's own registered_services (the real FetchDataset
servicer and handler) around tests.fakes.market_data.FakeRead, hosted by the real GrpcServerHost on 127.0.0.1, and
reached through the real GrpcIngestFetchClient. The fake's clock is the pass's clock.

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

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.database.sql_alchemy_nullable_datetime import NullableDateTime
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import Reason
from common.rpc.channel import create_channel
from common.rpc.clients.ingest_fetch import GrpcIngestFetchClient
from common.rpc.server import BindAddress, GrpcServerHost
from data.ingest.app.grpc_host import registered_services
from data.store.app import daily_refresh as refresh_module
from data.store.app.client_label import OPERATOR_PRINCIPAL
from data.store.app.daily_refresh import SCHEDULER_ACTOR, DailyRefresher, PassResult
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.events import DatasetChange, DatasetChanged
from data.store.app.ingest import data_action_request, run_recording
from tests.fakes.market_data import FEED as FAKE_FEED
from tests.fakes.market_data import FakeRead
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
LOOPBACK = '127.0.0.1'

# Tue 6 Oct 2026 22:00Z: the 6th's XNYS session closed at 20:00Z. FakeRead stamps a daily bar at UTC midnight, which
# is the previous evening in New York, so a bar at 2 Oct 00:00Z belongs to the 1st's session and the dataset is behind.
NOW = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
DAY = Granularity.ONE_DAY.offset
START = datetime(2026, 9, 28, tzinfo=UTC)
SEEDED = [START + DAY * k for k in range(5)]  # 28 Sep .. 2 Oct, 00:00Z
UNSERVABLE_FEED = Feed.SIP
assert UNSERVABLE_FEED is not FAKE_FEED


class CountingRead(FakeRead):
    """FakeRead, counting the bar queries that reach the vendor side."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        super().__init__(clock=clock)
        self.queries: list[Any] = []

    async def get_bars(self, query, *args, **kwargs):
        self.queries.append(query)
        return await super().get_bars(query, *args, **kwargs)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


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
        'retired': {**daily, 'expiry': NOW + timedelta(days=1)},
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


def _asked(reader: CountingRead) -> list[tuple[Feed, datetime]]:
    return sorted((query.feed, query.start) for query in reader.queries)


def _runs(engine: sa.Engine) -> list[sa.Row]:
    with engine.connect() as conn:
        return list(conn.execute(sa.select(RUN).order_by(RUN.c.started_at)))


@pytest.fixture
def point_refresh(monkeypatch: pytest.MonkeyPatch, scratch_async: AsyncEngine) -> list[Any]:
    """The refresh's and the run recorder's own_session on the scratch database; returns the events published."""

    @asynccontextmanager
    async def own_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(scratch_async, expire_on_commit=False) as session:
            yield session

    published: list[Any] = []
    monkeypatch.setattr(refresh_module, 'own_session', own_session)
    monkeypatch.setattr(run_recording, 'own_session', own_session)
    monkeypatch.setattr(refresh_module, 'publish', published.append)
    monkeypatch.setattr(run_recording, 'publish', published.append)
    monkeypatch.delenv(refresh_module.MAX_PER_PASS_ENV, raising=False)
    return published


@asynccontextmanager
async def _ingest(clock: Clock) -> AsyncIterator[tuple[GrpcIngestFetchClient, CountingRead]]:
    """data_ingest's FetchDataset servicer around FakeRead, on loopback, and the store's real client to it."""
    reader = CountingRead(clock)
    async with GrpcServerHost(BindAddress(LOOPBACK, 0), registered_services({DataSource.ALPACA_API: reader})) as host:
        channel = create_channel(f'{LOOPBACK}:{host.port}')
        try:
            yield GrpcIngestFetchClient(channel, deadline_s=30), reader
        finally:
            await channel.close()


@pytest.mark.asyncio
async def test_a_refresh_extends_the_same_entry_with_only_the_missing_tail_on_postgres(
    scratch_db, point_refresh, entry_values, bar_values, run_identity
):
    scratch_db.must('upgrade', _head())
    ids = _seed(scratch_db.engine, entry_values, bar_values, run_identity)
    before = _entries(scratch_db.engine)
    clock = Clock(NOW)

    async with _ingest(clock) as (client, reader):
        result = await DailyRefresher(client, clock=clock).run_pass(DataSource.ALPACA_API)

        # FEED_NOT_AVAILABLE is REFUSED in the vocabulary: a permanent refusal, not a transient failure.
        assert result == PassResult(behind=2, refreshed=1, failed=0, deferred=0, refused=1)
        # Retired is never asked for. Each request names the stored tape and starts at the missing tail.
        assert _asked(reader) == sorted([(FAKE_FEED, SEEDED[-1] + DAY), (UNSERVABLE_FEED, SEEDED[-1] + DAY)])

        after = _entries(scratch_db.engine)
        assert set(after) == set(before), 'the refresh created or removed an entry'
        behind = after[ids['behind']]
        assert (behind.start, behind.end, behind.feed, behind.expiry) == (
            START,
            NullableDateTime.EPOCH,
            FAKE_FEED,
            None,
        ), 'the entry is not the declared one any more'
        new_bars = [SEEDED[-1] + DAY * k for k in range(1, 5)]  # 3 Oct .. 6 Oct 00:00Z, all before NOW
        assert _bar_times(scratch_db.engine, ids['behind']) == SEEDED + new_bars
        for name in ('refused', 'retired'):
            assert _bar_times(scratch_db.engine, ids[name]) == SEEDED, f'{name} changed'
            assert after[ids[name]] == before[ids[name]]

        runs = _runs(scratch_db.engine)
        assert len(runs) == 2
        ok = next(run for run in runs if run.state == IngestRunState.SUCCEEDED)
        refused = next(run for run in runs if run.state == IngestRunState.FAILED)
        for run in runs:
            assert run.trigger == IngestRunTrigger.SCHEDULE
            assert run.actor == SCHEDULER_ACTOR
            assert run.requested_end is None
        assert (ok.dataset_id, ok.bars_written, ok.requested_start) == (ids['behind'], 4, SEEDED[-1] + DAY)
        assert (refused.dataset_id, refused.error_reason, refused.requested_start) == (
            None,
            str(Reason.FEED_NOT_AVAILABLE),
            SEEDED[-1] + DAY,
        )
        assert [e for e in point_refresh if isinstance(e, DatasetChanged)] == [
            DatasetChanged(ids['behind'], DatasetChange.UPDATED)
        ]

        # The next day's pass asks only from the new last bar, and still extends the same entry.
        clock.now = NOW + timedelta(days=1)
        reader.queries.clear()
        second = await DailyRefresher(client, clock=clock).run_pass(DataSource.ALPACA_API)

    # The refused tape's last scheduled attempt asked for the same tail and finished (server clock) after the last
    # close, so it is skipped; matched on the request's fields, since it failed before the ack.
    assert second == PassResult(behind=2, refreshed=1, skipped=1)
    assert _asked(reader) == [(FAKE_FEED, new_bars[-1] + DAY)]
    assert set(_entries(scratch_db.engine)) == set(before)
    assert _bar_times(scratch_db.engine, ids['behind']) == [*SEEDED, *new_bars, new_bars[-1] + DAY]


# ---------------------------------------------------------------------------------------------
# Round 2 (fix 0b6d9d8): an operator retire racing the scheduled write. The refresh locks the entry FOR UPDATE on
# the ack and re-checks it; its upsert never writes expiry. Only Postgres has the row lock, so only here does the
# second case (a PATCH after the ack waits for the refresh's commit) mean anything. The PATCH is the real operator
# route (test_reaper_race's in-process app on the scratch database); the dataset is the operator's own, so the PATCH
# writes no override row and any override row would be the system actor's.

RETIRE_AT = datetime(2026, 10, 10, tzinfo=UTC)


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
async def test_a_retire_that_commits_during_the_vendor_call_drops_the_answer_on_postgres(
    store, scratch_db, point_refresh, entry_values, bar_values, run_identity
):
    dataset_id = _operator_daily(scratch_db.engine, entry_values, bar_values, run_identity)
    clock = Clock(NOW)
    responses: list[Any] = []

    async with _ingest(clock) as (client, reader):
        served = reader.get_bars

        async def retire_then_serve(query, *args, **kwargs):
            # The operator's PATCH commits while the vendor call is in flight, before ingest's ack.
            responses.append(await race._patch(store, dataset_id, {'expiry': RETIRE_AT.isoformat()}))
            return await served(query, *args, **kwargs)

        reader.get_bars = retire_then_serve
        result = await DailyRefresher(client, clock=clock).run_pass(DataSource.ALPACA_API)

    (response,) = responses
    assert response.status_code == 200, response.text
    assert result == PassResult(behind=1, discarded=1)
    entry = _entries(scratch_db.engine)[dataset_id]
    assert entry.expiry == RETIRE_AT, 'the system actor undid the operator retire'
    assert _bar_times(scratch_db.engine, dataset_id) == SEEDED, 'bars were written for a retired dataset'
    (run,) = _runs(scratch_db.engine)
    assert (run.state, run.bars_written, run.error_reason) == (IngestRunState.EMPTY, 0, None)
    assert _override_rows(scratch_db.engine) == 0
    assert not [e for e in point_refresh if isinstance(e, DatasetChanged)]


@pytest.mark.asyncio
async def test_a_retire_after_the_ack_waits_for_the_refresh_and_is_kept_on_postgres(
    store, scratch_db, point_refresh, entry_values, bar_values, run_identity, monkeypatch
):
    dataset_id = _operator_daily(scratch_db.engine, entry_values, bar_values, run_identity)
    clock = Clock(NOW)
    upserted, release = asyncio.Event(), asyncio.Event()
    # The scheduled write stops right after its locked re-check and upsert, holding the row lock, before any bar.
    monkeypatch.setattr(
        data_action_request,
        'upsert_entry_in_transaction',
        race._held(data_action_request.upsert_entry_in_transaction, upserted, release, after=True),
    )

    async with _ingest(clock) as (client, _reader):
        refresh = asyncio.create_task(DailyRefresher(client, clock=clock).run_pass(DataSource.ALPACA_API))
        patch = None
        try:
            await asyncio.wait_for(upserted.wait(), race.WAIT_SECONDS)
            patch = asyncio.create_task(race._patch(store, dataset_id, {'expiry': RETIRE_AT.isoformat()}))
            assert await race._a_backend_waits_on_a_lock(scratch_db), 'the PATCH did not wait on the refresh lock'
            assert not patch.done(), 'the PATCH completed while the refresh held the entry'
        finally:
            release.set()
            result = await refresh
            response = await patch if patch is not None else None

    assert response is not None and response.status_code == 200, response
    assert result == PassResult(behind=1, refreshed=1)
    entry = _entries(scratch_db.engine)[dataset_id]
    assert entry.expiry == RETIRE_AT, 'the PATCH that waited for the refresh lost its expiry'
    assert _bar_times(scratch_db.engine, dataset_id) == SEEDED + [SEEDED[-1] + DAY * k for k in range(1, 5)]
    assert _override_rows(scratch_db.engine) == 0
