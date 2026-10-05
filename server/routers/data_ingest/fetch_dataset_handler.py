"""THE DOMAIN SIDE of the FetchDataset server seam (decision tj-tkm4tn): bars in, domain events out.

common/rpc/ingest.py owns every FetchStreamEncoder call and every generated message on this hop (ADR
tj-8konfu D3, enforced by ruff's TID251 outside common/rpc/**). This module never imports trader_joe.proto
and never holds a generated message; it conforms STRUCTURALLY to common.rpc.ingest.FetchDatasetHandler --
by shape, as BrokerRead is conformed to -- so it does not import that module either (tj-tkm4tn D1).

WHAT THIS MODULE DOES NOT DO, because something else already does it (tj-tkm4tn, amending tj-3mk3u5.9):
    - resolve the feed. BarsResponse.feed IS the resolved feed, decided by the adapter before iteration
      (brokers/interface.py); an unservable one comes back as a BarsFailure carrying BrokerUnsupportedError
      with FEED_NOT_AVAILABLE. This module only reads response.feed and never calls sip_enabled().
    - wire the single-flight guard or the rate budget. Both live inside the adapter (interface.py line 207,
      alpaca/broker_api.py), which is why the Kafka handler (ingest_control.py) does nothing explicit about
      either. Calling reader.get_bars is the whole of reusing them; this module holds no slot, so there is
      no slot release to design and nothing here waits on the vendor call returning (tj-6znw1h is
      structurally absent from this path).
    - install the error boundary. GrpcServerHost puts ErrorBoundaryInterceptor first in every server it
      builds and offers no way to remove it (ADR tj-fa1rpu D1(b); tj-19r2z5).

CHUNKING IS THIS MODULE'S JOB (tj-tkm4tn D2). MAX_PAGE_BARS comes from common.rpc.mapping.fetch_stream --
a plain constant, not a generated symbol, so TID251 permits the import -- and is never re-spelled.
FetchStreamEncoder.page() RAISES above it rather than splitting, which is coherent only because the
splitting happens here, beside the code draining the vendor's AsyncIterator: that is what keeps the
single-message memory profile off this path (tj-rh4b7f's third volume ceiling). BarsResponse.bars is
drained incrementally and never materialised as a whole list.

THE ONE IN-BAND FAILURE IS A RAISE, NOT A YIELD (tj-tkm4tn D3; user ruling tj-3mk3u5.22 Q5). A BarsFailure
whose error is FEED_NOT_AVAILABLE is re-raised before anything is yielded; common/rpc/ingest.py converts it
with refused_response() and ends the call OK. Every other BarsFailure, and FEED_NOT_AVAILABLE after the
ack, is also just re-raised -- the servicer's own rule decides which of those is in-band, not this module.
Validating data_types and looking up the reader BEFORE calling get_bars keeps every knowable failure ahead
of the ack, which is what an ack promises (tj-3mk3u5.9).

A NON-STOCK asset_type IS ALSO REFUSED HERE, BESIDE QUOTE AND TRADE (tj-msd6qo, architect ruling at the
tj-3mk3u5.9 gate). The Kafka path never let a reader see a non-STOCK asset type at all: the refusal lived
in METHOD DISPATCH (ingest_control.store_retrieve_crypto / store_retrieve_option), not in a reader and not
in a field. Collapsing three methods into this one handler with asset_type as a field deleted that
dispatch, and BrokerRead's contract does not require a reader to refuse an asset_type it cannot serve --
AlpacaRead happens to, FakeRead deliberately does not, because the fake stands for the interface rather
than for Alpaca's limits. Checking it here, not in the reader, is what makes this path answer the same
whichever reader is installed, which is the property the path it replaces had.

CANCELLATION IS EXACTLY ONE DUTY (tj-tkm4tn D5): close the bars iterator on every exit path. The try/finally
below covers all three ways this generator ends -- it runs out, it propagates a TraderJoeError the adapter's
iterator raised, or the caller (common/rpc/ingest.py's IngestServicer, or the asyncgen shutdown hook on a
cancelled call) throws GeneratorExit in at the generator's last suspension point. All three unwind through
the same finally before the adapter's resource is ever abandoned, whether that unwind is pinned to the
moment of cancellation or deferred to the event loop's own asyncgen finalisation -- the finally runs exactly
once either way, so no separate mechanism is needed on the servicer's side of the seam.
"""

from collections.abc import AsyncIterator, Mapping
from datetime import datetime

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource
from common.errors.vocabulary import InvalidRequestError, Reason
from common.logging import get_logger
from common.rpc.mapping.fetch_stream import MAX_PAGE_BARS
from data.ingest.app.brokers.interface import BarsFailure, BarsQuery, BarsResponse, BrokerRead, Instrument
from data.ingest.app.brokers.rate_budget import priority_for_update_type
from schemas.data_ingest import fetch_dataset as domain


log = get_logger(__name__)


class IngestFetchHandler:
    """Serves one FetchDataset call by draining a BrokerRead and chunking its bars into domain pages.

    Conforms structurally to common.rpc.ingest.FetchDatasetHandler. One instance is built per gRPC host
    (data/ingest/app/grpc_host.py, registered_services) and serves every call; it holds no per-call state
    of its own besides the readers mapping it was built with.
    """

    def __init__(self, readers: Mapping[DataSource, BrokerRead]) -> None:
        """Bind the handler to the broker handle serving each data source.

        Args:
            readers: Handle serving each data source, the same mapping installed into ingest_control for
                the Kafka path. Read only; nothing here mutates it.
        """
        self._readers = readers

    async def fetch(
        self, request: domain.FetchDatasetRequest, *, deadline: datetime | None
    ) -> AsyncIterator[domain.FetchAccepted | domain.BarPage | domain.FetchDone]:
        """Fetch one dataset: the accepted ack, then its pages, then its done.

        Args:
            request: What to fetch.
            deadline: When the call expires, an aware UTC datetime or None, passed straight through as
                BarsQuery.deadline -- it bounds only the rate-budget acquire, never the vendor call
                (broker_api.py; tj-6znw1h).

        Yields:
            domain.FetchAccepted: First, always, carrying the resolved feed.
            domain.BarPage: Zero or more, each with at most MAX_PAGE_BARS bars and never empty.
            domain.FetchDone: Last, with the true bar count, the served range and as_of.

        Raises:
            InvalidRequestError: UNSUPPORTED_ASSET_TYPE for a request naming QUOTE or TRADE (still out of
                scope; bars only) or a non-STOCK asset_type (tj-msd6qo; stock only, independent of which
                reader is installed), or INVALID_REQUEST for an empty data_types -- all known before any
                vendor call, so all are raised before the ack.
            NotImplementedError: If no reader is installed for request.source.
            TraderJoeError: Whatever reader.get_bars returns as a BarsFailure, re-raised as-is. The one
                raised before the ack with reason FEED_NOT_AVAILABLE is the hop's in-band refusal; every
                other case -- any other reason, or FEED_NOT_AVAILABLE after the ack, or a TraderJoeError the
                adapter's bars iterator raises mid-stream -- escapes to the error boundary unchanged.
        """
        # Validate everything knowable before the vendor is ever called: an ack is a promise the request is
        # viable, so nothing below this point may fail for a reason that was already decidable here.
        if not request.data_types:
            raise InvalidRequestError(Reason.INVALID_REQUEST, 'data_types must not be empty')
        if DataType.QUOTE in request.data_types:
            raise InvalidRequestError(Reason.UNSUPPORTED_ASSET_TYPE, 'Quotes are not supported yet: data_type=QUOTE')
        if DataType.TRADE in request.data_types:
            raise InvalidRequestError(Reason.UNSUPPORTED_ASSET_TYPE, 'Trades are not supported yet: data_type=TRADE')
        if request.asset_type is not AssetType.STOCK:
            # tj-msd6qo: the Kafka path refused crypto and option at METHOD DISPATCH
            # (ingest_control.store_retrieve_crypto / store_retrieve_option), before any reader saw the
            # request, so no reader on that path was ever handed a non-STOCK asset type. Collapsing three
            # methods into this one handler with asset_type as a field deleted that dispatch; this restores
            # it. Checked here rather than left to the reader: BrokerRead's contract does not require a
            # reader to refuse an asset_type it does not serve (AlpacaRead happens to; FakeRead does not,
            # deliberately -- it stands for the interface, not for Alpaca's limits), so leaving this to the
            # reader would make the gRPC path's answer depend on which reader is installed. Wording matches
            # ingest_control's refusal.
            raise InvalidRequestError(
                Reason.UNSUPPORTED_ASSET_TYPE,
                f'{request.asset_type.name.capitalize()} datasets are not supported yet: '
                f'asset_type={request.asset_type.name}',
            )

        reader = self._readers.get(request.source)
        if reader is None:
            raise NotImplementedError(f'data source not implemented: source={request.source!r}')

        query = BarsQuery(
            instrument=Instrument(
                symbol=request.asset_symbol, asset_type=request.asset_type, exchange=None, currency=None
            ),
            granularity=request.granularity,
            start=request.start,
            end=request.end,
            priority=priority_for_update_type(request.update_type),
            # The caller may name a feed; the adapter alone decides whether it can be served
            # (tj-tkm4tn D1 -- this module does not call sip_enabled()).
            feed=request.feed,
            deadline=deadline,
        )
        response: BarsResponse | BarsFailure = await reader.get_bars(query)
        if isinstance(response, BarsFailure):
            # Re-raised as-is: common/rpc/ingest.py is the only place that knows which reason, and which
            # position in the stream, makes a failure the one in-band refusal.
            raise response.error

        bars = response.bars
        try:
            yield domain.FetchAccepted(feed=response.feed)

            total = 0
            batch: list[domain.Bar] = []
            async for bar in bars:
                batch.append(
                    domain.Bar(
                        bar_start=bar.timestamp,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=bar.volume,
                        trade_count=bar.trade_count,
                        vwap=bar.vwap,
                        # Stamped from the one feed resolved for this whole fetch, never decided per bar
                        # (schemas/data_ingest/fetch_dataset.py, Bar.feed): the encoder raises instead of
                        # overwriting a mismatch, and keeping this the only place a bar's feed is set is
                        # what makes a mismatch impossible by construction.
                        feed=response.feed,
                    )
                )
                total += 1
                if len(batch) >= MAX_PAGE_BARS:
                    yield domain.BarPage(bars=batch)
                    batch = []
            if batch:
                yield domain.BarPage(bars=batch)
        finally:
            # The one cancellation duty (tj-tkm4tn D5): see the module docstring.
            await bars.aclose()

        log.debug(f'FetchDataset served bars: total={total}, source={request.source!r}')
        yield domain.FetchDone(
            bar_count=total,
            served_range=domain.ServedRange(start=response.served_range.start, end=response.served_range.end),
            as_of=response.as_of,
        )
