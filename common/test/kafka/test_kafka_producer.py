import pytest
import asyncio
from unittest.mock import patch, MagicMock
from kafka.errors import KafkaError

from common.kafka.kafka_producer import SharedKafkaProducer
from common.kafka.kafka_config import ProducerParams


@pytest.fixture
def mock_kafka_producer():
    """Fixture to mock KafkaProducer."""
    with patch('common.kafka.kafka_producer.KafkaProducer') as mock_producer:
        mock_instance = MagicMock()
        mock_producer.return_value = mock_instance
        yield mock_producer, mock_instance

    SharedKafkaProducer.shutdown()


def test_shutdown(mock_kafka_producer):
    """Test the shutdown method closes all producers."""
    # Mocking KafkaProducer instances
    _, producer_mock = mock_kafka_producer

    # Add fake producers to the internal dictionary
    SharedKafkaProducer._KAFKA_PUB_INSTANCES = {
        "key1": producer_mock,
        "key2": producer_mock
    }

    SharedKafkaProducer.shutdown()

    # Assertions
    assert len(SharedKafkaProducer._KAFKA_PUB_INSTANCES) == 0
    assert producer_mock.close.call_count == 2


def test_wait_for_kafka_success(mock_kafka_producer):
    """Test wait_for_kafka successfully creates a KafkaProducer."""
    mock_producer_class, _ = mock_kafka_producer
    params = ProducerParams(host='localhost', port=9092, timeout=5)

    result = SharedKafkaProducer.wait_for_kafka(params)

    assert result is True
    assert any(
        call_params.kwargs['bootstrap_servers'] == params.get_url()
        for call_params in mock_producer_class.call_args_list
    )


def test_wait_for_kafka_timeout(mock_kafka_producer):
    """Test wait_for_kafka times out if Kafka is not reachable."""
    mock_producer_class, _ = mock_kafka_producer

    # Simulate KafkaError when attempting connection
    mock_producer_class.side_effect = KafkaError
    params = ProducerParams(host='localhost', port=9092, timeout=1)

    result = SharedKafkaProducer.wait_for_kafka(params)

    assert result is False
    assert len(SharedKafkaProducer._KAFKA_PUB_INSTANCES) == 0


def test_get_producer_reuse(mock_kafka_producer):
    """Test get_producer reuses an existing producer."""
    mock_producer_class, _ = mock_kafka_producer

    params = ProducerParams(host='localhost', port=9092, timeout=5)
    producer_1 = SharedKafkaProducer.get_producer(params.host, params.port, params.timeout)
    producer_2 = SharedKafkaProducer.get_producer(params.host, params.port, params.timeout)

    assert producer_1 is producer_2
    mock_producer_class.assert_called_once()


def test_send_message_async(mock_kafka_producer):
    """Test send_message_async schedules producer.send in an executor."""
    _, producer_mock = mock_kafka_producer
    executor = MagicMock()

    with patch.object(asyncio, 'get_running_loop', return_value=MagicMock()) as mock_loop:
        SharedKafkaProducer.send_message_async(executor, producer_mock, "test_topic", {"key": "value"})
        mock_loop.return_value.run_in_executor.assert_called_once_with(
            executor, producer_mock.send, "test_topic", {"key": "value"}
        )


def test_flush_messages_async(mock_kafka_producer):
    """Test flush_messages_async schedules producer.flush in an executor."""
    _, producer_mock = mock_kafka_producer
    executor = MagicMock()

    with patch.object(asyncio, 'get_running_loop', return_value=MagicMock()) as mock_loop:
        SharedKafkaProducer.flush_messages_async(executor, producer_mock)
        mock_loop.return_value.run_in_executor.assert_called_once_with(
            executor, producer_mock.flush
        )
