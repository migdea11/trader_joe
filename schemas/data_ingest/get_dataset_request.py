from typing import Optional
from uuid import UUID
from pydantic import BaseModel
from datetime import datetime

from common.enums.data_select import DataType
from common.enums.data_stock import DataSource, Granularity, ExpiryType, UpdateType
from schemas.data_store.store_dataset_request import StoreDatasetRequestPath


class GetDatasetRequestBody(BaseModel):
    dataset_id: UUID
    source: DataSource

    granularity: Granularity
    start: datetime
    end: Optional[datetime]

    expiry: datetime
    expiry_type: ExpiryType
    update_type: UpdateType


class GetDatasetRequestPath(StoreDatasetRequestPath):
    pass


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
