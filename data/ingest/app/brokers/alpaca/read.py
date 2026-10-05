from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.models.bars import BarSet

from common.enums.data_select import AssetType, DataType
from common.errors.vocabulary import InvalidRequestError, Reason, TraderJoeError
from common.logging import get_logger
from common.worker_pool import SharedWorkerPool
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.broker_codes import AlpacaGranularity
from data.ingest.app.brokers.alpaca.classify import VENDOR_FAILURES, classify_vendor_error
from data.ingest.app.brokers.broker_errors import MissingCredentialsError
from data.ingest.app.brokers.interface import (
    Bar,
    BarsFailure,
    BarsQuery,
    BarsResponse,
    BrokerUnsupportedError,
    ServedRange,
)


log = get_logger(__name__)

# Alpaca serves one consolidated US line per symbol, quoted in dollars, and cannot select a
# listing exchange.
ALPACA_CURRENCY = 'USD'


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AlpacaRead:
    """The Alpaca implementation of BrokerRead.

    Conforms structurally and inherits nothing (decision tj-j4wknb U7). The vendor machinery --
    single-flight, the rate budget, the request key, the lazy client -- stays in broker_api and
    is reached through it, so nothing vendor-shaped leaks into the interface.

    Args:
        client (StockHistoricalDataClient | None): Client to call, or None to use this process's
            own, resolved on each call (broker_api.get_client()) so it stays lazy.
        executor_provider (Callable[[], ThreadPoolExecutor] | None): Yields the pool the blocking
            SDK calls run on, or None for SharedWorkerPool.get_instance. Resolved at call time,
            because the pool exists only once the app's lifespan has started it.
        clock (Callable[[], datetime]): Yields now, timezone aware. The one clock this reader
            reads: the future-range pre-check, the as_of a fetch is stamped with, the end its
            served_range is clamped to, and the reset a rate-limited answer is measured from.
            Injectable so tests need not sleep or patch datetime.
    """

    def __init__(
        self,
        client: StockHistoricalDataClient | None = None,
        executor_provider: Callable[[], ThreadPoolExecutor] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.__client = client
        self.__executor_provider = executor_provider if executor_provider is not None else SharedWorkerPool.get_instance
        self.__clock = clock

    async def get_bars(self, query: BarsQuery) -> BarsResponse | BarsFailure:
        """Fetch bars from Alpaca.

        Returns a BarsFailure, and never raises, for every failure that is expected (ADR tj-fa1rpu
        D6): a refusal found before any vendor call, a missing credential, a rate budget that
        cannot admit the call before query.deadline, and any failure the vendor's client raises
        that classify_vendor_error can name. A 200 is always SERVED, including one with no bars:
        an absent symbol and an empty list are both an empty window, and served_range says what
        the vendor answered for. A bug still raises, and nothing here converts one.

        Args:
            query (BarsQuery): What to fetch.

        Returns:
            BarsResponse | BarsFailure: The feed this deployment is entitled to, the converted
                bars and the range they answer for, or the failure: UNSUPPORTED_ASSET_TYPE or
                UNSUPPORTED_INSTRUMENT (BrokerUnsupportedError) for an instrument or adjustment
                Alpaca cannot serve, FEED_NOT_AVAILABLE for a feed this deployment cannot,
                RANGE_IN_FUTURE for a start at or after the clock, VENDOR_AUTH for a missing
                credential or a 401 or 403, VENDOR_INVALID_REQUEST for a 400, VENDOR_REJECTED for
                another 4xx, VENDOR_RATE_LIMITED for a 429, VENDOR_UNAVAILABLE for a 5xx or a
                connection error or timeout, and RATE_BUDGET for the rate budget's own refusal.

        Raises:
            ValueError: If a bar within [start, end) carries a non-whole trade_count (a fraction,
                NaN or infinity), which is vendor corruption. It is never truncated.
            AttributeError: If a 200 answers with a JSON null 'bars'. Deliberately not guarded
                (ADR tj-fa1rpu Q-EMPTY addendum item 4): it is loud and never a false empty.
            alpaca.common.exceptions.APIError: If the vendor answers an error with no HTTP status,
                which no classification can name.
        """
        instrument = query.instrument
        if instrument.asset_type is not AssetType.STOCK:
            return self.__refuse(
                Reason.UNSUPPORTED_ASSET_TYPE, f'Alpaca serves stocks only: asset_type={instrument.asset_type!r}'
            )
        if query.adjustment != broker_api.ALPACA_ADJUSTMENT:
            # Not an instrument, but the one of the three reasons a BrokerUnsupportedError may carry
            # that fits: the broker cannot serve the bars as specified. Unreachable today, since no
            # caller sets an adjustment.
            return self.__refuse(
                Reason.UNSUPPORTED_INSTRUMENT,
                f'Alpaca serves {broker_api.ALPACA_ADJUSTMENT} bars only: adjustment={query.adjustment!r}',
            )
        if instrument.exchange is not None:
            return self.__refuse(
                Reason.UNSUPPORTED_INSTRUMENT,
                f'Alpaca cannot select a listing exchange: exchange={instrument.exchange!r}',
            )
        if instrument.currency not in (None, ALPACA_CURRENCY):
            return self.__refuse(
                Reason.UNSUPPORTED_INSTRUMENT, f'Alpaca serves {ALPACA_CURRENCY} only: currency={instrument.currency!r}'
            )

        # Resolved once for the whole fetch and threaded through explicitly (fetch_data_type's
        # key, the vendor params below, and the response returned at the end) rather than
        # re-read from sip_enabled() at each site (tj-vhboky.9). One deployment has exactly one
        # active tape at a time (tj-rh4b7f), so it can serve a named feed only if that is the one.
        feed = broker_api.resolve_feed()
        if query.feed is not None and query.feed != feed:
            return self.__refuse(
                Reason.FEED_NOT_AVAILABLE,
                f'This deployment cannot serve the requested feed: feed={query.feed.value}',
                metadata={'feed': query.feed.value},
            )

        # A historical fetch: a range starting at or after now is a caller's mistake (a swapped date,
        # a time zone slip), refused here rather than asked of the vendor and served empty. A range
        # that starts in the past and ends in the future is fetched, and served up to as_of.
        now = self.__clock()
        if query.start >= now:
            return BarsFailure(
                InvalidRequestError(
                    Reason.RANGE_IN_FUTURE,
                    f'The range starts at or after now: start={query.start.isoformat()}, now={now.isoformat()}',
                    metadata={'range_start': query.start.isoformat()},
                )
            )

        # Resolved once, before any task is spawned, so a missing credential fails here with a
        # named variable rather than inside a gathered task.
        try:
            client = self.__client if self.__client is not None else broker_api.get_client()
        except MissingCredentialsError as e:
            return BarsFailure(e)
        params = {
            'symbol_or_symbols': instrument.symbol,
            'timeframe': AlpacaGranularity.from_granularity(query.granularity).broker_code,
            'start': query.start.isoformat(),
            'end': query.end.isoformat() if query.end is not None else None,
            # Sent to the vendor so the entitled tape is the one actually requested, rather than
            # whatever Alpaca defaults to in feed's absence.
            'feed': DataFeed(feed.value.lower()),
        }
        log.debug(f'params: {params}')

        # THE FEED RETURNED IS THE REQUESTED ONE, NOT A CONFIRMED SERVED ONE (tj-vhboky.9 part B,
        # tj-rh4b7f): Alpaca's bars response has no feed field to read an answered tape back off.
        #
        # The ONLY exceptions converted are the ones named here, never a broad except (ADR tj-fa1rpu D6):
        # a typed error (the rate budget's RATE_BUDGET) is already the answer, and the vendor client's
        # own failures are classified by status. Anything else, an AttributeError from a null 'bars'
        # included, is a bug and propagates.
        try:
            bar_set = await broker_api.fetch_data_type(
                self.__executor_provider(), query, DataType.MARKET_ACTIVITY, False, params, feed, client
            )
        except TraderJoeError as e:
            return BarsFailure(e)
        except VENDOR_FAILURES as e:
            error = classify_vendor_error(e, rate_budget=broker_api.get_rate_budget(), clock=self.__clock)
            if error is None:
                raise
            return BarsFailure(error)

        # A 200 answered, so this range is SERVED, whether or not it holds a bar. The vendor cannot have
        # answered for time after it answered, so an open end, or one past now, is clamped to as_of.
        as_of = self.__clock().astimezone(UTC)
        served_end = as_of if query.end is None else min(query.end, as_of)
        return BarsResponse(
            feed=feed,
            bars=self.__iterate(self.__convert(bar_set, instrument.symbol, query.end)),
            served_range=ServedRange(query.start, served_end),
            as_of=as_of,
        )

    @staticmethod
    def __refuse(reason: Reason, detail: str, metadata: dict[str, str] | None = None) -> BarsFailure:
        """Build the failure for a request this broker will not serve, found before any vendor call."""
        return BarsFailure(BrokerUnsupportedError(reason, detail, metadata=metadata))

    @staticmethod
    def __convert(bar_set: BarSet, symbol: str, end: datetime | None) -> list[Bar]:
        """Convert the vendor's bar set to interface bars, for one symbol, within [start, end)."""
        if symbol not in bar_set.data:
            log.warning(f'Symbol {symbol} not found in bar set')
            return []
        vendor_bars = bar_set[symbol] if isinstance(bar_set[symbol], list) else [bar_set[symbol]]
        log.debug(f'bars: {len(vendor_bars)}')
        # split_factor and dividends_factor are gone (tj-vhboky.1 section 6): bars are stored
        # RAW, never corrected, and adjustment is applied on read.
        return [
            Bar(
                timestamp=bar.timestamp,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                trade_count=AlpacaRead.__to_trade_count(bar.trade_count, symbol, bar.timestamp),
                vwap=bar.vwap,
            )
            for bar in vendor_bars
            # Half-open [start, end) at the interface for every broker (tj-irhy0a.15): Alpaca's
            # end is inclusive, so a bar stamped exactly at end is truncated here.
            if end is None or bar.timestamp < end
        ]

    @staticmethod
    def __to_trade_count(value: float | None, symbol: str, timestamp: datetime) -> int | None:
        """Give Bar.trade_count as the int the interface declares.

        alpaca-py types trade_count as a float (772630.0). None stays None, a whole value becomes
        an int, and a fractional one is vendor corruption: it fails the fetch rather than being
        truncated (tj-j4wknb addendum 2 B).

        Raises:
            ValueError: If the value is not whole (a fraction, NaN or infinity).
        """
        if value is None:
            return None
        if not float(value).is_integer():
            raise ValueError(
                f'Alpaca returned a non-whole trade_count {value!r} for {symbol} at {timestamp.isoformat()}'
            )
        return int(value)

    @staticmethod
    async def __iterate(bars: list[Bar]) -> AsyncIterator[Bar]:
        for bar in bars:
            yield bar
