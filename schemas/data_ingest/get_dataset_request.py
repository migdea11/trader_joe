from uuid import UUID
from pydantic import BaseModel
from datetime import datetime
from typing import Any, Dict

from common.enums.data_select import DataType
from common.enums.data_stock import DataSource, Granularity, ExpiryType, UpdateType
from schemas.data_store.store_dataset_request import StoreDatasetRequestPath


class GetDatasetRequestBody(BaseModel):
    dataset_id: UUID
    source: DataSource

    granularity: Granularity
    start: datetime
    end: datetime

    expiry: datetime
    expiry_type: ExpiryType
    update_type: UpdateType


class GetDatasetRequestPath(StoreDatasetRequestPath):
    pass


class GetDatasetRequest(GetDatasetRequestPath, GetDatasetRequestBody):
    def extract_path(self) -> Dict[str, Any]:
        path_fields = GetDatasetRequestPath.model_fields
        return {key: value for key, value in self.model_dump().items() if key in path_fields}

    def extract_path_obj(self) -> GetDatasetRequestPath:
        return GetDatasetRequestPath(**self.extract_path())

    def extract_body(self) -> Dict[str, Any]:
        body_fields = GetDatasetRequestBody.model_fields
        return {key: value for key, value in self.model_dump().items() if key in body_fields}

    def extract_body_obj(self) -> GetDatasetRequestBody:
        return GetDatasetRequestBody(**self.extract_body())


class StockDatasetRequest(GetDatasetRequestBody):
    # Adding path params except asset_type
    symbol: str
    data_type: DataType

    class Config:
        # ignore asset_type
        extra = "ignore"


class CryptoDatasetRequest(StockDatasetRequest):
    pass


class OptionDatasetRequest(StockDatasetRequest):
    pass

# class StoreDatasetIdentifiers:
#     def __init__(
#         self,
#         asset_type: AssetType,
#         symbol: str,
#         granularity: Granularity,
#         source: DataSource,
#         data_type: DataType,
#         start: Optional[datetime],
#         end: Optional[datetime]
#     ):
#         self.asset_type = asset_type
#         self.symbol = symbol
#         self.granularity = granularity
#         self.source = source
#         self.data_type = data_type
#         self.start = start
#         self.end = end


# class StoreDatasetEntryCreate(GetDatasetRequestPath, GetDatasetRequestBody):
#     item_count: int = 0


# class StoreDatasetEntryUpdate(StoreDatasetEntryCreate):
#     id: UUID


# class StoreDatasetEntryInDb(StoreDatasetEntryUpdate):
#     created_at: datetime
#     updated_at: datetime

#     class Config:
#         from_attributes = True


# class StoreDatasetEntry(StoreDatasetEntryInDb):
#     pass
