import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time
import traceback
from typing import Callable, Union

from common.logging import get_logger
from kafka import KafkaConsumer, KafkaProducer, TopicPartition
from kafka.consumer.fetcher import ConsumerRecord
from kafka.errors import KafkaError

log = get_logger(__name__)

__KAFKA_PUB_INSTANCES = {}
__KAFKA_SUB_INSTANCES = {}


class ConsumerParams:
    def __init__(self, host: str, port: int, topic: str, group_id: str, auto_commit: bool, timeout: int):
        self.host = host
        self.port = port
        self.topic = topic
        self.group_id = group_id
        self.enable_auto_commit = auto_commit
        self.timeout = timeout

        self.__url = f"{self.host}:{self.port}"
        self.__key = f"{self.__url}+{self.group_id}"

    def get_url(self) -> str:
        return self.__url

    def get_key(self) -> str:
        return self.__key


class ProducerParams:
    def __init__(self, host: str, port: int, timeout: int):
        self.host = host
        self.port = port
        self.timeout = timeout

        self.__url = f"{self.host}:{self.port}"
        self.__key = f"{self.__url}"

    def get_url(self) -> str:
        return self.__url

    def get_key(self) -> str:
        return self.__key


def wait_for_kafka(
    clientParams: Union[ConsumerParams, ProducerParams]
) -> bool:
    start_time = time.time()
    while True:
        try:
            if isinstance(clientParams, ConsumerParams):
                consumer = KafkaConsumer(
                    clientParams.topic,
                    group_id=clientParams.group_id,
                    bootstrap_servers=clientParams.get_url(),
                    auto_offset_reset="earliest",
                    enable_auto_commit=clientParams.enable_auto_commit
                )
                consumer.topics()
                __KAFKA_SUB_INSTANCES[clientParams.get_key()] = consumer
            elif isinstance(clientParams, ProducerParams):
                producer = KafkaProducer(
                    bootstrap_servers=clientParams.get_url(),
                    value_serializer=lambda v: json.dumps(v).encode('utf-8')
                )
                __KAFKA_PUB_INSTANCES[clientParams.get_key()] = producer
            else:
                raise TypeError(f"Invalid type: {clientParams}")
            log.info("Kafka is ready!")
            break
        except KafkaError:
            elapsed_time = time.time() - start_time
            if elapsed_time >= clientParams.timeout:
                log.error("Failed to connect to Kafka after {} seconds.".format(clientParams.timeout))
                return False
            log.debug("Waiting for Kafka to be ready...")
            time.sleep(5)
    return True


def get_producer(host: str, port: int, timeout: int) -> KafkaProducer:
    clientParams = ProducerParams(host, port, timeout)
    if clientParams.get_key() not in __KAFKA_PUB_INSTANCES:
        log.debug("waiting for Producer")
        wait_for_kafka(clientParams)
    return __KAFKA_PUB_INSTANCES[clientParams.get_key()]


def send_message_async(executor: ThreadPoolExecutor, producer: KafkaProducer, topic: str, message):
    loop = asyncio.get_running_loop()
    loop.run_in_executor(executor, producer.send, topic, message)


def flush_messages_async(executor: ThreadPoolExecutor, producer: KafkaProducer):
    loop = asyncio.get_running_loop()
    loop.run_in_executor(executor, producer.flush)


def get_consumer(host: str, port: int, topic: str, group_id: str, timeout: int) -> KafkaConsumer:
    clientParams = ConsumerParams(host, port, topic, group_id, False, timeout)
    if clientParams.get_key() not in __KAFKA_SUB_INSTANCES:
        log.debug("Waiting for Consumer")
        wait_for_kafka(clientParams)
    return __KAFKA_SUB_INSTANCES[clientParams.get_key()]


async def consume_messages_async(
    executor: ThreadPoolExecutor,
    consumer: KafkaConsumer,
    callback: Callable[[ConsumerRecord], bool],
    commit_batch_size: int,
    commit_batch_interval: int
):
    def consume_messages():
        """
        Run the KafkaConsumer in a blocking loop within a dedicated thread.
        """
        batch_offsets = {}  # Store offsets for each partition
        batch_start_time = time.time()
        try:
            log.debug("Starting consumer loop...")
            message: ConsumerRecord
            for message in consumer:
                log.debug(f"Received message: {message.value}")

                # Process the message immediately
                # future = asyncio.run_coroutine_threadsafe(callback(message), loop)
                log.debug(f"Processing message: {callback}")
                success = callback(message)

                # try:
                #     success = future.result()
                # except Exception as e:
                #     log.warning()(f"Error processing message: {e}")
                #     success = False
                # finally:
                if success:
                    # Track the latest offset for this partition
                    partition = TopicPartition(message.topic, message.partition)
                    batch_offsets[partition] = message.offset + 1
                    log.debug(f"Message processed successfully: {message.value}")
                else:
                    log.warning(f"Failed to process message: {message.value}")

                # Commit offsets if the batch size is reached for any partition
                time_limit_reached = time.time() - batch_start_time >= commit_batch_interval
                if len(batch_offsets) >= commit_batch_size or (time_limit_reached and batch_offsets):
                    log.debug(f"Offsets committing for batch: {batch_offsets}")
                    consumer.commit(offsets=batch_offsets)
                    log.debug(f"Offsets committed for batch: {batch_offsets}")
                    batch_offsets.clear()

        except Exception as e:
            log.error(f"Error consuming messages: {e}")
            traceback.print_exc()

    # Offload the Kafka consumer loop to a separate thread
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, consume_messages)
