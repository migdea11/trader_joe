from enum import Enum
from typing import List
from uuid import uuid4 as UUID

from common.environment import get_env_var
from common.kafka.topics import ConsumerGroup, StaticTopic


BROKER_NAME = get_env_var("BROKER_NAME")
BROKER_PORT = get_env_var("BROKER_PORT", cast_type=int)
BROKER_CONN_TIMEOUT = get_env_var("BROKER_CONN_TIMEOUT", cast_type=int)


class ConsumerParams:
    """Consumer parameters for Kafka."""
    def __init__(
        self,
        host: str,
        port: int,
        topics: List[StaticTopic],
        consumer_group: ConsumerGroup,
        auto_commit: bool,
        timeout: int
    ):
        """Create a new ConsumerParams object.

        Args:
            host (str): Kafka broker host.
            port (int): Kafka broker port.
            topics (List[StaticTopic]): List of topics to consume.
            consumer_group (ConsumerGroup): Consumer group.
            auto_commit (bool): Enable auto commit.
            timeout (int): Timeout period for Kafka consumer.
        """
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
        """Get the Kafka broker URL.

        Returns:
            str: Kafka broker URL.
        """
        return self.__url

    def get_key(self) -> int:
        """Unique identifier for Consumer based on config.

        Returns:
            int: Hash key.
        """
        return self.__key


class ProducerParams:
    """Producer parameters for Kafka."""
    class ProducerType(Enum):
        SHARED = 0
        DEDICATED = 1

    def __init__(
        self, host: str, port: int, timeout: int, retry: int = 1, producer_type: ProducerType = ProducerType.DEDICATED
    ):
        """Create a new ProducerParams object.

        Args:
            host (str): Kafka broker host.
            port (int): Kafka broker port.
            timeout (int): Timeout period for Kafka producer.
            retry (int, optional): Attempts to send message. Defaults to 1.
            producer_type (ProducerType, optional): Select dedicated producer or shared instance. Defaults to ProducerType.DEDICATED.
        """  # noqa: E501
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retry = retry
        self.producer_type = producer_type

        self.__url = f"{self.host}:{self.port}"
        # Unique key for producer based on config, including dedicated producers which could have same config as
        # another.
        self.__key_str = f"{self.__url}" \
            if producer_type is ProducerParams.ProducerType.SHARED \
            else f"{self.__url}+{UUID()}"
        self.__key = hash(self.__key_str)

    def get_url(self) -> str:
        """Get the Kafka broker URL.

        Returns:
            str: Kafka broker URL.
        """
        return self.__url

    def get_key(self) -> int:
        """Unique identifier for Producer based on config.

        Returns:
            int: Hash key.
        """
        return self.__key


class RpcParams:
    """RPC parameters for Kafka."""
    def __init__(self, host: str, port: int, consumer_group: ConsumerGroup):
        """Create a new RpcParams object.

        Args:
            host (str): Kafka broker host.
            port (int): Kafka broker port.
            consumer_group (ConsumerGroup): Consumer group.
        """
        self.host = host
        self.port = port
        self.consumer_group = consumer_group

        self.__url = f"{self.host}:{self.port}"
        self.__key_str = f"{self.__url}+{self.consumer_group}"
        self.__key = hash(self.__key_str)

    def get_url(self) -> str:
        """Get the Kafka broker URL.

        Returns:
            str: Kafka broker URL.
        """
        return self.__url

    def get_key(self) -> int:
        """Unique identifier for RPC based on config

        Returns:
            int: Hash key.
        """
        return self.__key


def get_consumer_params(topics: List[StaticTopic], consumer_group: ConsumerGroup) -> ConsumerParams:
    """Get the consumer parameters for default Kafka broker.

    Args:
        topics (List[StaticTopic]): List of topics to consume.
        consumer_group (ConsumerGroup): Consumer group.

    Returns:
        ConsumerParams: Consumer parameters.
    """
    return ConsumerParams(BROKER_NAME, BROKER_PORT, topics, consumer_group, False, BROKER_CONN_TIMEOUT)


def get_producer_params() -> ProducerParams:
    """Get the producer parameters for default Kafka broker.

    Returns:
        ProducerParams: Producer parameters.
    """
    return ProducerParams(BROKER_NAME, BROKER_PORT, BROKER_CONN_TIMEOUT)


def get_rpc_params(consumer_group: ConsumerGroup) -> RpcParams:
    """Get the RPC parameters for default Kafka broker.

    Args:
        consumer_group (ConsumerGroup): Consumer group.

    Returns:
        RpcParams: RPC parameters.
    """
    return RpcParams(BROKER_NAME, BROKER_PORT, consumer_group)
