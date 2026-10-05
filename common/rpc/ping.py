"""The Ping service: PIPELINE PROOF, NOT A CONTRACT (proto/trader_joe/ping/v1/ping.proto).

It carries proto -> make proto -> committed generated code -> a grpc.aio server and channel end to
end, before any real contract exists. It also shows the seam (ADR tj-8konfu D3): the generated
symbols stay in this package, and callers deal in a ServiceRegistration and plain strings. No service
registers it yet, and nothing should come to depend on it.
"""

import grpc

from common.rpc.channel import unary_call_options
from common.rpc.generated.trader_joe.ping.v1 import ping_pb2, ping_pb2_grpc
from common.rpc.server import ServiceRegistration


SERVICE_NAME = ping_pb2.DESCRIPTOR.services_by_name['PingService'].full_name


class PingServicer(ping_pb2_grpc.PingServiceServicer):
    """Echoes the request's message back."""

    async def Ping(self, request: ping_pb2.PingRequest, context: grpc.aio.ServicerContext) -> ping_pb2.PingResponse:
        """Answer a ping. The method name is the generated base class's, so it is not snake_case.

        Args:
            request: The ping.
            context: The call's context. Unused.

        Returns:
            ping_pb2.PingResponse: The request's message, unchanged.
        """
        return ping_pb2.PingResponse(message=request.message)


def ping_service() -> ServiceRegistration:
    """A registration for GrpcServerHost that serves Ping.

    Returns:
        ServiceRegistration: The Ping servicer, bound to its generated add_*_to_server function.
    """
    servicer = PingServicer()
    return ServiceRegistration(
        name=SERVICE_NAME,
        add_to_server=lambda server: ping_pb2_grpc.add_PingServiceServicer_to_server(servicer, server),
    )


async def ping(channel: grpc.aio.Channel, message: str, timeout_s: float) -> str:
    """Ping a server over the channel, waiting for it up to the deadline (ADR tj-8konfu D6.5, O1).

    Args:
        channel: A channel from common.rpc.channel.create_channel.
        message: The text to echo.
        timeout_s: The call's deadline, in seconds.

    Returns:
        str: The echoed message.

    Raises:
        grpc.aio.AioRpcError: If the call fails, including DEADLINE_EXCEEDED when the server is not
            ready before the deadline.
    """
    stub = ping_pb2_grpc.PingServiceStub(channel)
    response = await stub.Ping(ping_pb2.PingRequest(message=message), **unary_call_options(timeout_s))
    return response.message
