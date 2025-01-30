from abc import ABC, abstractmethod
from enum import Enum
from typing import Generic, Optional, Type, TypeVar
from uuid import uuid4 as UUID

from kafka.consumer.fetcher import ConsumerRecord
from pydantic import BaseModel

from common.kafka.kafka_config import ConsumerParams, ProducerParams, RpcParams
from common.kafka.messaging.kafka_consumer import KafkaConsumerFactory
from common.kafka.messaging.kafka_producer import KafkaProducerFactory
from common.kafka.topics import RpcEndpointTopic
from common.logging import get_logger
from common.worker_pool import SharedWorkerPool

log = get_logger(__name__)

Req = TypeVar("Req", bound=BaseModel)
Res = TypeVar("Res", bound=BaseModel)


class RpcRequest(BaseModel, Generic[Req]):
    """Internal wrapper class for RPC requests.

    Args:
        BaseModel: Pydantic BaseModel.
        Generic (Req): Request type.
    """
    correlation_id: int
    payload: Req

    class Config:
        # Required for Pydantic to validate generic models
        arbitrary_types_allowed = True
        json_encoders = {
            Req: lambda x: x.dict()
        }

    @staticmethod
    def create_request(payload: Req) -> "RpcRequest[Req]":
        """Create a new RPC request.

        Args:
            payload (Req): The request payload.

        Returns:
            RpcRequest[Req]: wrapped RPC request to be sent.
        """
        return RpcRequest[Req](correlation_id=UUID().int, payload=payload)


class RpcResponse(BaseModel, Generic[Res]):
    """Internal wrapper class for RPC responses.

    Args:
        BaseModel: Pydantic BaseModel.
        Generic (Req): Response type.
    """
    correlation_id: int
    payload: Res

    class Config:
        # Required for Pydantic to validate generic models
        arbitrary_types_allowed = True
        json_encoders = {
            Req: lambda x: x.dict()
        }

    @staticmethod
    def create_response(request: RpcRequest, payload: Res) -> "RpcResponse[Res]":
        """Create a new RPC response.

        Args:
            request (RpcRequest): Request that is being Responded to.
            payload (Res): The response payload.

        Returns:
            RpcResponse[Res]: wrapped RPC response to be sent.
        """
        return RpcResponse[Res](correlation_id=request.correlation_id, payload=payload)


class BaseRpcAck(BaseModel):
    """Basic RPC Response"""
    class Success(str, Enum):
        SUCCESS = "success"
        FAILED = "failed"
    success: 'BaseRpcAck.Success' = Success.SUCCESS
    error: Optional[str] = None


class BaseRpcPageAck(BaseRpcAck):
    """Basic RPC Response with pagination"""
    page: int
    total_pages: int


class RpcEndpoint(Generic[Req, Res]):
    """RPC Endpoint Definition

    Args:
        Generic (Req, Res): Request and Response types.
    """
    def __init__(
        self, topic: RpcEndpointTopic, request_model: Type[Req], response_model: Type[Res] = BaseRpcAck
    ):
        """Create a new RPC Endpoint Definition.

        Args:
            topic (RpcEndpointTopic): Topic used for the RPC endpoint.
            request_model (Type[Req]): Request type
            response_model (Type[Res], optional): Response type. Defaults to BaseRpcAck.
        """
        self.topic = topic
        self.request_model = request_model
        self.response_model = response_model


class KafkaRpcBase(Generic[Req, Res], ABC):
    """Base class for Kafka RPC clients and servers.

    Args:
        Generic (Req, Res): Request and Response types.
    """
    def __init__(self, kafka_config: RpcParams, endpoint: RpcEndpoint[Req, Res], timeout: int):
        self.endpoint = endpoint
        producer_params = ProducerParams(kafka_config.host, kafka_config.port, timeout)
        self.producer = KafkaProducerFactory.get_producer(producer_params)
        self._consumer_params = ConsumerParams(
            kafka_config.host,
            kafka_config.port,
            [],
            kafka_config.consumer_group,
            False,
            timeout
        )
        self._commit_batch_size = 10
        self._commit_batch_interval = 10

        self._executor = SharedWorkerPool.get_instance()

    @abstractmethod
    async def _callback(self, message: ConsumerRecord) -> bool:
        """Callback function for processing incoming messages.

        Args:
            message (ConsumerRecord): The raw incoming message.

        Returns:
            bool: True if the message was processed successfully.
        """
        ...

    def initialize(self, factory: KafkaConsumerFactory) -> 'KafkaConsumerFactory.ConsumerControl':
        """Initialize the RPC endpoint client/server.

        Args:
            factory (KafkaConsumerFactory): Kafka consumer factory, used to create a new consumer.

        Returns:
            KafkaConsumerFactory.ConsumerControl: Consumer control instance.
        """
        return factory.add_async_consumer(
            self._executor,
            self._consumer_params,
            self._callback,
            self._commit_batch_size,
            self._commit_batch_interval
        )
