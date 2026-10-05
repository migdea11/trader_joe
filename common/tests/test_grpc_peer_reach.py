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

from common.rpc.ping import ping_service
from common.rpc.server import GRPC_HOST_ENV, GRPC_PORT_ENV, BindAddress, GrpcServerHost
from common.tests import grpc_peer_docker_stub as stub
from common.tests.compose_model import BASE_FILE, interpolate, load
from common.tests.test_ci_invariants import (
    ENV_DEFAULT_FILE,
    LOCKDOWN_STEP,
    PEER_STEP,
    REPO_ROOT,
    STAGED_ENV_FILE,
    _env_file_values,
    _system_step,
)
from common.tests.test_grpc_bind_network import (
    CONTAINER_ID,
    DATA_INGEST_LAUNCH_SETS,
    EGRESS_NET,
    EGRESS_V4,
    EGRESS_V6,
    HOST,
    PORT,
    STORE_NET,
    STORE_V4,
    STORE_V6,
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
        'PYTHONPATH': str(REPO_ROOT),
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


def _peer_scenario(port: int | str, **changes: object) -> dict:
    """A healthy stack: data_ingest's environment names the alias; data_store resolves it to the listener."""
    scenario = {
        'container_env': {GRPC_HOST_ENV: HOST, GRPC_PORT_ENV: str(port), 'PATH': CONTAINER_PATH},
        'store_dns': {HOST: OFF_LOOPBACK},
    }
    scenario.update(changes)
    return scenario


def _env_without(name: str, port: int | str = PORT) -> dict[str, str]:
    return {key: value for key, value in _peer_scenario(port)['container_env'].items() if key != name}


def _env_with(name: str, value: str, port: int | str = PORT) -> dict[str, str]:
    return {**_peer_scenario(port)['container_env'], name: value}


@pytest.mark.asyncio
async def test_the_peer_check_passes_when_data_store_gets_serving(tmp_path: Path):
    """From inside data_store, Health.Check '' on the host and port data_ingest's environment names, once."""
    async with _grpc_peer(OFF_LOOPBACK, 'SERVING') as port:
        result, logs = await _run_step(tmp_path, PEER_STEP, _peer_scenario(port))
    assert result.returncode == 0, _report(result)
    assert not logs['unmodelled'], logs['unmodelled']
    assert logs['grpc'] == [{'target': f'{HOST}:{port}'}], (
        f'data_store must dial {HOST}:{port}, the host and port data_ingest names, once: {logs["grpc"]}'
    )
    assert len(_execs_into(logs['docker'], DATA_STORE)) == 1, f'expected one call from data_store: {logs["docker"]}'
    assert f'{HOST}:{port}: SERVING' in result.stdout, _report(result)


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
    assert logs['grpc'] == [{'target': f'{HOST}:{port}'}], f'data_store never dialled {HOST}:{port}: {logs["grpc"]}'
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


def _public_target() -> str:
    """The lockdown step's literal egress target, as '<address> <port>' (the stub's reach key)."""
    script = _system_step(LOCKDOWN_STEP).get('run') or ''
    address = re.search(r'^\s*PUBLIC_ADDRESS="?([^"\s]+)"?$', script, re.MULTILINE)
    port = re.search(r'^\s*PUBLIC_PORT="?([0-9]+)"?$', script, re.MULTILINE)
    assert address and port, f'{LOCKDOWN_STEP} no longer sets PUBLIC_ADDRESS and PUBLIC_PORT literals'
    return f'{address.group(1)} {port.group(1)}'


CLIENT_SEES_STORE = '172.25.0.3'


def _lockdown_scenario(reach: dict[str, dict[str, int]] | None = None, **changes: object) -> dict:
    """A locked-down prod stack: test_client sees data_store only; only data_store reaches the gRPC port."""
    public = _public_target()
    scenario = {
        'container_env': {GRPC_HOST_ENV: HOST, GRPC_PORT_ENV: PORT, 'PATH': CONTAINER_PATH},
        'ps_id': CONTAINER_ID,
        'networks': {STORE_NET: _net(STORE_V4), EGRESS_NET: _net(EGRESS_V4)},
        'client_resolves': {DATA_STORE: [CLIENT_SEES_STORE]},
        'reach': {
            DATA_STORE: {public: errno.ENETUNREACH, f'{STORE_V4} {PORT}': 0},
            POSTGRES: {public: errno.ENETUNREACH},
            TEST_CLIENT: {f'{STORE_V4} {PORT}': errno.ENETUNREACH},
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
    'another project prefix (the agent stack)': (
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
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('build', 'printed'), list(_LOCKDOWN_FAILING.values()), ids=list(_LOCKDOWN_FAILING))
async def test_the_lockdown_fails_on_any_reach_or_any_missing_fact(tmp_path: Path, build, printed: str):
    """test_client resolving or reaching the port, a failed control or probe, or a missing fact: exit 1."""
    result, logs = await _run_step(tmp_path, LOCKDOWN_STEP, build())
    assert result.returncode == 1, _report(result)
    assert not logs['unmodelled'], logs['unmodelled']
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
