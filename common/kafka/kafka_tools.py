import json
import time
from typing import Union, Type
from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import KafkaError

__KAFKA_PUB_INSTANCES = {}
__KAFKA_SUB_INSTANCES = {}

def wait_for_kafka(host: str, port: int, timeout: int, client: Union[Type[KafkaConsumer], Type[KafkaProducer]] = KafkaConsumer):
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
            print("Kafka is ready!")
            break
        except KafkaError as e:
            elapsed_time = time.time() - start_time
            if elapsed_time >= timeout:
                print("Failed to connect to Kafka after {} seconds.".format(timeout))
                return False
            print("Waiting for Kafka to be ready...")
            time.sleep(5)
    return True

def get_producer(host: str, port: int, timeout: int) -> KafkaProducer:
    url = f"{host}:{port}"
    print(__KAFKA_PUB_INSTANCES)
    if url not in __KAFKA_PUB_INSTANCES:
        print("waiting for Producer")
        wait_for_kafka(host, port, timeout, KafkaProducer)
        print(__KAFKA_PUB_INSTANCES)
    return __KAFKA_PUB_INSTANCES[url]

def get_consumer(host: str, port: int, timeout: int) -> KafkaConsumer:
    url = f"{host}:{port}"
    print(__KAFKA_SUB_INSTANCES)
    if url not in __KAFKA_SUB_INSTANCES:
        print("Waiting for Consumer")
        wait_for_kafka(host, port, timeout, KafkaConsumer)
        print(__KAFKA_SUB_INSTANCES)
    return __KAFKA_SUB_INSTANCES[url]