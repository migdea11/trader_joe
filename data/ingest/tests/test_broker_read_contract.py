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
the granularities an adapter maps, the failure it raises until PR 2, and the inputs it refuses.

THE CONTRACT, from decision tj-j4wknb:
* conformance is STRUCTURAL (addendum 3 U7): nothing here requires inheriting any base class, and
  there are no capability flags to assert (addendum 4 item 5);
* get_bars returns a BarsResponse whose feed is a Feed member, known before iteration (addendum 2 B);
* bars are aware UTC, strictly ascending (so unique) and within [start, end), HALF-OPEN on instants
  whatever offset the bounds carry (addendum 5, ruling on tj-irhy0a.15): the bar at start is served
  and a bar at end never is, exercised where each implementation WOULD have a bar at end;
* an empty range yields zero bars and still a well-formed BarsResponse;
* an open-ended query (end None) yields only the shared properties, never a count: FakeRead bounds
  it by count and Alpaca by 'up to now' (architect, 01:55 UTC 2026-09-30, FAKES-1 gate);
* a failure RAISES and yields no bar -- the ONLY failure assertion in PR 1 (addendum 2 B, addendum 4
  item 11); PR 2's typed errors (tj-fa1rpu) tighten it;
* an input the implementation declares it cannot serve raises BrokerUnsupportedError and yields no
  bar, and an input it does NOT declare is served (architect, 01:55 UTC, refusals settled);
* every served bar's trade_count is None or exactly an int -- never a float, never a bool
  (addendum 2 B: Bar.trade_count is int | None, the same type from every broker, R2; tj-irhy0a.19);
* two identical requests give equal results.
No market-calendar property is asserted anywhere: FakeRead's grid is 24/7 and epoch-anchored.

THE GUARD: every BrokerRead implementation in the repository's production and fake trees must be a
subject here, so a new broker cannot ship without joining this suite.
"""

import ast
import inspect
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

import pytest
from alpaca.common.exceptions import APIError

from common.enums.data_select import AssetType
from common.enums.data_stock import Feed, Granularity
from data.ingest.app.brokers.alpaca.broker_codes import AlpacaGranularity
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.interface import (
    Bar,
    BarsQuery,
    BarsResponse,
    BrokerRead,
    BrokerUnsupportedError,
    Instrument,
)
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.tests.alpaca_recorded import RecordedResponse, always, by_page_token, load, recorded_client
from tests.fakes.market_data import EMPTY_PREFIX, FAIL_PREFIX, GRID_EPOCH, FakeRead, FakeReadError, grid_index


pytestmark = pytest.mark.data_ingest

REPO_ROOT = Path(__file__).resolve().parents[3]

# Where a BrokerRead implementation may live: data_ingest's production tree and the fakes the
# test-only launcher installs. The bead names data/ingest/app/brokers; the whole app tree is
# scanned so an implementation placed beside the brokers package cannot slip past.
IMPLEMENTATION_ROOTS = (REPO_ROOT / 'data' / 'ingest' / 'app', REPO_ROOT / 'tests' / 'fakes')

# Bound offsets the range rule is checked under: it is on instants, whatever offset the query carries.
OFFSETS = pytest.mark.parametrize(
    'offset',
    [UTC, timezone(timedelta(hours=-5)), timezone(timedelta(hours=9, minutes=30))],
    ids=['Z', '-05:00', '+09:30'],
)

# More bars than any arrangement here serves; reaching it means an open-ended query did not end.
DRAIN_CAP = 10_000


# ---------------------------------------------------------------------------------------------
# Subjects: one per implementation. They arrange; the tests assert.
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Arrangement:
    """A reader and the query that exercises one contract case on it."""

    reader: BrokerRead
    query: BarsQuery


@dataclass(frozen=True)
class Unsupported:
    """One input an implementation declares it cannot serve: a field of the query or its instrument.

    Attributes:
        field (str): 'adjustment' (a BarsQuery field) or an Instrument field name.
        value (Any): The value that cannot be served.
    """

    field: str
    value: Any

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


class Subject:
    """What a contract-suite parameter provides. Every method returns a fresh Arrangement.

    Attributes:
        implementation (type): The BrokerRead class under test; the guard compares these.
        granularities (tuple[Granularity, ...]): Every Granularity the implementation maps.
        failure (type[BaseException]): What get_bars raises on a vendor failure until PR 2.
        unsupported (tuple[Unsupported, ...]): Inputs it declares it cannot serve.
    """

    implementation: ClassVar[type]
    granularities: ClassVar[tuple[Granularity, ...]]
    failure: ClassVar[type[BaseException]]
    unsupported: ClassVar[tuple[Unsupported, ...]]

    def __init__(self, executor: ThreadPoolExecutor) -> None:
        self.executor = executor

    def servable(self) -> Arrangement:
        """A query that serves at least one bar."""
        raise NotImplementedError

    def boundary(self) -> Arrangement:
        """A query whose start AND end are instants where the implementation would have a bar."""
        raise NotImplementedError

    def at_granularity(self, granularity: Granularity) -> Arrangement:
        """A query at one granularity that serves at least one bar."""
        raise NotImplementedError

    def empty(self) -> Arrangement:
        """A query whose range holds no bar."""
        raise NotImplementedError

    def failing(self) -> Arrangement:
        """A query the vendor (or its stand-in) fails."""
        raise NotImplementedError

    def open_ended(self) -> Arrangement:
        """A query with end None."""
        raise NotImplementedError


class FakeReadSubject(Subject):
    """FakeRead: bars on its epoch-anchored grid, behaviour chosen by the symbol's prefix."""

    implementation = FakeRead
    # FakeRead stands for the interface, so it serves every platform granularity.
    granularities = tuple(Granularity)
    failure = FakeReadError
    # EMPTY BY DESIGN, not by omission (architect, 01:55 UTC 2026-09-30, FAKES-1 gate on
    # tj-irhy0a.8, ruling 5): FakeRead stands for the interface, not for Alpaca's limits, and
    # never refuses. Do not make it refuse; test_an_input_the_subject_does_not_declare_is_served
    # pins that it serves every input another subject refuses.
    unsupported = ()

    SYMBOL = 'CONTRACT'
    # On every grid: an instant and the grid point at or after it, per the fake's published anchor.
    NEAR = datetime(2022, 1, 3, 15, tzinfo=UTC)

    def on_grid(self, granularity: Granularity) -> datetime:
        return GRID_EPOCH + (grid_index(granularity, self.NEAR) + 1) * granularity.offset

    def arrange(self, symbol: str, granularity: Granularity, steps: int | None) -> Arrangement:
        start = self.on_grid(granularity)
        end = None if steps is None else start + steps * granularity.offset
        return Arrangement(FakeRead(slow_delay_seconds=0), bars_query(symbol, granularity, start, end))

    def servable(self) -> Arrangement:
        return self.arrange(self.SYMBOL, Granularity.ONE_HOUR, 3)

    def boundary(self) -> Arrangement:
        # start and end both on the ONE_HOUR grid: the fake would have a bar at each.
        return self.arrange(self.SYMBOL, Granularity.ONE_HOUR, 2)

    def at_granularity(self, granularity: Granularity) -> Arrangement:
        return self.arrange(self.SYMBOL, granularity, 3)

    def empty(self) -> Arrangement:
        return self.arrange(f'{EMPTY_PREFIX}{self.SYMBOL}', Granularity.ONE_HOUR, 3)

    def failing(self) -> Arrangement:
        return self.arrange(f'{FAIL_PREFIX}{self.SYMBOL}', Granularity.ONE_HOUR, 3)

    def open_ended(self) -> Arrangement:
        return self.arrange(self.SYMBOL, Granularity.ONE_HOUR, None)


class AlpacaRecordedSubject(Subject):
    """AlpacaRead on the real alpaca-py client, its HTTP answered from recorded vendor bodies."""

    implementation = AlpacaRead
    granularities = tuple(mapped.granularity for mapped in AlpacaGranularity)
    # The SDK's own error propagates until PR 2's typed errors (tj-fa1rpu).
    failure = APIError
    # Decision tj-j4wknb addendum 4 items 3 and 5; architect, 01:55 UTC 2026-09-30.
    unsupported = (
        Unsupported('asset_type', AssetType.CRYPTO),
        Unsupported('asset_type', AssetType.OPTION),
        Unsupported('adjustment', 'split'),
        Unsupported('adjustment', 'all'),
        Unsupported('exchange', 'XNAS'),
        Unsupported('exchange', 'XTSE'),
        Unsupported('currency', 'CAD'),
        Unsupported('currency', 'EUR'),
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

    def reader(self, responder) -> AlpacaRead:
        client, _transport = recorded_client(responder)
        return AlpacaRead(client=client, executor_provider=lambda: self.executor)

    def whole(self, recorded: RecordedResponse, granularity: Granularity) -> BarsQuery:
        """A query covering every bar of a recorded body, ending one step after the last."""
        return bars_query(
            self.SYMBOL, granularity, recorded.timestamps[0], recorded.timestamps[-1] + granularity.offset
        )

    def servable(self) -> Arrangement:
        recorded = load('bars_1Day')
        return Arrangement(self.reader(always(recorded)), self.whole(recorded, Granularity.ONE_DAY))

    def boundary(self) -> Arrangement:
        # The vendor's end is inclusive, so this body carries a bar exactly at end (FAKES-2).
        recorded = load('bars_range_boundary')
        query = bars_query(self.SYMBOL, Granularity.ONE_HOUR, recorded.timestamps[0], recorded.timestamps[-1])
        return Arrangement(self.reader(always(recorded)), query)

    def at_granularity(self, granularity: Granularity) -> Arrangement:
        recorded = load(f'bars_{self.WIRE_TIMEFRAME[granularity]}')
        return Arrangement(self.reader(always(recorded)), self.whole(recorded, granularity))

    def empty(self) -> Arrangement:
        start = datetime(2022, 1, 8, 5, tzinfo=UTC)
        return Arrangement(
            self.reader(always(load('bars_empty_list'))),
            bars_query(self.SYMBOL, Granularity.ONE_DAY, start, start + timedelta(days=2)),
        )

    def failing(self) -> Arrangement:
        # Page 1 is served and page 2 fails: the strongest form of 'no partial range'. 500 is not a
        # status alpaca-py retries, so nothing sleeps.
        page1, page2 = load('bars_1Day_page1'), load('bars_1Day_page2')
        query = bars_query(
            self.SYMBOL, Granularity.ONE_DAY, page1.timestamps[0], page2.timestamps[-1] + Granularity.ONE_DAY.offset
        )
        responder = by_page_token({None: page1, page1.next_page_token: load('error_500')})
        return Arrangement(self.reader(responder), query)

    def open_ended(self) -> Arrangement:
        recorded = load('bars_range_boundary')
        return Arrangement(
            self.reader(always(recorded)), bars_query(self.SYMBOL, Granularity.ONE_HOUR, recorded.timestamps[0], None)
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
    """Pin the deployment's tape, so the resolved feed is not read from the host."""
    monkeypatch.delenv('ALPACA_SIP_ENABLED', raising=False)


@pytest.fixture(params=SUBJECTS)
def subject(request, executor) -> Subject:
    return request.param(executor)


# ---------------------------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------------------------


async def fetch(arrangement: Arrangement) -> tuple[BarsResponse, list[Bar]]:
    """Call get_bars, check the response's shape BEFORE iterating, then drain at most DRAIN_CAP bars."""
    response = await arrangement.reader.get_bars(arrangement.query)
    assert isinstance(response, BarsResponse)
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
    return response, bars


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
        assert inspect.signature(member) == inspect.signature(protocol_member)


# ---------------------------------------------------------------------------------------------
# The response and the half-open range
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_servable_query_returns_a_response_with_a_feed_and_well_formed_bars(subject):
    arrangement = subject.servable()

    _response, bars = await fetch(arrangement)

    assert bars
    assert_well_formed(bars, arrangement.query)


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
async def test_an_empty_range_yields_zero_bars_and_still_a_well_formed_response(subject):
    _response, bars = await fetch(subject.empty())

    assert bars == []


@pytest.mark.asyncio
async def test_an_open_ended_query_serves_only_the_shared_properties_and_ends(subject):
    # No count: FakeRead bounds end None by a count, Alpaca by 'up to now' (architect, 01:55 UTC).
    arrangement = subject.open_ended()
    assert arrangement.query.end is None

    _response, bars = await fetch(arrangement)

    assert_well_formed(bars, arrangement.query)


# ---------------------------------------------------------------------------------------------
# Failure and refusal
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failure_raises_and_yields_no_bar(subject):
    """The ONLY failure assertion in PR 1 (decision tj-j4wknb addendum 2 B, addendum 4 item 11).

    get_bars raises -- here or while its bars are iterated -- and nothing of that fetch is yielded,
    never a partial range. PR 2's typed errors (tj-fa1rpu) replace the raise and tighten this.
    """
    arrangement = subject.failing()
    yielded: list[Bar] = []

    with pytest.raises(subject.failure):
        response = await arrangement.reader.get_bars(arrangement.query)
        async for bar in response.bars:
            yielded.append(bar)

    assert yielded == []


REFUSALS = [
    pytest.param(subject_class, unsupported, id=f'{param.id}-{unsupported.id}')
    for param, subject_class in zip(SUBJECTS, SUBJECT_CLASSES, strict=True)
    for unsupported in subject_class.unsupported
]


@pytest.mark.asyncio
@pytest.mark.parametrize(('subject_class', 'unsupported'), REFUSALS)
async def test_an_input_the_subject_declares_unsupported_raises_and_yields_no_bar(executor, subject_class, unsupported):
    arrangement = subject_class(executor).servable()
    yielded: list[Bar] = []

    with pytest.raises(BrokerUnsupportedError):
        response = await arrangement.reader.get_bars(unsupported.apply(arrangement.query))
        async for bar in response.bars:
            yielded.append(bar)

    assert yielded == []


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
async def test_an_input_the_subject_does_not_declare_is_served(executor, subject_class, unsupported):
    """A refusal is a declaration: an implementation refuses what its list names, and nothing else."""
    arrangement = subject_class(executor).servable()
    query = unsupported.apply(arrangement.query)

    _response, bars = await fetch(Arrangement(arrangement.reader, query))

    assert bars
    assert_well_formed(bars, query)


def test_the_refusal_cases_are_not_vacuous():
    # Each list is an explicit tuple, and the pairings above produced cases on both sides.
    for subject_class in SUBJECT_CLASSES:
        assert isinstance(subject_class.unsupported, tuple)
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


# ---------------------------------------------------------------------------------------------
# THE GUARD: every BrokerRead implementation in the repository is a subject of this suite
# ---------------------------------------------------------------------------------------------


def module_name(path: Path) -> str:
    return '.'.join(path.relative_to(REPO_ROOT).with_suffix('').parts)


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
    interface = REPO_ROOT / 'data' / 'ingest' / 'app' / 'brokers' / 'interface.py'
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
