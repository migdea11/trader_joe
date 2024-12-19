from typing import List
from sqlalchemy.orm import Session

from common.logging import get_logger
from data.store.app.db.models.stock_market_activity import StockMarketActivityData
from schemas.data_store import asset_market_activity_data

log = get_logger(__name__)


def create_asset_market_activity_data_data(
    db: Session,
    stock_data: asset_market_activity_data.AssetMarketActivityDataCreate,
) -> asset_market_activity_data.AssetMarketActivityData:
    log.debug("Storing stock market activity data")
    db_asset_market_activity_data = StockMarketActivityData(**stock_data.model_dump())
    db.add(db_asset_market_activity_data)
    db.commit()
    db.refresh(db_asset_market_activity_data)

    return asset_market_activity_data.AssetMarketActivityData.model_validate(db_asset_market_activity_data)


def read_asset_market_activity_data_data(db: Session) -> List[asset_market_activity_data.AssetMarketActivityData]:
    log.debug("Reading stock market activity data")
    db_stock_market_activities = db.query(StockMarketActivityData).all()

    schema_objects = [
        asset_market_activity_data.AssetMarketActivityData.model_validate(obj) for obj in db_stock_market_activities
    ]
    return schema_objects


def delete_asset_market_activity_data_data(db: Session, stock_data_id: int):
    log.debug("Deleting stock market activity data")
    db_asset_market_activity_data = db.query(StockMarketActivityData).filter(
        StockMarketActivityData.id == stock_data_id
    ).first()
    db.delete(db_asset_market_activity_data)
    db.commit()


def delete_all_asset_market_activity_data_data(db: Session):
    log.debug("Deleting all stock market activity data")
    db.query(StockMarketActivityData).delete()
    db.commit()
