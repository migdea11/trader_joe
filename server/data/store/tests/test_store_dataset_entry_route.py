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

REPOINTED AT THE FetchDataset SEAM (validator, gating tj-3mk3u5.10). Everything above still holds;
what changed underneath it is the collaborator. The route now takes an ``IngestFetchClient`` through
``get_ingest_fetch_client`` instead of ``KafkaRpcFactory.RpcClients`` through ``get_rpc_clients``,
and the worker builds a ``FetchDatasetRequest`` instead of a ``GetDatasetRequest``. The "no ingest
fetch on a refusal" assertions are unchanged in substance -- they read the double's recorded
requests, which is the same evidence under a different method name. ONE PIN IS GENUINELY DEAD AND IS
NOT REPLACED BY A VAGUER ONE: ``expiry`` can no longer be compared across the two models, because
``FetchDatasetRequest`` does not declare it -- retention is the store's business and the fetch never
needed it. ``update_type``, which the new contract DOES carry and which selects ingest's rate-budget
priority, takes its place in the agreement list, and the fetch's ``feed`` gets an assertion of its
own. See the second worker case's docstring for what each of those costs.

THE RETIRED PIN IS BACK (validator, gating tj-hywf7w). 12e1251 hoists the own-overlap check ahead of
the FetchDataset stream, so ``fetch_client.requests == []`` on the 409 path is true again and is
asserted again -- see the first case below for why it died and why its return is the evidence that
the hoist is the right repair rather than a workaround. The two worker cases at the bottom also stop
handing the worker a bare ``AsyncMock()`` session: the worker now issues one real statement of its
own before the stream, which a bare AsyncMock cannot answer, so they take this file's ``FakeSession``
like every other case here. That is a fixture correction and changes nothing either case asserts.
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Select
from sqlalchemy.dialects.postgresql import Insert as PostgresInsert

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app.app_depends import get_ingest_fetch_client
from data.store.app.database.database import async_db
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.ingest import data_action_request
from data.store.app.main import app
from data.store.tests.fetch_double import (
    FetchScript,
    RecordingFetchClient,
    accepted_stream,
    assert_body_served_range,
    served_range_of,
)
from data.store.tests.problem_body import problem, validation_errors
from routers.common.instance_secret import INSTANCE_SECRET_ENV_VAR, INSTANCE_SECRET_HEADER
from schemas.data_ingest.fetch_dataset import FetchDatasetRequest
from schemas.data_store.asset_dataset_store import AssetDatasetStoreCreate, StoreAssetDatasetBody, StoreAssetDatasetPath


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
    """A callable that drives the real POST route against a given fake session and fetch client.

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
        Callable: (session, fetch_client, body) -> httpx.Response.
    """
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)

    def send(session: FakeSession, fetch_client: RecordingFetchClient, body: dict | None = None):
        app.dependency_overrides[async_db] = lambda: session
        app.dependency_overrides[get_ingest_fetch_client] = lambda: fetch_client
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

    NO INSERT, which is the ordering half of the requirement. The check runs before the insert is
    attempted (``check_own_overlap``, which the worker calls before it opens the stream). A conflict
    detected one step late would answer the same 409 having already written the row, and the status
    assertion alone cannot tell those apart. THAT IS ALREADY PINNED ONE LAYER DOWN, and this file does
    not claim it: test_dataset_entry_identity.py's test_the_overlap_check_runs_before_any_insert_is_sent
    asserts exactly the no-INSERT property against the crud function directly, so the insert assertion
    here is a second layer over a covered property.

    "AND NO INGEST FETCH" IS ALIVE AGAIN, AND IT IS THE HEADLINE EVIDENCE FOR tj-hywf7w. The history
    is kept because the pin's value is in what it survived. Under the Kafka path the entry was written
    first and ingest was asked second, so a refused request could be shown never to have reached
    ingest. tj-3mk3u5.10's cutover INVERTED that -- "1. open the stream... 2. on the ACK: upsert the
    entry" -- and this validator retired ``requests == []`` at that gate, correctly, because under that
    order it would have been asserting against the design. What the gate on tj-3mk3u5.10 did NOT see,
    and what the architect priced afterwards, is that the inversion made an own-overlap 409 cost a live
    vendor call and a single-flight slot: ``IngestFetchHandler`` awaits ``reader.get_bars`` BEFORE it
    yields the ack. 12e1251 hoists the check ahead of the stream, and because ``fetch`` is an async
    generator that does no work until its first pull, raising there means INGEST IS NEVER ASKED AT ALL.
    So the original guarantee is restored rather than replaced by a weaker one, and the two assertions
    below say it at both the levels it is observable:
      * ``requests == []`` -- the double records its request before it yields anything, so a non-empty
        list means ``fetch`` was entered, which in production is the vendor call. This is the retired
        assertion, back verbatim.
      * ``journal == []`` -- strictly more than a count, and the half that bounds the WIRE waste. The
        script carries two pages and a done, so a worker that opened the stream and then discovered the
        conflict would leave ``yield FetchAccepted`` here even though it pulled nothing further. That is
        exactly the state this bead exists to remove, and it is what the journal held before the hoist.
    Un-hoisting the check reds both, and reds them with messages naming the cost rather than the order.

    Args:
        post_dataset: Drives the real POST route against a fake session.
        collision_count: How many of the owner's own datasets the request is told it overlaps.
    """
    colliding_ids = [uuid.uuid4() for _ in range(collision_count)]
    session = FakeSession(FakeResult(rows=[(entry_id,) for entry_id in colliding_ids]))
    fetch_client = RecordingFetchClient(FetchScript(events=accepted_stream([[0, 1], [2, 3]])))

    response = post_dataset(session, fetch_client)

    body = problem(response, status=409, reason='OWN_OVERLAP_CONFLICT')

    # THE IDS MOVED UP A LEVEL, AND THE CONTRACT DID NOT (validator, gating tj-3mk3u5.37.8). The
    # hand-written HTTPException used to nest them in a dict under `detail`; the reason table now
    # renders them as a top-level extension member, which is what RFC 9457 extension members are
    # for and is the shape routers/common/errors.py's ProblemDetails declares. tj-vhboky.8's caller
    # contract -- "read the ids, then extend" -- is satisfied by either, so this is a move and not a
    # loss, and the assertion is on the member rather than on where it used to sit.
    assert body['colliding_ids'] == [str(entry_id) for entry_id in colliding_ids], (
        f'the 409 body does not carry the colliding dataset ids under colliding_ids: {body!r}'
    )
    # The three mutations this case was built on still apply to the member in its new place: the
    # whole list stringified into one element reds both parameters, and reporting only the first id
    # reds two-colliding-datasets. See this docstring's cardinality paragraph.

    # `detail` is now the human sentence, not a structure. ITS EXACT RENDERING IS DELIBERATELY NOT
    # PINNED: today it embeds a Python repr of the id list, which the orchestrator has taken to the
    # architect as an open question, and a test asserting that text would bless an accident as the
    # contract and turn the eventual repair into a regression. What IS asserted is the part that
    # holds whichever way that question is answered -- the sentence names every colliding id, so a
    # human reading the message alone can act on it.
    detail = body['detail']
    assert isinstance(detail, str) and detail, f'the 409 carries no human detail: {body!r}'
    unnamed = [str(entry_id) for entry_id in colliding_ids if str(entry_id) not in detail]
    assert unnamed == [], f'the 409 detail does not name the colliding ids {unnamed}: {detail!r}'

    assert _sent_selects(session), 'the overlap probe never ran, so the 409 came from somewhere else'
    assert _sent_inserts(session) == [], 'the entry was inserted despite the overlap being reported as a conflict'
    assert session.commits == 0, 'the conflicting request committed something'
    assert session.rollbacks == 1, 'the conflicting request left its transaction open instead of rolling it back'
    assert fetch_client.requests == [], (
        f'ingest was asked to fetch {len(fetch_client.requests)} dataset(s) for a request the store '
        f'already knew it would refuse. The own-overlap check is one local SELECT and depends on '
        f'nothing the fetch returns, so it must run BEFORE the stream is opened (tj-hywf7w): ingest '
        f'performs the vendor call and takes a rate-budget slot before it yields the ack, so a fetch '
        f'issued here is a live vendor call -- and a held single-flight slot blocking a concurrent '
        f'legitimate fetch for the same key -- spent entirely on a 409'
    )
    assert fetch_client.journal == [], (
        f'the conflicting request pulled {fetch_client.journal} off the stream. NOTHING should have '
        f'been consumed: the script carries two pages and a done behind the ack, so any entry here '
        f'means the stream was opened for a request that was already refusable'
    )


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

    The 200 body is asserted too. ``data_points`` is 0 because the scripted stream serves no bars --
    what a populated one stores belongs to test_bar_batch_chunking.py and
    test_dataset_fetch_transaction.py -- but the key must be there: the route's contract is a count,
    and the worker returning None would still be a 200. A served-but-empty window is a success
    carrying provenance, not a failure (ADR tj-fa1rpu D2), so this is also the case that pins an empty
    fetch answering 200 rather than an error.

    THE BODY GAINED served_range (TE-6 item 7, user ruling 2026-10-02). It is asserted as the WHOLE
    member set, not just the new member: a route that answered the two old keys and dropped the new
    one, and a route that added a fourth nobody declared, both have to red. The value is read off the
    script's own FetchDone rather than written out, because the claim is that it was COPIED. That it
    is not merely an echo of the REQUEST is this file's weakest angle on the property -- the fixture's
    window happens to differ from the request's, but only incidentally -- so the deliberate
    narrower-than-requested case lives in test_dataset_fetch_transaction.py, where it is the subject.
    """
    new_entry_id = uuid.uuid4()
    session = FakeSession(FakeResult(rows=[]), FakeResult(scalar=new_entry_id))
    fetch_client = RecordingFetchClient()

    response = post_dataset(session, fetch_client)

    assert response.status_code == 200, (
        f'a request that overlaps nothing answered {response.status_code}: {response.text}. The own-'
        f'overlap guard is refusing a legitimate create.'
    )
    body = response.json()
    assert set(body) == {'message', 'data_points', 'served_range'}, (
        f'the 200 body members are {sorted(body)}; the declared StoreAssetDatasetResponse is message, '
        f'data_points and served_range'
    )
    assert (body['message'], body['data_points']) == ('Data stored', 0)
    assert_body_served_range(body, served_range_of(fetch_client.script))
    assert len(_sent_inserts(session)) == 1, 'the accepted request did not insert exactly one entry'
    assert session.commits == 1, 'the accepted request was never committed'
    assert len(fetch_client.requests) == 1, 'the accepted request never asked ingest for its data'
    assert fetch_client.journal == ['yield FetchAccepted', 'yield FetchDone'], (
        f'the accepted request consumed {fetch_client.journal}: an empty stream is the ack and the '
        f'done, and a worker that stopped at the ack would commit an entry it never finished fetching'
    )


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
    fetch_client = RecordingFetchClient()

    response = post_dataset(session, fetch_client, REQUEST_BODY | {field: offset_less})

    assert [(error['loc'], error['type']) for error in validation_errors(response)] == [
        (['body', field], 'timezone_aware')
    ], f'the 422 does not name {field} as timezone_aware: {response.text}'
    assert session.statements == [], 'a request refused at the edge still reached the database'
    # STILL A LIVE PIN AFTER tj-3mk3u5.10, unlike the same assertion on the 409 above. The fetch now
    # precedes the entry upsert, so an own-overlap DOES reach ingest -- but a body refused by
    # validation never reaches the handler at all, so nothing may be fetched for it. That distinction
    # is the whole reason the two assertions diverged.
    assert fetch_client.requests == [], 'ingest was asked to fetch data for a request refused at the edge'


# ---------------------------------------------------------------------------------------------
# An empty or inverted range is refused at the edge (half-open ranges, tj-86g751.4)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    'end',
    ['2026-01-01T00:00:00Z', '2026-01-01T05:00:00+05:00', '2025-12-31T23:59:59.999999Z'],
    ids=['end-equals-start', 'end-equals-start-written-at-plus-five', 'end-one-microsecond-before-start'],
)
def test_a_create_whose_end_is_not_after_its_start_answers_422_on_the_body_and_writes_nothing(post_dataset, end: str):
    """tj-vhboky.1 addendum HALF-OPEN RANGES, item 3, at the route: [s, s) is empty and [s, e<s) never meant anything.

    REQUEST_BODY's start is 2026-01-01T00:00:00Z. Each end here is not after it AS AN INSTANT -- the
    +05:00 case reads five hours later on the wall clock and is the same instant. The refusal is
    model-level, so its loc is ['body'] with no field (FastAPI's rendering of loc () on a body
    model), and like the offset-less refusal above it happens before the database or ingest is
    reached. The model-level half, with the open-ended and smallest-non-empty positives, is
    schemas/tests/test_schemas_smoke_data_store.py, test_a_create_whose_end_is_not_after_its_start_is_refused.

    Args:
        post_dataset: Drives the real POST route against a fake session.
        end: An end that is not after the body's start.
    """
    session = FakeSession()
    fetch_client = RecordingFetchClient()

    response = post_dataset(session, fetch_client, REQUEST_BODY | {'end': end})

    errors = validation_errors(response)
    assert [(error['loc'], error['type']) for error in errors] == [(['body'], 'value_error')], (
        f'an empty or inverted range was not refused as one body-level 422: {response.text}'
    )
    assert 'half-open' in errors[0]['msg'], errors[0]['msg']
    assert session.statements == [], 'a request refused at the edge still reached the database'
    assert fetch_client.requests == [], 'ingest was asked to fetch data for a request refused at the edge'


def test_a_create_one_microsecond_long_reaches_the_handler(post_dataset):
    """The positive that keeps the refusal above honest: [s, s + 1us) is the smallest non-empty range, and is written."""
    session = FakeSession(FakeResult(rows=[]), FakeResult(scalar=uuid.uuid4()))
    fetch_client = RecordingFetchClient()

    response = post_dataset(session, fetch_client, REQUEST_BODY | {'end': '2026-01-01T00:00:00.000001Z'})

    assert response.status_code == 200, response.text
    assert len(fetch_client.requests) == 1, 'a valid one-microsecond range never reached ingest'


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

    assert [(error['loc'], error['type']) for error in validation_errors(response)] == [
        (['query', field], 'timezone_aware')
    ], f'the 422 does not name {field} as timezone_aware: {response.text}'
    assert session.statements == [], 'a search refused at the edge still reached the database'


# ---------------------------------------------------------------------------------------------
# Whose principal the write lands under
# ---------------------------------------------------------------------------------------------


def _only_instance(mock_call, model_type):
    """The single argument of ``mock_call`` that is an instance of ``model_type``, positional or keyword.

    Searched rather than indexed so that a later builder switching
    ``upsert_entry_in_transaction(db, entry)`` to a keyword argument -- a legitimate refactor -- does
    not red this test for a reason that has nothing to do with the principal it asserts.

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

    ``upsert_entry_in_transaction`` is replaced at the module level, not the session: this test asserts
    what the worker HANDS its collaborators, so the collaborators are the seam. The scripted stream
    serves no bars, so the bar write path is not reached at all.

    REPOINTED (validator, tj-3mk3u5.10): the second leg is now a ``FetchDatasetRequest`` instead of a
    ``GetDatasetRequest``, and ``owner`` is a declared required field on it too (SensitiveStr, "carried
    so the fetch knows which principal it acts for and may not invent one downstream"), so both the
    property and the mutation it guards survive the contract change unchanged.

    Args:
        monkeypatch: Replaces the worker's upsert collaborator for the duration of the test.
    """
    upsert = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(data_action_request, 'upsert_entry_in_transaction', upsert)

    fetch_client = RecordingFetchClient()

    # A FakeSession rather than a bare AsyncMock: the worker runs the hoisted own-overlap SELECT
    # itself (tj-hywf7w), ahead of the collaborator this case replaces, and an AsyncMock answers it
    # with a coroutine that `_find_own_overlap` cannot iterate. The empty row set is "no overlap".
    await data_action_request.store_market_activity_worker(
        StoreAssetDatasetPath(asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol='AAPL'),
        StoreAssetDatasetBody(**REQUEST_BODY),
        FakeSession(FakeResult(rows=[])),
        fetch_client,
    )

    created = _only_instance(upsert.await_args, AssetDatasetStoreCreate)
    assert created.owner == DECLARED_PRINCIPAL, (
        f'the entry is being written under {created.owner!r} rather than under the principal the caller '
        f'declared ({DECLARED_PRINCIPAL!r}). owner is identity, so this is a different dataset.'
    )

    assert len(fetch_client.requests) == 1, 'the worker did not ask ingest for the dataset exactly once'
    fetched = fetch_client.requests[0]
    assert isinstance(fetched, FetchDatasetRequest), f'ingest was asked with a {type(fetched).__name__}'
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

    IT EARNED ITS PLACE ON TWO MUTATIONS NOTHING ELSE CAUGHT, measured against the suite as it stood:
      * recomputing ``expiry`` on the fetch leg instead of forwarding the body's --
        ``model_dump() | {'expiry': datetime.now(UTC) + timedelta(days=2)}`` -- which is exactly the
        plausible repair, since the body computes that default itself. 1 red: this case. The entry then
        records one expiry and ingest is told another.
      * ``data_types=[request_path.data_type]`` -> ``data_types=[]``. 1 red: this case. The entry is
        written and ingest is asked for nothing, so the dataset exists and stays empty.
    The owner substitutions red this case too, but the case above is what names those.

    ONE OF THOSE TWO IS NOW DEAD, AND IT IS NOT REPLACED BY A SOFTER VERSION OF ITSELF (validator,
    tj-3mk3u5.10). ``FetchDatasetRequest`` does not declare ``expiry`` -- "exactly the fields a reader
    consumes, and nothing more"; retention is the store's business and no reader ever touched it -- so
    the expiry mutation cannot be written any more and the two models cannot disagree about a field one
    of them does not have. Comparing it would be comparing a value against ``AttributeError``.

    WHAT TAKES ITS PLACE IS A FIELD THAT IS LOAD-BEARING ON THE NEW CONTRACT AND WAS NOT ON THE OLD
    ONE. ``update_type`` travels the same splat and selects ingest's RATE BUDGET PRIORITY (STREAM ->
    LIVE, STATIC -> BACKFILL, anything else -> INTERACTIVE), so a substituted value makes a backfill
    compete with live traffic, or the reverse, while the entry records the update type the caller
    actually asked for. The second new assertion is that the fetch names NO feed: an absent feed means
    "the deployment decides", and a worker that started naming one would be steering a tape it is not
    entitled to choose (tj-3mk3u5.22 Q5 -- ingest can only CHECK a named feed, never be steered by it,
    so a wrong guess turns an ordinary fetch into a refusal).

    The ``data_types`` mutation is unchanged: the new contract carries the same list field.

    WHAT THIS CASE DOES NOT REACH, measured rather than assumed:
    ``data_types=[request_path.data_type]`` -> ``data_types=[DataType.MARKET_ACTIVITY]``, i.e. hard-coding
    the forwarded value, is GREEN across the whole suite. ``REQUEST_BODY`` drives this route with one
    ``data_type`` only, so the literal and the forwarded value are the same string in the only request that
    runs. That gap is left open deliberately: closing it would mean parametrising over ``DataType``, and a
    case asserting that a ``quote`` request forwards ``data_types=[quote]`` would pin behaviour nobody
    designed -- the route accepts all three members with no narrowing and then calls a worker named for
    market activity regardless. Pinning that would freeze an accident and make the eventual narrowing look
    like a regression, so the assertion message below claims only the emptied half.

    ``data_type`` is deliberately not compared: the entry carries it and ``FetchDatasetRequest`` takes
    a ``data_types`` LIST instead, which the worker builds from the path.

    Args:
        monkeypatch: Replaces the worker's upsert collaborator for the duration of the test.
    """
    upsert = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(data_action_request, 'upsert_entry_in_transaction', upsert)

    fetch_client = RecordingFetchClient()

    request_path = StoreAssetDatasetPath(
        asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol='AAPL'
    )
    # See the case above for why this is a FakeSession and not a bare AsyncMock (tj-hywf7w).
    await data_action_request.store_market_activity_worker(
        request_path, StoreAssetDatasetBody(**REQUEST_BODY), FakeSession(FakeResult(rows=[])), fetch_client
    )

    created = _only_instance(upsert.await_args, AssetDatasetStoreCreate)
    fetched = fetch_client.requests[0]

    for field in ('owner', 'asset_symbol', 'asset_type', 'source', 'granularity', 'start', 'end', 'update_type'):
        assert getattr(created, field) == getattr(fetched, field), (
            f'the entry being written and the fetch being requested disagree on {field}: '
            f'{getattr(created, field)!r} vs {getattr(fetched, field)!r}'
        )
    assert fetched.feed is None, (
        f'the fetch names feed={fetched.feed!r}. No caller can ask for a tape today, and an absent '
        f'feed is what means "the deployment decides" -- naming one turns a fetch ingest would have '
        f'served into one it can only check and refuse'
    )
    assert fetched.data_types == [request_path.data_type], (
        'the fetch asks for a data type the request path did not name -- an emptied data_types list '
        'would leave the entry claiming coverage of bars nobody asked ingest for. A HARD-CODED list is '
        'beyond the reach of this case and is not claimed here: the fixture drives one data_type, so the '
        'literal and the forwarded value are the same string in the only request that runs -- see the '
        'docstring for why widening the fixture is the wrong repair'
    )


# ---------------------------------------------------------------------------------------------
# What the private SDK reads: GET /store's JSON carries enum NAMES (M0, tj-vhboky.31)
# ---------------------------------------------------------------------------------------------

# Every member of both enums, in the pairs validate_fields accepts (the read model inherits it): each
# expiry type under STATIC, and each non-STATIC update type under ROLLING.
_WIRE_PAIRS: list[tuple[ExpiryType, UpdateType]] = [(member, UpdateType.STATIC) for member in ExpiryType] + [
    (ExpiryType.ROLLING, member) for member in UpdateType if member is not UpdateType.STATIC
]
_WHEN = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ('expiry_type', 'update_type'), _WIRE_PAIRS, ids=[f'{e.name}-{u.name}' for e, u in _WIRE_PAIRS]
)
def test_the_dataset_search_answers_enum_names_and_nulls_on_the_wire(expiry_type: ExpiryType, update_type: UpdateType):
    """GET /store/{asset_type}/{data_type}/{asset_symbol} answers expiry_type and update_type as NAMES.

    Decision tj-vhboky.30 (user approved option A, 2026-09-28): the names are the contract the
    private SDK consumes, and M1 (tj-vhboky.32) must keep them byte-identical while it replaces the
    deprecated ``json_encoders``. The schema-level pins are in
    schemas/tests/test_schemas_smoke_data_store.py; what only this layer shows is that FastAPI's
    response serialisation of ``list[AssetDatasetStore]`` -- which goes through its own TypeAdapter,
    not through ``model_dump_json`` -- honours the same encoding. A serializer that one path used and
    the other did not would pass there and fail here.

    The REAL search_entries runs, against the recording fake session: it is handed one transient
    StoreDatasetEntry row and its bar count, which is the shape ``result.all()`` yields, so the ORM ->
    AssetDatasetStore -> JSON path is the production one end to end. ``end`` and ``expiry`` are None on
    the row, and the answer must carry them as JSON null rather than drop them. No Postgres is
    reached: what a real row round-trips as is the host-verified tier's (tj-vhboky.14), not this one.

    Args:
        expiry_type: The expiry member stored on the row.
        update_type: The update member stored on the row.
    """
    entry_id = uuid.UUID('00000000-0000-0000-0000-00000000000a')
    row = StoreDatasetEntry(
        id=entry_id,
        owner=DECLARED_PRINCIPAL,
        source=DataSource.ALPACA_API,
        asset_symbol='AAPL',
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        granularity=Granularity.ONE_DAY,
        # The RESOLVED tape the row records (tj-3mk3u5.31). Not parameterised with the two enums
        # above: Feed is a plain StrEnum with no name/value split and no serializer of its own, so
        # it has nothing of the encoding question this test is about. What matters here is that it
        # reaches the wire AT ALL -- an AssetDatasetStore that dropped it would answer a dataset
        # without saying which tape covered it, which is the coverage lie tj-f2qz44 is about.
        feed=Feed.SIP,
        start=_WHEN,
        end=None,
        expiry=None,
        expiry_type=expiry_type,
        update_type=update_type,
        created_at=_WHEN,
        updated_at=_WHEN,
    )
    session = FakeSession(FakeResult(rows=[(row, 3)]))
    app.dependency_overrides[async_db] = lambda: session
    try:
        response = TestClient(app).get(app.url_path_for(SEARCH_ROUTE_NAME, **PATH_PARAMS))
    finally:
        # `app` is a module-level singleton other test modules import.
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            'owner': DECLARED_PRINCIPAL,
            'source': 'ALPACA',
            'granularity': '1day',
            'feed': 'SIP',
            'start': '2026-01-01T00:00:00Z',
            'end': None,
            'expiry': None,
            'expiry_type': expiry_type.name,
            'update_type': update_type.name,
            'asset_type': 'stock',
            'data_type': 'market-activity',
            'asset_symbol': 'AAPL',
            'id': str(entry_id),
            'item_count': 3,
            'created_at': '2026-01-01T00:00:00Z',
            'updated_at': '2026-01-01T00:00:00Z',
        }
    ], (
        f'GET /store no longer answers the enum names {expiry_type.name!r}/{update_type.name!r} (or dropped a '
        f'null) -- the wire form the private SDK reads (tj-vhboky.30): {response.text}'
    )


# ---------------------------------------------------------------------------------------------
# M1 (tj-vhboky.32): the OpenAPI documents the enum defaults as names
# ---------------------------------------------------------------------------------------------


def _openapi_schema_of(spec: dict, route_name: str, method: str, *, response: bool) -> dict:
    """The component schema one store route's request body or 200 response resolves to.

    Reached from the route, not by component name, so the assertion is about what that route actually
    documents rather than about a component that might no longer be the one it references.

    Args:
        spec: ``app.openapi()``.
        route_name: The FastAPI route name, as ``app.url_path_for`` takes it.
        method: The HTTP method, lower-case.
        response: True for the 200 response's schema (the list item, for a list), False for the body.

    Returns:
        dict: The resolved component schema.
    """
    # The OpenAPI keys paths by template. Fill each with this file's path parameters (a placeholder
    # it does not know, such as {id}, fills empty and so cannot match) and keep the one that equals
    # the URL the app resolves for the route.
    url = app.url_path_for(route_name, **PATH_PARAMS)
    fill = defaultdict(str, PATH_PARAMS)
    (path,) = [template for template in spec['paths'] if template.format_map(fill) == url]
    operation = spec['paths'][path][method]
    if response:
        schema = operation['responses']['200']['content']['application/json']['schema']
        schema = schema.get('items', schema)
    else:
        schema = operation['requestBody']['content']['application/json']['schema']
    ref = schema['$ref']
    assert ref.startswith('#/components/schemas/'), ref
    return spec['components']['schemas'][ref.rsplit('/', 1)[1]]


@pytest.mark.parametrize(
    ('route_name', 'method', 'response'),
    [(ROUTE_NAME, 'post', False), (SEARCH_ROUTE_NAME, 'get', True)],
    ids=['POST-store-body', 'GET-store-response'],
)
def test_the_store_openapi_documents_the_enum_defaults_as_names(route_name: str, method: str, response: bool):
    """The store's OpenAPI gives expiry_type and update_type the defaults 'BULK' and 'STATIC'.

    The OpenAPI is what the private SDK's typed client is generated from, and the wire carries names
    (tj-vhboky.30), so the documented default must be the name too. c535d98's field serializer does not
    reach the schema default -- Pydantic encodes it through config ``json_encoders`` only -- and without
    the ``json_schema_extra`` default on StoreAssetDatasetBody this document says 1 for both, with every
    wire pin still green. The schema-level half is in schemas/tests/test_schemas_smoke_data_store.py;
    this is the document FastAPI actually serves, for the POST body and for the search's response items.

    Args:
        route_name: The store route whose schema is read.
        method: Its HTTP method.
        response: Read the 200 response's item schema rather than the request body.
    """
    properties = _openapi_schema_of(app.openapi(), route_name, method, response=response)['properties']

    defaults = (properties['expiry_type'].get('default'), properties['update_type'].get('default'))

    assert defaults == ('BULK', 'STATIC'), (
        f'{method.upper()} {route_name} documents defaults {defaults!r}, not the enum names the wire carries'
    )


# ---------------------------------------------------------------------------------------------
# M5 (tj-vhboky.40): the OpenAPI documents the enum TYPE as a string enum of names
# ---------------------------------------------------------------------------------------------
#
# User ruling A on tj-vhboky.38. The schema-level half is in schemas/tests/test_schemas_smoke_data_store.py.
# The unreferenced integer ExpiryType/UpdateType components that app.openapi() once listed (the
# builder's finding on tj-vhboky.40) are pruned since tj-1b3aer; their absence is pinned in
# data/store/tests/test_openapi_pruning.py, not here. These tests still read only what each route
# REFERENCES, so they neither rely on nor forbid those components.
_ENUM_FIELDS = [('expiry_type', ExpiryType), ('update_type', UpdateType)]


def _search_query_parameter(spec: dict, name: str) -> dict:
    """The schema of one query parameter of GET /store, reached from the route.

    Args:
        spec: ``app.openapi()``.
        name: The query parameter's name.

    Returns:
        dict: That parameter's ``schema``; exactly one parameter of that name must be in the query.
    """
    url = app.url_path_for(SEARCH_ROUTE_NAME, **PATH_PARAMS)
    fill = defaultdict(str, PATH_PARAMS)
    (path,) = [template for template in spec['paths'] if template.format_map(fill) == url]
    (parameter,) = [
        parameter
        for parameter in spec['paths'][path]['get'].get('parameters', [])
        if parameter['name'] == name and parameter['in'] == 'query'
    ]
    return parameter['schema']


@pytest.mark.parametrize(('field', 'enum'), _ENUM_FIELDS, ids=[field for field, _ in _ENUM_FIELDS])
@pytest.mark.parametrize(
    ('route_name', 'method', 'response'),
    [(ROUTE_NAME, 'post', False), (SEARCH_ROUTE_NAME, 'get', True)],
    ids=['POST-store-body', 'GET-store-response'],
)
def test_the_store_openapi_documents_the_enum_body_fields_as_names(
    route_name: str, method: str, response: bool, field: str, enum: type
):
    """The POST body and the GET response items document the field as {type: string, enum: names}.

    Args:
        route_name: The store route whose schema is read.
        method: Its HTTP method.
        response: Read the 200 response's item schema rather than the request body.
        field: The enum field.
        enum: The enum class the field holds.
    """
    documented = _openapi_schema_of(app.openapi(), route_name, method, response=response)['properties'][field]

    assert '$ref' not in documented, f'{method.upper()} {route_name} {field} points at a component: {documented!r}'
    assert documented.get('type') == 'string', f'{method.upper()} {route_name} {field} documents {documented!r}'
    assert documented.get('enum') == [member.name for member in enum], (
        f'{method.upper()} {route_name} {field} documents enum {documented.get("enum")!r}, not the member names'
    )


@pytest.mark.parametrize(('field', 'enum'), _ENUM_FIELDS, ids=[field for field, _ in _ENUM_FIELDS])
def test_the_store_openapi_documents_the_search_filters_as_nullable_names(field: str, enum: type):
    """GET /store's enum query parameters are anyOf [{type: string, enum: names}, {type: null}].

    The filters are optional, so the null branch is expected; the one other branch must be the
    string enum of names, not an integer enum or a component reference.

    Args:
        field: The enum query parameter.
        enum: The enum class the parameter holds.
    """
    schema = _search_query_parameter(app.openapi(), field)

    assert {'type': 'null'} in schema.get('anyOf', []), f'GET /store ?{field} is not nullable: {schema!r}'
    branches = [branch for branch in schema['anyOf'] if branch != {'type': 'null'}]
    assert branches == [{'type': 'string', 'enum': [member.name for member in enum]}], (
        f'GET /store ?{field} documents {branches!r}, not a string enum of member names'
    )
