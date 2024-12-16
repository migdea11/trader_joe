from typing import List
from sqlalchemy.orm import Session

from common.logging import get_logger
from data.store.app.db.models.stock_market_activity_data import StockMarketActivityData
from schemas import stock_market_activity_data

log = get_logger(__name__)


def create_stock_market_activity_data(
    stock_data: stock_market_activity_data.StockMarketActivityDataCreate,
    db: Session
):
    log.debug("Storing stock market activity data")
    db_stock_price_volume = StockMarketActivityData(**stock_data.model_dump())
    db.add(db_stock_price_volume)
    db.commit()
    db.refresh(db_stock_price_volume)
    return db_stock_price_volume


def delete_stock_market_activity_data(db: Session, stock_data_id: int):
    log.debug("Deleting stock market activity data")
    db_stock_price_volume = db.query(StockMarketActivityData).filter(
        StockMarketActivityData.id == stock_data_id
    ).first()
    db.delete(db_stock_price_volume)
    db.commit()
    return db_stock_price_volume


def delete_all_stock_market_activity_data(db: Session) -> List[StockMarketActivityData]:
    log.debug("Deleting all stock market activity data")
    deleted = db.query(StockMarketActivityData).all()
    db.query(StockMarketActivityData).delete()
    db.commit()
    return deleted


def read_stock_market_activity_data(db: Session):
    log.debug("Reading stock market activity data")
    db_stock_price_volumes = db.query(StockMarketActivityData).all()
    return db_stock_price_volumes
