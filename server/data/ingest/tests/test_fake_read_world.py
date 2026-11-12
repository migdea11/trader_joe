"""FakeRead's P14 scenarios: the world (asset days, lookups, lineage bars, corporate actions), NOTFOUND_ and REUSED_.

Bead tj-7zjov1.24. Design: the bead's acceptance (an asset list over successive days with a delisting, a rename and a
reused ticker, corporate actions including a revision), the architect's notes on it (asset rows with names and one
empty one; active and inactive lists by status; name_change records with process_date only for FB to META, META to
METV and SQ to XYZ; the XLNX stock_merger with both dates and rate 1.7234; the asset-by-symbol vendor id with SQ not
found; bars that follow the current holder's lineage then the ticker; placeholder bars; a different vendor id for the
same symbol on a later call; a name_change list for the P51 clamp, FB clamped to 2022-06-09 and META not), the s3
findings on tj-7zjov1.7 (a delisting is a disappearance from both lists, WPRT and BUUU's old id among them; zero flips
between the lists; renames keep the id; a relisting under a new id; the inactive list drops an asset that still
exists), the P12 gate ruling (the default answers are unchanged: new behaviour is a prefix or an opt-in), and the
architect gate of 2026-10-26 17:24 (R1 to R3: no flip presented as replay; the recorded placeholder's vwap 0; the
illustrative parts labelled).

REPLAYED versus INVENTED versus ILLUSTRATIVE, as the fake labels them: the renames, the delistings, the reuses, the
dropped inactive entry, the lineages of META, FB, SQ and XYZ and the four recorded corporate actions are replayed;
the status flip (FLIPX) and the AAPL dividend revision are invented; the asset names and exchanges, WCCP's empty
name, the PARA run's dates and its single lineage are illustrative; a delisted symbol found inactive by symbol is one
of the two recorded answers (ABMD and TWTR 200 inactive, SIVB 404).

Expected values are written out where the test is about them.
"""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from common.enums.data_select import AssetType
from common.enums.data_stock import Granularity
from common.errors.vocabulary import ExogenousError, Reason
from data.ingest.app.brokers.interface import (
    AssetListQuery,
    AssetListResponse,
    AssetLookupQuery,
    AssetNotFound,
    AssetResponse,
    BarsFailure,
    BarsQuery,
    CallTally,
    CorporateActionsFailure,
    CorporateActionsQuery,
    Instrument,
)
from data.ingest.app.brokers.rate_budget import RequestPriority
from schemas.data_ingest.submit import AssetListStatus
from tests.fakes.market_data import (
    AAPL_DIVIDEND_ID,
    NOTFOUND_PREFIX,
    PARA_PLACEHOLDERS,
    REUSED_PREFIX,
    WORLD,
    WORLD_DAY_0,
    FakeRead,
    FakeWorld,
    Scenario,
    fake_asset,
    fake_bar,
    scenario_for,
)


pytestmark = pytest.mark.data_ingest

DAY = timedelta(days=1)
# Noon on each world day, and one day either side of them.
NOON = datetime(WORLD_DAY_0.year, WORLD_DAY_0.month, WORLD_DAY_0.day, 12, tzinfo=UTC)
DAY_0, DAY_1, DAY_2 = NOON, NOON + DAY, NOON + 2 * DAY
BACKFILL = RequestPriority.BACKFILL


def at(now: datetime) -> FakeRead:
    return FakeRead(clock=lambda: now, world=WORLD)


async def listed(now: datetime, status: AssetListStatus) -> list[tuple[str, str, bool]]:
    outcome = await at(now).get_assets(AssetListQuery(AssetType.STOCK, status, priority=BACKFILL))
    assert isinstance(outcome, AssetListResponse)
    return [(asset.symbol, asset.name, asset.active) for asset in outcome.assets]


async def ids(now: datetime, status: AssetListStatus) -> dict[str, str]:
    outcome = await at(now).get_assets(AssetListQuery(AssetType.STOCK, status, priority=BACKFILL))
    return {asset.vendor_asset_id: asset.symbol for asset in outcome.assets}


async def lookup(now: datetime, symbol: str, reader: FakeRead | None = None):
    return await (reader or at(now)).get_asset(AssetLookupQuery(symbol, priority=BACKFILL))


async def bars(reader: FakeRead, symbol: str, start: datetime, end: datetime) -> list:
    outcome = await reader.get_bars(
        BarsQuery(Instrument(symbol, AssetType.STOCK), Granularity.ONE_DAY, start, end, priority=BACKFILL)
    )
    assert not isinstance(outcome, BarsFailure), outcome
    return [bar async for bar in outcome.bars]


async def actions(now: datetime, symbol: str, start: datetime, end: datetime, tally: CallTally | None = None):
    outcome = await at(now).get_corporate_actions(
        CorporateActionsQuery(symbol, start, end, priority=BACKFILL, tally=tally)
    )
    assert not isinstance(outcome, CorporateActionsFailure), outcome
    return outcome


EVER = (datetime(2000, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC))


# ---------------------------------------------------------------------------------------------
# Asset days
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_day_serves_its_own_active_and_inactive_lists_in_order():
    assert [symbol for symbol, _, _ in await listed(DAY_0, AssetListStatus.ACTIVE)] == [
        'AAPL', 'FB', 'METV', 'SQ', 'AMD', 'WPRT', 'BUUU', 'FLIPX',
    ]  # fmt: skip
    assert [symbol for symbol, _, _ in await listed(DAY_0, AssetListStatus.INACTIVE)] == ['XLNX', 'WCCP']
    assert [symbol for symbol, _, _ in await listed(DAY_1, AssetListStatus.ACTIVE)] == [
        'AAPL', 'META', 'METV', 'XYZ', 'AMD',
    ]  # fmt: skip
    assert [symbol for symbol, _, _ in await listed(DAY_1, AssetListStatus.INACTIVE)] == ['XLNX', 'WCCP', 'FLIPX']
    assert [symbol for symbol, _, _ in await listed(DAY_2, AssetListStatus.ACTIVE)] == [
        'AAPL', 'META', 'METV', 'XYZ', 'AMD', 'FB', 'BUUU',
    ]  # fmt: skip
    assert [symbol for symbol, _, _ in await listed(DAY_2, AssetListStatus.INACTIVE)] == ['XLNX', 'FLIPX']


@pytest.mark.asyncio
async def test_a_clock_before_the_first_day_reads_the_first_and_after_the_last_reads_the_last():
    assert await listed(DAY_0 - 30 * DAY, AssetListStatus.ACTIVE) == await listed(DAY_0, AssetListStatus.ACTIVE)
    assert await listed(DAY_2 + 30 * DAY, AssetListStatus.ACTIVE) == await listed(DAY_2, AssetListStatus.ACTIVE)


@pytest.mark.asyncio
async def test_the_day_turns_at_midnight_utc():
    midnight = datetime(2026, 10, 27, tzinfo=UTC)

    assert 'FB' in [
        symbol for symbol, _, _ in await listed(midnight - timedelta(microseconds=1), AssetListStatus.ACTIVE)
    ]
    assert 'META' in [symbol for symbol, _, _ in await listed(midnight, AssetListStatus.ACTIVE)]


@pytest.mark.asyncio
async def test_a_rename_keeps_the_asset_id_under_the_new_symbol():
    before, after = await ids(DAY_0, AssetListStatus.ACTIVE), await ids(DAY_1, AssetListStatus.ACTIVE)
    by_symbol_before = {symbol: vendor_id for vendor_id, symbol in before.items()}

    assert after[by_symbol_before['FB']] == 'META'
    assert after[by_symbol_before['SQ']] == 'XYZ'


@pytest.mark.asyncio
@pytest.mark.parametrize('symbol', ['WPRT', 'BUUU'])
async def test_a_delisting_replayed_from_s3_disappears_from_both_lists(symbol):
    """s3: WPRT and BUUU's old id were among the six ids that left the lists.

    Each is gone from the active list with no appearance in the inactive one.
    """
    gone = {listed: vendor_id for vendor_id, listed in (await ids(DAY_0, AssetListStatus.ACTIVE)).items()}[symbol]

    for day in (DAY_1, DAY_2):
        assert gone not in await ids(day, AssetListStatus.ACTIVE)
        assert gone not in await ids(day, AssetListStatus.INACTIVE)


@pytest.mark.asyncio
async def test_no_replayed_asset_flips_between_the_lists():
    """s3 recorded zero flips between the lists: only the invented FLIPX moves from the active list to the inactive."""
    flipped = set()
    for before, after in ((DAY_0, DAY_1), (DAY_1, DAY_2)):
        was_active = await ids(before, AssetListStatus.ACTIVE)
        now_inactive = await ids(after, AssetListStatus.INACTIVE)
        flipped |= {now_inactive[vendor_id] for vendor_id in was_active if vendor_id in now_inactive}

    assert flipped == {'FLIPX'}


@pytest.mark.asyncio
async def test_an_invented_status_flip_moves_one_id_from_the_active_list_to_the_inactive_one():
    """INVENTED, not replayed (s3 saw none): kept because the store's snapshot diff (P17) must handle a flip."""
    flipx = {symbol: vendor_id for vendor_id, symbol in (await ids(DAY_0, AssetListStatus.ACTIVE)).items()}['FLIPX']

    assert flipx not in await ids(DAY_1, AssetListStatus.ACTIVE)
    assert (await ids(DAY_1, AssetListStatus.INACTIVE))[flipx] == 'FLIPX'
    assert (await lookup(DAY_1, 'FLIPX')).asset.name == 'Invented status-flip asset'


@pytest.mark.asyncio
async def test_a_reused_ticker_appears_under_a_new_id_while_the_old_id_lives_on():
    day_0 = {symbol: vendor_id for vendor_id, symbol in (await ids(DAY_0, AssetListStatus.ACTIVE)).items()}
    day_2_active = await ids(DAY_2, AssetListStatus.ACTIVE)
    day_2_by_symbol = {symbol: vendor_id for vendor_id, symbol in day_2_active.items()}

    # FB: Facebook's id is META's now; ProShares holds FB under an id never seen before.
    assert day_2_by_symbol['FB'] != day_0['FB']
    assert day_2_active[day_0['FB']] == 'META'
    # BUUU: relisted under a new id (s3); the old id is in neither list.
    assert day_2_by_symbol['BUUU'] != day_0['BUUU']
    assert day_0['BUUU'] not in day_2_active
    assert day_0['BUUU'] not in await ids(DAY_2, AssetListStatus.INACTIVE)


@pytest.mark.asyncio
async def test_the_inactive_list_drops_an_asset_that_still_exists():
    """s3: WCCP left the inactive list though a lookup still finds it."""
    assert 'WCCP' in (await ids(DAY_1, AssetListStatus.INACTIVE)).values()
    assert 'WCCP' not in (await ids(DAY_2, AssetListStatus.INACTIVE)).values()

    found = await lookup(DAY_2, 'WCCP')
    assert isinstance(found, AssetResponse)
    assert (found.asset.active, found.asset.tradable) == (False, False)


@pytest.mark.asyncio
async def test_world_assets_carry_illustrative_names_one_empty_and_their_own_json_as_the_raw_payload():
    """The names and exchanges, and WCCP's empty name, are illustrative: not taken from a capture."""
    names = {symbol: name for symbol, name, _ in await listed(DAY_0, AssetListStatus.INACTIVE)}
    assert names == {'XLNX': 'Xilinx, Inc. Common Stock', 'WCCP': ''}

    outcome = await at(DAY_0).get_assets(AssetListQuery(AssetType.STOCK, AssetListStatus.ACTIVE, priority=BACKFILL))
    fb = next(asset for asset in outcome.assets if asset.symbol == 'FB')
    assert json.loads(fb.raw_payload) == {
        'id': fb.vendor_asset_id,
        'class': 'us_equity',
        'exchange': 'NASDAQ',
        'symbol': 'FB',
        'name': 'Meta Platforms, Inc. Class A Common Stock',
        'status': 'active',
        'tradable': True,
    }


@pytest.mark.asyncio
async def test_a_world_asset_id_is_stable_across_reads_and_readers():
    assert await ids(DAY_1, AssetListStatus.ACTIVE) == await ids(DAY_1, AssetListStatus.ACTIVE)


def test_world_days_must_be_consecutive():
    first, _, third = WORLD.days
    with pytest.raises(ValueError, match='consecutive'):
        FakeWorld([first, third], {}, [])
    with pytest.raises(ValueError, match='at least one'):
        FakeWorld([], {}, [])


# ---------------------------------------------------------------------------------------------
# Asset by symbol
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('now', 'symbol', 'expected'),
    [
        pytest.param(DAY_0, 'SQ', ('SQ', True), id='sq-before-its-rename'),
        pytest.param(DAY_1, 'SQ', None, id='sq-renamed-away-is-not-found'),
        pytest.param(DAY_2, 'SQ', None, id='sq-still-not-found'),
        pytest.param(DAY_1, 'XYZ', ('XYZ', True), id='xyz-after-the-rename'),
        pytest.param(DAY_0, 'META', None, id='meta-before-anyone-holds-it'),
        pytest.param(DAY_1, 'FB', None, id='fb-between-rename-and-reuse'),
        # A delisted symbol found inactive by symbol: one of the two recorded answers (ABMD and TWTR; SIVB was 404).
        pytest.param(DAY_1, 'WPRT', ('WPRT', False), id='wprt-delisted-found-inactive-as-abmd-and-twtr'),
        pytest.param(DAY_2, 'WPRT', ('WPRT', False), id='wprt-still-found-inactive'),
        pytest.param(DAY_1, 'BUUU', ('BUUU', False), id='buuu-old-id-delisted-found-inactive'),
        pytest.param(DAY_1, 'FLIPX', ('FLIPX', False), id='invented-flipx-flipped-inactive'),
        pytest.param(DAY_0, 'XLNX', ('XLNX', False), id='xlnx-inactive'),
    ],
)
async def test_a_lookup_answers_the_days_holder_or_not_found(now, symbol, expected):
    """A delisted symbol is answered 200 inactive by symbol, as ABMD and TWTR were recorded.

    SIVB's recorded 404 is the other answer, which the fake does not serve for a delisting.
    """
    outcome = await lookup(now, symbol)

    if expected is None:
        assert outcome == AssetNotFound(symbol, now)
    else:
        assert isinstance(outcome, AssetResponse)
        assert (outcome.asset.symbol, outcome.asset.active) == expected


@pytest.mark.asyncio
async def test_a_lookup_answers_the_id_the_days_list_holds_under_the_symbol():
    """The P50 detector reads this id: FB is Facebook's on day 0 and ProShares' on day 2."""
    day_0 = {symbol: vendor_id for vendor_id, symbol in (await ids(DAY_0, AssetListStatus.ACTIVE)).items()}
    day_2 = {symbol: vendor_id for vendor_id, symbol in (await ids(DAY_2, AssetListStatus.ACTIVE)).items()}

    assert (await lookup(DAY_0, 'FB')).asset.vendor_asset_id == day_0['FB']
    assert (await lookup(DAY_2, 'FB')).asset.vendor_asset_id == day_2['FB'] != day_0['FB']
    assert (await lookup(DAY_2, 'META')).asset.vendor_asset_id == day_0['FB']
    assert (await lookup(DAY_2, 'BUUU')).asset.vendor_asset_id == day_2['BUUU'] != day_0['BUUU']


@pytest.mark.asyncio
async def test_a_symbol_the_world_does_not_list_is_answered_as_without_a_world():
    assert await lookup(DAY_1, 'VFV') == AssetResponse(asset=fake_asset('VFV'), as_of=DAY_1)
    assert isinstance(await lookup(DAY_1, f'{NOTFOUND_PREFIX}VFV'), AssetNotFound)


# ---------------------------------------------------------------------------------------------
# Lineage bars
# ---------------------------------------------------------------------------------------------


def utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


NOW = utc(2026, 10, 26)


@pytest.mark.asyncio
async def test_meta_serves_facebooks_lineage_so_its_bars_before_the_rename_equal_fbs():
    reader = FakeRead(clock=lambda: NOW, world=WORLD)
    window = (utc(2021, 7, 1), utc(2022, 2, 1))

    meta, fb = await bars(reader, 'META', *window), await bars(reader, 'FB', *window)

    assert len(meta) == (window[1] - window[0]).days
    assert meta == fb
    # Not the plain symbol's bars: the lineage is the seed.
    assert meta[0] != fake_bar('META', window[0])


@pytest.mark.asyncio
async def test_fb_serves_facebook_to_2022_06_08_nothing_until_2025_06_26_then_proshares():
    reader = FakeRead(clock=lambda: NOW, world=WORLD)

    fb = await bars(reader, 'FB', utc(2022, 6, 1), utc(2025, 6, 29))
    meta = await bars(reader, 'META', utc(2022, 6, 1), utc(2022, 6, 9))

    stamps = [bar.timestamp for bar in fb]
    assert stamps == [utc(2022, 6, d) for d in range(1, 9)] + [utc(2025, 6, d) for d in (26, 27, 28)]
    assert fb[:8] == meta
    assert fb[8].close != (await bars(reader, 'META', utc(2025, 6, 26), utc(2025, 6, 27)))[0].close


@pytest.mark.asyncio
async def test_sq_serves_blocks_bars_to_2025_01_17_and_xyz_the_same_lineage_on():
    reader = FakeRead(clock=lambda: NOW, world=WORLD)

    sq = await bars(reader, 'SQ', utc(2025, 1, 15), utc(2025, 1, 22))
    xyz = await bars(reader, 'XYZ', utc(2025, 1, 15), utc(2025, 1, 22))

    assert [bar.timestamp for bar in sq] == [utc(2025, 1, d) for d in (15, 16, 17)]
    assert sq == xyz[:3]
    assert len(xyz) == 7


@pytest.mark.asyncio
async def test_an_illustrative_para_run_of_placeholders_has_the_recorded_shape_vwap_0_inside_a_real_series():
    """The shape is the one recorded placeholder's (s4, META with asof: one flat price, v 0, n 0, vw 0).

    The run's dates, its flat price and PARA's single lineage are illustrative.
    """
    reader = FakeRead(clock=lambda: NOW, world=WORLD)
    start, end = PARA_PLACEHOLDERS

    served = await bars(reader, 'PARA', start - 2 * DAY, end + 2 * DAY)
    pads = [bar for bar in served if start <= bar.timestamp < end]
    real = [bar for bar in served if not start <= bar.timestamp < end]

    assert len(pads) == 5 and len(real) == 4
    price = pads[0].close
    assert price > 0
    assert all(
        (bar.open, bar.high, bar.low, bar.close, bar.vwap, bar.volume, bar.trade_count)
        == (price, price, price, price, 0.0, 0, 0)
        for bar in pads
    )
    assert all(bar.volume > 0 and bar.trade_count > 0 and bar.vwap > 0 for bar in real)


@pytest.mark.asyncio
async def test_without_a_world_meta_serves_its_own_plain_bars():
    reader = FakeRead(clock=lambda: NOW)

    served = await bars(reader, 'META', utc(2021, 7, 1), utc(2021, 7, 3))

    assert served == [fake_bar('META', utc(2021, 7, 1)), fake_bar('META', utc(2021, 7, 2))]


# ---------------------------------------------------------------------------------------------
# Corporate actions
# ---------------------------------------------------------------------------------------------


def summary(outcome) -> list[tuple[str, str, date | None, date | None]]:
    return [
        (
            action.action_type,
            action.vendor_action_id,
            action.process_date.date() if action.process_date else None,
            action.effective_date.date() if action.effective_date else None,
        )
        for action in outcome.actions
    ]


@pytest.mark.asyncio
async def test_fb_finds_its_rename_to_meta_with_a_process_date_only():
    """The P51 clamp's input for FB: old_symbol FB, process_date 2022-06-09, no other date."""
    outcome = await actions(DAY_0, 'FB', *EVER)

    assert summary(outcome) == [('name_changes', 'ed6f16b1-fc46-443e-8dba-898ed9007a7a', date(2022, 6, 9), None)]
    (fb,) = outcome.actions
    assert (fb.ex_date, fb.record_date, fb.payable_date) == (None, None, None)
    assert fb.identifiers == {
        'new_cusip': '30303M102',
        'new_symbol': 'META',
        'old_cusip': '30303M102',
        'old_symbol': 'FB',
    }


@pytest.mark.asyncio
async def test_meta_finds_both_names_so_its_later_arrival_can_overrule_its_earlier_departure():
    """The P51 clamp's input for META: META left (to METV, 2022-01-31) and later arrived (from FB, 2022-06-09)."""
    outcome = await actions(DAY_0, 'META', *EVER)

    assert [(action.identifiers['old_symbol'], action.identifiers['new_symbol']) for action in outcome.actions] == [
        ('FB', 'META'),
        ('META', 'METV'),
    ]
    assert [action.process_date for action in outcome.actions] == [utc(2022, 6, 9), utc(2022, 1, 31)]


@pytest.mark.asyncio
async def test_sq_and_xyz_find_the_same_rename():
    sq, xyz = await actions(DAY_0, 'SQ', *EVER), await actions(DAY_0, 'XYZ', *EVER)

    assert (
        summary(sq)
        == summary(xyz)
        == [('name_changes', '959d6941-3cb9-4b2d-a282-543a0a3f05d1', date(2025, 1, 21), None)]
    )


@pytest.mark.asyncio
async def test_the_xlnx_merger_into_amd_carries_both_dates_and_the_rate_as_served():
    outcome = await actions(DAY_0, 'XLNX', *EVER)

    assert summary(outcome) == [
        ('stock_mergers', '15459aa5-b297-4e33-9624-c9d59b75ba46', date(2022, 2, 14), date(2022, 2, 14))
    ]
    assert json.loads(outcome.actions[0].raw_payload)['acquirer_rate'] == 1.7234
    assert summary(await actions(DAY_0, 'AMD', *EVER)) == summary(outcome)


@pytest.mark.asyncio
async def test_a_recorded_record_is_served_byte_for_byte_as_the_fixture_holds_it():
    outcome = await actions(DAY_0, 'XLNX', *EVER)

    assert outcome.actions[0].raw_payload == (
        b'{"acquiree_cusip":"983919101","acquiree_rate":1,"acquiree_symbol":"XLNX","acquirer_cusip":"007903107",'
        b'"acquirer_rate":1.7234,"acquirer_symbol":"AMD","effective_date":"2022-02-14",'
        b'"id":"15459aa5-b297-4e33-9624-c9d59b75ba46","process_date":"2022-02-14"}'
    )


@pytest.mark.asyncio
async def test_a_revised_record_keeps_its_id_and_is_served_revised_from_the_next_day():
    first, revised = await actions(DAY_0, 'AAPL', *EVER), await actions(DAY_1, 'AAPL', *EVER)

    assert [action.vendor_action_id for action in first.actions] == [AAPL_DIVIDEND_ID]
    assert [action.vendor_action_id for action in revised.actions] == [AAPL_DIVIDEND_ID]
    assert (json.loads(first.actions[0].raw_payload)['rate'], first.actions[0].payable_date) == (0.25, utc(2026, 8, 14))
    assert (json.loads(revised.actions[0].raw_payload)['rate'], revised.actions[0].payable_date) == (
        0.26,
        utc(2026, 8, 15),
    )
    assert first.actions[0].action_type == revised.actions[0].action_type == 'cash_dividends'


@pytest.mark.asyncio
async def test_the_window_is_half_open_on_the_process_date():
    on_the_day = utc(2022, 6, 9)

    assert len((await actions(DAY_0, 'FB', on_the_day, on_the_day + DAY)).actions) == 1
    assert (await actions(DAY_0, 'FB', on_the_day - DAY, on_the_day)).actions == []
    assert (await actions(DAY_0, 'FB', on_the_day + DAY, utc(2030, 1, 1))).actions == []


@pytest.mark.asyncio
async def test_a_symbol_no_record_names_finds_nothing_and_the_answer_is_stamped_with_the_clock():
    outcome = await actions(DAY_2, 'VFV', *EVER)

    assert outcome.actions == []
    assert outcome.as_of == DAY_2


# ---------------------------------------------------------------------------------------------
# NOTFOUND_ and REUSED_
# ---------------------------------------------------------------------------------------------


def test_the_new_prefixes_select_their_scenarios():
    assert scenario_for(f'{NOTFOUND_PREFIX}VFV') is Scenario.NOTFOUND
    assert scenario_for(f'{REUSED_PREFIX}VFV') is Scenario.REUSED


@pytest.mark.asyncio
async def test_notfound_serves_bars_but_its_lookup_is_not_found():
    reader = FakeRead(clock=lambda: NOW)
    symbol = f'{NOTFOUND_PREFIX}VFV'

    served = await bars(reader, symbol, utc(2026, 1, 1), utc(2026, 1, 3))

    assert served == [fake_bar(symbol, utc(2026, 1, 1)), fake_bar(symbol, utc(2026, 1, 2))]
    assert await lookup(NOW, symbol, reader) == AssetNotFound(symbol, NOW)


@pytest.mark.asyncio
async def test_reused_answers_the_first_holder_once_then_another_id_on_every_later_lookup():
    reader = FakeRead(clock=lambda: NOW)
    symbol = f'{REUSED_PREFIX}VFV'

    answers = [(await lookup(NOW, symbol, reader)).asset for _ in range(3)]

    assert answers[0] == fake_asset(symbol)
    assert answers[1] == answers[2] == fake_asset(symbol, holder=2)
    assert answers[1].vendor_asset_id != answers[0].vendor_asset_id
    assert answers[1].symbol == symbol


@pytest.mark.asyncio
async def test_reused_memory_is_the_instances_and_per_symbol():
    first, second = FakeRead(clock=lambda: NOW), FakeRead(clock=lambda: NOW)
    a, b = f'{REUSED_PREFIX}A', f'{REUSED_PREFIX}B'

    await lookup(NOW, a, first)

    assert (await lookup(NOW, a, second)).asset == fake_asset(a)
    assert (await lookup(NOW, b, first)).asset == fake_asset(b)
    assert (await lookup(NOW, a, first)).asset == fake_asset(a, holder=2)


def test_a_holder_is_counted_from_one_and_holder_one_is_the_default_record():
    assert fake_asset('VFV', holder=1) == fake_asset('VFV')
    with pytest.raises(ValueError, match='from 1'):
        fake_asset('VFV', holder=0)


# ---------------------------------------------------------------------------------------------
# Pacing and the call log
# ---------------------------------------------------------------------------------------------


class Pacer:
    def __init__(self, refuse: bool = False) -> None:
        self.asked: list[tuple[str, RequestPriority, datetime | None]] = []
        self.refuse = refuse

    async def acquire(self, scope, priority=RequestPriority.INTERACTIVE, deadline=None) -> None:
        self.asked.append((scope, priority, deadline))
        if self.refuse:
            raise ExogenousError(
                Reason.RATE_BUDGET, 'no token', metadata={'vendor': scope}, reset_at=NOW + DAY, clock=lambda: NOW
            )


@pytest.mark.asyncio
async def test_a_paced_fake_acquires_each_vendor_call_on_its_scope_and_counts_it():
    pacer = Pacer()
    reader = FakeRead(clock=lambda: NOW, world=WORLD, scheduler=pacer, scope='DATA', trading_scope='TRADING')
    deadline = NOW + DAY
    bars_tally, list_tally, actions_tally = CallTally(), CallTally(), CallTally()

    await reader.get_bars(
        BarsQuery(
            Instrument('META', AssetType.STOCK),
            Granularity.ONE_DAY,
            utc(2026, 1, 1),
            utc(2026, 1, 2),
            priority=BACKFILL,
            deadline=deadline,
            tally=bars_tally,
        )
    )
    await reader.get_assets(
        AssetListQuery(AssetType.STOCK, AssetListStatus.ACTIVE, priority=BACKFILL, tally=list_tally)
    )
    await reader.get_asset(AssetLookupQuery('META', priority=BACKFILL))
    await reader.get_corporate_actions(CorporateActionsQuery('META', *EVER, priority=BACKFILL, tally=actions_tally))

    assert pacer.asked == [
        ('DATA', BACKFILL, deadline),
        ('TRADING', BACKFILL, None),
        ('TRADING', BACKFILL, None),
        ('DATA', BACKFILL, None),
    ]
    assert [(tally.calls, tally.pages) for tally in (bars_tally, list_tally, actions_tally)] == [(1, 1)] * 3
    assert bars_tally.first_call_at == NOW
    assert reader.calls == (('bars', 'META'), ('assets', 'active'), ('asset', 'META'), ('corporate_actions', 'META'))


@pytest.mark.asyncio
async def test_a_refusal_before_the_vendor_acquires_nothing_and_logs_no_call():
    pacer = Pacer()
    reader = FakeRead(clock=lambda: NOW, scheduler=pacer, scope='DATA')

    outcome = await reader.get_bars(
        BarsQuery(Instrument('VFV', AssetType.STOCK), Granularity.ONE_DAY, NOW + DAY, None, priority=BACKFILL)
    )

    assert isinstance(outcome, BarsFailure) and outcome.error.reason is Reason.RANGE_IN_FUTURE
    assert pacer.asked == [] and reader.calls == ()


@pytest.mark.asyncio
async def test_a_paced_fake_whose_scheduler_refuses_returns_the_refusal_and_makes_no_call():
    reader = FakeRead(clock=lambda: NOW, scheduler=Pacer(refuse=True), scope='DATA')

    outcome = await reader.get_bars(
        BarsQuery(Instrument('VFV', AssetType.STOCK), Granularity.ONE_DAY, utc(2026, 1, 1), None, priority=BACKFILL)
    )
    lookup_outcome = await reader.get_asset(AssetLookupQuery('VFV', priority=BACKFILL))

    assert outcome.error.reason is Reason.RATE_BUDGET
    assert lookup_outcome.error.reason is Reason.RATE_BUDGET
    assert reader.calls == ()


def test_a_scheduler_without_a_scope_is_refused():
    with pytest.raises(ValueError, match='quota scope'):
        FakeRead(scheduler=Pacer())


@pytest.mark.asyncio
async def test_an_unpaced_fake_counts_nothing_but_still_logs_its_calls():
    reader = FakeRead(clock=lambda: NOW)
    tally = CallTally()

    await reader.get_assets(AssetListQuery(AssetType.STOCK, AssetListStatus.INACTIVE, priority=BACKFILL, tally=tally))

    assert (tally.calls, tally.pages) == (0, 0)
    assert reader.calls == (('assets', 'inactive'),)
