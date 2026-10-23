"""The interim DAILY refresh (app/daily_refresh.py), driven over real rows in SQLite (validator, gating tj-grna9p.22).

THE DESIGN is the architect's ruling on tj-utqd77 Q1 and its notes on tj-grna9p.22 (2026-10-13), which win over the
bead's body: the STORE triggers and ingest sees an ORDINARY fetch (no new message type, no request queue); INTERIM
until ingest's scheduler (tj-pd41mt Addendum 1) lands; one `run_periodic` per source with a trading calendar on
`AfterTradingEvent(calendar, CLOSE, settle)`; every pass derives its work from the DATABASE (no job store, no replay);
only the MISSING TAIL of each behind, non-retired DAILY dataset is requested, through the existing fetch path and the
run recorder (trigger SCHEDULE, actor `scheduler`), one dataset at a time, capped per pass, with bounded backoff; a
retired subscription is never refreshed (an expiry on DAILY retires it at once, clearing it resumes: the .86 rule); a
dataset with a RUNNING ingest is skipped; one worker only. The body's "Postgres advisory lock" does not apply under
the single-worker invariant.

WHAT TIER THIS IS. The real pass, the real catalog read, the real RUNNING-ingest read, the real run recorder and the
real POST /store worker (store_market_activity_worker) with its real own-overlap check, over real tables in
in-memory SQLite, with the real XNYS calendar and an injected clock and sleep. The fetch client is the shared
RecordingFetchClient double (fetch_double.py), scripted per symbol. Two writes cannot run on SQLite because they are
Postgres INSERT ... ON CONFLICT statements: the entry upsert and the bar write. They are replaced by recorders, so
this tier proves WHAT the worker asks to write (the entry's identity is the declared one, the fetch is the tail);
that Postgres then resolves that write to the SAME entry, with no second one and no own-overlap 409, is
tests/system/test_daily_refresh_pass.py's.
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
from sqlalchemy import delete, event, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from common.enums.data_stock import DataSource, Feed, UpdateType
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
)
from data.store.app.database.crud.stock.store_dataset_entry import OwnOverlapConflict
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.events import DatasetChange, DatasetChanged, IngestRunChanged
from data.store.app.freshness import calendar_for_source
from data.store.app.ingest import data_action_request, run_recording
from data.store.app.main import app
from data.store.tests.fetch_double import FetchScript, RecordingFetchClient, accepted_stream
from data.store.tests.test_ui_datasets_route import (  # noqa: F401 (aware_sqlite_datetimes is a fixture)
    NOW,
    _sqlite_timezone,
    aware_sqlite_datetimes,
    bar,
    entry,
    ny_midnight,
    run_of,
)
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
# (last bar the 6th), `retired` (an expiry a day AHEAD: an expiry retires a DAILY dataset at once), `running` (a
# RUNNING ingest), `static` (a STATIC dataset with gaps: not a subscription).
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


def _seed() -> dict[str, StoreDatasetEntry]:
    daily = {'update_type': UpdateType.DAILY}
    rows = {
        'older': entry(f'{SYMBOL_PREFIX}A', **daily),
        'empty': entry(f'{SYMBOL_PREFIX}B', start=ny_midnight(1), **daily),
        'behind2': entry(f'{SYMBOL_PREFIX}C', feed=Feed.SIP, **daily),
        'behind': entry(f'{SYMBOL_PREFIX}D', **daily),
        'fresh': entry(f'{SYMBOL_PREFIX}E', **daily),
        'retired': entry(f'{SYMBOL_PREFIX}F', expiry=NOW + timedelta(days=1), **daily),
        'running': entry(f'{SYMBOL_PREFIX}G', **daily),
        'static': entry(f'{SYMBOL_PREFIX}H', start=ny_midnight(1, 9), end=ny_midnight(6)),
    }
    rows['behind'].owner = SECRET_OWNER
    return rows


class World:
    """The engine, the seeded rows, what the worker asked to write, and every event with the commit count."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.rows: dict[str, StoreDatasetEntry] = {}
        self.upserts: list[Any] = []
        self.upsert_kwargs: list[dict[str, Any]] = []
        self.bar_batches: list[Any] = []
        self.events: list[Any] = []
        self.sleeps: list[float] = []

    def name_of(self, symbol: str) -> str:
        return next(name for name, row in self.rows.items() if row.asset_symbol == symbol)

    def requested(self, client: RecordingFetchClient) -> list[str]:
        return [self.name_of(request.asset_symbol) for request in client.requests]

    async def runs(self) -> list[IngestRun]:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            return list((await db.execute(select(IngestRun).where(IngestRun.dataset_id.is_not(None)))).scalars())

    async def set_expiry(self, name: str, expiry: datetime | None) -> None:
        async with AsyncSession(self.engine) as db:
            await db.execute(
                update(StoreDatasetEntry).where(StoreDatasetEntry.id == self.rows[name].id).values(expiry=expiry)
            )
            await db.commit()

    async def finish_running(self) -> None:
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(IngestRun).where(IngestRun.state == IngestRunState.RUNNING))
            await db.commit()

    def refresher(self, client: RecordingFetchClient, now: datetime = NOW) -> DailyRefresher:
        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        return DailyRefresher(client, clock=lambda: now, sleep=sleep)


class ScriptedClient(RecordingFetchClient):
    """The shared double, with one script per symbol; any other symbol is served one bar on the tape it named.

    Ingest checks a named feed and never substitutes another (a preference it cannot honour is a refused ack), so
    the default answer resolves the requested tape.
    """

    def __init__(self, scripts: dict[str, FetchScript] | None = None) -> None:
        super().__init__()
        self.scripts = scripts or {}

    async def fetch(self, request):
        served = FetchScript(events=accepted_stream([[0]], feed=request.feed or Feed.IEX))
        self.script = self.scripts.get(request.asset_symbol, served)
        async for event_ in super().fetch(request):
            yield event_


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
    engine = _engine()
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: StoreDatasetEntry.metadata.create_all(
                sync, tables=[StoreDatasetEntry.__table__, StockMarketActivity.__table__, IngestRun.__table__]
            )
        )
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

    async def upsert(_db: AsyncSession, created: Any, **kwargs: Any) -> uuid.UUID:
        # The Postgres upsert cannot run on SQLite. Record what the worker asked for and answer the id an EXACT
        # identity match would resolve to; a new id would be a second entry (the system test proves Postgres).
        state.upserts.append(created)
        state.upsert_kwargs.append(kwargs)
        for row in state.rows.values():
            if (row.owner, row.asset_symbol, row.start, row.feed) == (
                created.owner,
                created.asset_symbol,
                created.start,
                created.feed,
            ) and created.end is None:
                return row.id
        return uuid.uuid4()

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
    monkeypatch.setattr(run_recording, 'own_session', own_session)
    monkeypatch.setattr(refresh_module, 'publish', state.events.append)
    monkeypatch.setattr(run_recording, 'publish', state.events.append)
    monkeypatch.setattr(data_action_request, 'upsert_entry_in_transaction', upsert)
    monkeypatch.setattr(data_action_request, 'write_market_activity_in_transaction', write_bars)
    try:
        yield state
    finally:
        await engine.dispose()


def _fails(reason: Reason = Reason.VENDOR_UNAVAILABLE) -> FetchScript:
    return FetchScript(events=[], raises=ExogenousError(reason, 'the vendor is not there'))


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
    assert result == PassResult()


@pytest.mark.asyncio
async def test_nothing_is_behind_before_the_session_closes(world: World):
    """At 19:00Z on the 6th the last completed session is the 5th: `behind` (last bar the 5th) is not owed."""
    client = ScriptedClient()

    await world.refresher(client, now=datetime(2026, 10, 6, 19, 0, tzinfo=UTC)).run_pass(DataSource.ALPACA_API)

    assert 'behind' not in world.requested(client)
    assert world.requested(client) == ['older', 'empty', 'behind2']


@pytest.mark.asyncio
async def test_a_retired_subscription_is_never_refreshed_and_a_cleared_one_resumes(world: World):
    """The .86 rule: setting an expiry on DAILY retires it at once (past or future); clearing it resumes."""
    for expiry in (NOW + timedelta(days=30), NOW - timedelta(hours=1)):
        await world.set_expiry('behind', expiry)
        client = ScriptedClient()
        await world.refresher(client).run_pass(DataSource.ALPACA_API)
        assert 'behind' not in world.requested(client), f'retired with expiry {expiry} was refreshed'

    await world.set_expiry('behind', None)
    client = ScriptedClient()
    await world.refresher(client).run_pass(DataSource.ALPACA_API)
    assert 'behind' in world.requested(client)

    await world.set_expiry('retired', None)
    client = ScriptedClient()
    await world.refresher(client).run_pass(DataSource.ALPACA_API)
    assert 'retired' in world.requested(client)


@pytest.mark.asyncio
async def test_a_dataset_with_a_running_ingest_is_skipped_and_picked_up_when_it_ends(world: World):
    client = ScriptedClient()
    await world.refresher(client).run_pass(DataSource.ALPACA_API)
    assert 'running' not in world.requested(client)

    await world.finish_running()
    client = ScriptedClient()
    await world.refresher(client).run_pass(DataSource.ALPACA_API)
    assert 'running' in world.requested(client)


# ------------------------------------------------------------------------------------------------- the request


@pytest.mark.asyncio
async def test_each_fetch_asks_only_for_the_missing_tail_with_the_stored_feed(world: World):
    """From the last bar plus one bar (the declared start when there are no bars), open-ended, the stored tape named."""
    client = ScriptedClient()

    await world.refresher(client).run_pass(DataSource.ALPACA_API)

    by_name = {world.name_of(request.asset_symbol): request for request in client.requests}
    for name in BEHIND_ORDER:
        row, request = world.rows[name], by_name[name]
        expected_start = row.start if name == 'empty' else LAST_BAR[name] + row.granularity.offset
        assert request.start == expected_start, name
        assert request.end is None, name
        assert request.feed is row.feed, f'{name}: the stored feed is not the preference sent'
        assert request.update_type is UpdateType.DAILY
        assert request.owner == row.owner
        assert request.granularity is row.granularity
    assert by_name['behind2'].feed is Feed.SIP


@pytest.mark.asyncio
async def test_the_entry_keeps_its_declared_range_as_identity(world: World):
    """The narrowed fetch never reaches the entry: the upsert gets the DECLARED start and the open end.

    The own-overlap check here is the real one over SQLite: had the overlap key carried the narrowed start, the
    stored entry would overlap it without being an exact repeat and the pass would refuse every dataset (409).
    """
    client = ScriptedClient()

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert result.failed == 0
    assert [world.name_of(created.asset_symbol) for created in world.upserts] == list(BEHIND_ORDER)
    for created in world.upserts:
        row = world.rows[world.name_of(created.asset_symbol)]
        assert created.start == row.start
        assert created.end is None
        assert created.owner == row.owner
        assert created.update_type is UpdateType.DAILY
        assert created.expiry is None
    # Every bar the fetch wrote is stamped with the EXISTING entry's id: nothing resolved to a second entry.
    assert {batch.dataset_id for batch in world.bar_batches} == {world.rows[name].id for name in BEHIND_ORDER}


@pytest.mark.asyncio
async def test_the_narrowed_start_would_collide_if_it_reached_the_overlap_key(world: World):
    """The guard the previous test leans on is real: a body whose START is the tail collides with the entry."""
    row = world.rows['behind']
    narrowed = StoreAssetDatasetBody(
        owner=row.owner,
        source=row.source,
        feed=row.feed,
        granularity=row.granularity,
        start=LAST_BAR['behind'] + row.granularity.offset,
        expiry_type=row.expiry_type,
        update_type=row.update_type,
    )
    path = StoreAssetDatasetPath(asset_type=row.asset_type, data_type=row.data_type, asset_symbol=row.asset_symbol)
    async with AsyncSession(world.engine) as db:
        with pytest.raises(OwnOverlapConflict):
            await data_action_request.store_market_activity_worker(path, narrowed, db, ScriptedClient())


@pytest.mark.asyncio
async def test_without_fetch_start_the_worker_asks_for_the_body_range_exactly(world: World):
    """POST /store's path is unchanged: no fetch_start, the request's start is the body's."""
    row = world.rows['behind']
    body = StoreAssetDatasetBody(
        owner=row.owner,
        source=row.source,
        granularity=row.granularity,
        start=row.start,
        expiry_type=row.expiry_type,
        update_type=row.update_type,
    )
    path = StoreAssetDatasetPath(asset_type=row.asset_type, data_type=row.data_type, asset_symbol=row.asset_symbol)
    client = ScriptedClient()
    async with AsyncSession(world.engine) as db:
        await data_action_request.store_market_activity_worker(path, body, db, client)

    (request,) = client.requests
    assert request.start == row.start
    assert request.feed is None
    assert data_action_request._fetch_request(path, body) == data_action_request._fetch_request(path, body, None)


# ------------------------------------------------------------------------------------------------- the record


@pytest.mark.asyncio
async def test_each_refresh_is_a_schedule_run_by_the_scheduler_with_the_tail_it_asked_for(world: World):
    client = ScriptedClient()

    await world.refresher(client).run_pass(DataSource.ALPACA_API)

    runs = {run.dataset_id: run for run in await world.runs() if run.state is not IngestRunState.RUNNING}
    for name in BEHIND_ORDER:
        row = world.rows[name]
        run = runs[row.id]
        assert run.trigger is IngestRunTrigger.SCHEDULE
        assert run.actor == SCHEDULER_ACTOR, "the run is the scheduler's, whatever owner the dataset has"
        expected_start = row.start if name == 'empty' else LAST_BAR[name] + row.granularity.offset
        assert run.requested_start == expected_start
        assert run.requested_end is None
        assert run.state is IngestRunState.SUCCEEDED
        assert run.bars_written == 1


@pytest.mark.asyncio
async def test_dataset_changed_is_published_after_each_successful_refresh(world: World):
    client = ScriptedClient({_symbol(world, 'behind2'): _fails()})

    await world.refresher(client).run_pass(DataSource.ALPACA_API)

    changed = [event_ for event_ in world.events if isinstance(event_, DatasetChanged)]
    assert changed == [
        DatasetChanged(world.rows[name].id, DatasetChange.UPDATED) for name in ('older', 'empty', 'behind')
    ]
    # Each DatasetChanged follows that dataset's terminal run event, i.e. the fetch's commit.
    for position, event_ in enumerate(world.events):
        if isinstance(event_, DatasetChanged):
            previous = world.events[position - 1]
            assert isinstance(previous, IngestRunChanged)
            assert previous.run.state is IngestRunState.SUCCEEDED


@pytest.mark.asyncio
async def test_a_failed_refresh_is_recorded_failed_with_its_reason(world: World):
    client = ScriptedClient({_symbol(world, 'older'): _fails()})

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        failed = list((await db.execute(select(IngestRun).where(IngestRun.state == IngestRunState.FAILED))).scalars())
    (run,) = failed
    assert run.trigger is IngestRunTrigger.SCHEDULE
    assert run.actor == SCHEDULER_ACTOR
    assert run.error_reason == Reason.VENDOR_UNAVAILABLE
    assert result == PassResult(behind=4, refreshed=3, failed=1, deferred=0)


@pytest.mark.asyncio
async def test_a_refused_ack_writes_no_entry(world: World):
    """A tape ingest can no longer serve is refused in the ack: nothing is upserted, so no second entry.

    FEED_NOT_AVAILABLE is REFUSED in the vocabulary, so it is a permanent refusal: counted in `refused`, not as a
    transient failure, and it neither backs off nor stops the pass.
    """
    refused = FetchScript(events=[], raises=InvalidRequestError(Reason.FEED_NOT_AVAILABLE, 'not this tape'))
    client = ScriptedClient({_symbol(world, 'behind2'): refused})

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert 'behind2' not in [world.name_of(created.asset_symbol) for created in world.upserts]
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
    """One dataset at a time: the next fetch starts only after the previous one has finished."""
    in_flight: list[int] = []
    peak = [0]

    class Serial(ScriptedClient):
        async def fetch(self, request):
            in_flight.append(1)
            peak[0] = max(peak[0], len(in_flight))
            try:
                await asyncio.sleep(0)
                async for event_ in super().fetch(request):
                    yield event_
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
    limited = FetchScript(
        events=[], raises=ExogenousError.from_retry_after(Reason.VENDOR_RATE_LIMITED, 'slow down', 60)
    )
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


def _tail(world: World, name: str) -> datetime:
    row = world.rows[name]
    return row.start if name == 'empty' else LAST_BAR[name] + row.granularity.offset


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


def _refused(reason: Reason = Reason.FEED_NOT_AVAILABLE) -> FetchScript:
    return FetchScript(events=[], raises=InvalidRequestError(reason, 'refused'))


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

    client.requests.clear()
    second = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    # The refusals failed before the ack (no dataset id): matched on the request's own fields.
    assert world.requested(client) == ['behind']
    assert second == PassResult(behind=4, refreshed=1, skipped=3)


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
    client = ScriptedClient({_symbol(world, name): FetchScript(events=[], raises=error) for name in BEHIND_ORDER})

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert world.requested(client) == ['older', 'empty', 'behind2']
    assert world.sleeps == [DEFAULT_BACKOFF_SECONDS, 2 * DEFAULT_BACKOFF_SECONDS]
    assert result == PassResult(behind=4, refreshed=0, failed=3, deferred=1, refused=0)


@pytest.mark.asyncio
async def test_after_a_failed_daily_run_the_next_pass_starts_at_the_last_stored_bar_plus_one_bar(world: World):
    """After a failed DAILY run the next pass asks again from the last stored bar plus one bar.

    The 2026-10-06 acceptance on tj-grna9p.22 (decision .55 option B): a failed DAILY update is not retried by
    itself, and the missed day is refetched by the next pass. Traceability gap filled by tj-grna9p.92.
    """
    failing = ScriptedClient({_symbol(world, 'older'): _fails()})
    first = await world.refresher(failing).run_pass(DataSource.ALPACA_API)
    assert first.failed == 1
    (failed,) = await _finished_runs_of(world, 'older')

    client = ScriptedClient()
    await world.refresher(client).run_pass(DataSource.ALPACA_API)

    (again,) = [request for request in client.requests if request.asset_symbol == _symbol(world, 'older')]
    assert again.start == LAST_BAR['older'] + world.rows['older'].granularity.offset
    # Each run is attributed to the pass that wrote it, never ordered by started_at: on SQLite func.now() has
    # whole-second resolution, so both runs usually share a started_at and an ORDER BY falls to the random id.
    (succeeded,) = [run for run in await _finished_runs_of(world, 'older') if run.id != failed.id]
    assert (failed.state, succeeded.state) == (IngestRunState.FAILED, IngestRunState.SUCCEEDED)
    assert failed.requested_start == succeeded.requested_start == again.start


async def _finished_runs_of(world: World, name: str) -> list[IngestRun]:
    """Every run of the dataset's symbol that is no longer RUNNING, matched on symbol (a pre-ack run has no id)."""
    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        statement = select(IngestRun).where(
            IngestRun.asset_symbol == _symbol(world, name), IngestRun.state != IngestRunState.RUNNING
        )
        return list((await db.execute(statement)).scalars())


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
                requested_start=ny_midnight(2) + world.rows[name].granularity.offset,
            )
            for name in dead
        ),
    )
    client = ScriptedClient({world.rows[name].asset_symbol: FetchScript(events=accepted_stream([])) for name in dead})

    first = await world.refresher(client).run_pass(DataSource.ALPACA_API)
    first_requested = world.requested(client)
    client.requests.clear()
    second = await world.refresher(client).run_pass(DataSource.ALPACA_API)
    second_requested = world.requested(client)

    for requested in (first_requested, second_requested):
        assert len(requested) == 4
        assert len([name for name in requested if name.startswith('dead')]) <= 2, requested
    assert first_requested[:2] == ['older', 'empty']
    assert set(BEHIND_ORDER) <= set(first_requested) | set(second_requested), 'a healthy dataset was starved'
    assert first.deferred == 16 - 4
    # The two dead ones tried in pass 1 ended EMPTY after the close (the recorder's wall clock): skipped in pass 2.
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


class _RetireDuringFetch(ScriptedClient):
    """Commits an operator write for one symbol while its vendor call is in flight, before the ack is yielded."""

    def __init__(self, engine: AsyncEngine, symbol: str, values: dict[str, Any] | None) -> None:
        super().__init__()
        self.engine, self.symbol, self.values = engine, symbol, values

    async def fetch(self, request):
        if request.asset_symbol == self.symbol:
            async with AsyncSession(self.engine) as db:
                target = StoreDatasetEntry.asset_symbol == self.symbol
                if self.values is None:
                    await db.execute(delete(StockMarketActivity).where(StockMarketActivity.asset_symbol == self.symbol))
                    await db.execute(delete(StoreDatasetEntry).where(target))
                else:
                    await db.execute(update(StoreDatasetEntry).where(target).values(**self.values))
                await db.commit()
        async for event_ in super().fetch(request):
            yield event_


@pytest.mark.asyncio
@pytest.mark.parametrize('values', [{'expiry': NOW + timedelta(days=3)}, None], ids=['retired', 'deleted'])
async def test_a_retire_or_delete_during_the_fetch_drops_the_answer(world: World, values):
    client = _RetireDuringFetch(world.engine, _symbol(world, 'behind'), values)

    result = await world.refresher(client).run_pass(DataSource.ALPACA_API)

    assert result == PassResult(behind=4, refreshed=3, discarded=1)
    assert 'behind' not in [world.name_of(created.asset_symbol) for created in world.upserts], 'upserted anyway'
    assert world.rows['behind'].id not in {batch.dataset_id for batch in world.bar_batches}, 'bars written'
    async with AsyncSession(world.engine, expire_on_commit=False) as db:
        stored = (
            await db.execute(select(StoreDatasetEntry).where(StoreDatasetEntry.id == world.rows['behind'].id))
        ).scalar_one_or_none()
        (run,) = (
            await db.execute(
                select(IngestRun).where(
                    IngestRun.asset_symbol == _symbol(world, 'behind'), IngestRun.state != IngestRunState.RUNNING
                )
            )
        ).scalars()
    if values is None:
        assert stored is None, 'the refresh recreated a deleted entry'
    else:
        assert stored.expiry == values['expiry'], 'the system actor undid the operator retire'
    assert (run.state, run.bars_written, run.error_reason) == (IngestRunState.EMPTY, 0, None)
    changed = [event_.id for event_ in world.events if isinstance(event_, DatasetChanged)]
    assert world.rows['behind'].id not in changed


@pytest.mark.asyncio
async def test_the_refresh_upserts_with_preserve_lifecycle_and_post_store_does_not(world: World):
    await world.refresher(ScriptedClient()).run_pass(DataSource.ALPACA_API)
    assert world.upsert_kwargs and all(kwargs == {'preserve_lifecycle': True} for kwargs in world.upsert_kwargs)

    world.upsert_kwargs.clear()
    row = world.rows['behind']
    body = StoreAssetDatasetBody(
        owner=row.owner,
        source=row.source,
        granularity=row.granularity,
        start=row.start,
        expiry_type=row.expiry_type,
        update_type=row.update_type,
    )
    path = StoreAssetDatasetPath(asset_type=row.asset_type, data_type=row.data_type, asset_symbol=row.asset_symbol)
    async with AsyncSession(world.engine) as db:
        await data_action_request.store_market_activity_worker(path, body, db, ScriptedClient())
    assert world.upsert_kwargs == [{}], 'POST /store no longer calls the upsert exactly as before'


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
        order.append('fetch_client.built')
        return client

    # The lifespan assigns these module globals; patched so they are restored and no later test sees this client.
    monkeypatch.setattr(app_depends, '__INGEST_CHANNEL', None)
    monkeypatch.setattr(app_depends, '__INGEST_FETCH_CLIENT', None)
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
    monkeypatch.setattr(app_depends, 'GrpcIngestFetchClient', build_client)


@pytest.mark.asyncio
async def test_the_lifespan_starts_the_refresh_after_the_fetch_client_and_stops_it_before_the_channel(monkeypatch):
    order: list[str] = []
    client = object()
    passes: list[Any] = []

    async def run_pass(self, source, calendar=None):
        passes.append(self._client)
        await asyncio.sleep(0)  # yields, as a real pass does
        return PassResult()

    real_start, real_stop = app_depends.start_daily_refresh_loops, app_depends.stop_daily_refresh_loops
    started: list[asyncio.Task] = []

    async def start(fetch_client):
        order.append('refresh.start')
        tasks = await real_start(fetch_client)
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

    async with app_depends.lifespan(app):
        await _turns(50, until=lambda: bool(passes))
        await _turns(50)
        assert passes == [client], 'the catch-up pass did not run once through the client the lifespan built'
        assert started and not any(task.done() for task in started)

    assert all(task.cancelled() for task in started)
    assert order.index('fetch_client.built') < order.index('refresh.start')
    assert order.index('refresh.stop') < order.index('channel.close')
    assert order.index('refresh.stop') < order.index('database.shutdown')


@pytest.mark.asyncio
async def test_the_lifespan_starts_no_refresh_when_it_is_off(monkeypatch):
    order: list[str] = []
    started = _Started()
    _lifespan_fakes(monkeypatch, order, object())
    monkeypatch.setenv(refresh_module.ENABLED_ENV, 'off')
    monkeypatch.setattr(refresh_module, 'run_periodic', started)

    async with app_depends.lifespan(app):
        pass

    assert started.calls == []
    assert 'channel.close' in order
