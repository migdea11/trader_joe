from enum import StrEnum

from common.environment import get_env_var

APP_NAME = get_env_var("DATA_STORE_NAME")
APP_PORT = get_env_var("DATA_STORE_PORT", cast_type=int)
APP_PORT_INTERNAL = get_env_var("APP_INTERNAL_PORT", cast_type=int)

ASSET_TYPE_DESC = "Type of financial asset"
SYMBOL_DESC = "Symbol of the financial asset (aka ticker)"
DATA_TYPE_DESC = "Type of financial data"
ASSET_DATASET_ID_DESC = "Unique identifier of the dataset entry."
ASSET_DATA_ID_DESC = "Unique identifier of the data entry."


class AssetDataInterface(StrEnum):
    PUT_ASSET_DATA = "/internal/asset-data/{asset_type}/{data_type}/{id}"
    POST_ASSET_DATA = "/internal/asset-data/{asset_type}/{data_type}"
    DELETE_ASSET_DATA = "/internal/asset-data/{asset_type}/{data_type}"
    GET_ASSET_DATA = "/internal/asset-data/{asset_type}/{data_type}"


class AssetDatasetStoreInterface(StrEnum):
    PUT_STORE_ASSET_DATASET = "/store/{asset_type}/{data_type}/{asset_symbol}/{id}"
    POST_STORE_ASSET_DATASET = "/store/{asset_type}/{data_type}/{asset_symbol}"
    GET_STORE_ASSET_DATASET = "/store/{asset_type}/{data_type}/{asset_symbol}"

    GET_STORE_ASSET_DATASET_BY_ID = "/store/{id}"
    DELETE_STORE_ASSET_DATASET_BY_ID = "/store/{id}"
