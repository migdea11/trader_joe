"""data_ingest hosts a grpc.aio server in its lifespan (tj-3mk3u5.24).

The design is bead tj-3mk3u5.24 with its architect notes -- N3, binding: the host is entered only as
'async with' around the lifespan's yield, so it stops on every exit path; the bind variables have no
default -- ADR tj-8konfu D2 (grpc.aio) and D6.5 (the standard health service), and decision tj-j4wknb
INJECTION (a servicer is handed the readers create_app received, never a broker class).

Pinned here, through the production lifespan (main.create_app -> app_depends.make_lifespan):

* health answers SERVING on the configured port while the app is up, and nothing answers after;
* grpc_host.registered_services() is the one registration point: what it returns is what the
  lifespan's host serves, and it is handed the readers create_app received;
* FetchDataset is registered ALWAYS (tj-3mk3u5.9), and the latency arm joins it -- beside it, never
  instead of it -- only when LATENCY_TEST_ENABLED is on, with common.rpc.latency left unimported when
  it is off (tj-3mk3u5.60). That last clause replaces this file's older pin that NO generated module
  was loaded with the harness off: FetchDataset has no flag and grpc_host imports common.rpc.ingest at
  module level, so the broad version is dead and the narrow one it was protecting is what remains. The
  reasoning is written out above HARNESS_OFF_PROBE;
* the bind address is read when the host is built, never once at import;
* an unset variable stops startup before the worker pool or the latency server are touched;
* a gRPC start that fails (port taken, wildcard host) still runs the teardown, and the process then
  exits instead of hanging on a pool thread;
* order: the host is serving before the ready log, and has stopped before the worker pool it
  dispatches onto is shut down.

THE LAST THREE WERE PHRASED AGAINST KAFKA until tj-3mk3u5.32 -- startup stopping "before Kafka",
the Kafka teardown running, the host starting after the Kafka RPC servers and stopping before they
shut down, with the readers still installed. tj-3mk3u5.11 deletes the RPC servers, the producer and
ingest_control's reader registry, so each of those fixed points went. What replaced them is not a
weaker version of the same assertion: the worker pool and the gRPC port are what the surviving
properties were always about, since the pool's threads are the non-daemon ones that outlive a
skipped teardown and the port is what a caller reaches.

Only the latency server is replaced, at app_depends' own name for it, plus whatever of the Kafka
startup still exists (data/ingest/tests/kafka_wiring.py -- transitional, tj-iwiq23). The gRPC host
is the real one on 127.0.0.1 (grpc_bind.LoopbackGrpc). LoopbackGrpc fails any test whose lifespan
left a host serving, then stops it, so that regression is red in every test here (and in the
re-pointed lifespan tests of test_read_seam.py and test_fake_read.py) instead of hanging the run.
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
from common.rpc.channel import create_channel
from common.rpc.latency import LatencyProbeClient
from common.rpc.ping import SERVICE_NAME as PING_SERVICE_NAME
from common.rpc.ping import ping, ping_service
from common.rpc.server import GRPC_HOST_ENV, GRPC_PORT_ENV
from common.tests.image_path import image_pythonpath
from common.worker_pool import SharedWorkerPool
from data.ingest.app import app_depends, grpc_host, main
from data.ingest.app.brokers.interface import BarsQuery, BarsResponse
from data.ingest.tests.grpc_bind import GUARD_S, LOOPBACK, LoopbackGrpc, accepts_connections, free_loopback_port
from data.ingest.tests.kafka_wiring import stub_kafka_startup
from routers.common import latency as latency_harness


pytestmark = pytest.mark.data_ingest

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVING = health_pb2.HealthCheckResponse.SERVING
READY_LOG: Final = 'Data Ingest App Ready!!!'


class UnusedRead:
    """A BrokerRead that no test here reads through: the lifespan only installs and clears it."""

    async def get_bars(self, query: BarsQuery) -> BarsResponse:
        raise AssertionError(f'nothing in this file fetches bars, yet get_bars was called with {query!r}')


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


class StubbedLifespan:
    """The production lifespan, with only what cannot run here stood in for.

    Nothing inside the lifespan is replaced except the latency server, whatever of the Kafka startup
    still exists (kafka_wiring.stub_kafka_startup -- transitional, see tj-iwiq23) and the recording
    wrapper LoopbackGrpc puts around build_grpc_host.

    It was called KafkaStubbed until tj-3mk3u5.32, and it recorded an event each time the Kafka RPC
    servers started or stopped, carrying whether the gRPC port accepted connections at that moment
    and which readers ingest_control held. tj-3mk3u5.11 deletes the servers and the reader registry
    both, so the events went with them; what the lifespan's order is now read against is the
    worker pool and the gRPC port itself, which are what the surviving properties are about.

    Args:
        grpc_bind (LoopbackGrpc): The gRPC binding the lifespan will read.
    """

    def __init__(self, grpc_bind: LoopbackGrpc):
        self.grpc_bind = grpc_bind
        self.readers = {DataSource.ALPACA_API: UnusedRead()}
        self.app = main.create_app(self.readers)
        self.latency_server = Mock()

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """Enter the app's lifespan with the stubs in place, and stop any host it built, whatever happens.

        Yields:
            None: While the lifespan is up.
        """
        with ExitStack() as stubs:
            stub_kafka_startup(stubs)
            stubs.enter_context(patch.object(app_depends, 'initialize_latency_server', self.latency_server))
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
    probe = StubbedLifespan(LoopbackGrpc())

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
    probe = StubbedLifespan(LoopbackGrpc())

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
async def test_an_unset_bind_variable_stops_startup_before_the_worker_pool_or_the_latency_server(unset: str):
    """No default, and no half-started app: the host is built FIRST, so the failure precedes every start.

    Built later instead, the same error would leave whatever startup had already run going, outside
    the finally that stops it, and the process would hang rather than exit. That was first written
    about the Kafka consumer threads, which were the non-daemon ones; tj-3mk3u5.11 removes them, and
    the worker pool is the thing that now has to not have been started (tj-3mk3u5.32).
    """
    probe = StubbedLifespan(LoopbackGrpc(unset=(unset,)))
    worker_startup = Mock()

    with patch.object(SharedWorkerPool, 'worker_startup', worker_startup), pytest.raises(ValueError, match=unset):
        async with probe.running():
            pytest.fail(f'the lifespan came up with {unset} unset')

    assert worker_startup.call_count == 0, 'the worker pool was started before the bind address was read'
    assert probe.latency_server.call_count == 0


# ---------------------------------------------------------------------------------------------------
# A FAILED gRPC START STILL TEARS DOWN KAFKA


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['port-taken', 'wildcard-host'])
async def test_a_grpc_start_that_fails_still_runs_the_teardown(failure: str, caplog: pytest.LogCaptureFixture):
    """The worker pool is already running when the gRPC host starts, so its failure must reach the finally.

    The two ways start() fails after the bind variables were read: the port is held by something
    else (A2 makes a second gRPC bind fail too), or the host is a wildcard (tj-r6vcgv A5).

    It used to assert the Kafka half of the teardown as well -- that the RPC servers had started and
    been shut down, that KafkaProducerFactory.shutdown ran, and that the readers had been cleared
    out of ingest_control. tj-3mk3u5.11 deletes all three (tj-3mk3u5.32). The worker pool is what
    remains of the teardown and it is the part that matters here anyway: its threads are what would
    otherwise outlive a failed startup.
    """
    caplog.set_level(logging.INFO)
    worker_shutdown = Mock(wraps=SharedWorkerPool.worker_shutdown)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder, ExitStack() as spies:
        holder.bind((LOOPBACK, 0))
        holder.listen()
        if failure == 'port-taken':
            probe = StubbedLifespan(LoopbackGrpc(port=holder.getsockname()[1]))
            expected = pytest.raises(RuntimeError, match='bind')
        else:
            # The wildcard is what is under test here; the holder is simply not used.
            probe = StubbedLifespan(LoopbackGrpc(host='0.0.0.0'))
            expected = pytest.raises(ValueError, match='wildcard')
        spies.enter_context(patch.object(SharedWorkerPool, 'worker_shutdown', worker_shutdown))

        with expected:
            async with probe.running():
                pytest.fail(f'the lifespan came up despite {failure}')

    assert worker_shutdown.call_count == 1, 'a failed gRPC start skipped the teardown'
    assert READY_LOG not in caplog.messages, 'the app logged ready although it never came up'


# The exit probe runs in a fresh interpreter, because what is under test is whether that interpreter
# EXITS. It submits a task to the shared worker pool that blocks until the lifespan's teardown
# releases it -- the shape of a long-running consumer, which occupies a pool thread until something
# in the teardown closes it. concurrent.futures joins its worker threads at interpreter exit, so a
# teardown that never runs leaves the process waiting on that task forever.
#
# THE BLOCKING TASK USED TO BE A KAFKA RPC CONSUMER STAND-IN, submitted from a stubbed
# rpc.init_servers and released by the servers' shutdown(). tj-3mk3u5.11 deletes both hooks, so it
# now rides on the pool's own startup and is released from inside its shutdown (tj-3mk3u5.32).
# Those are real lifespan steps on either side of the yield, so the shape is unchanged: startup
# submits, teardown releases, and a teardown that is skipped hangs. It has to be worker_startup and
# not an earlier step -- the lifespan builds the gRPC host FIRST, when there is no pool yet to
# submit to. Releasing from INSIDE worker_shutdown, before it joins, is what keeps the probe
# honest: the pool cannot join a task that was never released.
EXIT_PROBE = """
import asyncio
import os
import socket
import sys
import threading
from unittest.mock import Mock

from common.rpc.server import GRPC_HOST_ENV, GRPC_PORT_ENV
from common.worker_pool import SharedWorkerPool
from data.ingest.app import app_depends, main
from routers.data_ingest import get_dataset_request

released = threading.Event()
real_worker_startup = SharedWorkerPool.worker_startup
real_worker_shutdown = SharedWorkerPool.worker_shutdown


def worker_startup():
    real_worker_startup()
    SharedWorkerPool.get_instance().submit(released.wait)


def worker_shutdown():
    released.set()
    return real_worker_shutdown()


app_depends.initialize_latency_server = Mock()
SharedWorkerPool.worker_startup = worker_startup
SharedWorkerPool.worker_shutdown = worker_shutdown

# The same two tolerant Kafka stubs as data/ingest/tests/kafka_wiring.py, spelled out rather than
# imported: this child runs on the IMAGE path, which carries no test package, and reaching into one
# from here is the very thing test_no_production_test_imports.py forbids. Without the first of them
# the lifespan dies in get_consumer_params, casting an unset BROKER_PORT. Both go on tj-iwiq23.
try:
    from common.kafka.messaging.kafka_consumer import KafkaConsumerFactory
except ModuleNotFoundError:
    pass
else:
    KafkaConsumerFactory.wait_for_kafka = Mock(return_value=True)

if getattr(get_dataset_request, 'rpc', None) is not None:
    get_dataset_request.rpc.init_servers = Mock(return_value=Mock())

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
async def test_the_host_is_serving_before_the_ready_log_and_has_stopped_before_the_worker_pool(
    caplog: pytest.LogCaptureFixture,
):
    """The host serves before the app says it is ready, and stops before the pool it dispatches onto.

    THE TEARDOWN HALF IS THE ONE WITH TEETH. The host is entered as 'async with' around the yield,
    so it stops before anything in the finally -- and the servicers dispatch their blocking vendor
    calls onto the shared worker pool, which the finally shuts down. Stopping the host first is what
    lets a call still in flight finish on a pool that is still alive. The spy records whether the
    port was still accepting when worker_shutdown ran, which is that order stated as an observation
    rather than read off the source.

    It used to be phrased against Kafka -- the host starts after the Kafka RPC servers and stops
    before they shut down, with the readers still installed. tj-3mk3u5.11 deletes the servers and
    the reader registry, so the two fixed points are now the ready log and the worker pool
    (tj-3mk3u5.32). The startup half is the bead's placement either way.
    """
    caplog.set_level(logging.INFO)
    probe = StubbedLifespan(LoopbackGrpc())
    serving_at_shutdown = []
    real_shutdown = SharedWorkerPool.worker_shutdown

    def recording_shutdown():
        serving_at_shutdown.append(accepts_connections(probe.grpc_bind.port))
        return real_shutdown()

    with patch.object(SharedWorkerPool, 'worker_shutdown', recording_shutdown):
        async with probe.running():
            listening_while_up = accepts_connections(probe.grpc_bind.port)

    assert listening_while_up, 'the gRPC port was not accepting while the app was up'
    assert serving_at_shutdown == [False], 'the worker pool was shut down while the gRPC host was still serving'
    listening_log = f'gRPC server listening on {LOOPBACK}:{probe.grpc_bind.port}'
    started = [index for index, message in enumerate(caplog.messages) if message.startswith(listening_log)]
    assert len(started) == 1, caplog.messages
    assert started[0] < caplog.messages.index(READY_LOG)


# ---------------------------------------------------------------------------------------------------
# THE LATENCY ARM (tj-3mk3u5.60): REGISTERED WHEN THE HARNESS IS ON, ABSENT WHEN IT IS OFF

# Spelled out, not imported from common.rpc.latency or common.rpc.ingest: what is pinned is the wire
# name each arm's client dials (tj-3mk3u5.26, tj-3mk3u5.9), so a rename in the .proto has to turn this
# red rather than follow it. Importing the generated symbols here is banned anyway outside common/rpc
# (ADR tj-8konfu D3, TID251).
LATENCY_SERVICE_NAME: Final = 'trader_joe.proto.internal.latency.v1.LatencyService'
INGEST_SERVICE_NAME: Final = 'trader_joe.proto.internal.ingest.v1.IngestService'

# THE HARNESS-OFF PIN, INVERTED ON tj-3mk3u5.9, AND WHAT IT STOPPED PROTECTING.
#
# It used to assert 'services=[]' and 'generated=[]': with the flag off nothing was registered, and no
# generated module was loaded anywhere on the path from grpc_host to registered_services(). BOTH HALVES
# ARE NOW FALSE, and only one of them is replaceable.
#
#   services  FetchDataset is served ALWAYS -- it is production, not a dev harness, and has no flag to
#             be off. So the pin becomes "exactly the ingest service, and nothing of the harness".
#   generated THE GUARANTEE IS GONE, deliberately and irreversibly. grpc_host.py imports
#             common.rpc.ingest at module top level, which loads trader_joe.proto.internal.ingest.v1;
#             nothing could make that lazy and still register the service unconditionally. Weakening
#             the assertion to "not many modules" would be a pin that cannot fail, so it is replaced
#             rather than relaxed.
#
# WHAT THE SECOND HALF WAS ACTUALLY PROTECTING SURVIVES, and is pinned here in a stronger form. Its
# stated purpose was that get_latency_services() keeps its import of common.rpc.latency INSIDE the flag
# check, so that a top-level import added there later -- which would still leave the registration list
# empty, and so would still pass the services half -- is caught. That property is untouched by
# FetchDataset: with the harness off, common.rpc.latency must not be in sys.modules. Naming the one
# module instead of the whole generated tree is narrower in scope and sharper in aim, because it fails
# for exactly the regression the original sentence describes and for nothing else.
#
# STILL A FRESH INTERPRETER, for the same reason as before: what is asserted is an IMPORT fact, and this
# process imports common.rpc.ping at the top of the file. The child is given PYTHONPATH and nothing
# else, so LATENCY_TEST_ENABLED is unset, as in production.
HARNESS_OFF_PROBE = """
import sys

from data.ingest.app.grpc_host import registered_services

names = [service.name for service in registered_services({})]
print('services=' + repr(names), flush=True)
print('latency_loaded=' + repr('common.rpc.latency' in sys.modules), flush=True)
"""


def test_with_the_latency_harness_off_only_fetch_dataset_is_registered_and_the_latency_module_is_unloaded():
    """The production default: FetchDataset served, the dev-only arm neither served nor even imported.

    The second half is the one that protects production. get_latency_services() imports
    common.rpc.latency inside the flag check for exactly this reason, so a top-level import added
    there later -- which would still leave the harness unregistered -- is caught here.
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
    assert done.stdout.splitlines() == [f'services={[INGEST_SERVICE_NAME]!r}', 'latency_loaded=False'], done.stderr


def test_with_the_latency_harness_on_the_latency_servicer_joins_fetch_dataset_rather_than_replacing_it(
    monkeypatch: pytest.MonkeyPatch,
):
    """Flag on: two registrations, so tj-3mk3u5.26 has a server to dial and FetchDataset still answers.

    The ORDER is pinned with them. registered_services() builds the list as [fetch, *harness], and a
    dev-only arm that could displace production's own service -- rather than be appended beside it --
    is the failure this spells out rather than leaves to a set comparison.

    LATENCY_TEST_ENABLED is read ONCE, at import of routers/common/latency.py (:19), into a module
    attribute. Setting the environment variable now would change nothing -- the module was imported
    long before this test ran -- so the pin flips the attribute the production code actually reads.
    """
    monkeypatch.setattr(latency_harness, 'LATENCY_TEST_ENABLED', True)

    services = grpc_host.registered_services({})

    assert [service.name for service in services] == [INGEST_SERVICE_NAME, LATENCY_SERVICE_NAME]


def test_registered_services_builds_the_fetch_handler_from_the_readers_it_was_handed(monkeypatch: pytest.MonkeyPatch):
    """Decision tj-j4wknb: the servicer reads through the INJECTED readers, never a broker class.

    The lifespan test above proves the mapping reaches registered_services(); this proves
    registered_services() puts it into the handler it builds, which is the half that would otherwise be
    covered only by a function that was monkeypatched away. A handler built from a fresh {} instead
    would serve every FetchDataset call a NotImplementedError and pass every other test in this file.
    """
    built = []
    monkeypatch.setattr(grpc_host, 'IngestFetchHandler', lambda readers: built.append(readers) or Mock())
    readers = {DataSource.ALPACA_API: UnusedRead()}

    grpc_host.registered_services(readers)

    assert built == [readers]
    assert built[0] is readers


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
