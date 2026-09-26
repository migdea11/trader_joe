from typing import TypeVar

from common.logging import get_logger
from schemas.data_store.asset_data_interface import (
    AssetData,
    AssetDataCreate,
    AssetDataQuery,
    AssetDataUpdate,
    BatchAssetDataCreate,
)
from schemas.inbound_contract import InboundContract


log = get_logger(__name__)

DT = TypeVar('DT')  # Data Type
QT = TypeVar('QT')  # Query Type


class StockDataMarketActivityData(InboundContract):
    """Basic Data for a stock's market activity.

    BARS ARE RAW AND NEVER CORRECTED (tj-vhboky.1 section 6). There is deliberately no
    split_factor or dividends_factor here: corporate actions live in their own events table and
    are applied SERVER-SIDE AT READ TIME against a basis the caller names, so an adjusted value
    is derived and disposable and a bug in the adjustment is fixed by invalidating a cache rather
    than by rewriting history. The columns that used to sit here were hard-coded to 1.0 by the
    Alpaca adapter and nothing ever wrote a real factor.

    THIS IS THE MODEL THE STRICT-CONTRACT RULING WAS ABOUT. Dropping those two fields did not
    stop the Alpaca adapter sending them, and under the old permissive default every such call
    kept succeeding. As an InboundContract it now fails loudly instead, which is the whole point:
    see schemas/inbound_contract.py, including the deploy-ordering constraint it creates.
    """

    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: int


class StockMarketActivityDataQuery(InboundContract):
    """Basic Query for a stock's market activity."""

    pass


# class AssetMarketActivityRequestPath(BaseModel):
#     asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)
#     symbol: str = Field(..., description=SYMBOL_DESC)

#     @field_validator("symbol")
#     def uppercase_item_id(cls, value: str) -> str:
#         return value.upper()


# class StockDataMarketActivityCreate(AssetMarketActivityRequestPath, AssetMarketActivityRequestBody):
#     @model_validator(mode='before')
#     @classmethod
#     def validate(cls, data: Any):
#         # When this data is nested in another model, it is passed as a string
#         if isinstance(data, str):
#             return json.loads(data)
#         return data


# class BatchStockDataMarketActivityCreate(BaseModel):
#     data: Dict[DataType, List[StockDataMarketActivityCreate]]


# class StockDataMarketActivityUpdate(StockDataMarketActivityCreate):
#     id: Optional[int] = None


# class AssetMarketActivityDataDelete(BaseModel):
#     asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)


# class AssetMarketActivityDataGet(BaseModel):
#     # Path
#     asset_type: AssetType = Field(..., description=ASSET_TYPE_DESC)

#     # Query
#     dataset_id: Optional[UUID] = None
#     symbol: Optional[str] = None
#     granularity: Optional[Granularity] = None
#     start: Optional[datetime] = None
#     end: Optional[datetime] = None

#     @field_validator("symbol")
#     def uppercase_item_id(cls, value: str | None) -> str | None:
#         if value is None:
#             return value
#         return value.upper()

#     def query(self) -> bool:
#         return any([self.dataset_id, self.symbol, self.granularity, self.start, self.end])


# class AssetMarketActivityDataInDB(StockDataMarketActivityUpdate):
#     created_at: datetime
#     updated_at: datetime

#     class Config:
#         from_attributes = True


# class AssetMarketActivityData(AssetMarketActivityDataInDB):
#     pass


class StockDataMarketActivityCreate(AssetDataCreate[StockDataMarketActivityData]):
    pass


class BatchStockDataMarketActivityCreate(BatchAssetDataCreate[StockDataMarketActivityData]):
    pass


class StockDataMarketActivityUpdate(AssetDataUpdate[StockDataMarketActivityData]):
    pass


class StockDataMarketActivityQuery(AssetDataQuery[StockMarketActivityDataQuery]):
    pass


class StockDataMarketActivity(AssetData[StockDataMarketActivityData]):
    pass
