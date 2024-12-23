from uuid import UUID
from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from typing import Optional

from common.enums.data_select import AssetType
from common.enums.data_stock import Granularity, DataSource
from routers.data_store.app_endpoints import ASSET_DATA_ID_DESC, ASSET_TYPE_DESC, SYMBOL_DESC


class AssetMarketActivityRequestBody(BaseModel):
    dataset_id: UUID
    source: DataSource
    symbol: str

    timestamp: datetime
    granularity: Granularity

    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: int

    split_factor: float
    dividends_factor: float


class AssetMarketActivityRequestPath(BaseModel):
    asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)
    symbol: str = Field(..., description=SYMBOL_DESC)

    @field_validator("symbol")
    def uppercase_item_id(cls, value: str) -> str:
        return value.upper()


class AssetMarketActivityDataCreate(AssetMarketActivityRequestPath, AssetMarketActivityRequestBody):
    pass


class AssetMarketActivityDataUpdate(AssetMarketActivityDataCreate):
    id: Optional[int] = None


class AssetMarketActivityDataDelete(BaseModel):
    asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)
    id: str = Field(None, description=ASSET_DATA_ID_DESC)


class AssetMarketActivityDataGet(BaseModel):
    asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)
    symbol: Optional[str] = Field(None, description=SYMBOL_DESC)

    @field_validator("symbol")
    def uppercase_item_id(cls, value: str) -> str:
        return value.upper()


class AssetMarketActivityDataInDB(AssetMarketActivityDataUpdate):
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AssetMarketActivityData(AssetMarketActivityDataInDB):
    pass
