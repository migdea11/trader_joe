
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import time
from typing import Dict, Generator
from kafka import KafkaProducer
from kafka.errors import KafkaError

from common.kafka.kafka_config import ProducerParams
from common.logging import get_logger

log = get_logger(__name__)


class KafkaProducerFactory:
    _KAFKA_PUB_INSTANCES: Dict[str, KafkaProducer] = {}

    @classmethod
    def shutdown(cls):
        producer: KafkaProducer
        for producer in cls._KAFKA_PUB_INSTANCES.values():
            producer.close()
        cls._KAFKA_PUB_INSTANCES.clear()
        log.info("Kafka producer shutdown.")

    @classmethod
    def release(cls, producer: KafkaProducer | ProducerParams):
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
        start_time = time.time()
        if clientParams.get_key() in cls._KAFKA_PUB_INSTANCES:
            return True

        while True:
            try:
                producer = KafkaProducer(
                    bootstrap_servers=clientParams.get_url(),
                    value_serializer=lambda v: json.dumps(v).encode('utf-8')
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
    def get_producer(
        cls,
        host: str,
        port: int,
        timeout: int,
        producer_type: ProducerParams.ProducerType = ProducerParams.ProducerType.DEDICATED
    ) -> KafkaProducer:
        clientParams = ProducerParams(host, port, timeout, producer_type=producer_type)
        if clientParams.get_key() not in cls._KAFKA_PUB_INSTANCES:
            log.debug("waiting for Producer")
            cls.wait_for_kafka(clientParams)
        return cls._KAFKA_PUB_INSTANCES[clientParams.get_key()]

    @classmethod
    @contextmanager
    def scoped_producer(
        cls,
        host: str,
        port: int,
        timeout: int,
        producer_type: ProducerParams.ProducerType = ProducerParams.ProducerType.DEDICATED
    ) -> Generator[KafkaProducer, None, None]:
        producer: KafkaProducer = cls.get_producer(host, port, timeout, producer_type, producer_type)
        try:
            yield producer
        finally:
            cls.release(producer)

    @classmethod
    def send_message_async(cls, executor: ThreadPoolExecutor, producer: KafkaProducer, topic: str, message):
        loop = asyncio.get_running_loop()
        loop.run_in_executor(executor, producer.send, topic, message)

    @classmethod
    def flush_messages_async(cls, executor: ThreadPoolExecutor, producer: KafkaProducer):
        loop = asyncio.get_running_loop()
        loop.run_in_executor(executor, producer.flush)
