"""The latency harness's REST arm: one httpx client for the process, built only when the harness is on.

tj-3mk3u5.61. The harness measures transports for tj-3mk3u5.26, the one irreversible measurement, so
what it must NOT measure is connection setup or the client's own bookkeeping. Three things are pinned
here, and one thing deliberately is not.

WHAT IS PINNED

1. Production safety. With LATENCY_TEST_ENABLED off -- the production default -- importing
   routers/common/latency.py and calling initialize_latency_client() builds no client, registers no
   route, imports httpx at all, and opens no socket. Same shape as the harness-OFF pin on
   tj-3mk3u5.60 (data/ingest/tests/test_grpc_host.py), and for the same reason: half of it is an
   IMPORT fact, so it runs in a fresh interpreter. It could never be asserted in process -- this very
   test session imports fastapi.testclient, which imports httpx.
2. The client is built ONCE, in initialize_latency_client, with the limits the architect ruled on
   2026-10-03 after the builder's measurement: max_connections=None (so concurrent iterations never
   queue inside the client on httpx's default of 100) and max_keepalive_connections=20 (retaining a
   large idle pool cost about 6x on the second of three back-to-back 200-iteration runs, and the
   uvicorn 5 s keep-alive closes hoarded sockets underneath the client anyway). This SUPERSEDES the
   task's original "no pool or keepalive cap"; the numbers are in the bead.
3. The handler reuses that client and builds none of its own, and its request line is the one
   tj-3mk3u5.8 fixed: content= plus an explicit JSON content type. Sending the model as data= instead
   made the internal endpoint answer 422 on 200 of 200 calls, and the harness reported the failure as
   latency.

WHAT IS NOT PINNED, ON PURPOSE: anything about how FAST the arm is. The harness itself is untested by
the user's decision (tj-3mk3u5.8), and a timing assertion in the PR gate is a flake generator that
measures the CI runner rather than the code. The 6x figure above belongs to the throwaway measurement
rig recorded on the bead, not to this suite. These pins are all structural and deterministic.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Final

import httpx
import pytest
from fastapi.routing import APIRoute

import common.rpc.latency as grpc_arm
import common.timer
import routers.common.latency as latency
from common.kafka.topics import ConsumerGroup
from common.tests.image_path import image_pythonpath
from routers.common.app_endpoints import InterfaceRest
from schemas.common.latency import LatencyRequest


pytestmark = pytest.mark.common

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
EXIT_BUDGET_S: Final = 30

# The ruled configuration, spelled out rather than read from the module under test: a pin that imports
# its expected value from the code it guards asserts nothing.
RULED_MAX_CONNECTIONS: Final = None
RULED_MAX_KEEPALIVE_CONNECTIONS: Final = 20


# ---------------------------------------------------------------------------------------------------
# HARNESS OFF: THE PRODUCTION DEFAULT

# The child gets PYTHONPATH and nothing else, so LATENCY_TEST_ENABLED is unset exactly as in
# production. socket.socket is replaced before the first project import, so ANY socket built on the
# way -- by this module, by a top-level client, by a channel -- is a hard error rather than a thing
# the probe would have to go looking for afterwards.
HARNESS_OFF_PROBE = """
import socket
import sys


class _ForbiddenSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        raise AssertionError('a socket was constructed with the latency harness off')


socket.socket = _ForbiddenSocket

from common.kafka.topics import ConsumerGroup
import routers.common.latency as latency


class _AppStub:
    def __init__(self):
        self.routers = []

    def include_router(self, router):
        self.routers.append(router)


app = _AppStub()
latency.initialize_latency_client(app, 'probe', 1, ConsumerGroup.COMMON_GROUP)

print('client=' + repr(getattr(latency, '__REST_CLIENT')), flush=True)
print('httpx_imported=' + repr('httpx' in sys.modules), flush=True)
print('routers=' + repr(len(app.routers)), flush=True)
"""


def test_with_the_harness_off_no_rest_client_is_built_and_httpx_is_never_imported():
    """Production runs with the flag off, and a dev-only measurement client must not exist there.

    The httpx half is the one that protects production: initialize_latency_client imports httpx INSIDE
    the flag check, so a top-level `import httpx` added later -- which would still leave the client
    None and pass the first assertion -- is caught here.
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
    assert done.stdout.splitlines() == ['client=None', 'httpx_imported=False', 'routers=0'], done.stderr


# ---------------------------------------------------------------------------------------------------
# HARNESS ON: ONE CLIENT, THE RULED LIMITS, AND THE REQUEST LINE
#
# Everything the init touches besides the REST client is stood in for. The Kafka factory starts
# threads and the gRPC arm wants a reachable target; neither is what this file is about, and the PR
# gate may reach neither. What stays real is every line of initialize_latency_client that concerns the
# REST arm, and the whole request handler.


class _StubRpcClient:
    async def send_request(self, request):
        return SimpleNamespace(success=None)


class _StubRpcClients:
    def get_client(self, endpoint):
        return _StubRpcClient()


class _StubKafkaRpcFactory:
    """Stands in for KafkaRpcFactory with the three calls initialize_latency_client makes."""

    def __init__(self, params):
        self.params = params

    def add_client(self, endpoint, timeout):
        return None

    def init_clients(self):
        return _StubRpcClients()


class _StubProbeClient:
    def __init__(self, channel, timeout_s):
        self.channel = channel
        self.timeout_s = timeout_s


class _RecordingRestClient:
    """Records every post the handler makes, and answers 200 without touching the network."""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return SimpleNamespace(status_code=200)


@pytest.fixture
def harness_on(monkeypatch: pytest.MonkeyPatch):
    """Turn the harness on and neutralise everything in the init that is not the REST client.

    LATENCY_TEST_ENABLED is read ONCE, at import of routers/common/latency.py (:22), into a module
    attribute, so setting the environment variable now would change nothing. The module globals are
    reset because initialize_latency_client refuses a second call once __APP_NAME is set, and
    monkeypatch puts the real values back when the test ends.
    """
    monkeypatch.setattr(latency, 'LATENCY_TEST_ENABLED', True)
    for name in ('__APP_NAME', '__APP_PORT', '__REST_CLIENT', '__GRPC_CLIENT', '__PRC_CLIENTS'):
        monkeypatch.setattr(latency, name, None)
    monkeypatch.setattr(latency, 'target_from_env', lambda env_var: 'stub-target:1')
    monkeypatch.setattr(latency, 'create_channel', lambda target: SimpleNamespace(target=target))
    monkeypatch.setattr(grpc_arm, 'LatencyProbeClient', _StubProbeClient)
    monkeypatch.setattr(latency, 'get_rpc_params', lambda client_group: None)
    monkeypatch.setattr(latency, 'KafkaRpcFactory', _StubKafkaRpcFactory)


class _AppStub:
    """Captures the APIRouter initialize_latency_client builds, in place of a FastAPI app.

    FastAPI 0.141 defers an included router behind a private _IncludedRouter wrapper, so reading the
    registered route back off a real app means reaching into an implementation detail that is free to
    change. include_router is the only thing the init asks of the app.
    """

    def __init__(self):
        self.routers = []

    def include_router(self, router):
        self.routers.append(router)


def _rest_endpoint(app: _AppStub):
    """The handler initialize_latency_client registered, called directly rather than over a TestClient.

    Args:
        app (_AppStub): The app stub the init was given.

    Returns:
        The route's endpoint coroutine function.
    """
    assert len(app.routers) == 1, app.routers
    routes = [route for route in app.routers[0].routes if isinstance(route, APIRoute)]
    assert [route.path for route in routes] == [InterfaceRest.LATENCY]
    return routes[0].endpoint


def test_one_client_is_built_for_the_process_with_the_ruled_limits(harness_on, monkeypatch: pytest.MonkeyPatch):
    """One httpx.AsyncClient, built in the init, at max_connections=None and max_keepalive_connections=20.

    The limits are read off the constructor call rather than out of the client's internals: httpx keeps
    them in private httpcore pool attributes that are free to be renamed, and the Limits object is what
    the ruling is actually about.
    """
    real_client_class = httpx.AsyncClient
    built: list[dict] = []

    def recorder(*args, **kwargs):
        assert not args, f'the client was built with positional arguments: {args!r}'
        built.append(kwargs)
        return real_client_class(**kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', recorder)

    latency.initialize_latency_client(_AppStub(), 'probe', 1, ConsumerGroup.COMMON_GROUP)

    assert len(built) == 1, f'expected exactly one client for the process, got {len(built)}'
    assert built[0]['timeout'] == latency.LATENCY_TEST_TIMEOUT
    limits = built[0]['limits']
    assert limits.max_connections is RULED_MAX_CONNECTIONS
    assert limits.max_keepalive_connections == RULED_MAX_KEEPALIVE_CONNECTIONS
    assert isinstance(getattr(latency, '__REST_CLIENT'), real_client_class)


@pytest.mark.asyncio
async def test_the_handler_reuses_the_process_client_and_never_builds_its_own(
    harness_on, monkeypatch: pytest.MonkeyPatch
):
    """Every sample goes through the one client, so no sample pays for a pool that was just created.

    Two calls of three iterations, because one call cannot tell a process-lifetime client from a
    per-call one. Building a client is forbidden outright for the duration, which names the regression
    in the failure rather than leaving it to be inferred from a post count of zero.
    """
    app = _AppStub()
    latency.initialize_latency_client(app, 'probe', 1, ConsumerGroup.COMMON_GROUP)

    recorder = _RecordingRestClient()
    monkeypatch.setattr(latency, '__REST_CLIENT', recorder)

    def forbidden(*args, **kwargs):
        raise AssertionError('the request handler built its own httpx client; the process client is the point')

    monkeypatch.setattr(httpx, 'AsyncClient', forbidden)

    endpoint = _rest_endpoint(app)
    for _ in range(2):
        result = await endpoint(
            LatencyRequest(latency_type=LatencyRequest.LatencyType.REST, iterations=3, payload_size=1)
        )
        assert result['success'] is True, result

    assert len(recorder.posts) == 6
    assert len({url for url, _ in recorder.posts}) == 1


@pytest.mark.asyncio
async def test_the_rest_sample_posts_the_model_as_a_json_body(harness_on, monkeypatch: pytest.MonkeyPatch):
    """The request line tj-3mk3u5.8 fixed: content= plus an explicit JSON content type.

    Passing the serialised model as data= instead posts it form-encoded, the internal endpoint answers
    422, and the harness counts the failure as a sample. That was 200 of 200 calls, and nothing in the
    response says so, which is why it is pinned at the call rather than at the result.
    """
    app = _AppStub()
    latency.initialize_latency_client(app, 'probe', 1, ConsumerGroup.COMMON_GROUP)

    recorder = _RecordingRestClient()
    monkeypatch.setattr(latency, '__REST_CLIENT', recorder)

    await _rest_endpoint(app)(
        LatencyRequest(latency_type=LatencyRequest.LatencyType.REST, iterations=1, payload_size=1)
    )

    assert len(recorder.posts) == 1
    url, kwargs = recorder.posts[0]
    assert url.endswith(InterfaceRest.INTERNAL_LATENCY.value), url
    assert set(kwargs) == {'content', 'headers'}, 'the body must be sent as content=, never data= or json='
    assert kwargs['headers'] == {'Content-Type': 'application/json'}
    assert set(json.loads(kwargs['content'])) == {'payload'}


# ---------------------------------------------------------------------------------------------------
# common.timer.timeit IS GONE
#
# It had had no caller since a4d8041, and wrapped around an async function it timed coroutine CREATION
# rather than the call -- it would have reported the REST arm at roughly zero. The deletion is only
# worth anything while nothing reintroduces it, so both halves are pinned: the attribute is absent, and
# no module reaches for it.

TIMEIT_REFERENCE = re.compile(r'common\.timer\.timeit|from\s+common\.timer\s+import[^\n]*\btimeit\b|def\s+timeit\b')

SOURCE_TREES: Final = ('common', 'data', 'routers', 'schemas', 'tests', 'tools')


def test_common_timer_no_longer_offers_a_timeit_decorator():
    assert not hasattr(common.timer, 'timeit'), (
        'common.timer.timeit is back. Wrapped around an async function it stops its clock when the '
        'coroutine object is created, before the call has run: see routers/common/latency.py timed_send.'
    )


def test_nothing_in_the_repository_reaches_for_common_timer_timeit():
    """No caller left anywhere, which is what makes the deletion safe rather than merely done."""
    offenders = []
    for tree in SOURCE_TREES:
        for source in sorted((REPO_ROOT / tree).rglob('*.py')):
            if source == Path(__file__):
                continue
            for number, line in enumerate(source.read_text().splitlines(), start=1):
                if TIMEIT_REFERENCE.search(line):
                    offenders.append(f'{source.relative_to(REPO_ROOT)}:{number}: {line.strip()}')

    assert offenders == [], 'common.timer.timeit was deleted on tj-3mk3u5.61:\n' + '\n'.join(offenders)
