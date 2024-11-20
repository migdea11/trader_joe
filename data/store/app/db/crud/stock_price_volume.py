from sqlalchemy.orm import Session

from data.store.app.db.models.stock_price_volume import StockPriceVolume
from schemas import stock_price_volume


def create_stock_price_volume(stock_data: stock_price_volume.StockPriceVolumeCreate, db: Session):
    db_stock_price_volume = StockPriceVolume(**stock_data.dict())
    db.add(db_stock_price_volume)
    db.commit()
    db.refresh(db_stock_price_volume)
    return db_stock_price_volume

def read_stock_price_volumes(db: Session):
    db_stock_price_volumes = db.query(StockPriceVolume).all()
    return db_stock_price_volumes