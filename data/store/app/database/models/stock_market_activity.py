from typing import Any

from sqlalchemy import Column, Float, Index, Integer, UniqueConstraint

from common.enums.data_select import AssetType, DataType
from data.store.app.database.models.base_market_activity import BaseMarketActivity
from schemas.data_store.stock.market_activity_data import (
    BatchStockDataMarketActivityCreate,
    StockDataMarketActivity,
    StockDataMarketActivityCreate,
    StockDataMarketActivityData,
)


class StockMarketActivity(BaseMarketActivity):
    TABLE_NAME = 'stock_market_activity'
    __tablename__ = TABLE_NAME

    # Named so the ON CONFLICT clause in the repository can target it by name rather than by
    # column list; the name is the contract between this model and migration 8f41c2d7a3b9,
    # kept unchanged by this revision's widening of the column list underneath it so the ON
    # CONFLICT clause does not change shape across the upgrade.
    NATURAL_KEY_CONSTRAINT = 'uq_stock_market_activity_natural_key'

    # Reads asset_symbol, granularity and a timestamp range and NEVER source or feed
    # (read_market_activity_data, verified). A prefix leading with the value-identity columns
    # would give that read a one-column equality prefix and scan every bar for the symbol across
    # all vendors, tapes and time; this order gives it two equality columns and an ordered range.
    SYMBOL_GRANULARITY_TIMESTAMP_INDEX = 'ix_stock_market_activity_symbol_granularity_timestamp'

    # Columns the upsert refreshes when a bar already exists. OHLCV only -- nothing else a
    # vendor correction could legitimately refresh. split_factor and dividends_factor are gone
    # (tj-1ltrur: raw immutable bars, adjustment applied on read from an events table that is
    # not built now). dataset_id and feed are OUT: both are part of the key now, and a column
    # inside the key cannot be mutable -- a mutable dataset_id is exactly what made ownership
    # last-write-wins and produced tj-k207b7.
    MUTABLE_COLUMNS = ('open', 'high', 'low', 'close', 'volume', 'trade_count')

    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Integer, nullable=False)
    trade_count = Column(Integer, nullable=False)

    __table_args__ = (
        *BaseMarketActivity.__table_args__,
        UniqueConstraint(*BaseMarketActivity.NATURAL_KEY, name=NATURAL_KEY_CONSTRAINT),
        Index(SYMBOL_GRANULARITY_TIMESTAMP_INDEX, 'asset_symbol', 'granularity', 'timestamp'),
    )

    # feed IS NOT SET HERE, AND THIS IS A KNOWN GAP, NOT AN OVERSIGHT. The bar column is NOT
    # NULL (see base_market_activity.py), but neither StockDataMarketActivityCreate nor
    # BatchStockDataMarketActivityCreate carries a feed field -- _AssetIdentifier in
    # schemas/data_store/asset_data_interface.py (builder-shared scope) declares only
    # asset_symbol, source and granularity. Guessing a value here (defaulting to the caller's
    # source, or to a market's NOT_APPLICABLE) would be exactly the silent default the "no
    # sentinel for we do not know" ruling exists to prevent. Until that schema carries feed, an
    # insert built from these dicts fails on the NOT NULL constraint rather than writing a wrong
    # tape -- reported as a finding for T4 (tj-vhboky.5), not patched around here.
    @classmethod
    def from_batch_create(cls, batch_create: BatchStockDataMarketActivityCreate) -> list[dict[str, Any]]:
        return [
            {
                # Base Market Activity fields
                'dataset_id': batch_create.dataset_id,
                'source': batch_create.source,
                'asset_symbol': batch_create.asset_symbol,
                'granularity': batch_create.granularity,
                'timestamp': create.timestamp,
                # Stock Market Activity fields
                'open': create.data.open,
                'high': create.data.high,
                'low': create.data.low,
                'close': create.data.close,
                'volume': create.data.volume,
                'trade_count': create.data.trade_count,
            }
            for create in batch_create.dataset[DataType.MARKET_ACTIVITY]
        ]

    @classmethod
    def from_create(cls, create: StockDataMarketActivityCreate) -> dict[str, Any]:
        return {
            # Base Market Activity fields
            'dataset_id': create.dataset_id,
            'source': create.source,
            'asset_symbol': create.asset_symbol,
            'granularity': create.granularity,
            'timestamp': create.timestamp,
            # Stock Market Activity fields
            'open': create.data.open,
            'high': create.data.high,
            'low': create.data.low,
            'close': create.data.close,
            'volume': create.data.volume,
            'trade_count': create.data.trade_count,
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
            data=StockDataMarketActivityData(
                open=self.open,
                high=self.high,
                low=self.low,
                close=self.close,
                volume=self.volume,
                trade_count=self.trade_count,
            ),
        )

    def get_asset_type(self):
        return AssetType.STOCK

    def __repr__(self):
        return self._repr(
            self.TABLE_NAME,
            additional_fields=(
                f"open='{self.open}', high='{self.high}', low='{self.low}', close='{self.close}', "
                f"volume='{self.volume}', trade_count='{self.trade_count}', feed='{self.feed}', "
            ),
        )
