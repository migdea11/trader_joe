"""Scaffolding for driving the latency harness in-process, shared by the tests that pin it.

Not a test module. It exists because two files now drive initialize_latency_client -- the REST arm's
own pins (test_latency_rest_client.py, tj-3mk3u5.61) and the two-arms-survive pin
(test_latency_kafka_arm_removed.py, tj-3mk3u5.35) -- and a second copy of this setup would be a
second thing to keep true. routers/tests/problem_app.py is the same idea for the error boundary.

WHAT IS STUBBED AND WHAT IS NOT. Only the two transports are stood in for: the gRPC arm wants a
reachable target and the REST client wants a socket, neither of which the PR gate has. Everything
else runs for real -- the whole of initialize_latency_client, the APIRouter it builds, and the entire
request handler including the dispatch chain, asyncio.gather, Timer and the percentile maths.
"""

import inspect
from types import SimpleNamespace

from fastapi.routing import APIRoute

import common.rpc.latency as grpc_arm
import routers.common.latency as latency
from routers.common.app_endpoints import InterfaceRest


class StubProbeClient:
    """Stands in for LatencyProbeClient, recording each probe instead of calling a peer.

    The real client is constructed inside initialize_latency_client, so a test reaches its instance
    through the module global rather than being handed one: getattr(latency, '__GRPC_CLIENT').
    """

    def __init__(self, channel, timeout_s):
        self.channel = channel
        self.timeout_s = timeout_s
        self.probes: list[str] = []

    async def probe(self, payload):
        self.probes.append(payload)
        return SimpleNamespace()


class RecordingRestClient:
    """Records every post the handler makes, and answers 200 without touching the network."""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return SimpleNamespace(status_code=200)


class AppStub:
    """Captures the APIRouter initialize_latency_client builds, in place of a FastAPI app.

    FastAPI 0.141 defers an included router behind a private _IncludedRouter wrapper, so reading the
    registered route back off a real app means reaching into an implementation detail that is free to
    change. include_router is the only thing the init asks of the app.
    """

    def __init__(self):
        self.routers = []

    def include_router(self, router):
        self.routers.append(router)


def legacy_group_args() -> tuple:
    """The dead consumer-group argument initialize_latency_client still takes, while it still takes one.

    initialize_latency_client and initialize_latency_server accept client_group/server_group and
    never read them -- routers/common/latency.py says so where they are declared. The parameters are
    Kafka-era residue that outlived their arm on tj-3mk3u5.35, and their only remaining purpose is to
    keep two lifespan call sites compiling until tj-3mk3u5.11 and .12 reach them.

    So the value handed over is None rather than a ConsumerGroup: importing common.kafka.topics to
    fill a slot nothing reads would make every test here depend on a module tj-3mk3u5.14 deletes
    (tj-3mk3u5.32). And the slot is filled only while the signature has one, so the eventual removal
    of the parameters is a production edit that reds nothing here.

    THIS IS NOT turn_harness_on's raising=False. That one warns against tolerating a missing name,
    because a test that stops exercising an arm must say so loudly. Nothing is exercised here: the
    argument is provably never read, so supplying it and not supplying it assert exactly the same
    thing. Delete this helper with the parameters, on tj-iwiq23.

    Returns:
        tuple: (None,) while the parameter exists, otherwise ().
    """
    accepted = inspect.signature(latency.initialize_latency_client).parameters
    return (None,) if 'client_group' in accepted else ()


def turn_harness_on(monkeypatch):
    """Turn the harness on and neutralise everything in the init that is not under test.

    LATENCY_TEST_ENABLED is read ONCE, at import of routers/common/latency.py, into a module
    attribute, so setting the environment variable now would change nothing. The module globals are
    reset because initialize_latency_client refuses a second call once __APP_NAME is set, and
    monkeypatch puts the real values back when the test ends.

    No call here passes raising=False, which is deliberate: every name below must exist on the module.
    That is what turned the Kafka removal on tj-3mk3u5.35 into three loud errors in this setup, rather
    than three tests that silently stopped exercising the arm they name.

    Args:
        monkeypatch (pytest.MonkeyPatch): The active patcher; it undoes all of this at test end.
    """
    monkeypatch.setattr(latency, 'LATENCY_TEST_ENABLED', True)
    for name in ('__APP_NAME', '__APP_PORT', '__REST_CLIENT', '__GRPC_CLIENT'):
        monkeypatch.setattr(latency, name, None)
    monkeypatch.setattr(latency, 'target_from_env', lambda env_var: 'stub-target:1')
    monkeypatch.setattr(latency, 'create_channel', lambda target: SimpleNamespace(target=target))
    monkeypatch.setattr(grpc_arm, 'LatencyProbeClient', StubProbeClient)


def rest_endpoint(app: AppStub):
    """The handler initialize_latency_client registered, called directly rather than over a TestClient.

    Args:
        app (AppStub): The app stub the init was given.

    Returns:
        The route's endpoint coroutine function.
    """
    assert len(app.routers) == 1, app.routers
    routes = [route for route in app.routers[0].routes if isinstance(route, APIRoute)]
    assert [route.path for route in routes] == [InterfaceRest.LATENCY]
    return routes[0].endpoint
