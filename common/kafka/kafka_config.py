from enum import Enum
from typing import List
from uuid import uuid4 as UUID

from common.environment import get_env_var
from common.kafka.topics import ConsumerGroup, StaticTopic


BROKER_NAME = get_env_var("BROKER_NAME")
BROKER_PORT = get_env_var("BROKER_PORT", cast_type=int)
BROKER_CONN_TIMEOUT = get_env_var("BROKER_CONN_TIMEOUT", cast_type=int)


class ConsumerParams:
    def __init__(
        self,
        host: str,
        port: int,
        topics: List[StaticTopic],
        consumer_group: ConsumerGroup,
        auto_commit: bool,
        timeout: int
    ):
        self.host = host
        self.port = port
        self.topics = topics
        self.consumer_group = consumer_group
        self.enable_auto_commit = auto_commit
        self.timeout = timeout

        self.__url = f"{self.host}:{self.port}"
        self.__key_str = f"{self.__url}+{self.consumer_group}+{'_'.join([t.value for t in self.topics])}"
        self.__key = hash(self.__key_str)

    def get_url(self) -> str:
        return self.__url

    def get_key(self) -> int:
        return self.__key


class ProducerParams:
    class ProducerType(Enum):
        SHARED = 0
        DEDICATED = 1

    def __init__(
        self, host: str, port: int, timeout: int, retry: int = 1, producer_type: ProducerType = ProducerType.DEDICATED
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retry = retry
        self.producer_type = producer_type

        self.__url = f"{self.host}:{self.port}"
        self.__key_str = f"{self.__url}" \
            if producer_type is ProducerParams.ProducerType.SHARED \
            else f"{self.__url}+{UUID()}"
        self.__key = hash(self.__key_str)

    def get_url(self) -> str:
        return self.__url

    def get_key(self) -> int:
        return self.__key


class RpcParams:
    def __init__(self, host: str, port: int, consumer_group: ConsumerGroup):
        self.host = host
        self.port = port
        self.consumer_group = consumer_group

        self.__url = f"{self.host}:{self.port}"
        self.__key_str = f"{self.__url}+{self.consumer_group}"
        self.__key = hash(self.__key_str)

    def get_url(self) -> str:
        return self.__url

    def get_key(self) -> int:
        return self.__key


def get_consumer_params(topics: List[StaticTopic], consumer_group: ConsumerGroup) -> ConsumerParams:
    return ConsumerParams(BROKER_NAME, BROKER_PORT, topics, consumer_group, False, BROKER_CONN_TIMEOUT)


def get_producer_params() -> ProducerParams:
    return ProducerParams(BROKER_NAME, BROKER_PORT, BROKER_CONN_TIMEOUT)

def get_rpc_params(consumer_group: ConsumerGroup) -> RpcParams:
    return RpcParams(BROKER_NAME, BROKER_PORT, consumer_group)
