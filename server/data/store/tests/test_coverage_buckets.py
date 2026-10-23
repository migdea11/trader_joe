"""The coverage strip's pure half: coverage_buckets and local_midnight (validator, gating tj-grna9p.63).

Design: the architect's 'RESCOPED (phase 2, 2026-10-12)' note on tj-grna9p.63, items 5 to 7 and acceptance c/d.
The route half (the gap windows, the bounds, the 404/422s, the statement budget) is in test_ui_datasets_route.py,
section 'coverage'.

NOTHING HERE IS INVENTED. Every expected session, count and boundary is derived from the phase 1 calendar
(calendar_for_source, sessions_overlapping, session_close, session_of, gap_intervals): the instants passed IN are
chosen, the numbers asserted are computed from the calendar. A test that wrote '252 sessions in 2025' would pin the
calendar library, not this code.

TWO CALENDARS. XNYS, the one a source maps to (America/New_York, so a local midnight is 04:00 or 05:00 UTC and
crosses DST twice a year), and XTKS (Asia/Tokyo, +09:00), built with the same adapter, where a session's local
midnight falls on the PREVIOUS UTC date. A rule that took midnight in UTC, or that added 24 hours instead of a day,
passes on one and not the other.
"""

from bisect import bisect_right
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import exchange_calendars
import pandas
import pytest

from common.enums.data_stock import DataSource
from data.store.app.completeness import CoverageBucket, coverage_buckets, gap_intervals
from data.store.app.trading_calendar import (
    ExchangeCalendarAdapter,
    NaiveDatetimeError,
    TradingCalendar,
    calendar_for_source,
    local_midnight,
    session_of,
)


pytestmark = pytest.mark.data_store

XNYS = calendar_for_source(DataSource.ALPACA_API)
XTKS_START = date(2024, 1, 1)
XTKS = ExchangeCalendarAdapter(exchange_calendars.get_calendar('XTKS', start=pandas.Timestamp(XTKS_START)), XTKS_START)
TICK = timedelta(microseconds=1)
# After every range below, so every session in them is completed.
AFTER_ALL = datetime(2026, 9, 1, tzinfo=UTC)


def z(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


# name -> (calendar, start, end). The instants are inputs; nothing asserted below is read off them.
RANGES = {
    'xnys year': (XNYS, z('2025-01-01T00:00:00Z'), z('2026-01-01T00:00:00Z')),
    'xnys mid-session to mid-session': (XNYS, z('2025-03-05T15:00:00Z'), z('2025-03-20T15:00:00Z')),
    'xnys across spring DST': (XNYS, z('2025-03-01T00:00:00Z'), z('2025-03-15T00:00:00Z')),
    'xnys across autumn DST': (XNYS, z('2025-10-25T00:00:00Z'), z('2025-11-08T00:00:00Z')),
    'xnys one session': (XNYS, z('2025-03-05T15:00:00Z'), z('2025-03-05T16:00:00Z')),
    'xnys calendar start': (XNYS, z('1990-01-01T05:00:00Z'), z('1990-03-01T05:00:00Z')),
    'xtks half year': (XTKS, z('2025-01-01T00:00:00Z'), z('2025-07-01T00:00:00Z')),
}
BUCKET_COUNTS = [1, 2, 7, 120, 400, 10_000]


def _sizes(strip: list[CoverageBucket], sessions: list[date], calendar: TradingCalendar, start: datetime) -> list[int]:
    """How many sessions each bucket HOLDS, found by placing each session's first instant in range among the starts.

    Independent of expected_sessions: a session lands in the bucket whose [start, end) holds the later of its
    local midnight and the strip start.
    """
    starts = [bucket.start for bucket in strip]
    held = [0] * len(strip)
    for session in sessions:
        instant = max(local_midnight(session, calendar), start)
        index = bisect_right(starts, instant) - 1
        assert index >= 0 and instant < strip[index].end, (session, instant, strip[max(index, 0)])
        held[index] += 1
    return held


@pytest.mark.parametrize('buckets', BUCKET_COUNTS)
@pytest.mark.parametrize(('calendar', 'start', 'end'), RANGES.values(), ids=RANGES)
def test_buckets_tile_the_range_with_no_hole_or_overlap_and_hold_near_equal_session_counts(
    calendar, start, end, buckets
):
    sessions = calendar.sessions_overlapping(start, end)
    assert sessions, 'precondition: every range here holds a session'

    strip = coverage_buckets(start, end, frozenset(), calendar, AFTER_ALL, buckets)

    assert len(strip) == min(buckets, len(sessions))
    assert strip[0].start == start and strip[-1].end == end
    for earlier, later in pairwise(strip):
        assert earlier.end == later.start, (earlier, later)
    assert all(bucket.start < bucket.end for bucket in strip), strip
    held = _sizes(strip, sessions, calendar, start)
    # Every session completed (AFTER_ALL), so what a bucket expects is what it holds.
    assert [bucket.expected_sessions for bucket in strip] == held
    assert sum(held) == len(sessions)
    assert max(held) - min(held) <= 1, held
    assert held == sorted(held, reverse=True), f'the earlier buckets take the remainder: {held}'


@pytest.mark.parametrize(('calendar', 'start', 'end'), RANGES.values(), ids=RANGES)
def test_every_bucket_after_the_first_starts_at_its_first_session_s_local_midnight(calendar, start, end):
    strip = coverage_buckets(start, end, frozenset(), calendar, AFTER_ALL, 7)
    for bucket in strip[1:]:
        local = bucket.start.astimezone(calendar.tz)
        assert (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0), bucket
        assert session_of(bucket.start, calendar) in calendar.sessions_overlapping(start, end), bucket


def _every_nth(sessions: list[date], n: int) -> frozenset[date]:
    return frozenset(sessions[::n])


NOW_POSITIONS = {
    # now relative to the session at a third of the way through the range
    'at a close': lambda calendar, session: calendar.session_close(session),
    'a tick before a close': lambda calendar, session: calendar.session_close(session) - TICK,
    'mid-session': lambda calendar, session: calendar.session_open(session) + timedelta(minutes=30),
}


@pytest.mark.parametrize('position', NOW_POSITIONS.values(), ids=NOW_POSITIONS)
@pytest.mark.parametrize('buckets', [1, 5, 120])
@pytest.mark.parametrize(('calendar', 'start', 'end'), RANGES.values(), ids=RANGES)
def test_bucket_sums_equal_the_completed_sessions_and_the_gap_sessions(calendar, start, end, buckets, position):
    """Acceptance c and d: expected counts only COMPLETED sessions; expected minus covered is what the gaps hold."""
    sessions = calendar.sessions_overlapping(start, end)
    covered = _every_nth(sessions, 3)
    now = position(calendar, sessions[len(sessions) // 3])

    strip = coverage_buckets(start, end, covered, calendar, now, buckets)

    completed = [session for session in sessions if calendar.session_close(session) <= now]
    gaps = gap_intervals(start, end, covered, calendar, now)
    assert sum(bucket.expected_sessions for bucket in strip) == len(completed)
    assert sum(bucket.sessions_with_bars for bucket in strip) == len([s for s in completed if s in covered])
    assert sum(b.expected_sessions - b.sessions_with_bars for b in strip) == sum(gap.sessions for gap in gaps)
    assert all(0 <= b.sessions_with_bars <= b.expected_sessions for b in strip), strip
    # Not one bucket fewer because some are in the future: len is about sessions, not completed ones.
    assert len(strip) == min(buckets, len(sessions))


def test_a_session_in_progress_and_every_future_one_are_expected_nowhere():
    calendar, start, end = RANGES['xnys mid-session to mid-session']
    sessions = calendar.sessions_overlapping(start, end)
    current = sessions[2]
    now = calendar.session_open(current) + timedelta(minutes=1)

    strip = coverage_buckets(start, end, frozenset(), calendar, now, len(sessions))

    assert [bucket.expected_sessions for bucket in strip] == [1, 1] + [0] * (len(sessions) - 2)
    assert sum(gap.sessions for gap in gap_intervals(start, end, frozenset(), calendar, now)) == 2


def test_a_range_holding_no_session_has_no_buckets():
    """Between one session's close and the next one's open: nothing overlaps, so no strip and no gaps."""
    sessions = XNYS.sessions_overlapping(*RANGES['xnys year'][1:])
    friday = next(s for s, n in pairwise(sessions) if (n - s).days > 1)
    start, end = XNYS.session_close(friday), XNYS.session_open(XNYS.next_session(friday))
    assert XNYS.sessions_overlapping(start, end) == []

    assert coverage_buckets(start, end, frozenset(), XNYS, AFTER_ALL, 120) == []
    assert gap_intervals(start, end, frozenset(), XNYS, AFTER_ALL) == []


@pytest.mark.parametrize('buckets', [0, -1])
def test_fewer_than_one_bucket_is_refused(buckets):
    with pytest.raises(ValueError, match='buckets'):
        coverage_buckets(*RANGES['xnys year'][1:], frozenset(), XNYS, AFTER_ALL, buckets)


@pytest.mark.parametrize('argument', ['start', 'end', 'now'])
def test_a_naive_instant_is_refused_by_name(argument):
    arguments = {'start': RANGES['xnys year'][1], 'end': RANGES['xnys year'][2], 'now': AFTER_ALL}
    arguments[argument] = arguments[argument].replace(tzinfo=None)
    with pytest.raises(NaiveDatetimeError, match=argument):
        coverage_buckets(arguments['start'], arguments['end'], frozenset(), XNYS, arguments['now'], 10)


# ------------------------------------------------------------------------------------------------- local_midnight


def _sessions_either_side_of_an_offset_change(calendar: TradingCalendar, start: datetime, end: datetime):
    sessions = calendar.sessions_overlapping(start, end)
    for before, after in pairwise(sessions):
        if local_midnight(before, calendar).utcoffset() != local_midnight(after, calendar).utcoffset():
            return before, after
    raise AssertionError(f'precondition: no offset change between {start} and {end}')


@pytest.mark.parametrize('name', ['xnys across spring DST', 'xnys across autumn DST'])
def test_local_midnight_is_the_session_s_first_local_instant_on_both_sides_of_a_dst_change(name):
    calendar, start, end = RANGES[name]
    for session in _sessions_either_side_of_an_offset_change(calendar, start, end):
        midnight = local_midnight(session, calendar)
        assert midnight.tzinfo is not None and midnight.utcoffset() == midnight.astimezone(calendar.tz).utcoffset()
        assert midnight.astimezone(calendar.tz).replace(tzinfo=None) == datetime.combine(session, datetime.min.time())
        assert session_of(midnight, calendar) == session
        assert session_of(midnight - TICK, calendar) == session - timedelta(days=1)


def test_local_midnight_in_tokyo_falls_on_the_previous_utc_date():
    """The exchange whose local day differs from UTC's: midnight in Tokyo is 15:00 UTC the day before."""
    sessions = XTKS.sessions_overlapping(*RANGES['xtks half year'][1:])
    for session in sessions[:10]:
        midnight = local_midnight(session, XTKS)
        assert midnight.astimezone(UTC).date() == session - timedelta(days=1), midnight
        assert session_of(midnight, XTKS) == session
        assert session_of(midnight - TICK, XTKS) == session - timedelta(days=1)
