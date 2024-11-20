from pydantic import BaseModel
from datetime import datetime
from typing import Optional

from common.enums.stock_data import Granularity, DataSource

class StockPriceVolumeBase(BaseModel):
    symbol: str
    datetime: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    granularity: Granularity
    source: DataSource

class StockPriceVolumeCreate(StockPriceVolumeBase):
    pass

class StockPriceVolumeUpdate(StockPriceVolumeBase):
    pass

class StockPriceVolumeInDBBase(StockPriceVolumeBase):
    id: Optional[int] = None

    class Config:
        orm_mode = True

class StockPriceVolume(StockPriceVolumeInDBBase):
    pass

class StockPriceVolumeInDB(StockPriceVolumeInDBBase):
    pass
