import os
from enum import Enum

APP_NAME = os.getenv("DATA_STORE_NAME")
APP_PORT = int(os.getenv("DATA_STORE_PORT"))
APP_PORT_INTERNAL = int(os.getenv("APP_INTERNAL_PORT"))


class MarketDataInterface(str, Enum):
    POST_MARKET_ACTIVITY = "/market-activity/{asset_type}"
    GET_MARKET_ACTIVITY = "/market-activity/{asset_type}"


class StoreDataInterface(str, Enum):
    POST_STORE_STOCK = "/store/{asset_type}/{symbol}/"
