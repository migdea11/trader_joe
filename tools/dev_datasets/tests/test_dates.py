"""The D-relative date maths, the wire times and the degrade windows (validator, tj-grna9p.145).

D is the run's UTC date, or --date. Ranges and expiries go on the wire as UTC midnights; degrade windows are New York
midnights, so whole sessions fall inside. The session counts per window are checked against the exchange's own
calendar in test_store_contract.py, which needs the data-store packages.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from tools.dev_datasets import request as tool
from tools.dev_datasets.tests.doubles import NO_RESET, ExplodingHttp, exploding_connect, run_tool


pytestmark = pytest.mark.build_infra

NY_EDT = timedelta(hours=-4)
NY_EST = timedelta(hours=-5)


@pytest.mark.parametrize(
    ('value', 'today', 'expected'),
    [
        ('D', '2026-10-20', '2026-10-20'),
        ('D-8', '2026-10-20', '2026-10-12'),
        ('D+2', '2026-10-20', '2026-10-22'),
        ('D-0', '2026-10-20', '2026-10-20'),
        ('D+365', '2026-10-20', '2027-10-20'),
        ('D-370', '2026-10-20', '2025-10-15'),
        # Month and year boundaries.
        ('D-1', '2026-03-01', '2026-02-28'),
        ('D-1', '2026-01-01', '2025-12-31'),
        ('D+1', '2026-12-31', '2027-01-01'),
        ('D-5', '2026-03-03', '2026-02-26'),
        # Leap days, both directions.
        ('D-1', '2024-03-01', '2024-02-29'),
        ('D+1', '2024-02-28', '2024-02-29'),
        ('D+365', '2024-02-29', '2025-02-28'),
        ('D-366', '2025-03-01', '2024-02-29'),
        # An ISO date ignores D.
        ('2016-01-04', '2026-10-20', '2016-01-04'),
        ('2024-02-29', '2026-10-20', '2024-02-29'),
    ],
)
def test_resolve_day(value, today, expected):
    assert tool.resolve_day(value, date.fromisoformat(today)) == date.fromisoformat(expected)


@pytest.mark.parametrize('value', ['d-1', 'D--1', 'D+-1', 'D-', 'D1', 'D - 1', 'today', '2026-1-1', '20261020', ''])
def test_resolve_day_refuses_other_forms(value):
    with pytest.raises(tool.DevDatasetsError, match='not an ISO date or a D-relative date'):
        tool.resolve_day(value, date(2026, 10, 20))


@pytest.mark.parametrize('value', ['2026-02-30', '2025-02-29', '2026-13-01'])
def test_resolve_day_refuses_an_impossible_calendar_date(value):
    with pytest.raises(tool.DevDatasetsError, match='not a calendar date'):
        tool.resolve_day(value, date(2026, 10, 20))


def test_wire_time_is_an_aware_utc_midnight_with_a_z():
    assert tool.wire_time(date(2024, 2, 29)) == '2024-02-29T00:00:00Z'
    assert tool.wire_time(date(2026, 3, 8)) == '2026-03-08T00:00:00Z'


@pytest.mark.parametrize(
    ('day', 'utc_hour', 'offset'),
    [
        (date(2026, 7, 1), 4, NY_EDT),
        (date(2026, 1, 15), 5, NY_EST),
        # The DST days themselves: midnight comes before the 02:00 switch, so it has the old offset.
        (date(2026, 3, 8), 5, NY_EST),
        (date(2026, 3, 9), 4, NY_EDT),
        (date(2026, 11, 1), 4, NY_EDT),
        (date(2026, 11, 2), 5, NY_EST),
    ],
)
def test_market_midnight_is_new_york_midnight(day, utc_hour, offset):
    midnight = tool.market_midnight(day)
    assert midnight.utcoffset() == offset
    assert midnight.astimezone(UTC) == datetime(day.year, day.month, day.day, utc_hour, tzinfo=UTC)
    assert (midnight.hour, midnight.minute) == (0, 0)


# Windows written here rather than read from the plan, so these pin the maths and survive a change of drop days.
WINDOW = tool.Degrade('D-32', 'D-28', 'x')
SHORT = tool.Degrade('D-15', 'D-11', 'x')


def test_a_window_is_from_its_first_to_its_last_new_york_midnight():
    first, last = tool.degrade_window(WINDOW, date(2026, 10, 20))
    assert first.astimezone(UTC) == datetime(2026, 9, 18, 4, tzinfo=UTC)
    assert last.astimezone(UTC) == datetime(2026, 9, 22, 4, tzinfo=UTC)


def test_a_window_across_the_autumn_dst_change_takes_each_midnights_own_offset():
    # D = 2026-11-13: D-15 .. D-11 = 2026-10-29 (EDT) .. 2026-11-02 (EST).
    first, last = tool.degrade_window(SHORT, date(2026, 11, 13))
    assert first.astimezone(UTC) == datetime(2026, 10, 29, 4, tzinfo=UTC)
    assert last.astimezone(UTC) == datetime(2026, 11, 2, 5, tzinfo=UTC)


def test_a_window_across_the_spring_dst_change_takes_each_midnights_own_offset():
    # D = 2026-03-20: 2026-03-05 (EST) .. 2026-03-09 (EDT).
    first, last = tool.degrade_window(SHORT, date(2026, 3, 20))
    assert first.astimezone(UTC) == datetime(2026, 3, 5, 5, tzinfo=UTC)
    assert last.astimezone(UTC) == datetime(2026, 3, 9, 4, tzinfo=UTC)


def test_an_empty_degrade_window_is_refused():
    with pytest.raises(tool.DevDatasetsError, match='empty'):
        tool.degrade_window(tool.Degrade('D-5', 'D-5', 'x'), date(2026, 10, 20))
    with pytest.raises(tool.DevDatasetsError, match='empty'):
        tool.degrade_window(tool.Degrade('D-4', 'D-5', 'x'), date(2026, 10, 20))


@pytest.mark.parametrize('day', ['2026-10-20', '2024-03-01', '2026-03-09', '2026-11-02', '2027-01-01'])
def test_every_degrade_window_sits_strictly_inside_its_rows_range(day):
    # A gap at the very start or end is not the middle gap the design repairs by a repeat request.
    today = date.fromisoformat(day)
    windowed = []
    for row in tool.load_plan():
        # r14 is damaged by construction (tj-grna9p.148 (5)): no window, so nothing to place inside its range.
        if row.degrade is None or not row.degrade.drops_bars:
            continue
        windowed.append(row.id)
        first, last = tool.degrade_window(row.degrade, today)
        start = tool.utc_midnight(tool.resolve_day(row.start, today))
        end = tool.utc_midnight(tool.resolve_day(row.end, today))
        assert start < first and last < end, row.id
    assert windowed == ['r12', 'r13']


def test_a_degrade_without_drop_days_has_no_window():
    r14 = next(row for row in tool.load_plan() if row.id == 'r14')
    with pytest.raises(tool.DevDatasetsError, match='deletes nothing: it has no window'):
        tool.degrade_window(r14.degrade, date(2026, 10, 20))


# ---- --date and the past-only rule, through run() ---------------------------------------------------------------


def dry_bodies(outcome) -> dict[str, dict]:
    import json

    bodies, current = {}, None
    for line in outcome.lines:
        if ' POST ' in line:
            current = line.split()[0]
        elif line.startswith('{'):
            bodies[current] = json.loads(line)
    return bodies


def test_date_overrides_d_for_every_relative_field():
    outcome = run_tool(
        ['seed', '--dry-run', NO_RESET, '--date', '2024-03-01'], http=ExplodingHttp(), connect=exploding_connect
    )
    assert outcome.status == 0
    bodies = dry_bodies(outcome)
    assert bodies['r1']['start'] == '2024-02-22T00:00:00Z'
    assert bodies['r1']['end'] == '2024-02-25T00:00:00Z'
    assert bodies['r1']['expiry'] == '2024-03-03T00:00:00Z'
    assert bodies['r5']['end'] == '2024-02-29T00:00:00Z'
    # 365 days after 2024-03-01: the leap day is behind it, so a year later exactly.
    assert bodies['r6']['expiry'] == '2025-03-01T00:00:00Z'
    assert bodies['r9']['expiry'] == '2024-02-29T00:00:00Z'
    # The fixed ISO dates ignore D.
    assert bodies['r10']['start'] == '2021-01-04T00:00:00Z'
    assert (bodies['r14']['start'], bodies['r14']['end']) == ('2016-01-04T00:00:00Z', '2021-01-04T00:00:00Z')


def test_without_date_d_is_the_clocks_utc_date():
    # 00:30 UTC on the 21st is still the 20th in New York; D follows UTC.
    now = datetime(2026, 10, 21, 0, 30, tzinfo=UTC)
    outcome = run_tool(['seed', '--dry-run', NO_RESET], http=ExplodingHttp(), connect=exploding_connect, now=now)
    assert outcome.status == 0
    assert dry_bodies(outcome)['r5']['end'] == '2026-10-20T00:00:00Z'


def test_a_range_that_does_not_end_15_minutes_before_now_is_refused_and_nothing_is_sent():
    # D-1 is tomorrow when --date is two days ahead: the range would end in the future.
    http = ExplodingHttp()
    outcome = run_tool(['seed', '--date', '2026-10-22'], http=http, connect=exploding_connect)
    assert outcome.status == 1
    assert 'must end in the past' in outcome.text
    assert http.calls == []


def test_the_end_margin_is_fifteen_minutes_exactly():
    row = next(row for row in tool.load_plan() if row.id == 'r5')
    today = date(2026, 10, 21)
    end = datetime(2026, 10, 20, tzinfo=UTC)
    tool.render_body(row, today, end + timedelta(minutes=15))
    with pytest.raises(tool.DevDatasetsError, match='must end in the past'):
        tool.render_body(row, today, end + timedelta(minutes=14, seconds=59))


def test_an_empty_or_inverted_range_is_refused():
    row = next(row for row in tool.load_plan() if row.id == 'r1')
    for end in ('D-8', 'D-9'):
        broken = tool.PlanRow(**{**row.__dict__, 'end': end})
        with pytest.raises(tool.DevDatasetsError, match='not after start'):
            tool.render_body(broken, date(2026, 10, 20), datetime(2026, 10, 20, 15, tzinfo=UTC))
