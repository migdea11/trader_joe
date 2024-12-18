from enum import Enum
from typing import TypeVar, Self

T = TypeVar("T")


class ExchangeCode(str, Enum):
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


class BrokerExchangeBase(Enum):
    """
    Base class for mapping broker-specific exchange codes to standardized exchange enums.
    """
    def __init__(self, broker_code: T, exchange: ExchangeCode):
        self._broker_code = broker_code
        self._exchange = exchange

    @property
    def broker_code(self) -> T:
        """
        Returns the broker-specific exchange code.
        """
        return self._broker_code

    @property
    def exchange(self) -> ExchangeCode:
        """
        Returns the standardized exchange enum.
        """
        return self._exchange

    @classmethod
    def from_broker_code(cls, broker_code: T) -> Self:
        """
        Returns the standardized exchange enum for the given broker code.
        """
        for exchange_map in cls:
            if exchange_map.broker_code == broker_code:
                return exchange_map
        raise ValueError(f"Broker code '{broker_code}' not found in {cls.__name__}")

    @classmethod
    def get_broker_code(cls, exchange: ExchangeCode) -> Self:
        """
        Returns the broker-specific exchange code for the given standardized exchange.
        """
        for exchange_map in cls:
            if exchange_map.exchange == exchange:
                return exchange_map
        raise ValueError(f"Standardized exchange '{exchange}' not found in {cls.__name__}")
