import json
from uuid import UUID
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime
from typing import Any, Dict, List, Optional

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import Granularity, DataSource
from common.logging import get_logger
from routers.data_store.app_endpoints import ASSET_TYPE_DESC, SYMBOL_DESC

log = get_logger(__name__)


class AssetMarketActivityRequestBody(BaseModel):
    dataset_id: UUID
    source: DataSource
    symbol: str

    timestamp: datetime
    granularity: Granularity
    expiry: Optional[datetime]

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
    @model_validator(mode='before')
    @classmethod
    def validate(cls, data: Any):
        # When this data is nested in another model, it is passed as a string
        if isinstance(data, str):
            return json.loads(data)
        return data


class BatchStockDataCreate(BaseModel):
    data: Dict[DataType, List[AssetMarketActivityDataCreate]]


class AssetMarketActivityDataUpdate(AssetMarketActivityDataCreate):
    id: Optional[int] = None


class AssetMarketActivityDataDelete(BaseModel):
    asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)


class AssetMarketActivityDataGet(BaseModel):
    # Path
    asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)

    # Query
    dataset_id: Optional[UUID] = None
    symbol: Optional[str] = None
    granularity: Optional[Granularity] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None

    @field_validator("symbol")
    def uppercase_item_id(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return value.upper()

    def query(self) -> bool:
        return any([self.dataset_id, self.symbol, self.granularity, self.start, self.end])


class AssetMarketActivityDataInDB(AssetMarketActivityDataUpdate):
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AssetMarketActivityData(AssetMarketActivityDataInDB):
    pass
