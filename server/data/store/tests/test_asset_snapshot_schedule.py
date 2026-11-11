"""The asset-list snapshot schedule (P47, tj-7zjov1.138): app/asset_snapshot_schedule.py, over SQLite.

THE DESIGN: P47's description (WORK items 1 to 6), the owner ruling on tj-7zjov1.117 (once at ingest start, then daily,
no manual step), the architect's P47 note of 23:33 (the request row names its list through new_asset_list_request; the
age rules read the newest NON-EMPTY snapshot, so a served empty list is asked for again), the architect's epic notes
(the source is a parameter: one loop per source in the calendar map, the cadence a per-source parameter), ADR tj-26jt9w
addenda 10, 11 and 17, and the store CLAUDE.md environment row ASSET_SNAPSHOT_ENABLED.

AS BUILT, THREE DEPARTURES FROM THE BEAD, pinned here for the architect gate to rule on:
  (1) the ingest-start trigger compares the probe's instance id with the last one the schedule handled (seeded at start
      from the newest unsuperseded instance in the database) instead of the watcher's first-sighting flag, because the
      intake and the submit path note a new instance before the watcher's probe does; with INSTANCE_WATCH_ENABLED off
      there is no ingest-start trigger, the daily loop still runs;
  (2) the lifespan wiring is app/app_depends.py (the lifespan's home), not app/main.py;
  (3) one added read, live_instance_id in crud/ingest_instance.py.
ASSUMPTION: an ingest start requests the active list for EVERY source in the cadence map (the probe does not say which
sources the instance serves).

The schedule runs on the request-path harness (request_path_harness.py) with the asset_list_snapshot table added and
the schedule's own_session pointed at it; requests go through the REAL new_asset_list_request, create_request,
open_asset_list_request_exists and submit_record, to FakeSubmit. Snapshots are written directly: the arm that writes
them is test_asset_list_arm.py's. The same schedule against Postgres is tests/system/test_asset_snapshot_schedule_pg.py.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource
from common.errors.vocabulary import ExogenousError, Reason
from common.scheduling import AfterTradingEvent, TradingEvent
from data.store.app import app_depends, asset_snapshot_schedule
from data.store.app.asset_snapshot_schedule import (
    DEFAULT_INACTIVE_MAX_AGE,
    ENABLED_ENV,
    AssetSnapshotLoops,
    AssetSnapshotSchedule,
    SnapshotCadence,
    default_cadences,
    is_enabled,
    start_asset_snapshot_loops,
    stop_asset_snapshot_loops,
)
from data.store.app.daily_refresh import SCHEDULER_ACTOR
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.crud.ingest_instance import live_instance_id
from data.store.app.database.models.asset_list import AssetListSnapshot
from data.store.app.database.models.ingest_request import IngestRequestKind, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRunTrigger
from data.store.app.ingest.instance_watch import InstanceWatcher
from data.store.app.trading_calendar import calendar_for_source
from data.store.tests.request_path_harness import (
    INSTANCE_A,
    INSTANCE_B,
    INSTANCES,
    REQUESTS,
    FakeSubmit,
    Harness,
    make_engine,
)
from schemas.data_ingest.submit import AssetListStatus


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

ALPACA = DataSource.ALPACA_API
# The fake second source: nothing in production schedules it; it shows the schedule keys everything by source.
SECOND = DataSource.IB_API
ACTIVE = AssetListStatus.ACTIVE
INACTIVE = AssetListStatus.INACTIVE
XNYS = calendar_for_source(ALPACA)
OFFSET = timedelta(minutes=30)
CADENCE = SnapshotCadence(daily_offset=OFFSET)

# Wednesday 2026-02-04 (EST): XNYS closes 21:00 UTC, so the day's due time is 21:30 UTC.
WED_CLOSE_DUE = datetime(2026, 2, 4, 21, 30, tzinfo=UTC)
TUE_CLOSE_DUE = datetime(2026, 2, 3, 21, 30, tzinfo=UTC)
NOW = datetime(2026, 2, 4, 22, tzinfo=UTC)

REQUESTED = 'Asset snapshot requested for'
IN_FLIGHT = 'Asset snapshot skipped for {source} {status} list: in flight'
NOT_DUE = 'Asset snapshot skipped for {source}: not due (the newest active snapshot is current)'
INACTIVE_UNDER_AGE = 'Asset snapshot skipped for {source} inactive list: not due (newest is under age)'
OFF = f'Asset snapshot requests are off ({ENABLED_ENV}); no asset list is requested by the store'
SNAPSHOT_ENVS = (ENABLED_ENV, 'DAILY_REFRESH_SETTLE_MINUTES')


class WeekdayCalendar:
    """A second source's calendar, unlike XNYS: every Monday to Friday is a session, open 08:00 and close 12:00 UTC."""

    tz = ZoneInfo('UTC')

    def session_open(self, session: date) -> datetime:
        return datetime.combine(session, datetime.min.time(), tzinfo=UTC) + timedelta(hours=8)

    def session_close(self, session: date) -> datetime:
        return datetime.combine(session, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)

    def _is_session(self, day: date) -> bool:
        return day.weekday() < 5

    def sessions_overlapping(self, start: datetime, end: datetime) -> list[date]:
        day, out = start.date(), []
        while day <= end.date():
            if self._is_session(day) and self.session_open(day) < end and self.session_close(day) > start:
                out.append(day)
            day += timedelta(days=1)
        return out

    def last_completed_session(self, now: datetime) -> date | None:
        day = now.astimezone(UTC).date()
        while not (self._is_session(day) and self.session_close(day) <= now):
            day -= timedelta(days=1)
        return day

    def session_in_progress(self, now: datetime) -> date | None:
        day = now.astimezone(UTC).date()
        if self._is_session(day) and self.session_open(day) <= now < self.session_close(day):
            return day
        return None

    def next_session(self, session: date) -> date:
        day = session + timedelta(days=1)
        while not self._is_session(day):
            day += timedelta(days=1)
        return day


SECOND_CALENDAR = WeekdayCalendar()


@pytest.fixture(autouse=True)
def unset_snapshot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SNAPSHOT_ENVS:
        monkeypatch.delenv(name, raising=False)
    # A mutant that waits for the verdict waits this long, not the 300 s default.
    monkeypatch.setenv('STORE_VERDICT_DEADLINE_SECONDS', '1')
    monkeypatch.setenv('STORE_VERDICT_POLL_SECONDS', '0.05')


@pytest_asyncio.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    engine = await make_engine(monkeypatch)
    metadata = sa.MetaData()
    AssetListSnapshot.__table__.to_metadata(metadata)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    built = Harness(engine, monkeypatch)
    monkeypatch.setattr(asset_snapshot_schedule, 'own_session', built.session)
    try:
        yield built
    finally:
        await engine.dispose()


def _schedule(
    client: FakeSubmit, *, now: datetime = NOW, cadences=None, calendars=None, handled: str | None = None
) -> AssetSnapshotSchedule:
    schedule = AssetSnapshotSchedule(
        client,
        {ALPACA: CADENCE} if cadences is None else cadences,
        {ALPACA: XNYS} if calendars is None else calendars,
        clock=lambda: now,
    )
    schedule._handled_instance = handled
    return schedule


async def _snapshot(
    harness: Harness, as_of: datetime, *, status: AssetListStatus = ACTIVE, source=ALPACA, rows: int = 3
) -> None:
    """A stored snapshot header, as the asset-list arm writes it (rows = 0 is a served EMPTY list)."""
    async with AsyncSession(harness.engine) as db:
        await db.execute(
            sa.insert(AssetListSnapshot.__table__).values(
                id=uuid.uuid4(),
                source=source,
                asset_type=AssetType.STOCK,
                asset_list=status,
                request_id=uuid.uuid4(),
                attempt_no=1,
                as_of=as_of,
                row_count=rows,
                received_at=as_of,
            )
        )
        await db.commit()


async def _lists(harness: Harness, source=None) -> list[tuple[DataSource, AssetListStatus]]:
    """The asset-list requests written, as (source, list), in write order."""
    return [
        (row.source, row.asset_list_status)
        for row in await harness.requests()
        if row.kind is IngestRequestKind.ASSET_LIST and (source is None or row.source is source)
    ]


async def _asset_rows(harness: Harness) -> list[sa.Row]:
    return [row for row in await harness.requests() if row.kind is IngestRequestKind.ASSET_LIST]


async def _finish_all(harness: Harness) -> None:
    """Every asset-list request finished, as the intake would after a delivery: none is open any more."""
    for row in await _asset_rows(harness):
        await harness.set_request(row.id, state=IngestRequestState.SUCCEEDED)


def _messages(caplog: pytest.LogCaptureFixture, level: int = logging.INFO) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == asset_snapshot_schedule.__name__ and r.levelno == level]


# --------------------------------------------------------------------------------------------- the kill switch


async def test_the_switch_is_on_when_unset(monkeypatch):
    monkeypatch.delenv(ENABLED_ENV, raising=False)
    assert is_enabled() is True


@pytest.mark.parametrize('value', ['false', '0', 'no', 'off', 'FALSE', 'Off', 'No', ' false ', '\toff\n', ' 0'])
async def test_false_0_no_and_off_in_any_case_and_whitespace_turn_it_off(monkeypatch, value):
    monkeypatch.setenv(ENABLED_ENV, value)
    assert is_enabled() is False


@pytest.mark.parametrize('value', ['true', '1', 'yes', 'on', 'TRUE', '', 'disabled', 'nope', 'f', '00'])
async def test_anything_else_leaves_it_on(monkeypatch, value):
    monkeypatch.setenv(ENABLED_ENV, value)
    assert is_enabled() is True


async def test_off_starts_nothing_requests_nothing_and_logs_why(harness, monkeypatch, caplog):
    monkeypatch.setenv(ENABLED_ENV, 'off')
    client = FakeSubmit()
    started: list[str] = []
    monkeypatch.setattr(asset_snapshot_schedule, 'run_periodic', lambda name, *a, **k: started.append(name))

    with caplog.at_level(logging.INFO, logger=asset_snapshot_schedule.__name__):
        loops = await start_asset_snapshot_loops(client, {ALPACA: CADENCE})

    assert loops is None
    assert started == []
    assert client.sent == [] and client.probes == 0
    assert await _lists(harness) == []
    assert _messages(caplog) == [OFF]
    await stop_asset_snapshot_loops(loops)  # None is a no-op


async def test_the_switch_is_read_at_start_only(harness, monkeypatch):
    client = FakeSubmit()
    monkeypatch.setattr(asset_snapshot_schedule, 'run_periodic', lambda *a, **k: None)
    loops = await start_asset_snapshot_loops(client, {ALPACA: SnapshotCadence(daily_offset=None)})
    assert loops is not None

    monkeypatch.setenv(ENABLED_ENV, 'false')
    await loops.schedule.on_instance_seen(INSTANCE_A)

    assert await _lists(harness) == [(ALPACA, ACTIVE), (ALPACA, INACTIVE)], 'a change after start is not read'


# --------------------------------------------------------------------------------------------- the cadence


async def test_the_default_cadence_is_one_per_calendar_source_with_the_settle_offset_and_seven_days(monkeypatch):
    monkeypatch.setenv('DAILY_REFRESH_SETTLE_MINUTES', '45')
    assert default_cadences() == {
        ALPACA: SnapshotCadence(
            asset_type=AssetType.STOCK,
            at_ingest_start=True,
            daily_offset=timedelta(minutes=45),
            inactive_max_age=timedelta(days=7),
        )
    }
    assert timedelta(days=7) == DEFAULT_INACTIVE_MAX_AGE


async def test_the_default_cadence_follows_the_calendar_map_not_one_vendor(monkeypatch):
    monkeypatch.setattr(asset_snapshot_schedule, 'SOURCE_CALENDARS', {ALPACA: 'XNYS', SECOND: 'XTSE'})
    assert set(default_cadences()) == {ALPACA, SECOND}


# --------------------------------------------------------------------------------------------- the daily due rule


async def test_no_active_snapshot_requests_both_lists(harness):
    client = FakeSubmit()
    await _schedule(client).run_daily(ALPACA)
    assert await _lists(harness) == [(ALPACA, ACTIVE), (ALPACA, INACTIVE)]
    assert [request.asset_list.status for request in client.sent] == [ACTIVE, INACTIVE]


async def test_a_snapshot_older_than_the_last_due_time_requests_the_active_list(harness):
    await _snapshot(harness, WED_CLOSE_DUE - timedelta(seconds=1))
    await _snapshot(harness, NOW - timedelta(days=1), status=INACTIVE)

    await _schedule(FakeSubmit()).run_daily(ALPACA)

    assert await _lists(harness) == [(ALPACA, ACTIVE)]


@pytest.mark.parametrize('as_of', [WED_CLOSE_DUE, WED_CLOSE_DUE + timedelta(minutes=10)], ids=['at-due', 'after-due'])
async def test_a_snapshot_at_or_after_the_last_due_time_is_current_and_nothing_is_requested(harness, caplog, as_of):
    await _snapshot(harness, as_of)

    with caplog.at_level(logging.INFO, logger=asset_snapshot_schedule.__name__):
        await _schedule(FakeSubmit()).run_daily(ALPACA)

    assert await _lists(harness) == []
    assert _messages(caplog) == [NOT_DUE.format(source='ALPACA')]


async def test_before_the_days_due_time_yesterdays_due_time_rules(harness):
    # 21:29 UTC: the close has passed but not the settle margin, so the last due time is Tuesday's.
    await _snapshot(harness, TUE_CLOSE_DUE + timedelta(minutes=1))
    await _schedule(FakeSubmit(), now=WED_CLOSE_DUE - timedelta(minutes=1)).run_daily(ALPACA)
    assert await _lists(harness) == []

    await _schedule(FakeSubmit(), now=WED_CLOSE_DUE).run_daily(ALPACA)
    assert await _lists(harness) == [(ALPACA, ACTIVE), (ALPACA, INACTIVE)]


@pytest.mark.parametrize(
    ('now', 'due'),
    [
        # Saturday and Sunday: Friday's close (21:00 UTC) plus the margin.
        (datetime(2026, 2, 7, 12, tzinfo=UTC), datetime(2026, 2, 6, 21, 30, tzinfo=UTC)),
        (datetime(2026, 2, 8, 23, tzinfo=UTC), datetime(2026, 2, 6, 21, 30, tzinfo=UTC)),
        # Tuesday after Presidents' Day (Monday 2026-02-16, XNYS closed), before the close: still Friday's.
        (datetime(2026, 2, 17, 15, tzinfo=UTC), datetime(2026, 2, 13, 21, 30, tzinfo=UTC)),
        # The day after Thanksgiving 2025 is a half day: its real close 13:00 EST (18:00 UTC) plus the margin.
        (datetime(2025, 11, 28, 19, tzinfo=UTC), datetime(2025, 11, 28, 18, 30, tzinfo=UTC)),
    ],
    ids=['saturday', 'sunday', 'after-a-holiday', 'half-day'],
)
async def test_weekends_holidays_and_half_days_come_from_the_calendar(harness, now, due):
    await _snapshot(harness, due)
    await _schedule(FakeSubmit(), now=now).run_daily(ALPACA)
    assert await _lists(harness) == [], 'a snapshot at the due time is current'

    await _snapshot(harness, now - timedelta(minutes=1), status=INACTIVE)
    async with AsyncSession(harness.engine) as db:
        await db.execute(sa.delete(AssetListSnapshot.__table__).where(AssetListSnapshot.asset_list == ACTIVE))
        await db.commit()
    await _snapshot(harness, due - timedelta(seconds=1))
    await _schedule(FakeSubmit(), now=now).run_daily(ALPACA)
    assert await _lists(harness) == [(ALPACA, ACTIVE)], 'a snapshot a second before the due time is not'


async def test_an_empty_newest_snapshot_is_not_a_current_one(harness):
    # P47 note 23:33: the age reads the newest NON-EMPTY snapshot, so a served empty list is asked for again.
    await _snapshot(harness, TUE_CLOSE_DUE)
    await _snapshot(harness, NOW - timedelta(minutes=5), rows=0)
    await _snapshot(harness, NOW - timedelta(days=1), status=INACTIVE)

    await _schedule(FakeSubmit()).run_daily(ALPACA)

    assert await _lists(harness) == [(ALPACA, ACTIVE)]


async def test_a_snapshot_of_the_other_list_or_source_does_not_count(harness):
    await _snapshot(harness, NOW - timedelta(minutes=5), status=INACTIVE)
    await _snapshot(harness, NOW - timedelta(minutes=5), source=SECOND)

    await _schedule(FakeSubmit()).run_daily(ALPACA)

    assert await _lists(harness) == [(ALPACA, ACTIVE)]


async def test_a_cadence_with_no_daily_request_is_never_due(harness, caplog):
    with caplog.at_level(logging.INFO, logger=asset_snapshot_schedule.__name__):
        await _schedule(FakeSubmit(), cadences={ALPACA: SnapshotCadence(daily_offset=None)}).run_daily(ALPACA)
    assert await _lists(harness) == []
    assert _messages(caplog) == ['Asset snapshot skipped for ALPACA: not due (no daily request or no session yet)']


# --------------------------------------------------------------------------------------------- the inactive rule


@pytest.mark.parametrize(
    ('age', 'requested'),
    [
        (timedelta(days=6), False),
        (timedelta(days=7) - timedelta(seconds=1), False),
        (timedelta(days=7), True),
        (timedelta(days=8), True),
    ],
    ids=['6-days', '7-days-less-a-second', 'exactly-7-days', '8-days'],
)
async def test_the_inactive_list_is_asked_for_at_7_days_old_or_more(harness, caplog, age, requested):
    await _snapshot(harness, NOW - age, status=INACTIVE)

    with caplog.at_level(logging.INFO, logger=asset_snapshot_schedule.__name__):
        await _schedule(FakeSubmit()).run_daily(ALPACA)

    expected = [(ALPACA, ACTIVE), (ALPACA, INACTIVE)] if requested else [(ALPACA, ACTIVE)]
    assert await _lists(harness) == expected
    assert (INACTIVE_UNDER_AGE.format(source='ALPACA') in _messages(caplog)) is not requested


async def test_the_inactive_list_is_asked_for_when_none_exists(harness):
    await _schedule(FakeSubmit()).run_daily(ALPACA)
    assert (ALPACA, INACTIVE) in await _lists(harness)


async def test_an_empty_recent_inactive_snapshot_does_not_count(harness):
    # P47 note 23:33, P47v's addition: the inactive request is made when the only recent inactive snapshot is empty.
    await _snapshot(harness, NOW - timedelta(hours=1), status=INACTIVE, rows=0)
    await _snapshot(harness, NOW - timedelta(days=3), status=INACTIVE, rows=0)

    await _schedule(FakeSubmit()).run_daily(ALPACA)

    assert await _lists(harness) == [(ALPACA, ACTIVE), (ALPACA, INACTIVE)]


async def test_the_inactive_list_is_never_asked_for_without_an_active_request(harness):
    await _snapshot(harness, WED_CLOSE_DUE)  # the active list is current: no active request
    await _snapshot(harness, NOW - timedelta(days=30), status=INACTIVE)

    await _schedule(FakeSubmit()).run_daily(ALPACA)

    assert await _lists(harness) == []


async def test_the_inactive_age_is_a_per_source_parameter(harness):
    await _snapshot(harness, NOW - timedelta(days=2), status=INACTIVE)
    cadence = SnapshotCadence(daily_offset=OFFSET, inactive_max_age=timedelta(days=1))

    await _schedule(FakeSubmit(), cadences={ALPACA: cadence}).run_daily(ALPACA)

    assert await _lists(harness) == [(ALPACA, ACTIVE), (ALPACA, INACTIVE)]


# --------------------------------------------------------------------------------------------- what a request is


async def test_a_request_is_a_scheduled_asset_list_row_sent_through_the_submit_path(harness):
    client = FakeSubmit()
    await _schedule(client).run_daily(ALPACA)

    rows = await _asset_rows(harness)
    assert [row.asset_list_status for row in rows] == [ACTIVE, INACTIVE]
    for row in rows:
        assert row.kind is IngestRequestKind.ASSET_LIST
        assert row.source is ALPACA
        assert row.asset_type is AssetType.STOCK
        assert row.trigger is IngestRunTrigger.SCHEDULE
        assert row.client_label == SCHEDULER_ACTOR == 'scheduler'
        assert row.ready_by is None
        assert row.dataset_id is None and row.asset_symbol is None
        # Through submit_record: the answer was recorded (QUEUED under the answering instance), not waited out.
        assert row.state is IngestRequestState.QUEUED
        assert row.instance_id == INSTANCE_A
    assert [request.request_id for request in client.sent] == [str(row.id) for row in rows]
    for request, row in zip(client.sent, rows, strict=True):
        assert request.bars is None and request.corporate_actions is None
        assert (request.asset_list.source, request.asset_list.asset_type, request.asset_list.status) == (
            ALPACA,
            AssetType.STOCK,
            row.asset_list_status,
        )
        assert request.ready_by is None
        assert request.recovered is False


async def test_the_schedule_writes_no_snapshot_itself(harness):
    await _schedule(FakeSubmit()).run_daily(ALPACA)
    async with AsyncSession(harness.engine) as db:
        count = (await db.execute(sa.select(sa.func.count()).select_from(AssetListSnapshot.__table__))).scalar_one()
    assert count == 0


async def test_it_does_not_wait_for_the_verdict(harness, caplog):
    # Nothing delivers: a schedule that waited would sit out STORE_VERDICT_DEADLINE_SECONDS and log its deadline.
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger=asset_snapshot_schedule.__name__):
        await asyncio.wait_for(_schedule(FakeSubmit()).run_daily(ALPACA), timeout=10)
    assert time.monotonic() - started < 0.9
    assert {row.state for row in await _asset_rows(harness)} == {IngestRequestState.QUEUED}
    assert _messages(caplog, logging.WARNING) == []


# --------------------------------------------------------------------------------------------- in flight


@pytest.mark.parametrize(
    'state', [IngestRequestState.PENDING, IngestRequestState.QUEUED, IngestRequestState.NOT_DELIVERED]
)
async def test_an_open_request_for_a_list_suppresses_that_list_and_not_the_other(harness, caplog, state):
    async with AsyncSession(harness.engine, expire_on_commit=False) as db:
        record = await request_crud.create_request(
            db,
            request_crud.NewIngestRequest(
                kind=IngestRequestKind.ASSET_LIST,
                source=ALPACA,
                client_label=SCHEDULER_ACTOR,
                asset_type=AssetType.STOCK,
                asset_list_status=ACTIVE,
            ),
        )
    await harness.set_request(record.id, state=state)
    client = FakeSubmit()

    with caplog.at_level(logging.INFO, logger=asset_snapshot_schedule.__name__):
        await _schedule(client).run_daily(ALPACA)

    assert await _lists(harness) == [(ALPACA, ACTIVE), (ALPACA, INACTIVE)], 'only the inactive list was added'
    assert [request.asset_list.status for request in client.sent] == [INACTIVE]
    assert IN_FLIGHT.format(source='ALPACA', status='active') in _messages(caplog)


@pytest.mark.parametrize(
    'state', [IngestRequestState.SUCCEEDED, IngestRequestState.EMPTY, IngestRequestState.FAILED], ids=str
)
async def test_a_finished_request_does_not_suppress(harness, state):
    await _schedule(FakeSubmit()).run_daily(ALPACA)
    for row in await _asset_rows(harness):
        await harness.set_request(row.id, state=state)

    await _schedule(FakeSubmit()).run_daily(ALPACA)

    assert len(await _lists(harness)) == 4


async def test_an_open_request_of_another_source_does_not_suppress(harness):
    cadences = {ALPACA: CADENCE, SECOND: CADENCE}
    calendars = {ALPACA: XNYS, SECOND: SECOND_CALENDAR}
    await _schedule(FakeSubmit(), cadences=cadences, calendars=calendars).run_daily(SECOND)
    assert await _lists(harness) == [(SECOND, ACTIVE), (SECOND, INACTIVE)]

    await _schedule(FakeSubmit(), cadences=cadences, calendars=calendars).run_daily(ALPACA)

    assert await _lists(harness, ALPACA) == [(ALPACA, ACTIVE), (ALPACA, INACTIVE)]


async def test_a_store_restart_with_its_requests_open_stacks_nothing(harness, caplog):
    await _schedule(FakeSubmit()).run_daily(ALPACA)
    before = await _asset_rows(harness)

    client = FakeSubmit()
    with caplog.at_level(logging.INFO, logger=asset_snapshot_schedule.__name__):
        restarted = _schedule(client)
        await restarted.run_daily(ALPACA)
        await restarted.on_instance_seen(INSTANCE_B)

    assert [row.id for row in await _asset_rows(harness)] == [row.id for row in before]
    assert client.sent == []
    assert _messages(caplog).count(IN_FLIGHT.format(source='ALPACA', status='active')) == 2
    assert _messages(caplog).count(IN_FLIGHT.format(source='ALPACA', status='inactive')) == 2


async def test_the_daily_pass_and_the_ingest_start_hook_at_once_stack_nothing(harness):
    client = FakeSubmit()
    schedule = _schedule(client)
    await asyncio.gather(schedule.run_daily(ALPACA), schedule.on_instance_seen(INSTANCE_A))
    assert sorted(await _lists(harness)) == [(ALPACA, ACTIVE), (ALPACA, INACTIVE)]


# --------------------------------------------------------------------------------------------- ingest start


async def test_a_new_instance_id_requests_the_active_list_and_the_same_id_again_nothing(harness, caplog):
    await _snapshot(harness, NOW - timedelta(hours=1))
    await _snapshot(harness, NOW - timedelta(days=1), status=INACTIVE)
    client = FakeSubmit()
    schedule = _schedule(client, handled=None)

    with caplog.at_level(logging.INFO, logger=asset_snapshot_schedule.__name__):
        await schedule.on_instance_seen(INSTANCE_A)
        await _finish_all(harness)
        await schedule.on_instance_seen(INSTANCE_A)
        await schedule.on_instance_seen(INSTANCE_A)

    # A current snapshot does not stop it: an ingest start asks whatever the age.
    assert await _lists(harness) == [(ALPACA, ACTIVE)]
    requested = [message for message in _messages(caplog) if message.startswith(REQUESTED)]
    assert len(requested) == 1
    assert requested[0].startswith('Asset snapshot requested for ALPACA active list (ingest started or restarted): ')


async def test_an_ingest_restart_requests_again(harness):
    await _snapshot(harness, NOW - timedelta(days=1), status=INACTIVE)
    schedule = _schedule(FakeSubmit(), handled=INSTANCE_A)

    await schedule.on_instance_seen(INSTANCE_A)
    assert await _lists(harness) == []

    await schedule.on_instance_seen(INSTANCE_B)
    await _finish_all(harness)
    await schedule.on_instance_seen(INSTANCE_A)  # another id again (a second restart) asks again too

    assert await _lists(harness) == [(ALPACA, ACTIVE), (ALPACA, ACTIVE)]


async def test_an_ingest_start_requests_the_inactive_list_by_its_age_rule(harness):
    await _snapshot(harness, NOW - timedelta(days=7), status=INACTIVE)
    await _schedule(FakeSubmit()).on_instance_seen(INSTANCE_A)
    assert await _lists(harness) == [(ALPACA, ACTIVE), (ALPACA, INACTIVE)]


async def test_an_ingest_start_asks_every_scheduled_source(harness):
    # The ASSUMPTION flagged for the architect: the probe does not say which sources the instance serves.
    cadences = {ALPACA: CADENCE, SECOND: CADENCE}
    await _schedule(FakeSubmit(), cadences=cadences, calendars={ALPACA: XNYS}).on_instance_seen(INSTANCE_A)
    assert await _lists(harness) == [(ALPACA, ACTIVE), (ALPACA, INACTIVE), (SECOND, ACTIVE), (SECOND, INACTIVE)]


async def test_a_cadence_without_the_ingest_start_request_is_skipped_and_logged(harness, caplog):
    cadences = {ALPACA: SnapshotCadence(at_ingest_start=False), SECOND: CADENCE}
    with caplog.at_level(logging.INFO, logger=asset_snapshot_schedule.__name__):
        await _schedule(FakeSubmit(), cadences=cadences).on_instance_seen(INSTANCE_A)
    assert await _lists(harness) == [(SECOND, ACTIVE), (SECOND, INACTIVE)]
    assert 'Asset snapshot skipped for ALPACA: not due (no request at ingest start)' in _messages(caplog)


async def test_seed_takes_the_newest_unsuperseded_instance(harness):
    schedule = _schedule(FakeSubmit())
    await schedule.seed()
    assert schedule._handled_instance is None, 'a store that has seen no instance seeds none'

    await harness.register(INSTANCE_A)
    await harness.register(INSTANCE_B)  # supersedes A
    await schedule.seed()
    assert schedule._handled_instance == INSTANCE_B
    async with harness.session() as db:
        assert await live_instance_id(db) == INSTANCE_B


# --- the seed on a never-migrated database (tj-7zjov1.375) -----------------------------------------------------------
#
# DESIGN: tj-7zjov1.375 (architect diagnosis 2026-11-11), shaped like the sweep's guard (tj-s6r3rt.5). seed() asks
# ingest_instance_table_exists (a to_regclass READ) before live_instance_id's SELECT; when the table is missing it logs
# ONE warning naming make migrate and returns, leaving the handled instance None, so the first instance seen after
# migrating counts as an ingest start. On a migrated database nothing changes.
#
# to_regclass here is the conftest's stand-in (registered on every SQLite connection: the name when the table exists,
# NULL when not), so the crud's real statement runs against a table that is really there or really dropped. What it
# cannot show is Postgres binding the argument (asyncpg); CI's System Start against an empty Postgres is that proof.

SEED_SKIPPED = 'Asset snapshot seed skipped: the ingest_instance table does not exist'


async def _drop_instances(harness: Harness) -> None:
    async with harness.engine.begin() as connection:
        await connection.execute(sa.text(f'DROP TABLE {INSTANCES.name}'))


async def _create_instances(harness: Harness) -> None:
    metadata = sa.MetaData()
    INSTANCES.to_metadata(metadata)
    async with harness.engine.begin() as connection:
        await connection.run_sync(metadata.create_all)


def _spy_crud(monkeypatch: pytest.MonkeyPatch, name: str) -> list[object]:
    """Journal each call of asset_snapshot_schedule.<name> and what it returned, still calling the real one."""
    journal: list[object] = []
    real = getattr(asset_snapshot_schedule, name)

    async def spy(*args, **kwargs):
        result = await real(*args, **kwargs)
        journal.append(result)
        return result

    monkeypatch.setattr(asset_snapshot_schedule, name, spy)
    return journal


async def test_on_a_never_migrated_database_the_seed_warns_once_and_handles_no_instance(harness, caplog, monkeypatch):
    await _drop_instances(harness)
    probed = _spy_crud(monkeypatch, 'ingest_instance_table_exists')
    selected = _spy_crud(monkeypatch, 'live_instance_id')
    schedule = _schedule(FakeSubmit())

    with caplog.at_level(logging.DEBUG):
        await schedule.seed()

    assert probed == [False], 'the probe must have run, and answered that the table is missing'
    assert selected == [], 'no SELECT may be attempted against a table that does not exist'
    assert schedule._handled_instance is None
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert [r for r in caplog.records if r.exc_info] == [], 'no traceback'
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.name == asset_snapshot_schedule.__name__
    assert warning.getMessage().startswith(SEED_SKIPPED)
    assert 'make migrate' in warning.getMessage()


async def test_on_a_migrated_database_the_probe_finds_the_table_and_the_seed_is_unchanged(harness, caplog, monkeypatch):
    await harness.register(INSTANCE_A)
    await harness.register(INSTANCE_B)  # supersedes A
    probed = _spy_crud(monkeypatch, 'ingest_instance_table_exists')
    schedule = _schedule(FakeSubmit())

    with caplog.at_level(logging.DEBUG, logger=asset_snapshot_schedule.__name__):
        await schedule.seed()

    assert probed == [True]
    assert schedule._handled_instance == INSTANCE_B, 'the live instance is taken as handled, as before'
    assert _messages(caplog, logging.WARNING) == []
    await schedule.on_instance_seen(INSTANCE_B)
    assert await _lists(harness) == [], 'so a store restart is still not an ingest restart'


async def test_after_a_skipped_seed_the_first_instance_seen_once_migrated_is_an_ingest_start(harness, monkeypatch):
    """Through start_asset_snapshot_loops: the start survives the missing table, and the first sighting asks once."""
    await _drop_instances(harness)
    monkeypatch.setattr(asset_snapshot_schedule, 'run_periodic', lambda *a, **k: None)

    loops = await start_asset_snapshot_loops(FakeSubmit(), {ALPACA: SnapshotCadence(daily_offset=None)})
    assert loops is not None and loops.schedule._handled_instance is None

    # make migrate runs; ingest registers its instance; the watcher's probe names it.
    await _create_instances(harness)
    await harness.register(INSTANCE_A)
    await loops.schedule.on_instance_seen(INSTANCE_A)
    assert await _lists(harness) == [(ALPACA, ACTIVE), (ALPACA, INACTIVE)], 'the first sighting is an ingest start'

    await _finish_all(harness)
    await loops.schedule.on_instance_seen(INSTANCE_A)
    assert await _lists(harness) == [(ALPACA, ACTIVE), (ALPACA, INACTIVE)], 'and the same id again asks nothing'


@pytest.mark.parametrize('exists', [True, False])
async def test_the_instance_probe_is_one_to_regclass_read_of_the_models_table_name(exists):
    """Under the Postgres dialect: SELECT to_regclass(<IngestInstance.__tablename__>) IS NOT NULL, a plain read."""
    from sqlalchemy.dialects import postgresql

    from data.store.app.database.crud.ingest_instance import ingest_instance_table_exists
    from data.store.app.database.models.ingest_instance import IngestInstance

    executed = []

    class Result:
        def scalar_one(self):
            return exists

    class Session:
        async def execute(self, statement):
            executed.append(statement)
            return Result()

    assert await ingest_instance_table_exists(Session()) is exists
    (statement,) = executed
    compiled = statement.compile(dialect=postgresql.dialect())
    assert str(compiled).replace('\n', ' ') == 'SELECT to_regclass(%(to_regclass_1)s) IS NOT NULL AS anon_1'
    assert compiled.params == {'to_regclass_1': IngestInstance.__tablename__}
    assert IngestInstance.__tablename__ == 'ingest_instance', 'the table the warning names'


async def test_a_store_restart_is_not_an_ingest_restart(harness, monkeypatch):
    """Through start_asset_snapshot_loops: the seed runs at start, so the live instance the probe names asks nothing."""
    await harness.register(INSTANCE_A)
    monkeypatch.setattr(asset_snapshot_schedule, 'run_periodic', lambda *a, **k: None)
    loops = await start_asset_snapshot_loops(FakeSubmit(), {ALPACA: SnapshotCadence(daily_offset=None)})

    await loops.schedule.on_instance_seen(INSTANCE_A)
    assert await _lists(harness) == []

    await loops.schedule.on_instance_seen(INSTANCE_B)
    assert await _lists(harness) == [(ALPACA, ACTIVE), (ALPACA, INACTIVE)]


async def test_the_watcher_calls_the_hook_after_noting_the_probed_instance(harness):
    seen: list[tuple[str, object]] = []

    async def hook(instance_id: str) -> None:
        seen.append((instance_id, await harness.instance(instance_id)))

    client = FakeSubmit(INSTANCE_B)
    await InstanceWatcher(client, clock=lambda: NOW, on_instance=hook).run_pass()

    ((instance_id, row),) = seen
    assert instance_id == INSTANCE_B
    assert row is not None, 'the hook ran before the instance was noted'


async def test_the_watcher_calls_no_hook_when_the_probe_fails(harness):
    seen: list[str] = []

    async def hook(instance_id: str) -> None:
        seen.append(instance_id)

    client = FakeSubmit()
    client.probe_raises = ExogenousError(Reason.PEER_UNAVAILABLE, 'ingest is down')
    await InstanceWatcher(client, clock=lambda: NOW, on_instance=hook).run_pass()
    assert seen == []


async def test_a_typed_failure_in_the_hooks_requests_does_not_break_the_watchers_pass(harness):
    async with AsyncSession(harness.engine, expire_on_commit=False) as db:
        pending = await request_crud.create_request(
            db,
            request_crud.NewIngestRequest(
                kind=IngestRequestKind.ASSET_LIST,
                source=SECOND,
                client_label='owner',
                asset_type=AssetType.STOCK,
                asset_list_status=ACTIVE,
            ),
        )
    await harness.set_request(pending.id, created_at=NOW - timedelta(hours=1))
    client = FakeSubmit(INSTANCE_B)

    async def refuse_alpaca(request) -> None:
        if request.asset_list is not None and request.asset_list.source is ALPACA:
            raise ExogenousError(Reason.PEER_UNAVAILABLE, 'ingest is down')

    client.on_submit = refuse_alpaca
    schedule = _schedule(client)
    result = await InstanceWatcher(client, clock=lambda: NOW, on_instance=schedule.on_instance_seen).run_pass()

    assert result.resent == 1, "the watcher's own re-send still ran"
    assert {row.state for row in await _asset_rows(harness) if row.source is ALPACA} == {IngestRequestState.PENDING}


# --------------------------------------------------------------------------------------------- failures


async def test_a_typed_submit_failure_leaves_the_rows_pending_and_logs_a_warning(harness, caplog):
    client = FakeSubmit()
    client.raises = ExogenousError(Reason.PEER_UNAVAILABLE, 'ingest is down')

    with caplog.at_level(logging.INFO, logger=asset_snapshot_schedule.__name__):
        await _schedule(client).run_daily(ALPACA)

    rows = await _asset_rows(harness)
    assert [(row.asset_list_status, row.state) for row in rows] == [
        (ACTIVE, IngestRequestState.PENDING),
        (INACTIVE, IngestRequestState.PENDING),
    ], 'both written and left PENDING for the watcher'
    warnings = _messages(caplog, logging.WARNING)
    assert warnings == [
        'Asset snapshot request for ALPACA active list (daily) did not complete: ExogenousError PEER_UNAVAILABLE',
        'Asset snapshot request for ALPACA inactive list (daily) did not complete: ExogenousError PEER_UNAVAILABLE',
    ]
    assert not [m for m in _messages(caplog) if m.startswith(REQUESTED)]


async def test_an_untyped_failure_propagates_and_releases_the_lock(harness):
    client = FakeSubmit()
    client.raises = RuntimeError('a defect')
    schedule = _schedule(client)

    with pytest.raises(RuntimeError):
        await schedule.run_daily(ALPACA)

    client.raises = None
    await _finish_all(harness)
    await asyncio.wait_for(schedule.run_daily(ALPACA), timeout=5)
    assert len(await _lists(harness)) == 3


# --------------------------------------------------------------------------------------------- logs


async def test_each_request_made_is_logged_with_its_source_list_reason_and_id_only(harness, caplog):
    with caplog.at_level(logging.INFO, logger=asset_snapshot_schedule.__name__):
        await _schedule(FakeSubmit()).run_daily(ALPACA)

    rows = await _asset_rows(harness)
    assert _messages(caplog) == [
        f'Asset snapshot requested for ALPACA active list (daily): request {rows[0].id}',
        f'Asset snapshot requested for ALPACA inactive list (daily): request {rows[1].id}',
    ]
    for record in caplog.records:
        assert record.levelno == logging.INFO


# --------------------------------------------------------------------------------------------- the loops


async def test_one_loop_per_source_each_on_its_own_calendar_and_offset(harness, monkeypatch):
    calls: list[tuple[str, object, asyncio.Event]] = []

    def run_periodic(name, schedule, job, *, wake, log):
        calls.append((name, schedule, wake))
        return asyncio.get_running_loop().create_future()

    calendars = {ALPACA: XNYS, SECOND: SECOND_CALENDAR}
    monkeypatch.setattr(asset_snapshot_schedule, 'calendar_for_source', calendars.__getitem__)
    monkeypatch.setattr(asset_snapshot_schedule, 'run_periodic', run_periodic)
    second = SnapshotCadence(daily_offset=timedelta(hours=3))

    loops = await start_asset_snapshot_loops(FakeSubmit(), {ALPACA: CADENCE, SECOND: second})

    assert [name for name, _, _ in calls] == ['asset-snapshot-ALPACA', 'asset-snapshot-IB']
    assert calls[0][1] == AfterTradingEvent(XNYS, TradingEvent.CLOSE, OFFSET)
    assert calls[1][1] == AfterTradingEvent(SECOND_CALENDAR, TradingEvent.CLOSE, timedelta(hours=3))
    assert all(wake.is_set() for _, _, wake in calls), 'each loop runs its catch-up at start'
    assert len(loops.tasks) == 2


async def test_a_source_without_a_calendar_or_a_daily_request_gets_no_loop(harness, monkeypatch):
    names: list[str] = []
    monkeypatch.setattr(
        asset_snapshot_schedule, 'run_periodic', lambda name, *a, **k: names.append(name) or asyncio.Future()
    )
    cadences = {ALPACA: SnapshotCadence(daily_offset=None), DataSource.MANUAL_ENTRY: CADENCE}

    loops = await start_asset_snapshot_loops(FakeSubmit(), cadences)

    assert names == [] and loops.tasks == []
    assert isinstance(loops, AssetSnapshotLoops)


async def test_the_sources_cadences_are_independent(harness):
    # Wednesday 22:00 UTC. ALPACA's due time is 21:30 today; the second source's is Wednesday 12:00 + 12 h = 00:00
    # Thursday, so its last due time is Tuesday's (Wednesday 00:00). A snapshot of each at 21:00 is stale for ALPACA only.
    cadences = {ALPACA: CADENCE, SECOND: SnapshotCadence(daily_offset=timedelta(hours=12))}
    calendars = {ALPACA: XNYS, SECOND: SECOND_CALENDAR}
    for source in (ALPACA, SECOND):
        await _snapshot(harness, datetime(2026, 2, 4, 21, tzinfo=UTC), source=source)
        await _snapshot(harness, NOW - timedelta(days=1), source=source, status=INACTIVE)
    schedule = _schedule(FakeSubmit(), cadences=cadences, calendars=calendars)

    await schedule.run_daily(ALPACA)
    await schedule.run_daily(SECOND)

    assert await _lists(harness) == [(ALPACA, ACTIVE)]


async def test_the_second_sources_request_carries_its_own_source(harness):
    cadences = {SECOND: CADENCE}
    client = FakeSubmit()
    await _schedule(client, cadences=cadences, calendars={SECOND: SECOND_CALENDAR}).run_daily(SECOND)
    assert await _lists(harness) == [(SECOND, ACTIVE), (SECOND, INACTIVE)]
    assert {request.asset_list.source for request in client.sent} == {SECOND}


async def test_the_real_loops_run_the_catch_up_per_source_survive_a_failing_pass_and_stop(harness, monkeypatch):
    calendars = {ALPACA: XNYS, SECOND: SECOND_CALENDAR}
    monkeypatch.setattr(asset_snapshot_schedule, 'calendar_for_source', calendars.__getitem__)
    passes: list[DataSource] = []

    async def run_daily(self, source):
        passes.append(source)
        raise RuntimeError('a pass that fails')

    monkeypatch.setattr(AssetSnapshotSchedule, 'run_daily', run_daily)
    loops = await start_asset_snapshot_loops(FakeSubmit(), {ALPACA: CADENCE, SECOND: CADENCE})
    try:
        for _ in range(200):
            if len(passes) >= 2:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert sorted(passes) == sorted([ALPACA, SECOND])
        assert not any(task.done() for task in loops.tasks), 'a failing pass ended a loop'
    finally:
        await stop_asset_snapshot_loops(loops)
    assert all(task.cancelled() for task in loops.tasks)


# --------------------------------------------------------------------------------------------- the lifespan


def _lifespan_fakes(monkeypatch: pytest.MonkeyPatch, order: list[str], client: FakeSubmit) -> None:
    class FakeDatabase:
        async def initialize(self) -> None:
            order.append('database.initialize')

        async def shutdown(self) -> None:
            order.append('database.shutdown')

    class FakeChannel:
        async def close(self) -> None:
            order.append('channel.close')

    class FakePool:
        @staticmethod
        def worker_startup() -> None: ...

        @staticmethod
        def worker_shutdown() -> None: ...

    async def nothing(*_a, **_k) -> None:
        return None

    real_stop = app_depends.stop_asset_snapshot_loops

    async def stop(loops):
        order.append('snapshot.stop')
        await real_stop(loops)
        order.append('snapshot.stopped' if loops is None or all(t.done() for t in loops.tasks) else 'snapshot.running')

    for name in ('__INGEST_CHANNEL', '__INGEST_SUBMIT_CLIENT'):
        monkeypatch.setattr(app_depends, name, None)
    monkeypatch.setattr(app_depends, 'database', FakeDatabase())
    monkeypatch.setattr(app_depends, 'sweep_interrupted_runs', nothing)
    monkeypatch.setattr(app_depends, 'start_usage_loops', lambda: [])
    monkeypatch.setattr(app_depends, 'stop_usage_loops', nothing)
    monkeypatch.setattr(app_depends, 'start_reaper_loop', lambda: None)
    monkeypatch.setattr(app_depends, 'stop_reaper_loop', nothing)
    monkeypatch.setattr(app_depends, 'initialize_latency_client', lambda *a, **k: None)
    monkeypatch.setattr(app_depends, 'init_debugger', lambda: None)
    monkeypatch.setattr(app_depends, 'startup_logs', lambda _app: None)
    monkeypatch.setattr(app_depends, 'teardown_logs', lambda _app: None)
    monkeypatch.setattr(app_depends, 'SharedWorkerPool', FakePool)
    monkeypatch.setattr(app_depends, 'target_from_env', lambda _name: 'ingest:1')
    monkeypatch.setattr(app_depends, 'create_channel', lambda _target: FakeChannel())
    monkeypatch.setattr(app_depends, 'GrpcIngestSubmitClient', lambda _channel: client)
    monkeypatch.setattr(app_depends, 'stop_asset_snapshot_loops', stop)
    monkeypatch.setattr(app_depends, 'build_grpc_host', nullcontext)
    # The DAILY refresh shares the client and is not this test's subject.
    monkeypatch.setenv('DAILY_REFRESH_ENABLED', 'false')


async def _current_snapshots(harness: Harness) -> None:
    """Both lists current at the real clock, so the daily catch-up asks for nothing and only a trigger can."""
    now = datetime.now(UTC)
    await _snapshot(harness, now)
    await _snapshot(harness, now, status=INACTIVE)


async def _until(predicate, *, seconds: float = 5.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)


async def test_in_the_lifespan_an_ingest_restart_seen_by_the_watcher_requests_the_active_list(
    harness, monkeypatch, caplog
):
    order: list[str] = []
    await harness.register(INSTANCE_A)  # the instance the store knew before it restarted
    await _current_snapshots(harness)
    client = FakeSubmit(INSTANCE_B)  # ingest restarted while the store was down
    _lifespan_fakes(monkeypatch, order, client)

    with caplog.at_level(logging.INFO, logger=asset_snapshot_schedule.__name__):
        async with app_depends.lifespan(object()):
            await _until(lambda: bool(client.sent))
            await asyncio.sleep(0.05)

    assert client.probes >= 1
    assert await _lists(harness) == [(ALPACA, ACTIVE)], 'the inactive list is current: its age rule holds'
    assert NOT_DUE.format(source='ALPACA') in _messages(caplog), 'the daily catch-up ran too, and was not due'
    assert order.index('snapshot.stop') < order.index('channel.close') < order.index('database.shutdown')
    assert 'snapshot.stopped' in order


async def test_in_the_lifespan_a_store_restart_against_the_same_ingest_requests_nothing(harness, monkeypatch):
    order: list[str] = []
    await harness.register(INSTANCE_A)
    await _current_snapshots(harness)
    client = FakeSubmit(INSTANCE_A)
    _lifespan_fakes(monkeypatch, order, client)

    async with app_depends.lifespan(object()):
        await _until(lambda: client.probes >= 1)
        await asyncio.sleep(0.05)

    assert client.probes >= 1
    assert await _lists(harness) == []


async def test_in_the_lifespan_with_the_watcher_off_there_is_no_ingest_start_trigger_but_the_daily_loop_runs(
    harness, monkeypatch, caplog
):
    order: list[str] = []
    await harness.register(INSTANCE_A)
    await _current_snapshots(harness)
    client = FakeSubmit(INSTANCE_B)
    _lifespan_fakes(monkeypatch, order, client)
    monkeypatch.setenv('INSTANCE_WATCH_ENABLED', 'false')
    captured: list[AssetSnapshotLoops] = []
    real_start = app_depends.start_asset_snapshot_loops

    async def start(submit_client):
        loops = await real_start(submit_client)
        captured.append(loops)
        return loops

    monkeypatch.setattr(app_depends, 'start_asset_snapshot_loops', start)

    with caplog.at_level(logging.INFO, logger=asset_snapshot_schedule.__name__):
        async with app_depends.lifespan(object()):
            await _until(lambda: NOT_DUE.format(source='ALPACA') in _messages(caplog))
            await asyncio.sleep(0.05)
            (loops,) = captured
            assert loops.tasks and not any(task.done() for task in loops.tasks), 'the daily loop is running'

    assert client.probes == 0 and client.sent == []
    assert await _lists(harness) == []
    assert NOT_DUE.format(source='ALPACA') in _messages(caplog)
    assert all(task.cancelled() for task in loops.tasks)


async def test_in_the_lifespan_the_late_store_start_catch_up_requests_at_once(harness, monkeypatch, caplog):
    order: list[str] = []
    await harness.register(INSTANCE_A)
    client = FakeSubmit(INSTANCE_A)
    _lifespan_fakes(monkeypatch, order, client)
    monkeypatch.setenv('INSTANCE_WATCH_ENABLED', 'false')
    # The newest active snapshot predates the last due time: the store was down when it was due.
    await _snapshot(harness, datetime.now(UTC) - timedelta(days=5))

    # WAIT FOR THE PASS TO FINISH WRITING, NOT FOR THE SECOND SEND (tj-7zjov1.227). submit_record still writes after
    # client.submit returns (note_instance_seen, mark_queued) on the harness's ONE StaticPool connection. Leaving the
    # lifespan as soon as the second request was sent cancelled the loop inside one of those statements; SQLAlchemy
    # then invalidated the connection, the pool opened a new, empty in-memory database, and the read below failed
    # 'no such table: ingest_request' (about 1 run in 70 in isolation). The 'requested' line is logged only after
    # submit_record returns, so after two of them the pass has nothing left in flight.
    with caplog.at_level(logging.INFO, logger=asset_snapshot_schedule.__name__):
        async with app_depends.lifespan(object()):
            await _until(lambda: sum(m.startswith(REQUESTED) for m in _messages(caplog)) >= 2)

    assert await _lists(harness) == [(ALPACA, ACTIVE), (ALPACA, INACTIVE)]
    assert {row.trigger for row in await _asset_rows(harness)} == {IngestRunTrigger.SCHEDULE}


async def test_the_request_table_is_the_one_the_harness_reads(harness):
    # Guards the helpers above: an asset-list request lands in ingest_request with its list column.
    await _schedule(FakeSubmit()).run_daily(ALPACA)
    async with AsyncSession(harness.engine) as db:
        statuses = (await db.execute(sa.select(REQUESTS.c.asset_list_status))).scalars().all()
    assert sorted(statuses) == [ACTIVE, INACTIVE]
