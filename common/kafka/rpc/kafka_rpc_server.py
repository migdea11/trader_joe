import traceback
from typing import Any, Callable, Coroutine, Generic, Type

from kafka.consumer.fetcher import ConsumerRecord

from common.kafka.kafka_config import RpcParams
from common.kafka.messaging.kafka_producer import KafkaProducerFactory
from common.kafka.rpc.kafka_rpc_base import KafkaRpcBase, RpcEndpoint, RpcRequest, Req, Res, RpcResponse
from common.kafka.topics import StaticTopic
from common.logging import get_logger, limit

log = get_logger(__name__)


class KafkaRpcServer(Generic[Req, Res], KafkaRpcBase[Req, Res]):
    """RPC server that listens for requests and sends responses.

    Args:
        Generic (Req, Res): Request and response types.
        KafkaRpcBase (Req, Res): Base class for RPC servers.
    """
    def __init__(
        self,
        kafka_config: RpcParams,
        endpoint: RpcEndpoint[Req, Res],
        rpc_function: Callable[[RpcRequest], Coroutine[Any, Any, Res]],
        timeout: int = 5
    ):
        """Create a new RPC server.

        Args:
            kafka_config (RpcParams): Kafka configuration parameters.
            endpoint (RpcEndpoint[Req, Res]): RPC endpoint definition.
            rpc_function (Callable[[RpcRequest], Coroutine[Any, Any, Res]]): The function to process incoming requests.
            timeout (int, optional): Timeout period to consume request. Defaults to 5.
        """
        super().__init__(kafka_config, endpoint, timeout)

        # Update consumer params to listen for requests
        consumer_topic = StaticTopic(self.endpoint.topic.request)
        self._consumer_params.topics = [consumer_topic]

        self._rpc_function = rpc_function

    async def _callback(self, message: ConsumerRecord) -> bool:
        """Process an incoming request message.

        Args:
            message (ConsumerRecord): The raw incoming message.

        Returns:
            bool: True if the message was processed successfully.
        """
        success = False
        try:
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
            success = True
        except Exception as e:
            log.error(limit(f"Error processing response: {e}"))
            traceback.print_exc()
        finally:
            return success
