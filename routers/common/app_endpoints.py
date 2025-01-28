from enum import Enum

from common.kafka.rpc.kafka_rpc_base import BaseRpcAck, RpcEndpoint
from common.kafka.topics import RpcEndpointTopic
from schemas.common.latency import InternalLatencyRequest


class InterfaceRest(str, Enum):
    PING = "/ping"
    LATENCY = "/latency/{latency_type}"
    INTERNAL_LATENCY = "/latency_internal"


class InterfaceRpc:
    LATENCY = RpcEndpoint(
        RpcEndpointTopic.LATENCY_TEST, InternalLatencyRequest, BaseRpcAck
    )
