"""The panel manager's pure rules (validator, tj-7zjov1.180, P22v): no database, the clock and the calendar are arguments.

THE DESIGN checked against: P22 tj-7zjov1.34 (its 14:02 UTC note's as_of rule, the 14:16 UTC rate design, the 03:29 UTC
consolidation naming the cause constants, the consolidated creation ruling of 2026-10-31, option D), ADR tj-26jt9w
addenda 20 and 21, and the pins on tj-7zjov1.180: (10) the pure membership due function on the real
ExchangeCalendarAdapter, (13) the cause constants, the creation ruling's re-creation rule (14).

  R1 membership_due_at: first pass at creation; MONTHLY one calendar month from the last pass, day clamped (Jan 31 to
     Feb 28, leap Feb 29, Mar 31 to Apr 30, Dec to Jan), the UTC time kept across a DST change; WEEKLY +7 days; DAILY
     the next session close plus settle on the source calendar, over a holiday and a half day; a naive instant refused;
  R2 snapshot_gate: not before due; at once on a snapshot at or after due; else wait until the next close plus settle
     after due, then run and say the snapshot is older; a FIRST pass (tj-7zjov1.230) runs at once and says whether the
     snapshot is older than the latest close plus settle;
  R3 row_selected / select_members: every filter, the empty allow list, the case of the symbol, an unsaid active
     flag read from the list, an unsaid tradable flag never matching; the cut in symbol order then vendor id, after
     the filters;
  R4 evidence_as_of: the first snapshot showing the new outcome, else the default;
  R5 budget_cause: ramp first, then the TWO requests of a creation against the cap, at both boundaries;
  R6 member_action: the table of the creation ruling (existing entry, open request, finished without a dataset,
     PERMANENT, REFUSED, DEFERRED);
  R7 the cause strings, by value (free strings in the schema, pinned here).
"""

import ast
import inspect
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource
from data.store.app import panel_manager
from data.store.app.database.crud.asset_list import SnapshotRecord, SnapshotRow
from data.store.app.database.crud.panel_settings import PanelSettings
from data.store.app.database.models.ingest_request import IngestRequestFailureCause, IngestRequestState
from data.store.app.database.models.panel import PanelCadence, PanelTransition
from data.store.app.panel_manager import (
    CAUSE_CLAMP_UNAVAILABLE,
    CAUSE_CLAMP_WINDOW,
    CAUSE_OWN_OVERLAP,
    CAUSE_RAMP_RATE,
    CAUSE_REQUEST_RATE,
    CAUSE_VENDOR_REFUSED,
    MemberAction,
    budget_cause,
    evidence_as_of,
    member_action,
    membership_due_at,
    row_selected,
    select_members,
    snapshot_gate,
)
from data.store.app.trading_calendar import calendar_for_source
from data.store.tests.panel_manager_harness import EveryDayCalendar
from schemas.data_ingest.submit import AssetListStatus


pytestmark = [pytest.mark.data_store]

SETTLE = timedelta(minutes=30)
NEW_YORK = ZoneInfo('America/New_York')
CREATED = datetime(2026, 1, 5, 15, tzinfo=UTC)
ANY_CALENDAR = EveryDayCalendar()


@pytest.fixture(scope='module')
def xnys():
    return calendar_for_source(DataSource.ALPACA_API)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


# ----------------------------------------------------------------------------------------------- R1 due


@pytest.mark.parametrize('cadence', list(PanelCadence))
def test_the_first_pass_is_due_at_creation(cadence):
    assert membership_due_at(cadence, None, CREATED, ANY_CALENDAR, SETTLE) == CREATED


@pytest.mark.parametrize(
    ('last', 'due'),
    [
        (utc(2026, 1, 31, 15), utc(2026, 2, 28, 15)),
        (utc(2028, 1, 31, 15), utc(2028, 2, 29, 15)),
        (utc(2026, 3, 31, 15), utc(2026, 4, 30, 15)),
        (utc(2026, 12, 15, 15), utc(2027, 1, 15, 15)),
        (utc(2026, 12, 31, 23, 59), utc(2027, 1, 31, 23, 59)),
    ],
    ids=['jan31-feb28', 'leap-feb29', 'mar31-apr30', 'dec-jan', 'year-end'],
)
def test_monthly_is_one_calendar_month_from_the_last_pass_day_clamped(last, due):
    assert membership_due_at(PanelCadence.MONTHLY, last, CREATED, ANY_CALENDAR, SETTLE) == due


def test_monthly_keeps_the_utc_time_across_a_dst_change():
    # New York moves to EDT on 2026-03-08: the due instant is the same UTC time, not the same wall clock.
    last = utc(2026, 2, 20, 21, 30)
    due = membership_due_at(PanelCadence.MONTHLY, last, CREATED, ANY_CALENDAR, SETTLE)
    assert due == utc(2026, 3, 20, 21, 30)
    assert due.astimezone(NEW_YORK).hour != last.astimezone(NEW_YORK).hour


def test_weekly_is_seven_days_from_the_last_pass():
    last = utc(2026, 10, 30, 21, 35)
    assert membership_due_at(PanelCadence.WEEKLY, last, CREATED, ANY_CALENDAR, SETTLE) == last + timedelta(days=7)


def test_weekly_is_seven_utc_days_across_a_dst_change_not_seven_wall_clock_days():
    # New York leaves EDT on 2026-11-01: seven days of 24 hours, so the New York wall clock moves back an hour.
    last = datetime(2026, 10, 30, 17, 35, tzinfo=NEW_YORK)
    due = membership_due_at(PanelCadence.WEEKLY, last, CREATED, ANY_CALENDAR, SETTLE)
    assert due == last.astimezone(UTC) + timedelta(days=7)
    assert due.utcoffset() == timedelta(0)
    assert due.astimezone(NEW_YORK).hour == 16


@pytest.mark.parametrize(
    ('last', 'due'),
    [
        # Wed 2026-11-25 after its close plus settle: Thanksgiving (Thu 26) is a holiday, Fri 27 a half day (13:00 ET).
        (datetime(2026, 11, 25, 16, 35, tzinfo=NEW_YORK), datetime(2026, 11, 27, 13, 30, tzinfo=NEW_YORK)),
        # Exactly at the close plus settle: that session is done, the next one is due.
        (datetime(2026, 11, 25, 16, 30, tzinfo=NEW_YORK), datetime(2026, 11, 27, 13, 30, tzinfo=NEW_YORK)),
        # After the half day's close plus settle: the weekend is skipped.
        (datetime(2026, 11, 27, 13, 35, tzinfo=NEW_YORK), datetime(2026, 11, 30, 16, 30, tzinfo=NEW_YORK)),
        # During a session (before its close): that session's own close plus settle.
        (datetime(2026, 11, 24, 11, 0, tzinfo=NEW_YORK), datetime(2026, 11, 24, 16, 30, tzinfo=NEW_YORK)),
        # Between the close and the settle: the same session's close plus settle, the pass not yet repeated.
        (datetime(2026, 11, 24, 16, 10, tzinfo=NEW_YORK), datetime(2026, 11, 24, 16, 30, tzinfo=NEW_YORK)),
        # A Saturday: the Monday.
        (datetime(2026, 11, 28, 12, 0, tzinfo=NEW_YORK), datetime(2026, 11, 30, 16, 30, tzinfo=NEW_YORK)),
    ],
    ids=['over-holiday-to-half-day', 'at-settle', 'half-day-over-weekend', 'mid-session', 'in-settle', 'weekend'],
)
def test_daily_is_the_next_session_close_plus_settle_on_the_real_calendar(xnys, last, due):
    got = membership_due_at(PanelCadence.DAILY, last, CREATED, xnys, SETTLE)
    assert got == due.astimezone(UTC)
    assert got > last, 'a DAILY pass is never due again at or before the pass that ran'


def test_a_naive_instant_is_refused():
    with pytest.raises(ValueError):
        membership_due_at(PanelCadence.WEEKLY, datetime(2026, 1, 1), CREATED, ANY_CALENDAR, SETTLE)
    with pytest.raises(ValueError):
        membership_due_at(PanelCadence.WEEKLY, None, datetime(2026, 1, 1), ANY_CALENDAR, SETTLE)


# ----------------------------------------------------------------------------------------------- R2 snapshot gate

DUE = utc(2026, 10, 5, 21, 30)  # EveryDayCalendar: the next close plus settle after it is 10-06 21:30


def test_the_gate_is_shut_before_due():
    assert snapshot_gate(DUE - timedelta(seconds=1), DUE, DUE, ANY_CALENDAR, SETTLE) == (False, False)


@pytest.mark.parametrize('as_of', [DUE, DUE + timedelta(hours=1)])
def test_a_snapshot_at_or_after_due_runs_at_once(as_of):
    assert snapshot_gate(DUE, DUE, as_of, ANY_CALENDAR, SETTLE) == (True, False)


def test_an_older_snapshot_waits_until_the_next_close_plus_settle_then_runs_and_says_so():
    older = DUE - timedelta(minutes=1)
    give_up = utc(2026, 10, 6, 21, 30)
    assert snapshot_gate(give_up - timedelta(seconds=1), DUE, older, ANY_CALENDAR, SETTLE) == (False, True)
    assert snapshot_gate(give_up, DUE, older, ANY_CALENDAR, SETTLE) == (True, True)


# tj-7zjov1.230 (owner ruling (c) of 2026-10-27): the FIRST pass never waits; 'older' is against the latest close + settle.


def test_a_first_pass_runs_at_once_on_an_older_snapshot_and_says_so():
    older = DUE - timedelta(minutes=1)  # before 10-05 21:30, the latest close plus settle at DUE
    assert snapshot_gate(DUE, DUE, older, ANY_CALENDAR, SETTLE, first=True) == (True, True)
    assert snapshot_gate(DUE, DUE, older, ANY_CALENDAR, SETTLE) == (False, True), 'a later pass still waits'


@pytest.mark.parametrize('as_of', [DUE, DUE + timedelta(minutes=1)])
def test_a_first_pass_on_a_snapshot_at_or_after_the_latest_close_plus_settle_is_not_older(as_of):
    now = DUE + timedelta(hours=1)
    assert snapshot_gate(now, now, as_of, ANY_CALENDAR, SETTLE, first=True) == (True, False)


def test_a_first_pass_is_still_shut_before_its_due_time():
    assert snapshot_gate(DUE - timedelta(seconds=1), DUE, DUE, ANY_CALENDAR, SETTLE, first=True) == (False, False)


def test_a_first_pass_inside_the_settle_judges_against_the_previous_close():
    # 10-05 21:10: the close (21:00) is not settled yet, so the latest close plus settle is 10-04 21:30.
    now = utc(2026, 10, 5, 21, 10)
    assert snapshot_gate(now, now, utc(2026, 10, 5, 10), ANY_CALENDAR, SETTLE, first=True) == (True, False)
    assert snapshot_gate(now, now, utc(2026, 10, 4, 21, 29), ANY_CALENDAR, SETTLE, first=True) == (True, True)


def test_a_first_pass_with_no_completed_session_runs_and_is_not_older():
    no_session = SimpleNamespace(last_completed_session=lambda now: None)
    assert snapshot_gate(DUE, DUE, utc(2000, 1, 1), no_session, SETTLE, first=True) == (True, False)


# ----------------------------------------------------------------------------------------------- R3 filters


def row(vendor: str = 'v1', symbol: str = 'AAA', exchange: str = 'NYSE', active=True, tradable=True) -> SnapshotRow:
    return SnapshotRow(vendor_asset_id=vendor, symbol=symbol, exchange=exchange, active=active, tradable=tradable)


def settings(**kw) -> PanelSettings:
    return PanelSettings(enabled=True, size=kw.pop('size', 10), cadence=PanelCadence.DAILY, **kw)


ACTIVE = AssetListStatus.ACTIVE
STOCK = AssetType.STOCK


def test_no_filter_selects_every_row():
    assert row_selected(row(), settings(), STOCK, ACTIVE)


def test_the_asset_class_filter():
    assert row_selected(row(), settings(asset_class=STOCK), STOCK, ACTIVE)
    other = next(member for member in AssetType if member is not STOCK)
    assert not row_selected(row(), settings(asset_class=other), STOCK, ACTIVE)


def test_a_non_empty_allow_list_restricts_ignoring_case_and_an_empty_one_does_not():
    assert row_selected(row(symbol='AAA'), settings(allow_symbols=('aaa',)), STOCK, ACTIVE)
    assert not row_selected(row(symbol='BBB'), settings(allow_symbols=('AAA',)), STOCK, ACTIVE)
    assert row_selected(row(symbol='BBB'), settings(allow_symbols=()), STOCK, ACTIVE)


def test_the_deny_list_removes_ignoring_case_even_an_allowed_symbol():
    assert not row_selected(row(symbol='AAA'), settings(deny_symbols=('aaa',)), STOCK, ACTIVE)
    assert not row_selected(row(symbol='AAA'), settings(allow_symbols=('AAA',), deny_symbols=('AAA',)), STOCK, ACTIVE)


def test_the_exchange_filter():
    assert row_selected(row(exchange='NYSE'), settings(exchange='NYSE'), STOCK, ACTIVE)
    assert not row_selected(row(exchange='NASDAQ'), settings(exchange='NYSE'), STOCK, ACTIVE)


@pytest.mark.parametrize(
    ('flag', 'status', 'wanted', 'selected'),
    [
        (True, ACTIVE, True, True),
        (False, ACTIVE, True, False),
        (None, ACTIVE, True, True),
        (None, AssetListStatus.INACTIVE, True, False),
        (None, AssetListStatus.INACTIVE, False, True),
    ],
)
def test_an_unsaid_active_flag_is_read_from_the_list(flag, status, wanted, selected):
    assert row_selected(row(active=flag), settings(active=wanted), STOCK, status) is selected


@pytest.mark.parametrize(
    ('flag', 'wanted', 'selected'),
    [(True, True, True), (False, True, False), (None, True, False), (None, False, False)],
)
def test_an_unsaid_tradable_flag_never_matches_a_tradable_filter(flag, wanted, selected):
    assert row_selected(row(tradable=flag), settings(tradable=wanted), STOCK, ACTIVE) is selected
    assert row_selected(row(tradable=flag), settings(tradable=None), STOCK, ACTIVE)


def test_the_members_are_cut_to_size_in_symbol_order_then_vendor_id_after_the_filters():
    rows = [
        row('v5', 'EEE'),
        row('v2', 'BBB', tradable=False),
        row('v9', 'AAA'),
        row('v1', 'AAA'),
        row('v3', 'CCC'),
        row('v4', 'DDD'),
    ]
    chosen = select_members(rows, settings(size=3, tradable=True), STOCK, ACTIVE)
    assert [(member.symbol, member.vendor_asset_id) for member in chosen] == [
        ('AAA', 'v1'),
        ('AAA', 'v9'),
        ('CCC', 'v3'),
    ]
    assert select_members(list(reversed(rows)), settings(size=3, tradable=True), STOCK, ACTIVE) == chosen


# ----------------------------------------------------------------------------------------------- R4 evidence


def snapshot(day: int) -> SnapshotRecord:
    import uuid

    return SnapshotRecord(
        id=uuid.uuid4(),
        source=DataSource.ALPACA_API,
        asset_type=STOCK,
        asset_list=ACTIVE,
        request_id=uuid.uuid4(),
        attempt_no=1,
        as_of=utc(2026, 10, day, 21, 30),
        row_count=1,
    )


def test_evidence_is_the_first_snapshot_showing_the_new_outcome():
    s1, s2, s3 = snapshot(1), snapshot(2), snapshot(3)
    rows = {(s1.id, 'v1'): row(), (s3.id, 'v1'): row()}  # gone in s2, back in s3
    default = utc(2026, 10, 9)
    assert evidence_as_of('v1', False, [s1, s2, s3], rows, settings(), STOCK, ACTIVE, default) == s2.as_of
    assert evidence_as_of('v1', True, [s1, s2, s3], rows, settings(), STOCK, ACTIVE, default) == s1.as_of


def test_evidence_judges_the_filters_not_only_presence():
    s1, s2 = snapshot(1), snapshot(2)
    rows = {(s1.id, 'v1'): row(tradable=True), (s2.id, 'v1'): row(tradable=False)}
    got = evidence_as_of('v1', False, [s1, s2], rows, settings(tradable=True), STOCK, ACTIVE, utc(2026, 10, 9))
    assert got == s2.as_of


def test_no_snapshot_showing_the_outcome_falls_back_to_the_default():
    s1 = snapshot(1)
    default = utc(2026, 10, 9)
    assert evidence_as_of('v1', False, [s1], {(s1.id, 'v1'): row()}, settings(), STOCK, ACTIVE, default) == default
    assert evidence_as_of('v1', True, [], {}, settings(), STOCK, ACTIVE, default) == default


def test_pin_16a_a_baseline_already_showing_the_outcome_gives_the_default():
    """The size-cut rule: the filters selected it at the previous pass's snapshot too, so they did not change it."""
    s0, s1 = snapshot(1), snapshot(2)
    default = utc(2026, 10, 9)
    rows = {(s0.id, 'v1'): row(), (s1.id, 'v1'): row()}
    assert evidence_as_of('v1', True, [s1], rows, settings(), STOCK, ACTIVE, default, baseline=s0) == default
    assert evidence_as_of('v1', True, [s1], rows, settings(), STOCK, ACTIVE, default) == s1.as_of


def test_a_baseline_showing_the_old_outcome_still_backdates():
    s0, s1, s2 = snapshot(1), snapshot(2), snapshot(3)
    default = utc(2026, 10, 9)
    rows = {(s1.id, 'v1'): row(), (s2.id, 'v1'): row()}  # absent at the baseline: it appeared in s1
    assert evidence_as_of('v1', True, [s1, s2], rows, settings(), STOCK, ACTIVE, default, baseline=s0) == s1.as_of
    filtered = {(s0.id, 'v1'): row(tradable=False), (s1.id, 'v1'): row(tradable=True)}
    got = evidence_as_of('v1', True, [s1], filtered, settings(tradable=True), STOCK, ACTIVE, default, baseline=s0)
    assert got == s1.as_of


def test_the_baseline_also_holds_for_an_exit():
    s0, s1 = snapshot(1), snapshot(2)
    default = utc(2026, 10, 9)
    # Absent at the baseline already (not selected there): the exit is not dated by the later snapshot.
    assert evidence_as_of('v1', False, [s1], {}, settings(), STOCK, ACTIVE, default, baseline=s0) == default


# ----------------------------------------------------------------------------------------------- R4b clamp_refusal


def _clamp(start: datetime):
    from data.store.app.ingest.data_action_request import StartClamp

    return StartClamp('AAA', start, utc(2016, 1, 1), start)


@pytest.mark.parametrize(
    ('clamp_start', 'end', 'cause'),
    [
        (utc(2022, 6, 9), utc(2022, 6, 9), CAUSE_CLAMP_WINDOW),
        (utc(2022, 6, 10), utc(2022, 6, 9), CAUSE_CLAMP_WINDOW),
        (utc(2022, 6, 8, 23, 59), utc(2022, 6, 9), None),
        (utc(2022, 6, 9), None, None),
    ],
    ids=['at-end', 'after-end', 'before-end', 'open-ended'],
)
def test_pin_16b_clamp_refusal(clamp_start, end, cause):
    assert panel_manager.clamp_refusal(_clamp(clamp_start), end) == cause


@pytest.mark.parametrize('end', [None, utc(2022, 6, 9)])
def test_pin_16b_no_clamp_no_refusal(end):
    assert panel_manager.clamp_refusal(None, end) is None


# ----------------------------------------------------------------------------------------------- R5 budget


@pytest.mark.parametrize(
    ('created', 'requests', 'ramp', 'cap', 'cause'),
    [
        (0, 0, 1, 2, None),
        (0, 0, 0, 100, CAUSE_RAMP_RATE),
        (4, 0, 5, 100, None),
        (5, 0, 5, 100, CAUSE_RAMP_RATE),
        (0, 98, 5, 100, None),
        (0, 99, 5, 100, CAUSE_REQUEST_RATE),
        (0, 0, 5, 1, CAUSE_REQUEST_RATE),
        (5, 100, 5, 100, CAUSE_RAMP_RATE),
    ],
)
def test_ramp_is_checked_first_then_two_requests_against_the_cap(created, requests, ramp, cap, cause):
    assert budget_cause(created, requests, ramp, cap) == cause


def test_a_creation_costs_two_requests():
    assert panel_manager.REQUESTS_PER_CREATION == 2


# ----------------------------------------------------------------------------------------------- R6 member action


def state(kind: PanelTransition | None):
    return None if kind is None else SimpleNamespace(transition=kind, request_id=None, cause=None)


def request(state_: IngestRequestState, dataset_id=None, failure_cause: str | None = None):
    return SimpleNamespace(state=state_, dataset_id=dataset_id, failure_cause=failure_cause)


@pytest.mark.parametrize('kind', [None, *PanelTransition])
def test_an_existing_panel_entry_means_nothing_to_create(kind):
    assert member_action(state(kind), True, None) is MemberAction.NONE


@pytest.mark.parametrize(
    'kind',
    [
        None,
        PanelTransition.ENTERED,
        PanelTransition.EXITED,
        PanelTransition.DRAINED,
        PanelTransition.DRAINING,
        PanelTransition.DEFERRED,
    ],
)
def test_a_member_with_no_entry_is_created(kind):
    assert member_action(state(kind), False, None) is MemberAction.CREATE


def test_a_refused_member_stays_refused():
    assert member_action(state(PanelTransition.REFUSED), False, None) is MemberAction.NONE


@pytest.mark.parametrize(
    ('created_request', 'action'),
    [
        (None, MemberAction.NONE),
        (request(IngestRequestState.PENDING), MemberAction.NONE),
        (request(IngestRequestState.QUEUED), MemberAction.NONE),
        (request(IngestRequestState.NOT_DELIVERED), MemberAction.NONE),
        (request(IngestRequestState.SUCCEEDED, dataset_id='d'), MemberAction.NONE),
        (
            request(IngestRequestState.FAILED, failure_cause=IngestRequestFailureCause.PERMANENT.value),
            MemberAction.REFUSE_VENDOR,
        ),
        (
            request(IngestRequestState.FAILED, failure_cause=IngestRequestFailureCause.EXPIRED.value),
            MemberAction.CREATE,
        ),
        (
            request(IngestRequestState.FAILED, failure_cause=IngestRequestFailureCause.ATTEMPTS_EXHAUSTED.value),
            MemberAction.CREATE,
        ),
        (request(IngestRequestState.EMPTY), MemberAction.CREATE),
    ],
    ids=[
        'no-row',
        'pending',
        'queued',
        'not-delivered',
        'minted',
        'permanent',
        'expired',
        'exhausted',
        'empty-unminted',
    ],
)
def test_a_created_member_follows_its_request(created_request, action):
    assert member_action(state(PanelTransition.CREATED), False, created_request) is action


# ----------------------------------------------------------------------------------------------- R7 causes


def test_the_cause_strings_are_pinned_by_value():
    assert (
        CAUSE_RAMP_RATE,
        CAUSE_REQUEST_RATE,
        CAUSE_CLAMP_UNAVAILABLE,
        CAUSE_OWN_OVERLAP,
        CAUSE_CLAMP_WINDOW,
        CAUSE_VENDOR_REFUSED,
    ) == ('ramp_rate', 'request_rate', 'clamp_unavailable', 'own_overlap', 'clamp_window', 'vendor_refused')


def test_the_tick_is_five_minutes_and_the_window_one_hour():
    assert timedelta(minutes=5) == panel_manager.TICK_INTERVAL
    assert timedelta(minutes=60) == panel_manager.RATE_WINDOW


def test_an_unfinished_pass_holds_the_panel_off_for_one_hour():
    # tj-7zjov1.229: a pass that crashes every time retries at most once an hour, never at the tick rate.
    assert timedelta(hours=1) == panel_manager.UNFINISHED_PASS_RETRY


# ----------------------------------------------------------------------------------------------- R8 what it never calls


def _names_used(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {alias.name for alias in node.names}
    return names


def test_the_module_never_names_the_asset_list_request_the_awaiting_or_the_committing_helpers():
    """No asset-list request, no awaiting or committing helper, no daily bars: by the module's own syntax tree.

    P47v item 8 (the asset-list schedule is the one requester), the creation ruling's (7) and (2)(d), and the owner
    ruling of 2026-10-31 (the manager requests no daily bars).
    """
    forbidden = {
        'new_asset_list_request',
        'open_asset_list_request_exists',
        'ASSET_LIST',
        'await_verdict',
        'store_market_activity_worker',
        'submit_bars',
        '_check_overlap',
        'create_panel',
        'append_settings_version',
        'append_source_settings_version',
        'next_market_open',
    }
    assert _names_used(panel_manager) & forbidden == set()


def test_the_declared_start_is_the_calendar_start_at_the_source_calendar_s_local_midnight(xnys):
    """tj-7zjov1.239 ruling A': never UTC midnight, which is 1989-12-31 in New York, before the calendar's first day."""
    from data.store.app.trading_calendar import CALENDAR_START

    assert date(1990, 1, 1) == CALENDAR_START
    start = panel_manager.panel_dataset_start(DataSource.ALPACA_API)
    assert start == datetime.combine(CALENDAR_START, datetime.min.time(), tzinfo=NEW_YORK) == utc(1990, 1, 1, 5)
    assert start.astimezone(xnys.tz).date() == CALENDAR_START
    assert not hasattr(panel_manager, 'PANEL_DATASET_START'), 'the old constant is deleted, so no caller keeps it'
