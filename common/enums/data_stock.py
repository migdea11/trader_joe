from datetime import timedelta
from enum import StrEnum
from typing import Generic, Self, Type, TypeVar

from common.enums.pydantic_enums import NamedIntEnum


class Granularity(StrEnum):
    ONE_MINUTE = ("1min", timedelta(minutes=1))
    FIVE_MINUTES = ("5min", timedelta(minutes=5))
    THIRTY_MINUTES = ("30min", timedelta(minutes=30))
    ONE_HOUR = ("1hour", timedelta(hours=1))
    ONE_DAY = ("1day", timedelta(days=1))
    ONE_WEEK = ("1week", timedelta(weeks=1))
    ONE_MONTH = ("1month", timedelta(weeks=4))

    def __new__(cls, value: str, offset: timedelta):
        obj = str.__new__(cls, value)  # Ensure the Enum behaves like a str
        obj._value_ = value
        obj._offset = offset
        return obj

    @property
    def offset(self) -> timedelta:
        return self._offset

    def __str__(self) -> str:
        return self._value_


T = TypeVar("T")


class BrokerGranularityBase(Generic[T]):
    """Base class for mapping broker-specific granularities to standardized ones."""

    def __init__(self, broker_code: T, granularity: Granularity):
        self._broker_code = broker_code
        self._granularity = granularity

    @property
    def broker_code(self) -> T:
        """Get the broker-specific granularity code.

        Returns:
            T: Broker-specific granularity code.
        """
        return self._broker_code

    @property
    def granularity(self) -> Granularity:
        """Get the standardized granularity.

        Returns:
            Granularity: Standardized granularity.
        """
        return self._granularity

    @classmethod
    def from_broker_code(
        cls: Type["BrokerGranularityBase"], broker_code: T
    ) -> Self:
        """Find and return the granularity mapping for a given broker-specific

        Args:
            cls (Type[BrokerGranularityBase&quot])
            broker_code (T): Broker-specific granularity code.

        Raises:
            ValueError: If the broker code is not found.

        Returns:
            Self: Granularity mapping.
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
        """Find and return the granularity mapping for a given standardized granularity.

        Args:
            cls (Type[BrokerGranularityBase])
            granularity (Granularity): Standardized granularity.

        Raises:
            ValueError: If the standardized granularity is not found.

        Returns:
            Self: Granularity mapping.
        """
        granularity_map: Self
        for granularity_map in cls:
            if granularity_map.granularity == granularity:
                return granularity_map
        raise ValueError(f"Granularity '{granularity}' not found in {cls.__name__}")


class DataSource(StrEnum):
    IB_API = "IB"
    ALPACA_API = "ALPACA"
    MANUAL_ENTRY = "MANUAL"


class ExpiryType(NamedIntEnum):
    # All items from request expire at the same time
    BULK = 1
    # A max number of items are stored, when the limit is reached the oldest item is removed
    BUFFER_1K = 2
    BUFFER_10K = 3
    BUFFER_100K = 4
    # Each item from request expires at an offset from the first item (1day bars will expire 1 day after the previous)
    ROLLING = 5


class UpdateType(NamedIntEnum):
    # Data is pulled once and never updated
    STATIC = 1
    # Data is pulled at the end of the day
    DAILY = 2
    # Data is streamed in real-time
    STREAM = 3
