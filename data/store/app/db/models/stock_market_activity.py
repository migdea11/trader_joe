from sqlalchemy import Column, Float, Integer

from data.store.app.db.models.base_market_activity import BaseMarketActivity


# TODO figure out actual primary keys
class StockMarketActivity(BaseMarketActivity):
    TABLE_NAME = 'stock_market_activity'
    __tablename__ = TABLE_NAME

    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Integer, nullable=False)
    trade_count = Column(Integer, nullable=False)

    split_factor = Column(Float, nullable=False, default=1.0)
    dividends_factor = Column(Float, nullable=False, default=1.0)

    def __repr__(self):
        return self._repr(
            self.TABLE_NAME,
            additional_fields=(
                f"open='{self.open}', high='{self.high}', low='{self.low}', close='{self.close}', "
                f"volume='{self.volume}', trade_count='{self.trade_count}', split_factor='{self.split_factor}', "
                f"dividends_factor='{self.dividends_factor}', "
            )
        )
