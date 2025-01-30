from common.enums.data_stock import DataSource
from common.logging import get_logger
from common.worker_pool import SharedWorkerPool
from schemas.data_ingest.get_dataset_request import StockDatasetRequest
from schemas.data_store.stock.market_activity_data import BatchStockDataMarketActivityCreate

from .brokers.alpaca.broker_api import get_market_stock_data as alpaca_market_data

log = get_logger(__name__)


def verify_code_mapping():
    # TODO on start up verify that all enums are present
    pass


async def store_retrieve_stock(request: StockDatasetRequest) -> BatchStockDataMarketActivityCreate:
    if request.source is DataSource.ALPACA_API:
        return await alpaca_market_data(SharedWorkerPool.get_instance(), request)
    else:
        raise NotImplementedError("Data source not implemented")


def store_retrieve_crypto(request: StockDatasetRequest):
    pass


def store_retrieve_option(request: StockDatasetRequest):
    pass
