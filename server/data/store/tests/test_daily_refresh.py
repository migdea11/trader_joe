"""The interim DAILY refresh (app/daily_refresh.py), driven over real rows in SQLite (validator, gating tj-grna9p.22).

THE DESIGN is the architect's ruling on tj-utqd77 Q1 and its notes on tj-grna9p.22 (2026-10-13), which win over the
bead's body: the STORE triggers; INTERIM until ingest's scheduler (tj-pd41mt Addendum 1) lands; one `run_periodic` per
source with a trading calendar on `AfterTradingEvent(calendar, CLOSE, settle)`; every pass derives its work from the
DATABASE (no job store, no replay); only the MISSING TAIL of each behind, non-retired DAILY dataset is requested, one
dataset at a time, capped per pass, with bounded backoff; a retired subscription is never refreshed (since
tj-grna9p.175, decision tj-hhy3dk: retired is collect false and the expiry is the deletion date only, so setting or
clearing it stops or resumes nothing); a dataset with a RUNNING ingest is skipped; one worker only.

REPAIRED FOR THE PUSH PATH (validator, tj-7zjov1.28, P35). The pass no longer opens a FetchDataset stream and writes
nothing but the REQUEST: for each owed dataset it writes an ingest_request row (trigger SCHEDULE, dataset_id the
entry's, ready_by the next market open, actor `scheduler`, no expiry, collect or expiry type) and SUBMITS it, and it
does NOT wait. Ingest pushes the result to the store's delivery intake, which writes the bars and the attempt rows.
So every assertion that read what the pass's fetch wrote now reads what it SUBMITTED and the REQUEST ROW it wrote; the
ones that read a delivered result drive the REAL DeliveryIntake over the same tables (World.deliver), as ingest
would. "Running" is an OPEN REQUEST (PENDING, QUEUED, NOT_DELIVERED) as well as a legacy RUNNING run, so a dataset
the pass submitted is not owed again until its request finishes. What moved out of the pass entirely is named at the
test that used to pin it.

WHAT TIER THIS IS. The real pass, the real catalog read, the real Running read, the real submit path
(data_action_request.submit_bars, its own-overlap check and its request writes) and the real delivery intake, over
real tables in in-memory SQLite, with the real XNYS calendar and an injected clock and sleep. The Submit client is a
double (ScriptedClient) scripted per symbol. The bar write is a Postgres INSERT ... ON CONFLICT and is replaced by a
recorder. That Postgres resolves the refresh to the SAME entry is tests/system/test_daily_refresh_pass.py's.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import delete, event, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import ERROR_DOMAIN, ExogenousError, InvalidRequestError, Reason
from common.scheduling import AfterTradingEvent, TradingEvent
from data.store.app import app_depends
from data.store.app import daily_refresh as refresh_module
from data.store.app.daily_refresh import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_MAX_PER_PASS,
    DEFAULT_SETTLE_MINUTES,
    SCHEDULER_ACTOR,
    DailyRefresher,
    PassResult,
    _fetch_start,
)
from data.store.app.database.crud.ingest_request import get_request
from data.store.app.database.crud.stock.dataset_catalog import CatalogRow
from data.store.app.database.crud.stock.store_dataset_entry import OwnOverlapConflict
from data.store.app.database.models.ingest_request import (
    IngestRequest,
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.events import DatasetChange, DatasetChanged
from data.store.app.ingest import data_action_request, delivery_intake
from data.store.app.main import app
from data.store.app.trading_calendar import calendar_for_source
from data.store.tests.delivery_reports import INSTANCE_A, bar_page, report, stats, with_trailer
from data.store.tests.name_change_doubles import Plays, install_book
from data.store.tests.request_tables import create_request_tables
from data.store.tests.test_ui_datasets_route import (  # noqa: F401 (aware_sqlite_datetimes is a fixture)
    NOW,
    _sqlite_timezone,
    aware_sqlite_datetimes,
    bar,
    entry,
    ny_midnight,
    run_of,
)
from schemas.data_ingest.delivery import DeliveryErrorClass, DeliveryOutcome, DeliveryVerdict
from schemas.data_ingest.fetch_dataset import FetchRefused
from schemas.data_ingest.submit import BarsRequest, SubmitAccepted, SubmitAck, SubmitRequest, SubmitResponse
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath


pytestmark = pytest.mark.data_store

# Distinctive, so their absence from a log line is evidence and not luck.
SECRET_OWNER = 'strategy-refresh-owner-5521'
SYMBOL_PREFIX = 'DRZ'
# The early retry pass's variable, removed with the pass itself in 4e42182 (decision tj-grna9p.55: no automatic
# retry). Spelled out here because the module no longer names it: setting it must change nothing.
REMOVED_RETRY_ENV = 'DAILY_REFRESH_RETRY_MINUTES'

# NOW is Tue 6 Oct 2026 22:00Z: the 6th's session closed at 20:00Z, so the last completed session is the 6th.
# BEHIND, oldest newest-bar first: `older` (last bar Wed 30 Sep), `empty` (no bars, declared start Thu 1 Oct, so it
# sorts on its start), `behind2` (last bar Fri 2 Oct), `behind` (last bar Mon 5 Oct, LATE). NOT BEHIND: `fresh`
# (last bar the 6th), `retired` (collect false: the operator stopped it; decision tj-hhy3dk, an expiry retires
# nothing), `running` (a RUNNING ingest), `static` (a STATIC dataset with gaps: not a subscription).
BEHIND_ORDER = ('older', 'empty', 'behind2', 'behind')
NOT_BEHIND = ('fresh', 'retired', 'running', 'static')
LAST_BAR = {
    'older': ny_midnight(30, 9),
    'behind2': ny_midnight(2),
    'behind': ny_midnight(5),
    'fresh': ny_midnight(6),
    'retired': ny_midnight(1),
    'running': ny_midnight(1),
}
# Where each behind dataset's tail starts, WRITTEN OUT rather than computed from LAST_BAR: the local midnight of the
# day after its last 1day bar (tj-grna9p.154). `behind2`'s last bar is Fri 2 Oct, so its tail is Sat 3 Oct: the step
# is by local date, not to the next session. `empty` has no bars and starts at its declared start.
TAIL = {'older': ny_midnight(1), 'empty': ny_midnight(1), 'behind2': ny_midnight(3), 'behind': ny_midnight(6)}
# The DAILY loop's ready_by (ADR tj-26jt9w addendum of 2026-10-24 (1) 3): the open of the first session after NOW,
# Wed 7 Oct 09:30 New York (EDT), written out.
NEXT_OPEN = datetime(2026, 10, 7, 13, 30, tzinfo=UTC)
# A vendor call made just before NOW, for the attempt rows World.deliver writes: inside the refresh's lookback.
CALLED_AT = NOW - timedelta(minutes=5)


def _seed() -> dict[str, StoreDatasetEntry]:
    daily = {'update_type': UpdateType.DAILY}
    rows = {
        'older': entry(f'{SYMBOL_PREFIX}A', **daily),
        'empty': entry(f'{SYMBOL_PREFIX}B', start=ny_midnight(1), **daily),
        'behind2': entry(f'{SYMBOL_PREFIX}C', feed=Feed.SIP, **daily),
        'behind': entry(f'{SYMBOL_PREFIX}D', **daily),
        'fresh': entry(f'{SYMBOL_PREFIX}E', **daily),
        'retired': entry(f'{SYMBOL_PREFIX}F', collect=False, **daily),
        'running': entry(f'{SYMBOL_PREFIX}G', **daily),
        'static': entry(f'{SYMBOL_PREFIX}H', start=ny_midnight(1, 9), end=ny_midnight(6)),
    }
    rows['behind'].owner = SECRET_OWNER
    return rows


class World:
    """The engine, the seeded rows, the bars the intake asked to write, every event, and the sleeps."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.rows: dict[str, StoreDatasetEntry] = {}
        self.bar_batches: list[Any] = []
        self.events: list[Any] = []
        self.sleeps: list[float] = []
        self.intake = delivery_intake.DeliveryIntake(chunk_size=100)

    def name_of(self, symbol: str) -> str:
        return next(name for name, row in self.rows.items() if row.asset_symbol == symbol)

    def requested(self, client: 'ScriptedClient') -> list[str]:
        return [self.name_of(request.asset_symbol) for request in client.requests]

    async def runs(self) -> list[IngestRun]:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            return list((await db.execute(select(IngestRun).where(IngestRun.dataset_id.is_not(None)))).scalars())

    async def requests(self) -> list[IngestRequest]:
        """Every ingest_request row, oldest first."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            statement = select(IngestRequest).order_by(IngestRequest.created_at, IngestRequest.requested_start)
            return list((await db.execute(statement)).scalars())

    async def request_of(self, name: str) -> IngestRequest:
        """The one request row naming this dataset's symbol."""
        (row,) = [row for row in await self.requests() if row.asset_symbol == self.rows[name].asset_symbol]
        return row

    async def deliver(
        self,
        name: str,
        outcome: DeliveryOutcome = DeliveryOutcome.SUCCEEDED,
        *,
        bars: bool = True,
        attempt_no: int = 1,
        finished_at: datetime = NOW,
        last_attempt: bool = False,
        error_class: DeliveryErrorClass = DeliveryErrorClass.TRANSIENT,
        called_at: datetime = CALLED_AT,
    ) -> DeliveryVerdict:
        """Report one attempt of this dataset's open request to the REAL delivery intake, as ingest would."""
        (request,) = [
            row
            for row in await self.requests()
            if row.asset_symbol == self.rows[name].asset_symbol
            and row.state in IngestRequestState.__members__.values()
            and IngestRequestState(row.state).is_open
        ]
        called = stats()
        called = called.model_copy(update={'first_call_at': called_at, 'last_call_at': called_at})
        parts = [bar_page(0)] if bars and outcome is DeliveryOutcome.SUCCEEDED else []
        attempt = report(
            request.id,
            outcome,
            attempt_no=attempt_no,
            parts=parts,
            vendor_stats=called,
            last_attempt=last_attempt,
            error_class=error_class,
        )
        answer = await self.intake.deliver(with_trailer(attempt, as_of=finished_at))
        return answer.verdict

    async def set_expiry(self, name: str, expiry: datetime | None) -> None:
        async with AsyncSession(self.engine) as db:
            await db.execute(
                update(StoreDatasetEntry).where(StoreDatasetEntry.id == self.rows[name].id).values(expiry=expiry)
            )
            await db.commit()

    async def set_collect(self, name: str, collect: bool) -> None:
        async with AsyncSession(self.engine) as db:
            await db.execute(
                update(StoreDatasetEntry).where(StoreDatasetEntry.id == self.rows[name].id).values(collect=collect)
            )
            await db.commit()

    async def finish_running(self) -> None:
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(IngestRun).where(IngestRun.state == IngestRunState.RUNNING))
            await db.commit()

    def refresher(self, client: 'ScriptedClient', now: datetime = NOW) -> DailyRefresher:
        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        return DailyRefresher(client, clock=lambda: now, sleep=sleep)


class ScriptedClient:
    """The Submit seam's double, scripted per symbol; any other symbol is ACCEPTED by INSTANCE_A on the tape it named.

    A script is an exception (the call fails, as an unreachable peer or a typed transport error does) or a FetchRefused
    (ingest answers and refuses the request: a feed it cannot serve). `requests` holds each Submit's bars request, in
    order, so the assertions that read what ingest was asked for read the same fields as before the push path;
    `submitted` holds the whole SubmitRequest (owner, ready_by, attempt count).
    """

    def __init__(self, scripts: dict[str, BaseException | FetchRefused] | None = None) -> None:
        self.scripts = scripts or {}
        self.submitted: list[SubmitRequest] = []
        self.requests: list[BarsRequest] = []
        # A POST's name_change query (P51) is recorded here, apart; the refresh never sends one. on_submit is the hook
        # name_change_doubles.Plays takes over to answer it.
        self.queries: list[SubmitRequest] = []
        self.on_submit: Any = None

    async def submit(self, request: SubmitRequest) -> SubmitResponse:
        if request.corporate_actions is not None:
            self.queries.append(request)
            if self.on_submit is not None:
                await self.on_submit(request)
            accepted = SubmitAccepted(feed=None, quota_key='quota-key')
            return SubmitResponse(ack=SubmitAck(accepted=accepted), instance_id=INSTANCE_A)
        self.submitted.append(request)
        self.requests.append(request.bars)
        script = self.scripts.get(request.bars.asset_symbol)
        if isinstance(script, BaseException):
            raise script
        if isinstance(script, FetchRefused):
            return SubmitResponse(ack=SubmitAck(refused=script), instance_id=INSTANCE_A)
        accepted = SubmitAccepted(feed=request.bars.feed or Feed.IEX, quota_key='quota-key')
        return SubmitResponse(ack=SubmitAck(accepted=accepted), instance_id=INSTANCE_A)

    async def instance(self) -> Any:
        raise AssertionError('the refresh never probes the instance')


def _engine() -> AsyncEngine:
    engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, 'connect')
    def register(dbapi_connection, _record):
        dbapi_connection.run_async(lambda c: c.create_function('timezone', 2, _sqlite_timezone))
        dbapi_connection.run_async(lambda c: c.create_function('gen_random_uuid', 0, lambda: uuid.uuid4().hex))

    return engine


@pytest_asyncio.fixture
async def world(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    request.getfixturevalue('aware_sqlite_datetimes')
    # SQLite has no ARRAY: ingest_request.data_types binds and reads as JSON for this test only. The request path's
    # statements are built on the model's table, so the model's column is swapped (and restored by monkeypatch).
    monkeypatch.setattr(IngestRequest.__table__.c.data_types, 'type', sa.JSON())
    engine = _engine()
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: StoreDatasetEntry.metadata.create_all(
                sync, tables=[StoreDatasetEntry.__table__, StockMarketActivity.__table__, IngestRun.__table__]
            )
        )
        await connection.run_sync(create_request_tables)
    state = World(engine)
    state.rows = _seed()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        db.add_all(state.rows.values())
        await db.flush()
        db.add_all(bar(state.rows[name], at) for name, at in LAST_BAR.items())
        db.add(run_of(state.rows['running'], minutes_ago=2, state=IngestRunState.RUNNING))
        await db.commit()

    @asynccontextmanager
    async def own_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db

    async def write_bars(_db: AsyncSession, batch: Any, requested_chunk_size: int | None = None) -> int:
        state.bar_batches.append(batch)
        return sum(len(points) for points in batch.dataset.values())

    for env in (
        refresh_module.ENABLED_ENV,
        refresh_module.SETTLE_ENV,
        refresh_module.MAX_PER_PASS_ENV,
        refresh_module.BACKOFF_ENV,
        REMOVED_RETRY_ENV,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(refresh_module, 'own_session', own_session)
    monkeypatch.setattr(data_action_request, 'own_session', own_session)
    monkeypatch.setattr(delivery_intake, 'own_session', own_session)
    monkeypatch.setattr(delivery_intake, 'publish', state.events.append)
    monkeypatch.setattr(delivery_intake, 'write_market_activity_in_transaction', write_bars)
    try:
        yield state
    finally:
        await engine.dispose()


def _fails(reason: Reason = Reason.VENDOR_UNAVAILABLE) -> ExogenousError:
    return ExogenousError(reason, 'the vendor is not there')


def _refused(reason: Reason = Reason.FEED_NOT_AVAILABLE) -> FetchRefused:
    """Ingest's refused ack: it answered, and will not take the request (today only a feed it cannot serve)."""
    return FetchRefused(reason=str(reason), domain=ERROR_DOMAIN, detail='refused', metadata={})


def _symbol(world: World, name: str) -> str:
    return world.rows[name].asset_symbol


# ------------------------------------------------------------------------------------------------- what is behind


@pytest.mark.asyncio
async def test_a_pass_requests_only_the_behind_active_datasets_oldest_first(world: World):
    """Retired (an expiry AHEAD), running, fresh and STATIC are skipped; the rest go oldest newest-bar first."""
    client = ScriptedClient()

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API, calendar_for_source(DataSource.ALPACA_API))

    assert world.requested(client) == list(BEHIND_ORDER)
    assert not set(world.requested(client)) & set(NOT_BEHIND)
    assert result == PassResult(behind=4, refreshed=4, failed=0, deferred=0)


@pytest.mark.asyncio
async def test_the_calendar_is_looked_up_when_omitted(world: World):
    client = ScriptedClient()

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert result.behind == 4


@pytest.mark.asyncio
async def test_a_source_without_datasets_does_nothing(world: World):
    """The pass reads only its own source's DAILY entries."""
    client = ScriptedClient()

    result = await world.refresher(client).run_pass(DataSource.IB_API, calendar_for_source(DataSource.ALPACA_API))

    assert client.requests == []
    assert await world.requests() == []
    assert result == PassResult()


@pytest.mark.asyncio
async def test_nothing_is_behind_before_the_session_closes(world: World):
    """At 19:00Z on the 6th the last completed session is the 5th: `behind` (last bar the 5th) is not owed."""
    client = ScriptedClient()

    await world.refresher(client, now=datetime(2026, 10, 6, 19, 0, tzinfo=UTC)).run_pass(DataSource.ALPACA_API)

    assert 'behind' not in world.requested(client)
    assert world.requested(client) == ['older', 'empty', 'behind2']


async def _finish_open_requests(world: World) -> None:
    """Deliver an empty success for every open request, so the next pass sees the datasets as no longer running."""
    for row in await world.requests():
        if IngestRequestState(row.state).is_open:
            await world.deliver(world.name_of(row.asset_symbol), bars=False, finished_at=NOW - timedelta(hours=3))


@pytest.mark.asyncio
async def test_a_stopped_subscription_is_never_refreshed_and_a_started_one_resumes(world: World):
    """Decision tj-hhy3dk: collect false stops the refresh, whatever the expiry says; collect true resumes it."""
    for expiry in (None, NOW + timedelta(days=30), NOW - timedelta(hours=1)):
        await world.set_expiry('retired', expiry)
        client = ScriptedClient()
        await world.refresher(client).run_pass(DataSource.ALPACA_API)
        assert 'retired' not in world.requested(client), f'stopped with expiry {expiry} was refreshed'
        await _finish_open_requests(world)

    await world.set_collect('retired', True)
    client = ScriptedClient()
    await world.refresher(client).run_pass(DataSource.ALPACA_API)
    assert 'retired' in world.requested(client), 'collect true did not resume the refresh'
    await _finish_open_requests(world)

    await world.set_collect('behind', False)
    client = ScriptedClient()
    await world.refresher(client).run_pass(DataSource.ALPACA_API)
    assert 'behind' not in world.requested(client), 'collect false did not stop the refresh'


@pytest.mark.asyncio
@pytest.mark.parametrize('expiry', [NOW + timedelta(days=30), NOW - timedelta(hours=1)], ids=['future', 'past'])
async def test_an_expiry_alone_never_stops_the_refresh(world: World, expiry):
    """The expiry is the deletion date only: a collecting DAILY dataset with one, future or past, is still refreshed."""
    await world.set_expiry('behind', expiry)
    client = ScriptedClient()
    await world.refresher(client).run_pass(DataSource.ALPACA_API)
    assert 'behind' in world.requested(client), f'an expiry {expiry} stopped a collecting subscription'


@pytest.mark.asyncio
async def test_clearing_an_expiry_never_resumes_a_stopped_subscription(world: World):
    await world.set_expiry('retired', NOW + timedelta(days=3))
    await world.set_expiry('retired', None)
    client = ScriptedClient()
    await world.refresher(client).run_pass(DataSource.ALPACA_API)
    assert 'retired' not in world.requested(client), 'clearing an expiry resumed a stopped subscription'


@pytest.mark.asyncio
async def test_a_dataset_with_a_running_ingest_is_skipped_and_picked_up_when_it_ends(world: World):
    client = ScriptedClient()
    await world.refresher(client).run_pass(DataSource.ALPACA_API)
    assert 'running' not in world.requested(client)

    await world.finish_running()
    await _finish_open_requests(world)
    client = ScriptedClient()
    await world.refresher(client).run_pass(DataSource.ALPACA_API)
    assert 'running' in world.requested(client)


@pytest.mark.asyncio
async def test_a_dataset_with_an_open_request_is_not_requested_again_until_it_finishes(world: World):
    """The push path's Running (tj-7zjov1.28): the pass does not wait, so its own open request is what stops a second.

    A pass right after the first sends nothing for the datasets it already submitted (their requests are QUEUED);
    once the delivery intake finishes one, that dataset is owed again (its bars were not stored by this double, so
    it is still behind) and only that one is requested.
    """
    first = ScriptedClient()
    await world.refresher(first).run_pass(DataSource.ALPACA_API)
    assert world.requested(first) == list(BEHIND_ORDER)

    again = ScriptedClient()
    result = await world.refresher(again).run_pass(DataSource.ALPACA_API)
    assert again.requests == [], 'a dataset with an open request was submitted a second time'
    assert result == PassResult()

    await world.deliver('behind2', bars=False, finished_at=LAST_CLOSE - timedelta(hours=1))
    after = ScriptedClient()
    await world.refresher(after).run_pass(DataSource.ALPACA_API)
    assert world.requested(after) == ['behind2']


# ------------------------------------------------------------------------------------------------- the request


@pytest.mark.asyncio
async def test_each_fetch_asks_only_for_the_missing_tail_with_the_stored_feed(world: World):
    """From the last bar plus one bar (the declared start when there are no bars), open-ended, the stored tape named."""
    client = ScriptedClient()

    await world.refresher(client).run_pass(DataSource.ALPACA_API)

    by_name = {world.name_of(request.bars.asset_symbol): request for request in client.submitted}
    for name in BEHIND_ORDER:
        row, submitted = world.rows[name], by_name[name]
        request = submitted.bars
        assert request.start == TAIL[name], name
        assert request.end is None, name
        assert request.feed is row.feed, f'{name}: the stored feed is not the preference sent'
        assert request.update_type is UpdateType.DAILY
        assert request.granularity is row.granularity
        assert request.asset_type is row.asset_type
        assert request.data_types == [row.data_type]
        # The Submit's owner is the request row's label: for the refresh, the scheduler, whatever owner the dataset
        # has (the entry exists already; the refresh never writes its owner).
        assert submitted.owner == SCHEDULER_ACTOR
        assert submitted.ready_by == NEXT_OPEN, f'{name}: ready_by is not the next market open'
        assert submitted.recovered is False
        assert submitted.attempt_count == 0
    assert by_name['behind2'].bars.feed is Feed.SIP


@pytest.mark.asyncio
async def test_each_refresh_writes_a_schedule_request_naming_its_dataset_before_it_submits(world: World):
    """THE REQUEST ROW IS THE REFRESH'S RECORD (tj-7zjov1.28; note H on the bead, the DAILY writer).

    Trigger SCHEDULE, the entry's dataset_id (so the first delivered attempt refreshes THAT entry and never creates
    one), ready_by the next market open, the scheduler as its label, the tail as its window, and NONE of the POST's
    lifecycle fields (expiry, collect, expiry type): the refresh never writes one. After the accepted Submit the row
    is QUEUED under the answering instance. The row is written BEFORE the Submit: the double reads it back by the
    request id it is handed.
    """
    seen_before_submit: list[IngestRequestState] = []

    class ReadsTheRow(ScriptedClient):
        async def submit(self, request: SubmitRequest) -> SubmitResponse:
            async with AsyncSession(world.engine) as db:
                row = await db.get(IngestRequest, uuid.UUID(request.request_id))
                seen_before_submit.append(None if row is None else IngestRequestState(row.state))
            return await super().submit(request)

    client = ReadsTheRow()
    await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert seen_before_submit == [IngestRequestState.PENDING] * len(BEHIND_ORDER)
    rows = {world.name_of(row.asset_symbol): row for row in await world.requests()}
    assert set(rows) == set(BEHIND_ORDER)
    for name in BEHIND_ORDER:
        row, entry_row = rows[name], world.rows[name]
        assert row.kind == IngestRequestKind.BARS
        assert row.trigger is IngestRunTrigger.SCHEDULE, name
        assert row.dataset_id == entry_row.id, f'{name}: the request does not name its dataset'
        assert row.client_label == SCHEDULER_ACTOR
        assert row.ready_by == NEXT_OPEN
        assert row.requested_start == TAIL[name]
        assert row.requested_end is None
        assert row.feed is entry_row.feed
        assert (row.expiry, row.collect, row.expiry_type) == (None, None, None), f'{name}: a lifecycle field'
        assert IngestRequestState(row.state) is IngestRequestState.QUEUED
        assert row.instance_id == INSTANCE_A
    assert {submitted.request_id for submitted in client.submitted} == {str(row.id) for row in rows.values()}


@pytest.mark.asyncio
async def test_the_pass_does_not_wait_for_the_delivery(world: World):
    """The DAILY loop submits and moves on: every request is still open when the pass returns, no bar is written."""
    client = ScriptedClient()

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert result.refreshed == len(BEHIND_ORDER)
    assert all(IngestRequestState(row.state).is_open for row in await world.requests())
    assert world.bar_batches == []
    assert await _attempt_rows(world) == [], 'the pass wrote an attempt row itself'


@pytest.mark.asyncio
async def test_the_refresh_never_writes_the_entry(world: World):
    """The declared range stays the identity: the pass writes no entry, and the delivery writes under the named one.

    Before the push path the refresh's fetch upserted the entry with its DECLARED start (so the narrowed tail never
    reached the overlap key) and preserve_lifecycle. Now the request NAMES the entry (dataset_id) and the intake's
    bars arm only locks it and touches updated_at: no upsert, so no second entry and no lifecycle write can happen.
    Delivering one dataset's tail writes its bars under the existing id and leaves the table's rows as they were.
    """
    before = await _entries(world)
    client = ScriptedClient()

    await world.refresher(client).run_pass(DataSource.ALPACA_API)
    assert await _entries(world) == before, 'the pass wrote an entry'

    assert await world.deliver('behind') is DeliveryVerdict.FINAL
    after = await _entries(world)
    assert set(after) == set(before), 'the delivery created or deleted an entry'
    for entry_id, (expiry, collect, owner, start) in before.items():
        assert after[entry_id] == (expiry, collect, owner, start), 'the delivery wrote a lifecycle or identity field'
    assert {batch.dataset_id for batch in world.bar_batches} == {world.rows['behind'].id}


async def _attempt_rows(world: World) -> list[IngestRun]:
    """Every ingest_run row but the seeded legacy RUNNING one (`running`'s)."""
    return [run for run in await world.runs() if run.state is not IngestRunState.RUNNING]


async def _entries(world: World) -> dict[uuid.UUID, tuple]:
    async with AsyncSession(world.engine) as db:
        rows = (
            await db.execute(
                select(
                    StoreDatasetEntry.id,
                    StoreDatasetEntry.expiry,
                    StoreDatasetEntry.collect,
                    StoreDatasetEntry.owner,
                    StoreDatasetEntry.start,
                )
            )
        ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


@pytest.mark.asyncio
async def test_the_narrowed_start_would_collide_if_it_reached_the_overlap_key(world: World, monkeypatch):
    """The guard the refresh avoids is real: a POST body whose START is the tail collides with the entry.

    Since the push path the POST refuses its own overlap BEFORE its bars request is written or sent: no bars row, no
    bars Submit. Since P51 the POST's name_change query runs first (the overlap key is the clamped start), so that
    query is written, sent and answered (no record: no clamp) and the collision is still the body's own. The refresh
    never reaches either at all (it names its dataset).
    """
    install_book(monkeypatch)
    row = world.rows['behind']
    narrowed = StoreAssetDatasetBody(
        owner=row.owner,
        source=row.source,
        feed=row.feed,
        granularity=row.granularity,
        start=TAIL['behind'],
        expiry_type=row.expiry_type,
        update_type=row.update_type,
    )
    path = StoreAssetDatasetPath(asset_type=row.asset_type, data_type=row.data_type, asset_symbol=row.asset_symbol)
    client = ScriptedClient()
    Plays(world.intake.deliver, monkeypatch, client, bars=())
    async with AsyncSession(world.engine) as db:
        with pytest.raises(OwnOverlapConflict):
            await data_action_request.store_market_activity_worker(path, narrowed, db, client, deadline_s=10)
    assert client.submitted == []
    assert len(client.queries) == 1
    assert [IngestRequestKind(row.kind) for row in await world.requests()] == [IngestRequestKind.CORPORATE_ACTIONS]


@pytest.mark.asyncio
async def test_without_fetch_start_the_post_asks_for_the_body_range_exactly(world: World):
    """POST /store's path: no narrowed start, the request's start is the body's; trigger REQUEST, no dataset yet.

    store_market_activity_worker is the POST's whole path and WAITS for the verdict; the half before the wait is
    submit_bars, driven here with the POST's arguments so nothing has to time out.
    """
    row = world.rows['behind']
    body = StoreAssetDatasetBody(
        owner=row.owner,
        source=row.source,
        granularity=row.granularity,
        start=ny_midnight(1, 6),
        end=ny_midnight(1, 7),
        expiry_type=ExpiryType.BULK,
        update_type=UpdateType.STATIC,
    )
    path = StoreAssetDatasetPath(asset_type=row.asset_type, data_type=row.data_type, asset_symbol=row.asset_symbol)
    client = ScriptedClient()
    async with AsyncSession(world.engine) as db:
        await data_action_request.submit_bars(
            path, body, db, client, trigger=IngestRunTrigger.REQUEST, client_label=body.owner
        )

    (submitted,) = client.submitted
    assert submitted.bars.start == body.start
    assert submitted.bars.end == body.end
    assert submitted.bars.feed is None
    assert submitted.owner == row.owner
    (stored,) = await world.requests()
    assert stored.trigger is IngestRunTrigger.REQUEST
    assert stored.dataset_id is None, 'a POST names no dataset: its first delivered attempt creates the entry'
    assert stored.requested_start == body.start


# ------------------------------------------------------------------------------------------------- the record


@pytest.mark.asyncio
async def test_the_delivery_announces_the_refreshed_dataset_and_the_pass_announces_nothing(world: World):
    """DatasetChanged UPDATED follows the commit that wrote the bars, which is now the delivery intake's, not the pass's.

    Before the push path the pass's fetch committed the bars and published UPDATED after each successful refresh.
    Now the pass only submits, so it publishes nothing; when ingest delivers the tail, the intake publishes the
    attempt's IngestRunChanged and then UPDATED for the dataset the request named, both after its commit. A failed
    Submit leaves nothing to announce.
    """
    client = ScriptedClient({_symbol(world, 'behind2'): _fails()})

    await world.refresher(client).run_pass(DataSource.ALPACA_API)
    assert world.events == [], 'the pass published an event although it wrote no bar'

    await world.deliver('behind')
    changed = [event_ for event_ in world.events if isinstance(event_, DatasetChanged)]
    assert changed == [DatasetChanged(world.rows['behind'].id, DatasetChange.UPDATED)]


@pytest.mark.asyncio
async def test_a_failed_submit_leaves_the_request_pending_for_the_watcher(world: World):
    """A call that fails writes nothing past the request row: PENDING, no instance, no attempt row; counted failed.

    Before the push path a failed fetch was recorded as a FAILED run with its reason. Now the vendor is not called by
    the store at all; what can fail in the pass is the Submit, and its request is left PENDING so the instance
    watcher re-sends it (Submit is idempotent on the request id). A vendor failure reaches the store later, as a
    delivered FAILED attempt (the intake's tests).
    """
    client = ScriptedClient({_symbol(world, 'older'): _fails(Reason.PEER_UNAVAILABLE)})

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    row = await world.request_of('older')
    assert IngestRequestState(row.state) is IngestRequestState.PENDING
    assert row.instance_id is None
    assert row.trigger is IngestRunTrigger.SCHEDULE
    assert row.dataset_id == world.rows['older'].id
    assert await _attempt_rows(world) == []
    assert result == PassResult(behind=4, refreshed=3, failed=1, deferred=0)


@pytest.mark.asyncio
async def test_a_refused_ack_writes_no_entry(world: World):
    """A tape ingest can no longer serve is refused in the ack: the request finishes FAILED (PERMANENT), no entry.

    FEED_NOT_AVAILABLE is REFUSED in the vocabulary, so it is a permanent refusal: counted in `refused`, not as a
    transient failure, and it neither backs off nor stops the pass.
    """
    before = await _entries(world)
    client = ScriptedClient({_symbol(world, 'behind2'): _refused(Reason.FEED_NOT_AVAILABLE)})

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert await _entries(world) == before
    row = await world.request_of('behind2')
    assert IngestRequestState(row.state) is IngestRequestState.FAILED
    assert row.failure_cause == IngestRequestFailureCause.PERMANENT
    assert row.finished_at is not None
    assert result == PassResult(behind=4, refreshed=3, failed=0, deferred=0, refused=1)
    assert world.sleeps == []


# ------------------------------------------------------------------------------------------------- bounded


@pytest.mark.asyncio
async def test_a_pass_refreshes_at_most_the_cap_and_defers_the_rest(world: World, monkeypatch):
    monkeypatch.setenv(refresh_module.MAX_PER_PASS_ENV, '2')
    client = ScriptedClient()

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert world.requested(client) == ['older', 'empty']
    assert result == PassResult(behind=4, refreshed=2, failed=0, deferred=2)


@pytest.mark.asyncio
async def test_the_pass_is_serial_never_concurrent(world: World):
    """One dataset at a time: the next Submit starts only after the previous one has been answered."""
    in_flight: list[int] = []
    peak = [0]

    class Serial(ScriptedClient):
        async def submit(self, request):
            in_flight.append(1)
            peak[0] = max(peak[0], len(in_flight))
            try:
                await asyncio.sleep(0)
                return await super().submit(request)
            finally:
                in_flight.pop()

    await world.refresher(Serial()).run_pass(DataSource.ALPACA_API)

    assert peak[0] == 1


@pytest.mark.asyncio
async def test_failures_back_off_doubling_and_the_pass_stops_after_three_in_a_row(world: World):
    client = ScriptedClient({_symbol(world, name): _fails() for name in BEHIND_ORDER})

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert world.requested(client) == ['older', 'empty', 'behind2']
    assert world.sleeps == [DEFAULT_BACKOFF_SECONDS, 2 * DEFAULT_BACKOFF_SECONDS]
    assert result == PassResult(behind=4, refreshed=0, failed=3, deferred=1)


@pytest.mark.asyncio
async def test_the_backoff_is_capped_at_ten_minutes(world: World, monkeypatch):
    monkeypatch.setenv(refresh_module.BACKOFF_ENV, '400')
    client = ScriptedClient({_symbol(world, name): _fails() for name in BEHIND_ORDER})

    await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert world.sleeps == [400, refresh_module.BACKOFF_CAP_SECONDS]


@pytest.mark.asyncio
async def test_a_success_resets_the_failure_run(world: World):
    names = ('older', 'behind2', 'behind')
    client = ScriptedClient({_symbol(world, name): _fails() for name in names})

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert world.requested(client) == list(BEHIND_ORDER)
    assert world.sleeps == [DEFAULT_BACKOFF_SECONDS, DEFAULT_BACKOFF_SECONDS, 2 * DEFAULT_BACKOFF_SECONDS]
    assert result == PassResult(behind=4, refreshed=1, failed=3, deferred=0)


@pytest.mark.asyncio
async def test_a_rate_limit_naming_its_reset_stops_the_pass_at_once(world: World):
    limited = ExogenousError.from_retry_after(Reason.VENDOR_RATE_LIMITED, 'slow down', 60)
    client = ScriptedClient({_symbol(world, 'older'): limited})

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert world.requested(client) == ['older']
    assert world.sleeps == []
    assert result == PassResult(behind=4, refreshed=0, failed=1, deferred=3)


# ------------------------------------------------------------------------------------------------- logs


@pytest.mark.asyncio
async def test_the_failure_log_names_the_exception_type_and_never_the_owner_or_symbol(world: World, caplog):
    caplog.set_level(logging.DEBUG)
    client = ScriptedClient({_symbol(world, 'behind'): _fails()})

    await world.refresher(client).run_pass(DataSource.ALPACA_API)

    errors = [
        record.getMessage()
        for record in caplog.records
        if record.name == refresh_module.log.name and record.levelno >= logging.ERROR
    ]
    assert errors == ['Daily refresh could not refresh a dataset: ExogenousError']
    for record in caplog.records:
        message = record.getMessage()
        assert SECRET_OWNER not in message, f'{record.name} logged the owner: {message}'
        if record.name == refresh_module.log.name:
            assert SYMBOL_PREFIX not in message, f'the refresh logged a symbol: {message}'


# ------------------------------------------------------------------------------------------------- the environment


@pytest.mark.parametrize('value', ['false', 'FALSE', ' off ', '0', 'no', 'Off'])
def test_the_kill_switch_turns_the_refresh_off(monkeypatch, value):
    monkeypatch.setenv(refresh_module.ENABLED_ENV, value)
    assert refresh_module.is_enabled() is False


@pytest.mark.parametrize('value', [None, 'true', '1', 'yes', '', 'anything'])
def test_anything_but_an_explicit_false_leaves_it_on(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(refresh_module.ENABLED_ENV, raising=False)
    else:
        monkeypatch.setenv(refresh_module.ENABLED_ENV, value)
    assert refresh_module.is_enabled() is True


@pytest.mark.parametrize(
    ('env', 'reader', 'value', 'expected'),
    [
        ('MAX_PER_PASS_ENV', 'max_per_pass', None, DEFAULT_MAX_PER_PASS),
        ('MAX_PER_PASS_ENV', 'max_per_pass', '3', 3),
        ('MAX_PER_PASS_ENV', 'max_per_pass', '0', DEFAULT_MAX_PER_PASS),
        ('MAX_PER_PASS_ENV', 'max_per_pass', '-2', DEFAULT_MAX_PER_PASS),
        ('MAX_PER_PASS_ENV', 'max_per_pass', 'ten', DEFAULT_MAX_PER_PASS),
        ('BACKOFF_ENV', 'backoff_base_seconds', None, DEFAULT_BACKOFF_SECONDS),
        ('BACKOFF_ENV', 'backoff_base_seconds', '5', 5),
        ('BACKOFF_ENV', 'backoff_base_seconds', '0', DEFAULT_BACKOFF_SECONDS),
        ('BACKOFF_ENV', 'backoff_base_seconds', '1.5', DEFAULT_BACKOFF_SECONDS),
        ('SETTLE_ENV', 'settle_delay', None, timedelta(minutes=DEFAULT_SETTLE_MINUTES)),
        ('SETTLE_ENV', 'settle_delay', '0', timedelta(0)),
        ('SETTLE_ENV', 'settle_delay', '45', timedelta(minutes=45)),
        ('SETTLE_ENV', 'settle_delay', '-1', timedelta(minutes=DEFAULT_SETTLE_MINUTES)),
        ('SETTLE_ENV', 'settle_delay', 'soon', timedelta(minutes=DEFAULT_SETTLE_MINUTES)),
    ],
)
def test_the_env_readers_fall_back_to_their_defaults(monkeypatch, env, reader, value, expected):
    name = getattr(refresh_module, env)
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    assert getattr(refresh_module, reader)() == expected


# ------------------------------------------------------------------------------------------------- the loops


async def _turns(count: int, *, until: Callable[[], bool] | None = None, pause: float = 0) -> None:
    """Give the running loops up to COUNT turns (stopping early once UNTIL holds), bounded by a 10 s wait_for.

    The bound turns a loop that re-runs its pass at once (an immediate wake) into a FAILURE rather than a hang
    (tj-grna9p.92). pytest-timeout is not a dependency and is not added for this.
    """

    async def turns() -> None:
        for _ in range(count):
            if until is not None and until():
                return
            await asyncio.sleep(pause)

    await asyncio.wait_for(turns(), timeout=10)


class _Started:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, Callable, dict]] = []

    def __call__(self, name, schedule, job, **kwargs):
        self.calls.append((name, schedule, job, kwargs))
        return f'task-{len(self.calls)}'


@pytest.mark.asyncio
async def test_one_loop_per_calendar_source_on_the_close_plus_settle_with_the_catch_up_preset(monkeypatch):
    started = _Started()
    monkeypatch.setattr(refresh_module, 'run_periodic', started)
    monkeypatch.setenv(refresh_module.ENABLED_ENV, 'true')
    monkeypatch.setenv(refresh_module.SETTLE_ENV, '45')
    # A source listed without a calendar the lookup knows is skipped, not fatal.
    monkeypatch.setattr(refresh_module, 'SOURCE_CALENDARS', {DataSource.ALPACA_API: 'XNYS', DataSource.IB_API: 'X'})

    tasks = await refresh_module.start_daily_refresh_loops(ScriptedClient())

    assert tasks == ['task-1']
    ((name, schedule, _job, kwargs),) = started.calls
    assert name == f'daily-refresh-{DataSource.ALPACA_API.value}'
    assert schedule == AfterTradingEvent(
        calendar_for_source(DataSource.ALPACA_API), TradingEvent.CLOSE, timedelta(minutes=45)
    )
    assert kwargs['wake'].is_set(), 'the start-up catch-up pass is not pre-set'
    assert kwargs['log'] is refresh_module.log


@pytest.mark.asyncio
async def test_the_kill_switch_starts_nothing(monkeypatch):
    started = _Started()
    monkeypatch.setattr(refresh_module, 'run_periodic', started)
    monkeypatch.setenv(refresh_module.ENABLED_ENV, 'false')

    assert await refresh_module.start_daily_refresh_loops(ScriptedClient()) == []
    assert started.calls == []


# NO AUTOMATIC RETRY (decision tj-grna9p.55 option B; the architect's round-3 gate on this bead, fix 4e42182). A pass
# with transient failures schedules nothing: the failed dataset is requested again at the next close, or by the
# catch-up pass at the next start. The early retry pass and DAILY_REFRESH_RETRY_MINUTES are gone.


@pytest.mark.asyncio
@pytest.mark.parametrize('retry_env', [None, '1', '15', '0'], ids=['unset', 'one-minute', 'fifteen', 'zero'])
async def test_a_pass_with_transient_failures_schedules_no_extra_pass(monkeypatch, retry_env):
    """The job runs its pass and nothing else: no call_later, the wake left unset, whatever the removed env says."""
    started = _Started()
    scheduled: list[Any] = []
    passes: list[DataSource] = []

    async def run_pass(self, source, calendar=None):
        passes.append(source)
        return PassResult(behind=4, failed=3, deferred=1)

    monkeypatch.setattr(refresh_module, 'run_periodic', started)
    monkeypatch.setattr(DailyRefresher, 'run_pass', run_pass)
    monkeypatch.setenv(refresh_module.ENABLED_ENV, 'true')
    if retry_env is None:
        monkeypatch.delenv(REMOVED_RETRY_ENV, raising=False)
    else:
        monkeypatch.setenv(REMOVED_RETRY_ENV, retry_env)
    monkeypatch.setattr(asyncio.get_running_loop(), 'call_later', lambda *a, **k: scheduled.append(a))

    await refresh_module.start_daily_refresh_loops(ScriptedClient())
    ((_name, _schedule, job, kwargs),) = started.calls
    kwargs['wake'].clear()  # the start-up catch-up has been taken; only a close or an early pass could set it again
    for _ in range(4):
        await job()

    assert passes == [DataSource.ALPACA_API] * 4, 'a job ran more or fewer passes than it was called for'
    assert scheduled == [], 'a pass with failures scheduled something'
    assert not kwargs['wake'].is_set(), 'a pass with failures woke the loop for an early pass'


def test_the_retry_env_is_gone_from_the_module():
    for name in ('RETRY_ENV', 'DEFAULT_RETRY_MINUTES', 'MAX_RETRY_PASSES', 'retry_delay'):
        assert not hasattr(refresh_module, name), f'{name} is back: an automatic retry needs a ruling (tj-grna9p.55)'


@pytest.mark.asyncio
async def test_the_real_loop_runs_one_pass_after_transient_failures_and_waits_for_the_close(monkeypatch):
    """Through the real run_periodic: the catch-up pass fails transiently; no second pass comes before the close."""
    passes: list[DataSource] = []
    wakes: list[Any] = []
    real_call_later = asyncio.get_running_loop().call_later

    def call_later(delay, callback, *args, **kwargs):
        # A sleep inside the loop uses call_later too; only an Event.set callback would be an early pass.
        if getattr(callback, '__self__', None).__class__ is asyncio.Event:
            wakes.append(delay)
        return real_call_later(delay, callback, *args, **kwargs)

    async def run_pass(self, source, calendar=None):
        passes.append(source)
        await asyncio.sleep(0)  # yields, as a real pass does, so a loop that re-runs at once cannot starve the test
        return PassResult(behind=2, failed=2)

    monkeypatch.setattr(DailyRefresher, 'run_pass', run_pass)
    monkeypatch.setenv(refresh_module.ENABLED_ENV, 'true')
    monkeypatch.setenv(REMOVED_RETRY_ENV, '1')
    monkeypatch.setattr(asyncio.get_running_loop(), 'call_later', call_later)

    tasks = await refresh_module.start_daily_refresh_loops(ScriptedClient())
    try:
        await _turns(100, until=lambda: bool(passes))
        await _turns(100)
        assert passes == [DataSource.ALPACA_API]
        assert wakes == [], 'an early pass was scheduled after transient failures'
        assert not any(task.done() for task in tasks)
    finally:
        await refresh_module.stop_daily_refresh_loops(tasks)


@pytest.mark.asyncio
async def test_the_real_loop_runs_the_catch_up_at_once_then_waits_for_the_close(monkeypatch):
    passes: list[DataSource] = []

    async def run_pass(self, source, calendar=None):
        passes.append(source)
        await asyncio.sleep(0)  # yields, as a real pass does
        return PassResult()

    monkeypatch.setattr(DailyRefresher, 'run_pass', run_pass)
    monkeypatch.setenv(refresh_module.ENABLED_ENV, 'true')

    tasks = await refresh_module.start_daily_refresh_loops(ScriptedClient())
    try:
        await _turns(50, until=lambda: bool(passes))
        assert passes == [DataSource.ALPACA_API]
        await _turns(50)
        assert passes == [DataSource.ALPACA_API], 'the second pass did not wait for the next close'
        assert not any(task.done() for task in tasks)
    finally:
        await refresh_module.stop_daily_refresh_loops(tasks)
    assert all(task.cancelled() for task in tasks)


@pytest.mark.asyncio
async def test_an_unmigrated_database_warns_once_per_process_and_the_loop_goes_on(
    request: pytest.FixtureRequest, monkeypatch, caplog
):
    """No ingest_run table: each pass does nothing and says so ONCE per process, naming make migrate; no error."""
    request.getfixturevalue('aware_sqlite_datetimes')
    engine = _engine()
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: StoreDatasetEntry.metadata.create_all(
                sync, tables=[StoreDatasetEntry.__table__, StockMarketActivity.__table__]
            )
        )

    @asynccontextmanager
    async def own_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db

    monkeypatch.setattr(refresh_module, 'own_session', own_session)
    monkeypatch.setenv(refresh_module.ENABLED_ENV, 'true')
    attempts = [0]
    real_run_pass = DailyRefresher.run_pass

    async def counted(self, source, calendar=None):
        attempts[0] += 1
        return await real_run_pass(self, source, calendar)

    monkeypatch.setattr(refresh_module, '_unmigrated_warned', False)
    caplog.set_level(logging.WARNING)
    client = ScriptedClient()

    direct = DailyRefresher(client, clock=lambda: NOW)
    assert await direct.run_pass(DataSource.ALPACA_API) == PassResult()
    assert await direct.run_pass(DataSource.ALPACA_API) == PassResult()

    monkeypatch.setattr(DailyRefresher, 'run_pass', counted)
    tasks = await refresh_module.start_daily_refresh_loops(client)
    try:
        await _turns(200, until=lambda: bool(attempts[0]), pause=0.01)
        await _turns(50, pause=0.01)
        assert attempts[0] == 1
        assert not tasks[0].done(), 'the loop ended after a pass on an unmigrated database'
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len([m for m in warnings if 'make migrate' in m]) == 1, warnings
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR], 'an error was logged per pass'
        assert client.requests == []
    finally:
        await refresh_module.stop_daily_refresh_loops(tasks)
        await engine.dispose()


# ------------------------------------------------------------------------------------------------- round 2: fairness
# The fix 0b6d9d8 (round 2 of this gate): a REFUSED-outcome error is permanent (no stop, no backoff, no early pass,
# counted in `refused`); the order and the "tried already" state come from the SCHEDULE run history; a dataset whose
# last scheduled attempt asked for the same tail and ended EMPTY or permanently FAILED since the last close is
# skipped until the next close, and once due again holds at most half the cap while others wait.

# The 6th's XNYS close, the last close at NOW.
LAST_CLOSE = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)


def _tail(_world: World, name: str) -> datetime:
    return TAIL[name]


def _scheduled_run(
    world: World,
    name: str,
    *,
    state: IngestRunState,
    finished_at: datetime,
    requested_start: datetime | None = None,
    reason: Reason | None = None,
    with_dataset: bool = True,
) -> IngestRun:
    """A finished SCHEDULE run with EXPLICIT timestamps, judged against the injected NOW (not the wall clock)."""
    row = world.rows[name]
    return IngestRun(
        id=uuid.uuid4(),
        dataset_id=row.id if with_dataset else None,
        trigger=IngestRunTrigger.SCHEDULE,
        actor=SCHEDULER_ACTOR,
        asset_symbol=row.asset_symbol,
        asset_type=row.asset_type,
        data_type=row.data_type,
        source=row.source,
        granularity=row.granularity,
        update_type=UpdateType.DAILY.value,
        feed=row.feed if with_dataset else None,
        requested_start=_tail(world, name) if requested_start is None else requested_start,
        state=state,
        bars_written=1 if state is IngestRunState.SUCCEEDED else 0,
        error_domain=None if reason is None else ERROR_DOMAIN,
        error_reason=None if reason is None else str(reason),
        started_at=finished_at - timedelta(seconds=5),
        finished_at=finished_at,
    )


async def _add(world: World, *rows: Any) -> None:
    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        db.add_all(rows)
        await db.commit()


@pytest.mark.asyncio
async def test_permanent_refusals_ahead_of_a_healthy_dataset_never_starve_it(world: World):
    """The round-1 starvation: three permanently unservable datasets sort first; the healthy one is still served.

    `older` names a tape ingest cannot serve, `empty` an instrument the vendor refuses, and `behind2` is a stored
    row that no longer validates as a request (a DAILY with a declared end), so it is never sent.
    """
    async with AsyncSession(world.engine) as db:
        await db.execute(
            update(StoreDatasetEntry)
            .where(StoreDatasetEntry.id == world.rows['behind2'].id)
            .values(end=ny_midnight(1, 12))
        )
        await db.commit()
    client = ScriptedClient(
        {
            _symbol(world, 'older'): _refused(Reason.FEED_NOT_AVAILABLE),
            _symbol(world, 'empty'): _refused(Reason.VENDOR_INVALID_REQUEST),
        }
    )

    first = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert world.requested(client) == ['older', 'empty', 'behind']
    assert first == PassResult(behind=4, refreshed=1, failed=0, deferred=0, refused=2, skipped=1)
    assert world.sleeps == [], 'a permanent refusal backed off'


async def _refused_then_second_pass(world: World, refusals: dict[str, Any]) -> tuple[ScriptedClient, PassResult]:
    """Pass 1 with these Submit scripts, every accepted request delivered with a bar, then pass 2.

    The accepted ones (`behind2`, `behind`) stay behind, because the double stores no bar, and are delivered so their
    requests finish: an OPEN request is Running, and a Running dataset is not owed again, so a request left open would
    drop out of pass 2 for that reason and not for the rule under test.

    Returns:
        tuple[ScriptedClient, PassResult]: The second pass's client and result.
    """
    await world.refresher(ScriptedClient(refusals)).run_pass(DataSource.ALPACA_API)
    for name in ('behind2', 'behind'):
        await world.deliver(name, finished_at=NOW)
    client = ScriptedClient(refusals)
    return client, await world.refresher(client).run_pass(DataSource.ALPACA_API)


@pytest.mark.asyncio
async def test_a_vendor_refusal_delivered_for_good_is_skipped_until_the_next_close(world: World):
    """The skip rule over the push path: the vendor's permanent refusal arrives as a delivered FAILED attempt.

    Ingest reports the attempt FAILED with the PERMANENT class; the intake finishes the request FAILED, cause
    PERMANENT, and writes the attempt row. finished_scheduled_runs joins that row to its SCHEDULE request and carries
    the cause, so the next pass before the close skips the dataset (same tail) and still serves the healthy ones.
    """
    await world.refresher(ScriptedClient()).run_pass(DataSource.ALPACA_API)
    await world.deliver('older', DeliveryOutcome.FAILED, error_class=DeliveryErrorClass.PERMANENT, finished_at=NOW)
    for name in ('empty', 'behind2', 'behind'):
        await world.deliver(name, finished_at=NOW)
    request = await world.request_of('older')
    assert request.failure_cause == IngestRequestFailureCause.PERMANENT

    client = ScriptedClient()
    second = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert world.requested(client) == ['empty', 'behind2', 'behind']
    assert second == PassResult(behind=4, refreshed=3, skipped=1)


@pytest.mark.asyncio
async def test_a_submit_refused_for_good_is_skipped_until_the_next_close(world: World):
    """The round-1 rule for the refusal that never reaches the vendor: ingest refuses the Submit itself.

    Before the push path the run recorder wrote that refusal as a FAILED run (no dataset id: it failed before the
    ack), matched on the request's own fields, and the next pass skipped it. Now only the request row holds it: FAILED,
    cause PERMANENT, with no attempt row, and finished_scheduled_runs reads it into the history (tj-7zjov1.160).
    """
    client, second = await _refused_then_second_pass(
        world,
        {
            _symbol(world, 'older'): _refused(Reason.FEED_NOT_AVAILABLE),
            _symbol(world, 'empty'): _refused(Reason.VENDOR_INVALID_REQUEST),
        },
    )

    assert world.requested(client) == ['behind2', 'behind']
    assert second == PassResult(behind=4, refreshed=2, skipped=2)
    # What the history had to read: one request each, FAILED for good, and NO attempt row behind it.
    async with AsyncSession(world.engine) as db:
        attempted = set((await db.execute(select(IngestRun.request_id))).scalars())
    for name in ('older', 'empty'):
        request = await world.request_of(name)
        assert (request.state, request.failure_cause) == (
            IngestRequestState.FAILED,
            IngestRequestFailureCause.PERMANENT,
        )
        assert request.id not in attempted


@pytest.mark.asyncio
async def test_permanent_refusals_do_not_count_toward_the_stop(world: World):
    """Three refusals in a row and the fourth dataset is still requested: no stop, no sleep."""
    client = ScriptedClient({_symbol(world, name): _refused() for name in ('older', 'empty', 'behind2')})

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert world.requested(client) == list(BEHIND_ORDER)
    assert result == PassResult(behind=4, refreshed=1, failed=0, deferred=0, refused=3)
    assert world.sleeps == []


@pytest.mark.asyncio
async def test_a_pass_with_only_permanent_refusals_schedules_no_early_retry(monkeypatch):
    started = _Started()
    scheduled: list[Any] = []

    async def run_pass(self, source, calendar=None):
        return PassResult(behind=3, refused=3)

    monkeypatch.setattr(refresh_module, 'run_periodic', started)
    monkeypatch.setattr(DailyRefresher, 'run_pass', run_pass)
    monkeypatch.setenv(refresh_module.ENABLED_ENV, 'true')
    monkeypatch.delenv(REMOVED_RETRY_ENV, raising=False)
    monkeypatch.setattr(asyncio.get_running_loop(), 'call_later', lambda *a: scheduled.append(a))

    await refresh_module.start_daily_refresh_loops(ScriptedClient())
    await started.calls[0][2]()

    assert scheduled == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error',
    [ExogenousError(Reason.PEER_UNAVAILABLE, 'ingest is not there'), RuntimeError('nobody typed this')],
    ids=['peer-unavailable', 'untyped'],
)
async def test_a_transient_failure_still_counts_backs_off_and_stops(world: World, error):
    client = ScriptedClient({_symbol(world, name): error for name in BEHIND_ORDER})

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert world.requested(client) == ['older', 'empty', 'behind2']
    assert world.sleeps == [DEFAULT_BACKOFF_SECONDS, 2 * DEFAULT_BACKOFF_SECONDS]
    assert result == PassResult(behind=4, refreshed=0, failed=3, deferred=1, refused=0)


@pytest.mark.asyncio
async def test_after_a_failed_daily_run_the_next_request_asks_from_the_last_stored_bar_plus_one_bar(world: World):
    """After a failed DAILY attempt the dataset is asked for again from the last stored bar plus one bar.

    The 2026-10-06 acceptance on tj-grna9p.22 (decision .55 option B): a failed DAILY update is not retried by the
    pass itself, and the missed day is refetched later. Traceability gap filled by tj-grna9p.92.

    REPAIRED FOR THE PUSH PATH (tj-7zjov1.28). Two ways a refresh now fails, and both keep the tail:
      - the SUBMIT fails: the request stays PENDING with the tail as its window, the next pass does not send a
        second one (the dataset has an open request), and the watcher re-sends THAT row (its window unchanged);
      - ingest reports the attempt FAILED for good (last_attempt): the request finishes, and the next pass asks again
        for the same tail, because nothing was stored.
    """
    failing = ScriptedClient({_symbol(world, 'older'): _fails()})
    first = await world.refresher(failing).run_pass(DataSource.ALPACA_API)
    assert first.failed == 1
    pending = await world.request_of('older')
    assert IngestRequestState(pending.state) is IngestRequestState.PENDING
    assert pending.requested_start == TAIL['older']

    meanwhile = ScriptedClient()
    await world.refresher(meanwhile).run_pass(DataSource.ALPACA_API)
    assert 'older' not in world.requested(meanwhile), 'a second request was sent beside the pending one'

    # The watcher's re-send goes through submit_record with the stored row: the same request id and the same window.
    resend = ScriptedClient()
    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        record = await get_request(db, pending.id)
    await data_action_request.submit_record(record, resend)
    (resent,) = resend.submitted
    assert (resent.request_id, resent.bars.start) == (str(pending.id), TAIL['older'])
    await world.deliver(
        'older', DeliveryOutcome.FAILED, last_attempt=True, finished_at=LAST_CLOSE + timedelta(minutes=5)
    )
    assert IngestRequestState((await world.request_of('older')).state) is IngestRequestState.FAILED

    client = ScriptedClient()
    await world.refresher(client).run_pass(DataSource.ALPACA_API)

    (again,) = [request for request in client.requests if request.asset_symbol == _symbol(world, 'older')]
    assert again.start == TAIL['older']
    (attempt,) = [run for run in await world.runs() if run.dataset_id == world.rows['older'].id]
    assert attempt.state is IngestRunState.FAILED
    assert attempt.requested_start == again.start


def test_permanence_is_the_vocabularys_refused_outcome():
    assert refresh_module.is_permanent(InvalidRequestError(Reason.FEED_NOT_AVAILABLE, 'x'))
    assert refresh_module.is_permanent(InvalidRequestError(Reason.VENDOR_INVALID_REQUEST, 'x'))
    assert not refresh_module.is_permanent(ExogenousError(Reason.PEER_UNAVAILABLE, 'x'))
    assert not refresh_module.is_permanent(ExogenousError.from_retry_after(Reason.VENDOR_RATE_LIMITED, 'x', 5))
    assert not refresh_module.is_permanent(RuntimeError('untyped'))


@pytest.mark.asyncio
async def test_never_attempted_first_then_least_recently_attempted(world: World):
    """`behind2` and `behind` were never attempted (oldest newest-bar first); then `empty` (4th), then `older` (5th)."""
    await _add(
        world,
        _scheduled_run(
            world, 'older', state=IngestRunState.SUCCEEDED, finished_at=datetime(2026, 10, 5, 22, tzinfo=UTC)
        ),
        _scheduled_run(
            world, 'empty', state=IngestRunState.SUCCEEDED, finished_at=datetime(2026, 10, 4, 22, tzinfo=UTC)
        ),
    )
    client = ScriptedClient()

    await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert world.requested(client) == ['behind2', 'behind', 'empty', 'older']


@pytest.mark.asyncio
async def test_a_run_older_than_the_lookback_counts_as_never_attempted(world: World):
    await _add(
        world,
        _scheduled_run(world, 'older', state=IngestRunState.SUCCEEDED, finished_at=NOW - timedelta(days=8)),
        _scheduled_run(world, 'empty', state=IngestRunState.SUCCEEDED, finished_at=NOW - timedelta(days=1)),
    )
    client = ScriptedClient()

    await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert world.requested(client) == ['older', 'behind2', 'behind', 'empty']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('state', 'reason', 'finished', 'same_tail', 'with_dataset', 'skipped'),
    [
        # EMPTY for the same tail since the last close: skipped until the next close.
        (IngestRunState.EMPTY, None, LAST_CLOSE + timedelta(minutes=1), True, True, True),
        # The same, at the close exactly: still since it.
        (IngestRunState.EMPTY, None, LAST_CLOSE, True, True, True),
        # Before the last close: due again.
        (IngestRunState.EMPTY, None, LAST_CLOSE - timedelta(minutes=1), True, True, False),
        # A different tail (a bar has arrived since): says nothing about this one.
        (IngestRunState.EMPTY, None, LAST_CLOSE + timedelta(minutes=1), False, True, False),
        # A permanent failure before the ack (no dataset id), matched on the request: skipped.
        (IngestRunState.FAILED, Reason.FEED_NOT_AVAILABLE, LAST_CLOSE + timedelta(minutes=1), True, False, True),
        # A transient failure is never a reason to skip.
        (IngestRunState.FAILED, Reason.VENDOR_UNAVAILABLE, LAST_CLOSE + timedelta(minutes=1), True, True, False),
        # A success with this tail is not unproductive.
        (IngestRunState.SUCCEEDED, None, LAST_CLOSE + timedelta(minutes=1), True, True, False),
    ],
)
async def test_the_skip_rule_against_the_injected_now(
    world: World, state, reason, finished, same_tail, with_dataset, skipped
):
    tail = _tail(world, 'older') if same_tail else _tail(world, 'older') - timedelta(days=1)
    await _add(
        world,
        _scheduled_run(
            world,
            'older',
            state=state,
            finished_at=finished,
            requested_start=tail,
            reason=reason,
            with_dataset=with_dataset,
        ),
    )
    client = ScriptedClient()

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert ('older' not in world.requested(client)) is skipped
    assert result.skipped == (1 if skipped else 0)


@pytest.mark.asyncio
async def test_only_the_refreshs_own_runs_count_as_attempts(world: World):
    """A POST /store run (trigger REQUEST) for the same tail, EMPTY since the close, neither skips nor reorders."""
    run = _scheduled_run(world, 'older', state=IngestRunState.EMPTY, finished_at=LAST_CLOSE + timedelta(minutes=1))
    run.trigger = IngestRunTrigger.REQUEST
    run.actor = world.rows['older'].owner
    await _add(world, run)
    client = ScriptedClient()

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert world.requested(client) == list(BEHIND_ORDER)
    assert result.skipped == 0


@pytest.mark.asyncio
async def test_a_scheduled_request_whose_last_attempt_is_a_retry_is_judged_by_its_request_trigger(world: World):
    """Note H on tj-7zjov1.28, through the real submit path and the real intake: the DAILY writer's trigger counts.

    The pass writes the request trigger SCHEDULE; ingest reports attempt 1 FAILED (transient: CONTINUE) and attempt 2
    an empty success. The intake writes the first attempt row SCHEDULE and the second RETRY (only the first takes the
    request's trigger). The request finished EMPTY after the close for the same tail, so the next pass must SKIP it:
    finished_scheduled_runs judges the RETRY row by its REQUEST's trigger. A writer that dropped the trigger (NULL
    reads as REQUEST) would make both rows non-scheduled and the dataset would be requested again.
    """
    await world.refresher(ScriptedClient()).run_pass(DataSource.ALPACA_API)
    assert (
        await world.deliver('older', DeliveryOutcome.FAILED, attempt_no=1, finished_at=NOW) is DeliveryVerdict.CONTINUE
    )
    # The retry's vendor call is a minute after the first's, as in production: the history orders by started_at,
    # which the intake takes from the attempt's first call (two equal stamps would tie-break on a random id).
    retried = await world.deliver(
        'older', bars=False, attempt_no=2, finished_at=NOW, called_at=CALLED_AT + timedelta(minutes=1)
    )
    assert retried is DeliveryVerdict.FINAL
    for name in ('empty', 'behind2', 'behind'):
        await world.deliver(name, finished_at=NOW)

    attempts = sorted(
        (run for run in await world.runs() if run.dataset_id == world.rows['older'].id), key=lambda run: run.attempt_no
    )
    assert [(run.attempt_no, run.trigger, run.state) for run in attempts] == [
        (1, IngestRunTrigger.SCHEDULE, IngestRunState.FAILED),
        (2, IngestRunTrigger.RETRY, IngestRunState.EMPTY),
    ]
    assert IngestRequestState((await world.request_of('older')).state) is IngestRequestState.EMPTY

    client = ScriptedClient()
    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert 'older' not in world.requested(client), 'a scheduled request ending on a RETRY row was not seen as tried'
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_a_post_request_for_the_same_tail_is_not_the_refreshs_attempt(world: World, monkeypatch):
    """The other half over the push path: a POST's request (trigger REQUEST) that finished EMPTY skips nothing.

    The POST is written through the real submit path with the dataset's own identity and the refresh's tail as its
    window, and delivered EMPTY after the close; the attempt row carries the dataset id the intake resolved, so only
    the request's trigger can tell it from the refresh's own. The entry upsert is a Postgres ON CONFLICT, so it is
    answered here with the id an exact identity match resolves to (the system tier proves Postgres does).
    """
    row = world.rows['older']

    async def exact_match(_db, _created, **_kwargs) -> uuid.UUID:
        return row.id

    monkeypatch.setattr(delivery_intake, 'upsert_entry_in_transaction', exact_match)
    body = StoreAssetDatasetBody(
        owner=row.owner,
        source=row.source,
        feed=row.feed,
        granularity=row.granularity,
        start=row.start,
        expiry_type=row.expiry_type,
        update_type=row.update_type,
    )
    path = StoreAssetDatasetPath(asset_type=row.asset_type, data_type=row.data_type, asset_symbol=row.asset_symbol)
    async with AsyncSession(world.engine) as db:
        await data_action_request.submit_bars(
            path,
            body,
            db,
            ScriptedClient(),
            trigger=IngestRunTrigger.REQUEST,
            client_label=row.owner,
            start=TAIL['older'],
        )
    await world.deliver('older', bars=False, finished_at=NOW)
    (attempt,) = [run for run in await world.runs() if run.dataset_id == row.id]
    assert (attempt.trigger, attempt.state, attempt.requested_start) == (
        IngestRunTrigger.REQUEST,
        IngestRunState.EMPTY,
        TAIL['older'],
    )

    client = ScriptedClient()
    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert world.requested(client) == list(BEHIND_ORDER)
    assert result.skipped == 0


@pytest.mark.asyncio
async def test_datasets_always_empty_hold_at_most_half_the_cap_and_healthy_ones_are_served(world: World, monkeypatch):
    """Twelve delisted symbols, empty at their last attempt before the close, against the four healthy datasets."""
    monkeypatch.setenv(refresh_module.MAX_PER_PASS_ENV, '4')
    dead = {f'dead{i:02d}': entry(f'{SYMBOL_PREFIX}X{i:02d}', update_type=UpdateType.DAILY) for i in range(12)}
    world.rows.update(dead)
    await _add(world, *dead.values())
    await _add(world, *(bar(world.rows[name], ny_midnight(2)) for name in dead))
    await _add(
        world,
        *(
            _scheduled_run(
                world,
                name,
                state=IngestRunState.EMPTY,
                finished_at=LAST_CLOSE - timedelta(hours=20),
                # Last bar Fri 2 Oct: the tail is the next local midnight, Sat 3 Oct.
                requested_start=ny_midnight(3),
            )
            for name in dead
        ),
    )
    client = ScriptedClient()

    first = await world.refresher(client).run_pass(DataSource.ALPACA_API)
    first_requested = world.requested(client)
    # Ingest delivers pass 1's requests (the push path: the pass did not wait): the dead ones EMPTY, the healthy ones
    # with a bar (which the double does not store, so they stay behind), all after the last close.
    for name in first_requested:
        await world.deliver(name, bars=not name.startswith('dead'), finished_at=NOW)
    client.requests.clear()
    second = await world.refresher(client).run_pass(DataSource.ALPACA_API)
    second_requested = world.requested(client)

    for requested in (first_requested, second_requested):
        assert len(requested) == 4
        assert len([name for name in requested if name.startswith('dead')]) <= 2, requested
    assert first_requested[:2] == ['older', 'empty']
    assert set(BEHIND_ORDER) <= set(first_requested) | set(second_requested), 'a healthy dataset was starved'
    assert first.deferred == 16 - 4
    # The two dead ones tried in pass 1 ended EMPTY after the close (their attempt rows, joined to their SCHEDULE
    # requests): skipped in pass 2.
    assert second.skipped == 2


@pytest.mark.asyncio
async def test_the_due_again_take_whatever_the_others_leave(world: World, monkeypatch):
    """With fewer productive datasets than half the cap, the due-again ones fill the rest of it."""
    dead = {f'dead{i}': entry(f'{SYMBOL_PREFIX}Y{i}', update_type=UpdateType.DAILY) for i in range(8)}
    world.rows.update(dead)
    await _add(world, *dead.values())
    await _add(
        world,
        *(
            _scheduled_run(
                world,
                name,
                state=IngestRunState.EMPTY,
                finished_at=LAST_CLOSE - timedelta(hours=20),
                requested_start=dead[name].start,
            )
            for name in dead
        ),
    )
    client = ScriptedClient()

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert result.refreshed == DEFAULT_MAX_PER_PASS
    assert world.requested(client)[:4] == list(BEHIND_ORDER)


# ------------------------------------------------------------------------------------------------- round 2: retire race
# REPAIRED FOR THE PUSH PATH (tj-7zjov1.28). The race is no longer inside the pass: the pass submits and returns, and
# the answer lands later at the delivery intake, which judges the entry under its lock (the drop rule). So the
# operator's write is committed BETWEEN the pass and the delivery, the window that replaced "while the vendor call is
# in flight". PassResult.discarded is gone: a dropped delivery is the REQUEST's outcome, EMPTY with cause DISCARDED.
# The drop rule's own cases (a gone entry, a stopped one, the attempt row, the verdict) are test_delivery_intake.py's;
# these keep the refresh's end-to-end promise: the operator's Stop or delete is never undone by a refresh.


async def _operator_write(world: World, name: str, values: dict[str, Any] | None) -> None:
    async with AsyncSession(world.engine) as db:
        target = StoreDatasetEntry.id == world.rows[name].id
        if values is None:
            await db.execute(delete(StockMarketActivity).where(StockMarketActivity.dataset_id == world.rows[name].id))
            await db.execute(delete(StoreDatasetEntry).where(target))
        else:
            await db.execute(update(StoreDatasetEntry).where(target).values(**values))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize('values', [{'collect': False}, None], ids=['retired', 'deleted'])
async def test_a_retire_or_delete_before_the_delivery_drops_the_answer(world: World, values):
    result = await world.refresher(ScriptedClient()).run_pass(DataSource.ALPACA_API)
    assert result == PassResult(behind=4, refreshed=4)

    await _operator_write(world, 'behind', values)
    verdict = await world.deliver('behind')

    assert verdict is DeliveryVerdict.FINAL
    assert world.rows['behind'].id not in {batch.dataset_id for batch in world.bar_batches}, 'bars written'
    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        stored = (
            await db.execute(select(StoreDatasetEntry).where(StoreDatasetEntry.id == world.rows['behind'].id))
        ).scalar_one_or_none()
    if values is None:
        assert stored is None, 'the refresh recreated a deleted entry'
    else:
        assert stored.collect is False, 'the system actor undid the operator stop'
    request = await world.request_of('behind')
    assert IngestRequestState(request.state) is IngestRequestState.EMPTY
    assert request.failure_cause == IngestRequestFailureCause.DISCARDED
    (run,) = [run for run in await world.runs() if run.dataset_id == world.rows['behind'].id]
    assert (run.state, run.bars_written, run.error_reason) == (IngestRunState.EMPTY, 0, None)
    changed = [event_.id for event_ in world.events if isinstance(event_, DatasetChanged)]
    assert world.rows['behind'].id not in changed


@pytest.mark.asyncio
async def test_a_deletion_date_set_before_the_delivery_does_not_drop_the_answer(world: World):
    """Decision tj-hhy3dk: an expiry written while the request is out stops nothing; the answer is stored."""
    await world.refresher(ScriptedClient()).run_pass(DataSource.ALPACA_API)

    await _operator_write(world, 'behind', {'expiry': NOW + timedelta(days=3)})
    await world.deliver('behind')

    assert world.rows['behind'].id in {batch.dataset_id for batch in world.bar_batches}, 'the answer was dropped'
    assert IngestRequestState((await world.request_of('behind')).state) is IngestRequestState.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize('collect', [None, False, True], ids=['omitted', 'false', 'true'])
async def test_the_post_records_its_lifecycle_fields_and_the_refresh_none(world: World, collect):
    """Which writer carries the lifecycle (tj-7zjov1.28; note H on the bead), through the real submit path.

    Before the push path this was the upsert's keyword: the refresh passed preserve_lifecycle and no collect, POST
    /store passed the body's collect. Now neither writes the entry at request time: each writes its REQUEST, and the
    first delivered attempt of a POST writes the entry from it. So the property moved onto the request row: a POST
    records the body's expiry, expiry type and collect (an omitted collect is NULL, never true or false, so a repeat
    cannot undo an operator Stop), with trigger REQUEST and no dataset; the refresh records none of the three
    (test_each_refresh_writes_a_schedule_request_naming_its_dataset_before_it_submits).
    """
    row = world.rows['behind']
    expiry = NOW + timedelta(days=40)
    body = StoreAssetDatasetBody(
        owner=row.owner,
        source=row.source,
        granularity=row.granularity,
        start=ny_midnight(1, 6),
        expiry=expiry,
        expiry_type=ExpiryType.ROLLING,
        update_type=UpdateType.DAILY,
        collect=collect,
    )
    path = StoreAssetDatasetPath(asset_type=row.asset_type, data_type=row.data_type, asset_symbol=f'{SYMBOL_PREFIX}P')
    async with AsyncSession(world.engine) as db:
        await data_action_request.submit_bars(
            path, body, db, ScriptedClient(), trigger=IngestRunTrigger.REQUEST, client_label=body.owner
        )

    (stored,) = await world.requests()
    assert stored.trigger is IngestRunTrigger.REQUEST
    assert stored.dataset_id is None
    assert stored.client_label == row.owner
    assert stored.expiry == expiry
    assert stored.expiry_type == ExpiryType.ROLLING.value
    assert stored.collect is collect


# ------------------------------------------------------------------------------------------------- the tail across DST
# tj-grna9p.154. A 1day or 1week bar is stamped at LOCAL midnight in the calendar's zone (XNYS: America/New_York),
# 05:00Z under EST and 04:00Z under EDT. 2026's changes are Sun 8 Mar (spring forward) and Sun 1 Nov (fall back). A
# fixed offset from a winter stamp lands an hour AFTER the next summer stamp, and a vendor filtering on
# timestamp >= start would drop that bar. Every expected value below is a literal instant, never computed.

XNYS = calendar_for_source(DataSource.ALPACA_API)


def _z(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def _catalog_row(granularity: Granularity, last_bar: datetime, written: datetime | None = None) -> CatalogRow:
    """A DAILY row whose newest bar was written long after its period closed (final), unless `written` says so."""
    return CatalogRow(
        id=uuid.uuid4(),
        asset_symbol=f'{SYMBOL_PREFIX}T',
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        feed=Feed.IEX,
        granularity=granularity,
        start=_z('2026-01-05T05:00:00Z'),
        end=None,
        update_type=UpdateType.DAILY,
        expiry_type=ExpiryType.ROLLING,
        expiry=None,
        owner='operator',
        last_bar=last_bar,
        last_bar_written_at=last_bar + timedelta(days=40) if written is None else written,
        collect=True,
    )


TAIL_CASES = {
    # The two the bead names. A fixed 7 days would give 2026-03-09T05:00Z (an hour past the bar) and 2026-11-02T04:00Z.
    '1week spring forward': (Granularity.ONE_WEEK, '2026-03-02T05:00:00Z', '2026-03-09T04:00:00Z'),
    '1week fall back': (Granularity.ONE_WEEK, '2026-10-26T04:00:00Z', '2026-11-02T05:00:00Z'),
    # The vendor may stamp a week on any day of it: the tail is the next week's Monday, not the stamp plus a week.
    '1week stamped mid-week': (Granularity.ONE_WEEK, '2026-03-04T05:00:00Z', '2026-03-09T04:00:00Z'),
    # A Friday's tail is the Saturday (by local date, not the next session), in each zone offset.
    '1day Friday before spring forward': (Granularity.ONE_DAY, '2026-03-06T05:00:00Z', '2026-03-07T05:00:00Z'),
    '1day Friday before fall back': (Granularity.ONE_DAY, '2026-10-30T04:00:00Z', '2026-10-31T04:00:00Z'),
    # The day OF the change: a fixed 24h would give 05:00Z and 04:00Z, each an hour off the next local midnight.
    '1day the spring-forward Sunday': (Granularity.ONE_DAY, '2026-03-08T05:00:00Z', '2026-03-09T04:00:00Z'),
    '1day the fall-back Sunday': (Granularity.ONE_DAY, '2026-11-01T04:00:00Z', '2026-11-02T05:00:00Z'),
    # Shorter bars are absolute durations; a month keeps its 4-week offset (it always lands before the next month).
    '1hour across spring forward keeps the offset': (
        Granularity.ONE_HOUR,
        '2026-03-06T20:00:00Z',
        '2026-03-06T21:00:00Z',
    ),
    '1month across spring forward keeps the offset': (
        Granularity.ONE_MONTH,
        '2026-03-01T05:00:00Z',
        '2026-03-29T05:00:00Z',
    ),
}


@pytest.mark.parametrize(('granularity', 'last_bar', 'expected'), TAIL_CASES.values(), ids=TAIL_CASES.keys())
def test_the_tail_starts_at_the_first_instant_the_next_bar_can_be_stamped(granularity, last_bar, expected):
    tail = _fetch_start(_catalog_row(granularity, _z(last_bar)), XNYS)

    assert tail == _z(expected)
    assert tail.utcoffset() is not None, 'a naive tail names no instant'


def test_a_provisional_weekly_bar_is_asked_for_again_from_itself_across_the_change():
    """Written mid-week, before its week closed: the tail is the bar itself, inclusive, not the next Monday."""
    last_bar = _z('2026-03-02T05:00:00Z')
    row = _catalog_row(Granularity.ONE_WEEK, last_bar, written=_z('2026-03-04T21:00:00Z'))

    assert _fetch_start(row, XNYS) == last_bar


@pytest.mark.asyncio
async def test_a_pass_after_spring_forward_asks_for_and_records_the_next_local_midnight(world: World):
    """End to end: the request ingest gets and the request row read back both name 2026-03-09T04:00Z."""
    weekly = entry(
        f'{SYMBOL_PREFIX}W',
        granularity=Granularity.ONE_WEEK,
        update_type=UpdateType.DAILY,
        start=_z('2026-02-02T05:00:00Z'),
    )
    world.rows['weekly'] = weekly
    await _add(world, weekly)
    await _add(world, bar(weekly, _z('2026-03-02T05:00:00Z')))
    client = ScriptedClient()

    # Sat 14 Mar: the week of 9 Mar has ended, so the dataset holding only the week of 2 Mar is behind.
    await world.refresher(client, now=_z('2026-03-14T12:00:00Z')).run_pass(DataSource.ALPACA_API)

    (request,) = [request for request in client.requests if request.asset_symbol == weekly.asset_symbol]
    assert request.start == _z('2026-03-09T04:00:00Z')
    # The REQUEST ROW is the refresh's record since the push path (tj-7zjov1.28); read back from its table.
    stored = await world.request_of('weekly')
    assert stored.requested_start == _z('2026-03-09T04:00:00Z')


# ------------------------------------------------------------------------------------------------- the lifespan


def _lifespan_fakes(monkeypatch, order: list[str], client: object) -> None:
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

    def build_client(_channel):
        order.append('submit_client.built')
        return client

    # The lifespan assigns these module globals; patched so they are restored and no later test sees this client.
    monkeypatch.setattr(app_depends, '__INGEST_CHANNEL', None)
    monkeypatch.setattr(app_depends, '__INGEST_SUBMIT_CLIENT', None)
    monkeypatch.setattr(app_depends, 'database', FakeDatabase())
    monkeypatch.setattr(app_depends, 'sweep_interrupted_runs', nothing)
    monkeypatch.setattr(app_depends, 'start_usage_loops', lambda: [])
    monkeypatch.setattr(app_depends, 'stop_usage_loops', nothing)
    monkeypatch.setattr(app_depends, 'start_reaper_loop', lambda: None)
    monkeypatch.setattr(app_depends, 'stop_reaper_loop', nothing)
    monkeypatch.setattr(app_depends, 'initialize_latency_client', lambda *a, **k: None)
    monkeypatch.setattr(app_depends, 'init_debugger', lambda: None)
    monkeypatch.setattr(app_depends, 'startup_logs', lambda _app: None)
    monkeypatch.setattr(app_depends, 'teardown_logs', lambda _app: None)
    monkeypatch.setattr(app_depends, 'SharedWorkerPool', FakePool)
    monkeypatch.setattr(app_depends, 'target_from_env', lambda _name: 'ingest:1')
    monkeypatch.setattr(app_depends, 'create_channel', lambda _target: FakeChannel())
    # The refresh is handed the SUBMIT client, the one client the lifespan builds since the FetchDataset client was
    # removed (tj-7zjov1.272).
    monkeypatch.setattr(app_depends, 'GrpcIngestSubmitClient', build_client)
    # The instance watcher shares the client and is not this test's subject: it would run a real pass.
    monkeypatch.setenv('INSTANCE_WATCH_ENABLED', 'false')
    # The asset-list snapshot schedule (tj-7zjov1.138) shares it too and is not this test's subject: it would seed from
    # the uninitialised database module. Its own lifespan place is test_store_grpc_host.py's and
    # test_asset_snapshot_schedule.py's.
    monkeypatch.setenv('ASSET_SNAPSHOT_ENABLED', 'false')


@pytest.mark.asyncio
async def test_the_lifespan_starts_the_refresh_after_the_submit_client_and_stops_it_before_the_channel(monkeypatch):
    order: list[str] = []
    client = object()
    passes: list[Any] = []

    async def run_pass(self, source, calendar=None):
        passes.append(self._client)
        await asyncio.sleep(0)  # yields, as a real pass does
        return PassResult()

    real_start, real_stop = app_depends.start_daily_refresh_loops, app_depends.stop_daily_refresh_loops
    started: list[asyncio.Task] = []

    async def start(submit_client):
        order.append('refresh.start')
        tasks = await real_start(submit_client)
        started.extend(tasks)
        return tasks

    async def stop(tasks):
        order.append('refresh.stop')
        await real_stop(tasks)

    _lifespan_fakes(monkeypatch, order, client)
    monkeypatch.setenv(refresh_module.ENABLED_ENV, 'true')
    monkeypatch.setattr(DailyRefresher, 'run_pass', run_pass)
    monkeypatch.setattr(app_depends, 'start_daily_refresh_loops', start)
    monkeypatch.setattr(app_depends, 'stop_daily_refresh_loops', stop)
    # The lifespan builds the StoreService host from the bind env first (tj-7zjov1.27); none is bound here.
    from contextlib import nullcontext

    monkeypatch.setattr(app_depends, 'build_grpc_host', nullcontext)

    async with app_depends.lifespan(app):
        await _turns(50, until=lambda: bool(passes))
        await _turns(50)
        assert passes == [client], 'the catch-up pass did not run once through the client the lifespan built'
        assert started and not any(task.done() for task in started)

    assert all(task.cancelled() for task in started)
    assert order.index('submit_client.built') < order.index('refresh.start')
    assert order.index('refresh.stop') < order.index('channel.close')
    assert order.index('refresh.stop') < order.index('database.shutdown')


@pytest.mark.asyncio
async def test_the_lifespan_starts_no_refresh_when_it_is_off(monkeypatch):
    order: list[str] = []
    started = _Started()
    _lifespan_fakes(monkeypatch, order, object())
    monkeypatch.setenv(refresh_module.ENABLED_ENV, 'off')
    monkeypatch.setattr(refresh_module, 'run_periodic', started)
    # The lifespan builds the StoreService host from the bind env first (tj-7zjov1.27); none is bound here.
    from contextlib import nullcontext

    monkeypatch.setattr(app_depends, 'build_grpc_host', nullcontext)

    async with app_depends.lifespan(app):
        pass

    assert started.calls == []
    assert 'channel.close' in order
