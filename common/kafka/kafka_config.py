from typing import List
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
        self.__key = f"{self.__url}+{self.group_id}"

    def get_url(self) -> str:
        return self.__url

    def get_key(self) -> str:
        return self.__key


class ProducerParams:
    def __init__(self, host: str, port: int, timeout: int, retry: int = 1):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retry = retry

        self.__url = f"{self.host}:{self.port}"
        self.__key = f"{self.__url}"

    def get_url(self) -> str:
        return self.__url

    def get_key(self) -> str:
        return self.__key
