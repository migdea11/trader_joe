from pydantic import BaseModel, Field
from datetime import datetime
from typing import Any, Dict, Optional

from common.enums.data_select import AssetType
from common.enums.data_stock import Granularity, DataSource
from routers.data_store.app_endpoints import ASSET_DATA_ID_DESC, ASSET_TYPE_DESC, SYMBOL_DESC


class AssetMarketActivityRequestBody(BaseModel):
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


class AssetMarketActivityRequest(AssetMarketActivityRequestPath, AssetMarketActivityRequestBody):
    def extract_path(self) -> Dict[str, Any]:
        path_fields = AssetMarketActivityRequestPath.model_fields
        return {key: value for key, value in self.model_dump().items() if key in path_fields}

    def extract_path_obj(self) -> AssetMarketActivityRequestPath:
        return AssetMarketActivityRequestPath(**self.extract_path())

    def extract_body(self) -> Dict[str, Any]:
        body_fields = AssetMarketActivityRequestBody.model_fields
        return {key: value for key, value in self.model_dump().items() if key in body_fields}

    def extract_body_obj(self) -> AssetMarketActivityRequestBody:
        return AssetMarketActivityRequestBody(**self.extract_body())


class AssetMarketActivityDataCreate(AssetMarketActivityRequestPath, AssetMarketActivityRequestBody):
    pass


class AssetMarketActivityDataUpdate(AssetMarketActivityDataCreate):
    id: Optional[int] = None


class AssetMarketActivityDataDelete(BaseModel):
    asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)
    id: str = Field(None, description=ASSET_DATA_ID_DESC)


class AssetMarketActivityDataInDB(AssetMarketActivityDataUpdate):
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AssetMarketActivityData(AssetMarketActivityDataInDB):
    pass
