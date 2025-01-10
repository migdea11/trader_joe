import pytest
import asyncio
from unittest.mock import patch, MagicMock
from kafka.errors import KafkaError

from common.kafka.kafka_producer import KafkaProducerFactory
from common.kafka.kafka_config import ProducerParams


@pytest.fixture
def mock_kafka_producer():
    """Fixture to mock KafkaProducer."""
    with patch('common.kafka.kafka_producer.KafkaProducer') as mock_producer:
        mock_instance = MagicMock()
        mock_producer.return_value = mock_instance
        yield mock_producer, mock_instance

    KafkaProducerFactory.shutdown()


@pytest.fixture
def producer_params():
    """Fixture to create mock ProducerParams."""
    return ProducerParams(
        host="localhost",
        port=9092,
        timeout=0.1,
        producer_type=ProducerParams.ProducerType.DEDICATED
    )


def test_shutdown(mock_kafka_producer):
    """Test the shutdown method closes all producers."""
    # Mocking KafkaProducer instances
    _, producer_mock = mock_kafka_producer

    # Add fake producers to the internal dictionary
    KafkaProducerFactory._KAFKA_PUB_INSTANCES = {
        "key1": producer_mock,
        "key2": producer_mock
    }

    KafkaProducerFactory.shutdown()

    # Assertions
    assert len(KafkaProducerFactory._KAFKA_PUB_INSTANCES) == 0
    assert producer_mock.close.call_count == 2


def test_release(mock_kafka_producer, producer_params):
    # Add the mock producer to the factory's cache
    _, producer_mock = mock_kafka_producer
    dummy_producer_mock = MagicMock()

    # Add fake producers to the internal dictionary
    KafkaProducerFactory._KAFKA_PUB_INSTANCES = {
        "key1": producer_mock,
        "key2": dummy_producer_mock
    }

    KafkaProducerFactory.release(producer_mock)

    producer_mock.close.assert_called_once()
    assert producer_params.get_key() not in KafkaProducerFactory._KAFKA_PUB_INSTANCES
    assert dummy_producer_mock.close.call_count == 0


@patch("common.kafka.kafka_producer.KafkaProducer")
def test_scoped_producer(mock_producer, mock_kafka_producer, producer_params: ProducerParams):
    """Test the scoped_producer context manager to ensure proper resource cleanup."""
    # Mock the factory's get_producer method to return the mock producer
    with patch.object(KafkaProducerFactory, "get_producer", return_value=mock_producer):
        with patch.object(KafkaProducerFactory, "release") as mock_release:
            # Use the scoped_producer context manager
            with KafkaProducerFactory.scoped_producer(
                host=producer_params.host,
                port=producer_params.port,
                timeout=producer_params.timeout
            ) as producer:
                # Assert that the producer was obtained
                assert producer is mock_producer

            # Assert the release method was called
            mock_release.assert_called_once_with(mock_producer)


def test_wait_for_kafka_success(mock_kafka_producer, producer_params: ProducerParams):
    """Test wait_for_kafka successfully creates a KafkaProducer."""
    mock_producer_class, _ = mock_kafka_producer
    result = KafkaProducerFactory.wait_for_kafka(producer_params)

    assert result is True
    assert any(
        call_params.kwargs['bootstrap_servers'] == producer_params.get_url()
        for call_params in mock_producer_class.call_args_list
    )


def test_wait_for_kafka_timeout(mock_kafka_producer, producer_params: ProducerParams):
    """Test wait_for_kafka times out if Kafka is not reachable."""
    mock_producer_class, _ = mock_kafka_producer

    # Simulate KafkaError when attempting connection
    mock_producer_class.side_effect = KafkaError
    result = KafkaProducerFactory.wait_for_kafka(producer_params)

    assert result is False
    assert len(KafkaProducerFactory._KAFKA_PUB_INSTANCES) == 0


def test_get_producer_reuse(mock_kafka_producer):
    """Test get_producer reuses an existing producer."""
    mock_producer_class, _ = mock_kafka_producer

    params = ProducerParams(host='localhost', port=9092, timeout=5, producer_type=ProducerParams.ProducerType.SHARED)
    producer_1 = KafkaProducerFactory.get_producer(params.host, params.port, params.timeout, params.producer_type)
    producer_2 = KafkaProducerFactory.get_producer(params.host, params.port, params.timeout, params.producer_type)

    assert producer_1 is producer_2
    mock_producer_class.assert_called_once()


def test_send_message_async(mock_kafka_producer):
    """Test send_message_async schedules producer.send in an executor."""
    _, producer_mock = mock_kafka_producer
    executor = MagicMock()

    with patch.object(asyncio, 'get_running_loop', return_value=MagicMock()) as mock_loop:
        KafkaProducerFactory.send_message_async(executor, producer_mock, "test_topic", {"key": "value"})
        mock_loop.return_value.run_in_executor.assert_called_once_with(
            executor, producer_mock.send, "test_topic", {"key": "value"}
        )


def test_flush_messages_async(mock_kafka_producer):
    """Test flush_messages_async schedules producer.flush in an executor."""
    _, producer_mock = mock_kafka_producer
    executor = MagicMock()

    with patch.object(asyncio, 'get_running_loop', return_value=MagicMock()) as mock_loop:
        KafkaProducerFactory.flush_messages_async(executor, producer_mock)
        mock_loop.return_value.run_in_executor.assert_called_once_with(
            executor, producer_mock.flush
        )
