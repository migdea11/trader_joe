"""The BrokerRead contract suite: one set of assertions, run against every implementation.

Decision tj-j4wknb R3 and addendum 2 A: BrokerRead is a typing.Protocol and the repo runs no type
checker, so THIS suite is the conformance check a type checker would otherwise be -- structure AND
behaviour, in the PR gate. Every assertion below is written once and runs for every parameter of
the `subject` fixture; no assertion branches on which implementation it is looking at.

WHAT A SUBJECT IS. A Subject ARRANGES, it never asserts: for each contract case it hands back a
reader and a query that exercise that case for its own implementation -- FakeRead picks a symbol
prefix and a point on its epoch grid, AlpacaRead picks a recorded vendor body (FAKES-2's harness,
data/ingest/tests/alpaca_recorded.py, imported, not copied). What is true of the answer is then
asserted identically. The only per-implementation DATA is what the design makes per-implementation:
the granularities an adapter maps and the inputs it refuses. Every reader is built on one injected
clock, NOW, so as_of and every 'before the clock' refusal are the same instant for every subject.

VENDOR WORK. An arrangement also says how much 'vendor' work its reader has done: for AlpacaRead,
requests that reached the transport plus rate tokens asked of the budget; for FakeRead, the sleeps
of its SLOW_ scenario, which runs only once a query has passed every refusal. A refusal 'before any
vendor call or rate token' is asserted as zero, and the same arrangement served shows more than
zero, so the zero is never vacuous.

THE CONTRACT, from decision tj-j4wknb and ADR tj-fa1rpu (TE-5 tj-3mk3u5.37.6 re-pointed it to the
typed outcomes of TE-4 tj-3mk3u5.37.5 and the Q-EMPTY ruling on tj-3mk3u5.37.1):
* conformance is STRUCTURAL (addendum 3 U7): nothing here requires inheriting any base class, and
  there are no capability flags to assert (addendum 4 item 5);
* get_bars RETURNS a BarsResponse (SERVED) or a BarsFailure, and never raises for either (D1(a), D6);
* SERVED: a feed (a Feed member, known before iteration, addendum 2 B), served_range and as_of (D2).
  as_of is the reader's clock, in UTC. served_range starts at the query's start, is never wider than
  asked, never ends after as_of (D3 note a), and holds every bar served;
* bars are aware UTC, strictly ascending (so unique) and within [start, end), HALF-OPEN on instants
  whatever offset the bounds carry (addendum 5, ruling on tj-irhy0a.15): the bar at start is served
  and a bar at end never is, exercised where each implementation WOULD have a bar at end;
* an empty range is SERVED with zero bars and served_range = the asked window, never a failure
  (Q-EMPTY);
* a range that starts in the past and ends after the clock is SERVED up to as_of, not refused;
* an open-ended query (end None) yields only the shared properties, never a count: FakeRead bounds
  it by count and Alpaca by 'up to now' (architect, 01:55 UTC 2026-09-30, FAKES-1 gate);
* a FAILURE is a BarsFailure whose error is a TraderJoeError of the branch REASONS names for its
  reason, and whose outcome is READ from REASONS, never stated on the failure itself (TE-4 item 1):
  a vendor failure is VENDOR_UNAVAILABLE and keeps the caught exception as its __cause__ (D8, the
  16:22 UTC 2026-10-02 addendum item 5); a vendor rate limit is VENDOR_RATE_LIMITED with a reset_at
  after the clock; an end before the start is VENDOR_INVALID_REQUEST;
* a named feed the deployment cannot serve is FEED_NOT_AVAILABLE, and a range starting at or after
  the clock is RANGE_IN_FUTURE, each before any vendor call or rate token; the deployment's own feed,
  named, is served;
* an input the implementation declares it cannot serve is a BarsFailure carrying a
  BrokerUnsupportedError with the reason it declares, before any vendor work, and an input it does
  NOT declare is served (architect, 01:55 UTC, refusals settled);
* every served bar's trade_count is None or exactly an int -- never a float, never a bool
  (addendum 2 B: Bar.trade_count is int | None, the same type from every broker, R2; tj-irhy0a.19);
* two identical requests give equal results.
No market-calendar property is asserted anywhere: FakeRead's grid is 24/7 and epoch-anchored.

THE GUARD: every BrokerRead implementation in the repository's production and fake trees must be a
subject here, so a new broker cannot ship without joining this suite.
"""

import ast
import dataclasses
import inspect
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

import pytest

from common.enums.data_select import AssetType
from common.enums.data_stock import Feed, Granularity
from common.errors.vocabulary import REASONS, Disposition, Outcome, Reason, TraderJoeError
from common.tests.roots import SERVER_ROOT, repo_relative, resolve_tree
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.broker_codes import AlpacaGranularity
from data.ingest.app.brokers.alpaca.read import AlpacaRead
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
from data.ingest.tests.alpaca_recorded import (
    RecordedResponse,
    always,
    by_page_token,
    load,
    record_sdk_sleeps,
    recorded_client,
)
from tests.fakes.market_data import (
    EMPTY_PREFIX,
    FAIL_PREFIX,
    GRID_EPOCH,
    RATELIMIT_PREFIX,
    SLOW_PREFIX,
    FakeRead,
    grid_index,
)


pytestmark = pytest.mark.data_ingest

# Where a BrokerRead implementation may live: data_ingest's production tree and the fakes the
# test-only launcher installs. The bead names data/ingest/app/brokers; the whole app tree is
# scanned so an implementation placed beside the brokers package cannot slip past.
#
# THE TWO ROOTS SIT IN ONE TUPLE (tj-iontkq.2): data/ingest/app travels with the services and
# tests/fakes stays at the top of the repository, so each name is resolved by resolve_tree() under
# the root that owns it rather than both under one. test_the_scan_roots_exist_and_hold_the_interface
# below is what would catch a wrong answer -- it asserts each root is a directory holding .py files.
IMPLEMENTATION_ROOTS = (resolve_tree('data/ingest/app'), resolve_tree('tests/fakes'))

# Bound offsets the range rule is checked under: it is on instants, whatever offset the query carries.
OFFSETS = pytest.mark.parametrize(
    'offset',
    [UTC, timezone(timedelta(hours=-5)), timezone(timedelta(hours=9, minutes=30))],
    ids=['Z', '-05:00', '+09:30'],
)

# More bars than any arrangement here serves; reaching it means an open-ended query did not end.
DRAIN_CAP = 10_000

# Every reader's clock. After every recorded bar (2022) and every FakeRead grid point used below
# but the straddling one, and off every grid.
NOW = datetime(2026, 10, 2, 12, 17, tzinfo=UTC)


def clock() -> datetime:
    return NOW


# ---------------------------------------------------------------------------------------------
# Subjects: one per implementation. They arrange; the tests assert.
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Arrangement:
    """A reader, the query that exercises one contract case on it, and its vendor-work count so far."""

    reader: BrokerRead
    query: BarsQuery
    vendor_work: Callable[[], int] = field(default=lambda: 0)


@dataclass(frozen=True)
class Unsupported:
    """One input an implementation declares it cannot serve: a field of the query or its instrument.

    Attributes:
        field (str): 'adjustment' (a BarsQuery field) or an Instrument field name.
        value (Any): The value that cannot be served.
        reason (Reason): The reason the refusal carries, one of BrokerUnsupportedError's three.
    """

    field: str
    value: Any
    reason: Reason

    @property
    def id(self) -> str:
        return f'{self.field}={self.value}'

    def apply(self, query: BarsQuery) -> BarsQuery:
        """Return the query with this input substituted, wherever the field lives."""
        if self.field == 'adjustment':
            return replace(query, adjustment=self.value)
        return replace(query, instrument=replace(query.instrument, **{self.field: self.value}))


def bars_query(symbol: str, granularity: Granularity, start: datetime, end: datetime | None) -> BarsQuery:
    return BarsQuery(
        instrument=Instrument(symbol, AssetType.STOCK),
        granularity=granularity,
        start=start,
        end=end,
        priority=RequestPriority.BACKFILL,
    )


class CountingBudget:
    """Alpaca's rate budget, replaced by one that grants every token and counts what it was asked."""

    # Read by the classifier only for a header-less 429.
    full_refill_seconds = 1.0

    def __init__(self) -> None:
        self.acquires: list[tuple[RequestPriority, datetime | None]] = []

    async def acquire(self, priority: RequestPriority = RequestPriority.INTERACTIVE, deadline=None) -> None:
        self.acquires.append((priority, deadline))


class Subject:
    """What a contract-suite parameter provides. Every method returns a fresh Arrangement.

    Attributes:
        implementation (type): The BrokerRead class under test; the guard compares these.
        granularities (tuple[Granularity, ...]): Every Granularity the implementation maps.
        unsupported (tuple[Unsupported, ...]): Inputs it declares it cannot serve.
    """

    implementation: ClassVar[type]
    granularities: ClassVar[tuple[Granularity, ...]]
    unsupported: ClassVar[tuple[Unsupported, ...]]

    def __init__(self, executor: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
        self.executor = executor
        self.monkeypatch = monkeypatch

    def servable(self) -> Arrangement:
        """A query that serves at least one bar."""
        raise NotImplementedError

    def watched(self) -> Arrangement:
        """A servable query whose vendor work is counted; served, its count is above zero."""
        raise NotImplementedError

    def boundary(self) -> Arrangement:
        """A query whose start AND end are instants where the implementation would have a bar."""
        raise NotImplementedError

    def at_granularity(self, granularity: Granularity) -> Arrangement:
        """A query at one granularity that serves at least one bar."""
        raise NotImplementedError

    def empty(self) -> Arrangement:
        """A query whose range, entirely before the clock, holds no bar."""
        raise NotImplementedError

    def straddling(self) -> Arrangement:
        """A query that starts before the clock and ends after it, with a bar before the clock."""
        raise NotImplementedError

    def failing(self) -> Arrangement:
        """A query the vendor (or its stand-in) fails, after answering part of it if it can."""
        raise NotImplementedError

    def rate_limited(self) -> Arrangement:
        """A query the vendor (or its stand-in) rate-limits, past any retry the reader makes."""
        raise NotImplementedError

    def end_before_start(self) -> Arrangement:
        """A query whose end is before its start, answered as the vendor answers one."""
        raise NotImplementedError

    def open_ended(self) -> Arrangement:
        """A query with end None."""
        raise NotImplementedError


class FakeReadSubject(Subject):
    """FakeRead: bars on its epoch-anchored grid, behaviour chosen by the symbol's prefix."""

    implementation = FakeRead
    # FakeRead stands for the interface, so it serves every platform granularity.
    granularities = tuple(Granularity)
    # EMPTY BY DESIGN, not by omission (architect, 01:55 UTC 2026-09-30, FAKES-1 gate on
    # tj-irhy0a.8, ruling 5): FakeRead stands for the interface, not for Alpaca's limits, and
    # never refuses an instrument. Do not make it refuse one; the deployment-feed, future-range and
    # end-before-start refusals it does make are the interface's, asserted for every subject below.
    unsupported = ()

    SYMBOL = 'CONTRACT'
    # On every grid: an instant and the grid point at or after it, per the fake's published anchor.
    NEAR = datetime(2022, 1, 3, 15, tzinfo=UTC)

    def on_grid(self, granularity: Granularity, near: datetime = NEAR) -> datetime:
        return GRID_EPOCH + (grid_index(granularity, near) + 1) * granularity.offset

    def arrange(self, symbol: str, granularity: Granularity, steps: int | None) -> Arrangement:
        start = self.on_grid(granularity)
        end = None if steps is None else start + steps * granularity.offset
        return Arrangement(FakeRead(slow_delay_seconds=0, clock=clock), bars_query(symbol, granularity, start, end))

    def servable(self) -> Arrangement:
        return self.arrange(self.SYMBOL, Granularity.ONE_HOUR, 3)

    def watched(self) -> Arrangement:
        # SLOW_ sleeps only once a query has passed every refusal: its sleeps are the fake's vendor work.
        slept: list[float] = []

        async def record(seconds: float) -> None:
            slept.append(seconds)

        start = self.on_grid(Granularity.ONE_HOUR)
        reader = FakeRead(slow_delay_seconds=0, sleep=record, clock=clock)
        query = bars_query(
            f'{SLOW_PREFIX}{self.SYMBOL}', Granularity.ONE_HOUR, start, start + 3 * Granularity.ONE_HOUR.offset
        )
        return Arrangement(reader, query, lambda: len(slept))

    def boundary(self) -> Arrangement:
        # start and end both on the ONE_HOUR grid: the fake would have a bar at each.
        return self.arrange(self.SYMBOL, Granularity.ONE_HOUR, 2)

    def at_granularity(self, granularity: Granularity) -> Arrangement:
        return self.arrange(self.SYMBOL, granularity, 3)

    def empty(self) -> Arrangement:
        return self.arrange(f'{EMPTY_PREFIX}{self.SYMBOL}', Granularity.ONE_HOUR, 3)

    def straddling(self) -> Arrangement:
        start = self.on_grid(Granularity.ONE_HOUR, NOW - timedelta(hours=3))
        return Arrangement(
            FakeRead(slow_delay_seconds=0, clock=clock),
            bars_query(self.SYMBOL, Granularity.ONE_HOUR, start, NOW + timedelta(hours=3)),
        )

    def failing(self) -> Arrangement:
        return self.arrange(f'{FAIL_PREFIX}{self.SYMBOL}', Granularity.ONE_HOUR, 3)

    def rate_limited(self) -> Arrangement:
        return self.arrange(f'{RATELIMIT_PREFIX}{self.SYMBOL}', Granularity.ONE_HOUR, 3)

    def end_before_start(self) -> Arrangement:
        arrangement = self.servable()
        query = arrangement.query
        return replace(arrangement, query=replace(query, end=query.start - Granularity.ONE_HOUR.offset))

    def open_ended(self) -> Arrangement:
        return self.arrange(self.SYMBOL, Granularity.ONE_HOUR, None)


class AlpacaRecordedSubject(Subject):
    """AlpacaRead on the real alpaca-py client, its HTTP answered from recorded vendor bodies."""

    implementation = AlpacaRead
    granularities = tuple(mapped.granularity for mapped in AlpacaGranularity)
    # Decision tj-j4wknb addendum 4 items 3 and 5; architect, 01:55 UTC 2026-09-30. The reasons are
    # TE-4's (tj-3mk3u5.37.5 item 3): an adjustment is refused as UNSUPPORTED_INSTRUMENT, the one of
    # BrokerUnsupportedError's three reasons that fits (the builder's open point, for the architect).
    unsupported = (
        Unsupported('asset_type', AssetType.CRYPTO, Reason.UNSUPPORTED_ASSET_TYPE),
        Unsupported('asset_type', AssetType.OPTION, Reason.UNSUPPORTED_ASSET_TYPE),
        Unsupported('adjustment', 'split', Reason.UNSUPPORTED_INSTRUMENT),
        Unsupported('adjustment', 'all', Reason.UNSUPPORTED_INSTRUMENT),
        Unsupported('exchange', 'XNAS', Reason.UNSUPPORTED_INSTRUMENT),
        Unsupported('exchange', 'XTSE', Reason.UNSUPPORTED_INSTRUMENT),
        Unsupported('currency', 'CAD', Reason.UNSUPPORTED_INSTRUMENT),
        Unsupported('currency', 'EUR', Reason.UNSUPPORTED_INSTRUMENT),
    )

    SYMBOL = 'AAPL'
    # The wire spelling of each granularity names its recorded body (tj-vhboky.57 item 5).
    WIRE_TIMEFRAME: ClassVar[dict[Granularity, str]] = {
        Granularity.ONE_MINUTE: '1Min',
        Granularity.FIVE_MINUTES: '5Min',
        Granularity.THIRTY_MINUTES: '30Min',
        Granularity.ONE_HOUR: '1Hour',
        Granularity.ONE_DAY: '1Day',
        Granularity.ONE_WEEK: '1Week',
        Granularity.ONE_MONTH: '1Month',
    }

    def __init__(self, executor: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(executor, monkeypatch)
        self.budget = CountingBudget()
        monkeypatch.setattr(broker_api, '__RATE_BUDGET', self.budget)

    def arrangement(self, responder, query: BarsQuery) -> Arrangement:
        client, transport = recorded_client(responder)
        reader = AlpacaRead(client=client, executor_provider=lambda: self.executor, clock=clock)
        return Arrangement(reader, query, lambda: len(transport.sent) + len(self.budget.acquires))

    def whole(self, recorded: RecordedResponse, granularity: Granularity) -> BarsQuery:
        """A query covering every bar of a recorded body, ending one step after the last."""
        return bars_query(
            self.SYMBOL, granularity, recorded.timestamps[0], recorded.timestamps[-1] + granularity.offset
        )

    def servable(self) -> Arrangement:
        recorded = load('bars_1Day')
        return self.arrangement(always(recorded), self.whole(recorded, Granularity.ONE_DAY))

    def watched(self) -> Arrangement:
        return self.servable()

    def boundary(self) -> Arrangement:
        # The vendor's end is inclusive, so this body carries a bar exactly at end (FAKES-2).
        recorded = load('bars_range_boundary')
        query = bars_query(self.SYMBOL, Granularity.ONE_HOUR, recorded.timestamps[0], recorded.timestamps[-1])
        return self.arrangement(always(recorded), query)

    def at_granularity(self, granularity: Granularity) -> Arrangement:
        recorded = load(f'bars_{self.WIRE_TIMEFRAME[granularity]}')
        return self.arrangement(always(recorded), self.whole(recorded, granularity))

    def empty(self) -> Arrangement:
        # The RECORDED empty body: the symbol absent from the bar set (Q-EMPTY addendum item 1).
        start = datetime(2022, 1, 8, 5, tzinfo=UTC)
        return self.arrangement(
            always(load('bars_empty_absent_symbol')),
            bars_query(self.SYMBOL, Granularity.ONE_DAY, start, start + timedelta(days=2)),
        )

    def straddling(self) -> Arrangement:
        recorded = load('bars_range_boundary')
        return self.arrangement(
            always(recorded),
            bars_query(self.SYMBOL, Granularity.ONE_HOUR, recorded.timestamps[0], NOW + timedelta(days=1)),
        )

    def failing(self) -> Arrangement:
        # Page 1 is served and page 2 fails: the strongest form of 'no partial range'. 500 is not a
        # status alpaca-py retries, so nothing sleeps.
        page1, page2 = load('bars_1Day_page1'), load('bars_1Day_page2')
        query = bars_query(
            self.SYMBOL, Granularity.ONE_DAY, page1.timestamps[0], page2.timestamps[-1] + Granularity.ONE_DAY.offset
        )
        responder = by_page_token({None: page1, page1.next_page_token: load('error_500')})
        return self.arrangement(responder, query)

    def rate_limited(self) -> Arrangement:
        # alpaca-py retries a 429 three times, 3 s apart; its clock is replaced so nothing really sleeps.
        record_sdk_sleeps(self.monkeypatch)
        recorded = load('bars_1Day')
        return self.arrangement(always(load('error_429')), self.whole(recorded, Granularity.ONE_DAY))

    def end_before_start(self) -> Arrangement:
        # The RECORDED 400 is Alpaca's answer to exactly this request (error_400.json).
        start = load('bars_1Day').timestamps[1]
        query = bars_query(self.SYMBOL, Granularity.ONE_DAY, start, start - Granularity.ONE_DAY.offset)
        return self.arrangement(always(load('error_400')), query)

    def open_ended(self) -> Arrangement:
        recorded = load('bars_range_boundary')
        return self.arrangement(
            always(recorded), bars_query(self.SYMBOL, Granularity.ONE_HOUR, recorded.timestamps[0], None)
        )


# ONE LINE PER IMPLEMENTATION. The deferred live leg (tj-ummhfo) joins as one more entry, e.g.
#   pytest.param(AlpacaLiveSubject, id='AlpacaRead-live', marks=pytest.mark.external),
# and every assertion below runs against it unchanged.
SUBJECTS = [pytest.param(FakeReadSubject, id='FakeRead'), pytest.param(AlpacaRecordedSubject, id='AlpacaRead-recorded')]
SUBJECT_CLASSES: list[type[Subject]] = [param.values[0] for param in SUBJECTS]


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        yield pool


@pytest.fixture(autouse=True)
def iex_deployment(monkeypatch):
    """Pin the deployment's tape, so the resolved feed is not read from the host: IEX for every subject."""
    monkeypatch.delenv('ALPACA_SIP_ENABLED', raising=False)


@pytest.fixture(params=SUBJECTS)
def subject(request, executor, monkeypatch) -> Subject:
    return request.param(executor, monkeypatch)


# ---------------------------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------------------------


async def fetch(arrangement: Arrangement) -> tuple[BarsResponse, list[Bar]]:
    """Call get_bars, check the SERVED shape BEFORE iterating, then drain at most DRAIN_CAP bars."""
    response = await arrangement.reader.get_bars(arrangement.query)
    assert isinstance(response, BarsResponse), f'expected SERVED, got {response!r}'
    # Known before the first bar is asked for (addendum 2 B).
    feed = response.feed
    assert isinstance(feed, Feed)
    assert isinstance(response.bars, AsyncIterator)
    bars: list[Bar] = []
    async for bar in response.bars:
        bars.append(bar)
        if len(bars) >= DRAIN_CAP:
            break
    assert response.feed is feed
    assert_served(response, arrangement.query, bars)
    return response, bars


def assert_served(response: BarsResponse, query: BarsQuery, bars: list[Bar]) -> None:
    """D2 and D3 note (a): served_range and as_of, on every SERVED answer."""
    assert response.as_of == NOW, f'as_of {response.as_of} is not the reader clock {NOW}'
    assert response.as_of.utcoffset() == timedelta(0), f'as_of not UTC: {response.as_of!r}'
    served = response.served_range
    assert isinstance(served, ServedRange)
    assert served.start == query.start, f'served from {served.start}, asked from {query.start}'
    if query.end is not None:
        assert served.end <= query.end, f'served to {served.end}, wider than asked ({query.end})'
    assert served.end <= response.as_of, f'served to {served.end}, after as_of {response.as_of}'
    assert served.start <= served.end
    for bar in bars:
        assert served.start <= bar.timestamp < served.end, f'{bar.timestamp} outside served_range {served}'


def stamps(bars: list[Bar]) -> list[datetime]:
    return [bar.timestamp for bar in bars]


def assert_well_formed(bars: list[Bar], query: BarsQuery) -> None:
    """Aware UTC, strictly ascending (so unique), start <= t, t < end when set, int-or-None trade_count."""
    assert len(bars) < DRAIN_CAP, 'the bars did not end'
    for bar in bars:
        assert isinstance(bar, Bar)
        # By TYPE, not by value: Bar equality cannot tell 772630 from 772630.0, and bool is an int
        # subclass, so isinstance(True, int) would pass it (tj-j4wknb addendum 2 B, tj-irhy0a.19).
        assert bar.trade_count is None or type(bar.trade_count) is int, (
            f'trade_count {bar.trade_count!r} is {type(bar.trade_count).__name__}, not int or None, '
            f'at {bar.timestamp!r}'
        )
        assert bar.timestamp.tzinfo is not None, f'naive timestamp {bar.timestamp!r}'
        assert bar.timestamp.utcoffset() == timedelta(0), f'not UTC: {bar.timestamp!r}'
        assert query.start <= bar.timestamp, f'{bar.timestamp} before start {query.start}'
        if query.end is not None:
            assert bar.timestamp < query.end, f'{bar.timestamp} not before end {query.end}'
    assert all(earlier < later for earlier, later in pairwise(stamps(bars))), f'not strictly ascending: {stamps(bars)}'


async def fail(arrangement: Arrangement, query: BarsQuery | None = None) -> TraderJoeError:
    """Call get_bars, which must RETURN a BarsFailure (never raise), and give back its typed error."""
    outcome = await arrangement.reader.get_bars(query if query is not None else arrangement.query)
    assert isinstance(outcome, BarsFailure), f'expected a BarsFailure, got {outcome!r}'
    error = outcome.error
    assert isinstance(error, TraderJoeError)
    assert isinstance(error.reason, Reason)
    # The class and the table agree: the error is of the branch REASONS names for its reason.
    assert isinstance(error, REASONS[error.reason].branch)
    return error


# ---------------------------------------------------------------------------------------------
# Conformance: structural, no inheritance required, no capability flags
# ---------------------------------------------------------------------------------------------


@runtime_checkable
class RuntimeBrokerRead(BrokerRead, Protocol):
    """BrokerRead made runtime-checkable HERE, never by decorating the production Protocol."""


def test_the_subject_conforms_to_broker_read_structurally(subject):
    reader = subject.servable().reader
    members = [name for name, _ in inspect.getmembers(BrokerRead, inspect.isfunction) if not name.startswith('_')]

    assert isinstance(reader, subject.implementation)
    assert isinstance(reader, RuntimeBrokerRead)
    for name in members:
        protocol_member, member = getattr(BrokerRead, name), getattr(subject.implementation, name)
        assert inspect.iscoroutinefunction(member) == inspect.iscoroutinefunction(protocol_member)
        # The return annotation included: BarsResponse | BarsFailure, the typed result.
        assert inspect.signature(member) == inspect.signature(protocol_member)


def test_a_failure_states_no_outcome_of_its_own():
    """TE-4 item 1: the outcome class is READ from REASONS[error.reason], never stated a second time."""
    assert [field.name for field in dataclasses.fields(BarsFailure)] == ['error']


# ---------------------------------------------------------------------------------------------
# The response, served_range and the half-open range
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_servable_query_returns_a_response_with_a_feed_and_well_formed_bars(subject):
    arrangement = subject.servable()

    response, bars = await fetch(arrangement)

    assert bars
    assert_well_formed(bars, arrangement.query)
    # A past range, wholly answered: served exactly as asked.
    assert response.served_range == ServedRange(arrangement.query.start, arrangement.query.end)


@pytest.mark.asyncio
@OFFSETS
async def test_the_bar_at_start_is_served_and_none_at_end_on_a_boundary_where_one_would_be(subject, offset):
    """Half-open [start, end) on instants (addendum 5): the rule holds AT the boundary, not near it."""
    arrangement = subject.boundary()
    utc_start, utc_end = arrangement.query.start, arrangement.query.end
    query = replace(arrangement.query, start=utc_start.astimezone(offset), end=utc_end.astimezone(offset))

    _response, bars = await fetch(Arrangement(arrangement.reader, query))

    assert utc_start in stamps(bars)
    assert utc_end not in stamps(bars)
    assert_well_formed(bars, query)


@pytest.mark.asyncio
async def test_every_granularity_the_subject_maps_is_accepted_and_served_well_formed(subject):
    for granularity in subject.granularities:
        arrangement = subject.at_granularity(granularity)

        _response, bars = await fetch(arrangement)

        assert bars, f'no bar at {granularity}'
        assert_well_formed(bars, arrangement.query)


@pytest.mark.asyncio
async def test_an_empty_range_is_served_with_zero_bars_and_the_asked_window(subject):
    """Q-EMPTY (tj-3mk3u5.37.1): SERVED with rows [] and served_range = the asked window, never a failure."""
    arrangement = subject.empty()

    response, bars = await fetch(arrangement)

    assert bars == []
    assert response.served_range == ServedRange(arrangement.query.start, arrangement.query.end)


@pytest.mark.asyncio
async def test_a_range_ending_after_the_clock_is_served_up_to_as_of(subject):
    """Q-EMPTY addendum (3)(b): NOT refused; served_range ends at as_of, and no bar from after it is served."""
    arrangement = subject.straddling()
    assert arrangement.query.start < NOW < arrangement.query.end

    response, bars = await fetch(arrangement)

    assert response.served_range == ServedRange(arrangement.query.start, NOW)
    assert bars
    assert_well_formed(bars, arrangement.query)


@pytest.mark.asyncio
async def test_an_open_ended_query_serves_only_the_shared_properties_and_ends(subject):
    # No count: FakeRead bounds end None by a count, Alpaca by 'up to now' (architect, 01:55 UTC).
    arrangement = subject.open_ended()
    assert arrangement.query.end is None

    _response, bars = await fetch(arrangement)

    assert_well_formed(bars, arrangement.query)


# ---------------------------------------------------------------------------------------------
# Failures: RETURNED as a BarsFailure, never raised
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_vendor_failure_is_returned_not_ready_with_its_cause_and_no_bar(subject):
    """D1(a)/D6: catch, convert, RETURN. Nothing of the fetch is served, never a partial range."""
    error = await fail(subject.failing())

    assert error.reason is Reason.VENDOR_UNAVAILABLE
    assert REASONS[error.reason].outcome is Outcome.NOT_READY
    assert error.__cause__ is not None, 'the caught vendor exception was not kept as the cause (D8)'
    assert error.reset_at is None


@pytest.mark.asyncio
async def test_a_vendor_rate_limit_is_returned_not_ready_with_a_reset_after_the_clock(subject):
    """tj-vz1eta s3: a broker rate limit is its own typed error, carrying WHEN its window resets."""
    error = await fail(subject.rate_limited())

    assert error.reason is Reason.VENDOR_RATE_LIMITED
    assert REASONS[error.reason].outcome is Outcome.NOT_READY
    assert error.reset_at is not None and error.reset_at > NOW
    assert error.__cause__ is not None


@pytest.mark.asyncio
async def test_an_end_before_the_start_is_refused_as_the_callers_mistake(subject):
    """Q-EMPTY addendum (3)(a): VENDOR_INVALID_REQUEST, REFUSED and CLIENT_FIX, never a page to an operator."""
    error = await fail(subject.end_before_start())

    assert error.reason is Reason.VENDOR_INVALID_REQUEST
    assert (REASONS[error.reason].outcome, REASONS[error.reason].disposition) == (
        Outcome.REFUSED,
        Disposition.CLIENT_FIX,
    )
    assert error.reset_at is None


# ---------------------------------------------------------------------------------------------
# The pre-vendor refusals: a named feed, a range in the future (TE-4 items 2 and 4a)
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_named_feed_the_deployment_cannot_serve_is_refused_before_any_vendor_work(subject):
    arrangement = subject.watched()

    error = await fail(arrangement, replace(arrangement.query, feed=Feed.SIP))

    assert type(error) is BrokerUnsupportedError
    assert error.reason is Reason.FEED_NOT_AVAILABLE
    assert REASONS[error.reason].outcome is Outcome.REFUSED
    assert arrangement.vendor_work() == 0, 'vendor work was done before the feed refusal'


@pytest.mark.asyncio
async def test_the_deployments_own_feed_named_is_served_after_vendor_work(subject):
    # The other side of the refusal above, on the same arrangement: so its zero is not vacuous.
    arrangement = subject.watched()
    query = replace(arrangement.query, feed=Feed.IEX)

    response, _bars = await fetch(Arrangement(arrangement.reader, query))

    assert response.feed is Feed.IEX
    assert arrangement.vendor_work() > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'start', [pytest.param(NOW, id='at-the-clock'), pytest.param(NOW + timedelta(days=1), id='after-the-clock')]
)
async def test_a_range_starting_at_or_after_the_clock_is_refused_before_any_vendor_work(subject, start):
    arrangement = subject.watched()
    query = replace(arrangement.query, start=start, end=start + timedelta(days=2))

    error = await fail(arrangement, query)

    assert error.reason is Reason.RANGE_IN_FUTURE
    assert REASONS[error.reason].outcome is Outcome.REFUSED
    assert error.reset_at is None
    assert arrangement.vendor_work() == 0, 'vendor work was done for a range in the future'


# ---------------------------------------------------------------------------------------------
# Declared refusals
# ---------------------------------------------------------------------------------------------

REFUSALS = [
    pytest.param(subject_class, unsupported, id=f'{param.id}-{unsupported.id}')
    for param, subject_class in zip(SUBJECTS, SUBJECT_CLASSES, strict=True)
    for unsupported in subject_class.unsupported
]


@pytest.mark.asyncio
@pytest.mark.parametrize(('subject_class', 'unsupported'), REFUSALS)
async def test_an_input_the_subject_declares_unsupported_is_refused_before_any_vendor_work(
    executor, monkeypatch, subject_class, unsupported
):
    arrangement = subject_class(executor, monkeypatch).watched()

    error = await fail(arrangement, unsupported.apply(arrangement.query))

    assert type(error) is BrokerUnsupportedError
    assert error.reason is unsupported.reason
    assert REASONS[error.reason].outcome is Outcome.REFUSED
    assert arrangement.vendor_work() == 0


# Every input some subject refuses, paired with each subject that does NOT declare it. FakeRead's
# empty list puts every Alpaca refusal here for FakeRead: it serves them.
SERVED_ELSEWHERE_REFUSED = [
    pytest.param(subject_class, unsupported, id=f'{param.id}-{unsupported.id}')
    for param, subject_class in zip(SUBJECTS, SUBJECT_CLASSES, strict=True)
    for unsupported in dict.fromkeys(u for other in SUBJECT_CLASSES for u in other.unsupported)
    if unsupported not in subject_class.unsupported
]


@pytest.mark.asyncio
@pytest.mark.parametrize(('subject_class', 'unsupported'), SERVED_ELSEWHERE_REFUSED)
async def test_an_input_the_subject_does_not_declare_is_served(executor, monkeypatch, subject_class, unsupported):
    """A refusal is a declaration: an implementation refuses what its list names, and nothing else."""
    arrangement = subject_class(executor, monkeypatch).servable()
    query = unsupported.apply(arrangement.query)

    _response, bars = await fetch(Arrangement(arrangement.reader, query))

    assert bars
    assert_well_formed(bars, query)


def test_the_refusal_cases_are_not_vacuous():
    # Each list is an explicit tuple, and the pairings above produced cases on both sides.
    for subject_class in SUBJECT_CLASSES:
        assert isinstance(subject_class.unsupported, tuple)
        for unsupported in subject_class.unsupported:
            assert unsupported.reason in {
                Reason.UNSUPPORTED_INSTRUMENT,
                Reason.UNSUPPORTED_ASSET_TYPE,
                Reason.FEED_NOT_AVAILABLE,
            }
    assert REFUSALS
    assert SERVED_ELSEWHERE_REFUSED


# ---------------------------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_identical_requests_give_equal_results(subject):
    # The fake by construction, the recorded Alpaca path by fixture.
    arrangement = subject.servable()

    first_response, first = await fetch(arrangement)
    second_response, second = await fetch(arrangement)

    assert first
    assert second == first
    assert second_response.feed is first_response.feed
    assert second_response.served_range == first_response.served_range


# ---------------------------------------------------------------------------------------------
# THE GUARD: every BrokerRead implementation in the repository is a subject of this suite
# ---------------------------------------------------------------------------------------------


def module_name(path: Path) -> str:
    # repo_relative, not one root: a hit under data/ingest/app must name itself data.ingest.app.*
    # and a hit under tests/fakes must name itself tests.fakes.* -- the two live under different
    # roots once the service trees move down a level (tj-iontkq.2).
    return '.'.join(repo_relative(path).with_suffix('').parts)


def is_protocol(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(base, ast.Name) and base.id == 'Protocol')
        or (isinstance(base, ast.Attribute) and base.attr == 'Protocol')
        for base in node.bases
    )


def implementations_in(source: str, module: str) -> set[tuple[str, str]]:
    """Every class in the source that defines get_bars and is not itself a Protocol.

    Structural, like the Protocol: a class implements BrokerRead by having get_bars, whatever it
    inherits, so the scan looks for the member rather than a base class.
    """
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and not is_protocol(node):
            members = {item.name for item in node.body if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)}
            if 'get_bars' in members:
                found.add((module, node.name))
    return found


def scanned_implementations() -> set[tuple[str, str]]:
    found = set()
    for root in IMPLEMENTATION_ROOTS:
        for path in sorted(root.rglob('*.py')):
            found |= implementations_in(path.read_text(encoding='utf-8'), module_name(path))
    return found


def test_the_scan_roots_exist_and_hold_the_interface():
    for root in IMPLEMENTATION_ROOTS:
        assert root.is_dir(), root
        assert any(root.rglob('*.py')), root
    # The Protocol itself lives under a root and is not counted as an implementation.
    interface = SERVER_ROOT / 'data' / 'ingest' / 'app' / 'brokers' / 'interface.py'
    assert interface.is_relative_to(IMPLEMENTATION_ROOTS[0])
    assert 'class BrokerRead(Protocol)' in interface.read_text(encoding='utf-8')


def test_the_scan_finds_a_structural_implementation_and_ignores_a_protocol():
    source = (
        'import typing\n'
        'class Plain:\n    async def get_bars(self, query): ...\n'
        'class Subclassed(Base):\n    def get_bars(self, query): ...\n'
        'class Shape(Protocol):\n    async def get_bars(self, query): ...\n'
        'class Dotted(typing.Protocol):\n    async def get_bars(self, query): ...\n'
        'class Unrelated:\n    async def get_quotes(self, query): ...\n'
    )

    assert implementations_in(source, 'pkg.mod') == {('pkg.mod', 'Plain'), ('pkg.mod', 'Subclassed')}


def test_every_broker_read_implementation_in_the_repository_is_a_subject_of_this_suite():
    """Addendum 2 A: this suite is the conformance check a type checker would otherwise be.

    A new BrokerRead under data/ingest/app or tests/fakes turns this red until a Subject for it
    joins SUBJECTS -- and a Subject whose implementation left the tree turns it red too.
    """
    subjects = {(cls.implementation.__module__, cls.implementation.__qualname__) for cls in SUBJECT_CLASSES}

    assert scanned_implementations() == subjects
