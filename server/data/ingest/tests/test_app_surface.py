"""What data_ingest's composed app actually mounts, against what the manifests declare (tj-al33ax).

routers/tests/test_interface_surface.py asserts what each ROUTER declares. Nothing there asserts
what main.py actually MOUNTS: a router left out of the app, or a second app's router pulled into
this one, is invisible in the router-level view and visible here. That is this file's whole job,
and it is why these three tests are not redundant with the manifest test.

WHAT THIS FILE USED TO BE, because the name changed and the history should not have to be guessed
at. Until tj-3mk3u5.32 it was test_rpc_smoke.py, and most of it drove data_ingest's Kafka RPC
handler one layer inside the transport -- it reached into KafkaRpcFactory._rpc_servers for the
callable the decorator had registered, and asserted the handler was wired and answered in its
declared response schema. tj-3mk3u5.11 deletes that handler, its registration and ingest_control
with them, so those three tests went: they pinned a transport, not a behaviour, and the behaviour
they stood in front of is pinned on the gRPC path by test_fetch_dataset_handler.py.

The RPC half also depended on the manifest declaring an rpc line. Decision tj-3wgh03 replaced that
line with a `none` declaration, so RPC_ENTRIES would now be empty and the smoke tests would assert
nothing -- which is the state they were written to fail in. Deleting them is the honest answer, not
weakening the guard that caught it.

THE ENTRIES UNDER TEST COME FROM THE COMMITTED MANIFEST under routers/tests/interface_manifest/,
never from a list written out here: a route added without a manifest line is already a failure in
routers/tests/test_interface_surface.py, and one added WITH a manifest line arrives here
automatically.

data_ingest's own gRPC surface is NOT checked here and must not be. It is registered outside
routers/ (ADR tj-8konfu D3, decision tj-tkm4tn D1) and is pinned where it lives, by
data/ingest/tests/test_grpc_host.py, which asserts in a fresh interpreter that registered_services()
returns exactly the ingest service. Each surface is pinned where it lives (tj-3wgh03 D4).
"""

import importlib
import re
from types import ModuleType
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient
from starlette.routing import NoMatchFound

# The manifest parser, not a second copy of it: load_manifest() also enforces the manifest's
# structure (field count, sorting, duplicates, and the `none` declaration's shape), so a malformed
# manifest fails here too instead of being silently read as fewer interfaces to test.
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

    A component declaring `none` yields an empty list: load_manifest() drops the declaration line
    once it has checked its shape, because `none` says there is nothing to enumerate rather than
    describing an interface.

    Args:
        component (str): Component name, as the manifest file is named.

    Returns:
        list[Entry]: Every declared interface of that component.
    """
    return [Entry(*[field.strip() for field in line.split('|')]) for line in load_manifest(component)]


ENTRIES = {component: manifest_entries(component) for component in MOUNTED_COMPONENTS}
UNBOUND_ENTRIES = [entry for entry in ENTRIES['data_ingest'] if entry.kind == 'unbound-path']


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


@pytest.fixture(scope='module')
def http_client() -> TestClient:
    """Drive the composed FastAPI app over HTTP.

    NOT used as a context manager, deliberately: entering one runs the app's lifespan, which
    reaches for its peers and would make this a system test. Every route below is registered while
    the module body runs, so routing is fully decided without startup.

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
    # THE GUARD ASKS THE UNION, not data_ingest alone (tj-3mk3u5.32). It used to assert
    # ENTRIES['data_ingest'], which was fair while that manifest declared an rpc line; decision
    # tj-3wgh03 replaced the line with a `none` declaration, so data_ingest legitimately parses to
    # zero entries and the old guard would fail for the one reason it was never meant to catch.
    # The union still goes red if the parser itself breaks, which is what it is for.
    #
    # WHY THIS FILE LOOPS AND data/store/tests/test_http_smoke.py PARAMETRIZES for the same
    # category: no deep reason, and neither is wrong. Note for whoever unifies them that pytest's
    # default empty_parameter_set_mark is `skip`, so an empty parametrize reports
    # 'got empty parameter set for (...)' -- a visible skip, NOT silent collection of nothing.
    # Both files' comments claimed otherwise before this was measured; do not re-derive it from
    # the old wording.
    assert any(ENTRIES.values()), "every mounted component's manifest parsed to no entries at all"

    for entry in UNBOUND_ENTRIES:
        # Any value serves: nothing is routed to, so no path parameter is ever parsed.
        path = re.sub(r'\{[^}]+\}', 'unused', entry.address)
        for method in ('POST', 'GET'):
            assert http_client.request(method, path).status_code == 404, (
                f'{method} {path} is served, but {entry.symbol} is declared unbound in data_ingest.manifest'
            )
