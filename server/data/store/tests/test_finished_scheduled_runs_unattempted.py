"""finished_scheduled_runs reads refused SCHEDULE requests with no attempt row into the refresh history (P35f).

THE DESIGN is tj-7zjov1.160 (the architect's gate defect on P35 tj-7zjov1.28): a Submit ingest refuses for good writes
no attempt row, because no vendor call was made, and finishes its request FAILED with cause PERMANENT. The DAILY
refresh's history must still see it, or the next pass re-sends the refused tape. So finished_scheduled_runs ALSO returns
every ingest_request row where trigger is SCHEDULE, kind is BARS, source is the source, state is FAILED, created_at is
at or after since, and NOT EXISTS an ingest_run row naming it, mapped to an IngestRunRecord: id and request_id the
request id, the labels from the request, data_type the one element of data_types, trigger SCHEDULE, actor the
client_label, state FAILED, bars_written 0, no error fields, started_at the request's created_at, finished_at its
finished_at, request_failure_cause its failure_cause. A row missing a label field the record needs is left out, never
given a sentinel. Merged with the attempt rows, ordered by started_at then id.

WHAT TIER THIS IS. The real read over real tables in SQLite (request_path_harness: data_types as JSON, timestamptz read
back aware). Request rows are inserted directly so every filter can be varied alone, one field per case; the attempt
rows are written by the real delivery intake. That the same read runs on Postgres (ARRAY data_types, the enum types) is
tests/system/test_daily_refresh_pass.py's test_a_refused_tape_is_skipped_by_the_next_pass_before_the_close_on_postgres.
That the refresh acts on it is test_daily_refresh.py's test_a_submit_refused_for_good_is_skipped_until_the_next_close.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from data.store.app.daily_refresh import SCHEDULER_ACTOR
from data.store.app.database.crud.ingest_run import IngestRunRecord, finished_scheduled_runs
from data.store.app.database.models.ingest_request import (
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRunState, IngestRunTrigger
from data.store.tests.delivery_reports import report
from data.store.tests.request_path_harness import INSTANCE_A, REQUESTS, RUNS, Harness, make_engine
from schemas.data_ingest.delivery import DeliveryErrorClass, DeliveryOutcome
from schemas.data_ingest.submit import AssetListStatus


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

SOURCE = DataSource.ALPACA_API
# The read's lower bound, and a request created an hour inside it and finished a minute later. Distinct instants, so
# started_at = created_at and finished_at = finished_at cannot pass by swapping the two.
SINCE = datetime(2026, 3, 1, tzinfo=UTC)
CREATED = SINCE + timedelta(hours=1)
FINISHED = CREATED + timedelta(minutes=1)
TAIL_START = datetime(2026, 2, 20, 5, tzinfo=UTC)
TAIL_END = datetime(2026, 2, 27, 5, tzinfo=UTC)
# Distinctive, so a record carrying it proves it came from the row and not a default.
LABEL = 'refused-request-label-3e8a'


@pytest_asyncio.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    engine = await make_engine(monkeypatch)
    try:
        yield Harness(engine, monkeypatch)
    finally:
        await engine.dispose()


def _row(**overrides: Any) -> dict[str, Any]:
    """A refused DAILY request as the refresh writes and the submit path finishes it: every filter satisfied."""
    return {
        'id': uuid.uuid4(),
        'kind': IngestRequestKind.BARS,
        'source': SOURCE,
        'dataset_id': uuid.uuid4(),
        'asset_symbol': 'VFV',
        'asset_type': AssetType.STOCK,
        'data_types': [DataType.MARKET_ACTIVITY.value],
        'granularity': Granularity.ONE_HOUR,
        'update_type': UpdateType.DAILY.value,
        'feed': Feed.SIP,
        'requested_start': TAIL_START,
        'requested_end': TAIL_END,
        'client_label': LABEL,
        'trigger': IngestRunTrigger.SCHEDULE,
        'state': IngestRequestState.FAILED,
        'failure_cause': IngestRequestFailureCause.PERMANENT.value,
        'failure_detail': 'the feed asked for is not served',
        'instance_id': INSTANCE_A,
        'created_at': CREATED,
        'finished_at': FINISHED,
        **overrides,
    }


async def _insert(harness: Harness, *rows: dict[str, Any]) -> None:
    async with AsyncSession(harness.engine) as db:
        for row in rows:
            await db.execute(sa.insert(REQUESTS).values(**row))
        await db.commit()


async def _read(harness: Harness, source: DataSource = SOURCE, since: datetime = SINCE) -> list[IngestRunRecord]:
    async with AsyncSession(harness.engine) as db:
        return await finished_scheduled_runs(db, source, since)


# --------------------------------------------------------------------------------------------- the mapping


async def test_a_refused_schedule_request_with_no_attempt_row_is_read_as_one_failed_record(harness):
    """Every field of the record, against the bead's mapping, written out."""
    row = _row()
    await _insert(harness, row)

    (record,) = await _read(harness)

    assert record == IngestRunRecord(
        id=row['id'],
        dataset_id=row['dataset_id'],
        request_id=row['id'],
        trigger=IngestRunTrigger.SCHEDULE,
        actor=LABEL,
        asset_symbol='VFV',
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=SOURCE,
        granularity=Granularity.ONE_HOUR,
        update_type=UpdateType.DAILY,
        feed=Feed.SIP,
        requested_start=TAIL_START,
        requested_end=TAIL_END,
        state=IngestRunState.FAILED,
        bars_written=0,
        error_domain=None,
        error_reason=None,
        error_message=None,
        error_id=None,
        started_at=CREATED,
        finished_at=FINISHED,
        request_failure_cause=IngestRequestFailureCause.PERMANENT.value,
    )
    # Typed as the attempt rows' records are, so the refresh's comparisons do not depend on which read made it.
    assert type(record.update_type) is UpdateType and type(record.data_type) is DataType
    assert LABEL not in repr(record), 'the actor is the client label and is never in a repr'


@pytest.mark.parametrize(
    'cause',
    [IngestRequestFailureCause.EXPIRED, IngestRequestFailureCause.ATTEMPTS_EXHAUSTED, None],
    ids=['expired', 'exhausted', 'no-cause'],
)
async def test_the_record_carries_the_requests_own_failure_cause_whatever_it_is(harness, cause):
    """The cause is copied, not assumed PERMANENT: the refresh tells a permanent refusal from the rest by it."""
    await _insert(harness, _row(failure_cause=None if cause is None else cause.value))

    (record,) = await _read(harness)

    assert record.request_failure_cause == (None if cause is None else cause.value)
    assert record.state is IngestRunState.FAILED


async def test_an_open_ended_request_maps_its_null_end_and_feed(harness):
    """A NULL requested_end and a NULL feed (the deployment decides) are copied as None, not filled in."""
    await _insert(harness, _row(requested_end=None, feed=None))

    (record,) = await _read(harness)

    assert (record.requested_end, record.feed) == (None, None)


# --------------------------------------------------------------------------------------------- the filters


@pytest.mark.parametrize(
    'overrides',
    [
        pytest.param({'trigger': IngestRunTrigger.REQUEST}, id='a-POST-request'),
        pytest.param({'trigger': None}, id='a-NULL-trigger-reads-as-REQUEST'),
        pytest.param({'trigger': IngestRunTrigger.RETRY}, id='a-RETRY-trigger'),
        *[
            pytest.param({'state': state, 'finished_at': None if state.is_open else FINISHED}, id=f'state-{state}')
            for state in IngestRequestState
            if state is not IngestRequestState.FAILED
        ],
        # An asset-list row names its list (the table's CHECK since tj-7zjov1.30), so the kind case carries one.
        pytest.param(
            {'kind': IngestRequestKind.ASSET_LIST, 'asset_list_status': AssetListStatus.ACTIVE}, id='kind-ASSET_LIST'
        ),
        pytest.param({'kind': IngestRequestKind.CORPORATE_ACTIONS}, id='kind-CORPORATE_ACTIONS'),
        pytest.param({'source': DataSource.IB_API}, id='another-source'),
        pytest.param({'created_at': SINCE - timedelta(microseconds=1)}, id='created-just-before-since'),
    ],
)
async def test_a_request_failing_one_filter_is_not_read(harness, overrides):
    """Each case breaks exactly one filter of a row that is otherwise read; a kept control row proves the read ran.

    The non-BARS kinds keep their bars labels on purpose, so the KIND filter, not the label guard, is what leaves them
    out.
    """
    control = _row()
    await _insert(harness, control, _row(**overrides))

    records = await _read(harness)

    assert [record.request_id for record in records] == [control['id']]


async def test_a_request_created_exactly_at_since_is_read(harness):
    """created_at >= since: the bound is inclusive."""
    row = _row(created_at=SINCE)
    await _insert(harness, row)

    assert [record.id for record in await _read(harness)] == [row['id']]


@pytest.mark.parametrize(
    'overrides',
    [
        pytest.param({'asset_symbol': None}, id='no-symbol'),
        pytest.param({'asset_type': None}, id='no-asset-type'),
        pytest.param({'granularity': None}, id='no-granularity'),
        pytest.param({'update_type': None}, id='no-update-type'),
        pytest.param({'data_types': None}, id='no-data-types'),
        pytest.param({'data_types': []}, id='empty-data-types'),
        pytest.param(
            {'data_types': [DataType.MARKET_ACTIVITY.value, DataType.MARKET_ACTIVITY.value]}, id='two-data-types'
        ),
    ],
)
async def test_a_request_missing_a_label_the_record_needs_is_left_out_never_given_a_sentinel(harness, overrides):
    """Incomplete rows are dropped; the complete one beside them is still read, and the read does not raise."""
    control = _row()
    await _insert(harness, control, _row(**overrides))

    records = await _read(harness)

    assert [record.request_id for record in records] == [control['id']]


# --------------------------------------------------------------------------------------------- NOT EXISTS


async def _finished_with_an_attempt(harness: Harness) -> uuid.UUID:
    """A SCHEDULE request ingest accepted, whose one attempt the intake records as a PERMANENT failure.

    The intake finishes it FAILED / PERMANENT and writes the attempt row: every filter of the unattempted read holds,
    except the NOT EXISTS.
    """
    row = _row(state=IngestRequestState.QUEUED, failure_cause=None, failure_detail=None, finished_at=None)
    await _insert(harness, row)
    await harness.deliver(
        report(row['id'], DeliveryOutcome.FAILED, error_class=DeliveryErrorClass.PERMANENT, last_attempt=True)
    )
    finished = await harness.request_row(row['id'])
    assert (finished.state, finished.failure_cause) == (
        IngestRequestState.FAILED,
        IngestRequestFailureCause.PERMANENT.value,
    )
    assert len(await harness.runs(row['id'])) == 1
    return row['id']


async def test_a_request_with_an_attempt_row_is_read_once_from_its_attempt_row(harness):
    """The NOT EXISTS: the attempt row stands for the request, which is never read a second time from its own row."""
    request_id = await _finished_with_an_attempt(harness)
    (attempt,) = await harness.runs(request_id)

    records = await _read(harness, since=SINCE - timedelta(days=365))

    assert [(record.id, record.request_id) for record in records] == [(attempt.id, request_id)]
    assert records[0].id != request_id


async def test_an_attempt_row_of_another_request_does_not_hide_this_one(harness):
    """The NOT EXISTS is correlated on request_id = the request's id, not on any attempt row existing."""
    attempted = await _finished_with_an_attempt(harness)
    unattempted = _row()
    await _insert(harness, unattempted)

    records = await _read(harness, since=SINCE - timedelta(days=365))

    assert sorted((record.request_id, record.id == record.request_id) for record in records) == sorted(
        [(attempted, False), (unattempted['id'], True)]
    )


# --------------------------------------------------------------------------------------------- the order


async def test_attempt_rows_and_unattempted_requests_are_merged_by_started_at_then_id(harness):
    """One list, by started_at then id, whichever read each record came from.

    The attempt row starts at its vendor call. Around it: an unattempted request created an hour before it, one an
    hour after, and two created at the same instant later still, inserted in the reverse of their id order so the
    tie-break is the id and not the insert order.
    """
    attempted = await _finished_with_an_attempt(harness)
    (attempt,) = await harness.runs(attempted)
    called = attempt.started_at
    # A leading hex letter: SQLite gives a column declared UUID numeric affinity, so an all-digit hex would come back
    # as an integer.
    low, high = uuid.UUID('a0000000-0000-4000-8000-000000000001'), uuid.UUID('a0000000-0000-4000-8000-000000000002')
    before = _row(created_at=called - timedelta(hours=1))
    after = _row(created_at=called + timedelta(hours=1))
    tied_high = _row(id=high, created_at=called + timedelta(hours=2))
    tied_low = _row(id=low, created_at=called + timedelta(hours=2))
    await _insert(harness, after, tied_high, before, tied_low)

    records = await _read(harness, since=called - timedelta(days=1))

    assert [record.id for record in records] == [before['id'], attempt.id, after['id'], low, high]
    assert [record.started_at for record in records] == sorted(record.started_at for record in records)


async def test_an_unattempted_request_and_an_attempt_row_starting_together_are_ordered_by_id(harness):
    """The tie across the two reads is broken by id too, on both sides of the attempt row's id."""
    attempted = await _finished_with_an_attempt(harness)
    (attempt,) = await harness.runs(attempted)
    lower = uuid.UUID(int=attempt.id.int - 1)
    higher = uuid.UUID(int=attempt.id.int + 1)
    await _insert(
        harness, _row(id=higher, created_at=attempt.started_at), _row(id=lower, created_at=attempt.started_at)
    )

    records = await _read(harness, since=attempt.started_at - timedelta(days=1))

    assert [record.id for record in records] == [lower, attempt.id, higher]


async def test_scheduled_actor_rows_are_the_daily_loops_and_carry_its_label(harness):
    """A request the DAILY loop wrote carries the scheduler's label, the actor its attempt rows would carry."""
    await _insert(harness, _row(client_label=SCHEDULER_ACTOR))

    (record,) = await _read(harness)

    assert record.actor == SCHEDULER_ACTOR


async def test_the_attempt_rows_are_still_read_beside_the_unattempted_requests(harness):
    """The merge adds to the attempt rows' read; a RUNNING attempt row of a SCHEDULE request stays out of both."""
    attempted = await _finished_with_an_attempt(harness)
    running = _row(state=IngestRequestState.FAILED)
    await _insert(harness, running)
    async with AsyncSession(harness.engine) as db:
        (attempt,) = (await db.execute(sa.select(RUNS).where(RUNS.c.request_id == attempted))).all()
        copy = dict(attempt._mapping)
        copy.update(id=uuid.uuid4(), request_id=running['id'], state=IngestRunState.RUNNING, attempt_no=None)
        await db.execute(sa.insert(RUNS).values(**copy))
        await db.commit()

    records = await _read(harness, since=SINCE - timedelta(days=365))

    assert [record.request_id for record in records] == [attempted]


# --------------------------------------------------------------------------------------------- non-bars attempt rows


@pytest.mark.parametrize(
    'attempt',
    [
        pytest.param(
            {'outcome': DeliveryOutcome.FAILED, 'error_class': DeliveryErrorClass.PERMANENT, 'last_attempt': True},
            id='failed',
        ),
        pytest.param({'outcome': DeliveryOutcome.SUCCEEDED, 'parts': []}, id='succeeded-empty'),
    ],
)
async def test_a_scheduled_corporate_actions_attempt_row_never_enters_the_bars_history(harness, attempt):
    """tj-7zjov1.31: the refresh's history is bars attempts; a corporate-actions attempt row has no granularity.

    The corporate-actions request is SCHEDULE-triggered and its attempt row is written by the real intake (the symbol,
    and NULL for the other four labels), so every other filter of the attempt-row read holds for it. A bars attempt row
    beside it is the control that proves the read ran.
    """
    bars_request = await _finished_with_an_attempt(harness)
    actions_request = _row(
        kind=IngestRequestKind.CORPORATE_ACTIONS,
        dataset_id=None,
        asset_type=None,
        data_types=None,
        granularity=None,
        update_type=None,
        feed=None,
        state=IngestRequestState.QUEUED,
        failure_cause=None,
        failure_detail=None,
        finished_at=None,
    )
    await _insert(harness, actions_request)
    outcome = attempt.pop('outcome')
    await harness.deliver(report(actions_request['id'], outcome, kind=IngestRequestKind.CORPORATE_ACTIONS, **attempt))
    (run,) = await harness.runs(actions_request['id'])
    assert (run.trigger, run.granularity, run.asset_symbol) == (IngestRunTrigger.SCHEDULE, None, 'VFV')
    assert run.state is not IngestRunState.RUNNING

    records = await _read(harness, since=SINCE - timedelta(days=365))

    assert [record.request_id for record in records] == [bars_request]
