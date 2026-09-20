from datetime import datetime, timedelta
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType
from common.logging import get_logger
from routers.data_store.app_endpoints import ASSET_DATASET_ID_DESC, ASSET_TYPE_DESC, DATA_TYPE_DESC, SYMBOL_DESC


log = get_logger(__name__)


class StoreAssetDatasetBody(BaseModel):
    source: DataSource

    granularity: Granularity
    start: datetime | None = None
    end: datetime | None = None

    expiry: datetime | None = datetime.now() + timedelta(days=1)
    expiry_type: ExpiryType | None = ExpiryType.BULK
    update_type: UpdateType | None = UpdateType.STATIC

    @model_validator(mode="after")
    def validate_fields(cls, request: 'StoreAssetDatasetBody') -> 'StoreAssetDatasetBody':
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
        json_encoders: ClassVar[dict] = {
            ExpiryType: ExpiryType.encoder,
            UpdateType: UpdateType.encoder
        }
        # extra = "forbid"


class StoreAssetDatasetPath(BaseModel):
    asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)
    data_type: DataType = Field(..., description=DATA_TYPE_DESC)
    asset_symbol: str = Field(..., description=SYMBOL_DESC)

    @field_validator("asset_symbol")
    def uppercase_item_id(cls, value: str) -> str:
        return value.upper()


class StoreAssetDatasetQuery(BaseModel):
    # Same as body, but with optional fields
    source: DataSource | None = None
    granularity: Granularity | None = None
    start: datetime | None = None
    end: datetime | None = None
    expiry_type: ExpiryType | None = None
    update_type: UpdateType | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

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
        json_encoders: ClassVar[dict] = {
            ExpiryType: ExpiryType.encoder,
            UpdateType: UpdateType.encoder
        }
        # extra = "forbid"


class AssetDatasetStoreCreate(StoreAssetDatasetPath, StoreAssetDatasetBody):
    pass


class AssetDatasetStoreUpdate(AssetDatasetStoreCreate):
    id: UUID = Field(..., description=ASSET_DATASET_ID_DESC)


class AssetDatasetStoreGetById(BaseModel):
    id: UUID = Field(..., description=ASSET_DATASET_ID_DESC)


class AssetDatasetStoreDelete(BaseModel):
    id: UUID = Field(..., description=ASSET_DATASET_ID_DESC)


class AssetDatasetStore(AssetDatasetStoreUpdate):
    id: UUID

    item_count: int
    expiry: datetime | None = None

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
