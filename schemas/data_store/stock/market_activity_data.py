from uuid import UUID
from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from typing import Optional, TypeVar

from common.enums.data_select import AssetType, DataType
from common.logging import get_logger
from routers.data_store.app_endpoints import ASSET_TYPE_DESC, SYMBOL_DESC
from schemas.data_store.asset_data_interface import (
    AssetData, AssetDataCreate, AssetDataDelete, AssetDataQuery, AssetDataUpdate, BatchAssetDataCreate
)

log = get_logger(__name__)

DT = TypeVar('DT')  # Data Type
QT = TypeVar('QT')  # Query Type


class _StockDataMarketActivityData(BaseModel):
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: int

    split_factor: float
    dividends_factor: float


class _StockMarketActivityDataQuery(BaseModel):
    pass


class AssetMarketActivityRequestPath(BaseModel):
    asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)
    symbol: str = Field(..., description=SYMBOL_DESC)

    @field_validator("symbol")
    def uppercase_item_id(cls, value: str) -> str:
        return value.upper()


# class StockDataMarketActivityCreate(AssetMarketActivityRequestPath, AssetMarketActivityRequestBody):
#     @model_validator(mode='before')
#     @classmethod
#     def validate(cls, data: Any):
#         # When this data is nested in another model, it is passed as a string
#         if isinstance(data, str):
#             return json.loads(data)
#         return data


# class BatchStockDataMarketActivityCreate(BaseModel):
#     data: Dict[DataType, List[StockDataMarketActivityCreate]]


# class StockDataMarketActivityUpdate(StockDataMarketActivityCreate):
#     id: Optional[int] = None


# class AssetMarketActivityDataDelete(BaseModel):
#     asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)


# class AssetMarketActivityDataGet(BaseModel):
#     # Path
#     asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)

#     # Query
#     dataset_id: Optional[UUID] = None
#     symbol: Optional[str] = None
#     granularity: Optional[Granularity] = None
#     start: Optional[datetime] = None
#     end: Optional[datetime] = None

#     @field_validator("symbol")
#     def uppercase_item_id(cls, value: str | None) -> str | None:
#         if value is None:
#             return value
#         return value.upper()

#     def query(self) -> bool:
#         return any([self.dataset_id, self.symbol, self.granularity, self.start, self.end])


# class AssetMarketActivityDataInDB(StockDataMarketActivityUpdate):
#     created_at: datetime
#     updated_at: datetime

#     class Config:
#         from_attributes = True


# class AssetMarketActivityData(AssetMarketActivityDataInDB):
#     pass


class _StockDataMarketActivityProperties:
    @property
    def asset_type(self) -> AssetType:
        return AssetType.STOCK

    @property
    def data_type(self) -> DataType:
        return DataType.MARKET_ACTIVITY


class StockDataMarketActivityCreate(AssetDataCreate[_StockDataMarketActivityData], _StockDataMarketActivityProperties):
    pass


class BatchStockDataMarketActivityCreate(
    BatchAssetDataCreate[_StockDataMarketActivityData], _StockDataMarketActivityProperties
):
    pass


class StockDataMarketActivityUpdate(AssetDataUpdate[_StockDataMarketActivityData], _StockDataMarketActivityProperties):
    pass


class StockDataMarketActivityDelete(AssetDataDelete, _StockDataMarketActivityProperties):
    pass


class StockDataMarketActivityQuery(AssetDataQuery[_StockMarketActivityDataQuery], _StockDataMarketActivityProperties):
    dataset_id: Optional[UUID]


class StockDataMarketActivity(AssetData[_StockDataMarketActivityData], _StockDataMarketActivityProperties):
    id: int  # Isn't this uuid...
    dataset_id: UUID

    created_at: datetime
    updated_at: datetime