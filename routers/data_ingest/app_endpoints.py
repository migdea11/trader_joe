from common.environment import get_env_var
from common.kafka.rpc.kafka_rpc_base import RpcEndpoint
from common.kafka.topics import RpcEndpointTopic
from schemas.data_ingest.get_dataset_request import GetDatasetRequest
from schemas.data_store.stock.market_activity_data import BatchStockDataMarketActivityCreate


APP_NAME = get_env_var('DATA_INGEST_NAME')
APP_PORT = get_env_var('DATA_INGEST_PORT', cast_type=int)
APP_PORT_INTERNAL = get_env_var('APP_INTERNAL_PORT', cast_type=int)


class InterfaceRpc:
    INGEST_DATASET = RpcEndpoint(
        RpcEndpointTopic.STOCK_MARKET_ACTIVITY, GetDatasetRequest, BatchStockDataMarketActivityCreate
    )
