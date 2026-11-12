"""Shared parts of the PHASED P26 scenarios (S2, S3, S5, S6): seeding, hand-off, polling, cleanup (tj-7zjov1.286).

WHAT A PHASED SCENARIO IS. A scenario that needs the orchestrator to disturb the stack between two checks (restart
data_store, restart data_ingest, stop data_store while ingest runs, recreate data_store with an environment override)
is two files: phase A leaves tagged state in the database, the orchestrator runs its agent-stack verbs, and phase B
judges what the database holds afterwards. The step list in the module docstrings of the phase files (and in the
hand-back on tj-7zjov1.286) is the order the orchestrator follows. Design: P26 tj-7zjov1.38 (notes 01:05, 01:35 and
02:35 UTC 2026-11-02), ADR tj-4rr0la addendum 32 (test_stop, test_start, test_env), the owner's 2026-10-24 rulings in
ADR tj-26jt9w (the ack gate, the restart, the R2 buffer).

WHY A PHASE FILE NEVER RUNS IN A WHOLE-TIER RUN. Phase files are named phase_*.py, not test_*.py. pytest collects a
directory's files only when they match python_files (test_*.py), so `pytest tests/system`, and run_system_tests with
paths [] or ['tests/system'], recurses into this directory and collects nothing from it; a file NAMED on the command
line is collected whatever its name (an initial path skips the python_files match). conftest.py beside this module adds
a guard: a phase test collected without its file named fails the collection, so a renamed file cannot slip into the
tier. Proof: the collect-only counts in the hand-back of tj-7zjov1.286.

A PHASE FILE NEEDS A RUN OF ITS OWN, never one that also names tests/system. Measured: `pytest --collect-only
tests/system <phase file>` collects the tier's count and NOT the phase test, in either argument order (the file lies
under a directory argument already collected, which did not match it). Phase files may share a run with each other
(S3 phase B with S2 phase A), and with plain test files named one by one.

HAND-OFF THROUGH THE DATABASE ONLY. Phase A's rows carry a scenario tag in the symbol (SLOW_ZZP26<code><RUN_ID><nn>)
or in the panel name (p26-phase-s6-<RUN_ID>), so phase B finds them without a file and fails loudly when there is
none: phase A never ran. Phase A first deletes what an earlier, aborted run of its scenario left, but only rows that are
finished: an open request may still be held by ingest, whose report for a deleted request would be refused and
redelivered for ever, so phase A fails instead and says so.

HOW REQUESTS ARE SEEDED. Phase A writes POST-shaped bars request rows through the store's own code (new_bars_request,
create_request), PENDING, then backdates created_at by BACKDATE so the RUNNING data_store's instance watcher re-sends
them on its next pass (PENDING older than 30 s: the case of a store that wrote a request and lost its Submit). The
watcher submits each to the running ingest and marks it QUEUED under the answering instance; from there the path is
production's end to end: ingest's queue, its workers, FakeRead, the acknowledgement-gated executor, its outgoing
buffer, delivery over data-store-grpc, the delivery intake minting the entry and writing the bars and the attempt row.
The POST route is not used because it waits for the verdict and its name_change clamp queues one more request per
POST behind the slow ones, so many concurrent waiting clients would be needed to fill ingest's queue; the watcher puts
the whole set on the queue at once with nobody waiting. Each symbol carries FakeRead's SLOW_ prefix, so every attempt
sleeps the fake's slow delay (8 s by default) and the queue drains at about WORKERS attempts per delay: that is what
keeps work in ingest while the orchestrator disturbs the stack.

SEEDED_AT. The backdating UPDATE sets created_at to now() - BACKDATE, so created_at + BACKDATE is the database instant
of that UPDATE: when phase A put its work in. Phase B compares it with restart evidence.

RESTART EVIDENCE FROM THE DATABASE. data_store is the only service connected to Postgres (ingest has no database).
data_store_connected_since() reads the oldest backend_start among the client backends that are not this test
process's (another client address); a restarted data_store holds only connections opened after its start, so that
instant is a lower bound of the restart. Clocks: Postgres and the ingest container read the same host clock.

CLEANUP. Phase B deletes everything its scenario wrote, by id and by the scenario's symbol prefix (attempt rows,
request rows, entries; the cascade takes the bars), but only once every request is finished, for the reason above.
"""

import time
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Granularity
from common.rpc.clients.store_delivery import DEFAULT_DELIVERY_DEADLINE_S
from data.ingest.app.scheduler.executor import ExecutorSettings
from data.store.app.database.crud.ingest_request import NewIngestRequest, create_request
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.ingest.data_action_request import new_bars_request
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath
from tests.fakes.market_data import DEFAULT_SLOW_DELAY_SECONDS, SLOW_PREFIX, grid_timestamps


REQUESTS = IngestRequest.__table__
RUNS = IngestRun.__table__
INSTANCES = IngestInstance.__table__
ENTRIES = StoreDatasetEntry.__table__
BARS = StockMarketActivity.__table__

TAG = 'ZZP26'
GRANULARITY = Granularity.ONE_MINUTE
START = datetime(2001, 2, 5, 14, 30, tzinfo=UTC)
END = START + timedelta(minutes=30)
EXPECTED_TIMESTAMPS = tuple(grid_timestamps(GRANULARITY, START, END))

# created_at is moved back this far, so the instance watcher (PENDING older than 30 s) re-sends at its next pass.
BACKDATE = timedelta(hours=1)
# The watcher passes every 15 s (INSTANCE_WATCH_INTERVAL_SECONDS); a pass sends the rows one by one.
WATCHER_PICKUP_S = 120.0
POLL_S = 2.0
# Attempts the Alpaca handle runs at once: ExecutorSettings' default, which the handle does not override.
WORKERS = ExecutorSettings().workers

OPEN_STATES = (IngestRequestState.PENDING, IngestRequestState.QUEUED, IngestRequestState.NOT_DELIVERED)


@dataclass(frozen=True)
class Scenario:
    """One phased scenario's tag: its code names its symbols and its owner."""

    code: str

    @property
    def prefix(self) -> str:
        """Every symbol of this scenario starts with this: FakeRead's SLOW_ prefix, then the tag and the code."""
        return f'{SLOW_PREFIX}{TAG}{self.code.upper()}'

    def symbol(self, run_id: str, index: int) -> str:
        return f'{self.prefix}{run_id.upper()}{index:02d}'

    def owner(self, run_id: str) -> str:
        return f'p26-phase-{self.code.lower()}-{run_id}'

    def run_id_of(self, symbol: str) -> str:
        return symbol[len(self.prefix) : len(self.prefix) + 8].lower()


@dataclass(frozen=True)
class PhaseRun:
    """What phase B found of phase A: the run id, the owner and the request ids, oldest first."""

    scenario: Scenario
    run_id: str
    owner: str
    request_ids: tuple[uuid.UUID, ...]
    seeded_at: datetime


# ---------------------------------------------------------------------------------------------
# Reads.


def scenario_requests(pg_engine: Engine, scenario: Scenario) -> list[sa.Row]:
    """Every request row carrying the scenario's symbol prefix, whatever run wrote it."""
    with pg_engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(REQUESTS)
                .where(REQUESTS.c.asset_symbol.startswith(scenario.prefix, autoescape=True))
                .order_by(REQUESTS.c.created_at, REQUESTS.c.asset_symbol)
            )
        )


def request_rows(pg_engine: Engine, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, sa.Row]:
    with pg_engine.connect() as conn:
        return {row.id: row for row in conn.execute(sa.select(REQUESTS).where(REQUESTS.c.id.in_(list(ids))))}


def attempt_rows(pg_engine: Engine, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[sa.Row]]:
    """The attempt rows (ingest_run with an attempt number) of each request, by attempt number."""
    found: dict[uuid.UUID, list[sa.Row]] = {request_id: [] for request_id in ids}
    with pg_engine.connect() as conn:
        rows = conn.execute(
            sa.select(RUNS)
            .where(RUNS.c.request_id.in_(list(ids)), RUNS.c.attempt_no.is_not(None))
            .order_by(RUNS.c.request_id, RUNS.c.attempt_no)
        )
        for row in rows:
            found[row.request_id].append(row)
    return found


def instances(pg_engine: Engine) -> list[sa.Row]:
    with pg_engine.connect() as conn:
        return list(conn.execute(sa.select(INSTANCES).order_by(INSTANCES.c.first_seen_at)))


def live_instance(pg_engine: Engine) -> sa.Row:
    """The one instance the store holds as live (not superseded). Fails when there is not exactly one."""
    live = [row for row in instances(pg_engine) if row.superseded_at is None]
    if len(live) != 1:
        pytest.fail(f'expected exactly one live ingest instance, found {len(live)}', pytrace=False)
    return live[0]


def data_store_connected_since(pg_engine: Engine) -> datetime:
    """The oldest backend_start of data_store's connections: every client backend not from this process's address.

    A restarted data_store holds only connections it opened after it started, so this is a lower bound of the
    restart. Fails when no such connection is visible (nothing to judge by).
    """
    with pg_engine.connect() as conn:
        since = conn.execute(
            sa.text(
                'SELECT min(backend_start) FROM pg_stat_activity '
                "WHERE datname = current_database() AND backend_type = 'client backend' "
                'AND client_addr IS NOT NULL AND client_addr IS DISTINCT FROM inet_client_addr()'
            )
        ).scalar_one()
    if since is None:
        pytest.fail(
            'no data_store connection is visible in pg_stat_activity, so its restart cannot be dated; is data_store up?',
            pytrace=False,
        )
    return since


def states_of(rows: Sequence[sa.Row]) -> dict[str, int]:
    return dict(sorted(Counter(row.state.value for row in rows).items()))


# ---------------------------------------------------------------------------------------------
# Phase A.


def sweep_stale(pg_engine: Engine, scenario: Scenario) -> int:
    """Delete what an earlier run of this scenario left, if all of it is finished; fail if any of it is still open.

    Returns:
        int: How many stale request rows were deleted.
    """
    stale = scenario_requests(pg_engine, scenario)
    still_open = [row for row in stale if row.state in OPEN_STATES]
    if still_open:
        pytest.fail(
            f'{len(still_open)} request(s) of an earlier {scenario.code} run are still open ({states_of(still_open)}): '
            'ingest may still hold them, and deleting them would leave its reports refused for ever. Run that '
            "scenario's phase B (which waits for them and cleans up), or wait and rerun this phase.",
            pytrace=False,
        )
    delete_scenario_rows(pg_engine, scenario, [row.id for row in stale])
    return len(stale)


def delete_scenario_rows(pg_engine: Engine, scenario: Scenario, request_ids: Sequence[uuid.UUID]) -> None:
    """Delete the attempt rows and request rows by id, and every entry with the scenario's prefix (bars cascade)."""
    with pg_engine.begin() as conn:
        if request_ids:
            conn.execute(sa.delete(RUNS).where(RUNS.c.request_id.in_(list(request_ids))))
            conn.execute(sa.delete(REQUESTS).where(REQUESTS.c.id.in_(list(request_ids))))
        conn.execute(sa.delete(ENTRIES).where(ENTRIES.c.asset_symbol.startswith(scenario.prefix, autoescape=True)))
    with pg_engine.connect() as conn:
        bars_left = conn.execute(
            sa.select(sa.func.count())
            .select_from(BARS)
            .where(BARS.c.asset_symbol.startswith(scenario.prefix, autoescape=True))
        ).scalar_one()
    assert bars_left == 0, f'{bars_left} bars with the {scenario.code} prefix survived the cleanup'


async def seed_pending(
    async_engine: AsyncEngine, pg_engine: Engine, scenario: Scenario, run_id: str, count: int
) -> list[uuid.UUID]:
    """Write COUNT POST-shaped PENDING bars requests through the store's crud, then backdate them for the watcher."""
    owner = scenario.owner(run_id)
    ids: list[uuid.UUID] = []
    for index in range(count):
        path = StoreAssetDatasetPath(
            asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol=scenario.symbol(run_id, index)
        )
        body = StoreAssetDatasetBody(
            owner=owner, source=DataSource.ALPACA_API, granularity=GRANULARITY, start=START, end=END
        )
        new: NewIngestRequest = new_bars_request(path, body, trigger=IngestRunTrigger.REQUEST, client_label=owner)
        async with AsyncSession(async_engine, expire_on_commit=False) as db:
            record = await create_request(db, new)
        ids.append(record.id)
    with pg_engine.begin() as conn:
        moved = conn.execute(
            sa.update(REQUESTS)
            .where(REQUESTS.c.id.in_(ids), REQUESTS.c.state == IngestRequestState.PENDING)
            .values(created_at=sa.func.now() - BACKDATE)
        ).rowcount
    assert moved == count, f'backdated {moved} of the {count} seeded requests; some left PENDING before the update'
    return ids


def wait_until_sent(pg_engine: Engine, ids: Sequence[uuid.UUID], timeout_s: float = WATCHER_PICKUP_S) -> list[sa.Row]:
    """Poll until no seeded request is PENDING (the watcher sent every one); fail with the states at the timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        rows = list(request_rows(pg_engine, ids).values())
        if all(row.state is not IngestRequestState.PENDING for row in rows):
            return rows
        if time.monotonic() >= deadline:
            pytest.fail(
                f'the instance watcher did not send the seeded requests within {timeout_s:.0f} s: {states_of(rows)}. '
                'Is INSTANCE_WATCH_ENABLED off, or INSTANCE_WATCH_DRY_RUN on, on the test stack?',
                pytrace=False,
            )
        time.sleep(POLL_S)


def assert_left_open(rows: Sequence[sa.Row], *, at_least: int, scenario: Scenario) -> sa.Row:
    """Phase A's own verdict: every request was sent to ONE live instance, and AT_LEAST are still open in ingest.

    Returns:
        sa.Row: Any one of the rows (they share the instance).
    """
    holders = {row.instance_id for row in rows}
    assert len(holders) == 1 and None not in holders, f'the seeded requests are held by {len(holders)} instances'
    failed = [row for row in rows if row.state is IngestRequestState.FAILED]
    assert not failed, f'{len(failed)} seeded request(s) FAILED in phase A: {[row.failure_cause for row in failed]}'
    still_open = [row for row in rows if row.state in OPEN_STATES]
    assert len(still_open) >= at_least, (
        f'only {len(still_open)} of {len(rows)} {scenario.code} requests are still open in ingest at the end of phase A '
        f'(need {at_least}): the queue drained too soon for the disturbance to meet any work. Rerun phase A.'
    )
    return rows[0]


# ---------------------------------------------------------------------------------------------
# Phase B.


def phase_a_run(pg_engine: Engine, scenario: Scenario, phase_a_file: str) -> PhaseRun:
    """Find phase A's rows; fail loudly when phase A never ran, or when two runs' rows are present."""
    rows = scenario_requests(pg_engine, scenario)
    if not rows:
        pytest.fail(
            f'no {scenario.code} request rows: phase A never ran (or an earlier phase B already cleaned up). '
            f'Run {phase_a_file} first, then the orchestrator step, then this phase.',
            pytrace=False,
        )
    run_ids = sorted({scenario.run_id_of(row.asset_symbol) for row in rows})
    if len(run_ids) != 1:
        pytest.fail(f'{scenario.code} rows of {len(run_ids)} phase A runs are present; rerun phase A', pytrace=False)
    run_id = run_ids[0]
    return PhaseRun(
        scenario=scenario,
        run_id=run_id,
        owner=scenario.owner(run_id),
        request_ids=tuple(row.id for row in rows),
        seeded_at=min(row.created_at for row in rows) + BACKDATE,
    )


def wait_until_finished(pg_engine: Engine, run: PhaseRun, timeout_s: float) -> dict[uuid.UUID, sa.Row]:
    """Poll until every request of the run is finished; fail with the states at the timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        rows = request_rows(pg_engine, run.request_ids)
        if all(row.state not in OPEN_STATES for row in rows.values()):
            return rows
        if time.monotonic() >= deadline:
            pytest.fail(
                f'{run.scenario.code}: not every request finished within {timeout_s:.0f} s: '
                f'{states_of(list(rows.values()))}. Its rows are left for a rerun of this phase.',
                pytrace=False,
            )
        time.sleep(POLL_S)


def assert_each_reported_once(requests: dict[uuid.UUID, sa.Row], attempts: dict[uuid.UUID, list[sa.Row]]) -> None:
    """Every request SUCCEEDED, each of its reports counted exactly once.

    Attempt numbers 1..k without a gap or a repeat, one SUCCEEDED row and it the last, no FAILED row (a report that
    never reached the store is not a failure), the durable count equal to the rows, and the request's spend equal to
    the sum of its attempt rows' (each report counted once, however often it was redelivered).
    """
    problems: list[str] = []
    for request_id, request in requests.items():
        rows = attempts[request_id]
        numbers = [row.attempt_no for row in rows]
        states = [row.state for row in rows]
        where = f'request {request_id}'
        if request.state is not IngestRequestState.SUCCEEDED or request.failure_cause is not None:
            problems.append(f'{where} finished {request.state} cause {request.failure_cause}')
        if numbers != list(range(1, len(rows) + 1)):
            problems.append(f'{where} has attempt numbers {numbers}')
        if states.count(IngestRunState.SUCCEEDED) != 1 or (states and states[-1] is not IngestRunState.SUCCEEDED):
            problems.append(f'{where} has attempt states {[state.value for state in states]}')
        if IngestRunState.FAILED in states:
            problems.append(f'{where} counted a FAILED attempt')
        if request.attempts != len(rows):
            problems.append(f'{where} counts {request.attempts} attempts over {len(rows)} attempt rows')
        calls = sum(row.vendor_calls or 0 for row in rows)
        pages = sum(row.vendor_pages or 0 for row in rows)
        if (request.spent_calls, request.spent_pages) != (calls, pages) or calls < len(rows):
            problems.append(
                f'{where} spent calls/pages {request.spent_calls}/{request.spent_pages}, attempt rows {calls}/{pages}'
            )
        if request.spend_unknown_attempts != 0:
            problems.append(f'{where} has {request.spend_unknown_attempts} attempts of unknown spend')
        served = [row for row in rows if row.state is IngestRunState.SUCCEEDED]
        if served and (served[0].served_start is None or served[0].bars_written != len(EXPECTED_TIMESTAMPS)):
            problems.append(
                f'{where} success wrote {served[0].bars_written} bars, served from {served[0].served_start}'
            )
    assert not problems, f'{len(problems)} problem(s), first 10: {problems[:10]}'


def assert_one_entry_each(pg_engine: Engine, run: PhaseRun, requests: dict[uuid.UUID, sa.Row]) -> None:
    """One entry per symbol, the request's own, owned by the run's owner, holding exactly the generator's instants."""
    problems: list[str] = []
    with pg_engine.connect() as conn:
        for request in requests.values():
            entries = list(conn.execute(sa.select(ENTRIES).where(ENTRIES.c.asset_symbol == request.asset_symbol)))
            if len(entries) != 1:
                problems.append(f'{len(entries)} entries for request {request.id}')
                continue
            entry = entries[0]
            if entry.id != request.dataset_id or entry.owner != run.owner:
                problems.append(f'request {request.id} names dataset {request.dataset_id}, the entry is {entry.id}')
            stamps = list(
                conn.execute(
                    sa.select(BARS.c.timestamp).where(BARS.c.dataset_id == entry.id).order_by(BARS.c.timestamp)
                ).scalars()
            )
            if tuple(stamps) != EXPECTED_TIMESTAMPS:
                problems.append(f'entry {entry.id} holds {len(stamps)} bars, expected {len(EXPECTED_TIMESTAMPS)}')
    assert not problems, f'{len(problems)} problem(s), first 10: {problems[:10]}'


# HOW LONG A data_store OUTAGE MUST LAST BEFORE A DELIVERY CAN FAIL (tj-7zjov1.350 F-A). Ingest delivers each report
# in its own task (data/ingest/app/scheduler/executor.py, __deliver_until_acknowledged) through
# GrpcStoreDeliveryClient, which ingest builds with the default deadline (data/ingest/app/app_depends.py, no
# deadline_s). Each DeliverAttempt call carries timeout=DEFAULT_DELIVERY_DEADLINE_S (60 s) AND wait_for_ready=True
# (common/rpc/clients/store_delivery.py): a call made while the store is down WAITS for it, and fails only when the
# deadline runs out first. So an outage shorter than the deadline fails no delivery at all: the blocked first call
# succeeds when the store returns, the redelivery branch is never taken, and a mutant that abandons a failed delivery
# (M3r) survives. That is what the stack run showed: a 66 s outage, M3r survived with no redelivery logged; a 125 s
# outage, M3r red.
# The arithmetic. An attempt counted as crossing (outage_evidence) was BEGUN before last_before; its work is one slow
# delay (DEFAULT_SLOW_DELAY_SECONDS, 8 s, FAKE_READ_SLOW_SECONDS unset on S3), after which its delivery's first call
# starts at once (its own task). That call fails by its deadline no later than
#     last_before + slow delay + deadline  =  last_before + 8 + 60  =  last_before + 68 s,
# and the store's gRPC server is not up before up_at (its lifespan opens Postgres connections first). So when
# up_at - last_before exceeds 68 s, every crosser's first call failed and its report reached the store only by a
# redelivery. OUTAGE_MARGIN_S covers what the bound leaves out: the attempt's own overhead beyond the fake's sleep,
# the hand-off to the delivery task, the channel's reconnect backoff (about 2.4 s), and the commit-time dating of
# last_before. MIN_OUTAGE_S = 60 + 8 + 30 = 98 s. Read from the constants, so a changed deadline moves it.
OUTAGE_MARGIN_S = 30.0
MIN_OUTAGE_S = DEFAULT_DELIVERY_DEADLINE_S + DEFAULT_SLOW_DELAY_SECONDS + OUTAGE_MARGIN_S


@dataclass(frozen=True)
class OutageEvidence:
    """What the database shows of a data_store outage that phase B judges (S3; S5-B reads the same rows inline).

    up_at is when the store came back (data_store_connected_since). The stop leaves no row, so last_before, the last
    delivery committed before up_at, stands in for it (the run's seeded_at when nothing was delivered before it): the
    stop followed it by at most one slow delay. crossed holds the requests whose served attempt was BEGUN before
    last_before, so before the stop, and DELIVERED at or after up_at. A crosser proves a redelivery only when the
    outage is at least MIN_OUTAGE_S (assert_outage_long_enough): under it, its first call may simply have waited.
    """

    last_before: datetime
    up_at: datetime
    crossed: tuple[uuid.UUID, ...]

    @property
    def outage_s(self) -> float:
        return (self.up_at - self.last_before).total_seconds()


def outage_evidence(
    requests: dict[uuid.UUID, sa.Row], attempts: dict[uuid.UUID, list[sa.Row]], up_at: datetime, seeded_at: datetime
) -> OutageEvidence:
    """Date the outage from the request rows and name the attempts held across it.

    The served attempt is a request's last attempt row (its SUCCEEDED one, once assert_each_reported_once held). An
    attempt counts only once its request is finished: one still open has not been delivered after the outage.
    When the outage is at least MIN_OUTAGE_S, a crosser's first delivery call ran out its deadline before the store
    came back (the arithmetic at MIN_OUTAGE_S), so its report reaching the store after up_at is a delivery that failed
    and was redelivered. Under it, a crosser may be a first call that waited (wait_for_ready) and succeeded.
    """
    delivered_before = [
        request.finished_at
        for request in requests.values()
        if request.finished_at is not None and request.finished_at < up_at
    ]
    last_before = max(delivered_before) if delivered_before else seeded_at
    crossed = tuple(
        request_id
        for request_id, rows in attempts.items()
        if rows
        and requests[request_id].finished_at is not None
        and rows[-1].started_at < last_before
        and requests[request_id].finished_at >= up_at
    )
    return OutageEvidence(last_before=last_before, up_at=up_at, crossed=crossed)


def assert_outage_long_enough(evidence: OutageEvidence, minimum_s: float = MIN_OUTAGE_S) -> None:
    """Fail, as 'did not exercise', when the outage was too short for any delivery to fail (MIN_OUTAGE_S).

    Raises:
        AssertionError: The outage, dated from the database, is under minimum_s; the run proves nothing.
    """
    assert evidence.outage_s >= minimum_s, (
        f'data_store was down about {evidence.outage_s:.0f} s (last delivery {evidence.last_before}, back at '
        f'{evidence.up_at}), under {minimum_s:.0f} s (the {DEFAULT_DELIVERY_DEADLINE_S:.0f} s delivery deadline, '
        f'which a call waits out for a stopped store, plus the {DEFAULT_SLOW_DELAY_SECONDS:.0f} s slow delay and a '
        f'{OUTAGE_MARGIN_S:.0f} s margin): no delivery need have failed. Rerun from phase A with a longer wait '
        'between test_stop and test_start.'
    )


def cleanup_if_finished(pg_engine: Engine, run: PhaseRun) -> None:
    """Delete the run's rows when every request is finished; otherwise leave them for ingest and a rerun."""
    rows = request_rows(pg_engine, run.request_ids)
    if any(row.state in OPEN_STATES for row in rows.values()):
        return
    delete_scenario_rows(pg_engine, run.scenario, run.request_ids)
