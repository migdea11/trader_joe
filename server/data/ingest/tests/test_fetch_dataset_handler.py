"""IngestFetchHandler in domain terms alone: a fake BrokerRead in, FetchAccepted/BarPage/FetchDone out.

No server, no channel, no generated message appears anywhere in this file, which is the whole point of
the seam decision tj-tkm4tn draws (D1): the handler is exercised with a plain 'async for'. Everything
about encoding those events onto the wire belongs to common/rpc/ingest.py and is pinned by
common/tests/rpc/, not here.

WHAT THIS FILE PINS, and why each one earns its place:

* THE MULTI-PAGE PROOF (tj-tkm4tn D2). MAX_PAGE_BARS + 1 bars must come out as TWO pages, neither over
  the cap and no bar lost; exactly MAX_PAGE_BARS must come out as ONE. Both sides of the boundary. The
  5000 is never re-spelled: it is imported, as the handler imports it.
* CHUNK AS YOU GO (tj-rh4b7f's third volume ceiling; tj-r78vb5). SEPARATE from the page shapes above,
  because the shapes alone cannot see it: a handler that drained the whole window into a list and
  sliced it at the end yields exactly the same pages. What separates the two is the ORDER of vendor
  pulls against yielded pages, so ScriptedRead journals its pulls and one named test reads the gap.
* THE REFUSAL IS A RAISE BEFORE THE FIRST YIELD (tj-tkm4tn D3). Pinned as a position, not just as an
  exception type: the test asserts nothing was yielded, because 'raises eventually' is the bug the
  servicer's in-band conversion cannot survive -- once the ack is on the wire there is no refusal left
  to send.
* EVERY KNOWABLE FAILURE LANDS BEFORE THE ACK. An ack is a promise the request is viable, so the empty
  data_types, the unsupported data types, the non-STOCK asset types and the missing reader are all
  pinned as raising with get_bars never called at all -- which is stricter than the exception type and
  is the property the bead asks for.
* A NON-STOCK asset_type IS REFUSED BY THE HANDLER, NOT BY THE READER (tj-msd6qo). Pinned against
  FakeRead, which serves every asset type, with a control in the same test showing that this very
  reader answers that very instrument with bars -- so the refusal provably came from the handler.
  Pinned against AlpacaRead it would prove nothing, since AlpacaRead refuses it anyway. Its WORDING
  was pinned against the Kafka path's own two refusals until tj-3mk3u5.32; that test is retired
  where it stood, with the reasoning, because tj-3mk3u5.11 removes the path it compared against.
* THE READER THE REQUEST NAMES IS THE ONE CALLED, with others installed beside it. Every other test
  here builds the handler with one reader, so dispatch itself needs a case of its own.
* THE BARS ITERATOR IS CLOSED ON EVERY EXIT PATH (tj-tkm4tn D5), exactly once: exhaustion, a mid-stream
  raise from the adapter, an explicit aclose() after partial consumption, a double aclose(), and a task
  cancelled while the adapter is still awaiting. See the note on the asyncgen hook above those tests for
  the one finalisation route this file does not reach, and why that is complete rather than a gap.
* STRUCTURAL CONFORMANCE WITHOUT THE IMPORT. The handler must satisfy common.rpc.ingest.FetchDatasetHandler
  by shape while its module does not import that module at all (tj-tkm4tn D1). Asserting conformance
  through isinstance of a runtime-checkable Protocol would bless the opposite of the ruling, so the
  import ban is checked against the module's own source text and the shape is checked against the
  Protocol's signature rather than against the handler's inheritance.

THE FAKE'S BARS ARE A GENUINE ASYNC GENERATOR behind a thin wrapper, and the closure tests read TWO
separate facts off it, because they are not the same fact and one path has only the first:

    closed_by_handler  the handler called aclose() on the iterator. Its DUTY, discharged.
    body_finalised     the generator's own finally ran. The adapter's RESOURCE, released.

An async generator that has never been started has no body to finalise: aclose() on it is a complete and
correct close that runs nothing, because nothing was acquired. So a fetch abandoned at the ack -- before
the first bar is pulled -- can only show the first fact, and a test demanding the second there would be
measuring the fake's bookkeeping rather than the handler's behaviour. Every path that does reach the
generator's body asserts both.
"""

import asyncio
import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import ExogenousError, InvalidRequestError, Reason, TraderJoeError
from common.rpc.mapping.fetch_stream import MAX_PAGE_BARS
from common.tests.roots import SERVER_ROOT
from data.ingest.app.brokers.interface import Bar as BrokerBar
from data.ingest.app.brokers.interface import BarsFailure, BarsQuery, BarsResponse, BrokerUnsupportedError, Instrument
from data.ingest.app.brokers.interface import ServedRange as BrokerServedRange
from data.ingest.app.brokers.rate_budget import RequestPriority
from routers.data_ingest.fetch_dataset_handler import IngestFetchHandler
from schemas.data_ingest import fetch_dataset as domain
from tests.fakes.market_data import FakeRead


pytestmark = pytest.mark.data_ingest

START = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
AS_OF = datetime(2024, 1, 2, 21, 0, tzinfo=UTC)
SERVED_END = datetime(2024, 1, 2, 20, 0, tzinfo=UTC)
DEADLINE = datetime(2024, 1, 2, 21, 5, tzinfo=UTC)


def broker_bar(index: int) -> BrokerBar:
    """One broker-neutral bar, distinguishable from every other by its minute and its prices.

    Args:
        index (int): Which bar of the sequence.

    Returns:
        BrokerBar: The bar.
    """
    return BrokerBar(
        timestamp=START + timedelta(minutes=index),
        open=100.0 + index,
        high=101.0 + index,
        low=99.0 + index,
        close=100.5 + index,
        volume=1000.0 + index,
        trade_count=10 + index,
        vwap=100.25 + index,
    )


class ObservedBars:
    """The adapter's bars iterator, recording that the handler closed it and delegating everything else.

    A plain AsyncIterator with an aclose(), which is what BrokerRead's declared type allows and what every
    reader in the repository returns today. It wraps a real async generator rather than faking one, so the
    generator's own finally -- an adapter's resource release -- is observed where it genuinely runs.

    Args:
        inner (AsyncIterator[BrokerBar]): The generator doing the yielding.
        owner (ScriptedRead): Where the two observations are recorded.
    """

    def __init__(self, inner, owner: 'ScriptedRead') -> None:
        self.__inner = inner
        self.__owner = owner

    def __aiter__(self):
        """Return self, as an iterator does.

        Returns:
            ObservedBars: This iterator.
        """
        return self

    async def __anext__(self) -> BrokerBar:
        """Pull the next bar from the wrapped generator, recording the pull.

        Counting pulls here is what lets a test state the memory profile DIRECTLY -- how much of the
        vendor's window the handler had read by the time it yielded its first page -- rather than infer
        it from whether a generator's finally had run (tj-r78vb5).

        Returns:
            BrokerBar: The next bar.
        """
        bar = await self.__inner.__anext__()
        # Counted only once the pull SUCCEEDS, so the final __anext__ that raises StopAsyncIteration is
        # not mistaken for a bar and the count stays comparable with len(self.bars).
        self.__owner.pulls += 1
        self.__owner.journal.append('pull')
        return bar

    async def aclose(self) -> None:
        """Record that the handler discharged its one cancellation duty, and close the generator."""
        self.__owner.closed_by_handler += 1
        await self.__inner.aclose()


class ScriptedRead:
    """A BrokerRead that serves a scripted list of bars, recording the queries it was asked and its closure.

    Conforms to BrokerRead by shape, as every reader does. It is deliberately NOT one of the repository's
    real readers: what is under test is the handler's own behaviour given a contract-conforming adapter,
    so the adapter here is the contract and nothing else.

    Args:
        bars (list[BrokerBar] | None): The bars to serve, in order. None serves none.
        feed (Feed): The RESOLVED feed, which the adapter alone decides. Deliberately different from any
            feed a test puts on a request, so a handler that echoed request.feed back into the ack would
            be red.
        failure (TraderJoeError | None): Returned as a BarsFailure instead of serving, when set.
        raise_at (int | None): Raise raises_with after yielding this many bars, as an adapter whose
            iterator fails mid-stream is allowed to (interface.py, BrokerRead.get_bars).
        raises_with (TraderJoeError | None): What raise_at raises.
        pause_at (int | None): Await a never-set event after yielding this many bars, so a consumer can
            be cancelled while the adapter is genuinely suspended mid-fetch.
    """

    def __init__(
        self,
        bars: list[BrokerBar] | None = None,
        *,
        feed: Feed = Feed.IEX,
        failure: TraderJoeError | None = None,
        raise_at: int | None = None,
        raises_with: TraderJoeError | None = None,
        pause_at: int | None = None,
    ) -> None:
        self.bars = [] if bars is None else bars
        self.feed = feed
        self.failure = failure
        self.raise_at = raise_at
        self.raises_with = raises_with
        self.pause_at = pause_at
        self.queries: list[BarsQuery] = []
        # How many times the handler called aclose() on the iterator: its duty, and "exactly once".
        self.closed_by_handler = 0
        # Appended to by the generator's own finally, which is where an adapter would release a vendor
        # resource. Empty when the generator was never started, which is correct and not a miss.
        self.body_finalised: list[str] = []
        # How many bars the handler has pulled, and the interleaving of those pulls with what the
        # handler yielded. The journal is the gap analysis the paging tests cannot do: page SHAPES look
        # identical whether the handler chunks as it reads or drains everything and slices it, so only
        # the ORDER of pulls against yields tells the two apart (tj-r78vb5).
        self.pulls = 0
        self.journal: list[str] = []
        self.paused = asyncio.Event()
        self.__never_set = asyncio.Event()

    async def get_bars(self, query: BarsQuery) -> BarsResponse | BarsFailure:
        """Serve the scripted bars, or the scripted failure.

        Args:
            query (BarsQuery): What the handler asked for; recorded for inspection.

        Returns:
            BarsResponse | BarsFailure: The scripted outcome.
        """
        self.queries.append(query)
        if self.failure is not None:
            return BarsFailure(self.failure)
        return BarsResponse(
            feed=self.feed,
            bars=ObservedBars(self.__iterate(), self),
            served_range=BrokerServedRange(START, SERVED_END),
            as_of=AS_OF,
        )

    async def __iterate(self) -> AsyncIterator[BrokerBar]:
        try:
            for position, bar in enumerate(self.bars):
                if self.raise_at is not None and position == self.raise_at:
                    assert self.raises_with is not None
                    raise self.raises_with
                if self.pause_at is not None and position == self.pause_at:
                    self.paused.set()
                    await self.__never_set.wait()
                yield bar
        finally:
            self.body_finalised.append('closed')


def a_request(
    *,
    data_types: list[DataType] | None = None,
    source: DataSource = DataSource.ALPACA_API,
    asset_type: AssetType = AssetType.STOCK,
    feed: Feed | None = None,
    update_type: UpdateType = UpdateType.STATIC,
) -> domain.FetchDatasetRequest:
    """A valid FetchDatasetRequest, with the one field a test is about overridden.

    Args:
        data_types (list[DataType] | None): What to fetch; market activity by default.
        source (DataSource): Which reader to dispatch to.
        asset_type (AssetType): The asset path.
        feed (Feed | None): The tape the caller names, or None to leave it to the deployment.
        update_type (UpdateType): How the dataset is kept up to date; drives the rate budget priority.

    Returns:
        domain.FetchDatasetRequest: The request.
    """
    return domain.FetchDatasetRequest(
        owner='tester',
        source=source,
        asset_symbol='AAPL',
        asset_type=asset_type,
        data_types=[DataType.MARKET_ACTIVITY] if data_types is None else data_types,
        granularity=Granularity.ONE_MINUTE,
        start=START,
        end=SERVED_END,
        update_type=update_type,
        feed=feed,
    )


async def drain(handler: IngestFetchHandler, request: domain.FetchDatasetRequest, *, deadline=None) -> list:
    """Run one fetch to completion and collect its events.

    Args:
        handler (IngestFetchHandler): The handler under test.
        request (domain.FetchDatasetRequest): What to fetch.
        deadline (datetime | None): The call's deadline.

    Returns:
        list: Every event the fetch yielded, in order.
    """
    return [event async for event in handler.fetch(request, deadline=deadline)]


# ---------------------------------------------------------------------------------------------------
# THE MULTI-PAGE PROOF: both sides of the MAX_PAGE_BARS boundary


@pytest.mark.asyncio
async def test_one_bar_over_the_page_cap_is_served_as_two_pages_with_no_bar_lost_and_none_oversized():
    """tj-tkm4tn D2 and tj-rh4b7f's third volume ceiling: the handler chunks, and the proof is two pages.

    MAX_PAGE_BARS + 1 is the smallest window that can distinguish chunking from accumulation. A handler
    that drained the vendor into one list and yielded it whole would yield ONE page of 5001 bars here --
    which FetchStreamEncoder.page() then refuses rather than splits, so the failure would surface a layer
    later as a ProtoMappingError with no hint of where the memory went.
    """
    reader = ScriptedRead([broker_bar(index) for index in range(MAX_PAGE_BARS + 1)])
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    events = await drain(handler, a_request())

    pages = [event for event in events if isinstance(event, domain.BarPage)]
    assert [len(page.bars) for page in pages] == [MAX_PAGE_BARS, 1]
    assert all(len(page.bars) <= MAX_PAGE_BARS for page in pages)
    served = [bar for page in pages for bar in page.bars]
    assert len(served) == MAX_PAGE_BARS + 1
    assert [bar.bar_start for bar in served] == [bar.timestamp for bar in reader.bars]


@pytest.mark.asyncio
async def test_exactly_the_page_cap_is_served_as_one_page_and_never_an_empty_second():
    """The other side of the same boundary: 5000 is one page, not a page and an empty one.

    Off by one the other way, the handler would flush at the cap and then flush the empty remainder,
    yielding a second page with no bars -- which the contract forbids ('a page is never empty', the
    amended bead) and which the encoder would happily put on the wire.
    """
    reader = ScriptedRead([broker_bar(index) for index in range(MAX_PAGE_BARS)])
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    events = await drain(handler, a_request())

    pages = [event for event in events if isinstance(event, domain.BarPage)]
    assert [len(page.bars) for page in pages] == [MAX_PAGE_BARS]


@pytest.mark.asyncio
async def test_the_first_page_is_yielded_before_the_vendor_iterator_has_been_drained():
    """CHUNK AS YOU GO, stated directly (tj-r78vb5). The two tests above cannot tell this apart.

    Both page-shape tests yield [5000, 1] and [5000] whether the handler chunks as it reads or drains
    the whole window into a list and slices it at the end. The shapes are identical; only the ORDER of
    vendor pulls against yielded pages separates them. That order IS the memory profile -- tj-rh4b7f's
    third volume ceiling, which is the entire reason tj-tkm4tn D2 puts the chunking in the handler,
    beside the code draining the vendor iterator.

    Until this test existed the property was pinned by one unlabelled line in a CLOSURE test, which
    worked only because a materialising handler would have exhausted the adapter's generator and run
    its finally. That is a proxy, in a test about something else, and one tidy-up away from being
    deleted as redundant.

    Two statements, because each catches what the other cannot:
      THE COUNT   at the moment the first page is delivered, exactly one page's worth has been pulled.
      THE GAP     at least one pull happens AFTER the first page -- the store side's gap analysis
                  (test_every_page_is_written_before_the_next_one_is_pulled) read from the producer's
                  end. That test proves the CONSUMER writes as it reads, but its double SCRIPTS pages
                  rather than producing them from a vendor iterator, so it never runs this handler.
    """
    reader = ScriptedRead([broker_bar(index) for index in range(MAX_PAGE_BARS + 10)])
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    pulls_at_first_page = None
    async for event in handler.fetch(a_request(), deadline=None):
        reader.journal.append(type(event).__name__)
        if isinstance(event, domain.BarPage) and pulls_at_first_page is None:
            pulls_at_first_page = reader.pulls

    assert pulls_at_first_page == MAX_PAGE_BARS, (
        f'the handler had pulled {pulls_at_first_page} bars when it yielded its first page, not '
        f'{MAX_PAGE_BARS}: it is reading more of the vendor window than one page needs, which is the '
        'single-message memory profile this migration exists to remove (tj-rh4b7f, tj-tkm4tn D2)'
    )

    first_page = reader.journal.index(domain.BarPage.__name__)
    assert 'pull' in reader.journal[first_page:], (
        'every vendor pull happened before the first page was yielded, so the handler materialised the '
        'whole dataset and then sliced it; pages must be produced as the bars arrive'
    )
    assert reader.pulls == MAX_PAGE_BARS + 10, 'the handler did not read the whole window it was asked for'


# ---------------------------------------------------------------------------------------------------
# THE CONTRACT ORDER, AND WHAT EACH EVENT CARRIES


@pytest.mark.asyncio
async def test_the_ack_comes_first_carrying_the_feed_the_adapter_resolved_not_the_one_the_request_named():
    """tj-tkm4tn D1: BarsResponse.feed IS the resolved feed, and the handler only reads it.

    The request names SIP and the adapter answers IEX, which cannot happen through a real reader -- a
    reader refuses a feed it cannot serve. It is scripted here precisely because it cannot: it is the one
    arrangement that tells a handler reading response.feed apart from one echoing request.feed, and the
    second would put a tape on the store's dataset entry that no bar actually came from.
    """
    reader = ScriptedRead([broker_bar(0)], feed=Feed.IEX)
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    events = await drain(handler, a_request(feed=Feed.SIP))

    assert isinstance(events[0], domain.FetchAccepted)
    assert events[0].feed is Feed.IEX
    assert reader.queries[0].feed is Feed.SIP, 'the named feed must reach the adapter, which alone judges it'


@pytest.mark.asyncio
async def test_every_bar_is_stamped_with_the_one_resolved_feed_and_carries_the_vendors_own_values():
    """schemas/data_ingest/fetch_dataset.py, Bar.feed: one feed for the whole fetch, stamped here.

    The encoder raises on a bar whose feed disagrees with the ack's rather than overwriting it, so the
    handler being the only place a bar's feed is set is what makes that disagreement impossible.
    """
    source_bar = broker_bar(7)
    reader = ScriptedRead([source_bar], feed=Feed.IEX)
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    events = await drain(handler, a_request(feed=Feed.SIP))

    (page,) = [event for event in events if isinstance(event, domain.BarPage)]
    (bar,) = page.bars
    assert bar.feed is Feed.IEX
    assert (bar.bar_start, bar.open, bar.high, bar.low, bar.close) == (
        source_bar.timestamp,
        source_bar.open,
        source_bar.high,
        source_bar.low,
        source_bar.close,
    )
    assert (bar.volume, bar.trade_count, bar.vwap) == (source_bar.volume, source_bar.trade_count, source_bar.vwap)


@pytest.mark.asyncio
async def test_the_done_is_last_and_carries_the_true_count_the_served_range_and_the_as_of():
    """The count is the bars actually yielded, not the page count and not the window the caller asked for."""
    reader = ScriptedRead([broker_bar(index) for index in range(3)])
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    events = await drain(handler, a_request())

    done = events[-1]
    assert isinstance(done, domain.FetchDone)
    assert done.bar_count == 3
    assert (done.served_range.start, done.served_range.end) == (START, SERVED_END)
    assert done.as_of == AS_OF


@pytest.mark.asyncio
async def test_a_genuinely_empty_window_is_an_ack_and_a_done_with_no_page_and_never_a_failure():
    """ADR tj-fa1rpu D2 through this hop: 'no data' is a SERVED outcome carrying provenance.

    served_range present with bar_count 0 is the positive statement that nothing happened in the window,
    which is a different answer from a fetch that failed -- and the store must be able to tell them apart.
    """
    reader = ScriptedRead([])
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    events = await drain(handler, a_request())

    assert [type(event) for event in events] == [domain.FetchAccepted, domain.FetchDone]
    assert events[-1].bar_count == 0


@pytest.mark.asyncio
async def test_the_handler_passes_the_deadline_the_priority_and_the_instrument_through_to_the_query():
    """The deadline is passed straight through as BarsQuery.deadline; the priority comes from update_type.

    The deadline bounds only the rate-budget acquire inside the adapter (broker_api.py; tj-6znw1h), which
    is exactly why the handler must hand it over rather than act on it: a handler that enforced it itself
    would be the thing tj-6znw1h rules out.
    """
    reader = ScriptedRead([])
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    await drain(handler, a_request(update_type=UpdateType.STREAM), deadline=DEADLINE)

    (query,) = reader.queries
    assert query.deadline == DEADLINE
    assert query.priority is RequestPriority.LIVE
    assert query.instrument == Instrument(symbol='AAPL', asset_type=AssetType.STOCK, exchange=None, currency=None)
    assert (query.granularity, query.start, query.end) == (Granularity.ONE_MINUTE, START, SERVED_END)


@pytest.mark.asyncio
async def test_no_deadline_reaches_the_adapter_as_none_rather_than_as_an_invented_bound():
    """A call carrying no deadline keeps today's unbounded wait on the rate budget, as the Kafka path does."""
    reader = ScriptedRead([])
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    await drain(handler, a_request(), deadline=None)

    assert reader.queries[0].deadline is None


# ---------------------------------------------------------------------------------------------------
# EVERY KNOWABLE FAILURE LANDS BEFORE THE ACK


@pytest.mark.asyncio
async def test_the_feed_refusal_is_raised_before_the_first_event_is_yielded():
    """tj-tkm4tn D3: FEED_NOT_AVAILABLE before the ack is the hop's one in-band refusal.

    POSITION is what this pins, not the exception type. The servicer converts a FEED_NOT_AVAILABLE into
    the refused ack only while its encoder is still unbuilt; a handler that yielded the ack and then
    raised the same error would take the one in-band failure and turn it into an INTERNAL with an
    error_id, which is the honest answer to a handler bug and a terrible answer to an unservable feed.
    """
    refusal = BrokerUnsupportedError(
        Reason.FEED_NOT_AVAILABLE, 'This deployment cannot serve the requested feed: feed=sip'
    )
    reader = ScriptedRead(failure=refusal)
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    yielded = []
    with pytest.raises(BrokerUnsupportedError) as raised:
        async for event in handler.fetch(a_request(feed=Feed.SIP), deadline=None):
            yielded.append(event)

    assert yielded == [], 'the refusal must precede the ack; nothing may be yielded before it'
    assert raised.value is refusal, 'the adapter error is re-raised as-is, so the servicer reads its reason'
    assert raised.value.reason is Reason.FEED_NOT_AVAILABLE


@pytest.mark.asyncio
async def test_any_other_adapter_failure_is_also_re_raised_as_is_before_the_ack():
    """Every other BarsFailure escapes to the error boundary unchanged: the handler adds no judgement.

    Which reason is in band is the servicer's to decide (tj-tkm4tn D3), so a handler that classified here
    would be the second, divergent copy of that rule.
    """
    failure = ExogenousError(Reason.VENDOR_UNAVAILABLE, 'The vendor failed on its side.')
    reader = ScriptedRead(failure=failure)
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    yielded = []
    with pytest.raises(ExogenousError) as raised:
        async for event in handler.fetch(a_request(), deadline=None):
            yielded.append(event)

    assert yielded == []
    assert raised.value is failure


@pytest.mark.parametrize(
    ('request_kwargs', 'expected_reason'),
    [
        ({'data_types': []}, Reason.INVALID_REQUEST),
        ({'data_types': [DataType.QUOTE]}, Reason.UNSUPPORTED_ASSET_TYPE),
        ({'data_types': [DataType.TRADE]}, Reason.UNSUPPORTED_ASSET_TYPE),
        ({'data_types': [DataType.MARKET_ACTIVITY, DataType.QUOTE]}, Reason.UNSUPPORTED_ASSET_TYPE),
        ({'asset_type': AssetType.CRYPTO}, Reason.UNSUPPORTED_ASSET_TYPE),
        ({'asset_type': AssetType.OPTION}, Reason.UNSUPPORTED_ASSET_TYPE),
    ],
    ids=['empty', 'quote', 'trade', 'bars-and-quote', 'crypto', 'option'],
)
@pytest.mark.asyncio
async def test_an_unservable_request_is_refused_before_the_ack_and_before_the_vendor_is_called(
    request_kwargs: dict, expected_reason: Reason
):
    """'Validate everything that can be validated BEFORE acking' (tj-3mk3u5.9): an ack promises viability.

    The Kafka handler checks QUOTE and TRADE AFTER its get_bars, which is harmless there because nothing
    has been promised yet. On this contract it would not be: the ack would already be on the wire, the
    store would already have written its dataset entry, and the refusal would arrive as an INTERNAL.
    'bars-and-quote' is the case that catches a handler checking only data_types[0].

    THE TWO asset_type CASES ARE tj-msd6qo, and they join this parametrisation rather than starting a
    second one because they are the same statement in the same position: this deployment does not serve
    that kind of market data yet, refused before the ack, with the same reason. CRYPTO and OPTION are
    both reachable -- the wire carries ASSET_TYPE_CRYPTO and ASSET_TYPE_OPTION (market/v1/enums.proto)
    and the mapper decodes them -- so neither is hypothetical.
    """
    reader = ScriptedRead([broker_bar(0)])
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    yielded = []
    with pytest.raises(InvalidRequestError) as raised:
        async for event in handler.fetch(a_request(**request_kwargs), deadline=None):
            yielded.append(event)

    assert yielded == []
    assert raised.value.reason is expected_reason
    assert reader.queries == [], 'the vendor was called for a request that was already known to be unservable'


@pytest.mark.parametrize('asset_type', [AssetType.CRYPTO, AssetType.OPTION], ids=['crypto', 'option'])
@pytest.mark.asyncio
async def test_a_non_stock_asset_type_is_refused_even_by_a_reader_that_would_have_served_it(asset_type: AssetType):
    """tj-msd6qo, and the ONLY form of this test that proves anything: drive it with the fake that serves everything.

    The defect was relying on BrokerRead to refuse. AlpacaRead happens to refuse a non-STOCK asset_type
    (alpaca/read.py:100), so a test using it would pass whether or not the handler checks at all -- it
    would pin Alpaca's behaviour and call it the handler's. FakeRead deliberately does not refuse, and
    that is ruled design, not an oversight in the fake (test_broker_read_contract.py: 'FakeRead's empty
    list puts every Alpaca refusal here for FakeRead: it serves them').

    THE CONTROL IS THE POINT. The second half calls FakeRead directly with the same non-STOCK instrument
    and shows it answers with bars. So the refusal in the first half cannot have come from the reader,
    and the only thing left that could have produced it is the handler. Without that control this is
    just another test that something, somewhere, said no.
    """
    reader = FakeRead(slow_delay_seconds=0)
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    yielded = []
    with pytest.raises(InvalidRequestError) as raised:
        async for event in handler.fetch(a_request(asset_type=asset_type), deadline=None):
            yielded.append(event)

    assert yielded == [], 'the refusal must precede the ack, as every pre-ack refusal does'
    assert raised.value.reason is Reason.UNSUPPORTED_ASSET_TYPE

    # The control: this reader serves that very instrument, so the refusal above was the handler's.
    served = await reader.get_bars(
        BarsQuery(
            instrument=Instrument(symbol='AAPL', asset_type=asset_type, exchange=None, currency=None),
            granularity=Granularity.ONE_MINUTE,
            start=START,
            end=SERVED_END,
            priority=RequestPriority.BACKFILL,
        )
    )
    assert isinstance(served, BarsResponse), f'FakeRead refused {asset_type} itself, so the test above proved nothing'
    assert [bar async for bar in served.bars], 'FakeRead served no bars, so the control establishes nothing'


# RETIRED ON tj-3mk3u5.32 (architect ruling, relayed with the tj-3mk3u5.11 amendment):
# test_the_refusal_is_word_for_word_the_one_the_kafka_path_gives_for_the_same_asset_type.
#
# It CALLED ingest_control.store_retrieve_crypto/_option rather than copying their messages, so that
# the two paths drifting apart turned red. That was the right design while two paths existed.
# tj-3mk3u5.11 deletes the Kafka one, and with it the premise: THE DRIFT THIS GUARDED CANNOT HAPPEN
# ANY MORE. ADDENDUM 3's rule -- a guard must be able to fail in the state it forbids -- then says
# retire it rather than weaken it into something that still looks like a guard.
#
# INLINING THE TWO (reason, detail) PAIRS WAS CONSIDERED AND REJECTED. It would convert a parity
# test into a pin on a human-readable sentence, and tj-8feral ruled that `detail` is prose a client
# never parses. An exact-wording pin reds on a harmless reword and teaches people to edit tests.
#
# THE BEHAVIOUR IS NOT LOST, and that was verified rather than assumed before deleting: the crypto
# and option cases of test_an_unservable_request_is_refused_before_the_ack_and_before_the_vendor_is_called
# above pin the reason and that no vendor call is made, and
# test_a_non_stock_asset_type_is_refused_even_by_a_reader_that_would_have_served_it proves the
# refusal is the handler's rather than the reader's. What goes is only the word-for-word parity.
#
# THE PROVENANCE BELONGS IN THE CODE, NOT HERE: one line on the asset_type guard's comment in
# routers/data_ingest/fetch_dataset_handler.py, recording that the wording was verified against the
# Kafka path at a named SHA before that path was deleted. That is builder-ingest's to write; a
# validator editing the file under review would destroy the review.


@pytest.mark.asyncio
async def test_the_reader_for_the_requests_source_is_the_one_called_and_the_others_are_not():
    """Positive dispatch: with several readers installed, the request's source picks exactly one.

    NEW ON tj-3mk3u5.32, and it closes a real gap rather than restating a neighbour. Every other
    test in this file builds the handler with a SINGLE reader, so a handler that ignored the mapping
    and took next(iter(readers.values())) would pass all of them. The test below it proves there is
    no FALLBACK when the source is absent; this proves there is no COLLAPSE when it is present.

    The property was pinned until now by test_read_seam.py's
    test_store_retrieve_stock_dispatches_by_the_request_source, which installed two readers into
    ingest_control and asserted the same thing. tj-3mk3u5.11 deletes that module, so the pin moves
    here, onto the constructor injection that replaced install_readers().
    """
    alpaca, ib = ScriptedRead([broker_bar(0)]), ScriptedRead([broker_bar(1)])
    handler = IngestFetchHandler({DataSource.ALPACA_API: alpaca, DataSource.IB_API: ib})

    async for _ in handler.fetch(a_request(source=DataSource.IB_API), deadline=None):
        pass

    assert len(ib.queries) == 1, 'the reader the request named was not the one asked'
    assert alpaca.queries == [], 'a reader for another source was asked for this request'


@pytest.mark.asyncio
async def test_a_source_with_no_reader_installed_raises_before_the_ack_and_before_any_vendor_call():
    """A bare NotImplementedError, as the Kafka handler raises (ingest_control.py): a misconfiguration.

    It is not a TraderJoeError on purpose. No caller sent anything wrong, so there is no reason in the
    vocabulary that fits; the boundary renders it INTERNAL with an error_id and logs the traceback under
    that id (common/rpc/errors.py), which is what an operator needs to find the missing reader.
    """
    reader = ScriptedRead([broker_bar(0)])
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    yielded = []
    with pytest.raises(NotImplementedError, match='data source not implemented'):
        async for event in handler.fetch(a_request(source=DataSource.IB_API), deadline=None):
            yielded.append(event)

    assert yielded == []
    assert reader.queries == []


@pytest.mark.asyncio
async def test_a_traderjoeerror_the_adapter_raises_mid_stream_escapes_after_the_pages_already_yielded():
    """interface.py: an adapter that can fail while its bars are iterated raises a TraderJoeError there.

    Nothing is swallowed and nothing is converted -- the pages already yielded stand, and the error
    reaches the boundary, which is the only honest answer once part of a dataset is on the wire.
    """
    mid_stream = ExogenousError(Reason.VENDOR_UNAVAILABLE, 'The connection was cut mid-fetch.')
    reader = ScriptedRead(
        [broker_bar(index) for index in range(MAX_PAGE_BARS + 10)], raise_at=MAX_PAGE_BARS + 2, raises_with=mid_stream
    )
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    yielded = []
    with pytest.raises(ExogenousError) as raised:
        async for event in handler.fetch(a_request(), deadline=None):
            yielded.append(event)

    assert raised.value is mid_stream
    assert [type(event) for event in yielded] == [domain.FetchAccepted, domain.BarPage]
    assert not any(isinstance(event, domain.FetchDone) for event in yielded), 'a failed fetch never completes'


# ---------------------------------------------------------------------------------------------------
# THE BARS ITERATOR IS CLOSED ON EVERY EXIT PATH, EXACTLY ONCE (tj-tkm4tn D5)
#
# THE ONE ROUTE NOT REACHED HERE, named rather than left implicit: the event loop's asyncgen finalisation
# hook. When the servicer's 'async for' is abandoned by an exception, Python does not close the generator
# at that moment; asyncio's hook schedules agen.aclose() and the loop runs it later. What the hook DOES is
# call aclose(), which the explicit-aclose test below pins directly; WHEN it fires is CPython's asyncio
# behaviour and not this repository's code, and a test for it would have to drive garbage collection and
# loop shutdown inside a test whose loop pytest-asyncio owns. So the handler's duty is pinned in full and
# the scheduler's timing is left to the scheduler.


@pytest.mark.asyncio
async def test_running_the_fetch_to_completion_closes_the_adapters_iterator_exactly_once():
    """The ordinary path: the iterator is closed once even though exhausting it already finalised its body."""
    reader = ScriptedRead([broker_bar(index) for index in range(3)])
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    await drain(handler, a_request())

    assert reader.closed_by_handler == 1
    assert reader.body_finalised == ['closed']


@pytest.mark.asyncio
async def test_abandoning_the_fetch_part_way_through_the_bars_closes_the_adapters_iterator():
    """The cancellation duty itself: a consumer that stops reading must not strand the vendor's resource.

    aclose() on the handler's generator is exactly what the event loop's asyncgen hook calls when the
    servicer's 'async for' is abandoned, so this is that path with the scheduler's timing removed. The
    fetch is abandoned after a PAGE rather than after the ack, so the adapter's generator is genuinely
    mid-body and its finally is a real resource release rather than a no-op.
    """
    reader = ScriptedRead([broker_bar(index) for index in range(MAX_PAGE_BARS + 10)])
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    fetch = handler.fetch(a_request(), deadline=None)
    assert isinstance(await anext(fetch), domain.FetchAccepted)
    assert isinstance(await anext(fetch), domain.BarPage)
    assert reader.closed_by_handler == 0, 'closed while the fetch was still live'
    assert reader.body_finalised == [], (
        'the adapter generator had already run its finally, so it was exhausted before the first page '
        'was yielded -- incidental here, and pinned deliberately by '
        'test_the_first_page_is_yielded_before_the_vendor_iterator_has_been_drained (tj-r78vb5)'
    )

    await fetch.aclose()

    assert reader.closed_by_handler == 1
    assert reader.body_finalised == ['closed']


@pytest.mark.asyncio
async def test_abandoning_the_fetch_at_the_ack_still_closes_the_iterator_the_adapter_handed_over():
    """Abandoned before the first bar is pulled: the duty is still discharged, on an unstarted generator.

    body_finalised stays empty here and that is CORRECT, not a miss: an async generator that was never
    started has no body to unwind and acquired nothing to release, so aclose() completes it by running
    nothing. What must still happen is the handler calling aclose() at all -- a handler that only closed
    the iterator once it had begun reading would leak every fetch the store gave up on immediately, which
    is the cheapest cancellation there is and so the likeliest.
    """
    reader = ScriptedRead([broker_bar(index) for index in range(MAX_PAGE_BARS + 1)])
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    fetch = handler.fetch(a_request(), deadline=None)
    assert isinstance(await anext(fetch), domain.FetchAccepted)
    assert reader.closed_by_handler == 0

    await fetch.aclose()

    assert reader.closed_by_handler == 1
    assert reader.body_finalised == []


@pytest.mark.asyncio
async def test_closing_an_already_closed_fetch_does_not_close_the_adapters_iterator_a_second_time():
    """'Exactly once' is the half of D5 that a finally alone does not give you for free.

    An adapter releasing a pooled connection in that finally would release it twice, and the second
    release would be of somebody else's connection.
    """
    reader = ScriptedRead([broker_bar(index) for index in range(10)])
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    fetch = handler.fetch(a_request(), deadline=None)
    await anext(fetch)
    await fetch.aclose()
    await fetch.aclose()

    assert reader.closed_by_handler == 1


@pytest.mark.asyncio
async def test_a_mid_stream_adapter_failure_still_closes_the_adapters_iterator():
    """The exception path through the same finally: an error must not leak the vendor's resource either."""
    reader = ScriptedRead(
        [broker_bar(index) for index in range(5)],
        raise_at=2,
        raises_with=ExogenousError(Reason.VENDOR_UNAVAILABLE, 'The connection was cut mid-fetch.'),
    )
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    with pytest.raises(ExogenousError):
        await drain(handler, a_request())

    assert reader.closed_by_handler == 1
    assert reader.body_finalised == ['closed']


@pytest.mark.asyncio
async def test_cancelling_a_fetch_suspended_inside_the_adapter_closes_the_adapters_iterator():
    """The real shape of 'the store cancels mid-stream': the vendor has not answered yet.

    The adapter is suspended in an await when the cancellation lands, so CancelledError is delivered
    INTO the generator body and its finally runs there and then -- no hook, no scheduler, no GC. This is
    the case where stranding the iterator would actually cost something, because a vendor call is in
    flight.
    """
    reader = ScriptedRead([broker_bar(index) for index in range(MAX_PAGE_BARS + 1)], pause_at=1)
    handler = IngestFetchHandler({DataSource.ALPACA_API: reader})

    task = asyncio.create_task(drain(handler, a_request()))
    await asyncio.wait_for(reader.paused.wait(), timeout=5)
    assert reader.body_finalised == [], 'closed while the adapter was still being awaited'

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert reader.closed_by_handler == 1
    assert reader.body_finalised == ['closed']


# ---------------------------------------------------------------------------------------------------
# STRUCTURAL CONFORMANCE, WITHOUT THE IMPORT THE RULING FORBIDS


def test_the_handler_module_does_not_import_the_protocol_it_conforms_to():
    """tj-tkm4tn D1: conformance is by SHAPE, so routers/data_ingest never imports common/rpc/ingest.py.

    Read off the module's own source rather than off sys.modules, because this test file imports
    common.rpc.ingest itself in the test below and would therefore see it loaded either way. The ban is
    on the handler module's text, and that is what is checked.
    """
    # SERVER_ROOT, not REPO_ROOT (tj-iontkq.2): the module name is routers.data_ingest.*, so the path
    # it spells is relative to the directory the service trees live in. The counted parents[3] this
    # replaces was the repository root, which stops being that directory after the move -- and the
    # failure would be a read_text on a path that does not exist, not a wrong assertion.
    source = Path(IngestFetchHandler.__module__.replace('.', '/') + '.py')
    text = (SERVER_ROOT / source).read_text(encoding='utf-8')
    # Import STATEMENTS only. The module's prose names both of these to explain why it does not import
    # them, and a substring search over the whole file would be red on that prose alone.
    imports = [
        line
        for line in text.splitlines()
        if line.startswith(('import ', 'from ')) or line.lstrip().startswith(('import ', 'from '))
    ]

    assert [line for line in imports if 'common.rpc.ingest' in line] == [], (
        'the handler module imports the Protocol it must only conform to (tj-tkm4tn D1)'
    )
    assert [line for line in imports if 'trader_joe.proto' in line] == [], (
        'the handler module imports the generated package (ADR tj-8konfu D3, TID251)'
    )


def test_the_handler_is_not_a_subclass_of_the_protocol_but_matches_its_fetch_signature():
    """Conformance proved the way the ruling asks for it: by shape, with inheritance explicitly absent.

    Asserting isinstance against the Protocol would pass just as happily for a handler that INHERITED
    from it, so it would bless the arrangement D1 rejects -- and it cannot even be written here, because
    FetchDatasetHandler is deliberately not @runtime_checkable. Comparing the signature instead is what
    actually fails if the two sides drift -- a renamed keyword, a positional deadline, a missing method --
    which is the only drift that can break tj-vs7txw's servicer.
    """
    from common.rpc.ingest import FetchDatasetHandler

    assert FetchDatasetHandler not in IngestFetchHandler.__mro__, 'the handler must conform by shape, not inherit'
    assert IngestFetchHandler.__mro__ == (IngestFetchHandler, object)

    protocol_signature = inspect.signature(FetchDatasetHandler.fetch)
    handler_signature = inspect.signature(IngestFetchHandler.fetch)

    assert list(handler_signature.parameters) == list(protocol_signature.parameters)
    assert handler_signature.parameters['deadline'].kind is inspect.Parameter.KEYWORD_ONLY
    assert protocol_signature.parameters['deadline'].kind is inspect.Parameter.KEYWORD_ONLY
    assert inspect.isasyncgenfunction(IngestFetchHandler.fetch), (
        'fetch must be an async generator: the servicer drives it with async for, and a coroutine '
        'returning an iterator would be awaited into an object it cannot iterate'
    )
