from enum import Enum
from typing import Self, TypeVar


class Granularity(str, Enum):
    ONE_MINUTE = "1min"
    FIVE_MINUTES = "5min"
    THIRTY_MINUTES = "30min"
    ONE_HOUR = "1hour"
    ONE_DAY = "1day"
    ONE_WEEK = "1week"
    ONE_MONTH = "1month"


T = TypeVar("T")


class BrokerGranularityBase(Enum):
    """
    Base class for mapping
    """
    def __init__(self, broker_code: T, granularity: Granularity):
        self._broker_code = broker_code
        self._granularity = granularity

    @property
    def broker_code(self) -> T:
        """
        Returns the broker-specific exchange code.
        """
        return self._broker_code

    @property
    def granularity(self) -> Granularity:
        """
        Returns the standardized exchange enum.
        """
        return self._granularity

    @classmethod
    def from_broker_code(cls, broker_code: T) -> Self:
        """
        Returns the standardized exchange enum for the given broker code.
        """
        granularity_map: Self
        for granularity_map in cls:
            if granularity_map.broker_code == broker_code:
                return granularity_map
        raise ValueError(f"Broker code '{broker_code}' not found in {cls.__name__}")

    @classmethod
    def from_granularity(cls, granularity: Granularity) -> Self:
        """
        Returns the broker-specific exchange code for the given standardized exchange.
        """
        granularity_map: Self
        for granularity_map in cls:
            if granularity_map.granularity == granularity:
                return granularity_map
        raise ValueError(f"Standardized exchange '{granularity}' not found in {cls.__name__}")


class DataSource(str, Enum):
    IB_API = "IB"
    ALPACA_API = "ALPACA"
    MANUAL_ENTRY = "MANUAL"
