from pydantic import BaseModel
from datetime import datetime
from typing import Optional

from common.enums.data_stock import Granularity, DataSource


class StockMarketActivityDataBase(BaseModel):
    timestamp: datetime
    symbol: str
    source: DataSource
    granularity: Granularity
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: int
    vwap: float  # Volume Weighted Average Price
    split_factor: float
    dividends_factor: float
    created_at: datetime
    updated_at: datetime


class StockMarketActivityDataCreate(StockMarketActivityDataBase):
    pass


class StockMarketActivityDataUpdate(StockMarketActivityDataBase):
    pass


class StockMarketActivityDataInDBBase(StockMarketActivityDataBase):
    id: Optional[int] = None

    class Config:
        from_attributes = True


class StockMarketActivityData(StockMarketActivityDataInDBBase):
    pass


class StockMarketActivityDataInDB(StockMarketActivityDataInDBBase):
    pass
