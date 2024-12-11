from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Granularity


class DataRequestBody(BaseModel):
    granularity: Granularity
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    source: DataSource
    expiry: Optional[datetime] = None
    data: List[DataType]


class DataRequest(DataRequestBody):
    asset_type: AssetType = Field(..., description="The type of the asset")
    symbol: str = Field(..., description="The symbol of the asset")
