from typing import Any, Dict, Tuple, Type
from fastapi import APIRouter
from pydantic import BaseModel

from common.enums.data_select import AssetType
from common.kafka.kafka_config import get_rpc_params
from common.kafka.kafka_rpc_factory import KafkaRpcFactory
from common.kafka.topics import ConsumerGroup
from common.logging import get_logger
from data.ingest.app.ingest_control import (
    store_retrieve_crypto, store_retrieve_option, store_retrieve_stock
)
from schemas.data_ingest.get_dataset_request import (
    CryptoDatasetRequest, GetDatasetRequest, OptionDatasetRequest, StockDatasetRequest
)
from schemas.data_store.asset_market_activity_data import BatchStockDataCreate
from .app_endpoints import InterfaceRpc

log = get_logger(__name__)

router = APIRouter()
rpc = KafkaRpcFactory(get_rpc_params(ConsumerGroup.DATA_INGEST_GROUP))


@rpc.add_server(InterfaceRpc.INGEST_DATASET)
async def store_data(
    request: GetDatasetRequest
) -> BatchStockDataCreate:
    asset_map: Dict[AssetType, Tuple[Type[BaseModel], Any]] = {
        AssetType.STOCK: (StockDatasetRequest, store_retrieve_stock),
        AssetType.CRYPTO: (CryptoDatasetRequest, store_retrieve_crypto),
        AssetType.OPTION: (OptionDatasetRequest, store_retrieve_option),
    }
    input_type, callback = asset_map[request.asset_type]
    return await callback(input_type(**request.model_dump()))
