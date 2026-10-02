from collections.abc import Mapping

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource
from common.logging import get_logger
from schemas.data_ingest.get_dataset_request import StockDatasetRequest
from schemas.data_store.stock.market_activity_data import (
    BatchStockDataMarketActivityCreate,
    StockDataMarketActivityData,
)

from .brokers.broker_errors import MissingCredentialsError
from .brokers.interface import BarsQuery, BrokerRead, Instrument
from .brokers.rate_budget import priority_for_update_type


log = get_logger(__name__)

# The broker handle serving each data source. Installed by the app's lifespan before the RPC
# servers start and cleared after shutdown, so nothing in here knows which implementation it holds.
__READERS: dict[DataSource, BrokerRead] = {}


def install_readers(readers: Mapping[DataSource, BrokerRead]) -> None:
    """Install the broker handles that store_retrieve_stock dispatches through.

    Args:
        readers (Mapping[DataSource, BrokerRead]): Handle serving each data source.
    """
    __READERS.clear()
    __READERS.update(readers)


def clear_readers() -> None:
    """Drop every installed broker handle."""
    __READERS.clear()


def verify_code_mapping():
    # TODO on start up verify that all enums are present
    pass


async def store_retrieve_stock(request: StockDatasetRequest) -> BatchStockDataMarketActivityCreate:
    """Fetch a stock dataset through the broker handle for its source, as the store's batch schema.

    THE PLATFORM CONVERSION lives here, not in the broker: the broker speaks market data, this
    builds the store's schema from it.

    Args:
        request (StockDatasetRequest): Request being served.

    Returns:
        BatchStockDataMarketActivityCreate: Converted dataset for the store. A bare {} when the
            fetch failed (tj-fe19tu, kept at the RPC boundary until PR 2's typed errors).

    Raises:
        NotImplementedError: If no handle serves the source, or quotes or trades are requested.
        ValueError: If request.data_types is empty; raised before any get_bars call.
        MissingCredentialsError: If the broker has no credentials; surfaced before any vendor task.
    """
    reader = __READERS.get(request.source)
    if reader is None:
        raise NotImplementedError('Data source not implemented')
    if not request.data_types:
        # An empty batch would need the feed, which reaches here only through a BarsResponse.
        raise ValueError(f'data_types must not be empty: data_types={request.data_types!r}')

    batch_response = None
    if DataType.MARKET_ACTIVITY in request.data_types:
        query = BarsQuery(
            instrument=Instrument(
                symbol=request.asset_symbol, asset_type=AssetType.STOCK, exchange=None, currency=None
            ),
            granularity=request.granularity,
            start=request.start,
            end=request.end,
            priority=priority_for_update_type(request.update_type),
        )
        try:
            response = await reader.get_bars(query)
            bars = [bar async for bar in response.bars]
        except MissingCredentialsError:
            # Must reach the caller by name, before any vendor task, rather than be swallowed
            # into the bare {} below.
            raise
        except Exception as e:
            log.error(e)
            # TODO better error handling (tj-fe19tu; PR 2's typed errors)
            return {}

        batch_response = BatchStockDataMarketActivityCreate(
            dataset_id=request.dataset_id,
            asset_symbol=request.asset_symbol,
            source=request.source,
            granularity=request.granularity,
            # The feed the broker REQUESTED, not a confirmed served one (tj-vhboky.9 part B).
            feed=response.feed,
            dataset={},
        )
        for bar in bars:
            # No per-bar expiry: the lifetime moved to the dataset entry (tj-vhboky.1 section 6).
            batch_response.append_data(
                DataType.MARKET_ACTIVITY,
                StockDataMarketActivityData(
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    trade_count=bar.trade_count,
                ),
                bar.timestamp,
            )
        log.debug(f'dataset bars: {len(bars)}')
    if DataType.QUOTE in request.data_types:
        raise NotImplementedError('Quotes not implemented')
    if DataType.TRADE in request.data_types:
        raise NotImplementedError('Trades not implemented')

    return batch_response


def store_retrieve_crypto(request: StockDatasetRequest):
    pass


def store_retrieve_option(request: StockDatasetRequest):
    pass
