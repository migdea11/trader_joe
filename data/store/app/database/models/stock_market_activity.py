from typing import Any, Dict, List
from sqlalchemy import Column, Float, Integer

from common.enums.data_select import AssetType, DataType
from data.store.app.database.models.base_market_activity import BaseMarketActivity
from schemas.data_store.stock.market_activity_data import (
    BatchStockDataMarketActivityCreate,
    StockDataMarketActivity,
    StockDataMarketActivityCreate,
    StockDataMarketActivityData
)


# TODO figure out actual primary keys
class StockMarketActivity(BaseMarketActivity):
    TABLE_NAME = 'stock_market_activity'
    __tablename__ = TABLE_NAME

    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Integer, nullable=False)
    trade_count = Column(Integer, nullable=False)

    split_factor = Column(Float, nullable=False, default=1.0)
    dividends_factor = Column(Float, nullable=False, default=1.0)

    @classmethod
    def from_batch_create(cls, batch_create: BatchStockDataMarketActivityCreate) -> List[Dict[str, Any]]:
        return [{
            # Base Market Activity fields
            "dataset_id" : batch_create.dataset_id,
            "source" : batch_create.source,
            "asset_symbol" : batch_create.asset_symbol,
            "granularity" : batch_create.granularity,
            "timestamp" : create.timestamp,
            "expiry" : create.expiry,

            # Stock Market Activity fields
            "open" : create.data.open,
            "high" : create.data.high,
            "low" : create.data.low,
            "close" : create.data.close,
            "volume" : create.data.volume,
            "trade_count" : create.data.trade_count,
            "split_factor" : create.data.split_factor,
            "dividends_factor" : create.data.dividends_factor
        } for create in batch_create.dataset[DataType.MARKET_ACTIVITY]]

    @classmethod
    def from_create(cls, create: StockDataMarketActivityCreate) -> Dict[str, Any]:
        return {
            # Base Market Activity fields
            "dataset_id" : create.dataset_id,
            "source" : create.source,
            "asset_symbol" : create.asset_symbol,
            "granularity" : create.granularity,
            "timestamp" : create.timestamp,
            "expiry" : create.expiry,

            # Stock Market Activity fields
            "open" : create.data.open,
            "high" : create.data.high,
            "low" : create.data.low,
            "close" : create.data.close,
            "volume" : create.data.volume,
            "trade_count" : create.data.trade_count,
            "split_factor" : create.data.split_factor,
            "dividends_factor" : create.data.dividends_factor
        }

    def to_schema(self) -> StockDataMarketActivity:
        return StockDataMarketActivity(
            id=self.id,
            dataset_id=self.dataset_id,
            source=self.source,
            asset_symbol=self.asset_symbol,
            granularity=self.granularity,
            created_at=self.created_at,
            updated_at=self.updated_at,
            timestamp=self.timestamp,
            expiry=self.expiry,
            data=StockDataMarketActivityData(
                open=self.open,
                high=self.high,
                low=self.low,
                close=self.close,
                volume=self.volume,
                trade_count=self.trade_count,
                split_factor=self.split_factor,
                dividends_factor=self.dividends_factor
            )
        )

    def get_asset_type(self):
        return AssetType.STOCK

    def __repr__(self):
        return self._repr(
            self.TABLE_NAME,
            additional_fields=(
                f"open='{self.open}', high='{self.high}', low='{self.low}', close='{self.close}', "
                f"volume='{self.volume}', trade_count='{self.trade_count}', split_factor='{self.split_factor}', "
                f"dividends_factor='{self.dividends_factor}', "
            )
        )
