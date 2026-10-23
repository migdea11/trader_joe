"""The /ui/v1/stats routes on the SQLite tier: what the PR gate can prove without Postgres (validator, tj-grna9p.64).

Design: tj-grna9p.64, the architect's 2026-10-14 CONSOLIDATED BUILD BRIEF (it wins over the body). What is here:
  * the UTC guard: every stats read refuses a naive now before any statement;
  * read_within and utc_today, the ONE day-aligned rule: the summary, the stale list, the list's usage filter and
    the facets all decide through the module's function, so a change to it moves every one of them;
  * estimate_bytes: the split of the table's bytes, summing to the total within one byte per part;
  * every route: an unknown parameter is a 422 naming it, and the bounds (stale_days 1..3650, days 1..365, limit
    1..500, the cursor's length) are inclusive; storage takes no client filter;
  * the echo of the client filter on the four usage routes, the zero view and its one warning on a database with
    no usage tables, no route recording a read, and a label never reaching a log line or an error body;
  * the stale list's paging and its cursor's binding to stale_days, source and the client filter;
  * storage's estimates over the route tier's bars.

WHAT IS NOT HERE. Every figure that depends on a read (stale vs read, the buckets, reads per day, the rollup, the
last read per client, the statement budget with usage read) runs only on Postgres: the SQLite tier has no usage
tables, so every read below sees the zero view. That arithmetic is pinned in tests/system/test_http_ui_stats.py.

SQLITE HAS NO pg_total_relation_size. The `sized` fixture registers a stand-in on the tier's one connection that
answers STORAGE_BYTES for any relation, so storage and the stale list's estimates have a known total.
"""

import base64
import json
import logging
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from common.enums.data_stock import DataSource
from data.store.app import dataset_catalog as catalog_module
from data.store.app import usage_recorder as recorder_module
from data.store.app.database.crud import dataset_read
from data.store.app.dataset_catalog import ClientFilter, UsageFilter, estimate_bytes, read_within, utc_today
from data.store.app.main import app
from data.store.tests.problem_body import validation_errors

# The route tier's fixtures, reused: pytest finds a fixture by the module attribute's name, so they are imported
# under their own names, and each test taking one carries `noqa: F811` for ruff's view of that parameter.
from data.store.tests.test_ui_datasets_route import NOW, aware_sqlite_datetimes, catalog, client, engine  # noqa: F401
from routers.data_store.app_endpoints import UiDatasetsInterface, UiStatsInterface


pytestmark = pytest.mark.data_store

SDK, UI = 'CLIENT_KIND_SDK', 'CLIENT_KIND_UI'
SUMMARY = UiStatsInterface.GET_UI_USAGE_SUMMARY.value
BUCKETS = UiStatsInterface.GET_UI_USAGE_LAST_READ_BUCKETS.value
PER_DAY = UiStatsInterface.GET_UI_USAGE_READS_PER_DAY.value
STALE = UiStatsInterface.GET_UI_USAGE_STALE.value
STORAGE = UiStatsInterface.GET_UI_STORAGE_CURRENT.value
USAGE_ROUTES = (SUMMARY, BUCKETS, PER_DAY, STALE)
ROUTES = (*USAGE_ROUTES, STORAGE)

# The route tier's catalog holds 12 datasets and 5 bars: aaa_day 2, bbb 1 and ccc 1 (ALPACA, IEX), ggg 1 (IB, IEX).
CATALOG_SIZE = 12
STORAGE_BYTES = 1_000_003


@pytest_asyncio.fixture
async def sized(engine: AsyncEngine) -> None:  # noqa: F811
    """pg_total_relation_size on the tier's single (StaticPool) connection: STORAGE_BYTES for any relation."""
    async with engine.connect() as connection:
        raw = await connection.get_raw_connection()
        await raw.driver_connection.create_function(
            'pg_total_relation_size', 1, lambda name: None if name is None else STORAGE_BYTES
        )


async def ok(http: httpx.AsyncClient, path: str, params: Any = ()) -> dict:
    response = await http.get(path, params=params)
    assert response.status_code == 200, (path, response.text)
    return response.json()


# ------------------------------------------------------------------------------------------------- the UTC guard


class _Untouchable:
    """A session that fails the test if a statement is sent: the guard must refuse before any SQL."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError('a statement was sent for a naive now')


NAIVE = datetime(2026, 10, 6, 22, 0)
DEFAULT_FILTER = ClientFilter()
GUARDED = {
    'usage_summary': lambda db: catalog_module.usage_summary(db, NAIVE, client_filter=DEFAULT_FILTER),
    'last_read_buckets': lambda db: catalog_module.last_read_buckets(db, NAIVE, client_filter=DEFAULT_FILTER),
    'reads_per_day': lambda db: catalog_module.reads_per_day(db, NAIVE, client_filter=DEFAULT_FILTER),
    'reads_per_day by source': lambda db: catalog_module.reads_per_day(
        db, NAIVE, client_filter=DEFAULT_FILTER, source=DataSource.IB_API
    ),
    'stale_page': lambda db: catalog_module.stale_page(db, NAIVE, client_filter=DEFAULT_FILTER),
    'storage_current': lambda db: catalog_module.storage_current(db, NAIVE),
    'read_client_day_reads': lambda db: dataset_read.read_client_day_reads(db, None, now=NAIVE, days=30),
}


@pytest.mark.asyncio
@pytest.mark.parametrize('call', GUARDED.values(), ids=GUARDED)
async def test_every_stats_read_refuses_a_naive_now_before_any_statement(call):
    with pytest.raises(ValueError, match='timezone-aware'):
        await call(_Untouchable())


# ------------------------------------------------------------------------------------------------- the one rule


def test_utc_today_is_the_utc_date_whatever_zone_now_carries_and_refuses_a_naive_now():
    for zone in ('Pacific/Kiritimati', 'America/New_York', 'UTC'):
        assert utc_today(NOW.astimezone(ZoneInfo(zone))) == NOW.date(), zone
    with pytest.raises(ValueError, match='timezone-aware'):
        utc_today(NAIVE)


def test_read_within_is_day_aligned_on_utc_dates():
    """NOW is 22:00Z on 6 Oct: within 30 days is a last read DATED 6 Sep or later, at any time of that day."""
    first_day = datetime(2026, 9, 6, tzinfo=UTC)
    assert read_within(first_day, NOW, 30) and read_within(first_day + timedelta(hours=23, minutes=59), NOW, 30)
    assert not read_within(first_day - timedelta(microseconds=1), NOW, 30)
    assert read_within(NOW.replace(hour=0), NOW, 0) and not read_within(NOW - timedelta(days=1), NOW, 0)
    assert not read_within(None, NOW, 3650), 'no read is within no window'
    # The read's own zone does not matter: 23:30 in New York on 5 Sep is 03:30Z on 6 Sep, inside.
    assert read_within(datetime(2026, 9, 5, 23, 30, tzinfo=ZoneInfo('America/New_York')), NOW, 30)
    with pytest.raises(ValueError, match='timezone-aware'):
        read_within(first_day, NAIVE, 30)


@pytest.mark.asyncio
async def test_the_summary_the_stale_list_the_list_and_the_facets_all_decide_through_the_one_read_within(
    client,  # noqa: F811
    catalog,  # noqa: F811
    sized,
    monkeypatch,
):
    """Replace the module's read_within with 'everything was read': a copy of the rule anywhere would not follow.

    On this tier nothing was ever read, so with the real rule every dataset is stale; with the replacement none is.
    """
    before = await ok(client, SUMMARY)
    assert (before['stale'], before.get('readLast30d', '0')) == (str(CATALOG_SIZE), '0')
    monkeypatch.setattr(catalog_module, 'read_within', lambda last_read_at, now, days: True)

    summary = await ok(client, SUMMARY)
    assert (summary.get('stale', '0'), summary['readLast30d']) == ('0', str(CATALOG_SIZE)), summary
    assert (await ok(client, STALE)).get('items', []) == [], 'the stale list did not follow read_within'
    facets = await ok(client, UiDatasetsInterface.GET_UI_DATASET_FACETS.value)
    assert (facets.get('stale', '0'), facets['read30d']) == ('0', str(CATALOG_SIZE)), 'the facets did not follow'
    listed = await ok(client, UiDatasetsInterface.GET_UI_DATASETS.value, {'usage': 'STALE', 'limit': 500})
    assert listed.get('items', []) == [], 'the list did not follow'


def test_the_predicate_s_groups_still_partition_through_read_within():
    """READ_30D and STALE at 30 are complements; NEVER_READ is no last read."""
    row = type('Row', (), {'id': uuid.uuid4()})()
    for back in range(0, 40):
        last = NOW - timedelta(days=back)
        usage = {row.id: dataset_read.DatasetUsage(row.id, last, 1)}
        read = catalog_module._usage_predicate(UsageFilter.READ_30D, usage, NOW, 90)(row)
        stale = catalog_module._usage_predicate(UsageFilter.STALE, usage, NOW, 30)(row)
        assert read is not stale and read is read_within(last, NOW, 30), back


# ------------------------------------------------------------------------------------------------- estimate_bytes


def test_estimate_bytes_is_the_bar_count_times_the_average_row_size_floored():
    assert estimate_bytes(2, 1_000_003, 5) == 400_001
    assert estimate_bytes(1, 1_000_003, 5) == 200_000
    assert estimate_bytes(5, 1_000_003, 5) == 1_000_003, 'the whole table is the total'
    assert estimate_bytes(0, 1_000_003, 5) == 0
    assert estimate_bytes(3, 8192, 0) == 0 and estimate_bytes(0, 8192, 0) == 0, 'no bars: no average, no estimate'


def test_a_split_sums_to_the_total_within_one_byte_per_part():
    rng = random.Random(64)
    for _ in range(500):
        counts = [rng.randint(0, 10_000_000) for _ in range(rng.randint(1, 6))]
        total_bars = sum(counts)
        total_bytes = rng.randint(0, 50 * 2**30)
        parts = [estimate_bytes(count, total_bytes, total_bars) for count in counts]
        if total_bars == 0:
            assert parts == [0] * len(parts)
            continue
        assert 0 <= total_bytes - sum(parts) < len(parts), (counts, total_bytes, parts)


# ------------------------------------------------------------------------------------------------- parameters


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ROUTES)
@pytest.mark.parametrize('field', ['reader', 'owner', 'months', 'usage', 'stale_after_days'])
async def test_every_route_refuses_an_unknown_parameter_with_a_422_naming_it(client, catalog, path, field):  # noqa: F811
    response = await client.get(path, params={field: '1'})
    assert response.status_code == 422, (path, response.text)
    assert any(field in error['loc'] for error in validation_errors(response)), response.text


NOT_SERVED = {
    'quota burn': ('/ui/v1/stats/quota/burn', {'minutes': 60, 'quota_key': 'alpaca-a'}),
    'quota burn, no query': ('/ui/v1/stats/quota/burn', {}),
    'storage growth': ('/ui/v1/stats/storage/growth', {'months': 12}),
    'storage history': ('/ui/v1/stats/storage', {'months': 12}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('path', 'params'), NOT_SERVED.values(), ids=NOT_SERVED)
async def test_no_quota_burn_or_storage_growth_route_is_served(client, path, params):  # noqa: F811
    """tj-utqd77 Q2 (quota burn moved to tj-lenn8u) and .64's 'no growth route': a request is a plain 404.

    The QuotaBurn and StorageGrowth messages' absence is pinned in server/common/tests/test_proto_ui_phase_three.py.
    """
    response = await client.get(path, params=params)
    assert response.status_code == 404, (path, response.text)
    served = app.openapi()['paths']
    assert '/ui/v1/stats/storage/current' in served, 'the path listing is not the one the routes are in'
    assert not [path for path in served if any(word in path for word in ('quota', 'burn', 'growth'))]


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['client_id', 'client_kind'])
async def test_storage_takes_no_client_filter(client, catalog, sized, field):  # noqa: F811
    """Interpretation (1), as built: what the store holds is not a function of who read it."""
    response = await client.get(STORAGE, params={field: 'SDK'})
    assert response.status_code == 422 and any(field in error['loc'] for error in validation_errors(response))


BOUNDS = {
    'summary stale_days': (SUMMARY, 'stale_days', ('0', '3651', '-1', 'ninety', '1.5'), ('1', '3650')),
    'stale stale_days': (STALE, 'stale_days', ('0', '3651'), ('1', '3650')),
    'reads per day days': (PER_DAY, 'days', ('0', '366', '-30', 'week'), ('1', '365')),
    'stale limit': (STALE, 'limit', ('0', '501', 'all'), ('1', '500')),
    'stale cursor length': (STALE, 'cursor', ('', 'c' * 513), ()),
    'reads per day dataset_id': (PER_DAY, 'dataset_id', ('not-a-uuid',), (str(uuid.uuid4()),)),
    **{
        f'{path} source': (path, 'source', ('NYSE', ''), ('IB_API', 'data_source_alpaca_api', 'MANUAL_ENTRY'))
        for path in ROUTES
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('path', 'field', 'refused', 'accepted'), BOUNDS.values(), ids=BOUNDS)
async def test_the_bounds_are_inclusive_and_a_value_outside_is_a_422_naming_it(
    client,  # noqa: F811
    catalog,  # noqa: F811
    sized,
    path,
    field,
    refused,
    accepted,
):
    for value in refused:
        response = await client.get(path, params={field: value})
        assert response.status_code == 422, (field, value, response.text)
        assert any(field in error['loc'] for error in validation_errors(response)), response.text
    for value in accepted:
        assert (await client.get(path, params={field: value})).status_code == 200, (field, value)


@pytest.mark.asyncio
@pytest.mark.parametrize('path', USAGE_ROUTES)
async def test_a_bad_client_parameter_is_a_422_that_never_echoes_the_label(client, catalog, sized, path):  # noqa: F811
    label = 'secret-label-' + 'x' * 120
    long = await client.get(path, params={'client_id': label})
    assert long.status_code == 422 and 'client_id' in long.text and label not in long.text, long.text
    kind = await client.get(path, params={'client_kind': 'robot-secret-kind'})
    assert kind.status_code == 422 and 'robot-secret-kind' not in kind.text, kind.text
    empty = await client.get(path, params={'client_id': ''})
    assert empty.status_code == 422
    too_many = await client.get(path, params=[('client_id', f'c{index}') for index in range(33)])
    assert too_many.status_code == 422, too_many.text
    assert (await client.get(path, params=[('client_id', f'c{index}') for index in range(32)])).status_code == 200


# ------------------------------------------------------------------------------------------------- the echo


@pytest.mark.asyncio
@pytest.mark.parametrize('path', USAGE_ROUTES)
async def test_every_usage_route_echoes_the_client_filter_by_the_echo_rule(client, catalog, sized, path):  # noqa: F811
    def echo(body: dict) -> tuple:
        return body.get('clientIds'), body.get('clientKinds')

    assert echo(await ok(client, path)) == (None, [SDK]), 'the default is SDK'
    assert echo(await ok(client, path, {'client_kind': 'ui'})) == (None, [UI])
    kinds = [('client_kind', 'SDK'), ('client_kind', 'CLIENT_KIND_UI'), ('client_kind', 'sdk')]
    assert echo(await ok(client, path, kinds)) == (None, [UI, SDK]), 'once each, in the enum order'
    wins = [('client_kind', 'UI'), ('client_id', 'b'), ('client_id', 'a'), ('client_id', 'b')]
    assert echo(await ok(client, path, wins)) == (['b', 'a'], None), 'client_id wins, sent order, no repeats'


@pytest.mark.asyncio
async def test_storage_carries_no_echo_and_stale_days_is_echoed_where_it_is_taken(client, catalog, sized):  # noqa: F811
    storage = await ok(client, STORAGE)
    assert set(storage) == {'totalBytes', 'asOf', 'bySource'}, storage
    assert storage['asOf'] == '2026-10-06T22:00:00Z'
    for path in (SUMMARY, STALE):
        assert (await ok(client, path))['staleDays'] == 90
        assert (await ok(client, path, {'stale_days': 7}))['staleDays'] == 7


# ------------------------------------------------------------------------------------------------- the zero view


@pytest.mark.asyncio
async def test_with_no_usage_tables_every_route_reads_the_zero_view_with_exactly_one_warning(
    client,  # noqa: F811
    catalog,  # noqa: F811
    sized,
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(catalog_module, '_warned_usage_missing', False)
    caplog.set_level(logging.DEBUG, logger=catalog_module.log.name)
    total = str(CATALOG_SIZE)

    summary = await ok(client, SUMMARY)
    assert summary == {'stale': total, 'total': total, 'neverRead': total, 'clientKinds': [SDK], 'staleDays': 90}, (
        'read_last_30d, reads_today and distinct_clients_today are zero (absent)'
    )
    buckets = await ok(client, BUCKETS)
    assert buckets == {
        'buckets': [
            {'maxDays': 1},
            {'minDays': 1, 'maxDays': 8},
            {'minDays': 8, 'maxDays': 31},
            {'minDays': 31, 'maxDays': 91},
            {'minDays': 91},
        ],
        'neverRead': total,
        'clientKinds': [SDK],
    }, 'every bucket present, the last open, zeros omitted'
    assert await ok(client, PER_DAY) == {'clientKinds': [SDK]}
    assert await ok(client, PER_DAY, {'dataset_id': str(catalog.aaa_day.id), 'days': 365}) == {'clientKinds': [SDK]}
    stale = await ok(client, STALE, {'limit': 500})
    assert len(stale['items']) == CATALOG_SIZE and 'nextCursor' not in stale
    assert all('lastReadByClient' not in item for item in stale['items'])
    assert all(item['dataset']['usage'] == {'clientKinds': [SDK]} for item in stale['items'])
    assert (await ok(client, STORAGE))['totalBytes'] == str(STORAGE_BYTES), 'storage does not need the usage tables'

    warnings = [r for r in caplog.records if r.name == catalog_module.log.name and r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and 'make migrate' in warnings[0].getMessage(), [r.getMessage() for r in warnings]


@pytest.mark.asyncio
async def test_a_source_narrows_the_summary_the_buckets_and_the_stale_list_alike(client, catalog, sized):  # noqa: F811
    for source, count in (('IB_API', 1), ('ALPACA_API', CATALOG_SIZE - 1), ('MANUAL_ENTRY', 0)):
        summary = await ok(client, SUMMARY, {'source': source})
        assert int(summary.get('total', '0')) == int(summary.get('stale', '0')) == count, source
        assert int((await ok(client, BUCKETS, {'source': source})).get('neverRead', '0')) == count, source
        assert len((await ok(client, STALE, {'source': source, 'limit': 500})).get('items', [])) == count, source
        facets = await ok(client, UiDatasetsInterface.GET_UI_DATASET_FACETS.value, {'source': source})
        assert int(facets.get('stale', '0')) == count, 'the Stale facet agrees'


# ------------------------------------------------------------------------------------------------- no recording


@pytest.mark.asyncio
async def test_no_stats_route_records_a_read_and_no_label_reaches_a_log_line(
    client,  # noqa: F811
    catalog,  # noqa: F811
    sized,
    monkeypatch,
    caplog,
):
    recorded: list[Any] = []
    monkeypatch.setattr(recorder_module.UsageRecorder, 'record_read', lambda self, *a, **k: recorded.append(a))
    monkeypatch.setattr(recorder_module.UsageRecorder, '_append', lambda self, *a, **k: recorded.append(a))
    caplog.set_level(logging.DEBUG)
    label = 'secret-client-label-0064'
    for path in ROUTES:
        params = {} if path == STORAGE else {'client_id': label}
        await ok(client, path, params)
        await ok(client, path, params | {'source': 'IB_API'})
    await ok(client, PER_DAY, {'client_id': label, 'dataset_id': str(catalog.ggg.id)})
    cursor = (await ok(client, STALE, {'client_id': label, 'limit': 2}))['nextCursor']
    await ok(client, STALE, {'client_id': label, 'limit': 2, 'cursor': cursor})
    assert recorded == [], 'a stats route recorded a read'
    ours = [r.getMessage() for r in caplog.records if r.name.startswith(('data.', 'common.', 'routers.'))]
    assert all(label not in line for line in ours), [line for line in ours if label in line]


# ------------------------------------------------------------------------------------------------- stale paging


async def stale_all(http: httpx.AsyncClient, params: list[tuple[str, Any]]) -> list[dict]:
    """Every stale row under params, following next_cursor; asserts nothing is served twice."""
    items: list[dict] = []
    cursor = None
    for _ in range(100):
        body = await ok(http, STALE, [*params, *([('cursor', cursor)] if cursor else [])])
        items.extend(body.get('items', []))
        cursor = body.get('nextCursor')
        if not cursor:
            break
    ids = [item['dataset']['id'] for item in items]
    assert len(ids) == len(set(ids)), f'a row was served twice: {ids}'
    return items


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [1, 5, 11, 12, 500])
async def test_the_stale_list_pages_never_read_by_id_without_loss(client, catalog, sized, limit):  # noqa: F811
    items = await stale_all(client, [('limit', limit)])
    assert [item['dataset']['id'] for item in items] == sorted(str(row.id) for row in catalog.rows)


@pytest.mark.asyncio
async def test_each_stale_row_carries_its_estimate_and_the_dataset_summary(client, catalog, sized):  # noqa: F811
    rows = {item['dataset']['id']: item for item in await stale_all(client, [('limit', 500)])}
    # 5 bars in the table; the average row is STORAGE_BYTES / 5.
    assert rows[str(catalog.aaa_day.id)]['estBytes'] == str(2 * STORAGE_BYTES // 5)
    for single in (catalog.bbb, catalog.ccc, catalog.ggg):
        assert rows[str(single.id)]['estBytes'] == str(STORAGE_BYTES // 5)
    assert 'estBytes' not in rows[str(catalog.abx.id)], 'no bars: an estimate of zero'
    detail = await ok(client, f'{UiDatasetsInterface.GET_UI_DATASETS.value}/{catalog.aaa_day.id}')
    assert rows[str(catalog.aaa_day.id)]['dataset'] == detail, 'the row is the DatasetSummary the detail serves'


def _decoded(cursor: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))


CURSOR_FILTER = [('client_id', 'secret-reader-one'), ('client_id', 'secret-reader-two'), ('stale_days', 30)]
REBOUND = {
    'another stale_days': [('client_id', 'secret-reader-one'), ('client_id', 'secret-reader-two'), ('stale_days', 31)],
    'the default stale_days': [('client_id', 'secret-reader-one'), ('client_id', 'secret-reader-two')],
    'another client id': [('client_id', 'secret-reader-one'), ('stale_days', 30)],
    'a kind instead of ids': [('client_kind', 'SDK'), ('stale_days', 30)],
    'a source added': [*CURSOR_FILTER, ('source', 'ALPACA_API')],
}


@pytest.mark.asyncio
@pytest.mark.parametrize('other', REBOUND.values(), ids=REBOUND)
async def test_a_stale_cursor_is_bound_to_stale_days_source_and_the_client_filter(client, catalog, sized, other):  # noqa: F811
    cursor = (await ok(client, STALE, [*CURSOR_FILTER, ('limit', 2)]))['nextCursor']
    refused = await client.get(STALE, params=[*other, ('limit', 2), ('cursor', cursor)])
    assert refused.status_code == 422, refused.text
    assert cursor not in refused.text and 'secret-reader' not in refused.text


@pytest.mark.asyncio
async def test_the_same_filter_in_another_order_continues_and_the_cursor_carries_no_label(client, catalog, sized):  # noqa: F811
    first = await ok(client, STALE, [*CURSOR_FILTER, ('limit', 5)])
    cursor = first['nextCursor']
    assert 'secret-reader' not in json.dumps(_decoded(cursor)), 'a client id reached the cursor'
    reordered = [('stale_days', 30), ('client_id', 'secret-reader-two'), ('client_id', 'secret-reader-one')]
    second = await ok(client, STALE, [*reordered, ('limit', 5), ('cursor', cursor)])
    assert second['clientIds'] == ['secret-reader-two', 'secret-reader-one'], 'the echo is the sent order'
    ids = [item['dataset']['id'] for item in first['items'] + second['items']]
    assert ids == sorted(str(row.id) for row in catalog.rows)[:10]
    kinds = await ok(client, STALE, [('client_kind', 'UI'), ('client_kind', 'SDK'), ('limit', 5)])
    swapped = [('client_kind', 'SDK'), ('client_kind', 'UI'), ('limit', 5), ('cursor', kinds['nextCursor'])]
    assert (await client.get(STALE, params=swapped)).status_code == 200, 'the same kinds in another order'


@pytest.mark.asyncio
async def test_list_and_stale_cursors_are_not_interchangeable_and_a_forged_one_is_a_422(client, catalog, sized):  # noqa: F811
    list_cursor = (await ok(client, UiDatasetsInterface.GET_UI_DATASETS.value, {'limit': 2}))['nextCursor']
    stale_cursor = (await ok(client, STALE, {'limit': 2}))['nextCursor']
    assert (await client.get(STALE, params={'cursor': list_cursor})).status_code == 422
    listed = await client.get(UiDatasetsInterface.GET_UI_DATASETS.value, params={'cursor': stale_cursor})
    assert listed.status_code == 422
    payload = _decoded(stale_cursor)
    for forged in (
        {**payload, 'k': [0, 0]},
        {**payload, 'k': [True, 0, payload['k'][2]]},
        {**payload, 'k': [0, 0, 'not-a-uuid']},
        {**payload, 'f': '0' * 16},
        {**payload, 'o': 'symbol'},
        'not base64 at all',
    ):
        text = forged if isinstance(forged, str) else base64.urlsafe_b64encode(json.dumps(forged).encode()).decode()
        response = await client.get(STALE, params={'cursor': text})
        assert response.status_code == 422, (forged, response.text)


# ------------------------------------------------------------------------------------------------- storage


@pytest.mark.asyncio
async def test_storage_splits_the_measured_total_by_source_and_feed(client, catalog, sized):  # noqa: F811
    body = await ok(client, STORAGE)
    assert body['totalBytes'] == str(STORAGE_BYTES)
    assert body['bySource'] == [
        {'source': 'DATA_SOURCE_IB_API', 'feed': 'FEED_IEX', 'estBytes': str(STORAGE_BYTES // 5)},
        {'source': 'DATA_SOURCE_ALPACA_API', 'feed': 'FEED_IEX', 'estBytes': str(4 * STORAGE_BYTES // 5)},
    ] or body['bySource'] == [
        {'source': 'DATA_SOURCE_ALPACA_API', 'feed': 'FEED_IEX', 'estBytes': str(4 * STORAGE_BYTES // 5)},
        {'source': 'DATA_SOURCE_IB_API', 'feed': 'FEED_IEX', 'estBytes': str(STORAGE_BYTES // 5)},
    ], body
    parts = sum(int(share['estBytes']) for share in body['bySource'])
    assert 0 <= STORAGE_BYTES - parts < len(body['bySource'])


@pytest.mark.asyncio
async def test_storage_with_a_source_totals_that_source_s_estimates(client, catalog, sized):  # noqa: F811
    """Interpretation (2), as built: with a source, total_bytes is the sum of its shares, not the table's size."""
    ib = await ok(client, STORAGE, {'source': 'IB_API'})
    assert ib['totalBytes'] == str(STORAGE_BYTES // 5)
    assert [share['source'] for share in ib['bySource']] == ['DATA_SOURCE_IB_API']
    alpaca = await ok(client, STORAGE, {'source': 'ALPACA_API'})
    assert alpaca['totalBytes'] == str(4 * STORAGE_BYTES // 5)
    manual = await ok(client, STORAGE, {'source': 'MANUAL_ENTRY'})
    assert 'totalBytes' not in manual and 'bySource' not in manual, 'a source holding nothing is zero'


@pytest.mark.asyncio
async def test_storage_of_an_empty_table_has_no_shares(client, sized):  # noqa: F811
    body = await ok(client, STORAGE)
    assert body['totalBytes'] == str(STORAGE_BYTES) and 'bySource' not in body, 'no bars: nothing to split'
    assert await ok(client, STALE) == {'clientKinds': [SDK], 'staleDays': 90}
