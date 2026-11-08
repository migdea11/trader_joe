"""GrpcServerHost.set_service_health: the serving status of a NAMED grpc.health.v1 service.

Bead tj-7zjov1.54 (PR 5 P11b), from the architect ruling (a) on P11 tj-7zjov1.20. A component reports
a degraded part -- the ack-gated executor's stall, in P11c -- on a named health service. The overall
status '' is what the compose healthcheck probes; it stays owned by the host's own start and stop and
is refused here, because a container marked unhealthy invites a restart that loses the very buffer a
stall is holding.

Every case reads the status back through a real Health Check (or Watch) over a loopback GrpcServerHost,
never through the host's private state. Every network await is bounded by GUARD_S, so a regression
fails a test instead of hanging the suite.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

from common.rpc.channel import create_channel
from common.rpc.ping import SERVICE_NAME, ping_service
from common.rpc.server import BindAddress, GrpcServerHost


pytestmark = pytest.mark.common

LOOPBACK = '127.0.0.1'
GUARD_S = 10.0
SERVING = health_pb2.HealthCheckResponse.SERVING
NOT_SERVING = health_pb2.HealthCheckResponse.NOT_SERVING
STALL = 'trader_joe.ingest.delivery.alpaca'
OTHER = 'trader_joe.ingest.delivery.other'


def _loopback_host() -> GrpcServerHost:
    return GrpcServerHost(BindAddress(LOOPBACK, 0), [ping_service()])


@asynccontextmanager
async def _probe(host: GrpcServerHost) -> AsyncIterator[health_pb2_grpc.HealthStub]:
    """A health stub on a channel to a started host; the channel is closed on the way out."""
    channel = create_channel(f'{LOOPBACK}:{host.port}')
    try:
        yield health_pb2_grpc.HealthStub(channel)
    finally:
        await channel.close()


async def _status(stub: health_pb2_grpc.HealthStub, service: str) -> int:
    response = await asyncio.wait_for(stub.Check(health_pb2.HealthCheckRequest(service=service), timeout=5), GUARD_S)
    return response.status


# ---------------------------------------------------------------------------------------------------
# A NAMED SERVICE READS BACK THE STATUS SET


@pytest.mark.asyncio
@pytest.mark.parametrize(('serving', 'expected'), [(False, NOT_SERVING), (True, SERVING)], ids=['stalled', 'serving'])
async def test_a_named_service_reads_back_the_status_set(serving: bool, expected: int):
    async with _loopback_host() as host, _probe(host) as stub:
        await host.set_service_health(STALL, serving)
        assert await _status(stub, STALL) == expected


@pytest.mark.asyncio
async def test_a_stall_then_a_recovery_reads_back_each_in_turn():
    """False then True: the status is not latched at the first value set."""
    async with _loopback_host() as host, _probe(host) as stub:
        await host.set_service_health(STALL, False)
        assert await _status(stub, STALL) == NOT_SERVING
        await host.set_service_health(STALL, True)
        assert await _status(stub, STALL) == SERVING
        await host.set_service_health(STALL, False)
        assert await _status(stub, STALL) == NOT_SERVING


@pytest.mark.asyncio
async def test_an_unknown_name_is_not_found_until_set_and_then_answers_the_status_set():
    """A name no servicer was registered under is created by the set, not refused."""
    async with _loopback_host() as host, _probe(host) as stub:
        with pytest.raises(grpc.aio.AioRpcError) as raised:
            await _status(stub, STALL)
        assert raised.value.code() == grpc.StatusCode.NOT_FOUND
        await host.set_service_health(STALL, False)
        assert await _status(stub, STALL) == NOT_SERVING


@pytest.mark.asyncio
async def test_a_watch_on_a_named_service_sees_each_transition():
    async with _loopback_host() as host, _probe(host) as stub:
        await host.set_service_health(STALL, True)
        watch = stub.Watch(health_pb2.HealthCheckRequest(service=STALL))
        try:
            assert (await asyncio.wait_for(watch.read(), GUARD_S)).status == SERVING
            await host.set_service_health(STALL, False)
            assert (await asyncio.wait_for(watch.read(), GUARD_S)).status == NOT_SERVING
            await host.set_service_health(STALL, True)
            assert (await asyncio.wait_for(watch.read(), GUARD_S)).status == SERVING
        finally:
            watch.cancel()


# ---------------------------------------------------------------------------------------------------
# THE OVERALL STATUS '' IS NEVER A COMPONENT'S


@pytest.mark.asyncio
@pytest.mark.parametrize('serving', [True, False], ids=['serving', 'stalled'])
async def test_the_empty_name_is_refused_and_the_overall_status_stays_serving(serving: bool):
    async with _loopback_host() as host, _probe(host) as stub:
        with pytest.raises(ValueError, match="''"):
            await host.set_service_health('', serving)
        assert await _status(stub, '') == SERVING
        assert await _status(stub, SERVICE_NAME) == SERVING


@pytest.mark.asyncio
async def test_the_empty_name_is_refused_before_start_too_and_never_applied_at_start():
    host = _loopback_host()
    with pytest.raises(ValueError, match="''"):
        await host.set_service_health('', False)
    async with host, _probe(host) as stub:
        assert await _status(stub, '') == SERVING


@pytest.mark.asyncio
async def test_a_named_stall_leaves_the_overall_status_and_every_other_name_unchanged():
    async with _loopback_host() as host, _probe(host) as stub:
        await host.set_service_health(OTHER, True)
        await host.set_service_health(STALL, False)
        assert await _status(stub, STALL) == NOT_SERVING
        assert await _status(stub, '') == SERVING
        assert await _status(stub, SERVICE_NAME) == SERVING
        assert await _status(stub, OTHER) == SERVING


@pytest.mark.asyncio
async def test_a_registered_services_own_name_can_be_set_and_flips_only_that_name():
    """Setting a registered service's own name overwrites that service's SERVING, and only that name.

    PINS CURRENT BEHAVIOUR FOR THE ARCHITECT GATE. The design (tj-7zjov1.54) neither requires nor forbids it, and
    the builder chose not to refuse it; if the architect rules it must be refused, this test inverts.
    Either way the overall '' is untouched.
    """
    async with _loopback_host() as host, _probe(host) as stub:
        await host.set_service_health(SERVICE_NAME, False)
        assert await _status(stub, SERVICE_NAME) == NOT_SERVING
        assert await _status(stub, '') == SERVING


# ---------------------------------------------------------------------------------------------------
# BEFORE START AND AFTER STOP


@pytest.mark.asyncio
async def test_a_status_set_before_start_is_in_force_once_the_server_serves():
    """And the registered services still come up SERVING beside it, as does the overall status."""
    host = _loopback_host()
    await host.set_service_health(STALL, False)
    await host.set_service_health(OTHER, True)
    async with host, _probe(host) as stub:
        assert await _status(stub, STALL) == NOT_SERVING
        assert await _status(stub, OTHER) == SERVING
        assert await _status(stub, SERVICE_NAME) == SERVING
        assert await _status(stub, '') == SERVING


@pytest.mark.asyncio
async def test_the_last_status_set_before_start_wins():
    host = _loopback_host()
    await host.set_service_health(STALL, False)
    await host.set_service_health(STALL, True)
    async with host, _probe(host) as stub:
        assert await _status(stub, STALL) == SERVING


@pytest.mark.asyncio
async def test_a_set_after_stop_does_not_raise_and_is_not_served():
    host = _loopback_host()
    await host.start()
    port = host.port
    await host.stop()
    await host.set_service_health(STALL, False)
    channel = create_channel(f'{LOOPBACK}:{port}')
    try:
        stub = health_pb2_grpc.HealthStub(channel)
        with pytest.raises(grpc.aio.AioRpcError):
            await asyncio.wait_for(stub.Check(health_pb2.HealthCheckRequest(service=STALL), timeout=0.5), GUARD_S)
    finally:
        await channel.close()
