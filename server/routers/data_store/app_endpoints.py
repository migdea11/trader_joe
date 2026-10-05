from enum import StrEnum

from common.environment import get_env_var


APP_NAME = get_env_var('DATA_STORE_NAME')
APP_PORT = get_env_var('DATA_STORE_PORT', cast_type=int)
APP_PORT_INTERNAL = get_env_var('APP_INTERNAL_PORT', cast_type=int)

# The five field-description constants that used to sit here now live in
# schemas/data_store/field_descriptions.py (tj-iontkq.3). They are OpenAPI contract text, and their
# only consumers are the schemas models, which had to import this module -- a router -- to reach
# them. Declare new description text there, not here.


class AssetDataInterface(StrEnum):
    POST_ASSET_DATA = '/internal/asset-data/{asset_type}/{data_type}'
    GET_ASSET_DATA = '/internal/asset-data/{asset_type}/{data_type}'


class AssetDatasetStoreInterface(StrEnum):
    POST_STORE_ASSET_DATASET = '/store/{asset_type}/{data_type}/{asset_symbol}'
    GET_STORE_ASSET_DATASET = '/store/{asset_type}/{data_type}/{asset_symbol}'

    GET_STORE_ASSET_DATASET_BY_ID = '/store/{id}'
    DELETE_STORE_ASSET_DATASET_BY_ID = '/store/{id}'
