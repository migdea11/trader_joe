from enum import Enum


class StaticTopic(str, Enum):
    STOCK_MARKET_ACTIVITY = "stock_market_activity"
    STOCK_MARKET_QUOTE = "stock_market_quote"
    STOCK_MARKET_TRADE = "stock_market_trade"


class DynamicTopic(str, Enum):
    LATER = "none"


TopicTyping = StaticTopic | DynamicTopic


class ConsumerGroup(str, Enum):
    DATA_STORE_GROUP = "data_store_group"
