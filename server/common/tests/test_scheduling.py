"""common/scheduling.py: run_periodic and its schedules Once, AfterTradingEvent, Every (validator, tj-grna9p.130).

Gated against the architect's design notes on tj-grna9p.130.

NO REAL TIME PASSES. Every runner test injects the clock and the sleep: AdvancingSleep moves a FakeClock forward by
exactly the delay it is asked for, and BlockingSleep never returns on its own, so only a wake or a cancel ends it.
asyncio.timeout(GUARD_S) is a hang guard, never the thing a test measures.

THE CALENDAR IS A FAKE HERE, deliberately: common's image carries neither exchange_calendars nor pandas, and the module
under test must not need them. FakeNewYorkCalendar models XNYS for 2026 from 23 Feb: weekday sessions 09:30-16:00
America/New_York, Thanksgiving (Thu 26 Nov) closed, Fri 27 Nov an early close at 13:00. It returns NEW YORK LOCAL
aware datetimes, stricter than the store's adapter (which returns UTC): adding an offset to a local aware datetime is
wall-clock arithmetic in Python, so a schedule that forgot to convert to UTC first is caught across a DST change.
The same cases over the real XNYS adapter live in data/store/tests/test_scheduling_xnys.py, beside the pandas import
they need.

THE DATES, all 2026, UTC:
    Fri 6 Mar   EST: open 14:30Z, close 21:00Z      Sun 8 Mar 02:00 local: clocks go forward
    Mon 9 Mar   EDT: open 13:30Z, close 20:00Z
    Wed 14 Oct  EDT: open 13:30Z, close 20:00Z      Fri 16 Oct the same; Sat 17 and Sun 18 no session
    Fri 30 Oct  EDT: close 20:00Z                   Sun 1 Nov 02:00 local: clocks go back
    Mon 2 Nov   EST: open 14:30Z, close 21:00Z
    Wed 25 Nov  EST: close 21:00Z                   Thu 26 Nov Thanksgiving, no session
    Fri 27 Nov  early close: 14:30Z to 18:00Z
"""

import ast
import asyncio
import logging
import random
import sys
from bisect import bisect_right
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.scheduling import AfterTradingEvent, Every, Once, TradingEvent, run_periodic
from common.tests.roots import SERVER_ROOT


pytestmark = pytest.mark.common

NEW_YORK = ZoneInfo('America/New_York')
GUARD_S = 5.0
NAME = 'reaper'
LOGGER = 'test.common.scheduling'
# A message that must never reach a log line (D8): only an exception's type name may.
SECRET = 'postgres://owner:hunter2@db/trader_joe'
DEFAULT_RETRY_S = 300.0


def z(text: str) -> datetime:
    """An instant from an ISO string ending in Z."""
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


T0 = z('2026-10-14T12:00:00Z')


# ---------------------------------------------------------------------------------------------------------------------
# Fakes


class FakeNewYorkCalendar:
    """The SessionCalendar surface over a hand-built 2026 XNYS (see the module docstring)."""

    FIRST = date(2026, 2, 23)
    LAST = date(2026, 12, 31)
    HOLIDAYS = frozenset({date(2026, 11, 26)})
    EARLY_CLOSES = frozenset({date(2026, 11, 27)})

    def __init__(self) -> None:
        days = (self.FIRST + timedelta(days=n) for n in range((self.LAST - self.FIRST).days + 1))
        self.sessions = [d for d in days if d.weekday() < 5 and d not in self.HOLIDAYS]
        self._known = frozenset(self.sessions)

    def _require_session(self, session: date) -> None:
        if session not in self._known:
            raise ValueError(f'{session} is not a session')

    def session_open(self, session: date) -> datetime:
        self._require_session(session)
        return datetime.combine(session, time(9, 30), NEW_YORK)

    def _close(self, session: date) -> datetime:
        self._require_session(session)
        return datetime.combine(session, time(13) if session in self.EARLY_CLOSES else time(16), NEW_YORK)

    def session_close(self, session: date) -> datetime:
        return self._close(session)

    def next_session(self, session: date) -> date:
        index = bisect_right(self.sessions, session)
        if index == len(self.sessions):
            raise LookupError(f'no session after {session}: past the calendar end')
        return self.sessions[index]

    def last_completed_session(self, now: datetime) -> date | None:
        done = [s for s in self.sessions if self._close(s) <= now]
        return done[-1] if done else None


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class AdvancingSleep:
    """Sleeps by moving the fake clock forward exactly `delay`, and records each delay."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.clock.advance(delay)
        await asyncio.sleep(0)


class BlockingSleep:
    """Never returns by itself: only a wake (which cancels it) or a cancel of the runner ends it."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.cancelled = 0

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class Job:
    """Records each run's start on the fake clock; runs past `block_after` block until cancelled."""

    def __init__(self, clock: FakeClock, *, block_after: int, duration_s: float = 0.0, raises=None, during=None):
        self.clock = clock
        self.block_after = block_after
        self.duration_s = duration_s
        self.raises = raises or {}
        self.during = during or {}
        self.starts: list[datetime] = []
        self.active = 0
        self.max_active = 0
        self.cancelled = False

    @property
    def calls(self) -> int:
        return len(self.starts)

    async def __call__(self) -> None:
        self.starts.append(self.clock())
        call = self.calls
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if call > self.block_after:
                await asyncio.Event().wait()
            for _ in range(3):
                await asyncio.sleep(0)
            if call in self.during:
                self.during[call]()
            self.clock.advance(self.duration_s)
            if call in self.raises:
                raise self.raises[call]
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.active -= 1


class ScriptedSchedule:
    """next_fire answers from a script: an exception is raised, anything else is returned. Records each ask."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.asks: list[tuple[datetime, datetime | None]] = []

    def next_fire(self, now: datetime, last_run: datetime | None) -> datetime | None:
        self.asks.append((now, last_run))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class UpTo:
    """Delegates to `inner` until `job` has run `runs` times, then answers None, so the runner ends by itself.

    The loop tests end this way rather than by a cancel: a runner that wrongly swallowed CancelledError would then
    never end, and would hang pytest-asyncio's teardown instead of failing the one test that pins cancellation.
    """

    def __init__(self, inner, job: Job, runs: int) -> None:
        self.inner, self.job, self.runs = inner, job, runs

    def next_fire(self, now: datetime, last_run: datetime | None) -> datetime | None:
        return None if self.job.calls >= self.runs else self.inner.next_fire(now, last_run)


async def until(predicate, what: str) -> None:
    """Yield to the loop until `predicate()` holds."""
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f'never happened: {what}')


async def ended(task: asyncio.Task) -> None:
    """Wait for `task` to end, bounded, without cancelling it on timeout.

    asyncio.wait, not asyncio.timeout around `await task`: a timeout delivers a CancelledError to the waiter, and a
    test expecting the task's own CancelledError would swallow it and pass on a task that never ended.
    """
    done, _ = await asyncio.wait({task}, timeout=GUARD_S)
    assert task in done, 'the runner did not end'


async def finished(task: asyncio.Task) -> None:
    """Require that the runner ends by itself, normally (its schedule answered None)."""
    await ended(task)
    assert task.result() is None


async def stop(task: asyncio.Task) -> None:
    """Cancel the runner and require that it ends promptly, as cancelled."""
    task.cancel()
    await ended(task)
    assert task.cancelled()


def start(schedule, job, clock, sleep, **kwargs) -> asyncio.Task:
    return run_periodic(NAME, schedule, job, clock=clock, sleep=sleep, log=logging.getLogger(LOGGER), **kwargs)


def records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER]


# ---------------------------------------------------------------------------------------------------------------------
# The module's imports: stdlib and common only


def test_scheduling_imports_only_the_stdlib_and_common():
    """The ingest image has no exchange_calendars or pandas, and common never imports a service (design: DEPENDENCIES)."""
    tree = ast.parse((SERVER_ROOT / 'common' / 'scheduling.py').read_text(encoding='utf-8'))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split('.')[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots.add(node.module.split('.')[0])
    assert roots - set(sys.stdlib_module_names) == {'common'}
    assert not roots & {'exchange_calendars', 'pandas', 'data'}


# ---------------------------------------------------------------------------------------------------------------------
# Aware only


NAIVE = datetime(2026, 10, 14, 12)


@pytest.mark.parametrize(
    'build',
    [
        pytest.param(lambda: Once(NAIVE), id='Once at'),
        pytest.param(lambda: Once.after(timedelta(minutes=5), NAIVE), id='Once.after now'),
        pytest.param(lambda: Every(timedelta(hours=1), first=NAIVE), id='Every first'),
    ],
)
def test_a_naive_argument_is_refused(build):
    with pytest.raises(ValueError, match='naive datetime names no instant'):
        build()


@pytest.mark.parametrize(
    'schedule',
    [
        pytest.param(Once(T0), id='Once'),
        pytest.param(Every(timedelta(hours=1)), id='Every'),
        pytest.param(AfterTradingEvent(FakeNewYorkCalendar(), TradingEvent.CLOSE), id='AfterTradingEvent'),
    ],
)
@pytest.mark.parametrize(
    'now, last_run', [pytest.param(NAIVE, None, id='naive now'), pytest.param(T0, NAIVE, id='naive last_run')]
)
def test_next_fire_refuses_a_naive_now_or_last_run(schedule, now, last_run):
    with pytest.raises(ValueError, match='naive datetime names no instant'):
        schedule.next_fire(now, last_run)


def test_a_calendar_that_answers_naive_is_refused():
    class NaiveCalendar(FakeNewYorkCalendar):
        def session_close(self, session: date) -> datetime:
            return super().session_close(session).replace(tzinfo=None)

    with pytest.raises(ValueError, match='naive datetime names no instant'):
        AfterTradingEvent(NaiveCalendar(), TradingEvent.CLOSE).next_fire(T0, None)


@pytest.mark.asyncio
async def test_the_runner_refuses_a_naive_clock():
    job = Job(FakeClock(), block_after=0)
    task = start(Once(T0), job, lambda: NAIVE, BlockingSleep())
    async with asyncio.timeout(GUARD_S):
        with pytest.raises(ValueError, match='naive datetime names no instant'):
            await task
    assert job.calls == 0


# ---------------------------------------------------------------------------------------------------------------------
# Once


def test_once_fires_at_its_instant():
    at = T0 + timedelta(hours=2)
    assert Once(at).next_fire(T0, None) == at


@pytest.mark.parametrize('ago', [timedelta(0), timedelta(seconds=1), timedelta(days=3)])
def test_once_in_the_past_fires_at_once(ago):
    assert Once(T0 - ago).next_fire(T0, None) == T0


def test_once_never_fires_again_after_a_run():
    once = Once(T0 + timedelta(hours=1))
    assert once.next_fire(T0, T0) is None
    assert once.next_fire(T0 + timedelta(hours=2), T0 + timedelta(hours=1)) is None


def test_once_after_is_a_delay_from_the_given_instant():
    assert Once.after(timedelta(minutes=5), T0).at == T0 + timedelta(minutes=5)


def test_once_keeps_the_instant_of_a_non_utc_time():
    at = datetime(2026, 10, 14, 16, 30, tzinfo=NEW_YORK)
    assert Once(at).next_fire(T0, None) == z('2026-10-14T20:30:00Z')


# ---------------------------------------------------------------------------------------------------------------------
# Every


HOUR = timedelta(hours=1)


def test_every_with_no_run_and_no_first_fires_at_once():
    assert Every(HOUR).next_fire(T0, None) == T0


def test_every_with_a_future_first_fires_at_first():
    assert Every(HOUR, first=T0 + timedelta(minutes=10)).next_fire(T0, None) == T0 + timedelta(minutes=10)


def test_every_with_a_past_first_fires_at_once():
    assert Every(HOUR, first=T0 - timedelta(hours=5)).next_fire(T0, None) == T0


def test_every_fires_an_interval_after_the_last_run():
    assert Every(HOUR).next_fire(T0 + timedelta(minutes=5), T0) == T0 + HOUR


@pytest.mark.parametrize('missed', [1, 5, 100])
def test_an_overrun_fires_once_at_once_never_a_burst(missed):
    """However many intervals were missed, the next fire is now, one instant, not the first missed slot."""
    now = T0 + HOUR * missed + timedelta(minutes=1)
    assert Every(HOUR).next_fire(now, T0) == now


@pytest.mark.parametrize('draw, expected', [(0.0, timedelta(0)), (0.5, timedelta(minutes=5))])
def test_jitter_scales_the_random_draw(draw, expected):
    every = Every(HOUR, jitter=timedelta(minutes=10), rng=lambda: draw)
    assert every.next_fire(T0, T0 - HOUR) == T0 + expected


def test_jitter_stays_in_its_half_open_bound():
    jitter = timedelta(minutes=10)
    every = Every(HOUR, jitter=jitter, rng=random.Random(130).random)
    fires = [every.next_fire(T0, T0 - HOUR) for _ in range(2000)]
    assert all(T0 <= fire < T0 + jitter for fire in fires)
    # The draw is used, not ignored: the fires spread across the bound.
    assert max(fires) - min(fires) > jitter * 0.9


def test_jitter_is_added_after_an_overrun_too():
    every = Every(HOUR, jitter=timedelta(minutes=10), rng=lambda: 0.5)
    now = T0 + timedelta(hours=7)
    assert every.next_fire(now, T0) == now + timedelta(minutes=5)


def test_no_jitter_draws_nothing():
    def boom() -> float:
        raise AssertionError('rng drawn with no jitter')

    assert Every(HOUR, rng=boom).next_fire(T0, T0 - HOUR) == T0


@pytest.mark.parametrize(
    'kwargs, message',
    [
        ({'interval': timedelta(0)}, 'interval must be positive'),
        ({'interval': -HOUR}, 'interval must be positive'),
        ({'interval': HOUR, 'jitter': timedelta(seconds=-1)}, 'jitter must not be negative'),
    ],
)
def test_every_refuses_a_bad_interval_or_jitter(kwargs, message):
    with pytest.raises(ValueError, match=message):
        Every(**kwargs)


# ---------------------------------------------------------------------------------------------------------------------
# AfterTradingEvent


CLOSE, OPEN = TradingEvent.CLOSE, TradingEvent.OPEN
M = timedelta(minutes=1)


@pytest.mark.parametrize(
    'event, offset, now, last_run, expected',
    [
        pytest.param(CLOSE, 30 * M, '2026-10-14T12:00Z', None, '2026-10-14T20:30Z', id='close+30 normal day'),
        pytest.param(CLOSE, 30 * M, '2026-10-14T20:15Z', None, '2026-10-14T20:30Z', id='after close before fire'),
        pytest.param(CLOSE, 30 * M, '2026-10-14T20:30Z', None, '2026-10-15T20:30Z', id='now exactly at the fire'),
        pytest.param(
            CLOSE, 30 * M, '2026-10-14T20:29Z', '2026-10-14T20:30Z', '2026-10-15T20:30Z', id='last_run at fire'
        ),
        pytest.param(
            CLOSE, 30 * M, '2026-10-14T12:00Z', '2026-10-13T20:30Z', '2026-10-14T20:30Z', id='last_run earlier'
        ),
        pytest.param(CLOSE, 30 * M, '2026-10-16T20:31Z', None, '2026-10-19T20:30Z', id='friday skips to monday'),
        pytest.param(CLOSE, 30 * M, '2026-10-17T12:00Z', None, '2026-10-19T20:30Z', id='from a saturday'),
        # DST, March: EST close 21:00Z on Friday, EDT close 20:00Z on Monday.
        pytest.param(CLOSE, 30 * M, '2026-03-06T12:00Z', None, '2026-03-06T21:30Z', id='march before dst'),
        pytest.param(CLOSE, 30 * M, '2026-03-06T21:30Z', None, '2026-03-09T20:30Z', id='march across dst'),
        # DST, November: EDT close 20:00Z on Friday, EST close 21:00Z on Monday.
        pytest.param(CLOSE, 30 * M, '2026-10-30T12:00Z', None, '2026-10-30T20:30Z', id='november before dst'),
        pytest.param(CLOSE, 30 * M, '2026-10-30T21:00Z', None, '2026-11-02T21:30Z', id='november across dst'),
        # Thanksgiving is skipped and the day after closes early, 13:00 EST = 18:00Z.
        pytest.param(CLOSE, 30 * M, '2026-11-25T21:30Z', None, '2026-11-27T18:30Z', id='holiday skipped'),
        pytest.param(CLOSE, 30 * M, '2026-11-26T15:00Z', None, '2026-11-27T18:30Z', id='from the holiday'),
        pytest.param(CLOSE, 30 * M, '2026-11-27T12:00Z', None, '2026-11-27T18:30Z', id='half day early close'),
        pytest.param(CLOSE, -60 * M, '2026-11-27T12:00Z', None, '2026-11-27T17:00Z', id='half day close-1h'),
        # Negative offsets.
        pytest.param(OPEN, -15 * M, '2026-10-14T12:00Z', None, '2026-10-14T13:15Z', id='open-15 before'),
        pytest.param(OPEN, -15 * M, '2026-10-14T13:15Z', None, '2026-10-15T13:15Z', id='open-15 now exactly at fire'),
        pytest.param(OPEN, 0 * M, '2026-10-14T13:30Z', None, '2026-10-15T13:30Z', id='open now exactly at fire'),
        pytest.param(OPEN, -15 * M, '2026-10-14T13:20Z', None, '2026-10-15T13:15Z', id='open-15 past fire'),
        pytest.param(OPEN, -15 * M, '2026-10-14T17:00Z', None, '2026-10-15T13:15Z', id='open-15 in session'),
        pytest.param(OPEN, -15 * M, '2026-03-06T20:00Z', None, '2026-03-09T13:15Z', id='open-15 across dst'),
        pytest.param(OPEN, 0 * M, '2026-03-06T15:00Z', None, '2026-03-09T13:30Z', id='open across march dst'),
        # An offset landing on a non-session day stays keyed to its session.
        pytest.param(CLOSE, 600 * M, '2026-10-17T01:00Z', None, '2026-10-17T06:00Z', id='close+10h on saturday'),
        pytest.param(CLOSE, 600 * M, '2026-10-17T06:00Z', None, '2026-10-20T06:00Z', id='after the saturday fire'),
        pytest.param(CLOSE, 600 * M, '2026-11-26T03:00Z', None, '2026-11-26T07:00Z', id='close+10h on the holiday'),
        # Elapsed time across the change itself: wall-clock arithmetic in New York would be an hour off either way.
        # `now` is past Thursday's own fire (close + 36 h lands on Saturday), so the answer is Friday's.
        pytest.param(CLOSE, 36 * 60 * M, '2026-03-07T10:00Z', None, '2026-03-08T09:00Z', id='close+36h over spring'),
        pytest.param(CLOSE, 36 * 60 * M, '2026-10-31T09:00Z', None, '2026-11-01T08:00Z', id='close+36h over fall'),
        # Before the calendar's first session: last_completed_session is None.
        pytest.param(CLOSE, 30 * M, '2026-02-21T12:00Z', None, '2026-02-23T21:30Z', id='before any session'),
    ],
)
def test_after_trading_event(event, offset, now, last_run, expected):
    schedule = AfterTradingEvent(FakeNewYorkCalendar(), event, offset)
    fire = schedule.next_fire(z(now), None if last_run is None else z(last_run))
    assert fire == z(expected)
    assert fire.utcoffset() == timedelta(0)


def test_after_trading_event_walks_each_fire_in_turn():
    """Feeding each fire back as now and last_run steps session by session: no session skipped, none repeated."""
    schedule = AfterTradingEvent(FakeNewYorkCalendar(), CLOSE, 30 * M)
    now, fires = z('2026-11-23T12:00Z'), []
    for _ in range(6):
        now = schedule.next_fire(now, now if fires else None)
        fires.append(now)
    assert fires == [
        z(t)
        for t in (
            '2026-11-23T21:30Z',
            '2026-11-24T21:30Z',
            '2026-11-25T21:30Z',
            '2026-11-27T18:30Z',
            '2026-11-30T21:30Z',
            '2026-12-01T21:30Z',
        )
    ]


def test_a_calendar_that_never_advances_is_an_error_not_a_hang():
    class StuckCalendar(FakeNewYorkCalendar):
        def next_session(self, session: date) -> date:
            return date(2026, 10, 13)

    with pytest.raises(RuntimeError, match='no session that fires after the floor'):
        AfterTradingEvent(StuckCalendar(), CLOSE, 30 * M).next_fire(T0, None)


def test_past_the_calendar_end_the_calendar_error_reaches_the_runner():
    with pytest.raises(LookupError):
        AfterTradingEvent(FakeNewYorkCalendar(), CLOSE).next_fire(z('2026-12-31T22:00Z'), None)


# ---------------------------------------------------------------------------------------------------------------------
# run_periodic: the loop


@pytest.mark.asyncio
async def test_the_runner_sleeps_until_the_fire_runs_once_and_ends_on_none(caplog):
    clock = FakeClock()
    sleep = AdvancingSleep(clock)
    job = Job(clock, block_after=1)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        task = start(Once(T0 + timedelta(minutes=2)), job, clock, sleep)
        assert task.get_name() == f'periodic:{NAME}'
        async with asyncio.timeout(GUARD_S):
            assert await task is None
    assert sleep.delays == [120.0]
    assert job.starts == [T0 + timedelta(minutes=2)]
    assert [r.getMessage() for r in records(caplog)] == [f'periodic job finished: name={NAME}']


@pytest.mark.asyncio
async def test_a_fire_already_due_runs_without_sleeping():
    clock = FakeClock()
    sleep = AdvancingSleep(clock)
    job = Job(clock, block_after=1)
    async with asyncio.timeout(GUARD_S):
        await start(Once(T0 - HOUR), job, clock, sleep)
    assert sleep.delays == []
    assert job.starts == [T0]


@pytest.mark.asyncio
async def test_the_next_delay_is_computed_after_the_run_finishes():
    """A 4 s job on a 10 s interval: the runner sleeps 6 s, measured from the end of the run to start + 10 s."""
    clock = FakeClock()
    sleep = AdvancingSleep(clock)
    job = Job(clock, block_after=4, duration_s=4)
    await finished(start(UpTo(Every(timedelta(seconds=10)), job, 4), job, clock, sleep))
    assert sleep.delays == [6.0, 6.0, 6.0]
    assert job.starts == [T0 + timedelta(seconds=10 * n) for n in range(4)]


@pytest.mark.asyncio
async def test_a_slow_job_delays_the_next_run_and_never_overlaps_it():
    """A 25 s job on a 10 s interval: runs back to back, one at a time, each start one job-length after the last."""
    clock = FakeClock()
    sleep = AdvancingSleep(clock)
    job = Job(clock, block_after=5, duration_s=25)
    await finished(start(UpTo(Every(timedelta(seconds=10)), job, 5), job, clock, sleep))
    assert job.max_active == 1
    assert sleep.delays == []
    assert job.starts == [T0 + timedelta(seconds=25 * n) for n in range(5)]


@pytest.mark.asyncio
async def test_a_long_stall_is_one_immediate_run_then_the_full_interval():
    """The first run takes an hour on a 10 min interval: one catch-up run at once, then a full 10 min sleep."""
    clock = FakeClock()
    sleep = AdvancingSleep(clock)
    job = Job(clock, block_after=4)
    job.during = {1: lambda: clock.advance(3600)}
    await finished(start(UpTo(Every(timedelta(minutes=10)), job, 4), job, clock, sleep))
    assert job.starts == [T0, T0 + HOUR, T0 + HOUR + timedelta(minutes=10), T0 + HOUR + timedelta(minutes=20)]
    assert sleep.delays == [600.0, 600.0]


# ---------------------------------------------------------------------------------------------------------------------
# run_periodic: wake


@pytest.mark.asyncio
async def test_wake_ends_the_sleep_and_runs_at_once():
    clock = FakeClock()
    sleep = BlockingSleep()
    wake = asyncio.Event()
    job = Job(clock, block_after=5)
    task = start(Every(HOUR, first=T0 + HOUR), job, clock, sleep, wake=wake)
    async with asyncio.timeout(GUARD_S):
        await until(lambda: len(sleep.delays) == 1, 'the first sleep')
        assert job.calls == 0
        wake.set()
        await until(lambda: len(sleep.delays) == 2, 'the sleep after the woken run')
    assert job.calls == 1
    assert sleep.cancelled == 1
    assert not wake.is_set()
    # The woken run counts as the last run: the next fire is an interval after it.
    assert sleep.delays == [3600.0, 3600.0]
    await stop(task)


@pytest.mark.asyncio
async def test_wake_set_before_the_first_sleep_runs_without_sleeping():
    clock = FakeClock()
    sleep = BlockingSleep()
    wake = asyncio.Event()
    wake.set()
    job = Job(clock, block_after=5)
    task = start(Every(HOUR, first=T0 + HOUR), job, clock, sleep, wake=wake)
    async with asyncio.timeout(GUARD_S):
        await until(lambda: len(sleep.delays) == 1, 'the sleep after the run')
    assert job.calls == 1
    assert sleep.delays == [3600.0]
    await stop(task)


@pytest.mark.asyncio
async def test_wakes_during_a_run_cause_exactly_one_more_run():
    clock = FakeClock()
    sleep = BlockingSleep()
    wake = asyncio.Event()

    def three_wakes() -> None:
        for _ in range(3):
            wake.set()

    job = Job(clock, block_after=5, during={1: three_wakes})
    task = start(Every(HOUR, first=T0 + HOUR), job, clock, sleep, wake=wake)
    async with asyncio.timeout(GUARD_S):
        await until(lambda: len(sleep.delays) == 1, 'the first sleep')
        wake.set()
        await until(lambda: len(sleep.delays) == 2, 'the sleep after the extra run')
    assert job.calls == 2
    assert not wake.is_set()
    await stop(task)


# ---------------------------------------------------------------------------------------------------------------------
# run_periodic: a failing job


@pytest.mark.asyncio
async def test_a_failing_job_is_logged_by_type_only_and_the_loop_carries_on(caplog):
    clock = FakeClock()
    sleep = AdvancingSleep(clock)
    job = Job(clock, block_after=3, raises={1: RuntimeError(SECRET)})
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await finished(start(UpTo(Every(timedelta(seconds=10)), job, 3), job, clock, sleep))
    # On schedule: the failed run still counts as a run, so the next is an interval after its start.
    assert job.starts == [T0, T0 + timedelta(seconds=10), T0 + timedelta(seconds=20)]
    (failure,) = [r for r in records(caplog) if r.levelno >= logging.WARNING]
    assert failure.levelno == logging.ERROR
    assert failure.getMessage() == f'periodic job failed: name={NAME} error=RuntimeError'
    assert failure.exc_info is None
    assert all(SECRET not in (r.getMessage() + (r.exc_text or '')) for r in caplog.records)


@pytest.mark.asyncio
async def test_a_cancelled_error_from_the_job_is_not_swallowed():
    # Once, so a runner that swallowed it would end normally (and fail here) rather than outlive the test.
    clock = FakeClock()
    job = Job(clock, block_after=5, raises={1: asyncio.CancelledError()})
    task = start(Once(T0), job, clock, AdvancingSleep(clock))
    await ended(task)
    assert task.cancelled()
    assert job.calls == 1


# ---------------------------------------------------------------------------------------------------------------------
# run_periodic: cancellation


def _others() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]


@pytest.mark.asyncio
@pytest.mark.parametrize('with_wake', [False, True], ids=['no wake', 'wake'])
async def test_cancel_during_the_sleep_ends_the_task_and_leaves_nothing_behind(with_wake):
    clock = FakeClock()
    sleep = BlockingSleep()
    job = Job(clock, block_after=5)
    task = start(Every(HOUR, first=T0 + HOUR), job, clock, sleep, wake=asyncio.Event() if with_wake else None)
    async with asyncio.timeout(GUARD_S):
        await until(lambda: len(sleep.delays) == 1, 'the sleep')
        await stop(task)
        await asyncio.sleep(0)
    assert task.cancelled()
    assert sleep.cancelled == 1
    assert job.calls == 0
    assert _others() == []


@pytest.mark.asyncio
async def test_cancel_during_the_job_ends_the_task():
    # Once, so a runner that swallowed the cancel would end normally (and fail here) rather than outlive the test.
    clock = FakeClock()
    job = Job(clock, block_after=0)
    task = start(Once(T0), job, clock, BlockingSleep(), wake=asyncio.Event())
    async with asyncio.timeout(GUARD_S):
        await until(lambda: job.calls == 1, 'the run')
        await stop(task)
    assert task.cancelled()
    assert job.cancelled


# ---------------------------------------------------------------------------------------------------------------------
# run_periodic: a schedule that cannot compute its next fire


@pytest.mark.asyncio
async def test_a_failing_schedule_is_logged_by_type_and_retried_after_the_default_delay(caplog):
    clock = FakeClock()
    sleep = AdvancingSleep(clock)
    job = Job(clock, block_after=1)
    at = T0 + timedelta(minutes=10)
    schedule = ScriptedSchedule(LookupError(SECRET), LookupError(SECRET), at, None)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        async with asyncio.timeout(GUARD_S):
            await start(schedule, job, clock, sleep)
    # Two 5-minute waits, then the schedule answers and the job runs at its fire; the loop never ended early.
    assert sleep.delays == [DEFAULT_RETRY_S, DEFAULT_RETRY_S]
    assert [now for now, _ in schedule.asks] == [T0, T0 + timedelta(minutes=5), at, at]
    assert job.starts == [at]
    failures = [r for r in records(caplog) if r.levelno == logging.ERROR]
    assert [r.getMessage() for r in failures] == [
        f'periodic schedule failed: name={NAME} error=LookupError retry_in=0:05:00'
    ] * 2
    assert all(r.exc_info is None for r in failures)
    assert all(SECRET not in (r.getMessage() + (r.exc_text or '')) for r in caplog.records)


@pytest.mark.asyncio
async def test_a_failing_schedule_waits_the_given_retry_delay():
    clock = FakeClock()
    sleep = AdvancingSleep(clock)
    schedule = ScriptedSchedule(RuntimeError('x'), None)
    async with asyncio.timeout(GUARD_S):
        await start(schedule, Job(clock, block_after=0), clock, sleep, retry_delay=timedelta(seconds=7))
    assert sleep.delays == [7.0]


@pytest.mark.asyncio
async def test_a_failing_real_schedule_keeps_the_task_alive():
    """A calendar asked past its end raises; the runner retries rather than ending the task."""
    clock = FakeClock(z('2026-12-31T22:00Z'))
    sleep = BlockingSleep()
    task = start(AfterTradingEvent(FakeNewYorkCalendar(), CLOSE), Job(clock, block_after=0), clock, sleep)
    async with asyncio.timeout(GUARD_S):
        await until(lambda: len(sleep.delays) == 1, 'the retry wait')
    assert not task.done()
    assert sleep.delays == [DEFAULT_RETRY_S]
    await stop(task)


@pytest.mark.asyncio
async def test_wake_ends_the_retry_wait_and_the_schedule_is_asked_again():
    clock = FakeClock()
    sleep = BlockingSleep()
    wake = asyncio.Event()
    schedule = ScriptedSchedule(RuntimeError('x'), None)
    task = start(schedule, Job(clock, block_after=0), clock, sleep, wake=wake)
    async with asyncio.timeout(GUARD_S):
        await until(lambda: len(sleep.delays) == 1, 'the retry wait')
        wake.set()
        await task
    assert sleep.delays == [DEFAULT_RETRY_S]
    assert sleep.cancelled == 1
    assert len(schedule.asks) == 2
    assert not wake.is_set()


@pytest.mark.asyncio
async def test_cancel_during_the_retry_wait_ends_the_task():
    clock = FakeClock()
    sleep = BlockingSleep()
    task = start(ScriptedSchedule(RuntimeError('x')), Job(clock, block_after=0), clock, sleep, wake=asyncio.Event())
    async with asyncio.timeout(GUARD_S):
        await until(lambda: len(sleep.delays) == 1, 'the retry wait')
        await stop(task)
        await asyncio.sleep(0)
    assert sleep.cancelled == 1
    assert _others() == []


@pytest.mark.parametrize('retry_delay', [timedelta(0), timedelta(seconds=-1)])
def test_a_non_positive_retry_delay_is_refused_before_any_task_starts(retry_delay):
    # Called with no running loop: reaching create_task would raise RuntimeError, so ValueError proves the check is first.
    with pytest.raises(ValueError, match='retry_delay must be positive'):
        run_periodic(NAME, Once(T0), Job(FakeClock(), block_after=0), clock=FakeClock(), retry_delay=retry_delay)


# ---------------------------------------------------------------------------------------------------------------------
# Restart: no replay of missed runs


@pytest.mark.asyncio
async def test_after_a_restart_missed_trading_fires_are_not_replayed():
    """A restarted runner waits for the next fire and never runs the one it missed.

    Process one fires on Monday and Tuesday, then stops. Process two starts on Thursday morning: it waits for
    Thursday's fire and never runs Wednesday's (catch-up is the caller's, from the database).
    """
    calendar = FakeNewYorkCalendar()

    clock = FakeClock(z('2026-10-12T12:00Z'))
    job = Job(clock, block_after=2)
    await finished(start(UpTo(AfterTradingEvent(calendar, CLOSE, 30 * M), job, 2), job, clock, AdvancingSleep(clock)))
    assert job.starts == [z('2026-10-12T20:30Z'), z('2026-10-13T20:30Z')]

    clock = FakeClock(z('2026-10-15T12:00Z'))
    sleep = AdvancingSleep(clock)
    job = Job(clock, block_after=1)
    await finished(start(UpTo(AfterTradingEvent(calendar, CLOSE, 30 * M), job, 1), job, clock, sleep))
    assert sleep.delays == [(z('2026-10-15T20:30Z') - z('2026-10-15T12:00Z')).total_seconds()]
    assert job.starts == [z('2026-10-15T20:30Z')]


@pytest.mark.asyncio
async def test_after_a_restart_an_interval_schedule_runs_once_not_once_per_missed_interval():
    clock = FakeClock()
    sleep = AdvancingSleep(clock)
    job = Job(clock, block_after=3)
    await finished(start(UpTo(Every(HOUR, first=T0 - timedelta(hours=5)), job, 3), job, clock, sleep))
    assert job.starts == [T0, T0 + HOUR, T0 + 2 * HOUR]
    assert sleep.delays == [3600.0, 3600.0]


# The test above passes while either of Every's two replay guards holds, so it cannot tell when one of them breaks
# (the architect's finding 4 on tj-grna9p.130). Each test below takes the OTHER guard away and pins the one left:
#   guard 1, the runner: last_run is the clock at the start of the run, never the instant the schedule named;
#   guard 2, Every: max(base, now), so a fire behind `now` is now, never the missed slot.


class Unclamped:
    """An interval schedule WITHOUT guard 2: it names the missed slot itself, however far behind `now` it is."""

    def __init__(self, interval: timedelta, first: datetime) -> None:
        self.interval, self.first = interval, first

    def next_fire(self, now: datetime, last_run: datetime | None) -> datetime | None:
        return self.first if last_run is None else last_run + self.interval


@pytest.mark.asyncio
async def test_the_runner_alone_prevents_a_replay_by_recording_the_run_start_not_the_fire():
    """Guard 1 alone: a schedule that names past slots still runs once, then waits a full interval.

    Were the runner to record the fire it was given as last_run, each next slot (first + 1 h, + 2 h, ...) would also
    be behind the clock, and the five missed runs would replay back to back.
    """
    clock = FakeClock()
    sleep = AdvancingSleep(clock)
    job = Job(clock, block_after=3)
    await finished(start(UpTo(Unclamped(HOUR, T0 - timedelta(hours=5)), job, 3), job, clock, sleep))
    assert job.starts == [T0, T0 + HOUR, T0 + 2 * HOUR]
    assert sleep.delays == [3600.0, 3600.0]


def test_every_alone_prevents_a_replay_when_last_run_is_the_fire_it_named():
    """Guard 2 alone: Every is asked as a runner WITHOUT guard 1 would ask it, last_run = the fire it named.

    The clock stands still (the job takes no time) and only moves forward to a fire that lies ahead of it. Every
    must still answer now, then one interval later, never first + 1 h, + 2 h, ... behind the clock.
    """
    every = Every(HOUR, first=T0 - timedelta(hours=5))
    now, last_run, fires = T0, None, []
    for _ in range(3):
        fire = every.next_fire(now, last_run)
        fires.append(fire)
        now, last_run = max(now, fire), fire
    assert fires == [T0, T0 + HOUR, T0 + 2 * HOUR]


# ---------------------------------------------------------------------------------------------------------------------
# The two docstring claims of tj-grna9p.132, pinned


def test_jitter_is_added_to_a_first_fire_too():
    """Every's docstring: jitter is added to every fire, the first included (with and without `first`)."""
    jitter = timedelta(minutes=10)
    assert Every(HOUR, jitter=jitter, rng=lambda: 0.5).next_fire(T0, None) == T0 + timedelta(minutes=5)
    first = T0 + timedelta(minutes=20)
    assert Every(HOUR, first=first, jitter=jitter, rng=lambda: 0.5).next_fire(T0, None) == first + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_a_wake_during_the_retry_wait_asks_again_and_does_not_force_a_run():
    """run_periodic's docstring: the wake ends the retry wait, the schedule is asked again, and its answer stands.

    The second answer is an hour ahead, so the runner sleeps that hour before the run: the wake did not run the job.
    """
    clock = FakeClock()
    sleep = BlockingSleep()
    wake = asyncio.Event()
    job = Job(clock, block_after=0)
    schedule = ScriptedSchedule(RuntimeError('x'), T0 + HOUR)
    task = start(schedule, job, clock, sleep, wake=wake)
    async with asyncio.timeout(GUARD_S):
        await until(lambda: len(sleep.delays) == 1, 'the retry wait')
        wake.set()
        await until(lambda: len(sleep.delays) == 2, 'the sleep until the next fire')
        for _ in range(20):
            await asyncio.sleep(0)
    assert sleep.delays == [DEFAULT_RETRY_S, 3600.0]
    assert job.calls == 0
    assert len(schedule.asks) == 2
    await stop(task)
