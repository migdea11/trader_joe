from enum import Enum

from common.environment import get_env_var

APP_NAME = get_env_var("DATA_STORE_NAME")
APP_PORT = get_env_var("DATA_STORE_PORT", is_num=True)
APP_PORT_INTERNAL = get_env_var("APP_INTERNAL_PORT", is_num=True)


class MarketDataInterface(str, Enum):
    POST_MARKET_ACTIVITY = "/market-activity/{asset_type}"
    DELETE_MARKET_ACTIVITY = "/market-activity/{stock_data_id}"
    DELETE_ALL_MARKET_ACTIVITY = "/market-activity/"
    GET_MARKET_ACTIVITY = "/market-activity/{asset_type}"


class StoreDataInterface(str, Enum):
    POST_STORE_STOCK = "/store/{asset_type}/{symbol}/"
