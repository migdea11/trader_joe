from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.models.bars import BarSet

from common.enums.data_select import AssetType, DataType
from common.logging import get_logger
from common.worker_pool import SharedWorkerPool
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.broker_codes import AlpacaGranularity
from data.ingest.app.brokers.interface import Bar, BarsQuery, BarsResponse, BrokerUnsupportedError


log = get_logger(__name__)

# Alpaca serves one consolidated US line per symbol, quoted in dollars, and cannot select a
# listing exchange.
ALPACA_CURRENCY = 'USD'


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
    """

    def __init__(
        self,
        client: StockHistoricalDataClient | None = None,
        executor_provider: Callable[[], ThreadPoolExecutor] | None = None,
    ) -> None:
        self.__client = client
        self.__executor_provider = executor_provider if executor_provider is not None else SharedWorkerPool.get_instance

    async def get_bars(self, query: BarsQuery) -> BarsResponse:
        """Fetch bars from Alpaca.

        Args:
            query (BarsQuery): What to fetch.

        Returns:
            BarsResponse: The feed this deployment is entitled to and the converted bars.

        Raises:
            BrokerUnsupportedError: If the instrument is not a stock, names a currency other than
                USD or any exchange, or the query asks for an adjustment other than 'raw'.
            MissingCredentialsError: If no client is injected and none can be built.
            alpaca.common.exceptions.APIError: If the vendor answers an error status on any page
                of the fetch, once alpaca-py's own retries of 429 and 504 are spent. Raised from
                this call, so nothing of the fetch is yielded, not even a page already answered.
            ValueError: If a bar within [start, end) carries a non-whole trade_count (a fraction,
                NaN or infinity), which is vendor corruption. It is never truncated.
        """
        instrument = query.instrument
        if instrument.asset_type is not AssetType.STOCK:
            raise BrokerUnsupportedError(f'Alpaca serves stocks only: asset_type={instrument.asset_type!r}')
        if query.adjustment != broker_api.ALPACA_ADJUSTMENT:
            raise BrokerUnsupportedError(
                f'Alpaca serves {broker_api.ALPACA_ADJUSTMENT} bars only: adjustment={query.adjustment!r}'
            )
        if instrument.exchange is not None:
            raise BrokerUnsupportedError(f'Alpaca cannot select a listing exchange: exchange={instrument.exchange!r}')
        if instrument.currency not in (None, ALPACA_CURRENCY):
            raise BrokerUnsupportedError(f'Alpaca serves {ALPACA_CURRENCY} only: currency={instrument.currency!r}')

        # Resolved once, before any task is spawned, so a missing credential fails here with a
        # named variable rather than inside a gathered task.
        client = self.__client if self.__client is not None else broker_api.get_client()
        # Resolved once for the whole fetch and threaded through explicitly (fetch_data_type's
        # key, the vendor params below, and the response returned at the end) rather than
        # re-read from sip_enabled() at each site (tj-vhboky.9). One deployment has exactly one
        # active tape at a time (tj-rh4b7f).
        feed = broker_api.resolve_feed()
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
        bar_set = await broker_api.fetch_data_type(
            self.__executor_provider(), query, DataType.MARKET_ACTIVITY, False, params, feed, client
        )
        return BarsResponse(feed=feed, bars=self.__iterate(self.__convert(bar_set, instrument.symbol, query.end)))

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
