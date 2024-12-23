from enum import Enum


class DataType(str, Enum):
    MARKET_ACTIVITY = "market-activity"
    QUOTE = "quote"
    TRADE = "trade"


class AssetType(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"
    OPTION = "option"
