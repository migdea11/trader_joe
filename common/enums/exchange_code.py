from enum import StrEnum
from typing import Generic, Type, TypeVar, Self

T = TypeVar("T")


class ExchangeCode(StrEnum):
    AMEX = "A"         # NYSE American (formerly AMEX)
    ARCA = "P"         # NYSE Arca
    BATS = "B"         # BATS Exchange
    BYX = "Y"          # Cboe BYX Exchange
    CBOE = "Z"         # Chicago Board Options Exchange
    CHX = "C"          # Chicago Stock Exchange
    EDGA = "K"         # Cboe EDGA Exchange
    EDGX = "J"         # Cboe EDGX Exchange
    FINRA_ADF = "D"    # FINRA Alternative Display Facility
    IEX = "V"          # Investors Exchange
    LTSE = "L"         # Long-Term Stock Exchange
    MEMX = "M"         # Members Exchange
    MIAX = "H"         # MIAX Exchange
    NASDAQ = "Q"       # NASDAQ Stock Market
    NSX = "NSX"        # National Stock Exchange
    NYSE = "N"         # New York Stock Exchange
    OTC = "T"          # Over-The-Counter Markets
    OTHER = "O"        # Other or unknown exchanges
    PEARL = "X"        # MIAX Pearl
    PSX = "X"          # NASDAQ PSX (Philadelphia Stock Exchange)


class BrokerExchangeBase(Generic[T]):
    """Base class for mapping broker-specific exchange codes to standardized exchange enums."""
    def __init__(self, broker_code: T, exchange: ExchangeCode):
        self._broker_code = broker_code
        self._exchange = exchange

    @property
    def broker_code(self) -> T:
        """Returns the broker-specific exchange code.

        Returns:
            T: Broker-specific exchange code.
        """
        return self._broker_code

    @property
    def exchange(self) -> ExchangeCode:
        """Returns the standardized exchange enum.

        Returns:
            ExchangeCode: Standardized exchange enum.
        """
        return self._exchange

    @classmethod
    def from_broker_code(cls: Type["BrokerExchangeBase"], broker_code: T) -> Self:
        """Find and return the exchange code mapping for a given broker-specific exchange code.

        Args:
            cls (Type[BrokerExchangeBase])
            broker_code (T): Broker-specific exchange code.

        Raises:
            ValueError: If the broker code is not found.

        Returns:
            Self: Broker code mapping.
        """
        exchange_map: Self
        for exchange_map in cls:
            if exchange_map.broker_code == broker_code:
                return exchange_map
        raise ValueError(f"Broker code '{broker_code}' not found in {cls.__name__}")

    @classmethod
    def get_broker_code(cls: Type["BrokerExchangeBase"], exchange: ExchangeCode) -> Self:
        """Find and return the exchange code mapping for a given standardized exchange

        Args:
            cls (Type[BrokerExchangeBase])
            exchange (ExchangeCode): Standardized exchange.

        Raises:
            ValueError: If the exchange is not found.

        Returns:
            Self: Broker code mapping.
        """
        exchange_map: Self
        for exchange_map in cls:
            if exchange_map.exchange == exchange:
                return exchange_map
        raise ValueError(f"Standardized exchange '{exchange}' not found in {cls.__name__}")
