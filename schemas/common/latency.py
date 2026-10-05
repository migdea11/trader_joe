from enum import Enum

from fastapi import Path, Query

from common.kafka.rpc.kafka_rpc_base import BaseRpcAck
from schemas.inbound_contract import InboundContract


LATENCY_TYPE_DESC = 'Type of latency to measure'
LOOP_DESC = 'Number of times to measure latency'
PAYLOAD_SIZE_DESC = 'Size of payload to send'


class LatencyRequest(InboundContract):
    class LatencyType(str, Enum):
        REST = 'rest'
        RPC_KAFKA = 'rpc_kafka'
        GRPC = 'grpc'

    latency_type: LatencyType = Path(..., title='Latency type', description=LATENCY_TYPE_DESC)
    iterations: int | None = Query(..., title='Iterations', description=LOOP_DESC)
    payload_size: int | None = Query(..., title='Payload size', description=PAYLOAD_SIZE_DESC)


class LatencyResponse(BaseRpcAck):
    latency: float


class InternalLatencyRequest(InboundContract):
    payload: str
