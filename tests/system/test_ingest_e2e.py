"""The real ingest service end to end through FakeRead, over data_store's HTTP (tj-vhboky.63, Sys-7).

Design: decision tj-j4wknb (the fake is a BrokerRead in tests/fakes, run by the test-only launcher
the fake-mode overlay starts; addendum 5: the broker range is half-open), tj-vhboky.54 DECISION 4
(what a failure test asserts), tj-vhboky.6 (identity, own overlap, no policy merge), tj-vhboky.8
(write routes), tj-1bl90i (a naive expiry is refused, not converted), ADR tj-4rr0la addendum 3 (3)
(the agent stack loads the fake overlay last, so data_ingest runs FakeRead).

THE STACK MUST BE IN FAKE MODE: data_ingest's log carries the 'FAKE BROKER:' banner. Against a
real broker these symbols are not tickers and every test here fails -- never skips.

EVERY ENTRY HERE IS CREATED BY THE DATASET POST, the one route that reaches data_ingest: data_store
upserts the entry, asks data_ingest over the Kafka RPC, and writes the bars it gets back. Entry ids
are read by SQL (the POST answers with a count, not an id) and adopted for cleanup whether or not
the POST succeeded. Bars are read back over HTTP, FILTERED BY dataset_id: data_store's read is
closed at end while the broker range is half-open (tj-j4wknb addendum 5; tj-6w07z8 open), so an
unfiltered read over two owners' overlapping ranges would show the other entry's bar at this
entry's end and make a comparison lie.

WHAT A STORED BAR MUST EQUAL is computed here from the generator, tests.fakes.market_data's
grid_timestamps and fake_bar, over the request's [start, end) -- deliberately NOT by calling
FakeRead, which the test client mounts from the same tree as the stack, so a FakeRead defect would
appear on both sides and cancel. GAPS adds the scenario's documented rule (only even grid indices
are served) through grid_index. vwap is not compared: the store's bar has no vwap column.

FAILURE CLASS ONLY. A failure is asserted as NOT 2xx, never as a particular status code: the codes
are PR 2's (gRPC and typed errors, tj-fa1rpu, tj-8konfu D6.4). Today a FakeRead failure becomes the
bare {} at ingest's RPC edge and data_store's 5 s RPC deadline expires; that is not pinned here.

Items, tj-vhboky.14 numbering where they moved here from Sys-4:
  * default round trip -- bars equal the generator's for the range, feed IEX, data_points equals
    the count. Sent with the RIGHT secret and asserted 2xx: this CLOSES 7c FOR THE DATASET POST
    (Sys-3 asserted its 409 half; architect note 16:51 UTC 2026-09-28 on tj-vhboky.63).
  * EMPTY, GAPS, FAIL, SLOW, FAILONCE -- one test each, symbols carrying the scenario's prefix.
  * 4 -- an exact repeat resolves to the same entry and adds none; changing only expiry_type, then
    only owner, each makes a new entry; no entry takes the stronger expiry_type of the two. Feed is
    not part of an entry's identity at this revision and is not varied.
  * 5 (success half) -- a different owner's overlapping range is a separate entry, and both hold
    their own bars at the shared instants.
  * 8 (HTTP half) -- a range of more bars than POSTGRES_MAX_BIND_PARAMETERS //
    StockMarketActivity.bind_params_per_row() lands whole.
  * 13 -- an aware, non-UTC expiry reads back as the same instant with an offset, under the
    client's non-UTC TZ (docker-compose.test-client.yaml); a naive expiry is a 422 at the edge
    (tj-1bl90i: refuse) and creates nothing.
"""

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity
from data.store.app.database.crud.stock.asset_market_activity import POSTGRES_MAX_BIND_PARAMETERS
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from routers.data_store.app_endpoints import AssetDataInterface, AssetDatasetStoreInterface
from tests.fakes.market_data import (
    DEFAULT_SLOW_DELAY_SECONDS,
    EMPTY_PREFIX,
    FAIL_PREFIX,
    FAILONCE_PREFIX,
    GAPS_PREFIX,
    SLOW_PREFIX,
    fake_bar,
    grid_index,
    grid_timestamps,
)


pytestmark = pytest.mark.data_store

GRANULARITY = Granularity.ONE_MINUTE
# On the one-minute grid, so the range's first instant is itself a bar.
START = datetime.fromisoformat('2001-02-05T14:30:00+00:00')
END = START + timedelta(minutes=30)

READ_PATH = AssetDataInterface.GET_ASSET_DATA.format(
    asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value
)

# What a stored bar is compared on: (timestamp, open, high, low, close, volume, trade_count).
BarTuple = tuple[datetime, float, float, float, float, int, int]

# How long past FakeRead's SLOW_ delay the SLOW test waits before its second read, so the late RPC
# answer has certainly arrived (and been dropped) by then.
SLOW_MARGIN_SECONDS = 4.0


def _store_path(symbol: str) -> str:
    return AssetDatasetStoreInterface.POST_STORE_ASSET_DATASET.format(
        asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value, asset_symbol=symbol
    )


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None, f'{value!r} came back without an offset'
    return parsed


def _body(owner: str, start: datetime = START, end: datetime = END, **fields: Any) -> dict[str, Any]:
    return {
        'owner': owner,
        'source': DataSource.ALPACA_API.value,
        'granularity': GRANULARITY.value,
        'start': start.isoformat(),
        'end': end.isoformat(),
        **fields,
    }


def _post(data_store, adopt: Callable[[str], list[UUID]], symbol: str, body: dict[str, Any]):
    """POST the dataset request; adopt every entry for the symbol whatever the answer. Returns (response, ids)."""
    try:
        response = data_store.post(_store_path(symbol), json=body, auth='right')
    finally:
        ids = adopt(symbol)
    return response, ids


def _is_2xx(response: httpx.Response) -> bool:
    return 200 <= response.status_code < 300


def _expected(symbol: str, start: datetime = START, end: datetime = END, *, gaps: bool = False) -> list[BarTuple]:
    """The bars FakeRead's documented contract serves for [start, end), from the generator alone."""
    expected = []
    for timestamp in grid_timestamps(GRANULARITY, start, end):
        if gaps and grid_index(GRANULARITY, timestamp) % 2:
            continue
        bar = fake_bar(symbol, timestamp)
        expected.append((bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.trade_count))
    return expected


def _read(data_store, dataset_id: UUID) -> list[dict[str, Any]]:
    response = data_store.get(READ_PATH, params={'dataset_id': str(dataset_id)})
    assert response.status_code == 200, data_store.describe(response)
    return response.json()


def _stored(data_store, dataset_id: UUID) -> list[BarTuple]:
    """The bars data_store holds for one entry, in the read's timestamp order."""
    stored = []
    for bar in _read(data_store, dataset_id):
        data = bar['data']
        stored.append(
            (
                _instant(bar['timestamp']),
                data['open'],
                data['high'],
                data['low'],
                data['close'],
                data['volume'],
                data['trade_count'],
            )
        )
    return stored


def _listing(data_store, symbol: str) -> dict[str, dict[str, Any]]:
    """GET /store for the symbol, every owner: entry id -> listed entry."""
    response = data_store.get(_store_path(symbol))
    assert response.status_code == 200, data_store.describe(response)
    return {entry['id']: entry for entry in response.json()}


def _diff(stored: list[BarTuple], expected: list[BarTuple]) -> str:
    """A short account of how two bar lists differ, for an assertion message."""
    stored_ts, expected_ts = {bar[0] for bar in stored}, {bar[0] for bar in expected}
    changed = sorted(bar[0] for bar in set(stored) - set(expected) if bar[0] in expected_ts)
    return (
        f'stored {len(stored)} bars, expected {len(expected)}; '
        f'extra {sorted(stored_ts - expected_ts)[:5]}, missing {sorted(expected_ts - stored_ts)[:5]}, '
        f'values differ at {changed[:5]}'
    )


# ---------------------------------------------------------------------------------------------
# The default scenario.


def test_default_round_trip_stores_exactly_the_generated_bars(
    own_symbol: str, run_identity, data_store, adopt_entries: Callable[[str], list[UUID]]
) -> None:
    """Default round trip, with the RIGHT secret and a 2xx: this closes 7c for the dataset POST.

    The pipeline loses, adds and changes nothing: the bars read back under the new entry are the
    generator's for [start, end), timestamps preserved, each labelled with FakeRead's feed (IEX),
    and data_points and the listing's item_count both equal the count.
    """
    expected = _expected(own_symbol)

    response, ids = _post(data_store, adopt_entries, own_symbol, _body(run_identity.owner))

    assert response.status_code == 200, data_store.describe(response)
    assert len(ids) == 1, f'expected one entry for {own_symbol}, found {ids}'
    assert response.json()['data_points'] == len(expected), data_store.describe(response)
    bars = _read(data_store, ids[0])
    assert {bar['feed'] for bar in bars} == {Feed.IEX.value}, f'feeds read back: {sorted({b["feed"] for b in bars})}'
    assert {bar['dataset_id'] for bar in bars} == {str(ids[0])}, 'a bar read back under another dataset id'
    stored = _stored(data_store, ids[0])
    assert stored == expected, _diff(stored, expected)
    assert _listing(data_store, own_symbol)[str(ids[0])]['item_count'] == len(expected)


# ---------------------------------------------------------------------------------------------
# FakeRead's scenarios.


def test_empty_scenario_is_2xx_with_an_entry_and_no_bars(
    own_symbol: str, run_identity, data_store, scenario_entries: Callable[[str], list[UUID]]
) -> None:
    symbol = f'{EMPTY_PREFIX}{own_symbol}'

    response, ids = _post(data_store, scenario_entries, symbol, _body(run_identity.owner))

    assert _is_2xx(response), data_store.describe(response)
    assert response.json()['data_points'] == 0, data_store.describe(response)
    assert len(ids) == 1, f'expected one entry for {symbol}, found {ids}'
    assert _read(data_store, ids[0]) == []
    assert _listing(data_store, symbol)[str(ids[0])]['item_count'] == 0


def test_gaps_scenario_stores_exactly_the_served_bars(
    own_symbol: str, run_identity, data_store, scenario_entries: Callable[[str], list[UUID]]
) -> None:
    """Only the bars FakeRead served are stored: nothing fabricated to fill the holes."""
    symbol = f'{GAPS_PREFIX}{own_symbol}'
    expected = _expected(symbol, gaps=True)
    assert 0 < len(expected) < len(_expected(symbol)), 'the range holds no gap to fill; the test proves nothing'

    response, ids = _post(data_store, scenario_entries, symbol, _body(run_identity.owner))

    assert _is_2xx(response), data_store.describe(response)
    assert response.json()['data_points'] == len(expected), data_store.describe(response)
    assert len(ids) == 1, f'expected one entry for {symbol}, found {ids}'
    stored = _stored(data_store, ids[0])
    assert stored == expected, _diff(stored, expected)


def test_fail_scenario_is_not_2xx_and_stores_no_bars(
    own_symbol: str, run_identity, data_store, scenario_entries: Callable[[str], list[UUID]]
) -> None:
    symbol = f'{FAIL_PREFIX}{own_symbol}'

    response, ids = _post(data_store, scenario_entries, symbol, _body(run_identity.owner))

    assert not _is_2xx(response), data_store.describe(response)
    for entry_id in ids:
        assert _read(data_store, entry_id) == [], f'bars stored under {entry_id} after a failed fetch'


def test_slow_scenario_is_not_2xx_and_the_late_answer_is_dropped(
    own_symbol: str, run_identity, data_store, scenario_entries: Callable[[str], list[UUID]]
) -> None:
    """SLOW_ outlives data_store's RPC deadline. Read again once FakeRead has answered: still no bars.

    The wait assumes the stack runs FakeRead's default delay (FAKE_READ_SLOW_SECONDS unset, as in
    docker-compose.fake.yaml; data_ingest's banner states the delay in force).
    """
    symbol = f'{SLOW_PREFIX}{own_symbol}'
    sent = time.monotonic()

    response, ids = _post(data_store, scenario_entries, symbol, _body(run_identity.owner))

    assert not _is_2xx(response), data_store.describe(response)
    assert len(ids) == 1, f'expected one entry for {symbol}, found {ids}'
    assert _read(data_store, ids[0]) == [], 'bars stored before the slow answer could have arrived'
    time.sleep(max(0.0, sent + DEFAULT_SLOW_DELAY_SECONDS + SLOW_MARGIN_SECONDS - time.monotonic()))
    late = _read(data_store, ids[0])
    assert late == [], f'{len(late)} bars stored from an RPC answer that arrived after the deadline'


def test_failonce_scenario_fails_then_the_identical_post_fills_the_same_entry(
    own_symbol: str, run_identity, data_store, scenario_entries: Callable[[str], list[UUID]]
) -> None:
    symbol = f'{FAILONCE_PREFIX}{own_symbol}'
    body = _body(run_identity.owner)
    expected = _expected(symbol)

    first, first_ids = _post(data_store, scenario_entries, symbol, body)

    assert not _is_2xx(first), data_store.describe(first)
    assert len(first_ids) == 1, f'expected one entry for {symbol} after the failed POST, found {first_ids}'
    assert _read(data_store, first_ids[0]) == [], 'bars stored by the failed first POST'

    second, second_ids = _post(data_store, scenario_entries, symbol, body)

    assert second.status_code == 200, data_store.describe(second)
    assert second_ids == first_ids, f'the retry resolved to {second_ids}, not the first entry {first_ids}'
    assert second.json()['data_points'] == len(expected), data_store.describe(second)
    stored = _stored(data_store, first_ids[0])
    assert stored == expected, _diff(stored, expected)


# ---------------------------------------------------------------------------------------------
# Item 4: identity.


def test_identity_repeat_resolves_and_policy_or_owner_change_makes_a_new_entry(
    own_symbol: str, run_identity, data_store, adopt_entries: Callable[[str], list[UUID]]
) -> None:
    body = _body(run_identity.owner)
    expected = _expected(own_symbol)

    first, after_first = _post(data_store, adopt_entries, own_symbol, body)
    repeat, after_repeat = _post(data_store, adopt_entries, own_symbol, body)

    assert first.status_code == 200, data_store.describe(first)
    assert repeat.status_code == 200, data_store.describe(repeat)
    assert len(after_first) == 1, f'expected one entry after the first POST, found {after_first}'
    assert after_repeat == after_first, f'the exact repeat added an entry: {after_first} -> {after_repeat}'
    original = after_first[0]
    stored = _stored(data_store, original)
    assert stored == expected, f'after the repeat: {_diff(stored, expected)}'

    policy, after_policy = _post(
        data_store, adopt_entries, own_symbol, {**body, 'expiry_type': ExpiryType.BUFFER_1K.name}
    )
    assert policy.status_code == 200, data_store.describe(policy)
    assert len(after_policy) == 2 and after_policy[0] == original, f'expiry_type change: {after_policy}'
    by_policy = after_policy[1]

    other_owner = run_identity.alt_owner('identity')
    owner, after_owner = _post(data_store, adopt_entries, own_symbol, {**body, 'owner': other_owner})
    assert owner.status_code == 200, data_store.describe(owner)
    assert len(after_owner) == 3 and after_owner[:2] == after_policy, f'owner change: {after_owner}'
    by_owner = after_owner[2]

    listed = _listing(data_store, own_symbol)
    kinds = {entry_id: listed[str(entry_id)]['expiry_type'] for entry_id in after_owner}
    # No policy merge: the original keeps the weaker BULK it asked for, beside the BUFFER_1K entry.
    assert kinds == {
        original: ExpiryType.BULK.name,
        by_policy: ExpiryType.BUFFER_1K.name,
        by_owner: ExpiryType.BULK.name,
    }, kinds
    for entry_id in (original, by_policy, by_owner):
        stored = _stored(data_store, entry_id)
        assert stored == expected, f'entry {entry_id}: {_diff(stored, expected)}'


# ---------------------------------------------------------------------------------------------
# Item 5, the success half: another owner's overlap is its own dataset.


def test_another_owners_overlapping_range_is_a_separate_entry_with_its_own_bars(
    own_symbol: str, run_identity, data_store, adopt_entries: Callable[[str], list[UUID]]
) -> None:
    a_start, a_end = START, START + timedelta(minutes=10)
    b_start, b_end = START + timedelta(minutes=5), START + timedelta(minutes=15)
    other_owner = run_identity.alt_owner('overlap')

    a_response, after_a = _post(data_store, adopt_entries, own_symbol, _body(run_identity.owner, a_start, a_end))
    b_response, after_b = _post(data_store, adopt_entries, own_symbol, _body(other_owner, b_start, b_end))

    assert a_response.status_code == 200, data_store.describe(a_response)
    assert b_response.status_code == 200, data_store.describe(b_response)
    assert len(after_b) == 2 and after_b[0] == after_a[0], f'entries: {after_a} then {after_b}'
    a_id, b_id = after_b
    a_stored, b_stored = _stored(data_store, a_id), _stored(data_store, b_id)
    a_expected, b_expected = _expected(own_symbol, a_start, a_end), _expected(own_symbol, b_start, b_end)
    assert a_stored == a_expected, f'entry A: {_diff(a_stored, a_expected)}'
    assert b_stored == b_expected, f'entry B: {_diff(b_stored, b_expected)}'
    shared = {bar[0] for bar in a_stored} & {bar[0] for bar in b_stored}
    assert shared == {b_start + timedelta(minutes=m) for m in range(5)}, f'instants held by both: {sorted(shared)}'


# ---------------------------------------------------------------------------------------------
# Item 8, the HTTP half: more bars than one statement can bind.


def test_a_range_larger_than_one_bind_limited_statement_lands_whole(
    own_symbol: str, run_identity, data_store, adopt_entries: Callable[[str], list[UUID]]
) -> None:
    count = POSTGRES_MAX_BIND_PARAMETERS // StockMarketActivity.bind_params_per_row() + 1
    end = START + count * GRANULARITY.offset
    expected = _expected(own_symbol, START, end)
    assert len(expected) == count

    response, ids = _post(data_store, adopt_entries, own_symbol, _body(run_identity.owner, START, end))

    assert response.status_code == 200, data_store.describe(response)
    assert response.json()['data_points'] == count, data_store.describe(response)
    assert len(ids) == 1, f'expected one entry for {own_symbol}, found {ids}'
    stored = _stored(data_store, ids[0])
    assert stored == expected, _diff(stored, expected)


# ---------------------------------------------------------------------------------------------
# Item 13: expiry on the create body.


def test_aware_non_utc_expiry_reads_back_as_the_same_instant(
    own_symbol: str, run_identity, data_store, adopt_entries: Callable[[str], list[UUID]]
) -> None:
    # +05:30: neither UTC nor the client's America/Toronto, so neither a dropped offset nor a
    # session-zone reinterpretation could land on the same instant by accident.
    expiry = datetime(2031, 3, 4, 5, 6, 7, 123456, tzinfo=ZoneInfo('Asia/Kolkata'))

    response, ids = _post(data_store, adopt_entries, own_symbol, _body(run_identity.owner, expiry=expiry.isoformat()))

    assert response.status_code == 200, data_store.describe(response)
    assert len(ids) == 1, f'expected one entry for {own_symbol}, found {ids}'
    listed = _listing(data_store, own_symbol)[str(ids[0])]
    assert listed['expiry'] is not None, f'no expiry listed: {listed}'
    assert _instant(listed['expiry']) == expiry, f'expiry {listed["expiry"]} is not {expiry.isoformat()}'


def test_naive_expiry_is_refused_at_the_edge_and_creates_nothing(
    own_symbol: str, run_identity, data_store, adopt_entries: Callable[[str], list[UUID]]
) -> None:
    response, ids = _post(
        data_store, adopt_entries, own_symbol, _body(run_identity.owner, expiry='2031-03-04T05:06:07')
    )

    assert response.status_code == 422, data_store.describe(response)
    errors = [(error['loc'], error['type']) for error in response.json()['detail']]
    assert errors == [(['body', 'expiry'], 'timezone_aware')], data_store.describe(response)
    assert ids == [], f'a refused POST created entries: {ids}'
