"""FakeRead and the test-only ingest launcher (tj-irhy0a.8, decision tj-j4wknb R3, R4, addendum 5).

WHAT THIS FILE PINS. FakeRead's behaviour per scenario; that its bars are deterministic and a pure
function of (symbol, timestamp); that they are aware UTC, ascending, unique and inside the
half-open [start, end) for every Granularity the Alpaca adapter maps; how it bounds an open-ended
query; that tests.fakes.ingest_launcher composes production's app around a FakeRead in the
ALPACA_API slot; and that nothing under tests/fakes imports what the production image lacks.

EXPECTED VALUES ARE WRITTEN OUT, NOT RE-DERIVED. Timestamps below are literal instants, and the
epoch-grid arithmetic appears only where the test is ABOUT the grid (GAPS' even-index rule). The
one exported helper used for expected values is fake_bar(), whose own purity is pinned here too:
it is the published generator the seed producer (tj-vhboky.60) imports, so a test that recomputed
bar prices inline would be a second copy of it, not a check on it.

THE CONTRACT SUITE (FAKES-3, tj-irhy0a.10) asserts the interface properties identically for every
implementation. This file is FakeRead's own: its scenarios, its grid and its launcher, which no
other implementation has.

THE TYPED OUTCOMES (TE-5 tj-3mk3u5.37.6; ADR tj-fa1rpu D2, D3, D8; the Q-EMPTY ruling on
tj-3mk3u5.37.1): FakeRead RETURNS a BarsResponse or a BarsFailure. FAIL_ and a first FAILONCE_ are
VENDOR_UNAVAILABLE, RATELIMIT_ is VENDOR_RATE_LIMITED with a deterministic reset_at, EMPTY_ is
SERVED with no bars, and three refusals (a named feed the fake's deployment feed is not, a start at or
after its clock, an end before the start) come before any scenario. Tests of those use an injected
clock, NOW; the older tests keep the real clock, whose ranges all lie in the past.
"""

import ast
import asyncio
import importlib
import inspect
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from importlib.metadata import packages_distributions
from itertools import pairwise
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import REASONS, ExogenousError, InvalidRequestError, Outcome, Reason, TraderJoeError
from common.tests.image_path import image_pythonpath
from data.ingest.app import app_depends, grpc_host
from data.ingest.app.brokers.alpaca.broker_codes import AlpacaGranularity
from data.ingest.app.brokers.interface import (
    Bar,
    BarsFailure,
    BarsQuery,
    BarsResponse,
    BrokerRead,
    BrokerUnsupportedError,
    Instrument,
    ServedRange,
)
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.tests.grpc_bind import LoopbackGrpc
from schemas.data_ingest.get_dataset_request import StockDatasetRequest
from tests.fakes.market_data import (
    DEFAULT_SLOW_DELAY_SECONDS,
    EMPTY_PREFIX,
    FAIL_PREFIX,
    FAILONCE_PREFIX,
    FEED,
    GAPS_PREFIX,
    MAX_SLOW_DELAY_SECONDS,
    OPEN_ENDED_STEPS,
    RATELIMIT_PREFIX,
    RATELIMIT_RESET_SECONDS,
    SCENARIO_PREFIXES,
    SLOW_PREFIX,
    FakeRead,
    FakeReadError,
    Scenario,
    fake_bar,
    range_end,
    scenario_for,
)


pytestmark = pytest.mark.data_ingest

REPO_ROOT = Path(__file__).resolve().parents[3]
FAKES_DIR = REPO_ROOT / 'tests' / 'fakes'
LAUNCHER_MODULE = 'tests.fakes.ingest_launcher'


HOUR = timedelta(hours=1)
# 2026-01-05 is a Monday; 14:00 UTC is on the ONE_HOUR grid (whole hours since the epoch).
ON_GRID = datetime(2026, 1, 5, 14, tzinfo=UTC)
PLUS_FIVE = timezone(timedelta(hours=5))

# The injected clock of the typed-outcome tests: two hours and a half after ON_GRID, off the grid.
NOW = ON_GRID + timedelta(hours=2, minutes=30)


def at_now() -> datetime:
    return NOW


def query(
    symbol: str,
    start: datetime = ON_GRID,
    end: datetime | None = ON_GRID + 4 * HOUR,
    granularity: Granularity = Granularity.ONE_HOUR,
    feed: Feed | None = None,
) -> BarsQuery:
    """Build a BarsQuery with the priority stated explicitly, as the interface requires.

    Args:
        symbol (str): Requested symbol, scenario prefix included.
        start (datetime): Inclusive start.
        end (datetime | None): Exclusive end, or None.
        granularity (Granularity): Bar size.
        feed (Feed | None): The feed the query names, or None to leave it to the deployment.

    Returns:
        BarsQuery: The query.
    """
    return BarsQuery(
        instrument=Instrument(symbol=symbol, asset_type=AssetType.STOCK),
        granularity=granularity,
        start=start,
        end=end,
        priority=RequestPriority.INTERACTIVE,
        feed=feed,
    )


async def failure(reader: FakeRead, bars_query: BarsQuery) -> TraderJoeError:
    """Fetch, and return the error of the BarsFailure that must come back."""
    outcome = await reader.get_bars(bars_query)
    assert isinstance(outcome, BarsFailure), f'expected a BarsFailure, got {outcome!r}'
    return outcome.error


async def served(reader: FakeRead, bars_query: BarsQuery, cap: int = OPEN_ENDED_STEPS * 10) -> list[Bar]:
    """Fetch and drain the bars, never more than cap of them.

    The cap is what keeps a regression that made an open-ended query unbounded a RED test rather
    than a hung one: the list comes back one longer than any bound under test, and the length
    assertion fails.

    Args:
        reader (FakeRead): Reader under test.
        bars_query (BarsQuery): Query to serve.
        cap (int): Most bars to take.

    Returns:
        list[Bar]: The bars, in the order served.
    """
    response = await reader.get_bars(bars_query)
    assert isinstance(response, BarsResponse), f'expected SERVED, got {response!r}'
    assert response.feed is Feed.IEX
    bars: list[Bar] = []
    async for bar in response.bars:
        bars.append(bar)
        if len(bars) >= cap:
            break
    return bars


def hours(first: datetime, count: int) -> list[datetime]:
    """Consecutive whole hours, written as instants."""
    return [first + index * HOUR for index in range(count)]


# ---------------------------------------------------------------------------------------------
# Conformance: structural, no inheritance, no capability flags
# ---------------------------------------------------------------------------------------------


def test_fake_read_has_every_member_of_the_broker_read_protocol_with_the_same_signature():
    # BrokerRead is not runtime_checkable and the repo runs no type checker, so structural
    # conformance is checked member by member: same names, coroutine where the Protocol has one,
    # same parameters.
    members = [name for name, _ in inspect.getmembers(BrokerRead, inspect.isfunction) if not name.startswith('_')]
    assert members == ['get_bars']
    for name in members:
        protocol_member, fake_member = getattr(BrokerRead, name), getattr(FakeRead, name)
        assert inspect.iscoroutinefunction(fake_member)
        assert list(inspect.signature(fake_member).parameters) == list(inspect.signature(protocol_member).parameters)


def test_fake_read_inherits_nothing_and_declares_no_capabilities():
    # U7: conformance is structural; FakeRead must not subclass AlpacaRead or any helper.
    # Addendum 4 item 5: capability flags were withdrawn, so none may creep back in via the fake.
    assert FakeRead.__mro__ == (FakeRead, object)
    assert not hasattr(FakeRead(), 'capabilities')


# ---------------------------------------------------------------------------------------------
# The default scenario and the half-open range
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_serves_one_bar_per_grid_step_of_the_range_from_the_published_generator():
    bars = await served(FakeRead(), query('VFV'))

    assert [bar.timestamp for bar in bars] == hours(ON_GRID, 4)
    assert bars == [fake_bar('VFV', timestamp) for timestamp in hours(ON_GRID, 4)]


@pytest.mark.asyncio
async def test_a_range_whose_end_is_on_the_grid_ends_one_step_before_end():
    # Addendum 5 (tj-irhy0a.15): half-open [start, end). 18:00 is itself a grid point, and it is
    # exactly the bar that must NOT come back.
    end = ON_GRID + 4 * HOUR

    bars = await served(FakeRead(), query('VFV', end=end))

    assert bars[-1].timestamp == end - HOUR
    assert end not in [bar.timestamp for bar in bars]


@pytest.mark.asyncio
async def test_a_start_on_the_grid_is_served_and_an_off_grid_start_begins_at_the_next_grid_point():
    on_grid = await served(FakeRead(), query('VFV', start=ON_GRID, end=ON_GRID + 2 * HOUR))
    off_grid = await served(FakeRead(), query('VFV', start=ON_GRID + timedelta(minutes=1), end=ON_GRID + 2 * HOUR))

    assert [bar.timestamp for bar in on_grid] == hours(ON_GRID, 2)
    assert [bar.timestamp for bar in off_grid] == [ON_GRID + HOUR]


@pytest.mark.asyncio
async def test_bounds_in_another_offset_are_compared_as_instants_and_bars_come_back_in_utc():
    # The same two instants as the UTC query above, spelled at +05:00.
    utc_bars = await served(FakeRead(), query('VFV'))
    offset_bars = await served(
        FakeRead(), query('VFV', start=ON_GRID.astimezone(PLUS_FIVE), end=(ON_GRID + 4 * HOUR).astimezone(PLUS_FIVE))
    )

    assert offset_bars == utc_bars
    assert {bar.timestamp.tzinfo for bar in offset_bars} == {UTC}


@pytest.mark.asyncio
@pytest.mark.parametrize('granularity', [member.granularity for member in AlpacaGranularity], ids=str)
async def test_bars_are_aware_utc_ascending_unique_and_inside_the_range_at_every_mapped_granularity(
    granularity: Granularity,
):
    # A half-open range exactly ten steps long holds exactly ten grid points wherever it starts,
    # so the count is known without placing the grid by hand. The start is deliberately off every
    # grid (a Monday, 07:13) so the first bar is never simply `start`.
    start = datetime(2026, 1, 5, 7, 13, tzinfo=UTC)
    end = start + 10 * granularity.offset

    bars = await served(FakeRead(), query('VFV', start=start, end=end, granularity=granularity))

    timestamps = [bar.timestamp for bar in bars]
    assert len(bars) == 10
    assert all(timestamp.tzinfo is UTC for timestamp in timestamps)
    assert all(start <= timestamp < end for timestamp in timestamps)
    assert all(later - earlier == granularity.offset for earlier, later in pairwise(timestamps))


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix', ['', GAPS_PREFIX, SLOW_PREFIX], ids=lambda prefix: prefix or 'default')
async def test_every_bar_served_carries_an_int_volume_and_an_int_trade_count(prefix: str):
    # The store's batch schema requires both as int. What ingest_control should do with a None
    # from a real broker is an open question on tj-irhy0a.7 and is NOT pinned here; the fake
    # simply never emits one. type() rather than isinstance(): a bool would pass isinstance(int).
    bars = await served(FakeRead(slow_delay_seconds=0.0), query(f'{prefix}VFV', end=None))

    assert bars
    assert {(type(bar.volume), type(bar.trade_count)) for bar in bars} == {(int, int)}


# ---------------------------------------------------------------------------------------------
# Determinism: a pure function of (symbol, timestamp)
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_calls_serve_equal_bars_on_one_instance_and_across_instances():
    reader = FakeRead()

    first, second, other_instance = (
        await served(reader, query('VFV')),
        await served(reader, query('VFV')),
        await served(FakeRead(), query('VFV')),
    )

    assert first == second == other_instance


@pytest.mark.asyncio
async def test_overlapping_queries_agree_on_every_shared_timestamp():
    # The grid is anchored at the epoch, not at the query's start, so where a query begins
    # changes which bars it covers and never what those bars are.
    wide = await served(FakeRead(), query('VFV', start=ON_GRID - 2 * HOUR, end=ON_GRID + 4 * HOUR))
    narrow = await served(FakeRead(), query('VFV', start=ON_GRID + timedelta(minutes=30), end=ON_GRID + 3 * HOUR))

    assert narrow == [bar for bar in wide if bar.timestamp in {bar.timestamp for bar in narrow}]
    assert len(narrow) == 2


def test_the_generator_is_a_pure_function_of_symbol_and_instant_and_separates_symbols():
    assert fake_bar('VFV', ON_GRID) == fake_bar('VFV', ON_GRID.astimezone(PLUS_FIVE))
    assert fake_bar('VFV', ON_GRID) != fake_bar('XIC', ON_GRID)
    assert fake_bar('VFV', ON_GRID) != fake_bar('VFV', ON_GRID + HOUR)


def test_the_generator_serves_well_formed_bars_the_same_in_a_fresh_interpreter():
    # hash() is salted per process; the seed producer and the stack run in other interpreters, so
    # the generator must give byte-identical bars there. Compared by repr through a subprocess.
    probe = (
        'from datetime import datetime, UTC\n'
        'from tests.fakes.market_data import fake_bar\n'
        "print(repr(fake_bar('VFV', datetime(2026, 1, 5, 14, tzinfo=UTC))))\n"
    )
    result = subprocess.run(
        [sys.executable, '-c', probe],
        cwd=REPO_ROOT,
        env={'PYTHONPATH': image_pythonpath(REPO_ROOT), 'PYTHONHASHSEED': '12345'},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == repr(fake_bar('VFV', ON_GRID))

    bar = fake_bar('VFV', ON_GRID)
    assert bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
    assert bar.low <= bar.vwap <= bar.high
    assert bar.volume > 0 and bar.trade_count > 0


# ---------------------------------------------------------------------------------------------
# The open-ended bound
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_open_ended_query_serves_exactly_open_ended_steps_grid_points_from_start():
    # served() takes at most OPEN_ENDED_STEPS + 1 bars, so an unbounded regression fails on the
    # length rather than hanging the run.
    bars = await served(FakeRead(), query('VFV', end=None), cap=OPEN_ENDED_STEPS + 1)

    assert [bar.timestamp for bar in bars] == hours(ON_GRID, OPEN_ENDED_STEPS)


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix', ['', *SCENARIO_PREFIXES], ids=lambda prefix: prefix or 'default')
async def test_no_scenario_runs_forever_on_an_open_ended_query(prefix: str):
    # FAIL_, a first FAILONCE_ and RATELIMIT_ return a BarsFailure; the point is that every scenario
    # RETURNS, inside a hard timeout, with at most OPEN_ENDED_STEPS bars.
    reader = FakeRead(slow_delay_seconds=0.0)

    async def settle() -> list[Bar] | BarsFailure:
        outcome = await reader.get_bars(query(f'{prefix}VFV', end=None))
        if isinstance(outcome, BarsFailure):
            return outcome
        bars: list[Bar] = []
        async for bar in outcome.bars:
            bars.append(bar)
            if len(bars) > OPEN_ENDED_STEPS:
                break
        return bars

    outcome = await asyncio.wait_for(settle(), timeout=10)

    assert isinstance(outcome, BarsFailure) or len(outcome) <= OPEN_ENDED_STEPS


# ---------------------------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------------------------


def test_every_scenario_but_default_has_exactly_one_exported_prefix_and_no_prefix_starts_another():
    assert set(SCENARIO_PREFIXES.values()) == set(Scenario) - {Scenario.DEFAULT}
    assert len(SCENARIO_PREFIXES) == len(set(SCENARIO_PREFIXES.values()))
    # scenario_for() takes the first prefix that matches. That is only well defined while no
    # prefix starts another: a FAILONCE_ spelled FAIL_ONCE_ would be read as FAIL_ or not,
    # depending on dict order.
    overlapping = [(a, b) for a in SCENARIO_PREFIXES for b in SCENARIO_PREFIXES if a != b and b.startswith(a)]
    assert overlapping == []
    assert scenario_for(f'{FAILONCE_PREFIX}VFV') is Scenario.FAILONCE
    assert scenario_for(f'{FAIL_PREFIX}VFV') is Scenario.FAIL
    assert scenario_for('VFV') is Scenario.DEFAULT
    # data_store upper-cases symbols, so a lower-case prefix can never arrive; it selects nothing.
    assert scenario_for(f'{FAIL_PREFIX.lower()}VFV') is Scenario.DEFAULT


@pytest.mark.asyncio
async def test_empty_serves_the_feed_and_zero_bars():
    assert await served(FakeRead(), query(f'{EMPTY_PREFIX}VFV')) == []


@pytest.mark.asyncio
async def test_empty_is_served_with_the_asked_window_as_its_served_range_never_a_failure():
    """Q-EMPTY (tj-3mk3u5.37.1): an empty window is SERVED, rows [], served_range = (start, min(end, as_of))."""
    start, end = ON_GRID - 4 * HOUR, ON_GRID - HOUR

    response = await FakeRead(clock=at_now).get_bars(query(f'{EMPTY_PREFIX}VFV', start=start, end=end))

    assert isinstance(response, BarsResponse)
    assert [bar async for bar in response.bars] == []
    assert response.served_range == ServedRange(start, end)
    assert response.as_of == NOW


@pytest.mark.asyncio
async def test_gaps_serves_exactly_the_even_grid_indices_and_nothing_else():
    # The grid index of an hour is hours since the epoch; 2026-01-05 14:00 UTC is 20_458 days and
    # 14 hours on, index 491_006, even, so of four hours from ON_GRID the 14:00 and 16:00 bars remain.
    symbol = f'{GAPS_PREFIX}VFV'
    assert (ON_GRID - datetime(1970, 1, 1, tzinfo=UTC)) // HOUR == 20_458 * 24 + 14 == 491_006

    bars = await served(FakeRead(), query(symbol))

    assert bars == [fake_bar(symbol, ON_GRID), fake_bar(symbol, ON_GRID + 2 * HOUR)]


def assert_vendor_unavailable(error: TraderJoeError) -> None:
    """FAIL_'s failure: VENDOR_UNAVAILABLE, NOT_READY, with a FakeReadError as its cause and no reset_at."""
    assert type(error) is ExogenousError
    assert error.reason is Reason.VENDOR_UNAVAILABLE
    assert REASONS[error.reason].outcome is Outcome.NOT_READY
    assert isinstance(error.__cause__, FakeReadError)
    assert error.reset_at is None


@pytest.mark.asyncio
async def test_fail_returns_vendor_unavailable_on_every_call_and_never_a_response():
    reader = FakeRead()

    for _ in range(2):
        assert_vendor_unavailable(await failure(reader, query(f'{FAIL_PREFIX}VFV')))


@pytest.mark.asyncio
async def test_failonce_fails_the_first_call_per_range_then_serves_the_default_bars():
    reader = FakeRead()
    symbol = f'{FAILONCE_PREFIX}VFV'

    assert_vendor_unavailable(await failure(reader, query(symbol)))
    second = await served(reader, query(symbol))
    third = await served(reader, query(symbol))

    assert second == third == [fake_bar(symbol, timestamp) for timestamp in hours(ON_GRID, 4)]


@pytest.mark.asyncio
async def test_failonce_remembers_per_range_and_per_instance():
    reader = FakeRead()
    symbol = f'{FAILONCE_PREFIX}VFV'
    assert_vendor_unavailable(await failure(reader, query(symbol)))

    # A different range of the same symbol is a first call of its own.
    assert_vendor_unavailable(await failure(reader, query(symbol, end=ON_GRID + 5 * HOUR)))
    # The same range spelled in another offset is the same range.
    assert isinstance(await reader.get_bars(query(symbol, start=ON_GRID.astimezone(PLUS_FIVE))), BarsResponse)
    # The memory is the instance's: a second process (a second worker) fails again. Hence
    # SERVICE_WORKERS=1 for the fake-mode stack.
    assert_vendor_unavailable(await failure(FakeRead(), query(symbol)))


@pytest.mark.asyncio
async def test_ratelimit_returns_vendor_rate_limited_with_a_deterministic_reset_on_every_call():
    reader = FakeRead(clock=at_now)

    for _ in range(2):
        error = await failure(reader, query(f'{RATELIMIT_PREFIX}VFV', end=ON_GRID + 2 * HOUR))

        assert type(error) is ExogenousError
        assert error.reason is Reason.VENDOR_RATE_LIMITED
        assert REASONS[error.reason].outcome is Outcome.NOT_READY
        assert error.reset_at == NOW + timedelta(seconds=RATELIMIT_RESET_SECONDS)
        assert error.retry_after == RATELIMIT_RESET_SECONDS == 60
        assert isinstance(error.__cause__, FakeReadError)


# ---------------------------------------------------------------------------------------------
# The refusals, before any scenario (Q-EMPTY tj-3mk3u5.37.1; tj-3mk3u5.9's feed-entitlement note)
# ---------------------------------------------------------------------------------------------


def recording_reader(feed: Feed = FEED) -> tuple[FakeRead, list[float]]:
    """A FakeRead whose SLOW_ sleeps are recorded, so 'before any scenario' is observable."""
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    return FakeRead(slow_delay_seconds=0.5, sleep=record, clock=at_now, feed=feed), slept


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('deployment', 'named'),
    [
        pytest.param(Feed.IEX, Feed.SIP, id='sip-on-the-default-iex'),
        pytest.param(Feed.SIP, Feed.IEX, id='iex-on-a-sip-deployment'),
        pytest.param(Feed.IEX, Feed.NOT_APPLICABLE, id='not-applicable'),
    ],
)
async def test_a_named_feed_other_than_the_deployment_feed_is_refused_before_any_scenario(deployment, named):
    reader, slept = recording_reader(deployment)

    error = await failure(reader, query(f'{SLOW_PREFIX}VFV', end=ON_GRID + 2 * HOUR, feed=named))

    assert type(error) is BrokerUnsupportedError
    assert error.reason is Reason.FEED_NOT_AVAILABLE
    assert REASONS[error.reason].outcome is Outcome.REFUSED
    assert named.value in error.detail
    assert dict(error.metadata) == {'feed': named.value}
    assert slept == [], 'SLOW_ slept before the feed refusal'


@pytest.mark.asyncio
@pytest.mark.parametrize('named', [None, Feed.SIP], ids=['unnamed', 'named-sip'])
async def test_the_deployment_feed_is_served_and_reported_named_or_not(named):
    reader, slept = recording_reader(Feed.SIP)

    response = await reader.get_bars(query(f'{SLOW_PREFIX}VFV', end=ON_GRID + 2 * HOUR, feed=named))

    assert isinstance(response, BarsResponse)
    assert response.feed is Feed.SIP
    assert reader.feed is Feed.SIP
    # The same arrangement that is refused above does reach the scenario here, so 'no sleep' was not vacuous.
    assert slept == [0.5]


def test_the_default_deployment_feed_is_iex():
    assert FakeRead().feed is FEED is Feed.IEX


@pytest.mark.asyncio
async def test_a_refused_failonce_query_does_not_spend_the_fail_once_memory():
    reader, _slept = recording_reader()
    symbol = f'{FAILONCE_PREFIX}VFV'

    await failure(reader, query(symbol, end=ON_GRID + 2 * HOUR, feed=Feed.SIP))

    # The first query that reaches the scenario is still the first call for the range.
    assert_vendor_unavailable(await failure(reader, query(symbol, end=ON_GRID + 2 * HOUR)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'start',
    [
        pytest.param(NOW, id='exactly-the-clock'),
        pytest.param(NOW.astimezone(PLUS_FIVE), id='the-clock-at-plus-five'),
        pytest.param(NOW + timedelta(microseconds=1), id='just-after'),
        pytest.param(NOW + 30 * 24 * HOUR, id='next-month'),
    ],
)
async def test_a_range_starting_at_or_after_the_clock_is_refused_before_any_scenario(start):
    reader, slept = recording_reader()

    error = await failure(reader, query(f'{SLOW_PREFIX}VFV', start=start, end=start + 2 * HOUR))

    assert type(error) is InvalidRequestError
    assert error.reason is Reason.RANGE_IN_FUTURE
    assert error.reset_at is None
    assert dict(error.metadata) == {'range_start': start.isoformat()}
    assert slept == []


@pytest.mark.asyncio
async def test_a_range_starting_just_before_the_clock_is_served():
    response = await FakeRead(clock=at_now).get_bars(query('VFV', start=NOW - timedelta(microseconds=1), end=None))

    assert isinstance(response, BarsResponse)


@pytest.mark.asyncio
async def test_an_end_before_the_start_is_refused_as_alpacas_400_is_before_any_scenario():
    reader, slept = recording_reader()

    error = await failure(reader, query(f'{SLOW_PREFIX}VFV', start=ON_GRID, end=ON_GRID - HOUR))

    assert type(error) is InvalidRequestError
    assert error.reason is Reason.VENDOR_INVALID_REQUEST
    assert REASONS[error.reason].outcome is Outcome.REFUSED
    assert error.detail == 'end should not be before start'
    assert slept == []


@pytest.mark.asyncio
async def test_an_end_equal_to_the_start_is_an_empty_window_served_not_refused():
    response = await FakeRead(clock=at_now).get_bars(query('VFV', start=ON_GRID, end=ON_GRID))

    assert isinstance(response, BarsResponse)
    assert [bar async for bar in response.bars] == []
    assert response.served_range == ServedRange(ON_GRID, ON_GRID)


# ---------------------------------------------------------------------------------------------
# served_range and as_of (D2, D3 note a)
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_past_range_is_served_as_asked_with_as_of_from_the_clock():
    start, end = ON_GRID - 3 * HOUR, ON_GRID

    response = await FakeRead(clock=at_now).get_bars(query('VFV', start=start, end=end))

    assert response.served_range == ServedRange(start, end)
    assert response.as_of == NOW
    assert [bar.timestamp async for bar in response.bars] == hours(start, 3)


@pytest.mark.asyncio
async def test_a_range_ending_after_the_clock_is_served_up_to_as_of_and_no_bar_from_after_it():
    # NOW is 16:30: the 14:00, 15:00 and 16:00 bars have happened; 17:00 has not.
    response = await FakeRead(clock=at_now).get_bars(query('VFV', start=ON_GRID, end=ON_GRID + 6 * HOUR))

    assert response.served_range == ServedRange(ON_GRID, NOW)
    assert [bar.timestamp async for bar in response.bars] == hours(ON_GRID, 3)


@pytest.mark.asyncio
async def test_an_open_ended_query_reports_its_count_bound_as_the_served_end():
    # D3 note (a): a window the fake clamps by count is reported clamped, never as the open range.
    start = ON_GRID - 1000 * HOUR
    bound = start + OPEN_ENDED_STEPS * HOUR
    assert range_end(Granularity.ONE_HOUR, start, None) == bound < NOW

    response = await FakeRead(clock=at_now).get_bars(query('VFV', start=start, end=None))

    assert response.served_range == ServedRange(start, bound)
    assert len([bar async for bar in response.bars]) == OPEN_ENDED_STEPS


@pytest.mark.asyncio
async def test_an_open_ended_query_reaching_the_clock_is_served_up_to_as_of():
    response = await FakeRead(clock=at_now).get_bars(query('VFV', start=ON_GRID, end=None))

    assert response.served_range == ServedRange(ON_GRID, NOW)
    assert [bar.timestamp async for bar in response.bars] == hours(ON_GRID, 3)


@pytest.mark.asyncio
async def test_as_of_is_read_after_the_slow_delay():
    readings = iter([NOW - HOUR, NOW])
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    reader = FakeRead(slow_delay_seconds=1.0, sleep=record, clock=lambda: next(readings))

    response = await reader.get_bars(query(f'{SLOW_PREFIX}VFV', start=ON_GRID - 3 * HOUR, end=None))

    assert slept == [1.0]
    assert response.as_of == NOW
    assert response.served_range.end == NOW


@pytest.mark.asyncio
async def test_as_of_is_utc_whatever_offset_the_clock_answers_in():
    response = await FakeRead(clock=lambda: NOW.astimezone(PLUS_FIVE)).get_bars(query('VFV', end=None))

    assert response.as_of == NOW
    assert response.as_of.utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_slow_sleeps_the_configured_delay_before_answering_then_serves_the_default_bars():
    slept: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        slept.append(seconds)

    reader = FakeRead(slow_delay_seconds=6.5, sleep=recording_sleep)
    symbol = f'{SLOW_PREFIX}VFV'

    response = await reader.get_bars(query(symbol))
    # Slept before answering at all: the feed ack is late too, as a slow vendor's would be.
    assert slept == [6.5]
    bars = [bar async for bar in response.bars]

    assert bars == [fake_bar(symbol, timestamp) for timestamp in hours(ON_GRID, 4)]
    # Only SLOW_ sleeps.
    await served(reader, query('VFV'))
    assert slept == [6.5]


@pytest.mark.asyncio
async def test_slow_really_waits_on_the_event_loop_by_default():
    # The injected sleep above proves the call; this proves the default is a real, non-blocking
    # wait, with a delay short enough to keep the suite fast.
    loop = asyncio.get_running_loop()
    began = loop.time()

    await served(FakeRead(slow_delay_seconds=0.2), query(f'{SLOW_PREFIX}VFV'))

    assert loop.time() - began >= 0.2


def test_slow_takes_its_default_delay_and_is_bounded():
    """The default is a real delay and the ceiling is enforced at construction.

    IT USED TO ASSERT SLOW_ OUTLIVES THE CALLER'S DEADLINE, against a module-level
    RPC_DEADLINE_SECONDS = 5.0 that cited common/kafka/rpc/kafka_rpc_client.py. That file is deleted
    and the comparison had stopped meaning anything: the fetch deadline is now
    DEFAULT_FETCH_DEADLINE_S = 300 s (common/rpc/clients/ingest_fetch.py), set deliberately high on
    tj-3mk3u5.10 because 5 s abandoned requests the vendor was still serving. 8.0 > 5.0 passed and
    proved nothing against a number the system no longer uses.

    NOR CAN THE PROPERTY BE REPAIRED BY RAISING THE NUMBER: MAX_SLOW_DELAY_SECONDS is 60, so no
    permitted delay outlives a 300 s deadline. SLOW_ now exercises a vendor that is slow but SERVED,
    which is what tests/system/test_ingest_e2e.py asserts -- it reversed this very expectation on
    tj-xhcoyc item 2, and that reversal is the ruling rather than a concession.

    SO WHAT IS LEFT TO PIN IS THAT SLOW_ IS SLOW AT ALL. A default of 0 would make the scenario
    indistinguishable from DEFAULT and quietly delete it, which no other test here would notice --
    the one that proves the delay is applied passes its own 0.2 explicitly. The comparison is
    against 0 and the ceiling, NOT against DEFAULT_SLOW_DELAY_SECONDS: asserting the attribute
    equals the constant that sets it cannot fail, and the first version of this repair did exactly
    that. The specific value 8.0 no longer has a derivation, and inventing one here would be the
    same mistake in the other direction.
    """
    assert 0 < DEFAULT_SLOW_DELAY_SECONDS <= MAX_SLOW_DELAY_SECONDS, (
        'SLOW_ must be a real, bounded delay; at 0 it is the DEFAULT scenario under another name'
    )
    assert FakeRead().slow_delay_seconds == DEFAULT_SLOW_DELAY_SECONDS
    assert FakeRead(slow_delay_seconds=MAX_SLOW_DELAY_SECONDS).slow_delay_seconds == MAX_SLOW_DELAY_SECONDS
    for refused in (-0.1, MAX_SLOW_DELAY_SECONDS + 0.1, float('inf'), float('nan')):
        with pytest.raises(ValueError, match='slow_delay_seconds'):
            FakeRead(slow_delay_seconds=refused)


# ---------------------------------------------------------------------------------------------
# Through ingest_control, the RPC edge
# ---------------------------------------------------------------------------------------------


def stock_request(symbol: str) -> StockDatasetRequest:
    """Build a market-activity request for ON_GRID to four hours on."""
    return StockDatasetRequest(
        dataset_id=uuid4(),
        owner='rebalancer',
        source=DataSource.ALPACA_API,
        feed=Feed.IEX,
        granularity=Granularity.ONE_HOUR,
        start=ON_GRID,
        end=ON_GRID + 4 * HOUR,
        expiry=ON_GRID,
        expiry_type=ExpiryType.ROLLING,
        update_type=UpdateType.DAILY,
        asset_symbol=symbol,
        asset_type=AssetType.STOCK,
        data_types=[DataType.MARKET_ACTIVITY],
    )


# RETIRED ON tj-3mk3u5.32, with the fake_installed fixture they shared:
# test_ingest_control_stores_the_fake_bars_under_the_fake_feed and
# test_ingest_control_turns_a_failure_into_the_bare_empty_answer_as_for_the_real_reader.
#
# Both drove ingest_control.store_retrieve_stock, which tj-3mk3u5.11 deletes along with the
# install_readers slot the fixture filled. The second was explicit that it was pinning the Kafka RPC
# edge's bare {} "until tj-3mk3u5.11 removes it", and that behaviour has no successor by design --
# PR 2's typed errors replaced the swallow.
#
# The first says something that does survive, and it is already pinned where it belongs: that
# FakeRead serves the bars and the feed it claims to. test_broker_read_contract.py runs FakeRead
# through the same contract suite as every other reader, and the tests above in this file pin its
# grid, its prefixes and its delays directly. What went with the edge is only the BATCH's view of
# them.


# ---------------------------------------------------------------------------------------------
# The launcher
# ---------------------------------------------------------------------------------------------

LAUNCHER_PROBE = """
import sys

import tests.fakes.ingest_launcher as launcher

print('app-type=' + type(launcher.app).__name__)
print('reader-type=' + type(launcher.reader).__name__)
print('slow-delay=' + repr(launcher.reader.slow_delay_seconds))
print('test-modules=' + ','.join(sorted(n for n in sys.modules if n.split('.')[0] in {'pytest', '_pytest', 'mock'} or n == 'unittest.mock')))
"""


def run_launcher_probe(**environ: str) -> subprocess.CompletedProcess:
    """Import the launcher in a fresh interpreter, as uvicorn does, with a controlled environment.

    PYTHONPATH is the image's (common/tests/image_path.py): the root, then the generated gRPC code's
    root, which the launcher's app reaches once a servicer is registered (decision tj-3mk3u5.42 F1).

    Args:
        **environ (str): Variables to set beside PYTHONPATH.

    Returns:
        subprocess.CompletedProcess: The finished probe.
    """
    return subprocess.run(
        [sys.executable, '-c', LAUNCHER_PROBE],
        cwd=REPO_ROOT,
        env={'PYTHONPATH': image_pythonpath(REPO_ROOT), **environ},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture(scope='module')
def launcher_probe() -> subprocess.CompletedProcess:
    """One default-environment import of the launcher, shared by the assertions below."""
    return run_launcher_probe()


def test_the_launcher_imports_in_a_fresh_interpreter_as_a_fastapi_app_around_a_fake_read(launcher_probe):
    assert launcher_probe.returncode == 0, launcher_probe.stderr
    assert 'app-type=FastAPI' in launcher_probe.stdout
    assert 'reader-type=FakeRead' in launcher_probe.stdout
    assert f'slow-delay={DEFAULT_SLOW_DELAY_SECONDS!r}' in launcher_probe.stdout


def test_the_launcher_logs_exactly_one_warning_banner_at_import(launcher_probe):
    from tests.fakes.ingest_launcher import BANNER

    warnings = [line for line in launcher_probe.stderr.splitlines() if line.startswith('WARNING')]
    assert len(warnings) == 1, launcher_probe.stderr
    assert BANNER in warnings[0]
    assert 'no vendor is called' in BANNER and 'FakeRead' in BANNER


def test_importing_the_launcher_loads_no_test_framework_or_mock_library(launcher_probe):
    # The runtime half of the import-discipline scan below: what actually got imported,
    # transitively, in an interpreter that had not already loaded pytest.
    assert re.search(r'^test-modules=$', launcher_probe.stdout, re.MULTILINE), launcher_probe.stdout


def test_the_launcher_takes_the_slow_delay_from_its_environment_variable():
    from tests.fakes.ingest_launcher import SLOW_DELAY_ENV

    configured = run_launcher_probe(**{SLOW_DELAY_ENV: '1.5'})
    refused = run_launcher_probe(**{SLOW_DELAY_ENV: 'not-a-number'})
    out_of_bounds = run_launcher_probe(**{SLOW_DELAY_ENV: str(MAX_SLOW_DELAY_SECONDS * 2)})

    assert 'slow-delay=1.5' in configured.stdout, configured.stderr
    # A bad value stops the launcher at import rather than serving with a delay nobody asked for.
    assert refused.returncode != 0 and 'ValueError' in refused.stderr
    assert out_of_bounds.returncode != 0 and 'slow_delay_seconds' in out_of_bounds.stderr


@pytest.mark.asyncio
async def test_the_launcher_apps_lifespan_serves_alpaca_through_its_fake_read(monkeypatch, tmp_path):
    """Fake mode's composition root: the launcher's app hands ITS FakeRead to what serves requests.

    Only what cannot run here is stubbed -- the latency server and the debugger. The lifespan
    itself, create_app and the registration are production's, and so is the gRPC host it starts,
    bound to loopback here as the overlay binds it to the compose alias.

    RE-POINTED ON tj-3mk3u5.32, from the reader registry to the registration. It used to wrap
    ingest_control.install_readers and then prove dispatch really went through the installed fake by
    fetching a batch, and prove teardown by fetching again and expecting NotImplementedError.
    tj-3mk3u5.11 deletes install_readers, clear_readers and store_retrieve_stock together: the
    handler is CONSTRUCTED with its readers, so there is no install step to observe and no cleared
    state to catch. What matters, and is what this asserts, is that the mapping reaching the
    registration is the launcher's own fake and nothing else -- fake mode serving a real AlpacaRead
    is the failure this guards, and it would be a live broker call in a test deployment.

    The fake's own bars and feed are not re-asserted through the lifespan: they are pinned directly
    above and through test_broker_read_contract.py, and routing them through a lifespan only to read
    them back proves the fake twice and the wiring once.
    """
    handed: list[dict[DataSource, Any]] = []
    real_registered_services = grpc_host.registered_services

    def recording_registration(readers):
        handed.append(dict(readers))
        return real_registered_services(readers)

    monkeypatch.setattr(app_depends, 'initialize_latency_server', Mock())
    monkeypatch.setattr(app_depends, 'init_debugger', Mock())
    monkeypatch.setattr(grpc_host, 'registered_services', recording_registration)
    launcher = importlib.import_module(LAUNCHER_MODULE)
    assert isinstance(launcher.app, FastAPI)

    async with LoopbackGrpc(), launcher.app.router.lifespan_context(launcher.app):
        pass

    assert [set(readers) for readers in handed] == [{DataSource.ALPACA_API}]
    assert handed[0][DataSource.ALPACA_API] is launcher.reader
    assert isinstance(launcher.reader, FakeRead), 'fake mode wired something other than its fake reader'


# ---------------------------------------------------------------------------------------------
# Import discipline: tests/fakes runs inside the production image
# ---------------------------------------------------------------------------------------------

# data_ingest's production dependencies, by IMPORT name: the base and data-ingest groups the prod
# image installs (Dockerfile: uv sync --only-group base --only-group data-ingest). The test below
# ties every name back to a distribution those groups declare, so this list cannot drift into the
# testing group.
#
# 'aenum' and 'kafka' came out on tj-3mk3u5.32, ahead of the distributions themselves, because a
# name left here whose distribution is no longer declared reds the test below. Removing them early
# cost nothing: this is a WHITELIST of what tests/fakes may import, and nothing under tests/fakes
# imports either -- checked, not assumed.
#
# ONE HALF OF THAT REASONING WAS WRONG AND IS CORRECTED HERE. It said tj-3mk3u5.14 would drop aenum
# from pyproject.toml. It did not: that bead's claim that common/kafka/topics.py was aenum's only
# importer was REFUTED at the .14 gate -- common/enums/composed_enum.py imports Enum and
# extend_enum from it directly, reached from common/enums/data_stock.py and two migrations -- so
# aenum stays declared and dropping it would have broken production, not a test. 'aenum' is absent
# from this set because tests/fakes does not import it, which was always the real reason.
# kafka-python-ng still goes, on tj-3mk3u5.15.
PRODUCTION_THIRD_PARTY = frozenset({'alpaca', 'dotenv', 'fastapi', 'httpx', 'pydantic', 'starlette', 'uvicorn'})
PRODUCTION_GROUPS = ('base', 'data-ingest')

# First-party code the data_ingest image contains (Dockerfile: COPY common, routers, schemas and
# data/ingest/app), plus this package, which the fake-mode overlay mounts beside it.
FIRST_PARTY_ROOTS = ('common', 'routers', 'schemas', 'data.ingest.app', 'tests.fakes')

# unittest.mock is a mock library that happens to ship with Python.
FORBIDDEN_STDLIB = frozenset({'unittest'})


def import_is_allowed(module: str) -> bool:
    """Decide whether tests/fakes may import a module.

    Args:
        module (str): Fully qualified module name.

    Returns:
        bool: True for the standard library (less unittest), production's third-party dependencies
            and first-party code under FIRST_PARTY_ROOTS that is not itself a test package.
    """
    parts = module.split('.')
    if parts[0] in sys.stdlib_module_names:
        return parts[0] not in FORBIDDEN_STDLIB
    if parts[0] in PRODUCTION_THIRD_PARTY:
        return True
    under_root = any(module == root or module.startswith(f'{root}.') for root in FIRST_PARTY_ROOTS)
    # A test package under an allowed root (common/tests, routers/tests, schemas/tests) is not in
    # the image's runtime contract and imports pytest freely; only tests.fakes itself is allowed.
    is_test_package = 'tests' in parts and not module.startswith('tests.fakes')
    return under_root and not is_test_package


def imports_of(path: Path) -> Iterator[tuple[int, str]]:
    """Every module a file imports, with the line it is imported on.

    A `from X import Y` is checked as X.Y where X alone is not allowed, so `from data.ingest import
    app` is judged by what it actually binds. Relative imports resolve against tests.fakes.

    Args:
        path (Path): Python file under tests/fakes.

    Yields:
        tuple[int, str]: (line number, fully qualified module name).
    """
    package = '.'.join(path.relative_to(REPO_ROOT).with_suffix('').parts[:-1])
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            if node.level:
                anchor = package.split('.')[: len(package.split('.')) - (node.level - 1)]
                base = '.'.join([*anchor, base] if base else anchor)
            if import_is_allowed(base):
                yield node.lineno, base
            else:
                for alias in node.names:
                    yield node.lineno, f'{base}.{alias.name}'


def fakes_modules() -> list[Path]:
    """Every Python module under tests/fakes, including ones added after this file was written."""
    return sorted(FAKES_DIR.rglob('*.py'))


def test_the_scan_covers_every_module_this_bead_wrote():
    # A scan that walked the wrong directory would pass on zero files.
    names = {path.name for path in fakes_modules()}
    assert {'__init__.py', 'market_data.py', 'ingest_launcher.py'} <= names


def test_nothing_under_tests_fakes_imports_outside_the_production_image():
    violations = [
        f'{path.relative_to(REPO_ROOT)}:{line}: {module}'
        for path in fakes_modules()
        for line, module in imports_of(path)
        if not import_is_allowed(module)
    ]
    assert not violations, (
        'tests/fakes runs inside the data_ingest production image (decision tj-j4wknb R4), which '
        'holds no pytest, no testing-group package, no mock library and no test package:\n' + '\n'.join(violations)
    )


def test_the_checker_refuses_what_the_production_image_lacks_and_accepts_what_it_has():
    refused = [
        'pytest',
        '_pytest.monkeypatch',
        'pytest_asyncio',
        'unittest.mock',
        'mock',
        'aiosqlite',
        'data.ingest.tests.test_broker_api',
        'common.tests.kafka',
        'data.store.app.main',
        'tools.agent_mcp',
    ]
    accepted = [
        'asyncio',
        'datetime',
        'alpaca.data.historical',
        'fastapi',
        'common.enums.data_stock',
        'data.ingest.app.main',
        'tests.fakes.market_data',
    ]

    assert [module for module in refused if import_is_allowed(module)] == []
    assert [module for module in accepted if not import_is_allowed(module)] == []


def test_every_allowed_third_party_name_is_a_production_dependency_of_data_ingest():
    groups = tomllib.loads((REPO_ROOT / 'pyproject.toml').read_text())['dependency-groups']
    declared = {
        re.match(r'[A-Za-z0-9._-]+', requirement).group(0).lower().replace('_', '-')
        for group in PRODUCTION_GROUPS
        for requirement in groups[group]
    }
    distributions = packages_distributions()

    undeclared = {
        name: distributions.get(name)
        for name in PRODUCTION_THIRD_PARTY
        if not {dist.lower().replace('_', '-') for dist in distributions.get(name, [])} & declared
    }

    assert not undeclared, f'allowed import names whose distribution is not in {PRODUCTION_GROUPS}: {undeclared}'


def test_the_fakes_package_holds_no_module_pytest_would_collect():
    # pytest.ini keeps default python_files (test_*.py, *_test.py). tests/fakes must add nothing to
    # what make test collects.
    collectable = [
        path.name for path in fakes_modules() if path.name.startswith('test_') or path.name.endswith('_test.py')
    ]
    assert collectable == []
