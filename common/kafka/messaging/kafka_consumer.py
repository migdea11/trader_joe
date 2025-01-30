import asyncio
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Coroutine, Dict

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
from common.kafka.topics import StaticTopic
from common.logging import get_logger, limit

log = get_logger(__name__)


class KafkaConsumerFactory:
    """Factory that manages the creation and lifecycle of Kafka consumers."""
    __KAFKA_ADMIN_CLIENT: KafkaAdminClient = None
    _KAFKA_SUB_INSTANCES = []

    def __init__(self):
        self.__async_instances: Dict[str, 'KafkaConsumerFactory.ConsumerControl'] = {}

    @classmethod
    def shutdown(cls):
        """Shutdown all Kafka consumers and clients."""
        if cls.__KAFKA_ADMIN_CLIENT is not None:
            cls.__KAFKA_ADMIN_CLIENT.close()
            cls.__KAFKA_ADMIN_CLIENT = None
        consumer: KafkaConsumer
        for consumer in cls._KAFKA_SUB_INSTANCES:
            consumer.close()
        cls._KAFKA_SUB_INSTANCES.clear()
        log.info("Kafka consumer shutdown.")

    @classmethod
    def release(cls, consumer: KafkaConsumer):
        """Close and remove a Kafka consumer instance.

        Args:
            consumer (KafkaConsumer): The consumer instance to release.
        """
        consumer.close()
        cls._KAFKA_SUB_INSTANCES.remove(consumer)

    @classmethod
    def wait_for_kafka(
        cls,
        clientParams: ConsumerParams,
        partitions: int = 1,
        replicas: int = 1
    ) -> bool:
        """Wait for Kafka to be ready before creating a consumer.

        Args:
            clientParams (ConsumerParams): The consumer parameters.
            partitions (int, optional): Kafka consumer partitions. Defaults to 1.
            replicas (int, optional): Kafka consumer replicas. Defaults to 1.

        Raises:
            ValueError: Unknown topic type (Based on Enum).

        Returns:
            bool: True if Kafka is ready, False otherwise.
        """
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
                        if isinstance(topic, StaticTopic):
                            cls.__KAFKA_ADMIN_CLIENT.create_topics(
                                new_topics=[NewTopic(topic.value, partitions, replicas)]
                            )
                        else:
                            raise ValueError(f"Unknown topic type: {topic}")
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
        """Create a Kafka consumer instance.

        Args:
            clientParams (ConsumerParams): The consumer parameters.

        Returns:
            KafkaConsumer: The Kafka consumer instance.
        """
        cls.wait_for_kafka(clientParams)
        consumer_topics = [topic.value for topic in clientParams.topics]
        consumer = KafkaConsumer(
            *consumer_topics,
            bootstrap_servers=clientParams.get_url(),
            group_id=clientParams.consumer_group.value,
            auto_offset_reset="earliest",
            enable_auto_commit=clientParams.enable_auto_commit
        )
        cls._KAFKA_SUB_INSTANCES.append(consumer)
        return consumer

    class ConsumerControl:
        """Wrapper class used to manage consumer lifecycle and message processing.
        """
        def __init__(
            self,
            executor: ThreadPoolExecutor,
            consumer_params: ConsumerParams,
            callback: Callable[[ConsumerRecord], Coroutine[Any, Any, bool]],
            commit_batch_size: int,
            commit_batch_interval: int
        ):
            """Initialize the ConsumerControl instance.

            Args:
                executor (ThreadPoolExecutor): Consumer thread executor.
                consumer_params (ConsumerParams): Consumer parameters.
                callback (Callable[[ConsumerRecord], Coroutine[Any, Any, bool]]): Message processing callback.
                commit_batch_size (int): Number of messages to process before committing offsets.
                commit_batch_interval (int): Time interval in seconds to commit offsets if batch size not reached.
            """
            self.consumer = KafkaConsumerFactory.get_consumer(consumer_params)
            self.executor = executor
            self.callback = callback
            self.commit_batch_size = commit_batch_size
            self.commit_batch_interval = commit_batch_interval

        def start(self):
            """Start the consumer in a dedicated thread."""
            loop = asyncio.get_event_loop()
            loop.run_in_executor(self.executor, self.__consume_messages)

        def stop(self):
            """Stop the consumer and close the Kafka consumer instance."""
            self.consumer.close()

        def __consume_messages(self):
            """Run the KafkaConsumer in a blocking loop within a dedicated thread."""
            batch_offsets = {}  # Store offsets for each partition
            batch_start_time = time.time()
            try:
                message: ConsumerRecord
                for message in self.consumer:

                    log.debug(f"Received message from: {message.topic}")
                    success = asyncio.run(self.callback(message))

                    if success:
                        # Track the latest offset for this partition
                        partition = TopicPartition(message.topic, message.partition)
                        batch_offsets[partition] = OffsetAndMetadata(message.offset + 1, "")
                    else:
                        log.warning(limit(f"Failed to process message: {message.value}"))

                    # Commit offsets if the batch size is reached for any partition
                    time_limit_reached = time.time() - batch_start_time >= self.commit_batch_interval
                    if len(batch_offsets) >= self.commit_batch_size or (time_limit_reached and batch_offsets):
                        self.consumer.commit(offsets=batch_offsets)
                        batch_start_time = time.time()
                        batch_offsets.clear()

            except Exception as e:
                log.error(f"Error consuming messages, exiting consumer: {e}")
                traceback.print_exc()

    def add_async_consumer(
        self,
        executor: ThreadPoolExecutor,
        consumer_params: ConsumerParams,
        callback: Callable[[ConsumerRecord], Coroutine[Any, Any, bool]],
        commit_batch_size: int,
        commit_batch_interval: int,
    ) -> None:
        """Add a new Kafka consumer instance to the factory.

        Args:
            executor (ThreadPoolExecutor): Consumer thread executor.
            consumer_params (ConsumerParams): Consumer parameters.
            callback (Callable[[ConsumerRecord], Coroutine[Any, Any, bool]]): Message processing callback.
            commit_batch_size (int): Number of messages to process before committing offsets.
            commit_batch_interval (int): Time interval in seconds to commit offsets if batch size not reached.

        Raises:
            ValueError: No topics specified for consumer.
        """
        log.info(
            f"Adding consumer {consumer_params.consumer_group.value} for topics: "
            f"{[topic.value for topic in consumer_params.topics]}"
        )
        if consumer_params.topics is None or len(consumer_params.topics) == 0:
            raise ValueError("No topics specified for consumer")
        control = self.ConsumerControl(executor, consumer_params, callback, commit_batch_size, commit_batch_interval)
        self.__async_instances[consumer_params.get_key()] = control
        control.start()
