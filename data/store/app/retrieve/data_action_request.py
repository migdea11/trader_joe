import asyncio
import json
import traceback
from typing import Dict, List

from sqlalchemy.ext.asyncio import AsyncSession
from kafka.consumer.fetcher import ConsumerRecord

from common.enums.data_select import AssetType
from common.environment import get_env_var
from common.kafka.kafka_consumer import ConsumerParams, SharedKafkaConsumer
from common.kafka.topics import ConsumerGroup, StaticTopic
from common.logging import get_logger
from common.worker_pool import SharedWorkerPool
from data.store.app.db.crud.asset_market_activity import batch_insert_asset_market_activity_data
from data.store.app.db.database import get_async_db
from schemas.data_store.asset_market_activity_data import AssetMarketActivityDataCreate

log = get_logger(__name__)

MARKET_ACTIVITY_BATCH_SIZE = get_env_var("MARKET_ACTIVITY_BATCH_SIZE", cast_type=int)
MARKET_ACTIVITY_BATCH_INTERVAL = get_env_var("MARKET_ACTIVITY_BATCH_INTERVAL", cast_type=int)


def store_market_activity_worker(host: str, port: int, timeout: int):
    async def callback(message: ConsumerRecord) -> bool:
        async with get_async_db() as db:
            try:
                message_json = message.value.decode('utf-8')
                message_dict = json.loads(message_json)
                batch_data: Dict[AssetType, List[AssetMarketActivityDataCreate]] = {}
                for item in message_dict:
                    data_item = AssetMarketActivityDataCreate.model_validate_json(item)
                    if data_item.asset_type not in batch_data:
                        batch_data[data_item.asset_type] = []
                    batch_data[data_item.asset_type].append(data_item)
                await batch_insert_asset_market_activity_data(db, batch_data)
                return True
            except Exception as e:
                log.error(f"Failed to store data: {e}")
                traceback.print_exc()
                return False

    clientConfig = ConsumerParams(
        host,
        port,
        [StaticTopic.STOCK_MARKET_ACTIVITY],
        ConsumerGroup.DATA_STORE_GROUP,
        False,
        timeout
    )

    asyncio.create_task(SharedKafkaConsumer.consume_messages_async(
        SharedWorkerPool.get_instance(),
        clientConfig,
        callback,
        MARKET_ACTIVITY_BATCH_SIZE,
        MARKET_ACTIVITY_BATCH_INTERVAL
    ))
    log.info(f"Started consumer {StaticTopic.STOCK_MARKET_ACTIVITY.value}:{ConsumerGroup.DATA_STORE_GROUP.value}")
