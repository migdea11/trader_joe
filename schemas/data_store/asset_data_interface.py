from abc import ABC
from datetime import datetime
from typing import Dict, Generic, List, Optional, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Granularity
from routers.data_store.app_endpoints import ASSET_DATA_ID_DESC, ASSET_DATASET_ID_DESC, ASSET_TYPE_DESC, DATA_TYPE_DESC

DT = TypeVar('DT')  # Data Type
QT = TypeVar('QT')  # Query Type

class _AssetDataType(BaseModel, Generic[DT], ABC):
    """Basic Data for a financial asset.

    Args:
        Generic (DT): The data type for the asset.
    """
    timestamp: datetime
    expiry: Optional[datetime]
    data: DT

    def add_data(self, data: DT, timestamp: datetime, expiry: Optional[datetime]):
        self.timestamp = timestamp
        self.expiry = expiry
        self.data = data


class _AssetIdentifier(BaseModel, ABC):
    """Basic Identifiers for a financial asset's data"""
    asset_symbol: str
    source: DataSource
    granularity: Granularity

    @field_validator("asset_symbol")
    def uppercase_item_id(cls, value: str) -> str:
        return value.upper()


class _AssetIdentifierQuery(BaseModel, ABC):
    """Similar to _AssetIdentifier but with optional fields for querying data."""
    asset_symbol: str

    source: Optional[DataSource]
    granularity: Optional[Granularity]
    dataset_id: Optional[UUID]

    @field_validator("asset_symbol")
    def uppercase_item_id(cls, value: str | None) -> str:
        return value.upper()


class _AssetDataQuery(BaseModel, Generic[QT], ABC):
    """Remaining base query parameters that aren't direct identifiers.

    Args:
        Generic (QT): The query type for the asset.
    """
    start: Optional[datetime]
    end: Optional[datetime]
    expiry: Optional[datetime]
    query: QT


class AssetDataPath(BaseModel):
    """Asset Properties, as path to identify the endpoint for the desired data and asset type."""
    asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)
    data_type: DataType = Field(..., description=DATA_TYPE_DESC)


class AssetDataCreate(_AssetIdentifier, _AssetDataType[DT], Generic[DT], ABC):
    """Create a new asset data entry linked to the dataset provided.

    Args:
        _AssetIdentifier: Identifies the asset, data source and granularity.
        _AssetDataType (DT): Data for the asset including asset type specific data
    """
    dataset_id: UUID


class BatchAssetDataCreate(_AssetIdentifier, Generic[DT], ABC):
    """Create multiple asset data entries linked to the dataset provided.

    Args:
        _AssetIdentifier: Identifies the asset, data source and granularity.
        Generic (DT): Data for the asset including asset type specific data
    """
    dataset_id: UUID
    dataset: Dict[DataType, List[_AssetDataType[DT]]]

    def append_data(self, data_type: DataType, data: DT, timestamp: datetime, expiry: Optional[datetime]):
        if data_type not in self.dataset:
            self.dataset[data_type] = []
        self.dataset[data_type].append(_AssetDataType(timestamp=timestamp, expiry=expiry, data=data))


class AssetDataUpdate(_AssetIdentifier, _AssetDataType[DT], Generic[DT], ABC):
    """Update an existing asset data entry linked to the dataset provided.

    Args:
        _AssetIdentifier: Identifies the asset, data source and granularity.
        _AssetDataType (DT): Data for the asset including asset type specific data
    """
    id: int = Field(..., description=ASSET_DATA_ID_DESC)
    dataset_id: UUID


class AssetDataDeleteById(ABC):
    """Delete an existing asset data entry linked to the dataset provided.

    Args:
        _AssetIdentifier: Identifies the asset, data source and granularity.
    """
    dataset_id: UUID = Field(..., description=ASSET_DATASET_ID_DESC)


class AssetDataQuery(_AssetIdentifierQuery, _AssetDataQuery[QT], Generic[QT], ABC):
    """Query for asset data entries linked to the dataset provided.

    Args:
        _AssetIdentifierQuery: Queries the asset, data source and granularity.
        _AssetDataQuery (QT): Queries for the asset data, including asset type specific data.
    """
    dataset_id: Optional[UUID]


class AssetData(_AssetIdentifier, _AssetDataType[DT], Generic[DT], ABC):
    """Asset Data entry linked to the dataset provided as found in DB.

    Args:
        _AssetIdentifier: Identifies the asset, data source and granularity.
        _AssetDataType (DT): Data for the asset including asset type specific data
    """
    id: int
    dataset_id: UUID

    created_at: datetime
    updated_at: datetime
