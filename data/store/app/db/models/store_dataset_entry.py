from sqlalchemy import UUID, Column, Integer, DateTime, Enum as SQLAlchemyEnum, PrimaryKeyConstraint, String, func
from sqlalchemy.ext.declarative import declarative_base

from common.enums.data_select import DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType

Base = declarative_base()


class StoreDatasetEntry(Base):
    __tablename__ = "store_dataset_entry"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), unique=True, nullable=False)
    # Data request details
    symbol = Column(String, nullable=False)
    source = Column(SQLAlchemyEnum(DataSource), nullable=False)
    data_type = Column(SQLAlchemyEnum(DataType), nullable=False)

    # Data time and period
    granularity = Column(SQLAlchemyEnum(Granularity), nullable=False)
    start = Column(DateTime, nullable=True)
    end = Column(DateTime, nullable=True)

    # Manage data expiry and updates
    expiry = Column(DateTime, nullable=True)
    expiry_type = Column(SQLAlchemyEnum(ExpiryType), nullable=True)
    update_type = Column(SQLAlchemyEnum(UpdateType), nullable=True)
    item_count = Column(Integer, nullable=False, default=0)

    # Dates used to manage split and dividends adjustments
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("symbol", "granularity", "start", "end", "source", "data_type"),
    )

    def __repr__(self):
        return (
            f"<DataRequest(id='{self.id}', symbol='{self.symbol}', source='{self.source}', "
            f"data_types='{self.data_type}', granularity='{self.granularity}', start='{self.start}', end='{self.end}', "
            f"expiry='{self.expiry}', expiry_type='{self.expiry_type}', update_type='{self.update_type}', "
            f"item_count={self.item_count}, created_at='{self.created_at}', updated_at='{self.updated_at}')>"
        )
