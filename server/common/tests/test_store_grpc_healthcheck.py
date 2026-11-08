"""build_infra pins for data_store's compose healthcheck and its gRPC half (tj-7zjov1.49).

Design: the bead (P34b, raised at the P32 architect gate tj-7zjov1.16 from validator finding (b)) and ADR
tj-8konfu D6 -- the same shape as data_ingest's healthcheck, pinned in test_grpc_peer_reach.py item 1.
data_store's healthcheck keeps HTTP /ping and adds the standard grpc.health.v1 Health service on the
store's own gRPC server: Check '' and Check for StoreService by its full name must both read SERVING. It
dials APP_INTERNAL_GRPC_HOST:APP_INTERNAL_GRPC_PORT from the container environment, never loopback (ADR
tj-q9ae5u addendum 3: the server binds the data-store-grpc alias's ingest_store address alone).

The probe's own python runs here, with only the environment the container would hand it, against a real
HTTP /ping and a real gRPC listener bound OFF 127.0.0.1 -- on 127.0.0.2, still the loopback interface,
so nothing leaves the machine. The SERVING listener is production's GrpcServerHost serving StoreService,
as data_store's lifespan hosts it.

What only Docker can show -- compose running this probe in the real image, `up --wait` going healthy,
the alias resolving to the ingest_store address -- is CI System Testing's run. NOTHING here has run
against a live stack.
"""

import asyncio
import re
import sys
import time

import pytest

from common.rpc.server import GRPC_HOST_ENV, GRPC_PORT_ENV
from common.rpc.store import SERVICE_NAME as STORE_SERVICE_NAME
from common.tests.compose_model import BASE_FILE, interpolate, load
from common.tests.test_ci_invariants import ENV_DEFAULT_FILE, _env_file_values
from common.tests.test_grpc_peer_reach import LOOPBACK, OFF_LOOPBACK, VENV_PYTHON, _free_port, _ping_server, _store_peer
from common.tests.test_store_grpc_wiring import _sets


pytestmark = pytest.mark.build_infra

DATA_STORE = 'data_store'


def _base_healthcheck() -> dict:
    return ((load(BASE_FILE).get('services') or {}).get(DATA_STORE) or {}).get('healthcheck') or {}


def _probe_script() -> str:
    """The python the healthcheck runs: the exec form, so the interpreter is the image's venv."""
    test = _base_healthcheck().get('test')
    assert isinstance(test, list) and len(test) == 4 and test[:3] == ['CMD', VENV_PYTHON, '-c'], (
        f'{DATA_STORE} healthcheck is {test!r}; this module runs [CMD, {VENV_PYTHON}, -c, <script>]'
    )
    return str(test[3])


def _declared_timeout_s() -> float:
    value = str(_base_healthcheck().get('timeout', ''))
    match = re.fullmatch(r'([0-9]+)s', value)
    assert match, f'{DATA_STORE} healthcheck timeout {value!r} is not whole seconds'
    return float(match.group(1))


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
    stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
    return process.returncode, f'stdout:\n{stdout.decode()}\nstderr:\n{stderr.decode()}', time.monotonic() - started


def test_the_healthcheck_reaches_the_container_as_written():
    """Compose interpolation leaves the probe untouched, so it reads its addresses from os.environ when it runs."""
    script = _probe_script()
    for env in ({}, _env_file_values(ENV_DEFAULT_FILE)):
        assert interpolate(script, env) == script, f'compose rewrites {DATA_STORE} healthcheck script: {script!r}'


def test_the_healthcheck_keeps_ping_and_asks_both_names():
    """The HTTP half stays, and the gRPC half asks '' and StoreService by its proto name, never loopback."""
    script = _probe_script()
    assert "/ping'" in script and "'http://127.0.0.1:' + os.environ['APP_INTERNAL_PORT']" in script, script
    assert f"('', '{STORE_SERVICE_NAME}')" in script, f'the probe does not ask both health names: {script}'
    assert "os.environ['APP_INTERNAL_GRPC_HOST']" in script and "os.environ['APP_INTERNAL_GRPC_PORT']" in script
    grpc_half = script[script.index('import grpc') :].split('/ping', 1)[-1]
    assert '127.0.0.1' not in grpc_half and 'localhost' not in grpc_half, f'the gRPC half dials loopback: {script}'


def test_every_launch_set_that_runs_data_store_runs_the_base_files_healthcheck():
    """No overlay replaces the two-halved probe: every set that runs data_store checks gRPC health too."""
    sets = _sets()
    assert sets, 'no launch set runs data_store'
    drifted = {
        label: (model['services'][DATA_STORE] or {}).get('healthcheck')
        for label, (model, _) in sets.items()
        if (model['services'][DATA_STORE] or {}).get('healthcheck') != _base_healthcheck()
    }
    assert not drifted, f'these sets run {DATA_STORE} with another healthcheck: {drifted}'


@pytest.mark.asyncio
async def test_the_healthcheck_passes_when_ping_answers_and_both_names_read_serving():
    """/ping 200, '' and StoreService SERVING on production's host bound to 127.0.0.2: exit 0, inside the timeout."""
    with _ping_server(200) as http_port:
        async with _store_peer(OFF_LOOPBACK, 'SERVING') as grpc_port:
            status, output, elapsed = await _run_probe(_container_env(http_port, OFF_LOOPBACK, grpc_port))
    assert status == 0, output
    assert elapsed < _declared_timeout_s(), f'the probe took {elapsed:.1f}s of a {_declared_timeout_s()}s timeout'


@pytest.mark.asyncio
async def test_the_healthcheck_never_settles_for_a_listener_on_loopback():
    """A listener on 127.0.0.1 alone, the environment naming 127.0.0.2 on the same port: red, UNAVAILABLE.

    A probe that fell back to loopback, or ignored the host the environment names, would pass here.
    """
    with _ping_server(200) as http_port:
        async with _store_peer(LOOPBACK, 'SERVING') as grpc_port:
            status, output, _ = await _run_probe(_container_env(http_port, OFF_LOOPBACK, grpc_port))
    assert status != 0, output
    assert 'UNAVAILABLE' in output, output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('state', 'printed'),
    [
        ('OVERALL_NOT_SERVING', "gRPC health for '' at"),
        ('STORE_NOT_SERVING', f"gRPC health for '{STORE_SERVICE_NAME}' at"),
        ('NO_STORE_SERVICE', 'NOT_FOUND'),
        ('STOPPED', 'UNAVAILABLE'),
    ],
    ids=["'' not serving", 'StoreService not serving', 'StoreService unregistered (NOT_FOUND)', 'listener stopped'],
)
async def test_the_healthcheck_fails_unless_both_names_read_serving(state: str, printed: str):
    """/ping answers but gRPC health does not read SERVING for both names: red, naming why, inside the timeout."""
    with _ping_server(200) as http_port:
        async with _store_peer(OFF_LOOPBACK, state) as grpc_port:
            status, output, elapsed = await _run_probe(_container_env(http_port, OFF_LOOPBACK, grpc_port))
    assert status != 0, output
    assert printed in output, output
    if state.endswith('NOT_SERVING'):
        assert 'reads NOT_SERVING, not SERVING' in output, output
    assert elapsed < _declared_timeout_s(), f'the probe took {elapsed:.1f}s of a {_declared_timeout_s()}s timeout'


@pytest.mark.asyncio
@pytest.mark.parametrize('ping', ['refused', 'HTTP 500'])
async def test_the_healthcheck_fails_when_ping_does_not_answer(ping: str):
    """Both names read SERVING but /ping is down or failing: red. Both halves must pass."""
    async with _store_peer(OFF_LOOPBACK, 'SERVING') as grpc_port:
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
        async with _store_peer(OFF_LOOPBACK, 'SERVING') as grpc_port:
            status, output, _ = await _run_probe(_container_env(http_port, OFF_LOOPBACK, grpc_port, without=missing))
    assert status != 0, output
    assert missing in output, output
