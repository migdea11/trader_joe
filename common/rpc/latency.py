"""The latency harness's gRPC arm (proto/trader_joe/proto/internal/latency/v1/latency.proto).

MEASUREMENT TOOLING, NOT A DATA CONTRACT. routers/common/latency.py times this call beside its REST and
Kafka-RPC arms (tj-3mk3u5.8), and the user records p50 and p99 for all three (tj-3mk3u5.26). This module
is the arm's seam (ADR tj-8konfu D3): the generated symbols stay in this package, and the harness deals
in a ServiceRegistration, a LatencyProbeClient and a plain string payload.

The servicer acknowledges a probe without reading its payload, as the other two arms' servers do, so
the three timings differ by transport and not by the work done on arrival. It does nothing blocking,
because grpc.aio runs every handler on the event loop (ADR tj-8konfu D2).

THE PAYLOAD CEILING. A probe is one message, so the shared 4 MiB limit (common.rpc.config, D6.3) bounds
it: the hex payload doubles payload_size KB, so a probe above about 2047 KB fails with
RESOURCE_EXHAUSTED. That is the limit doing its job, not a defect in the arm.
"""

import grpc

from common.rpc.channel import unary_call_options
from common.rpc.server import ServiceRegistration
from trader_joe.proto.internal.latency.v1 import latency_pb2, latency_pb2_grpc


SERVICE_NAME = latency_pb2.DESCRIPTOR.services_by_name['LatencyService'].full_name


class LatencyServicer(latency_pb2_grpc.LatencyServiceServicer):
    """Acknowledges a probe without reading its payload."""

    async def Probe(
        self, request: latency_pb2.ProbeRequest, context: grpc.aio.ServicerContext
    ) -> latency_pb2.ProbeResponse:
        """Acknowledge a probe. The method name is the generated base class's, so it is not snake_case.

        Args:
            request: The probe. Its payload is not read.
            context: The call's context. Unused.

        Returns:
            latency_pb2.ProbeResponse: The empty acknowledgement.
        """
        return latency_pb2.ProbeResponse()


def latency_service() -> ServiceRegistration:
    """A registration for GrpcServerHost that serves the latency probe.

    Returns:
        ServiceRegistration: The latency servicer, bound to its generated add_*_to_server function.
    """
    servicer = LatencyServicer()
    return ServiceRegistration(
        name=SERVICE_NAME,
        add_to_server=lambda server: latency_pb2_grpc.add_LatencyServiceServicer_to_server(servicer, server),
    )


class LatencyProbeClient:
    """Sends probes over one channel, each bounded by the same per-call deadline.

    Build it once and reuse it: the stub and the call options are made here, so a timed probe measures
    the call and nothing else, as the other two arms build their clients before they time anything.
    """

    def __init__(self, channel: grpc.aio.Channel, timeout_s: float) -> None:
        """Bind the client to a channel and a deadline.

        Args:
            channel: A channel from common.rpc.channel.create_channel. The caller owns it.
            timeout_s: Each probe's deadline, in seconds. Each probe waits for the peer up to it
                (ADR tj-8konfu D6.5, O1).

        Raises:
            ValueError: If the deadline is not a finite, positive number of seconds.
        """
        self._stub = latency_pb2_grpc.LatencyServiceStub(channel)
        self._call_options = unary_call_options(timeout_s)

    async def probe(self, payload: str) -> None:
        """Send one probe and wait for its acknowledgement.

        Args:
            payload: The text to carry, unread by the server.

        Raises:
            grpc.aio.AioRpcError: If the call fails: DEADLINE_EXCEEDED when the server is not ready
                before the deadline, RESOURCE_EXHAUSTED when the probe exceeds the message limit.
        """
        await self._stub.Probe(latency_pb2.ProbeRequest(payload=payload), **self._call_options)
