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
    def __init__(self, kafka_config: RpcParams, endpoint: RpcEndpoint[Req, Res], timeout: int = 5):
        super().__init__(kafka_config, endpoint, timeout)

        # Update consumer params to listen for responses
        consumer_topic = StaticTopic(self.endpoint.topic.response)
        self._consumer_params.topics = [consumer_topic]

        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._timeout = timeout

    async def _callback(self, message: ConsumerRecord) -> bool:
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
