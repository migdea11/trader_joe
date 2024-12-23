from sqlalchemy import UUID, Column, Integer, DateTime, Enum as SQLAlchemyEnum, String, UniqueConstraint, func

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType
from common.database.sql_alchemy_types import IntEnum as SQLAlchemyIntEnum
from .alembic_base import Base


class StoreDatasetEntry(Base):
    TABLE_NAME = 'store_dataset_entry'
    __tablename__ = TABLE_NAME

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), unique=True, nullable=False)
    # Data request details
    symbol = Column(String, nullable=False)
    source = Column(SQLAlchemyEnum(DataSource), nullable=False)
    asset_type = Column(SQLAlchemyEnum(AssetType), nullable=False)
    data_type = Column(SQLAlchemyEnum(DataType), nullable=False)

    # Data time and period
    granularity = Column(SQLAlchemyEnum(Granularity), nullable=False)
    start = Column(DateTime, nullable=False)
    end = Column(DateTime, nullable=True)

    # Manage data expiry and updates
    expiry = Column(DateTime, nullable=True)
    expiry_type = Column(SQLAlchemyIntEnum(ExpiryType), nullable=True)
    update_type = Column(SQLAlchemyIntEnum(UpdateType), nullable=True)
    item_count = Column(Integer, default=0, nullable=False)

    # Dates used to manage split and dividends adjustments
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("symbol", "granularity", "start", "end", "source", "data_type"),
    )

    def __repr__(self):
        return (
            f"<StoreDatasetEntry(id='{self.id}', symbol='{self.symbol}', source='{self.source}', "
            f"data_types='{self.data_type}', granularity='{self.granularity}', start='{self.start}', end='{self.end}', "
            f"expiry='{self.expiry}', expiry_type='{self.expiry_type}', update_type='{self.update_type}', "
            f"item_count={self.item_count}, created_at='{self.created_at}', updated_at='{self.updated_at}')>"
        )
