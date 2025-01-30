import asyncio
import traceback
from typing import Dict, Generic, Type

from kafka.consumer.fetcher import ConsumerRecord

from common.kafka.kafka_config import RpcParams
from common.kafka.messaging.kafka_producer import KafkaProducerFactory
from common.kafka.rpc.kafka_rpc_base import KafkaRpcBase, RpcEndpoint, RpcRequest, Req, Res, RpcResponse
from common.kafka.topics import StaticTopic
from common.logging import get_logger, limit

log = get_logger(__name__)


class KafkaRpcClient(Generic[Req, Res], KafkaRpcBase[Req, Res]):
    """RPC client that sends requests and listens for responses.

    Args:
        Generic (Req, Res): Request and response types.
        KafkaRpcBase (Req, Res): Base class for RPC clients.
    """
    def __init__(self, kafka_config: RpcParams, endpoint: RpcEndpoint[Req, Res], timeout: int = 5):
        """Create a new RPC client.

        Args:
            kafka_config (RpcParams): Kafka configuration parameters.
            endpoint (RpcEndpoint[Req, Res]): RPC endpoint definition.
            timeout (int, optional): Timeout period for request and to consume response. Defaults to 5.
        """
        super().__init__(kafka_config, endpoint, timeout)

        # Update consumer params to listen for responses
        consumer_topic = StaticTopic(self.endpoint.topic.response)
        self._consumer_params.topics = [consumer_topic]

        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._timeout = timeout

    async def _callback(self, message: ConsumerRecord) -> bool:
        """Process a response message.

        Args:
            message (ConsumerRecord): The raw incoming message.

        Returns:
            bool: True if the message was processed successfully.
        """
        success = False
        try:
            response_type: Type[Res] = self.endpoint.response_model
            message_value: bytes = message.value
            response = RpcResponse[response_type].model_validate_json(message_value.decode('utf-8'))

            if response.correlation_id in self._pending_requests:
                self._pending_requests[response.correlation_id].set_result(response)
            else:
                log.warning(limit(f"Received unexpected response: {response.model_dump_json()}"))
            success = True
        except Exception as e:
            log.error(limit(f"Error processing response: {e}"))
            traceback.print_exc()
        finally:
            return success

    async def send_request(self, request_payload: Req) -> Res:
        """Send an RPC request and wait for a response.

        Args:
            request_payload (Req): The request payload.

        Raises:
            TimeoutError: If the request times out.

        Returns:
            Res: The response payload.
        """
        request = RpcRequest.create_request(request_payload)
        future = asyncio.get_event_loop().create_future()
        self._pending_requests[request.correlation_id] = future
        topic: StaticTopic = self.endpoint.topic.request
        KafkaProducerFactory.send_message_async(
            self._executor, self.producer, topic.value, request.model_dump_json()
        )
        try:
            response: RpcResponse[Res] = await asyncio.wait_for(future, timeout=self._timeout)
            return response.payload
        except asyncio.TimeoutError:
            raise TimeoutError(f"RPC request timed out after {self._timeout} seconds")
        finally:
            self._pending_requests.pop(request.correlation_id)
