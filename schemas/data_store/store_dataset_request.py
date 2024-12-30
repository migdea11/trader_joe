from uuid import UUID
from fastapi import Query
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime, timedelta
from typing import Optional

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Granularity, ExpiryType, UpdateType
from common.logging import get_logger
from routers.data_store.app_endpoints import ASSET_DATA_ID_DESC, ASSET_TYPE_DESC, DATA_TYPE_DESC, SYMBOL_DESC

log = get_logger(__name__)


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
        log.debug(f"Validating request: {request}")
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

    @field_validator("expiry_type", mode="before")
    def validate_expiry_type(cls, value):
        return ExpiryType.validate(value)

    @field_validator("update_type", mode="before")
    def validate_update_type(cls, value):
        return UpdateType.validate(value)

    class Config:
        json_encoders = {
            ExpiryType: ExpiryType.encoder,
            UpdateType: UpdateType.encoder
        }
        # extra = "forbid"


class StoreDatasetRequestPath(BaseModel):
    asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)
    symbol: str = Field(..., description=SYMBOL_DESC)
    data_type: DataType = Field(..., description=DATA_TYPE_DESC)

    @field_validator("symbol")
    def uppercase_item_id(cls, value: str) -> str:
        return value.upper()


class StoreDatasetRequestById(BaseModel):
    dataset_id: UUID = Field(..., description=ASSET_DATA_ID_DESC)


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


class StoreDatasetEntrySearch(BaseModel):
    # Same as body, but with optional fields
    source: Optional[DataSource] = None
    granularity: Optional[Granularity] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    expiry_type: Optional[ExpiryType] = None
    update_type: Optional[UpdateType] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("expiry_type", mode="before")
    def validate_expiry_type(cls, value):
        if value is None:
            return value
        return ExpiryType.validate(value)

    @field_validator("update_type", mode="before")
    def validate_update_type(cls, value):
        if value is None:
            return value
        return UpdateType.validate(value)

    class Config:
        json_encoders = {
            ExpiryType: ExpiryType.encoder,
            UpdateType: UpdateType.encoder
        }
        # extra = "forbid"


class StoreDatasetEntryCreate(StoreDatasetRequestPath, StoreDatasetRequestBody):
    pass


class StoreDatasetEntryUpdate(StoreDatasetEntryCreate):
    id: UUID


class StoreDatasetEntryInDb(StoreDatasetEntryUpdate):
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class StoreDatasetEntry(StoreDatasetEntryInDb):
    item_count: int
    expiry: datetime
