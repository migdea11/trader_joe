from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from common.enums.data_select import AssetType
from common.logging import get_logger
from data.store.app.db.crud import \
    stock_market_activity as crud_stock_market_activity
from data.store.app.db.database import get_instance
from routers.data_store.app_endpoints import MarketDataInterface
from schemas.data_store.asset_market_activity_data import (
    AssetMarketActivityData,
    AssetMarketActivityDataCreate,
    AssetMarketActivityDataDelete
)

router = APIRouter()
log = get_logger(__name__)


class UnsupportedAssetType(ValueError):
    def __init__(self, asset_type: AssetType):
        super().__init__(f"Asset type not supported: {asset_type}")


@router.post(
    MarketDataInterface.POST_MARKET_ACTIVITY, response_model=AssetMarketActivityData
)
def create_stock_market_activity_data(
    request: AssetMarketActivityDataCreate,
    db: Session = Depends(get_instance)
):
    log.debug("Storing data for", request.asset_type)
    if request.asset_type == AssetType.STOCK:
        return crud_stock_market_activity.create_asset_market_activity_data_data(db, request)

    raise UnsupportedAssetType(request.asset_type)


@router.delete(MarketDataInterface.DELETE_MARKET_ACTIVITY)
def delete_stock_market_activity_data(
    request: AssetMarketActivityDataDelete,
    db: Session = Depends(get_instance)
):
    if request.asset_type == AssetType.STOCK:
        if request.id is None:
            crud_stock_market_activity.delete_all_stock_market_activity_data(db=db)
            return {"message": "All stock market activity data deleted"}
        else:
            crud_stock_market_activity.delete_stock_market_activity_data(db, request.id)
            return {"message": "Stock market activity data deleted"}

    raise UnsupportedAssetType(request.asset_type)


@router.get(MarketDataInterface.GET_MARKET_ACTIVITY, response_model=List[AssetMarketActivityData])
def read_stock_market_activity_data(db: Session = Depends(get_instance)):
    return crud_stock_market_activity.read_stock_market_activity_data(db)
