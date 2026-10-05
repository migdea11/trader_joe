"""Bars over real HTTP: single-bar refresh after commit, and the filtering read (tj-vhboky.50, Sys-3).

tj-vhboky.14 items 9 and 11, and the tj-vhboky.43 gate's finding c. Design: tj-vhboky.25 (the
filtering read: selectors, filters, inclusive bounds, ORDER BY timestamp then dataset_id, the
422s), tj-vhboky.26 and .28 (a query must name dataset_id or a non-blank asset_symbol),
tj-vhboky.43 (the single-bar write through write_transaction), tj-p78ng6 (feed filter),
tj-6z03hd (a filtered read returns fewer rows than a symbol-only one).

REFRESH AFTER COMMIT. POST /internal/asset-data for one bar returns the stored row, with its
server-assigned id, created_at and updated_at populated. The unit tier only fakes the refresh
after write_transaction's commit; here the returned row is compared with the row Postgres holds.

THE FILTERING READ. One module-scoped seed under a symbol of this run, bars written over HTTP:
  * A: the run's owner, 1min, IEX bars at minutes 0-6;
  * B: a second owner, otherwise A's spec, SIP bars at minutes 1-3 -- so minutes 1-3 each hold
    two rows, one per dataset;
  * C: the run's owner, 5min, IEX bars at minutes 0 and 5.
Each read's expected rows are computed from what the seed POSTs returned, never from the read
itself. Bars are POSTed newest first, and the entry with the larger id first, so a read that
lost its ORDER BY would come back in (likely) the opposite order rather than the required one by
luck of insertion order.

Entries are seeded by SQL (the only HTTP route that creates one reaches data_ingest, never
driven here -- decision tj-vhboky.54). Every request goes through the prod image's
uvicorn/starlette (tj-jon3d1 carry-over, covered implicitly; see test_http_write_secret.py).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import Feed, Granularity
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from routers.data_store.app_endpoints import AssetDataInterface


pytestmark = pytest.mark.data_store

BAR_TABLE = StockMarketActivity.__table__
BAR_PATH = AssetDataInterface.POST_ASSET_DATA.format(
    asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value
)
READ_PATH = AssetDataInterface.GET_ASSET_DATA.format(
    asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value
)
# A non-UTC zone for writing query bounds, so the read is shown to compare INSTANTS: the same
# instant written with a -05:00 offset must select the same rows as its UTC spelling.
BOUND_ZONE = ZoneInfo('America/Toronto')

# (dataset_id, timestamp, feed, granularity): what identifies a returned bar for these tests.
BarKey = tuple[UUID, datetime, str, str]


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None, f'{value!r} came back without an offset'
    return parsed


def _key(bar: dict[str, Any]) -> BarKey:
    return (UUID(bar['dataset_id']), _instant(bar['timestamp']), bar['feed'], bar['granularity'])


def _read(data_store, params: dict[str, Any]) -> tuple[httpx.Response, list[BarKey]]:
    response = data_store.get(READ_PATH, params=params)
    assert response.status_code == 200, data_store.describe(response)
    return response, [_key(bar) for bar in response.json()]


def _ordered(keys: set[BarKey] | list[BarKey]) -> list[BarKey]:
    """ORDER BY timestamp, dataset_id, as the read must return them.

    Python orders UUIDs by their 128-bit value, which is Postgres's uuid order (bytewise, big-endian).
    """
    return sorted(keys, key=lambda key: (key[1], key[0]))


# ---------------------------------------------------------------------------------------------
# Refresh after commit (tj-vhboky.43, finding c).


def test_single_bar_post_returns_the_stored_row_with_server_fields(
    insert_entry: Callable[..., sa.Row],
    own_symbol: str,
    data_store,
    bar_create_body: Callable[..., dict[str, Any]],
    pg_engine: Engine,
) -> None:
    entry = insert_entry(asset_symbol=own_symbol)
    timestamp = entry.start + timedelta(minutes=7)
    body = bar_create_body(
        entry, timestamp, open=101.25, high=102.5, low=100.75, close=101.5, volume=31_337, trade_count=7
    )

    response = data_store.post(BAR_PATH, json=body, auth='right')

    assert response.status_code == 200, data_store.describe(response)
    returned = response.json()
    # Server-assigned fields: present, and populated by the database, not left at None.
    assert isinstance(returned['id'], int) and returned['id'] > 0, data_store.describe(response)
    created_at, updated_at = _instant(returned['created_at']), _instant(returned['updated_at'])
    # The caller's fields, round-tripped.
    assert UUID(returned['dataset_id']) == entry.id, data_store.describe(response)
    assert returned['asset_symbol'] == own_symbol, data_store.describe(response)
    assert returned['feed'] == body['feed'], data_store.describe(response)
    assert returned['granularity'] == body['granularity'], data_store.describe(response)
    assert _instant(returned['timestamp']) == timestamp, data_store.describe(response)
    assert returned['data'] == body['data'], data_store.describe(response)

    # And it IS the stored row: the row Postgres holds under that id says the same.
    with pg_engine.connect() as conn:
        stored = conn.execute(sa.select(BAR_TABLE).where(BAR_TABLE.c.dataset_id == entry.id)).one()
    assert stored.id == returned['id'], f'stored id {stored.id}; {data_store.describe(response)}'
    assert stored.timestamp == timestamp, f'stored timestamp {stored.timestamp}'
    assert (stored.created_at, stored.updated_at) == (created_at, updated_at), (
        f'stored created_at/updated_at {stored.created_at}/{stored.updated_at}; {data_store.describe(response)}'
    )
    assert (stored.open, stored.volume, stored.trade_count) == (101.25, 31_337, 7)


# ---------------------------------------------------------------------------------------------
# The filtering read (items 9 and 11).

A_MINUTES = (0, 1, 2, 3, 4, 5, 6)
B_MINUTES = (1, 2, 3)
C_MINUTES = (0, 5)  # 5-minute bars stay on 5-minute boundaries

# The item-11 read: granularity plus an inclusive window, in minutes from the seed's start. Chosen
# WITH the seed above so that each of the three filters removes a seeded bar the other two admit:
# granularity removes C's minute 5, start removes A's minute 0, end removes A's minute 6. The test
# asserts that as a precondition (tj-3mk3u5.41; tj-vhboky.14 F1 is the window that did not).
FILTER_GRANULARITY = Granularity.ONE_MINUTE
FILTER_FIRST_MINUTE = 1
FILTER_LAST_MINUTE = 5


@dataclass(frozen=True)
class ReadSeed:
    symbol: str
    a: sa.Row
    b: sa.Row
    c: sa.Row
    keys: dict[str, set[BarKey]]  # dataset letter -> the keys its POSTs returned

    def instant(self, minute: int) -> datetime:
        return self.a.start + timedelta(minutes=minute)

    @property
    def every_key(self) -> set[BarKey]:
        return set().union(*self.keys.values())


@pytest.fixture(scope='module')
def read_seed(
    insert_entry: Callable[..., sa.Row], run_identity, data_store, bar_create_body: Callable[..., dict[str, Any]]
) -> ReadSeed:
    symbol = run_identity.alt_symbol('READ')
    a_entry = insert_entry(asset_symbol=symbol)
    b_entry = insert_entry(asset_symbol=symbol, owner=run_identity.alt_owner('other'))
    c_entry = insert_entry(asset_symbol=symbol, granularity=Granularity.FIVE_MINUTES)

    plan = {
        'a': (a_entry, A_MINUTES, Feed.IEX),
        'b': (b_entry, B_MINUTES, Feed.SIP),
        'c': (c_entry, C_MINUTES, Feed.IEX),
    }
    keys: dict[str, set[BarKey]] = {letter: set() for letter in plan}
    # Larger dataset id first, newest bar first: the opposite of the required order.
    for letter in sorted(plan, key=lambda letter: plan[letter][0].id, reverse=True):
        entry, minutes, feed = plan[letter]
        for minute in sorted(minutes, reverse=True):
            timestamp = entry.start + timedelta(minutes=minute)
            response = data_store.post(BAR_PATH, json=bar_create_body(entry, timestamp, feed=feed), auth='right')
            assert response.status_code == 200, data_store.describe(response)
            keys[letter].add(_key(response.json()))
    return ReadSeed(symbol=symbol, a=a_entry, b=b_entry, c=c_entry, keys=keys)


def _table_rows(pg_engine: Engine) -> int:
    with pg_engine.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(BAR_TABLE)).scalar_one()


def test_filtered_read_returns_only_the_seeded_matches_and_fewer_than_symbol_only(
    read_seed: ReadSeed, data_store, pg_engine: Engine
) -> None:
    """Item 11: symbol + granularity + range returns exactly the matches, fewer than symbol-only.

    PRECONDITION, checked before any request: each of the three filters removes at least one
    seeded bar that the other two admit. Without that, deleting one filter from the query leaves
    this test green -- tj-vhboky.14 F1, where the window alone already excluded every 5-minute bar
    and the granularity predicate went unpinned. It is computed from the seed's own keys, so a
    later edit to the seed or the window cannot vacate a filter silently.
    """
    start, end = read_seed.instant(FILTER_FIRST_MINUTE), read_seed.instant(FILTER_LAST_MINUTE)
    admitted_by: dict[str, Callable[[BarKey], bool]] = {
        'granularity': lambda key: key[3] == FILTER_GRANULARITY.value,
        'start': lambda key: key[1] >= start,
        'end': lambda key: key[1] <= end,
    }
    for name, admits in admitted_by.items():
        others = [other for other_name, other in admitted_by.items() if other_name != name]
        removed_by_it_alone = {
            key for key in read_seed.every_key if all(other(key) for other in others) and not admits(key)
        }
        assert removed_by_it_alone, (
            f'precondition: the {name} filter removes no seeded bar that the other two admit, so this test '
            f'cannot see that filter dropped; re-cut read_seed or the FILTER_* window'
        )

    table_rows = _table_rows(pg_engine)
    _, symbol_only = _read(data_store, {'asset_symbol': read_seed.symbol})
    _, filtered = _read(
        data_store,
        {
            'asset_symbol': read_seed.symbol,
            'granularity': FILTER_GRANULARITY.value,
            'start': start.isoformat(),
            'end': end.isoformat(),
        },
    )
    # From what the seed POSTs returned, never from the read.
    expected = {key for key in read_seed.every_key if all(admits(key) for admits in admitted_by.values())}
    counts = (
        f'table {table_rows} rows; symbol-only {len(symbol_only)}; filtered {len(filtered)}; expected {len(expected)}'
    )

    assert set(symbol_only) == read_seed.every_key, counts
    assert len(symbol_only) == len(read_seed.every_key), f'duplicates in the symbol-only read; {counts}'
    assert filtered == _ordered(expected), counts
    assert len(filtered) < len(symbol_only) <= table_rows, counts


def test_range_bounds_are_inclusive_and_compare_instants(read_seed: ReadSeed, data_store) -> None:
    """Item 9: start and end are inclusive on timestamptz, whatever offset the bound is written in."""
    first, last = read_seed.instant(1), read_seed.instant(3)
    selector = {'dataset_id': str(read_seed.a.id)}

    _, on_the_edges = _read(
        data_store,
        {**selector, 'start': first.astimezone(BOUND_ZONE).isoformat(), 'end': last.astimezone(BOUND_ZONE).isoformat()},
    )
    assert [key[1] for key in on_the_edges] == [read_seed.instant(m) for m in (1, 2, 3)], on_the_edges

    nudge = timedelta(microseconds=1)
    _, inside = _read(data_store, {**selector, 'start': (first + nudge).isoformat(), 'end': (last - nudge).isoformat()})
    assert [key[1] for key in inside] == [read_seed.instant(2)], inside


def test_feed_filter_returns_one_tape(read_seed: ReadSeed, data_store) -> None:
    """tj-p78ng6: feed=IEX returns no SIP row, and feed=SIP only SIP rows."""
    _, iex = _read(data_store, {'asset_symbol': read_seed.symbol, 'feed': Feed.IEX.value})
    _, sip = _read(data_store, {'asset_symbol': read_seed.symbol, 'feed': Feed.SIP.value})

    assert set(iex) == read_seed.keys['a'] | read_seed.keys['c'], iex
    assert Feed.SIP.value not in {key[2] for key in iex}, iex
    assert set(sip) == read_seed.keys['b'], sip


def test_dataset_id_scopes_the_read_to_one_dataset(read_seed: ReadSeed, data_store) -> None:
    """Item 9: dataset_id alone returns one dataset's rows -- one tape's -- in timestamp order."""
    _, only_b = _read(data_store, {'dataset_id': str(read_seed.b.id)})
    _, a_with_symbol = _read(data_store, {'dataset_id': str(read_seed.a.id), 'asset_symbol': read_seed.symbol})

    assert only_b == _ordered(read_seed.keys['b']), only_b
    assert {key[2] for key in only_b} == {Feed.SIP.value}, only_b
    assert a_with_symbol == _ordered(read_seed.keys['a']), a_with_symbol


def test_read_orders_by_timestamp_then_dataset_id(read_seed: ReadSeed, data_store) -> None:
    """ORDER BY timestamp, then dataset_id, and nothing else (tj-vhboky.25 addendum)."""
    _, symbol_only = _read(data_store, {'asset_symbol': read_seed.symbol})

    shared = [key for key in symbol_only if key[1] in {read_seed.instant(m) for m in B_MINUTES}]
    assert len({key[0] for key in shared}) >= 2, (
        f'no instant is held by two datasets; the tie-break is untested: {shared}'
    )
    assert symbol_only == _ordered(read_seed.every_key), symbol_only


# ---------------------------------------------------------------------------------------------
# The 422s: refused at the edge, before the handler, over real HTTP.

NAIVE = '2001-02-05T14:30:00'
# Refused before any query runs, so it never reads a row; any syntactically valid symbol would do.
READ_ONLY_SYMBOL = 'ZZSYSNOREAD'


@pytest.mark.parametrize(
    ('params', 'loc', 'error_type'),
    [
        pytest.param({'granularity': Granularity.ONE_MINUTE.value}, ['query'], 'value_error', id='no-selector'),
        pytest.param({'asset_symbol': '   '}, ['query'], 'value_error', id='blank-symbol'),
        pytest.param(
            {'asset_symbol': READ_ONLY_SYMBOL, 'start': NAIVE}, ['query', 'start'], 'timezone_aware', id='naive-start'
        ),
        pytest.param(
            {'asset_symbol': READ_ONLY_SYMBOL, 'end': NAIVE}, ['query', 'end'], 'timezone_aware', id='naive-end'
        ),
        pytest.param(
            {'asset_symbol': READ_ONLY_SYMBOL, 'symbol': READ_ONLY_SYMBOL},
            ['query', 'symbol'],
            'extra_forbidden',
            id='unknown-parameter',
        ),
    ],
)
def test_malformed_read_is_422_naming_the_cause(
    params: dict[str, str], loc: list[str], error_type: str, data_store
) -> None:
    response = data_store.get(READ_PATH, params=params)

    assert response.status_code == 422, data_store.describe(response)
    errors = [(error['loc'], error['type']) for error in response.json()['detail']]
    assert errors == [(loc, error_type)], data_store.describe(response)
