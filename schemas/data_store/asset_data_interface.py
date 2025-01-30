from abc import ABC, abstractmethod
from datetime import datetime
from typing import Generic, List, Optional, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Granularity
from routers.data_store.app_endpoints import ASSET_TYPE_DESC, DATA_TYPE_DESC, SYMBOL_DESC

DT = TypeVar('DT')  # Data Type
QT = TypeVar('QT')  # Query Type


class _AssetDataType(BaseModel, Generic[DT], ABC):
    timestamp: datetime
    expiry: Optional[datetime]
    data: DT


class _AssetProperties(ABC):
    @property
    @abstractmethod
    def asset_type(self) -> AssetType:
        ...

    @property
    @abstractmethod
    def data_type(self) -> DataType:
        ...


class _AssetIdentifier(BaseModel, _AssetProperties, ABC):
    asset_symbol: str
    source: DataSource
    granularity: Granularity

    @field_validator("symbol")
    def uppercase_item_id(cls, value: str) -> str:
        return value.upper()


class _AssetIdentifierQuery(BaseModel, _AssetProperties, ABC):
    asset_symbol: str

    source: Optional[DataSource]
    granularity: Optional[Granularity]
    dataset_id: Optional[UUID]

    @field_validator("symbol")
    def uppercase_item_id(cls, value: str | None) -> str:
        return value.upper()


class _AssetDataQuery(BaseModel, Generic[QT], ABC):
    start: Optional[datetime]
    end: Optional[datetime]
    expiry: Optional[datetime]
    data: QT


class AssetDataPath(BaseModel):
    asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)
    data_type: DataType = Field(..., description=DATA_TYPE_DESC)


class AssetDataCreate(_AssetIdentifier, _AssetProperties, _AssetDataType[DT], ABC):
    dataset_id: UUID


class BatchAssetDataCreate(_AssetIdentifier, Generic[DT], ABC):
    dataset_id: UUID
    dataset: List[_AssetDataType[DT]]


class AssetDataUpdate(_AssetIdentifier, _AssetDataType[DT], ABC):
    id: int  # Isn't this uuid...
    dataset_id: UUID


class AssetDataDelete(_AssetIdentifier, ABC):
    dataset_id: UUID


class AssetDataQuery(_AssetIdentifierQuery, _AssetDataQuery[QT], ABC):
    dataset_id: Optional[UUID]


class AssetData(_AssetIdentifier, _AssetDataType[DT], ABC):
    id: int  # Isn't this uuid...
    dataset_id: UUID

    created_at: datetime
    updated_at: datetime
