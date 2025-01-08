from enum import Enum

from common.environment import get_env_var

APP_NAME = get_env_var("DATA_INGEST_NAME")
APP_PORT = get_env_var("DATA_INGEST_PORT", cast_type=int)
APP_PORT_INTERNAL = get_env_var("APP_INTERNAL_PORT", cast_type=int)


class Interface(str, Enum):
    POST_STORE_DATASET = "/broker/{asset_type}/{symbol}/{data_type}"
