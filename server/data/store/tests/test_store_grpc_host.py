"""data_store's gRPC host for StoreService and its place in the lifespan (tj-7zjov1.27, P34).

THE DESIGN: app/grpc_host.py and app_depends.lifespan as the bead and the store CLAUDE.md delivery section put them:
  * the host is BUILT FIRST, from APP_INTERNAL_GRPC_HOST and APP_INTERNAL_GRPC_PORT, with no default: a missing one
    fails startup naming the variable before ANYTHING else has started (no database, no loop, no channel);
  * it is ENTERED LAST, after the database, the loops, the ingest channel and the refresh, so every dependency of a
    delivery is up before ingest can reach it, and it EXITS FIRST, before the refresh, the channel and the database
    are taken down, so an in-flight delivery ends whole against a live database;
  * it serves StoreService through the delivery intake, and the standard health service reports StoreService under
    its own fully qualified name (what P34b's compose healthcheck and CI peer-reach step read).

The host and the health check run for real over loopback; the delivery path through it reaches a recording handler,
since what the intake writes is test_delivery_intake.py's.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

from common.enums.data_stock import Feed
from common.rpc.channel import create_channel
from common.rpc.clients.store_delivery import GrpcStoreDeliveryClient
from common.rpc.server import GRPC_HOST_ENV, GRPC_PORT_ENV, BindAddress, GrpcServerHost
from common.rpc.store import SERVICE_NAME
from data.store.app import app_depends, grpc_host
from data.store.app.ingest.delivery_intake import DeliveryIntake
from data.store.tests.delivery_reports import report
from schemas.data_ingest.delivery import DeliverAttemptResponse, DeliveryVerdict, RegisterInstanceRequest


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

LOOPBACK = '127.0.0.1'
GUARD_S = 10.0
STORE_SERVICE = 'trader_joe.proto.internal.store.v1.StoreService'


# --------------------------------------------------------------------------------------------- the lifespan


class _Order:
    """Every lifespan step this test can see, in the order it happened."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self.steps: list[str] = []
        order = self.steps

        class FakeDatabase:
            async def initialize(self) -> None:
                order.append('database.initialize')

            async def shutdown(self) -> None:
                order.append('database.shutdown')

        class FakeChannel:
            async def close(self) -> None:
                order.append('channel.close')

        class FakePool:
            @staticmethod
            def worker_startup() -> None:
                order.append('pool.startup')

            @staticmethod
            def worker_shutdown() -> None:
                order.append('pool.shutdown')

        def step(name, result=None):
            def record(*_args, **_kwargs):
                order.append(name)
                return result

            return record

        def async_step(name, result=None):
            async def record(*_args, **_kwargs):
                order.append(name)
                return result

            return record

        monkeypatch.setattr(app_depends, '__INGEST_CHANNEL', None)
        monkeypatch.setattr(app_depends, '__INGEST_FETCH_CLIENT', None)
        monkeypatch.setattr(app_depends, '__INGEST_SUBMIT_CLIENT', None)
        monkeypatch.setattr(app_depends, 'database', FakeDatabase())
        monkeypatch.setattr(app_depends, 'SharedWorkerPool', FakePool)
        monkeypatch.setattr(app_depends, 'initialize_latency_client', step('latency_client'))
        monkeypatch.setattr(app_depends, 'init_debugger', step('debugger'))
        monkeypatch.setattr(app_depends, 'startup_logs', step('startup_logs'))
        monkeypatch.setattr(app_depends, 'teardown_logs', step('teardown_logs'))
        monkeypatch.setattr(app_depends, 'sweep_interrupted_runs', async_step('sweep'))
        monkeypatch.setattr(app_depends, 'start_usage_loops', step('usage.start', []))
        monkeypatch.setattr(app_depends, 'stop_usage_loops', async_step('usage.stop'))
        monkeypatch.setattr(app_depends, 'start_reaper_loop', step('reaper.start'))
        monkeypatch.setattr(app_depends, 'stop_reaper_loop', async_step('reaper.stop'))
        monkeypatch.setattr(app_depends, 'target_from_env', lambda _name: 'ingest:1')
        monkeypatch.setattr(
            app_depends, 'create_channel', lambda _target: (order.append('channel.open'), FakeChannel())[1]
        )
        monkeypatch.setattr(app_depends, 'GrpcIngestFetchClient', step('fetch_client', object()))
        # The push path's seam (tj-7zjov1.28): the refresh and the instance watcher are handed this client.
        self.submit_client = object()
        self.handed: dict[str, object] = {}
        monkeypatch.setattr(app_depends, 'GrpcIngestSubmitClient', step('submit_client', self.submit_client))

        async def refresh_start(client):
            order.append('refresh.start')
            self.handed['refresh'] = client
            return []

        def watch_start(client):
            order.append('watch.start')
            self.handed['watch'] = client

        monkeypatch.setattr(app_depends, 'start_daily_refresh_loops', refresh_start)
        monkeypatch.setattr(app_depends, 'stop_daily_refresh_loops', async_step('refresh.stop'))
        monkeypatch.setattr(app_depends, 'start_instance_watch_loop', watch_start)
        monkeypatch.setattr(app_depends, 'stop_instance_watch_loop', async_step('watch.stop'))


class _FakeHost:
    def __init__(self, order: list[str]):
        self.order = order

    async def __aenter__(self):
        self.order.append('grpc.enter')
        return self

    async def __aexit__(self, *exc):
        self.order.append('grpc.exit')
        return False


@pytest.mark.parametrize(('missing', 'present'), [(GRPC_HOST_ENV, GRPC_PORT_ENV), (GRPC_PORT_ENV, GRPC_HOST_ENV)])
async def test_a_missing_bind_variable_fails_startup_naming_it_before_anything_starts(monkeypatch, missing, present):
    order = _Order(monkeypatch)
    monkeypatch.delenv(missing, raising=False)
    monkeypatch.setenv(present, LOOPBACK if present == GRPC_HOST_ENV else '50052')

    with pytest.raises(ValueError, match=missing):
        async with app_depends.lifespan(object()):
            pytest.fail('the lifespan started without a bind address')

    assert order.steps == [], f'something started before the bind address was checked: {order.steps}'


async def test_the_host_is_entered_last_and_exited_first(monkeypatch):
    order = _Order(monkeypatch)
    built: list[str] = []
    monkeypatch.setattr(app_depends, 'build_grpc_host', lambda: (built.append('built'), _FakeHost(order.steps))[1])

    async with app_depends.lifespan(object()):
        order.steps.append('serving')

    steps = order.steps
    assert built == ['built']
    assert steps.index('grpc.enter') == steps.index('serving') - 1, 'the host is the last thing entered'
    assert steps.index('grpc.exit') == steps.index('serving') + 1, 'the host is the first thing exited'
    for before in (
        'database.initialize',
        'sweep',
        'usage.start',
        'reaper.start',
        'channel.open',
        'submit_client',
        'refresh.start',
        'watch.start',
    ):
        assert steps.index(before) < steps.index('grpc.enter'), before
    for after in ('watch.stop', 'refresh.stop', 'channel.close', 'reaper.stop', 'usage.stop', 'database.shutdown'):
        assert steps.index('grpc.exit') < steps.index(after), after
    # The loops that submit are built on the channel's client and stop before the channel closes.
    assert steps.index('channel.open') < steps.index('submit_client') < steps.index('refresh.start')
    assert steps.index('watch.stop') < steps.index('channel.close')
    assert steps.index('refresh.stop') < steps.index('channel.close')
    assert order.handed == {'refresh': order.submit_client, 'watch': order.submit_client}
    assert app_depends.get_ingest_submit_client() is order.submit_client


async def test_the_host_is_built_before_anything_starts(monkeypatch):
    order = _Order(monkeypatch)

    def build():
        assert order.steps == [], f'built after {order.steps}'
        order.steps.append('built')
        return _FakeHost(order.steps)

    monkeypatch.setattr(app_depends, 'build_grpc_host', build)

    async with app_depends.lifespan(object()):
        pass

    assert order.steps[0] == 'built'


# --------------------------------------------------------------------------------------------- the host


async def test_build_grpc_host_reads_the_bind_address_from_the_environment(monkeypatch):
    monkeypatch.setenv(GRPC_HOST_ENV, LOOPBACK)
    monkeypatch.setenv(GRPC_PORT_ENV, '50999')

    host = grpc_host.build_grpc_host(DeliveryIntake(chunk_size=10))

    assert isinstance(host, GrpcServerHost)


async def test_the_store_service_name_is_the_contracts():
    assert SERVICE_NAME == STORE_SERVICE
    (registration,) = grpc_host.registered_services(DeliveryIntake(chunk_size=10))
    assert registration.name == STORE_SERVICE


class _RecordingIntake:
    def __init__(self):
        self.delivered: list[object] = []
        self.registered: list[RegisterInstanceRequest] = []

    async def deliver(self, attempt):
        self.delivered.append(attempt)
        return DeliverAttemptResponse(verdict=DeliveryVerdict.CONTINUE, attempt_count=4)

    async def register_instance(self, request):
        self.registered.append(request)


async def test_the_host_serves_store_service_and_reports_it_healthy_under_its_own_name():
    intake = _RecordingIntake()
    host = GrpcServerHost(BindAddress(LOOPBACK, 0), grpc_host.registered_services(intake))

    async with host:
        channel = create_channel(f'{LOOPBACK}:{host.port}')
        try:
            health = health_pb2_grpc.HealthStub(channel)
            for name in (STORE_SERVICE, ''):
                response = await asyncio.wait_for(
                    health.Check(health_pb2.HealthCheckRequest(service=name), timeout=5), GUARD_S
                )
                assert response.status == health_pb2.HealthCheckResponse.SERVING, name

            client = GrpcStoreDeliveryClient(channel, deadline_s=5, register_deadline_s=5)
            attempt = report('6f1c2b3a-0d4e-4f5a-9b8c-7d6e5f4a3b2c')
            answer = await asyncio.wait_for(client.deliver(attempt), GUARD_S)
            await asyncio.wait_for(
                client.register_instance(
                    RegisterInstanceRequest(instance_id='instance-a', started_at=datetime(2026, 1, 1, tzinfo=UTC))
                ),
                GUARD_S,
            )
        finally:
            await channel.close()

    assert answer == DeliverAttemptResponse(verdict=DeliveryVerdict.CONTINUE, attempt_count=4)
    (received,) = intake.delivered
    assert received.header.request_id == attempt.header.request_id
    assert received.header.feed is Feed.IEX
    assert [request.instance_id for request in intake.registered] == ['instance-a']
