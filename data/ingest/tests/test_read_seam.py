"""The broker read seam: the BrokerRead Protocol, AlpacaRead behind it, and how the app injects it.

HANDLES-2 (tj-irhy0a.7) against HANDLES-1 (tj-irhy0a.6, 539ec64 + 16dce3b). The design is decision
tj-j4wknb: addendum 2 B (BarsQuery / BarsResponse / get_bars), addendum 3 U7 (the Protocol is the
enforcement, no inheritance required), addendum 4 items 3 and 5 (Instrument; no capability flags,
an unsupported request raises BrokerUnsupportedError), addendum 5 (the half-open bar range) and
the body's INJECTION section (create_app hands the composition root's readers to the thing that
serves requests).

Zero network beyond loopback. The vendor is a stub client handed to AlpacaRead through its
constructor, and the lifespan's latency collaborator is replaced with a mock of its public entry
point (decision tj-j4wknb R4: mock libraries are fine for in-process unit tests). The lifespan's
gRPC host is the real one, bound to 127.0.0.1 by grpc_bind.LoopbackGrpc, because the lifespan reads
its bind address from the environment with no default (tj-3mk3u5.24). Readers standing in for a
broker are plain classes that conform structurally, like any BrokerRead.

THE TYPED RESULT (TE-5 tj-3mk3u5.37.6, re-pointing this file to TE-4 tj-3mk3u5.37.5): a refusal is a
BarsFailure RETURNED by get_bars, not a raise, and RecordingRead returns one too.

WHAT THIS FILE NO LONGER COVERS, and where it went (tj-3mk3u5.32). Half of it drove
ingest_control.store_retrieve_stock -- the Kafka edge -- because that was the only caller that built
a BarsQuery from a request and the only place a failure became a bare {}. tj-3mk3u5.11 deletes the
module, so what remains here is the SEAM ITSELF: the Protocol, AlpacaRead behind it, and how the app
injects a reader. The retirement record in the middle of the file names every test that went and the
successor for each; it is written out rather than left to the bead, because a reader asking "was
this ever covered?" should not have to find the bead to answer it.
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
from common.errors.vocabulary import InvalidRequestError, Reason, TraderJoeError
from data.ingest.app import app_depends, grpc_host, main
from data.ingest.app.brokers.alpaca import broker_api
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
from data.ingest.tests.grpc_bind import LoopbackGrpc
from schemas.data_ingest.get_dataset_request import StockDatasetRequest


pytestmark = pytest.mark.data_ingest

START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
# The half-open range under test: START <= t < END.
END = START + timedelta(hours=1)
BETWEEN = START + timedelta(minutes=30)
AFTER = END + timedelta(minutes=30)
# When RecordingRead's stand-in vendor answered.
AS_OF = datetime(2026, 10, 2, 12, tzinfo=UTC)

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
        failure (TraderJoeError | None): RETURNED by get_bars as a BarsFailure: the typed result.
        error (BaseException | None): Raised by get_bars itself, before any response: a bug (D5).
        iteration_error (BaseException | None): Raised while the bars are iterated.
    """

    def __init__(
        self,
        timestamps: tuple[datetime, ...] = (START,),
        failure: TraderJoeError | None = None,
        error: BaseException | None = None,
        iteration_error: BaseException | None = None,
    ):
        self.timestamps = timestamps
        self.failure = failure
        self.error = error
        self.iteration_error = iteration_error
        self.queries: list[BarsQuery] = []

    async def get_bars(self, query: BarsQuery) -> BarsResponse | BarsFailure:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        if self.failure is not None:
            return BarsFailure(self.failure)
        return BarsResponse(
            feed=Feed.IEX, bars=self.__iterate(), served_range=ServedRange(query.start, AS_OF), as_of=AS_OF
        )

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
    """A rate budget that grants every token and records the priority and deadline each was asked for.

    acquire's signature is RateBudget's since TE-4 (ADR tj-fa1rpu U4 = A): a priority and a deadline.
    """

    def __init__(self):
        self.calls: list[tuple[RequestPriority, datetime | None]] = []

    @property
    def priorities(self) -> list[RequestPriority]:
        return [priority for priority, _deadline in self.calls]

    async def acquire(self, priority: RequestPriority = RequestPriority.INTERACTIVE, deadline=None) -> None:
        self.calls.append((priority, deadline))


@pytest.fixture(autouse=True)
def no_state_leaks_between_tests():
    """Clear any cached Alpaca client around every test."""
    broker_api.set_client(None)
    yield
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
# Refusals: what Alpaca cannot serve is RETURNED as a BrokerUnsupportedError before any vendor call
# ---------------------------------------------------------------------------------------------

REFUSED = [
    pytest.param(
        {'instrument': Instrument('VFV', AssetType.STOCK, currency='CAD')},
        'currency',
        'CAD',
        Reason.UNSUPPORTED_INSTRUMENT,
        id='cad',
    ),
    pytest.param(
        {'instrument': Instrument('VFV', AssetType.STOCK, currency='EUR')},
        'currency',
        'EUR',
        Reason.UNSUPPORTED_INSTRUMENT,
        id='eur',
    ),
    pytest.param(
        {'instrument': Instrument('VFV', AssetType.STOCK, exchange='XNYS')},
        'exchange',
        'XNYS',
        Reason.UNSUPPORTED_INSTRUMENT,
        id='xnys',
    ),
    pytest.param(
        {'instrument': Instrument('VFV', AssetType.STOCK, exchange='XNAS', currency='USD')},
        'exchange',
        'XNAS',
        Reason.UNSUPPORTED_INSTRUMENT,
        id='xnas-even-in-usd',
    ),
    pytest.param(
        {'instrument': Instrument('BTC', AssetType.CRYPTO)},
        'asset_type',
        AssetType.CRYPTO,
        Reason.UNSUPPORTED_ASSET_TYPE,
        id='crypto',
    ),
    pytest.param(
        {'instrument': Instrument('VFV', AssetType.OPTION)},
        'asset_type',
        AssetType.OPTION,
        Reason.UNSUPPORTED_ASSET_TYPE,
        id='option',
    ),
    # The builder's choice for an adjustment, the one of the three reasons that fits (TE-4 open point).
    pytest.param({'adjustment': 'split'}, 'adjustment', 'split', Reason.UNSUPPORTED_INSTRUMENT, id='split-adjusted'),
    pytest.param({'adjustment': 'all'}, 'adjustment', 'all', Reason.UNSUPPORTED_INSTRUMENT, id='all-adjusted'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(('overrides', 'field', 'value', 'reason'), REFUSED)
async def test_alpaca_refuses_what_it_cannot_serve_before_any_vendor_call(
    overrides: dict, field: str, value, reason: Reason, executor: ThreadPoolExecutor, single_flight
):
    """Addendum 4 items 3 and 5: a handle that cannot serve the combination REFUSES, never guesses.

    Alpaca serves one consolidated US line per symbol, in dollars, stocks only, raw bars only.
    Anything else would otherwise be silently served as a raw USD stock fetch. The refusal is a
    typed error RETURNED in a BarsFailure (TE-4), REFUSED by its reason, its detail names the field
    and the value, and neither the injected client nor the single flight (the only way to the
    vendor) is touched.
    """
    client = Mock()
    reader = AlpacaRead(client=client, executor_provider=lambda: executor)

    outcome = await reader.get_bars(build_query(**overrides))

    assert isinstance(outcome, BarsFailure)
    error = outcome.error
    assert type(error) is BrokerUnsupportedError
    assert isinstance(error, InvalidRequestError)
    assert error.reason is reason
    assert field in error.detail
    assert repr(value) in error.detail
    assert client.mock_calls == [], 'the vendor client was touched before the refusal'
    assert single_flight.keys == [], 'a vendor call was attempted before the refusal'


@pytest.mark.asyncio
@pytest.mark.parametrize(('overrides', 'field', 'value', 'reason'), REFUSED)
async def test_a_refusal_comes_before_the_credential_is_even_resolved(overrides: dict, field: str, value, reason):
    """With no client injected and no credentials set, the refusal still wins.

    So the refusal comes before broker_api.get_client() as well as before the vendor call: a
    request Alpaca cannot serve is reported as unsupported, not as a missing credential (VENDOR_AUTH).
    """
    reader = AlpacaRead()

    with patch.dict(os.environ, {}, clear=True):
        outcome = await reader.get_bars(build_query(**overrides))

    assert isinstance(outcome, BarsFailure)
    assert type(outcome.error) is BrokerUnsupportedError
    assert outcome.error.reason is reason


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


# ---------------------------------------------------------------------------------------------
# RETIRED ON tj-3mk3u5.32: everything this file pinned THROUGH ingest_control.store_retrieve_stock
# ---------------------------------------------------------------------------------------------
#
# tj-3mk3u5.11 deletes data/ingest/app/ingest_control.py outright -- the three store_retrieve_*
# dispatch functions AND install_readers/clear_readers, which existed to hand the module-level Kafka
# handlers their readers. The gRPC path replaced that with constructor injection into
# IngestFetchHandler, so there is no module-global reader registry left to install into or read
# back. Eleven tests here drove that function. Each is accounted for below, because "it was deleted
# with the Kafka edge" is only an acceptable answer where the BEHAVIOUR went with it.
#
# DIED WITH THE EDGE -- no successor, and none wanted. These pinned the bare {} that
# store_retrieve_stock returned for every failure, which is the one place D6's banned
# return-a-sentinel form survived, and the single log line that carried the reason because no caller
# could see it. PR 2's typed errors replaced all of it: the servicer reports the TraderJoeError
# itself (common/rpc/errors.py), so there is nothing to swallow and nothing to recover from a log.
#   test_the_kafka_edge_answers_every_bars_failure_with_the_bare_empty_dict
#   test_a_reader_that_raises_is_still_swallowed_into_a_bare_empty_dict
#   test_the_kafka_edge_logs_a_bars_failure_with_its_reason_and_its_cause_chain
#   test_a_missing_credential_is_not_swallowed -- the RE-RAISE BY NAME was the edge's deviation from
#     "{} for ANY BarsFailure", kept so an operator saw the variable rather than an empty batch. On
#     gRPC the typed error is the reply, so nothing has to be re-raised to survive the hop. The
#     adapter's returned form is pinned by test_broker_api.py's
#     test_a_request_without_credentials_or_a_client_fails_before_any_call and by
#     test_typed_outcomes.py's VENDOR_AUTH cases.
#
# MOVED, because the behaviour is the gRPC handler's now. Each successor was read before this was
# deleted, not assumed:
#   test_store_retrieve_stock_dispatches_by_the_request_source
#     -> test_fetch_dataset_handler.py::test_the_reader_for_the_requests_source_is_the_one_called_and_the_others_are_not,
#        written on this bead precisely because nothing on the gRPC side had it.
#   test_an_unmapped_source_raises_not_implemented
#     -> test_fetch_dataset_handler.py::test_a_source_with_no_reader_installed_raises_before_the_ack_and_before_any_vendor_call
#   test_empty_data_types_raise_value_error_before_any_get_bars_call
#   test_quotes_and_trades_alone_are_refused_as_unsupported_without_fetching_bars
#     -> the 'empty', 'quote', 'trade' and 'bars-and-quote' cases of
#        test_fetch_dataset_handler.py::test_an_unservable_request_is_refused_before_the_ack_and_before_the_vendor_is_called
#   test_the_request_maps_onto_the_bars_query
#     -> test_fetch_dataset_handler.py::test_the_handler_passes_the_deadline_the_priority_and_the_instrument_through_to_the_query
#        (instrument, granularity, start, end, priority, deadline) and
#        ::test_the_ack_comes_first_carrying_the_feed_the_adapter_resolved_not_the_one_the_request_named
#        (feed). The gRPC side asserts field by field rather than comparing a whole BarsQuery, which
#        is weaker against an ADDED field; that is a known and accepted difference, not an oversight.
#        feed and deadline inverted rather than moved: the Kafka edge pinned them as always None,
#        and carrying both is the gRPC path's whole point.
#   test_the_request_priority_reaches_the_alpaca_rate_budget, and EXPECTED_PRIORITY with
#     test_the_expected_priority_table_covers_every_update_type
#     -> test_rate_budget.py, where the table and its exhaustiveness check now live. They were here
#        because this was the only place every UpdateType was driven end to end; the hand-written
#        three-line version in test_rate_budget.py would have let a fourth member be added with no
#        priority at all, so the table moved rather than being dropped.
#   test_the_rpc_batch_carries_no_bar_stamped_at_end -- the half-open [start, end) rule, observed on
#     the batch. The adapter half is directly above, in
#     test_a_bar_stamped_exactly_at_end_is_excluded_whatever_the_offset, and the contract suite
#     applies it to every reader. Only the BATCH's view of it goes.


def test_a_bars_query_without_a_priority_is_a_type_error():
    # Required with no default: a silent INTERACTIVE would outrank BACKFILL for a caller that forgot.
    with pytest.raises(TypeError, match='priority'):
        BarsQuery(instrument=Instrument('VFV', AssetType.STOCK), granularity=Granularity.ONE_DAY, start=START)


# ---------------------------------------------------------------------------------------------
# Injection: create_app and the lifespan
# ---------------------------------------------------------------------------------------------


class LifespanProbe:
    """The lifespan's collaborators replaced, and the readers it handed to the gRPC registration.

    WHAT IS REPLACED, AND WHY IT IS SO LITTLE: the latency server, at app_depends' own name for it,
    and nothing else. A tolerant Kafka stub sat beside it from tj-3mk3u5.32 until tj-iwiq23, for the
    window in which the lifespan still waited on a broker; tj-3mk3u5.11 removed the wait and
    tj-3mk3u5.14 the module, and removing the stub changed no result. Nothing inside the lifespan
    under test is patched. Enter the lifespan inside LoopbackGrpc, which gives its real gRPC host a loopback
    address and stops it whatever happens.

    REGISTERED_SERVICES IS WRAPPED, NOT REPLACED: the real one still runs and its services are still
    the ones served, so the lifespan is not hollowed out by being observed. What is recorded is the
    readers mapping it was handed, which since tj-3mk3u5.10 is HOW A READER REACHES A REQUEST --
    constructor injection into IngestFetchHandler. Until tj-3mk3u5.32 this class recorded the Kafka
    RPC servers starting and stopping instead, and read the readers back out of ingest_control's
    module global; tj-3mk3u5.11 deletes both of those.
    """

    def __init__(self):
        self.handed: list[dict] = []
        self.__real = grpc_host.registered_services
        self.__stack = ExitStack()

    def __enter__(self) -> 'LifespanProbe':
        self.__stack.enter_context(patch.object(app_depends, 'initialize_latency_server'))
        self.__stack.enter_context(
            patch.object(grpc_host, 'registered_services', side_effect=self.__registered_services)
        )
        return self

    def __exit__(self, *exc_info) -> None:
        self.__stack.close()

    def __registered_services(self, readers):
        # The real one, captured before the patch: it still builds the services the host serves.
        self.handed.append(dict(readers))
        return self.__real(readers)


@pytest.mark.asyncio
async def test_the_lifespan_hands_the_readers_create_app_received_to_the_grpc_registration():
    """Decision tj-j4wknb INJECTION, re-pointed on tj-3mk3u5.32 to the injection that survives.

    It used to assert that the readers were installed into ingest_control BEFORE the Kafka RPC
    servers started and cleared at teardown, so that a request arriving the instant the servers came
    up could not find an empty registry. tj-3mk3u5.11 deletes install_readers, clear_readers and the
    RPC servers together: there is no window left to get wrong, because the handler is CONSTRUCTED
    with its readers and cannot exist without them.

    What still has to be true, and is what this now asserts, is that the READER create_app was given
    is the reader the registration is handed: the composition root decides which handle serves each
    source, and nothing in between may substitute its own. The `is` check is against the reader
    rather than against the mapping, and that is deliberate rather than sloppy -- a rebuilt-but-equal
    MAPPING is caught next door, by test_grpc_host.py's
    test_what_registered_services_returns_is_what_the_lifespan_serves_and_it_gets_the_injected_readers,
    which compares the mapping itself and reds on exactly that. Measured, not assumed: handing
    registered_services a dict(readers) copy reds that test and not this one.
    """
    reader = RecordingRead()
    app = main.create_app({DataSource.ALPACA_API: reader})

    with LifespanProbe() as probe:
        async with LoopbackGrpc(), app.router.lifespan_context(app):
            pass

    assert probe.handed == [{DataSource.ALPACA_API: reader}]
    assert probe.handed[0][DataSource.ALPACA_API] is reader


@pytest.mark.asyncio
async def test_the_production_app_serves_alpaca_through_alpaca_read_and_nothing_else():
    """data.ingest.app.main.app is create_app({ALPACA_API: AlpacaRead()}) -- exactly that mapping.

    Read from what the lifespan HANDS THE REGISTRATION, not from main.py's source: that is what a
    request is actually dispatched through. No fake, no second source, no flag (decision tj-j4wknb
    R4). It read ingest_control's installed readers until tj-3mk3u5.32, for the same reason and
    through the registry tj-3mk3u5.11 deletes.

    THIS IS THE ONLY TEST THAT LOOKS AT main.app's OWN MAPPING. Every other lifespan and
    registration test, here and in test_grpc_host.py, builds an app with create_app(<test readers>),
    so a production composition root that wired the wrong reader -- or a second one -- would pass
    all of them.
    """
    with LifespanProbe() as probe:
        async with LoopbackGrpc(), main.app.router.lifespan_context(main.app):
            pass

    [readers] = probe.handed
    assert set(readers) == {DataSource.ALPACA_API}
    assert type(readers[DataSource.ALPACA_API]) is AlpacaRead
