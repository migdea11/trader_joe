import uuid
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient
from starlette.routing import NoMatchFound

from common.database.postgres_tools import PostgresSessionFactory
from data.store.app.app_depends import get_rpc_clients
from data.store.app.database.database import async_db
from data.store.app.main import app
from routers.tests.test_interface_surface import SEPARATOR, load_manifest
from schemas.data_store.stock.market_activity_data import BatchStockDataMarketActivityCreate


# THE data_store HTTP SMOKE TEST (tj-xerngg, ADR tj-fdb9gz).
#
# WHAT IT PROVES: every route routers/tests/interface_manifest/data_store.manifest declares is
# actually MOUNTED on data.store.app.main:app, answers a well-formed request without a server
# error, and rejects a malformed one with 422. REACHABILITY AND SHAPE, never business correctness.
#
# WHAT IT DOES NOT DO, deliberately: tj-19qp1q owns the happy path, not-found and real queries
# against Postgres. This file proves the routes are WIRED, which tj-19qp1q currently assumes
# without checking. Nothing here asserts that a stored row comes back.
#
# WHY IT DRIVES THE MANIFEST RATHER THAN A HAND-WRITTEN LIST: a hand-written list drifts from the
# manifest, and the drift is the exact thing tj-ru24i2 exists to prevent. The manifest is parsed
# with tj-ru24i2's own load_manifest() rather than a second parser here, for the same reason: two
# parsers are two things to keep in step. A new route therefore costs a manifest line AND a CASES
# entry below; a manifest line with no CASES entry FAILS rather than being quietly skipped.
#
# NO POSTGRES AND NO KAFKA ARE NEEDED, and that rests on two things, not one:
#   1. The TestClient is NEVER entered as a context manager. Starlette runs the app's lifespan on
#      __enter__, and data/store/app/app_depends.py lifespan() calls database.initialize() and
#      KafkaConsumerFactory.wait_for_kafka() -- both of which need a live service. Constructing the
#      client without `with` dispatches requests without ever running it. If you add a `with` here
#      to make something work, you have just made this file need a database.
#   2. Both injected dependencies are replaced through app.dependency_overrides -- async_db and
#      get_rpc_clients -- rather than by monkeypatching module globals, because the handlers take
#      them as Depends() and that is the seam FastAPI gives you.
# test_the_routes_are_driven_without_a_lifespan asserts both halves instead of trusting them.
#
# THE FAKES ARE DELIBERATELY FAITHFUL, NOT PERMISSIVE. FakeSession.add is SYNCHRONOUS because
# AsyncSession.add is synchronous. Making it awaitable would hide tj-v7340n (a crud function that
# writes `await db.add(...)`), and a fake that is more forgiving than the real object turns a smoke
# test into a green result asserting nothing -- the failure shape this project keeps hitting.

pytestmark = pytest.mark.data_store

COMPONENT = 'data_store'
HTTP = 'http'
UNBOUND_PATH = 'unbound-path'

# Statuses a well-formed request must not produce. 404/405 mean the route is not mounted where the
# manifest says it is; 500 means it is mounted and broken.
UNREACHABLE_STATUSES = (404, 405)

# A route that CANNOT answer a well-formed request today, for reasons that have nothing to do
# with the database being absent. Found by this file and filed against builder-store, whose scope
# routers/data_store is -- a validator that patches the code under test has destroyed the review.
# strict=True: when the route is fixed the test goes RED (XPASS), which is the signal to delete
# the entry here in the same diff. This is not a skip -- the request is really made and its
# failure is really observed -- and it is not an assertion that 500 is correct, which is what
# writing `assert status == 500` would have meant.
#
# DELETE /internal/asset-data/{asset_type}/{data_type} (tj-h7ikz2) is no longer here: builder-store
# removed the route rather than repair it. Its schema, StockDataMarketActivityDeleteById, inherits
# a plain ABC (AssetDataDeleteById, tj-9dqfjo) that cannot be constructed with **kwargs, so keeping
# the route working would have needed a schemas/ fix outside builder-store's scope; the route also
# carried a stale '# TODO this will be removed in the future' comment and its crud function deleted
# every row in the table regardless of any argument. See tj-h7ikz2 for the full reasoning.
KNOWN_BROKEN = {
    # tj-v7340n's original three defects (the bad log field, the awaited sync `db.add()`, and the
    # missing return) are fixed. What remains, still filed against tj-v7340n:
    # create_market_activity_data now does `db.add(...); await db.commit(); await db.refresh(...)`
    # to populate the response's DB-generated id/created_at/updated_at -- the standard async
    # SQLAlchemy pattern -- but FakeSession below has no `refresh`, so the well-formed request
    # still raises AttributeError. Adding `refresh` to FakeSession is this file's call, not
    # builder-store's, which is why it is left unadded rather than the fixture being extended here.
    'POST /internal/asset-data/{asset_type}/{data_type}': (
        "tj-v7340n: create_market_activity_data awaits db.refresh(...) to populate the response's "
        'DB-generated id/created_at/updated_at, and FakeSession has no refresh method'
    )
}


class Case(NamedTuple):
    """One manifest route's well-formed and malformed requests.

    Attributes:
        path_params: Values substituted into the route's path placeholders.
        request: Extra httpx keyword arguments (json, params) for the well-formed call.
        malformed_path_params: Path values for the malformed call.
        malformed_request: Extra httpx keyword arguments for the malformed call.
        malformed_field: The field the malformed call corrupts, which the 422 must name.
    """

    path_params: dict[str, str]
    request: dict[str, Any]
    malformed_path_params: dict[str, str]
    malformed_request: dict[str, Any]
    malformed_field: str


ASSET_PATH = {'asset_type': 'stock', 'data_type': 'market-activity'}
SYMBOL_PATH = {**ASSET_PATH, 'asset_symbol': 'AAPL'}

# A complete StockDataMarketActivityCreate. Written out rather than minimised: the point of the
# well-formed half is that nothing about the PAYLOAD can be blamed when a route misbehaves.
DATA_POINT = {
    'dataset_id': '8f41c2d7-a3b9-4d1e-9c2f-0a1b2c3d4e5f',
    'asset_symbol': 'AAPL',
    'source': 'ALPACA',
    'granularity': '1day',
    'timestamp': '2026-01-02T00:00:00Z',
    'expiry': None,
    'data': {
        'open': 1.0,
        'high': 2.0,
        'low': 0.5,
        'close': 1.5,
        'volume': 100,
        'trade_count': 10,
        'split_factor': 1.0,
        'dividends_factor': 1.0,
    },
}

# start is present although StoreAssetDatasetBody makes it optional: the handler forwards the body
# into GetDatasetRequest, where start is REQUIRED, so a body without it is not in fact well-formed.
# That asymmetry between the two schemas is a real trap and the reason this constant is commented.
DATASET_REQUEST = {'source': 'ALPACA', 'granularity': '1day', 'start': '2026-01-01T00:00:00Z'}

# One entry per `http` line in data_store.manifest. The key is the manifest's address verbatim, so
# an address that changes shape turns this file red rather than silently matching nothing.
#
# Each malformed request corrupts exactly ONE field, and the assertion checks that the 422 names
# that field. Asserting only "status == 422" would pass on a 422 raised by something else entirely
# -- an unrelated required parameter, say -- and prove nothing about the field under test. The
# three corruptions are deliberately different in kind: a path enum, a body enum, and a path UUID.
CASES: dict[str, Case] = {
    'GET /internal/asset-data/{asset_type}/{data_type}': Case(
        path_params=ASSET_PATH,
        request={},
        malformed_path_params={**ASSET_PATH, 'asset_type': 'not-an-asset-type'},
        malformed_request={},
        malformed_field='asset_type',
    ),
    'POST /internal/asset-data/{asset_type}/{data_type}': Case(
        path_params=ASSET_PATH,
        request={'json': DATA_POINT},
        malformed_path_params={**ASSET_PATH, 'asset_type': 'not-an-asset-type'},
        malformed_request={'json': DATA_POINT},
        malformed_field='asset_type',
    ),
    'GET /store/{asset_type}/{data_type}/{asset_symbol}': Case(
        path_params=SYMBOL_PATH,
        request={},
        malformed_path_params={**SYMBOL_PATH, 'data_type': 'not-a-data-type'},
        malformed_request={},
        malformed_field='data_type',
    ),
    'POST /store/{asset_type}/{data_type}/{asset_symbol}': Case(
        path_params=SYMBOL_PATH,
        request={'json': DATASET_REQUEST},
        malformed_path_params=SYMBOL_PATH,
        malformed_request={'json': {**DATASET_REQUEST, 'source': 'not-a-data-source'}},
        malformed_field='source',
    ),
    'DELETE /store/{id}': Case(
        path_params={'id': str(uuid.uuid4())},
        request={},
        malformed_path_params={'id': 'not-a-uuid'},
        malformed_request={},
        malformed_field='id',
    ),
}


class FakeResult:
    """Stands in for a SQLAlchemy Result over an empty table."""

    def all(self) -> list:
        return []

    def scalars(self) -> 'FakeResult':
        return self

    def scalar_one(self) -> uuid.UUID:
        return uuid.uuid4()

    def first(self) -> None:
        return None


class FakeSession:
    """Stands in for AsyncSession, matching its sync/async split exactly.

    execute/commit/rollback/close are coroutines and add is not, because that is what
    AsyncSession does. Every method here is one a data_store handler actually reaches.
    """

    async def execute(self, statement: Any) -> FakeResult:
        return FakeResult()

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def add(self, instance: Any) -> None:
        return None


class FakeRpcClient:
    """Answers an RPC request with an empty dataset, so no Kafka round trip happens."""

    async def send_request(self, request: Any) -> BatchStockDataMarketActivityCreate:
        # An empty `dataset` means the handler stores nothing and reports 0 data points. A
        # populated one would be tj-19qp1q's job, and would need a database to store into.
        return BatchStockDataMarketActivityCreate(
            asset_symbol='AAPL', source='ALPACA', granularity='1day', dataset_id=uuid.uuid4(), dataset={}
        )


class FakeRpcClients:
    """Stands in for KafkaRpcFactory.RpcClients."""

    def get_client(self, endpoint: Any) -> FakeRpcClient:
        return FakeRpcClient()


def manifest_entries(kind: str, *, allow_empty: bool = False) -> list[list[str]]:
    """Read one kind of entry out of the data_store manifest.

    Args:
        kind (str): Manifest kind, e.g. 'http'.
        allow_empty (bool): True lets zero matches through instead of raising. A kind whose count
            reflects a real decision -- unbound-path went to zero when tj-wc4pe8 deleted the last
            two unimplemented declarations -- is a legitimate empty, not a vacuous-parametrize
            accident. `http` never passes this: a manifest with no bound routes at all is exactly
            the accident the guard exists to catch.

    Returns:
        list[list[str]]: The matching entries, each split into its fields.
    """
    entries = [line.split(SEPARATOR) for line in load_manifest(COMPONENT)]
    matching = [fields for fields in entries if fields[0] == kind]
    if not matching and not allow_empty:
        # Guards the vacuous pass: an empty parametrization collects zero tests and reports
        # success. tj-ru24i2 makes the same guard for the same reason.
        raise AssertionError(f'{COMPONENT}.manifest declares no {kind} entries, so this file asserts nothing')
    return matching


def http_addresses() -> list[str]:
    """Return the address field of every http entry in the manifest.

    Returns:
        list[str]: Addresses, e.g. 'GET /store/{id}'.
    """
    return [fields[1] for fields in manifest_entries(HTTP)]


def unbound_path_addresses() -> list[Any]:
    """Return the address field of every unbound-path entry in the manifest, for parametrize.

    Unlike http_addresses(), zero is a real state here, not the vacuous-parametrize accident
    manifest_entries() otherwise guards against: tj-wc4pe8 deleted the last two unimplemented
    declarations, and tj-2h1q3k may add a new one back. An empty argvalues list would still
    collect zero tests silently, so an empty manifest returns one explicitly skipped case instead
    of nothing -- the run says out loud that the category is empty today, rather than the category
    just vanishing from the report.

    Returns:
        list[Any]: Addresses, e.g. '/store/{id}', or a single skip placeholder when there are none.
    """
    entries = manifest_entries(UNBOUND_PATH, allow_empty=True)
    if not entries:
        return [
            pytest.param(
                None,
                marks=pytest.mark.skip(
                    reason=f'{COMPONENT}.manifest currently declares no {UNBOUND_PATH} entries (tj-wc4pe8)'
                ),
            )
        ]
    return [fields[1] for fields in entries]


def substitute(path: str, path_params: dict[str, str]) -> str:
    """Substitute path parameters into a path template.

    Args:
        path (str): Path with {placeholders}.
        path_params (dict[str, str]): Values to substitute.

    Returns:
        str: The concrete path.
    """
    concrete = path
    for name, value in path_params.items():
        concrete = concrete.replace(f'{{{name}}}', value)
    assert '{' not in concrete, f'{path} still has an unsubstituted placeholder after {sorted(path_params)}: {concrete}'
    return concrete


def app_url_for(address: str, symbol: str, path_params: dict[str, str]) -> str:
    """Resolve a manifest entry to the URL the app actually serves it at.

    This is the step the manifest cannot do for itself. The manifest's address is
    ROUTER-RELATIVE: it is built from FastAPI's route.path, which carries no prefix passed at
    app.include_router(), and the manifest test cannot see data/store/app/main.py at all. Only
    main.py knows which routers are included and under what prefix, so the URL is asked of the
    app and the manifest address is then checked to be its tail. No prefix is passed today, which
    makes the two strings equal -- assuming that would be assuming away the whole point.

    Asked through the app's own url_path_for(), which is public and prefix-aware. Walking
    app.routes was the first attempt and it silently found nothing: FastAPI 0.141 stopped
    flattening an included router into app.routes and stores an internal _IncludedRouter wrapper
    instead. Resolving by name survives that; reading private route attributes did not.

    Args:
        address (str): Manifest address, 'METHOD /router/relative/path'.
        symbol (str): Implementing symbol from the manifest, which is also the route's name.
        path_params (dict[str, str]): Values for the path placeholders.

    Returns:
        str: The concrete app-level URL.
    """
    router_path = address.split(' ', 1)[1]
    try:
        url = str(app.url_path_for(symbol, **path_params))
    except NoMatchFound as exc:
        raise AssertionError(
            f'{address} is declared in {COMPONENT}.manifest and implemented by {symbol}, but '
            f'data.store.app.main:app serves no route of that name taking {sorted(path_params)}. '
            f'Its router is most likely not passed to app.include_router() in '
            f'data/store/app/main.py. (A route whose endpoint is not a module-level function '
            f"would also land here: the manifest records a qualname and a route's name is the "
            f"endpoint's bare __name__.)"
        ) from exc
    suffix = substitute(router_path, path_params)
    assert url.endswith(suffix), (
        f'{address} is served at {url}, which does not end with its router-relative path '
        f'{suffix}. A prefix at app.include_router() may only prepend to the path.'
    )
    return url


def mount_prefix() -> str:
    """Return the single prefix every data_store router is mounted under.

    DERIVED, never assumed to be empty. It is empty today because data/store/app/main.py passes
    no prefix, and this function is what notices the day it does not.

    Returns:
        str: The common prefix, '' when the routers are mounted at the root.
    """
    prefixes = set()
    for fields in manifest_entries(HTTP):
        address, symbol = fields[1], fields[3]
        path_params = case_for(address).path_params
        suffix = substitute(address.split(' ', 1)[1], path_params)
        url = app_url_for(address, symbol, path_params)
        prefixes.add(url[: len(url) - len(suffix)])
    assert len(prefixes) == 1, (
        f"data_store's routers are mounted under more than one prefix ({sorted(prefixes)}). That "
        f'is legal, but this file assumes one when it builds a URL for an unbound path; teach it '
        f'to resolve the prefix per router.'
    )
    return prefixes.pop()


def case_for(address: str) -> Case:
    """Look up the requests to drive one manifest route with.

    Args:
        address (str): Manifest address.

    Returns:
        Case: The route's well-formed and malformed requests.
    """
    case = CASES.get(address)
    assert case is not None, (
        f'{address} is declared in {COMPONENT}.manifest but has no entry in CASES, so it is not '
        f'smoke-tested. Add one. If the route genuinely cannot be driven without a real database, '
        f'say so here in a comment and record it for tj-19qp1q rather than leaving it out silently.'
    )
    return case


def http_params() -> list[Any]:
    """Build the parametrization for the well-formed half, xfailing the known-broken routes.

    Returns:
        list[Any]: One pytest.param per http entry in the manifest.
    """
    params = []
    for address in http_addresses():
        reason = KNOWN_BROKEN.get(address)
        marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
        params.append(pytest.param(address, marks=marks, id=address))
    return params


@pytest.fixture(scope='module')
def client():
    """A TestClient over the store app with its database and RPC clients replaced.

    NOT entered as a context manager -- see the header. The overrides are cleared afterwards
    because `app` is a module-level singleton other test modules import.
    """
    app.dependency_overrides[async_db] = FakeSession
    app.dependency_overrides[get_rpc_clients] = FakeRpcClients
    try:
        # raise_server_exceptions defaults to True and is left that way: an unhandled exception in
        # a handler reaches the test as its own traceback instead of an opaque 500, which is the
        # difference between "this route is broken" and "this route is broken, here is the line".
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def symbol_for(address: str) -> str:
    """Return the implementing symbol the manifest records for one address.

    Args:
        address (str): Manifest address.

    Returns:
        str: The symbol field.
    """
    return next(fields[3] for fields in manifest_entries(HTTP) if fields[1] == address)


@pytest.mark.parametrize('address', http_addresses())
def test_every_manifest_route_is_mounted_on_the_app(address: str):
    # The assertions live in app_url_for(), which every other test in this file also goes
    # through; this test is what names the failure when a router stops being included. It is the
    # only thing in the repository that would notice: the manifest test enumerates ROUTERS, so it
    # stays green with data/store/app/main.py serving nothing at all.
    assert app_url_for(address, symbol_for(address), case_for(address).path_params)


@pytest.mark.parametrize('address', http_params())
def test_a_well_formed_request_reaches_the_handler(address: str, client: TestClient):
    case = case_for(address)
    method = address.split(' ', 1)[0]
    url = app_url_for(address, symbol_for(address), case.path_params)

    response = client.request(method, url, **case.request)

    assert response.status_code not in UNREACHABLE_STATUSES, (
        f'{method} {url} returned {response.status_code}: the manifest declares this route but the '
        f'app does not answer it at that address.'
    )
    assert response.status_code != 422, (
        f'{method} {url} returned 422 ({response.text}). The request this test calls well-formed '
        f'is not; fix the CASES entry, or the malformed half below proves nothing.'
    )
    assert response.status_code < 500, f'{method} {url} returned {response.status_code}: {response.text}'


@pytest.mark.parametrize('address', http_addresses())
def test_a_malformed_request_is_rejected_with_422(address: str, client: TestClient):
    case = case_for(address)
    method = address.split(' ', 1)[0]
    url = app_url_for(address, symbol_for(address), case.malformed_path_params)

    response = client.request(method, url, **case.malformed_request)

    assert response.status_code == 422, f'{method} {url} returned {response.status_code}: {response.text}'
    complained_about = [error['loc'][-1] for error in response.json()['detail']]
    assert case.malformed_field in complained_about, (
        f'{method} {url} returned 422, but about {complained_about} rather than about '
        f'{case.malformed_field!r}, which is the only field this request corrupts.'
    )


@pytest.mark.parametrize('address', unbound_path_addresses())
def test_nothing_serves_an_unbound_path(address: str):
    # The manifest records these as declared-but-not-implemented: a path some interface enum in
    # routers/data_store/app_endpoints.py names, that no route binds. tj-ru24i2 proves no ROUTER
    # binds them; this proves no APP serves them, which is the stronger statement and the one a
    # caller experiences. Implementing one turns both red, in the same diff.
    #
    # tj-wc4pe8 deleted the last two (both PUTs); this is a skipped no-op until tj-2h1q3k or
    # another declaration adds one back, per unbound_path_addresses().
    #
    # Every method is tried because an unbound-path entry carries none: the manifest is recording
    # a path nobody serves, not a method nobody serves.
    url = substitute(
        mount_prefix() + address,
        {'asset_type': 'stock', 'data_type': 'market-activity', 'asset_symbol': 'AAPL', 'id': str(uuid.uuid4())},
    )
    # A fresh client, deliberately without the dependency overrides: if one of these paths ever
    # does reach a handler, that handler must not be handed a fake database by accident.
    unconfigured = TestClient(app)
    for method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
        response = unconfigured.request(method, url)
        assert response.status_code in UNREACHABLE_STATUSES, (
            f'{method} {url} returned {response.status_code}, so something now serves a path '
            f'{COMPONENT}.manifest records as unbound. Promote its manifest line to an http entry.'
        )


def test_the_routes_are_driven_without_a_lifespan(client: TestClient):
    # The claim this file rests on, asserted rather than trusted. Both halves come from
    # data/store/app/app_depends.py lifespan(), which is the only thing that calls
    # database.initialize() or builds the store's RPC clients.
    #
    # Reaches into AsyncSessionHandle._async_engines because the factory exposes no public view of
    # what it has opened. Compared before and after rather than against {} so that another test
    # module opening an engine cannot make this one fail for something it did not do.
    engines_before = set(PostgresSessionFactory.AsyncSessionHandle._async_engines)

    address = next(fields[1] for fields in manifest_entries(HTTP) if fields[1] not in KNOWN_BROKEN)
    case = case_for(address)
    url = app_url_for(address, symbol_for(address), case.path_params)
    client.request(address.split(' ', 1)[0], url, **case.request)

    assert set(PostgresSessionFactory.AsyncSessionHandle._async_engines) == engines_before, (
        'driving a route opened a Postgres engine, so the database dependency override is not '
        'taking effect or the lifespan ran'
    )
    assert get_rpc_clients() is None, (
        'the store app lifespan ran in this process: it is the only thing that sets the module '
        'global get_rpc_clients() reads, and running it needs a live Kafka broker'
    )
