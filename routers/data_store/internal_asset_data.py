from typing import Annotated, List

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType, DataType
from common.logging import get_logger
from data.store.app.database.crud.stock import asset_market_activity as crud_stock_market_activity
from data.store.app.database.database import async_db
from routers.data_store.app_endpoints import AssetDataInterface
from schemas.data_store.asset_data_interface import AssetDataPath
from schemas.data_store.stock.market_activity_data import (
    StockDataMarketActivity,
    StockDataMarketActivityCreate,
    StockDataMarketActivityDeleteById,
    StockDataMarketActivityQuery,
)

router = APIRouter()
log = get_logger(__name__)


class UnsupportedAssetType(ValueError):
    def __init__(self, asset_type: AssetType):
        super().__init__(f"Asset type not supported: {asset_type}")

@router.post(
    AssetDataInterface.POST_ASSET_DATA, response_model=StockDataMarketActivity
)
async def create_stock_market_activity_data(
    db: Annotated[AsyncSession, Depends(async_db)],
    asset_path: Annotated[AssetDataPath, Depends()],
    asset_data: Annotated[dict, Body(...)]
):
    log.debug(f"Storing data: /{asset_path.asset_type}/{asset_path.data_type}/{asset_path.asset_symbol}")
    match (asset_path.asset_type, asset_path.data_type):
        case (AssetType.STOCK, DataType.MARKET_ACTIVITY):
            stock_market_activity = StockDataMarketActivityCreate(**asset_data)
            return await crud_stock_market_activity.create_market_activity_data(db, stock_market_activity)
        case _:
            raise UnsupportedAssetType(asset_path.asset_type)


# TODO this will be removed in the future
@router.delete(AssetDataInterface.DELETE_ASSET_DATA)
async def delete_stock_market_activity_data(
    db: Annotated[AsyncSession, Depends(async_db)],
    asset_path: Annotated[AssetDataPath, Depends()],
    asset_request: Annotated[List[str], Depends()]
):
    asset_query = asset_request  # dict(asset_request.query_params)
    log.debug(f"Deleting data: /{asset_path.asset_type}/{asset_path.data_type}")
    log.debug(f"Query: {asset_query}")
    match (asset_path.asset_type, asset_path.data_type):
        case (AssetType.STOCK, DataType.MARKET_ACTIVITY):
            stock_market_activity = StockDataMarketActivityDeleteById(**asset_query)
            await crud_stock_market_activity.delete_all_market_activity_data(db, stock_market_activity)
            return {"message": "Data deleted successfully"}
        case _:
            raise UnsupportedAssetType(asset_path.asset_type)


@router.get(AssetDataInterface.GET_ASSET_DATA, response_model=List[StockDataMarketActivity])
async def read_stock_market_activity_data(
    db: Annotated[AsyncSession, Depends(async_db)],
    asset_path: Annotated[AssetDataPath, Depends()],
    # asset_request: Annotated[Dict[str, Any], Depends()]
):
    asset_query = None  # dict(asset_request.query_params)
    log.debug(f"Reading data: /{asset_path.asset_type}/{asset_path.data_type}")
    log.debug(f"Query: {asset_query}")
    match (asset_path.asset_type, asset_path.data_type):
        case (AssetType.STOCK, DataType.MARKET_ACTIVITY):
            if asset_query is None:
                return await crud_stock_market_activity.read_all_asset_market_activity_data(db)
            query = StockDataMarketActivityQuery(**asset_query)
            return await crud_stock_market_activity.read_market_activity_data(db, query)
        case _:
            raise UnsupportedAssetType(asset_path.asset_type)
