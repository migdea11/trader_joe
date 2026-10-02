"""FakeRead: a deterministic BrokerRead for tests, seeds and the fake-mode stack.

Decision tj-j4wknb (R3, R4, addenda 2-5) and tj-vhboky.54 DECISION 3 as amended. FakeRead
conforms to data/ingest/app/brokers/interface.py's BrokerRead Protocol STRUCTURALLY: it inherits
nothing, not even from AlpacaRead, and carries no capability flags (none exist, addendum 4 item 5).
The launcher installs it in the DataSource.ALPACA_API slot, so its bars are labelled ALPACA; a
DataSource.FAKE member would be test instrumentation in production code (R4).

THE BARS. Every bar is a pure function of (symbol, timestamp) -- fake_bar() below -- so a test or
the seed producer computes what must come back instead of recording it. Timestamps lie on a grid
ANCHORED AT THE UNIX EPOCH: GRID_EPOCH + k * granularity.offset for integer k. Anchoring at the
epoch rather than at the query's start means two overlapping queries agree on every shared
timestamp, and which bars GAPS drops is itself a function of the timestamp, never of where the
query began. Granularity.ONE_MONTH steps 28 days, because that is its offset.

THE RANGE is half-open, [start, end), the same for every broker (addendum 5, tj-irhy0a.15): a bar
at t is served when start <= t < end, compared as instants, so a grid point exactly at end is
NEVER served. Bars are aware UTC whatever offset the query's bounds carry, ascending and unique.

AN OPEN-ENDED QUERY (end None) is bounded by a COUNT, not by the clock: it serves the first
OPEN_ENDED_STEPS grid points at or after start, exactly as a query whose end were that many steps
on. A wall-clock bound would make two identical queries disagree across a step boundary; an
unbounded one would never finish. Scenarios then apply to that range as to any other, so no
scenario runs forever.

SCENARIOS are chosen by the requested symbol's PREFIX (no control channel, no restart, parallel
tests cannot interfere). Import the constants rather than retyping them. data_store upper-cases
symbols, so every prefix is upper case. No prefix starts with another (FAILONCE_ is not FAIL_
followed by more), so at most one ever matches and the order they are tried in cannot matter.

* default (no prefix matches) -- every grid bar of the range.
* EMPTY_ -- a response with zero bars.
* GAPS_ -- every other bar missing: only grid points with an EVEN index k are served.
* FAIL_ -- get_bars raises FakeReadError and yields nothing, on every call. That is the
  interface's failure contract in PR 1 (addendum 2 section B); ingest_control turns it into the
  bare {} at the Kafka RPC edge exactly as it does for the real reader. PR 2's typed errors
  tighten both.
* SLOW_ -- sleeps slow_delay_seconds (default DEFAULT_SLOW_DELAY_SECONDS, longer than
  data_store's 5 s RPC deadline; refused above MAX_SLOW_DELAY_SECONDS), then serves as default.
* FAILONCE_ -- the first call for a given (symbol, granularity, start, end) fails as FAIL_ does;
  every later call serves as default. The memory is this INSTANCE's, in process: a service that
  runs more than one worker process fails once PER WORKER, so the fake-mode stack must run
  data_ingest with SERVICE_WORKERS=1.

A true hang is not a scenario: nothing here blocks forever.
"""

import asyncio
import hashlib
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from enum import StrEnum

from common.enums.data_stock import Feed, Granularity
from data.ingest.app.brokers.interface import Bar, BarsQuery, BarsResponse


# The entitlement every fake response reports, known before iteration as the interface requires.
FEED = Feed.IEX

# Every bar timestamp is GRID_EPOCH + k * granularity.offset for an integer k.
GRID_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# How many grid points an open-ended query (end None) serves, counted from the first at or after start.
OPEN_ENDED_STEPS = 100

# Longer than data_store's 5 s Kafka RPC deadline, so SLOW_ outlives the caller by default.
DEFAULT_SLOW_DELAY_SECONDS = 8.0
# SLOW_ is bounded: a delay above this is refused when the fake is built, never slept.
MAX_SLOW_DELAY_SECONDS = 60.0

EMPTY_PREFIX = 'EMPTY_'
GAPS_PREFIX = 'GAPS_'
FAIL_PREFIX = 'FAIL_'
SLOW_PREFIX = 'SLOW_'
FAILONCE_PREFIX = 'FAILONCE_'


class Scenario(StrEnum):
    """What FakeRead does for a symbol, chosen by the symbol's prefix."""

    DEFAULT = 'default'
    EMPTY = 'empty'
    GAPS = 'gaps'
    FAIL = 'fail'
    SLOW = 'slow'
    FAILONCE = 'failonce'


# Every scenario but DEFAULT, by the prefix that selects it.
SCENARIO_PREFIXES: dict[str, Scenario] = {
    EMPTY_PREFIX: Scenario.EMPTY,
    GAPS_PREFIX: Scenario.GAPS,
    FAIL_PREFIX: Scenario.FAIL,
    SLOW_PREFIX: Scenario.SLOW,
    FAILONCE_PREFIX: Scenario.FAILONCE,
}


class FakeReadError(Exception):
    """The failure FAIL_ and FAILONCE_ raise from get_bars, standing in for a vendor failure."""


def scenario_for(symbol: str) -> Scenario:
    """Choose the scenario a symbol selects.

    Args:
        symbol (str): Requested symbol.

    Returns:
        Scenario: The scenario of the one prefix the symbol starts with, or DEFAULT.
    """
    for prefix in SCENARIO_PREFIXES:
        if symbol.startswith(prefix):
            return SCENARIO_PREFIXES[prefix]
    return Scenario.DEFAULT


def grid_index(granularity: Granularity, timestamp: datetime) -> int:
    """Index k of the grid point at or before a timestamp: GRID_EPOCH + k * granularity.offset.

    Args:
        granularity (Granularity): Grid step.
        timestamp (datetime): Timezone-aware instant.

    Returns:
        int: The grid index, floor-rounded.
    """
    return (timestamp - GRID_EPOCH) // granularity.offset


def grid_timestamps(granularity: Granularity, start: datetime, end: datetime | None) -> Iterator[datetime]:
    """Every grid point of [start, end), ascending, aware UTC.

    Args:
        granularity (Granularity): Grid step.
        start (datetime): Inclusive start, timezone aware.
        end (datetime | None): Exclusive end, timezone aware, or None for OPEN_ENDED_STEPS points.

    Yields:
        datetime: Each grid point t with start <= t < end.
    """
    step = granularity.offset
    # Ceiling division: the index of the first grid point at or after start.
    k = -((GRID_EPOCH - start) // step)
    stop = GRID_EPOCH + (k + OPEN_ENDED_STEPS) * step if end is None else end
    timestamp = GRID_EPOCH + k * step
    while timestamp < stop:
        yield timestamp
        timestamp += step


def _unit(digest: bytes, slot: int) -> float:
    """Read four bytes of a digest as a number in [0, 1)."""
    return int.from_bytes(digest[slot * 4 : slot * 4 + 4], 'big') / 2**32


def fake_bar(symbol: str, timestamp: datetime) -> Bar:
    """The bar FakeRead serves for a symbol at a timestamp: a pure function of the two.

    Built from SHA-256 rather than hash(), which is salted per process: the seed producer and the
    stack must compute the same bars in different interpreters. Prices sit near a per-symbol level
    between 10 and 500 and satisfy low <= open, close, vwap <= high. volume and trade_count are
    always ints and never None.

    Args:
        symbol (str): Requested symbol, prefix included.
        timestamp (datetime): Bar start, timezone aware.

    Returns:
        Bar: The bar, stamped in UTC.
    """
    utc = timestamp.astimezone(UTC)
    level = 10.0 + 490.0 * _unit(hashlib.sha256(symbol.encode()).digest(), 0)
    digest = hashlib.sha256(f'{symbol}|{utc.isoformat()}'.encode()).digest()
    open_ = round(level * (0.98 + 0.04 * _unit(digest, 0)), 4)
    close = round(level * (0.98 + 0.04 * _unit(digest, 1)), 4)
    high = round(max(open_, close) * (1 + 0.01 * _unit(digest, 2)), 4)
    low = round(min(open_, close) * (1 - 0.01 * _unit(digest, 3)), 4)
    return Bar(
        timestamp=utc,
        open=open_,
        high=high,
        low=low,
        close=close,
        # Both ints, never None: the store's batch schema requires int volume and trade_count, and
        # a None there is an open question on tj-irhy0a.7, not something the fake should provoke.
        volume=1_000 + int(99_000 * _unit(digest, 4)),
        trade_count=10 + int(990 * _unit(digest, 5)),
        vwap=round(low + (high - low) * _unit(digest, 6), 4),
    )


class FakeRead:
    """A deterministic BrokerRead: bars from fake_bar(), behaviour chosen by the symbol's prefix.

    See the module docstring for the grid, the half-open range, the open-ended bound and each
    scenario.

    Args:
        slow_delay_seconds (float): How long SLOW_ sleeps before serving. Finite, 0 to
            MAX_SLOW_DELAY_SECONDS inclusive.
        sleep (Callable[[float], Awaitable[None]]): What SLOW_ awaits; asyncio.sleep unless a
            test injects its own.

    Raises:
        ValueError: If slow_delay_seconds is not finite or lies outside 0 to MAX_SLOW_DELAY_SECONDS.
    """

    def __init__(
        self,
        slow_delay_seconds: float = DEFAULT_SLOW_DELAY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not (math.isfinite(slow_delay_seconds) and 0 <= slow_delay_seconds <= MAX_SLOW_DELAY_SECONDS):
            raise ValueError(
                f'slow_delay_seconds must be finite and within 0 to {MAX_SLOW_DELAY_SECONDS}: '
                f'slow_delay_seconds={slow_delay_seconds!r}'
            )
        self.__slow_delay_seconds = slow_delay_seconds
        self.__sleep = sleep
        self.__failed_once: set[tuple[str, Granularity, datetime, datetime | None]] = set()

    @property
    def slow_delay_seconds(self) -> float:
        """How long SLOW_ sleeps before serving, in seconds."""
        return self.__slow_delay_seconds

    async def get_bars(self, query: BarsQuery) -> BarsResponse:
        """Serve fake bars for one instrument, per the scenario its symbol selects.

        Args:
            query (BarsQuery): What to fetch.

        Returns:
            BarsResponse: FEED and the bars of [start, end), or of OPEN_ENDED_STEPS grid points
                when end is None.

        Raises:
            FakeReadError: For FAIL_ always, and for FAILONCE_ on the first call per range.
        """
        symbol = query.instrument.symbol
        scenario = scenario_for(symbol)
        if scenario is Scenario.FAIL:
            raise FakeReadError(f'FAIL scenario: symbol={symbol!r}')
        if scenario is Scenario.FAILONCE:
            key = (symbol, query.granularity, query.start, query.end)
            if key not in self.__failed_once:
                self.__failed_once.add(key)
                raise FakeReadError(f'FAILONCE scenario, first call: symbol={symbol!r}')
        if scenario is Scenario.SLOW:
            await self.__sleep(self.__slow_delay_seconds)
        return BarsResponse(feed=FEED, bars=self.__iterate(query, scenario))

    @staticmethod
    async def __iterate(query: BarsQuery, scenario: Scenario) -> AsyncIterator[Bar]:
        if scenario is Scenario.EMPTY:
            return
        for timestamp in grid_timestamps(query.granularity, query.start, query.end):
            if scenario is Scenario.GAPS and grid_index(query.granularity, timestamp) % 2:
                continue
            yield fake_bar(query.instrument.symbol, timestamp)
