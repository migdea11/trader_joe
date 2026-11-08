from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from http import HTTPStatus
from urllib.parse import quote

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.models.bars import BarSet
from alpaca.trading.client import TradingClient

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import Feed
from common.errors.vocabulary import InvalidRequestError, Reason, TraderJoeError
from common.logging import get_logger
from common.worker_pool import SharedWorkerPool
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.assets import parse_asset, parse_asset_list
from data.ingest.app.brokers.alpaca.broker_codes import AlpacaGranularity
from data.ingest.app.brokers.alpaca.classify import VENDOR_FAILURES, classify_vendor_error
from data.ingest.app.brokers.broker_errors import MalformedCredentialsError, MissingCredentialsError
from data.ingest.app.brokers.interface import (
    AssetListFailure,
    AssetListQuery,
    AssetListResponse,
    AssetLookupFailure,
    AssetLookupQuery,
    AssetNotFound,
    AssetResponse,
    Bar,
    BarsFailure,
    BarsQuery,
    BarsResponse,
    BrokerUnsupportedError,
    CallTally,
    CorporateActionsFailure,
    CorporateActionsQuery,
    CorporateActionsResponse,
    ServedRange,
)
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler import Scheduler


log = get_logger(__name__)

# Alpaca serves one consolidated US line per symbol, quoted in dollars, and cannot select a
# listing exchange.
ALPACA_CURRENCY = 'USD'

# Sent on every asset-list call: without it the vendor adds its crypto rows, which are not stock listings.
ASSET_CLASS_US_EQUITY = 'us_equity'


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_day_rounded_up(moment: datetime) -> date:
    """Give the UTC day of moment, or the next day unless moment is exactly midnight UTC."""
    utc = moment.astimezone(UTC)
    day = utc.date()
    return day if utc.time() == time.min else day + timedelta(days=1)


class AlpacaRead:
    """The Alpaca implementation of BrokerRead.

    Conforms structurally and inherits nothing (decision tj-j4wknb U7). The vendor machinery --
    single-flight, the quota scope, the request key, the lazy client -- stays in broker_api and
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
        scheduler (Scheduler): The Alpaca handle's scheduler. Its quota limits pace every vendor call this
            reader makes, the FetchDataset path's included; nothing else does.
        scope (str): The quota scope those calls are charged to.
        trading_client (TradingClient | None): Client for the asset reads, or None to use this process's own
            (broker_api.get_trading_client()), resolved on each call so it stays lazy.
        corporate_actions_client (CorporateActionsClient | None): Client for the corporate-actions read, or None to use
            this process's own (broker_api.get_corporate_actions_client()), resolved on each call so it stays lazy.
        feed (Feed): The tape the Alpaca handle resolved for its account (tj-7zjov1.91). This reader reads no
            environment for it: it serves this feed and refuses a named one that differs (FEED_NOT_AVAILABLE).
        trading_scope (str | None): The trading-API quota scope the asset reads are charged to, apart from `scope`
            so they never count against the data API. None for a reader built without asset reads; calling one then
            is a bug and raises.
    """

    def __init__(
        self,
        client: StockHistoricalDataClient | None = None,
        executor_provider: Callable[[], ThreadPoolExecutor] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        *,
        scheduler: Scheduler,
        scope: str,
        trading_client: TradingClient | None = None,
        corporate_actions_client: CorporateActionsClient | None = None,
        trading_scope: str | None = None,
        feed: Feed,
    ) -> None:
        self.__feed = feed
        self.__scheduler = scheduler
        self.__scope = scope
        self.__trading_client = trading_client
        self.__corporate_actions_client = corporate_actions_client
        self.__trading_scope = trading_scope
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
            BarsResponse | BarsFailure: The feed of the account the handle resolved, the converted
                bars and the range they answer for, or the failure: UNSUPPORTED_ASSET_TYPE or
                UNSUPPORTED_INSTRUMENT (BrokerUnsupportedError) for an instrument or adjustment
                Alpaca cannot serve, FEED_NOT_AVAILABLE for a feed this account cannot,
                RANGE_IN_FUTURE for a start at or after the clock, VENDOR_AUTH for a missing
                or malformed credential or a 401 or 403, VENDOR_INVALID_REQUEST for a 400, VENDOR_REJECTED for
                another 4xx, VENDOR_RATE_LIMITED for a 429, VENDOR_UNAVAILABLE for a 5xx, a
                connection error or timeout, or a connection cut while the body was read, and
                RATE_BUDGET for the rate budget's own refusal.

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

        # The handle's feed, resolved once for the account (tj-7zjov1.97): this reader decides nothing. It is threaded
        # through explicitly (fetch_data_type's key, the vendor params below, and the response returned at the end).
        feed = self.__feed
        if query.feed is not None and query.feed != feed:
            return self.__refuse(
                Reason.FEED_NOT_AVAILABLE,
                f'This account cannot serve the requested feed: feed={query.feed.value}',
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

        # Resolved once, before any task is spawned, so a missing or malformed credential fails here with a
        # named variable rather than inside a gathered task.
        try:
            client = self.__client if self.__client is not None else broker_api.get_client()
        except (MissingCredentialsError, MalformedCredentialsError) as e:
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
                self.__executor_provider(),
                query,
                DataType.MARKET_ACTIVITY,
                False,
                params,
                feed,
                client,
                scheduler=self.__scheduler,
                scope=self.__scope,
            )
        except TraderJoeError as e:
            return BarsFailure(e)
        except VENDOR_FAILURES as e:
            error = classify_vendor_error(
                e,
                rate_budget=self.__scheduler.window(self.__scope),
                clock=self.__clock,
                last_reset=broker_api.last_reset(self.__scope, self.__clock()),
            )
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

    async def get_assets(self, query: AssetListQuery) -> AssetListResponse | AssetListFailure:
        """Fetch one of Alpaca's asset lists, whole, in one vendor call.

        Sends asset_class=us_equity explicitly (without it the vendor mixes crypto rows into the list) and the one
        status the query names, never both and never absent. The body is read as served, not through the SDK model
        (see assets.py). Paced on the trading-API scope; the data scope is not touched.

        Args:
            query (AssetListQuery): Which list to fetch.

        Returns:
            AssetListResponse | AssetListFailure: Every asset served, or BrokerUnsupportedError
                (UNSUPPORTED_ASSET_TYPE) for a kind that is not a stock, VENDOR_AUTH for a missing or malformed
                credential or an unset trading host or a 401 or 403, VENDOR_RATE_LIMITED with its reset_at for a 429,
                VENDOR_UNAVAILABLE for a 5xx, a connection error or a timeout, another vendor status as the bars
                read classifies it, and RATE_BUDGET for the rate budget's own refusal.

        Raises:
            ValueError: If the vendor's body is not a JSON array of asset records. Vendor corruption, loud.
            RuntimeError: If this reader was built without a trading scope.
        """
        if query.asset_type is not AssetType.STOCK:
            return AssetListFailure(
                BrokerUnsupportedError(
                    Reason.UNSUPPORTED_ASSET_TYPE, f'Alpaca lists stocks only: asset_type={query.asset_type!r}'
                )
            )
        try:
            page = await self.__read_raw(
                broker_api.ASSETS_PATH,
                {'status': query.status.value, 'asset_class': ASSET_CLASS_US_EQUITY},
                query.priority,
                query.deadline,
                query.tally,
            )
        except TraderJoeError as e:
            return AssetListFailure(e)
        except VENDOR_FAILURES as e:
            return AssetListFailure(self.__classify(e, self.__trading_scope or self.__scope))
        as_of = self.__clock().astimezone(UTC)
        return AssetListResponse(assets=parse_asset_list(page.content), as_of=as_of)

    async def get_asset(self, query: AssetLookupQuery) -> AssetResponse | AssetNotFound | AssetLookupFailure:
        """Look up Alpaca's record for one symbol, in one vendor call.

        A vendor 404 is AssetNotFound, an answer. Every other expected failure is an AssetLookupFailure, a rate limit
        carrying its reset_at. Paced on the trading-API scope. Nothing about the record is judged: an asset the
        vendor reports inactive or untradable is returned like any other.

        Args:
            query (AssetLookupQuery): The symbol to look up.

        Returns:
            AssetResponse | AssetNotFound | AssetLookupFailure: The record, the not-found answer, or the failure,
                classified as get_assets does; an empty symbol is INVALID_REQUEST before any vendor call.

        Raises:
            ValueError: If the vendor's body is not one asset record. Vendor corruption, loud.
            RuntimeError: If this reader was built without a trading scope.
        """
        if not query.symbol.strip():
            return AssetLookupFailure(InvalidRequestError(Reason.INVALID_REQUEST, 'An asset lookup needs a symbol'))
        try:
            page = await self.__read_raw(
                f'{broker_api.ASSETS_PATH}/{quote(query.symbol, safe="")}', None, query.priority, query.deadline, None
            )
        except TraderJoeError as e:
            return AssetLookupFailure(e)
        except VENDOR_FAILURES as e:
            if isinstance(e, APIError) and e.status_code == HTTPStatus.NOT_FOUND:
                return AssetNotFound(symbol=query.symbol, as_of=self.__clock().astimezone(UTC))
            return AssetLookupFailure(self.__classify(e, self.__trading_scope or self.__scope))
        as_of = self.__clock().astimezone(UTC)
        return AssetResponse(asset=parse_asset(page.content), as_of=as_of)

    async def get_corporate_actions(
        self, query: CorporateActionsQuery
    ) -> CorporateActionsResponse | CorporateActionsFailure:
        """Fetch Alpaca's corporate actions for one symbol over a window, every page, nothing filtered.

        Sends the symbol and the window's days (sorted ascending, no type filter: the store decides which types it
        wants) and follows the page token to the end, one scheduler token per page on the DATA scope. The vendor
        matches the symbol on either side of an action. The window is sent as UTC days: the start's own day (rounded
        down) and the end's day rounded up, the next day unless the end is exactly midnight UTC. The vendor's
        treatment of its end day is unconfirmed, so the read is a superset whether it counts that day inclusively or
        not: a record beyond the window may be returned (the store keeps every record and recognises repeats by
        vendor id and payload), and none inside it is missed. Nothing is filtered afterwards. The bodies are read as
        served, not through the SDK model (see corporate_actions.py). A failure on any page fails the whole read.

        Args:
            query (CorporateActionsQuery): The symbol and the window.

        Returns:
            CorporateActionsResponse | CorporateActionsFailure: Every action served, or the failure: INVALID_REQUEST
                for an empty symbol or a window that ends at or before its start (before any vendor call), VENDOR_AUTH
                for a missing or malformed credential or a 401 or 403 (the entitlement too), VENDOR_RATE_LIMITED with
                its reset_at for a 429, VENDOR_UNAVAILABLE for a 5xx, a connection error or a timeout, another vendor
                status as the bars read classifies it, and RATE_BUDGET for the rate budget's own refusal.

        Raises:
            ValueError: If a page is not a corporate-actions page, or the vendor repeats a page token or never ends.
                Vendor corruption, loud.
        """
        if not query.symbol.strip():
            return CorporateActionsFailure(
                InvalidRequestError(Reason.INVALID_REQUEST, 'A corporate-actions read needs a symbol')
            )
        if query.end <= query.start:
            return CorporateActionsFailure(
                InvalidRequestError(Reason.INVALID_REQUEST, 'A corporate-actions window must end after it starts')
            )
        try:
            client = (
                self.__corporate_actions_client
                if self.__corporate_actions_client is not None
                else broker_api.get_corporate_actions_client()
            )
        except (MissingCredentialsError, MalformedCredentialsError) as e:
            return CorporateActionsFailure(e)
        params = {
            'symbols': query.symbol,
            'start': query.start.astimezone(UTC).date().isoformat(),
            'end': _utc_day_rounded_up(query.end).isoformat(),
            'sort': 'asc',
        }
        try:
            actions = await broker_api.read_corporate_action_pages(
                self.__executor_provider(),
                client,
                params,
                scheduler=self.__scheduler,
                scope=self.__scope,
                priority=query.priority,
                deadline=query.deadline,
                tally=query.tally,
            )
        except TraderJoeError as e:
            return CorporateActionsFailure(e)
        except VENDOR_FAILURES as e:
            return CorporateActionsFailure(self.__classify(e, self.__scope))
        return CorporateActionsResponse(actions=actions, as_of=self.__clock().astimezone(UTC))

    async def __read_raw(
        self,
        path: str,
        params: dict | None,
        priority: RequestPriority,
        deadline: datetime | None,
        tally: CallTally | None,
    ) -> broker_api.RawPage:
        """Make one paced trading-API read. Credential and host problems raise the typed errors the callers return."""
        if self.__trading_scope is None:
            raise RuntimeError('AlpacaRead was built without a trading scope, so it cannot read assets')
        client = self.__trading_client if self.__trading_client is not None else broker_api.get_trading_client()
        return await broker_api.read_raw(
            self.__executor_provider(),
            client,
            path,
            params,
            scheduler=self.__scheduler,
            scope=self.__trading_scope,
            priority=priority,
            deadline=deadline,
            tally=tally,
        )

    def __classify(self, error: BaseException, scope: str) -> TraderJoeError:
        """Classify a failure of a raw read by the same table as the bars read, on the scope the read was charged to."""
        classified = classify_vendor_error(
            error,
            rate_budget=self.__scheduler.window(scope),
            clock=self.__clock,
            last_reset=broker_api.last_reset(scope, self.__clock()),
        )
        if classified is None:
            raise error
        return classified

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
