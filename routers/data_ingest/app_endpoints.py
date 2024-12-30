from enum import Enum

from common.environment import get_env_var

APP_NAME = get_env_var("DATA_INGEST_NAME")
APP_PORT = get_env_var("DATA_INGEST_PORT", is_num=True)
APP_PORT_INTERNAL = get_env_var("APP_INTERNAL_PORT", is_num=True)


class Interface(str, Enum):
    POST_STORE_DATASET = "/broker/{asset_type}/{symbol}/{data_type}"
