"""Smoke test for the interfaces data_ingest actually answers on (tj-al33ax, ADR tj-fdb9gz).

WHY THIS DRIVES THE HANDLER AND NOT THE KAFKA TRANSPORT. data_ingest's real interface is not
HTTP: it is the Kafka RPC handler registered by a decorator at module scope in
routers/data_ingest/get_dataset_request.py. This file reaches one layer inside that transport
and invokes the registered handler callable directly, because tj-3mk3u5 DELETES the Kafka RPC
layer and tj-8konfu RE-HOSTS it on gRPC. A transport-level smoke test -- serialisation,
correlation ids, timeouts over a real or faked broker -- is work that migration throws away.
The handler survives it: the same callable, the same request and response schemas, re-hosted on
a different transport. So the test is written against the thing that outlives the migration.

WHAT THIS THEREFORE DOES NOT PROVE, stated rather than hidden: nothing here exercises the RPC
transport. A request never gets serialised, published, consumed, correlated or timed out. That
layer is covered only by the mocked tests in common/tests/kafka, which assert on the call
arguments handed to kafka-python-ng rather than on a round trip (kafka_rpc_client.py sits at
53% line coverage). The gap is known and accepted; it is closed by the system tier
(tj-6bvoic, tj-0qxnzw), not by this file.

THE ENTRIES UNDER TEST COME FROM THE COMMITTED MANIFEST under routers/tests/interface_manifest/,
never from a list written out here: a handler or route added without a manifest line is already
a failure in routers/tests/test_interface_surface.py, and one added WITH a manifest line arrives
here automatically.

TWO THINGS THIS FILE DELIBERATELY DOES NOT DO. It does not absorb tj-utfeno, which owns real
behavioural tests for routers/data_ingest against a stubbed broker; the assertion here is that
the handler is WIRED and answers in the declared shape, nothing richer. And it does not
implement the REST path data_ingest declares but does not serve (tj-427x50) -- it pins the fact
that the path 404s, so implementing it turns this test red and the manifest line from
`unbound-path` into `http` in the same diff.
"""

import importlib
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from typing import Any, NamedTuple
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.routing import NoMatchFound

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType
from common.kafka.kafka_rpc_factory import KafkaRpcFactory
from common.worker_pool import SharedWorkerPool
from data.ingest.app.brokers.alpaca import broker_api

# The manifest parser, not a second copy of it: load_manifest() also enforces the manifest's
# structure (field count, sorting, duplicates), so a malformed manifest fails here too instead
# of being silently read as fewer interfaces to test.
from routers.tests.test_interface_surface import load_manifest


pytestmark = pytest.mark.data_ingest

# The service whose composed app is under test. main.py is the only place that decides which
# routers are mounted and under which prefix -- a manifest address is RELATIVE TO ITS ROUTER and
# says nothing about a prefix passed to include_router(), so every app-level path below is
# derived from the live app rather than from the manifest string (noted at the S2 gate).
APP_MODULE = 'data.ingest.app.main'

# Every component whose routers this app mounts. routers/common/ping.py is included by main.py,
# so common's manifest is part of THIS app's declared surface even though the file lives
# elsewhere. Keeping both here is what lets the route check below assert an equality rather than
# a containment.
MOUNTED_COMPONENTS = ('common', 'data_ingest')

START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
CLOSES = (10.0, 11.0, 12.0)


class Entry(NamedTuple):
    """One manifest line, split into its fields."""

    kind: str
    address: str
    file: str
    symbol: str
    request: str
    response: str
    touches: str


def manifest_entries(component: str) -> list[Entry]:
    """Read one component's committed manifest.

    Args:
        component (str): Component name, as the manifest file is named.

    Returns:
        list[Entry]: Every declared interface of that component.
    """
    return [Entry(*[field.strip() for field in line.split('|')]) for line in load_manifest(component)]


ENTRIES = {component: manifest_entries(component) for component in MOUNTED_COMPONENTS}
RPC_ENTRIES = [entry for entry in ENTRIES['data_ingest'] if entry.kind == 'rpc']
UNBOUND_ENTRIES = [entry for entry in ENTRIES['data_ingest'] if entry.kind == 'unbound-path']


def build_stock_dataset_request() -> dict[str, Any]:
    """Build the payload for a GetDatasetRequest that asks for stock market activity.

    Written out per request model rather than as one superset payload shared by several: a
    superset payload goes on passing if a model starts rejecting extras, and hides which field
    each model actually requires.

    Returns:
        dict[str, Any]: Constructor keyword arguments.
    """
    return {
        'dataset_id': uuid4(),
        'source': DataSource.ALPACA_API,
        'granularity': Granularity.ONE_DAY,
        'start': START,
        'end': None,
        'expiry': START,
        'expiry_type': ExpiryType.ROLLING,
        'update_type': UpdateType.DAILY,
        'asset_symbol': 'VFV',
        'asset_type': AssetType.STOCK,
        'data_types': [DataType.MARKET_ACTIVITY],
    }


# Keyed by the fully qualified request schema the manifest declares, so that a handler added
# with a NEW request type fails the coverage test below ("add a payload") rather than quietly
# never being invoked.
REQUEST_PAYLOADS = {'schemas.data_ingest.get_dataset_request.GetDatasetRequest': build_stock_dataset_request}


class StubBarSet:
    """The slice of alpaca-py's BarSet that convert_bars_to_batch_schema actually touches.

    The same shape data/ingest/tests/test_broker_api.py uses. A heavier double buys nothing:
    the vendor response is read through exactly two operations, and a stub that supports only
    those two cannot drift into asserting something the real BarSet does differently.
    """

    def __init__(self, symbol: str, bars: list[SimpleNamespace]):
        self.data = {symbol: bars}

    def __getitem__(self, symbol: str) -> list[SimpleNamespace]:
        return self.data[symbol]


def build_bar(close: float) -> SimpleNamespace:
    """Build one vendor bar.

    Args:
        close (float): Closing price, the one field the assertions read back.

    Returns:
        SimpleNamespace: A bar with every attribute the converter touches.
    """
    return SimpleNamespace(open=1.0, high=2.0, low=0.5, close=close, volume=10, trade_count=3, timestamp=START)


@pytest.fixture
def stub_broker_client() -> Mock:
    """Install a stub Alpaca client for the duration of one test.

    set_client() is production's own injection seam (tj-84jfb9), so nothing is patched and no
    credential is read: the injected client IS the credential. Cleared afterwards so one test's
    stub is never another test's vendor.

    Returns:
        Mock: The installed client.
    """
    client = Mock()
    client.get_stock_bars.return_value = StubBarSet(
        build_stock_dataset_request()['asset_symbol'], [build_bar(close) for close in CLOSES]
    )
    broker_api.set_client(client)
    yield client
    broker_api.set_client(None)


@pytest.fixture
def worker_pool() -> Iterator[ThreadPoolExecutor]:
    """Start the shared worker pool the handler's blocking vendor call runs on.

    The app's lifespan calls worker_startup(); this is the same call, because the handler reaches
    SharedWorkerPool.get_instance() through ingest_control and an unstarted pool hands it None.
    Shut down in teardown: since tj-1bv25s, worker_shutdown() clears the class attribute as well as
    stopping the executor, so a later test that starts the pool again is handed a live one instead
    of this one's corpse. The workaround that left it running is gone with the defect.

    Yields:
        ThreadPoolExecutor: The running pool.
    """
    SharedWorkerPool.worker_startup()
    pool = SharedWorkerPool.get_instance()
    assert pool is not None, 'worker_startup() left no executor, so the handler would run on the default one'
    yield pool
    SharedWorkerPool.worker_shutdown()


def import_module_of(entry: Entry) -> ModuleType:
    """Import the module a manifest entry names by file path.

    Args:
        entry (Entry): Manifest entry.

    Returns:
        ModuleType: The implementing module.
    """
    return importlib.import_module(entry.file.removesuffix('.py').replace('/', '.'))


def import_symbol(dotted: str) -> Any:
    """Import a fully qualified symbol, as the manifest spells the schemas.

    Args:
        dotted (str): Fully qualified name, e.g. package.module.Class.

    Returns:
        Any: The named object.
    """
    module_name, _, attribute = dotted.rpartition('.')
    return getattr(importlib.import_module(module_name), attribute)


def registered_handler(entry: Entry) -> Any:
    """Find the live RPC registration a manifest entry describes, and return its handler.

    THIS IS THE POINT OF THE TEST AND NOT A DETOUR. The callable is taken from what the factory
    RECORDED when the module body ran, never from getattr(module, symbol): add_server() returns
    the function unchanged, so a module symbol stays importable and callable after the decorator
    is deleted, and a test that called it would pass on a service that answers nothing. Reaching
    into _rpc_servers is how test_interface_surface.py and test_app_import.py read the same
    registry; the factory exposes no public view of it.

    Args:
        entry (Entry): Manifest entry of kind 'rpc'.

    Returns:
        Any: The registered handler coroutine function.
    """
    module = import_module_of(entry)
    servers = [
        server
        for factory in vars(module).values()
        if isinstance(factory, KafkaRpcFactory)
        for server in factory._rpc_servers
        if server.endpoint.topic.value == entry.address and server._rpc_function.__qualname__ == entry.symbol
    ]
    assert len(servers) == 1, (
        f'{entry.symbol} is declared in the manifest as serving {entry.address}, but {len(servers)} handlers are '
        f'registered for it on the factories in {module.__name__}. A count of 0 means the registration decorator '
        f'did not run: the service would start and answer nothing.'
    )
    server = servers[0]
    assert import_symbol(entry.request) is server.endpoint.request_model
    assert import_symbol(entry.response) is server.endpoint.response_model
    return server._rpc_function


def test_every_declared_rpc_request_schema_has_a_payload_in_this_file():
    # Without this, a handler taking a new request type would be added to the manifest, arrive in
    # the parametrization below, and KeyError -- or worse, be quietly filtered out. The equality
    # also catches the reverse: a payload left behind for a handler that no longer exists.
    assert RPC_ENTRIES, 'data_ingest.manifest declares no RPC handler, so the smoke test below asserts nothing'
    assert {entry.request for entry in RPC_ENTRIES} == set(REQUEST_PAYLOADS)


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', RPC_ENTRIES, ids=lambda entry: entry.symbol)
async def test_the_registered_rpc_handler_answers_in_its_declared_response_schema(
    entry: Entry, stub_broker_client: Mock, worker_pool: ThreadPoolExecutor
):
    handler = registered_handler(entry)
    request = import_symbol(entry.request)(**REQUEST_PAYLOADS[entry.request]())

    response = await handler(request)

    # isinstance, not truthiness: get_market_stock_data() catches every exception on the fetch
    # path and returns a bare {} (broker_api.py, "TODO better error handling"). A failed request
    # therefore comes back as an empty dict that a weaker assertion would read as success.
    assert isinstance(response, import_symbol(entry.response)), (
        f'{entry.symbol} returned {type(response).__name__}, not the declared {entry.response}'
    )
    # The stub really was reached, so the response above was built from a vendor answer rather
    # than from an empty short circuit.
    stub_broker_client.get_stock_bars.assert_called_once()


@pytest.mark.asyncio
async def test_the_rpc_handler_carries_the_vendor_bars_through_into_its_response(
    stub_broker_client: Mock, worker_pool: ThreadPoolExecutor
):
    # One concrete round trip behind the generic assertions above: the request's own dataset_id
    # comes back on the batch (it is what the store correlates on), and every bar the vendor
    # returned is in the response, in order. tj-utfeno owns the behaviour beyond this.
    entry = RPC_ENTRIES[0]
    payload = build_stock_dataset_request()
    request = import_symbol(entry.request)(**payload)

    response = await registered_handler(entry)(request)

    assert response.dataset_id == payload['dataset_id']
    assert [created.data.close for created in response.dataset[DataType.MARKET_ACTIVITY]] == list(CLOSES)


@pytest.fixture(scope='module')
def http_client() -> TestClient:
    """Drive the composed FastAPI app over HTTP.

    NOT used as a context manager, deliberately: entering one runs the app's lifespan, which
    waits for Kafka and would make this a system test. Every route below is registered while the
    module body runs, so routing is fully decided without startup.

    Returns:
        TestClient: Client bound to data_ingest's app.
    """
    return TestClient(importlib.import_module(APP_MODULE).app)


def served_routes() -> set[tuple[str, str]]:
    """List what the composed app serves, at its app-level paths.

    Read from the app's own OpenAPI schema rather than by walking app.routes: FastAPI 0.141
    does not copy an included router's routes onto the app, it stores a _IncludedRouter wrapper
    whose routes are resolved at request time. Walking app.routes therefore finds only the
    framework's own /docs endpoints and reports a service with routes as a service with none.
    The schema is the version-stable public view, and it is already app-level, prefixes included.

    Returns:
        set[tuple[str, str]]: (method, app-level path) for every route served.
    """
    schema = importlib.import_module(APP_MODULE).app.openapi()
    return {(method.upper(), path) for path, operations in schema['paths'].items() for method in operations}


def declared_routes() -> dict[tuple[str, str], Entry]:
    """Resolve every http interface the mounted components declare to its app-level path.

    THE MANIFEST ADDRESS IS ROUTER-RELATIVE and is not a URL: it cannot see a prefix passed to
    include_router() in main.py (recorded at the S2 gate). The app-level path is therefore asked
    of the app, by route name -- which defaults to the endpoint function's name, the same name
    the manifest records as the symbol. Add a prefix in main.py and this mapping follows it,
    while the manifest line stays exactly as it is.

    Returns:
        dict[tuple[str, str], Entry]: (method, app-level path) -> the entry that declares it.
    """
    resolved: dict[tuple[str, str], Entry] = {}
    app = importlib.import_module(APP_MODULE).app
    for entries in ENTRIES.values():
        for entry in entries:
            if entry.kind != 'http':
                continue
            method, _, declared_path = entry.address.partition(' ')
            try:
                app_level = str(app.url_path_for(entry.symbol))
            except NoMatchFound:
                # Declared by a router this app does not mount. Routers/common is shared, so this
                # is ordinary; the assertions below are one-directional for exactly that reason.
                continue
            assert app_level.endswith(declared_path), (
                f'{entry.symbol} is mounted at {app_level}, which does not end in its manifest address '
                f'{declared_path}: the manifest and the mounted path describe different routes'
            )
            resolved[(method, app_level)] = entry
    return resolved


def test_every_route_the_ingest_app_serves_is_declared_in_a_manifest():
    # routers/tests/test_interface_surface.py asserts what each ROUTER declares. Nothing asserted
    # what main.py actually MOUNTS: a router left out of the app, or a second app's router pulled
    # into this one, is invisible there and visible here.
    served = served_routes()
    assert served, f'{APP_MODULE} serves no routes at all'
    undeclared = served - set(declared_routes())
    assert not undeclared, (
        f'{APP_MODULE} serves routes that no manifest under routers/tests/interface_manifest declares: '
        f'{sorted(undeclared)}'
    )


def test_each_route_the_app_mounts_answers_over_http(http_client: TestClient):
    # Looped rather than parametrized: an empty parametrization is a SKIP, and pytest.ini's
    # fail-never-skip rule means a surface that quietly became empty has to be a failure. The
    # non-empty assertion above is that failure.
    for method, path in sorted(served_routes()):
        response = http_client.request(method, re.sub(r'\{[^}]+\}', 'unused', path))

        assert response.status_code != 404, f'{method} {path} is registered on the app but answers 404'


def test_a_path_the_manifest_records_as_unbound_is_not_served(http_client: TestClient):
    # tj-427x50: data_ingest used to declare a REST path in an interface enum with no route bound
    # to it. That declaration is deleted, so UNBOUND_ENTRIES is empty today and the loop below
    # runs zero times.
    #
    # THAT IS NOT THE VACUOUS-PASS BUG, and the distinction is the whole point of this comment.
    # Zero unbound paths is the DESIRED end state of this category -- every declaration either
    # implemented or deleted -- not a surface that silently disappeared. Contrast
    # test_each_route_the_app_mounts_answers_over_http above, where empty means the app serves
    # nothing and MUST fail. The guard below is therefore deliberately weak: it proves the
    # manifest parsed real data, so a parsing regression that empties every category still fails
    # here, while a legitimately empty category does not.
    #
    # WHY THIS FILE LOOPS AND data/store/tests/test_http_smoke.py PARAMETRIZES for the same
    # category: no deep reason, and neither is wrong. Note for whoever unifies them that pytest's
    # default empty_parameter_set_mark is `skip`, so an empty parametrize reports
    # 'got empty parameter set for (...)' -- a visible skip, NOT silent collection of nothing.
    # Both files' comments claimed otherwise before this was measured; do not re-derive it from
    # the old wording.
    assert ENTRIES['data_ingest'], 'data_ingest.manifest parsed to no entries at all'

    for entry in UNBOUND_ENTRIES:
        # Any value serves: nothing is routed to, so no path parameter is ever parsed.
        path = re.sub(r'\{[^}]+\}', 'unused', entry.address)
        for method in ('POST', 'GET'):
            assert http_client.request(method, path).status_code == 404, (
                f'{method} {path} is served, but {entry.symbol} is declared unbound in data_ingest.manifest'
            )
