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

THE TYPED RESULT (ADR tj-fa1rpu D1(a), D2, D3; TE-5 tj-3mk3u5.37.6). get_bars RETURNS a
BarsResponse (SERVED) or a BarsFailure, exactly as AlpacaRead does, and never raises for either.

* SERVED carries served_range and as_of. as_of is when the fake "answered", read from its clock
  after any SLOW_ delay. served_range starts at the query's start and ends at the range's end
  (end, or the OPEN_ENDED_STEPS bound for end None) clamped to as_of: a vendor cannot have answered
  for time after it answered, and a window this fake clamps by count is reported as clamped (D3
  note a), never as the whole open range. Every bar served lies inside served_range.
* Three REFUSALS come before any "vendor" work -- before a scenario is looked at, so no SLOW_
  sleep is slept and no FAILONCE_ memory is spent -- mirroring AlpacaRead and the Q-EMPTY ruling
  (tj-3mk3u5.37.1), so fake mode can show each end to end:
    - a query naming a feed other than this fake's deployment feed: FEED_NOT_AVAILABLE
      (BrokerUnsupportedError). The deployment feed is the `feed` argument, FEED unless set, so
      in the fake-mode stack a request naming SIP is the rejection case (tj-3mk3u5.9, 16:36 UTC
      2026-09-28: the fake's feed-entitlement setting);
    - a start at or after the fake's clock: RANGE_IN_FUTURE;
    - an end before the start: VENDOR_INVALID_REQUEST, as Alpaca's recorded 400 answers it.
* A vendor failure (FAIL_, FAILONCE_, RATELIMIT_) carries a FakeReadError as its error's
  __cause__, standing in for the exception a real adapter catches and keeps (D8, the 16:22 UTC
  2026-10-02 addendum item 5).

SCENARIOS are chosen by the requested symbol's PREFIX (no control channel, no restart, parallel
tests cannot interfere). Import the constants rather than retyping them. data_store upper-cases
symbols, so every prefix is upper case. No prefix starts with another (FAILONCE_ is not FAIL_
followed by more), so at most one ever matches and the order they are tried in cannot matter.

* default (no prefix matches) -- every grid bar of the range.
* EMPTY_ -- SERVED with zero bars, served_range as for any range (Q-EMPTY: never a failure).
* GAPS_ -- every other bar missing: only grid points with an EVEN index k are served.
* FAIL_ -- a BarsFailure, VENDOR_UNAVAILABLE, on every call, and nothing served. It reaches the
  caller as the typed failure it is: the ingest_control edge that used to turn any BarsFailure
  into a bare {} was deleted on tj-3mk3u5.11, so nothing swallows it any more.
* SLOW_ -- sleeps slow_delay_seconds (default DEFAULT_SLOW_DELAY_SECONDS, refused above
  MAX_SLOW_DELAY_SECONDS), then serves as default. Slow but SERVED: see the constant's own note
  for why it no longer outlives its caller's deadline.
* FAILONCE_ -- the first call for a given (symbol, granularity, start, end) fails as FAIL_ does;
  every later call serves as default. The memory is this INSTANCE's, in process: a service that
  runs more than one worker process fails once PER WORKER, so the fake-mode stack must run
  data_ingest with SERVICE_WORKERS=1.
* RATELIMIT_ -- a BarsFailure, VENDOR_RATE_LIMITED, on every call, whose reset_at is the fake's
  clock plus RATELIMIT_RESET_SECONDS: deterministic under an injected clock.

A true hang is not a scenario: nothing here blocks forever.
"""

import asyncio
import hashlib
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from common.enums.data_stock import Feed, Granularity
from common.errors.vocabulary import ExogenousError, InvalidRequestError, Reason
from data.ingest.app.brokers.interface import (
    Bar,
    BarsFailure,
    BarsQuery,
    BarsResponse,
    BrokerUnsupportedError,
    ServedRange,
)


# The deployment feed a FakeRead serves unless built with another: the entitlement every fake
# response reports, known before iteration as the interface requires.
FEED = Feed.IEX

# Every bar timestamp is GRID_EPOCH + k * granularity.offset for an integer k.
GRID_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# How many grid points an open-ended query (end None) serves, counted from the first at or after start.
OPEN_ENDED_STEPS = 100

# A vendor that is SLOW BUT STILL SERVED. This was chosen to be longer than data_store's 5 s Kafka
# RPC deadline, so that SLOW_ outlived its caller; tj-3mk3u5.10 replaced that transport and set
# DEFAULT_FETCH_DEADLINE_S to 300 s deliberately, because 5 s abandoned requests the vendor was
# still serving. So 8 s outlives nothing now, and nothing permitted here would: the ceiling below
# is 60 s. tests/system/test_ingest_e2e.py asserts the served outcome and reversed its own
# expectation on tj-xhcoyc item 2 for this reason.
DEFAULT_SLOW_DELAY_SECONDS = 8.0
# SLOW_ is bounded: a delay above this is refused when the fake is built, never slept.
MAX_SLOW_DELAY_SECONDS = 60.0

# How far past the fake's clock a RATELIMIT_ failure says its window resets.
RATELIMIT_RESET_SECONDS = 60.0

# The detail of the end-before-start refusal: Alpaca's own recorded 400 message (error_400.json).
END_BEFORE_START_DETAIL = 'end should not be before start'

EMPTY_PREFIX = 'EMPTY_'
GAPS_PREFIX = 'GAPS_'
FAIL_PREFIX = 'FAIL_'
SLOW_PREFIX = 'SLOW_'
FAILONCE_PREFIX = 'FAILONCE_'
RATELIMIT_PREFIX = 'RATELIMIT_'


class Scenario(StrEnum):
    """What FakeRead does for a symbol, chosen by the symbol's prefix."""

    DEFAULT = 'default'
    EMPTY = 'empty'
    GAPS = 'gaps'
    FAIL = 'fail'
    SLOW = 'slow'
    FAILONCE = 'failonce'
    RATELIMIT = 'ratelimit'


# Every scenario but DEFAULT, by the prefix that selects it.
SCENARIO_PREFIXES: dict[str, Scenario] = {
    EMPTY_PREFIX: Scenario.EMPTY,
    GAPS_PREFIX: Scenario.GAPS,
    FAIL_PREFIX: Scenario.FAIL,
    SLOW_PREFIX: Scenario.SLOW,
    FAILONCE_PREFIX: Scenario.FAILONCE,
    RATELIMIT_PREFIX: Scenario.RATELIMIT,
}


class FakeReadError(Exception):
    """The vendor-side exception a FAIL_, FAILONCE_ or RATELIMIT_ failure carries as its error's __cause__.

    Never raised out of get_bars: it stands for what a real adapter catches from its vendor and keeps
    as the cause of the typed error it returns (ADR tj-fa1rpu D8, the 16:22 UTC 2026-10-02 addendum).
    """


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
    stop = range_end(granularity, start, end)
    timestamp = GRID_EPOCH + _first_index(granularity, start) * step
    while timestamp < stop:
        yield timestamp
        timestamp += step


def _first_index(granularity: Granularity, start: datetime) -> int:
    # Ceiling division: the index of the first grid point at or after start.
    return -((GRID_EPOCH - start) // granularity.offset)


def range_end(granularity: Granularity, start: datetime, end: datetime | None) -> datetime:
    """The exclusive end of the range FakeRead serves for [start, end), before any clamp to its clock.

    Args:
        granularity (Granularity): Grid step.
        start (datetime): Inclusive start, timezone aware.
        end (datetime | None): Exclusive end, timezone aware, or None for an open-ended query.

    Returns:
        datetime: end itself, or for end None the grid point OPEN_ENDED_STEPS steps on from the first
            grid point at or after start: the count bound.
    """
    if end is not None:
        return end
    return GRID_EPOCH + (_first_index(granularity, start) + OPEN_ENDED_STEPS) * granularity.offset


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

    See the module docstring for the grid, the half-open range, the open-ended bound, the typed
    result, the refusals and each scenario.

    Args:
        slow_delay_seconds (float): How long SLOW_ sleeps before serving. Finite, 0 to
            MAX_SLOW_DELAY_SECONDS inclusive.
        sleep (Callable[[float], Awaitable[None]]): What SLOW_ awaits; asyncio.sleep unless a
            test injects its own.
        clock (Callable[[], datetime]): Yields now, timezone aware: the future-range refusal, the
            as_of a response is stamped with, the end served_range is clamped to and RATELIMIT_'s
            reset_at all read it. Injectable, as AlpacaRead's is, so tests need not sleep.
        feed (Feed): The deployment feed: the tape this fake serves, and the only one a query may
            name. FEED unless set.

    Raises:
        ValueError: If slow_delay_seconds is not finite or lies outside 0 to MAX_SLOW_DELAY_SECONDS.
    """

    def __init__(
        self,
        slow_delay_seconds: float = DEFAULT_SLOW_DELAY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = _utc_now,
        feed: Feed = FEED,
    ) -> None:
        if not (math.isfinite(slow_delay_seconds) and 0 <= slow_delay_seconds <= MAX_SLOW_DELAY_SECONDS):
            raise ValueError(
                f'slow_delay_seconds must be finite and within 0 to {MAX_SLOW_DELAY_SECONDS}: '
                f'slow_delay_seconds={slow_delay_seconds!r}'
            )
        self.__slow_delay_seconds = slow_delay_seconds
        self.__sleep = sleep
        self.__clock = clock
        self.__feed = feed
        self.__failed_once: set[tuple[str, Granularity, datetime, datetime | None]] = set()

    @property
    def slow_delay_seconds(self) -> float:
        """How long SLOW_ sleeps before serving, in seconds."""
        return self.__slow_delay_seconds

    @property
    def feed(self) -> Feed:
        """The deployment feed this fake serves."""
        return self.__feed

    async def get_bars(self, query: BarsQuery) -> BarsResponse | BarsFailure:
        """Serve fake bars for one instrument, per the scenario its symbol selects.

        Args:
            query (BarsQuery): What to fetch.

        Returns:
            BarsResponse | BarsFailure: The deployment feed, the bars of [start, end) (or of
                OPEN_ENDED_STEPS grid points when end is None) up to as_of, served_range and as_of;
                or the failure: FEED_NOT_AVAILABLE, RANGE_IN_FUTURE or VENDOR_INVALID_REQUEST before
                any scenario, VENDOR_UNAVAILABLE for FAIL_ and a first FAILONCE_, VENDOR_RATE_LIMITED
                for RATELIMIT_.
        """
        refusal = self.__refusal(query)
        if refusal is not None:
            return BarsFailure(refusal)

        symbol = query.instrument.symbol
        scenario = scenario_for(symbol)
        if scenario is Scenario.FAIL:
            return self.__unavailable(f'FAIL scenario: symbol={symbol!r}')
        if scenario is Scenario.FAILONCE:
            key = (symbol, query.granularity, query.start, query.end)
            if key not in self.__failed_once:
                self.__failed_once.add(key)
                return self.__unavailable(f'FAILONCE scenario, first call: symbol={symbol!r}')
        if scenario is Scenario.RATELIMIT:
            return self.__rate_limited(symbol)
        if scenario is Scenario.SLOW:
            await self.__sleep(self.__slow_delay_seconds)

        # When the "vendor" answered: after any delay, so a slow answer is stamped late, as a real one is.
        as_of = self.__clock().astimezone(UTC)
        served_end = min(range_end(query.granularity, query.start, query.end), as_of)
        return BarsResponse(
            feed=self.__feed,
            bars=self.__iterate(query, scenario, served_end),
            served_range=ServedRange(query.start, served_end),
            as_of=as_of,
        )

    def __refusal(self, query: BarsQuery) -> BrokerUnsupportedError | InvalidRequestError | None:
        """The refusal a query earns before any "vendor" work, or None to go on to the scenario."""
        if query.feed is not None and query.feed != self.__feed:
            return BrokerUnsupportedError(
                Reason.FEED_NOT_AVAILABLE,
                f'This deployment cannot serve the requested feed: feed={query.feed.value}',
                metadata={'feed': query.feed.value},
            )
        now = self.__clock()
        if query.start >= now:
            return InvalidRequestError(
                Reason.RANGE_IN_FUTURE,
                f'The range starts at or after now: start={query.start.isoformat()}, now={now.isoformat()}',
                metadata={'range_start': query.start.isoformat()},
            )
        if query.end is not None and query.end < query.start:
            return InvalidRequestError(Reason.VENDOR_INVALID_REQUEST, END_BEFORE_START_DETAIL)
        return None

    @staticmethod
    def __unavailable(cause: str) -> BarsFailure:
        error = ExogenousError(Reason.VENDOR_UNAVAILABLE, 'The fake vendor failed on its side.')
        error.__cause__ = FakeReadError(cause)
        return BarsFailure(error)

    def __rate_limited(self, symbol: str) -> BarsFailure:
        error = ExogenousError(
            Reason.VENDOR_RATE_LIMITED,
            'The fake vendor rate-limited the request.',
            reset_at=self.__clock() + timedelta(seconds=RATELIMIT_RESET_SECONDS),
            clock=self.__clock,
        )
        error.__cause__ = FakeReadError(f'RATELIMIT scenario: symbol={symbol!r}')
        return BarsFailure(error)

    @staticmethod
    async def __iterate(query: BarsQuery, scenario: Scenario, served_end: datetime) -> AsyncIterator[Bar]:
        if scenario is Scenario.EMPTY:
            return
        for timestamp in grid_timestamps(query.granularity, query.start, served_end):
            if scenario is Scenario.GAPS and grid_index(query.granularity, timestamp) % 2:
                continue
            yield fake_bar(query.instrument.symbol, timestamp)
