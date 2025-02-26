from abc import abstractmethod
from sqlalchemy import UUID, Column, DateTime, Enum, ForeignKey, Index, Integer, String, func

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource, Granularity
from common.database.sql_alchemy_table import AppBase
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry


# TODO figure out actual primary keys
class BaseMarketActivity(AppBase.DATA_STORE_BASE):
    __abstract__ = True

    id = Column(Integer, primary_key=True)
    dataset_id = Column(
        UUID, ForeignKey(f"{StoreDatasetEntry.TABLE_NAME}.id", ondelete="CASCADE"), index=True, nullable=False
    )

    source = Column(Enum(DataSource), nullable=False)
    asset_symbol = Column(String, nullable=False)

    timestamp = Column(DateTime(timezone=True), nullable=False)
    granularity = Column(Enum(Granularity), nullable=False)
    expiry = Column(DateTime, index=True, nullable=True)

    # Dates used to manage split and dividends adjustments
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('ix_dataset_id_timestamp', 'dataset_id', 'timestamp'),
    )

    def _repr(self, table_name: str, additional_fields: str) -> str:
        return (
            f"<{table_name}(id='{self.id}', dataset_id='{self.dataset_id}', source='{self.source}, "
            f"symbol='{self.asset_symbol}', timestamp='{self.timestamp}', granularity='{self.granularity}', "
            f"{additional_fields}"
            f"created_at='{self.created_at}', updated_at='{self.updated_at}')>"
        )

    @abstractmethod
    def get_asset_type(self) -> AssetType:
        ...

    @abstractmethod
    def __repr__(self):
        ...
