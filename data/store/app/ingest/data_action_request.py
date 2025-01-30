from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import DataType
from common.environment import get_env_var
from common.kafka.kafka_rpc_factory import KafkaRpcFactory
from common.logging import get_logger
from data.store.app.database.crud.asset_market_activity import batch_insert_asset_market_activity_data
from data.store.app.database.crud.store_dataset_entry import upsert_entry
from routers.data_ingest.app_endpoints import InterfaceRpc
from schemas.data_ingest.get_dataset_request import GetDatasetRequest
from schemas.data_store.store_dataset_request import (
    StoreDatasetEntryCreate,
    StoreDatasetRequestBody,
    StoreDatasetRequestPath
)

log = get_logger(__name__)

MARKET_ACTIVITY_BATCH_SIZE = get_env_var("MARKET_ACTIVITY_BATCH_SIZE", cast_type=int)
MARKET_ACTIVITY_BATCH_INTERVAL = get_env_var("MARKET_ACTIVITY_BATCH_INTERVAL", cast_type=int)


async def store_market_activity_worker(
    request_path: StoreDatasetRequestPath,
    request_body: StoreDatasetRequestBody,
    db: AsyncSession,
    rpc_clients: KafkaRpcFactory.RpcClients
) -> int:
    dataset_id = await upsert_entry(
        db, StoreDatasetEntryCreate(**request_path.model_dump(), **request_body.model_dump())
    )
    data_ingest_request = GetDatasetRequest(
        **request_path.model_dump(),
        **request_body.model_dump(),
        dataset_id=dataset_id,
        data_types=[request_path.data_type]
    )

    rpc_client = rpc_clients.get_client(InterfaceRpc.INGEST_DATASET)
    batch_data = await rpc_client.send_request(data_ingest_request)

    # TODO function for each data type
    item_count = 0
    if DataType.MARKET_ACTIVITY in batch_data.data:
        await batch_insert_asset_market_activity_data(db, batch_data.data[DataType.MARKET_ACTIVITY])
        item_count += len(batch_data.data[DataType.MARKET_ACTIVITY])
    return item_count
