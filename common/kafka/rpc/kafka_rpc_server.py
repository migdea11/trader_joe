from typing import Any, Callable, Coroutine, Generic, Type

from kafka.consumer.fetcher import ConsumerRecord

from common.kafka.kafka_config import RpcParams
from common.kafka.messaging.kafka_producer import KafkaProducerFactory
from common.kafka.rpc.kafka_rpc_base import KafkaRpcBase, RpcEndpoint, RpcRequest, Req, Res, RpcResponse
from common.kafka.topics import StaticTopic
from common.logging import get_logger

log = get_logger(__name__)


class KafkaRpcServer(Generic[Req, Res], KafkaRpcBase[Req, Res]):
    def __init__(
        self,
        kafka_config: RpcParams,
        endpoint: RpcEndpoint[Req, Res],
        rpc_function: Callable[[RpcRequest], Coroutine[Any, Any, Res]],
        timeout: int = 5
    ):
        super().__init__(kafka_config, endpoint, timeout)

        # Update consumer params to listen for requests
        consumer_topic = StaticTopic(self.endpoint.topic.request)
        self._consumer_params.topics = [consumer_topic]

        self._rpc_function = rpc_function

    async def _callback(self, message: ConsumerRecord) -> None:
        request_type: Type[Req] = self.endpoint.request_model
        message_value: bytes = message.value
        request = RpcRequest[request_type].model_validate_json(message_value.decode('utf-8'))

        response = await self._rpc_function(request.payload)

        topic: StaticTopic = self.endpoint.topic.response
        payload = RpcResponse[Res].create_response(request, response)
        KafkaProducerFactory.send_message_async(
            self._executor, self.producer, topic.value, payload.model_dump_json()
        )
        KafkaProducerFactory.flush_messages_async(self._executor, self.producer)
