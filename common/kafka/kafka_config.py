from enum import Enum
from typing import List
from uuid import uuid4 as UUID
from common.kafka.topics import ConsumerGroup, TopicTyping


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
        self.__key_str = f"{self.__url}+{self.group_id}+{'_'.join(self.topics)}"
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
