import asyncio
import pytest
from unittest.mock import MagicMock, patch
from kafka.admin import NewTopic
from kafka import KafkaConsumer, KafkaAdminClient
from kafka.consumer.fetcher import ConsumerRecord
from concurrent.futures import ThreadPoolExecutor

from common.kafka.topics import StaticTopic, ConsumerGroup
from common.kafka.kafka_config import ConsumerParams
from common.kafka.kafka_consumer import KafkaConsumerFactory


@pytest.fixture
def consumer_params():
    """Fixture to create mock ConsumerParams."""
    return ConsumerParams(
        host="localhost",
        port=9092,
        topics=[StaticTopic.STOCK_MARKET_ACTIVITY],
        group_id=ConsumerGroup.DATA_STORE_GROUP,
        timeout=0.1,
        auto_commit=False
    )


def test_release():
    """Test the release method to ensure a consumer is closed and removed from the list."""
    # Add mock consumer to the instance list
    mock_consumer = MagicMock(spec=KafkaConsumer)
    KafkaConsumerFactory._KAFKA_SUB_INSTANCES.append(mock_consumer)

    # Call release
    KafkaConsumerFactory.release(mock_consumer)

    # Assert consumer was closed and removed
    mock_consumer.close.assert_called_once()
    assert mock_consumer not in KafkaConsumerFactory._KAFKA_SUB_INSTANCES


@patch("common.kafka.kafka_consumer.KafkaConsumer")
def test_consume_messages_async(mock_kafka_consumer, consumer_params):
    """Test the consume_messages_async method to ensure it processes messages correctly."""
    # Mock the consumer
    mock_consumer_instance = MagicMock(spec=KafkaConsumer)
    mock_kafka_consumer.return_value = mock_consumer_instance

    # Mock a ConsumerRecord
    mock_message = MagicMock(spec=ConsumerRecord)
    mock_message.topic = "test-topic"
    mock_message.partition = 0
    mock_message.offset = 10

    # Add the mock message to the consumer's iterator
    mock_consumer_instance.__iter__.return_value = [mock_message]

    # Mock callback
    async def mock_callback(message):
        assert message is mock_message
        return True

    # Run the function with a ThreadPoolExecutor
    async def test_async_consume():
        with ThreadPoolExecutor() as executor:
            await KafkaConsumerFactory.consume_messages_async(
                executor=executor,
                consumer_params=consumer_params,
                callback=mock_callback,
                commit_batch_size=1,
                commit_batch_interval=5
            )

    # Run the async test
    asyncio.run(test_async_consume())

    # Assert message was processed
    mock_consumer_instance.commit.assert_called_once_with(offsets={})


@patch("common.kafka.kafka_consumer.KafkaConsumer")
def test_consume_messages_async_failed_callback(mock_kafka_consumer, consumer_params):
    """Test consume_messages_async with a callback that fails to process a message."""
    # Mock the consumer
    mock_consumer_instance = MagicMock(spec=KafkaConsumer)
    mock_kafka_consumer.return_value = mock_consumer_instance

    # Mock a ConsumerRecord
    mock_message = MagicMock(spec=ConsumerRecord)
    mock_message.topic = "test-topic"
    mock_message.partition = 0
    mock_message.offset = 10

    # Add the mock message to the consumer's iterator
    mock_consumer_instance.__iter__.return_value = [mock_message]

    # Mock callback
    async def mock_callback_failure(message):
        return False

    # Run the function with a ThreadPoolExecutor
    async def test_async_consume():
        with ThreadPoolExecutor() as executor:
            await KafkaConsumerFactory.consume_messages_async(
                executor=executor,
                consumer_params=consumer_params,
                callback=mock_callback_failure,
                commit_batch_size=1,
                commit_batch_interval=5
            )

    # Run the async test
    asyncio.run(test_async_consume())

    # Assert commit was not called because the message failed processing
    mock_consumer_instance.commit.assert_not_called()


@patch("common.kafka.kafka_consumer.NewTopic")
@patch("common.kafka.kafka_consumer.KafkaAdminClient")
def test_wait_for_kafka(mock_admin_client, mock_new_topic, consumer_params: ConsumerParams):
    """Test the wait_for_kafka method to ensure topics are created if missing."""
    # Mock the admin client
    mock_admin_instance = MagicMock(spec=KafkaAdminClient)
    mock_admin_client.return_value = mock_admin_instance
    mock_admin_instance.list_topics.return_value = []

    # Mock the NewTopic class
    mock_new_topic_instance = MagicMock(spec=NewTopic)
    mock_new_topic.return_value = mock_new_topic_instance

    # Call the wait_for_kafka method
    result = KafkaConsumerFactory.wait_for_kafka(consumer_params)

    # Assert topics were created
    assert result is True
    mock_new_topic.assert_called_once_with(StaticTopic.STOCK_MARKET_ACTIVITY.value, 1, 1)
    mock_admin_instance.create_topics.assert_called_once_with(new_topics=[mock_new_topic_instance])
