from pydantic import BaseModel, Field, model_validator
from datetime import datetime
from typing import Any, Dict, Optional, List

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Granularity


class DataRequestBody(BaseModel):
    granularity: Granularity
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    source: DataSource
    expiry: Optional[datetime] = None
    data: List[DataType]

    @model_validator(mode="before")
    def validate_start_and_end(cls, values: Dict[str, Any]):
        start = values.get("start")
        end = values.get("end")

        if end is not None and start is None:
            raise ValueError("The 'start' field is required when 'end' is provided.")
        return values


class DataRequest(DataRequestBody):
    asset_type: AssetType = Field(..., description="The type of the asset")
    symbol: str = Field(..., description="The symbol of the asset")
