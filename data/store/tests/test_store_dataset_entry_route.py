"""POST /store/{asset_type}/{data_type}/{asset_symbol}: the 409 own-overlap answer, and whose principal the write lands under.

WHY THIS FILE EXISTS (validator, gating tj-vhboky.8). Commit fe68561 wired the write route to answer
409 with the colliding dataset id(s), and threaded the caller's principal through to the entry and to
the ingest fetch. The architect gate measured what the suite actually held over that and found two
holes, each invisible to all 432 tests as they stood:

  1. THE 409 MAPPING WAS PINNED NOWHERE ABOVE THE EXCEPTION. Only the exception layer was covered --
     test_dataset_entry_identity.py asserts that ``OwnOverlapConflict`` carries its colliding ids --
     and nothing drove the ROUTE into an overlap. Three separate mutations were green across the whole
     suite: 409 -> 400; the structured ``detail`` replaced by ``str(e)``; and the entire
     ``except OwnOverlapConflict`` clause bound to a class this path never raises, which turns an own
     overlap into an unhandled 500. tj-vhboky.8 words the body shape as "the entire point of the
     ruling... a bare 500 or an unstructured message makes the ruling unbuildable", because a caller
     builds auto-extend by catching this, reading the id and issuing the extend itself. That third
     mutation is exactly the named regression, and it measured nothing.
  2. THE PRINCIPAL WAS THREADED BUT ITS VALUE WAS UNASSERTED, and the split is the useful part.
     Dropping ``owner`` from ``request_body.model_dump()`` in data_action_request.py reds two existing
     tests, so "the dump carries the new fields" IS covered. SUBSTITUTING the value is not:
     ``model_dump() | {'owner': 'somebody-else'}`` was green across all 432 on BOTH legs -- the
     ``AssetDatasetStoreCreate`` handed to ``upsert_entry`` and the ``GetDatasetRequest`` handed to
     ``send_request``. owner is identity on the entry (tj-vhboky.1 section 2), so that mutation writes
     the dataset under the wrong principal and makes ingest fetch for the wrong principal, silently.

WHY THE SECOND HALF IS A TEST RATHER THAN A CODE FIX, which is the part worth not misreading: the
production code is CORRECT. fe68561 never touched data_action_request.py and did not need to -- owner
is a declared required field on both target models, so the pre-existing ``model_dump()`` splat carries
it by construction. That is precisely why it needs an assertion. The property holds by accident of the
splat and nothing records that it must, which is the same "correct but incidental" shape as the
owner-column constraint pinned in test_delete_dataset_entry_route.py, one layer up.

WHAT TIER THIS IS, and what it therefore cannot say. The route is driven through TestClient against a
recording fake session, exactly as test_delete_dataset_entry_route.py drives the DELETE. There is no
Postgres and no Kafka, so:
  * every status code here is one FastAPI assembled IN-PROCESS, not one a live deployment returned;
  * that ``_find_own_overlap``'s SQL selects the right rows is NOT proved here -- the fake answers a
    canned row set and never evaluates a predicate. The overlap FORMULA (the sentinel-aware range
    comparison, the exact-repeat exclusion, the eight-column equality prefix) is
    test_dataset_entry_identity.py's subject and stays there;
  * nothing about the ON CONFLICT clause firing against a real unique index is observable here
    (host-verified tier, tj-vhboky.14).
What IS proved is the half a unit-tier test owns: which status and which BODY SHAPE a caller receives
when their request overlaps their own dataset, that no INSERT and no ingest fetch happen on that
refusal, that a request overlapping nothing still gets through, and that the principal the caller
declared is the principal both collaborators are handed.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Select
from sqlalchemy.dialects.postgresql import Insert as PostgresInsert

from common.enums.data_select import AssetType, DataType
from data.store.app.app_depends import get_rpc_clients
from data.store.app.database.database import async_db
from data.store.app.ingest import data_action_request
from data.store.app.main import app
from routers.common.instance_secret import INSTANCE_SECRET_ENV_VAR, INSTANCE_SECRET_HEADER
from schemas.data_ingest.get_dataset_request import GetDatasetRequest
from schemas.data_store.asset_dataset_store import AssetDatasetStoreCreate, StoreAssetDatasetBody, StoreAssetDatasetPath
from schemas.data_store.stock.market_activity_data import BatchStockDataMarketActivityCreate


pytestmark = pytest.mark.data_store

# The principal the request declares. DELIBERATELY UNLIKE EVERY OTHER STRING IN THE PAYLOAD: the
# mutation this pins substitutes one owner string for another, so an assertion that matched a value
# also carried by asset_symbol or by source could pass for the wrong reason.
DECLARED_PRINCIPAL = 'strategy-that-asked'

CONFIGURED_SECRET = 'store-route-instance-secret'

# The route's name, which FastAPI takes from the endpoint's __name__ and which app.url_path_for()
# resolves through whatever prefix data/store/app/main.py mounts the router under. It is also the
# symbol routers/tests/interface_manifest/data_store.manifest records for this address.
ROUTE_NAME = 'store_data'

PATH_PARAMS = {'asset_type': 'stock', 'data_type': 'market-activity', 'asset_symbol': 'AAPL'}

# A well-formed StoreAssetDatasetBody. `start` is present although the model gives it no default,
# and `end` is absent -- the open-ended case, which is also the one data_action_request.py forwards
# into GetDatasetRequest where `end` is required-but-nullable. See test_http_smoke.py's DATASET_REQUEST
# for why a body without `start` is not in fact well-formed.
REQUEST_BODY = {'owner': DECLARED_PRINCIPAL, 'source': 'ALPACA', 'granularity': '1day', 'start': '2026-01-01T00:00:00Z'}


class FakeResult:
    """One canned answer, faithful to the two accessors the create path uses.

    ``all()`` is what ``_find_own_overlap`` reads, as a list of one-column rows
    (``[row[0] for row in result.all()]``). ``scalar_one()`` is what the upsert reads back from its
    RETURNING clause. Nothing here evaluates the statement it is answering -- see the module header
    for what that means this file does and does not prove.
    """

    def __init__(self, *, rows: list[tuple] | None = None, scalar: uuid.UUID | None = None):
        self._rows = rows if rows is not None else []
        self._scalar = scalar

    def all(self) -> list[tuple]:
        return self._rows

    def scalar_one(self) -> uuid.UUID:
        assert self._scalar is not None, 'the create path read an id back from a result that was not given one'
        return self._scalar


class FakeSession:
    """An async session that records every statement and reaches no database.

    The recording is the point, and for the same reason it is in the DELETE route's file: "it answered
    409" is not the whole claim. A conflict that was detected AFTER the insert had been sent would
    satisfy the status assertion while having already written the row the refusal claims it rejected,
    so the conflict cases below also assert that no INSERT was ever issued.

    Statements are classified BY TYPE rather than by compiling them to SQL text (which is what
    test_dataset_entry_identity.py does, because the SQL text is its subject). Here the only question
    is "was an insert sent at all", and a type check cannot be fooled by a formatting change.
    """

    def __init__(self, *results: FakeResult):
        self._results = list(results)
        self.statements: list = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement):
        self.statements.append(statement)
        assert self._results, 'the create path issued more statements than the fixture planned for'
        return self._results.pop(0)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        return None


class RecordingRpcClient:
    """Answers the ingest RPC with an empty dataset, and records what it was asked for.

    An empty ``dataset`` means the handler stores no bars and reports 0 data points: the bar write
    path has its own files (test_bar_write_path.py, test_bar_batch_chunking.py) and re-driving it here
    would make this file depend on things it asserts nothing about.
    """

    def __init__(self) -> None:
        self.requests: list = []

    async def send_request(self, request) -> BatchStockDataMarketActivityCreate:
        self.requests.append(request)
        return BatchStockDataMarketActivityCreate(
            asset_symbol='AAPL', source='ALPACA', feed='IEX', granularity='1day', dataset_id=uuid.uuid4(), dataset={}
        )


class RecordingRpcClients:
    """Stands in for KafkaRpcFactory.RpcClients, handing out one shared client so its record is readable."""

    def __init__(self) -> None:
        self.client = RecordingRpcClient()

    def get_client(self, endpoint) -> RecordingRpcClient:
        return self.client


def _sent_inserts(session: FakeSession) -> list:
    """Every INSERT the session was handed.

    Returns:
        list: The insert statements, which the conflict cases require to be empty.
    """
    return [statement for statement in session.statements if isinstance(statement, PostgresInsert)]


def _sent_selects(session: FakeSession) -> list:
    """Every SELECT the session was handed.

    Returns:
        list: The select statements. The overlap probe is one of these, and a conflict case asserts
            the probe ran rather than the refusal coming from somewhere else entirely.
    """
    return [statement for statement in session.statements if isinstance(statement, Select)]


@pytest.fixture
def post_dataset(monkeypatch: pytest.MonkeyPatch):
    """A callable that drives the real POST route against a given fake session and RPC clients.

    The instance secret is configured and sent, because the guard is a decorator-level dependency and
    is answered BEFORE any of this route's own parameters are read -- an unauthenticated request never
    reaches the handler and could not exercise a single assertion in this file. The 401 itself, and
    that ordering, are pinned in test_http_smoke.py and are not this file's subject.

    The URL is asked of ``app.url_path_for`` rather than written out: only data/store/app/main.py knows
    what prefix the router is mounted under, and hard-coding the path would assume the answer.

    ``raise_server_exceptions`` is left at its default True, so an unhandled exception inside the
    handler -- which is what an ``except`` clause bound to the wrong class produces -- arrives as its
    own traceback rather than as an opaque 500.

    Args:
        monkeypatch: Sets INSTANCE_WRITE_SECRET for the duration of one test.

    Yields:
        Callable: (session, rpc_clients, body) -> httpx.Response.
    """
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)

    def send(session: FakeSession, rpc_clients: RecordingRpcClients, body: dict | None = None):
        app.dependency_overrides[async_db] = lambda: session
        app.dependency_overrides[get_rpc_clients] = lambda: rpc_clients
        try:
            client = TestClient(app)
            return client.post(
                app.url_path_for(ROUTE_NAME, **PATH_PARAMS),
                json=REQUEST_BODY if body is None else body,
                headers={INSTANCE_SECRET_HEADER: CONFIGURED_SECRET},
            )
        finally:
            # `app` is a module-level singleton other test modules import.
            app.dependency_overrides.clear()

    yield send


# ---------------------------------------------------------------------------------------------
# The 409, and the body shape that is the point of it
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize('collision_count', [1, 2], ids=['one-colliding-dataset', 'two-colliding-datasets'])
def test_an_own_overlap_answers_409_carrying_the_ids_a_caller_can_extend(post_dataset, collision_count: int):
    """THE MAPPING tj-vhboky.8 CALLS THE ENTIRE POINT OF THE RULING, driven at the route for the first time.

    The status and the body are asserted as two separate claims because they fail separately, and all
    three of the mutations below were green across the whole suite before this test existed:
      * ``status.HTTP_409_CONFLICT`` -> 400. A caller cannot distinguish "your request collides with a
        dataset you already hold, here is its id" from "your request is malformed" -- and 409 is the
        code the retry-then-extend flow keys on.
      * ``detail={'message': ..., 'colliding_ids': [...]}`` -> ``detail=str(e)``. The ids are still in
        the response TEXT, which is why this is asserted by KEY and not by substring: a caller that
        has to regex an error message to find the id it must extend cannot be said to have an API.
        ``isinstance(detail, dict)`` is asserted explicitly so this mutation names itself instead of
        arriving as a TypeError on a string subscript.
      * ``except OwnOverlapConflict`` bound to a class this path never raises -> the conflict escapes
        the handler as an unhandled 500, which is the regression the bead names in so many words.

    WHY THE COLLISION COUNT IS PARAMETRIZED, and it is not symmetry. ``OwnOverlapConflict`` carries a
    LIST -- one request can overlap several of the same owner's datasets -- and the route comprehends
    over it (``[str(entry_id) for entry_id in e.colliding_ids]``). TWO OPERAND-LEVEL MUTATIONS OF THAT
    COMPREHENSION SPLIT THE TWO PARAMETERS, measured rather than assumed:
      * ``[str(e.colliding_ids)]`` -- the whole list stringified into one element. Reds BOTH parameters.
      * ``[str(e.colliding_ids[0])]`` -- only the first id reported, which is the plausible one, because
        "there is a collision, here is the id" reads like a singular answer. Reds ONLY
        two-colliding-datasets. A caller told about one of three collisions extends that one and gets
        refused again on the next attempt, with no way to see why.
    So the one-id case pins the SHAPE and the two-id case pins the CARDINALITY, and the second is the
    half a single case would have lost.

    NO INSERT AND NO INGEST FETCH, which is the ordering half of the requirement. The check runs before
    the insert is attempted (``upsert_entry``: "checked BEFORE the insert is attempted"), and the route
    calls ingest only after the entry exists. A conflict detected one step late would answer the same
    409 having already written the row and asked the vendor for data, and the status assertion alone
    cannot tell those apart. THE FIRST HALF OF THAT IS ALREADY PINNED ONE LAYER DOWN, and this file does
    not claim it: test_dataset_entry_identity.py's test_the_overlap_check_runs_before_any_insert_is_sent
    asserts exactly the no-INSERT property against ``upsert_entry`` directly. What is only observable
    here is the INGEST leg -- the route asks the vendor for data through the worker, and no crud-level
    test drives that -- so the insert assertion is a second layer over a covered property and the
    ``rpc_clients.client.requests == []`` assertion is a new one.

    Args:
        post_dataset: Drives the real POST route against a fake session.
        collision_count: How many of the owner's own datasets the request is told it overlaps.
    """
    colliding_ids = [uuid.uuid4() for _ in range(collision_count)]
    session = FakeSession(FakeResult(rows=[(entry_id,) for entry_id in colliding_ids]))
    rpc_clients = RecordingRpcClients()

    response = post_dataset(session, rpc_clients)

    assert response.status_code == 409, (
        f"a request overlapping the same owner's existing dataset(s) answered {response.status_code} "
        f'rather than 409: {response.text}'
    )

    detail = response.json()['detail']
    assert isinstance(detail, dict), (
        f'the 409 detail is {type(detail).__name__} rather than a structured object: {detail!r}. A '
        f'caller builds auto-extend by READING the colliding id out of this body; an unstructured '
        f'message makes that unbuildable (tj-vhboky.8).'
    )
    assert detail['colliding_ids'] == [str(entry_id) for entry_id in colliding_ids], (
        f'the 409 body does not carry the colliding dataset ids under colliding_ids: {detail!r}'
    )

    assert _sent_selects(session), 'the overlap probe never ran, so the 409 came from somewhere else'
    assert _sent_inserts(session) == [], 'the entry was inserted despite the overlap being reported as a conflict'
    assert session.commits == 0, 'the conflicting request committed something'
    assert rpc_clients.client.requests == [], 'ingest was asked to fetch data for a request that was refused'


def test_a_request_overlapping_nothing_is_written_and_answers_200(post_dataset):
    """THE SUCCESS PATH, and it is not decoration -- it is the only thing that catches a guard that refuses too much.

    Every assertion above is satisfied by a route that answers 409 to EVERYTHING, and the operand-level
    mutation that does exactly that looks like a null-safety tidy-up: ``if colliding_ids:`` ->
    ``if colliding_ids is not None:`` in ``upsert_entry``. ``_find_own_overlap`` returns a LIST, so the
    empty list is not None, and every legitimate create becomes a 409 naming no ids at all.

    WHAT THAT MUTATION ACTUALLY MEASURED, stated as measured and not as this case's own achievement: 13
    red, of which 12 are pre-existing cases in test_dataset_entry_identity.py that drive ``upsert_entry``
    through to its insert, and the thirteenth is this one. So this case is NOT the only witness -- the
    crud layer already refuses to let that mutation through. It is the witness AT THE ROUTE, which is
    the layer the two conflict cases above live at, and without it every assertion this file makes about
    POST /store is a refusal assertion.

    That is the same shape as the lesson from the DELETE work, one function over, and it is why a
    success case is worth its place at each layer that has refusal cases: changing ``!=`` to ``is not``
    in ``_check_owner`` reds ONLY the happy path, because an HTTP-parsed query string is never the
    interned constant, so identity comparison refuses the LEGITIMATE owner. A file made only of refusal
    cases cannot see a guard that refuses too much.

    The 200 body is asserted too. ``data_points`` is 0 because the fake ingest reply carries an empty
    dataset -- what a populated one stores belongs to test_bar_batch_chunking.py -- but the key must be
    there: the route's contract is a count, and the worker returning None would still be a 200.
    """
    new_entry_id = uuid.uuid4()
    session = FakeSession(FakeResult(rows=[]), FakeResult(scalar=new_entry_id))
    rpc_clients = RecordingRpcClients()

    response = post_dataset(session, rpc_clients)

    assert response.status_code == 200, (
        f'a request that overlaps nothing answered {response.status_code}: {response.text}. The own-'
        f'overlap guard is refusing a legitimate create.'
    )
    assert response.json() == {'message': 'Data stored', 'data_points': 0}
    assert len(_sent_inserts(session)) == 1, 'the accepted request did not insert exactly one entry'
    assert session.commits == 1, 'the accepted request was never committed'
    assert len(rpc_clients.client.requests) == 1, 'the accepted request never asked ingest for its data'


# ---------------------------------------------------------------------------------------------
# A naive datetime is refused at the edge (tj-1bl90i)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('field', 'offset_less'),
    [('start', '2026-01-01T00:00:00'), ('end', '2026-03-01T00:00:00'), ('expiry', '2026-02-01T00:00:00')],
)
def test_an_offset_less_datetime_answers_422_naming_its_field_and_writes_nothing(
    post_dataset, field: str, offset_less: str
):
    """THE CALLER'S VIEW OF THE USER RULING ON tj-1bl90i (2026-09-27): REFUSE, for start, end and expiry.

    All three land in timestamptz columns, where an offset-less value is read in the Postgres SESSION
    timezone. The schema-level refusal is pinned in schemas/tests/test_schemas_smoke_data_store.py;
    what only this layer shows is that the refusal reaches the caller as a 422 naming the field --
    not a 500 from a ValidationError raised deeper in the worker, where AssetDatasetStoreCreate is
    rebuilt from the body -- and that it happens BEFORE anything is sent to the database or to ingest.
    The session is given no canned results at all, so a single statement would fail the test on its
    own assertion.

    Args:
        post_dataset: Drives the real POST route against a fake session.
        field: The datetime field sent without an offset.
        offset_less: Its ISO-8601 text, with no 'Z' and no offset.
    """
    session = FakeSession()
    rpc_clients = RecordingRpcClients()

    response = post_dataset(session, rpc_clients, REQUEST_BODY | {field: offset_less})

    assert response.status_code == 422, f'an offset-less {field} answered {response.status_code}: {response.text}'
    assert [(error['loc'], error['type']) for error in response.json()['detail']] == [
        (['body', field], 'timezone_aware')
    ], f'the 422 does not name {field} as timezone_aware: {response.text}'
    assert session.statements == [], 'a request refused at the edge still reached the database'
    assert rpc_clients.client.requests == [], 'ingest was asked to fetch data for a request that was refused'


# The GET on the same address: the dataset SEARCH, bound to StoreAssetDatasetQuery with Query().
SEARCH_ROUTE_NAME = 'get_data'


@pytest.mark.parametrize(
    ('field', 'offset_less'),
    [
        ('start', '2026-01-01T00:00:00'),
        ('end', '2026-03-01T00:00:00'),
        ('created_at', '2026-02-01T09:30:00'),
        ('updated_at', '2026-02-02T16:45:00'),
    ],
)
def test_an_offset_less_search_bound_answers_422_naming_its_field_and_reads_nothing(field: str, offset_less: str):
    """THE CALLER'S VIEW OF USER RULING D2 = (A) ON tj-vhboky.20 (2026-09-27): REFUSE, on the search too.

    GET /store/{asset_type}/{data_type}/{asset_symbol} filters store_dataset_entry by these four
    bounds, compared against timestamptz columns, where an offset-less value is read in the Postgres
    SESSION timezone. fa1d7ee made them AwareDatetime; the schema-level refusal is pinned in
    schemas/tests/test_schemas_smoke_data_store.py. What only this layer shows is that the refusal
    reaches the caller as a 422 at ``['query', field]`` -- the route binds the model with Query(), so
    a Depends() or body binding would name a different location -- and that it happens before
    search_entries runs. The session is given no canned results, so a single statement would fail on
    the session's own assertion as well as on the empty-statements check.

    The GET carries no instance-secret guard (only the write routes do; test_http_smoke.py pins
    which), so no header is sent and the 422 cannot be a 401 in disguise.

    Args:
        field: The time filter sent without an offset.
        offset_less: Its ISO-8601 text, with no 'Z' and no offset.
    """
    session = FakeSession()
    app.dependency_overrides[async_db] = lambda: session
    try:
        response = TestClient(app).get(app.url_path_for(SEARCH_ROUTE_NAME, **PATH_PARAMS), params={field: offset_less})
    finally:
        # `app` is a module-level singleton other test modules import.
        app.dependency_overrides.clear()

    assert response.status_code == 422, f'an offset-less {field} answered {response.status_code}: {response.text}'
    assert [(error['loc'], error['type']) for error in response.json()['detail']] == [
        (['query', field], 'timezone_aware')
    ], f'the 422 does not name {field} as timezone_aware: {response.text}'
    assert session.statements == [], 'a search refused at the edge still reached the database'


# ---------------------------------------------------------------------------------------------
# Whose principal the write lands under
# ---------------------------------------------------------------------------------------------


def _only_instance(mock_call, model_type):
    """The single argument of ``mock_call`` that is an instance of ``model_type``, positional or keyword.

    Searched rather than indexed so that a later builder switching ``upsert_entry(db, entry)`` to a
    keyword argument -- a legitimate refactor -- does not red this test for a reason that has nothing
    to do with the principal it asserts.

    Args:
        mock_call: A ``unittest.mock`` call object, i.e. ``some_mock.await_args``.
        model_type: The model class to look for.

    Returns:
        The one matching argument.
    """
    assert mock_call is not None, f'nothing was ever called with a {model_type.__name__}'
    matching = [value for value in (*mock_call.args, *mock_call.kwargs.values()) if isinstance(value, model_type)]
    assert len(matching) == 1, f'the call was handed {len(matching)} {model_type.__name__} arguments, expected 1'
    return matching[0]


@pytest.mark.asyncio
async def test_the_worker_writes_and_fetches_under_the_principal_the_caller_declared(monkeypatch: pytest.MonkeyPatch):
    """The caller's ``owner`` is the owner on BOTH models the worker builds -- the value, not just the key.

    WHY THIS EXISTS WHEN THE PRODUCTION CODE IS ALREADY CORRECT. ``store_market_activity_worker`` builds
    ``AssetDatasetStoreCreate`` and ``GetDatasetRequest`` by splatting ``request_body.model_dump()``, and
    ``owner`` is a declared required field on both, so the splat carries it by construction and nothing
    had to be written for it to work. The gap is that nothing RECORDED that it must: substituting the
    value on either leg -- ``request_body.model_dump() | {'owner': 'somebody-else'}`` -- was green across
    all 432 tests, on both legs independently.

    WHAT EACH LEG COSTS, because they are two failures and not one. owner is identity on the entry
    (tj-vhboky.1 section 2), so a substituted owner on the ``AssetDatasetStoreCreate`` leg writes the
    dataset under a principal that never asked for it -- and since owner is part of the ten-column
    unique key, it is a DIFFERENT dataset, not a mislabelled one: the requesting principal's own later
    request for the same spec will not find it, and the id it was handed belongs to somebody else's
    entry. On the ``GetDatasetRequest`` leg it makes ingest fetch on behalf of the wrong principal;
    ``BaseGetDatasetRequest.owner`` exists precisely "so the fetch knows which principal it is acting
    for" and its comment already says the field "may not be invented here".

    WHY THE EXISTING COVERAGE DOES NOT REACH IT, measured by the architect gate rather than assumed:
    DROPPING owner from the splat reds two tests (the chunk-size worker test and the POST case in
    test_http_smoke.py), because both target models then fail validation on a missing required field.
    Those tests prove the KEY is present. Neither looks at the VALUE, and a wrong value validates
    perfectly.

    ``upsert_entry`` and ``batch_create_market_activity_data`` are replaced at the module level, not the
    session: this test asserts what the worker HANDS its collaborators, so the collaborators are the
    seam. The reply carries an empty dataset, so the bar write path is not reached at all.

    Args:
        monkeypatch: Replaces the worker's two collaborators for the duration of the test.
    """
    upsert = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(data_action_request, 'upsert_entry', upsert)

    rpc_client = RecordingRpcClient()
    rpc_clients = MagicMock()
    rpc_clients.get_client = MagicMock(return_value=rpc_client)

    await data_action_request.store_market_activity_worker(
        StoreAssetDatasetPath(asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol='AAPL'),
        StoreAssetDatasetBody(**REQUEST_BODY),
        MagicMock(),
        rpc_clients,
    )

    created = _only_instance(upsert.await_args, AssetDatasetStoreCreate)
    assert created.owner == DECLARED_PRINCIPAL, (
        f'the entry is being written under {created.owner!r} rather than under the principal the caller '
        f'declared ({DECLARED_PRINCIPAL!r}). owner is identity, so this is a different dataset.'
    )

    assert len(rpc_client.requests) == 1, 'the worker did not ask ingest for the dataset exactly once'
    fetched = rpc_client.requests[0]
    assert isinstance(fetched, GetDatasetRequest), f'ingest was asked with a {type(fetched).__name__}'
    assert fetched.owner == DECLARED_PRINCIPAL, (
        f'ingest is being asked to fetch on behalf of {fetched.owner!r} rather than the principal the '
        f'caller declared ({DECLARED_PRINCIPAL!r})'
    )


@pytest.mark.asyncio
async def test_the_entry_and_the_fetch_agree_on_every_identity_field_the_caller_sent(monkeypatch: pytest.MonkeyPatch):
    """The generalisation of the case above, for the fields that travel the same splat as ``owner``.

    NOT A DUPLICATE, and the reason is the failure mode rather than the fields. The case above pins ONE
    field by name because owner is the one that is an authorisation and identity decision. This pins
    that the two models the worker builds do not DISAGREE about the request: they are built from two
    separate splats of the same ``model_dump()``, so a substitution or a stale forward on one leg and
    not the other is a real shape, and it is the shape that makes the entry claim coverage of a range
    the fetch was never asked for -- this epic's stated failure class.

    IT EARNS ITS PLACE ON TWO MUTATIONS NOTHING ELSE CATCHES, measured against the whole suite:
      * recomputing ``expiry`` on the fetch leg instead of forwarding the body's --
        ``model_dump() | {'expiry': datetime.now(UTC) + timedelta(days=2)}`` -- which is exactly the
        plausible repair, since the body computes that default itself. 1 red: this case. The entry then
        records one expiry and ingest is told another.
      * ``data_types=[request_path.data_type]`` -> ``data_types=[]``. 1 red: this case. The entry is
        written and ingest is asked for nothing, so the dataset exists and stays empty.
    The owner substitutions red this case too, but the case above is what names those.

    WHAT THIS CASE DOES NOT REACH, measured rather than assumed:
    ``data_types=[request_path.data_type]`` -> ``data_types=[DataType.MARKET_ACTIVITY]``, i.e. hard-coding
    the forwarded value, is GREEN across the whole suite. ``REQUEST_BODY`` drives this route with one
    ``data_type`` only, so the literal and the forwarded value are the same string in the only request that
    runs. That gap is left open deliberately: closing it would mean parametrising over ``DataType``, and a
    case asserting that a ``quote`` request forwards ``data_types=[quote]`` would pin behaviour nobody
    designed -- the route accepts all three members with no narrowing and then calls a worker named for
    market activity regardless. Pinning that would freeze an accident and make the eventual narrowing look
    like a regression, so the assertion message below claims only the emptied half.

    ``data_type`` is deliberately not compared: the entry carries it and ``GetDatasetRequest`` takes a
    ``data_types`` LIST instead, which the worker builds from the path. ``expiry`` is compared because
    it is the field whose null-through-the-splat was a 500 on caller-shaped input (tj-uupb4q), so the
    two models agreeing on it is worth having recorded here as well.

    Args:
        monkeypatch: Replaces the worker's upsert collaborator for the duration of the test.
    """
    upsert = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(data_action_request, 'upsert_entry', upsert)

    rpc_client = RecordingRpcClient()
    rpc_clients = MagicMock()
    rpc_clients.get_client = MagicMock(return_value=rpc_client)

    request_path = StoreAssetDatasetPath(
        asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol='AAPL'
    )
    await data_action_request.store_market_activity_worker(
        request_path, StoreAssetDatasetBody(**REQUEST_BODY), MagicMock(), rpc_clients
    )

    created = _only_instance(upsert.await_args, AssetDatasetStoreCreate)
    fetched = rpc_client.requests[0]

    for field in ('owner', 'asset_symbol', 'asset_type', 'source', 'granularity', 'start', 'end', 'expiry'):
        assert getattr(created, field) == getattr(fetched, field), (
            f'the entry being written and the fetch being requested disagree on {field}: '
            f'{getattr(created, field)!r} vs {getattr(fetched, field)!r}'
        )
    assert fetched.data_types == [request_path.data_type], (
        'the fetch asks for a data type the request path did not name -- an emptied data_types list '
        'would leave the entry claiming coverage of bars nobody asked ingest for. A HARD-CODED list is '
        'beyond the reach of this case and is not claimed here: the fixture drives one data_type, so the '
        'literal and the forwarded value are the same string in the only request that runs -- see the '
        'docstring for why widening the fixture is the wrong repair'
    )
