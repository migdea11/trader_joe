from sqlalchemy import Column, DateTime, Enum, Float, Integer, String
from sqlalchemy.ext.declarative import declarative_base

from common.enums.data_stock import DataSource, Granularity

Base = declarative_base()

class StockPriceVolume(Base):
    __tablename__ = 'stock_price_volume'

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

    def __repr__(self):
        return f"<StockPriceVolume(symbol='{self.symbol}', datetime='{self.datetime}', open={self.open}, high={self.high}, low={self.low}, close={self.close}, volume={self.volume}, granularity='{self.granularity}', source='{self.source}')>"
