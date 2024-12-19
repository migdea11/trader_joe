from enum import Enum
from typing import Self, TypeVar, Generic, Type


# Define a TypeVar to represent the broker-specific type
T = TypeVar("T")


class Granularity(str, Enum):
    ONE_MINUTE = "1min"
    FIVE_MINUTES = "5min"
    THIRTY_MINUTES = "30min"
    ONE_HOUR = "1hour"
    ONE_DAY = "1day"
    ONE_WEEK = "1week"
    ONE_MONTH = "1month"


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
        for granularity_map in cls:
            if granularity_map.granularity == granularity:
                return granularity_map
        raise ValueError(f"Granularity '{granularity}' not found in {cls.__name__}")


class AlpacaGranularity(BrokerGranularityBase[str], Enum):
    """
    Specific implementation of BrokerGranularityBase for Alpaca's granularity mappings.
    """

    ONE_MINUTE = ("1Min", Granularity.ONE_MINUTE)
    FIVE_MINUTES = ("5Min", Granularity.FIVE_MINUTES)
    THIRTY_MINUTES = ("30Min", Granularity.THIRTY_MINUTES)
    ONE_HOUR = ("1Hour", Granularity.ONE_HOUR)
    ONE_DAY = ("1Day", Granularity.ONE_DAY)
    ONE_WEEK = ("1Week", Granularity.ONE_WEEK)
    ONE_MONTH = ("1Month", Granularity.ONE_MONTH)

    # def __init__(self, broker_code: str, granularity: Granularity):
    #     self._broker_code = broker_code
    #     self._granularity = granularity

    # @property
    # def broker_code(self) -> str:
    #     return self._broker_code

    # @property
    # def granularity(self) -> Granularity:
    #     return self._granularity


# Example usage
print(AlpacaGranularity.ONE_MONTH.broker_code)  # Output: "1Month"
print(AlpacaGranularity.ONE_MONTH.granularity)  # Output: Granularity.ONE_MONTH
print(AlpacaGranularity.from_broker_code("1Month").granularity)  # Output: AlpacaGranularity.ONE_MONTH
print(AlpacaGranularity.from_granularity(Granularity.ONE_MONTH).broker_code)  # Output: AlpacaGranularity.ONE_MONTH