"""data_ingest hosts a grpc.aio server in its lifespan, beside the Kafka RPC server (tj-3mk3u5.24).

The design is bead tj-3mk3u5.24 with its architect notes -- N3, binding: the host is entered only as
'async with' around the lifespan's yield, so it stops on every exit path; the bind variables have no
default -- ADR tj-8konfu D2 (grpc.aio) and D6.5 (the standard health service), and decision tj-j4wknb
INJECTION (a servicer is handed the readers create_app received, never a broker class).

Pinned here, through the production lifespan (main.create_app -> app_depends.make_lifespan):

* health answers SERVING on the configured port while the app is up, and nothing answers after;
* grpc_host.registered_services() is the one registration point: what it returns is what the
  lifespan's host serves, and it is handed the readers create_app received;
* the latency arm is registered there when LATENCY_TEST_ENABLED is on and nothing is registered --
  and no generated module is even imported -- when it is off (tj-3mk3u5.60);
* the bind address is read when the host is built, never once at import;
* an unset variable stops startup before Kafka, the worker pool or the readers are touched;
* a gRPC start that fails (port taken, wildcard host) still runs the Kafka teardown, and the process
  then exits instead of hanging on a consumer thread;
* order: the host starts after the Kafka RPC servers and before the ready log, and stops before the
  Kafka RPC servers shut down, while the readers are still installed.

Kafka is replaced at its public entry points (wait_for_kafka, the ingest RPC factory's init_servers)
and the latency server at app_depends' own name for it, as test_read_seam.py's LifespanProbe does.
The gRPC host is the real one on 127.0.0.1 (grpc_bind.LoopbackGrpc). LoopbackGrpc fails any test
whose lifespan left a host serving, then stops it, so that regression is red in every test here (and
in the re-pointed lifespan tests of test_read_seam.py and test_fake_read.py) instead of hanging the run.
"""

import asyncio
import logging
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Final
from unittest.mock import Mock, patch

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

from common.enums.data_stock import DataSource
from common.kafka.messaging.kafka_consumer import KafkaConsumerFactory
from common.kafka.messaging.kafka_producer import KafkaProducerFactory
from common.rpc.channel import create_channel
from common.rpc.latency import LatencyProbeClient
from common.rpc.ping import SERVICE_NAME as PING_SERVICE_NAME
from common.rpc.ping import ping, ping_service
from common.rpc.server import GRPC_HOST_ENV, GRPC_PORT_ENV
from common.tests.image_path import image_pythonpath
from common.worker_pool import SharedWorkerPool
from data.ingest.app import app_depends, grpc_host, ingest_control, main
from data.ingest.app.brokers.interface import BarsQuery, BarsResponse
from data.ingest.tests.grpc_bind import GUARD_S, LOOPBACK, LoopbackGrpc, accepts_connections, free_loopback_port
from routers.common import latency as latency_harness
from routers.data_ingest import get_dataset_request


pytestmark = pytest.mark.data_ingest

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVING = health_pb2.HealthCheckResponse.SERVING
READY_LOG: Final = 'Data Ingest App Ready!!!'


class UnusedRead:
    """A BrokerRead that no test here reads through: the lifespan only installs and clears it."""

    async def get_bars(self, query: BarsQuery) -> BarsResponse:
        raise AssertionError(f'nothing in this file fetches bars, yet get_bars was called with {query!r}')


def installed_readers() -> dict:
    """A copy of the mapping store_retrieve_stock dispatches through right now (as test_read_seam.py)."""
    return dict(getattr(ingest_control, '__READERS'))


@pytest.fixture(autouse=True)
def no_readers_leak_between_tests():
    ingest_control.clear_readers()
    yield
    ingest_control.clear_readers()


async def health_status(port: int, service: str = '', timeout_s: float = 5.0) -> int:
    """Ask the standard health service on 127.0.0.1:port for a service's status.

    Args:
        port (int): The server's port.
        service (str): The service name; '' is the server as a whole.
        timeout_s (float): The call's deadline.

    Returns:
        int: The HealthCheckResponse status.
    """
    channel = create_channel(f'{LOOPBACK}:{port}')
    try:
        stub = health_pb2_grpc.HealthStub(channel)
        request = health_pb2.HealthCheckRequest(service=service)
        response = await asyncio.wait_for(stub.Check(request, timeout=timeout_s), GUARD_S)
        return response.status
    finally:
        await channel.close()


class KafkaStubbed:
    """The production lifespan with Kafka stubbed, recording when the Kafka RPC servers start and stop.

    Each event records whether the gRPC port accepted connections at that moment and which readers
    were installed. Nothing inside the lifespan is replaced except Kafka, the latency server, and the
    recording wrapper LoopbackGrpc puts around build_grpc_host.

    Args:
        grpc_bind (LoopbackGrpc): The gRPC binding the lifespan will read.
    """

    def __init__(self, grpc_bind: LoopbackGrpc):
        self.grpc_bind = grpc_bind
        self.readers = {DataSource.ALPACA_API: UnusedRead()}
        self.app = main.create_app(self.readers)
        self.events: list[tuple[str, bool, dict]] = []
        self.rpc_servers = Mock()
        self.rpc_servers.shutdown.side_effect = lambda: self.__record('rpc-shutdown')
        self.wait_for_kafka = Mock(return_value=True)
        self.latency_server = Mock()

    def __record(self, name: str) -> None:
        self.events.append((name, accepts_connections(self.grpc_bind.port), installed_readers()))

    def __start_rpc_servers(self):
        self.__record('rpc-start')
        return self.rpc_servers

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """Enter the app's lifespan with the stubs in place, and stop any host it built, whatever happens.

        Yields:
            None: While the lifespan is up.
        """
        with ExitStack() as stubs:
            stubs.enter_context(patch.object(KafkaConsumerFactory, 'wait_for_kafka', self.wait_for_kafka))
            stubs.enter_context(patch.object(app_depends, 'initialize_latency_server', self.latency_server))
            stubs.enter_context(
                patch.object(get_dataset_request.rpc, 'init_servers', side_effect=self.__start_rpc_servers)
            )
            async with self.grpc_bind, self.app.router.lifespan_context(self.app):
                yield


# ---------------------------------------------------------------------------------------------------
# SERVING, and nothing after


@pytest.mark.asyncio
async def test_health_answers_serving_on_the_configured_port_while_up_and_nothing_answers_after():
    """D6.5 through the lifespan: SERVING on APP_INTERNAL_GRPC_PORT while the app is up; gone after.

    Before this task nothing listened. A lifespan that started the host but never stopped it would
    fail the second half (and, outside this harness, hang process exit: N3).
    """
    probe = KafkaStubbed(LoopbackGrpc())

    async with probe.running():
        assert await health_status(probe.grpc_bind.port) == SERVING

    assert not accepts_connections(probe.grpc_bind.port), 'the gRPC port still accepts after the lifespan ended'
    with pytest.raises(grpc.aio.AioRpcError):
        await health_status(probe.grpc_bind.port, timeout_s=0.3)


# ---------------------------------------------------------------------------------------------------
# THE ONE REGISTRATION POINT


@pytest.mark.asyncio
async def test_what_registered_services_returns_is_what_the_lifespan_serves_and_it_gets_the_injected_readers(
    monkeypatch: pytest.MonkeyPatch,
):
    """A later servicer (.8, .9) is added by appending to registered_services(), never by editing the lifespan.

    Ping stands in for that servicer. It must answer through the lifespan's own server, health must
    report it by name, and registered_services() must have been handed the very mapping create_app
    received (decision tj-j4wknb: the servicer reads the injected readers, not a broker class).
    """
    received = []

    def ping_only(readers):
        received.append(readers)
        return [ping_service()]

    monkeypatch.setattr(grpc_host, 'registered_services', ping_only)
    probe = KafkaStubbed(LoopbackGrpc())

    async with probe.running():
        channel = create_channel(f'{LOOPBACK}:{probe.grpc_bind.port}')
        try:
            answer = await asyncio.wait_for(ping(channel, 'through data_ingest', timeout_s=5), GUARD_S)
        finally:
            await channel.close()
        status = await health_status(probe.grpc_bind.port, PING_SERVICE_NAME)

    assert answer == 'through data_ingest'
    assert status == SERVING
    assert len(received) == 1
    assert received[0] is probe.readers


# ---------------------------------------------------------------------------------------------------
# THE BIND ADDRESS IS READ WHEN THE HOST IS BUILT


@pytest.mark.asyncio
async def test_the_bind_address_is_read_each_time_the_host_is_built_never_once_at_import(
    monkeypatch: pytest.MonkeyPatch,
):
    # Two builds under two different ports must bind those two ports. A host built from a value read
    # once -- at import, or cached on first use -- would bind the first port both times.
    first = free_loopback_port()
    second = free_loopback_port()
    while second == first:
        second = free_loopback_port()

    bound = []
    for port in (first, second):
        monkeypatch.setenv(GRPC_HOST_ENV, LOOPBACK)
        monkeypatch.setenv(GRPC_PORT_ENV, str(port))
        host = grpc_host.build_grpc_host({})
        try:
            await asyncio.wait_for(host.start(), GUARD_S)
            bound.append(host.port)
        finally:
            await asyncio.wait_for(host.stop(), GUARD_S)

    assert bound == [first, second]


# ---------------------------------------------------------------------------------------------------
# AN UNSET VARIABLE STOPS STARTUP BEFORE ANYTHING STARTS


@pytest.mark.asyncio
@pytest.mark.parametrize('unset', [GRPC_HOST_ENV, GRPC_PORT_ENV])
async def test_an_unset_bind_variable_stops_startup_before_kafka_the_worker_pool_or_the_readers(unset: str):
    """No default, and no half-started app: the host is built FIRST, so the failure precedes every start.

    Built after the Kafka RPC servers instead, the same error would leave their consumer threads
    running, outside the finally that stops them, and the process would hang rather than exit.
    """
    probe = KafkaStubbed(LoopbackGrpc(unset=(unset,)))
    worker_startup = Mock()

    with patch.object(SharedWorkerPool, 'worker_startup', worker_startup), pytest.raises(ValueError, match=unset):
        async with probe.running():
            pytest.fail(f'the lifespan came up with {unset} unset')

    assert probe.wait_for_kafka.call_count == 0
    assert probe.events == [], 'the Kafka RPC servers were started before the bind address was read'
    assert worker_startup.call_count == 0
    assert probe.latency_server.call_count == 0
    assert installed_readers() == {}


# ---------------------------------------------------------------------------------------------------
# A FAILED gRPC START STILL TEARS DOWN KAFKA


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['port-taken', 'wildcard-host'])
async def test_a_grpc_start_that_fails_still_tears_down_kafka_the_readers_and_the_worker_pool(
    failure: str, caplog: pytest.LogCaptureFixture
):
    """The Kafka consumers are already running when the gRPC host starts, so its failure must reach the teardown.

    The two ways start() fails after the bind variables were read: the port is held by something
    else (A2 makes a second gRPC bind fail too), or the host is a wildcard (tj-r6vcgv A5).
    """
    caplog.set_level(logging.INFO)
    worker_shutdown = Mock(wraps=SharedWorkerPool.worker_shutdown)
    producer_shutdown = Mock(wraps=KafkaProducerFactory.shutdown)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder, ExitStack() as spies:
        holder.bind((LOOPBACK, 0))
        holder.listen()
        if failure == 'port-taken':
            probe = KafkaStubbed(LoopbackGrpc(port=holder.getsockname()[1]))
            expected = pytest.raises(RuntimeError, match='bind')
        else:
            # The wildcard is what is under test here; the holder is simply not used.
            probe = KafkaStubbed(LoopbackGrpc(host='0.0.0.0'))
            expected = pytest.raises(ValueError, match='wildcard')
        spies.enter_context(patch.object(SharedWorkerPool, 'worker_shutdown', worker_shutdown))
        spies.enter_context(patch.object(KafkaProducerFactory, 'shutdown', producer_shutdown))

        with expected:
            async with probe.running():
                pytest.fail(f'the lifespan came up despite {failure}')

    assert [name for name, _, _ in probe.events] == ['rpc-start', 'rpc-shutdown']
    assert worker_shutdown.call_count == 1
    assert producer_shutdown.call_count == 1
    assert installed_readers() == {}
    assert READY_LOG not in caplog.messages, 'the app logged ready although it never came up'


# The exit probe runs in a fresh interpreter, because what is under test is whether that interpreter
# EXITS. Its Kafka RPC consumer stand-in is a task on the shared worker pool that blocks until the RPC
# servers' shutdown() releases it -- the shape of the real consumers, which block in 'for message in
# consumer' on that pool until shutdown closes them. concurrent.futures joins its worker threads at
# interpreter exit, so a teardown that never runs leaves the process waiting on that task forever.
EXIT_PROBE = """
import asyncio
import os
import socket
import sys
import threading
from unittest.mock import Mock

from common.kafka.messaging.kafka_consumer import KafkaConsumerFactory
from common.rpc.server import GRPC_HOST_ENV, GRPC_PORT_ENV
from common.worker_pool import SharedWorkerPool
from data.ingest.app import app_depends, main
from routers.data_ingest import get_dataset_request

released = threading.Event()
servers = Mock()
servers.shutdown.side_effect = released.set


def init_servers():
    SharedWorkerPool.get_instance().submit(released.wait)
    return servers


KafkaConsumerFactory.wait_for_kafka = Mock(return_value=True)
app_depends.initialize_latency_server = Mock()
get_dataset_request.rpc.init_servers = init_servers

holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
holder.bind(('127.0.0.1', 0))
holder.listen()
port = holder.getsockname()[1]
if sys.argv[1] == 'clean':
    holder.close()
os.environ[GRPC_HOST_ENV] = '127.0.0.1'
os.environ[GRPC_PORT_ENV] = str(port)

app = main.create_app({})


async def run():
    try:
        async with app.router.lifespan_context(app):
            print('lifespan=up', flush=True)
    except Exception as error:
        print('lifespan=failed ' + type(error).__name__, flush=True)


asyncio.run(run())
print('main=returned', flush=True)
"""

# Under a second locally, imports included. A hang is a hang at any budget.
EXIT_BUDGET_S: Final = 30


@pytest.mark.parametrize(
    ('case', 'outcome'),
    [('clean', 'lifespan=up'), ('port-taken', 'lifespan=failed RuntimeError')],
    ids=['clean-shutdown', 'port-taken'],
)
def test_the_process_exits_after_the_lifespan_whether_or_not_the_grpc_host_started(case: str, outcome: str):
    """The consequence the teardown exists for: the interpreter exits.

    port-taken: the gRPC start fails after the consumers are running, so only the finally's teardown
    releases them. clean-shutdown: the same, through a lifespan that came up and served.

    What this does NOT catch, measured rather than assumed: a host that is started and never stopped
    did not hang THIS probe's exit (no call in flight, 2026-10-02). N3 is carried by LoopbackGrpc's
    check that nothing still serves once the lifespan has ended, not by this exit.
    """
    try:
        done = subprocess.run(
            [sys.executable, '-c', EXIT_PROBE, case],
            cwd=REPO_ROOT,
            # The image's path: the probe runs the real lifespan, whose servicers import generated code
            # once tj-3mk3u5.9/.10 register them (decision tj-3mk3u5.42 F1).
            env={'PYTHONPATH': image_pythonpath(REPO_ROOT)},
            capture_output=True,
            text=True,
            timeout=EXIT_BUDGET_S,
            check=False,
        )
    except subprocess.TimeoutExpired as hung:
        pytest.fail(f'the process was still alive {EXIT_BUDGET_S}s in; it printed {hung.stdout!r}')

    assert done.stdout.splitlines() == [outcome, 'main=returned'], done.stderr
    assert done.returncode == 0, done.stderr


# ---------------------------------------------------------------------------------------------------
# ORDER


@pytest.mark.asyncio
async def test_the_host_starts_after_the_kafka_rpc_servers_and_before_ready_and_stops_before_they_shut_down(
    caplog: pytest.LogCaptureFixture,
):
    """The host starts after the existing startup and before the ready log, and stops before Kafka does.

    The first half is the bead's placement. The second is the teardown order: gRPC stops first, so a
    call still in flight finds the readers and the worker pool it was dispatched to (the servicers of
    .8 and .9 read through them).
    """
    caplog.set_level(logging.INFO)
    probe = KafkaStubbed(LoopbackGrpc())

    async with probe.running():
        listening_while_up = accepts_connections(probe.grpc_bind.port)

    assert listening_while_up
    assert probe.events == [('rpc-start', False, probe.readers), ('rpc-shutdown', False, probe.readers)]
    listening_log = f'gRPC server listening on {LOOPBACK}:{probe.grpc_bind.port}'
    started = [index for index, message in enumerate(caplog.messages) if message.startswith(listening_log)]
    assert len(started) == 1, caplog.messages
    assert started[0] < caplog.messages.index(READY_LOG)


# ---------------------------------------------------------------------------------------------------
# THE LATENCY ARM (tj-3mk3u5.60): REGISTERED WHEN THE HARNESS IS ON, ABSENT WHEN IT IS OFF

# Spelled out, not imported from common.rpc.latency: what is pinned is the wire name the gRPC arm's
# client dials (tj-3mk3u5.26), so a rename in the .proto has to turn this red rather than follow it.
# Importing the generated symbol here is banned anyway outside common/rpc (ADR tj-8konfu D3, TID251).
LATENCY_SERVICE_NAME: Final = 'trader_joe.proto.internal.latency.v1.LatencyService'

# The harness-OFF pin runs in a FRESH INTERPRETER, because half of what it asserts is an IMPORT fact:
# with the flag off, nothing on the path from grpc_host to registered_services() loads generated code.
# This process cannot answer that -- it imports common.rpc.ping at the top of this file, which imports
# trader_joe.proto.ping -- so an in-process check would be red on arrival and could never go green.
# The child is given PYTHONPATH and nothing else, so LATENCY_TEST_ENABLED is unset, as in production.
HARNESS_OFF_PROBE = """
import sys

from data.ingest.app.grpc_host import registered_services

names = [service.name for service in registered_services({})]
generated = sorted(name for name in sys.modules if name == 'trader_joe' or name.startswith('trader_joe.'))
print('services=' + repr(names), flush=True)
print('generated=' + repr(generated), flush=True)
"""


def test_with_the_latency_harness_off_nothing_is_registered_and_no_generated_module_is_loaded():
    """The production default: a dev-only servicer is not served, and its generated tree is not even loaded.

    The second half is the one that protects production. get_latency_services() imports
    common.rpc.latency inside the flag check for exactly this reason, so a top-level import added
    there later -- which would still leave the registration list empty -- is caught here.
    """
    try:
        done = subprocess.run(
            [sys.executable, '-c', HARNESS_OFF_PROBE],
            cwd=REPO_ROOT,
            env={'PYTHONPATH': image_pythonpath(REPO_ROOT)},
            capture_output=True,
            text=True,
            timeout=EXIT_BUDGET_S,
            check=False,
        )
    except subprocess.TimeoutExpired as hung:
        pytest.fail(f'the probe was still alive {EXIT_BUDGET_S}s in; it printed {hung.stdout!r}')

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ['services=[]', 'generated=[]'], done.stderr


def test_with_the_latency_harness_on_exactly_the_latency_servicer_is_registered(monkeypatch: pytest.MonkeyPatch):
    """Flag on: one registration, the gRPC arm's, so tj-3mk3u5.26 has a server to dial.

    LATENCY_TEST_ENABLED is read ONCE, at import of routers/common/latency.py (:22), into a module
    attribute. Setting the environment variable now would change nothing -- the module was imported
    long before this test ran -- so the pin flips the attribute the production code actually reads.
    """
    monkeypatch.setattr(latency_harness, 'LATENCY_TEST_ENABLED', True)

    services = grpc_host.registered_services({})

    assert [service.name for service in services] == [LATENCY_SERVICE_NAME]


@pytest.mark.asyncio
async def test_with_the_latency_harness_on_the_host_built_here_actually_serves_the_probe(
    monkeypatch: pytest.MonkeyPatch,
):
    """The registration's consequence, not just its name: a probe sent to the built host is answered.

    A ServiceRegistration whose add_to_server did not match its name would pass the list pin above and
    still leave the arm unreachable. .26's runtime proof is the dev launch; this is the local half of
    it -- the same registration path, through build_grpc_host, over a real loopback channel.
    """
    monkeypatch.setattr(latency_harness, 'LATENCY_TEST_ENABLED', True)
    monkeypatch.setenv(GRPC_HOST_ENV, LOOPBACK)
    monkeypatch.setenv(GRPC_PORT_ENV, str(free_loopback_port()))

    host = grpc_host.build_grpc_host({})
    try:
        await asyncio.wait_for(host.start(), GUARD_S)
        status = await health_status(host.port, LATENCY_SERVICE_NAME)
        channel = create_channel(f'{LOOPBACK}:{host.port}')
        try:
            await asyncio.wait_for(LatencyProbeClient(channel, timeout_s=5).probe('through data_ingest'), GUARD_S)
        finally:
            await channel.close()
    finally:
        await asyncio.wait_for(host.stop(), GUARD_S)

    assert status == SERVING
