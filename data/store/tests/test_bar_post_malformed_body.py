"""POST /internal/asset-data/{asset_type}/{data_type}: a malformed bar body answers 422 and writes nothing.

WHY THIS FILE EXISTS (validator, gating tj-vhboky.66). The single-bar POST takes its body as
``Body(dict)`` and builds ``StockDataMarketActivityCreate`` inside the handler, so FastAPI never
validates the body itself. Until 666df2d a malformed body raised pydantic's ``ValidationError`` in the
handler, which is not FastAPI's ``RequestValidationError``, and it escaped as an unhandled 500. The fix
catches it around that one construction and re-raises it as ``RequestValidationError`` with each loc
prefixed by ``'body'``. Nothing in the suite drove this route with a bad BODY before:
test_http_smoke.py's malformed case corrupts the PATH (asset_type), which FastAPI rejects before the
handler runs, so it stayed green with the 500 in place.

What is pinned here, each as its own assertion because each fails on its own:
  * the status is 422, not 500;
  * the loc starts with ``['body', <field>]``, so a caller can find the field the way it does on every
    other route. A 422 that names some other field, or the right field without the ``'body'`` prefix,
    fails here;
  * the instance secret, which the request carries, does not appear in the response body or headers;
  * nothing is written. The fake session records every call, and a refusal leaves it with no add, no
    commit and no statement. A check that ran after the row had been staged would pass the status
    assertion and fail this one;
  * a well-formed body still stores, so the catch has not swallowed the success path;
  * each error item has the keys of a 422 FastAPI builds itself on another route of the same app, and
    none carries pydantic's ``url`` key (tj-vhboky.71).

The secret is set by the ``post_bar`` fixture for the whole test, which is why the native-route helper
below needs no fixture of its own.

WHAT TIER THIS IS. The real app and the real handler, in-process through TestClient, with the database
session replaced by a recording fake. There is no Postgres, so the 200 case proves that the handler hands
one row to the session and commits it, not that Postgres stores it. That half is
tests/system/test_http_bars.py's subject.

``raise_server_exceptions=False`` is deliberate here and differs from the other route files. The
regression this file exists for is a 500 status, as the bead words it. With the default of True, a
TestClient re-raises the handler's exception, and the test goes red with a traceback instead of the
status. False lets the red read "returned 500". The 200 case prints the body on failure, so a
traceback is not lost.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import InvalidRequestError

from data.store.app.app_depends import get_rpc_clients
from data.store.app.database.database import async_db
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.main import app
from data.store.tests.problem_body import validation_errors
from routers.common.instance_secret import INSTANCE_SECRET_ENV_VAR, INSTANCE_SECRET_HEADER


pytestmark = pytest.mark.data_store

# Deliberately unlike every other string in the payloads, so a leak check can only match the secret.
CONFIGURED_SECRET = 'bar-post-instance-secret-7f3a'

# The endpoint's __name__, which is also the symbol data_store.manifest records for this address.
ROUTE_NAME = 'create_stock_market_activity_data'
PATH_PARAMS = {'asset_type': 'stock', 'data_type': 'market-activity'}

# A complete StockDataMarketActivityCreate, the same shape as test_http_smoke.py's DATA_POINT. feed is
# top-level, and the bar data carries no split or dividend factors (StockDataMarketActivityData is an
# InboundContract, so an unknown key is a 422).
VALID_BAR = {
    'dataset_id': '8f41c2d7-a3b9-4d1e-9c2f-0a1b2c3d4e5f',
    'asset_symbol': 'AAPL',
    'source': 'ALPACA',
    'feed': 'IEX',
    'granularity': '1day',
    'timestamp': '2026-01-02T00:00:00Z',
    'data': {'open': 1.0, 'high': 2.0, 'low': 0.5, 'close': 1.5, 'volume': 100, 'trade_count': 10},
}


def _without(key: str) -> dict[str, Any]:
    return {field: value for field, value in VALID_BAR.items() if field != key}


# Each body corrupts exactly ONE field, so the loc the 422 names can only be that field. The first three differ
# in kind: a missing top-level field, a wrong type nested inside `data`, and a wrong type at the top
# level. The fourth is a well-typed value the contract refuses: a timestamp with no offset. The nested one matters most for the 'body' prefix: its loc has three parts, so a prefix that
# replaced the first element instead of prepending to it fails here and would pass on the others.
MALFORMED = [
    pytest.param(_without('asset_symbol'), ['body', 'asset_symbol'], 'missing', id='missing-field'),
    pytest.param(
        {**VALID_BAR, 'data': {**VALID_BAR['data'], 'volume': 'lots'}},
        ['body', 'data', 'volume'],
        'int_parsing',
        id='wrong-type-nested',
    ),
    pytest.param({**VALID_BAR, 'dataset_id': 'not-a-uuid'}, ['body', 'dataset_id'], 'uuid_parsing', id='wrong-type'),
    # tj-vhboky.70 (ad7aefe): a timestamp with no offset is REFUSED, not converted (tj-1bl90i; D2 = A on
    # tj-vhboky.20). Before that commit this body was a 200, and the bar was stored at whatever instant
    # the database session's timezone made of it. Being a row here, it gets the same three pins as the
    # others: 422 at ['body', 'timestamp'], no secret echoed, nothing written. The schema-level pins,
    # per bar shape, are schemas/tests/test_bar_timestamp_aware.py.
    pytest.param(
        {**VALID_BAR, 'timestamp': '2026-01-02T00:00:00'}, ['body', 'timestamp'], 'timezone_aware', id='naive-timestamp'
    ),
]


class RecordingSession:
    """An async session that reaches no database and records every call the write path could make.

    add is sync and the rest are coroutines, matching AsyncSession. refresh refuses an instance the
    session was never handed, as a real session does. Otherwise, dropping ``db.add`` from the crud
    function would stay green. It populates the server-generated columns, because a real refresh
    re-reads the row and the response model requires them. The values are placeholders, and nothing
    asserts them.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.statements: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.refreshes = 0

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def execute(self, statement: Any) -> None:
        self.statements.append(statement)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, instance: Any) -> None:
        if not any(instance is added for added in self.added):
            raise InvalidRequestError(f'Instance {type(instance).__name__} is not persisted in this Session')
        self.refreshes += 1
        now = datetime.now(UTC)
        for column, value in (('id', 1), ('created_at', now), ('updated_at', now)):
            if getattr(instance, column, None) is None:
                setattr(instance, column, value)

    async def close(self) -> None:
        return None


@pytest.fixture
def post_bar(monkeypatch: pytest.MonkeyPatch):
    """A callable that drives the real single-bar POST with the right secret against a given session.

    The secret is configured and sent, because the guard is a decorator-level dependency answered
    before the body is read. Without it every request here would be a 401, and not one assertion in
    this file would reach the handler. The URL comes from ``app.url_path_for``, because only main.py
    knows the mount prefix.

    Args:
        monkeypatch: Sets the instance secret for the duration of one test.

    Yields:
        Callable: (session, body) -> httpx.Response.
    """
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)

    def send(session: RecordingSession, body: dict[str, Any]):
        app.dependency_overrides[async_db] = lambda: session
        try:
            client = TestClient(app, raise_server_exceptions=False)
            return client.post(
                app.url_path_for(ROUTE_NAME, **PATH_PARAMS),
                json=body,
                headers={INSTANCE_SECRET_HEADER: CONFIGURED_SECRET},
            )
        finally:
            # `app` is a module-level singleton other test modules import.
            app.dependency_overrides.clear()

    yield send


@pytest.mark.parametrize(('body', 'loc', 'error_type'), MALFORMED)
def test_a_malformed_bar_body_answers_422_at_its_body_loc(post_bar, body: dict, loc: list[str], error_type: str):
    """THE REGRESSION tj-vhboky.66 FIXES: a bad body was an unhandled 500.

    The loc is compared whole, not only its last element. test_http_smoke.py reads ``loc[-1]``, and a
    loc missing its ``'body'`` prefix would pass that check. Pinning the prefix is the point here,
    because it is what makes this route's 422 look like every other route's.
    """
    response = post_bar(RecordingSession(), body)

    errors = [(error['loc'], error['type']) for error in validation_errors(response)]
    assert errors == [(loc, error_type)], (
        f'the 422 should name exactly {loc} ({error_type}), the one field this body corrupts; it named {errors}'
    )


@pytest.mark.parametrize(('body', 'loc', 'error_type'), MALFORMED)
def test_a_malformed_bar_body_does_not_echo_the_secret(post_bar, body: dict, loc: list[str], error_type: str):
    """Nothing of the request's own may come back in its refusal, and the request carries the secret.

    Two things keep this check from being vacuous. First, the status is asserted to be the 422 and not
    a 401, so the secret was accepted and the handler ran. Second, the check is a bool, so a leaked
    secret is never printed in the failure message.

    THE PREMISE CHANGED AND THE CASE SURVIVES IT (validator, gating tj-3mk3u5.37.8). This used to open
    "the 422 carries the body's own input back to the caller", which was true of FastAPI's native 422
    and is the reason an echoed secret was reachable at all. TE-6's ValidationIssue drops `input`, so
    that route to a leak is closed by construction. The case is NOT therefore retired: the secret
    travels in a HEADER, and the assertion below reads the headers as well as the body, which no
    structural guarantee about the errors array covers. What it pins is now narrower and still real.
    """
    response = post_bar(RecordingSession(), body)

    assert response.status_code == 422, f'returned {response.status_code}, so the handler never ran'
    leaked = CONFIGURED_SECRET in response.text or CONFIGURED_SECRET in str(response.headers)
    assert not leaked, 'the 422 response carries the instance secret (value withheld)'


@pytest.mark.parametrize(('body', 'loc', 'error_type'), MALFORMED)
def test_a_malformed_bar_body_writes_nothing(post_bar, body: dict, loc: list[str], error_type: str):
    """A refusal is a refusal only if nothing reached the session before it."""
    session = RecordingSession()

    response = post_bar(session, body)

    assert response.status_code == 422, f'returned {response.status_code}: {response.text}'
    assert session.added == [], f'the refused body was still staged: {session.added}'
    assert session.statements == [], f'the refused body still sent statements: {session.statements}'
    assert session.commits == 0, 'the refused body was still committed'


# ---------------------------------------------------------------------------------------------
# THE ERROR ITEM'S SHAPE (tj-vhboky.71). The re-raise above was built from pydantic's e.errors(), whose
# items carry a 'url' key (a link to pydantic's docs) that FastAPI's own 422s omit, so this route's
# items did not have the shape every other route's have -- which is the whole reason tj-vhboky.66
# re-raised as RequestValidationError. The tj-vhboky.66 tests above compare (loc, type) only, and so
# pinned neither the presence nor the absence of the key.

# The native side of the comparison: POST /store, whose body FastAPI validates itself. The same
# unknown source is sent to both routes, so both answer with the SAME error type ('enum', which carries
# a ctx), and a key difference can only come from how the item was built, not from the error kind.
NATIVE_ROUTE_NAME = 'store_data'
NATIVE_PATH_PARAMS = {'asset_type': 'stock', 'data_type': 'market-activity', 'asset_symbol': 'AAPL'}
NATIVE_BODY = {'owner': 'a-strategy', 'source': 'ALPACA', 'granularity': '1day', 'start': '2026-01-02T00:00:00Z'}
UNKNOWN_SOURCE = 'NOT-A-SOURCE'


def _native_422(session: RecordingSession) -> list[dict[str, Any]]:
    """The error items FastAPI itself builds for a bad `source` on POST /store, from the same app.

    The rpc factory is overridden with a sentinel only so that resolving the route's dependencies
    reaches no Kafka; the body is refused before the handler runs, so nothing is asked of either.
    """
    app.dependency_overrides[async_db] = lambda: session
    app.dependency_overrides[get_rpc_clients] = lambda: object()
    try:
        response = TestClient(app, raise_server_exceptions=False).post(
            app.url_path_for(NATIVE_ROUTE_NAME, **NATIVE_PATH_PARAMS),
            json={**NATIVE_BODY, 'source': UNKNOWN_SOURCE},
            headers={INSTANCE_SECRET_HEADER: CONFIGURED_SECRET},
        )
    finally:
        app.dependency_overrides.clear()
    return validation_errors(response)


@pytest.mark.parametrize(('body', 'loc', 'error_type'), MALFORMED)
def test_no_malformed_bar_error_item_carries_a_url_key(post_bar, body: dict, loc: list[str], error_type: str):
    """Every refused body, not just the one compared below: no item carries pydantic's docs link.

    The comparison test below uses one error kind ('enum'). This covers the four kinds in MALFORMED,
    so an error kind whose item gains a url some other way (a hand-built item in a later branch, say)
    is still caught.

    THE GUARANTEE IS STRUCTURAL NOW, NOT INCIDENTAL (validator, gating tj-3mk3u5.37.8), and this case
    is kept rather than deleted because it is the one that says so. Before TE-6 the item was whatever
    FastAPI handed out, so the absence of pydantic's url key was a property of how the item happened
    to be built and this assertion was the only thing holding it. Now every 422 item is rendered
    through routers/common/errors.py's declared ValidationIssue, which has exactly loc, msg and type,
    so the url key is unreachable by construction -- and the assertion below, read on its own, has
    become one that cannot fail. What replaces it is the same check widened and moved into
    validation_errors: no key BEYOND loc/msg/type, over every caller of that helper rather than over
    this file's four bodies. The widening matters more than the url key ever did, because the key
    that would actually hurt is `input`, which echoes the rejected value -- an owner, here -- back to
    the caller (D8). The url assertion stays below as the named regression it was.
    """
    response = post_bar(RecordingSession(), body)

    carrying = [error['loc'] for error in validation_errors(response) if 'url' in error]
    assert carrying == [], f"these error items still carry pydantic's url key: {carrying}"


def test_the_bar_422_items_have_the_keys_of_a_fastapi_native_422(post_bar):
    """The single-bar 422 has the same item keys as a 422 FastAPI builds itself, compared, not listed.

    The expected key set is read off a real FastAPI 422 from another route of the same app, never
    written out here: a hand-listed set would pin this file's belief about FastAPI, and would go on
    passing if a FastAPI upgrade changed its own shape while this route kept the old one. What is
    asserted is the property tj-vhboky.66 set out to give callers -- one shape across every route.

    The error type is asserted equal first, so the key comparison is like for like: an 'enum' item
    carries a ctx that a 'missing' item does not, and comparing different kinds would red, or pass,
    for a reason unrelated to the url key.
    """
    native = _native_422(RecordingSession())
    response = post_bar(RecordingSession(), {**VALID_BAR, 'source': UNKNOWN_SOURCE})

    (bar_error,) = validation_errors(response)
    (native_error,) = native
    assert bar_error['type'] == native_error['type'], (
        f'not a like-for-like comparison: the bar route answered {bar_error["type"]}, the native route '
        f'{native_error["type"]}'
    )
    assert sorted(bar_error) == sorted(native_error), (
        f'the single-bar 422 item has keys {sorted(bar_error)}; a FastAPI-native 422 from POST /store has '
        f'{sorted(native_error)}'
    )


def test_a_well_formed_bar_body_is_stored_and_returned(post_bar):
    """The catch wraps the construction only, so a valid body still reaches the write and comes back."""
    session = RecordingSession()

    response = post_bar(session, VALID_BAR)

    assert response.status_code == 200, f'returned {response.status_code}: {response.text}'
    assert len(session.added) == 1, f'expected one staged row, got {session.added}'
    (row,) = session.added
    assert isinstance(row, StockMarketActivity), f'staged a {type(row).__name__}, not a bar row'
    assert session.commits == 1, f'expected one commit, got {session.commits}'
    assert session.rollbacks == 0, f'the write rolled back {session.rollbacks} time(s)'

    returned = response.json()
    assert uuid.UUID(returned['dataset_id']) == uuid.UUID(VALID_BAR['dataset_id'])
    assert returned['asset_symbol'] == VALID_BAR['asset_symbol']
    assert returned['feed'] == VALID_BAR['feed']
    assert returned['data'] == VALID_BAR['data']
