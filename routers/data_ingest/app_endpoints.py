from enum import Enum

from common.environment import get_env_var
from common.kafka.rpc.kafka_rpc_base import RpcEndpoint
from common.kafka.topics import RpcEndpointTopic
from schemas.data_ingest.get_dataset_request import GetDatasetRequest
from schemas.data_store.asset_market_activity_data import BatchStockDataCreate

APP_NAME = get_env_var("DATA_INGEST_NAME")
APP_PORT = get_env_var("DATA_INGEST_PORT", cast_type=int)
APP_PORT_INTERNAL = get_env_var("APP_INTERNAL_PORT", cast_type=int)


class InterfaceRest(str, Enum):
    POST_STORE_DATASET = "/broker/{asset_type}/{symbol}/{data_type}"


class InterfaceRpc:
    INGEST_DATASET = RpcEndpoint(
        RpcEndpointTopic.STOCK_MARKET_ACTIVITY, GetDatasetRequest, BatchStockDataCreate
    )
