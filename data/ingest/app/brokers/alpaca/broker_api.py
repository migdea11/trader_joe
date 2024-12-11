import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Dict, List

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.models.bars import Bar, BarSet
from alpaca.data.models.quotes import Quote, QuoteSet
from alpaca.data.requests import (
    StockBarsRequest, StockLatestBarRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest, StockQuotesRequest,
    StockTradesRequest
)
from alpaca.data.timeframe import TimeFrame

from common.enums.data_select import DataType
from common.enums.data_stock import DataSource, Granularity
from common.kafka.topics import StaticTopic
from common.logging import get_logger
from schemas.stock_market_activity_data import StockMarketActivityData
from schemas.store_broker_data import DataRequest

log = get_logger(__name__)

# Configure Alpaca Client
API_KEY = os.getenv('ALPACA_API_KEY')
API_SECRET = os.getenv('ALPACA_API_SECRET')
log.debug(f"API_KEY: {API_KEY}, API_SECRET: {API_SECRET}")
__CLIENT = StockHistoricalDataClient(API_KEY, API_SECRET)


def create_stock_market_activity_data(
    data: Bar, symbol: str, granularity: Granularity, source: DataSource
) -> StockMarketActivityData:
    log.debug("Adding: ", data)
    return StockMarketActivityData(
        symbol=symbol,
        granularity=granularity,
        source=source,
        datetime=data.timestamp,
        open=data.open,
        high=data.high,
        low=data.low,
        close=data.close,
        volume=data.volume,
        trade_count=data.trade_count,
        vwap=data.vwap,
    )


def create_stock_quote(data: Quote, symbol: str, granularity: Granularity, source: DataSource) -> None:
    return None


async def get_market_data(executor: ThreadPoolExecutor, request: DataRequest) -> Dict[StaticTopic, List[str]]:
    params = {
            "symbol_or_symbols": request.symbol,
            "timeframe": TimeFrame.Day,
            "limit": 10,
            "start": (datetime.now() - timedelta(days=10)).isoformat(),
            "end": (datetime.now() - timedelta(days=1)).isoformat()
        }
    log.debug("params", params)

    tasks = []
    loop = asyncio.get_running_loop()
    results = None
    response_map = {}
    latest = False
    if "start" not in params and "end" not in params:
        latest = True

    if DataType.BAR in request.data:
        if latest is True:
            task = loop.run_in_executor(executor, __CLIENT.get_stock_latest_bar, StockLatestBarRequest(**params))
        else:
            task = loop.run_in_executor(executor, __CLIENT.get_stock_bars, StockBarsRequest(**params))
        tasks.append(task)
        response_map[DataType.BAR] = len(tasks) - 1

    if DataType.QUOTE in request.data:
        if latest is True:
            task = loop.run_in_executor(executor, __CLIENT.get_stock_latest_quote, StockLatestQuoteRequest(**params))
        else:
            task = loop.run_in_executor(executor, __CLIENT.get_stock_quotes, StockQuotesRequest(**params))
        tasks.append(task)
        response_map[DataType.QUOTE] = len(tasks) - 1

    if DataType.TRADE in request.data:
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
    if DataType.BAR in response_map:
        stock_bars: BarSet = results[response_map[DataType.BAR]]

        if symbol in stock_bars.data:
            bars = stock_bars[symbol] if isinstance(stock_bars[symbol], list) else [stock_bars[symbol]]
            log.debug(bars)
            topic_map[StaticTopic.STOCK_MARKET_ACTIVITY] = [
                create_stock_market_activity_data(
                    bar, symbol, request.granularity, request.source
                ).model_dump_json() for bar in bars
            ]
    if DataType.QUOTE in response_map:
        stock_quotes: QuoteSet = results[response_map[DataType.QUOTE]]

        if symbol in stock_quotes.data:
            quotes = stock_quotes[symbol] if isinstance(stock_quotes[symbol], list) else [stock_quotes[symbol]]
            log.debug(quotes)
            topic_map[StaticTopic.STOCK_MARKET_QUOTE] = [
                create_stock_quote(
                    quote, symbol, request.granularity, request.source
                ).model_dump_json() for quote in quotes
            ]
    if DataType.TRADE in response_map:
        stock_trades = results[response_map[DataType.TRADE]]
        log.debug(f" trade results: {len(stock_trades[symbol])}")
        log.debug(f"    last: {stock_trades}")

    return topic_map
