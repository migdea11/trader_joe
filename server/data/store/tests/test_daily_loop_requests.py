"""The DAILY loop on the push path: write and submit a request per owed dataset, and do not wait.

The DAILY loop on the push path (P35, tj-7zjov1.28): it writes and submits a request per owed dataset and does not wait.
Over SQLite, through the real submit path and the real delivery intake.

THE DESIGN: the bead's acceptance (the daily loop writes the request row before sending, then submits; it sets ready_by
to the next market open from the store calendar; the Running facet reads open requests), architect ruling D and note H
(DAILY requests are written trigger SCHEDULE with dataset_id set at request time; the first attempt row is SCHEDULE and
later ones RETRY; finished_scheduled_runs judges by the REQUEST's trigger, since a scheduled request's last attempt row
can be RETRY; a mutation dropping the trigger from either writer must turn a test red), ruling A (the refresh never
writes a lifecycle field), and the store CLAUDE.md DAILY refresh section (a dataset with an open request is not owed
again).

The pass's planning (what is owed, the tail, the order, the cap) is test_daily_refresh*.py's, another validator's files;
here the pass is cut at its seams to show only what P35 changed: the request it writes, its ready_by, and that it does
not wait.
"""

import types
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app import daily_refresh
from data.store.app.daily_refresh import SCHEDULER_ACTOR, DailyRefresher, _Planned, next_market_open
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.crud.ingest_run import (
    dataset_has_running_run,
    finished_scheduled_runs,
    running_dataset_ids,
)
from data.store.app.database.models.ingest_request import (
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRunState, IngestRunTrigger
from data.store.app.ingest import data_action_request
from data.store.app.trading_calendar import CalendarRangeError, calendar_for_source
from data.store.tests.delivery_reports import FIRST_CALL, report
from data.store.tests.request_path_harness import INSTANCE_A, RUNS, START, FakeSubmit, Harness, make_engine
from schemas.data_ingest.delivery import DeliveryErrorClass, DeliveryOutcome
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

PATH = StoreAssetDatasetPath(asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol='VFV')
TAIL = START + timedelta(days=20)
READY_BY = datetime(2026, 3, 9, 13, 30, tzinfo=UTC)
CALENDAR = calendar_for_source(DataSource.ALPACA_API)


@pytest_asyncio.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    engine = await make_engine(monkeypatch)
    built = Harness(engine, monkeypatch)
    monkeypatch.setattr(daily_refresh, 'own_session', built.session)
    try:
        yield built
    finally:
        await engine.dispose()


def _planned(dataset_id: uuid.UUID, *, collect: bool | None = None) -> _Planned:
    """What the pass hands _refresh: the entry's own body (its lifecycle fields included) and the missing tail."""
    body = StoreAssetDatasetBody(
        owner='the-entrys-owner',
        source=DataSource.ALPACA_API,
        feed=Feed.IEX,
        granularity=Granularity.ONE_DAY,
        start=START,
        update_type=UpdateType.DAILY,
        expiry_type=ExpiryType.ROLLING,
        expiry=START + timedelta(days=500),
        collect=collect,
    )
    return _Planned(types.SimpleNamespace(id=dataset_id), PATH, body, TAIL, None)


async def _refresh(harness: Harness, client: FakeSubmit, dataset_id: uuid.UUID, **kwargs) -> None:
    await DailyRefresher(client)._refresh(_planned(dataset_id, **kwargs), READY_BY)


# --------------------------------------------------------------------------------------------- the request it writes


@pytest.mark.parametrize('collect', [None, False, True])
async def test_h_the_daily_loop_writes_a_schedule_request_naming_its_dataset_with_no_lifecycle_field(harness, collect):
    """The DAILY request: SCHEDULE, its dataset named, its ready_by, and no lifecycle field.

    Trigger SCHEDULE, dataset_id set at request time, actor scheduler, the missing tail's start, ready_by the next
    market open; never expiry, expiry type or collect, whatever the entry's body holds.
    """
    dataset_id = await harness.entry()
    client = FakeSubmit()
    seen_at_send: list = []

    async def look(request):
        seen_at_send.append(await harness.request_row(uuid.UUID(request.request_id)))

    client.on_submit = look

    await _refresh(harness, client, dataset_id, collect=collect)

    (row,) = seen_at_send
    assert row.state is IngestRequestState.PENDING, 'written and committed before the Submit'
    assert (row.trigger, row.dataset_id, row.client_label) == (IngestRunTrigger.SCHEDULE, dataset_id, SCHEDULER_ACTOR)
    assert (row.requested_start, row.ready_by) == (TAIL, READY_BY)
    assert (row.expiry, row.expiry_type, row.collect) == (None, None, None)
    (sent,) = client.sent
    assert (sent.ready_by, sent.bars.start, sent.recovered, sent.attempt_count) == (READY_BY, TAIL, False, 0)


async def test_the_daily_loop_submits_and_does_not_wait(harness, monkeypatch):
    """The result is pushed to the intake: _refresh returns with the request QUEUED, and never calls the waiter."""
    dataset_id = await harness.entry()

    async def never(*args, **kwargs):
        raise AssertionError('the DAILY loop must not wait for the verdict')

    monkeypatch.setattr(data_action_request, 'await_verdict', never)

    await _refresh(harness, FakeSubmit(), dataset_id)

    (row,) = await harness.requests()
    assert (row.state, row.instance_id, row.finished_at) == (IngestRequestState.QUEUED, INSTANCE_A, None)


async def test_the_refresh_runs_no_own_overlap_check(harness, monkeypatch):
    """The overlap refusal is the POST's; a refresh names its own entry, which its window always overlaps."""
    dataset_id = await harness.entry()

    async def never(*args, **kwargs):
        raise AssertionError('a refresh must not run the own-overlap check')

    monkeypatch.setattr(data_action_request, '_check_overlap', never)

    await _refresh(harness, FakeSubmit(), dataset_id)

    assert len(await harness.requests()) == 1


async def test_h_its_first_attempt_row_is_schedule_and_later_ones_retry_and_the_entry_is_only_touched(harness):
    """Through the real path: the DAILY loop's request, then ingest's reports to the intake."""
    dataset_id = await harness.entry()
    await _refresh(harness, FakeSubmit(), dataset_id)
    (row,) = await harness.requests()

    await harness.deliver(report(row.id, DeliveryOutcome.FAILED))
    await harness.deliver(report(row.id, DeliveryOutcome.FAILED, attempt_no=2))
    await harness.deliver(report(row.id, attempt_no=3))

    runs = await harness.runs(row.id)
    assert [run.trigger for run in runs] == [IngestRunTrigger.SCHEDULE, IngestRunTrigger.RETRY, IngestRunTrigger.RETRY]
    assert {run.dataset_id for run in runs} == {dataset_id}
    assert harness.upserts == [], 'a refresh never upserts the entry: only updated_at is written'
    assert (await harness.request_row(row.id)).state is IngestRequestState.SUCCEEDED


# --------------------------------------------------------------------------------------------- ready_by


@pytest.mark.parametrize(
    ('now', 'expected'),
    [
        # Saturday: the Monday open (US daylight time began on Sunday 8 March, so 09:30 New York is 13:30 UTC).
        (datetime(2026, 3, 7, 12, tzinfo=UTC), datetime(2026, 3, 9, 13, 30, tzinfo=UTC)),
        # A session day before its open: that day's open.
        (datetime(2026, 3, 10, 12, tzinfo=UTC), datetime(2026, 3, 10, 13, 30, tzinfo=UTC)),
        # During a session: the next session's open, never the one in progress.
        (datetime(2026, 3, 10, 15, tzinfo=UTC), datetime(2026, 3, 11, 13, 30, tzinfo=UTC)),
        # At the open exactly: that open has come, so the next one.
        (datetime(2026, 3, 10, 13, 30, tzinfo=UTC), datetime(2026, 3, 11, 13, 30, tzinfo=UTC)),
        # After a Thursday close before Good Friday (3 April 2026, no session): the Monday open.
        (datetime(2026, 4, 2, 21, tzinfo=UTC), datetime(2026, 4, 6, 13, 30, tzinfo=UTC)),
        # Winter time: 09:30 New York is 14:30 UTC.
        (datetime(2026, 1, 9, 22, tzinfo=UTC), datetime(2026, 1, 12, 14, 30, tzinfo=UTC)),
    ],
    ids=['weekend', 'before-open', 'in-session', 'at-open', 'holiday', 'winter'],
)
async def test_next_market_open_is_the_open_of_the_first_session_starting_after_now(now, expected):
    assert next_market_open(CALENDAR, now) == expected


async def test_next_market_open_is_none_when_the_calendar_cannot_name_one():
    class OutOfRange:
        def session_in_progress(self, now):
            raise CalendarRangeError('past the calendar end')

    assert next_market_open(OutOfRange(), datetime(2026, 3, 10, tzinfo=UTC)) is None


async def test_a_pass_sends_every_request_with_the_next_market_open_as_its_ready_by(harness, monkeypatch):
    """run_pass computes ready_by once from its clock and the source's calendar and hands it to every refresh."""
    now = datetime(2026, 3, 7, 12, tzinfo=UTC)
    planned = [_planned(uuid.uuid4()), _planned(uuid.uuid4())]
    handed: list = []

    async def tables_exist(session):
        return True

    async def nothing(*args, **kwargs):
        return []

    async def no_running(session):
        return frozenset()

    async def no_tails(owed, calendar):
        return {}

    async def refresh(self, plan, ready_by):
        handed.append((plan, ready_by))

    monkeypatch.setattr(daily_refresh, '_tables_exist', tables_exist)
    monkeypatch.setattr(daily_refresh, 'list_catalog', nothing)
    monkeypatch.setattr(daily_refresh, 'running_dataset_ids', no_running)
    monkeypatch.setattr(daily_refresh, 'finished_scheduled_runs', nothing)
    monkeypatch.setattr(daily_refresh, '_read_placeholder_tails', no_tails)
    monkeypatch.setattr(DailyRefresher, '_plan', lambda self, *args: ((planned, []), 0))
    monkeypatch.setattr(DailyRefresher, '_refresh', refresh)

    result = await DailyRefresher(FakeSubmit(), clock=lambda: now).run_pass(DataSource.ALPACA_API, CALENDAR)

    assert result.refreshed == 2
    assert handed == [(plan, datetime(2026, 3, 9, 13, 30, tzinfo=UTC)) for plan in planned]


# --------------------------------------------------------------------------------------------- what the pass reads


async def _scheduled_request(harness: Harness, dataset_id: uuid.UUID | None, trigger: IngestRunTrigger) -> uuid.UUID:
    async with AsyncSession(harness.engine, expire_on_commit=False) as db:
        record = await request_crud.create_request(
            db,
            request_crud.NewIngestRequest(
                kind=IngestRequestKind.BARS,
                source=DataSource.ALPACA_API,
                client_label=SCHEDULER_ACTOR,
                dataset_id=dataset_id,
                asset_symbol='VFV',
                asset_type=AssetType.STOCK,
                data_types=(DataType.MARKET_ACTIVITY.value,),
                granularity=Granularity.ONE_DAY,
                update_type=UpdateType.DAILY,
                feed=Feed.IEX,
                requested_start=TAIL,
                trigger=trigger,
                expiry_type=ExpiryType.ROLLING,
            ),
        )
    await harness.set_request(record.id, state=IngestRequestState.QUEUED, instance_id=INSTANCE_A)
    return record.id


async def test_d_h_finished_scheduled_runs_judges_by_the_requests_trigger_when_the_last_attempt_row_is_retry(harness):
    """A run is the refresh's by its request's trigger, not by its own.

    A scheduled request whose attempts are SCHEDULE then RETRY rows: both are the refresh's, the RETRY one included,
    and each carries its request's failure cause. A POST's attempt rows are not, though a later one is a RETRY row
    too. A legacy row with no request is judged by its own trigger.
    """
    dataset_id = await harness.entry()
    scheduled = await _scheduled_request(harness, dataset_id, IngestRunTrigger.SCHEDULE)
    await harness.deliver(report(scheduled, DeliveryOutcome.FAILED))
    await harness.deliver(
        report(scheduled, DeliveryOutcome.FAILED, attempt_no=2, error_class=DeliveryErrorClass.PERMANENT)
    )
    posted = await _scheduled_request(harness, dataset_id, IngestRunTrigger.REQUEST)
    await harness.deliver(report(posted, DeliveryOutcome.FAILED))
    await harness.deliver(report(posted, attempt_no=2))
    legacy = await _scheduled_request(harness, dataset_id, IngestRunTrigger.REQUEST)
    await harness.deliver(report(legacy))
    async with AsyncSession(harness.engine) as db:
        await db.execute(
            RUNS.update().where(RUNS.c.request_id == legacy).values(request_id=None, trigger=IngestRunTrigger.SCHEDULE)
        )
        await db.commit()

    async with AsyncSession(harness.engine) as db:
        runs = await finished_scheduled_runs(db, DataSource.ALPACA_API, FIRST_CALL - timedelta(days=1))

    by_request = sorted(((run.request_id, run.trigger, run.request_failure_cause) for run in runs), key=str)
    assert by_request == sorted(
        [
            (scheduled, IngestRunTrigger.SCHEDULE, IngestRequestFailureCause.PERMANENT.value),
            (scheduled, IngestRunTrigger.RETRY, IngestRequestFailureCause.PERMANENT.value),
            (None, IngestRunTrigger.SCHEDULE, None),
        ],
        key=str,
    )


async def test_d_a_permanent_request_failure_makes_the_last_scheduled_attempt_unproductive(harness):
    """The refresh reads a permanent refusal off the REQUEST's cause, since the attempt row carries only a sentence."""
    dataset_id = await harness.entry()
    request_id = await _scheduled_request(harness, dataset_id, IngestRunTrigger.SCHEDULE)
    await harness.deliver(report(request_id, DeliveryOutcome.FAILED, error_class=DeliveryErrorClass.PERMANENT))
    async with AsyncSession(harness.engine) as db:
        (run,) = await finished_scheduled_runs(db, DataSource.ALPACA_API, FIRST_CALL - timedelta(days=1))

    assert run.state is IngestRunState.FAILED and run.error_reason is None
    assert daily_refresh._unproductive(run, TAIL) is True


@pytest.mark.parametrize(
    ('state', 'running'),
    [
        (IngestRequestState.PENDING, True),
        (IngestRequestState.QUEUED, True),
        (IngestRequestState.NOT_DELIVERED, True),
        (IngestRequestState.SUCCEEDED, False),
        (IngestRequestState.EMPTY, False),
        (IngestRequestState.FAILED, False),
    ],
)
async def test_a_dataset_with_an_open_request_is_running_so_the_loop_does_not_owe_it_again(harness, state, running):
    """The Running facet and the refresh's skip read OPEN REQUESTS (PENDING, QUEUED, NOT_DELIVERED) with a dataset."""
    dataset_id = await harness.entry()
    request_id = await _scheduled_request(harness, dataset_id, IngestRunTrigger.SCHEDULE)
    await harness.set_request(request_id, state=state)
    # An open request with no dataset yet (a POST before its first delivered attempt) names none.
    await _scheduled_request(harness, None, IngestRunTrigger.REQUEST)

    async with AsyncSession(harness.engine) as db:
        ids = await running_dataset_ids(db)
        one = await dataset_has_running_run(db, dataset_id)

    assert ids == (frozenset({dataset_id}) if running else frozenset())
    assert one is running
