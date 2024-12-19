from sqlalchemy import UUID, Column, DateTime, Enum, Float, ForeignKey, Integer, String, func
from sqlalchemy.ext.declarative import declarative_base

from common.enums.data_stock import DataSource, Granularity

Base = declarative_base()


# TODO figure out actual primary keys
class StockMarketActivityData(Base):
    __tablename__ = 'stock_market_activity_data'

    id = Column(Integer, primary_key=True)
    store_dataset_id = Column(UUID, ForeignKey("store_dataset_entry.id"), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    symbol = Column(String, nullable=False)
    granularity = Column(Enum(Granularity), nullable=False)
    source = Column(Enum(DataSource), nullable=False)
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

    def __repr__(self):
        return (
            f"<StockPriceVolume(id='{self.id}', store_dataset_id='{self.store_dataset_id}', symbol='{self.symbol}', "
            f"datetime='{self.datetime}', open='{self.open}', high='{self.high}', low='{self.low}', "
            f"close='{self.close}', volume='{self.volume}', granularity='{self.granularity}', source='{self.source}')>"
        )
