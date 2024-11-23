import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockQuotesRequest, StockSnapshotRequest, StockTradesRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.models.bars import Bar
from kafka import KafkaProducer

from common.enums.data_select import DataType
from common.enums.data_stock import DataSource, Granularity
from common.kafka.kafka_tools import get_producer
from schemas.stock_price_volume import StockPriceVolume
from schemas.store_broker_data import DataRequest

# Configure Alpaca Client
API_KEY = os.getenv('ALPACA_API_KEY')
API_SECRET = os.getenv('ALPACA_API_SECRET')
client = StockHistoricalDataClient(API_KEY, API_SECRET)

# Configure Kafka Producer
BROKER_NAME = os.getenv("BROKER_NAME")
BROKER_PORT = int(os.getenv("BROKER_PORT"))
BROKER_CONN_TIMEOUT = int(os.getenv("BROKER_CONN_TIMEOUT"))

# Configure Worker Threads
THREAD_WORKERS = int(os.getenv('THREAD_WORKERS'))
executor = ThreadPoolExecutor(max_workers=THREAD_WORKERS)

def create_stock_price_volume(data: Bar, symbol: str, granularity: Granularity, source: DataSource) -> StockPriceVolume:
    return StockPriceVolume(
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

async def store_retrieve_stock(request: DataRequest):
    params = {
        "symbol_or_symbols": request.symbol,
        "timeframe": TimeFrame.Day,
        "limit": 10,
        "start": (datetime.now() - timedelta(days=2)).isoformat(),
        # "end": (datetime.now() - timedelta(days=1)).isoformat()
    }

    print(request)

    tasks = []
    loop = asyncio.get_running_loop()
    results = None
    response_map = {}

    if DataType.BAR in request.data:
        stock_bar_request = StockBarsRequest(**params)
        task = loop.run_in_executor(executor, client.get_stock_bars, stock_bar_request)
        tasks.append(task)
        response_map[DataType.BAR] = len(tasks) - 1

    if DataType.QUOTE in request.data:
        stock_quote_request = StockQuotesRequest(**params)
        task = loop.run_in_executor(executor, client.get_stock_quotes, stock_quote_request)
        tasks.append(task)
        response_map[DataType.QUOTE] = len(tasks) - 1

    if DataType.TRADE in request.data:
        stock_trade_request = StockTradesRequest(**params)
        task = loop.run_in_executor(executor, client.get_stock_trades, stock_trade_request)
        tasks.append(task)
        response_map[DataType.TRADE] = len(tasks) - 1

    if DataType.SNAPSHOT in request.data:
        stock_snapshot_request = StockSnapshotRequest(**params)
        task = loop.run_in_executor(executor, client.get_stock_snapshot, stock_snapshot_request)
        tasks.append(task)
        response_map[DataType.SNAPSHOT] = len(tasks) - 1

    try:
        results = await asyncio.gather(*tasks)
    except Exception as e:
        print(e)
        return

    symbol = request.symbol.upper()
    producer: KafkaProducer = get_producer(BROKER_NAME, BROKER_PORT, BROKER_CONN_TIMEOUT)
    if DataType.BAR in response_map:
        stock_bars = results[response_map[DataType.BAR]]
        print(f" bar results: {len(stock_bars[symbol])}")

        data_json = [create_stock_price_volume(bar, symbol, request.granularity, request.source).model_dump_json() for bar in stock_bars[symbol]]
        producer.send('stock_price_volume', value=data_json)
    if DataType.QUOTE in response_map:
        stock_quotes = results[response_map[DataType.QUOTE]]
        print(f" quote results: {len(stock_quotes[symbol])}")
        print(f"    last: {stock_quotes}")
    if DataType.TRADE in response_map:
        stock_trades = results[response_map[DataType.TRADE]]
        print(f" trade results: {len(stock_trades[symbol])}")
        print(f"    last: {stock_trades}")
    if DataType.SNAPSHOT in response_map:
        stock_snapshots = results[response_map[DataType.SNAPSHOT]]
        print(f" snapshot results: {stock_snapshots[symbol]}")

    producer.flush()

def store_retrieve_crypto(request: DataRequest):
    pass

def store_retrieve_option(request: DataRequest):
    pass