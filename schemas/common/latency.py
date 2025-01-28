from enum import Enum
from typing import Optional
from fastapi import Path, Query
from pydantic import BaseModel

from common.kafka.rpc.kafka_rpc_base import BaseRpcAck


LATENCY_TYPE_DESC = "Type of latency to measure"
LOOP_DESC = "Number of times to measure latency"
PAYLOAD_SIZE_DESC = "Size of payload to send"


class LatencyRequest(BaseModel):
    class LatencyType(str, Enum):
        REST = "rest"
        RPC_KAFKA = "rpc_kafka"

    latency_type: LatencyType = Path(..., title="Latency type", description=LATENCY_TYPE_DESC)
    iterations: Optional[int] = Query(..., title="Iterations", description=LOOP_DESC)
    payload_size: Optional[int] = Query(..., title="Payload size", description=PAYLOAD_SIZE_DESC)


class LatencyResponse(BaseRpcAck):
    latency: float


class InternalLatencyRequest(BaseModel):
    payload: str
