import asyncio
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Coroutine

from kafka import (
    KafkaAdminClient,
    KafkaConsumer,
    OffsetAndMetadata,
    TopicPartition
)
from kafka.admin import NewTopic
from kafka.consumer.fetcher import ConsumerRecord
from kafka.errors import KafkaError

from common.kafka.kafka_config import ConsumerParams
from common.logging import get_logger

log = get_logger(__name__)


class SharedKafkaConsumer:
    __KAFKA_ADMIN_CLIENT: KafkaAdminClient = None
    __KAFKA_SUB_INSTANCES = {}

    @classmethod
    def shutdown(cls):
        if cls.__KAFKA_ADMIN_CLIENT is not None:
            cls.__KAFKA_ADMIN_CLIENT.close()
            cls.__KAFKA_ADMIN_CLIENT = None
        consumer: KafkaConsumer
        for consumer in cls.__KAFKA_SUB_INSTANCES.values():
            consumer.close()
        cls.__KAFKA_SUB_INSTANCES.clear()
        log.info("Kafka consumer shutdown.")

    @classmethod
    def wait_for_kafka(
        cls,
        clientParams: ConsumerParams,
        partitions: int = 1,
        replicas: int = 1
    ) -> bool:
        start_time = time.time()
        while True:
            try:
                if cls.__KAFKA_ADMIN_CLIENT is None:
                    cls.__KAFKA_ADMIN_CLIENT = KafkaAdminClient(
                        bootstrap_servers=clientParams.get_url()
                    )

                topics = cls.__KAFKA_ADMIN_CLIENT.list_topics()
                consumer_topics = []
                for topic in clientParams.topics:
                    if topic.value not in topics:
                        log.debug(f"Creating topic: {topic.value}")
                        cls.__KAFKA_ADMIN_CLIENT.create_topics(
                            new_topics=[NewTopic(topic.value, partitions, replicas)]
                        )
                    consumer_topics.append(topic.value)
                break
            except KafkaError:
                elapsed_time = time.time() - start_time
                if elapsed_time >= clientParams.timeout:
                    log.error("Failed to connect to Kafka after {} seconds.".format(clientParams.timeout))
                    return False
                log.debug("Waiting for Kafka to be ready...")
                time.sleep(5)
        return True

    @classmethod
    def get_consumer(cls, clientParams: ConsumerParams) -> KafkaConsumer:
        cls.wait_for_kafka(clientParams)
        if clientParams.get_key() not in cls.__KAFKA_SUB_INSTANCES:
            consumer_topics = [topic.value for topic in clientParams.topics]
            consumer = KafkaConsumer(
                *consumer_topics,
                bootstrap_servers=clientParams.get_url(),
                group_id=clientParams.group_id.value,
                auto_offset_reset="earliest",
                enable_auto_commit=clientParams.enable_auto_commit
            )
            cls.__KAFKA_SUB_INSTANCES[clientParams.get_key()] = consumer
        return cls.__KAFKA_SUB_INSTANCES[clientParams.get_key()]

    @classmethod
    async def consume_messages_async(
        cls,
        executor: ThreadPoolExecutor,
        consumer_params: ConsumerParams,
        callback: Callable[[ConsumerRecord], Coroutine[Any, Any, bool]],
        commit_batch_size: int,
        commit_batch_interval: int
    ):
        def consume_messages():
            """
            Run the KafkaConsumer in a blocking loop within a dedicated thread.
            """
            consumer = cls.get_consumer(consumer_params)
            batch_offsets = {}  # Store offsets for each partition
            batch_start_time = time.time()
            try:
                message: ConsumerRecord
                for message in consumer:

                    log.debug(f"Received message from: {message.topic}")
                    success = asyncio.run(callback(message))

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
        # asyncio.create_task(consume_messages)
