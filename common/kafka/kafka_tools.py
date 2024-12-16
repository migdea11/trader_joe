import asyncio
import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Union

from kafka import (
    KafkaAdminClient,
    KafkaConsumer,
    KafkaProducer,
    OffsetAndMetadata,
    TopicPartition
)
from kafka.admin import NewTopic
from kafka.consumer.fetcher import ConsumerRecord
from kafka.errors import KafkaError

from common.kafka.topics import ConsumerGroup, TopicTyping
from common.logging import get_logger

log = get_logger(__name__)

__KAFKA_ADMIN_CLIENT = None
__KAFKA_PUB_INSTANCES = {}
__KAFKA_SUB_INSTANCES = {}


class ConsumerParams:
    def __init__(
        self, host: str, port: int, topics: List[TopicTyping], group_id: ConsumerGroup, auto_commit: bool, timeout: int
    ):
        self.host = host
        self.port = port
        self.topics = topics
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
    clientParams: Union[ConsumerParams, ProducerParams],
    partitions: int = 1,
    replicas: int = 1
) -> bool:
    start_time = time.time()
    while True:
        try:
            if isinstance(clientParams, ConsumerParams):
                global __KAFKA_ADMIN_CLIENT
                if __KAFKA_ADMIN_CLIENT is None:
                    __KAFKA_ADMIN_CLIENT = KafkaAdminClient(
                        bootstrap_servers=clientParams.get_url()
                    )

                topics = __KAFKA_ADMIN_CLIENT.list_topics()
                consumer_topics = []
                for topic in clientParams.topics:
                    if topic.value not in topics:
                        log.debug(f"Creating topic: {topic.value}")
                        __KAFKA_ADMIN_CLIENT.create_topics(
                            new_topics=[NewTopic(topic.value, partitions, replicas)]
                        )
                    consumer_topics.append(topic.value)

            elif isinstance(clientParams, ProducerParams):
                producer = KafkaProducer(
                    bootstrap_servers=clientParams.get_url(),
                    value_serializer=lambda v: json.dumps(v).encode('utf-8')
                )
                __KAFKA_PUB_INSTANCES[clientParams.get_key()] = producer
            else:
                raise TypeError(f"Invalid type: {clientParams}")
            break
        except KafkaError:
            elapsed_time = time.time() - start_time
            if elapsed_time >= clientParams.timeout:
                log.error("Failed to connect to Kafka after {} seconds.".format(clientParams.timeout))
                return False
            log.debug("Waiting for Kafka to be ready...")
            time.sleep(5)
    return True


def close_kafka():
    if __KAFKA_ADMIN_CLIENT is not None:
        __KAFKA_ADMIN_CLIENT.close()
    for producer in __KAFKA_PUB_INSTANCES.values():
        producer.close()
    for consumer in __KAFKA_SUB_INSTANCES.values():
        consumer.close()


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


def get_consumer(clientParams: ConsumerParams) -> KafkaConsumer:
    wait_for_kafka(clientParams)
    if clientParams.get_key() not in __KAFKA_SUB_INSTANCES:
        consumer_topics = [topic.value for topic in clientParams.topics]
        consumer = KafkaConsumer(
            *consumer_topics,
            bootstrap_servers=clientParams.get_url(),
            group_id=clientParams.group_id.value,
            auto_offset_reset="earliest",
            enable_auto_commit=clientParams.enable_auto_commit
        )
        __KAFKA_SUB_INSTANCES[clientParams.get_key()] = consumer
    return __KAFKA_SUB_INSTANCES[clientParams.get_key()]


async def consume_messages_async(
    executor: ThreadPoolExecutor,
    consumer_params: ConsumerParams,
    callback: Callable[[ConsumerRecord], bool],
    commit_batch_size: int,
    commit_batch_interval: int
):
    def consume_messages():
        """
        Run the KafkaConsumer in a blocking loop within a dedicated thread.
        """
        consumer = get_consumer(consumer_params)
        batch_offsets = {}  # Store offsets for each partition
        batch_start_time = time.time()
        try:
            message: ConsumerRecord
            for message in consumer:
                # if message is None:
                #     consumer.poll(timeout_ms=100)
                #     continue

                log.debug(f"Received message: {message.value}")
                success = callback(message)

                if success:
                    # Track the latest offset for this partition
                    partition = TopicPartition(message.topic, message.partition)
                    batch_offsets[partition] = OffsetAndMetadata(message.offset + 1, "")
                else:
                    log.warning(f"Failed to process message: {message.value}")

                # Commit offsets if the batch size is reached for any partition
                time_limit_reached = time.time() - batch_start_time >= commit_batch_interval
                if len(batch_offsets) >= commit_batch_size or (time_limit_reached and batch_offsets):
                    consumer.commit(offsets=batch_offsets)
                    batch_start_time = time.time()
                    batch_offsets.clear()

        except Exception as e:
            log.error(f"Error consuming messages: {e}")
            traceback.print_exc()

    # Offload the Kafka consumer loop to a separate thread
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, consume_messages)

