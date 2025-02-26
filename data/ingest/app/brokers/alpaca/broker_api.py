import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Tuple, Type

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.models.bars import Bar, BarSet
from alpaca.data.models.quotes import Quote
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestBarRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
    StockQuotesRequest,
    StockTradesRequest
)

from common.data_lifecyle import expiry_inc
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Granularity
from common.environment import get_env_var
from common.logging import get_logger
from data.ingest.app.brokers.alpaca.broker_codes import AlpacaGranularity
from schemas.data_ingest.get_dataset_request import StockDatasetRequest
from schemas.data_store.stock.market_activity_data import (
    StockDataMarketActivityCreate, BatchStockDataMarketActivityCreate, StockDataMarketActivityData
)

log = get_logger(__name__)

ALPACA_SIP_ENABLED = get_env_var('ALPACA_SIP_ENABLED')

# Configure Alpaca Client
API_KEY = get_env_var('ALPACA_API_KEY')
API_SECRET = get_env_var('ALPACA_API_SECRET')
# TODO this should probably be wrapped to utilize dependency injection base on source selection
__CLIENT = StockHistoricalDataClient(API_KEY, API_SECRET)


def convert_bar_to_schema(data: Bar) -> StockDataMarketActivityCreate:
    return StockDataMarketActivityData(
        open=data.open,
        high=data.high,
        low=data.low,
        close=data.close,
        volume=data.volume,
        trade_count=data.trade_count,

        split_factor=1,
        dividends_factor=1
    )


def convert_bars_to_batch_schema(
    batch_response: BatchStockDataMarketActivityCreate, request: StockDatasetRequest, stock_bars: BarSet
) -> List[StockDataMarketActivityCreate]:
    stock_symbol = request.asset_symbol
    if stock_symbol not in stock_bars.data:
        log.warning(f"Symbol {stock_symbol} not found in bar set")
        return []

    latest_expiry = request.expiry
    bars: List[Bar] = \
        stock_bars[stock_symbol] if isinstance(stock_bars[stock_symbol], list) else [stock_bars[stock_symbol]]

    log.debug(f"bars: {len(bars)}")
    for bar in bars:
        batch_response.append_data(
            DataType.MARKET_ACTIVITY,
            convert_bar_to_schema(bar),
            bar.timestamp,
            latest_expiry
        )
        latest_expiry = expiry_inc(latest_expiry, request.expiry_type, request.granularity)


def create_stock_quote(data: Quote, symbol: str, granularity: Granularity, source: DataSource) -> None:
    return None


def match_client_request(asset_type: AssetType, data_type: DataType, request_latest: bool) -> Tuple[Callable, Type]:
    match (asset_type, data_type, request_latest):
        ### STOCK ###
        ## MARKET ACTIVITY ##
        case (AssetType.STOCK, DataType.MARKET_ACTIVITY, False):
            return __CLIENT.get_stock_bars, StockBarsRequest
        case (AssetType.STOCK, DataType.MARKET_ACTIVITY, True):
            return __CLIENT.get_stock_latest_bar, StockLatestBarRequest
        ## QUOTE ##
        case (AssetType.STOCK, DataType.QUOTE, False):
            return __CLIENT.get_stock_quotes, StockQuotesRequest
        case (AssetType.STOCK, DataType.QUOTE, True):
            return __CLIENT.get_stock_latest_quote, StockLatestQuoteRequest
        ## TRADE ##
        case (AssetType.STOCK, DataType.TRADE, False):
            return __CLIENT.get_stock_trades, StockTradesRequest
        case (AssetType.STOCK, DataType.TRADE, True):
            return __CLIENT.get_stock_latest_trade, StockLatestTradeRequest
        ### CRYPTO ###
        ### OPTION ###
        case (_, _):
            raise NotImplementedError(f"{asset_type} - {data_type} not implemented")


async def get_market_stock_data(
    executor: ThreadPoolExecutor, request: StockDatasetRequest
) -> BatchStockDataMarketActivityCreate:
    granularity = AlpacaGranularity.from_granularity(request.granularity).broker_code
    params = {
        "symbol_or_symbols": request.asset_symbol,
        "timeframe": granularity,
        "start": request.start.isoformat(),
        "end": request.end.isoformat() if request.end is not None else None,
    }
    log.debug(f"params: {params}")

    tasks = []
    loop = asyncio.get_running_loop()
    results = None
    response_map = {}
    latest = False
    if "start" not in params and "end" not in params:
        latest = True

    for data_type in request.data_types:
        if data_type in response_map:
            log.warning(f"Duplicate data type found: {data_type}")
            continue

        client_request, client_request_type = match_client_request(AssetType.STOCK, data_type, latest)
        task = loop.run_in_executor(executor, client_request, client_request_type(**params))
        tasks.append(task)
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
        dataset={}
    )
    if DataType.MARKET_ACTIVITY in response_map:
        convert_bars_to_batch_schema(
            batch_response, request, results[response_map[DataType.MARKET_ACTIVITY]]
        )
        log.debug(f"dataset bars: {len(batch_response.dataset[DataType.MARKET_ACTIVITY])}")
    if DataType.QUOTE in response_map:
        raise NotImplementedError("Quotes not implemented")
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
        raise NotImplementedError("Trades not implemented")
        # stock_trades = results[response_map[DataType.TRADE]]
        # log.debug(f" trade results: {len(stock_trades[symbol])}")
        # log.debug(f"    last: {stock_trades}")

    return batch_response
