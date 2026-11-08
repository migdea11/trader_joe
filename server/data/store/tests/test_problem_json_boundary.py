"""data_store's HTTP boundary end to end: what a caller actually receives for every way a fetch fails.

WHY THIS FILE EXISTS (validator, gating tj-3mk3u5.37.8 / TE-6, closing tj-fe19tu). The builder
proved the two properties that matter most with throwaway scripts and then deleted them, as its
bead told it to. They are the two this file makes permanent, because neither is reachable from any
test that stops below the edge:

  1. THE D8 NO-LEAK, ON THE WIRE. write_transaction's unit tests pin that the ExogenousError it
     raises carries a fixed sentence. They cannot pin what the RENDERER then does with it -- a
     handler that reached for ``str(exc.__cause__)`` to be helpful, or an exception middleware that
     echoed the chain, would leave every one of those tests green and publish the statement and its
     bound parameters to whoever made the request. THIS REPOSITORY HAS ALREADY SHIPPED EXACTLY THAT
     DEFECT ONCE: a canary secret reached the wire through an error boundary, and it was a test that
     caught it, not a review. So the canary is planted in the statement and in a bound parameter and
     searched for in the whole response -- body and headers -- for every classification case.
  2. A FAILED FETCH IS DISTINGUISHABLE FROM AN EMPTY ONE. That sentence is tj-fe19tu's entire
     acceptance criterion. Before TE-6 a vendor failure and a genuinely empty window were both
     "something went wrong or maybe nothing did": one surfaced as a 500, the other as a 200 whose
     body said nothing about what had been fetched. Now the first is a typed problem+json whose
     reason names the condition, and the second is a 200 carrying the window the vendor answered
     for. The pair of cases below is what makes that a tested claim rather than a design intention.

THE FETCH DOUBLE IS AN IMPLEMENTATION OF THE INTERFACE, not a patched stub, which the bead requires
in those words. data/store/tests/fetch_double.py's RecordingFetchClient is an ordinary
IngestFetchClient (ADR tj-8konfu D3), scripted per case; nothing here monkeypatches a method onto
production code, so a change to the seam's shape reds these cases instead of sliding past them.

REPAIRED FOR THE PUSH PATH (validator, tj-7zjov1.28, P35). The POST now writes a request row, SUBMITS it and waits
while ingest pushes the result to the delivery intake. The canary half (section 1) is unchanged: the database fails
on the very first statement, the overlap SELECT, so nothing past the request's session is reached and the fake
session stays. Sections 2 to 4 need a request that is written, submitted and finished, so they run the whole chain
over post_path.py's SQLite world: the real route, submit path, waiter and delivery intake, with a Submit double that
answers and delivers. A failure can now come from the Submit call or from the request's delivered outcome, and both
tables are driven. served_range is the finishing attempt row's, so the anti-echo fixture is now the DELIVERED window.

WHAT TIER THIS IS. The real app with real handlers installed, over fake sessions (section 1) or SQLite (the rest)
and a scripted seam. Every status here is one FastAPI assembled in-process; no Postgres and no
ingest are reached. That a real deployment returns the same codes is the System Testing job's
(TE-7), and nothing below should be read as having shown it.
"""

import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError, ProgrammingError

from common.enums.data_stock import Feed
from common.errors.vocabulary import ERROR_DOMAIN, REASONS, ExogenousError, Reason
from data.store.app.app_depends import get_ingest_submit_client
from data.store.app.database.database import async_db
from data.store.app.database.models.ingest_request import IngestRequestState
from data.store.app.ingest import data_action_request
from data.store.app.main import app
from data.store.tests.fetch_double import bar
from data.store.tests.post_path import Delivery, PostStore, SubmitDouble, post
from data.store.tests.post_path import post_store as _post_store  # noqa: F401 (a fixture)
from data.store.tests.problem_body import PROBLEM_MEDIA_TYPE, problem
from data.store.tests.test_ui_datasets_route import aware_sqlite_datetimes  # noqa: F401 (a fixture)
from routers.common.errors import ProblemDetails
from routers.common.instance_secret import INSTANCE_SECRET_ENV_VAR, INSTANCE_SECRET_HEADER
from schemas.data_ingest import fetch_dataset
from schemas.data_ingest.delivery import DeliveryErrorClass, DeliveryOutcome


pytestmark = pytest.mark.data_store

CONFIGURED_SECRET = 'boundary-instance-secret'
ROUTE_NAME = 'store_data'
PATH_PARAMS = {'asset_type': 'stock', 'data_type': 'market-activity', 'asset_symbol': 'AAPL'}

# The window the CALLER asks for. Its end is the value every served_range assertion below must not
# be allowed to match by accident -- see SERVED_* for why.
REQUESTED_START = datetime(2026, 1, 2, tzinfo=UTC)
REQUESTED_END = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
REQUEST_BODY = {
    'owner': 'strategy-that-asked',
    'source': 'ALPACA',
    'granularity': '1day',
    'start': REQUESTED_START.isoformat().replace('+00:00', 'Z'),
    'end': REQUESTED_END.isoformat().replace('+00:00', 'Z'),
}

# THE SERVED WINDOW IS DELIBERATELY NARROWER THAN THE REQUESTED ONE, and this is the single most
# load-bearing fixture decision in the file. ingest clamps the end to as_of (end = min(requested
# end, as_of)), so in production the served window routinely stops short of the asked one -- and a
# store that ECHOED THE REQUEST back as served_range would be indistinguishable from one that
# copied FetchDone's, for every fixture where the two coincide. Three minutes against twelve hours
# makes the echo impossible to miss. An equal-window fixture would pass against the bug and prove
# nothing, which is the same trap as a non-lazy stream double on the paging tests.
SERVED_START = REQUESTED_START
SERVED_END = REQUESTED_START + timedelta(minutes=3)
AS_OF = SERVED_END


def served_done(bar_count: int) -> fetch_dataset.FetchDone:
    """A FetchDone whose served window stops well short of the requested one.

    Args:
        bar_count: How many bars the stream served.

    Returns:
        fetch_dataset.FetchDone: The terminator.
    """
    return fetch_dataset.FetchDone(
        bar_count=bar_count, served_range=fetch_dataset.ServedRange(start=SERVED_START, end=SERVED_END), as_of=AS_OF
    )


class FakeResult:
    """One canned answer, with the three accessors the create path reads."""

    def __init__(self, *, rows: list[tuple] | None = None, scalar: uuid.UUID | None = None, stored: bool | None = None):
        self._rows = rows if rows is not None else []
        self._scalar = scalar
        self._stored = stored

    def all(self) -> list[tuple]:
        return self._rows

    def scalar_one_or_none(self) -> bool | None:
        """The stored collect the pre-upsert lock reads (tj-grna9p.189); None is no row yet."""
        return self._stored

    def scalar_one(self) -> uuid.UUID:
        assert self._scalar is not None, 'the create path read an id back from a result that was not given one'
        return self._scalar


class FakeSession:
    """An async session that answers canned results, or raises a given error on its first statement."""

    def __init__(self, *results: FakeResult, raises: BaseException | None = None):
        self._results = list(results)
        self.raises = raises
        self.statements: list = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement):
        self.statements.append(statement)
        if self.raises is not None:
            raise self.raises
        assert self._results, 'the create path issued more statements than the fixture planned for'
        return self._results.pop(0)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        return None


def _accepting_session(bar_statements: int = 0) -> FakeSession:
    """A session that finds no overlap, returns a fresh entry id, and answers each bar write.

    Four kinds of statement reach it on a served fetch, in this order: the hoisted own-overlap
    SELECT, the stored-collect SELECT ... FOR UPDATE on the ack (tj-grna9p.189; no row, a create),
    the entry upsert (whose RETURNING id is read back), and one INSERT per chunk of bars. Only the
    first three have results anyone reads, but the fake insists on having one per statement so that
    a path issuing an unplanned statement fails loudly rather than silently.

    Args:
        bar_statements: How many bar-write statements the scripted pages will produce.

    Returns:
        FakeSession: The session.
    """
    answers = [FakeResult(rows=[]), FakeResult(stored=None), FakeResult(scalar=uuid.uuid4())]
    answers.extend(FakeResult() for _ in range(bar_statements))
    return FakeSession(*answers)


class NeverSubmitted:
    """The Submit seam for the canary cases: the database fails on the overlap SELECT, so nothing may be sent."""

    async def submit(self, request):
        raise AssertionError('a request whose overlap check failed was submitted to ingest')

    async def instance(self):
        raise AssertionError('the POST never probes the instance')


@pytest.fixture
def store(_post_store: PostStore) -> PostStore:  # noqa: F811 (the imported fixture, under its own name here)
    """post_path.py's SQLite world, for the cases that run the whole push path."""
    return _post_store


@pytest.fixture
def post_dataset(monkeypatch: pytest.MonkeyPatch):
    """A callable driving the real POST /store against a given session and fetch client.

    ``raise_server_exceptions=False`` so that the bug path -- an unclassified SQLAlchemyError, which
    TE-6 deliberately leaves unconverted -- arrives as the 500 RESPONSE a caller would receive
    rather than as an exception re-raised into the test. That response is the subject of the canary
    case for the two bug rows: what matters is what the caller is handed, and for a bug that must be
    an error_id and nothing else.

    Args:
        monkeypatch: Sets INSTANCE_WRITE_SECRET for the duration of one test.

    Yields:
        Callable: (session, client) -> httpx.Response.
    """
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)

    def send(session: FakeSession, client: 'NeverSubmitted', body: dict | None = None):
        app.dependency_overrides[async_db] = lambda: session
        app.dependency_overrides[get_ingest_submit_client] = lambda: client
        try:
            return TestClient(app, raise_server_exceptions=False).post(
                app.url_path_for(ROUTE_NAME, **PATH_PARAMS),
                json=REQUEST_BODY if body is None else body,
                headers={INSTANCE_SECRET_HEADER: CONFIGURED_SECRET},
            )
        finally:
            # `app` is a module-level singleton other test modules import.
            app.dependency_overrides.clear()

    yield send


# ---------------------------------------------------------------------------------------------
# 1. THE CANARY: no statement and no bound parameter reaches the wire, on any classification path
# ---------------------------------------------------------------------------------------------

# Distinctive enough that finding either in a response can only mean the error's statement or its
# parameters were rendered there. The parameter stands in for an owner, which is the value D8 names
# and which this service really does bind into the statements this path sends.
CANARY_SQL = 'SELECT store_dataset_entry -- canary-stmt-6e0c2b'
CANARY_OWNER = 'canary-owner-a71d35'


def _canary(error_class, sqlstate: str | None = None):
    """A database error whose statement and bound parameters both carry the canary."""

    class _Orig(Exception):
        def __init__(self):
            super().__init__('the driver failed')
            if sqlstate is not None:
                self.sqlstate = sqlstate

    return error_class(CANARY_SQL, {'owner': CANARY_OWNER}, _Orig())


_WIRE_CASES: list[tuple[str, object, int, str | None]] = [
    ('OperationalError', _canary(OperationalError), 503, 'DATABASE_UNAVAILABLE'),
    ('InterfaceError', _canary(InterfaceError), 503, 'DATABASE_UNAVAILABLE'),
    # DATABASE_INTEGRITY is 409 and DATABASE_CONFLICT is 503, which reads backwards until you see
    # why: a constraint violation is something about the REQUEST that will keep failing, so the
    # caller must change it; a lost race is retryable as it stands, so the caller is told to wait.
    # The statuses are read from the reason table in common/errors/vocabulary.py, not chosen here.
    ('IntegrityError', _canary(IntegrityError), 409, 'DATABASE_INTEGRITY'),
    ('serialization failure 40001', _canary(OperationalError, '40001'), 503, 'DATABASE_CONFLICT'),
    ('deadlock detected 40P01', _canary(OperationalError, '40P01'), 503, 'DATABASE_CONFLICT'),
    # The two BUG rows. Re-raised unchanged by write_transaction, so the object that reaches the
    # edge still carries the SQL in its own str() -- which makes these the cases most able to leak,
    # and therefore the ones most worth driving. The 500 handler must publish an error_id and
    # NOTHING else.
    ('ProgrammingError (a bug)', _canary(ProgrammingError), 500, None),
    ('DataError-shaped bug', _canary(IntegrityError.__mro__[1]), 500, None),
]


@pytest.mark.parametrize(
    ('error', 'status', 'reason'), [(e, s, r) for _, e, s, r in _WIRE_CASES], ids=[n for n, _, _, _ in _WIRE_CASES]
)
def test_no_statement_or_bound_parameter_ever_reaches_the_response(post_dataset, error, status: int, reason):
    """D8, measured over the whole answer rather than over the detail string.

    THE SEARCH IS THE WHOLE RESPONSE -- body text and headers alike -- and not the parsed detail.
    A leak that mattered would not announce itself by appearing in the field someone thought to
    check: it would arrive in an extension member a renderer copied from metadata, in a header some
    middleware added, or in a 500 body that fell back to str(exc). Searching response.text and the
    headers covers all three, and costs nothing over checking one field.

    THE ASSERTION PRINTS NO VALUE. A failure here means something secret is in the response, and a
    pytest failure message is itself a place that value would then be written. The check is a
    boolean and the message names the surface, never the content.

    Args:
        post_dataset: Drives the real POST route.
        error: A database error carrying the canary in its statement and parameters.
        status: The status its reason renders as -- or 500 for a bug.
        reason: The reason the body must name, or None for the bug rows, which carry none.
    """
    session = FakeSession(raises=error)

    response = post_dataset(session, NeverSubmitted())

    body = problem(response, status=status, reason=reason)
    assert CANARY_SQL not in response.text, 'the STATEMENT reached the response body (value withheld)'
    assert CANARY_OWNER not in response.text, 'a BOUND PARAMETER reached the response body (value withheld)'
    assert CANARY_SQL not in str(response.headers), 'the STATEMENT reached a response header (value withheld)'
    assert CANARY_OWNER not in str(response.headers), 'a BOUND PARAMETER reached a header (value withheld)'
    # Positive control: the body is not empty of everything, so the four checks above are not
    # passing because nothing was rendered at all.
    assert body['error_id'], f'the answer carries no error_id, so the checks above may be vacuous: {body}'


def test_a_bug_publishes_an_error_id_and_nothing_else(post_dataset):
    """The 500 body carries an id and no description of what broke (D5, D8).

    THE COMPLEMENT OF THE CANARY, and the reason the canary alone is not enough: a 500 that said
    "ProgrammingError: column store_dataset_entry.nonexistent does not exist" leaks our schema and
    our defect without containing either canary string, so the search above would pass it. Asserting
    what the body MAY contain, rather than what it may not, is what closes that.

    The members are compared as a set against the ones ProblemDetails always emits for a reason-less
    failure. `reason` and `domain` are deliberately absent: a bug is not one of ours in the
    vocabulary's sense, and a client branching on reason must not be handed one here.
    """
    session = FakeSession(raises=ProgrammingError('SELECT ...', {}, Exception('column does not exist')))

    response = post_dataset(session, NeverSubmitted())

    body = problem(response, status=500)
    assert set(body) == {'type', 'title', 'status', 'error_id'}, (
        f'the 500 body carries {sorted(body)}. A bug publishes an error_id and nothing describing '
        f'what broke: the traceback belongs in the log, under that id, and nowhere else (D5, D8).'
    )
    assert 'reason' not in body, 'a bug was given a reason, so a client would branch on our own defect'
    assert 'column does not exist' not in response.text, "the driver's message reached the caller"


def test_the_body_error_id_is_the_one_write_transaction_logged(post_dataset, caplog: pytest.LogCaptureFixture):
    """D8's correlation id: the id the caller quotes is the id the operator greps for.

    THE WHOLE POINT OF AN error_id IS THE JOIN, and an id that appears in the body but names nothing
    in the log is worse than none: it reads like a handle and is a dead end. Two independent
    new_error_id() calls -- one in write_transaction, one at the edge -- produce a body and a log
    line that each look perfectly correct and cannot be connected, and no assertion on either alone
    would notice.

    THE CHAIN IS LOGGED EXACTLY ONCE, which is the other half. write_transaction logs it with
    exc_info; the edge, finding the error already carries an id, renders without logging the chain
    again (routers/common/errors.py). A second ERROR record carrying a traceback would mean the SQL
    and its parameters are written to the log twice, which is not a leak but is the noise D8's
    addendum set out to remove.
    """
    caplog.set_level(logging.DEBUG)
    session = FakeSession(raises=_canary(OperationalError))

    response = post_dataset(session, NeverSubmitted())

    body = problem(response, status=503, reason='DATABASE_UNAVAILABLE')
    transaction_records = [record for record in caplog.records if record.name == 'data.store.app.database.transaction']
    (logged,) = transaction_records
    assert f'error_id {body["error_id"]}' in logged.getMessage(), (
        f'the body names error_id {body["error_id"]} and write_transaction logged '
        f'{logged.getMessage()!r}. An id a caller quotes that matches no log line is a dead end.'
    )
    with_traceback = [record for record in caplog.records if record.exc_info is not None]
    assert len(with_traceback) == 1, (
        f'the cause chain was logged {len(with_traceback)} times. write_transaction logs it once and '
        f"the edge renders without logging it again, because the error already carries the edge's id."
    )


# ---------------------------------------------------------------------------------------------
# 2. THE OUTCOMES (TE-6 item 6, tj-fe19tu): a failure, by reason, at the edge
#
# REPAIRED FOR THE PUSH PATH (validator, tj-7zjov1.28). A POST can now fail in two places, and both are driven:
#   - THE SUBMIT CALL: the seam raises a typed error (the peer is not there, a deadline, a broken contract, a rate
#     budget). The request row was written first, by design, so "writes nothing" became "writes nothing PAST the
#     request row": it stays PENDING for the instance watcher, with no entry and no attempt row.
#   - THE REQUEST'S OUTCOME, delivered later by ingest and read by the waiter: a refused Submit (a tape ingest cannot
#     serve), a vendor refusal for good, attempts exhausted, a request that expired on its ready_by, and a request
#     still running at the POST's deadline. Each renders its reason's status; none writes an entry.
# ---------------------------------------------------------------------------------------------

_RESET_AT = datetime(2026, 1, 2, 13, 0, tzinfo=UTC)

_CALL_FAILURES: list[tuple[str, Reason, int]] = [
    ('the store rate budget', Reason.RATE_BUDGET, 429),
    ('a rate limit on the call', Reason.VENDOR_RATE_LIMITED, 429),
    ('the vendor is down', Reason.VENDOR_UNAVAILABLE, 503),
    ('the vendor refused our credentials', Reason.VENDOR_AUTH, 503),
    ('the peer is not there', Reason.PEER_UNAVAILABLE, 503),
    ('the deadline passed', Reason.DEADLINE, 504),
    ('the peer answered INTERNAL', Reason.PEER_INTERNAL, 502),
    ('the peer broke the contract', Reason.PEER_PROTOCOL_ERROR, 502),
]


def _failure_for(reason: Reason) -> ExogenousError:
    """The error the seam raises for one reason, with reset_at where the row requires it."""
    requires_reset_at = reason in {Reason.RATE_BUDGET, Reason.VENDOR_RATE_LIMITED}
    detail = f'the call failed with {reason.value}'
    return ExogenousError(reason, detail, reset_at=_RESET_AT if requires_reset_at else None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('reason', 'status'), [(r, s) for _, r, s in _CALL_FAILURES], ids=[n for n, _, _ in _CALL_FAILURES]
)
async def test_a_failed_submit_renders_as_its_reasons_status_and_writes_nothing_past_the_request(
    store: PostStore, monkeypatch, reason: Reason, status: int
):
    """TE-6 item 6's table, driven at the edge, one case per row the Submit call can raise.

    EVERY ROW, because the rows are the contract a caller programs against: 429 from 503 from 504 tell a caller
    whether to back off on a schedule, fail over, or lengthen its deadline.

    AND NOTHING PAST THE REQUEST ROW: no entry claiming coverage of bars nobody has, no attempt row; the request is
    left PENDING so the watcher re-sends it.
    """
    response = await post(store, SubmitDouble(store, raises=_failure_for(reason)), REQUEST_BODY, monkeypatch)

    body = problem(response, status=status, reason=reason.value)
    assert 'served_range' not in body, (
        f'a FAILED request carries served_range: {body}. Only a 200 does -- a failure answered no window.'
    )
    assert store.created == [] and await store.runs() == []
    (request,) = await store.requests()
    assert IngestRequestState(request.state) is IngestRequestState.PENDING


_REFUSED_FEED = fetch_dataset.FetchRefused(
    reason=Reason.FEED_NOT_AVAILABLE.value, domain=ERROR_DOMAIN, detail='not this tape', metadata={}
)

_OUTCOMES: list[tuple[str, dict[str, Any], int, str]] = [
    ('a refused Submit', {'refused': _REFUSED_FEED}, 422, 'FEED_NOT_AVAILABLE'),
    (
        'the vendor refused for good',
        {'delivery': Delivery(DeliveryOutcome.FAILED, error_class=DeliveryErrorClass.PERMANENT, detail='no such')},
        422,
        'VENDOR_REJECTED',
    ),
    (
        'the last attempt failed',
        {'delivery': Delivery(DeliveryOutcome.FAILED, last_attempt=True, detail='the vendor answered 503')},
        503,
        'VENDOR_UNAVAILABLE',
    ),
    ('attempts exhausted', {'delivery': Delivery(DeliveryOutcome.EXHAUSTED)}, 503, 'VENDOR_UNAVAILABLE'),
    ('expired on its ready_by', {'delivery': Delivery(DeliveryOutcome.EXPIRED)}, 504, 'DEADLINE'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('script', 'status', 'reason'), [(c, s, r) for _, c, s, r in _OUTCOMES], ids=[n for n, _, _, _ in _OUTCOMES]
)
async def test_a_failed_request_renders_as_its_reasons_status_and_writes_no_entry(
    store: PostStore, monkeypatch, script: dict[str, Any], status: int, reason: str
):
    """The request's own outcome, read by the waiter from the request row, at the edge; the request is FAILED."""
    response = await post(store, SubmitDouble(store, **script), REQUEST_BODY, monkeypatch)

    body = problem(response, status=status, reason=reason)
    assert 'served_range' not in body
    assert store.created == [], 'a failed request wrote an entry'
    (request,) = await store.requests()
    assert IngestRequestState(request.state) is IngestRequestState.FAILED


@pytest.mark.asyncio
async def test_a_request_still_running_at_the_deadline_answers_504_and_keeps_running(store: PostStore, monkeypatch):
    """Owner ruling R4 (a): the POST's wait is bounded; the request is not abandoned, its outcome lands in the history."""
    monkeypatch.setenv(data_action_request.VERDICT_DEADLINE_ENV, '0.05')

    response = await post(store, SubmitDouble.silent(store), REQUEST_BODY, monkeypatch)

    problem(response, status=504, reason='DEADLINE')
    (request,) = await store.requests()
    assert IngestRequestState(request.state) is IngestRequestState.QUEUED


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', [Reason.RATE_BUDGET, Reason.VENDOR_RATE_LIMITED])
async def test_a_rate_limited_submit_carries_retry_after_and_reset_at(store: PostStore, monkeypatch, reason: Reason):
    """The 429s say WHEN, in a header and in the body, and the two must agree.

    TWO SPELLINGS OF ONE FACT, which is exactly the shape that drifts: an HTTP client obeys the Retry-After header and
    a typed SDK reads reset_at, so a renderer that derived them from two readings of the clock would send the two
    clients to different times. The header is delta-seconds and reset_at an instant: compared by reconstruction.
    """
    response = await post(store, SubmitDouble(store, raises=_failure_for(reason)), REQUEST_BODY, monkeypatch)

    body = problem(response, status=429, reason=reason.value)
    assert 'Retry-After' in response.headers, f'a 429 carries no Retry-After header: {dict(response.headers)}'
    assert datetime.fromisoformat(body['reset_at']) == _RESET_AT, (
        f'the body says the window resets at {body["reset_at"]}, not at {_RESET_AT.isoformat()}'
    )
    header_seconds = int(response.headers['Retry-After'])
    assert header_seconds == body['retry_after'], (
        f'Retry-After says {header_seconds}s and the body says {body["retry_after"]}s. A client '
        f'obeying the header and one reading the member would wait different lengths of time.'
    )


# ---------------------------------------------------------------------------------------------
# 3. served_range (TE-6 item 7): copied from the finishing attempt, never echoed back from the request
# ---------------------------------------------------------------------------------------------


def _served(bar_minutes: list[int]) -> Delivery:
    """A delivered success whose served window stops well short of the requested one."""
    return Delivery(
        bars=[bar(minute, feed=Feed.IEX) for minute in bar_minutes],
        served=fetch_dataset.ServedRange(start=SERVED_START, end=SERVED_END),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('bar_minutes', 'expected_points'),
    [pytest.param([0, 1], 2, id='a-served-window-with-bars'), pytest.param([], 0, id='a-served-but-empty-window')],
)
async def test_the_200_carries_the_window_the_vendor_answered_for_not_the_one_asked(
    store: PostStore, monkeypatch, bar_minutes: list[int], expected_points: int
):
    """THE ECHO IS THE BUG THIS GUARDS, and the fixture is what makes it detectable.

    served_range is the FINISHING ATTEMPT ROW's (served_start, served_end), as the delivery reported it -- never
    rebuilt from the request and never clamped a second time at the store (TE-6 item 7; tj-7zjov1.28 ruling B).
    SERVED_END is three minutes after the start while the request asks for twelve hours, so an echo cannot
    coincide. The empty window is the operationally important one: a misspelled symbol is SERVED, for the window
    asked and with no bars, and only the range tells a caller that from a window the vendor had nothing for.
    """
    response = await post(store, SubmitDouble(store, _served(bar_minutes)), REQUEST_BODY, monkeypatch)

    assert response.status_code == 200, f'a served request answered {response.status_code}: {response.text}'
    body = response.json()
    assert body['data_points'] == expected_points, f'the body reports {body["data_points"]} bars'
    served = body['served_range']
    assert (datetime.fromisoformat(served['start']), datetime.fromisoformat(served['end'])) == (
        SERVED_START,
        SERVED_END,
    ), (
        f'the body served_range is {served}, not the window the vendor answered for '
        f'([{SERVED_START.isoformat()}, {SERVED_END.isoformat()})).'
    )
    assert datetime.fromisoformat(served['end']) != REQUESTED_END, (
        f'the body served_range ends exactly where the REQUEST ended ({REQUESTED_END.isoformat()}), '
        f'so the store is echoing what was asked for instead of copying what the delivery answered.'
    )
    assert 'as_of' not in body, 'as_of is not exposed at the HTTP edge: the ruling names served_range only'


@pytest.mark.asyncio
async def test_an_empty_window_is_a_success_and_a_vendor_failure_is_not(store: PostStore, monkeypatch):
    """tj-fe19tu's acceptance criterion in one case: the two outcomes are distinguishable at the edge."""
    served_response = await post(store, SubmitDouble(store, _served([])), REQUEST_BODY, monkeypatch)
    failed = Delivery(DeliveryOutcome.FAILED, last_attempt=True, detail='the vendor answered 503')
    failed_response = await post(store, SubmitDouble(store, failed), REQUEST_BODY, monkeypatch)

    assert served_response.status_code == 200, (
        f'a window the vendor SERVED with no bars answered {served_response.status_code}. An empty '
        f'result is a success carrying provenance, not a failure (ADR tj-fa1rpu D2, Q-EMPTY).'
    )
    assert served_response.json()['data_points'] == 0
    failed_body = problem(failed_response, status=503, reason='VENDOR_UNAVAILABLE')
    assert 'served_range' in served_response.json() and 'served_range' not in failed_body


# ---------------------------------------------------------------------------------------------
# 4. WHAT A SERVED REQUEST LEAVES BEHIND (Q-EMPTY, tj-3mk3u5.37.1): the attempt row, and no owner in any log line
# ---------------------------------------------------------------------------------------------
# The "Fetch served" INFO line (served range, as_of, feed, count) is RETIRED by the architect at the P35 gate
# (tj-7zjov1.28, gate note of 2026-10-26, item 4): owner rulings 4 and 4b of tj-3mk3u5.37.1 never required a log, and
# the push path keeps all four values durably on the attempt row. That row is what the next test pins.


@pytest.mark.asyncio
async def test_a_served_post_keeps_the_range_as_of_feed_and_count_on_its_attempt_row(store: PostStore, monkeypatch):
    """The four values the retired INFO line carried, read from the one attempt row a served POST leaves.

    served_start and served_end are the trailer's served range (narrower than the request, so an echo is visible),
    finished_at is the trailer's as_of, feed is the header's resolved tape and bars_written the bars stored.
    """
    sent: list[Any] = []
    real_deliver = store.intake.deliver

    async def deliver(attempt: Any) -> Any:
        sent.append(attempt)
        return await real_deliver(attempt)

    monkeypatch.setattr(store.intake, 'deliver', deliver)

    response = await post(store, SubmitDouble(store, _served([0])), REQUEST_BODY, monkeypatch)

    assert response.status_code == 200, response.text
    (attempt,) = sent
    (run,) = await store.runs()
    assert (run.served_start, run.served_end) == (SERVED_START, SERVED_END)
    assert run.served_end != REQUESTED_END, 'the attempt row echoes the requested end, not the served one'
    assert run.finished_at == attempt.trailer.as_of
    assert (run.feed, run.bars_written) == (Feed.IEX, 1)


@pytest.mark.asyncio
async def test_no_log_line_from_a_served_request_names_the_owner(
    store: PostStore, monkeypatch, caplog: pytest.LogCaptureFixture
):
    """D8 over EVERY record the request produced, at every level: the route, the submit path, the waiter, the intake.

    The owner now rides further than before -- the request row's client_label, the Submit's owner, the entry the
    intake writes -- so there are more lines that could be tempted to name it, and this scans all of them.
    """
    caplog.set_level(logging.DEBUG)

    response = await post(store, SubmitDouble(store, _served([0])), REQUEST_BODY, monkeypatch)

    assert response.status_code == 200, response.text
    naming = [
        f'{record.name}: {record.getMessage()}'
        for record in caplog.records
        if REQUEST_BODY['owner'] in record.getMessage()
    ]
    assert naming == [], f'these log records name the owner, which D8 keeps out of logs entirely: {naming}'


# ---------------------------------------------------------------------------------------------
# 5. THE DECLARED HALF (TE-6 item 1, D1(c)): the OpenAPI says what the service does
# ---------------------------------------------------------------------------------------------


def test_the_openapi_declares_every_failure_status_as_problem_details():
    """``responses=PROBLEM_RESPONSES`` on the app, observed in the document it produces.

    WHY A DOCUMENT TEST WHEN THE BEHAVIOUR IS PINNED SIXTY TIMES OVER. Every other case in this
    file drives the running service, and all of them stay green if ``responses=PROBLEM_RESPONSES``
    is deleted from main.py -- the handlers are a separate call, and the service would go on
    answering correct problem+json. What breaks is the CONTRACT: the private SDK's typed client is
    generated from this document (tj-vhboky.30 is the precedent), so a document that still claims
    FastAPI's default HTTPValidationError for 422 and says nothing at all about 409, 429, 502, 503
    or 504 generates a client that cannot parse the answers it will actually receive. That is a
    silent break of a public contract, and it is invisible to every behavioural test.

    THE EXPECTED SET IS DERIVED FROM THE REASON TABLE, not written out: a reason added later with a
    status nothing else uses must appear in this document too, and a hand-listed set would not
    notice. 'default' is required separately -- it is what covers an HTTPException carrying no
    reason, such as require_instance_secret's 401 and routing's 405.
    """
    spec = app.openapi()
    declared_everywhere: set[str] | None = None
    for path, operations in spec['paths'].items():
        for method, operation in operations.items():
            if method not in {'get', 'post', 'put', 'patch', 'delete'}:
                continue
            statuses = set(operation.get('responses', {}))
            declared_everywhere = statuses if declared_everywhere is None else declared_everywhere & statuses
            assert 'default' in statuses, (
                f'{method.upper()} {path} declares no default response, so an HTTPException that '
                f"carries no reason -- the write secret's 401, routing's 405 -- is undocumented"
            )

    assert declared_everywhere is not None, 'the app exposes no operations, so this test asserts nothing'
    required = {str(row.http_status) for row in REASONS.values()} | {'500'}
    missing = sorted(required - declared_everywhere, key=int)
    assert missing == [], (
        f'these failure statuses are not declared on every operation: {missing}. Each is a status '
        f'some reason in the vocabulary renders as, so a generated client will meet it and have no '
        f'schema for it (ADR tj-fa1rpu D1(c)).'
    )


def test_the_declared_failure_schema_is_the_problem_details_model():
    """The declared statuses point at ProblemDetails, served as problem+json -- not at FastAPI's default.

    The statuses being PRESENT is the case above; this is that they mean the right thing. FastAPI
    documents a 422 of its own accord, with HTTPValidationError and application/json, so a 422 in
    the document proves nothing on its own -- it is there whether PROBLEM_RESPONSES was passed or
    not. The media type and the referenced schema are what tell the two apart, and 422 is exactly
    the status where getting it wrong is invisible to a count.
    """
    spec = app.openapi()
    # The OpenAPI keys paths by TEMPLATE, so the route's resolved URL is not a key. Fill each
    # template with this file's path parameters and keep the one that resolves to the same URL --
    # the same approach test_store_dataset_entry_route.py's _openapi_schema_of takes, and for the
    # same reason: reached from the route rather than by writing the template out here.
    url = app.url_path_for(ROUTE_NAME, **PATH_PARAMS)
    fill = defaultdict(str, PATH_PARAMS)
    (template,) = [candidate for candidate in spec['paths'] if candidate.format_map(fill) == url]
    operation = spec['paths'][template]['post']

    for status in ('422', '500', 'default'):
        content = operation['responses'][status]['content']
        assert PROBLEM_MEDIA_TYPE in content, (
            f'the {status} response is documented as {sorted(content)}, not as {PROBLEM_MEDIA_TYPE}. '
            f'A client generated from this document would not know to parse it as problem details.'
        )
        ref = content[PROBLEM_MEDIA_TYPE]['schema']['$ref']
        assert ref.rsplit('/', 1)[1] == ProblemDetails.__name__, (
            f'the {status} response references {ref}, not the ProblemDetails model every failure renders from'
        )
