"""The broker read seam: the BrokerRead Protocol, AlpacaRead behind it, and how the app injects it.

HANDLES-2 (tj-irhy0a.7) against HANDLES-1 (tj-irhy0a.6, 539ec64 + 16dce3b). The design is decision
tj-j4wknb: addendum 2 B (BarsQuery / BarsResponse / get_bars), addendum 3 U7 (the Protocol is the
enforcement, no inheritance required), addendum 4 items 3 and 5 (Instrument; no capability flags,
an unsupported request raises BrokerUnsupportedError), addendum 5 (the half-open bar range) and
the body's INJECTION section (create_app, readers installed by the lifespan before the RPC servers
start and cleared at teardown).

Zero network. The vendor is a stub client handed to AlpacaRead through its constructor, and the
lifespan's Kafka and latency collaborators are replaced with mocks of their public entry points
(decision tj-j4wknb R4: mock libraries are fine for in-process unit tests). Readers standing in for
a broker below ingest_control are plain classes that conform structurally, like any BrokerRead.
"""

import inspect
import os
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Protocol, runtime_checkable
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.kafka.messaging.kafka_consumer import KafkaConsumerFactory
from data.ingest.app import app_depends, ingest_control, main
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.broker_errors import MissingCredentialsError
from data.ingest.app.brokers.interface import (
    Bar,
    BarsQuery,
    BarsResponse,
    BrokerRead,
    BrokerUnsupportedError,
    Instrument,
)
from data.ingest.app.brokers.rate_budget import RequestPriority
from routers.data_ingest import get_dataset_request
from schemas.data_ingest.get_dataset_request import StockDatasetRequest


pytestmark = pytest.mark.data_ingest

START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
# The half-open range under test: START <= t < END.
END = START + timedelta(hours=1)
BETWEEN = START + timedelta(minutes=30)
AFTER = END + timedelta(minutes=30)

# What priority_for_update_type must give each update type, written out rather than re-derived
# so the wiring tests below cannot agree with a wrong mapping. STATIC is the case that matters:
# it is the only one whose priority (BACKFILL) yields to the others under contention.
EXPECTED_PRIORITY = {
    UpdateType.STATIC: RequestPriority.BACKFILL,
    UpdateType.DAILY: RequestPriority.INTERACTIVE,
    UpdateType.STREAM: RequestPriority.LIVE,
}


# ---------------------------------------------------------------------------------------------
# Builders and doubles
# ---------------------------------------------------------------------------------------------


def build_request(**overrides) -> StockDatasetRequest:
    """Build a stock dataset request, with any field overridden.

    Returns:
        StockDatasetRequest: The request.
    """
    fields = {
        'dataset_id': uuid4(),
        'owner': 'rebalancer',
        'source': DataSource.ALPACA_API,
        'feed': Feed.IEX,
        'granularity': Granularity.ONE_DAY,
        'start': START,
        'end': None,
        'expiry': START,
        'expiry_type': ExpiryType.ROLLING,
        'update_type': UpdateType.DAILY,
        'asset_symbol': 'VFV',
        'asset_type': AssetType.STOCK,
        'data_types': [DataType.MARKET_ACTIVITY],
    }
    return StockDatasetRequest(**{**fields, **overrides})


def build_query(**overrides) -> BarsQuery:
    """Build a BarsQuery AlpacaRead can serve, with any field overridden.

    Returns:
        BarsQuery: The query.
    """
    fields = {
        'instrument': Instrument(symbol='VFV', asset_type=AssetType.STOCK),
        'granularity': Granularity.THIRTY_MINUTES,
        'start': START,
        'end': END,
        'priority': RequestPriority.INTERACTIVE,
    }
    return BarsQuery(**{**fields, **overrides})


def vendor_bar(timestamp: datetime, close: float = 10.0) -> SimpleNamespace:
    """One alpaca-py bar, with every attribute AlpacaRead reads (vwap included, as the SDK's is)."""
    return SimpleNamespace(
        open=1.0, high=2.0, low=0.5, close=close, volume=10, trade_count=3, vwap=1.5, timestamp=timestamp
    )


class StubBarSet:
    """The slice of alpaca-py's BarSet that AlpacaRead's conversion touches."""

    def __init__(self, symbol: str, bars: list[SimpleNamespace]):
        self.data = {symbol: bars}

    def __getitem__(self, symbol: str) -> list[SimpleNamespace]:
        return self.data[symbol]


def stub_client(timestamps: list[datetime], symbol: str = 'VFV') -> Mock:
    """A stub Alpaca client answering get_stock_bars with one bar per timestamp."""
    client = Mock()
    client.get_stock_bars.return_value = StubBarSet(symbol, [vendor_bar(t) for t in timestamps])
    return client


class RecordingRead:
    """A BrokerRead that records every query and answers with fixed bars, or fails as told.

    Conforms structurally and inherits nothing, as any BrokerRead may (decision tj-j4wknb U7).

    Args:
        timestamps (list[datetime]): One bar per timestamp.
        error (BaseException | None): Raised by get_bars itself, before any response.
        iteration_error (BaseException | None): Raised while the bars are iterated.
    """

    def __init__(
        self,
        timestamps: tuple[datetime, ...] = (START,),
        error: BaseException | None = None,
        iteration_error: BaseException | None = None,
    ):
        self.timestamps = timestamps
        self.error = error
        self.iteration_error = iteration_error
        self.queries: list[BarsQuery] = []

    async def get_bars(self, query: BarsQuery) -> BarsResponse:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return BarsResponse(feed=Feed.IEX, bars=self.__iterate())

    async def __iterate(self) -> AsyncIterator[Bar]:
        for timestamp in self.timestamps:
            if self.iteration_error is not None:
                raise self.iteration_error
            # trade_count set: the store's schema requires it, although Bar allows None (raised as
            # a question on tj-irhy0a.7, not pinned either way here).
            yield Bar(timestamp=timestamp, open=1.0, high=2.0, low=0.5, close=10.0, volume=10, trade_count=3)


class KeyRecordingSingleFlight:
    """The real single flight, with every key it is asked to run recorded -- the way to the vendor."""

    def __init__(self, inner):
        self.inner = inner
        self.keys = []

    async def run(self, key, factory):
        self.keys.append(key)
        return await self.inner.run(key, factory)


class PriorityRecordingBudget:
    """A rate budget that grants every token and records the priority each was asked for."""

    def __init__(self):
        self.priorities: list[RequestPriority] = []

    async def acquire(self, priority: RequestPriority = RequestPriority.INTERACTIVE) -> None:
        self.priorities.append(priority)


def installed_readers() -> dict:
    """A copy of the mapping store_retrieve_stock dispatches through right now.

    Read through getattr on the module global, as test_broker_api.py reads broker_api's single
    flight: ingest_control exposes install and clear, and no read-back.
    """
    return dict(getattr(ingest_control, '__READERS'))


@pytest.fixture(autouse=True)
def no_state_leaks_between_tests():
    """Clear installed readers and any cached Alpaca client around every test."""
    ingest_control.clear_readers()
    broker_api.set_client(None)
    yield
    ingest_control.clear_readers()
    broker_api.set_client(None)


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    """A pool for the blocking SDK call, handed to AlpacaRead through executor_provider."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        yield pool


@pytest.fixture
def single_flight() -> Iterator[KeyRecordingSingleFlight]:
    """Record every vendor call attempt made through broker_api's single flight."""
    recorder = KeyRecordingSingleFlight(getattr(broker_api, '__SINGLE_FLIGHT'))
    with patch.object(broker_api, '__SINGLE_FLIGHT', recorder):
        yield recorder


async def collect(response: BarsResponse) -> list[Bar]:
    """Drain a response's bars."""
    return [bar async for bar in response.bars]


# ---------------------------------------------------------------------------------------------
# Conformance: AlpacaRead is a BrokerRead
# ---------------------------------------------------------------------------------------------


@runtime_checkable
class RuntimeBrokerRead(BrokerRead, Protocol):
    """BrokerRead made runtime-checkable HERE, not by decorating the production Protocol.

    runtime_checkable mutates the class it is given, so applying it to BrokerRead itself would
    change production behaviour for every later test in the session.
    """


def test_alpaca_read_satisfies_the_broker_read_protocol():
    """AlpacaRead conforms to BrokerRead STRUCTURALLY; nothing requires it to inherit (U7).

    Two checks, because isinstance against a runtime-checkable Protocol sees only that the member
    exists: the signature is compared too, so a get_bars that took other arguments, returned
    something else or stopped being a coroutine function fails here, in the PR gate, rather than
    at the first request. No capability flags are asserted: there are none (addendum 4 item 5).
    """
    reader = AlpacaRead(client=Mock())

    assert isinstance(reader, RuntimeBrokerRead)
    assert inspect.iscoroutinefunction(AlpacaRead.get_bars)
    assert inspect.signature(AlpacaRead.get_bars) == inspect.signature(BrokerRead.get_bars)


# ---------------------------------------------------------------------------------------------
# Refusals: what Alpaca cannot serve raises BrokerUnsupportedError before any vendor call
# ---------------------------------------------------------------------------------------------

REFUSED = [
    pytest.param({'instrument': Instrument('VFV', AssetType.STOCK, currency='CAD')}, 'currency', 'CAD', id='cad'),
    pytest.param({'instrument': Instrument('VFV', AssetType.STOCK, currency='EUR')}, 'currency', 'EUR', id='eur'),
    pytest.param({'instrument': Instrument('VFV', AssetType.STOCK, exchange='XNYS')}, 'exchange', 'XNYS', id='xnys'),
    pytest.param(
        {'instrument': Instrument('VFV', AssetType.STOCK, exchange='XNAS', currency='USD')},
        'exchange',
        'XNAS',
        id='xnas-even-in-usd',
    ),
    pytest.param({'instrument': Instrument('BTC', AssetType.CRYPTO)}, 'asset_type', AssetType.CRYPTO, id='crypto'),
    pytest.param({'instrument': Instrument('VFV', AssetType.OPTION)}, 'asset_type', AssetType.OPTION, id='option'),
    pytest.param({'adjustment': 'split'}, 'adjustment', 'split', id='split-adjusted'),
    pytest.param({'adjustment': 'all'}, 'adjustment', 'all', id='all-adjusted'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(('overrides', 'field', 'value'), REFUSED)
async def test_alpaca_refuses_what_it_cannot_serve_before_any_vendor_call(
    overrides: dict, field: str, value, executor: ThreadPoolExecutor, single_flight: KeyRecordingSingleFlight
):
    """Addendum 4 items 3 and 5: a handle that cannot serve the combination REFUSES, never guesses.

    Alpaca serves one consolidated US line per symbol, in dollars, stocks only, raw bars only.
    Anything else would otherwise be silently served as a raw USD stock fetch. The error is a
    typed one a caller catches by class, its message names the field and the value, and neither
    the injected client nor the single flight (the only way to the vendor) is touched.
    """
    client = Mock()
    reader = AlpacaRead(client=client, executor_provider=lambda: executor)

    with pytest.raises(BrokerUnsupportedError) as err:
        await reader.get_bars(build_query(**overrides))

    assert field in str(err.value)
    assert repr(value) in str(err.value)
    assert client.mock_calls == [], 'the vendor client was touched before the refusal'
    assert single_flight.keys == [], 'a vendor call was attempted before the refusal'


@pytest.mark.asyncio
@pytest.mark.parametrize(('overrides', 'field', 'value'), REFUSED)
async def test_a_refusal_comes_before_the_credential_is_even_resolved(overrides: dict, field: str, value):
    """With no client injected and no credentials set, the refusal still wins.

    So the refusal comes before broker_api.get_client() as well as before the vendor call: a
    request Alpaca cannot serve is reported as unsupported, not as a missing credential.
    """
    reader = AlpacaRead()

    with patch.dict(os.environ, {}, clear=True), pytest.raises(BrokerUnsupportedError):
        await reader.get_bars(build_query(**overrides))


@pytest.mark.asyncio
@pytest.mark.parametrize('currency', [None, 'USD'], ids=['currency-none', 'currency-usd'])
async def test_alpaca_serves_its_own_listing_with_or_without_a_named_currency(
    currency: str | None, executor: ThreadPoolExecutor
):
    # The other side of the refusals: None means 'the broker's own listing' and USD names it, so
    # both are served. Without this, a reader that refused everything would pass the tests above.
    client = stub_client([START])
    reader = AlpacaRead(client=client, executor_provider=lambda: executor)

    response = await reader.get_bars(build_query(instrument=Instrument('VFV', AssetType.STOCK, currency=currency)))

    assert [bar.timestamp for bar in await collect(response)] == [START]
    client.get_stock_bars.assert_called_once()


# ---------------------------------------------------------------------------------------------
# The half-open range [start, end) -- decision tj-j4wknb addendum 5, ruled on tj-irhy0a.15
# ---------------------------------------------------------------------------------------------

VENDOR_ANSWER = [START, BETWEEN, END, AFTER]


@pytest.mark.asyncio
async def test_alpaca_read_keeps_the_bar_at_start_and_drops_the_bar_at_end(executor: ThreadPoolExecutor):
    """Alpaca's end is INCLUSIVE, so AlpacaRead truncates to [start, end) itself.

    The stub answers with a bar at start, one strictly between, one EXACTLY at end and one after
    end -- the vendor's inclusive answer plus a stray. Only start and between may come out.
    """
    reader = AlpacaRead(client=stub_client(VENDOR_ANSWER), executor_provider=lambda: executor)

    bars = await collect(await reader.get_bars(build_query(end=END)))

    assert [bar.timestamp for bar in bars] == [START, BETWEEN]


@pytest.mark.asyncio
async def test_an_open_ended_query_truncates_nothing(executor: ThreadPoolExecutor):
    reader = AlpacaRead(client=stub_client(VENDOR_ANSWER), executor_provider=lambda: executor)

    bars = await collect(await reader.get_bars(build_query(end=None)))

    assert [bar.timestamp for bar in bars] == VENDOR_ANSWER


@pytest.mark.asyncio
async def test_an_end_with_a_non_utc_offset_truncates_at_the_same_instant(executor: ThreadPoolExecutor):
    # Comparison is on instants, whatever offset the bound carries (addendum 5). The same instant
    # written at -05:00 must cut exactly where the Z form does.
    end_at_minus_five = END.astimezone(timezone(timedelta(hours=-5)))
    assert end_at_minus_five.utcoffset() != timedelta(0), 'the bound really does carry another offset'
    assert end_at_minus_five == END

    utc_bars = await collect(
        await AlpacaRead(client=stub_client(VENDOR_ANSWER), executor_provider=lambda: executor).get_bars(
            build_query(end=END)
        )
    )
    offset_bars = await collect(
        await AlpacaRead(client=stub_client(VENDOR_ANSWER), executor_provider=lambda: executor).get_bars(
            build_query(end=end_at_minus_five)
        )
    )

    assert [bar.timestamp for bar in offset_bars] == [bar.timestamp for bar in utc_bars] == [START, BETWEEN]


@pytest.mark.asyncio
async def test_the_rpc_batch_carries_no_bar_stamped_at_end(executor: ThreadPoolExecutor):
    """The ONE ruled exception to HANDLES-1's byte-for-byte RPC property (tj-irhy0a.15).

    Before the broker interface, a bar the vendor stamped exactly at end reached the Kafka RPC
    batch. Under decision tj-j4wknb addendum 5 the interface serves [start, end) for every
    broker, so that bar is no longer in the batch. Every other byte-for-byte property stands, and
    the PR description must say so (tj-0pobey.3).
    """
    client = stub_client(VENDOR_ANSWER)
    ingest_control.install_readers(
        {DataSource.ALPACA_API: AlpacaRead(client=client, executor_provider=lambda: executor)}
    )

    batch = await ingest_control.store_retrieve_stock(build_request(end=END))

    assert [entry.timestamp for entry in batch.dataset[DataType.MARKET_ACTIVITY]] == [START, BETWEEN]


# ---------------------------------------------------------------------------------------------
# store_retrieve_stock: dispatch, the request->BarsQuery mapping, and its failure paths
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('source', [DataSource.ALPACA_API, DataSource.IB_API], ids=['alpaca', 'ib'])
async def test_store_retrieve_stock_dispatches_by_the_request_source(source: DataSource):
    readers = {DataSource.ALPACA_API: RecordingRead(), DataSource.IB_API: RecordingRead()}
    ingest_control.install_readers(readers)

    batch = await ingest_control.store_retrieve_stock(build_request(source=source))

    assert {name: len(reader.queries) for name, reader in readers.items()} == {
        name: int(name is source) for name in readers
    }
    assert batch.source is source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'installed', [{}, {DataSource.IB_API: RecordingRead()}], ids=['nothing-installed', 'another-source-only']
)
async def test_an_unmapped_source_raises_not_implemented(installed: dict):
    ingest_control.install_readers(installed)

    with pytest.raises(NotImplementedError):
        await ingest_control.store_retrieve_stock(build_request(source=DataSource.ALPACA_API))

    assert all(reader.queries == [] for reader in installed.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'reader',
    [
        pytest.param(RecordingRead(error=RuntimeError('vendor down')), id='get-bars-raises'),
        pytest.param(RecordingRead(iteration_error=RuntimeError('page 2 failed')), id='iteration-raises'),
    ],
)
async def test_a_reader_failure_is_swallowed_into_a_bare_empty_dict(reader: RecordingRead):
    """TODAY'S SWALLOW, pinned so that changing it is deliberate (tj-fe19tu).

    store_retrieve_stock is the Kafka RPC boundary and keeps the old observable behaviour: any
    failure of get_bars or of iterating its bars comes back as a bare {} (decision tj-j4wknb
    addendum 2 B, 'the swallow moves one layer up, it does not grow'). PR 2's typed errors
    rewrite this, and this test is the one to invert when they do.
    """
    ingest_control.install_readers({DataSource.ALPACA_API: reader})

    assert await ingest_control.store_retrieve_stock(build_request()) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'reader',
    [
        pytest.param(RecordingRead(error=MissingCredentialsError('ALPACA_API_KEY unset')), id='get-bars-raises'),
        pytest.param(
            RecordingRead(iteration_error=MissingCredentialsError('ALPACA_API_KEY unset')), id='iteration-raises'
        ),
    ],
)
async def test_a_missing_credential_is_not_swallowed(reader: RecordingRead):
    # The one exception the swallow above must NOT eat: an operator needs the variable's name,
    # not an empty batch that looks like a symbol with no data.
    ingest_control.install_readers({DataSource.ALPACA_API: reader})

    with pytest.raises(MissingCredentialsError):
        await ingest_control.store_retrieve_stock(build_request())


@pytest.mark.asyncio
async def test_empty_data_types_raise_value_error_before_any_get_bars_call():
    # Ruled by the architect on the HANDLES-1 gate: the old empty batch needed a feed, which now
    # reaches ingest_control only through a BarsResponse, and the None it would otherwise return
    # violates the return annotation.
    reader = RecordingRead()
    ingest_control.install_readers({DataSource.ALPACA_API: reader})

    with pytest.raises(ValueError, match='data_types'):
        await ingest_control.store_retrieve_stock(build_request(data_types=[]))

    assert reader.queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize('data_type', [DataType.QUOTE, DataType.TRADE], ids=['quote', 'trade'])
async def test_quotes_and_trades_alone_raise_not_implemented_without_fetching_bars(data_type: DataType):
    reader = RecordingRead()
    ingest_control.install_readers({DataSource.ALPACA_API: reader})

    with pytest.raises(NotImplementedError):
        await ingest_control.store_retrieve_stock(build_request(data_types=[data_type]))

    assert reader.queries == []


def test_the_expected_priority_table_covers_every_update_type():
    # Otherwise a new UpdateType would silently fall out of the parametrizations below.
    assert set(EXPECTED_PRIORITY) == set(UpdateType)


@pytest.mark.asyncio
@pytest.mark.parametrize('update_type', list(EXPECTED_PRIORITY), ids=lambda update_type: update_type.name)
async def test_the_request_maps_onto_the_bars_query(update_type: UpdateType):
    """Every field store_retrieve_stock puts in the BarsQuery, compared as one value.

    Non-default granularity and a set end, so a mapping that dropped either would show. The
    instrument is the generic symbol with exchange and currency None -- 'the broker's own
    listing' -- because the request carries neither today (addendum 4 item 3).
    """
    reader = RecordingRead()
    ingest_control.install_readers({DataSource.ALPACA_API: reader})
    request = build_request(granularity=Granularity.ONE_HOUR, end=END, update_type=update_type, asset_symbol='XIC')

    await ingest_control.store_retrieve_stock(request)

    assert reader.queries == [
        BarsQuery(
            instrument=Instrument(symbol='XIC', asset_type=AssetType.STOCK, exchange=None, currency=None),
            granularity=Granularity.ONE_HOUR,
            start=START,
            end=END,
            adjustment='raw',
            priority=EXPECTED_PRIORITY[update_type],
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('update_type', list(EXPECTED_PRIORITY), ids=lambda update_type: update_type.name)
async def test_the_request_priority_reaches_the_alpaca_rate_budget(
    update_type: UpdateType, executor: ThreadPoolExecutor
):
    """The update type's priority travels request -> BarsQuery -> fetch_data_type -> rate budget.

    The priority used to be computed next to the budget; it now crosses the interface, so this
    drives the whole path with a real AlpacaRead and records what the budget is asked for.
    """
    budget = PriorityRecordingBudget()
    client = stub_client([START])
    ingest_control.install_readers(
        {DataSource.ALPACA_API: AlpacaRead(client=client, executor_provider=lambda: executor)}
    )

    with patch.object(broker_api, '__RATE_BUDGET', budget):
        await ingest_control.store_retrieve_stock(build_request(update_type=update_type))

    assert budget.priorities == [EXPECTED_PRIORITY[update_type]]


def test_a_bars_query_without_a_priority_is_a_type_error():
    # Required with no default: a silent INTERACTIVE would outrank BACKFILL for a caller that forgot.
    with pytest.raises(TypeError, match='priority'):
        BarsQuery(instrument=Instrument('VFV', AssetType.STOCK), granularity=Granularity.ONE_DAY, start=START)


# ---------------------------------------------------------------------------------------------
# Injection: create_app and the lifespan
# ---------------------------------------------------------------------------------------------


class LifespanProbe:
    """The lifespan's collaborators replaced, and what the readers were at each step recorded.

    Kafka is replaced at its public entry points -- KafkaConsumerFactory.wait_for_kafka and the
    ingest RPC factory's init_servers -- and the latency server at app_depends' own name for it.
    Nothing inside the lifespan under test is patched.
    """

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self.rpc_servers = Mock()
        self.rpc_servers.shutdown.side_effect = lambda: self.events.append(('rpc-shutdown', installed_readers()))
        self.__stack = ExitStack()

    def __enter__(self) -> 'LifespanProbe':
        self.__stack.enter_context(patch.object(KafkaConsumerFactory, 'wait_for_kafka', return_value=True))
        self.__stack.enter_context(patch.object(app_depends, 'initialize_latency_server'))
        self.__stack.enter_context(
            patch.object(get_dataset_request.rpc, 'init_servers', side_effect=self.__init_servers)
        )
        return self

    def __exit__(self, *exc_info) -> None:
        self.__stack.close()

    def __init_servers(self):
        self.events.append(('rpc-start', installed_readers()))
        return self.rpc_servers


@pytest.mark.asyncio
async def test_the_lifespan_installs_readers_before_the_rpc_servers_start_and_clears_them_at_teardown():
    """Decision tj-j4wknb INJECTION: installed BEFORE the RPC servers start, cleared at teardown.

    Before: a request arriving the moment the servers start would find no reader and raise
    NotImplementedError. After: the readers stay installed while the servers shut down (so a
    request still in flight is served) and are gone once the lifespan has ended (so nothing
    outlives the app that installed it).
    """
    reader = RecordingRead()
    app = main.create_app({DataSource.ALPACA_API: reader})

    with LifespanProbe() as probe:
        async with app.router.lifespan_context(app):
            serving = installed_readers()
        after = installed_readers()

    assert probe.events == [
        ('rpc-start', {DataSource.ALPACA_API: reader}),
        ('rpc-shutdown', {DataSource.ALPACA_API: reader}),
    ]
    assert serving == {DataSource.ALPACA_API: reader}
    assert after == {}


@pytest.mark.asyncio
async def test_the_production_app_serves_alpaca_through_alpaca_read_and_nothing_else():
    """data.ingest.app.main.app is create_app({ALPACA_API: AlpacaRead()}) -- exactly that mapping.

    Read from what the lifespan INSTALLS, not from main.py's source: that is what a request is
    actually dispatched through. No fake, no second source, no flag (decision tj-j4wknb R4).
    """
    with LifespanProbe():
        async with main.app.router.lifespan_context(main.app):
            readers = installed_readers()

    assert set(readers) == {DataSource.ALPACA_API}
    assert type(readers[DataSource.ALPACA_API]) is AlpacaRead
