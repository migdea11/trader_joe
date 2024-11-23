from enum import Enum

class DataType(str, Enum):
    BAR = "bar"
    QUOTE = "quote"
    TRADE = "trade"
    SNAPSHOT = "snapshot"

class AssetType(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"
    OPTION = "option"