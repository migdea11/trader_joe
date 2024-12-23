from sqlalchemy import UUID, Column, DateTime, Enum, Float, ForeignKey, Index, Integer, String, func

from common.enums.data_stock import DataSource, Granularity
from data.store.app.db.models.store_dataset_entry import StoreDatasetEntry
from .alembic_base import Base


# TODO figure out actual primary keys
class StockMarketActivity(Base):
    TABLE_NAME = 'stock_market_activity'
    __tablename__ = TABLE_NAME

    id = Column(Integer, primary_key=True)
    dataset_id = Column(
        UUID, ForeignKey(f"{StoreDatasetEntry.TABLE_NAME}.id", ondelete="CASCADE"), index=True, nullable=False
    )

    source = Column(Enum(DataSource), nullable=False)
    symbol = Column(String, nullable=False)

    timestamp = Column(DateTime, nullable=False)
    granularity = Column(Enum(Granularity), nullable=False)
    expiry = Column(DateTime, index=True, nullable=True)

    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Integer, nullable=False)
    trade_count = Column(Integer, nullable=False)

    split_factor = Column(Float, nullable=False, default=1.0)
    dividends_factor = Column(Float, nullable=False, default=1.0)

    # Dates used to manage split and dividends adjustments
    created_at = Column(DateTime, nullable=False, default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('ix_dataset_id_timestamp', 'dataset_id', 'timestamp'),
    )

    def __repr__(self):
        return (
            f"<StockPriceVolume(id='{self.id}', dataset_id='{self.dataset_id}', source='{self.source}, "
            f"symbol='{self.symbol}', timestamp='{self.timestamp}', granularity='{self.granularity}', "
            f"open='{self.open}', high='{self.high}', low='{self.low}', close='{self.close}', volume='{self.volume}', "
            f"trade_count='{self.trade_count}', split_factor='{self.split_factor}', "
            f"dividends_factor='{self.dividends_factor}', "
            f"created_at='{self.created_at}', updated_at='{self.updated_at}')>"
        )
