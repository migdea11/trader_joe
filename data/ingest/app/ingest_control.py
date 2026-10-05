from collections.abc import Mapping
from typing import NoReturn

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource
from common.errors.vocabulary import InvalidRequestError, Reason
from common.logging import get_logger
from schemas.data_ingest.get_dataset_request import StockDatasetRequest
from schemas.data_store.stock.market_activity_data import (
    BatchStockDataMarketActivityCreate,
    StockDataMarketActivityData,
)

from .brokers.broker_errors import MissingCredentialsError
from .brokers.interface import BarsFailure, BarsQuery, BrokerRead, Instrument
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
            fetch failed (tj-fe19tu, kept at this Kafka edge until tj-3mk3u5.11 unwires it).

    Raises:
        NotImplementedError: If no handle serves the source.
        InvalidRequestError: With the reason UNSUPPORTED_ASSET_TYPE, if quotes or trades are requested.
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
            # The Kafka path names no feed, so the deployment decides (the request's own feed stays inert
            # here; the gRPC servicer carries that half, tj-3mk3u5.9), and it passes no deadline, so it
            # keeps today's unbounded wait on the rate budget.
            feed=None,
            deadline=None,
        )
        try:
            response = await reader.get_bars(query)
            if isinstance(response, BarsFailure):
                if isinstance(response.error, MissingCredentialsError):
                    # Must reach the caller by name, before any vendor task, rather than be swallowed
                    # into the bare {} below. It is what this edge has always done, and an operator needs
                    # the variable's name, not an empty batch that looks like a symbol with no data. This
                    # is the one BarsFailure that does not become {} (tj-3mk3u5.37.5 handoff).
                    raise response.error
                # THE ONE PLACE D6's banned 'return a sentinel' form SURVIVES, deliberately and only
                # until tj-3mk3u5.11 unwires the Kafka edge: today's observable behaviour, byte for byte
                # (tj-fe19tu). The typed failure is logged here, and no caller on this edge ever sees it.
                log.error(str(response.error), exc_info=response.error.__cause__)
                return {}
            bars = [bar async for bar in response.bars]
        except MissingCredentialsError:
            # As above, and for a reader that still raises it.
            raise
        except Exception as e:
            log.error(e)
            # TODO better error handling (tj-fe19tu; removed with the Kafka edge, tj-3mk3u5.11)
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
        raise InvalidRequestError(Reason.UNSUPPORTED_ASSET_TYPE, 'Quotes are not supported yet: data_type=QUOTE')
    if DataType.TRADE in request.data_types:
        raise InvalidRequestError(Reason.UNSUPPORTED_ASSET_TYPE, 'Trades are not supported yet: data_type=TRADE')

    return batch_response


async def store_retrieve_crypto(request: StockDatasetRequest) -> NoReturn:
    """Refuse a crypto dataset: this service serves stocks only today.

    Async because the Kafka handler awaits whatever it dispatches to; this used to return None from a
    plain function, which that await turned into an accidental TypeError.

    Raises:
        InvalidRequestError: Always, with the reason UNSUPPORTED_ASSET_TYPE.
    """
    raise InvalidRequestError(Reason.UNSUPPORTED_ASSET_TYPE, 'Crypto datasets are not supported yet: asset_type=CRYPTO')


async def store_retrieve_option(request: StockDatasetRequest) -> NoReturn:
    """Refuse an option dataset: this service serves stocks only today.

    Raises:
        InvalidRequestError: Always, with the reason UNSUPPORTED_ASSET_TYPE.
    """
    raise InvalidRequestError(Reason.UNSUPPORTED_ASSET_TYPE, 'Option datasets are not supported yet: asset_type=OPTION')
