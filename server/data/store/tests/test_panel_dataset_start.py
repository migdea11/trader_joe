"""The declared start of a panel dataset is POINT IN TIME (validator, tj-7zjov1.364.11): no 1990 backfill.

THE DESIGN checked against: ADR tj-iov38l P1 to P5 (owner ruling 2026-11-10, superseding tj-7zjov1.239 ruling A', the
constant start at the local midnight of CALENDAR_START, 1990-01-01), as built by tj-7zjov1.364.10:

  P1 a member's dataset starts at local_midnight(D, calendar) where D is the LOCAL date, in the source calendar's zone, of
     the as_of of the member's current (newest, non-dry-run) ENTERED transition on the panel;
  P2 the snapshot's own day is included, whatever the hour; the UTC date is wrong for an evening snapshot;
  P3 a weekend or holiday D is a fine start: nothing is owed until the first session on or after it closes;
  P4 first pass: the snapshot's (or evidence) date; a later entrant: its own ENTERED date; DEFERRED then created by a
     RELEASE or a later pass: the stint's ENTERED date from the ledger, not the creation day; a re-entrant with a
     dataset: nothing created, its start unchanged; a dataset another panel already created: shared, the earlier start
     stands;
  P5 the name_change clamp stays, on the new start, and can only move it later; REQUESTS_PER_CREATION stays 2.

And the builder's contract on tj-7zjov1.364.10: panel_dataset_start(source, entered_as_of) refuses a naive instant
and raises UnknownCalendarError for a source with no calendar; _create takes entered_dates (the pass's ONE read of
crud.panel_ledger.entered_as_of_by_member, pinned in test_panel_boundary_read.py R5): a NEW member uses the ENTERED
as_of this call writes, any other the ledger's; a member with no ENTERED row is logged at ERROR by vendor asset id and
skipped BEFORE the budget check, never given a fallback start; the membership pass reads the ledger once and only when
a non-new member is to be created, the release tick once before its loop.

  (a) the rule on the real XNYS calendar: evening, morning, around local midnight, weekend, holiday, both DST changes,
      an instant in another zone, a naive instant, a source with no calendar;
  (b) health and the DAILY refresh on a start set this way (P2, P3): a Saturday start reads FRESH until Monday's close;
      an evening snapshot's first bar is final, FRESH; a mid-session one is provisional, LATE, then FRESH on the rewrite;
      a holiday start owes nothing until the next session closes; the refresh plans a bar-less entry from the start;
  (c) the manager on the SQLite harness (panel_manager_harness.py), the real panel_dataset_start and ledger read;
  (d) no code path yields 1990.

NOT PINNED, by design: a date before the calendar's first day (CALENDAR_START). An ENTERED as_of is a stored snapshot's
instant, so it cannot precede 1990; panel_dataset_start has no range guard (local_midnight checks nothing) and none is
invented here. The clamp's "an earlier rename leaves the start" rule belongs to the real first_request_clamp, pinned by
test_name_change_clamp.py::test_the_clamp_applies_only_when_after_the_bodys_start; the harness double mirrors it.

Postgres (asyncpg, the window function on real timestamptz) is tests/system/test_panel_manager_pg.py PM1, PM2 and PM3.
"""

import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app import panel_manager
from data.store.app.daily_refresh import DailyRefresher
from data.store.app.database.crud.panel_ledger import NewTransition
from data.store.app.database.crud.stock.dataset_catalog import CatalogRow
from data.store.app.database.models.ingest_request import IngestRequestKind, IngestRequestState
from data.store.app.database.models.panel import PanelCadence, PanelTransition
from data.store.app.dataset_catalog import effective_end, evaluate_views
from data.store.app.health import DatasetHealthStatus, HealthConfig, evaluate_dataset
from data.store.app.panel_manager import REQUESTS_PER_CREATION, panel_dataset_start
from data.store.app.trading_calendar import (
    CALENDAR_START,
    SOURCE_CALENDARS,
    NaiveDatetimeError,
    UnknownCalendarError,
    calendar_for_source,
)
from data.store.tests.panel_manager_harness import ENTRY_START, PASSES, SNAPSHOTS, Clock, kinds, make_world, manager


pytestmark = [pytest.mark.data_store]

SOURCE = DataSource.ALPACA_API
NEW_YORK = ZoneInfo('America/New_York')
EST, EDT = timedelta(hours=-5), timedelta(hours=-4)
CONFIG = HealthConfig(settle_margin=timedelta(minutes=30))
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
ENTERED, CREATED, DEFERRED = PanelTransition.ENTERED, PanelTransition.CREATED, PanelTransition.DEFERRED


def z(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def ny(day: date) -> datetime:
    """New York midnight of a date, by hand: the oracle the rule is compared with."""
    return datetime.combine(day, time.min, tzinfo=NEW_YORK)


@pytest.fixture(scope='module')
def xnys():
    return calendar_for_source(SOURCE)


# ===================================================================================== (a) the rule


@pytest.mark.parametrize(
    ('entered', 'start', 'offset'),
    [
        # The owner's first snapshot: 21:48 New York on Monday 9 Nov is 02:48Z on Tuesday 10 Nov (EST).
        (z(2026, 11, 10, 2, 48), z(2026, 11, 9, 5), EST),
        # A morning snapshot (09:00 New York) is its own day.
        (z(2026, 11, 10, 14), z(2026, 11, 10, 5), EST),
        # UTC midnight is 19:00 the day before in New York.
        (z(2026, 11, 10), z(2026, 11, 9, 5), EST),
        # One second before New York midnight, and New York midnight itself (a start at its own instant).
        (z(2026, 11, 10, 4, 59, 59), z(2026, 11, 9, 5), EST),
        (z(2026, 11, 10, 5), z(2026, 11, 10, 5), EST),
        # Summer (EDT): 23:30 New York on 15 Jul is 03:30Z on the 16th.
        (z(2026, 7, 16, 3, 30), z(2026, 7, 15, 4), EDT),
    ],
    ids=['evening-2148', 'morning', 'utc-midnight', 'second-before-local-midnight', 'local-midnight', 'summer-evening'],
)
def test_the_start_is_new_york_midnight_of_the_entered_as_of_s_new_york_date(xnys, entered, start, offset):
    got = panel_dataset_start(SOURCE, entered)
    assert got == start
    assert got.utcoffset() == offset, 'the local midnight carries the local offset of that date'
    assert got.astimezone(xnys.tz).time() == time.min
    assert got <= entered < got + DAY + HOUR, 'the snapshot day is included: the start is never after the as_of'


def test_the_evening_snapshot_starts_on_its_local_day_not_its_utc_day():
    """P2's named case: the UTC date would be the wrong day (a day late, a bar the list was valid for dropped)."""
    entered = z(2026, 11, 10, 2, 48)
    assert panel_dataset_start(SOURCE, entered) == ny(date(2026, 11, 9))
    assert panel_dataset_start(SOURCE, entered) != ny(entered.date())


@pytest.mark.parametrize(
    ('entered', 'day'),
    [
        (z(2026, 11, 7, 18), date(2026, 11, 7)),  # Saturday 13:00 New York
        (z(2026, 11, 9, 1), date(2026, 11, 8)),  # Sunday 20:00 New York, already Monday in UTC
        (z(2026, 11, 26, 20), date(2026, 11, 26)),  # Thanksgiving, a full holiday
    ],
    ids=['saturday', 'sunday-evening', 'thanksgiving'],
)
def test_a_weekend_or_holiday_date_starts_at_that_date_s_own_local_midnight(xnys, entered, day):
    """P3: a calendar date, never moved to a session. The date holds no session (the precondition of the pin)."""
    assert xnys.sessions_overlapping(ny(day), ny(day + DAY)) == []
    assert panel_dataset_start(SOURCE, entered) == ny(day)


@pytest.mark.parametrize(
    ('entered', 'start', 'offset'),
    [
        # March 2026: clocks go forward at 02:00 on Sunday 8 March.
        (z(2026, 3, 7, 22), z(2026, 3, 7, 5), EST),  # Saturday, still EST
        (z(2026, 3, 8, 12), z(2026, 3, 8, 5), EST),  # the change day: its midnight is before 02:00, so EST
        (z(2026, 3, 9, 15), z(2026, 3, 9, 4), EDT),  # Monday, EDT
        (z(2026, 3, 9, 3, 30), z(2026, 3, 8, 5), EST),  # 23:30 EDT on the change day is still that day
        # November 2026: clocks go back at 02:00 on Sunday 1 November.
        (z(2026, 10, 31, 20), z(2026, 10, 31, 4), EDT),  # Saturday, EDT
        (z(2026, 11, 1, 12), z(2026, 11, 1, 4), EDT),  # the change day: its midnight is before 02:00, so EDT
        (z(2026, 11, 2, 15), z(2026, 11, 2, 5), EST),  # Monday, EST
        (z(2026, 11, 2, 4, 30), z(2026, 11, 1, 4), EDT),  # 23:30 EST on the change day is still that day
    ],
    ids=[
        'mar-sat-est',
        'mar-change-day',
        'mar-mon-edt',
        'mar-change-day-late-evening',
        'nov-sat-edt',
        'nov-change-day',
        'nov-mon-est',
        'nov-change-day-late-evening',
    ],
)
def test_the_dst_changes_give_minus_five_or_minus_four_by_the_date(entered, start, offset):
    got = panel_dataset_start(SOURCE, entered)
    assert got == start and got.utcoffset() == offset


def test_an_instant_in_another_zone_is_the_same_instant():
    """The rule converts the instant, never reads its wall clock: 11:48 in Tokyo on the 10th is 21:48 NY on the 9th."""
    tokyo = z(2026, 11, 10, 2, 48).astimezone(ZoneInfo('Asia/Tokyo'))
    assert tokyo.date() == date(2026, 11, 10)
    assert panel_dataset_start(SOURCE, tokyo) == ny(date(2026, 11, 9))


def test_a_naive_instant_is_refused():
    with pytest.raises(NaiveDatetimeError, match='entered_as_of'):
        panel_dataset_start(SOURCE, datetime(2026, 11, 10, 2, 48))


@pytest.mark.parametrize('source', [s for s in DataSource if s not in SOURCE_CALENDARS], ids=str)
def test_a_source_with_no_calendar_raises_and_never_falls_back_to_utc(source):
    with pytest.raises(UnknownCalendarError):
        panel_dataset_start(source, z(2026, 11, 10, 2, 48))


def test_there_is_a_source_with_no_calendar_to_pin():
    assert {s for s in DataSource if s not in SOURCE_CALENDARS}, 'every source has a calendar: the pin above is empty'


# ===================================================================================== (b) health and the refresh


def panel_row(start: datetime, last_bar: datetime | None = None, written: datetime | None = None) -> CatalogRow:
    """A panel-owned DAILY 1day ROLLING open-ended entry, collecting, as the delivery intake mints it."""
    return CatalogRow(
        id=uuid.uuid4(),
        asset_symbol='AAA',
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=SOURCE,
        feed=Feed.IEX,
        granularity=Granularity.ONE_DAY,
        start=start,
        end=None,
        update_type=UpdateType.DAILY,
        expiry_type=ExpiryType.ROLLING,
        expiry=None,
        owner='panel',
        last_bar=last_bar,
        last_bar_written_at=written,
        collect=True,
    )


def judge(row: CatalogRow, calendar, now: datetime) -> DatasetHealthStatus:
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
    ).status


# A Saturday snapshot (13:00 New York, 7 Nov): XNYS's next session is Monday 9 Nov, closing 21:00Z (16:00 EST).
SATURDAY_SNAPSHOT = z(2026, 11, 7, 18)
MONDAY_CLOSE = z(2026, 11, 9, 21)


@pytest.mark.parametrize(
    ('now', 'status'),
    [
        (z(2026, 11, 8, 12), DatasetHealthStatus.FRESH),  # Sunday
        (z(2026, 11, 9, 15), DatasetHealthStatus.FRESH),  # Monday mid-session
        (MONDAY_CLOSE - timedelta(seconds=1), DatasetHealthStatus.FRESH),
        (MONDAY_CLOSE, DatasetHealthStatus.LATE),  # Monday's bar is owed from its close
        (z(2026, 11, 10, 14, 30), DatasetHealthStatus.OVERDUE),  # Tuesday's open
    ],
    ids=['sunday', 'monday-session', 'just-before-monday-close', 'monday-close', 'tuesday-open'],
)
def test_a_saturday_start_owes_nothing_until_monday_closes(xnys, now, status):
    """P3: the first fetch stores no bar (no session); nothing is owed before the first session after the start."""
    row = panel_row(panel_dataset_start(SOURCE, SATURDAY_SNAPSHOT))
    assert row.start == ny(date(2026, 11, 7))
    assert judge(row, xnys, now) is status


def test_a_fresh_saturday_start_is_not_owed_by_the_refresh_and_is_owed_after_monday_closes(xnys):
    row = panel_row(panel_dataset_start(SOURCE, SATURDAY_SNAPSHOT))
    assert DailyRefresher._behind([row], frozenset(), xnys, z(2026, 11, 9, 15)) == []
    owed = DailyRefresher._behind([row], frozenset(), xnys, MONDAY_CLOSE + timedelta(minutes=30))
    assert [r.id for r in owed] == [row.id]
    (productive, again), skipped = DailyRefresher._plan(owed, [], None, xnys)
    assert (skipped, again) == (0, [])
    assert [planned.fetch_start for planned in productive] == [ny(date(2026, 11, 7))], 'from the point-in-time start'


def test_an_evening_snapshot_s_first_bar_is_final_and_the_dataset_reads_fresh_until_the_next_close(xnys):
    """P2: taken after the close (21:48 New York, Monday), Monday's bar is complete; the first fetch stores it."""
    start = panel_dataset_start(SOURCE, z(2026, 11, 10, 2, 48))
    monday_bar, written = ny(date(2026, 11, 9)), z(2026, 11, 10, 2, 50)
    row = panel_row(start, monday_bar, written)
    assert row.start == monday_bar, "the snapshot day's bar is inside [start, end)"
    assert judge(row, xnys, z(2026, 11, 10, 3)) is DatasetHealthStatus.FRESH
    assert judge(row, xnys, z(2026, 11, 10, 20, 59)) is DatasetHealthStatus.FRESH
    assert judge(row, xnys, z(2026, 11, 10, 21)) is DatasetHealthStatus.LATE, "then Tuesday's bar is owed"


def test_a_mid_session_snapshot_s_first_bar_is_provisional_late_then_fresh_after_the_rewrite(xnys):
    """P2: taken at 12:00 New York on Tuesday, the first fetch's Tuesday bar is part-way (LATE after the close)."""
    start = panel_dataset_start(SOURCE, z(2026, 11, 10, 17))
    tuesday_bar = ny(date(2026, 11, 10))
    assert start == tuesday_bar
    provisional = panel_row(start, tuesday_bar, z(2026, 11, 10, 17, 5))
    assert judge(provisional, xnys, z(2026, 11, 10, 18)) is DatasetHealthStatus.FRESH
    assert judge(provisional, xnys, z(2026, 11, 10, 21, 30)) is DatasetHealthStatus.LATE
    assert judge(provisional, xnys, z(2026, 11, 11, 14, 30)) is DatasetHealthStatus.OVERDUE
    rewritten = panel_row(start, tuesday_bar, z(2026, 11, 10, 21, 35))
    assert judge(rewritten, xnys, z(2026, 11, 10, 21, 40)) is DatasetHealthStatus.FRESH


def test_a_thanksgiving_start_owes_nothing_until_friday_s_early_close(xnys):
    """P3 on a holiday: Wednesday's session is before the start, so nothing is owed until Friday closes (18:00Z)."""
    row = panel_row(panel_dataset_start(SOURCE, z(2026, 11, 26, 20)))
    assert judge(row, xnys, z(2026, 11, 26, 22)) is DatasetHealthStatus.FRESH
    assert judge(row, xnys, z(2026, 11, 27, 17, 59)) is DatasetHealthStatus.FRESH
    assert judge(row, xnys, z(2026, 11, 27, 18)) is DatasetHealthStatus.LATE


@pytest.mark.asyncio
async def test_the_catalog_gives_a_point_in_time_panel_entry_health():
    """dataset_catalog.evaluate_views, the path the UI reads (DAILY rows need no statement, so no session)."""
    rows = [panel_row(panel_dataset_start(SOURCE, SATURDAY_SNAPSHOT)), panel_row(ny(date(2026, 11, 9)))]
    views = await evaluate_views(None, rows, z(2026, 11, 9, 15), CONFIG)
    assert [view.health.status for view in views] == [DatasetHealthStatus.FRESH, DatasetHealthStatus.FRESH]


# ===================================================================================== (c) the manager


@pytest_asyncio.fixture
async def world(monkeypatch):
    built = await make_world(monkeypatch)
    try:
        yield built
    finally:
        await built.engine.dispose()


@pytest.fixture
def reads(monkeypatch):
    """Every call of the manager's ledger read (panel_manager.entered_as_of_by_member), still answered by the real one."""
    calls = []
    real = panel_manager.entered_as_of_by_member

    async def counting(db, panel_id):
        calls.append(panel_id)
        return await real(db, panel_id)

    monkeypatch.setattr(panel_manager, 'entered_as_of_by_member', counting)
    return calls


def now() -> datetime:
    return datetime.now(UTC)


def evening(days_ago: int) -> datetime:
    """21:48 New York, `days_ago` New York days before today: always the NEXT day in UTC (P2's named case)."""
    today = now().astimezone(NEW_YORK).date()
    instant = datetime.combine(today - timedelta(days=days_ago), time(21, 48), tzinfo=NEW_YORK).astimezone(UTC)
    assert instant.date() != instant.astimezone(NEW_YORK).date()
    return instant


def local_start(as_of: datetime) -> datetime:
    return ny(as_of.astimezone(NEW_YORK).date())


def t(vendor: str, symbol: str, kind: PanelTransition, as_of: datetime, **kw) -> NewTransition:
    return NewTransition(vendor, symbol, kind, as_of, **kw)


async def bars_by_symbol(world) -> dict[str, object]:
    return {row.asset_symbol: row for row in await world.requests(IngestRequestKind.BARS)}


async def snapshot_as_of(world, snapshot_id) -> datetime:
    (row,) = await world.rows(SNAPSHOTS, SNAPSHOTS.c.id == snapshot_id)
    return row.as_of


@pytest.mark.asyncio
async def test_a_first_pass_starts_every_new_dataset_at_the_snapshot_s_local_date(world, reads, monkeypatch):
    """P4 first pass, P2: an evening snapshot. The bars request, the clamp body and the own-overlap key carry it."""
    await world.source_settings()
    await world.panel(now() - 2 * DAY)
    snap = evening(3)
    await world.snapshot(snap, [('v1', 'AAA'), ('v2', 'BBB')])
    keys = []
    real = panel_manager.check_own_overlap

    async def recording(db, key):
        keys.append(key)
        return await real(db, key)

    monkeypatch.setattr(panel_manager, 'check_own_overlap', recording)

    await manager(Clock()).tick()

    expected = local_start(snap)
    assert expected != ny(snap.date()), 'the precondition: the UTC date is the next day'
    bars = await bars_by_symbol(world)
    assert sorted(bars) == ['AAA', 'BBB']
    assert {row.requested_start for row in bars.values()} == {expected}
    assert [call.body.start for call in world.clamps] == [expected, expected]
    assert [key.start for key in keys] == [expected, expected]
    assert reads == [], 'a first pass of new members only reads no ledger: their ENTERED is the one it writes'


@pytest.mark.asyncio
async def test_an_evidence_backdated_entry_starts_at_the_first_snapshot_showing_it(world, reads):
    """P1, P4: an evidence-backdated ENTERED gives that earlier date.

    Under the same settings version an appeared member's ENTERED is dated by the first snapshot showing it, so its start
    is that snapshot's local date, before the pass's own snapshot and the creation day.
    """
    await world.source_settings()
    seeded = await world.panel(now() - 8 * DAY)
    s0 = await world.snapshot(now() - 7 * DAY, [('v9', 'ZZZ')])
    await world.snapshot(evening(6), [('v9', 'ZZZ')])
    appeared = evening(5)
    await world.snapshot(appeared, [('v9', 'ZZZ'), ('v1', 'AAA')])
    newest = evening(3)
    await world.snapshot(newest, [('v9', 'ZZZ'), ('v1', 'AAA')])
    keep = await world.request('ZZZ', state=IngestRequestState.QUEUED)
    as_of0 = await snapshot_as_of(world, s0)
    await world.ledger_pass(
        seeded,
        s0,
        [t('v9', 'ZZZ', ENTERED, as_of0), t('v9', 'ZZZ', CREATED, as_of0, request_id=keep)],
        started_at=now() - 7 * DAY + timedelta(minutes=1),
    )
    await world.age_window()

    await manager(Clock()).tick()

    (entered,) = [r for r in await world.transitions(seeded) if r.vendor_asset_id == 'v1' and r.transition is ENTERED]
    assert entered.as_of == appeared, 'the precondition: ENTERED is evidence-backdated'
    (bars,) = [row for row in await world.requests(IngestRequestKind.BARS) if row.id != keep]
    assert bars.asset_symbol == 'AAA'
    assert bars.requested_start == local_start(appeared) != local_start(newest)
    assert reads == [], 'v1 is new in this pass: no ledger read'


@pytest.mark.asyncio
async def test_a_late_entrant_starts_at_its_own_entry_date_never_the_panel_s_first(world):
    """P4: a later entrant starts at its own ENTERED date.

    A new listing entering under a new settings version is dated by this pass's snapshot; the earlier members keep
    theirs (nothing is created for them again).
    """
    await world.source_settings()
    seeded = await world.panel(now() - 9 * DAY)
    first = evening(8)
    s0 = await world.snapshot(first, [('v9', 'ZZZ')])
    keep = await world.request('ZZZ', state=IngestRequestState.QUEUED)
    await world.ledger_pass(
        seeded,
        s0,
        [t('v9', 'ZZZ', ENTERED, first), t('v9', 'ZZZ', CREATED, first, request_id=keep)],
        started_at=now() - 8 * DAY,
    )
    await world.age_window()
    later = evening(2)
    await world.snapshot(later, [('v9', 'ZZZ'), ('v1', 'AAA')])
    await world.settings(seeded.panel_id)

    await manager(Clock()).tick()

    (bars,) = [row for row in await world.requests(IngestRequestKind.BARS) if row.id != keep]
    assert bars.asset_symbol == 'AAA', 'the earlier member is not created again'
    assert bars.requested_start == local_start(later) > local_start(first)


async def _deferred_ledger(world, members, *, entered_at, deferred_at):
    """A panel whose last finished MEMBERSHIP pass entered and deferred `members`, so the next tick is a RELEASE.

    ENTERED at entered_at, DEFERRED at deferred_at (the pass's snapshot); MONTHLY, so no membership pass is due.
    `members` maps vendor id to symbol; a symbol prefixed '!' has its ENTERED row left out (a corrupt ledger).
    """
    await world.source_settings()
    seeded = await world.panel(now() - 10 * DAY, cadence=PanelCadence.MONTHLY)
    deciding = await world.snapshot(deferred_at, [(v, s.lstrip('!')) for v, s in members.items()])
    rows = []
    for vendor, symbol in members.items():
        if not symbol.startswith('!'):
            rows.append(t(vendor, symbol, ENTERED, entered_at))
        rows.append(t(vendor, symbol.lstrip('!'), DEFERRED, deferred_at, cause=panel_manager.CAUSE_RAMP_RATE))
    await world.ledger_pass(seeded, deciding, rows, started_at=now() - HOUR)
    await world.age_window()
    return seeded


@pytest.mark.asyncio
async def test_a_deferred_member_released_days_later_starts_at_its_entered_date_not_its_creation_date(world, reads):
    """P4: a member DEFERRED and created by a RELEASE days later starts at its ENTERED date.

    ENTERED evening 6 days ago, DEFERRED by the ramp on the pass snapshot 2 days ago, released now. The start is the
    ENTERED date: not the release's snapshot (CREATED's as_of) and not the creation day.
    """
    entered_at, deferred_at = evening(6), evening(2)
    seeded = await _deferred_ledger(world, {'v1': 'AAA', 'v2': 'BBB'}, entered_at=entered_at, deferred_at=deferred_at)

    await manager(Clock()).tick()

    rows = await world.transitions(seeded)
    assert kinds(rows)[-2:] == [('v1', 'CREATED'), ('v2', 'CREATED')], 'the release created both'
    assert {row.as_of for row in rows[-2:]} == {deferred_at}, "CREATED keeps the deciding snapshot's as_of"
    bars = await bars_by_symbol(world)
    expected = local_start(entered_at)
    assert {row.requested_start for row in bars.values()} == {expected}
    assert expected not in (local_start(deferred_at), local_start(now()))
    assert [call.body.start for call in world.clamps] == [expected, expected]
    assert reads == [seeded.panel_id], 'the release reads the ledger ONCE, before its loop'


@pytest.mark.asyncio
async def test_a_member_deferred_then_created_by_a_later_membership_pass_starts_at_its_entered_date(world, reads):
    """P4: the next MEMBERSHIP pass (a newer snapshot) creates the deferred member: ENTERED date, one read."""
    await world.source_settings(ramp=1)
    seeded = await world.panel(now() - 6 * DAY)
    first = evening(5)
    await world.snapshot(first, [('v1', 'AAA'), ('v2', 'BBB'), ('v3', 'CCC')])
    await manager(Clock()).tick()
    assert kinds(await world.transitions(seeded))[-2:] == [('v3', 'ENTERED'), ('v3', 'DEFERRED')]
    for row in await world.passes(seeded):
        await world.put(PASSES.update().where(PASSES.c.id == row.id).values(started_at=row.started_at - 3 * DAY))
    await world.age_window()
    await world.source_settings(ramp=None, cap=None)
    newer = evening(1)
    await world.snapshot(newer, [('v1', 'AAA'), ('v2', 'BBB'), ('v3', 'CCC')])

    await manager(Clock()).tick()

    passes = await world.passes(seeded)
    assert len(passes) == 2 and passes[-1].kind.value == 'MEMBERSHIP'
    bars = await bars_by_symbol(world)
    assert sorted(bars) == ['AAA', 'BBB', 'CCC']
    assert {row.requested_start for row in bars.values()} == {local_start(first)}, 'v2, v3: their first-pass ENTERED'
    assert local_start(first) != local_start(newer)
    assert reads == [seeded.panel_id], 'one ledger read for the pass, not one per member'


@pytest.mark.asyncio
async def test_a_member_with_no_entered_row_is_skipped_and_logged_and_never_given_a_start(world, reads, caplog):
    """A member with no ENTERED row is skipped and logged, never given a fallback start (1990 or any other).

    A corrupt ledger (DEFERRED with no ENTERED): an ERROR naming the vendor id, no clamp, no request, no CREATED; the
    next member is created as usual.
    """
    entered_at = evening(6)
    seeded = await _deferred_ledger(world, {'v1': '!AAA', 'v2': 'BBB'}, entered_at=entered_at, deferred_at=evening(2))

    with caplog.at_level(logging.ERROR):
        await manager(Clock()).tick()

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and 'v1' in errors[0] and 'ENTERED' in errors[0]
    assert 'AAA' not in errors[0], 'logs name ids, never a symbol'
    assert [call.symbol for call in world.clamps] == ['BBB']
    bars = await bars_by_symbol(world)
    assert list(bars) == ['BBB'] and bars['BBB'].requested_start == local_start(entered_at)
    assert ('v1', 'CREATED') not in kinds(await world.transitions(seeded))
    assert reads == [seeded.panel_id]


@pytest.mark.asyncio
async def test_a_member_with_no_entered_row_is_skipped_before_the_budget_check(world):
    """With the ramp window full, a member that reached the budget would be DEFERRED; the corrupt one writes nothing.

    The ledger: v1 CREATED alone (its request EXPIRED, so it is to be created again) and no ENTERED; v2 and v3 entered
    and created in the last hour, filling a ramp of 1. A MEMBERSHIP pass is due (DAILY, the last pass two days back).
    """
    await world.source_settings(ramp=1)
    seeded = await world.panel(now() - 3 * DAY)
    snapshot_id = await world.snapshot(now() - HOUR, [('v1', 'AAA'), ('v2', 'BBB')])
    as_of = await snapshot_as_of(world, snapshot_id)
    expired = await world.request(
        'AAA', state=IngestRequestState.FAILED, failure_cause='EXPIRED', finished_at=now() - DAY
    )
    open_one = await world.request('BBB', state=IngestRequestState.QUEUED)
    await world.ledger_pass(
        seeded,
        snapshot_id,
        [
            t('v1', 'AAA', CREATED, as_of, request_id=expired),
            t('v2', 'BBB', ENTERED, as_of),
            t('v2', 'BBB', CREATED, as_of, request_id=open_one),
        ],
        started_at=now() - 2 * DAY,
    )
    before = await world.transitions(seeded)

    await manager(Clock()).tick()

    assert len(await world.passes(seeded)) == 2, 'the precondition: a membership pass ran'
    new_rows = (await world.transitions(seeded))[len(before) :]
    assert [(r.vendor_asset_id, r.transition) for r in new_rows if r.vendor_asset_id == 'v1'] == []
    assert world.clamps == [] and len(await world.requests(IngestRequestKind.BARS)) == 2


@pytest.mark.asyncio
async def test_a_re_entrant_that_already_has_a_dataset_creates_nothing_keeps_its_start_and_reads_no_ledger(
    world, reads
):
    """P4 re-entry: the manager-stopped entry is restarted, its declared start untouched."""
    await world.source_settings()
    seeded = await world.panel(now() - 6 * DAY)
    snapshot_id = await world.snapshot(evening(1), [('v1', 'AAA')])
    entry_id = await world.entry('AAA', collect=False, stopped_cause='PANEL_MEMBER_LEFT')
    old = evening(5)
    await world.ledger_pass(
        seeded,
        snapshot_id,
        [t('v1', 'AAA', ENTERED, old), t('v1', 'AAA', CREATED, old, request_id=uuid.uuid4())],
        started_at=now() - 5 * DAY,
    )
    await world.ledger_pass(
        seeded, snapshot_id, [t('v1', 'AAA', PanelTransition.EXITED, old + DAY)], started_at=now() - 4 * DAY
    )

    await manager(Clock()).tick()

    assert kinds(await world.transitions(seeded))[-1] == ('v1', 'ENTERED')
    assert await world.requests() == [] and world.clamps == []
    entry = await world.entry_row(entry_id)
    assert entry.collect is True and entry.start == ENTRY_START, 'the restart keeps the stored start'
    assert reads == []


@pytest.mark.asyncio
async def test_a_dataset_another_panel_already_holds_is_shared_and_keeps_the_earlier_start(world, reads):
    """P4: a second panel selecting a symbol whose panel entry exists creates nothing; the earlier start stands."""
    await world.source_settings()
    await world.panel(now() - 30 * DAY, allow_symbols=['XXX'])
    entry_id = await world.entry('XXX')
    second = await world.panel(now() - DAY, allow_symbols=['XXX'])
    await world.snapshot(evening(1), [('vx', 'XXX')])

    await manager(Clock()).tick()

    assert kinds(await world.transitions(second)) == [('vx', 'ENTERED')]
    assert await world.requests(IngestRequestKind.BARS) == []
    assert (await world.entry_row(entry_id)).start == ENTRY_START < local_start(evening(1))
    assert reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize(('shift', 'moves'), [(2 * DAY, True), (-2 * DAY, False), (timedelta(0), False)])
async def test_the_clamp_runs_on_the_new_start_and_only_a_later_rename_moves_it(world, shift, moves):
    """P5: the clamp runs on the point-in-time start and only a rename after it moves it.

    The body the clamp gets carries the new start; a rename after it moves the bars request's start, one at or before it
    leaves it (the real clamp's rule, mirrored by the harness double).
    """
    await world.source_settings()
    await world.panel(now() - 2 * DAY)
    snap = evening(3)
    await world.snapshot(snap, [('v1', 'AAA')])
    declared = local_start(snap)
    world.clamp_starts['AAA'] = declared + shift

    await manager(Clock()).tick()

    (call,) = world.clamps
    assert call.body.start == declared
    (bars,) = await world.requests(IngestRequestKind.BARS)
    assert bars.requested_start == (declared + shift if moves else declared)


def test_a_creation_still_costs_two_requests():
    """P5 and ADR tj-2ayia2 D1: the clamp query and the bars request; the windows' arithmetic is unchanged."""
    assert REQUESTS_PER_CREATION == 2


@pytest.mark.asyncio
async def test_the_minimum_cap_still_admits_exactly_one_point_in_time_creation_an_hour(world):
    await world.source_settings(ramp=None, cap=2)
    seeded = await world.panel(now() - 2 * DAY)
    snap = evening(3)
    await world.snapshot(snap, [('v1', 'AAA'), ('v2', 'BBB')])

    await manager(Clock()).tick()

    assert kinds(await world.transitions(seeded)) == [
        ('v1', 'ENTERED'),
        ('v1', 'CREATED'),
        ('v2', 'ENTERED'),
        ('v2', 'DEFERRED'),
    ]
    assert len(await world.requests()) == REQUESTS_PER_CREATION
    (bars,) = await world.requests(IngestRequestKind.BARS)
    assert bars.requested_start == local_start(snap)


# ===================================================================================== (d) no 1990


def test_no_point_in_time_start_is_the_old_calendar_start(xnys):
    """The constant is gone from the panel path; CALENDAR_START stays for the calendars and the clamp window."""
    assert 'CALENDAR_START' not in vars(panel_manager)
    assert date(1990, 1, 1) == CALENDAR_START
    for entered in (z(2026, 11, 10, 2, 48), SATURDAY_SNAPSHOT, z(2026, 3, 8, 12)):
        assert panel_dataset_start(SOURCE, entered).astimezone(xnys.tz).date() != CALENDAR_START
