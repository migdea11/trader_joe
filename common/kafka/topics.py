from enum import Enum
from aenum import Enum as AEnum, extend_enum


class StaticTopic(AEnum):
    STOCK_MARKET_ACTIVITY = "stock_market_activity"
    STOCK_MARKET_QUOTE = "stock_market_quote"
    STOCK_MARKET_TRADE = "stock_market_trade"


class RpcEndpointTopic(str, Enum):
    LATENCY_TEST = "latency_test_rpc"
    STOCK_MARKET_ACTIVITY = "stock_market_activity_rpc"

    @property
    def request(self) -> StaticTopic:
        return StaticTopic(f"{self.value}_request")

    @property
    def response(self) -> StaticTopic:
        return StaticTopic(f"{self.value}_response")


class ConsumerGroup(str, Enum):
    DATA_STORE_GROUP = "data_store_group"
    DATA_INGEST_GROUP = "data_ingest_group"
    COMMON_GROUP = "common_group"


# Extend StaticTopic with RPC endpoints
for rpc_endpoint in RpcEndpointTopic:
    extend_enum(StaticTopic, f'{rpc_endpoint.name}_REQUEST', f'{rpc_endpoint.value}_request')
    extend_enum(StaticTopic, f'{rpc_endpoint.name}_RESPONSE', f'{rpc_endpoint.value}_response')
