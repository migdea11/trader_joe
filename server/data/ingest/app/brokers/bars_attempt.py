"""One attempt at a bars request, for any broker: BrokerRead in, an attempt report out.

The AttemptRunner of a bars request (scheduler/executor.py). It calls BrokerRead.get_bars once, drains the bars
into bounded pages, and turns what came back into the trailer of the report: SUCCEEDED with its served range,
or FAILED with an error class the executor's retry gate reads. It never retries and never waits for the store;
both belong to the executor. It holds no vendor knowledge: Alpaca-shaped facts stay behind BrokerRead.

THE ERROR CLASS COMES FROM REASONS (ADR tj-fa1rpu D3, D6), never a second table. A REFUSED reason is PERMANENT.
A NOT_READY reason is RATE_LIMITED when it is one of the two rate reasons, which carry a reset_at to wait for,
and TRANSIENT otherwise.
"""

from dataclasses import dataclass

from common.enums.data_stock import Feed
from common.errors.vocabulary import REASONS, Outcome, Reason, TraderJoeError
from common.rpc.mapping.fetch_stream import MAX_PAGE_BARS
from data.ingest.app.brokers.interface import Bar, BarsFailure, BarsQuery, BrokerRead, CallTally, Instrument
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler.executor import AttemptResult
from data.ingest.app.scheduler.queue import ScheduledRequest
from schemas.data_ingest import fetch_dataset as domain
from schemas.data_ingest.delivery import (
    DeliveryErrorClass,
    DeliveryOutcome,
    DeliveryPayload,
    DeliveryTrailer,
    VendorCallStats,
)
from schemas.data_ingest.submit import BarsRequest


_RATE_REASONS = frozenset({Reason.VENDOR_RATE_LIMITED, Reason.RATE_BUDGET})


@dataclass(frozen=True)
class BarsWork:
    """The payload of a bars ScheduledRequest.

    Attributes:
        request (BarsRequest): What to fetch.
        priority (RequestPriority): The rate budget's priority class for its vendor calls.
        feed (Feed): The tape the Submit ack resolved, carried on a FAILED report whose attempt never learned one.
    """

    request: BarsRequest
    priority: RequestPriority
    feed: Feed


def domain_bar(bar: Bar, feed: Feed) -> domain.Bar:
    """Convert a broker-neutral bar to the domain bar, stamped with the one feed resolved for the whole fetch.

    Args:
        bar (Bar): The reader's bar.
        feed (Feed): The resolved feed of the fetch, never decided per bar.

    Returns:
        domain.Bar: The bar as the contract carries it.
    """
    return domain.Bar(
        bar_start=bar.timestamp,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        trade_count=bar.trade_count,
        vwap=bar.vwap,
        feed=feed,
    )


def error_class(error: TraderJoeError) -> DeliveryErrorClass:
    """Classify a failure for the retry gate from its reason's outcome.

    Args:
        error (TraderJoeError): The typed failure a reader returned.

    Returns:
        DeliveryErrorClass: PERMANENT for a REFUSED reason, RATE_LIMITED for a rate reason, else TRANSIENT.
    """
    if REASONS[error.reason].outcome is Outcome.REFUSED:
        return DeliveryErrorClass.PERMANENT
    if error.reason in _RATE_REASONS:
        return DeliveryErrorClass.RATE_LIMITED
    return DeliveryErrorClass.TRANSIENT


class BarsAttemptRunner:
    """Runs one attempt of a bars request through a BrokerRead. Conforms to scheduler.executor.AttemptRunner."""

    def __init__(self, reader: BrokerRead) -> None:
        """Bind the runner to the broker's reader.

        Args:
            reader (BrokerRead): What the attempt reads bars from.
        """
        self.__reader = reader

    async def __call__(self, request: ScheduledRequest, attempt_no: int) -> AttemptResult:
        """Run one attempt: one get_bars, its bars drained into pages.

        Args:
            request (ScheduledRequest): A request whose payload is BarsWork. Its ready_by bounds the rate wait.
            attempt_no (int): The attempt's number; carried by the executor's header, not read here.

        Returns:
            AttemptResult: SUCCEEDED with the pages and the served range, or FAILED with its class and, for a
            rate limit, the reset to wait for.
        """
        work: BarsWork = request.payload
        bars = work.request
        tally = CallTally()
        query = BarsQuery(
            instrument=Instrument(symbol=bars.asset_symbol, asset_type=bars.asset_type, exchange=None, currency=None),
            granularity=bars.granularity,
            start=bars.start,
            end=bars.end,
            priority=work.priority,
            feed=bars.feed,
            deadline=request.ready_by,
            tally=tally,
        )
        response = await self.__reader.get_bars(query)
        if isinstance(response, BarsFailure):
            return self.__failed(request, work, response.error, tally)

        parts: list[DeliveryPayload] = []
        batch: list[domain.Bar] = []
        try:
            async for bar in response.bars:
                batch.append(domain_bar(bar, response.feed))
                if len(batch) >= MAX_PAGE_BARS:
                    parts.append(domain.BarPage(bars=batch))
                    batch = []
        finally:
            await response.bars.aclose()
        if batch:
            parts.append(domain.BarPage(bars=batch))
        return AttemptResult(
            trailer=DeliveryTrailer(
                outcome=DeliveryOutcome.SUCCEEDED,
                vendor_stats=self.__stats(tally),
                served_range=domain.ServedRange(start=response.served_range.start, end=response.served_range.end),
                as_of=response.as_of,
            ),
            parts=parts,
            quota_key=request.quota_scope,
            feed=response.feed,
        )

    def __failed(
        self, request: ScheduledRequest, work: BarsWork, error: TraderJoeError, tally: CallTally
    ) -> AttemptResult:
        klass = error_class(error)
        return AttemptResult(
            trailer=DeliveryTrailer(
                outcome=DeliveryOutcome.FAILED, error_class=klass, detail=error.detail, vendor_stats=self.__stats(tally)
            ),
            quota_key=request.quota_scope,
            feed=work.feed,
            reset_at=error.reset_at if klass is DeliveryErrorClass.RATE_LIMITED else None,
        )

    @staticmethod
    def __stats(tally: CallTally) -> VendorCallStats:
        # The true counts of the attempt: the reader acquires, and counts, once per HTTP request (tj-7zjov1.56).
        return VendorCallStats(
            calls=tally.calls,
            pages=tally.pages,
            throttled_429=tally.throttled_429,
            retries=tally.retries,
            first_call_at=tally.first_call_at,
            last_call_at=tally.last_call_at,
        )
