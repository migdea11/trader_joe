from typing import Annotated

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType, DataType
from common.logging import get_logger
from data.store.app.database.crud.stock import asset_market_activity as crud_stock_market_activity
from data.store.app.database.database import async_db
from routers.data_store.app_endpoints import AssetDataInterface
from schemas.data_store.asset_data_interface import AssetDataPath
from schemas.data_store.stock.market_activity_data import StockDataMarketActivity, StockDataMarketActivityCreate


router = APIRouter()
log = get_logger(__name__)


class UnsupportedAssetType(ValueError):
    def __init__(self, asset_type: AssetType):
        super().__init__(f'Asset type not supported: {asset_type}')


@router.post(AssetDataInterface.POST_ASSET_DATA, response_model=StockDataMarketActivity)
async def create_stock_market_activity_data(
    db: Annotated[AsyncSession, Depends(async_db)],
    asset_path: Annotated[AssetDataPath, Depends()],
    asset_data: Annotated[dict, Body(...)],
):
    log.debug(f'Storing data: /{asset_path.asset_type}/{asset_path.data_type}')
    match (asset_path.asset_type, asset_path.data_type):
        case (AssetType.STOCK, DataType.MARKET_ACTIVITY):
            stock_market_activity = StockDataMarketActivityCreate(**asset_data)
            return await crud_stock_market_activity.create_market_activity_data(db, stock_market_activity)
        case _:
            raise UnsupportedAssetType(asset_path.asset_type)


@router.get(AssetDataInterface.GET_ASSET_DATA, response_model=list[StockDataMarketActivity])
async def read_stock_market_activity_data(
    db: Annotated[AsyncSession, Depends(async_db)], asset_path: Annotated[AssetDataPath, Depends()]
):
    log.debug(f'Reading data: /{asset_path.asset_type}/{asset_path.data_type}')
    match (asset_path.asset_type, asset_path.data_type):
        case (AssetType.STOCK, DataType.MARKET_ACTIVITY):
            return await crud_stock_market_activity.read_all_asset_market_activity_data(db)
        case _:
            raise UnsupportedAssetType(asset_path.asset_type)
