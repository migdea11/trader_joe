"""The seed scenario (data/store/seeds/scenario.py): what "synthetic" admits, and what the requests cover.

WHY THIS FILE EXISTS (validator, tj-vhboky.60). The producer's synthetic-only refusal is the one thing
between a public repository and a dump of a real database (decision tj-vhboky.55, SEED FORMAT), and
is_synthetic() is what it refuses on. The consumers (tj-vhboky.62, .65) test the producer's OUTPUT,
so a loosened pattern would pass every one of them until the day a real row went through it.

WHAT TIER THIS IS. Pure: no stack, no Postgres. The requests are checked against the store's real
inbound contract (StoreAssetDatasetBody, StoreAssetDatasetPath) and against the store's own-overlap
rule, restated here from data/store/app/database/crud/stock/store_dataset_entry.py _find_own_overlap
(half-open [start, end), an exact repeat excluded) -- that restatement is the one place this file
re-derives production logic, because the rule lives in an async SQL query. NOT PROVED HERE: that the
real ingest serves the scenario, or that a POST answers 200 (tj-vhboky.65, through the MCP).
"""

import json
from itertools import combinations

import pytest

from common.enums.data_stock import ExpiryType
from data.store.app.database.crud.stock.store_dataset_entry import _OVERLAP_EQUALITY_COLUMNS
from data.store.seeds import scenario
from data.store.seeds.scenario import (
    OWNER_PATTERN,
    SEED_EXPIRY,
    SEED_REQUESTS,
    SYMBOL_PATTERN,
    SeedRequest,
    is_synthetic,
)
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath
from tests.fakes import market_data


pytestmark = pytest.mark.data_store


# ---------------------------------------------------------------------------------------------
# The refusal's patterns
# ---------------------------------------------------------------------------------------------


def test_the_scenario_prefixes_are_the_fakes_own_objects():
    """Imported, not retyped (tj-vhboky.60 item 1): a renamed prefix in the fake moves the pattern with it."""
    assert scenario.EMPTY_PREFIX is market_data.EMPTY_PREFIX
    assert scenario.GAPS_PREFIX is market_data.GAPS_PREFIX


@pytest.mark.parametrize(
    'symbol',
    ['ZZSEEDA', 'ZZSEEDAA', 'ZZSEEDAAA', f'{market_data.EMPTY_PREFIX}ZZSEEDEE', f'{market_data.GAPS_PREFIX}ZZSEEDGG'],
)
def test_synthetic_symbols_are_admitted(symbol):
    assert is_synthetic(symbol)
    assert is_synthetic(symbol, 'seed-owner-a')


@pytest.mark.parametrize(
    'symbol',
    [
        'AAPL',
        'SPY',
        'XIC.TO',
        '',
        'ZZSEED',  # no suffix letter
        'ZZSEEDAAAA',  # four suffix letters
        'zzseedaa',  # case matters
        'ZZSEEDA1',
        'ZZSYSAA',  # the system suite's run identity is not a seed symbol
        ' ZZSEEDAA',
        'ZZSEEDAA ',
        'ZZSEEDAA\n',
        'XZZSEEDAA',
        'AAPLZZSEEDAA',
        f'{market_data.EMPTY_PREFIX}{market_data.GAPS_PREFIX}ZZSEEDAA',  # at most one prefix
        f'{market_data.FAIL_PREFIX}ZZSEEDAA',  # a scenario the seed does not use
        f'{market_data.SLOW_PREFIX}ZZSEEDAA',
        f'{market_data.FAILONCE_PREFIX}ZZSEEDAA',
        'EMPTYZZSEEDAA',  # the prefix without its separator
        'ZZSEEDAA|X',
    ],
)
def test_other_symbols_are_refused(symbol):
    assert not is_synthetic(symbol)
    assert not is_synthetic(symbol, 'seed-owner-a')


@pytest.mark.parametrize(
    'prefix',
    [
        prefix
        for prefix in market_data.SCENARIO_PREFIXES
        if prefix not in (market_data.EMPTY_PREFIX, market_data.GAPS_PREFIX)
    ],
)
def test_every_scenario_prefix_the_seed_does_not_use_is_refused(prefix):
    """Enumerated from the fake, so a scenario added later (RATELIMIT_, TE-5 tj-3mk3u5.37.6) is covered too."""
    assert not is_synthetic(f'{prefix}ZZSEEDAA')
    assert not is_synthetic(f'{prefix}ZZSEEDAA', 'seed-owner-a')


@pytest.mark.parametrize(
    'owner',
    [
        '',
        'seed-owner-',
        'seed-owner-ab',
        'seed-owner-A',
        'seed-owner-1',
        'Seed-owner-a',
        'seed-owner-a\n',
        'miguel',
        'unassigned',
        ' seed-owner-a',
        'seed-owner-a ',
    ],
)
def test_other_owners_are_refused(owner):
    """'unassigned' included: it is the column's server default, i.e. what a pre-owner row migrates to."""
    assert not is_synthetic('ZZSEEDAA', owner)


def test_a_row_without_an_owner_column_is_judged_on_its_symbol_alone():
    """Bars carry no owner: None means 'no such column', never 'any owner'."""
    assert is_synthetic('ZZSEEDAA', None)
    assert not is_synthetic('AAPL', None)


def test_the_patterns_are_anchored():
    """Fullmatch is what is_synthetic uses; the patterns are anchored too, so a switch to search() stays safe."""
    assert SYMBOL_PATTERN.search('AAPL ZZSEEDAA') is None
    assert OWNER_PATTERN.search('x seed-owner-a') is None


# ---------------------------------------------------------------------------------------------
# The requests
# ---------------------------------------------------------------------------------------------


def test_every_request_is_synthetic():
    """The producer would refuse its own scenario otherwise."""
    for request in SEED_REQUESTS:
        assert is_synthetic(request.symbol, request.owner), request


def test_every_request_passes_the_stores_inbound_contract():
    """Body and path, as the wire carries them (JSON), through the store's real models."""
    for request in SEED_REQUESTS:
        body = StoreAssetDatasetBody.model_validate_json(json.dumps(request.body()))
        assert body.start == request.start
        assert body.end == request.end
        assert body.expiry == SEED_EXPIRY
        assert body.expiry_type is request.expiry_type
        _, _, asset_type, data_type, symbol = request.path.split('/')
        path = StoreAssetDatasetPath(asset_type=asset_type, data_type=data_type, asset_symbol=symbol)
        # The store upper-cases the symbol; a scenario symbol that changed under it would dump a
        # value the refusal was never asked about.
        assert path.asset_symbol == request.symbol


def test_every_body_names_the_expiry_and_is_the_same_on_every_call():
    """The body's own default expiry is now() plus a day: sending it is what keeps two runs equal."""
    for request in SEED_REQUESTS:
        first, second = request.body(), request.body()
        assert first == second
        assert first['expiry'] == SEED_EXPIRY.isoformat()
        assert all(isinstance(value, str) for value in first.values())


def test_every_range_is_aware_utc_and_non_empty():
    for request in SEED_REQUESTS:
        assert request.start.utcoffset() is not None and request.start.utcoffset().total_seconds() == 0
        assert request.end.utcoffset() is not None and request.end.utcoffset().total_seconds() == 0
        assert request.start < request.end, request


def _identity(request: SeedRequest) -> tuple:
    """The request's non-range identity, over the columns the store's own-overlap check compares.

    THE OWN-OVERLAP KEY, NOT THE IDENTITY KEY, and the two stopped being the same tuple when feed
    joined identity (tj-xn3qa6 D1, tj-3mk3u5.31). This function has always been about predicting a
    409, which the docstring above said before the split and still says -- so it follows the
    REFUSAL's eight columns. Keyed on identity instead it would read a tape off a body that has
    none and raise KeyError, which is how this file found out the keys had diverged.

    The seed requests name no feed, which costs this nothing: the refusal never reads one, and the
    tape the entries end up recording is whatever the deployment resolves. If a seed request ever
    DOES name a preference, note that two requests differing only in it would look like an exact
    repeat to test_no_request_is_repeated_exactly below -- correctly, for the refusal's purposes,
    and wrongly for the entry key's.
    """
    body = request.body()
    values = {
        'owner': request.owner,
        'asset_symbol': request.symbol,
        'asset_type': request.path.split('/')[2],
        'data_type': request.path.split('/')[3],
        'source': body['source'],
        'granularity': body['granularity'],
        'expiry_type': body['expiry_type'],
        'update_type': body['update_type'],
    }
    return tuple(values[column] for column in _OVERLAP_EQUALITY_COLUMNS)


def test_the_restated_identity_covers_every_equality_column():
    """_identity above must know every column the store compares, or the overlap test below is blind.

    Length is the weaker half and the dict lookup above is the stronger one: a column added to the
    refusal that `values` does not carry raises KeyError before this assertion is reached. What the
    length catches is the opposite -- a column REMOVED from the refusal, which would leave
    `values` carrying a term the store no longer compares and the overlap prediction below
    narrower than the store's.
    """
    assert len(_identity(SEED_REQUESTS[0])) == len(_OVERLAP_EQUALITY_COLUMNS)


def test_no_request_collides_with_the_same_owners_other_requests():
    """The store answers 409 to a same-identity overlap (half-open [start, end), exact repeat excluded).

    Half-open since tj-vhboky.1 addendum HALF-OPEN RANGES (2026-09-30), item 2: two ranges that only
    touch share no bar and do not collide. The closed restatement this replaced was stricter than the
    store, so it stayed green; it would have flagged an adjacent pair the store accepts.
    """
    for a, b in combinations(SEED_REQUESTS, 2):
        if _identity(a) != _identity(b):
            continue
        exact = a.start == b.start and a.end == b.end
        overlaps = a.start < b.end and a.end > b.start
        assert exact or not overlaps, f'same-owner overlap would 409: {a} / {b}'


def test_no_request_is_repeated_exactly():
    """An exact repeat returns the existing id and seeds nothing new -- a wasted line, or a typo."""
    keys = [(_identity(r), r.start, r.end) for r in SEED_REQUESTS]
    assert len(keys) == len(set(keys))


def test_two_owners_hold_the_same_range_of_a_default_symbol():
    """E4 (tj-vhboky.55): both old keys collide only if both entries hold bars at the same instants."""
    pairs = [
        (a, b)
        for a, b in combinations(SEED_REQUESTS, 2)
        if a.owner != b.owner
        and a.symbol == b.symbol
        and a.granularity == b.granularity
        and (a.start, a.end) == (b.start, b.end)
        and not a.symbol.startswith((market_data.EMPTY_PREFIX, market_data.GAPS_PREFIX))
    ]
    assert pairs


def test_two_owners_overlap_on_different_ranges():
    pairs = [
        (a, b)
        for a, b in combinations(SEED_REQUESTS, 2)
        if a.owner != b.owner
        and a.symbol == b.symbol
        and (a.start, a.end) != (b.start, b.end)
        and a.start < b.end
        and b.start < a.end
    ]
    assert pairs


def test_two_entries_differ_only_in_expiry_type():
    pairs = [
        (a, b)
        for a, b in combinations(SEED_REQUESTS, 2)
        if a.expiry_type != b.expiry_type
        and (a.owner, a.symbol, a.granularity, a.start, a.end) == (b.owner, b.symbol, b.granularity, b.start, b.end)
    ]
    assert pairs
    assert {ExpiryType.BULK, ExpiryType.ROLLING} <= {r.expiry_type for r in SEED_REQUESTS}


def test_one_empty_and_one_gapped_range_and_more_than_one_granularity():
    symbols = [r.symbol for r in SEED_REQUESTS]
    assert sum(s.startswith(market_data.EMPTY_PREFIX) for s in symbols) == 1
    assert sum(s.startswith(market_data.GAPS_PREFIX) for s in symbols) == 1
    assert len({r.granularity for r in SEED_REQUESTS}) > 1


def test_the_scenario_asks_for_a_few_hundred_bars():
    """'A few hundred' bars (tj-vhboky.55), bounded from above by every step of every non-EMPTY range.

    GAPS yields about half its steps, so this is an upper bound; the real count is tj-vhboky.65's.
    """
    steps = sum(
        (r.end - r.start) // r.granularity.offset
        for r in SEED_REQUESTS
        if not r.symbol.startswith(market_data.EMPTY_PREFIX)
    )
    assert 100 <= steps <= 1000, steps
