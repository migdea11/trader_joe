from abc import abstractmethod

from sqlalchemy import UUID, Column, DateTime, Enum, ForeignKey, Index, Integer, String, func

from common.database.sql_alchemy_table import AppBase
from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource, Granularity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry


class BaseMarketActivity(AppBase.DATA_STORE_BASE):
    __abstract__ = True

    # The natural key of a bar. A bar is identified by what it describes — a symbol, from a
    # vendor, at a granularity, at an instant — so re-fetching an overlapping window is a
    # no-op rather than a duplicate row. dataset_id is deliberately NOT part of the key:
    # two fetches of the same minute through different dataset entries are the same bar, and
    # including dataset_id would defeat the point. See tj-3mk3u5.3. Concrete tables declare
    # the constraint itself so each gets its own name.
    NATURAL_KEY = ('asset_symbol', 'source', 'granularity', 'timestamp')

    # Surrogate key, kept deliberately: it is exposed as StockDataMarketActivity.id and is a
    # cheaper join and delete target than the four-column natural key above.
    id = Column(Integer, primary_key=True)
    dataset_id = Column(
        UUID, ForeignKey(f'{StoreDatasetEntry.TABLE_NAME}.id', ondelete='CASCADE'), index=True, nullable=False
    )

    source = Column(Enum(DataSource), nullable=False)
    asset_symbol = Column(String, nullable=False)

    timestamp = Column(DateTime(timezone=True), nullable=False)
    granularity = Column(Enum(Granularity), nullable=False)
    expiry = Column(DateTime, index=True, nullable=True)

    # Dates used to manage split and dividends adjustments
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (Index('ix_dataset_id_timestamp', 'dataset_id', 'timestamp'),)

    def _repr(self, table_name: str, additional_fields: str) -> str:
        return (
            f"<{table_name}(id='{self.id}', dataset_id='{self.dataset_id}', source='{self.source}, "
            f"symbol='{self.asset_symbol}', timestamp='{self.timestamp}', granularity='{self.granularity}', "
            f'{additional_fields}'
            f"created_at='{self.created_at}', updated_at='{self.updated_at}')>"
        )

    @abstractmethod
    def get_asset_type(self) -> AssetType: ...

    @abstractmethod
    def __repr__(self): ...
