import os
from enum import Enum

APP_NAME = os.getenv("DATA_INGEST_NAME")
APP_PORT = int(os.getenv("DATA_INGEST_PORT"))
APP_PORT_INTERNAL = int(os.getenv("APP_INTERNAL_PORT"))


class Interface(str, Enum):
    POST_STORE_STOCK = "/broker/{asset_type}/{symbol}/"
