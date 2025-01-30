from sqlalchemy import UUID, Column, DateTime, Enum, String, UniqueConstraint, func

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType
from common.database.sql_alchemy_types import (
    CustomColumn, OrderedEnum as SqlIntEnum, NullableDateTime as SqlNullableDateTime
)
from common.database.sql_alchemy_table import AppBase, CustomTypeTable


class StoreDatasetEntry(AppBase.DATA_STORE_BASE, CustomTypeTable):
    TABLE_NAME = 'store_dataset_entry'
    __tablename__ = TABLE_NAME

    id = Column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), unique=True, nullable=False
    )
    # Data request details
    symbol = Column(String, nullable=False)
    source = Column(Enum(DataSource), nullable=False)
    asset_type = Column(Enum(AssetType), nullable=False)
    data_type = Column(Enum(DataType), nullable=False)

    # Data time and period
    granularity = Column(Enum(Granularity), nullable=False)
    start = Column(DateTime(timezone=True), nullable=False)
    end = CustomColumn(SqlNullableDateTime, nullable=False)

    # Manage data expiry and updates
    expiry_type = CustomColumn(SqlIntEnum(ExpiryType), nullable=True)
    update_type = CustomColumn(SqlIntEnum(UpdateType), nullable=True)

    # Dates used to manage split and dividends adjustments
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("symbol", "granularity", "start", "end", "source", "data_type"),
    )

    def __repr__(self):
        return (
            f"<StoreDatasetEntry(id='{self.id}', symbol='{self.symbol}', source='{self.source}', "
            f"data_types='{self.data_type}', granularity='{self.granularity}', start='{self.start}', end='{self.end}', "
            f"expiry_type='{self.expiry_type}', update_type='{self.update_type}', created_at='{self.created_at}', "
            f"updated_at='{self.updated_at}')>"
        )
