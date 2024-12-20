from typing import List

from fastapi import APIRouter, Body, Depends, Path
from sqlalchemy.orm import Session

from common.enums.data_select import AssetType
from common.logging import get_logger
from data.store.app.db.crud import \
    stock_market_activity_data as crud_stock_price_volume
from data.store.app.db.database import get_instance
from routers.data_store.app_endpoints import MarketDataInterface
from schemas.stock_market_activity_data import (
    StockMarketActivityData,
    StockMarketActivityDataCreate
)

router = APIRouter()
log = get_logger(__name__)


@router.post(MarketDataInterface.POST_MARKET_ACTIVITY, response_model=StockMarketActivityData)
def create_stock_market_activity_data(
    asset_type: AssetType = Path(..., description="The type of the asset"),
    stock_data: StockMarketActivityDataCreate = Body(..., description="Data request parameters"),
    db: Session = Depends(get_instance)
):
    log.debug("Storing data for", asset_type)
    if asset_type == AssetType.STOCK:
        return crud_stock_price_volume.create_stock_market_activity_data(stock_data=stock_data, db=db)
    return {}  # TODO: Add other asset types


@router.delete(MarketDataInterface.DELETE_MARKET_ACTIVITY, response_model=StockMarketActivityData)
def delete_stock_market_activity_data(
    stock_data_id: int = Path(..., description="The ID of the stock data"),
    db: Session = Depends(get_instance)
):
    return crud_stock_price_volume.delete_stock_market_activity_data(db=db, stock_data_id=stock_data_id)


@router.delete(MarketDataInterface.DELETE_ALL_MARKET_ACTIVITY, response_model=List[StockMarketActivityData])
def delete_all_stock_market_activity_data(db: Session = Depends(get_instance)):
    return crud_stock_price_volume.delete_all_stock_market_activity_data(db=db)


@router.get(MarketDataInterface.GET_MARKET_ACTIVITY, response_model=List[StockMarketActivityData])
def read_stock_market_activity_data(db: Session = Depends(get_instance)):
    return crud_stock_price_volume.read_stock_market_activity_data(db=db)
