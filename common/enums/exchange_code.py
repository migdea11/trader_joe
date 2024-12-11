from enum import Enum


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

    def __init__(self, broker_code: str, standardized_exchange: ExchangeCode):
        self._broker_code = broker_code
        self._standardized_exchange = standardized_exchange

    @property
    def broker_code(self) -> str:
        """
        Returns the broker-specific exchange code.
        """
        return self._broker_code

    @property
    def standardized_exchange(self) -> ExchangeCode:
        """
        Returns the standardized exchange enum.
        """
        return self._standardized_exchange
