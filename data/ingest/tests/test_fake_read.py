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
from data.ingest.app import app_depends, ingest_control
from data.ingest.app.brokers.alpaca.broker_codes import AlpacaGranularity
from data.ingest.app.brokers.interface import Bar, BarsQuery, BrokerRead, Instrument
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.tests.grpc_bind import LoopbackGrpc
from routers.data_ingest import get_dataset_request
from schemas.data_ingest.get_dataset_request import StockDatasetRequest
from tests.fakes.market_data import (
    DEFAULT_SLOW_DELAY_SECONDS,
    EMPTY_PREFIX,
    FAIL_PREFIX,
    FAILONCE_PREFIX,
    GAPS_PREFIX,
    MAX_SLOW_DELAY_SECONDS,
    OPEN_ENDED_STEPS,
    SCENARIO_PREFIXES,
    SLOW_PREFIX,
    FakeRead,
    FakeReadError,
    Scenario,
    fake_bar,
    scenario_for,
)


pytestmark = pytest.mark.data_ingest

REPO_ROOT = Path(__file__).resolve().parents[3]
FAKES_DIR = REPO_ROOT / 'tests' / 'fakes'
LAUNCHER_MODULE = 'tests.fakes.ingest_launcher'

# data_store's Kafka RPC deadline (common/kafka/rpc/kafka_rpc_client.py, timeout default 5). SLOW_
# exists to outlive it.
RPC_DEADLINE_SECONDS = 5.0

HOUR = timedelta(hours=1)
# 2026-01-05 is a Monday; 14:00 UTC is on the ONE_HOUR grid (whole hours since the epoch).
ON_GRID = datetime(2026, 1, 5, 14, tzinfo=UTC)
PLUS_FIVE = timezone(timedelta(hours=5))


def query(
    symbol: str,
    start: datetime = ON_GRID,
    end: datetime | None = ON_GRID + 4 * HOUR,
    granularity: Granularity = Granularity.ONE_HOUR,
) -> BarsQuery:
    """Build a BarsQuery with the priority stated explicitly, as the interface requires.

    Args:
        symbol (str): Requested symbol, scenario prefix included.
        start (datetime): Inclusive start.
        end (datetime | None): Exclusive end, or None.
        granularity (Granularity): Bar size.

    Returns:
        BarsQuery: The query.
    """
    return BarsQuery(
        instrument=Instrument(symbol=symbol, asset_type=AssetType.STOCK),
        granularity=granularity,
        start=start,
        end=end,
        priority=RequestPriority.INTERACTIVE,
    )


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
        env={'PYTHONPATH': str(REPO_ROOT), 'PYTHONHASHSEED': '12345'},
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
    # FAIL_ and FAILONCE_'s first call raise; the point is that every scenario RETURNS, inside a
    # hard timeout, with at most OPEN_ENDED_STEPS bars.
    reader = FakeRead(slow_delay_seconds=0.0)

    async def settle() -> list[Bar] | FakeReadError:
        try:
            return await served(reader, query(f'{prefix}VFV', end=None), cap=OPEN_ENDED_STEPS + 1)
        except FakeReadError as error:
            return error

    outcome = await asyncio.wait_for(settle(), timeout=10)

    assert isinstance(outcome, FakeReadError) or len(outcome) <= OPEN_ENDED_STEPS


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
async def test_gaps_serves_exactly_the_even_grid_indices_and_nothing_else():
    # The grid index of an hour is hours since the epoch; 2026-01-05 14:00 UTC is 20_458 days and
    # 14 hours on, index 491_006, even, so of four hours from ON_GRID the 14:00 and 16:00 bars remain.
    symbol = f'{GAPS_PREFIX}VFV'
    assert (ON_GRID - datetime(1970, 1, 1, tzinfo=UTC)) // HOUR == 20_458 * 24 + 14 == 491_006

    bars = await served(FakeRead(), query(symbol))

    assert bars == [fake_bar(symbol, ON_GRID), fake_bar(symbol, ON_GRID + 2 * HOUR)]


@pytest.mark.asyncio
async def test_fail_raises_on_every_call_and_never_yields_a_response():
    reader = FakeRead()

    for _ in range(2):
        with pytest.raises(FakeReadError):
            await reader.get_bars(query(f'{FAIL_PREFIX}VFV'))


@pytest.mark.asyncio
async def test_failonce_fails_the_first_call_per_range_then_serves_the_default_bars():
    reader = FakeRead()
    symbol = f'{FAILONCE_PREFIX}VFV'

    with pytest.raises(FakeReadError):
        await reader.get_bars(query(symbol))
    second = await served(reader, query(symbol))
    third = await served(reader, query(symbol))

    assert second == third == [fake_bar(symbol, timestamp) for timestamp in hours(ON_GRID, 4)]


@pytest.mark.asyncio
async def test_failonce_remembers_per_range_and_per_instance():
    reader = FakeRead()
    symbol = f'{FAILONCE_PREFIX}VFV'
    with pytest.raises(FakeReadError):
        await reader.get_bars(query(symbol))

    # A different range of the same symbol is a first call of its own.
    with pytest.raises(FakeReadError):
        await reader.get_bars(query(symbol, end=ON_GRID + 5 * HOUR))
    # The same range spelled in another offset is the same range.
    await reader.get_bars(query(symbol, start=ON_GRID.astimezone(PLUS_FIVE)))
    # The memory is the instance's: a second process (a second worker) fails again. Hence
    # SERVICE_WORKERS=1 for the fake-mode stack.
    with pytest.raises(FakeReadError):
        await FakeRead().get_bars(query(symbol))


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


def test_slow_outlives_the_rpc_deadline_by_default_and_is_bounded():
    assert FakeRead().slow_delay_seconds == DEFAULT_SLOW_DELAY_SECONDS > RPC_DEADLINE_SECONDS
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


@pytest.fixture
def fake_installed() -> Iterator[FakeRead]:
    """Install a FakeRead in the ALPACA_API slot of ingest_control, and clear it afterwards."""
    reader = FakeRead()
    ingest_control.install_readers({DataSource.ALPACA_API: reader})
    yield reader
    ingest_control.clear_readers()


@pytest.mark.asyncio
async def test_ingest_control_stores_the_fake_bars_under_the_fake_feed(fake_installed: FakeRead):
    batch = await ingest_control.store_retrieve_stock(stock_request('VFV'))

    assert batch.feed is Feed.IEX
    assert [created.data.close for created in batch.dataset[DataType.MARKET_ACTIVITY]] == [
        fake_bar('VFV', timestamp).close for timestamp in hours(ON_GRID, 4)
    ]


@pytest.mark.asyncio
async def test_ingest_control_turns_a_fail_into_the_bare_empty_answer_as_for_the_real_reader(fake_installed: FakeRead):
    # tj-fe19tu, kept at the RPC edge until PR 2: a failed fetch is a bare {}, never a batch.
    assert await ingest_control.store_retrieve_stock(stock_request(f'{FAIL_PREFIX}VFV')) == {}


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

    Args:
        **environ (str): Variables to set beside PYTHONPATH.

    Returns:
        subprocess.CompletedProcess: The finished probe.
    """
    return subprocess.run(
        [sys.executable, '-c', LAUNCHER_PROBE],
        cwd=REPO_ROOT,
        env={'PYTHONPATH': str(REPO_ROOT), **environ},
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
async def test_the_launcher_apps_lifespan_installs_a_fake_read_for_alpaca_and_clears_it(monkeypatch):
    # Kafka is the only thing stubbed: the wait for a broker, the RPC servers' consumers and the
    # latency server. The lifespan itself, create_app and ingest_control are production's, and so is
    # the gRPC host it starts, bound to loopback here as the overlay binds it to the compose alias.
    installed: list[dict[DataSource, Any]] = []
    real_install = ingest_control.install_readers

    def recording_install(readers):
        installed.append(dict(readers))
        real_install(readers)

    monkeypatch.setattr(app_depends, 'initialize_latency_server', Mock())
    monkeypatch.setattr(app_depends, 'init_debugger', Mock())
    monkeypatch.setattr(app_depends.KafkaConsumerFactory, 'wait_for_kafka', Mock())
    monkeypatch.setattr(get_dataset_request.rpc, 'init_servers', Mock())
    monkeypatch.setattr(ingest_control, 'install_readers', recording_install)
    launcher = importlib.import_module(LAUNCHER_MODULE)
    assert isinstance(launcher.app, FastAPI)

    async with LoopbackGrpc(), launcher.app.router.lifespan_context(launcher.app):
        batch = await ingest_control.store_retrieve_stock(stock_request('VFV'))

    assert [set(readers) for readers in installed] == [{DataSource.ALPACA_API}]
    assert installed[0][DataSource.ALPACA_API] is launcher.reader
    assert isinstance(launcher.reader, FakeRead)
    # Dispatch really went through the installed fake: its bars, its feed.
    assert batch.feed is Feed.IEX
    assert [created.data.close for created in batch.dataset[DataType.MARKET_ACTIVITY]] == [
        fake_bar('VFV', timestamp).close for timestamp in hours(ON_GRID, 4)
    ]
    # And teardown cleared it.
    with pytest.raises(NotImplementedError):
        await ingest_control.store_retrieve_stock(stock_request('VFV'))


# ---------------------------------------------------------------------------------------------
# Import discipline: tests/fakes runs inside the production image
# ---------------------------------------------------------------------------------------------

# data_ingest's production dependencies, by IMPORT name: the base and data-ingest groups the prod
# image installs (Dockerfile: uv sync --only-group base --only-group data-ingest). The test below
# ties every name back to a distribution those groups declare, so this list cannot drift into the
# testing group.
PRODUCTION_THIRD_PARTY = frozenset(
    {'aenum', 'alpaca', 'dotenv', 'fastapi', 'httpx', 'kafka', 'pydantic', 'starlette', 'uvicorn'}
)
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
