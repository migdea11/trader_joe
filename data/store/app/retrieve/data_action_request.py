import asyncio
import json
import os
import traceback
from kafka.consumer.fetcher import ConsumerRecord
from sqlalchemy.orm import Session

from common.kafka.kafka_tools import ConsumerParams, consume_messages_async, get_consumer
from common.kafka.topics import ConsumerGroup, StaticTopic
from common.logging import get_logger
from common.worker_pool import EXECUTOR
from data.store.app.db.crud.stock_market_activity_data import \
    create_stock_market_activity_data
from schemas.stock_market_activity_data import StockMarketActivityData

log = get_logger(__name__)

MARKET_ACTIVITY_BATCH_SIZE = int(os.getenv("MARKET_ACTIVITY_BATCH_SIZE"))
MARKET_ACTIVITY_BATCH_INTERVAL = int(os.getenv("MARKET_ACTIVITY_BATCH_INTERVAL"))


def store_market_activity_worker(host: str, port: int, timeout: int, db: Session):
    def callback(message: ConsumerRecord):
        try:
            message_json = message.value.decode('utf-8')
            message_dict = json.loads(message_json)
            for item in message_dict:
                data = StockMarketActivityData.model_validate_json(item)
                # TODO implement batch insert
                create_stock_market_activity_data(data, db)
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

    asyncio.create_task(consume_messages_async(
        EXECUTOR,
        clientConfig,
        callback,
        MARKET_ACTIVITY_BATCH_SIZE,
        MARKET_ACTIVITY_BATCH_INTERVAL
    ))
    log.info(f"Started consumer {StaticTopic.STOCK_MARKET_ACTIVITY.value}:{ConsumerGroup.DATA_STORE_GROUP.value}")
