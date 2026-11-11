"""build_infra pins for data_ingest's gRPC healthcheck, the peer-reach check and the lockdown's gRPC half (tj-3mk3u5.25).

Design: the bead (T3b, the listener-dependent half split from T3a tj-3mk3u5.49), ADR tj-8konfu D6.5 = O1
(the standard grpc.health.v1 service is data_ingest's own healthcheck and never a gate on data_store, so
no compose depends_on joins the two) and ADR tj-q9ae5u addendum 3 (the server binds the data-ingest-grpc
alias, on ingest_store only, so nothing may dial loopback). The bead's validator gate, one section each:

  1. data_ingest's healthcheck checks Health '' SERVING in addition to /ping, and dials the host and port
     the container environment names, never loopback. Its own python runs against a real GrpcServerHost
     bound OFF 127.0.0.1 -- on 127.0.0.2, still the loopback interface, so nothing leaves the machine --
     and a real HTTP /ping, with only the environment the container would hand it.
  2. CI's Check gRPC Peer Reach runs from data_store and fails on anything but SERVING; Check Network
     Lockdown keeps test_client off data_ingest's gRPC port, by name and by address, behind a positive
     control from data_store. Each step's own script runs under bash with `docker` stubbed
     (grpc_peer_docker_stub.py); the peer step's Health.Check is a real call to a real server. Their
     places in the job and their compose spellings are pinned with the rest of the job in
     test_ci_invariants.py (PEER_STEP).
  2'. Since ADR tj-q9ae5u addendum 5 item 7 (tj-3mk3u5.62) the peer step is also the CLIENT-CONFIGURATION
     check: what it dials is data_store's own DATA_INGEST_GRPC_TARGET, read from the data_store container,
     and it must first equal data_ingest's bind. Composing the address from data_ingest's two variables
     would prove the network path while a missing or wrong target passed unnoticed, and from tj-3mk3u5.10
     the dataset path dials that target on every launch. So the pins below require, with a REACHABLE
     listener in place throughout: no target -> exit 1 naming the variable and NOTHING dialled; a target
     that differs from the bind -> exit 1 with BOTH values in one ::error:: and NOTHING dialled, even when
     that wrong target would have reached the very same listener; a matching target -> the stripped
     data_store value dialled, once.
  3. No depends_on between data_store and data_ingest, either way, in any file of any launch set.
  4. Dev reach was not taken, so test_grpc_bind_network.py keeps the alias strict in every set.
  5. No env file can redirect the host: test_grpc_bind_network.py,
     test_the_bind_host_and_its_alias_are_literal_in_the_base_file.

What only Docker can show -- compose up --wait going healthy on the real image, data_store reaching
data-ingest-grpc over ingest_store, test_client's real refusal on internal networks, docker inspect's
real output -- is CI System Testing's run. CI runs this suite as root, so nothing here depends on mode
bits being enforced: the shims are made executable and that is all.
"""

import asyncio
import contextlib
import errno
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import grpc
import pytest
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from common.rpc.channel import DATA_INGEST_GRPC_TARGET_ENV, DATA_STORE_GRPC_TARGET_ENV
from common.rpc.ping import ping_service
from common.rpc.server import GRPC_HOST_ENV, GRPC_PORT_ENV, BindAddress, GrpcServerHost
from common.rpc.store import SERVICE_NAME as STORE_SERVICE_NAME
from common.rpc.store import store_service
from common.tests import grpc_peer_docker_stub as stub
from common.tests.compose_model import BASE_FILE, interpolate, load
from common.tests.image_path import image_pythonpath
from common.tests.test_ci_invariants import (
    ENV_DEFAULT_FILE,
    LOCKDOWN_STEP,
    PEER_STEP,
    STAGED_ENV_FILE,
    _env_file_values,
    _system_step,
)
from common.tests.test_grpc_bind_network import (
    API_NET,
    CONTAINER_ID,
    DATA_INGEST_LAUNCH_SETS,
    DB_NET,
    DS_API_V4,
    DS_API_V6,
    DS_DB_V4,
    DS_INGEST_V4,
    DS_INGEST_V6,
    EGRESS_NET,
    EGRESS_V4,
    EGRESS_V6,
    HOST,
    PORT,
    STORE_CONTAINER_ID,
    STORE_HOST,
    STORE_NET,
    STORE_PORT,
    STORE_V4,
    STORE_V6,
    UNSET_V6,
    _model,
    _net,
    _report,
    _set_files,
)


pytestmark = pytest.mark.build_infra

DATA_INGEST, DATA_STORE, POSTGRES, TEST_CLIENT = 'data_ingest', 'data_store', 'postgres', 'test_client'
VENV_PYTHON = '/code/.venv/bin/python'
LOOPBACK = '127.0.0.1'
# Still the loopback interface, so nothing leaves the machine -- but not the address a probe that
# hard-codes 127.0.0.1 (or localhost) dials, so such a probe finds nothing listening.
OFF_LOOPBACK = '127.0.0.2'
GUARD_S = 60.0
CONTAINER_PATH = '/usr/local/bin:/usr/bin:/bin'
DOCKER_SHIM = '#!/bin/sh\nexec "$STUB_PYTHON" -P "$STUB_IMPL" "$@"\n'
TIMEOUT_SHIM = f'#!/bin/sh\nexec "$STUB_PYTHON" -P "$STUB_IMPL" {stub.TIMEOUT_MODE} "$@"\n'


# --- Item 1: data_ingest's healthcheck -----------------------------------------------------------------


def _base_healthcheck() -> dict:
    return ((load(BASE_FILE).get('services') or {}).get(DATA_INGEST) or {}).get('healthcheck') or {}


def _probe_script() -> str:
    """The python the healthcheck runs: the exec form, so the interpreter can be this venv's."""
    test = _base_healthcheck().get('test')
    assert isinstance(test, list) and len(test) == 4 and test[:3] == ['CMD', VENV_PYTHON, '-c'], (
        f'{DATA_INGEST} healthcheck is {test!r}; this module runs [CMD, {VENV_PYTHON}, -c, <script>]'
    )
    return str(test[3])


def _declared_timeout_s() -> float:
    value = str(_base_healthcheck().get('timeout', ''))
    match = re.fullmatch(r'([0-9]+)s', value)
    assert match, f'{DATA_INGEST} healthcheck timeout {value!r} is not whole seconds'
    return float(match.group(1))


def test_the_healthcheck_reaches_the_container_as_written():
    """Compose interpolation leaves the probe untouched, so it reads its addresses from os.environ when it runs.

    An exec-form CMD runs no shell: a $VAR in it would be compose's render-time guess (or a literal
    '$' after a $$ escape), never what the server read from the container environment at start.
    """
    script = _probe_script()
    for env in ({}, _env_file_values(ENV_DEFAULT_FILE)):
        assert interpolate(script, env) == script, f'compose rewrites {DATA_INGEST} healthcheck script: {script!r}'


@pytest.mark.parametrize('launch_set', DATA_INGEST_LAUNCH_SETS)
def test_every_launch_set_runs_the_base_files_healthcheck(launch_set: str):
    """No overlay replaces the two-halved probe: every set that runs data_ingest checks gRPC health too."""
    merged = (_model(launch_set)['services'][DATA_INGEST] or {}).get('healthcheck')
    assert merged == _base_healthcheck(), f'{launch_set} runs {DATA_INGEST} with another healthcheck: {merged!r}'


class _PingHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status = self.server.ping_status if self.path == '/ping' else 404
        body = b'{"message":"pong"}'
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextlib.contextmanager
def _ping_server(status: int) -> Iterator[int]:
    """An HTTP /ping on 127.0.0.1 answering STATUS, as uvicorn answers inside the container; yields its port."""
    server = http.server.ThreadingHTTPServer((LOOPBACK, 0), _PingHandler)
    server.ping_status = status
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(GUARD_S)


def _free_port(host: str) -> int:
    """A port nothing listens on: bound by the OS, then released."""
    with socket.socket() as holder:
        holder.bind((host, 0))
        return holder.getsockname()[1]


@contextlib.asynccontextmanager
async def _production_host(host: str) -> AsyncIterator[GrpcServerHost]:
    server = GrpcServerHost(BindAddress(host, 0), [ping_service()], stop_grace_s=0.1)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@contextlib.asynccontextmanager
async def _grpc_peer(host: str, state: str) -> AsyncIterator[int]:
    """A gRPC listener on HOST whose health '' reads STATE; yields its port.

    SERVING is the production host, GrpcServerHost. STOPPED is one that started and stopped, so nothing
    listens on the port. NOT_SERVING and UNKNOWN are the standard health servicer GrpcServerHost
    attaches, on a plain grpc.aio server, set to a status production reports only while it stops.
    """
    if state in ('SERVING', 'STOPPED'):
        async with _production_host(host) as server:
            port = server.port
            if state == 'STOPPED':
                await server.stop()
            yield port
        return
    server = grpc.aio.server()
    servicer = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(servicer, server)
    port = server.add_insecure_port(f'{host}:0')
    await server.start()
    try:
        await servicer.set('', health_pb2.HealthCheckResponse.ServingStatus.Value(state))
        yield port
    finally:
        await server.stop(None)


class _NoDelivery:
    """A DeliveryHandler no health check reaches: StoreService is registered, and only its health is asked."""

    async def deliver(self, attempt):
        raise AssertionError('a health probe must never deliver an attempt')

    async def register_instance(self, request):
        raise AssertionError('a health probe must never register an instance')


# data_store's gRPC listener in each state the store healthcheck and the peer step's reverse leg must tell apart.
STORE_STATES = ('SERVING', 'STOPPED', 'STORE_NOT_SERVING', 'NO_STORE_SERVICE', 'OVERALL_NOT_SERVING')


@contextlib.asynccontextmanager
async def _store_peer(host: str, state: str) -> AsyncIterator[int]:
    """data_store's gRPC listener on HOST in STATE; yields its port (tj-7zjov1.49).

    SERVING is production's own host, GrpcServerHost serving StoreService: '' and the StoreService name
    both read SERVING, as data_store's lifespan hosts it. STOPPED started and stopped, so nothing listens.
    STORE_NOT_SERVING is that host with StoreService's named status set NOT_SERVING through production's
    set_service_health. NO_STORE_SERVICE is a production host serving only PingService, so the StoreService
    name is unregistered and answers NOT_FOUND. OVERALL_NOT_SERVING is the standard health servicer on a
    plain grpc.aio server, '' NOT_SERVING and StoreService SERVING, the status production's '' reports
    only while it stops.
    """
    assert state in STORE_STATES, state
    if state != 'OVERALL_NOT_SERVING':
        services = [ping_service()] if state == 'NO_STORE_SERVICE' else [store_service(_NoDelivery())]
        server = GrpcServerHost(BindAddress(host, 0), services, stop_grace_s=0.1)
        await server.start()
        try:
            if state == 'STORE_NOT_SERVING':
                await server.set_service_health(STORE_SERVICE_NAME, False)
            port = server.port
            if state == 'STOPPED':
                await server.stop()
            yield port
        finally:
            await server.stop()
        return
    server = grpc.aio.server()
    servicer = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(servicer, server)
    port = server.add_insecure_port(f'{host}:0')
    await server.start()
    try:
        await servicer.set('', health_pb2.HealthCheckResponse.NOT_SERVING)
        await servicer.set(STORE_SERVICE_NAME, health_pb2.HealthCheckResponse.SERVING)
        yield port
    finally:
        await server.stop(None)


def _container_env(http_port: int, grpc_host: str, grpc_port: int, *, without: str = '') -> dict[str, str]:
    """The healthcheck's whole environment, as the container hands it; nothing from this process leaks in."""
    environment = {'APP_INTERNAL_PORT': str(http_port), GRPC_HOST_ENV: grpc_host, GRPC_PORT_ENV: str(grpc_port)}
    environment.pop(without, None)
    return environment


async def _run_probe(environment: dict[str, str]) -> tuple[int, str, float]:
    """Run the healthcheck's own script with this venv's interpreter: (exit status, output, seconds taken)."""
    script = _probe_script()
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-c', script, env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), GUARD_S)
    output = f'stdout:\n{stdout.decode()}\nstderr:\n{stderr.decode()}'
    return process.returncode, output, time.monotonic() - started


@pytest.mark.asyncio
async def test_the_healthcheck_passes_when_ping_answers_and_grpc_health_reads_serving():
    """Both halves up: exit 0, inside the healthcheck's own timeout.

    The listener is a real GrpcServerHost bound to 127.0.0.2 -- data_ingest's binds the alias's
    ingest_store address, not loopback -- so a probe that dialled 127.0.0.1 would find nothing.
    """
    with _ping_server(200) as http_port:
        async with _grpc_peer(OFF_LOOPBACK, 'SERVING') as grpc_port:
            status, output, elapsed = await _run_probe(_container_env(http_port, OFF_LOOPBACK, grpc_port))
    assert status == 0, output
    assert elapsed < _declared_timeout_s(), f'the probe took {elapsed:.1f}s of a {_declared_timeout_s()}s timeout'


@pytest.mark.asyncio
async def test_the_healthcheck_never_settles_for_a_listener_on_loopback():
    """A listener on 127.0.0.1 alone, the environment naming 127.0.0.2 on the same port: red.

    The bind address is the alias's ingest_store address. A probe that fell back to loopback, or
    ignored the host the environment names, would pass here.
    """
    with _ping_server(200) as http_port:
        async with _grpc_peer(LOOPBACK, 'SERVING') as grpc_port:
            status, output, _ = await _run_probe(_container_env(http_port, OFF_LOOPBACK, grpc_port))
    assert status != 0, output
    assert 'UNAVAILABLE' in output, output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('state', 'printed'),
    [('NOT_SERVING', 'NOT_SERVING'), ('UNKNOWN', 'UNKNOWN'), ('STOPPED', 'UNAVAILABLE')],
    ids=['not serving', 'unknown', 'listener stopped'],
)
async def test_the_healthcheck_fails_when_grpc_health_is_not_serving(state: str, printed: str):
    """/ping answers but gRPC health does not read SERVING: red, naming why, inside the timeout.

    /ping alone reports healthy a container whose gRPC listener is dead -- what the gRPC half exists for.
    """
    with _ping_server(200) as http_port:
        async with _grpc_peer(OFF_LOOPBACK, state) as grpc_port:
            status, output, elapsed = await _run_probe(_container_env(http_port, OFF_LOOPBACK, grpc_port))
    assert status != 0, output
    assert printed in output, output
    assert elapsed < _declared_timeout_s(), f'the probe took {elapsed:.1f}s of a {_declared_timeout_s()}s timeout'


@pytest.mark.asyncio
@pytest.mark.parametrize('ping', ['refused', 'HTTP 500'])
async def test_the_healthcheck_fails_when_ping_does_not_answer(ping: str):
    """Health reads SERVING but /ping is down or failing: red. Both halves must pass."""
    async with _grpc_peer(OFF_LOOPBACK, 'SERVING') as grpc_port:
        if ping == 'refused':
            status, output, _ = await _run_probe(_container_env(_free_port(LOOPBACK), OFF_LOOPBACK, grpc_port))
        else:
            with _ping_server(500) as http_port:
                status, output, _ = await _run_probe(_container_env(http_port, OFF_LOOPBACK, grpc_port))
    assert status != 0, output


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', [GRPC_HOST_ENV, GRPC_PORT_ENV])
async def test_the_healthcheck_fails_without_a_bind_variable(missing: str):
    """No default: without either variable the probe fails naming it, instead of dialling a guess."""
    with _ping_server(200) as http_port:
        async with _grpc_peer(OFF_LOOPBACK, 'SERVING') as grpc_port:
            environment = _container_env(http_port, OFF_LOOPBACK, grpc_port, without=missing)
            status, output, _ = await _run_probe(environment)
    assert status != 0, output
    assert missing in output, output


# --- Item 2: Check gRPC Peer Reach and the lockdown's gRPC half, each step's own script --------------


# What the stand-in records, by the variable it reads the path from.
_LOGS = {
    'docker': 'STUB_LOG',
    'grpc': 'STUB_GRPC_LOG',
    'connect': 'STUB_CONNECT_LOG',
    'unmodelled': 'STUB_UNMODELLED_LOG',
}


def _shim(directory: Path, name: str, text: str) -> None:
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(text, encoding='utf-8')
    path.chmod(0o755)


def _prepare_step(tmp_path: Path, step: str, scenario: dict) -> tuple[list[str], dict[str, str]]:
    """The step's own script as GitHub runs a `run:` with no shell (bash -e), docker stubbed."""
    bash = shutil.which('bash')
    assert bash, 'bash is not on PATH, so the step cannot be exercised'
    script = _system_step(step).get('run') or ''
    assert '${{' not in script, f'{step} now uses a workflow expression, which this test cannot evaluate'
    _shim(tmp_path / 'bin', 'docker', DOCKER_SHIM)
    _shim(tmp_path / 'postgres-bin', 'timeout', TIMEOUT_SHIM)
    (tmp_path / 'scenario.json').write_text(json.dumps(scenario), encoding='utf-8')
    (tmp_path / 'step.sh').write_text(script, encoding='utf-8')
    # The project env file, staged as the job stages it from the template (the lockdown step reads
    # BROKER_NAME from it).
    (tmp_path / STAGED_ENV_FILE).write_text(ENV_DEFAULT_FILE.read_text(encoding='utf-8'), encoding='utf-8')
    environment = {
        'PATH': f'{tmp_path / "bin"}{os.pathsep}{os.environ.get("PATH", "")}',
        # A fresh interpreter importing common.tests.*, so it gets the same host mirror of the
        # image's path every other subprocess probe gets. The bare repository root stopped being
        # enough when the service trees moved under server/ (tj-iontkq.4).
        'PYTHONPATH': image_pythonpath(),
        'STUB_PYTHON': sys.executable,
        'STUB_IMPL': stub.__file__,
        'STUB_SCENARIO': str(tmp_path / 'scenario.json'),
        'STUB_POSTGRES_BIN': str(tmp_path / 'postgres-bin'),
        **{variable: str(tmp_path / f'{name}.log') for name, variable in _LOGS.items()},
    }
    return [bash, '--noprofile', '--norc', '-e', str(tmp_path / 'step.sh')], environment


def _read_logs(tmp_path: Path) -> dict[str, list]:
    logs = {}
    for name in _LOGS:
        path = tmp_path / f'{name}.log'
        lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
        logs[name] = [json.loads(line) for line in lines]
    return logs


async def _run_step(tmp_path: Path, step: str, scenario: dict) -> tuple[subprocess.CompletedProcess, dict]:
    """Run STEP's script. Awaited, so a server on this test's event loop keeps answering the step's call."""
    argv, environment = _prepare_step(tmp_path, step, scenario)
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=tmp_path, env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), GUARD_S)
    result = subprocess.CompletedProcess(argv, process.returncode, stdout.decode(), stderr.decode())
    return result, _read_logs(tmp_path)


def _execs_into(docker: list[list[str]], service: str) -> list[list[str]]:
    return [call for call in docker if call[:6] == ['compose', '-f', 'docker-compose.yaml', 'exec', '-T', service]]


# The calls the step makes into data_store: reading its DATA_INGEST_GRPC_TARGET, then dialling it, then
# (tj-7zjov1.49, the reverse leg) reading its own bind host and port. Re-pinned from 2 for that leg.
STORE_CALLS = 4
# And into data_ingest: its bind host and port, then (the reverse leg) its DATA_STORE_GRPC_TARGET and the dial.
INGEST_CALLS = 4


def _peer_scenario(port: int | str, store_port: int | str = STORE_PORT, **changes: object) -> dict:
    """A healthy, correctly configured stack: each side's bind, the target the other side carries, two listeners.

    container_env and store_env are each a whole container environment, so a scenario can set the
    client's target apart from the server's bind -- the drift addendum 5 item 7 exists to catch, in
    either direction since tj-7zjov1.49.
    """
    scenario = {
        'container_env': {
            GRPC_HOST_ENV: HOST,
            GRPC_PORT_ENV: str(port),
            DATA_STORE_GRPC_TARGET_ENV: f'{STORE_HOST}:{store_port}',
            'PATH': CONTAINER_PATH,
        },
        'store_env': {
            DATA_INGEST_GRPC_TARGET_ENV: f'{HOST}:{port}',
            GRPC_HOST_ENV: STORE_HOST,
            GRPC_PORT_ENV: str(store_port),
            'PATH': CONTAINER_PATH,
        },
        'store_dns': {HOST: OFF_LOOPBACK},
        'ingest_dns': {STORE_HOST: OFF_LOOPBACK},
    }
    scenario.update(changes)
    return scenario


def _dials(logs: dict, container: str) -> list[str]:
    """The targets CONTAINER handed grpc.insecure_channel, in order."""
    return [call['target'] for call in logs['grpc'] if call['container'] == container]


def _env_without(name: str, port: int | str = PORT, store_port: int | str = STORE_PORT) -> dict[str, str]:
    return {key: value for key, value in _peer_scenario(port, store_port)['container_env'].items() if key != name}


def _env_with(name: str, value: str, port: int | str = PORT, store_port: int | str = STORE_PORT) -> dict[str, str]:
    return {**_peer_scenario(port, store_port)['container_env'], name: value}


def _store_without(name: str, port: int | str = PORT, store_port: int | str = STORE_PORT) -> dict[str, str]:
    return {key: value for key, value in _peer_scenario(port, store_port)['store_env'].items() if key != name}


def _store_with(name: str, value: str, port: int | str = PORT, store_port: int | str = STORE_PORT) -> dict[str, str]:
    return {**_peer_scenario(port, store_port)['store_env'], name: value}


def _annotations(result: subprocess.CompletedProcess) -> list[str]:
    """The step's ::error:: lines alone: the echo above them repeats both values, so it cannot stand in."""
    return [line for line in result.stdout.splitlines() if line.startswith('::error::')]


@pytest.mark.asyncio
async def test_the_peer_check_passes_when_data_store_gets_serving(tmp_path: Path):
    """From inside data_store, Health.Check '' on the target data_store carries, once.

    Re-pinned for tj-7zjov1.49: then from inside data_ingest, Health.Check '' and StoreService on the
    target data_ingest carries, over one channel, dialled once; both legs must pass for exit 0.
    """
    async with _grpc_peer(OFF_LOOPBACK, 'SERVING') as port, _store_peer(OFF_LOOPBACK, 'SERVING') as store_port:
        result, logs = await _run_step(tmp_path, PEER_STEP, _peer_scenario(port, store_port))
    assert result.returncode == 0, _report(result)
    assert not logs['unmodelled'], logs['unmodelled']
    assert logs['grpc'] == [
        {'container': DATA_STORE, 'target': f'{HOST}:{port}'},
        {'container': DATA_INGEST, 'target': f'{STORE_HOST}:{store_port}'},
    ], (
        f'data_store must dial {HOST}:{port}, the host and port data_ingest names, once; then data_ingest '
        f'{STORE_HOST}:{store_port}, once: {logs["grpc"]}'
    )
    assert len(_execs_into(logs['docker'], DATA_STORE)) == STORE_CALLS, (
        f'expected {STORE_CALLS} calls from data_store, its target read, the dial and its bind: {logs["docker"]}'
    )
    assert len(_execs_into(logs['docker'], DATA_INGEST)) == INGEST_CALLS, (
        f'expected {INGEST_CALLS} calls from data_ingest, its bind, its target read and the dial: {logs["docker"]}'
    )
    assert f'{HOST}:{port}: SERVING' in result.stdout, _report(result)
    for name in ('', STORE_SERVICE_NAME):
        assert f'{STORE_HOST}:{store_port} service {name!r}: SERVING' in result.stdout, _report(result)


# A target compose hands over intact, and the same value with the whitespace a value picks up on its way
# through an env file: the image interpreter strips it, exactly as it strips data_ingest's bind, so both
# equal the bind and both dial the stripped value.
_PEER_TARGET_ACCEPTED = {'as compose sets it': '{target}', 'with surrounding whitespace': '  {target}  '}


@pytest.mark.asyncio
@pytest.mark.parametrize('spelling', list(_PEER_TARGET_ACCEPTED.values()), ids=list(_PEER_TARGET_ACCEPTED))
async def test_the_peer_check_dials_the_target_data_store_itself_carries(tmp_path: Path, spelling: str):
    """What is dialled is data_store's own DATA_INGEST_GRPC_TARGET, read from the data_store container.

    addendum 5 item 7: the variable every data_store -> data_ingest client reads through
    common.rpc.channel's target_from_env, which has no default. The step reads it with its own call into
    data_store -- hence two calls, not one -- and hands that value to the channel.
    """
    async with _grpc_peer(OFF_LOOPBACK, 'SERVING') as port, _store_peer(OFF_LOOPBACK, 'SERVING') as store_port:
        target = f'{HOST}:{port}'
        store_env = _store_with(DATA_INGEST_GRPC_TARGET_ENV, spelling.format(target=target), port, store_port)
        result, logs = await _run_step(tmp_path, PEER_STEP, _peer_scenario(port, store_port, store_env=store_env))
    assert result.returncode == 0, _report(result)
    assert not logs['unmodelled'], logs['unmodelled']
    assert _dials(logs, DATA_STORE) == [target], f'data_store must dial its own target {target} once: {logs["grpc"]}'
    assert len(_execs_into(logs['docker'], DATA_STORE)) == STORE_CALLS, (
        f'the target must be read from the data_store container, not the runner environment: {logs["docker"]}'
    )


_PEER_NO_TARGET = {
    'target unset': _store_without(DATA_INGEST_GRPC_TARGET_ENV),
    'target empty': _store_with(DATA_INGEST_GRPC_TARGET_ENV, ''),
    'target blank': _store_with(DATA_INGEST_GRPC_TARGET_ENV, '   '),
}


@pytest.mark.asyncio
@pytest.mark.parametrize('store_env', list(_PEER_NO_TARGET.values()), ids=list(_PEER_NO_TARGET))
async def test_the_peer_check_fails_naming_the_variable_when_data_store_has_no_target(tmp_path: Path, store_env: dict):
    """No DATA_INGEST_GRPC_TARGET in data_store: exit 1 naming it, and nothing dialled.

    The listener is up and data_ingest's bind is readable throughout, so a step that fell back to the
    reconstructed address would pass here -- the fallback addendum 5 item 7 forbids.
    """
    async with _grpc_peer(OFF_LOOPBACK, 'SERVING') as port:
        result, logs = await _run_step(tmp_path, PEER_STEP, _peer_scenario(port, store_env=store_env))
    assert result.returncode == 1, _report(result)
    assert not logs['unmodelled'], logs['unmodelled']
    assert not logs['grpc'], f'the step dialled {logs["grpc"]} with no target in data_store'
    assert len(_annotations(result)) == 1, _report(result)
    assert DATA_INGEST_GRPC_TARGET_ENV in _annotations(result)[0], _report(result)


# Each wrong target RESOLVES TO THE VERY SAME LISTENER in the scenario's store_dns, so reaching it would
# succeed: only the equality check can make these red, and reachability must never bypass it.
_PEER_MISMATCH = {
    # ADR tj-q9ae5u addendum 5 item 6: never the REST hostname, which on devnet can resolve to an
    # address the server never bound.
    'the REST hostname': (DATA_INGEST, '{port}'),
    'a loopback address': (LOOPBACK, '{port}'),
    # A port an env file moved on the client end alone.
    'a stale port': (HOST, '1'),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('host', 'port_spelling'), list(_PEER_MISMATCH.values()), ids=list(_PEER_MISMATCH))
async def test_the_peer_check_fails_with_both_values_when_the_target_is_not_the_bind(
    tmp_path: Path, host: str, port_spelling: str
):
    """A target that differs from data_ingest's bind: exit 1, one ::error:: carrying BOTH values, nothing dialled.

    The equality check comes first and is not a fallback. Each wrong target here would have reached the
    same live listener, so a step that dialled first and asked afterwards would pass -- the hole addendum 5
    item 7 closes, and the reason the assertion is on what was dialled, not only on the exit status.
    """
    async with _grpc_peer(OFF_LOOPBACK, 'SERVING') as port:
        target = f'{host}:{port_spelling.format(port=port)}'
        scenario = _peer_scenario(
            port,
            store_env=_store_with(DATA_INGEST_GRPC_TARGET_ENV, target),
            store_dns={HOST: OFF_LOOPBACK, DATA_INGEST: OFF_LOOPBACK, LOOPBACK: OFF_LOOPBACK},
        )
        result, logs = await _run_step(tmp_path, PEER_STEP, scenario)
    assert result.returncode == 1, _report(result)
    assert not logs['unmodelled'], logs['unmodelled']
    assert not logs['grpc'], f'the step dialled {logs["grpc"]} although the target is not the bind'
    annotations = _annotations(result)
    assert len(annotations) == 1, _report(result)
    assert target in annotations[0] and f'{HOST}:{port}' in annotations[0], (
        f'the mismatch must print both {target} and {HOST}:{port}: {annotations}'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('state', 'printed'),
    [('NOT_SERVING', 'NOT_SERVING'), ('UNKNOWN', 'UNKNOWN'), ('STOPPED', 'UNAVAILABLE')],
    ids=['not serving', 'unknown', 'listener stopped'],
)
async def test_the_peer_check_fails_on_anything_but_serving_and_prints_why(tmp_path: Path, state: str, printed: str):
    """data_store reached the listener, or tried to, and did not get SERVING: exit 1, the status or code printed."""
    async with _grpc_peer(OFF_LOOPBACK, state) as port:
        result, logs = await _run_step(tmp_path, PEER_STEP, _peer_scenario(port))
    assert result.returncode == 1, _report(result)
    assert not logs['unmodelled'], logs['unmodelled']
    # Re-pinned for tj-7zjov1.49's container field: data_store dialled once, and the step stopped there.
    assert logs['grpc'] == [{'container': DATA_STORE, 'target': f'{HOST}:{port}'}], (
        f'data_store never dialled {HOST}:{port}, or the step went on: {logs["grpc"]}'
    )
    assert printed in result.stdout, _report(result)


_PEER_UNASKED = {
    'host unset': _peer_scenario(PORT, container_env=_env_without(GRPC_HOST_ENV)),
    'host empty': _peer_scenario(PORT, container_env=_env_with(GRPC_HOST_ENV, '')),
    'host blank': _peer_scenario(PORT, container_env=_env_with(GRPC_HOST_ENV, '   ')),
    'port unset': _peer_scenario(PORT, container_env=_env_without(GRPC_PORT_ENV)),
    'port empty': _peer_scenario(PORT, container_env=_env_with(GRPC_PORT_ENV, '')),
    'data_ingest not running': _peer_scenario(PORT, exec_fails=[DATA_INGEST]),
    'data_store not running': _peer_scenario(PORT, exec_fails=[DATA_STORE]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', list(_PEER_UNASKED.values()), ids=list(_PEER_UNASKED))
async def test_the_peer_check_fails_when_it_cannot_ask(tmp_path: Path, scenario: dict):
    """No address to dial, or no data_store to dial from: exit 1, and no call is made to a guessed address."""
    result, logs = await _run_step(tmp_path, PEER_STEP, scenario)
    assert result.returncode == 1, _report(result)
    assert not logs['unmodelled'], logs['unmodelled']
    assert not logs['grpc'], f'the step dialled {logs["grpc"]} without an address from data_ingest'


# --- tj-7zjov1.49: the peer step's reverse leg, data_ingest -> data_store's StoreService ----------------
#
# Every case below has data_ingest's listener SERVING and data_store's forward call succeeding, so the
# forward leg holds and whatever fails is the reverse leg's own refusal.


async def _reverse_leg(tmp_path: Path, store_state: str, **changes) -> tuple[subprocess.CompletedProcess, dict, int]:
    """Run the peer step with both listeners up, CHANGES applied to the scenario; (result, logs, store port)."""
    async with _grpc_peer(OFF_LOOPBACK, 'SERVING') as port, _store_peer(OFF_LOOPBACK, store_state) as store_port:
        built = {key: value(port, store_port) if callable(value) else value for key, value in changes.items()}
        result, logs = await _run_step(tmp_path, PEER_STEP, _peer_scenario(port, store_port, **built))
    assert not logs['unmodelled'], logs['unmodelled']
    assert _dials(logs, DATA_STORE) == [f'{HOST}:{port}'], f'the forward leg did not run first: {logs["grpc"]}'
    assert 'gRPC peer reach holds: data_store' in result.stdout, f'the forward leg did not hold:\n{_report(result)}'
    return result, logs, store_port


_REVERSE_NO_TARGET = {
    'target unset': lambda port, store_port: _env_without(DATA_STORE_GRPC_TARGET_ENV, port, store_port),
    'target empty': lambda port, store_port: _env_with(DATA_STORE_GRPC_TARGET_ENV, '', port, store_port),
    'target blank': lambda port, store_port: _env_with(DATA_STORE_GRPC_TARGET_ENV, '   ', port, store_port),
}


@pytest.mark.asyncio
@pytest.mark.parametrize('container_env', list(_REVERSE_NO_TARGET.values()), ids=list(_REVERSE_NO_TARGET))
async def test_the_reverse_leg_fails_naming_the_variable_when_data_ingest_has_no_target(tmp_path: Path, container_env):
    """No DATA_STORE_GRPC_TARGET in data_ingest: exit 1, one ::error:: naming it, and data_ingest dials nothing.

    data_store's listener is up and its bind readable, so a step that rebuilt the address from the
    server's side would pass here.
    """
    result, logs, _ = await _reverse_leg(tmp_path, 'SERVING', container_env=container_env)
    assert result.returncode == 1, _report(result)
    assert not _dials(logs, DATA_INGEST), f'data_ingest dialled {logs["grpc"]} with no target'
    annotations = _annotations(result)
    assert len(annotations) == 1, _report(result)
    assert DATA_STORE_GRPC_TARGET_ENV in annotations[0], _report(result)


# Each wrong target resolves, in data_ingest's dns, to the very same live store listener, so only the
# equality check can make these red.
_REVERSE_MISMATCH = {
    "data_store's REST hostname": (DATA_STORE, '{store_port}'),
    'a loopback address': (LOOPBACK, '{store_port}'),
    "data_ingest's own alias": (HOST, '{store_port}'),
    'a stale port': (STORE_HOST, '1'),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('host', 'port_spelling'), list(_REVERSE_MISMATCH.values()), ids=list(_REVERSE_MISMATCH))
async def test_the_reverse_leg_fails_with_both_values_when_the_target_is_not_the_stores_bind(
    tmp_path: Path, host: str, port_spelling: str
):
    """DATA_STORE_GRPC_TARGET differs from data_store's bind: exit 1, one ::error:: with BOTH values, no dial."""
    targets = {}

    def target_env(port, store_port):
        targets['wrong'] = f'{host}:{port_spelling.format(store_port=store_port)}'
        targets['bind'] = f'{STORE_HOST}:{store_port}'
        return _env_with(DATA_STORE_GRPC_TARGET_ENV, targets['wrong'], port, store_port)

    dns = {STORE_HOST: OFF_LOOPBACK, DATA_STORE: OFF_LOOPBACK, LOOPBACK: OFF_LOOPBACK, HOST: OFF_LOOPBACK}
    result, logs, _ = await _reverse_leg(tmp_path, 'SERVING', container_env=target_env, ingest_dns=dns)
    assert result.returncode == 1, _report(result)
    assert not _dials(logs, DATA_INGEST), f'data_ingest dialled {logs["grpc"]} although the target is not the bind'
    annotations = _annotations(result)
    assert len(annotations) == 1, _report(result)
    assert targets['wrong'] in annotations[0] and targets['bind'] in annotations[0], (
        f'the mismatch must print both {targets}: {annotations}'
    )


_REVERSE_NO_BIND = {
    'store host unset': lambda port, store_port: _store_without(GRPC_HOST_ENV, port, store_port),
    'store host blank': lambda port, store_port: _store_with(GRPC_HOST_ENV, '  ', port, store_port),
    'store port unset': lambda port, store_port: _store_without(GRPC_PORT_ENV, port, store_port),
}


@pytest.mark.asyncio
@pytest.mark.parametrize('store_env', list(_REVERSE_NO_BIND.values()), ids=list(_REVERSE_NO_BIND))
async def test_the_reverse_leg_fails_when_data_stores_bind_is_missing(tmp_path: Path, store_env):
    """Nothing to check data_ingest's target against: exit 1 naming the bind variables, and no dial."""
    result, logs, _ = await _reverse_leg(tmp_path, 'SERVING', store_env=store_env)
    assert result.returncode == 1, _report(result)
    assert not _dials(logs, DATA_INGEST), logs['grpc']
    annotations = _annotations(result)
    assert len(annotations) == 1 and GRPC_HOST_ENV in annotations[0], _report(result)


_REVERSE_NOT_SERVING = {
    "'' NOT_SERVING": ('OVERALL_NOT_SERVING', "service '': NOT_SERVING"),
    'StoreService NOT_SERVING': ('STORE_NOT_SERVING', f'service {STORE_SERVICE_NAME!r}: NOT_SERVING'),
    'StoreService unregistered (NOT_FOUND)': ('NO_STORE_SERVICE', f'service {STORE_SERVICE_NAME!r}: NOT_FOUND'),
    'listener stopped (an RPC error)': ('STOPPED', 'UNAVAILABLE'),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('state', 'printed'), list(_REVERSE_NOT_SERVING.values()), ids=list(_REVERSE_NOT_SERVING))
async def test_the_reverse_leg_fails_unless_both_names_read_serving(tmp_path: Path, state: str, printed: str):
    """data_ingest dialled data_store's own bind, once, and either name did not read SERVING: exit 1, why printed."""
    result, logs, store_port = await _reverse_leg(tmp_path, state)
    assert result.returncode == 1, _report(result)
    assert _dials(logs, DATA_INGEST) == [f'{STORE_HOST}:{store_port}'], logs['grpc']
    annotations = _annotations(result)
    assert len(annotations) == 1, _report(result)
    # In the step's output, not necessarily its ::error:: line: the probe prints one line per name and the
    # annotation carries the first only (a non-blocking finding on tj-7zjov1.49's validator note).
    assert printed in result.stdout, f'expected {printed!r} in the step output:\n{_report(result)}'
    assert 'gRPC peer reach holds: data_ingest' not in result.stdout, _report(result)


def test_the_reverse_leg_and_the_store_healthcheck_name_the_service_by_its_proto_name():
    """The StoreService literal in the step and in the compose healthcheck is common.rpc.store.SERVICE_NAME.

    Both probes import nothing of ours, so the name is written out; a renamed proto package would leave
    them asking for a name nothing registers, which reads NOT_FOUND -- red, but for the wrong reason.
    """
    healthcheck = ((load(BASE_FILE).get('services') or {}).get(DATA_STORE) or {}).get('healthcheck') or {}
    texts = {PEER_STEP: _system_step(PEER_STEP).get('run') or '', 'data_store healthcheck': str(healthcheck)}
    pattern = re.compile(r'trader_joe\.proto\.[\w.]*StoreService\b')
    for where, text in texts.items():
        assert set(pattern.findall(text)) == {STORE_SERVICE_NAME}, f'{where} names {pattern.findall(text)}'


def _public_target() -> str:
    """The lockdown step's literal egress target, as '<address> <port>' (the stub's reach key)."""
    script = _system_step(LOCKDOWN_STEP).get('run') or ''
    address = re.search(r'^\s*PUBLIC_ADDRESS="?([^"\s]+)"?$', script, re.MULTILINE)
    port = re.search(r'^\s*PUBLIC_PORT="?([0-9]+)"?$', script, re.MULTILINE)
    assert address and port, f'{LOCKDOWN_STEP} no longer sets PUBLIC_ADDRESS and PUBLIC_PORT literals'
    return f'{address.group(1)} {port.group(1)}'


# data_store's store_api address: what test_client, which shares store_api, resolves data_store to.
CLIENT_SEES_STORE = DS_API_V4


def _lockdown_scenario(reach: dict[str, dict[str, int]] | None = None, **changes: object) -> dict:
    """A locked-down prod stack: test_client sees data_store only; each gRPC port is reached only by the peer.

    Since tj-7zjov1.49 data_store's gRPC port too: data_ingest connects to it on data_store's ingest_store
    address (the control), and test_client is refused on every data_store address -- unreachable on
    ingest_store and store_db, which it never joins, and refused on store_api, which it shares but where
    nothing listens on the gRPC port.
    """
    public = _public_target()
    scenario = {
        'container_env': {GRPC_HOST_ENV: HOST, GRPC_PORT_ENV: PORT, 'PATH': CONTAINER_PATH},
        'store_env': {GRPC_HOST_ENV: STORE_HOST, GRPC_PORT_ENV: STORE_PORT, 'PATH': CONTAINER_PATH},
        'ps_id': CONTAINER_ID,
        'networks': {STORE_NET: _net(STORE_V4), EGRESS_NET: _net(EGRESS_V4)},
        'store_ps_id': STORE_CONTAINER_ID,
        'store_networks': {STORE_NET: _net(DS_INGEST_V4), API_NET: _net(DS_API_V4), DB_NET: _net(DS_DB_V4)},
        'client_resolves': {DATA_STORE: [CLIENT_SEES_STORE]},
        'reach': {
            DATA_STORE: {public: errno.ENETUNREACH, f'{STORE_V4} {PORT}': 0},
            DATA_INGEST: {f'{DS_INGEST_V4} {STORE_PORT}': 0},
            POSTGRES: {public: errno.ENETUNREACH},
            TEST_CLIENT: {
                f'{STORE_V4} {PORT}': errno.ENETUNREACH,
                f'{DS_INGEST_V4} {STORE_PORT}': errno.ENETUNREACH,
                f'{DS_API_V4} {STORE_PORT}': errno.ECONNREFUSED,
                f'{DS_DB_V4} {STORE_PORT}': errno.ENETUNREACH,
            },
        },
    }
    for container, answers in (reach or {}).items():
        scenario['reach'][container] = {**scenario['reach'][container], **answers}
    scenario.update(changes)
    return scenario


_DUAL_STACK = {'networks': {STORE_NET: _net(STORE_V4, STORE_V6), EGRESS_NET: _net(EGRESS_V4, EGRESS_V6)}}
_DUAL_REACH = {DATA_STORE: {f'{STORE_V6} {PORT}': 0}, TEST_CLIENT: {f'{STORE_V6} {PORT}': errno.ENETUNREACH}}

_LOCKDOWN_PASSING = {
    'prod': (lambda: _lockdown_scenario(), [STORE_V4]),
    'dual-stack: both ingest_store families': (
        lambda: _lockdown_scenario(_DUAL_REACH, **_DUAL_STACK),
        [STORE_V4, STORE_V6],
    ),
    # The retired agent stack's project name, kept as a prefix no live project uses (tj-iee6sn.27).
    'another project prefix (not the checkout directory)': (
        lambda: _lockdown_scenario(
            networks={
                'trader_joe_agent_stack_ingest_store': _net(STORE_V4),
                'trader_joe_agent_stack_ingest_egress': _net(EGRESS_V4),
            }
        ),
        [STORE_V4],
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('build', 'addresses'), list(_LOCKDOWN_PASSING.values()), ids=list(_LOCKDOWN_PASSING))
async def test_the_lockdown_passes_when_test_client_neither_resolves_nor_reaches_the_grpc_port(
    tmp_path: Path, build, addresses: list[str]
):
    """Every ingest_store address: data_store connects first (the control), then test_client is refused.

    And the alias is among the names test_client looks up and must not resolve. Only ingest_store
    addresses are dialled -- never data_ingest's egress address, where the server does not listen.
    """
    result, logs = await _run_step(tmp_path, LOCKDOWN_STEP, build())
    assert result.returncode == 0, _report(result)
    assert not logs['unmodelled'], logs['unmodelled']
    grpc_connects = [(call['container'], call['host']) for call in logs['connect'] if call['port'] == int(PORT)]
    expected = [(container, address) for address in addresses for container in (DATA_STORE, TEST_CLIENT)]
    assert grpc_connects == expected, f'the gRPC port was probed as {grpc_connects}, expected {expected}'
    looked_up = [call[-1] for call in logs['docker'] if 'getent' in call]
    assert HOST in looked_up, f'test_client never looked up {HOST}: {looked_up}'
    # tj-7zjov1.49: data_store's gRPC port, the control from data_ingest on its ingest_store address,
    # then test_client on EVERY data_store address, store_api's (the network test_client shares) included.
    store_connects = [(call['container'], call['host']) for call in logs['connect'] if call['port'] == int(STORE_PORT)]
    expected_store = [(DATA_INGEST, DS_INGEST_V4)] + [(TEST_CLIENT, a) for a in (DS_INGEST_V4, DS_API_V4, DS_DB_V4)]
    assert store_connects == expected_store, f'the store gRPC port was probed as {store_connects}'
    assert STORE_HOST in looked_up, f'test_client never looked up {STORE_HOST}: {looked_up}'


# Each must exit 1 for its own reason (the fragment it prints), never a stub that met a call it does
# not model.
_LOCKDOWN_FAILING = {
    'test_client resolves the gRPC alias': (
        lambda: _lockdown_scenario(client_resolves={DATA_STORE: [CLIENT_SEES_STORE], HOST: [STORE_V4]}),
        f'test_client resolved {HOST}',
    ),
    'test_client resolves data_ingest': (
        lambda: _lockdown_scenario(client_resolves={DATA_STORE: [CLIENT_SEES_STORE], DATA_INGEST: [STORE_V4]}),
        f'test_client resolved {DATA_INGEST}',
    ),
    'test_client connects to the gRPC port': (
        lambda: _lockdown_scenario({TEST_CLIENT: {f'{STORE_V4} {PORT}': 0}}),
        "test_client connected to data_ingest's gRPC port",
    ),
    'dual-stack, test_client connects over IPv6': (
        lambda: _lockdown_scenario(
            {DATA_STORE: {f'{STORE_V6} {PORT}': 0}, TEST_CLIENT: {f'{STORE_V6} {PORT}': 0}}, **_DUAL_STACK
        ),
        f"test_client connected to data_ingest's gRPC port at {STORE_V6}",
    ),
    'test_client probe did not run (exit 125)': (
        lambda: _lockdown_scenario(client_python_exit=125),
        'gRPC reach probe failed with exit 125',
    ),
    'the control: data_store cannot connect either': (
        lambda: _lockdown_scenario({DATA_STORE: {f'{STORE_V4} {PORT}': errno.ECONNREFUSED}}),
        "data_store could not connect to data_ingest's gRPC port",
    ),
    'no gRPC port in data_ingest': (
        lambda: _lockdown_scenario(container_env={GRPC_HOST_ENV: HOST, 'PATH': CONTAINER_PATH}),
        'no data_ingest container or no APP_INTERNAL_GRPC_PORT',
    ),
    'no data_ingest container': (
        lambda: _lockdown_scenario(ps_id=''),
        'no data_ingest container or no APP_INTERNAL_GRPC_PORT',
    ),
    'not on any *_ingest_store network': (
        lambda: _lockdown_scenario(networks={EGRESS_NET: _net(EGRESS_V4)}),
        'reports no ingest_store address',
    ),
    'inspect reports no ingest_store address': (
        lambda: _lockdown_scenario(networks={STORE_NET: _net(), EGRESS_NET: _net(EGRESS_V4)}),
        'reports no ingest_store address',
    ),
    'data_ingest not running': (lambda: _lockdown_scenario(exec_fails=[DATA_INGEST]), ''),
    'data_store has egress': (
        lambda: _lockdown_scenario({DATA_STORE: {_public_target(): 0}}),
        'data_store connected to',
    ),
    'postgres has egress': (lambda: _lockdown_scenario({POSTGRES: {_public_target(): 0}}), 'postgres connected to'),
    # tj-7zjov1.49: data_store's gRPC port and alias.
    'test_client resolves the store gRPC alias': (
        lambda: _lockdown_scenario(client_resolves={DATA_STORE: [CLIENT_SEES_STORE], STORE_HOST: [DS_INGEST_V4]}),
        f'test_client resolved {STORE_HOST}',
    ),
    'test_client connects to the store gRPC port on store_api (a mis-bind onto the shared network)': (
        lambda: _lockdown_scenario({TEST_CLIENT: {f'{DS_API_V4} {STORE_PORT}': 0}}),
        f"test_client connected to data_store's gRPC port at {DS_API_V4}:{STORE_PORT}",
    ),
    'test_client connects to the store gRPC port on ingest_store': (
        lambda: _lockdown_scenario({TEST_CLIENT: {f'{DS_INGEST_V4} {STORE_PORT}': 0}}),
        f"test_client connected to data_store's gRPC port at {DS_INGEST_V4}:{STORE_PORT}",
    ),
    'test_client connects to the store gRPC port on store_db': (
        lambda: _lockdown_scenario({TEST_CLIENT: {f'{DS_DB_V4} {STORE_PORT}': 0}}),
        f"test_client connected to data_store's gRPC port at {DS_DB_V4}:{STORE_PORT}",
    ),
    'dual-stack, test_client connects to the store_api IPv6 address': (
        lambda: _lockdown_scenario(
            {TEST_CLIENT: {f'{DS_API_V6} {STORE_PORT}': 0}},
            store_networks={STORE_NET: _net(DS_INGEST_V4), API_NET: _net(DS_API_V4, DS_API_V6), DB_NET: _net(DS_DB_V4)},
        ),
        f"test_client connected to data_store's gRPC port at {DS_API_V6}:{STORE_PORT}",
    ),
    'the store control: data_ingest cannot connect': (
        lambda: _lockdown_scenario({DATA_INGEST: {f'{DS_INGEST_V4} {STORE_PORT}': errno.ECONNREFUSED}}),
        "data_ingest could not connect to data_store's gRPC port",
    ),
    'the store probe did not run (exit 125)': (
        lambda: _lockdown_scenario(client_python_exit_for={STORE_PORT: 125}),
        'test_client data_store gRPC reach probe failed with exit 125',
    ),
    'the store probe crashed (exit 1)': (
        lambda: _lockdown_scenario(client_python_exit_for={STORE_PORT: 1}),
        'test_client data_store gRPC reach probe failed with exit 1,',
    ),
    'no gRPC port in data_store': (
        lambda: _lockdown_scenario(store_env={GRPC_HOST_ENV: STORE_HOST, 'PATH': CONTAINER_PATH}),
        'no data_store container or no APP_INTERNAL_GRPC_PORT',
    ),
    'no data_store container': (
        lambda: _lockdown_scenario(store_ps_id=''),
        'no data_store container or no APP_INTERNAL_GRPC_PORT',
    ),
    'data_store not on any *_ingest_store network': (
        lambda: _lockdown_scenario(store_networks={API_NET: _net(DS_API_V4), DB_NET: _net(DS_DB_V4)}),
        'reports no ingest_store address for data_store',
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('build', 'printed'), list(_LOCKDOWN_FAILING.values()), ids=list(_LOCKDOWN_FAILING))
async def test_the_lockdown_fails_on_any_reach_or_any_missing_fact(tmp_path: Path, build, printed: str):
    """test_client resolving or reaching the port, a failed control or probe, or a missing fact: exit 1."""
    result, logs = await _run_step(tmp_path, LOCKDOWN_STEP, build())
    assert result.returncode == 1, _report(result)
    assert not logs['unmodelled'], logs['unmodelled']
    assert printed in result.stdout, f'expected the step to say {printed!r}:\n{_report(result)}'


# --- tj-7zjov1.378: an unset IPv6 address, however the engine renders it ----------------------------
#
# Both lockdown inspect sites -- data_ingest's ingest_store addresses (probed from data_store, then
# test_client) and data_store's addresses (probed from data_ingest, then test_client on every one) --
# keep only the fields that are addresses. The scenario carries the engine's rendering of the unset
# IPv6 as the GlobalIPv6Address string, which the shared renderer prints verbatim (pinned in
# test_grpc_bind_network.py). A junk word that reached a probe would be dialled: the stub routes no
# such host, so the call goes UNMODELLED, and the connect log shows it.

JUNK_HOSTS = frozenset({'invalid', 'IP', '<invalid', 'IP>', ''})
EXPECTED_STORE_CONNECTS = [(DATA_INGEST, DS_INGEST_V4)] + [
    (TEST_CLIENT, address) for address in (DS_INGEST_V4, DS_API_V4, DS_DB_V4)
]


def _ingest_unset(unset: str) -> dict:
    return {'networks': {STORE_NET: _net(STORE_V4, unset), EGRESS_NET: _net(EGRESS_V4, unset)}}


def _store_unset(unset: str) -> dict:
    return {
        'store_networks': {
            STORE_NET: _net(DS_INGEST_V4, unset),
            API_NET: _net(DS_API_V4, unset),
            DB_NET: _net(DS_DB_V4, unset),
        }
    }


def _probes(logs: dict, port: str) -> list[tuple[str, str]]:
    return [(call['container'], call['host']) for call in logs['connect'] if call['port'] == int(port)]


def _assert_no_junk_dialled(logs: dict, result: subprocess.CompletedProcess) -> None:
    dialled = {call['host'] for call in logs['connect']}
    assert not dialled & JUNK_HOSTS, f'a non-address was dialled: {sorted(dialled & JUNK_HOSTS)}\n{_report(result)}'
    assert not logs['unmodelled'], logs['unmodelled']


@pytest.mark.asyncio
@pytest.mark.parametrize('unset', list(UNSET_V6.values()), ids=list(UNSET_V6))
async def test_the_lockdown_probes_only_data_ingests_real_addresses_when_its_ipv6_is_unset(tmp_path: Path, unset: str):
    """Site 2: data_ingest's ingest_store address set is its IPv4 address alone, probed control first."""
    result, logs = await _run_step(tmp_path, LOCKDOWN_STEP, _lockdown_scenario(**_ingest_unset(unset)))
    assert result.returncode == 0, _report(result)
    _assert_no_junk_dialled(logs, result)
    assert _probes(logs, PORT) == [(DATA_STORE, STORE_V4), (TEST_CLIENT, STORE_V4)], logs['connect']
    assert _probes(logs, STORE_PORT) == EXPECTED_STORE_CONNECTS, logs['connect']


@pytest.mark.asyncio
@pytest.mark.parametrize('unset', list(UNSET_V6.values()), ids=list(UNSET_V6))
async def test_the_lockdown_probes_only_data_stores_real_addresses_when_its_ipv6_is_unset(tmp_path: Path, unset: str):
    """Site 3: the control dials data_store's ingest_store IPv4 alone, test_client its three IPv4 addresses."""
    result, logs = await _run_step(tmp_path, LOCKDOWN_STEP, _lockdown_scenario(**_store_unset(unset)))
    assert result.returncode == 0, _report(result)
    _assert_no_junk_dialled(logs, result)
    assert _probes(logs, PORT) == [(DATA_STORE, STORE_V4), (TEST_CLIENT, STORE_V4)], logs['connect']
    assert _probes(logs, STORE_PORT) == EXPECTED_STORE_CONNECTS, logs['connect']


@pytest.mark.asyncio
@pytest.mark.parametrize('unset', list(UNSET_V6.values()), ids=list(UNSET_V6))
async def test_the_lockdown_still_probes_a_real_ipv6_beside_an_unset_one(tmp_path: Path, unset: str):
    """Dual-stack on ingest_store for both services, the other networks unset: each real IPv6 is dialled as one."""
    reach = {
        DATA_STORE: {f'{STORE_V6} {PORT}': 0},
        DATA_INGEST: {f'{DS_INGEST_V6} {STORE_PORT}': 0},
        TEST_CLIENT: {f'{STORE_V6} {PORT}': errno.ENETUNREACH, f'{DS_INGEST_V6} {STORE_PORT}': errno.ENETUNREACH},
    }
    scenario = _lockdown_scenario(
        reach,
        networks={STORE_NET: _net(STORE_V4, STORE_V6), EGRESS_NET: _net(EGRESS_V4, unset)},
        store_networks={
            STORE_NET: _net(DS_INGEST_V4, DS_INGEST_V6),
            API_NET: _net(DS_API_V4, unset),
            DB_NET: _net(DS_DB_V4, unset),
        },
    )
    result, logs = await _run_step(tmp_path, LOCKDOWN_STEP, scenario)
    assert result.returncode == 0, _report(result)
    _assert_no_junk_dialled(logs, result)
    assert _probes(logs, PORT) == [
        (DATA_STORE, STORE_V4),
        (TEST_CLIENT, STORE_V4),
        (DATA_STORE, STORE_V6),
        (TEST_CLIENT, STORE_V6),
    ], logs['connect']
    assert _probes(logs, STORE_PORT) == [
        (DATA_INGEST, DS_INGEST_V4),
        (DATA_INGEST, DS_INGEST_V6),
        (TEST_CLIENT, DS_INGEST_V4),
        (TEST_CLIENT, DS_INGEST_V6),
        (TEST_CLIENT, DS_API_V4),
        (TEST_CLIENT, DS_DB_V4),
    ], logs['connect']
    v6_families = {call['family'] for call in logs['connect'] if call['host'] in (STORE_V6, DS_INGEST_V6)}
    assert v6_families == {int(socket.AF_INET6)}, logs['connect']


# (scenario builder taking the rendering, the fragment the step prints)
_LOCKDOWN_UNSET_FAILING = {
    'site 2: test_client connects to the gRPC port': (
        lambda unset: _lockdown_scenario({TEST_CLIENT: {f'{STORE_V4} {PORT}': 0}}, **_ingest_unset(unset)),
        f"test_client connected to data_ingest's gRPC port at {STORE_V4}:{PORT}",
    ),
    'site 2: inspect reports no ingest_store address': (
        lambda unset: _lockdown_scenario(networks={STORE_NET: _net('', unset), EGRESS_NET: _net(EGRESS_V4, unset)}),
        'docker inspect reports no ingest_store address for data_ingest',
    ),
    'site 3: test_client connects to the store gRPC port on store_api': (
        lambda unset: _lockdown_scenario({TEST_CLIENT: {f'{DS_API_V4} {STORE_PORT}': 0}}, **_store_unset(unset)),
        f"test_client connected to data_store's gRPC port at {DS_API_V4}:{STORE_PORT}",
    ),
    'site 3: inspect reports no ingest_store address for data_store': (
        lambda unset: _lockdown_scenario(
            store_networks={STORE_NET: _net('', unset), API_NET: _net(DS_API_V4, unset), DB_NET: _net(DS_DB_V4, unset)}
        ),
        'docker inspect reports no ingest_store address for data_store',
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize('unset', list(UNSET_V6.values()), ids=list(UNSET_V6))
@pytest.mark.parametrize(
    ('build', 'printed'), list(_LOCKDOWN_UNSET_FAILING.values()), ids=list(_LOCKDOWN_UNSET_FAILING)
)
async def test_the_lockdown_keeps_its_meaning_with_an_unset_ipv6(tmp_path: Path, unset: str, build, printed: str):
    """A reach from test_client, or no ingest_store address, still fails with the same message; no junk is dialled."""
    result, logs = await _run_step(tmp_path, LOCKDOWN_STEP, build(unset))
    assert result.returncode == 1, _report(result)
    _assert_no_junk_dialled(logs, result)
    assert printed in result.stdout, f'expected the step to say {printed!r}:\n{_report(result)}'


# --- Item 3: no compose ordering between the two services (ADR tj-8konfu D6.5 = O1) ---------------------


def test_no_depends_on_between_data_store_and_data_ingest():
    """O1: a lazy channel and per-call wait_for_ready, so NO depends_on either way, in any launch set.

    Read from every file each set loads, not only the merged model, so the pin does not rest on how
    compose merges depends_on: an overlay adding the edge is caught whatever the merge would make of it.
    """
    files = sorted({path for launch_set in DATA_INGEST_LAUNCH_SETS for path in _set_files(launch_set)})
    assert BASE_FILE in files, f'no launch set loads {BASE_FILE.name}: {[path.name for path in files]}'
    documents = [(path.name, load(path)) for path in files]
    documents += [(f'{launch_set} (merged)', _model(launch_set)) for launch_set in DATA_INGEST_LAUNCH_SETS]
    edges = [
        f'{name}: {service} depends_on {peer}'
        for name, document in documents
        for service, peer in ((DATA_STORE, DATA_INGEST), (DATA_INGEST, DATA_STORE))
        if peer in (((document.get('services') or {}).get(service) or {}).get('depends_on') or [])
    ]
    assert not edges, f'compose orders data_store and data_ingest, which D6.5 = O1 rules out: {edges}'
