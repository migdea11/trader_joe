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
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.dialects.sqlite import DATETIME
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from common.database.sql_alchemy_nullable_datetime import NullableDateTime
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app import dataset_catalog as dataset_catalog_module
from data.store.app.database.crud.stock import dataset_catalog as catalog_repo
from data.store.app.database.crud.stock.dataset_catalog import CatalogFilter
from data.store.app.database.database import async_db
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.dataset_catalog import evaluate_views
from data.store.app.freshness import FreshnessStatus, NaiveDatetimeError, UnknownCalendarError
from data.store.app.main import app
from data.store.tests.problem_body import problem, validation_errors
from routers.data_store.ui_datasets import utc_now


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
                sync, tables=[StoreDatasetEntry.__table__, StockMarketActivity.__table__]
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


async def store(engine: AsyncEngine, *rows: Any) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as db:
        db.add_all([row for row in rows if isinstance(row, StoreDatasetEntry)])
        await db.flush()
        db.add_all([row for row in rows if isinstance(row, StockMarketActivity)])
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
async def test_the_default_bar_page_is_1000_and_10000_is_the_most(client, engine):
    dataset = entry('MANY', granularity=Granularity.ONE_MINUTE, source=DataSource.IB_API)
    start = z('2026-10-01T00:00:00Z')
    await store(engine, dataset, *(bar(dataset, start + timedelta(minutes=minute)) for minute in range(1001)))
    url = f'/ui/v1/datasets/{dataset.id}/bars'
    default = (await client.get(url)).json()
    assert (len(default['bars']), bool(default.get('nextCursor'))) == (1000, True)
    assert len((await client.get(url, params={'limit': 10_000})).json()['bars']) == 1001


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
    'list unknown parameter': ('/ui/v1/datasets', {'stale_days': 3}, 'stale_days'),
    'facets ignores no unknown parameter': ('/ui/v1/datasets/facets', {'limit': 5}, 'limit'),
    'bars limit 0': ('/ui/v1/datasets/{id}/bars', {'limit': 0}, 'limit'),
    'bars limit 10001': ('/ui/v1/datasets/{id}/bars', {'limit': 10_001}, 'limit'),
    'bars naive start': ('/ui/v1/datasets/{id}/bars', {'start': '2026-10-05T14:00:00'}, 'start'),
    'bars naive end': ('/ui/v1/datasets/{id}/bars', {'end': '2026-10-05T14:00:00'}, 'end'),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('path', 'params', 'field'), LIMIT_CASES.values(), ids=LIMIT_CASES)
async def test_a_bad_parameter_is_a_422_naming_it(client, bars, path, params, field):
    response = await client.get(path.replace('{id}', str(bars.dataset.id)), params=params)
    assert field in [error['loc'][-1] for error in validation_errors(response)]


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
    """tj-grna9p.8 item 2: one statement for the entries, then one each for extents, covered dates and siblings."""
    statements: list[str] = []
    event.listen(engine.sync_engine, 'before_cursor_execute', lambda *args: statements.append(args[2]))

    async def count(path: str, **params: Any) -> int:
        statements.clear()
        assert (await client.get(path, params=params)).status_code == 200
        return len(statements)

    small = {
        'list': await count('/ui/v1/datasets', limit=500),
        'list filtered by status': await count('/ui/v1/datasets', limit=500, status='healthy'),
        'facets': await count('/ui/v1/datasets/facets'),
        'detail': await count(f'/ui/v1/datasets/{catalog.ddd.id}'),
    }
    await store(engine, *(entry(f'X{index:03d}', **WEEKEND) for index in range(40)))
    large = {
        'list': await count('/ui/v1/datasets', limit=500),
        'list filtered by status': await count('/ui/v1/datasets', limit=500, status='healthy'),
        'facets': await count('/ui/v1/datasets/facets'),
        'detail': await count(f'/ui/v1/datasets/{catalog.ddd.id}'),
    }
    assert small == large, f'the statement count grew with the number of datasets: {small} -> {large}'
    assert max(large.values()) <= 4, large


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
