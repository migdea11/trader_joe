from pydantic import BaseModel
from datetime import datetime
from typing import Optional

from common.enums.data_stock import Granularity, DataSource

class StockPriceVolumeBase(BaseModel):
    symbol: str
    datetime: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: int
    vwap: float
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
