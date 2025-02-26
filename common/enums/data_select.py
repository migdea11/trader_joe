from enum import StrEnum


class DataType(StrEnum):
    MARKET_ACTIVITY = "market-activity"
    QUOTE = "quote"
    TRADE = "trade"


class AssetType(StrEnum):
    STOCK = "stock"
    CRYPTO = "crypto"
    OPTION = "option"
