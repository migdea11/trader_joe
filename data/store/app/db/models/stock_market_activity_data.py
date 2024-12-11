from sqlalchemy import Column, DateTime, Enum, Float, Integer, String, func
from sqlalchemy.ext.declarative import declarative_base

from common.enums.data_stock import DataSource, Granularity

Base = declarative_base()


class StockMarketActivityData(Base):
    __tablename__ = 'stock_market_activity_data'

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False)
    datetime = Column(DateTime, nullable=False, index=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Integer, nullable=False)
    granularity = Column(Enum(Granularity), nullable=False)  # e.g., '1min', '1hour', 'daily'
    source = Column(Enum(DataSource), nullable=False)        # e.g., 'Broker API', 'Manual Entry'
    split_factor = Column(Float, nullable=False, default=1.0)
    dividends_factor = Column(Float, nullable=False, default=1.0)
    # Dates used to manage split and dividends adjustments
    created_at = Column(DateTime, nullable=False, default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())

    def __repr__(self):
        return (
            f"<StockPriceVolume(symbol='{self.symbol}', datetime='{self.datetime}', open={self.open}, "
            f"high={self.high}, low={self.low}, close={self.close}, volume={self.volume}, "
            f"granularity='{self.granularity}', source='{self.source}')>"
        )
