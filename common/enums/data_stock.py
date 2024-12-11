from enum import Enum

class Granularity(str, Enum):
    ONE_MINUTE = "1min"
    FIVE_MINUTES = "5min"
    ONE_HOUR = "1hour"
    ONE_DAY = "1day"
    ONE_WEEK = "1week"
    ONE_MONTH = "1month"

class DataSource(str, Enum):
    IB_API = "IB"
    ALPACA_API = "ALPACA"
    MANUAL_ENTRY = "MANUAL"
