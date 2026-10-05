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

WHAT TIER THIS IS. TestClient against the real app with real handlers installed, over fake sessions
and a scripted seam. Every status here is one FastAPI assembled in-process; no Postgres and no
ingest are reached. That a real deployment returns the same codes is the System Testing job's
(TE-7), and nothing below should be read as having shown it.
"""

import logging
import re
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError, ProgrammingError

from common.enums.data_stock import Feed
from common.errors.vocabulary import REASONS, ExogenousError, InvalidRequestError, Reason
from data.store.app.app_depends import get_ingest_fetch_client
from data.store.app.database.database import async_db
from data.store.app.main import app
from data.store.tests.fetch_double import FetchScript, RecordingFetchClient, accepted_stream, bar
from data.store.tests.problem_body import PROBLEM_MEDIA_TYPE, problem
from routers.common.errors import ProblemDetails
from routers.common.instance_secret import INSTANCE_SECRET_ENV_VAR, INSTANCE_SECRET_HEADER
from schemas.data_ingest import fetch_dataset


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
    """One canned answer, with the two accessors the create path reads."""

    def __init__(self, *, rows: list[tuple] | None = None, scalar: uuid.UUID | None = None):
        self._rows = rows if rows is not None else []
        self._scalar = scalar

    def all(self) -> list[tuple]:
        return self._rows

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

    Three kinds of statement reach it on a served fetch, in this order: the hoisted own-overlap
    SELECT, the entry upsert (whose RETURNING id is read back), and one INSERT per chunk of bars.
    Only the first two have results anyone reads, but the fake insists on having one per statement
    so that a path issuing an unplanned statement fails loudly rather than silently.

    Args:
        bar_statements: How many bar-write statements the scripted pages will produce.

    Returns:
        FakeSession: The session.
    """
    answers = [FakeResult(rows=[]), FakeResult(scalar=uuid.uuid4())]
    answers.extend(FakeResult() for _ in range(bar_statements))
    return FakeSession(*answers)


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
        Callable: (session, fetch_client) -> httpx.Response.
    """
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)

    def send(session: FakeSession, fetch_client: RecordingFetchClient, body: dict | None = None):
        app.dependency_overrides[async_db] = lambda: session
        app.dependency_overrides[get_ingest_fetch_client] = lambda: fetch_client
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

    response = post_dataset(session, RecordingFetchClient())

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

    response = post_dataset(session, RecordingFetchClient())

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

    response = post_dataset(session, RecordingFetchClient())

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
# 2. THE FETCH OUTCOMES (TE-6 item 6, tj-fe19tu): a vendor failure, by reason, at the edge
# ---------------------------------------------------------------------------------------------

_RESET_AT = datetime(2026, 1, 2, 13, 0, tzinfo=UTC)

_FETCH_FAILURES: list[tuple[str, Reason, int]] = [
    ('a refused ack', Reason.FEED_NOT_AVAILABLE, 422),
    ('the store rate budget', Reason.RATE_BUDGET, 429),
    ('the vendor rate-limited us', Reason.VENDOR_RATE_LIMITED, 429),
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
    detail = f'the fetch failed with {reason.value}'
    if reason is Reason.FEED_NOT_AVAILABLE:
        return InvalidRequestError(reason, detail)
    return ExogenousError(reason, detail, reset_at=_RESET_AT if requires_reset_at else None)


@pytest.mark.parametrize(
    ('reason', 'status'), [(r, s) for _, r, s in _FETCH_FAILURES], ids=[n for n, _, _ in _FETCH_FAILURES]
)
def test_a_fetch_failure_renders_as_its_reasons_status_and_writes_nothing(post_dataset, reason: Reason, status: int):
    """TE-6 item 6's table, driven at the edge, one case per row.

    EVERY ROW, because the rows are the contract a caller programs against and a single
    representative would pin only the handler's existence. The split that matters most is 429 from
    503 from 504: all three mean "not your fault, try later", and only the status and reason tell a
    caller whether to back off on a schedule, fail over, or lengthen its deadline.

    AND WRITES NOTHING, which is the half a status assertion cannot see. The failure arrives inside
    the one fetch transaction, so it must roll that transaction back on the way out -- a route that
    rendered the right 503 having committed a dataset entry for a fetch that never happened would
    leave an entry claiming coverage of bars nobody has. That is this epic's stated failure class,
    and it is invisible to the body.

    Args:
        post_dataset: Drives the real POST route.
        reason: The reason the seam raises before yielding anything.
        status: The status its row renders as.
    """
    session = FakeSession(FakeResult(rows=[]))
    fetch_client = RecordingFetchClient(FetchScript(raises=_failure_for(reason)))

    response = post_dataset(session, fetch_client)

    body = problem(response, status=status, reason=reason.value)
    assert 'served_range' not in body, (
        f'a FAILED fetch carries served_range: {body}. Only a 200 does -- a failure answered no '
        f'window at all, and publishing one would tell the caller a range was served when none was.'
    )
    assert session.commits == 0, 'a failed fetch committed its transaction'
    assert session.rollbacks == 1, 'a failed fetch did not roll its transaction back exactly once'


@pytest.mark.parametrize('reason', [Reason.RATE_BUDGET, Reason.VENDOR_RATE_LIMITED])
def test_a_rate_limited_fetch_carries_retry_after_and_reset_at(post_dataset, reason: Reason):
    """The 429s say WHEN, in a header and in the body, and the two must agree.

    TWO SPELLINGS OF ONE FACT, which is exactly the shape that drifts: an HTTP client obeys the
    Retry-After header and a typed SDK reads reset_at, so a renderer that derived them from two
    readings of the clock would have them disagree by a second or more and send the two clients to
    different times. routers/common/errors.py derives both from one read for that reason; this is
    the case at the edge that would notice if that stopped being true.

    THE HEADER IS DELTA-SECONDS and reset_at is an instant, so they are compared by reconstruction
    rather than by equality: now + Retry-After must land on reset_at, within the second that
    rendering takes.

    Args:
        post_dataset: Drives the real POST route.
        reason: The rate-limit reason, whose row requires reset_at.
    """
    session = FakeSession(FakeResult(rows=[]))
    fetch_client = RecordingFetchClient(FetchScript(raises=_failure_for(reason)))

    response = post_dataset(session, fetch_client)

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
# 3. served_range (TE-6 item 7): copied out of FetchDone, never echoed back from the request
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('bar_minutes', 'expected_points'),
    [pytest.param([0, 1], 2, id='a-served-window-with-bars'), pytest.param([], 0, id='a-served-but-empty-window')],
)
def test_the_200_carries_the_window_the_vendor_answered_for_not_the_one_asked(
    post_dataset, bar_minutes: list[int], expected_points: int
):
    """THE ECHO IS THE BUG THIS GUARDS, and the fixture is what makes it detectable.

    served_range is the FetchDone's, COPIED UNCHANGED -- never rebuilt from the request and never
    clamped a second time at the store (TE-6 item 7, user ruling 2026-10-02). The plausible wrong
    implementation is not an exotic one: it is building ServedRange(start=request.start,
    end=request.end) at the route, which looks right, needs no plumbing out of the transaction, and
    is correct for every fetch the vendor happens to serve in full. SERVED_END is three minutes
    after the start while the request asks for twelve hours, so the echo is off by the whole
    difference and cannot coincide.

    BOTH PARAMETERS ARE REQUIRED BY THE BEAD and they are not the same case. The empty one is the
    one that matters operationally: a misspelled symbol is SERVED, successfully, for the window
    asked and with no bars in it (tj-lldllr's known gap), and the only thing that lets a caller tell
    that from a window the vendor had nothing for is the range in the body. It is also the case a
    lazy implementation gets wrong for free, by returning early on a zero-bar stream before the
    FetchDone has been read.

    Args:
        post_dataset: Drives the real POST route.
        bar_minutes: Minute offsets for the single scripted page, or none for an empty window.
        expected_points: How many bars the body must report.
    """
    pages = [fetch_dataset.BarPage(bars=[bar(minute, feed=Feed.IEX) for minute in bar_minutes])] if bar_minutes else []
    events = [fetch_dataset.FetchAccepted(feed=Feed.IEX), *pages, served_done(len(bar_minutes))]
    session = _accepting_session(bar_statements=len(pages))
    fetch_client = RecordingFetchClient(FetchScript(events=events))

    response = post_dataset(session, fetch_client)

    assert response.status_code == 200, f'a served fetch answered {response.status_code}: {response.text}'
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
    # THE ANTI-ECHO ASSERTION, stated separately so its failure says what went wrong rather than
    # merely that two timestamps differ.
    assert datetime.fromisoformat(served['end']) != REQUESTED_END, (
        f'the body served_range ends exactly where the REQUEST ended ({REQUESTED_END.isoformat()}), '
        f'so the store is echoing what was asked for instead of copying what FetchDone answered. '
        f"The clamp is ingest's (end = min(requested end, as_of)); a store that recomputes it "
        f"publishes its own guess as the vendor's answer."
    )
    assert 'as_of' not in body, 'as_of is not exposed at the HTTP edge: the ruling names served_range only'


def test_an_empty_window_is_a_success_and_a_vendor_failure_is_not(post_dataset):
    """tj-fe19tu's acceptance criterion in one case: the two outcomes are distinguishable.

    THE WHOLE FEATURE IS THIS COMPARISON, so it is asserted as one rather than left implicit across
    two files. Before TE-6 a vendor failure surfaced as an unhandled 500 and a served-but-empty
    window as a 200 with a bare count, so a caller seeing "no bars" could not tell whether the
    vendor had answered with nothing or the fetch had never happened -- and the 500 carried nothing
    it could branch on either. Both halves are driven here, through the same route and the same
    double, so neither can be repaired into agreement with the other by accident.
    """
    empty = RecordingFetchClient(FetchScript(events=[fetch_dataset.FetchAccepted(feed=Feed.IEX), served_done(0)]))
    served_response = post_dataset(_accepting_session(), empty)

    failed = RecordingFetchClient(FetchScript(raises=_failure_for(Reason.VENDOR_UNAVAILABLE)))
    failed_response = post_dataset(FakeSession(FakeResult(rows=[])), failed)

    assert served_response.status_code == 200, (
        f'a window the vendor SERVED with no bars answered {served_response.status_code}. An empty '
        f'result is a success carrying provenance, not a failure (ADR tj-fa1rpu D2, Q-EMPTY).'
    )
    assert served_response.json()['data_points'] == 0
    failed_body = problem(failed_response, status=503, reason='VENDOR_UNAVAILABLE')
    assert failed_body['reason'] != served_response.status_code, 'the two outcomes are not distinguishable'
    assert 'served_range' in served_response.json() and 'served_range' not in failed_body


# ---------------------------------------------------------------------------------------------
# 4. THE INFO LINE (Q-EMPTY, tj-3mk3u5.37.1): what a served fetch leaves in the log
# ---------------------------------------------------------------------------------------------


def test_a_served_fetch_logs_one_info_line_naming_the_range_as_of_feed_and_count(
    post_dataset, caplog: pytest.LogCaptureFixture
):
    """One line per served fetch, carrying four values and NEVER the owner (D8).

    WHY THE LINE EXISTS: tj-lldllr's known gap is a misspelled symbol served empty, and this line is
    what makes that diagnosable from the logs alone. Each of the four values answers a different
    question -- which window the vendor actually covered, how current it is, which tape, and how
    much landed -- so each is asserted by name. A line that dropped one would still look like a
    perfectly good log line.

    AND NEVER THE OWNER. The owner is a principal's name and D8 keeps it out of logs entirely; it is
    a SensitiveString elsewhere for the same reason. Adding it to this line is the single most
    natural "improvement" anyone would make to it -- it is the one piece of context the line lacks
    -- so the prohibition is asserted rather than left to the comment in the production file.
    """
    caplog.set_level(logging.INFO)
    events = [fetch_dataset.FetchAccepted(feed=Feed.IEX), fetch_dataset.BarPage(bars=[bar(0, feed=Feed.IEX)])]
    fetch_client = RecordingFetchClient(FetchScript(events=[*events, served_done(1)]))

    response = post_dataset(_accepting_session(bar_statements=1), fetch_client)

    assert response.status_code == 200, response.text
    served_lines = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and 'Fetch served' in record.getMessage()
    ]
    assert len(served_lines) == 1, f'a served fetch logged {len(served_lines)} INFO lines, not one: {served_lines}'
    line = served_lines[0]
    for label, value in (
        ('the served start', str(SERVED_START)),
        ('the served end', str(SERVED_END)),
        ('as_of', str(AS_OF)),
        ('the resolved feed', str(Feed.IEX)),
    ):
        assert value in line, f'the INFO line does not name {label} ({value!r}): {line!r}'
    assert re.search(r'\b1 written\b', line), f'the INFO line does not name the row count: {line!r}'
    assert REQUEST_BODY['owner'] not in line, (
        f'the INFO line names the owner, which is a principal and never goes to a log (D8): {line!r}'
    )


def test_no_log_line_from_a_served_fetch_names_the_owner(post_dataset, caplog: pytest.LogCaptureFixture):
    """The prohibition over EVERY record, not only the one line that is required to exist.

    The case above would stay green if the owner were logged by a different line at a different
    level -- the route's own DEBUG line, say, which does log the symbol. This scans every record the
    request produced, at every level, which is the form the D8 rule actually takes.
    """
    caplog.set_level(logging.DEBUG)
    fetch_client = RecordingFetchClient(FetchScript(events=accepted_stream([[0]])))

    response = post_dataset(_accepting_session(bar_statements=1), fetch_client)

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
