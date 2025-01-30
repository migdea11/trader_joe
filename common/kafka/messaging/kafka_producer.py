
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import time
from typing import Dict, Generator
from kafka import KafkaProducer
from kafka.errors import KafkaError

from common.kafka.kafka_config import ProducerParams
from common.logging import get_logger, limit

log = get_logger(__name__)


class KafkaProducerFactory:
    """Factory that manages the creation and lifecycle of Kafka producers."""
    _KAFKA_PUB_INSTANCES: Dict[str, KafkaProducer] = {}

    @classmethod
    def shutdown(cls):
        """Shutdown all Kafka producers."""
        producer: KafkaProducer
        for producer in cls._KAFKA_PUB_INSTANCES.values():
            producer.close()
        cls._KAFKA_PUB_INSTANCES.clear()
        log.info("Kafka producer shutdown.")

    @classmethod
    def release(cls, producer: KafkaProducer | ProducerParams):
        """Close and remove a Kafka producer instance.

        Args:
            producer (KafkaProducer | ProducerParams): The producer instance or parameters of instance to release.
        """
        producer_key = None
        if isinstance(producer, ProducerParams):
            producer_key = producer.get_key()
        else:
            for key, stored_producer in cls._KAFKA_PUB_INSTANCES.items():
                if stored_producer is producer:
                    producer_key = key
                    break

        if producer_key is None:
            producer_instance: KafkaProducer = producer
            log.warning(f"Producer not found in factory: {producer_instance}. Closing anyway")
            return
        else:
            producer_instance = cls._KAFKA_PUB_INSTANCES.pop(producer_key)
        producer_instance.close()

    @classmethod
    def wait_for_kafka(
        cls,
        clientParams: ProducerParams
    ) -> bool:
        """Wait for Kafka to be ready before creating a producer.

        Args:
            clientParams (ProducerParams): The parameters to create the producer.

        Returns:
            bool: True if the producer was created successfully, False otherwise.
        """
        start_time = time.time()
        if clientParams.get_key() in cls._KAFKA_PUB_INSTANCES:
            return True

        while True:
            try:
                producer = KafkaProducer(
                    bootstrap_servers=clientParams.get_url(),
                    value_serializer=lambda v: v.encode('utf-8')
                )
                cls._KAFKA_PUB_INSTANCES[clientParams.get_key()] = producer
                break
            except KafkaError as e:
                elapsed_time = time.time() - start_time
                if elapsed_time >= clientParams.timeout:
                    log.error(f"Failed to connect to Kafka: {e}".format(clientParams.timeout))
                    return False
                log.debug("Waiting for Kafka to be ready...")
                time.sleep(clientParams.retry)
        return True

    @classmethod
    def get_producer(cls, producer_params: ProducerParams) -> KafkaProducer:
        """Get a Kafka producer instance.

        Args:
            producer_params (ProducerParams): The parameters to create the producer.

        Returns:
            KafkaProducer: The Kafka producer instance.
        """
        if producer_params.get_key() not in cls._KAFKA_PUB_INSTANCES:
            log.debug("waiting for Producer")
            cls.wait_for_kafka(producer_params)
        return cls._KAFKA_PUB_INSTANCES[producer_params.get_key()]

    @classmethod
    @contextmanager
    def scoped_producer(cls, producer_params: ProducerParams) -> Generator[KafkaProducer, None, None]:
        """Context manager to get a Kafka producer instance.

        Args:
            producer_params (ProducerParams): The parameters to create the producer.

        Yields:
            Generator[KafkaProducer, None, None]: The Kafka producer instance generator.
        """
        producer: KafkaProducer = cls.get_producer(producer_params)
        try:
            yield producer
        finally:
            cls.release(producer)

    @classmethod
    def send_message_async(cls, executor: ThreadPoolExecutor, producer: KafkaProducer, topic: str, message):
        """Send a message to a Kafka topic asynchronously.

        Args:
            executor (ThreadPoolExecutor): Producer thread pool executor.
            producer (KafkaProducer): The Kafka producer instance.
            topic (str): The Kafka topic to send the message to.
            message (_type_): The message to send.
        """
        log.debug(limit(f"Sending message to topic: {topic}\n{message}"))
        loop = asyncio.get_running_loop()
        loop.run_in_executor(executor, producer.send, topic, message)

    @classmethod
    def flush_messages_async(cls, executor: ThreadPoolExecutor, producer: KafkaProducer):
        """Flush messages in the producer asynchronously.

        Args:
            executor (ThreadPoolExecutor): _description_
            producer (KafkaProducer): _description_
        """
        loop = asyncio.get_running_loop()
        loop.run_in_executor(executor, producer.flush)
