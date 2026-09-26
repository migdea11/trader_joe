from abc import ABC
from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import Field, field_validator

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Granularity
from routers.data_store.app_endpoints import ASSET_DATA_ID_DESC, ASSET_TYPE_DESC, DATA_TYPE_DESC
from schemas.inbound_contract import InboundContract


DT = TypeVar('DT')  # Data Type
QT = TypeVar('QT')  # Query Type


class _AssetDataType(InboundContract, Generic[DT], ABC):
    """Basic Data for a financial asset.

    THE LIFETIME BELONGS TO THE DATASET, NOT THE BAR (tj-vhboky.1 section 9). There is
    deliberately no expiry here: expiry was a per-FETCH attribute stored on a per-BAR row, which
    is how it acquired the same last-write-wins defect as dataset_id. It is a column on the
    dataset entry, which is the row that IS the fetch, and the entry is upserted before any bar
    is written -- so nothing at this level needs to carry it.

    Args:
        Generic (DT): The data type for the asset.
    """

    timestamp: datetime
    data: DT

    def add_data(self, data: DT, timestamp: datetime):
        self.timestamp = timestamp
        self.data = data


class _AssetIdentifier(InboundContract, ABC):
    """Basic Identifiers for a financial asset's data."""

    asset_symbol: str
    source: DataSource
    granularity: Granularity

    @field_validator('asset_symbol')
    def uppercase_item_id(cls, value: str) -> str:
        return value.upper()


class _AssetIdentifierQuery(InboundContract, ABC):
    """Similar to _AssetIdentifier but with optional fields for querying data.

    EVERY FIELD HAS AN UNSET DEFAULT, and that is the point of the model (tj-vhboky.1 section 8).
    These were annotated "| None" with NO default, which in Pydantic v2 means required-but-
    nullable: a caller had to pass every one explicitly, so the object could not represent an
    unfiltered request and could not serve as an optional FastAPI query dependency. An absent
    parameter means NO CONSTRAINT on that column.
    """

    asset_symbol: str | None = None

    source: DataSource | None = None
    granularity: Granularity | None = None
    dataset_id: UUID | None = None

    @field_validator('asset_symbol')
    def uppercase_item_id(cls, value: str | None) -> str | None:
        # None means "do not filter on symbol". The annotation already claimed to accept None
        # while the body could not, so this raised AttributeError the moment a default existed.
        if value is None:
            return value
        return value.upper()


class _AssetDataQuery(InboundContract, Generic[QT], ABC):
    """Remaining base query parameters that aren't direct identifiers.

    Args:
        Generic (QT): The query type for the asset.
    """

    start: datetime | None = None
    end: datetime | None = None
    expiry: datetime | None = None
    query: QT | None = None


class AssetDataPath(InboundContract):
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
    dataset: dict[DataType, list[_AssetDataType[DT]]]

    def append_data(self, data_type: DataType, data: DT, timestamp: datetime):
        if data_type not in self.dataset:
            self.dataset[data_type] = []
        self.dataset[data_type].append(_AssetDataType(timestamp=timestamp, data=data))


class AssetDataUpdate(_AssetIdentifier, _AssetDataType[DT], Generic[DT], ABC):
    """Update an existing asset data entry linked to the dataset provided.

    Args:
        _AssetIdentifier: Identifies the asset, data source and granularity.
        _AssetDataType (DT): Data for the asset including asset type specific data
    """

    id: int = Field(..., description=ASSET_DATA_ID_DESC)
    dataset_id: UUID


class AssetDataQuery(_AssetIdentifierQuery, _AssetDataQuery[QT], Generic[QT], ABC):
    """Query for asset data entries linked to the dataset provided.

    Args:
        _AssetIdentifierQuery: Queries the asset, data source and granularity.
        _AssetDataQuery (QT): Queries for the asset data, including asset type specific data.
    """

    dataset_id: UUID | None = None


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
