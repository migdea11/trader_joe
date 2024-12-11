import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Type, Union

from common.logging import get_logger
from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import KafkaError

log = get_logger(__name__)

__KAFKA_PUB_INSTANCES = {}
__KAFKA_SUB_INSTANCES = {}
__EXECUTOR = ThreadPoolExecutor()


def wait_for_kafka(
    host: str, port: int, timeout: int, client: Union[Type[KafkaConsumer], Type[KafkaProducer]] = KafkaConsumer
) -> bool:
    start_time = time.time()
    url = f"{host}:{port}"
    while True:
        try:
            if client is KafkaConsumer:
                consumer = client(bootstrap_servers=f'{host}:{port}')
                consumer.topics()
                __KAFKA_SUB_INSTANCES[url] = consumer
            elif client is KafkaProducer:
                producer = client(
                    bootstrap_servers=f'{host}:{port}',
                    value_serializer=lambda v: json.dumps(v).encode('utf-8')
                )
                __KAFKA_PUB_INSTANCES[url] = producer
            else:
                raise TypeError(f"Invalid type: {client}")
            log.info("Kafka is ready!")
            break
        except KafkaError:
            elapsed_time = time.time() - start_time
            if elapsed_time >= timeout:
                log.error("Failed to connect to Kafka after {} seconds.".format(timeout))
                return False
            log.debug("Waiting for Kafka to be ready...")
            time.sleep(5)
    return True


def get_producer(host: str, port: int, timeout: int) -> KafkaProducer:
    url = f"{host}:{port}"
    if url not in __KAFKA_PUB_INSTANCES:
        log.debug("waiting for Producer")
        wait_for_kafka(host, port, timeout, KafkaProducer)
    return __KAFKA_PUB_INSTANCES[url]


async def send_message_async(producer: KafkaProducer, topic: str, message):
    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(__EXECUTOR, producer.send, topic, message)
    log.debug(response)


async def flush_messages_async(producer: KafkaProducer):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(__EXECUTOR, producer.flush)


def get_consumer(host: str, port: int, timeout: int) -> KafkaConsumer:
    url = f"{host}:{port}"
    if url not in __KAFKA_SUB_INSTANCES:
        log.debug("Waiting for Consumer")
        wait_for_kafka(host, port, timeout, KafkaConsumer)
    return __KAFKA_SUB_INSTANCES[url]
