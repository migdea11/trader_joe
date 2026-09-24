import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.models.bars import Bar, BarSet
from alpaca.data.models.quotes import Quote
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestBarRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
    StockQuotesRequest,
    StockTradesRequest,
)

from common.data_lifecyle import expiry_inc
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Granularity
from common.environment import get_env_var
from common.logging import get_logger
from data.ingest.app.brokers.alpaca.broker_codes import AlpacaGranularity
from data.ingest.app.brokers.broker_errors import MissingCredentialsError
from data.ingest.app.brokers.rate_budget import RateBudget, priority_for_update_type
from data.ingest.app.brokers.request_key import VendorRequestKey
from data.ingest.app.brokers.single_flight import SingleFlight
from schemas.data_ingest.get_dataset_request import StockDatasetRequest
from schemas.data_store.stock.market_activity_data import (
    BatchStockDataMarketActivityCreate,
    StockDataMarketActivityCreate,
    StockDataMarketActivityData,
)


log = get_logger(__name__)

# The bars request never sets an adjustment, so every bar we hold is the vendor's raw one.
# Named here because it is part of the request key: a raw bar is not a split-adjusted one.
ALPACA_ADJUSTMENT = 'raw'

ALPACA_CREDENTIAL_VARS = ('ALPACA_API_KEY', 'ALPACA_API_SECRET')

__CLIENT: StockHistoricalDataClient | None = None
__SINGLE_FLIGHT = SingleFlight()
__RATE_BUDGET: RateBudget | None = None


def sip_enabled() -> bool:
    """Report whether this process is configured for the SIP feed rather than IEX.

    Read on each call rather than at import, for the same reason the client is built
    lazily. Cast, because an unset variable and the string 'false' are both truthy
    without it, and the feed is part of the request key rather than a log line.

    Returns:
        bool: True when ALPACA_SIP_ENABLED is set to a true value.
    """
    return get_env_var('ALPACA_SIP_ENABLED', default=False, cast_type=bool)


def get_client() -> StockHistoricalDataClient:
    """Get this process's Alpaca client, building it on first use.

    BUILT LAZILY, NOT AT IMPORT (tj-84jfb9). get_env_var() returns None for an unset
    variable, so building the client at module scope made alpaca-py raise at import time
    and left the whole ingest package unimportable without credentials -- no tests, no
    app start, and a secret rotation that needed a process restart rather than a fresh
    client. Callers that want to supply their own client inject it instead; see
    set_client() and the client argument on get_market_stock_data().

    Returns:
        StockHistoricalDataClient: Shared client for every Alpaca call this process makes.

    Raises:
        MissingCredentialsError: If either credential variable is unset or empty.
    """
    global __CLIENT
    if __CLIENT is None:
        missing = [name for name in ALPACA_CREDENTIAL_VARS if not get_env_var(name)]
        if missing:
            raise MissingCredentialsError(f'Alpaca credentials are not configured: {", ".join(missing)} unset')
        __CLIENT = StockHistoricalDataClient(*(get_env_var(name) for name in ALPACA_CREDENTIAL_VARS))
    return __CLIENT


def set_client(client: StockHistoricalDataClient | None) -> None:
    """Install a client for this process, or clear the one already built.

    The injection seam: tests pass a stub, and passing None drops the cached client so
    the next call rebuilds it -- which is what makes a rotated secret take effect without
    a restart.

    Args:
        client (StockHistoricalDataClient | None): Client to use, or None to clear.
    """
    global __CLIENT
    __CLIENT = client


def get_rate_budget() -> RateBudget:
    """Get this vendor's rate budget, building it on first use.

    Built lazily so that importing the module does not read the environment.

    Returns:
        RateBudget: Shared budget for every Alpaca call this process makes.
    """
    global __RATE_BUDGET
    if __RATE_BUDGET is None:
        __RATE_BUDGET = RateBudget.from_env(DataSource.ALPACA_API)
    return __RATE_BUDGET


def convert_bar_to_schema(data: Bar) -> StockDataMarketActivityCreate:
    return StockDataMarketActivityData(
        open=data.open,
        high=data.high,
        low=data.low,
        close=data.close,
        volume=data.volume,
        trade_count=data.trade_count,
        split_factor=1,
        dividends_factor=1,
    )


def convert_bars_to_batch_schema(
    batch_response: BatchStockDataMarketActivityCreate, request: StockDatasetRequest, stock_bars: BarSet
) -> list[StockDataMarketActivityCreate]:
    stock_symbol = request.asset_symbol
    if stock_symbol not in stock_bars.data:
        log.warning(f'Symbol {stock_symbol} not found in bar set')
        return []

    latest_expiry = request.expiry
    bars: list[Bar] = (
        stock_bars[stock_symbol] if isinstance(stock_bars[stock_symbol], list) else [stock_bars[stock_symbol]]
    )

    log.debug(f'bars: {len(bars)}')
    for bar in bars:
        batch_response.append_data(DataType.MARKET_ACTIVITY, convert_bar_to_schema(bar), bar.timestamp, latest_expiry)
        latest_expiry = expiry_inc(latest_expiry, request.expiry_type, request.granularity)


def create_stock_quote(data: Quote, symbol: str, granularity: Granularity, source: DataSource) -> None:
    return None


def match_client_request(
    client: StockHistoricalDataClient, asset_type: AssetType, data_type: DataType, request_latest: bool
) -> tuple[Callable, type]:
    match (asset_type, data_type, request_latest):
        ### STOCK ###
        ## MARKET ACTIVITY ##
        case (AssetType.STOCK, DataType.MARKET_ACTIVITY, False):
            return client.get_stock_bars, StockBarsRequest
        case (AssetType.STOCK, DataType.MARKET_ACTIVITY, True):
            return client.get_stock_latest_bar, StockLatestBarRequest
        ## QUOTE ##
        case (AssetType.STOCK, DataType.QUOTE, False):
            return client.get_stock_quotes, StockQuotesRequest
        case (AssetType.STOCK, DataType.QUOTE, True):
            return client.get_stock_latest_quote, StockLatestQuoteRequest
        ## TRADE ##
        case (AssetType.STOCK, DataType.TRADE, False):
            return client.get_stock_trades, StockTradesRequest
        case (AssetType.STOCK, DataType.TRADE, True):
            return client.get_stock_latest_trade, StockLatestTradeRequest
        ### CRYPTO ###
        ### OPTION ###
        case (_, _):
            raise NotImplementedError(f'{asset_type} - {data_type} not implemented')


async def fetch_data_type(
    executor: ThreadPoolExecutor,
    request: StockDatasetRequest,
    data_type: DataType,
    latest: bool,
    params: dict,
    client: StockHistoricalDataClient | None = None,
):
    """Fetch one data type from the vendor, collapsed and rate-limited.

    Concurrent callers asking for the same content share one vendor call; the extra ones
    attach to it and nothing is retained afterwards (tj-84ty47 section 6). The shared value
    is the vendor's own response, which every caller then converts with ITS OWN dataset_id
    and expiry -- collapsing the conversion too would hand one caller another's dataset_id.

    Args:
        executor (ThreadPoolExecutor): Pool the blocking SDK call runs on.
        request (StockDatasetRequest): Request being served.
        data_type (DataType): Data type to fetch.
        latest (bool): Whether the latest-value endpoint is being used.
        params (dict): Vendor request parameters.
        client (StockHistoricalDataClient | None): Client to call, or None for this
            process's own (see get_client()).

    Returns:
        The vendor's response, shared read-only with any caller that attached to this call.
    """
    client_request, client_request_type = match_client_request(
        client if client is not None else get_client(), AssetType.STOCK, data_type, latest
    )
    key = VendorRequestKey(
        broker=DataSource.ALPACA_API,
        feed='sip' if sip_enabled() else 'iex',
        asset_type=AssetType.STOCK,
        asset_symbol=request.asset_symbol,
        data_type=data_type,
        granularity=request.granularity,
        range_start=request.start,
        range_end=request.end,
        adjustment=ALPACA_ADJUSTMENT,
    )
    priority = priority_for_update_type(request.update_type)

    async def call_vendor():
        # One token per call that actually reaches the vendor. Calls collapsed by the guard
        # cost nothing, which is the point of doing this inside it rather than outside.
        await get_rate_budget().acquire(priority)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, client_request, client_request_type(**params))

    return await __SINGLE_FLIGHT.run(key, call_vendor)


async def get_market_stock_data(
    executor: ThreadPoolExecutor, request: StockDatasetRequest, client: StockHistoricalDataClient | None = None
) -> BatchStockDataMarketActivityCreate:
    """Fetch a stock dataset from Alpaca and convert it into the store's batch schema.

    Args:
        executor (ThreadPoolExecutor): Pool the blocking SDK calls run on.
        request (StockDatasetRequest): Request being served.
        client (StockHistoricalDataClient | None): Client to call, or None for this
            process's own. Injected by tests, and by a caller that selects its own
            adapter instance.

    Returns:
        BatchStockDataMarketActivityCreate: Converted dataset for the store.

    Raises:
        MissingCredentialsError: If no client is injected and none can be built.
    """
    # Resolved once, before any task is spawned, so a missing credential fails here with a
    # named variable rather than inside a gathered task whose errors are swallowed below.
    client = client if client is not None else get_client()
    granularity = AlpacaGranularity.from_granularity(request.granularity).broker_code
    params = {
        'symbol_or_symbols': request.asset_symbol,
        'timeframe': granularity,
        'start': request.start.isoformat(),
        'end': request.end.isoformat() if request.end is not None else None,
    }
    log.debug(f'params: {params}')

    tasks = []
    results = None
    response_map = {}
    latest = False
    if 'start' not in params and 'end' not in params:
        latest = True

    for data_type in request.data_types:
        if data_type in response_map:
            log.warning(f'Duplicate data type found: {data_type}')
            continue

        tasks.append(fetch_data_type(executor, request, data_type, latest, params, client))
        response_map[data_type] = len(tasks) - 1

    try:
        results = await asyncio.gather(*tasks)
    except Exception as e:
        log.error(e)
        # TODO better error handling
        return {}

    batch_response = BatchStockDataMarketActivityCreate(
        dataset_id=request.dataset_id,
        asset_symbol=request.asset_symbol,
        source=request.source,
        granularity=request.granularity,
        dataset={},
    )
    if DataType.MARKET_ACTIVITY in response_map:
        convert_bars_to_batch_schema(batch_response, request, results[response_map[DataType.MARKET_ACTIVITY]])
        log.debug(f'dataset bars: {len(batch_response.dataset[DataType.MARKET_ACTIVITY])}')
    if DataType.QUOTE in response_map:
        raise NotImplementedError('Quotes not implemented')
        # stock_quotes: QuoteSet = results[response_map[DataType.QUOTE]]

        # if symbol in stock_quotes.data:
        #     quotes = stock_quotes[symbol] if isinstance(stock_quotes[symbol], list) else [stock_quotes[symbol]]
        #     log.debug(quotes)
        #     topic_map[StaticTopic.STOCK_MARKET_QUOTE] = [
        #         create_stock_quote(
        #             quote, symbol, request.granularity, request.source
        #         ).model_dump_json() for quote in quotes
        #     ]
    if DataType.TRADE in response_map:
        raise NotImplementedError('Trades not implemented')
        # stock_trades = results[response_map[DataType.TRADE]]
        # log.debug(f" trade results: {len(stock_trades[symbol])}")
        # log.debug(f"    last: {stock_trades}")

    return batch_response
