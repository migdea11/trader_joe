import importlib
import uuid
from datetime import UTC, datetime
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import InvalidRequestError
from starlette.routing import NoMatchFound

from common.database.postgres_tools import PostgresSessionFactory
from data.store.app.app_depends import get_ingest_fetch_client, get_rpc_clients
from data.store.app.database.database import async_db
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.main import app
from data.store.tests.fetch_double import RecordingFetchClient
from data.store.tests.problem_body import validation_errors
from routers.common.instance_secret import (
    INSTANCE_SECRET_ENV_VAR,
    INSTANCE_SECRET_HEADER,
    INSTANCE_SECRET_REJECTION_DETAIL,
    require_instance_secret,
)
from routers.tests.test_interface_surface import SEPARATOR, load_manifest


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
# NO POSTGRES, NO KAFKA AND NO data_ingest ARE NEEDED, and that rests on two things, not one:
#   1. The TestClient is NEVER entered as a context manager. Starlette runs the app's lifespan on
#      __enter__, and data/store/app/app_depends.py lifespan() calls database.initialize(),
#      KafkaConsumerFactory.wait_for_kafka() and -- since tj-3mk3u5.10 -- create_channel() against
#      DATA_INGEST_GRPC_TARGET, which also fails outright when that variable is unset. Constructing
#      the client without `with` dispatches requests without ever running it. If you add a `with`
#      here to make something work, you have just made this file need a database.
#   2. Both injected dependencies are replaced through app.dependency_overrides -- async_db and
#      get_ingest_fetch_client -- rather than by monkeypatching module globals, because the
#      handlers take them as Depends() and that is the seam FastAPI gives you.
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

# ---------------------------------------------------------------------------------------------
# THE INSTANCE SECRET, AND WHY EVERY REQUEST BELOW CARRIES ONE ON A WRITE ROUTE (tj-vhboky.8)
#
# The three write routes now take require_instance_secret as a DECORATOR-level dependency. FastAPI
# inserts those AHEAD of the endpoint's own parameter dependencies, so the 401 is answered before
# any body or path value is validated. That is designed behaviour, not a defect: an auth gate that
# ran after body parsing would do work for an unauthenticated caller.
#
# It made three malformed-request cases fail, because they were written when no route had a guard.
# THE FIX IS TO AUTHENTICATE THEM, NOT TO WIDEN WHAT THEY ACCEPT. A malformed-request test that
# accepted "401 or 422" would no longer pin either status, and this branch has twice shipped an
# assertion that passed in both worlds. So a guarded route is given a configured secret and a valid
# header for the SHAPE halves of this file, and the fail-closed behaviour gets its own two cases
# below -- test_a_guarded_route_rejects_a_request_carrying_no_secret and
# test_a_guarded_route_answers_401_before_it_validates_anything. Two properties, separately named.
#
# THE WELL-FORMED HALF IS ALSO AFFECTED, which is the less obvious half and the more important one.
# 401 is not in UNREACHABLE_STATUSES, is not 422 and is below 500, so a guarded route satisfied
# every assertion in test_a_well_formed_request_reaches_the_handler WITHOUT REACHING THE HANDLER --
# the test's own name became false for three of the five routes the moment the guard landed. Giving
# the well-formed half a real secret is what makes that test mean again what it says.
# ---------------------------------------------------------------------------------------------

# Any non-empty value works. The comparison itself -- compare_digest, the absent-header case, the
# unset-variable case, the non-ASCII header -- belongs to common/tests/test_instance_secret.py and
# is not re-tested here; this file only proves WHICH ROUTES the dependency sits on.
CONFIGURED_SECRET = 'smoke-test-instance-secret'

# The write routes that must carry the guard, and the only routes that may. Written out rather than
# derived from the routers, because the derived set is the thing under test: a guard that appears on
# a READ route -- which routers/common/instance_secret.py forbids by design, reads being open --
# would otherwise be adopted silently by any test that asked the routers what they guard.
GUARDED_ADDRESSES = frozenset(
    {
        'POST /internal/asset-data/{asset_type}/{data_type}',
        'POST /store/{asset_type}/{data_type}/{asset_symbol}',
        'DELETE /store/{id}',
    }
)

# Routes that CANNOT answer a well-formed request today, for reasons that have nothing to do with
# the database being absent. Found by this file and filed against builder-store, whose scope
# routers/data_store is -- a validator that patches the code under test has destroyed the review.
# strict=True: when the route is fixed the test goes RED (XPASS), which is the signal to delete
# the entry here in the same diff. This is not a skip -- the request is really made and its
# failure is really observed -- and it is not an assertion that 500 is correct, which is what
# writing `assert status == 500` would have meant.
#
# EMPTY TODAY, and that is a real state rather than an oversight: every route the manifest
# declares now answers a well-formed request. The mapping stays because it is the mechanism for
# recording a broken route without a validator patching the code under review, and the next such
# finding should land here rather than reinvent it. Unlike the manifest categories below, an
# empty mapping asserts nothing vacuously: it is a lookup, not a parametrize source, and both
# readers (`KNOWN_BROKEN.get(address)` and the `not in KNOWN_BROKEN` scan) behave when it is
# empty -- the addresses still come from the manifest, so every route is still driven.
#
# The last two entries, and why neither is here:
#   * POST /internal/asset-data/{asset_type}/{data_type} (tj-v7340n) -- all four defects fixed,
#     and FakeSession.refresh() below now simulates the DB-generated columns, so the route is
#     driven end to end and the strict xfail would XPASS. Removed in that same diff, as above.
#   * DELETE /internal/asset-data/{asset_type}/{data_type} (tj-h7ikz2) -- the route was removed
#     rather than repaired. Its schema, StockDataMarketActivityDeleteById, inherits a plain ABC
#     (AssetDataDeleteById, tj-9dqfjo) that cannot be constructed with **kwargs, so repairing it
#     needed a schemas/ fix outside builder-store's scope; it also carried a stale
#     '# TODO this will be removed in the future' comment and its crud function deleted every row
#     in the table regardless of any argument. See tj-h7ikz2 for the full reasoning.
KNOWN_BROKEN: dict[str, str] = {}


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
#
# SHAPE AS OF eec8f88a7443, and both halves of that change are visible here (tj-1njw7c). feed is a
# TOP-LEVEL key, a sibling of source, because it is declared on AssetDataCreate rather than on the
# per-bar data model -- putting it inside `data` gets an extra_forbidden on the way in and a
# missing-field error at the same time, which reads like two problems and is one. And expiry,
# split_factor and dividends_factor are GONE rather than set to None: StockDataMarketActivityData
# is an InboundContract, so an unknown key is a 422 and not a silently ignored extra.
DATA_POINT = {
    'dataset_id': '8f41c2d7-a3b9-4d1e-9c2f-0a1b2c3d4e5f',
    'asset_symbol': 'AAPL',
    'source': 'ALPACA',
    'feed': 'IEX',
    'granularity': '1day',
    'timestamp': '2026-01-02T00:00:00Z',
    'data': {'open': 1.0, 'high': 2.0, 'low': 0.5, 'close': 1.5, 'volume': 100, 'trade_count': 10},
}

# start is present although StoreAssetDatasetBody makes it optional: the handler forwards the body
# into GetDatasetRequest, where start is REQUIRED, so a body without it is not in fact well-formed.
# That asymmetry between the two schemas is a real trap and the reason this constant is commented.
#
# owner is required with no default (schemas/data_store/asset_dataset_store.py) and has been since
# 7e0b2ee, which predates the migration that stalled the rest of this file -- so this line and the
# ones above went stale for unrelated reasons and only looked like one failure (tj-1njw7c).
DATASET_REQUEST = {'owner': 'test-owner', 'source': 'ALPACA', 'granularity': '1day', 'start': '2026-01-01T00:00:00Z'}

# The well-formed GET /internal/asset-data names a dataset_id, and the malformed one does too. Until
# F1b (tj-vhboky.26, ebb2439) both sent NO query at all, which was well-formed while every bars-query
# field was optional. The user ruled on 2026-09-27 (tj-vhboky.20, ruling 5) that a bars query naming
# neither dataset_id nor asset_symbol must be impossible to construct, so `{}` now answers 422 at loc
# ['query'] and is no longer a well-formed request.
#
# THE EMPTY QUERY DID NOT MOVE TO THE MALFORMED SIDE, because it does not fit this file's design:
# each malformed request corrupts exactly ONE named field, and the refusal names no field -- it is a
# model-level error. It is also why the malformed request carries the selector: without it that
# request would corrupt the path AND omit the selector, two faults where the design allows one. The
# empty-query refusal over HTTP is the behaviour-test bead's subject (F3, tj-vhboky.23); the model
# level is pinned in schemas/tests/test_schemas_smoke_data_store.py.
BARS_QUERY = {'params': {'dataset_id': '8f41c2d7-a3b9-4d1e-9c2f-0a1b2c3d4e5f'}}

# The well-formed DELETE. `owner` is a QUERY parameter on /store/{id} (AssetDatasetStoreDelete), and
# it is an authorisation assertion rather than a data field -- so a well-formed delete declares one
# that MATCHES the stored entry FakeResult.scalar_one_or_none() hands back. Without a matching owner
# the handler answers 403, which is below 500 and not in UNREACHABLE_STATUSES and would therefore
# have let the well-formed half pass on a refusal. What a refusal looks like, and that an absent
# owner is refused too, is data/store/tests/test_delete_dataset_entry_route.py's subject, not this
# file's: here the point is only that the route is reachable and answers.
DELETE_OWNER = 'test-owner'
DELETE_ID = uuid.uuid4()

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
        request=BARS_QUERY,
        malformed_path_params={**ASSET_PATH, 'asset_type': 'not-an-asset-type'},
        malformed_request=BARS_QUERY,
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
        path_params={'id': str(DELETE_ID)},
        request={'params': {'owner': DELETE_OWNER}},
        malformed_path_params={'id': 'not-a-uuid'},
        malformed_request={},
        malformed_field='id',
    ),
}


class FakeResult:
    """Stands in for a SQLAlchemy Result over an empty table -- with one row the DELETE must find.

    scalar_one_or_none() is the exception to "empty table", and it is deliberate. Its ONLY reader in
    the driven paths is _get_entry_or_raise() (store_dataset_entry.py:180), reached by
    delete_entry_by_id and by the two id-addressed updates, neither of which has a route. Returning
    None there would make every well-formed DELETE answer 404 -- a status this file reads as "the
    route is not mounted where the manifest says it is", so the well-formed half would report a
    mounting failure for a route that is mounted and working.

    rowcount is a real int for the same reason the recording fake in test_dataset_entry_identity.py
    gives it one: delete_entry_by_id compares result.rowcount to 0, and an attribute that does not
    exist raises AttributeError inside the handler -- which raise_server_exceptions=True surfaces as
    a traceback, not as the 500 a reader would expect.
    """

    rowcount = 1

    def all(self) -> list:
        return []

    def scalars(self) -> 'FakeResult':
        return self

    def scalar_one(self) -> uuid.UUID:
        return uuid.uuid4()

    def first(self) -> None:
        return None

    def scalar_one_or_none(self) -> StoreDatasetEntry:
        # Only id and owner are set: those are the two attributes _check_owner and the DELETE
        # statement read, and inventing values for the other eight identity columns would suggest
        # this file asserts something about them. It does not -- content is tj-19qp1q's tier.
        return StoreDatasetEntry(id=DELETE_ID, owner=DELETE_OWNER)


class FakeSession:
    """Stands in for AsyncSession, matching its sync/async split exactly.

    execute/commit/rollback/close are coroutines and add is not, because that is what
    AsyncSession does. Every method here is one a data_store handler actually reaches.
    """

    def __init__(self) -> None:
        # What add() was handed, so refresh() can refuse an instance the session never saw. Without
        # this the fake was permissive in exactly the way the class docstring forbids: refresh()
        # populated unconditionally, so deleting `db.add(...)` from a crud function outright stayed
        # green here while a real AsyncSession raises InvalidRequestError. Found by the tj-1bv25s
        # architect gate reviewing this file, not by a failure.
        self._added: list[int] = []

    async def execute(self, statement: Any) -> FakeResult:
        return FakeResult()

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def add(self, instance: Any) -> None:
        self._added.append(id(instance))

    async def refresh(self, instance: Any) -> None:
        # REFUSES AN INSTANCE THE SESSION NEVER SAW, as a real AsyncSession does: refreshing an
        # object that was never added raises InvalidRequestError rather than quietly populating it.
        if id(instance) not in self._added:
            raise InvalidRequestError(f'Instance {type(instance).__name__} is not persisted in this Session')

        # POPULATING, NOT A NO-OP, and that is the faithful choice rather than the permissive one.
        # A real AsyncSession.refresh() re-reads the row, so the server-generated columns ARE set
        # when it returns. A no-op refresh would leave id/created_at/updated_at as None and the
        # response_model would then raise a ValidationError against AssetData's required
        # `id: int`, `created_at` and `updated_at` -- a failure no real deployment can produce,
        # which is the fake inventing a defect rather than reproducing one. tj-v7340n's reason
        # text predicted a no-op would be enough; it would not have been.
        #
        # The VALUES are placeholders and nothing asserts them: this file proves reachability and
        # shape, never content (tj-19qp1q owns content against a real database). What the values
        # buy is that the POST route is driven all the way through the real handler, the real
        # crud function and StockMarketActivity.from_create() -- so a regression that reintroduces
        # any of tj-v7340n's four defects, the nested `data` splat above all, turns this red.
        now = datetime.now(UTC)
        for column, value in (('id', 1), ('created_at', now), ('updated_at', now)):
            if getattr(instance, column, None) is None:
                setattr(instance, column, value)


def fake_fetch_client() -> RecordingFetchClient:
    """An IngestFetchClient double answering with an accepted, empty, completed stream.

    REPOINTED FROM THE KAFKA RPC FAKE (validator, gating tj-3mk3u5.10). POST /store now takes an
    ``IngestFetchClient`` through ``get_ingest_fetch_client``, so the override below has to supply
    one; the old ``FakeRpcClients`` answered a dependency the route no longer declares, and the
    POST case failed with ``'NoneType' object has no attribute 'fetch'`` -- the real
    ``get_ingest_fetch_client`` returning its un-initialised module global, which is itself
    evidence that the lifespan never ran.

    THE STREAM IS ACK-THEN-DONE WITH NO PAGES, which is the same decision the Kafka fake's empty
    ``dataset`` made and for the same reason: the handler stores nothing and reports 0 data points,
    because what a populated stream stores is tj-19qp1q's tier and would need a database to store
    into. An empty window is a legitimate success here, not a degenerate one (ADR tj-fa1rpu D2).

    The ack still carries a resolved feed, as the Kafka fake's batch did (tj-1njw7c): ingest is the
    only party entitled to decide the tape, and a fake that omitted it would be more permissive
    than the thing it stands in for.

    Returns:
        RecordingFetchClient: A fresh double per request, matching the old fake's per-call shape.
    """
    return RecordingFetchClient()


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
    declarations, and tj-2h1q3k may add a new one back.

    The placeholder buys a NAMED reason, not visibility as such. Measured, not assumed: pytest's
    default empty_parameter_set_mark is `skip`, so an empty argvalues list already reports
    'got empty parameter set for (address)' rather than collecting nothing silently. What it does
    not say is WHICH category is empty, or why that is expected -- so the placeholder names the
    manifest and the bead instead, and a reader of the summary line learns something.

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
    app.dependency_overrides[get_ingest_fetch_client] = fake_fetch_client
    try:
        # raise_server_exceptions defaults to True and is left that way: an unhandled exception in
        # a handler reaches the test as its own traceback instead of an opaque 500, which is the
        # difference between "this route is broken" and "this route is broken, here is the line".
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def configured_instance_secret(monkeypatch: pytest.MonkeyPatch):
    """Put a real instance secret in the environment for every test in this module.

    AUTOUSE, because the alternative is remembering it per test: a guarded route with no secret
    configured answers 401 no matter what header the request carries (fail closed), and a 401 passes
    every assertion in the well-formed half except the explicit one added there. The dependency reads
    the variable per request, so a function-scoped fixture works against the module-scoped client.

    The two fail-closed cases below deliberately do NOT rely on this: they leave the secret
    configured and omit the HEADER, which is the cause an ordinary caller produces.

    Args:
        monkeypatch: Sets INSTANCE_WRITE_SECRET for the duration of one test.
    """
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)


def module_for(file_path: str):
    """Import the router module the manifest names for one route.

    Args:
        file_path (str): Repository-relative module path, e.g. 'routers/data_store/x.py'.

    Returns:
        ModuleType: The imported module.
    """
    return importlib.import_module(file_path.removesuffix('.py').replace('/', '.'))


def guarded_addresses() -> set[str]:
    """The manifest addresses whose route carries require_instance_secret, read off the routers.

    ASKED OF THE ROUTE OBJECT, not of the wire. A request with no header answers 401 whether the
    route is guarded or the guard is gone and something else rejected it, so deriving the set by
    sending requests would classify a route that LOST its guard as unguarded and go green. The
    decorator-level dependency list is the declaration itself, which is what a reviewer would read.

    Returns:
        set[str]: Guarded addresses.
    """
    guarded = set()
    for fields in manifest_entries(HTTP):
        address, file_path, symbol = fields[1], fields[2], fields[3]
        matching = [route for route in module_for(file_path).router.routes if route.name == symbol]
        assert len(matching) == 1, (
            f'{file_path} registers {len(matching)} routes named {symbol}, so {address} cannot be '
            f'resolved to one route object'
        )
        if any(dependency.dependency is require_instance_secret for dependency in matching[0].dependencies):
            guarded.add(address)
    return guarded


def secret_headers(address: str) -> dict[str, str]:
    """The headers a request to one address must carry to get past the instance-secret guard.

    Keyed on the hand-written GUARDED_ADDRESSES rather than on guarded_addresses(): if the derived
    set were used here, a guard newly added to a read route would be silently accommodated by every
    request this file sends, and only one test would notice. Keyed on the literal, the shape halves
    break too.

    Args:
        address (str): Manifest address.

    Returns:
        dict[str, str]: The secret header, or nothing for an open route.
    """
    return {INSTANCE_SECRET_HEADER: CONFIGURED_SECRET} if address in GUARDED_ADDRESSES else {}


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

    response = client.request(method, url, headers=secret_headers(address), **case.request)

    assert response.status_code not in UNREACHABLE_STATUSES, (
        f'{method} {url} returned {response.status_code}: the manifest declares this route but the '
        f'app does not answer it at that address.'
    )
    assert response.status_code != 422, (
        f'{method} {url} returned 422 ({response.text}). The request this test calls well-formed '
        f'is not; fix the CASES entry, or the malformed half below proves nothing.'
    )
    # THE ASSERTION THAT KEEPS THIS TEST'S NAME TRUE. 401 is not in UNREACHABLE_STATUSES, is not 422
    # and is under 500, so between the guard landing (tj-vhboky.8) and this line the three write
    # routes passed every other assertion here WITHOUT THE HANDLER EVER RUNNING. A broken secret
    # constant, a renamed header or a dropped autouse fixture puts them straight back there, and
    # nothing else in this file would say so.
    assert response.status_code != 401, (
        f'{method} {url} returned 401, so the request never reached the handler and the rest of '
        f'this test asserts nothing about it. The secret header or the configured secret is wrong.'
    )
    assert response.status_code < 500, f'{method} {url} returned {response.status_code}: {response.text}'


@pytest.mark.parametrize('address', http_addresses())
def test_a_malformed_request_is_rejected_with_422(address: str, client: TestClient):
    case = case_for(address)
    method = address.split(' ', 1)[0]
    url = app_url_for(address, symbol_for(address), case.malformed_path_params)

    # AUTHENTICATED, so validation is actually reached on the guarded routes. Widening this
    # assertion to "401 or 422" instead would have made it pin neither.
    response = client.request(method, url, headers=secret_headers(address), **case.malformed_request)

    # problem+json since TE-6 (tj-3mk3u5.37.8): the per-field list is `errors`, not `detail`.
    complained_about = [error['loc'][-1] for error in validation_errors(response)]
    assert case.malformed_field in complained_about, (
        f'{method} {url} returned 422, but about {complained_about} rather than about '
        f'{case.malformed_field!r}, which is the only field this request corrupts.'
    )


def test_exactly_the_write_routes_carry_the_instance_secret_guard():
    """The guard is on the three writes and on nothing else -- both halves of that, in one equality.

    READS ARE OPEN BY DESIGN (routers/common/instance_secret.py: "Apply this to write routes one at a
    time, as a per-route dependency. Never put it in a router-wide dependencies=[...] list, where a
    GET added later would silently inherit it"). A router-wide list is the plausible tidy-up, and it
    would break the open-read design without editing that file or any handler -- so the direction
    that must fail here is a guard APPEARING, not only one going missing. An equality against a
    written-out set catches both; a subset check catches one.
    """
    assert guarded_addresses() == set(GUARDED_ADDRESSES), (
        'the set of routes carrying require_instance_secret is not the set this file expects. A '
        'route that LOST the guard is a write path open to an unauthenticated caller; a route that '
        'GAINED one is a read path that is no longer open, which tj-vhboky.8 decided against.'
    )


@pytest.mark.parametrize('address', sorted(GUARDED_ADDRESSES))
def test_a_guarded_route_rejects_a_request_carrying_no_secret(address: str, client: TestClient):
    """The other property of the same three routes: the guard is live, not merely declared.

    test_exactly_the_write_routes_carry_the_instance_secret_guard reads the declaration off the route
    object; this drives the wire. A dependency that is listed but no longer raises -- an early
    return, a swallowed exception, a refactor that drops the raise -- satisfies the declaration and
    fails here, which is why both exist rather than one standing in for the other.

    The secret IS configured (the autouse fixture) and the HEADER is absent, because that is the
    cause an ordinary misconfigured caller produces. The unset-variable and wrong-value causes, and
    the fact that all three answer identically, belong to common/tests/test_instance_secret.py.

    Args:
        address: A manifest address the design says must be guarded.
        client: The store app with its database and RPC clients replaced.
    """
    case = case_for(address)
    method = address.split(' ', 1)[0]
    url = app_url_for(address, symbol_for(address), case.path_params)

    response = client.request(method, url, **case.request)

    assert response.status_code == 401, (
        f'{method} {url} answered {response.status_code} to a well-formed request carrying no '
        f'instance secret ({response.text}). This is a dataset write path.'
    )
    assert response.json()['detail'] == INSTANCE_SECRET_REJECTION_DETAIL


@pytest.mark.parametrize('address', sorted(GUARDED_ADDRESSES))
def test_a_guarded_route_answers_401_before_it_validates_anything(address: str, client: TestClient):
    """THE ORDERING, pinned as the intended behaviour rather than worked around.

    A decorator-level dependency is inserted AHEAD of the endpoint's own parameter dependencies, so
    the 401 is decided before any path or body value is parsed. That ordering is the reason the three
    malformed cases above had to be authenticated, and it is worth pinning in its own right: an auth
    gate that ran after body validation would parse an unauthenticated caller's payload, and would
    also tell them which of their fields was malformed -- a free validation oracle in front of a
    credential check.

    The request here is BOTH unauthenticated and malformed, which is the only request that can tell
    the two orderings apart. The detail is asserted as well as the status: a 401 raised for some
    unrelated reason would satisfy the status alone.

    Args:
        address: A manifest address the design says must be guarded.
        client: The store app with its database and RPC clients replaced.
    """
    case = case_for(address)
    method = address.split(' ', 1)[0]
    url = app_url_for(address, symbol_for(address), case.malformed_path_params)

    response = client.request(method, url, **case.malformed_request)

    assert response.status_code == 401, (
        f'{method} {url} answered {response.status_code} to a request that is both unauthenticated '
        f'and malformed ({response.text}); validation now runs in front of the credential check.'
    )
    assert response.json()['detail'] == INSTANCE_SECRET_REJECTION_DETAIL


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
    # Authenticated like the halves above: a 401 short-circuit would never reach the database
    # dependency at all, so this test would pass against an app whose override was not taking effect.
    client.request(address.split(' ', 1)[0], url, headers=secret_headers(address), **case.request)

    assert set(PostgresSessionFactory.AsyncSessionHandle._async_engines) == engines_before, (
        'driving a route opened a Postgres engine, so the database dependency override is not '
        'taking effect or the lifespan ran'
    )
    assert get_rpc_clients() is None, (
        'the store app lifespan ran in this process: it is the only thing that sets the module '
        'global get_rpc_clients() reads, and running it needs a live Kafka broker'
    )
    # BOTH GLOBALS, since tj-3mk3u5.10 (validator). The lifespan now also builds the gRPC channel
    # to data_ingest and the IngestFetchClient over it, and that half is the one the POST route
    # actually depends on -- so an override that stopped taking effect would be caught by this
    # line rather than by a confusing AttributeError inside the handler. Asserting only the Kafka
    # global would go on passing if the lifespan were somehow half-run.
    assert get_ingest_fetch_client() is None, (
        'the store app lifespan ran in this process: it is the only thing that sets the module '
        'global get_ingest_fetch_client() reads, and it opens a channel to data_ingest'
    )
