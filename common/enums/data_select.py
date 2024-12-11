from enum import Enum


class DataType(str, Enum):
    BAR = "bar"
    QUOTE = "quote"
    TRADE = "trade"


class AssetType(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"
    OPTION = "option"
