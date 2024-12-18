from enum import Enum

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from common.enums.exchange_code import BrokerExchangeBase, ExchangeCode
from common.enums.data_stock import BrokerGranularityBase, Granularity


class AlpacaGranularity(BrokerGranularityBase):
    ONE_MINUTE = (TimeFrame(1, TimeFrameUnit.Minute), Granularity.ONE_MINUTE)
    FIVE_MINUTES = (TimeFrame(5, TimeFrameUnit.Minute), Granularity.FIVE_MINUTES)
    THIRTY_MINUTES = (TimeFrame(30, TimeFrameUnit.Minute), Granularity.THIRTY_MINUTES)
    ONE_HOUR = (TimeFrame(1, TimeFrameUnit.Hour), Granularity.ONE_HOUR)
    ONE_DAY = (TimeFrame(1, TimeFrameUnit.Day), Granularity.ONE_DAY)
    ONE_WEEK = (TimeFrame(1, TimeFrameUnit.Week), Granularity.ONE_WEEK)
    ONE_MONTH = (TimeFrame(1, TimeFrameUnit.Month), Granularity.ONE_MONTH)


class ConditionCode(str, Enum):
    REGULAR = "@"
    ACQUISITION = "A"
    BUNCHED = "B"


class AlpacaExchangeCode(BrokerExchangeBase):
    CBOE = ("W", ExchangeCode.CBOE)              # CBOE
    CBOE_BYX = ("Y", ExchangeCode.BYX)           # Cboe BYX
    CBOE_BZ = ("Z", ExchangeCode.OTHER)          # Cboe BZ
    CBOE_EDGA = ("J", ExchangeCode.EDGA)         # Cboe EDGA
    CBOE_EDGX = ("K", ExchangeCode.EDGX)         # Cboe EDGX
    CHX = ("M", ExchangeCode.CHX)                # Chicago Stock Exchange
    FINRA_ADF = ("D", ExchangeCode.FINRA_ADF)    # FINRA ADF
    IEX = ("V", ExchangeCode.IEX)                # IEX
    ISE = ("I", ExchangeCode.OTHER)              # International Securities Exchange
    LTSE = ("L", ExchangeCode.LTSE)              # Long Term Stock Exchange
    MEMX = ("U", ExchangeCode.MEMX)              # Members Exchange
    MI = ("E", ExchangeCode.OTHER)               # Market Independent
    MIAX = ("H", ExchangeCode.MIAX)              # MIAX
    NASDAQ_I = ("T", ExchangeCode.NASDAQ)        # NASDAQ Int
    NASDAQ_OMX = ("Q", ExchangeCode.NASDAQ)      # NASDAQ OMX
    NASDAQ_OMX_BX = ("B", ExchangeCode.NASDAQ)   # NASDAQ OMX BX
    NASDAQ_OMX_PSX = ("X", ExchangeCode.NASDAQ)  # NASDAQ OMX PSX
    NASDAQ_SC = ("S", ExchangeCode.NASDAQ)       # NASDAQ Small Cap
    NSX = ("C", ExchangeCode.NSX)                # National Stock Exchange
    NYSE = ("N", ExchangeCode.NYSE)              # New York Stock Exchange
    NYSE_AM = ("A", ExchangeCode.AMEX)           # NYSE American (AMEX)
    NYSE_AR = ("P", ExchangeCode.ARCA)           # NYSE Arca
