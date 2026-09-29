"""The Kafka RPC envelope's wire bytes, and the loop ConsumerControl.start demands (tj-vhboky.33).

M2 moved RpcRequest and RpcResponse from a class-based ``Config`` to ``ConfigDict`` and dropped
their ``json_encoders`` entry, on the claim that the entry was inert: keyed on the ``Req``
TypeVar, it never matched a payload type. Nothing pinned the envelope's serialised form, so that
claim rested on a one-off probe. These tests pin it -- exact bytes, and datetime offsets carried
intact through the real server and client decode paths.

The tj-vhboky.24 gate recorded that whatever replaces this envelope must carry offsets intact, and
left the Kafka envelope itself unpinned because it is slated for removal (epic tj-3mk3u5). These
tests are cheap to delete with it; until then they guard the change that touched its config.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from kafka.consumer.fetcher import ConsumerRecord
from pydantic import AwareDatetime, BaseModel

from common.kafka.kafka_config import ConsumerParams, RpcParams
from common.kafka.messaging.kafka_consumer import KafkaConsumerFactory
from common.kafka.rpc.kafka_rpc_base import RpcEndpoint, RpcRequest, RpcResponse
from common.kafka.rpc.kafka_rpc_client import KafkaRpcClient
from common.kafka.rpc.kafka_rpc_server import KafkaRpcServer
from common.kafka.topics import ConsumerGroup, RpcEndpointTopic, StaticTopic


pytestmark = pytest.mark.common

EST = timezone(timedelta(hours=-5))
ACST = timezone(timedelta(hours=9, minutes=30))


class Stamped(BaseModel):
    """Payload with one datetime per offset shape: negative, fractional-hour positive, and UTC."""

    start: AwareDatetime
    end: AwareDatetime
    expiry: AwareDatetime


SENT = Stamped(
    start=datetime(2026, 3, 9, 9, 30, tzinfo=EST),
    end=datetime(2026, 3, 10, 1, 0, tzinfo=ACST),
    expiry=datetime(2026, 3, 9, 14, 30, tzinfo=UTC),
)

PAYLOAD_JSON = '{"start":"2026-03-09T09:30:00-05:00","end":"2026-03-10T01:00:00+09:30","expiry":"2026-03-09T14:30:00Z"}'


def _assert_same_instants_and_offsets(received: Stamped) -> None:
    """Every field names the same instant as SENT and still carries SENT's offset."""
    for field in ('start', 'end', 'expiry'):
        sent_value: datetime = getattr(SENT, field)
        got_value: datetime = getattr(received, field)
        assert got_value == sent_value, field
        assert got_value.utcoffset() == sent_value.utcoffset(), field


def _record(value: str) -> ConsumerRecord:
    """Wrap a serialised envelope in a fake ConsumerRecord, as the consumer thread hands it over."""
    message = MagicMock(spec=ConsumerRecord)
    message.value = value.encode('utf-8')
    return message


# ---------------------------------------------------------------------------
# Exact wire bytes
# ---------------------------------------------------------------------------


def test_a_parametrised_request_serialises_to_the_pinned_bytes():
    """The form the server decodes into, ``RpcRequest[Model]``, keeps every offset verbatim."""
    request = RpcRequest[Stamped](correlation_id=7, payload=SENT)

    assert request.model_dump_json() == '{"correlation_id":7,"payload":' + PAYLOAD_JSON + '}'


def test_the_client_built_request_serialises_to_the_pinned_bytes():
    """``create_request`` is what KafkaRpcClient.send_request puts on the wire."""
    request = RpcRequest.create_request(SENT)

    assert request.model_dump_json() == f'{{"correlation_id":{request.correlation_id},"payload":{PAYLOAD_JSON}}}'


def test_the_server_built_response_serialises_to_the_pinned_bytes():
    """``RpcResponse[Res].create_response`` is what KafkaRpcServer._callback puts on the wire."""
    request = RpcRequest[Stamped](correlation_id=7, payload=SENT)

    response = RpcResponse.create_response(request, SENT)

    assert response.model_dump_json() == '{"correlation_id":7,"payload":' + PAYLOAD_JSON + '}'


# ---------------------------------------------------------------------------
# Offsets through the production decode paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offsets_survive_the_server_and_the_client_decode():
    """A request goes client -> server -> client and every offset arrives as it was sent.

    The server decodes with ``RpcRequest[request_model].model_validate_json`` and encodes its
    reply with ``RpcResponse[Res].create_response(...).model_dump_json()``; the client decodes
    that reply with ``RpcResponse[response_model]``. All four are the production calls: only the
    producer is replaced, so the bytes the server would have sent can be handed to the client.
    """
    received: list[Stamped] = []

    async def rpc_function(payload: Stamped) -> Stamped:
        received.append(payload)
        return payload

    params = RpcParams('kafka.invalid', 9092, ConsumerGroup.COMMON_GROUP)
    endpoint = RpcEndpoint(topic=RpcEndpointTopic.STOCK_MARKET_ACTIVITY, request_model=Stamped, response_model=Stamped)
    server = KafkaRpcServer(params, endpoint, rpc_function)
    server._executor = MagicMock()
    server.producer = MagicMock()
    client = KafkaRpcClient(params, endpoint)

    request = RpcRequest.create_request(SENT)
    with patch('common.kafka.rpc.kafka_rpc_server.KafkaProducerFactory') as producer_factory:
        assert await server._callback(_record(request.model_dump_json())) is True

    _assert_same_instants_and_offsets(received[0])

    _, _, topic, reply = producer_factory.send_message_async.call_args.args
    assert topic == StaticTopic.STOCK_MARKET_ACTIVITY_RESPONSE.value

    future = asyncio.get_running_loop().create_future()
    client._pending_requests[request.correlation_id] = future
    assert await client._callback(_record(reply)) is True

    _assert_same_instants_and_offsets(future.result().payload)


# ---------------------------------------------------------------------------
# ConsumerControl.start fails loud without a running loop
# ---------------------------------------------------------------------------


def test_start_without_a_running_loop_raises_and_starts_no_consumer():
    """A synchronous caller gets RuntimeError, and no consumer thread is started.

    Before M2, ``get_event_loop()`` here depended on thread history. On a thread that had never
    set a loop, Python 3.12 conjured an implicit one with a DeprecationWarning and quietly started
    the consumer. On one where a loop had been set and cleared -- as pytest-asyncio leaves the
    main thread after any async test -- it raised "There is no current event loop". The bead
    ruled that a sync caller must fail loud, always. The message match pins that
    ``get_running_loop`` is what raises, so the test is red in both old regimes, whatever ran
    before it. Every production caller runs inside an async lifespan.
    """
    consumer = MagicMock()
    consumer.__iter__.return_value = iter([])
    consumer_params = ConsumerParams(
        host='kafka.invalid',
        port=9092,
        topics=[StaticTopic.STOCK_MARKET_ACTIVITY],
        consumer_group=ConsumerGroup.DATA_STORE_GROUP,
        timeout=0.1,
        auto_commit=False,
    )

    async def callback(message: ConsumerRecord) -> bool:
        return True

    with (
        patch.object(KafkaConsumerFactory, 'get_consumer', return_value=consumer),
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        control = KafkaConsumerFactory.ConsumerControl(executor, consumer_params, callback, 1, 5)
        with pytest.raises(RuntimeError, match='no running event loop'):
            control.start()

    # The executor has shut down and joined, so a submitted consumer would have iterated by now.
    consumer.__iter__.assert_not_called()
