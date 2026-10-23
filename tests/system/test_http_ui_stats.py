"""The /ui/v1/stats usage and storage reads against a real Postgres (validator, gating tj-grna9p.64).

Design: tj-grna9p.64, the architect's 2026-10-14 CONSOLIDATED BUILD BRIEF (it wins over the body), and the closed
messages of proto/trader_joe/proto/ui/v1/stats.proto. The acceptance, each pinned below on the real routes:
  * the summary's stale, the stale list's total and the Stale facet AGREE for the same filter and stale_days, and
    read_last_30d and never_read equal the facets' read_30d and never_read;
  * the last-read buckets ([0,1) [1,8) [8,31) [31,91) [91,open) whole UTC days, plus never_read) sum to the total;
  * reads per day is one entry per (UTC day, client id, kind), and is CONTINUOUS across the rollup: folding the
    per-read rows into dataset_read_daily (the real fold, cut mid-day) changes no figure of any route;
  * client_id wins over client_kind, and a UI-only dataset is stale under SDK;
  * storage's split by (source, feed) sums to the measured pg_total_relation_size within one byte per part, and each
    stale row's est_bytes is its bar count times the average row size;
  * a request runs a constant number of statements whatever the store holds;
  * an unmigrated database reads as the zero view with one warning.

HOW. The REAL app in process (httpx's ASGI transport) on a scratch database this test creates, migrates and drops
(test_usage_record.scratch_db, reused), with the clock pinned through the routes' own utc_now dependency. Seeded
reads are written straight to dataset_read and dataset_read_daily, and in the rollup test folded by the store's own
fold_reads_before. Run by make test-system and the agent-stack MCP's run_system_tests. FAIL, NEVER SKIP.
"""

import logging
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from common.enums.data_stock import DataSource, Feed
from data.store.app import dataset_catalog as catalog_module
from data.store.app.client_label import OPERATOR_PRINCIPAL
from routers.data_store.app_endpoints import UiDatasetsInterface, UiStatsInterface
from tests.system import test_http_ui_dataset_usage as usage_tests
from tests.system import test_usage_record
from tests.system.conftest import BAR_TABLE, ENTRY_TABLE


pytestmark = pytest.mark.data_store

# Reused, not copied: the scratch database, its asyncpg engine, the in-process client pinned at NOW.
scratch_db = test_usage_record.scratch_db
scratch_async = test_usage_record.scratch_async
client = usage_tests.client
Statements = usage_tests.Statements
_head = test_usage_record._head
READ_TABLE = test_usage_record.READ_TABLE
DAILY_TABLE = test_usage_record.DAILY_TABLE
PRE_USAGE_REVISION = test_usage_record.PARENT_REVISION
NOW = usage_tests.NOW  # 2026-10-06T22:00Z
TODAY = NOW.date()

SUMMARY = UiStatsInterface.GET_UI_USAGE_SUMMARY.value
BUCKETS = UiStatsInterface.GET_UI_USAGE_LAST_READ_BUCKETS.value
PER_DAY = UiStatsInterface.GET_UI_USAGE_READS_PER_DAY.value
STALE = UiStatsInterface.GET_UI_USAGE_STALE.value
STORAGE = UiStatsInterface.GET_UI_STORAGE_CURRENT.value
LIST = UiDatasetsInterface.GET_UI_DATASETS.value
FACETS = UiDatasetsInterface.GET_UI_DATASET_FACETS.value

SDK, UI = 'CLIENT_KIND_SDK', 'CLIENT_KIND_UI'
A, B, C = 'client-a', 'client-b', 'client-c'
KIND_OF = {A: 'SDK', B: 'SDK', C: 'SDK', OPERATOR_PRINCIPAL: 'UI'}


def day_start(back: int) -> datetime:
    """00:00:00Z of the UTC day `back` days before today."""
    return datetime.combine(TODAY - timedelta(days=back), datetime.min.time(), tzinfo=UTC)


def last_second(back: int) -> datetime:
    return day_start(back - 1) - timedelta(seconds=1)


# ---------------------------------------------------------------------------------------------
# The world: one dataset per edge of the buckets, the 30-day window and the 90-day stale threshold, raw reads and
# rolled-up days, two sources and two feeds of bars.
#
#   name      source/feed      reads (client, at)                                       whole UTC days since
#   NOWREAD   ALPACA/IEX       a now-1h; b twice today 00:00:00 and 00:00:01           0
#   D1        ALPACA/IEX       b at 23:59:59 yesterday                                  1
#   D7        ALPACA/IEX       a at 00:00 seven days ago                                7
#   D8        ALPACA/IEX       a at 00:00 eight days ago                                8
#   D30       ALPACA/IEX       c at 00:00 thirty days ago (in read_last_30d)            30
#   D31       ALPACA/IEX       c at 23:59:59 thirty-one days ago (out)                  31
#   D90       ALPACA/IEX       b at 23:59:59 ninety days ago, raw (not stale at 90)     90
#   D91       ALPACA/IEX       b ninety-one days ago, ROLLED (stale at 90)              91
#   R120      ALPACA/IEX       a 120 days ago ROLLED, 3 reads; operator 2 days ago      120 under SDK
#   UIONLY    ALPACA/IEX       operator today                                           never under SDK
#   IBREAD    IB/IEX           a two days ago                                           2
#   SIP       ALPACA/SIP       none                                                     never
#   NEVER     ALPACA/IEX       none                                                     never

NAMES = ('NOWREAD', 'D1', 'D7', 'D8', 'D30', 'D31', 'D90', 'D91', 'R120', 'UIONLY', 'IBREAD', 'SIP', 'NEVER')
SOURCES = {'IBREAD': (DataSource.IB_API, Feed.IEX), 'SIP': (DataSource.ALPACA_API, Feed.SIP)}
BARS = {'NOWREAD': 3, 'D91': 1, 'IBREAD': 2, 'SIP': 4}


@dataclass(frozen=True)
class Read:
    name: str
    client: str
    at: datetime
    reads: int = 1
    rolled: bool = False


READS = (
    Read('NOWREAD', A, NOW - timedelta(hours=1)),
    Read('NOWREAD', B, day_start(0)),
    Read('NOWREAD', B, day_start(0) + timedelta(seconds=1)),
    Read('D1', B, last_second(1)),
    Read('D7', A, day_start(7)),
    Read('D8', A, day_start(8)),
    Read('D30', C, day_start(30)),
    Read('D31', C, last_second(31)),
    Read('D90', B, last_second(90)),
    Read('D91', B, day_start(91) + timedelta(hours=9), rolled=True),
    Read('R120', A, day_start(120) + timedelta(hours=15), reads=3, rolled=True),
    Read('R120', OPERATOR_PRINCIPAL, NOW - timedelta(days=2)),
    Read('UIONLY', OPERATOR_PRINCIPAL, NOW - timedelta(hours=3)),
    Read('IBREAD', A, NOW - timedelta(days=2)),
)


class World:
    def __init__(self, ids: dict[str, uuid.UUID], reads: tuple[Read, ...]) -> None:
        self.ids = ids
        self.names = {value: key for key, value in ids.items()}
        self.reads = reads

    def named(self, items: list[dict]) -> list[str]:
        return [self.names[uuid.UUID(item['dataset']['id'])] for item in items]

    # The ORACLE: what each figure must be, from the seed alone.

    def selected(self, clients: list[tuple[str, str]]) -> Callable[[str], bool]:
        ids = [value for key, value in clients if key == 'client_id']
        kinds = {value.upper().removeprefix('CLIENT_KIND_') for key, value in clients if key == 'client_kind'} or {
            'SDK'
        }
        return (lambda client: client in ids) if ids else (lambda client: KIND_OF[client] in kinds)

    def last_reads(
        self, clients: list[tuple[str, str]], source: DataSource | None = None
    ) -> dict[str, datetime | None]:
        keep = self.selected(clients)
        last: dict[str, datetime | None] = {
            name: None
            for name in self.ids
            if source is None or SOURCES.get(name, (DataSource.ALPACA_API,))[0] is source
        }
        for read in self.reads:
            if read.name in last and keep(read.client):
                current = last[read.name]
                last[read.name] = read.at if current is None else max(current, read.at)
        return last

    def per_day(self, clients: list[tuple[str, str]], days: int, only: set[str] | None = None) -> list[dict]:
        keep = self.selected(clients)
        first = TODAY - timedelta(days=days - 1)
        counts: dict[tuple[date, str], int] = defaultdict(int)
        for read in self.reads:
            if keep(read.client) and read.at.date() >= first and (only is None or read.name in only):
                counts[(read.at.date(), read.client)] += read.reads
        return [
            {
                'day': day.isoformat(),
                'clientId': client_id,
                'clientKind': f'CLIENT_KIND_{KIND_OF[client_id]}',
                'reads': str(n),
            }
            for (day, client_id), n in sorted(counts.items())
        ]


def _entry(conn: sa.Connection, entry_values: Callable[..., dict[str, Any]], symbol: str, name: str) -> sa.Row:
    source, feed = SOURCES.get(name, (DataSource.ALPACA_API, Feed.IEX))
    return conn.execute(
        sa.insert(ENTRY_TABLE)
        .values(entry_values(asset_symbol=symbol, source=source, feed=feed))
        .returning(*ENTRY_TABLE.c)
    ).one()


def _read_rows(ids: dict[str, uuid.UUID], reads: tuple[Read, ...]) -> tuple[list[dict], list[dict]]:
    raw, daily = [], []
    for read in reads:
        if read.rolled:
            daily.append(
                {
                    'dataset_id': ids[read.name],
                    'client_id': read.client,
                    'client_kind': KIND_OF[read.client],
                    'day': read.at.date(),
                    'reads': read.reads,
                    'sessions': 0,
                    'bars_returned': 5 * read.reads,
                    'last_read_at': read.at,
                }
            )
        else:
            raw.extend([usage_tests._read_row(ids[read.name], read.client, KIND_OF[read.client], read.at)] * read.reads)
    return raw, daily


def _seed(engine: Engine, entry_values, bar_values, prefix: str = '') -> World:
    ids: dict[str, uuid.UUID] = {}
    with engine.begin() as conn:
        for name in NAMES:
            row = _entry(conn, entry_values, f'{prefix}{name}', name)
            ids[name] = row.id
            if BARS.get(name):
                conn.execute(
                    sa.insert(BAR_TABLE),
                    [bar_values(row, timestamp=row.start + timedelta(minutes=m)) for m in range(BARS[name])],
                )
        raw, daily = _read_rows(ids, READS)
        conn.execute(sa.insert(READ_TABLE), raw)
        conn.execute(sa.insert(DAILY_TABLE), daily)
    return World(ids, READS)


@pytest.fixture
def world(scratch_db, entry_values, bar_values) -> World:
    scratch_db.must('upgrade', _head())
    return _seed(scratch_db.engine, entry_values, bar_values)


async def get(client: httpx.AsyncClient, path: str, params: Any = ()) -> dict:
    response = await client.get(path, params=list(params.items()) if isinstance(params, dict) else list(params))
    assert response.status_code == 200, (path, response.text)
    return response.json()


async def stale_all(client: httpx.AsyncClient, params: list[tuple[str, Any]], limit: int = 500) -> list[dict]:
    """Every stale row under params, following next_cursor; asserts nothing is served twice."""
    items: list[dict] = []
    cursor = None
    for _ in range(100):
        body = await get(client, STALE, [*params, ('limit', limit), *([('cursor', cursor)] if cursor else [])])
        items.extend(body.get('items', []))
        cursor = body.get('nextCursor')
        if not cursor:
            break
    ids = [item['dataset']['id'] for item in items]
    assert len(ids) == len(set(ids)), f'a row was served twice: {ids}'
    return items


def n(body: dict, field: str) -> int:
    return int(body.get(field, '0'))


def stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------------------------------------------------------------------------------------------
# The figures, against hand-written answers first.


@pytest.mark.asyncio
async def test_the_summary_under_the_default_sdk_filter(world, client):
    body = await get(client, SUMMARY)
    # SDK: never read are UIONLY, SIP, NEVER. Stale at 90: those, D91 and R120 (rolled), not D90.
    # read_last_30d is NOWREAD, D1, D7, D8, D30 and IBREAD: D31 is a second too early.
    assert body == {
        'stale': '5',
        'readLast30d': '6',
        'total': '13',
        'neverRead': '3',
        'readsToday': '3',
        'distinctClientsToday': '2',
        'clientKinds': [SDK],
        'staleDays': 90,
    }, body


@pytest.mark.asyncio
async def test_reads_today_and_distinct_clients_follow_the_filter(world, client):
    cases = {
        (): ('3', '2'),  # SDK: a once, b twice on NOWREAD
        (('client_kind', 'UI'),): ('1', '1'),  # the operator on UIONLY today
        (('client_kind', 'UI'), ('client_kind', 'SDK')): ('4', '3'),
        (('client_id', B), ('client_kind', 'UI')): ('2', '1'),  # client_id wins
        (('client_id', C),): (None, None),
    }
    for params, (reads, clients) in cases.items():
        body = await get(client, SUMMARY, params)
        assert (body.get('readsToday'), body.get('distinctClientsToday')) == (reads, clients), params


@pytest.mark.asyncio
async def test_the_buckets_by_whole_utc_days_under_the_default_filter(world, client):
    body = await get(client, BUCKETS)
    assert body['buckets'] == [
        {'maxDays': 1, 'datasets': '1'},  # NOWREAD
        {'minDays': 1, 'maxDays': 8, 'datasets': '3'},  # D1 (23:59:59 yesterday), IBREAD, D7
        {'minDays': 8, 'maxDays': 31, 'datasets': '2'},  # D8, D30
        {'minDays': 31, 'maxDays': 91, 'datasets': '2'},  # D31, D90
        {'minDays': 91, 'datasets': '2'},  # D91 and R120, both rolled up
    ], body
    assert body['neverRead'] == '3' and body['clientKinds'] == [SDK]


# ---------------------------------------------------------------------------------------------
# The acceptance: agreement, sums and the oracle, over many filters.

FILTERS = {
    'default': [],
    'UI': [('client_kind', 'UI')],
    'both kinds': [('client_kind', 'SDK'), ('client_kind', 'UI')],
    'client a wins over UI': [('client_id', A), ('client_kind', 'UI')],
    'clients b and c': [('client_id', B), ('client_id', C)],
    'operator by id': [('client_id', OPERATOR_PRINCIPAL)],
    'an id nobody uses': [('client_id', 'nobody')],
}
STALE_DAYS = (1, 7, 8, 29, 30, 31, 89, 90, 91, 119, 120, 121, 3650)


@pytest.mark.asyncio
async def test_summary_stale_list_and_stale_facet_agree_for_every_filter_and_stale_days(world, client):
    for clients, days in ((clients, days) for clients in FILTERS.values() for days in STALE_DAYS):
        params = [*clients, ('stale_days', days)]
        summary = await get(client, SUMMARY, params)
        listed = world.named(await stale_all(client, params, limit=3))
        facets = await get(client, FACETS, params)
        expected = {
            name
            for name, last in world.last_reads(clients).items()
            if last is None or last.date() < TODAY - timedelta(days=days)
        }
        assert set(listed) == expected, (clients, days, listed)
        assert n(summary, 'stale') == len(listed) == n(facets, 'stale'), (clients, days)
        assert summary['staleDays'] == days and summary.get('clientIds') == facets.get('clientIds')
        # The other two figures are the facets' too.
        assert n(summary, 'readLast30d') == n(facets, 'read30d'), clients
        assert n(summary, 'neverRead') == n(facets, 'neverRead'), clients
        assert n(summary, 'total') == n(facets, 'all') == len(NAMES)


@pytest.mark.asyncio
async def test_the_buckets_sum_to_the_total_and_match_the_summary_s_rule(world, client):
    for clients in FILTERS.values():
        for source in (None, 'ALPACA_API', 'IB_API', 'MANUAL_ENTRY'):
            await _buckets_case(world, client, clients, source)


async def _buckets_case(world: World, client: httpx.AsyncClient, clients: list, source: str | None) -> None:
    params = [*clients, *([('source', source)] if source else [])]
    buckets = await get(client, BUCKETS, params)
    summary = await get(client, SUMMARY, params)
    counts = [n(bucket, 'datasets') for bucket in buckets['buckets']]
    assert sum(counts) + n(buckets, 'neverRead') == n(summary, 'total'), (counts, buckets, summary)
    assert n(buckets, 'neverRead') == n(summary, 'neverRead')
    # [0,31) is read within 30 days; [91,open) plus never is stale at 90: the one rule, two shapes.
    assert sum(counts[:3]) == n(summary, 'readLast30d')
    assert counts[4] + n(buckets, 'neverRead') == n(summary, 'stale')
    last = world.last_reads(clients, None if source is None else DataSource[source])
    expected = [0] * 5
    for at in last.values():
        if at is not None:
            age = (TODAY - at.date()).days
            expected[sum(age >= start for start in (1, 8, 31, 91))] += 1
    assert counts == expected and n(buckets, 'neverRead') == sum(at is None for at in last.values())
    facets = await get(client, FACETS, params)
    assert n(summary, 'stale') == n(facets, 'stale'), 'the Stale facet agrees under a source too'
    assert len(await stale_all(client, params)) == n(summary, 'stale')


@pytest.mark.asyncio
async def test_a_ui_only_dataset_is_stale_under_sdk_and_not_under_ui_and_client_id_wins(world, client):
    sdk = set(world.named(await stale_all(client, [])))
    ui = set(world.named(await stale_all(client, [('client_kind', 'UI')])))
    by_id = set(world.named(await stale_all(client, [('client_kind', 'UI'), ('client_id', A)])))
    assert 'UIONLY' in sdk and 'UIONLY' not in ui and 'UIONLY' in by_id
    assert 'R120' in sdk and 'R120' not in ui, 'the operator read it two days ago; the SDK read is rolled, 120 days'
    assert by_id == set(NAMES) - {'NOWREAD', 'D7', 'D8', 'IBREAD'}


@pytest.mark.asyncio
async def test_the_stale_list_order_rows_and_last_read_by_client(world, client, scratch_db):
    items = await stale_all(client, [], limit=2)
    names = world.named(items)
    never = sorted(['UIONLY', 'SIP', 'NEVER'], key=lambda name: str(world.ids[name]))
    assert names == [*never, 'R120', 'D91'], 'never-read first by id, then the oldest last read'
    rows = dict(zip(names, items, strict=True))

    # dataset.usage carries the filtered last read; last_read_by_client every client, whatever the filter.
    assert rows['R120']['dataset']['usage']['lastReadAt'] == stamp(world.reads[10].at)
    assert rows['R120']['lastReadByClient'] == [
        {'clientId': A, 'clientKind': SDK, 'lastReadAt': stamp(world.reads[10].at)},
        {'clientId': OPERATOR_PRINCIPAL, 'clientKind': UI, 'lastReadAt': stamp(NOW - timedelta(days=2))},
    ]
    assert rows['UIONLY']['lastReadByClient'] == [
        {'clientId': OPERATOR_PRINCIPAL, 'clientKind': UI, 'lastReadAt': stamp(NOW - timedelta(hours=3))}
    ]
    assert 'lastReadAt' not in rows['UIONLY']['dataset']['usage'], 'never read by SDK'
    assert 'lastReadByClient' not in rows['NEVER']
    assert rows['D91']['lastReadByClient'] == [
        {'clientId': B, 'clientKind': SDK, 'lastReadAt': stamp(world.reads[9].at)}
    ]

    # est_bytes = bar_count x (pg_total_relation_size / all bars), from the database directly.
    with scratch_db.engine.connect() as conn:
        total = conn.execute(sa.text("SELECT pg_total_relation_size('stock_market_activity')")).scalar_one()
    bars = sum(BARS.values())
    for name in names:
        assert n(rows[name], 'estBytes') == BARS.get(name, 0) * total // bars, name
    assert n(rows['SIP'], 'estBytes') > 0 and n(rows['D91'], 'estBytes') > 0


@pytest.mark.asyncio
async def test_a_stale_cursor_continues_only_under_its_own_filter(world, client):
    first = await get(client, STALE, [('client_id', B), ('client_id', A), ('stale_days', 30), ('limit', 3)])
    cursor = first['nextCursor']
    assert A not in cursor and B not in cursor
    same = await get(
        client, STALE, [('client_id', A), ('client_id', B), ('stale_days', 30), ('limit', 3), ('cursor', cursor)]
    )
    assert not set(world.named(same['items'])) & set(world.named(first['items']))
    for other in (
        [('client_id', A), ('stale_days', 30)],
        [('client_id', A), ('client_id', B), ('stale_days', 31)],
        [('client_id', A), ('client_id', B), ('stale_days', 30), ('source', 'ALPACA_API')],
        [('stale_days', 30)],
    ):
        response = await client.get(STALE, params=[*other, ('cursor', cursor)])
        assert response.status_code == 422, (other, response.text)
        assert A not in response.text and B not in response.text


# ---------------------------------------------------------------------------------------------
# Reads per day.


@pytest.mark.asyncio
async def test_reads_per_day_is_one_entry_per_day_and_client_over_rows_and_rollup(world, client):
    for clients in FILTERS.values():
        for days in (1, 2, 8, 30, 31, 91, 92, 121, 365):
            body = await get(client, PER_DAY, [*clients, ('days', days)])
            assert body.get('days', []) == world.per_day(clients, days), (clients, days, body)


@pytest.mark.asyncio
async def test_reads_per_day_by_hand_and_by_dataset_and_source(world, client):
    body = await get(client, PER_DAY, {'days': 2})
    assert body['days'] == [
        {'day': (TODAY - timedelta(days=1)).isoformat(), 'clientId': B, 'clientKind': SDK, 'reads': '1'},
        {'day': TODAY.isoformat(), 'clientId': A, 'clientKind': SDK, 'reads': '1'},
        {'day': TODAY.isoformat(), 'clientId': B, 'clientKind': SDK, 'reads': '2'},
    ]
    # days includes today: 30 stops a day short of D30's read, 31 holds it.
    assert 'D30' not in {read.name for read in READS if read.at.date() >= TODAY - timedelta(days=29)}
    assert (await get(client, PER_DAY, [('client_id', C), ('days', 30)])).get('days') is None
    assert len((await get(client, PER_DAY, [('client_id', C), ('days', 31)]))['days']) == 1
    # The rolled 120-day read, by dataset (Dataset detail).
    rolled = await get(client, PER_DAY, {'days': 365, 'dataset_id': str(world.ids['R120'])})
    assert rolled['days'] == [
        {'day': (TODAY - timedelta(days=120)).isoformat(), 'clientId': A, 'clientKind': SDK, 'reads': '3'}
    ]
    ib = await get(client, PER_DAY, {'days': 365, 'source': 'IB_API'})
    assert ib['days'] == world.per_day([], 365, {'IBREAD'})
    assert (await get(client, PER_DAY, {'source': 'IB_API', 'dataset_id': str(world.ids['NOWREAD'])})).get(
        'days'
    ) is None
    assert (await get(client, PER_DAY, {'dataset_id': str(uuid.uuid4())})).get('days') is None


@pytest.mark.asyncio
async def test_reads_of_a_deleted_dataset_still_count_without_a_dataset_or_source(world, client, scratch_db):
    """Interpretation (5): with neither, every read counts, as reads_today does; with a source, only its datasets."""
    with scratch_db.engine.begin() as conn:
        conn.execute(sa.delete(ENTRY_TABLE).where(ENTRY_TABLE.c.id == world.ids['NOWREAD']))
    everything = await get(client, PER_DAY, {'days': 1})
    assert sum(n(entry, 'reads') for entry in everything['days']) == 3
    assert n(await get(client, SUMMARY), 'readsToday') == 3
    by_source = await get(client, PER_DAY, {'days': 1, 'source': 'ALPACA_API'})
    assert by_source.get('days') is None, 'NOWREAD is gone; no other ALPACA read today by SDK'
    assert n(await get(client, SUMMARY, {'source': 'ALPACA_API'}), 'readsToday') == 0


# ---------------------------------------------------------------------------------------------
# Continuity across the rollup: the real fold changes no figure.


async def _snapshot(client: httpx.AsyncClient, world: World) -> dict:
    found: dict[str, Any] = {}
    for label, clients in FILTERS.items():
        found[f'per day {label}'] = await get(client, PER_DAY, [*clients, ('days', 365)])
        found[f'summary {label}'] = await get(client, SUMMARY, clients)
        found[f'buckets {label}'] = await get(client, BUCKETS, clients)
        found[f'stale {label}'] = await stale_all(client, clients)
    for name in ('NOWREAD', 'D90', 'R120'):
        found[f'detail per day {name}'] = await get(client, PER_DAY, {'days': 365, 'dataset_id': str(world.ids[name])})
    return found


@pytest.mark.asyncio
async def test_reads_per_day_and_every_figure_are_continuous_across_the_real_rollup(
    world, client, scratch_db, scratch_async: AsyncEngine
):
    """Fold at three cutoffs, each mid-day, so a day is split between dataset_read and dataset_read_daily."""
    # Several reads on one day, spread over it, by two clients of two kinds, so a mid-day cut splits each.
    split_day = day_start(90)
    extra = tuple(
        Read('D90', client_id, split_day + timedelta(hours=hour))
        for hour in (1, 6, 11, 13, 18, 23)
        for client_id in (A, OPERATOR_PRINCIPAL)
    )
    world.reads = world.reads + extra
    raw, _ = _read_rows(world.ids, extra)
    with scratch_db.engine.begin() as conn:
        conn.execute(sa.insert(READ_TABLE), raw)
    before = await _snapshot(client, world)
    assert before['per day both kinds']['days'] == world.per_day(FILTERS['both kinds'], 365)
    split = [entry for entry in before['per day both kinds']['days'] if entry['day'] == split_day.date().isoformat()]
    assert {(entry['clientId'], entry['reads']) for entry in split} == {(A, '6'), (OPERATOR_PRINCIPAL, '6'), (B, '1')}

    for cutoff in (split_day + timedelta(hours=12), day_start(7) + timedelta(hours=12), NOW - timedelta(minutes=30)):
        folded = await test_usage_record._fold(scratch_async, cutoff)
        assert folded > 0, cutoff
        if cutoff.date() == split_day.date():
            with scratch_db.engine.connect() as conn:
                left = conn.execute(
                    sa.select(sa.func.count()).where(
                        READ_TABLE.c.read_at >= split_day, READ_TABLE.c.read_at < day_start(89)
                    )
                ).scalar_one()
            assert left == 7, 'the day is split: the afternoon (and D90 23:59:59) stays as rows'
        after = await _snapshot(client, world)
        for key, value in before.items():
            assert after[key] == value, (cutoff, key)


# ---------------------------------------------------------------------------------------------
# Storage.


@pytest.mark.asyncio
async def test_storage_splits_the_measured_size_by_source_and_feed(world, client, scratch_db):
    body = await get(client, STORAGE)
    with scratch_db.engine.connect() as conn:
        total = conn.execute(sa.text("SELECT pg_total_relation_size('stock_market_activity')")).scalar_one()
    assert int(body['totalBytes']) == total > 0 and body['asOf'] == stamp(NOW)
    bars = sum(BARS.values())
    shares = {(share['source'], share['feed']): int(share['estBytes']) for share in body['bySource']}
    assert shares == {
        ('DATA_SOURCE_ALPACA_API', 'FEED_IEX'): 4 * total // bars,
        ('DATA_SOURCE_ALPACA_API', 'FEED_SIP'): 4 * total // bars,
        ('DATA_SOURCE_IB_API', 'FEED_IEX'): 2 * total // bars,
    }
    assert 0 <= total - sum(shares.values()) < len(shares), 'the split sums to the total within rounding'
    alpaca = await get(client, STORAGE, {'source': 'ALPACA_API'})
    # Interpretation (2), as built: with a source, the total is the sum of that source's estimates.
    assert int(alpaca['totalBytes']) == 2 * (4 * total // bars)
    assert int(alpaca['totalBytes']) == sum(int(share['estBytes']) for share in alpaca['bySource'])
    assert {share['source'] for share in alpaca['bySource']} == {'DATA_SOURCE_ALPACA_API'}
    assert 'totalBytes' not in await get(client, STORAGE, {'source': 'MANUAL_ENTRY'})


# ---------------------------------------------------------------------------------------------
# The statement budget.

BUDGET = {
    'summary': (SUMMARY, [('stale_days', 30)]),
    'summary by source and id': (SUMMARY, [('source', 'ALPACA_API'), ('client_id', A)]),
    'buckets': (BUCKETS, [('client_kind', 'UI'), ('client_kind', 'SDK')]),
    'per day': (PER_DAY, [('days', 365)]),
    'per day by source': (PER_DAY, [('days', 365), ('source', 'ALPACA_API')]),
    'per day by dataset': (PER_DAY, [('days', 365), ('dataset_id', None)]),
    'stale': (STALE, [('limit', 500)]),
    'stale by ids and source': (STALE, [('limit', 500), ('client_id', A), ('client_id', B), ('source', 'ALPACA_API')]),
    'storage': (STORAGE, []),
    'storage by source': (STORAGE, [('source', 'IB_API')]),
}


@pytest.mark.asyncio
async def test_each_stats_request_runs_a_constant_number_of_statements(
    world, client, scratch_db, scratch_async, entry_values, bar_values, caplog
):
    caplog.set_level(logging.WARNING, logger='httpx')
    statements = Statements(scratch_async)

    async def counts() -> dict[str, int]:
        found = {}
        for name, (path, params) in BUDGET.items():
            sent = [(key, str(world.ids['NOWREAD']) if value is None else value) for key, value in params]
            statements.sent.clear()
            await get(client, path, sent)
            found[name] = len(statements.sent)
        return found

    await counts()  # warm: the first connection runs the dialect's own setup statements
    small = await counts()
    for index in range(4):  # 52 more datasets, every shape again, read raw and rolled, with bars
        _seed(scratch_db.engine, entry_values, bar_values, prefix=f'X{index}')
    large = await counts()
    assert small == large, f'the statement count grew with the datasets: {small} -> {large}'
    # Pinned, so a new statement is a deliberate change. summary: catalog, probe, usage, reads today. buckets:
    # catalog, probe, usage. per day: probe and the union (plus the catalog with a source). stale: catalog, probe,
    # usage, size, counts, extents, last read per client, freshness, siblings, readers, pending, last runs.
    # storage: size and counts.
    assert large == {
        'summary': 4,
        'summary by source and id': 4,
        'buckets': 3,
        'per day': 2,
        'per day by source': 3,
        'per day by dataset': 2,
        'stale': 12,
        'stale by ids and source': 12,
        'storage': 2,
        'storage by source': 2,
    }, large


# ---------------------------------------------------------------------------------------------
# An unmigrated database.


@pytest.mark.asyncio
async def test_an_unmigrated_database_reads_as_zeros_with_one_warning(
    scratch_db, client, entry_values, monkeypatch, caplog
):
    scratch_db.must('upgrade', PRE_USAGE_REVISION)
    with scratch_db.engine.begin() as conn:
        conn.execute(sa.insert(ENTRY_TABLE).values(entry_values(asset_symbol='UNREAD')))
    monkeypatch.setattr(catalog_module, '_warned_usage_missing', False)
    caplog.set_level(logging.DEBUG, logger=catalog_module.log.name)

    assert await get(client, SUMMARY) == {
        'stale': '1',
        'total': '1',
        'neverRead': '1',
        'clientKinds': [SDK],
        'staleDays': 90,
    }
    buckets = await get(client, BUCKETS)
    assert buckets['neverRead'] == '1' and all('datasets' not in bucket for bucket in buckets['buckets'])
    assert await get(client, PER_DAY, {'days': 365}) == {'clientKinds': [SDK]}
    stale = await get(client, STALE)
    assert len(stale['items']) == 1 and 'lastReadByClient' not in stale['items'][0]
    assert 'totalBytes' in await get(client, STORAGE)

    warnings = [r for r in caplog.records if r.name == catalog_module.log.name and r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and 'make migrate' in warnings[0].getMessage(), [r.getMessage() for r in warnings]
