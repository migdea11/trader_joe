from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from data.store.app.db import database
from data.store.app.db.crud import stock_price_volume as crud_stock_price_volume
from schemas.stock_price_volume import StockPriceVolume, StockPriceVolumeCreate

router = APIRouter()

@router.post("/stock_prices/", response_model=StockPriceVolume)
def create_stock_price_volume(stock_data: StockPriceVolumeCreate, db: Session = Depends(database.get_db)):
    return crud_stock_price_volume.create_stock_price_volume(stock_data=stock_data, db=db)

@router.get("/stock_prices/", response_model=List[StockPriceVolume])
def read_items(db: Session = Depends(database.get_db)):
    return crud_stock_price_volume.read_stock_price_volumes(db=db)