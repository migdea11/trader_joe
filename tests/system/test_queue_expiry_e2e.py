"""Queue expiry and EXHAUSTED end to end through the REAL ingest queue (P26 S4, tj-7zjov1.287; owner ruling R4 2026-10-24).

Design: ADR tj-26jt9w addendum 2 (FIFO, dated requests first in arrival order, then null ready_by in arrival order),
addendum 4 (the ack gate, retries re-enter through submit), the owner's queue-expiry ruling of 2026-10-24 (expiry is
checked ONLY when a request is taken off the queue; a null ready_by never expires), the 14:49 UTC note on tj-7zjov1.38
(EXPIRED and EXHAUSTED reports reach the real store intake, are accepted, and leave the request FAILED with cause
EXPIRED / ATTEMPTS_EXHAUSTED and no ingest_run row), and P36c (the bars report names the resolved tape).

THE STACK MUST BE IN FAKE MODE (FakeRead behind the production AlpacaHandle, P53), as for test_ingest_e2e.py.

HOW A REQUEST REACHES THE REAL QUEUE FROM HERE. test_client is kept off ingest_store (ADR tj-q9ae5u addendum 3), so
nothing here calls ingest. Two routes reach it, both the stack's own code end to end:
  * POST /store, which writes the request and submits it (the clamp query first, then the bars request);
  * a PENDING ingest_request row written by SQL with created_at in the past: the RUNNING data_store's instance watcher
    (app/ingest/instance_watch.py; a pass every 15 s, re-sending PENDING rows older than 30 s, oldest created_at
    first, in one pass) submits it exactly as it would a POST whose Submit was lost, with the row's own ready_by and
    durable attempt count (submit_request_of). That is how the ORDER of arrival and the attempt count are controlled
    without a knob in the fake: all of one test's rows are inserted in one transaction, so one pass sends them all, in
    created_at order.

HOW THE QUEUE IS HELD. The Alpaca handle runs ExecutorSettings().workers (4) attempts at once. FakeRead's SLOW_
scenario holds a worker for DEFAULT_SLOW_DELAY_SECONDS (8 s). So WORKERS SLOW_ requests sent first occupy every
worker, and whatever arrives next waits in the queue for at least that long. A worker that something else on the stack
happens to hold only makes the wait longer: a request can be taken early only once every blocker ahead of it in its
tier has been taken, which needs a blocker to finish.

"NEVER SENT TO THE FAKE VENDOR" is read from the store: no ingest_run row for the request (an expired or exhausted
report writes none, and every attempt that reached the vendor writes one), attempts and spend unchanged, no entry.

"THE TAPE ON THE REPORT" is read the only way the store can show it for a report with no attempt row: the report was
ACCEPTED. The intake refuses a bars report with no resolved feed (validate_report rule (e)) with INVALID_REQUEST,
ingest redelivers it for ever, and the request would stay QUEUED: the bounded wait below then fails naming its state.

NOT HERE: S2, S3, S5, S6 (phased, tj-7zjov1.286). Nothing here restarts or reconfigures a service.

CLEANUP. ingest_request and ingest_run carry no foreign key, so the entry registry's cascade does not reach them: every
symbol a test here uses is recorded, and its request and attempt rows are deleted at the end of the module. Entries are
adopted (adopt_entries, scenario_entries) after the requests have finished, so an entry minted by a delivery is never
left behind.
"""

import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType
from common.errors.vocabulary import Reason
from data.ingest.app.scheduler.executor import ExecutorSettings
from data.ingest.app.scheduler.retry import DEFAULT_MAX_ATTEMPTS
from data.store.app.database.models.ingest_request import (
    OPEN_STATES,
    IngestRequest,
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from routers.data_store.app_endpoints import AssetDatasetStoreInterface
from tests.fakes.market_data import DEFAULT_SLOW_DELAY_SECONDS, FAIL_PREFIX, SLOW_PREFIX
from tests.system.problem_json import assert_problem


pytestmark = pytest.mark.data_store

REQUESTS = IngestRequest.__table__
RUNS = IngestRun.__table__
ENTRIES = StoreDatasetEntry.__table__
GRANULARITY = Granularity.ONE_MINUTE
START = datetime.fromisoformat('2001-02-05T14:30:00+00:00')
END = START + timedelta(minutes=30)

# The attempts the Alpaca handle runs at once: its executor's default (executor_settings_from_env sets no worker count).
WORKERS = ExecutorSettings().workers
# Far enough in the past that the watcher's PENDING cutoff (30 s by default) is behind every row written here.
BACKDATE = timedelta(minutes=2)
# A ready_by that cannot pass while the test runs.
FAR = timedelta(minutes=10)
# A bound on a whole scenario: one watcher interval (15 s), two SLOW_ waves (2 x 8 s), the rate budget, and room.
SCENARIO_BOUND_SECONDS = 150.0
POLL_SECONDS = 0.5
# A POST answered by the expiry comes back long before the verdict deadline (300 s); a POST answered by the deadline
# would not. This bound tells the two DEADLINE answers apart.
EXPIRY_ANSWER_BOUND_SECONDS = 60.0


class Symbols:
    """Every symbol this module's requests carry, for the module's cleanup."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def add(self, symbol: str) -> str:
        self.names.append(symbol)
        return symbol


@pytest.fixture(scope='module')
def symbols(pg_engine: Engine) -> Iterator[Symbols]:
    used = Symbols()
    yield used
    if not used.names:
        return
    with pg_engine.begin() as conn:
        ids = sa.select(REQUESTS.c.id).where(REQUESTS.c.asset_symbol.in_(used.names))
        conn.execute(sa.delete(RUNS).where(sa.or_(RUNS.c.request_id.in_(ids), RUNS.c.asset_symbol.in_(used.names))))
        conn.execute(sa.delete(REQUESTS).where(REQUESTS.c.asset_symbol.in_(used.names)))


def _store_path(symbol: str) -> str:
    return AssetDatasetStoreInterface.POST_STORE_ASSET_DATASET.format(
        asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value, asset_symbol=symbol
    )


def _body(owner: str, ready_by: datetime) -> dict[str, Any]:
    return {
        'owner': owner,
        'source': DataSource.ALPACA_API.value,
        'granularity': GRANULARITY.value,
        'start': START.isoformat(),
        'end': END.isoformat(),
        'ready_by': ready_by.isoformat(),
    }


def _pending_row(
    owner: str, symbol: str, created_at: datetime, *, ready_by: datetime | None = None, attempts: int = 0
) -> dict[str, Any]:
    """A PENDING bars request row, shaped as POST /store writes one (new_bars_request), for the watcher to send."""
    return {
        'id': uuid.uuid4(),
        'kind': IngestRequestKind.BARS,
        'source': DataSource.ALPACA_API,
        'asset_symbol': symbol,
        'asset_type': AssetType.STOCK,
        'data_types': [DataType.MARKET_ACTIVITY.value],
        'granularity': GRANULARITY,
        'update_type': UpdateType.STATIC.value,
        'requested_start': START,
        'requested_end': END,
        'ready_by': ready_by,
        'client_label': owner,
        'trigger': IngestRunTrigger.REQUEST,
        'expiry_type': ExpiryType.BULK.value,
        'state': IngestRequestState.PENDING,
        'attempts': attempts,
        'created_at': created_at,
    }


def _insert_in_arrival_order(pg_engine: Engine, rows: list[dict[str, Any]]) -> None:
    """Insert the rows in ONE transaction, created_at 1 ms apart in list order, so one watcher pass sends them in order."""
    for offset, row in enumerate(rows):
        row['created_at'] = row['created_at'] + timedelta(milliseconds=offset)
    with pg_engine.begin() as conn:
        conn.execute(sa.insert(REQUESTS), rows)


def _requests(pg_engine: Engine, ids: list[UUID]) -> dict[UUID, sa.Row]:
    with pg_engine.connect() as conn:
        return {row.id: row for row in conn.execute(sa.select(REQUESTS).where(REQUESTS.c.id.in_(ids)))}


def _wait(pg_engine: Engine, ids: list[UUID], done: Callable[[sa.Row], bool], what: str) -> dict[UUID, sa.Row]:
    """Poll the request rows until every one satisfies `done`, within SCENARIO_BOUND_SECONDS; fail naming the states."""
    deadline = time.monotonic() + SCENARIO_BOUND_SECONDS
    while True:
        rows = _requests(pg_engine, ids)
        if len(rows) == len(ids) and all(done(row) for row in rows.values()):
            return rows
        if time.monotonic() > deadline:
            states = sorted(str(row.state) for row in rows.values())
            pytest.fail(
                f'{what}: not reached within {SCENARIO_BOUND_SECONDS:.0f}s; request states {states} '
                '(a report the store refused stays QUEUED while ingest redelivers it)',
                pytrace=False,
            )
        time.sleep(POLL_SECONDS)


def _finished(pg_engine: Engine, ids: list[UUID]) -> dict[UUID, sa.Row]:
    return _wait(pg_engine, ids, lambda row: row.state not in OPEN_STATES, 'every request finished')


def _runs(pg_engine: Engine, request_id: UUID) -> list[sa.Row]:
    with pg_engine.connect() as conn:
        return list(conn.execute(sa.select(RUNS).where(RUNS.c.request_id == request_id).order_by(RUNS.c.attempt_no)))


def _entries(pg_engine: Engine, symbol: str) -> list[UUID]:
    with pg_engine.connect() as conn:
        return list(conn.execute(sa.select(ENTRIES.c.id).where(ENTRIES.c.asset_symbol == symbol)).scalars())


def _requests_of(pg_engine: Engine, symbol: str, kind: IngestRequestKind) -> list[sa.Row]:
    with pg_engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(REQUESTS)
                .where(REQUESTS.c.asset_symbol == symbol, REQUESTS.c.kind == kind)
                .order_by(REQUESTS.c.created_at, REQUESTS.c.id)
            )
        )


def _adopt(symbol: str, adopt_entries, scenario_entries) -> list[UUID]:
    return (scenario_entries if symbol.startswith((SLOW_PREFIX, FAIL_PREFIX)) else adopt_entries)(symbol)


def _assert_never_sent(pg_engine: Engine, request: sa.Row, cause: IngestRequestFailureCause, attempts: int) -> None:
    """FAILED with `cause`, from a report that made no vendor call: no attempt row, count and spend unchanged."""
    assert (request.state, request.failure_cause) == (IngestRequestState.FAILED, cause), (
        f'request {request.id} finished {request.state} cause {request.failure_cause}, not FAILED {cause}'
    )
    assert request.finished_at is not None and request.dataset_id is None, request.id
    assert request.attempts == attempts, f'request {request.id} counts {request.attempts} attempts, not {attempts}'
    assert (request.spent_calls, request.spent_pages) == (0, 0), f'request {request.id} was charged a vendor call'
    assert _runs(pg_engine, request.id) == [], f'request {request.id} has an attempt row: it reached the vendor'


def _assert_served_once(pg_engine: Engine, request: sa.Row) -> sa.Row:
    assert request.state == IngestRequestState.SUCCEEDED, f'request {request.id} finished {request.state}'
    (run,) = _runs(pg_engine, request.id)
    assert (run.state, run.attempt_no) == (IngestRunState.SUCCEEDED, 1), run
    assert run.vendor_first_call_at is not None, f'attempt of {request.id} carries no vendor_first_call_at'
    return run


# --------------------------------------------------------------------------------------------- order and expiry


def test_dated_requests_go_first_a_null_ready_by_waits_last_and_a_passed_one_expires_at_dequeue(
    own_symbol, run_identity, pg_engine, adopt_entries, scenario_entries, symbols
):
    """Dated before null, null never expired, a passed ready_by expired at dequeue: one watcher pass, nine requests.

    The pass sends, in this order: WORKERS undated SLOW_ blockers, U (null ready_by), WORKERS dated SLOW_ requests D,
    then E, dated, whose ready_by passes 5 s after the insert. The blockers take every worker. When they finish, the workers take the dated D before U, which ARRIVED FIRST, and
    E only after them: E is taken at least 2 x 8 s after the pass, so its ready_by has passed and it is expired at
    dequeue, never sent. U is taken last of all, after every D's vendor call, and is served: a null ready_by is never
    checked, however long it waits.

    Reds on: an undated-first or plain-arrival queue (U's vendor call comes before a D's); a skipped dequeue check (E
    is served: an attempt row and SUCCEEDED); a dropped feed on the EXPIRED report (E stays QUEUED, the wait fails).
    """
    t0 = datetime.now(UTC)
    created = t0 - BACKDATE
    owner = run_identity.owner
    blockers = [_pending_row(owner, symbols.add(f'{SLOW_PREFIX}{own_symbol}B{n}'), created) for n in range(WORKERS)]
    undated = _pending_row(owner, symbols.add(f'{own_symbol}U'), created)
    dated = [
        _pending_row(owner, symbols.add(f'{SLOW_PREFIX}{own_symbol}D{n}'), created, ready_by=t0 + FAR)
        for n in range(WORKERS)
    ]
    expiring = _pending_row(owner, symbols.add(f'{own_symbol}E'), created, ready_by=t0 + timedelta(seconds=5))
    rows = [*blockers, undated, *dated, expiring]
    ids = [row['id'] for row in rows]

    _insert_in_arrival_order(pg_engine, rows)
    try:
        finished = _finished(pg_engine, ids)
    finally:
        for row in rows:
            _adopt(row['asset_symbol'], adopt_entries, scenario_entries)

    # E: expired at dequeue, accepted by the store (its tape was on the report), never sent.
    _assert_never_sent(pg_engine, finished[expiring['id']], IngestRequestFailureCause.EXPIRED, attempts=0)
    assert _entries(pg_engine, expiring['asset_symbol']) == [], 'an expired request wrote an entry'

    # Everyone else served once.
    for row in blockers:
        _assert_served_once(pg_engine, finished[row['id']])
    dated_calls = [_assert_served_once(pg_engine, finished[row['id']]).vendor_first_call_at for row in dated]
    undated_run = _assert_served_once(pg_engine, finished[undated['id']])

    # U arrived before every D, and was served after every D's vendor call: dated first, null last.
    assert all(finished[undated['id']].created_at < finished[row['id']].created_at for row in dated)
    assert undated_run.vendor_first_call_at > max(dated_calls), (
        f'the null ready_by request reached the vendor at {undated_run.vendor_first_call_at}, '
        f'before the last dated request did ({max(dated_calls)}): it did not sort last'
    )
    # It waited behind two SLOW_ waves and was still served: a null ready_by is never expired.
    assert finished[undated['id']].failure_cause is None


def test_a_retry_that_reenters_the_queue_after_its_ready_by_is_expired_and_the_post_fails(
    own_symbol, run_identity, data_store, pg_engine, scenario_entries, symbols
):
    """A retry that re-enters the queue after the ready_by is expired at its dequeue, and the POST fails.

    POST a FAIL_ symbol with ready_by 10 s out: attempt 1 is taken in time and fails, the store answers CONTINUE,
    and each retry re-enters through Scheduler.submit after its backoff (1, 2, 4, 8 s). The re-entry that is taken
    after the ready_by is expired at dequeue: the request is FAILED with cause EXPIRED, not ATTEMPTS_EXHAUSTED (the
    fifth attempt would come at about 15 s), every attempt that did run is a FAILED row, and the expired one has none.
    The POST answers 504 DEADLINE with the expired sentence, in seconds, not at the 300 s verdict deadline.

    Reds on: a retry that skips the dequeue check (five FAILED rows, ATTEMPTS_EXHAUSTED, 503).
    """
    symbol = symbols.add(f'{FAIL_PREFIX}{own_symbol}')
    sent = time.monotonic()
    try:
        response = data_store.post(
            _store_path(symbol), json=_body(run_identity.owner, datetime.now(UTC) + timedelta(seconds=10)), auth='right'
        )
    finally:
        ids = scenario_entries(symbol)
    elapsed = time.monotonic() - sent

    problem = assert_problem(
        response, status=504, reason=Reason.DEADLINE.value, title='Gateway Timeout', describe=data_store.describe
    )
    assert 'expired' in problem['detail'], data_store.describe(response)
    assert elapsed < EXPIRY_ANSWER_BOUND_SECONDS, f'the POST took {elapsed:.0f}s: a deadline answer, not an expiry'
    assert ids == [], f'a request that never delivered a success left entries {ids}'

    (request,) = _requests_of(pg_engine, symbol, IngestRequestKind.BARS)
    assert (request.state, request.failure_cause) == (IngestRequestState.FAILED, IngestRequestFailureCause.EXPIRED), (
        f'the request finished {request.state} cause {request.failure_cause}'
    )
    runs = _runs(pg_engine, request.id)
    assert 1 <= len(runs) < DEFAULT_MAX_ATTEMPTS, (
        f'{len(runs)} attempt rows: expected at least the first (taken in time) and fewer than the budget'
    )
    assert len(runs) == request.attempts, f'{len(runs)} attempt rows but the request counts {request.attempts}'
    assert [run.attempt_no for run in runs] == list(range(1, len(runs) + 1)), runs
    assert [run.trigger for run in runs] == [IngestRunTrigger.REQUEST] + [IngestRunTrigger.RETRY] * (len(runs) - 1)
    for run in runs:
        assert (run.state, run.dataset_id, run.bars_written) == (IngestRunState.FAILED, None, 0), run


def test_a_synchronous_post_whose_ready_by_passes_while_queued_gets_a_failure(
    own_symbol, run_identity, data_store, pg_engine, adopt_entries, scenario_entries, symbols
):
    """A POST whose ready_by passes while its first request waits in the queue answers a failure, in seconds.

    WORKERS + 2 DATED SLOW_ blockers are sent first (ready_by far off), then a POST with ready_by 2 s out. The POST's first request, its name_change clamp query, carries the POST's ready_by (first_request_clamp) and is
    dated, so it queues BEHIND the blockers still waiting (dated FIFO), and is taken only after a first SLOW_ wave ends,
    past its ready_by: expired at dequeue. The POST fails closed on it: 504 DEADLINE with the expired sentence, in
    seconds, and no bars request is written. A still-running reply (the deadline answer with the request open) would
    come at 300 s with the request still open.

    Reds on: a POST that answers from anything but the request row's verdict; a clamp that drops the ready_by (it is
    served, the bars request is written and served, a 200); a queue that does not check the ready_by at dequeue.
    """
    t0 = datetime.now(UTC)
    owner = run_identity.owner
    blockers = [
        _pending_row(owner, symbols.add(f'{SLOW_PREFIX}{own_symbol}Q{n}'), t0 - BACKDATE, ready_by=t0 + FAR)
        for n in range(WORKERS + 2)
    ]
    blocker_ids = [row['id'] for row in blockers]
    symbol = symbols.add(f'{own_symbol}P')

    _insert_in_arrival_order(pg_engine, blockers)
    try:
        # Every blocker answered by ingest: the first wave holds every worker, the rest wait in the dated tier.
        _wait(pg_engine, blocker_ids, lambda row: row.state is not IngestRequestState.PENDING, 'every blocker queued')
        sent = time.monotonic()
        try:
            response = data_store.post(
                _store_path(symbol), json=_body(owner, datetime.now(UTC) + timedelta(seconds=2)), auth='right'
            )
        finally:
            ids = adopt_entries(symbol)
        elapsed = time.monotonic() - sent
        finished = _finished(pg_engine, blocker_ids)
    finally:
        for row in blockers:
            scenario_entries(row['asset_symbol'])

    problem = assert_problem(
        response, status=504, reason=Reason.DEADLINE.value, title='Gateway Timeout', describe=data_store.describe
    )
    assert 'expired' in problem['detail'], data_store.describe(response)
    assert elapsed < EXPIRY_ANSWER_BOUND_SECONDS, f'the POST took {elapsed:.0f}s: a deadline answer, not an expiry'
    assert elapsed >= DEFAULT_SLOW_DELAY_SECONDS / 2, (
        f'the POST failed after {elapsed:.1f}s, before a SLOW_ wave could end: it never queued behind the blockers'
    )
    assert ids == []
    (clamp,) = _requests_of(pg_engine, symbol, IngestRequestKind.CORPORATE_ACTIONS)
    _assert_never_sent(pg_engine, clamp, IngestRequestFailureCause.EXPIRED, attempts=0)
    assert _requests_of(pg_engine, symbol, IngestRequestKind.BARS) == [], 'the POST wrote a bars request after expiry'
    for row in blockers:
        _assert_served_once(pg_engine, finished[row['id']])


# --------------------------------------------------------------------------------------------- EXHAUSTED


def test_a_request_sent_at_its_attempt_budget_is_exhausted_by_the_real_ingest_without_a_vendor_call(
    own_symbol, run_identity, pg_engine, adopt_entries, symbols
):
    """A bars request sent at its attempt budget is ended EXHAUSTED by the real ingest, with no vendor call.

    A PENDING bars request whose durable count already equals the budget (DEFAULT_MAX_ATTEMPTS, 5) is sent by the
    watcher with that count (submit_request_of). The real handle's Submit bounds it: no vendor attempt, never queued,
    an EXHAUSTED report (BrokerHandle.submit, AckGatedExecutor.report_exhausted). The store accepts it (the tape is
    on its header) and finishes the request FAILED with cause ATTEMPTS_EXHAUSTED, the count unchanged, no attempt row.

    No phase file and no ALPACA_MAX_ATTEMPTS override is needed: the count is the store's, written here. It assumes the
    stack does not RAISE ALPACA_MAX_ATTEMPTS above the default (no committed file sets it); a lower budget is still
    reached by a count of 5.

    Reds on: a Submit that queues a request at its budget (an attempt row, FAILED, then ATTEMPTS_EXHAUSTED from a
    real attempt: the no-attempt-row check catches it); a dropped feed on the EXHAUSTED report (it stays QUEUED).
    """
    row = _pending_row(
        run_identity.owner, symbols.add(f'{own_symbol}X'), datetime.now(UTC) - BACKDATE, attempts=DEFAULT_MAX_ATTEMPTS
    )

    _insert_in_arrival_order(pg_engine, [row])
    try:
        finished = _finished(pg_engine, [row['id']])
    finally:
        ids = adopt_entries(row['asset_symbol'])

    _assert_never_sent(
        pg_engine, finished[row['id']], IngestRequestFailureCause.ATTEMPTS_EXHAUSTED, attempts=DEFAULT_MAX_ATTEMPTS
    )
    assert ids == [], f'an exhausted request wrote entries {ids}'
