import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List
from uuid import UUID

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

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Granularity
from common.environment import get_env_var
from common.kafka.topics import StaticTopic, TopicTyping
from common.logging import get_logger
from data.ingest.app.brokers.alpaca.broker_codes import AlpacaGranularity
from schemas.data_store.asset_market_activity_data import AssetMarketActivityDataCreate
from schemas.data_ingest.get_dataset_request import StockDatasetRequest

log = get_logger(__name__)

ALPACA_SIP_ENABLED = get_env_var('ALPACA_SIP_ENABLED')

# Configure Alpaca Client
API_KEY = get_env_var('ALPACA_API_KEY')
API_SECRET = get_env_var('ALPACA_API_SECRET')
__CLIENT = StockHistoricalDataClient(API_KEY, API_SECRET)


def convert_bar_to_schema(
    data: Bar, dataset_id: UUID, symbol: str, source: DataSource, asset_type: AssetType, granularity: Granularity
) -> AssetMarketActivityDataCreate:
    return AssetMarketActivityDataCreate(
        dataset_id=dataset_id,

        asset_type=asset_type,
        source=source,
        symbol=symbol,

        timestamp=data.timestamp,
        granularity=granularity,

        open=data.open,
        high=data.high,
        low=data.low,
        close=data.close,
        volume=data.volume,
        trade_count=data.trade_count,

        split_factor=1,
        dividends_factor=1
    )


def create_stock_quote(data: Quote, symbol: str, granularity: Granularity, source: DataSource) -> None:
    return None


async def get_market_stock_data(
    executor: ThreadPoolExecutor, request: StockDatasetRequest
) -> Dict[TopicTyping, List[str]]:
    granularity = AlpacaGranularity.from_granularity(request.granularity).broker_code
    params = {
        "symbol_or_symbols": request.symbol,
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

    if DataType.MARKET_ACTIVITY in request.data_type:
        if latest is True:
            task = loop.run_in_executor(executor, __CLIENT.get_stock_latest_bar, StockLatestBarRequest(**params))
        else:
            task = loop.run_in_executor(executor, __CLIENT.get_stock_bars, StockBarsRequest(**params))
        tasks.append(task)
        response_map[DataType.MARKET_ACTIVITY] = len(tasks) - 1

    if DataType.QUOTE in request.data_type:
        if latest is True:
            task = loop.run_in_executor(executor, __CLIENT.get_stock_latest_quote, StockLatestQuoteRequest(**params))
        else:
            task = loop.run_in_executor(executor, __CLIENT.get_stock_quotes, StockQuotesRequest(**params))
        tasks.append(task)
        response_map[DataType.QUOTE] = len(tasks) - 1

    if DataType.TRADE in request.data_type:
        if latest is True:
            task = loop.run_in_executor(executor, __CLIENT.get_stock_latest_trade, StockLatestTradeRequest(**params))
        else:
            task = loop.run_in_executor(executor, __CLIENT.get_stock_trades, StockTradesRequest(**params))
        tasks.append(task)
        response_map[DataType.TRADE] = len(tasks) - 1

    try:
        results = await asyncio.gather(*tasks)
    except Exception as e:
        log.error(e)
        # TODO better error handling
        return {}

    symbol = request.symbol.upper()
    topic_map = {}
    if DataType.MARKET_ACTIVITY in response_map:
        stock_bars: BarSet = results[response_map[DataType.MARKET_ACTIVITY]]
        stock_bars

        if symbol in stock_bars.data:
            bars = stock_bars[symbol] if isinstance(stock_bars[symbol], list) else [stock_bars[symbol]]
            topic_map[StaticTopic.STOCK_MARKET_ACTIVITY] = [
                convert_bar_to_schema(
                    bar,
                    request.dataset_id,
                    symbol,
                    request.source,
                    AssetType.STOCK,
                    request.granularity
                ).model_dump_json() for bar in bars
            ]
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

    return topic_map
