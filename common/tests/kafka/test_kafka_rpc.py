import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kafka.consumer.fetcher import ConsumerRecord
from pydantic import BaseModel

from common.kafka.rpc.kafka_rpc_base import RpcEndpoint, RpcRequest
from common.kafka.rpc.kafka_rpc_client import KafkaRpcClient
from common.kafka.rpc.kafka_rpc_server import KafkaRpcServer
from common.kafka.topics import StaticTopic


class SampleRequest(BaseModel):
    """Minimal request payload for exercising the RPC callbacks."""

    value: str


def _build_server(rpc_function) -> KafkaRpcServer:
    """Build a KafkaRpcServer without touching Kafka.

    ``KafkaRpcBase.__init__`` builds a real producer and consumer, so the
    instance is created directly and given only the attributes ``_callback``
    reads. The method under test is the real, unpatched production one.

    Args:
        rpc_function: Coroutine function invoked with the decoded payload.

    Returns:
        KafkaRpcServer: Server whose ``_callback`` can be awaited directly.
    """
    server = object.__new__(KafkaRpcServer)
    server.endpoint = RpcEndpoint(
        topic=SimpleNamespace(response=StaticTopic.STOCK_MARKET_ACTIVITY), request_model=SampleRequest
    )
    server._rpc_function = rpc_function
    server._executor = MagicMock()
    server.producer = MagicMock()
    return server


def _message(payload: SampleRequest) -> ConsumerRecord:
    """Wrap a payload in an RpcRequest and hand back a fake ConsumerRecord."""
    request = RpcRequest.create_request(payload)
    message = MagicMock(spec=ConsumerRecord)
    message.value = request.model_dump_json().encode('utf-8')
    return message


@pytest.mark.asyncio
@patch('common.kafka.rpc.kafka_rpc_server.KafkaProducerFactory')
async def test_server_callback_returns_true_on_success(mock_producer_factory):
    """A handled request reports success so the consumer commits its offset."""

    async def rpc_function(payload):
        return payload

    server = _build_server(rpc_function)

    assert await server._callback(_message(SampleRequest(value='ok'))) is True
    mock_producer_factory.send_message_async.assert_called_once()
    mock_producer_factory.flush_messages_async.assert_called_once()


@pytest.mark.asyncio
async def test_server_callback_swallows_exception_and_returns_false():
    """An ordinary Exception stays caught and reports failure, not a raise.

    This is the half of the B012 fix that must NOT change: the consumer loop
    relies on a False return to skip the offset rather than die.
    """

    async def rpc_function(payload):
        raise ValueError('handler blew up')

    server = _build_server(rpc_function)

    assert await server._callback(_message(SampleRequest(value='boom'))) is False


@pytest.mark.asyncio
async def test_server_callback_propagates_cancelled_error():
    """CancelledError must escape rather than be reported as a failed message.

    Regression guard for the B012 fix (tj-isypb9). The callback previously
    ended in ``finally: return success``, and a ``return`` inside ``finally``
    discards the in-flight exception -- so this BaseException was swallowed
    and turned into a False return. It must now propagate.
    """

    async def rpc_function(payload):
        raise asyncio.CancelledError

    server = _build_server(rpc_function)

    with pytest.raises(asyncio.CancelledError):
        await server._callback(_message(SampleRequest(value='cancelled')))


@pytest.mark.asyncio
async def test_client_callback_swallows_exception_and_returns_false():
    """The client callback reports failure on a malformed response body."""
    client = object.__new__(KafkaRpcClient)
    client.endpoint = RpcEndpoint(
        topic=SimpleNamespace(response=StaticTopic.STOCK_MARKET_ACTIVITY),
        request_model=SampleRequest,
        response_model=SampleRequest,
    )
    client._pending_requests = {}

    message = MagicMock(spec=ConsumerRecord)
    message.value = b'not valid json'

    assert await client._callback(message) is False
