import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest
from kafka.consumer.fetcher import ConsumerRecord
from pydantic import BaseModel

from common.kafka.kafka_config import RpcParams
from common.kafka.rpc.kafka_rpc_base import RpcEndpoint, RpcRequest
from common.kafka.rpc.kafka_rpc_client import KafkaRpcClient
from common.kafka.rpc.kafka_rpc_server import KafkaRpcServer
from common.kafka.topics import ConsumerGroup, RpcEndpointTopic, StaticTopic


class SampleRequest(BaseModel):
    """Minimal request payload for exercising the RPC callbacks."""

    value: str


@pytest.fixture
def no_kafka_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every KAFKA_* variable, so a constructor that reads one cannot pass.

    The constructor takes its host and port from the RpcParams argument rather than the
    environment, so today this changes nothing. It is here to keep it that way: the bug
    this module guards (tj-vs32kk) was an import-time environment read, and a future
    ``__init__`` that reaches for os.environ should fail here rather than in a container.
    """
    for name in [key for key in os.environ if key.startswith('KAFKA_')]:
        monkeypatch.delenv(name, raising=False)


def _endpoint(response_model: type[BaseModel] | None = None) -> RpcEndpoint:
    """Build an endpoint on a real RpcEndpointTopic.

    A real topic rather than a stand-in, because ``KafkaRpcServer.__init__`` resolves
    ``endpoint.topic.request`` and the client resolves ``endpoint.topic.response`` through
    ``StaticTopic``. Both of those run only when the real constructor does.

    Args:
        response_model: Response type, or None for the endpoint default.

    Returns:
        RpcEndpoint: Endpoint pointing at the stock market activity RPC topic pair.
    """
    if response_model is None:
        return RpcEndpoint(topic=RpcEndpointTopic.STOCK_MARKET_ACTIVITY, request_model=SampleRequest)
    return RpcEndpoint(
        topic=RpcEndpointTopic.STOCK_MARKET_ACTIVITY, request_model=SampleRequest, response_model=response_model
    )


def _rpc_params() -> RpcParams:
    """Return RpcParams naming a host and port that no broker is listening on."""
    return RpcParams('kafka.invalid', 9092, ConsumerGroup.COMMON_GROUP)


def _build_server(rpc_function) -> KafkaRpcServer:
    """Build a KafkaRpcServer through its real constructor.

    This deliberately does NOT use ``object.__new__``. Until tj-vs32kk,
    ``KafkaRpcBase.__init__`` built a Kafka producer eagerly, so a test could not call it
    without a reachable broker and the constructor went entirely unexercised. It no longer
    does, and running every test in this module through the real ``__init__`` is what makes
    reintroducing that eager build break the file rather than slip through.

    The producer is assigned afterwards because the real constructor leaves it None on
    purpose -- that invariant is asserted directly in
    ``test_constructor_leaves_the_producer_unbuilt``, not assumed here.

    Args:
        rpc_function: Coroutine function invoked with the decoded payload.

    Returns:
        KafkaRpcServer: Server whose ``_callback`` can be awaited directly.
    """
    server = KafkaRpcServer(_rpc_params(), _endpoint(), rpc_function)
    server._executor = MagicMock()
    server.producer = MagicMock()
    return server


def _message(payload: SampleRequest) -> ConsumerRecord:
    """Wrap a payload in an RpcRequest and hand back a fake ConsumerRecord."""
    request = RpcRequest.create_request(payload)
    message = MagicMock(spec=ConsumerRecord)
    message.value = request.model_dump_json().encode('utf-8')
    return message


# ---------------------------------------------------------------------------
# Constructor and initialize() -- tj-vs32kk
# ---------------------------------------------------------------------------


def _new_server() -> KafkaRpcServer:
    """Construct a server through the real __init__, with no callback wired up."""
    return KafkaRpcServer(_rpc_params(), _endpoint(), None)


def _new_client() -> KafkaRpcClient:
    """Construct a client through the real __init__."""
    return KafkaRpcClient(_rpc_params(), _endpoint())


@pytest.mark.usefixtures('no_kafka_env')
@pytest.mark.parametrize('build', [_new_server, _new_client], ids=['server', 'client'])
def test_constructor_leaves_the_producer_unbuilt(build):
    """Constructing an RPC endpoint must not build a producer or contact Kafka.

    tj-vs32kk. ``KafkaRpcBase.__init__`` used to call
    ``KafkaProducerFactory.get_producer``, which blocks in ``wait_for_kafka``. Because
    ``routers/data_ingest/get_dataset_request.py`` applies ``@rpc.add_server`` at module
    scope, that made importing ``data.ingest.app.main`` require a live broker and a
    populated KAFKA_* environment -- it died with "invalid literal for int() with base 10:
    'None'". Construction must stay inert.
    """
    with patch('common.kafka.rpc.kafka_rpc_base.KafkaProducerFactory') as producer_factory:
        rpc = build()

    assert rpc.producer is None
    producer_factory.get_producer.assert_not_called()


@pytest.mark.usefixtures('no_kafka_env')
def test_constructor_keeps_the_producer_params_for_later():
    """__init__ resolves the producer parameters even though it defers the producer.

    The parameters have to be resolved eagerly and kept: ``ProducerParams`` defaults to
    ``ProducerType.DEDICATED``, whose cache key embeds a fresh UUID, so rebuilding them in
    ``initialize()`` would key a different producer on every call.
    """
    server = _new_server()

    assert server._producer_params.host == 'kafka.invalid'
    assert server._producer_params.port == 9092


@pytest.mark.usefixtures('no_kafka_env')
def test_initialize_builds_the_producer_from_the_stored_params():
    """initialize() is what builds the producer, using the params __init__ resolved."""
    server = _new_server()
    sentinel = MagicMock(name='producer')

    with patch('common.kafka.rpc.kafka_rpc_base.KafkaProducerFactory') as producer_factory:
        producer_factory.get_producer.return_value = sentinel
        server.initialize(MagicMock())

    assert server.producer is sentinel
    producer_factory.get_producer.assert_called_once_with(server._producer_params)


@pytest.mark.usefixtures('no_kafka_env')
def test_initialize_reuses_the_same_params_object_every_time():
    """A second initialize() must ask for the producer with the identical params object.

    ``KafkaProducerFactory`` caches by ``ProducerParams.get_key()``, and a DEDICATED key
    embeds a UUID generated per params instance. Passing the stored instance is therefore
    what makes a repeat call idempotent rather than a producer leak.
    """
    server = _new_server()

    with patch('common.kafka.rpc.kafka_rpc_base.KafkaProducerFactory') as producer_factory:
        server.initialize(MagicMock())
        server.initialize(MagicMock())

    first, second = producer_factory.get_producer.call_args_list
    assert first.args[0] is second.args[0] is server._producer_params


@pytest.mark.usefixtures('no_kafka_env')
def test_initialize_builds_the_producer_before_creating_the_consumer():
    """The producer must exist before the consumer that drives _callback is created.

    This is the ordering the deferral rests on. ``KafkaRpcServer._callback`` reads
    ``self.producer`` with no None guard, and the only thing that calls it is the consumer
    ``initialize()`` hands ``self._callback`` to. If the two lines in ``initialize()`` were
    ever reordered, the first message to arrive would hit ``AttributeError: 'NoneType'
    object has no attribute 'send'`` inside a worker thread instead of failing here.
    """
    server = _new_server()
    producer_when_consumer_created = []

    factory = MagicMock()
    factory.add_async_consumer.side_effect = lambda *args, **kwargs: producer_when_consumer_created.append(
        server.producer
    )

    with patch('common.kafka.rpc.kafka_rpc_base.KafkaProducerFactory') as producer_factory:
        producer_factory.get_producer.return_value = MagicMock(name='producer')
        server.initialize(factory)

    factory.add_async_consumer.assert_called_once()
    assert producer_when_consumer_created == [server.producer]
    assert producer_when_consumer_created[0] is not None


@pytest.mark.usefixtures('no_kafka_env')
def test_constructor_subscribes_each_side_to_its_own_topic():
    """The server consumes requests and the client consumes responses.

    Consumer parameters are the other half of what ``__init__`` sets up, and no test
    reached them while the constructor was being bypassed.
    """
    server = _new_server()
    client = _new_client()

    assert server._consumer_params.topics == [StaticTopic.STOCK_MARKET_ACTIVITY_REQUEST]
    assert client._consumer_params.topics == [StaticTopic.STOCK_MARKET_ACTIVITY_RESPONSE]


# ---------------------------------------------------------------------------
# Callback behaviour -- tj-isypb9
# ---------------------------------------------------------------------------


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
    client = KafkaRpcClient(_rpc_params(), _endpoint(response_model=SampleRequest))

    message = MagicMock(spec=ConsumerRecord)
    message.value = b'not valid json'

    assert await client._callback(message) is False
