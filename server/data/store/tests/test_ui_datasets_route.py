"""The /ui/v1 dataset reads and GET /store/{id}, driven over real rows in SQLite (validator, gating tj-grna9p.20).

WHAT TIER THIS IS. The real app, the real handlers, the real repository SQL and the real freshness rules, over
two real tables in an in-memory SQLite database (aiosqlite, the testing group's Docker-free async pool). The
clock is pinned through the routes' own utc_now dependency, never by patching a module. Requests go through
httpx's ASGI transport in the test's own event loop, so the async engine is never shared across loops, and the
app's lifespan never runs (no Postgres, no data_ingest).

WHAT SQLITE CANNOT SAY, AND THEREFORE WHAT THIS FILE DOES NOT CLAIM. Each is named so a Postgres-tier test
(follow-up, see the bead's verdict note) knows exactly what is left:
  * INSTANTS. SQLite stores a DateTime's wall clock and drops the offset; every instant here is UTC, so
    comparisons are between equal-offset strings. The fixture below hands timestamps back UTC-aware, which is
    what asyncpg does for timestamptz. That a [start, end) given in another offset compares as an instant is
    Postgres's to show.
  * covered_dates. Its SQL calls Postgres's timezone(zone, timestamptz). SQLite has no such function, so the
    fixture registers a stand-in that does the same conversion, purely so the statement prepares. Its date()
    then comes back as TEXT rather than a date, so a STATIC dataset WITH bars would be judged wrongly here.
    Every STATIC dataset in this file therefore holds no bars: GAPS is reached by missing sessions, COMPLETE by
    a range that holds no completed session. That covered_dates returns the right local dates is Postgres's.
  * THE 1000-DATASET CEILING the task's acceptance names is a latency measured on Postgres, with its index.
    The query BUDGET (a constant number of statements per request, never one per dataset) is the part that does
    not need Postgres, and it is pinned here.

THE DATE. NOW is Tue 6 Oct 2026, 22:00Z: after Tuesday's 20:00Z close, before Wednesday's 13:30Z open. So the
last completed session is Tuesday's, a DAILY dataset holding Tuesday is FRESH, one holding only Monday is LATE
(its deadline, Wednesday's open, has not come), and a 1min STREAM is judged against Tuesday's close.
"""

import base64
import json
import logging
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from google.protobuf import json_format
from sqlalchemy import event, insert
from sqlalchemy.dialects.sqlite import DATETIME
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from common.database.sql_alchemy_nullable_datetime import NullableDateTime
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app import dataset_catalog as dataset_catalog_module
from data.store.app.completeness import gap_intervals
from data.store.app.database.crud.stock import dataset_catalog as catalog_repo
from data.store.app.database.crud.stock.dataset_catalog import CatalogFilter
from data.store.app.database.database import async_db
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.dataset_catalog import (
    DEFAULT_COVERAGE_BUCKETS,
    MAX_COVERAGE_BUCKETS,
    MAX_COVERAGE_GAPS,
    evaluate_views,
)
from data.store.app.health import FreshnessStatus
from data.store.app.main import app
from data.store.app.trading_calendar import (
    NaiveDatetimeError,
    TradingCalendar,
    UnknownCalendarError,
    calendar_for_source,
    local_midnight,
    session_of,
)
from data.store.tests.problem_body import problem, validation_errors
from data.store.tests.test_coverage_buckets import XTKS
from routers.data_store.ui_datasets import utc_now
from routers.data_store.ui_mapping import BarPageIndexMessage


pytestmark = pytest.mark.data_store

# FOUND HERE FIRST (validator, gating tj-grna9p.20; fixed in 51b9db9). update_type and expiry_type are
# CustomColumn(OrderedEnum), a plain Integer column: the ORM hands back an int and only to_validated_schema converts
# it. The catalog read path copied the ints unconverted, so every list or detail read holding a row was a 500
# (ProtoMappingError) and a STATIC dataset was never given its covered sessions. Every case below that renders a
# summary, and test_a_static_dataset_is_judged_on_its_covered_sessions, is the regression guard for it.

NOW = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
NEW_YORK = ZoneInfo('America/New_York')
OWNER = 'operator'


def ny_midnight(day: int, month: int = 10) -> datetime:
    """A session's local midnight, as a daily bar is stamped, in UTC (the only offset SQLite can hold)."""
    return datetime(2026, month, day, tzinfo=NEW_YORK).astimezone(UTC)


def z(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


# ------------------------------------------------------------------------------------------------- fixture


def _sqlite_timezone(zone: str, stamp: str | None) -> str | None:
    """Postgres's timezone(zone, timestamptz) for a UTC wall clock stored as SQLite text: the local wall clock."""
    if stamp is None:
        return None
    local = datetime.fromisoformat(stamp).replace(tzinfo=UTC).astimezone(ZoneInfo(zone))
    return local.strftime('%Y-%m-%d %H:%M:%S.%f')


@pytest.fixture
def aware_sqlite_datetimes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read a timezone=True column back UTC-aware, as asyncpg reads timestamptz. Undone after the test."""
    original = DATETIME.result_processor

    def result_processor(self, dialect, coltype):
        process = original(self, dialect, coltype)
        if not self.timezone:
            return process

        def aware(value):
            value = process(value) if process else value
            return None if value is None else value.replace(tzinfo=UTC)

        return aware

    monkeypatch.setattr(DATETIME, 'result_processor', result_processor)


@pytest_asyncio.fixture
async def engine(aware_sqlite_datetimes) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, 'connect')
    def register_timezone(dbapi_connection, _record):
        dbapi_connection.run_async(lambda connection: connection.create_function('timezone', 2, _sqlite_timezone))
        # SQLite's LIKE ignores ASCII case by default and Postgres's does not, so a case-sensitive startswith would
        # pass the case-insensitive prefix cases here while failing them in production. Match Postgres.
        dbapi_connection.run_async(lambda connection: connection.execute('PRAGMA case_sensitive_like = ON'))

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: StoreDatasetEntry.metadata.create_all(
                # ingest_run since tj-grna9p.72: every list, detail and facets read now joins in the latest
                # run (last_run) and the RUNNING set (the running filter and facet).
                sync,
                tables=[StoreDatasetEntry.__table__, StockMarketActivity.__table__, IngestRun.__table__],
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    async def session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db

    async def pinned_now() -> datetime:
        return NOW

    app.dependency_overrides[async_db] = session
    app.dependency_overrides[utc_now] = pinned_now
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://store') as http:
            yield http
    finally:
        app.dependency_overrides.clear()


def entry(
    symbol: str,
    *,
    granularity: Granularity = Granularity.ONE_DAY,
    update_type: UpdateType = UpdateType.STATIC,
    start: datetime | None = None,
    end: datetime | None = None,
    expiry: datetime | None = None,
    source: DataSource = DataSource.ALPACA_API,
    feed: Feed = Feed.IEX,
) -> StoreDatasetEntry:
    return StoreDatasetEntry(
        id=uuid.uuid4(),
        owner=OWNER,
        source=source,
        feed=feed,
        asset_symbol=symbol,
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        granularity=granularity,
        start=start or ny_midnight(1, 9),
        # The open end is stored as the EPOCH sentinel, as the write path stores it (OverlapKey.column_values).
        end=NullableDateTime.EPOCH if end is None else end,
        expiry=expiry,
        # BULK only on STATIC: the schema refuses a subscription with a BULK expiry type, and AssetDatasetStore
        # (GET /store/{id}) validates every row it reads back against that rule.
        expiry_type=ExpiryType.BULK if update_type is UpdateType.STATIC else ExpiryType.ROLLING,
        update_type=update_type,
        created_at=NOW,
        updated_at=NOW,
    )


def bar(of: StoreDatasetEntry, at: datetime, close: float = 1.0) -> StockMarketActivity:
    return StockMarketActivity(
        dataset_id=of.id,
        source=of.source,
        asset_symbol=of.asset_symbol,
        feed=of.feed,
        granularity=of.granularity,
        timestamp=at,
        open=1.0,
        high=2.0,
        low=0.5,
        close=close,
        volume=100,
        trade_count=10,
        created_at=NOW,
        updated_at=NOW,
    )


def run_of(
    of: StoreDatasetEntry | None,
    *,
    minutes_ago: int,
    state: IngestRunState = IngestRunState.SUCCEEDED,
    run_id: uuid.UUID | None = None,
) -> IngestRun:
    """One ingest_run row (tj-grna9p.18), started MINUTES_AGO before NOW. OF None is a run with no dataset yet."""
    return IngestRun(
        id=run_id or uuid.uuid4(),
        dataset_id=None if of is None else of.id,
        trigger=IngestRunTrigger.REQUEST,
        actor=OWNER,
        asset_symbol='NONE' if of is None else of.asset_symbol,
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        granularity=Granularity.ONE_DAY,
        update_type=UpdateType.STATIC.value,
        feed=None if of is None else of.feed,
        requested_start=ny_midnight(1, 9),
        state=state,
        bars_written=0 if state is IngestRunState.RUNNING else 7,
        started_at=NOW - timedelta(minutes=minutes_ago),
        finished_at=None
        if state is IngestRunState.RUNNING
        else NOW - timedelta(minutes=minutes_ago) + timedelta(seconds=3),
    )


async def store(engine: AsyncEngine, *rows: Any) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as db:
        db.add_all([row for row in rows if isinstance(row, StoreDatasetEntry)])
        await db.flush()
        db.add_all([row for row in rows if isinstance(row, StockMarketActivity | IngestRun)])
        await db.commit()


# A weekend: [Sat 3 Oct, Mon 5 Oct) local, which holds no session, so a STATIC dataset over it is COMPLETE
# without holding a bar (see WHAT SQLITE CANNOT SAY).
WEEKEND = {'start': ny_midnight(3), 'end': ny_midnight(5)}


class Catalog:
    """One dataset per computed status, plus the series and symbol shapes the filters and siblings need.

    status (UI group)       datasets
    FRESH (healthy)         aaa_day
    COMPLETE (healthy)      aaa_min, aaa_day2, aaa_sip, a_under, a_pct, abx
    LATE (late)             bbb
    OVERDUE (failed)        ccc
    GAPS (failed)           ddd             three missing sessions, Thu 1 to Mon 5 Oct
    RETIRED (retired)       eee
    none (no calendar)      ggg             source IB
    """

    def __init__(self) -> None:
        self.aaa_day = entry('AAA', update_type=UpdateType.DAILY)
        self.aaa_min = entry('AAA', granularity=Granularity.ONE_MINUTE, expiry=z('2026-10-15T00:00:00Z'), **WEEKEND)
        # Same granularity as aaa_day, another range: not its sibling.
        self.aaa_day2 = entry('AAA', start=ny_midnight(26, 9), end=ny_midnight(28, 9))
        # Another tape: not a sibling either.
        self.aaa_sip = entry('AAA', granularity=Granularity.ONE_HOUR, feed=Feed.SIP, **WEEKEND)
        self.a_under = entry('A_X', **WEEKEND)
        self.a_pct = entry('A%Z', **WEEKEND)
        self.abx = entry('ABX', **WEEKEND)
        # Open-ended: end is None, so the summary's end is the request time.
        self.bbb = entry('BBB', update_type=UpdateType.DAILY, end=None)
        self.ccc = entry('CCC', granularity=Granularity.ONE_MINUTE, update_type=UpdateType.STREAM)
        self.ddd = entry('DDD', start=ny_midnight(1), end=ny_midnight(6), expiry=z('2026-10-10T00:00:00Z'))
        self.eee = entry('EEE', update_type=UpdateType.DAILY, expiry=z('2026-10-20T00:00:00Z'))
        self.ggg = entry('GGG', update_type=UpdateType.DAILY, source=DataSource.IB_API)
        self.rows = [
            self.aaa_day,
            self.aaa_min,
            self.aaa_day2,
            self.aaa_sip,
            self.a_under,
            self.a_pct,
            self.abx,
            self.bbb,
            self.ccc,
            self.ddd,
            self.eee,
            self.ggg,
        ]
        self.bars = [
            bar(self.aaa_day, ny_midnight(5)),
            bar(self.aaa_day, ny_midnight(6)),
            bar(self.bbb, ny_midnight(5)),
            bar(self.ccc, z('2026-10-06T19:00:00Z')),
            bar(self.ggg, ny_midnight(6)),
        ]

    def ids(self, *rows: StoreDatasetEntry) -> set[str]:
        return {str(row.id) for row in rows}


@pytest_asyncio.fixture
async def catalog(engine: AsyncEngine) -> Catalog:
    seeded = Catalog()
    await store(engine, *seeded.rows, *seeded.bars)
    return seeded


async def list_all(client: httpx.AsyncClient, **params: Any) -> list[dict]:
    """Every item of the list under params, following next_cursor to the end. Asserts no page repeats an item."""
    items: list[dict] = []
    cursor = None
    for _ in range(100):
        response = await client.get('/ui/v1/datasets', params={**params, **({'cursor': cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        body = response.json()
        items.extend(body.get('items', []))
        cursor = body.get('nextCursor')
        if not cursor:
            break
    ids = [item['id'] for item in items]
    assert len(ids) == len(set(ids)), f'an item was served twice: {ids}'
    return items


def status_of(item: dict) -> str | None:
    return item.get('freshness', {}).get('status')


def encode(payload: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')


# ------------------------------------------------------------------------------------------------- the list


@pytest.mark.asyncio
async def test_each_dataset_gets_the_status_the_pinned_clock_gives_it(client, catalog):
    statuses = {item['id']: status_of(item) for item in await list_all(client)}
    assert statuses == {
        str(catalog.aaa_day.id): 'FRESHNESS_STATUS_FRESH',
        str(catalog.aaa_min.id): 'FRESHNESS_STATUS_COMPLETE',
        str(catalog.aaa_day2.id): 'FRESHNESS_STATUS_COMPLETE',
        str(catalog.aaa_sip.id): 'FRESHNESS_STATUS_COMPLETE',
        str(catalog.a_under.id): 'FRESHNESS_STATUS_COMPLETE',
        str(catalog.a_pct.id): 'FRESHNESS_STATUS_COMPLETE',
        str(catalog.abx.id): 'FRESHNESS_STATUS_COMPLETE',
        str(catalog.bbb.id): 'FRESHNESS_STATUS_LATE',
        str(catalog.ccc.id): 'FRESHNESS_STATUS_OVERDUE',
        str(catalog.ddd.id): 'FRESHNESS_STATUS_GAPS',
        str(catalog.eee.id): 'FRESHNESS_STATUS_RETIRED',
        # No calendar for IB: UNSPECIFIED, which canonical JSON omits.
        str(catalog.ggg.id): None,
    }


@pytest.mark.asyncio
async def test_the_list_carries_freshness_detail_extent_and_state(client, catalog):
    items = {item['id']: item for item in await list_all(client)}
    gaps = items[str(catalog.ddd.id)]['freshness']
    assert (gaps['gapCount'], gaps['asOf']) == (3, '2026-10-06T22:00:00Z')
    fresh = items[str(catalog.aaa_day.id)]
    # DAILY's expected_last_bar is the expected session's local midnight (Tue 6 Oct, 04:00Z).
    assert fresh['freshness']['expectedLastBar'] == '2026-10-06T04:00:00Z'
    assert (fresh['barCount'], fresh['firstBar'], fresh['lastBar']) == (
        '2',
        '2026-10-05T04:00:00Z',
        '2026-10-06T04:00:00Z',
    )
    assert fresh['state'] == 'DATASET_STATE_ACTIVE'
    assert items[str(catalog.eee.id)]['state'] == 'DATASET_STATE_RETIRED'
    # STATIC with an expiry is not retired: retirement is a subscription's state.
    assert items[str(catalog.ddd.id)]['state'] == 'DATASET_STATE_ACTIVE'


@pytest.mark.asyncio
async def test_an_empty_dataset_reports_no_count_and_no_bar_bounds(client, catalog):
    """bar_count zero is the proto3 default, so canonical JSON omits it, and first/last bar are unset."""
    item = next(item for item in await list_all(client) if item['id'] == str(catalog.abx.id))
    assert 'barCount' not in item and 'firstBar' not in item and 'lastBar' not in item, item


@pytest.mark.asyncio
async def test_an_open_ended_dataset_reports_the_request_time_as_its_end(client, catalog):
    item = next(item for item in await list_all(client) if item['id'] == str(catalog.bbb.id))
    assert item['end'] == '2026-10-06T22:00:00Z'


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [1, 2, 5, 11, 12, 500])
async def test_paging_by_symbol_serves_every_dataset_once_in_symbol_then_id_order(client, catalog, limit):
    items = await list_all(client, limit=limit)
    expected = sorted(catalog.rows, key=lambda row: (row.asset_symbol, str(row.id)))
    assert [item['id'] for item in items] == [str(row.id) for row in expected]


@pytest.mark.asyncio
async def test_the_last_page_has_no_cursor_and_a_full_one_does(client, catalog):
    exact = (await client.get('/ui/v1/datasets', params={'limit': 12})).json()
    assert len(exact['items']) == 12 and 'nextCursor' not in exact, 'an exactly full last page offers a next page'
    short = (await client.get('/ui/v1/datasets', params={'limit': 11})).json()
    assert len(short['items']) == 11 and short['nextCursor']


@pytest.mark.asyncio
async def test_sort_by_expires_is_soonest_first_with_no_expiry_last(client, catalog):
    items = await list_all(client, sort='expires', limit=2)
    dated = [catalog.ddd, catalog.aaa_min, catalog.eee]
    undated = sorted(
        (row for row in catalog.rows if row.expiry is None), key=lambda row: (row.asset_symbol, str(row.id))
    )
    assert [item['id'] for item in items] == [str(row.id) for row in [*dated, *undated]]


@pytest.mark.asyncio
async def test_a_dataset_inserted_ahead_of_the_cursor_is_served_and_one_behind_it_is_not(client, catalog, engine):
    """Keyset paging: nothing already served comes back, nothing ahead is skipped, whatever lands meanwhile."""
    first = (await client.get('/ui/v1/datasets', params={'limit': 3})).json()
    behind, ahead = entry('A0'), entry('ZZZ')
    await store(engine, behind, ahead)
    rest = await list_all(client, limit=3, cursor=first['nextCursor'])
    served = [item['id'] for item in [*first['items'], *rest]]
    assert len(served) == len(set(served))
    assert set(served) == catalog.ids(*catalog.rows) | {str(ahead.id)}


@pytest.mark.asyncio
async def test_a_status_filter_pages_over_the_whole_filtered_set(client, catalog):
    """The status filter applies over all rows, not the page window: limit 1 still finds every match."""
    assert {item['id'] for item in await list_all(client, status='failed', limit=1)} == catalog.ids(
        catalog.ccc, catalog.ddd
    )


FILTER_CASES = {
    'healthy': ({'status': 'healthy'}, ['aaa_day', 'aaa_min', 'aaa_day2', 'aaa_sip', 'a_under', 'a_pct', 'abx']),
    'HEALTHY in capitals': (
        {'status': 'HEALTHY'},
        ['aaa_day', 'aaa_min', 'aaa_day2', 'aaa_sip', 'a_under', 'a_pct', 'abx'],
    ),
    'late': ({'status': 'late'}, ['bbb']),
    'failed is OVERDUE and GAPS': ({'status': 'failed'}, ['ccc', 'ddd']),
    'retired': ({'status': 'retired'}, ['eee']),
    'needs attention is late and failed': ({'needs_attention': 'true'}, ['bbb', 'ccc', 'ddd']),
    'source by member name': ({'source': 'IB_API'}, ['ggg']),
    'source by wire name, lower case': ({'source': 'data_source_ib_api'}, ['ggg']),
    'update type': ({'update_type': 'UPDATE_TYPE_STREAM'}, ['ccc']),
    'symbol prefix, case-insensitive': ({'asset_symbol': 'aa'}, ['aaa_day', 'aaa_min', 'aaa_day2', 'aaa_sip']),
    'an underscore is literal': ({'asset_symbol': 'A_'}, ['a_under']),
    'a percent sign is literal': ({'asset_symbol': 'a%'}, ['a_pct']),
    'filters combine': ({'asset_symbol': 'A', 'status': 'healthy', 'update_type': 'DAILY'}, ['aaa_day']),
    'nothing matches': ({'asset_symbol': 'QQQ'}, []),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('params', 'names'), FILTER_CASES.values(), ids=FILTER_CASES)
async def test_the_list_filters(client, catalog, params, names):
    expected = catalog.ids(*(getattr(catalog, name) for name in names))
    assert {item['id'] for item in await list_all(client, **params)} == expected


# ------------------------------------------------------------------------------------------------- facets


def facet_counts(body: dict, key: str, field: str) -> dict[str, int]:
    return {facet[field]: int(facet.get('count', '0')) for facet in body.get(key, [])}


@pytest.mark.asyncio
async def test_the_facets_name_every_value_and_count_zero_where_nothing_matches(client, catalog):
    body = (await client.get('/ui/v1/datasets/facets')).json()
    assert (int(body['all']), int(body['needsAttention'])) == (12, 3)
    assert facet_counts(body, 'sources', 'source') == {
        'DATA_SOURCE_ALPACA_API': 11,
        'DATA_SOURCE_IB_API': 1,
        'DATA_SOURCE_MANUAL_ENTRY': 0,
    }
    assert facet_counts(body, 'updateTypes', 'updateType') == {
        'UPDATE_TYPE_STATIC': 7,
        'UPDATE_TYPE_DAILY': 4,
        'UPDATE_TYPE_STREAM': 1,
    }
    assert facet_counts(body, 'statuses', 'status') == {
        'FRESHNESS_STATUS_FRESH': 1,
        'FRESHNESS_STATUS_LATE': 1,
        'FRESHNESS_STATUS_OVERDUE': 1,
        'FRESHNESS_STATUS_COMPLETE': 6,
        'FRESHNESS_STATUS_GAPS': 1,
        'FRESHNESS_STATUS_RETIRED': 1,
    }


FACET_FILTERS = {
    'no filter': {},
    'symbol prefix': {'asset_symbol': 'a'},
    'source': {'source': 'ALPACA_API'},
    'update type': {'update_type': 'DAILY'},
    'status': {'status': 'healthy'},
    'needs attention': {'needs_attention': 'true'},
    'everything': {'asset_symbol': 'a', 'source': 'ALPACA_API', 'update_type': 'STATIC', 'status': 'healthy'},
}

_STATUS_GROUP = {
    'FRESHNESS_STATUS_FRESH': 'healthy',
    'FRESHNESS_STATUS_COMPLETE': 'healthy',
    'FRESHNESS_STATUS_LATE': 'late',
    'FRESHNESS_STATUS_OVERDUE': 'failed',
    'FRESHNESS_STATUS_GAPS': 'failed',
    'FRESHNESS_STATUS_RETIRED': 'retired',
}


@pytest.mark.asyncio
@pytest.mark.parametrize('filters', FACET_FILTERS.values(), ids=FACET_FILTERS)
async def test_every_facet_count_equals_the_total_of_the_list_it_describes(client, catalog, filters):
    """The acceptance: a count equals what the matching list returns. Each count leaves its own filter out."""
    body = (await client.get('/ui/v1/datasets/facets', params=filters)).json()
    without = {key: value for key, value in filters.items() if key != 'needs_attention'}
    assert int(body.get('all', '0')) == len(await list_all(client, **without))
    assert int(body.get('needsAttention', '0')) == len(await list_all(client, **without, needs_attention='true'))
    for source, count in facet_counts(body, 'sources', 'source').items():
        listed = await list_all(client, **{**filters, 'source': source})
        assert count == len(listed), source
    for update_type, count in facet_counts(body, 'updateTypes', 'updateType').items():
        listed = await list_all(client, **{**filters, 'update_type': update_type})
        assert count == len(listed), update_type
    # A status facet is a raw status, and the list filters by its UI group: the group's list, narrowed to it.
    for status, count in facet_counts(body, 'statuses', 'status').items():
        listed = await list_all(client, **{**filters, 'status': _STATUS_GROUP[status]})
        assert count == sum(status_of(item) == status for item in listed), status


@pytest.mark.asyncio
async def test_an_empty_store_lists_nothing_and_counts_zero_everywhere(client):
    page = (await client.get('/ui/v1/datasets')).json()
    assert page == {}, 'an empty page is the all-defaults message: no items, no cursor'
    body = (await client.get('/ui/v1/datasets/facets')).json()
    assert 'all' not in body and 'needsAttention' not in body
    assert set(facet_counts(body, 'sources', 'source').values()) == {0}
    assert len(body['sources']) == len(DataSource)
    assert len(body['updateTypes']) == len(UpdateType)
    assert len(body['statuses']) == 6


# ------------------------------------------------------------------------------------------------- detail


@pytest.mark.asyncio
async def test_detail_lists_the_other_widths_of_the_same_series_only(client, catalog):
    body = (await client.get(f'/ui/v1/datasets/{catalog.aaa_day.id}')).json()
    assert body['siblings'] == [{'id': str(catalog.aaa_min.id), 'granularity': 'GRANULARITY_ONE_MINUTE'}]
    assert body['freshness']['status'] == 'FRESHNESS_STATUS_FRESH'
    assert body['expiryType'] == 'EXPIRY_TYPE_ROLLING'
    # The 1min dataset sees the 1day one, and not the second 1day range twice.
    other = (await client.get(f'/ui/v1/datasets/{catalog.aaa_min.id}')).json()
    assert {sibling['id'] for sibling in other['siblings']} == catalog.ids(catalog.aaa_day, catalog.aaa_day2)
    assert 'siblings' not in (await client.get(f'/ui/v1/datasets/{catalog.bbb.id}')).json()


@pytest.mark.asyncio
async def test_detail_of_an_unknown_dataset_is_a_404(client, catalog):
    problem(await client.get(f'/ui/v1/datasets/{uuid.uuid4()}'), status=404, reason='NOT_FOUND')


@pytest.mark.asyncio
async def test_facets_is_not_taken_for_a_dataset_id(client, catalog):
    """The facets route is registered before the by-id one, so 'facets' is never parsed as a UUID."""
    assert (await client.get('/ui/v1/datasets/facets')).status_code == 200


@pytest.mark.asyncio
async def test_get_store_by_id_answers_the_entry_with_its_bar_count(client, catalog):
    response = await client.get(f'/store/{catalog.aaa_day.id}')
    assert response.status_code == 200, response.text
    assert (response.json()['id'], response.json()['item_count']) == (str(catalog.aaa_day.id), 2)
    empty = await client.get(f'/store/{catalog.abx.id}')
    assert (empty.status_code, empty.json()['item_count']) == (200, 0), 'an entry with no bars is 0, not missing'
    problem(await client.get(f'/store/{uuid.uuid4()}'), status=404, reason='NOT_FOUND')


# ------------------------------------------------------------------------------------------------- bars


class Bars:
    """A 1min series of ten bars, 14:00Z to 14:09Z on Mon 5 Oct, in a dataset of its own.

    Source IB, so no freshness is computed and nothing STATIC with bars meets SQLite's text dates.
    """

    def __init__(self) -> None:
        self.dataset = entry('BARS', granularity=Granularity.ONE_MINUTE, source=DataSource.IB_API)
        self.times = [z('2026-10-05T14:00:00Z') + timedelta(minutes=minute) for minute in range(10)]
        self.rows = [bar(self.dataset, at, close=float(index)) for index, at in enumerate(self.times)]


@pytest_asyncio.fixture
async def bars(engine: AsyncEngine) -> Bars:
    seeded = Bars()
    await store(engine, seeded.dataset, *seeded.rows)
    return seeded


async def all_bars(client: httpx.AsyncClient, dataset_id: uuid.UUID, **params: Any) -> list[str]:
    starts: list[str] = []
    cursor = None
    for _ in range(100):
        response = await client.get(
            f'/ui/v1/datasets/{dataset_id}/bars', params={**params, **({'cursor': cursor} if cursor else {})}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        starts.extend(item['barStart'] for item in body.get('bars', []))
        cursor = body.get('nextCursor')
        if not cursor:
            break
    return starts


def stamp(at: datetime) -> str:
    return at.strftime('%Y-%m-%dT%H:%M:%SZ')


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [1, 3, 7, 1000])
async def test_bars_are_half_open_ascending_and_paged_without_loss(client, bars, limit):
    """[14:01, 14:08): the bar AT start is in, the bar AT end is out, whatever the page size."""
    starts = await all_bars(
        client, bars.dataset.id, start='2026-10-05T14:01:00Z', end='2026-10-05T14:08:00Z', limit=limit
    )
    assert starts == [stamp(at) for at in bars.times[1:8]]


@pytest.mark.asyncio
async def test_bars_one_tick_either_side_of_the_bounds(client, bars):
    starts = await all_bars(
        client, bars.dataset.id, start='2026-10-05T14:01:00.000001Z', end='2026-10-05T14:08:00.000001Z'
    )
    assert starts == [stamp(at) for at in bars.times[2:9]]


@pytest.mark.asyncio
async def test_no_bounds_reads_everything_and_an_empty_or_inverted_window_is_an_empty_page(client, bars):
    assert await all_bars(client, bars.dataset.id) == [stamp(at) for at in bars.times]
    for start, end in (
        ('2026-10-05T14:03:00Z', '2026-10-05T14:03:00Z'),
        ('2026-10-05T14:05:00Z', '2026-10-05T14:01:00Z'),
    ):
        response = await client.get(f'/ui/v1/datasets/{bars.dataset.id}/bars', params={'start': start, 'end': end})
        assert (response.status_code, response.json()) == (200, {})


@pytest.mark.asyncio
async def test_bars_carry_their_values_through_the_shared_bar_mapper(client, bars):
    body = (await client.get(f'/ui/v1/datasets/{bars.dataset.id}/bars', params={'limit': 1})).json()
    (first,) = body['bars']
    assert (first['barStart'], first['open'], first['high'], first['low']) == ('2026-10-05T14:00:00Z', 1.0, 2.0, 0.5)
    assert (first['volume'], first['tradeCount']) == (100.0, '10')


@pytest.mark.asyncio
async def test_a_bar_inserted_ahead_of_the_cursor_is_served_and_one_behind_it_is_not(client, bars, engine):
    url = f'/ui/v1/datasets/{bars.dataset.id}/bars'
    first = (await client.get(url, params={'limit': 3})).json()
    assert [item['barStart'] for item in first['bars']] == [stamp(at) for at in bars.times[:3]]
    behind = bar(bars.dataset, z('2026-10-05T14:00:30Z'))
    ahead = bar(bars.dataset, z('2026-10-05T14:05:30Z'))
    await store(engine, behind, ahead)
    rest = await all_bars(client, bars.dataset.id, limit=3, cursor=first['nextCursor'])
    expected = [stamp(at) for at in bars.times[3:]]
    expected.insert(3, '2026-10-05T14:05:30Z')
    assert rest == expected


@pytest.mark.asyncio
async def test_the_default_bar_page_is_1000(client, engine):
    dataset = entry('MANY', granularity=Granularity.ONE_MINUTE, source=DataSource.IB_API)
    start = z('2026-10-01T00:00:00Z')
    await store(engine, dataset, *(bar(dataset, start + timedelta(minutes=minute)) for minute in range(1001)))
    url = f'/ui/v1/datasets/{dataset.id}/bars'
    default = (await client.get(url)).json()
    assert (len(default['bars']), bool(default.get('nextCursor'))) == (1000, True)
    assert len((await client.get(url, params={'limit': 1001})).json()['bars']) == 1001


# tj-grna9p.125 (owner ruling 2026-10-13): the most bars one read returns is 50,000 (was 10,000); the default stays
# 1,000. The series below is 50,123 bars, so a 50,000 read is a FULL page with a cursor and the next page is 123.
MAX_BARS = 50_000
OVER_MAX = 50_123


@pytest.mark.asyncio
async def test_a_50000_bar_read_is_a_full_page_and_its_cursor_continues_without_loss(client, engine):
    """Up to 50,000 rows in one answer, ascending, then the cursor's page carries on at bar 50,000 to the end."""
    dataset = await minute_series(engine, OVER_MAX, symbol='MAXBARS')
    url = f'/ui/v1/datasets/{dataset.id}/bars'

    first = await client.get(url, params={'limit': MAX_BARS})
    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body['bars']) == MAX_BARS, 'a full page at the cap, not clipped to the old 10,000'
    assert [item['barStart'] for item in body['bars']] == [stamp(minute(index)) for index in range(MAX_BARS)]
    assert [item['close'] for item in body['bars'][-2:]] == [49_998.0, 49_999.0], 'the values travel with the rows'
    assert body.get('nextCursor'), 'a full page carries a cursor'

    rest = (await client.get(url, params={'limit': MAX_BARS, 'cursor': body['nextCursor']})).json()
    assert [item['barStart'] for item in rest['bars']] == [stamp(minute(index)) for index in range(MAX_BARS, OVER_MAX)]
    assert 'nextCursor' not in rest, 'the short last page ends the walk'


@pytest.mark.asyncio
async def test_the_bars_limit_is_bounded_1_to_50000_and_the_refusal_names_the_cap(client, bars):
    url = f'/ui/v1/datasets/{bars.dataset.id}/bars'
    assert len((await client.get(url, params={'limit': MAX_BARS})).json()['bars']) == 10, 'the cap itself is accepted'
    errors = validation_errors(await client.get(url, params={'limit': MAX_BARS + 1}))
    assert [error['loc'][-1] for error in errors] == ['limit'], errors
    assert '50000' in errors[0]['msg'], f'the refusal names the cap: {errors}'


@pytest.mark.asyncio
async def test_bars_of_an_unknown_dataset_is_a_404(client, bars):
    problem(await client.get(f'/ui/v1/datasets/{uuid.uuid4()}/bars'), status=404, reason='NOT_FOUND')


# ------------------------------------------------------------------------------------------------- refusals


LIMIT_CASES = {
    'list limit 0': ('/ui/v1/datasets', {'limit': 0}, 'limit'),
    'list limit 501': ('/ui/v1/datasets', {'limit': 501}, 'limit'),
    'list unknown sort': ('/ui/v1/datasets', {'sort': 'size'}, 'sort'),
    'list unknown status': ('/ui/v1/datasets', {'status': 'stale'}, 'status'),
    'list unknown source': ('/ui/v1/datasets', {'source': 'NYSE'}, 'source'),
    'list empty symbol prefix': ('/ui/v1/datasets', {'asset_symbol': ''}, 'asset_symbol'),
    # Was stale_days until tj-grna9p.85 made it a parameter. owner is the /store search filter and never the reader
    # (tj-grna9p.131), so the catalog must refuse it rather than take it for a client filter.
    'list unknown parameter': ('/ui/v1/datasets', {'owner': 'operator'}, 'owner'),
    'facets ignores no unknown parameter': ('/ui/v1/datasets/facets', {'limit': 5}, 'limit'),
    'facets unknown owner parameter': ('/ui/v1/datasets/facets', {'owner': 'operator'}, 'owner'),
    'list stale_days 0': ('/ui/v1/datasets', {'stale_days': 0}, 'stale_days'),
    'list stale_days 3651': ('/ui/v1/datasets', {'stale_days': 3651}, 'stale_days'),
    'facets stale_days 0': ('/ui/v1/datasets/facets', {'stale_days': 0}, 'stale_days'),
    'facets stale_days 3651': ('/ui/v1/datasets/facets', {'stale_days': 3651}, 'stale_days'),
    'list unknown usage': ('/ui/v1/datasets', {'usage': 'cold'}, 'usage'),
    'facets unknown client kind': ('/ui/v1/datasets/facets', {'client_kind': 'BOT'}, 'client_kind'),
    'list empty client id': ('/ui/v1/datasets', {'client_id': ''}, 'client_id'),
    'list naive expires_before': ('/ui/v1/datasets', {'expires_before': '2026-10-10T00:00:00'}, 'expires_before'),
    'list unknown sort last_used': ('/ui/v1/datasets', {'sort': 'last_used'}, 'sort'),
    'bars limit 0': ('/ui/v1/datasets/{id}/bars', {'limit': 0}, 'limit'),
    'bars limit 50001': ('/ui/v1/datasets/{id}/bars', {'limit': 50_001}, 'limit'),
    'bars naive start': ('/ui/v1/datasets/{id}/bars', {'start': '2026-10-05T14:00:00'}, 'start'),
    'bars naive end': ('/ui/v1/datasets/{id}/bars', {'end': '2026-10-05T14:00:00'}, 'end'),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('path', 'params', 'field'), LIMIT_CASES.values(), ids=LIMIT_CASES)
async def test_a_bad_parameter_is_a_422_naming_it(client, bars, path, params, field):
    response = await client.get(path.replace('{id}', str(bars.dataset.id)), params=params)
    # A repeatable parameter's error ends with the item's index (['query', 'client_kind', 0]), so match any part.
    assert any(field in error['loc'] for error in validation_errors(response))


@pytest.mark.asyncio
async def test_the_list_limit_bounds_are_inclusive(client, catalog):
    assert len((await client.get('/ui/v1/datasets', params={'limit': 500})).json()['items']) == 12
    assert len((await client.get('/ui/v1/datasets', params={'limit': 1})).json()['items']) == 1


# (cursor, the sort it is sent under). Each is wrong in exactly one way for that sort.
BAD_CURSORS = {
    'not base64': ('!!!not-a-cursor!!!', 'symbol'),
    'base64 of not json': (base64.urlsafe_b64encode(b'\xff\xfe garbage').decode(), 'symbol'),
    'json but a list': (encode([1, 2]), 'symbol'),
    'json with the wrong keys': (encode({'x': 1}), 'symbol'),
    'the right keys, the wrong types': (encode({'o': 'symbol', 'k': [1, 2]}), 'symbol'),
    'a key that is no uuid': (encode({'o': 'symbol', 'k': ['AAA', 'not-a-uuid']}), 'symbol'),
    'a key one part short': (encode({'o': 'expires', 'k': [0, 'AAA', str(uuid.uuid4())]}), 'expires'),
    'booleans for ints': (encode({'o': 'expires', 'k': [True, 0, 'AAA', str(uuid.uuid4())]}), 'expires'),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('cursor', 'sort'), BAD_CURSORS.values(), ids=BAD_CURSORS)
async def test_a_bad_list_cursor_is_a_422_that_never_echoes_it(client, catalog, cursor, sort):
    response = await client.get('/ui/v1/datasets', params={'cursor': cursor, 'sort': sort})
    body = problem(response, status=422, reason='INVALID_REQUEST')
    assert cursor not in response.text, f'the refusal echoes the cursor back: {body}'


@pytest.mark.asyncio
async def test_a_cursor_is_bound_to_the_sort_it_was_issued_under(client, catalog):
    by_symbol = (await client.get('/ui/v1/datasets', params={'limit': 2})).json()['nextCursor']
    by_expiry = (await client.get('/ui/v1/datasets', params={'limit': 2, 'sort': 'expires'})).json()['nextCursor']
    for cursor, sort in ((by_symbol, 'expires'), (by_expiry, 'symbol')):
        response = await client.get('/ui/v1/datasets', params={'limit': 2, 'sort': sort, 'cursor': cursor})
        problem(response, status=422, reason='INVALID_REQUEST')
        assert cursor not in response.text


@pytest.mark.asyncio
async def test_list_and_bar_cursors_are_not_interchangeable(client, catalog, bars):
    list_cursor = (await client.get('/ui/v1/datasets', params={'limit': 1})).json()['nextCursor']
    bar_cursor = (await client.get(f'/ui/v1/datasets/{bars.dataset.id}/bars', params={'limit': 1})).json()['nextCursor']
    on_bars = await client.get(f'/ui/v1/datasets/{bars.dataset.id}/bars', params={'cursor': list_cursor})
    problem(on_bars, status=422, reason='INVALID_REQUEST')
    on_list = await client.get('/ui/v1/datasets', params={'cursor': bar_cursor})
    problem(on_list, status=422, reason='INVALID_REQUEST')
    assert list_cursor not in on_bars.text and bar_cursor not in on_list.text


BAD_BAR_CURSORS = {
    'not base64': '%%%',
    'extra keys': encode({'t': '2026-10-05T14:00:00+00:00', 'x': 1}),
    'not a timestamp': encode({'t': 'yesterday'}),
    'a naive timestamp': encode({'t': '2026-10-05T14:00:00'}),
    'a number': encode({'t': 5}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize('cursor', BAD_BAR_CURSORS.values(), ids=BAD_BAR_CURSORS)
async def test_a_bad_bar_cursor_is_a_422_that_never_echoes_it(client, bars, cursor):
    response = await client.get(f'/ui/v1/datasets/{bars.dataset.id}/bars', params={'cursor': cursor})
    problem(response, status=422, reason='INVALID_REQUEST')
    assert cursor not in response.text


@pytest.mark.asyncio
async def test_an_over_long_cursor_is_refused_without_echoing_it(client, catalog):
    cursor = 'A' * 600
    response = await client.get('/ui/v1/datasets', params={'cursor': cursor})
    assert 'cursor' in [error['loc'][-1] for error in validation_errors(response)]
    assert cursor not in response.text


# ------------------------------------------------------------------------------------------------- query budget


@pytest.mark.asyncio
async def test_a_request_runs_a_constant_number_of_statements_not_one_per_dataset(client, catalog, engine):
    """tj-grna9p.8 item 2: one statement for the entries, then one each for extents, covered dates and siblings.

    tj-grna9p.72 adds exactly ONE statement to the list and the detail -- the latest run per dataset for the
    whole page, a window function, never one per dataset -- and the RUNNING set to the facets and to the
    running filter. So the budget is pinned per route, exactly, rather than as one ceiling: list and detail 5
    (was 4), facets 3, and the large set below gives MANY datasets runs, so a per-dataset last_run read would
    show as growth.

    tj-grna9p.85 adds TWO to the list, the detail and the facets HERE: the usage tables' to_regclass probe (once
    per request) and the reaper's pending set. On SQLite the probe itself fails (no to_regclass), so usage reads
    as the zero view and the usage, readers and clients reads never run. Their budget, with the tables present,
    is pinned on Postgres: tests/system/test_http_ui_dataset_usage.py.
    """
    statements: list[str] = []
    event.listen(engine.sync_engine, 'before_cursor_execute', lambda *args: statements.append(args[2]))

    async def count(path: str, **params: Any) -> int:
        statements.clear()
        assert (await client.get(path, params=params)).status_code == 200
        return len(statements)

    async def counts() -> dict[str, int]:
        return {
            'list': await count('/ui/v1/datasets', limit=500),
            'list filtered by status': await count('/ui/v1/datasets', limit=500, status='healthy'),
            'list filtered by running': await count('/ui/v1/datasets', limit=500, running='true'),
            'facets': await count('/ui/v1/datasets/facets'),
            'detail': await count(f'/ui/v1/datasets/{catalog.ddd.id}'),
            # tj-grna9p.63: the entry and its covered dates, never the detail's five.
            'coverage': await count(f'/ui/v1/datasets/{catalog.ddd.id}/coverage'),
            # tj-grna9p.120: the entry, then the page index (count and ranks) in one statement. Its growth with
            # the WINDOW rather than with the datasets is pinned in the bar page index section below.
            'bar pages': await count(f'/ui/v1/datasets/{catalog.ddd.id}/bar-pages'),
        }

    # One running dataset in the small set too, so the running-filtered page is not empty: an empty page skips
    # the per-page statements, which would read as growth when it is only a smaller answer.
    await store(engine, run_of(catalog.ddd, minutes_ago=1, state=IngestRunState.RUNNING))
    small = await counts()
    many = [entry(f'X{index:03d}', **WEEKEND) for index in range(40)]
    await store(engine, *many)
    await store(engine, *(run_of(of, minutes_ago=index) for index, of in enumerate(many)))
    await store(engine, *(run_of(of, minutes_ago=0, state=IngestRunState.RUNNING) for of in many[:10]))
    large = await counts()
    assert small == large, f'the statement count grew with the number of datasets: {small} -> {large}'
    # The running filter reads the RUNNING set once more, and only when asked for.
    assert large == {
        'list': 7,
        'list filtered by status': 7,
        'list filtered by running': 8,
        'facets': 5,
        'detail': 7,
        'coverage': 2,
        'bar pages': 2,
    }, large


@pytest.mark.asyncio
async def test_a_static_dataset_is_judged_on_its_covered_sessions(client, catalog, engine):
    """A STATIC dataset on the page sends the covered-dates statement.

    Without it every STATIC dataset with bars would read as all gaps. The statement is recognised by the timezone()
    call only it makes.
    """
    statements: list[str] = []
    event.listen(engine.sync_engine, 'before_cursor_execute', lambda *args: statements.append(args[2]))
    assert (await client.get(f'/ui/v1/datasets/{catalog.ddd.id}')).status_code == 200
    assert sum('timezone(' in statement for statement in statements) == 1, statements
    statements.clear()
    assert (await client.get(f'/ui/v1/datasets/{catalog.bbb.id}')).status_code == 200
    assert not any('timezone(' in statement for statement in statements), 'a DAILY dataset needs no covered dates'


# ------------------------------------------------------------------------------------------------- before 1990
#
# tj-grna9p.103 (ruling: architect addendum of 18:58 on decision tj-grna9p.8). The trading calendar starts on
# CALENDAR_START, 1990-01-01, and raises CalendarRangeError for a range before it. A dataset whose range begins
# earlier cannot be judged, and nothing bounds a start below, so evaluate_views catches that error PER ROW: the
# row's health is unset (as for a source with no calendar), a WARNING names it, and every other row is judged as
# before. STATIC and DAILY read the range and raise; STREAM ignores it and is judged; a retired subscription reads
# RETIRED before the calendar is asked anything.

OLD_START = z('1985-06-03T04:00:00Z')
OLD_END = z('1985-06-10T04:00:00Z')
CATALOG_LOGGER = 'data.store.app.dataset_catalog'
EXPIRY = z('2026-10-20T00:00:00Z')


class Early:
    """Pre-1990 entries, one per path through evaluate_views."""

    def __init__(self) -> None:
        minute = Granularity.ONE_MINUTE
        self.static = entry('OLDS', start=OLD_START, end=OLD_END)
        self.daily = entry('OLDD', update_type=UpdateType.DAILY, start=OLD_START)
        self.daily_retired = entry('OLDR', update_type=UpdateType.DAILY, start=OLD_START, expiry=EXPIRY)
        self.stream_retired = entry(
            'OLDX', granularity=minute, update_type=UpdateType.STREAM, start=OLD_START, expiry=EXPIRY
        )
        self.stream = entry('OLDT', granularity=minute, update_type=UpdateType.STREAM, start=OLD_START)
        # The two that cannot be judged: no health, in the list, the facets and the detail alike.
        self.unjudged = [self.static, self.daily]
        self.rows = [self.static, self.daily, self.daily_retired, self.stream_retired, self.stream]


def judged_statuses(catalog: Catalog) -> dict[str, str | None]:
    """Each catalog dataset's status at NOW, as the first list test pins it: what a pre-1990 neighbour must keep."""
    complete = (catalog.aaa_min, catalog.aaa_day2, catalog.aaa_sip, catalog.a_under, catalog.a_pct, catalog.abx)
    return {
        str(catalog.aaa_day.id): 'FRESHNESS_STATUS_FRESH',
        **{str(row.id): 'FRESHNESS_STATUS_COMPLETE' for row in complete},
        str(catalog.bbb.id): 'FRESHNESS_STATUS_LATE',
        str(catalog.ccc.id): 'FRESHNESS_STATUS_OVERDUE',
        str(catalog.ddd.id): 'FRESHNESS_STATUS_GAPS',
        str(catalog.eee.id): 'FRESHNESS_STATUS_RETIRED',
        str(catalog.ggg.id): None,
    }


@pytest_asyncio.fixture
async def early(engine: AsyncEngine, catalog: Catalog) -> Early:
    seeded = Early()
    await store(engine, *seeded.rows)
    return seeded


async def views_by_id(engine: AsyncEngine, only: set[str] | None = None) -> dict[str, Any]:
    """evaluate_views over the stored rows (those whose id is in only, when given), keyed by dataset id."""
    async with AsyncSession(engine, expire_on_commit=False) as db:
        rows = await catalog_repo.list_catalog(db, CatalogFilter())
        rows = [row for row in rows if only is None or str(row.id) in only]
        return {str(view.row.id): view for view in await evaluate_views(db, rows, NOW)}


def wire(health: Any) -> str | None:
    return None if health is None else f'FRESHNESS_STATUS_{health.status.value}'


@pytest.mark.asyncio
async def test_evaluate_views_leaves_pre_1990_static_and_daily_unjudged_and_judges_the_rest(engine, catalog, early):
    views = await views_by_id(engine)
    assert views[str(early.static.id)].health is None
    assert views[str(early.daily.id)].health is None
    assert {key: wire(views[key].health) for key in judged_statuses(catalog)} == judged_statuses(catalog)
    # STREAM ignores its start, so a pre-1990 one is judged like any other: no bars, after Tuesday's close.
    assert views[str(early.stream.id)].health.status is FreshnessStatus.OVERDUE


@pytest.mark.asyncio
async def test_a_retired_pre_1990_subscription_still_reads_retired(engine, catalog, early):
    # Judged on their own, without the unjudgeable rows: RETIRED is answered before the range is read, so this
    # holds whether or not the per-row catch exists.
    views = await views_by_id(engine, only=catalog.ids(early.daily_retired, early.stream_retired))
    assert len(views) == 2
    for row in (early.daily_retired, early.stream_retired):
        assert views[str(row.id)].health.status is FreshnessStatus.RETIRED, row.asset_symbol
        assert views[str(row.id)].health.retired_on == row.expiry


@pytest.mark.asyncio
async def test_each_unjudged_row_is_logged_once_at_warning_by_its_id(engine, catalog, early, caplog):
    with caplog.at_level(logging.DEBUG, logger=CATALOG_LOGGER):
        await views_by_id(engine)
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    for row in early.unjudged:
        named = [record for record in warnings if str(row.id) in record.getMessage()]
        assert len(named) == 1, (row.asset_symbol, [record.getMessage() for record in warnings])
        assert named[0].name == CATALOG_LOGGER
        # A known state of the data, not a bug: no traceback.
        assert named[0].exc_info is None
    judgeable = catalog.ids(*catalog.rows, early.stream, early.daily_retired, early.stream_retired)
    assert not [record for record in warnings if any(key in record.getMessage() for key in judgeable)]


class EvaluationBrokeError(Exception):
    """Stands in for any failure of evaluate_dataset other than the known pre-1990 case."""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error',
    [EvaluationBrokeError('boom'), UnknownCalendarError('a sibling LookupError'), NaiveDatetimeError('naive')],
    ids=['unrelated', 'another-lookup-error', 'naive-datetime'],
)
async def test_only_the_calendar_range_error_is_caught(engine, catalog, monkeypatch, error):
    def broken(**_kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(dataset_catalog_module, 'evaluate_dataset', broken)
    with pytest.raises(type(error)):
        await views_by_id(engine)


@pytest.mark.asyncio
async def test_the_list_with_a_pre_1990_dataset_is_a_200_that_judges_every_other_row(client, catalog, early):
    statuses = {item['id']: status_of(item) for item in await list_all(client)}
    assert statuses[str(early.static.id)] is None
    assert statuses[str(early.daily.id)] is None
    assert {key: statuses[key] for key in judged_statuses(catalog)} == judged_statuses(catalog)


@pytest.mark.asyncio
async def test_the_detail_of_a_pre_1990_dataset_is_a_200_with_health_unset(client, catalog, early):
    for row in early.unjudged:
        response = await client.get(f'/ui/v1/datasets/{row.id}')
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['id'] == str(row.id)
        assert 'status' not in body.get('freshness', {}), body
        assert body['start'] == '1985-06-03T04:00:00Z'


@pytest.mark.asyncio
async def test_facets_count_a_pre_1990_dataset_everywhere_but_the_statuses(client, catalog, early):
    response = await client.get('/ui/v1/datasets/facets')
    assert response.status_code == 200, response.text
    body = response.json()
    # The catalog's 12, then 5 early rows; needs attention gains only the judged STREAM (OVERDUE).
    assert (int(body['all']), int(body['needsAttention'])) == (17, 4)
    assert facet_counts(body, 'sources', 'source')['DATA_SOURCE_ALPACA_API'] == 16
    assert facet_counts(body, 'updateTypes', 'updateType') == {
        'UPDATE_TYPE_STATIC': 8,
        'UPDATE_TYPE_DAILY': 6,
        'UPDATE_TYPE_STREAM': 3,
    }
    # The catalog's counts plus the judged STREAM and the two retired subscriptions. The unjudged rows are
    # UNSPECIFIED, which no status facet counts.
    assert facet_counts(body, 'statuses', 'status') == {
        'FRESHNESS_STATUS_FRESH': 1,
        'FRESHNESS_STATUS_LATE': 1,
        'FRESHNESS_STATUS_OVERDUE': 2,
        'FRESHNESS_STATUS_COMPLETE': 6,
        'FRESHNESS_STATUS_GAPS': 1,
        'FRESHNESS_STATUS_RETIRED': 3,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'params',
    [
        {'status': 'healthy'},
        {'status': 'late'},
        {'status': 'failed'},
        {'status': 'retired'},
        {'needs_attention': 'true'},
    ],
    ids=['healthy', 'late', 'failed', 'retired', 'needs-attention'],
)
async def test_no_status_filter_matches_a_pre_1990_dataset(client, catalog, early, params):
    listed = {item['id'] for item in await list_all(client, **params)}
    assert listed, 'each group holds a judged dataset, so an empty answer proves nothing'
    assert not listed & catalog.ids(*early.unjudged), params


# ------------------------------------------------------------------------------------------------- runs (tj-grna9p.72)
#
# The design is tj-grna9p.72's architect re-baseline of 2026-10-11: GET /ui/v1/datasets/{id}/runs is an IngestRunPage
# keyset on (started_at DESC, id DESC) with an opaque cursor, limit 50 by default and at most 500, a 404 NOT_FOUND for
# an unknown dataset and a 422 for a bad cursor; DatasetSummary.last_run is the newest run on list and detail, read for
# the whole page in ONE statement; running=true lists exactly the datasets with a RUNNING run, orthogonal to status,
# and DatasetFacets.running counts them under every OTHER filter. The ingest_run rows are written directly, so these
# tests read the routes and nothing about the recorder (test_run_recording.py).


async def all_runs(client: httpx.AsyncClient, dataset_id: uuid.UUID, *, limit: int) -> list[dict]:
    """Every run of a dataset, following next_cursor. Asserts no page repeats a run."""
    items: list[dict] = []
    cursor = None
    for _ in range(100):
        params = {'limit': limit, **({'cursor': cursor} if cursor else {})}
        response = await client.get(f'/ui/v1/datasets/{dataset_id}/runs', params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        items.extend(body.get('items', []))
        cursor = body.get('nextCursor')
        if not cursor:
            break
    ids = [item['id'] for item in items]
    assert len(ids) == len(set(ids)), f'a run was served twice: {ids}'
    return items


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [1, 2, 3, 7, 500])
async def test_runs_page_newest_first_with_equal_start_times_broken_by_id(client, catalog, engine, limit):
    tied = sorted((uuid.uuid4() for _ in range(3)), reverse=True)
    runs = [
        run_of(catalog.aaa_day, minutes_ago=30),
        *(run_of(catalog.aaa_day, minutes_ago=10, run_id=run_id) for run_id in tied),
        run_of(catalog.aaa_day, minutes_ago=5, state=IngestRunState.FAILED),
        run_of(catalog.aaa_day, minutes_ago=1, state=IngestRunState.RUNNING),
        # Another dataset's run and an unacknowledged run never appear on this dataset's page.
        run_of(catalog.bbb, minutes_ago=2),
        run_of(None, minutes_ago=3),
    ]
    await store(engine, *runs)

    items = await all_runs(client, catalog.aaa_day.id, limit=limit)

    expected = [runs[5], runs[4], *runs[1:4], runs[0]]
    assert [item['id'] for item in items] == [str(run.id) for run in expected]


@pytest.mark.asyncio
async def test_a_run_inserted_while_paging_does_not_shift_the_pages_not_yet_read(client, catalog, engine):
    """A NEWER run lands before the cursor and is not served; an OLDER one behind it is, in its place."""
    runs = [run_of(catalog.aaa_day, minutes_ago=minutes) for minutes in (10, 20, 30, 40)]
    await store(engine, *runs)
    path = f'/ui/v1/datasets/{catalog.aaa_day.id}/runs'
    first = (await client.get(path, params={'limit': 2})).json()
    assert [item['id'] for item in first['items']] == [str(runs[0].id), str(runs[1].id)]

    newer, older = run_of(catalog.aaa_day, minutes_ago=0), run_of(catalog.aaa_day, minutes_ago=35)
    await store(engine, newer, older)
    rest = (await client.get(path, params={'limit': 5, 'cursor': first['nextCursor']})).json()

    assert [item['id'] for item in rest['items']] == [str(runs[2].id), str(older.id), str(runs[3].id)]
    assert 'nextCursor' not in rest


@pytest.mark.asyncio
async def test_a_run_renders_as_the_ingest_run_message(client, catalog, engine):
    """Every column the UI reads, by its wire name, and the error only on a FAILED run."""
    failed = run_of(catalog.ddd, minutes_ago=5, state=IngestRunState.FAILED)
    failed.error_domain, failed.error_reason, failed.error_message = 'trader-joe', 'PEER_UNAVAILABLE', 'ingest is down'
    failed.requested_end = ny_midnight(6)
    # A failed fetch writes nothing (its transaction rolls back), and zero is omitted in canonical JSON.
    failed.bars_written = 0
    await store(engine, failed, run_of(catalog.ddd, minutes_ago=50))

    items = (await client.get(f'/ui/v1/datasets/{catalog.ddd.id}/runs')).json()['items']

    assert items[0] == {
        'id': str(failed.id),
        'datasetId': str(catalog.ddd.id),
        'label': 'DDD 1day',
        'trigger': 'RUN_TRIGGER_REQUEST',
        'actor': OWNER,
        'start': '2026-09-01T04:00:00Z',
        'end': '2026-10-06T04:00:00Z',
        'state': 'INGEST_RUN_STATE_FAILED',
        'error': {'domain': 'trader-joe', 'reason': 'PEER_UNAVAILABLE', 'message': 'ingest is down'},
        'startedAt': '2026-10-06T21:55:00Z',
        'finishedAt': '2026-10-06T21:55:03Z',
    }
    assert items[1]['state'] == 'INGEST_RUN_STATE_SUCCEEDED' and 'error' not in items[1]
    assert items[1]['barsWritten'] == '7'


@pytest.mark.asyncio
async def test_runs_of_an_unknown_dataset_is_a_404(client, catalog, engine):
    """A dataset that does not exist -- including one whose runs survive its deletion -- is NOT_FOUND here."""
    orphan = run_of(catalog.aaa_day, minutes_ago=5)
    orphan.dataset_id = uuid.uuid4()
    await store(engine, orphan)
    for dataset_id in (uuid.uuid4(), orphan.dataset_id):
        problem(await client.get(f'/ui/v1/datasets/{dataset_id}/runs'), status=404, reason='NOT_FOUND')


@pytest.mark.asyncio
async def test_a_dataset_with_no_runs_answers_an_empty_last_page(client, catalog):
    assert (await client.get(f'/ui/v1/datasets/{catalog.abx.id}/runs')).json() == {}


BAD_RUN_CURSORS = {
    'not base64': '%%%',
    'a list cursor': encode({'o': 'symbol', 'k': ['AAA', str(uuid.uuid4())]}),
    'the wrong order tag': encode({'o': 'symbol', 'k': ['2026-10-06T21:00:00+00:00', str(uuid.uuid4())]}),
    'a naive start': encode({'o': 'runs', 'k': ['2026-10-06T21:00:00', str(uuid.uuid4())]}),
    'a key that is no uuid': encode({'o': 'runs', 'k': ['2026-10-06T21:00:00+00:00', 'not-a-uuid']}),
    'a key one part short': encode({'o': 'runs', 'k': ['2026-10-06T21:00:00+00:00']}),
    'not a timestamp': encode({'o': 'runs', 'k': ['yesterday', str(uuid.uuid4())]}),
    'numbers': encode({'o': 'runs', 'k': [5, 6]}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize('cursor', BAD_RUN_CURSORS.values(), ids=BAD_RUN_CURSORS)
async def test_a_bad_runs_cursor_is_a_422_that_never_echoes_it(client, catalog, engine, cursor):
    await store(engine, run_of(catalog.aaa_day, minutes_ago=5))
    response = await client.get(f'/ui/v1/datasets/{catalog.aaa_day.id}/runs', params={'cursor': cursor})
    problem(response, status=422, reason='INVALID_REQUEST')
    assert cursor not in response.text


@pytest.mark.asyncio
async def test_runs_and_list_cursors_are_not_interchangeable(client, catalog, engine):
    await store(engine, *(run_of(catalog.aaa_day, minutes_ago=minutes) for minutes in (1, 2, 3)))
    path = f'/ui/v1/datasets/{catalog.aaa_day.id}/runs'
    runs_cursor = (await client.get(path, params={'limit': 1})).json()['nextCursor']
    list_cursor = (await client.get('/ui/v1/datasets', params={'limit': 1})).json()['nextCursor']
    problem(await client.get('/ui/v1/datasets', params={'cursor': runs_cursor}), status=422, reason='INVALID_REQUEST')
    problem(await client.get(path, params={'cursor': list_cursor}), status=422, reason='INVALID_REQUEST')


@pytest.mark.asyncio
async def test_the_runs_limit_defaults_to_50_and_is_bounded_1_to_500(client, catalog, engine):
    await store(engine, *(run_of(catalog.aaa_day, minutes_ago=minutes) for minutes in range(51)))
    path = f'/ui/v1/datasets/{catalog.aaa_day.id}/runs'
    page = (await client.get(path)).json()
    assert len(page['items']) == 50 and page['nextCursor']
    assert len((await client.get(path, params={'limit': 500})).json()['items']) == 51
    for limit in (0, 501):
        errors = validation_errors(await client.get(path, params={'limit': limit}))
        assert [error['loc'] for error in errors] == [['query', 'limit']], errors


# ------------------------------------------------------------------------------------------------- last_run


@pytest.mark.asyncio
async def test_last_run_is_the_newest_run_on_list_and_detail_and_absent_without_one(client, catalog, engine):
    older = run_of(catalog.aaa_day, minutes_ago=30)
    newest = run_of(catalog.aaa_day, minutes_ago=2, state=IngestRunState.RUNNING)
    bbb_only = run_of(catalog.bbb, minutes_ago=60, state=IngestRunState.FAILED)
    await store(engine, older, newest, bbb_only, run_of(None, minutes_ago=0))

    listed = {item['id']: item for item in await list_all(client)}
    detail = (await client.get(f'/ui/v1/datasets/{catalog.aaa_day.id}')).json()

    assert listed[str(catalog.aaa_day.id)]['lastRun']['id'] == str(newest.id)
    assert detail['lastRun'] == listed[str(catalog.aaa_day.id)]['lastRun']
    assert detail['lastRun']['state'] == 'INGEST_RUN_STATE_RUNNING' and 'finishedAt' not in detail['lastRun']
    assert listed[str(catalog.bbb.id)]['lastRun']['id'] == str(bbb_only.id)
    without = [item for item in listed.values() if item['id'] not in catalog.ids(catalog.aaa_day, catalog.bbb)]
    assert without and not [item['id'] for item in without if 'lastRun' in item]
    assert 'lastRun' not in (await client.get(f'/ui/v1/datasets/{catalog.ddd.id}')).json()


@pytest.mark.asyncio
async def test_last_run_breaks_an_equal_start_time_by_the_higher_id(client, catalog, engine):
    low, high = sorted((uuid.uuid4(), uuid.uuid4()))
    await store(engine, *(run_of(catalog.aaa_day, minutes_ago=4, run_id=run_id) for run_id in (low, high)))
    detail = (await client.get(f'/ui/v1/datasets/{catalog.aaa_day.id}')).json()
    assert detail['lastRun']['id'] == str(high), 'last_run and the runs list disagree on which run is newest'


@pytest.mark.asyncio
async def test_the_latest_runs_are_read_in_one_statement_for_the_whole_page(client, catalog, engine):
    """Store CLAUDE.md: a constant statement count. The ingest_run reads on a list request, counted."""
    await store(engine, *(run_of(row, minutes_ago=index) for index, row in enumerate(catalog.rows)))
    statements: list[str] = []
    event.listen(engine.sync_engine, 'before_cursor_execute', lambda *args: statements.append(args[2]))

    assert (await client.get('/ui/v1/datasets', params={'limit': 500})).status_code == 200

    run_reads = [statement for statement in statements if 'ingest_run' in statement]
    assert len(run_reads) == 1, run_reads


# ------------------------------------------------------------------------------------------------- running


@pytest_asyncio.fixture
async def running(engine: AsyncEngine, catalog: Catalog) -> set[str]:
    """RUNNING runs on aaa_day (FRESH), bbb (LATE) and ccc (OVERDUE), twice on bbb; finished runs elsewhere.

    A RUNNING run with no dataset yet (before its ack) names no dataset and must be counted nowhere.
    """
    await store(
        engine,
        run_of(catalog.aaa_day, minutes_ago=1, state=IngestRunState.RUNNING),
        run_of(catalog.bbb, minutes_ago=1, state=IngestRunState.RUNNING),
        run_of(catalog.bbb, minutes_ago=2, state=IngestRunState.RUNNING),
        run_of(catalog.ccc, minutes_ago=1, state=IngestRunState.RUNNING),
        run_of(catalog.ddd, minutes_ago=1, state=IngestRunState.FAILED),
        run_of(catalog.eee, minutes_ago=1),
        run_of(None, minutes_ago=1, state=IngestRunState.RUNNING),
    )
    return catalog.ids(catalog.aaa_day, catalog.bbb, catalog.ccc)


@pytest.mark.asyncio
async def test_running_true_lists_exactly_the_datasets_with_a_running_run(client, catalog, running):
    assert {item['id'] for item in await list_all(client, running='true')} == running
    assert len(await list_all(client, running='false')) == len(catalog.rows), 'false is no filter'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'params',
    [
        {},
        {'status': 'healthy'},
        {'status': 'late'},
        {'needs_attention': 'true'},
        {'asset_symbol': 'B'},
        {'update_type': 'DAILY'},
        {'status': 'failed', 'update_type': 'STREAM'},
    ],
    ids=['no filter', 'healthy', 'late', 'needs attention', 'symbol', 'update type', 'combined'],
)
async def test_the_running_facet_equals_the_running_filtered_total_under_every_other_filter(
    client, catalog, running, params
):
    """The facet rule: facets.running under the OTHER filters is the total running=true returns under them."""
    listed = await list_all(client, **params, running='true')
    for facet_params in (params, {**params, 'running': 'true'}):
        facets = (await client.get('/ui/v1/datasets/facets', params=facet_params)).json()
        assert int(facets.get('running', '0')) == len(listed), (facet_params, facets, [item['id'] for item in listed])
    assert {item['id'] for item in listed} <= running


@pytest.mark.asyncio
async def test_running_is_orthogonal_to_status_so_a_late_dataset_can_be_running(client, catalog, running):
    listed = await list_all(client, running='true', status='late')
    assert [item['id'] for item in listed] == [str(catalog.bbb.id)]
    assert status_of(listed[0]) == 'FRESHNESS_STATUS_LATE'


@pytest.mark.asyncio
async def test_with_running_true_the_other_facets_count_only_running_datasets(client, catalog, running):
    facets = (await client.get('/ui/v1/datasets/facets', params={'running': 'true'})).json()
    assert facets['all'] == '3'
    statuses = {facet['status']: int(facet.get('count', '0')) for facet in facets['statuses']}
    assert statuses['FRESHNESS_STATUS_FRESH'] == 1 and statuses['FRESHNESS_STATUS_LATE'] == 1
    assert statuses['FRESHNESS_STATUS_OVERDUE'] == 1 and statuses['FRESHNESS_STATUS_GAPS'] == 0


@pytest.mark.asyncio
async def test_no_running_run_means_a_zero_facet_and_an_empty_running_list(client, catalog):
    assert 'running' not in (await client.get('/ui/v1/datasets/facets')).json()
    assert await list_all(client, running='true') == []


# ------------------------------------------------------------------------------------------------- coverage
#
# GET /ui/v1/datasets/{dataset_id}/coverage (tj-grna9p.63; design: the architect's 'RESCOPED (phase 2,
# 2026-10-12)' note on that bead, items 1 to 8 and acceptance a to h). The pure bucket rule is pinned in
# test_coverage_buckets.py; this section drives the route over real rows.
#
# STATIC DATASETS WITH BARS, HERE ONLY. The file header says SQLite returns covered_dates' date() as TEXT, so a
# STATIC dataset with bars is judged wrongly at this tier. The coverage tests need bars, so they take the
# fixture below: it runs the REAL covered_dates statement (the statement budget still counts it) and only parses
# the 'YYYY-MM-DD' text SQLite returns into the date Postgres returns. The local date itself is computed by the
# timezone() stand-in in the calendar's zone, as Postgres would; tests/system/test_http_ui_datasets.py pins the
# Postgres side of covered_dates, and its coverage section the known gap end to end.
#
# NOTHING IS INVENTED: every expected session, count and window is derived from the phase 1 calendar
# (sessions_overlapping, session_close, local_midnight, session_of, gap_intervals).

XNYS = calendar_for_source(DataSource.ALPACA_API)
TICK = timedelta(microseconds=1)
DAY = timedelta(days=1)
# Every session of September 2026: all closed before NOW.
SEPTEMBER = XNYS.sessions_overlapping(ny_midnight(1, 9), ny_midnight(1, 10))


@pytest.fixture
def covered_dates_as_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    real = catalog_repo.covered_dates

    async def as_dates(db: AsyncSession, rows: list, tz_name: str) -> dict[uuid.UUID, frozenset[date]]:
        found = await real(db, rows, tz_name)
        return {
            key: frozenset(day if isinstance(day, date) else date.fromisoformat(day) for day in days)
            for key, days in found.items()
        }

    monkeypatch.setattr(catalog_repo, 'covered_dates', as_dates)


def midnight(session: date, calendar: TradingCalendar = XNYS) -> datetime:
    """A session's local midnight as UTC, the only offset SQLite can hold."""
    return local_midnight(session, calendar).astimezone(UTC)


def window(first: date, last: date, calendar: TradingCalendar = XNYS) -> tuple[datetime, datetime, int]:
    """The gap the design says a run of missing sessions FIRST..LAST reads as (item 6), counted in the calendar."""
    sessions = calendar.sessions_overlapping(midnight(first, calendar), midnight(last + DAY, calendar))
    return midnight(first, calendar), midnight(last + DAY, calendar), len(sessions)


@dataclass(frozen=True)
class Strip:
    start: datetime
    end: datetime
    buckets: list[tuple[datetime, datetime, int, int]]
    gaps: list[tuple[datetime, datetime, int]]
    gap_count: int
    as_of: datetime

    @property
    def expected(self) -> int:
        return sum(bucket[2] for bucket in self.buckets)

    @property
    def missing(self) -> int:
        return sum(bucket[2] - bucket[3] for bucket in self.buckets)


def coverage_path(dataset_id: uuid.UUID) -> str:
    return f'/ui/v1/datasets/{dataset_id}/coverage'


async def coverage(client: httpx.AsyncClient, dataset: StoreDatasetEntry, **params: Any) -> Strip:
    response = await client.get(coverage_path(dataset.id), params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    instant = datetime.fromisoformat
    return Strip(
        start=instant(body['start']),
        end=instant(body['end']),
        buckets=[
            (instant(b['start']), instant(b['end']), b.get('expectedSessions', 0), b.get('sessionsWithBars', 0))
            for b in body.get('buckets', [])
        ],
        gaps=[(instant(g['start']), instant(g['end']), g.get('sessions', 0)) for g in body.get('gaps', [])],
        gap_count=body.get('gapCount', 0),
        as_of=instant(body['asOf']),
    )


def assert_tiles(strip: Strip) -> None:
    assert strip.buckets[0][0] == strip.start and strip.buckets[-1][1] == strip.end, strip
    for earlier, later in pairwise(strip.buckets):
        assert earlier[1] == later[0], (earlier, later)
    assert all(start < end for start, end, _, _ in strip.buckets), strip.buckets


def pin_clock(now: datetime) -> None:
    """Move the routes' clock for one test; the client fixture clears every override afterwards."""

    async def pinned() -> datetime:
        return now

    app.dependency_overrides[utc_now] = pinned


def daily_bars(of: StoreDatasetEntry, sessions: list[date], calendar: TradingCalendar = XNYS) -> list:
    return [bar(of, midnight(session, calendar)) for session in sessions]


def _weekend_run(sessions: list[date]) -> list[date]:
    """Three consecutive sessions whose last two straddle a weekend, from the calendar."""
    for index in range(1, len(sessions) - 2):
        if (sessions[index + 1] - sessions[index]).days > 1:
            return sessions[index - 1 : index + 2]
    raise AssertionError('precondition: no weekend in the range')


@pytest.mark.asyncio
async def test_a_known_three_session_gap_reads_as_one_local_midnight_window(client, engine, covered_dates_as_dates):
    """Acceptance a, b, c, g: one gap spanning a weekend, its window, the bucket sums, the GAPS count agree."""
    missing = _weekend_run(SEPTEMBER)
    dataset = entry('COV', start=midnight(SEPTEMBER[0]), end=midnight(SEPTEMBER[-1] + DAY))
    await store(engine, dataset, *daily_bars(dataset, [s for s in SEPTEMBER if s not in missing]))

    strip = await coverage(client, dataset)

    assert (strip.start, strip.end, strip.as_of) == (dataset.start, dataset.end, NOW)
    assert strip.gap_count == 1 and strip.gaps == [window(missing[0], missing[-1])], strip.gaps
    assert strip.gaps[0][2] == len(missing) == 3
    assert strip.expected == len(SEPTEMBER) and strip.missing == len(missing)
    assert_tiles(strip)
    assert len(strip.buckets) == min(DEFAULT_COVERAGE_BUCKETS, len(SEPTEMBER))
    # g: the same rule as freshness -- the GAPS count (missing SESSIONS) equals the sessions the gaps hold.
    detail = (await client.get(f'/ui/v1/datasets/{dataset.id}')).json()['freshness']
    assert (detail['status'], detail['gapCount']) == ('FRESHNESS_STATUS_GAPS', sum(g[2] for g in strip.gaps))


@pytest.mark.asyncio
async def test_the_gap_window_holds_exactly_the_missing_sessions_bars(client, engine, covered_dates_as_dates):
    """A bar a tick before the window closes nothing; one a tick before its end closes the last missing session.

    (A bar AT the end is the next session's own daily bar, already stored: the end is excluded.)
    """
    missing = _weekend_run(SEPTEMBER)
    dataset = entry('WIN', start=midnight(SEPTEMBER[0]), end=midnight(SEPTEMBER[-1] + DAY))
    await store(engine, dataset, *daily_bars(dataset, [s for s in SEPTEMBER if s not in missing]))
    start, end, _ = window(missing[0], missing[-1])

    assert session_of(end, XNYS) in SEPTEMBER and session_of(end, XNYS) not in missing, 'precondition'
    await store(engine, bar(dataset, start - TICK))
    assert (await coverage(client, dataset)).gaps == [window(missing[0], missing[-1])]

    await store(engine, bar(dataset, end - TICK))
    assert (await coverage(client, dataset)).gaps == [window(missing[0], missing[-2])]

    await store(engine, *daily_bars(dataset, missing[:-1]))
    closed = await coverage(client, dataset)
    assert (closed.gaps, closed.gap_count, closed.missing) == ([], 0, 0)


@pytest.mark.asyncio
async def test_weekends_holidays_and_early_closes_are_never_gaps(client, engine, covered_dates_as_dates):
    """Acceptance b, over the 2025 holiday season: a mid-week holiday joins its neighbours into ONE gap."""
    start, end = midnight(date(2025, 11, 3)), midnight(date(2026, 1, 10))
    sessions = XNYS.sessions_overlapping(start, end)
    close_times = Counter(XNYS.session_close(s).astimezone(XNYS.tz).time() for s in sessions)
    regular_close = close_times.most_common(1)[0][0]
    early = [s for s in sessions if XNYS.session_close(s).astimezone(XNYS.tz).time() < regular_close]
    holidays = [
        day
        for day in (sessions[0] + timedelta(days=n) for n in range((sessions[-1] - sessions[0]).days))
        if day.weekday() in (1, 2, 3) and day not in sessions
    ]
    assert early and holidays, ('precondition', early, holidays)

    def neighbours(holiday: date) -> list[date]:
        return [max(s for s in sessions if s < holiday), min(s for s in sessions if s > holiday)]

    # The first mid-week holiday whose neighbours close on time, so every early close stays covered.
    missing = next(neighbours(h) for h in holidays if not set(neighbours(h)) & set(early))
    dataset = entry('HOL', start=start, end=end)
    held = [s for s in sessions if s not in missing]
    # An early close holds one intraday bar at its open; every other session a daily bar at local midnight.
    await store(
        engine,
        dataset,
        *(bar(dataset, XNYS.session_open(s).astimezone(UTC)) for s in held if s in early),
        *daily_bars(dataset, [s for s in held if s not in early]),
    )

    strip = await coverage(client, dataset)

    assert strip.gaps == [window(missing[0], missing[-1])] and strip.gap_count == 1, strip.gaps
    assert strip.gaps[0][2] == len(missing), 'the holiday is not a session, so it is not counted'
    assert strip.expected == len(sessions) and strip.missing == len(missing)
    for session in early:
        assert not any(gap_start <= midnight(session) < gap_end for gap_start, gap_end, _ in strip.gaps), session


@pytest.mark.asyncio
async def test_sessions_in_progress_or_in_the_future_are_neither_expected_nor_gaps(client, engine):
    """Acceptance d: a DAILY dataset created today owes nothing yet; a STATIC one ending next week owes only the past."""
    today = XNYS.next_session(SEPTEMBER[-1] + timedelta(days=5))
    now = XNYS.session_open(today) + timedelta(hours=1)
    assert XNYS.session_close(today) > now, 'precondition: today is in progress'
    daily = entry('NEWD', update_type=UpdateType.DAILY, start=midnight(today))
    static = entry('FUT', start=midnight(today) - timedelta(days=7), end=midnight(today) + timedelta(days=7))
    await store(engine, daily, static)
    pin_clock(now)

    fresh = await coverage(client, daily)
    assert (fresh.gaps, fresh.gap_count, fresh.expected) == ([], 0, 0)
    assert fresh.end == now and len(fresh.buckets) == 1, fresh

    later = await coverage(client, static)
    sessions = XNYS.sessions_overlapping(static.start, static.end)
    completed = [s for s in sessions if XNYS.session_close(s) <= now]
    assert today in sessions and today not in completed
    assert later.expected == len(completed) < len(sessions)
    assert sum(gap[2] for gap in later.gaps) == len(completed)
    assert all(gap_end <= midnight(today) for _, gap_end, _ in later.gaps), later.gaps
    assert len(later.buckets) == len(sessions), 'future sessions are still bucketed, just not expected'


S = SEPTEMBER
# name -> (start, end, the sessions the strip must expect). Half-open, at both ends.
BOUND_CASES = {
    'local midnights': (lambda: midnight(S[1]), lambda: midnight(S[4]), lambda: S[1:4]),
    'end at the next open': (lambda: midnight(S[1]), lambda: XNYS.session_open(S[4]), lambda: S[1:4]),
    'end a tick past the next open': (lambda: midnight(S[1]), lambda: XNYS.session_open(S[4]) + TICK, lambda: S[1:5]),
    'start at a close': (lambda: XNYS.session_close(S[1]), lambda: midnight(S[4]), lambda: S[2:4]),
    'start a tick before a close': (lambda: XNYS.session_close(S[1]) - TICK, lambda: midnight(S[4]), lambda: S[1:4]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('start', 'end', 'sessions'), BOUND_CASES.values(), ids=BOUND_CASES)
async def test_the_range_is_half_open_at_the_dataset_s_start_and_end(client, engine, start, end, sessions):
    dataset = entry('BND', start=start().astimezone(UTC), end=end().astimezone(UTC))
    await store(engine, dataset)

    strip = await coverage(client, dataset)

    assert (strip.start, strip.end) == (dataset.start, dataset.end)
    assert strip.expected == len(sessions()) == strip.missing
    assert strip.gaps == [(midnight(sessions()[0]), midnight(sessions()[-1] + DAY), len(sessions()))]
    assert_tiles(strip)


DST_RANGES = {'spring': (date(2026, 3, 2), date(2026, 3, 14)), 'autumn': (date(2025, 10, 27), date(2025, 11, 8))}


@pytest.mark.asyncio
@pytest.mark.parametrize(('first', 'last'), DST_RANGES.values(), ids=DST_RANGES)
async def test_a_gap_across_a_dst_change_runs_local_midnight_to_local_midnight(
    client, engine, covered_dates_as_dates, first, last
):
    start, end = midnight(first), midnight(last)
    sessions = XNYS.sessions_overlapping(start, end)
    pair = next(
        [a, b]
        for a, b in pairwise(sessions)
        if local_midnight(a, XNYS).utcoffset() != local_midnight(b, XNYS).utcoffset()
    )
    dataset = entry('DST', start=start, end=end)
    await store(engine, dataset, *daily_bars(dataset, [s for s in sessions if s not in pair]))

    strip = await coverage(client, dataset)

    assert strip.gaps == [window(pair[0], pair[1])], strip.gaps
    gap_start, gap_end, _ = strip.gaps[0]
    assert (gap_end - gap_start) % DAY != timedelta(0), 'precondition: the window crosses an offset change'
    assert session_of(gap_start, XNYS) == pair[0] and session_of(gap_start - TICK, XNYS) == pair[0] - DAY
    assert session_of(gap_end - TICK, XNYS) == pair[1] and session_of(gap_end, XNYS) == pair[1] + DAY
    for instant in (gap_start, gap_end):
        assert instant.astimezone(XNYS.tz).time() == datetime.min.time(), instant
    assert_tiles(strip)


@pytest.mark.asyncio
async def test_a_gap_on_an_exchange_whose_local_day_is_not_utc_s(client, engine, covered_dates_as_dates, monkeypatch):
    """XTKS (+09:00) in place of the source's calendar: midnight in Tokyo is the previous UTC date.

    The calendar reaches dataset_coverage through the name it looks it up by; the covered dates are computed in
    Asia/Tokyo by the same statement, because the zone passed is the calendar's. A rule that took a UTC date or a
    UTC midnight puts the window a day off.
    """
    monkeypatch.setattr(dataset_catalog_module, 'calendar_for_source', lambda source: XTKS)
    sessions = XTKS.sessions_overlapping(midnight(date(2025, 6, 2), XTKS), midnight(date(2025, 6, 28), XTKS))
    missing = sessions[3:5]
    dataset = entry('TKY', start=midnight(sessions[0], XTKS), end=midnight(sessions[-1] + DAY, XTKS))
    await store(engine, dataset, *daily_bars(dataset, [s for s in sessions if s not in missing], XTKS))

    strip = await coverage(client, dataset)

    assert strip.gaps == [window(missing[0], missing[-1], XTKS)], strip.gaps
    gap_start, gap_end, count = strip.gaps[0]
    assert count == len(missing)
    assert gap_start.astimezone(UTC).date() == missing[0] - DAY, 'Tokyo midnight is the previous UTC date'
    assert session_of(gap_start, XTKS) == missing[0] and session_of(gap_end - TICK, XTKS) == missing[-1]
    assert strip.expected == len(sessions) and strip.missing == len(missing)
    assert_tiles(strip)


@pytest.mark.asyncio
async def test_more_than_500_gaps_lists_the_first_500_and_counts_them_all(client, engine, covered_dates_as_dates):
    start, end = midnight(date(2021, 1, 4)), midnight(date(2026, 1, 5))
    sessions = XNYS.sessions_overlapping(start, end)
    held = sessions[::2]
    dataset = entry('MANY', start=start, end=end)
    await store(engine, dataset, *daily_bars(dataset, held))
    intervals = gap_intervals(start, end, frozenset(held), XNYS, NOW)
    assert len(intervals) > MAX_COVERAGE_GAPS, 'precondition'

    strip = await coverage(client, dataset)

    assert strip.gap_count == len(intervals)
    assert strip.gaps == [window(i.first_session, i.last_session) for i in intervals[:MAX_COVERAGE_GAPS]]
    assert len(strip.gaps) == MAX_COVERAGE_GAPS == 500
    assert strip.missing == sum(i.sessions for i in intervals) > sum(gap[2] for gap in strip.gaps)


@pytest.mark.asyncio
async def test_buckets_default_to_120_and_are_bounded_1_to_400(client, engine):
    year = entry('YEAR', start=midnight(date(2025, 1, 2)), end=midnight(date(2026, 1, 2)))
    await store(engine, year)
    sessions = XNYS.sessions_overlapping(year.start, year.end)
    assert DEFAULT_COVERAGE_BUCKETS < len(sessions) < MAX_COVERAGE_BUCKETS, 'precondition'

    assert len((await coverage(client, year)).buckets) == DEFAULT_COVERAGE_BUCKETS == 120
    assert len((await coverage(client, year, buckets=1)).buckets) == 1
    every = await coverage(client, year, buckets=MAX_COVERAGE_BUCKETS)
    assert MAX_COVERAGE_BUCKETS == 400 and len(every.buckets) == len(sessions)
    assert {bucket[2] for bucket in every.buckets} == {1}
    for buckets in (0, MAX_COVERAGE_BUCKETS + 1, 'many'):
        errors = validation_errors(await client.get(coverage_path(year.id), params={'buckets': buckets}))
        assert [error['loc'] for error in errors] == [['query', 'buckets']], errors


@pytest.mark.asyncio
async def test_coverage_of_an_unknown_dataset_is_the_detail_s_404(client, catalog):
    unknown = uuid.uuid4()
    body = problem(await client.get(coverage_path(unknown)), status=404, reason='NOT_FOUND')
    detail = problem(await client.get(f'/ui/v1/datasets/{unknown}'), status=404, reason='NOT_FOUND')
    assert body['detail'] == detail['detail']


@pytest.mark.asyncio
async def test_coverage_with_no_calendar_is_a_typed_422_never_an_empty_strip(client, catalog, early):
    """Acceptance e: a source with no calendar, and a range before the calendar's start, are 422 INVALID_REQUEST."""
    for dataset in (catalog.ggg, early.static, early.daily):
        body = problem(await client.get(coverage_path(dataset.id)), status=422, reason='INVALID_REQUEST')
        assert 'calendar' in body['detail'], body


@pytest.mark.asyncio
async def test_a_coverage_read_is_two_selects_whatever_the_datasets_and_bars(client, engine, covered_dates_as_dates):
    """Acceptance f: the entry and its covered dates; no write; flat as datasets and bars grow."""
    dataset = entry('FLAT', start=midnight(SEPTEMBER[0]), end=midnight(SEPTEMBER[-1] + DAY))
    await store(engine, dataset, *daily_bars(dataset, SEPTEMBER[:3]))
    statements: list[str] = []
    event.listen(engine.sync_engine, 'before_cursor_execute', lambda *args: statements.append(args[2]))

    async def read() -> list[str]:
        statements.clear()
        await coverage(client, dataset)
        return list(statements)

    few = await read()
    others = [entry(f'F{index:03d}', start=dataset.start, end=dataset.end) for index in range(40)]
    await store(engine, *others)
    await store(engine, *daily_bars(dataset, SEPTEMBER[3:]), *(b for o in others for b in daily_bars(o, SEPTEMBER)))
    many = await read()

    assert len(few) == len(many) == 2, (few, many)
    assert all(statement.lstrip().upper().startswith('SELECT') for statement in few + many), many


# ------------------------------------------------------------------------------------------------- bar page index
#
# GET /ui/v1/datasets/{id}/bar-pages (validator, gating tj-grna9p.120; design: the 2026-10-13 OWNER RULINGS note on
# decision tj-eozqhi, FIXED BLOCKS, which supersedes the decision's item 1, the 950 step). With N bars in the
# half-open window, q = N div size and r = N mod size: pages are fixed blocks of `size` bars counted BACK FROM THE
# NEWEST bar, so their offsets are r, r + size, ..., N - size; the earliest page is a display exception at offset 0,
# listed first when r > 0, which still holds `size` bars and so overlaps its newer neighbour by size - r; when r = 0
# the first block is already at 0 and is listed once. N <= size is the single page [0], N = 0 has no pages, and
# M = q + (r > 0). `overlap` is now the LOOK-AHEAD the client adds from the next block (fetch limit size + overlap,
# none past the newest page): it is still validated 0 <= overlap < size and never moves an offset.
#
# The offsets below are the ruling's table written out by hand, not recomputed by the rule, so a test cannot share
# a mistake with the code it checks. (Re-pinned from the 950-step rule; every changed row says why under the new
# rule. Rows the two rules agree on -- 0, 1, 999, 1000, 1001, 1950 -- are unchanged.)
#
# NOT CLAIMED HERE (SQLite): the plan and its cost. The index-range scan, the streaming WindowAgg and the timings on
# a million-bar window are Postgres's: tests/system/test_http_ui_bar_pages.py and the measurement on the bead.

PAGE_EPOCH = z('2026-10-01T00:00:00Z')

OFFSET_TABLE = {
    0: [],
    1: [0],
    999: [0],  # r = 999 but N <= size: the single page, never [0, 999]
    1000: [0],  # r = 0, q = 1: the one full block IS offset 0, listed once
    1001: [0, 1],  # r = 1: the earliest page [0, 1000) overlaps the newest block [1, 1001) by 999
    1950: [0, 950],
    1951: [0, 951],  # was [0, 950, 951]: the newest block is [951, 1951), the earliest anchored at 0
    1952: [0, 952],  # was [0, 950, 952]
    2500: [0, 500, 1500],  # r = 500: blocks [500, 1500) and [1500, 2500), plus the earliest
    3000: [0, 1000, 2000],  # r = 0: three disjoint blocks, offset 0 once; was [0, 950, 1900, 2000]
}

# 180,437 bars (the ruling's example: about a year of one-minute bars): r = 437, so the earliest page at 0, then
# 180 full blocks from 437 to 179,437. 181 pages. Spelled with range() for length, then checked against literals.
LARGE_WINDOW = 180_437
LARGE_OFFSETS = [0, *range(437, LARGE_WINDOW, 1000)]
assert (len(LARGE_OFFSETS), LARGE_OFFSETS[:4], LARGE_OFFSETS[-1]) == (181, [0, 437, 1437, 2437], 179_437)


def minute(index: int) -> datetime:
    return PAGE_EPOCH + timedelta(minutes=index)


async def minute_series(engine: AsyncEngine, count: int, symbol: str = 'PAGES') -> StoreDatasetEntry:
    """A 1min dataset of its own holding COUNT bars, one a minute from PAGE_EPOCH. Source IB: no freshness.

    The entry goes through the ORM; the bars through one Core executemany, so a 180k- or 50k-bar series costs
    seconds, not the ORM's unit of work per row. The values are bar()'s, column for column.
    """
    dataset = entry(symbol, granularity=Granularity.ONE_MINUTE, source=DataSource.IB_API)
    await store(engine, dataset)
    template = bar(dataset, PAGE_EPOCH)
    columns = {
        column.key: getattr(template, column.key)
        for column in StockMarketActivity.__table__.columns
        if getattr(template, column.key, None) is not None
    }
    rows = [{**columns, 'timestamp': minute(index), 'close': float(index)} for index in range(count)]
    if rows:
        async with engine.begin() as connection:
            await connection.execute(insert(StockMarketActivity.__table__), rows)
    return dataset


def bar_pages_path(dataset_id: uuid.UUID) -> str:
    return f'/ui/v1/datasets/{dataset_id}/bar-pages'


async def page_index(client: httpx.AsyncClient, dataset_id: uuid.UUID, **params: Any) -> dict:
    response = await client.get(bar_pages_path(dataset_id), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def offsets_of(body: dict) -> list[int]:
    """Each page's offset. Canonical JSON writes an int64 as a string and OMITS a zero, so absent reads as 0."""
    return [int(page.get('offset', '0')) for page in body.get('pages', [])]


@pytest.mark.asyncio
@pytest.mark.parametrize(('count', 'expected'), OFFSET_TABLE.items(), ids=[f'N={n}' for n in OFFSET_TABLE])
async def test_the_page_offsets_are_the_owner_s_fixed_block_table(client, engine, count, expected):
    """At size 1000, overlap 50: M = len(pages) exactly, ascending, each start the timestamp of the bar at its offset."""
    dataset = await minute_series(engine, count)
    body = await page_index(client, dataset.id, size=1000, overlap=50)

    assert int(body.get('barCount', '0')) == count
    assert offsets_of(body) == expected
    assert [page['start'] for page in body.get('pages', [])] == [stamp(minute(offset)) for offset in expected]
    assert (body['pageSize'], body['overlap']) == (1000, 50), 'the index names the geometry it was cut with'
    if count:
        # The newest page ends on the window's last bar, full whenever the window holds a full page.
        assert expected[-1] + min(count, 1000) == count
    else:
        assert 'pages' not in body and 'barCount' not in body, body


@pytest.mark.asyncio
async def test_a_year_of_minutes_is_181_pages_counted_back_from_the_newest_bar(client, engine):
    """N = 180,437, r = 437: [0, 437, 1437, ..., 179437]. M = q + (r > 0) = 180 + 1, exactly, in one request."""
    dataset = await minute_series(engine, LARGE_WINDOW)
    body = await page_index(client, dataset.id)

    assert int(body['barCount']) == LARGE_WINDOW
    assert offsets_of(body) == LARGE_OFFSETS
    starts = [page['start'] for page in body['pages']]
    assert (starts[0], starts[1], starts[-1]) == (stamp(minute(0)), stamp(minute(437)), stamp(minute(179_437)))
    assert starts == [stamp(minute(offset)) for offset in LARGE_OFFSETS]


@pytest.mark.asyncio
async def test_size_and_overlap_default_to_1000_and_50(client, engine):
    dataset = await minute_series(engine, 1951)
    default = await page_index(client, dataset.id)
    assert default == await page_index(client, dataset.id, size=1000, overlap=50)
    # Was [0, 950, 951] under the 950 step: now the earliest page and the one full block counted from the newest.
    assert offsets_of(default) == [0, 951]


# (size, overlap, N, offsets). Off the default geometry: the step is SIZE whatever the overlap, blocks are counted
# back from the newest bar, the earliest page is anchored at 0 when r > 0, and r = 0 lists offset 0 once.
GEOMETRY_CASES = {
    'no overlap, r = 0, offset 0 not repeated': (1000, 0, 3000, [0, 1000, 2000]),
    # was [0, 1000, 1500] (blocks from the oldest bar, last anchored): now from the newest, r = 500.
    'no overlap, r = 500': (1000, 0, 2500, [0, 500, 1500]),
    # was [0, 1, 2] (a step of one): the overlap no longer shortens the step. r = 2: blocks [2, 12), earliest [0, 10).
    'overlap size - 1 does not shorten the step': (10, 9, 12, [0, 2]),
    'size 1 is one page per bar': (1, 0, 3, [0, 1, 2]),
    # was [0, 7, 14, 20] (step 7): now three disjoint blocks of 10, r = 0.
    'small pages, default-like overlap, r = 0': (10, 3, 30, [0, 10, 20]),
    'small pages, r = 7': (10, 3, 27, [0, 7, 17]),
    'small pages, r = 1, earliest overlaps by 9': (10, 5, 11, [0, 1]),
    'small pages, N = size': (10, 9, 10, [0]),
    'small pages, N < size': (10, 0, 9, [0]),
    'odd size, r = 2': (7, 6, 23, [0, 2, 9, 16]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('size', 'overlap', 'count', 'expected'), GEOMETRY_CASES.values(), ids=GEOMETRY_CASES)
async def test_other_page_geometries_follow_the_same_rule(client, engine, size, overlap, count, expected):
    dataset = await minute_series(engine, count)
    body = await page_index(client, dataset.id, size=size, overlap=overlap)
    assert (int(body['barCount']), offsets_of(body)) == (count, expected)
    assert [page['start'] for page in body['pages']] == [stamp(minute(offset)) for offset in expected]
    assert (body['pageSize'], body.get('overlap', 0)) == (size, overlap), 'the overlap asked for is echoed'


# (size, N, the overlaps to try). Every valid overlap, 0 to size - 1, must give the same offsets: it is the
# client's look-ahead and never enters the page rule.
OVERLAP_INVARIANCE = {
    'size 1000, N 2500': (1000, 2500, [0, 1, 50, 500, 999]),
    'size 1000, N 3000': (1000, 3000, [0, 1, 50, 999]),
    'size 10, N 27, every overlap': (10, 27, list(range(10))),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('size', 'count', 'overlaps'), OVERLAP_INVARIANCE.values(), ids=OVERLAP_INVARIANCE)
async def test_the_overlap_never_moves_an_offset(client, engine, size, count, overlaps):
    dataset = await minute_series(engine, count)
    seen = {
        overlap: offsets_of(await page_index(client, dataset.id, size=size, overlap=overlap)) for overlap in overlaps
    }
    assert len({tuple(offsets) for offsets in seen.values()}) == 1, seen
    assert seen[overlaps[0]] == {2500: [0, 500, 1500], 3000: [0, 1000, 2000], 27: [0, 7, 17]}[count]


@pytest.mark.asyncio
async def test_the_window_is_half_open_and_offsets_count_from_its_first_bar(client, engine):
    """[minute 10, minute 1962): the bar AT start is offset 0, the bar AT end is not counted. N = 1952, r = 952."""
    dataset = await minute_series(engine, 2000)
    window = {'start': stamp(minute(10)), 'end': stamp(minute(1962))}
    body = await page_index(client, dataset.id, **window)

    # Was [0, 950, 952]: now the earliest page and the one full block [952, 1952) of the window.
    assert (int(body['barCount']), offsets_of(body)) == (1952, [0, 952])
    assert [page['start'] for page in body['pages']] == [stamp(minute(10 + offset)) for offset in (0, 952)]


# (first bar of the window, N): the window is [minute(first), minute(first + N)) over a 3,100-bar series, so the
# offsets are relative to a window that does not start at the series' first bar.
READ_BACK_WINDOWS = {
    'r = 952, two pages': (10, 1952),
    'r = 500, three pages': (37, 2500),
    'r = 0, three disjoint blocks': (5, 3000),
    'r = 1, earliest overlaps by 999': (90, 1001),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('first', 'count'), READ_BACK_WINDOWS.values(), ids=READ_BACK_WINDOWS)
async def test_each_page_read_back_through_the_bars_route_is_one_fixed_block(client, engine, first, count):
    """The docstring's contract: page k = bars?start=pages[k].start&end=<window end>&limit=size = [offset, +size).

    Under the fixed-block rule (replacing 'consecutive pages share at least the overlap'): every page is exactly
    `size` bars; the full blocks never overlap one another and tile the window's newest q * size bars; the earliest
    page overlaps its newer neighbour by exactly size - r when r > 0 (and is itself a block when r = 0); the window's
    last bar is the newest page's last bar; together the pages hold every bar of the window once over.
    """
    size, remainder = 1000, count % 1000
    dataset = await minute_series(engine, 3100)
    end = stamp(minute(first + count))
    body = await page_index(client, dataset.id, start=stamp(minute(first)), end=end)
    window = [stamp(minute(index)) for index in range(first, first + count)]

    pages: list[list[str]] = []
    for offset, page in zip(offsets_of(body), body['pages'], strict=True):
        response = await client.get(
            f'/ui/v1/datasets/{dataset.id}/bars', params={'start': page['start'], 'end': end, 'limit': size}
        )
        read = [item['barStart'] for item in response.json()['bars']]
        assert read == window[offset : offset + size]
        assert len(read) == size, 'every page of a window over size bars is exactly size bars'
        pages.append(read)

    shared = [len(set(before) & set(after)) for before, after in pairwise(pages)]
    if remainder:
        assert shared == [size - remainder] + [0] * (len(pages) - 2), shared
    else:
        assert shared == [0] * (len(pages) - 1), shared
    assert pages[-1][-1] == window[-1], 'the window ends on the newest page'
    assert sorted({start for page in pages for start in page}) == window


@pytest.mark.asyncio
async def test_a_window_of_at_most_one_page_reads_back_as_that_page(client, engine):
    """The single-page exception: N = 999 is one page of 999 bars, not a short page plus a full one."""
    dataset = await minute_series(engine, 1200)
    end = stamp(minute(1009))
    body = await page_index(client, dataset.id, start=stamp(minute(10)), end=end)
    assert (int(body['barCount']), offsets_of(body)) == (999, [0])
    response = await client.get(
        f'/ui/v1/datasets/{dataset.id}/bars', params={'start': body['pages'][0]['start'], 'end': end, 'limit': 1000}
    )
    assert [item['barStart'] for item in response.json()['bars']] == [stamp(minute(index)) for index in range(10, 1009)]


@pytest.mark.asyncio
async def test_a_page_read_with_its_look_ahead_takes_the_next_block_s_first_bars(client, engine):
    """The client's read, limit = size + overlap: page k gains the NEXT (newer) block's first `overlap` bars.

    The newest page has none, because the window's end bounds it. N = 2500, r = 500, overlap 50.
    """
    dataset = await minute_series(engine, 2600)
    end = stamp(minute(2510))
    body = await page_index(client, dataset.id, start=stamp(minute(10)), end=end)
    window = [stamp(minute(index)) for index in range(10, 2510)]
    assert offsets_of(body) == [0, 500, 1500]

    reads = []
    for page in body['pages']:
        response = await client.get(
            f'/ui/v1/datasets/{dataset.id}/bars', params={'start': page['start'], 'end': end, 'limit': 1050}
        )
        reads.append([item['barStart'] for item in response.json()['bars']])
    assert reads[0] == window[0:1050], 'the earliest page plus 50 bars past its own end, inside the next block'
    assert reads[1] == window[500:1550], "block [500, 1500) plus the next block [1500, ...)'s first 50"
    assert reads[2] == window[1500:2500], 'the newest page: no look-ahead past the window'


@pytest.mark.asyncio
async def test_bars_of_another_granularity_or_feed_never_enter_the_index(client, engine):
    """Same symbol, other width: a sibling dataset AND stray rows under this dataset's own id are not counted.

    The stray rows pin the five-column identity predicate the index shares with the bars read: if either read
    dropped a column, the index's offsets and the bars route's pages would disagree.
    """
    dataset = await minute_series(engine, 1001)
    sibling = entry('PAGES', granularity=Granularity.ONE_HOUR, source=DataSource.IB_API)
    strays = []
    for index in range(1001):
        for granularity, feed in ((Granularity.ONE_HOUR, dataset.feed), (dataset.granularity, Feed.SIP)):
            stray = bar(dataset, minute(index) + timedelta(seconds=30))
            stray.granularity, stray.feed = granularity, feed
            strays.append(stray)
    await store(engine, sibling, *(bar(sibling, minute(index)) for index in range(1500)), *strays)

    body = await page_index(client, dataset.id)
    assert (int(body['barCount']), offsets_of(body)) == (1001, [0, 1])
    assert [page['start'] for page in body['pages']] == [stamp(minute(0)), stamp(minute(1))]


@pytest.mark.asyncio
async def test_an_empty_or_inverted_window_is_an_empty_index_not_an_error(client, engine):
    """As the bars read (test_no_bounds_reads_everything_and_an_empty_or_inverted_window_is_an_empty_page)."""
    dataset = await minute_series(engine, 20)
    for start, end in ((minute(5), minute(5)), (minute(9), minute(2)), (minute(100), minute(200))):
        body = await page_index(client, dataset.id, start=stamp(start), end=stamp(end))
        assert body == {'pageSize': 1000, 'overlap': 50}, body


@pytest.mark.asyncio
async def test_bar_pages_of_an_unknown_dataset_is_a_404(client, engine):
    await minute_series(engine, 3)
    problem(await client.get(bar_pages_path(uuid.uuid4())), status=404, reason='NOT_FOUND')


BAR_PAGES_REFUSALS = {
    'size 0': ({'size': 0}, 'size'),
    'size 1001': ({'size': 1001}, 'size'),
    'negative overlap': ({'overlap': -1}, 'overlap'),
    'naive start': ({'start': '2026-10-01T00:00:00'}, 'start'),
    'naive end': ({'end': '2026-10-01T00:00:00'}, 'end'),
    'unknown parameter': ({'limit': 1000}, 'limit'),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('params', 'field'), BAR_PAGES_REFUSALS.values(), ids=BAR_PAGES_REFUSALS)
async def test_a_bad_bar_pages_parameter_is_a_422_naming_it(client, engine, params, field):
    dataset = await minute_series(engine, 3)
    response = await client.get(bar_pages_path(dataset.id), params=params)
    assert field in [error['loc'][-1] for error in validation_errors(response)]


@pytest.mark.asyncio
@pytest.mark.parametrize(('size', 'overlap'), [(1000, 1000), (1000, 1001), (10, 50), (1, 1)])
async def test_an_overlap_not_below_the_size_is_a_422(client, engine, size, overlap):
    """An overlap >= size would be a step of zero or less: a 422 INVALID_REQUEST, never a page list."""
    dataset = await minute_series(engine, 30)
    body = problem(
        await client.get(bar_pages_path(dataset.id), params={'size': size, 'overlap': overlap}),
        status=422,
        reason='INVALID_REQUEST',
    )
    assert 'overlap' in body['detail'], body


@pytest.mark.asyncio
async def test_the_size_and_overlap_bounds_are_inclusive(client, engine):
    dataset = await minute_series(engine, 5)
    assert offsets_of(await page_index(client, dataset.id, size=1000, overlap=999)) == [0]
    assert offsets_of(await page_index(client, dataset.id, size=1, overlap=0)) == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_the_first_page_has_no_offset_on_the_wire_and_parses_as_offset_0(client, engine):
    """proto3 canonical JSON omits a zero int64: the raw body has no 'offset' key, and a parser reads 0.

    The Python half is here; the generated TypeScript client's reading of it is the web bead's (tj-grna9p.121).
    """
    dataset = await minute_series(engine, 1001)
    response = await client.get(bar_pages_path(dataset.id))
    raw = response.json()
    assert 'offset' not in raw['pages'][0], raw
    assert raw['pages'][1]['offset'] == '1', 'an int64 is a JSON string in canonical JSON'

    parsed = json_format.Parse(response.text, BarPageIndexMessage())
    assert [(page.offset, page.start.ToDatetime(tzinfo=UTC)) for page in parsed.pages] == [
        (0, minute(0)),
        (1, minute(1)),
    ]
    assert (parsed.bar_count, parsed.page_size, parsed.overlap) == (1001, 1000, 50)


@pytest.mark.asyncio
async def test_a_bar_pages_request_is_two_selects_however_large_the_window(client, engine):
    """The entry, then the index: flat from 5 bars to 3000, never one statement per page or per bar."""
    statements: list[str] = []
    event.listen(engine.sync_engine, 'before_cursor_execute', lambda *args: statements.append(args[2]))

    async def read(dataset: StoreDatasetEntry) -> list[str]:
        statements.clear()
        await page_index(client, dataset.id)
        return list(statements)

    small = await read(await minute_series(engine, 5, symbol='SMALL'))
    large = await read(await minute_series(engine, 3000, symbol='LARGE'))
    assert len(small) == len(large) == 2, (small, large)
    assert all(statement.lstrip().upper().startswith('SELECT') for statement in small + large), large


@pytest.mark.asyncio
@pytest.mark.parametrize(('count', 'pages'), [(3000, 3), (2500, 3), (2999, 3), (1001, 2)])
async def test_the_index_fetches_one_row_per_page_never_one_per_bar(client, engine, monkeypatch, count, pages):
    """The page filter runs in SQL: the rows leaving the database are exactly the pages, q + (r > 0) of them.

    Re-pinned from 'at most ceil(N / 950) + 1': under fixed blocks the count is exact, so it is asserted exactly.
    """
    original = AsyncSession.execute
    fetched: list[int] = []

    async def spying(self, statement, *args, **kwargs):
        result = await original(self, statement, *args, **kwargs)
        if 'row_number' not in str(statement):
            return result
        frozen = result.freeze()
        fetched.append(len(frozen.data))
        return frozen()

    monkeypatch.setattr(AsyncSession, 'execute', spying)
    dataset = await minute_series(engine, count)
    body = await page_index(client, dataset.id)

    assert fetched == [len(body['pages'])] == [pages], fetched
    assert pages == count // 1000 + (count % 1000 > 0)


# ------------------------------------------------------------------------------------------------- 1week and 1month
#
# tj-grna9p.151 (decision tj-grna9p.8, addendum of 2026-10-20): a 1week or 1month dataset is judged BY PERIOD, and
# the catalog callers pass row.granularity (dataset_catalog.py evaluate_views and dataset_coverage). The pure rules
# are pinned in test_freshness_periods.py; this drives them end to end through the list, the detail and the coverage
# strip. Under the per-session rule every case below reads otherwise (the weekly one as 35 missing sessions).
#
# NOW is Tue 6 Oct 2026 22:00Z: the week of 28 Sep has ended (Fri 2 Oct), the week of 5 Oct has not.

WEEKLY_BARS = [date(2026, 8, day) for day in (3, 10, 17, 24, 31)] + [
    # Labor Day week: the vendor stamps the first session, Tuesday 8 Sep.
    date(2026, 9, 8),
    date(2026, 9, 14),
    date(2026, 9, 21),
    date(2026, 9, 28),
]


def weekly_static(bars: list[date]) -> tuple[StoreDatasetEntry, list]:
    dataset = entry('WKLY', granularity=Granularity.ONE_WEEK, start=midnight(date(2026, 8, 3)), end=ny_midnight(5))
    return dataset, daily_bars(dataset, bars)


@pytest.mark.asyncio
async def test_a_complete_weekly_dataset_reads_complete_with_no_gap_windows(client, engine, covered_dates_as_dates):
    dataset, bars = weekly_static(WEEKLY_BARS)
    await store(engine, dataset, *bars)

    detail = (await client.get(f'/ui/v1/datasets/{dataset.id}')).json()['freshness']
    assert detail['status'] == 'FRESHNESS_STATUS_COMPLETE' and 'gapCount' not in detail, detail
    listed = {item['id']: item for item in await list_all(client)}
    assert status_of(listed[str(dataset.id)]) == 'FRESHNESS_STATUS_COMPLETE'
    strip = await coverage(client, dataset)
    assert (strip.gaps, strip.gap_count, strip.missing) == ([], 0, 0), strip
    # Buckets still count SESSIONS: Mon 3 Aug to Fri 2 Oct, 44 of them (Labor Day is not one).
    assert strip.expected == 44 == len(XNYS.sessions_overlapping(dataset.start, dataset.end))
    assert_tiles(strip)


@pytest.mark.asyncio
async def test_a_weekly_dataset_missing_a_week_reads_its_five_sessions_in_one_window(
    client, engine, covered_dates_as_dates
):
    dataset, bars = weekly_static([day for day in WEEKLY_BARS if day != date(2026, 8, 31)])
    await store(engine, dataset, *bars)

    detail = (await client.get(f'/ui/v1/datasets/{dataset.id}')).json()['freshness']
    assert (detail['status'], detail['gapCount']) == ('FRESHNESS_STATUS_GAPS', 5)
    strip = await coverage(client, dataset)
    assert strip.gaps == [window(date(2026, 8, 31), date(2026, 9, 4))] and strip.gaps[0][2] == 5, strip.gaps
    assert (strip.gap_count, strip.missing, strip.expected) == (1, 5, 44)


@pytest.mark.asyncio
async def test_a_complete_monthly_dataset_reads_complete(client, engine, covered_dates_as_dates):
    """Bars on each 1st, Jan to Sep 2026, whatever the weekday (1 Jan is a holiday, 1 Feb and 1 Mar are Sundays)."""
    dataset = entry('MNTH', granularity=Granularity.ONE_MONTH, start=ny_midnight(1, 1), end=ny_midnight(1, 10))
    await store(engine, dataset, *daily_bars(dataset, [date(2026, month, 1) for month in range(1, 10)]))

    detail = (await client.get(f'/ui/v1/datasets/{dataset.id}')).json()['freshness']
    assert detail['status'] == 'FRESHNESS_STATUS_COMPLETE', detail
    strip = await coverage(client, dataset)
    assert (strip.gaps, strip.gap_count, strip.missing) == ([], 0, 0), strip


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('last', 'status'),
    [(date(2026, 9, 28), 'FRESHNESS_STATUS_FRESH'), (date(2026, 9, 21), 'FRESHNESS_STATUS_OVERDUE')],
    ids=['last ended week stored', 'a week behind'],
)
async def test_a_weekly_subscription_reads_by_its_last_ended_week(client, engine, last, status):
    """The week of 28 Sep ended Fri 2 Oct; its deadline was Monday 5 Oct's open.

    That week's local midnight (04:00Z, EDT) is the wire's expected_last_bar.
    """
    dataset = entry('WSUB', granularity=Granularity.ONE_WEEK, update_type=UpdateType.DAILY, start=ny_midnight(7, 9))
    await store(engine, dataset, *daily_bars(dataset, [day for day in WEEKLY_BARS[5:] if day <= last]))

    for freshness in (
        (await client.get(f'/ui/v1/datasets/{dataset.id}')).json()['freshness'],
        {item['id']: item for item in await list_all(client)}[str(dataset.id)]['freshness'],
    ):
        assert (freshness['status'], freshness['expectedLastBar']) == (status, '2026-09-28T04:00:00Z'), freshness


# tj-grna9p.156 (the clarification on decision tj-grna9p.8 of 2026-10-20): over a range ending mid-period, the week that
# straddles the end is expected, but only its sessions inside [start, end) are counted. The list, the detail and the
# coverage strip read the same count. [Mon 3 Aug, Thu 1 Oct): the week of 28 Sep has three sessions in range (28, 29,
# 30 Sep); 1 and 2 Oct are outside. Before the fix the list and the detail read 5 here and the strip's buckets 3.
MID_WEEK_END = ny_midnight(1)
MID_WEEK_CASES = {
    'the straddling week missing': (date(2026, 9, 28), date(2026, 9, 28), date(2026, 9, 30), 3),
    'an interior week missing': (date(2026, 8, 31), date(2026, 8, 31), date(2026, 9, 4), 5),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('dropped', 'first', 'last', 'count'), MID_WEEK_CASES.values(), ids=MID_WEEK_CASES)
async def test_a_weekly_dataset_ending_mid_week_reads_one_count_on_list_detail_and_strip(
    client, engine, covered_dates_as_dates, dropped, first, last, count
):
    dataset = entry('WKMD', granularity=Granularity.ONE_WEEK, start=midnight(date(2026, 8, 3)), end=MID_WEEK_END)
    await store(engine, dataset, *daily_bars(dataset, [day for day in WEEKLY_BARS if day != dropped]))

    detail = (await client.get(f'/ui/v1/datasets/{dataset.id}')).json()['freshness']
    listed = {item['id']: item for item in await list_all(client)}[str(dataset.id)]['freshness']
    for freshness in (detail, listed):
        assert (freshness['status'], freshness['gapCount']) == ('FRESHNESS_STATUS_GAPS', count), freshness
    strip = await coverage(client, dataset)
    assert strip.gaps == [window(first, last)] and strip.gaps[0][2] == count, strip.gaps
    assert (strip.gap_count, strip.missing) == (1, count), strip
    # 44 sessions Mon 3 Aug..Fri 2 Oct, less Thu 1 and Fri 2 Oct.
    assert strip.expected == 42 == len(XNYS.sessions_overlapping(dataset.start, dataset.end))
    # No window reaches past the range end.
    assert all(gap_end <= MID_WEEK_END for _, gap_end, _ in strip.gaps), strip.gaps
    assert_tiles(strip)


@pytest.mark.asyncio
async def test_a_complete_weekly_dataset_ending_mid_week_reads_complete(client, engine, covered_dates_as_dates):
    dataset = entry('WKMC', granularity=Granularity.ONE_WEEK, start=midnight(date(2026, 8, 3)), end=MID_WEEK_END)
    await store(engine, dataset, *daily_bars(dataset, WEEKLY_BARS))

    detail = (await client.get(f'/ui/v1/datasets/{dataset.id}')).json()['freshness']
    assert detail['status'] == 'FRESHNESS_STATUS_COMPLETE' and 'gapCount' not in detail, detail
    listed = {item['id']: item for item in await list_all(client)}
    assert status_of(listed[str(dataset.id)]) == 'FRESHNESS_STATUS_COMPLETE'
    strip = await coverage(client, dataset)
    assert (strip.gaps, strip.gap_count, strip.missing, strip.expected) == ([], 0, 0, 42), strip
