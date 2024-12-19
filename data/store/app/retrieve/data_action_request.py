import asyncio
import json
import traceback

from kafka.consumer.fetcher import ConsumerRecord
from sqlalchemy.orm import Session

from common.environment import get_env_var
from common.kafka.kafka_consumer import ConsumerParams, SharedKafkaConsumer
from common.kafka.topics import ConsumerGroup, StaticTopic
from common.logging import get_logger
from common.worker_pool import SharedWorkerPool
from data.store.app.db.crud.stock_market_activity import create_asset_market_activity_data_data
from schemas.data_store.asset_market_activity_data import AssetMarketActivityDataCreate

log = get_logger(__name__)

MARKET_ACTIVITY_BATCH_SIZE = get_env_var("MARKET_ACTIVITY_BATCH_SIZE", is_num=True)
MARKET_ACTIVITY_BATCH_INTERVAL = get_env_var("MARKET_ACTIVITY_BATCH_INTERVAL", is_num=True)


def store_market_activity_worker(host: str, port: int, timeout: int, db: Session):
    def callback(message: ConsumerRecord):
        try:
            message_json = message.value.decode('utf-8')
            message_dict = json.loads(message_json)
            for item in message_dict:
                data = AssetMarketActivityDataCreate.model_validate_json(item)
                # TODO implement batch insert
                create_asset_market_activity_data_data(data, db)
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
