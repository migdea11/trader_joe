from uuid import UUID
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime, timedelta
from typing import Optional

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Granularity, ExpiryType, UpdateType
from routers.data_store.app_endpoints import ASSET_TYPE_DESC, DATA_TYPE_DESC, SYMBOL_DESC


class StoreDatasetRequestBody(BaseModel):
    source: DataSource

    granularity: Granularity
    start: Optional[datetime] = None
    end: Optional[datetime] = None

    expiry: Optional[datetime] = datetime.now() + timedelta(days=1)
    expiry_type: Optional[ExpiryType] = ExpiryType.BULK
    update_type: Optional[UpdateType] = UpdateType.STATIC

    @model_validator(mode="after")
    def validate_fields(cls, request: 'StoreDatasetRequestBody') -> 'StoreDatasetRequestBody':
        if request.end is not None and request.start is None:
            raise ValueError("The 'start' field is required when 'end' is provided.")

        if request.update_type is not UpdateType.STATIC and request.end is not None:
            raise ValueError(f"The 'update_type' field must be '{ExpiryType.BULK.value}' when 'end' is provided.")

        if request.update_type is not UpdateType.STATIC and request.expiry_type is ExpiryType.BULK:
            raise ValueError(
                f"The 'update_type' field must be '{UpdateType.STATIC.value}' "
                f"when 'expiry_type' is '{ExpiryType.BULK.value}'."
            )
        return request


class StoreDatasetRequestPath(BaseModel):
    asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)
    symbol: str = Field(..., description=SYMBOL_DESC)
    data_type: DataType = Field(..., description=DATA_TYPE_DESC)

    @field_validator("symbol")
    def uppercase_item_id(cls, value: str) -> str:
        return value.upper()


# class StoreDatasetRequest(StoreDatasetRequestPath, StoreDatasetRequestBody):
#     def extract_path(self) -> Dict[str, Any]:
#         path_fields = StoreDatasetRequestPath.model_fields
#         return {key: value for key, value in self.model_dump().items() if key in path_fields}

#     def extract_path_obj(self) -> StoreDatasetRequestPath:
#         return StoreDatasetRequestPath(**self.extract_path())

#     def extract_body(self) -> Dict[str, Any]:
#         body_fields = StoreDatasetRequestBody.model_fields
#         return {key: value for key, value in self.model_dump().items() if key in body_fields}

#     def extract_body_obj(self) -> StoreDatasetRequestBody:
#         return StoreDatasetRequestBody(**self.extract_body())


class StoreDatasetIdentifiers:
    def __init__(
        self,
        asset_type: AssetType,
        symbol: str,
        granularity: Granularity,
        source: DataSource,
        data_type: DataType,
        start: Optional[datetime],
        end: Optional[datetime]
    ):
        self.asset_type = asset_type
        self.symbol = symbol
        self.granularity = granularity
        self.source = source
        self.data_type = data_type
        self.start = start
        self.end = end


class StoreDatasetEntryCreate(StoreDatasetRequestPath, StoreDatasetRequestBody):
    item_count: int = 0


class StoreDatasetEntryUpdate(StoreDatasetEntryCreate):
    id: UUID


class StoreDatasetEntryInDb(StoreDatasetEntryUpdate):
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class StoreDatasetEntry(StoreDatasetEntryInDb):
    pass
