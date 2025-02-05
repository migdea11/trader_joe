from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType
from common.logging import get_logger
from data.store.app.database.crud import \
    asset_market_activity as crud_stock_market_activity
from data.store.app.database.database import async_db
from routers.data_store.app_endpoints import MarketDataInterface
from schemas.data_store.stock.market_activity_data import (
    StockDataMarketActivityCreate,
    StockDataMarketActivity,
    StockDataMarketActivityDelete,
    StockDataMarketActivityQuery
)

router = APIRouter()
log = get_logger(__name__)


class UnsupportedAssetType(ValueError):
    def __init__(self, asset_type: AssetType):
        super().__init__(f"Asset type not supported: {asset_type}")


@router.post(
    MarketDataInterface.POST_MARKET_ACTIVITY, response_model=StockDataMarketActivity
)
async def create_stock_market_activity_data(
    db: AsyncSession = Depends(async_db),
    request: StockDataMarketActivityCreate = Depends()
):
    log.debug("Storing data for", request.asset_type)
    if request.asset_type is AssetType.STOCK:
        await crud_stock_market_activity.create_asset_market_activity_data(db, request)
        return {"message": "Stock market activity data stored"}

    raise UnsupportedAssetType(request.asset_type)


# TODO this will be removed in the future
@router.delete(MarketDataInterface.DELETE_MARKET_ACTIVITY)
async def delete_stock_market_activity_data(
    db: AsyncSession = Depends(async_db),
    request: StockDataMarketActivityDelete = Depends()
):
    if request.asset_type is AssetType.STOCK:
        await crud_stock_market_activity.delete_all_asset_market_activity_data(db=db)
        return {"message": "All stock market activity data deleted"}

    raise UnsupportedAssetType(request.asset_type)


@router.get(MarketDataInterface.GET_MARKET_ACTIVITY, response_model=List[StockDataMarketActivity])
async def read_stock_market_activity_data(
    db: AsyncSession = Depends(async_db),
    request: StockDataMarketActivityQuery = Depends()
):
    if request.query() is False:
        return await crud_stock_market_activity.read_all_asset_market_activity_data(db, request.asset_type)
    else:
        return await crud_stock_market_activity.read_asset_market_activity_dataset(db, request)

    raise UnsupportedAssetType(request.asset_type)
