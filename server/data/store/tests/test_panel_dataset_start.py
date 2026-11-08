"""The declared start of a panel dataset (validator, tj-7zjov1.239): judged and refreshed on the REAL XNYS calendar.

THE DESIGN checked against: bug tj-7zjov1.239, ruling A' (the 03:20 UTC note on tj-7zjov1.36): the start is
local_midnight(CALENDAR_START, calendar_for_source(source)), 1990-01-01T05:00Z for ALPACA_API (XNYS), used for the
clamp body and the unclamped start in PanelManager._create; a source with no calendar raises UnknownCalendarError and
never falls back to UTC midnight. The old instant, 1990-01-01T00:00Z, is 1989-12-31 19:00 in New York, before the
calendar's first day: ExchangeCalendarAdapter._require_covered raised CalendarRangeError, so the catalog gave no
health and DailyRefresher._behind skipped the dataset for good.

  (a) the start is the local midnight of CALENDAR_START, never before it; evaluate_dataset and the catalog's
      evaluate_views judge a DAILY 1day panel entry starting there (a status, not None);
  (b) a manager-created unclamped panel request's requested_start, the clamp body's start and the own-overlap key's
      start are that instant (the SQLite pass harness, the real calendar_for_source); the clamp's own corporate-actions
      window is NOT this instant (the real clamp asks from CALENDAR_START at UTC midnight) and is not pinned here;
  (c) the DAILY refresh owes such an entry when it is LATE or OVERDUE, and plans its tail from the declared start;
  (d) a source with no calendar raises UnknownCalendarError.

Each of (a) to (c) is red with the old UTC-midnight instant restored (the mutation table is on the verdict note).
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app import panel_manager
from data.store.app.daily_refresh import DailyRefresher
from data.store.app.database.crud.stock.dataset_catalog import CatalogRow
from data.store.app.database.models.ingest_request import IngestRequestKind
from data.store.app.dataset_catalog import effective_end, evaluate_views
from data.store.app.health import DatasetHealthStatus, HealthConfig, evaluate_dataset
from data.store.app.panel_manager import panel_dataset_start
from data.store.app.trading_calendar import (
    CALENDAR_START,
    SOURCE_CALENDARS,
    CalendarRangeError,
    UnknownCalendarError,
    calendar_for_source,
)
from data.store.tests.panel_manager_harness import Clock, make_world, manager


pytestmark = [pytest.mark.data_store]

SOURCE = DataSource.ALPACA_API
# By hand: 1990-01-01 00:00 EST (UTC-5) in New York. The old instant was 1990-01-01T00:00Z.
DECLARED_START = datetime(1990, 1, 1, 5, tzinfo=UTC)
OLD_UTC_MIDNIGHT = datetime(1990, 1, 1, tzinfo=UTC)
CONFIG = HealthConfig(settle_margin=timedelta(minutes=30))

# 2026-10-20 is a Tuesday session on XNYS (EDT): close 20:00Z, settled 20:30Z; 2026-10-21 opens 13:30Z.
OCT20_SETTLED = datetime(2026, 10, 20, 20, 30, tzinfo=UTC)
OCT21_OPEN = datetime(2026, 10, 21, 13, 30, tzinfo=UTC)
# A 1day bar of 2026-10-19 is stamped at that session's New York midnight (04:00Z, EDT), written after its close.
OCT19_BAR = datetime(2026, 10, 19, 4, tzinfo=UTC)
OCT19_WRITTEN = datetime(2026, 10, 19, 21, tzinfo=UTC)


@pytest.fixture(scope='module')
def xnys():
    return calendar_for_source(SOURCE)


def panel_row(last_bar: datetime | None, written: datetime | None, start: datetime | None = None) -> CatalogRow:
    """A panel-owned DAILY 1day ROLLING open-ended entry, collecting, as the delivery intake mints it."""
    return CatalogRow(
        id=uuid.uuid4(),
        asset_symbol='AAA',
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=SOURCE,
        feed=Feed.IEX,
        granularity=Granularity.ONE_DAY,
        start=panel_dataset_start(SOURCE) if start is None else start,
        end=None,
        update_type=UpdateType.DAILY,
        expiry_type=ExpiryType.ROLLING,
        expiry=None,
        owner='panel',
        last_bar=last_bar,
        last_bar_written_at=written,
        collect=True,
    )


def judge(row: CatalogRow, calendar, now: datetime):
    return evaluate_dataset(
        update_type=row.update_type,
        granularity=row.granularity,
        collect=row.collect,
        start=row.start,
        end=effective_end(row, now),
        last_bar=row.last_bar,
        covered=None,
        calendar=calendar,
        now=now,
        config=CONFIG,
        last_bar_written_at=row.last_bar_written_at,
    )


# ----------------------------------------------------------------------------------------------- (a) the instant


def test_the_start_for_alpaca_is_new_york_midnight_of_the_calendar_start(xnys):
    start = panel_dataset_start(SOURCE)
    assert start == DECLARED_START
    assert start.astimezone(xnys.tz).date() == CALENDAR_START
    assert start.astimezone(xnys.tz).time() == datetime.min.time()
    assert start != OLD_UTC_MIDNIGHT


def test_the_old_utc_midnight_is_before_the_calendar_and_is_what_the_fix_removed(xnys):
    """The precondition of the bug, on the real calendar: if this stops raising, the pins below prove less."""
    with pytest.raises(CalendarRangeError):
        judge(panel_row(None, None, start=OLD_UTC_MIDNIGHT), xnys, OCT21_OPEN)


@pytest.mark.parametrize(
    ('last_bar', 'written', 'now', 'status'),
    [
        (None, None, OCT21_OPEN, DatasetHealthStatus.OVERDUE),
        (OCT19_BAR, OCT19_WRITTEN, OCT20_SETTLED, DatasetHealthStatus.LATE),
        (OCT19_BAR, OCT19_WRITTEN, OCT21_OPEN, DatasetHealthStatus.OVERDUE),
        (datetime(2026, 10, 20, 4, tzinfo=UTC), datetime(2026, 10, 20, 21, tzinfo=UTC), OCT21_OPEN, None),
    ],
    ids=['no-bars-overdue', 'one-session-behind-late', 'one-session-behind-overdue', 'current-fresh'],
)
def test_a_panel_entry_starting_there_is_judged(xnys, last_bar, written, now, status):
    health = judge(panel_row(last_bar, written), xnys, now)
    assert health.status is (status or DatasetHealthStatus.FRESH)


@pytest.mark.asyncio
async def test_the_catalog_gives_a_panel_entry_health_not_none():
    """dataset_catalog.evaluate_views, the path the UI reads (DAILY rows need no statement, so no session)."""
    rows = [panel_row(None, None), panel_row(OCT19_BAR, OCT19_WRITTEN)]
    views = await evaluate_views(None, rows, OCT21_OPEN, CONFIG)
    assert [view.health is not None for view in views] == [True, True]
    assert [view.health.status for view in views] == [DatasetHealthStatus.OVERDUE, DatasetHealthStatus.OVERDUE]


# ----------------------------------------------------------------------------------------------- (b) the manager


@pytest_asyncio.fixture
async def world(monkeypatch):
    built = await make_world(monkeypatch)
    try:
        yield built
    finally:
        await built.engine.dispose()


@pytest.mark.asyncio
async def test_an_unclamped_panel_creation_sends_and_keys_on_new_york_midnight(world, monkeypatch):
    await world.source_settings()
    await world.panel(datetime.now(UTC) - timedelta(days=2))
    await world.snapshot(datetime.now(UTC) - timedelta(hours=1), [('v1', 'AAA')])
    keys = []
    real = panel_manager.check_own_overlap

    async def recording(db, key):
        keys.append(key)
        return await real(db, key)

    monkeypatch.setattr(panel_manager, 'check_own_overlap', recording)

    await manager(Clock()).tick()

    (clamp,) = world.clamps
    assert clamp.body.start == DECLARED_START, 'the clamp body carries the declared start'
    (request,) = await world.requests(IngestRequestKind.BARS)
    assert request.requested_start == DECLARED_START, 'no clamp: the bars request keeps the declared start'
    (key,) = keys
    assert key.start == DECLARED_START
    # No claim on the clamp's corporate-actions request window: the harness double writes that row, and the real
    # first_request_clamp asks from CALENDAR_START at UTC midnight whatever the body's start (P51, by design), pinned on
    # the real clamp by test_name_change_clamp.py::test_fb_from_2015_is_requested_written_and_entered_from_2022_06_09_
    # local_midnight. The declared start reaches the clamp as the body's start (asserted above), which the real clamp
    # compares a name_change's process date against.


# ----------------------------------------------------------------------------------------------- (c) the refresh


@pytest.mark.parametrize(
    ('last_bar', 'written', 'now'),
    [(None, None, OCT21_OPEN), (OCT19_BAR, OCT19_WRITTEN, OCT20_SETTLED), (OCT19_BAR, OCT19_WRITTEN, OCT21_OPEN)],
    ids=['no-bars-overdue', 'late', 'overdue'],
)
def test_the_daily_refresh_owes_a_behind_panel_entry(xnys, last_bar, written, now):
    row = panel_row(last_bar, written)
    owed = DailyRefresher._behind([row], frozenset(), xnys, now)
    assert [r.id for r in owed] == [row.id]


def test_the_daily_refresh_plans_a_bar_less_panel_entry_from_the_declared_start(xnys):
    row = panel_row(None, None)
    owed = DailyRefresher._behind([row], frozenset(), xnys, OCT21_OPEN)
    (productive, again), skipped = DailyRefresher._plan(owed, [], None, xnys)
    assert (skipped, again) == (0, [])
    assert [planned.fetch_start for planned in productive] == [DECLARED_START]


def test_a_current_panel_entry_is_not_owed(xnys):
    """The control: the selection is by health, so a FRESH entry is left alone."""
    row = panel_row(datetime(2026, 10, 20, 4, tzinfo=UTC), datetime(2026, 10, 20, 21, tzinfo=UTC))
    assert DailyRefresher._behind([row], frozenset(), xnys, OCT21_OPEN) == []


# ----------------------------------------------------------------------------------------------- (d) no calendar


@pytest.mark.parametrize('source', [s for s in DataSource if s not in SOURCE_CALENDARS], ids=str)
def test_a_source_with_no_calendar_raises_and_never_falls_back_to_utc(source):
    with pytest.raises(UnknownCalendarError):
        panel_dataset_start(source)


def test_there_is_a_source_with_no_calendar_to_pin():
    assert {s for s in DataSource if s not in SOURCE_CALENDARS}, 'every source has a calendar: (d) pins nothing'
    assert isinstance(CALENDAR_START, date)
