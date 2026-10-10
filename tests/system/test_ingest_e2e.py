"""The real ingest service end to end through FakeRead, over data_store's HTTP (tj-vhboky.63, Sys-7).

Design: decision tj-j4wknb (the fake is a BrokerRead in tests/fakes, run by the test-only launcher
the fake-mode overlay starts; addendum 5: the broker range is half-open), tj-vhboky.54 DECISION 4
(what a failure test asserts), tj-vhboky.6 (identity, own overlap, no policy merge), tj-vhboky.8
(write routes), tj-1bl90i (a naive expiry is refused, not converted), ADR tj-4rr0la addendum 3 (3)
(the agent stack loads the fake overlay last, so data_ingest runs FakeRead).

THE STACK MUST BE IN FAKE MODE: data_ingest's log carries the 'FAKE BROKER:' banner. Against a
real broker these symbols are not tickers and every test here fails -- never skips.

EVERY ENTRY HERE IS CREATED BY THE DATASET POST, the one route that reaches data_ingest: data_store
upserts the entry, asks data_ingest over the FetchDataset gRPC stream, and writes the bars it gets
back -- the Kafka RPC it used to ask over went on tj-3mk3u5.11 and .12. Entry ids
are read by SQL (the POST answers with a count, not an id) and adopted for cleanup whether or not
the POST succeeded. Bars are read back over HTTP, FILTERED BY dataset_id: the store's read is
half-open [start, end) like the broker range since tj-86g751.3 (tj-j4wknb addendum 5), but two
owners' overlapping ranges still share instants, so an unfiltered read would mix the other
entry's bars into this entry's and make a comparison lie.

WHAT A STORED BAR MUST EQUAL is computed here from the generator, tests.fakes.market_data's
grid_timestamps and fake_bar, over the request's [start, end) -- deliberately NOT by calling
FakeRead, which the test client mounts from the same tree as the stack, so a FakeRead defect would
appear on both sides and cancel. GAPS adds the scenario's documented rule (only even grid indices
are served) through grid_index. vwap is not compared: the store's bar has no vwap column.

FAILURE CLASS ONLY NO LONGER HOLDS, AND THE PARAGRAPH THAT SAID SO WAS LEFT STANDING TOO LONG
(corrected on tj-3mk3u5.16). It read: "A failure is asserted as NOT 2xx, never as a particular
status code ... Today a FakeRead failure becomes the bare {} at ingest's RPC edge and data_store's
5 s RPC deadline expires; that is not pinned here." BOTH HALVES ARE NOW FALSE. tj-3mk3u5.11 deleted
the edge that turned a BarsFailure into the bare {}, and tj-3mk3u5.10 replaced the 5 s deadline, so
every failure here arrives as a TYPED one and the status is the thing a caller branches on. The
tests pin the exact status accordingly (tj-xhcoyc, TE-7): 503 for a vendor or peer that is not
there, 422 for a refusal the caller can fix. The reason is pinned beside it wherever the body is
problem+json, because a reason's row can change the status while the reason stays the same.

Items, tj-vhboky.14 numbering where they moved here from Sys-4:
  * default round trip -- bars equal the generator's for the range, data_points equals the count,
    and the RESOLVED tape is recorded on BOTH the bars and the entry (tj-3mk3u5.16 A2). Sent with
    the RIGHT secret and asserted 2xx: this CLOSES 7c FOR THE DATASET POST (Sys-3 asserted its 409
    half; architect note 16:51 UTC 2026-09-28 on tj-vhboky.63).
  * an unservable feed -- a POST naming a tape this deployment cannot serve is a 422
    FEED_NOT_AVAILABLE naming the refused tape, and writes no entry and no bar (tj-3mk3u5.16 A2).
  * EMPTY, GAPS, FAIL, SLOW, FAILONCE -- one test each, symbols carrying the scenario's prefix.
  * 4 -- an exact repeat resolves to the same entry, adds none, and answers with the SAME BODY
    (tj-gp5xxi: data_points and served_range, not just a 200); changing only expiry_type, then
    only owner, each makes a new entry; no entry takes the stronger expiry_type of the two. Feed IS
    part of an entry's identity since c4a1f7b2e905, but it cannot be varied from here: the entry
    records the tape the ACK resolved, and this deployment resolves one. Varying it is the unit
    tier's (data/store/tests/test_dataset_entry_identity.py).
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
from common.errors.vocabulary import Reason
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
from tests.fakes.market_data import FEED as FAKE_DEPLOYMENT_FEED
from tests.system.problem_json import assert_problem


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

# The tape this deployment serves, read off the fake rather than restated, so that rebuilding the
# stack with another entitlement moves both halves of the feed pair together.
SERVED_FEED = FAKE_DEPLOYMENT_FEED
# A tape it does not serve. FakeRead refuses any named feed that is not its deployment feed, so
# this is unservable by construction rather than by configuration. Restated rather than derived
# from Feed, because a derived "any other member" silently becomes vacuous the day the enum has
# one member; the guard below is what keeps the pair honest instead.
UNSERVABLE_FEED = Feed.SIP

# SLOW_MARGIN_SECONDS IS GONE (tj-xhcoyc). It padded a second read taken after FakeRead's SLOW_
# delay, to catch a late RPC answer arriving once data_store had already given up on it. There is
# no giving up to observe any more: the Kafka RPC's 5 s deadline became
# DEFAULT_FETCH_DEADLINE_S = 300 s under tj-3mk3u5.10, so an 8 s vendor is served rather than
# abandoned, and the SLOW case now asserts it completes. See that test for what is no longer
# provable at this tier and where it is pinned instead.


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

    THE RESOLVED FEED IS ASSERTED ON THE ENTRY AS WELL AS ON THE BARS (tj-3mk3u5.16 A2). The bar
    assertion alone was the weaker half of the pair: a bar's feed is stamped from FetchAccepted
    by the mapper, so bars could carry the resolved tape while the entry carried something else
    entirely, and nothing here would have noticed. The entry's feed is the one tj-3mk3u5.31 made
    a NOT NULL identity column, and the one a caller filters on, so it is the one that has to be
    the resolution rather than a preference. This POST sends NO feed at all -- the ordinary
    request, meaning "the deployment decides" -- which is what makes the stored value evidence of
    a resolution: there was no preference for it to have been copied from.
    """
    expected = _expected(own_symbol)

    response, ids = _post(data_store, adopt_entries, own_symbol, _body(run_identity.owner))

    assert response.status_code == 200, data_store.describe(response)
    assert len(ids) == 1, f'expected one entry for {own_symbol}, found {ids}'
    assert response.json()['data_points'] == len(expected), data_store.describe(response)
    bars = _read(data_store, ids[0])
    assert {bar['feed'] for bar in bars} == {SERVED_FEED.value}, f'feeds read back: {sorted({b["feed"] for b in bars})}'
    assert {bar['dataset_id'] for bar in bars} == {str(ids[0])}, 'a bar read back under another dataset id'
    stored = _stored(data_store, ids[0])
    assert stored == expected, _diff(stored, expected)
    listed = _listing(data_store, own_symbol)[str(ids[0])]
    assert listed['item_count'] == len(expected)
    assert listed['feed'] == SERVED_FEED.value, f'the entry was stored with feed {listed["feed"]!r}'


def test_an_unservable_feed_is_refused_and_leaves_no_entry_and_no_bar(
    own_symbol: str, run_identity, data_store, adopt_entries: Callable[[str], list[UUID]]
) -> None:
    """A POST naming a tape this deployment cannot serve is refused, and writes nothing.

    THE ONE IN-BAND FAILURE OF THE FETCH STREAM, which is why it earns a system test rather than
    a unit one. FEED_NOT_AVAILABLE never travels as a gRPC status: data_ingest answers it on the
    FetchDataset ack's refused arm and that ack is the stream's only event (tj-3mk3u5.22 Q5;
    common/rpc/ingest.py, schemas/data_ingest/fetch_dataset.py::FetchRefused). So this is the one
    failure that reaches data_store as a successful stream carrying a refusal, and the only place
    the store has to turn one into a 4xx. Every other failure arrives as an error already.

    NOTHING WRITTEN IS THE SECOND HALF AND IS NOT A RESTATEMENT OF THE FAIL SCENARIO. Both end
    with no entry, but for different reasons that could regress apart: FAIL_ never produces an
    ack at all, while this one produces an ack whose refused arm the store must read BEFORE it
    upserts. The entry is upserted on the ACCEPTED ack (tj-3mk3u5.10), so an implementation that
    wrote the entry first and read the arm second would pass the FAIL case and fail this one --
    and it would store the caller's PREFERENCE as though it were a resolution, which is the exact
    placeholder tj-3mk3u5.31 made impossible by making feed a NOT NULL identity column. No bar
    can survive either: a bar has no dataset entry to point at.

    THE REFUSED TAPE IS NAMED IN THE BODY, not just in its prose. feed is an allowlisted metadata
    key (common/errors METADATA_KEYS) and becomes a member of the same name, so a caller can read
    which tape was refused without parsing detail -- which tj-8feral ruled is prose.
    """
    assert UNSERVABLE_FEED is not SERVED_FEED, (
        f'this test asks for {UNSERVABLE_FEED.value}, the tape the fake serves, so it proves nothing'
    )

    response, ids = _post(data_store, adopt_entries, own_symbol, _body(run_identity.owner, feed=UNSERVABLE_FEED.value))

    problem = assert_problem(
        response,
        status=422,
        reason=Reason.FEED_NOT_AVAILABLE.value,
        title='Unprocessable Entity',
        describe=data_store.describe,
    )
    assert problem['feed'] == UNSERVABLE_FEED.value, data_store.describe(response)
    assert ids == [], f'a refused feed left {len(ids)} entry/entries for {own_symbol}: {ids}'


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
    """A failed fetch leaves NO entry and no bars: the whole POST is one transaction that rolls back.

    RE-PINNED TO THE RULED ORDER (tj-xhcoyc, item 2). This case used to assert the opposite --
    "the entry is upserted before the fetch, so a failed fetch leaves exactly one entry" -- and it
    was CORRECT when d1e363b wrote it. tj-3mk3u5.10 then reversed the order: the store opens the
    FetchDataset stream FIRST and upserts the entry on the ACK, because only the ack carries the
    resolved feed, and the entry, every page and the commit live in ONE transaction that commits
    after FetchDone. A fetch that fails before the ack therefore writes nothing at all. The
    assertion was never re-pinned because nothing ran this suite between the cutover and
    tj-3mk3u5.12's gate.

    THE UNIT TIER PINS THE SAME CLAIM and is green:
    data/store/tests/test_dataset_fetch_transaction.py::test_a_refusal_before_the_ack_writes_
    nothing_at_all asserts no write statement is sent at all. What THIS tier adds is that the
    transaction really rolled back in Postgres, which no fake session can show.

    WHAT tj-3mk3u5.53's ORIGINAL POINT BECOMES. The old entry count existed so that a POST refused
    before it ever reached data_ingest could not pass with nothing asserted about the fetch. Zero
    entries no longer distinguishes those two cases, so the discrimination moves to the RESPONSE:
    a refusal that never reached ingest is a 4xx from the edge, while the FAIL scenario is served
    by data_ingest and comes back 503. Asserting the status rather than merely not-2xx is what
    keeps this case about the fetch.
    """
    symbol = f'{FAIL_PREFIX}{own_symbol}'

    response, ids = _post(data_store, scenario_entries, symbol, _body(run_identity.owner))

    assert response.status_code == 503, data_store.describe(response)
    assert ids == [], f'a fetch that failed before the ack left {len(ids)} entry/entries for {symbol}: {ids}'


def test_slow_scenario_is_served_rather_than_abandoned(
    own_symbol: str, run_identity, data_store, scenario_entries: Callable[[str], list[UUID]]
) -> None:
    """A vendor slower than the old deadline but well inside the new one is SERVED, not abandoned.

    THE NAME WAS test_slow_scenario_is_not_2xx_and_the_late_answer_is_dropped UNTIL tj-3mk3u5.16,
    which is the opposite of what the body has asserted since tj-xhcoyc reversed it. I reversed
    the assertions and left the name, so the file read as a suite where SLOW_ still failed; that
    is a defect in a test of mine, found by reading the file for A2's citations rather than by
    anything going red, and a stale name is exactly the kind a green suite never catches.

    THIS CASE NOW ASSERTS THE OPPOSITE OF WHAT IT USED TO, and the reversal is the ruling rather
    than a concession (tj-xhcoyc, item 2). It was written against data_store's Kafka RPC deadline
    of 5 s, which is why FakeRead's delay is 8 s -- tests/fakes/market_data.py still says, in the
    comment on DEFAULT_SLOW_DELAY_SECONDS, "longer than data_store's 5 s Kafka RPC deadline, so
    SLOW_ outlives the caller by default". tj-3mk3u5.10 replaced that transport, and
    common/rpc/clients/ingest_fetch.py sets DEFAULT_FETCH_DEADLINE_S to 300 s DELIBERATELY: the
    5 s deadline "abandoned requests the vendor was still serving", since one rate-limited Alpaca
    call alone can cost 10 s or more of SDK sleep (tj-6znw1h). An 8 s vendor no longer outlives
    anything.

    SO THE SCENARIO STILL EARNS ITS PLACE, measuring the thing the new deadline was chosen FOR. A
    fetch that takes 8 s completes: one entry, every bar, a 200. Had the deadline stayed at 5 s --
    or were one reintroduced somewhere between the route and the stub -- this reds immediately,
    which is the regression worth catching now that the number is large.

    WHAT IS NO LONGER PROVABLE HERE, said plainly rather than quietly dropped: that a fetch
    exceeding the deadline is abandoned and its late answer discarded. Reaching that would need a
    vendor slower than 300 s, and FakeRead refuses any delay above MAX_SLOW_DELAY_SECONDS (60 s)
    precisely so no test can sleep that long. The deadline's own behaviour is pinned where it is
    cheap -- common/tests/rpc/ drives the client directly -- and this tier pins that the deadline
    is not so tight that honest work is thrown away.

    The elapsed-time assertion is what keeps the case honest: without it a fake that ignored the
    delay entirely, or a store that answered from somewhere else, would read as success.
    """
    symbol = f'{SLOW_PREFIX}{own_symbol}'
    expected = _expected(symbol)
    sent = time.monotonic()

    response, ids = _post(data_store, scenario_entries, symbol, _body(run_identity.owner))
    elapsed = time.monotonic() - sent

    assert response.status_code == 200, data_store.describe(response)
    assert elapsed >= DEFAULT_SLOW_DELAY_SECONDS, (
        f'the POST came back in {elapsed:.1f}s, faster than the {DEFAULT_SLOW_DELAY_SECONDS}s the fake sleeps, '
        'so this run did not exercise a slow vendor at all'
    )
    assert len(ids) == 1, f'a served slow fetch left {len(ids)} entries for {symbol}, not one: {ids}'
    stored = _stored(data_store, ids[0])
    assert stored == expected, _diff(stored, expected)


def test_failonce_scenario_fails_then_the_identical_post_creates_the_entry_and_fills_it(
    own_symbol: str, run_identity, data_store, scenario_entries: Callable[[str], list[UUID]]
) -> None:
    """A failed POST leaves nothing; the identical retry then creates the entry and fills it.

    RE-PINNED TO THE RULED ORDER (tj-xhcoyc, item 2). The old version asserted the failed POST
    left one entry and the retry resolved to THAT id -- true before tj-3mk3u5.10, when the entry
    was written before the fetch. Under the ruled order the first POST rolls back entirely, so the
    retry CREATES the entry rather than filling one.

    THE RETRY CLAIM IS STRONGER AFTER THE CHANGE, NOT WEAKER, which is why this is a re-pin and
    not a deletion. The old assertion could be satisfied by a store that never cleaned up after a
    failure; this one requires that a failure leaves the database exactly as it was AND that the
    identical request then succeeds in full -- one entry, every bar. That is the real idempotency
    claim a retried POST rests on, and FAILONCE is the only scenario that can make it.
    """
    symbol = f'{FAILONCE_PREFIX}{own_symbol}'
    body = _body(run_identity.owner)
    expected = _expected(symbol)

    first, first_ids = _post(data_store, scenario_entries, symbol, body)

    assert not _is_2xx(first), data_store.describe(first)
    assert first_ids == [], f'the failed first POST left {len(first_ids)} entry/entries for {symbol}: {first_ids}'

    second, second_ids = _post(data_store, scenario_entries, symbol, body)

    assert second.status_code == 200, data_store.describe(second)
    assert len(second_ids) == 1, f'the retry left {len(second_ids)} entries for {symbol}, not one: {second_ids}'
    assert second.json()['data_points'] == len(expected), data_store.describe(second)
    stored = _stored(data_store, second_ids[0])
    assert stored == expected, _diff(stored, expected)


# ---------------------------------------------------------------------------------------------
# Item 4: identity.


def test_identity_repeat_resolves_and_policy_or_owner_change_makes_a_new_entry(
    own_symbol: str, run_identity, data_store, adopt_entries: Callable[[str], list[UUID]]
) -> None:
    """The exact repeat is indistinguishable from the first POST, in the database AND on the wire.

    THE REPEAT'S RESPONSE BODY IS ASSERTED, not just its status (tj-gp5xxi). This test used to read
    `repeat.status_code` and nothing else of the repeat's answer, which left the one observable
    tj-3mk3u5.16 Part B's U4 actually checks by hand -- "the same data_points" -- unpinned at every
    tier. The database half was never the weak one: a mutation removing either ON CONFLICT DO
    UPDATE, the entry's or the bar's, reds this test and nothing else in this file. What no test
    reached was the COUNT THE CALLER IS TOLD, and a caller retrying a POST branches on that number
    rather than on our row count. data_points is len(the bars written), not a database rowcount
    (data/store/app/database/crud/stock/asset_market_activity.py), so a repeat reports the full
    count and not zero -- and that, rather than "it is 30 either way", is the contract being
    pinned here.

    served_range is pinned beside it because the three-member response (message, data_points,
    served_range) is then asserted whole for the repeat: a repeat that answered for a different
    window would be a different answer to the same question.
    """
    body = _body(run_identity.owner)
    expected = _expected(own_symbol)

    first, after_first = _post(data_store, adopt_entries, own_symbol, body)
    repeat, after_repeat = _post(data_store, adopt_entries, own_symbol, body)

    assert first.status_code == 200, data_store.describe(first)
    assert repeat.status_code == 200, data_store.describe(repeat)
    assert first.json()['data_points'] == len(expected), data_store.describe(first)
    assert repeat.json() == first.json(), f'the exact repeat answered differently: {first.json()} then {repeat.json()}'
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

    # The per-field issues moved from a bare `detail` list to the problem+json `errors` member
    # under TE-6; loc and type are unchanged, so this is the same pin at a new address
    # (tj-3mk3u5.37.9). The envelope assertion is new: a 422 that lost its reason would have
    # passed the old one, and reason is what a caller branches on.
    body = assert_problem(
        response,
        status=422,
        reason=Reason.INVALID_REQUEST.value,
        title='Unprocessable Entity',
        describe=data_store.describe,
    )
    errors = [(error['loc'], error['type']) for error in body['errors']]
    assert errors == [(['body', 'expiry'], 'timezone_aware')], data_store.describe(response)
    assert ids == [], f'a refused POST created entries: {ids}'
    assert 'served_range' not in body, 'a refusal carries a served_range, which only a served fetch has'
