from enum import Enum, IntEnum
from typing import Generic, Self, Type, TypeVar


class Granularity(str, Enum):
    ONE_MINUTE = "1min"
    FIVE_MINUTES = "5min"
    THIRTY_MINUTES = "30min"
    ONE_HOUR = "1hour"
    ONE_DAY = "1day"
    ONE_WEEK = "1week"
    ONE_MONTH = "1month"


T = TypeVar("T")


class BrokerGranularityBase(Generic[T]):
    """
    Base class for mapping broker-specific granularities to standardized ones.
    """

    def __init__(self, broker_code: T, granularity: Granularity):
        self._broker_code = broker_code
        self._granularity = granularity

    @property
    def broker_code(self) -> T:
        """
        Returns the broker-specific granularity code.
        """
        return self._broker_code

    @property
    def granularity(self) -> Granularity:
        """
        Returns the standardized granularity enum.
        """
        return self._granularity

    @classmethod
    def from_broker_code(
        cls: Type["BrokerGranularityBase"], broker_code: T
    ) -> Self:
        """
        Find and return the granularity mapping for a given broker code.
        """
        granularity_map: Self
        for granularity_map in cls:
            if granularity_map.broker_code == broker_code:
                return granularity_map
        raise ValueError(f"Broker code '{broker_code}' not found in {cls.__name__}")

    @classmethod
    def from_granularity(
        cls: Type["BrokerGranularityBase"], granularity: Granularity
    ) -> Self:
        """
        Find and return the broker-specific code for a given standardized granularity.
        """
        granularity_map: Self
        for granularity_map in cls:
            if granularity_map.granularity == granularity:
                return granularity_map
        raise ValueError(f"Granularity '{granularity}' not found in {cls.__name__}")


class DataSource(str, Enum):
    IB_API = "IB"
    ALPACA_API = "ALPACA"
    MANUAL_ENTRY = "MANUAL"


class ExpiryType(IntEnum):
    # All items from request expire at the same time
    BULK = 1
    # A max number of items are stored, when the limit is reached the oldest item is removed
    BUFFER_1K = 2
    BUFFER_10K = 3
    BUFFER_100K = 4
    # Each item from request expires at an offset from the first item (1day bars will expire 1 day after the previous)
    ROLLING = 5


class UpdateType(IntEnum):
    # Data is pulled once and never updated
    STATIC = 1
    # Data is pulled at the end of the day
    DAILY = 2
    # Data is streamed in real-time
    STREAM = 3
