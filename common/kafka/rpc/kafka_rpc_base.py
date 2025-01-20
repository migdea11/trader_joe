from abc import ABC, abstractmethod
from enum import Enum
import json
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
        return RpcRequest[Req](correlation_id=UUID().int, payload=payload)


class RpcResponse(BaseModel, Generic[Res]):
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
        return RpcResponse[Res](correlation_id=request.correlation_id, payload=payload)


class BaseRpcAck(BaseModel):
    class Success(str, Enum):
        SUCCESS = "success"
        FAILED = "failed"
    success: 'BaseRpcAck.Success' = Success.SUCCESS
    error: Optional[str] = None


class BaseRpcPageAck(BaseRpcAck):
    page: int
    total_pages: int


class RpcEndpoint(Generic[Req, Res]):
    def __init__(
        self, topic: RpcEndpointTopic, request_model: Type[Req], response_model: Type[Res] = BaseRpcAck
    ):
        self.topic = topic
        self.request_model = request_model
        self.response_model = response_model


class KafkaRpcBase(Generic[Req, Res], ABC):
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
    async def _callback(self, message: ConsumerRecord) -> None:
        ...

    def initialize(self, factory: KafkaConsumerFactory) -> 'KafkaConsumerFactory.ConsumerControl':
        return factory.add_async_consumer(
            self._executor,
            self._consumer_params,
            self._callback,
            self._commit_batch_size,
            self._commit_batch_interval
        )
