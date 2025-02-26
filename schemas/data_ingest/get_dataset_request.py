from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType


class BaseGetDatasetRequest(BaseModel):
    dataset_id: UUID
    source: DataSource

    granularity: Granularity
    start: datetime
    end: Optional[datetime]

    expiry: datetime
    expiry_type: ExpiryType
    update_type: UpdateType


class GetDatasetRequest(BaseGetDatasetRequest):
    asset_symbol: str
    asset_type: AssetType
    data_types: List[DataType]


class StockDatasetRequest(GetDatasetRequest):
    # Adding path params except asset_type
    asset_symbol: str
    data_types: List[DataType]

    class Config:
        # ignore asset_type
        extra = "ignore"


class CryptoDatasetRequest(StockDatasetRequest):
    pass


class OptionDatasetRequest(StockDatasetRequest):
    pass
