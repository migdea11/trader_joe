import asyncio
import json
import os
import traceback
from kafka.consumer.fetcher import ConsumerRecord
from sqlalchemy.orm import Session

from common.kafka.kafka_tools import consume_messages_async, get_consumer
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
        log.debug("CALLBACK!!!")
        try:
            message_json = message.value.decode('utf-8')
            message_dict = json.loads(message_json)
            # if not isinstance(message_dict, list):
            #     message_dict = [message_dict]
            for item in message_dict:
                data = StockMarketActivityData.model_validate_json(item)
                create_stock_market_activity_data(data, db)
            return True
        except Exception as e:
            log.error(f"Failed to store data: {e}")
            traceback.print_exc()
            return False
    consumer = get_consumer(
        host,
        port,
        StaticTopic.STOCK_MARKET_ACTIVITY.value,
        ConsumerGroup.DATA_STORE_GROUP.value,
        timeout
    )

    asyncio.create_task(consume_messages_async(
        EXECUTOR,
        consumer,
        callback,
        MARKET_ACTIVITY_BATCH_SIZE,
        MARKET_ACTIVITY_BATCH_INTERVAL
    ))
    log.info("Started Market Activity Worker")
