"""Completeness and freshness BY PERIOD for 1week and 1month (validator, gating tj-grna9p.151).

DESIGN: decision tj-grna9p.8, addendum of 2026-10-20 (COMPLETENESS AND FRESHNESS FOR 1week AND 1month):
    (1) each granularity has a PERIOD (the session for 1min..1day, Monday..Sunday for 1week, the calendar month for
        1month, both in the calendar's zone); a bar covers its whole period; counts, windows and buckets stay in
        SESSIONS.
    (2) a period is EXPECTED when its first day's local midnight is in [start, end), it has ended, and it holds a
        session. A partial first period is not expected; a range ending inside a period keeps it.
    (3) DAILY 1week/1month: the expected last bar is the last ENDED period in range; FRESH when last_bar's period is at
        least it; LATE until the first session after the missed period opens, OVERDUE after.
    (4) 1min..1day are IDENTICAL to the per-session rules, pinned at the bottom against an oracle of the pre-change
        code (8f3ce43^), written out here.

Every case runs over the real XNYS calendar (calendar_for_source), as test_freshness.py does, so holidays, early closes
and DST are the exchange's. THE COUNTS ARE WRITTEN BY HAND from the 2019..2026 NYSE calendar and are worth reading
once, since a count derived from the code under test would pin nothing:
    Feb 2022  20 weekdays less Presidents' Day (21 Feb)        19 sessions
    Apr 2022  21 weekdays less Good Friday (15 Apr)            20
    Aug 2023  23 weekdays, no holiday                          23
    Feb 2026  20 weekdays less Presidents' Day (16 Feb)        19
    Mar 2026  22 weekdays, no holiday (Good Friday is 3 Apr)   22
    Jun 2026  22 weekdays less Juneteenth (Fri 19 Jun)         21
    Sep 2026  22 weekdays less Labor Day (Mon 7 Sep)           21
    Oct 2026  22 weekdays, no holiday                          22
    week of Mon 7 Sep 2026 (Labor Day)                          4
    week of Mon 3 Apr 2023 (Good Friday 7 Apr)                  4
    [Mon 3 Aug 2020, Mon 19 Oct 2026): 324 Mondays, 1560 sessions
DST in 2026: begins Sun 8 Mar, ends Sun 1 Nov. Both fall on a Sunday, the LAST day of a Monday..Sunday week.
"""

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app.completeness import (
    CoverageBucket,
    GapInterval,
    _in_range,
    coverage_buckets,
    covered_sessions,
    gap_intervals,
    period_start,
)
from data.store.app.daily_refresh import DailyRefresher
from data.store.app.database.crud.stock.dataset_catalog import CatalogRow
from data.store.app.health import DatasetHealth, FreshnessStatus, evaluate_dataset
from data.store.app.trading_calendar import TradingCalendar, calendar_for_source, session_of


pytestmark = pytest.mark.data_store

XNYS = calendar_for_source(DataSource.ALPACA_API)
NEW_YORK = ZoneInfo('America/New_York')
TICK = timedelta(microseconds=1)
WEEK = Granularity.ONE_WEEK
MONTH = Granularity.ONE_MONTH
OPEN_ENDED = datetime(2100, 1, 1, tzinfo=ZoneInfo('UTC'))
# Tue 20 Oct 2026 15:00Z, in session: the week of 12 Oct has ended, the week of 19 Oct and October have not.
NOW = datetime(2026, 10, 20, 15, 0, tzinfo=ZoneInfo('UTC'))
# After every 2026 period used below.
LATER = datetime(2026, 12, 31, tzinfo=ZoneInfo('UTC'))


def z(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def m(day: date) -> datetime:
    """A day's local midnight in New York: where a daily, weekly or monthly bar is stamped."""
    return datetime.combine(day, datetime.min.time(), tzinfo=NEW_YORK)


def d(text: str) -> date:
    return date.fromisoformat(text)


def weekly(first: date, last: date) -> list[date]:
    out = []
    while first <= last:
        out.append(first)
        first += timedelta(weeks=1)
    return out


def monthly(first: date, last: date) -> list[date]:
    out = []
    while first <= last:
        out.append(first)
        first = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    return out


def static(start, end, covered, granularity, now=NOW) -> DatasetHealth:
    return evaluate_dataset(
        update_type=UpdateType.STATIC,
        granularity=granularity,
        collect=None,
        start=start,
        end=end,
        last_bar=None,
        covered=covered,
        calendar=XNYS,
        now=now,
    )


def subscription(start, now, last_bar, granularity, end=OPEN_ENDED) -> DatasetHealth:
    return evaluate_dataset(
        update_type=UpdateType.DAILY,
        granularity=granularity,
        collect=True,
        start=start,
        end=end,
        last_bar=last_bar,
        covered=None,
        calendar=XNYS,
        now=now,
    )


def assert_gaps(health: DatasetHealth, count: int) -> None:
    if count == 0:
        assert (health.status, health.gap_count) == (FreshnessStatus.COMPLETE, 0), health
    else:
        assert (health.status, health.gap_count) == (FreshnessStatus.GAPS, count), health


# ------------------------------------------------------------------------------------------------- period_start
#
# The builder's report says no test reaches period_start directly. It is public (__all__), so it is pinned here.

PERIOD_START_CASES = {
    'week: a monday is its own start': (d('2026-10-12'), WEEK, d('2026-10-12')),
    'week: a wednesday': (d('2026-10-14'), WEEK, d('2026-10-12')),
    'week: a sunday is the LAST day of the week before it': (d('2026-10-18'), WEEK, d('2026-10-12')),
    'week: a saturday': (d('2026-10-17'), WEEK, d('2026-10-12')),
    'week: across a year end': (d('2026-01-01'), WEEK, d('2025-12-29')),
    'week: across a month end': (d('2026-11-01'), WEEK, d('2026-10-26')),
    'month: the 1st is its own start': (d('2026-10-01'), MONTH, d('2026-10-01')),
    'month: the 31st': (d('2026-10-31'), MONTH, d('2026-10-01')),
    'month: a leap day': (d('2024-02-29'), MONTH, d('2024-02-01')),
    'month: 31 december': (d('2026-12-31'), MONTH, d('2026-12-01')),
}


@pytest.mark.parametrize(('day', 'granularity', 'start'), PERIOD_START_CASES.values(), ids=PERIOD_START_CASES)
def test_period_start_is_the_monday_or_the_first_of_the_month(day, granularity, start):
    assert period_start(day, granularity) == start


@pytest.mark.parametrize('granularity', [g for g in Granularity if g not in (WEEK, MONTH)], ids=lambda g: str(g))
@pytest.mark.parametrize('day', [d('2026-10-14'), d('2026-10-18'), d('2026-10-31'), d('2024-02-29')], ids=str)
def test_period_start_of_an_intraday_or_daily_granularity_is_the_day_itself(granularity, day):
    assert period_start(day, granularity) == day


# ------------------------------------------------------------------------------------------------- 1week STATIC

WEEKLY_START, WEEKLY_END = m(d('2020-08-03')), m(d('2026-10-19'))
MONDAYS = weekly(d('2020-08-03'), d('2026-10-12'))


def test_the_weekly_window_holds_what_the_header_says():
    """Guard the guard: 324 Mondays and 1560 sessions, written by hand above, against the calendar."""
    assert len(MONDAYS) == 324
    assert len(XNYS.sessions_overlapping(WEEKLY_START, WEEKLY_END)) == 1560


def test_a_complete_weekly_dataset_is_complete():
    """One bar per Monday, 3 Aug 2020 to 12 Oct 2026, over [3 Aug 2020, 19 Oct 2026): every week has its bar.

    Before tj-grna9p.151 this read 1269 missing sessions, as the live SPY 1week did (the addendum's figure).
    """
    assert_gaps(static(WEEKLY_START, WEEKLY_END, set(MONDAYS), WEEK), 0)


def test_one_missing_week_is_its_five_sessions_in_one_window():
    covered = set(MONDAYS) - {d('2023-03-13')}
    assert_gaps(static(WEEKLY_START, WEEKLY_END, covered, WEEK), 5)
    assert gap_intervals(WEEKLY_START, WEEKLY_END, covered, XNYS, NOW, WEEK) == [
        GapInterval(d('2023-03-13'), d('2023-03-17'), 5)
    ]


def test_nothing_stored_misses_every_session_of_every_week():
    assert_gaps(static(WEEKLY_START, WEEKLY_END, None, WEEK), 1560)


LABOR_DAY = d('2026-09-07')


def test_a_holiday_monday_week_is_covered_by_a_bar_stamped_tuesday():
    """Labor Day: the vendor stamps the week's bar on the first session, Tuesday 8 Sep."""
    covered = (set(MONDAYS) - {LABOR_DAY}) | {d('2026-09-08')}
    assert LABOR_DAY in MONDAYS, 'precondition'
    assert_gaps(static(WEEKLY_START, WEEKLY_END, covered, WEEK), 0)


def test_a_missing_holiday_monday_week_is_its_four_sessions():
    covered = set(MONDAYS) - {LABOR_DAY}
    assert_gaps(static(WEEKLY_START, WEEKLY_END, covered, WEEK), 4)
    assert gap_intervals(WEEKLY_START, WEEKLY_END, covered, XNYS, NOW, WEEK) == [
        GapInterval(d('2026-09-08'), d('2026-09-11'), 4)
    ]


# Bars as the vendor stamps them, at each Monday's local midnight, read back through covered_sessions: the offset of
# that midnight changes with DST (05:00Z in EST, 04:00Z in EDT), and the change itself lands on a Sunday.
DST_WEEKS = {
    'march: EST week then EDT week': (d('2026-03-02'), d('2026-03-09'), d('2026-03-16')),
    'november: EDT week then EST week': (d('2026-10-26'), d('2026-11-02'), d('2026-11-09')),
}


@pytest.mark.parametrize(('first', 'second', 'end'), DST_WEEKS.values(), ids=DST_WEEKS)
def test_weeks_either_side_of_a_dst_change(first, second, end):
    stamps = [m(first), m(second)]
    assert stamps[0].utcoffset() != stamps[1].utcoffset(), 'precondition: the two Mondays are in different offsets'
    covered = covered_sessions(stamps, XNYS)
    assert_gaps(static(m(first), m(end), covered, WEEK, LATER), 0)
    assert_gaps(static(m(first), m(end), covered_sessions([stamps[0]], XNYS), WEEK, LATER), 5)
    assert gap_intervals(m(first), m(end), covered_sessions([stamps[0]], XNYS), XNYS, LATER, WEEK) == [
        GapInterval(second, second + timedelta(days=4), 5)
    ]


def test_a_bar_stamped_on_the_sunday_the_clocks_change_covers_the_week_that_ends_on_it():
    """Sun 1 Nov 2026 is the last day of the week of 26 Oct, not the first of the next (Monday..Sunday)."""
    sunday = m(d('2026-11-01'))
    assert session_of(sunday, XNYS) == d('2026-11-01')
    covered = covered_sessions([sunday, m(d('2026-11-02'))], XNYS)
    assert_gaps(static(m(d('2026-10-26')), m(d('2026-11-09')), covered, WEEK, LATER), 0)


# A range that starts on Wednesday 16 Sep: the week of 14 Sep is a PARTIAL FIRST PERIOD, its bar stamped on the 14th,
# before the range, so never stored. Only the weeks of 21 and 28 Sep (10 sessions) are expected.
MID_WEEK = m(d('2026-09-16'))


def test_a_partial_first_week_before_the_start_is_not_expected():
    assert_gaps(static(MID_WEEK, m(d('2026-10-05')), {d('2026-09-21'), d('2026-09-28')}, WEEK), 0)
    assert_gaps(static(MID_WEEK, m(d('2026-10-05')), None, WEEK), 10)
    # A bar inside the partial week covers nothing that is expected.
    assert_gaps(static(MID_WEEK, m(d('2026-10-05')), {d('2026-09-16')}, WEEK), 10)
    assert gap_intervals(MID_WEEK, m(d('2026-10-05')), frozenset(), XNYS, NOW, WEEK) == [
        GapInterval(d('2026-09-21'), d('2026-10-02'), 10)
    ]


def test_the_first_monday_s_local_midnight_is_inside_the_range_and_a_tick_later_is_not():
    """Half-open at the start: the week whose Monday midnight IS the start is expected; one tick after, it is not."""
    monday = m(d('2026-09-21'))
    assert_gaps(static(monday, m(d('2026-10-05')), None, WEEK), 10)
    assert_gaps(static(monday + TICK, m(d('2026-10-05')), None, WEEK), 5)


def test_a_range_ending_mid_week_keeps_the_week_that_starts_inside_it():
    """[Mon 21 Sep, Thu 1 Oct): the week of 28 Sep starts inside, so it is expected, but only its in-range sessions.

    Re-pinned for tj-grna9p.156 (the clarification on tj-grna9p.8 of 2026-10-20): Mon 28, Tue 29 and Wed 30 Sep are
    inside the range; Thu 1 and Fri 2 Oct are not, so they are never reported missing. The window ends on 30 Sep.
    """
    end = m(d('2026-10-01'))
    assert_gaps(static(m(d('2026-09-21')), end, {d('2026-09-21'), d('2026-09-28')}, WEEK), 0)
    assert_gaps(static(m(d('2026-09-21')), end, {d('2026-09-21')}, WEEK), 3)
    assert_gaps(static(m(d('2026-09-21')), end, None, WEEK), 8)
    assert gap_intervals(m(d('2026-09-21')), end, {d('2026-09-21')}, XNYS, NOW, WEEK) == [
        GapInterval(d('2026-09-28'), d('2026-09-30'), 3)
    ]
    # A bar stamped on a day of the week AFTER the range end still covers the week (the period decides).
    assert_gaps(static(m(d('2026-09-21')), end, {d('2026-09-21'), d('2026-10-02')}, WEEK), 0)


def test_a_range_ending_exactly_at_a_monday_s_midnight_does_not_expect_that_week():
    """A tick past Monday's midnight the week of 28 Sep is expected, but none of its sessions is in range.

    Re-pinned for tj-grna9p.156 (orchestrator ruling): gap counts cover only sessions inside [start, end), so there is
    nothing to miss and the dataset is COMPLETE, where it read 5 before.
    """
    assert_gaps(static(m(d('2026-09-21')), m(d('2026-09-28')), {d('2026-09-21')}, WEEK), 0)
    assert_gaps(static(m(d('2026-09-21')), m(d('2026-09-28')) + TICK, {d('2026-09-21')}, WEEK), 0)
    assert gap_intervals(m(d('2026-09-21')), m(d('2026-09-28')) + TICK, {d('2026-09-21')}, XNYS, NOW, WEEK) == []


def test_the_current_unended_week_is_not_expected_until_its_last_session_closes():
    """The week of 12 Oct ends at Friday 16 Oct's 20:00Z close: not expected a tick before it, expected at it."""
    start, end = m(d('2026-10-05')), OPEN_ENDED
    friday_close = XNYS.session_close(d('2026-10-16'))
    assert friday_close == z('2026-10-16T20:00:00Z')
    assert_gaps(static(start, end, {d('2026-10-05')}, WEEK, friday_close - TICK), 0)
    assert_gaps(static(start, end, {d('2026-10-05')}, WEEK, friday_close), 5)


STAMP_DAYS = {'monday': 0, 'tuesday': 1, 'friday': 4, 'saturday': 5, 'sunday': 6}


@pytest.mark.parametrize('offset', STAMP_DAYS.values(), ids=STAMP_DAYS)
def test_the_day_of_the_week_the_bar_is_stamped_does_not_matter(offset):
    covered = {monday + timedelta(days=offset) for monday in MONDAYS}
    assert_gaps(static(WEEKLY_START, WEEKLY_END, covered, WEEK), 0)


def test_two_adjacent_missing_weeks_are_one_window_and_two_apart_are_two():
    adjacent = set(MONDAYS) - {d('2023-03-13'), d('2023-03-20')}
    assert gap_intervals(WEEKLY_START, WEEKLY_END, adjacent, XNYS, NOW, WEEK) == [
        GapInterval(d('2023-03-13'), d('2023-03-24'), 10)
    ]
    apart = set(MONDAYS) - {d('2023-03-13'), d('2023-04-03')}
    assert gap_intervals(WEEKLY_START, WEEKLY_END, apart, XNYS, NOW, WEEK) == [
        GapInterval(d('2023-03-13'), d('2023-03-17'), 5),
        # Good Friday 7 Apr 2023: four sessions.
        GapInterval(d('2023-04-03'), d('2023-04-06'), 4),
    ]
    assert static(WEEKLY_START, WEEKLY_END, apart, WEEK).gap_count == 9


# ------------------------------------------------------------------------------------------------- 1month STATIC

MONTHLY_START, MONTHLY_END = m(d('2019-01-01')), m(d('2026-11-01'))
FIRSTS = monthly(d('2019-01-01'), d('2026-10-01'))


def test_a_complete_monthly_dataset_is_complete():
    """One bar on each 1st, Jan 2019 to Oct 2026; October 2026 has not ended at NOW, so it is not expected anyway."""
    assert len(FIRSTS) == 94
    assert_gaps(static(MONTHLY_START, MONTHLY_END, set(FIRSTS), MONTH), 0)
    assert_gaps(static(MONTHLY_START, MONTHLY_END, set(FIRSTS) - {d('2026-10-01')}, MONTH), 0)


def test_a_missing_month_is_its_sessions_in_one_window():
    covered = set(FIRSTS) - {d('2022-02-01')}
    assert_gaps(static(MONTHLY_START, MONTHLY_END, covered, MONTH), 19)
    assert gap_intervals(MONTHLY_START, MONTHLY_END, covered, XNYS, NOW, MONTH) == [
        GapInterval(d('2022-02-01'), d('2022-02-28'), 19)
    ]


# Months of 19 to 23 sessions (four-week and five-week months alike), each missing alone.
MONTH_SIZES = {
    'feb 2022 (19)': (d('2022-02-01'), 19),
    'apr 2022 (20, good friday)': (d('2022-04-01'), 20),
    'jun 2026 (21, juneteenth)': (d('2026-06-01'), 21),
    'sep 2026 (21, labor day)': (d('2026-09-01'), 21),
    'mar 2026 (22)': (d('2026-03-01'), 22),
    'aug 2023 (23)': (d('2023-08-01'), 23),
}


@pytest.mark.parametrize(('first', 'sessions'), MONTH_SIZES.values(), ids=MONTH_SIZES)
def test_a_missing_month_counts_exactly_its_own_sessions(first, sessions):
    assert_gaps(static(MONTHLY_START, MONTHLY_END, set(FIRSTS) - {first}, MONTH), sessions)


def test_two_missing_months_are_two_windows():
    covered = set(FIRSTS) - {d('2022-02-01'), d('2022-04-01')}
    assert gap_intervals(MONTHLY_START, MONTHLY_END, covered, XNYS, NOW, MONTH) == [
        GapInterval(d('2022-02-01'), d('2022-02-28'), 19),
        GapInterval(d('2022-04-01'), d('2022-04-29'), 20),
    ]


def test_a_monthly_range_starting_on_the_15th_skips_that_month():
    """[15 Jan 2026, 1 Apr 2026): January is a partial first period; February (19) and March (22) are expected."""
    start, end = m(d('2026-01-15')), m(d('2026-04-01'))
    assert_gaps(static(start, end, None, MONTH), 19 + 22)
    assert_gaps(static(start, end, {d('2026-01-15')}, MONTH), 19 + 22)
    # 1 Feb 2026 is a Sunday: a bar stamped on it still covers February.
    assert_gaps(static(start, end, {d('2026-02-01'), d('2026-03-02')}, MONTH), 0)


def test_the_current_unended_month_is_not_expected_until_its_last_session_closes():
    """October 2026's last session is Fri 30 Oct, closing 20:00Z. Until then only September (21) is owed."""
    start, end = m(d('2026-09-01')), m(d('2026-11-01'))
    last_close = XNYS.session_close(d('2026-10-30'))
    assert last_close == z('2026-10-30T20:00:00Z')
    assert_gaps(static(start, end, None, MONTH, NOW), 21)
    assert_gaps(static(start, end, None, MONTH, last_close - TICK), 21)
    assert_gaps(static(start, end, None, MONTH, last_close), 21 + 22)
    assert_gaps(static(start, end, {d('2026-09-01')}, MONTH, last_close - TICK), 0)


@pytest.mark.parametrize('day', [1, 2, 15, 30], ids=lambda day: f'stamped on the {day}')
def test_the_day_of_the_month_the_bar_is_stamped_does_not_matter(day):
    covered = {first.replace(day=min(day, 28)) for first in FIRSTS}
    assert_gaps(static(MONTHLY_START, MONTHLY_END, covered, MONTH), 0)


def test_a_bar_on_the_last_day_of_a_month_covers_that_month_not_the_next():
    """31 Jan 2026 is a Saturday: its bar is January's. February then has no bar."""
    start, end = m(d('2026-01-01')), m(d('2026-03-01'))
    assert_gaps(static(start, end, {d('2026-01-31')}, MONTH), 19)
    assert_gaps(static(start, end, {d('2026-01-31'), d('2026-02-28')}, MONTH), 0)


# ------------------------------------------------------------------------------------------------- coverage_buckets


def test_weekly_coverage_buckets_count_sessions_all_covered():
    strip = coverage_buckets(WEEKLY_START, WEEKLY_END, set(MONDAYS), XNYS, NOW, 120, WEEK)
    assert len(strip) == 120
    assert sum(bucket.expected_sessions for bucket in strip) == 1560
    assert sum(bucket.sessions_with_bars for bucket in strip) == 1560


def test_weekly_coverage_buckets_with_a_hole_miss_that_week_s_sessions():
    covered = set(MONDAYS) - {d('2023-03-13')}
    strip = coverage_buckets(WEEKLY_START, WEEKLY_END, covered, XNYS, NOW, 120, WEEK)
    assert sum(bucket.expected_sessions for bucket in strip) == 1560
    assert sum(bucket.sessions_with_bars for bucket in strip) == 1555
    short = [bucket for bucket in strip if bucket.expected_sessions != bucket.sessions_with_bars]
    # The missing week's sessions sit in the buckets that hold the week of 13 Mar 2023.
    assert all(bucket.start <= m(d('2023-03-17')) and bucket.end > m(d('2023-03-13')) for bucket in short), short
    assert sum(b.expected_sessions - b.sessions_with_bars for b in short) == 5


def test_weekly_coverage_buckets_do_not_expect_a_partial_first_week():
    """[Wed 16 Sep, Mon 5 Oct): 13 sessions bucketed, 10 expected (the weeks of 21 and 28 Sep)."""
    strip = coverage_buckets(MID_WEEK, m(d('2026-10-05')), {d('2026-09-21')}, XNYS, NOW, 1, WEEK)
    assert strip == [CoverageBucket(MID_WEEK, m(d('2026-10-05')), 10, 5)]


def test_monthly_coverage_buckets_expect_no_session_of_the_unended_month():
    strip = coverage_buckets(m(d('2026-09-01')), m(d('2026-11-01')), {d('2026-09-01')}, XNYS, NOW, 1, MONTH)
    assert strip == [CoverageBucket(m(d('2026-09-01')), m(d('2026-11-01')), 21, 21)]


# ------------------------------------------------------------------------------------------------- clipped to [start, end)
#
# tj-grna9p.156, from the clarification appended to decision tj-grna9p.8 on 2026-10-20: a period straddling the range
# end is EXPECTED when its first day is inside [start, end), but gap_count, the gap windows and the coverage buckets
# count only its sessions INSIDE [start, end) ('how many sessions in [start, end) have no bar', ui/v1/data.proto:64).
# Before the fix the gap side counted the period's sessions after the end, so it disagreed with the buckets.
#
# THE COUNTS ARE BY HAND, from the 2026 NYSE calendar: Sep 2026 sessions are 1-4, 8-11 (Labor Day 7 Sep), 14-18,
# 21-25, 28-30; Oct 2026 are 1-2, 5-9, ... (no holiday). Wed 30 Sep opens 13:30Z.
#
# (start, end, covered, granularity, now, gap_count, gap windows as (first, last, sessions))

OPEN_30_SEP = XNYS.session_open(d('2026-09-30'))
CLIP_CASES = {
    'week ending wednesday, nothing stored': (
        m(d('2026-09-21')),
        m(d('2026-09-30')),
        None,
        WEEK,
        NOW,
        7,
        [('2026-09-21', '2026-09-29', 7)],
    ),
    'week ending wednesday, the straddling week missing': (
        m(d('2026-09-21')),
        m(d('2026-09-30')),
        {d('2026-09-21')},
        WEEK,
        NOW,
        2,
        [('2026-09-28', '2026-09-29', 2)],
    ),
    'mid-week start and mid-week end: the partial first week is not expected': (
        m(d('2026-09-16')),
        m(d('2026-09-30')),
        None,
        WEEK,
        NOW,
        7,
        [('2026-09-21', '2026-09-29', 7)],
    ),
    'mid-week start and mid-week end, the last week missing': (
        m(d('2026-09-16')),
        m(d('2026-09-30')),
        {d('2026-09-21')},
        WEEK,
        NOW,
        2,
        [('2026-09-28', '2026-09-29', 2)],
    ),
    'end mid-session counts the session it cuts': (
        m(d('2026-09-21')),
        OPEN_30_SEP + timedelta(hours=1),
        {d('2026-09-21')},
        WEEK,
        NOW,
        3,
        [('2026-09-28', '2026-09-30', 3)],
    ),
    'end exactly at an open does not count that session': (
        m(d('2026-09-21')),
        OPEN_30_SEP,
        {d('2026-09-21')},
        WEEK,
        NOW,
        2,
        [('2026-09-28', '2026-09-29', 2)],
    ),
    'end a tick after an open counts it': (
        m(d('2026-09-21')),
        OPEN_30_SEP + TICK,
        {d('2026-09-21')},
        WEEK,
        NOW,
        3,
        [('2026-09-28', '2026-09-30', 3)],
    ),
    'one-day range on a monday': (
        m(d('2026-09-28')),
        m(d('2026-09-29')),
        None,
        WEEK,
        NOW,
        1,
        [('2026-09-28', '2026-09-28', 1)],
    ),
    'one-day range on a wednesday is a partial first week': (
        m(d('2026-09-30')),
        m(d('2026-10-01')),
        None,
        WEEK,
        NOW,
        0,
        [],
    ),
    'month ending on the 10th, nothing stored': (
        m(d('2026-09-01')),
        m(d('2026-10-10')),
        None,
        MONTH,
        LATER,
        21 + 7,
        [('2026-09-01', '2026-10-09', 28)],
    ),
    'month ending on the 10th, october missing': (
        m(d('2026-09-01')),
        m(d('2026-10-10')),
        {d('2026-09-01')},
        MONTH,
        LATER,
        7,
        [('2026-10-01', '2026-10-09', 7)],
    ),
    'month ending on the 10th before october has ended: only september': (
        m(d('2026-09-01')),
        m(d('2026-10-10')),
        None,
        MONTH,
        NOW,
        21,
        [('2026-09-01', '2026-09-30', 21)],
    ),
    'month starting on the 15th and ending on the 10th': (
        m(d('2026-09-15')),
        m(d('2026-10-10')),
        None,
        MONTH,
        LATER,
        7,
        [('2026-10-01', '2026-10-09', 7)],
    ),
    'one-day monthly range on the 1st': (
        m(d('2026-10-01')),
        m(d('2026-10-02')),
        None,
        MONTH,
        LATER,
        1,
        [('2026-10-01', '2026-10-01', 1)],
    ),
}


def _bucket_missing(start, end, covered, granularity, now, buckets) -> int:
    strip = coverage_buckets(start, end, covered or frozenset(), XNYS, now, buckets, granularity)
    return sum(bucket.expected_sessions - bucket.sessions_with_bars for bucket in strip)


@pytest.mark.parametrize(
    ('start', 'end', 'covered', 'granularity', 'now', 'count', 'windows'), CLIP_CASES.values(), ids=CLIP_CASES
)
def test_gap_count_and_windows_count_only_the_sessions_inside_the_range(
    start, end, covered, granularity, now, count, windows
):
    assert_gaps(static(start, end, covered, granularity, now), count)
    assert gap_intervals(start, end, covered or frozenset(), XNYS, now, granularity) == [
        GapInterval(d(first), d(last), sessions) for first, last, sessions in windows
    ]


@pytest.mark.parametrize(
    ('start', 'end', 'covered', 'granularity', 'now', 'count', 'windows'), CLIP_CASES.values(), ids=CLIP_CASES
)
def test_gap_count_equals_the_coverage_buckets_missing_count(start, end, covered, granularity, now, count, windows):
    """The two sides of the strip and the freshness count agree, however the buckets are cut."""
    gap_count = static(start, end, covered, granularity, now).gap_count
    for buckets in (1, 2, 3, 40):
        assert _bucket_missing(start, end, covered, granularity, now, buckets) == gap_count == count, buckets
    gaps = gap_intervals(start, end, covered or frozenset(), XNYS, now, granularity)
    assert sum(gap.sessions for gap in gaps) == count


@pytest.mark.parametrize(
    ('start', 'end', 'covered', 'granularity', 'now', 'count', 'windows'), CLIP_CASES.values(), ids=CLIP_CASES
)
def test_a_complete_dataset_over_a_range_ending_mid_period_is_still_complete(
    start, end, covered, granularity, now, count, windows
):
    every = {d('2026-09-14'), d('2026-09-21'), d('2026-09-28')} if granularity is WEEK else set(FIRSTS)
    assert_gaps(static(start, end, every, granularity, now), 0)
    assert gap_intervals(start, end, every, XNYS, now, granularity) == []
    assert _bucket_missing(start, end, every, granularity, now, 3) == 0


def test_a_bar_stamped_after_the_range_end_still_covers_the_straddling_month():
    """The period decides covered or not: October's bar on the 30th covers the in-range 1st..9th."""
    start, end = m(d('2026-09-01')), m(d('2026-10-10'))
    assert_gaps(static(start, end, {d('2026-09-01'), d('2026-10-30')}, MONTH, LATER), 0)


# A sweep of starts and ends around and inside the weeks of 14, 21 and 28 Sep and the month of October: whatever the
# range, every reported session lies inside it and the three counts agree.
SWEEP_STARTS = [m(d('2026-09-14')) + timedelta(hours=h) for h in range(0, 24 * 9, 15)]
SWEEP_ENDS = [m(d('2026-09-28')) + timedelta(hours=h) for h in range(0, 24 * 16, 13)]


@pytest.mark.parametrize('granularity', [WEEK, MONTH], ids=str)
def test_gap_windows_never_reach_outside_the_range_and_every_count_agrees(granularity):
    checked = 0
    for start in SWEEP_STARTS:
        for end in SWEEP_ENDS:
            inside = set(XNYS.sessions_overlapping(start, end))
            for covered in (frozenset(), frozenset({d('2026-09-21')}), frozenset({d('2026-09-01')})):
                gaps = gap_intervals(start, end, covered, XNYS, LATER, granularity)
                for gap in gaps:
                    run = XNYS.sessions_overlapping(m(gap.first_session), m(gap.last_session + timedelta(days=1)))
                    assert set(run) <= inside, (start, end, gap)
                    assert len(run) == gap.sessions, gap
                count = static(start, end, covered, granularity, LATER).gap_count
                assert (
                    count
                    == sum(gap.sessions for gap in gaps)
                    == _bucket_missing(start, end, covered, granularity, LATER, 7)
                ), (start, end, covered)
                checked += 1
    assert checked > 300, 'precondition: the sweep is not empty'


# _in_range, directly. It is private, but it is the one place the clip lives.

CLIP_START, CLIP_END = m(d('2026-09-21')), m(d('2026-10-01'))


def test_in_range_keeps_only_the_sessions_inside_the_half_open_range_with_their_periods():
    period = d('2026-09-14')
    expected = {
        d('2026-09-18'): period,  # before the start
        d('2026-09-21'): d('2026-09-21'),
        d('2026-09-30'): d('2026-09-28'),
        d('2026-10-01'): d('2026-09-28'),  # opens after the end
        d('2026-10-02'): d('2026-09-28'),
    }
    kept = _in_range(expected, CLIP_START, CLIP_END, XNYS, WEEK)
    assert kept == {d('2026-09-21'): d('2026-09-21'), d('2026-09-30'): d('2026-09-28')}
    assert _in_range(expected, CLIP_START, CLIP_END, XNYS, MONTH) == kept


def test_in_range_is_half_open_on_the_session_s_open_and_close():
    """A session is inside when its [open, close) overlaps [start, end).

    An end AT its open, or a start AT its close, leaves it out; a tick either way puts it in.
    """
    session = d('2026-09-30')
    one = {session: d('2026-09-28')}
    open_, close = XNYS.session_open(session), XNYS.session_close(session)
    assert _in_range(one, CLIP_START, open_, XNYS, WEEK) == {}
    assert _in_range(one, CLIP_START, open_ + TICK, XNYS, WEEK) == one
    assert _in_range(one, close, CLIP_END, XNYS, WEEK) == {}
    assert _in_range(one, close - TICK, CLIP_END, XNYS, WEEK) == one


@pytest.mark.parametrize('granularity', [g for g in Granularity if g not in (WEEK, MONTH)], ids=str)
def test_in_range_passes_session_granularities_through_unchanged(granularity):
    """1min..1day: the expected sessions come from sessions_overlapping already, so nothing is filtered.

    Not even a session outside the range handed in by hand.
    """
    expected = {d('2026-09-18'): d('2026-09-18'), d('2026-10-02'): d('2026-10-02')}
    assert _in_range(expected, CLIP_START, CLIP_END, XNYS, granularity) == expected


# ------------------------------------------------------------------------------------------------- DAILY 1week
#
# A weekly subscription from Mon 3 Aug 2026. Weeks end at Friday's close (20:00Z in EDT); the next session is
# Monday's 13:30Z open, or Tuesday's after Labor Day, or Monday's 14:30Z open after the November DST change.
# (now, last bar stamp or None, status, expected period)

WEEKLY_SUB_START = m(d('2026-08-03'))
DAILY_WEEK_CASES = {
    'mid-week, the last ended week stored': (z('2026-10-14T15:00:00Z'), m(d('2026-10-05')), 'FRESH', '2026-10-05'),
    'a tick before friday close, the previous week stored': (
        z('2026-10-16T20:00:00Z') - TICK,
        m(d('2026-10-05')),
        'FRESH',
        '2026-10-05',
    ),
    'at friday close the week ends: late': (z('2026-10-16T20:00:00Z'), m(d('2026-10-05')), 'LATE', '2026-10-12'),
    'all weekend: late': (z('2026-10-18T12:00:00Z'), m(d('2026-10-05')), 'LATE', '2026-10-12'),
    'a tick before monday open: late': (z('2026-10-19T13:30:00Z') - TICK, m(d('2026-10-05')), 'LATE', '2026-10-12'),
    'at monday open: overdue': (z('2026-10-19T13:30:00Z'), m(d('2026-10-05')), 'OVERDUE', '2026-10-12'),
    'at monday open, the week stored': (z('2026-10-19T13:30:00Z'), m(d('2026-10-12')), 'FRESH', '2026-10-12'),
    # A missed week stays OVERDUE when the following week ends: the deadline is the FIRST missed week's.
    'a missed week stays overdue after the next week ends': (
        z('2026-10-23T20:00:00Z'),
        m(d('2026-10-05')),
        'OVERDUE',
        '2026-10-19',
    ),
    'nothing stored at all: overdue': (z('2026-10-14T15:00:00Z'), None, 'OVERDUE', '2026-10-05'),
    # LABOR DAY: the week of 31 Aug ends Fri 4 Sep; the next session is Tuesday 8 Sep, so the deadline is its open.
    'labor day monday: still late': (z('2026-09-07T18:00:00Z'), m(d('2026-08-24')), 'LATE', '2026-08-31'),
    'a tick before tuesday open after labor day': (
        z('2026-09-08T13:30:00Z') - TICK,
        m(d('2026-08-24')),
        'LATE',
        '2026-08-31',
    ),
    'at tuesday open after labor day': (z('2026-09-08T13:30:00Z'), m(d('2026-08-24')), 'OVERDUE', '2026-08-31'),
    # The holiday week itself: the vendor stamps it Tuesday 8 Sep, and the expected period is still the week of 7 Sep.
    'the labor day week stamped tuesday is fresh': (
        z('2026-09-14T15:00:00Z'),
        m(d('2026-09-08')),
        'FRESH',
        '2026-09-07',
    ),
    # DST: the week of 26 Oct ends Fri 30 Oct 20:00Z (EDT); Monday 2 Nov opens at 14:30Z (EST), not 13:30Z.
    'monday after the clocks change, at the summer open time: late': (
        z('2026-11-02T13:30:00Z'),
        m(d('2026-10-19')),
        'LATE',
        '2026-10-26',
    ),
    'a tick before the winter open: late': (z('2026-11-02T14:30:00Z') - TICK, m(d('2026-10-19')), 'LATE', '2026-10-26'),
    'at the winter open: overdue': (z('2026-11-02T14:30:00Z'), m(d('2026-10-19')), 'OVERDUE', '2026-10-26'),
}


@pytest.mark.parametrize(('now', 'last_bar', 'status', 'period'), DAILY_WEEK_CASES.values(), ids=DAILY_WEEK_CASES)
def test_a_weekly_subscription_owes_the_last_ended_week(now, last_bar, status, period):
    health = subscription(WEEKLY_SUB_START, now, last_bar, WEEK)
    assert health.status is FreshnessStatus(status), health
    assert health.is_failure is (status == 'OVERDUE')
    assert health.gap_count == 0


@pytest.mark.parametrize(('now', 'last_bar', 'status', 'period'), DAILY_WEEK_CASES.values(), ids=DAILY_WEEK_CASES)
def test_a_weekly_subscription_expects_its_period_s_first_day_and_its_local_midnight(now, last_bar, status, period):
    """THE BUILDER'S OPEN DESIGN POINT, pinned as built.

    The expected 'session' is the period's FIRST CALENDAR DAY, which need not be a session (Labor Day), and the
    expected bar is that day's local midnight. Only expected_last_bar reaches the wire (routers/data_store/
    ui_mapping.py _freshness).
    """
    health = subscription(WEEKLY_SUB_START, now, last_bar, WEEK)
    assert health.expected_last_session == d(period)
    assert health.expected_last_bar == m(d(period))


@pytest.mark.parametrize('offset', STAMP_DAYS.values(), ids=STAMP_DAYS)
def test_a_weekly_subscription_does_not_care_which_day_of_the_week_is_stamped(offset):
    """The week of 12 Oct, stamped on any of its days, is FRESH at Monday 19 Oct's open; the week before is not."""
    now = z('2026-10-19T13:30:00Z')
    assert subscription(WEEKLY_SUB_START, now, m(d('2026-10-12') + timedelta(days=offset)), WEEK).status is (
        FreshnessStatus.FRESH
    )
    assert subscription(WEEKLY_SUB_START, now, m(d('2026-10-05') + timedelta(days=offset)), WEEK).status is (
        FreshnessStatus.OVERDUE
    )


def test_a_weekly_subscription_started_mid_week_owes_nothing_until_its_first_whole_week_ends():
    """Started Wed 14 Oct: the week of 12 Oct is a partial first period. Nothing is owed until Fri 23 Oct closes."""
    start = m(d('2026-10-14'))
    nothing = subscription(start, z('2026-10-23T20:00:00Z') - TICK, None, WEEK)
    assert (nothing.status, nothing.expected_last_session, nothing.expected_last_bar) == (
        FreshnessStatus.FRESH,
        None,
        None,
    )
    owed = subscription(start, z('2026-10-23T20:00:00Z'), None, WEEK)
    assert (owed.status, owed.expected_last_session) == (FreshnessStatus.LATE, d('2026-10-19'))
    assert subscription(start, z('2026-10-26T13:30:00Z'), None, WEEK).status is FreshnessStatus.OVERDUE


def test_a_weekly_subscription_whose_end_has_passed_owes_its_last_week_in_range():
    """[3 Aug, Thu 1 Oct): the week of 28 Sep starts inside, so it is the last owed, even judged weeks later."""
    end = m(d('2026-10-01'))
    assert subscription(WEEKLY_SUB_START, NOW, m(d('2026-09-28')), WEEK, end).status is FreshnessStatus.FRESH
    stale = subscription(WEEKLY_SUB_START, NOW, m(d('2026-09-21')), WEEK, end)
    assert (stale.status, stale.expected_last_session) == (FreshnessStatus.OVERDUE, d('2026-09-28'))


# ------------------------------------------------------------------------------------------------- DAILY 1month
#
# A monthly subscription from 1 Jun 2026. September 2026 ends at Wed 30 Sep's 20:00Z close; Thu 1 Oct opens 13:30Z.

MONTHLY_SUB_START = m(d('2026-06-01'))
DAILY_MONTH_CASES = {
    'judged in october, september stored': (NOW, m(d('2026-09-01')), 'FRESH', '2026-09-01'),
    'a tick before september s last close, august stored': (
        z('2026-09-30T20:00:00Z') - TICK,
        m(d('2026-08-03')),
        'FRESH',
        '2026-08-01',
    ),
    'at september s last close: late': (z('2026-09-30T20:00:00Z'), m(d('2026-08-03')), 'LATE', '2026-09-01'),
    'a tick before october s first open: late': (
        z('2026-10-01T13:30:00Z') - TICK,
        m(d('2026-08-03')),
        'LATE',
        '2026-09-01',
    ),
    'at october s first open: overdue': (z('2026-10-01T13:30:00Z'), m(d('2026-08-03')), 'OVERDUE', '2026-09-01'),
    'a missed month stays overdue after the next month ends': (
        z('2026-10-30T20:00:00Z'),
        m(d('2026-08-03')),
        'OVERDUE',
        '2026-10-01',
    ),
    'nothing stored: overdue from july s first open': (NOW, None, 'OVERDUE', '2026-09-01'),
    # LABOR DAY: August ends Mon 31 Aug; September's first session is Tue 1 Sep, not a holiday, opening 13:30Z.
    'august ended: late until 1 sep opens': (
        z('2026-09-01T13:30:00Z') - TICK,
        m(d('2026-07-01')),
        'LATE',
        '2026-08-01',
    ),
    'at 1 sep open: overdue': (z('2026-09-01T13:30:00Z'), m(d('2026-07-01')), 'OVERDUE', '2026-08-01'),
}


@pytest.mark.parametrize(('now', 'last_bar', 'status', 'period'), DAILY_MONTH_CASES.values(), ids=DAILY_MONTH_CASES)
def test_a_monthly_subscription_owes_the_last_ended_month(now, last_bar, status, period):
    health = subscription(MONTHLY_SUB_START, now, last_bar, MONTH)
    assert health.status is FreshnessStatus(status), health
    assert health.expected_last_session == d(period)
    assert health.expected_last_bar == m(d(period))
    assert health.is_failure is (status == 'OVERDUE')


@pytest.mark.parametrize('day', [1, 2, 15, 30], ids=lambda day: f'stamped on the {day}')
def test_a_monthly_subscription_does_not_care_which_day_of_the_month_is_stamped(day):
    assert subscription(MONTHLY_SUB_START, NOW, m(d('2026-09-01').replace(day=day)), MONTH).status is (
        FreshnessStatus.FRESH
    )
    assert subscription(MONTHLY_SUB_START, NOW, m(d('2026-08-01').replace(day=day)), MONTH).status is (
        FreshnessStatus.OVERDUE
    )


def test_a_monthly_subscription_started_on_the_15th_owes_nothing_for_that_month():
    """Started 15 Sep: September is a partial first period, so nothing is owed until October's last close."""
    start = m(d('2026-09-15'))
    assert subscription(start, NOW, None, MONTH).expected_last_session is None
    assert subscription(start, NOW, None, MONTH).status is FreshnessStatus.FRESH
    late = subscription(start, z('2026-10-30T20:00:00Z'), None, MONTH)
    assert (late.status, late.expected_last_session) == (FreshnessStatus.LATE, d('2026-10-01'))
    # The first session after October is Mon 2 Nov, opening 14:30Z (EST).
    assert subscription(start, z('2026-11-02T14:30:00Z') - TICK, None, MONTH).status is FreshnessStatus.LATE
    assert subscription(start, z('2026-11-02T14:30:00Z'), None, MONTH).status is FreshnessStatus.OVERDUE


# ------------------------------------------------------------------------------------------------- the refresh's due set
#
# daily_refresh.py:440 passes row.granularity to evaluate_dataset: the due set is LATE or OVERDUE under the period
# rule, so a weekly subscription holding its last ended week is not refreshed.


def _row(granularity: Granularity, last_bar: datetime | None, start: datetime = WEEKLY_SUB_START) -> CatalogRow:
    return CatalogRow(
        id=uuid.uuid4(),
        asset_symbol='SPY',
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        feed=Feed.IEX,
        granularity=granularity,
        start=start,
        end=None,
        update_type=UpdateType.DAILY,
        expiry_type=ExpiryType.ROLLING,
        expiry=None,
        owner='operator',
        last_bar=last_bar,
    )


def test_the_refresh_does_not_request_a_weekly_or_monthly_subscription_holding_its_last_period():
    fresh_week = _row(WEEK, m(d('2026-10-12')))
    behind_week = _row(WEEK, m(d('2026-10-05')))
    fresh_month = _row(MONTH, m(d('2026-09-01')), MONTHLY_SUB_START)
    behind_month = _row(MONTH, m(d('2026-08-03')), MONTHLY_SUB_START)
    owed = DailyRefresher._behind([fresh_week, behind_week, fresh_month, behind_month], frozenset(), XNYS, NOW)
    assert {row.id for row in owed} == {behind_week.id, behind_month.id}


# ------------------------------------------------------------------------------------------------- defaults
#
# gap_intervals and coverage_buckets gained a trailing granularity parameter defaulting to ONE_DAY, so a positional
# call written before tj-grna9p.151 is unchanged.


def test_the_granularity_parameter_defaults_to_one_day():
    start, end, covered = m(d('2026-09-01')), m(d('2026-10-01')), {d('2026-09-01'), d('2026-09-14')}
    assert gap_intervals(start, end, covered, XNYS, NOW) == gap_intervals(
        start, end, covered, XNYS, NOW, Granularity.ONE_DAY
    )
    assert coverage_buckets(start, end, covered, XNYS, NOW, 7) == coverage_buckets(
        start, end, covered, XNYS, NOW, 7, Granularity.ONE_DAY
    )
    # And the default is not a periodic one: the same inputs judged weekly differ.
    assert gap_intervals(start, end, covered, XNYS, NOW) != gap_intervals(start, end, covered, XNYS, NOW, WEEK)


# ------------------------------------------------------------------------------------------------- 1min..1day identical
#
# THE ORACLE is the pre-change code (8f3ce43^, server/data/store/app/freshness.py), copied here verbatim in logic: it
# reads only the calendar's own answers. Every result of the changed functions for 1min, 5min, 30min, 1hour and 1day
# must equal it over a grid of ranges, clocks, covered sets and last bars spanning a weekend, Thanksgiving, its
# half-day, and the November DST change. This is equivalence, not a re-derivation: the rules themselves are pinned
# against hand-written values in test_freshness.py and test_coverage_buckets.py, unchanged.


def _old_missing(start, end, covered, calendar: TradingCalendar, now):
    return [
        s for s in calendar.sessions_overlapping(start, end) if calendar.session_close(s) <= now and s not in covered
    ]


def _old_gap_intervals(start, end, covered, calendar: TradingCalendar, now):
    sessions = calendar.sessions_overlapping(start, end)
    missing = set(_old_missing(start, end, covered, calendar, now))
    runs, run = [], []
    for session in sessions:
        if session in missing:
            run.append(session)
            continue
        if run:
            runs.append(GapInterval(run[0], run[-1], len(run)))
            run = []
    if run:
        runs.append(GapInterval(run[0], run[-1], len(run)))
    return runs


def _old_buckets(start, end, covered, calendar: TradingCalendar, now, buckets):
    sessions = calendar.sessions_overlapping(start, end)
    if not sessions:
        return []
    groups = min(buckets, len(sessions))
    size, remainder = divmod(len(sessions), groups)
    out, first = [], 0
    for index in range(groups):
        last = first + size + (1 if index < remainder else 0)
        members = sessions[first:last]
        expected = [s for s in members if calendar.session_close(s) <= now]
        bucket_start = start if index == 0 else m(members[0])
        bucket_end = end if index == groups - 1 else m(sessions[last])
        out.append(CoverageBucket(bucket_start, bucket_end, len(expected), sum(s in covered for s in expected)))
        first = last
    return out


def _old_daily(start, end, last_bar, calendar: TradingCalendar, now):
    owed = [s for s in calendar.sessions_overlapping(start, min(end, now)) if calendar.session_close(s) <= now]
    if not owed:
        return DatasetHealth(FreshnessStatus.FRESH)
    expected = owed[-1]
    expected_bar = m(expected)
    if last_bar is not None and session_of(last_bar, calendar) >= expected:
        return DatasetHealth(FreshnessStatus.FRESH, expected, expected_bar)
    missed = owed[0]
    if last_bar is not None:
        missed = min(expected, calendar.next_session(session_of(last_bar, calendar)))
    deadline = calendar.session_open(calendar.next_session(missed))
    status = FreshnessStatus.LATE if now < deadline else FreshnessStatus.OVERDUE
    return DatasetHealth(status, expected, expected_bar)


def _old_static(start, end, covered, calendar, now):
    missing = _old_missing(start, end, covered or frozenset(), calendar, now)
    return (
        DatasetHealth(FreshnessStatus.GAPS, gap_count=len(missing))
        if missing
        else DatasetHealth(FreshnessStatus.COMPLETE)
    )


GRID_SESSIONS = XNYS.sessions_overlapping(m(d('2026-10-28')), m(d('2026-12-02')))
GRID_RANGES = [
    (m(d('2026-10-29')), m(d('2026-11-04'))),  # across the DST change
    (z('2026-10-30T15:00:00Z'), z('2026-11-03T16:00:00Z')),  # mid-session to mid-session
    (m(d('2026-11-24')), m(d('2026-11-30'))),  # Thanksgiving and its half-day
    (m(d('2026-10-28')), OPEN_ENDED),
    # tj-grna9p.156: ends (and starts) that fall mid-session and exactly on a session boundary, a tick either side.
    (m(d('2026-10-29')), XNYS.session_open(d('2026-11-03'))),
    (m(d('2026-10-29')), XNYS.session_open(d('2026-11-03')) + TICK),
    (m(d('2026-10-29')), XNYS.session_close(d('2026-11-03')) - TICK),
    (m(d('2026-10-29')), XNYS.session_close(d('2026-11-03'))),
    (XNYS.session_close(d('2026-10-30')), z('2026-11-27T16:00:00Z')),  # from a close to mid half-day
    (XNYS.session_open(d('2026-11-02')) + TICK, XNYS.session_close(d('2026-11-27'))),
]
GRID_NOWS = sorted(
    {
        instant
        for session in GRID_SESSIONS
        for edge in (XNYS.session_open(session), XNYS.session_close(session))
        for instant in (edge - TICK, edge, edge + TICK)
    }
    | {m(day) + timedelta(hours=12) for day in (d('2026-10-31'), d('2026-11-01'), d('2026-11-26'))}
)[::3]
GRID_COVERED = [
    frozenset(),
    frozenset(GRID_SESSIONS),
    frozenset(GRID_SESSIONS[::2]),
    frozenset(GRID_SESSIONS) - {d('2026-11-02'), d('2026-11-25')},
]
GRID_LAST_BARS = [None, m(d('2026-10-30')), m(d('2026-11-02')), z('2026-11-02T15:00:00Z'), m(d('2026-11-25'))]
SESSION_GRANULARITIES = [g for g in Granularity if g not in (WEEK, MONTH)]


def test_the_session_granularities_are_the_five_the_design_names():
    assert {str(g) for g in SESSION_GRANULARITIES} == {'1min', '5min', '30min', '1hour', '1day'}


@pytest.mark.parametrize('granularity', SESSION_GRANULARITIES, ids=lambda g: str(g))
def test_session_granularities_give_exactly_the_pre_change_results(granularity):
    differ = []
    statuses = set()
    for start, end in GRID_RANGES:
        for now in GRID_NOWS:
            for covered in GRID_COVERED:
                got = static(start, end, covered, granularity, now)
                statuses.add(got.status)
                if got != _old_static(start, end, covered, XNYS, now):
                    differ.append(('static', start, end, now, covered, got))
                if gap_intervals(start, end, covered, XNYS, now, granularity) != _old_gap_intervals(
                    start, end, covered, XNYS, now
                ):
                    differ.append(('gaps', start, end, now, covered))
                for buckets in (1, 3, 40):
                    if coverage_buckets(start, end, covered, XNYS, now, buckets, granularity) != _old_buckets(
                        start, end, covered, XNYS, now, buckets
                    ):
                        differ.append(('buckets', start, end, now, covered, buckets))
            for last_bar in GRID_LAST_BARS:
                got = subscription(start, now, last_bar, granularity, end)
                statuses.add(got.status)
                if got != _old_daily(start, end, last_bar, XNYS, now):
                    differ.append(('daily', start, end, now, last_bar, got))
    assert not differ, differ[:5]
    # Guard the guard: the grid reaches every status the two rules can give.
    assert statuses == {
        FreshnessStatus.FRESH,
        FreshnessStatus.LATE,
        FreshnessStatus.OVERDUE,
        FreshnessStatus.COMPLETE,
        FreshnessStatus.GAPS,
    }
