
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time
from kafka import KafkaProducer
from kafka.errors import KafkaError

from common.kafka.kafka_config import ProducerParams
from common.logging import get_logger

log = get_logger(__name__)


class SharedKafkaProducer:
    _KAFKA_PUB_INSTANCES = {}

    @classmethod
    def shutdown(cls):
        producer: KafkaProducer
        for producer in cls._KAFKA_PUB_INSTANCES.values():
            producer.close()
        cls._KAFKA_PUB_INSTANCES.clear()
        log.info("Kafka producer shutdown.")

    @classmethod
    def wait_for_kafka(
        cls,
        clientParams: ProducerParams
    ) -> bool:
        start_time = time.time()
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
    def get_producer(cls, host: str, port: int, timeout: int) -> KafkaProducer:
        clientParams = ProducerParams(host, port, timeout)
        if clientParams.get_key() not in cls._KAFKA_PUB_INSTANCES:
            log.debug("waiting for Producer")
            cls.wait_for_kafka(clientParams)
        return cls._KAFKA_PUB_INSTANCES[clientParams.get_key()]

    @classmethod
    def send_message_async(cls, executor: ThreadPoolExecutor, producer: KafkaProducer, topic: str, message):
        loop = asyncio.get_running_loop()
        loop.run_in_executor(executor, producer.send, topic, message)

    @classmethod
    def flush_messages_async(cls, executor: ThreadPoolExecutor, producer: KafkaProducer):
        loop = asyncio.get_running_loop()
        loop.run_in_executor(executor, producer.flush)
