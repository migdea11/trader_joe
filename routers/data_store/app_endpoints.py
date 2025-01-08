from enum import Enum

from common.environment import get_env_var

APP_NAME = get_env_var("DATA_STORE_NAME")
APP_PORT = get_env_var("DATA_STORE_PORT", cast_type=int)
APP_PORT_INTERNAL = get_env_var("APP_INTERNAL_PORT", cast_type=int)

ASSET_TYPE_DESC = "Type of financial asset"
SYMBOL_DESC = "Symbol of the financial asset (aka ticker)"
DATA_TYPE_DESC = "Type of financial data"
ASSET_DATA_ID_DESC = "Unique identifier of the dataset entry."


class MarketDataInterface(str, Enum):
    POST_MARKET_ACTIVITY = "/market-activity/{asset_type}/{symbol}"
    DELETE_MARKET_ACTIVITY = "/market-activity/{asset_type}"
    GET_MARKET_ACTIVITY = "/market-activity/{asset_type}"


class StoreDataInterface(str, Enum):
    POST_STORE_STOCK = "/store/{asset_type}/{symbol}/{data_type}"
    GET_STORE_STOCK = "/store/{asset_type}/{symbol}/{data_type}"
    DELETE_STORE_STOCK = "/store/{dataset_id}"
