"""Periodic and timed work: one runner and three schedule kinds, shared by both services.

A `Schedule` is pure: given `now` and the instant of the last run it names the next fire. `run_periodic` owns the
loop around it: sleep until the fire, run the job, record the run, repeat.

Restart semantics. The runner keeps NO state across a restart and NEVER replays missed runs. After a restart a
schedule computes its next fire from `now` with `last_run=None`. Catch-up is the caller's job, derived from
database state: the daily pass requests from the last stored bar, the reaper reads `expiry < now`, the rollup folds
whatever is past the window. The database is the truth, so a replayed timer would only repeat work it already
describes. A job must therefore be idempotent and must work out what is outstanding from the database, not from the
fact that it fired.

A schedule that cannot produce a fire time (its `next_fire` raises, for instance a calendar asked for a session past
its end) is retried every `retry_delay`, so a job never stops silently. The failure is logged by exception type only.
Catch-up and replay remain the caller's job.

Kinds:
    Once: one fire at an instant (or at once, if that instant is past and it has not run).
    AfterTradingEvent: a session's open or close plus an offset, from a calendar passed in as an argument.
    Every: a fixed interval, with optional jitter; an overrun fires once, never a burst.

Every instant is an aware datetime and all arithmetic is on UTC. A naive datetime names no instant and is refused.

The calendar is a structural `SessionCalendar`, never an import: this module needs neither exchange_calendars nor
pandas, which only the data-store image carries. The store's own calendar adapter matches the protocol.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from logging import Logger
from typing import Protocol

from common.logging import get_logger


# A session whose event is already behind the floor is skipped; only a session in progress needs it, so the walk is
# short. The bound turns a misbehaving calendar into an error rather than a hang.
_MAX_SESSIONS_SCANNED = 8


def _require_aware(value: datetime, what: str) -> datetime:
    """Return `value` in UTC; a naive datetime names no instant."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f'{what}: a naive datetime names no instant')
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Schedule(Protocol):
    """When a job fires next. Pure: no I/O, no sleeping."""

    def next_fire(self, now: datetime, last_run: datetime | None) -> datetime | None:
        """The next instant to run, or None for never again.

        Args:
            now (datetime): The current instant (aware).
            last_run (datetime | None): When the job last started, or None if it has not run in this process.

        Returns:
            datetime | None: An aware instant. One at or before `now` means run immediately.
        """
        ...


class SessionCalendar(Protocol):
    """The part of a trading calendar that `AfterTradingEvent` needs. All datetimes are aware."""

    def session_open(self, session: date) -> datetime: ...

    def session_close(self, session: date) -> datetime: ...

    def next_session(self, session: date) -> date:
        """The first session strictly after `session`, which need not itself be a session."""
        ...

    def last_completed_session(self, now: datetime) -> date | None:
        """The latest session whose close is at or before `now`, or None if there is none."""
        ...


class TradingEvent(Enum):
    OPEN = 'open'
    CLOSE = 'close'


@dataclass(frozen=True)
class Once:
    """Fire once at `at`, or at once if `at` is already past and the job has not run. Never again after a run.

    A run that fails is not retried: a failed one-shot is the caller's catch-up to derive from the database.
    """

    at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.at, 'at')

    @classmethod
    def after(cls, delay: timedelta, now: datetime) -> 'Once':
        """Fire `delay` after `now`."""
        return cls(_require_aware(now, 'now') + delay)

    def next_fire(self, now: datetime, last_run: datetime | None) -> datetime | None:
        now = _require_aware(now, 'now')
        if last_run is not None:
            _require_aware(last_run, 'last_run')
            return None
        return max(self.at.astimezone(UTC), now)


@dataclass(frozen=True)
class AfterTradingEvent:
    """Fire at a session's open or close plus `offset` (positive or negative).

    The next fire is the earliest session whose event + offset is after `now` and after `last_run`. Non-sessions and
    holidays are skipped, a half day uses its real close, and the offset is elapsed time on a UTC instant, so it is
    DST-correct. A fire that lands on a non-session day (close + 10 h) is kept: it belongs to its session.
    """

    calendar: SessionCalendar
    event: TradingEvent
    offset: timedelta = timedelta(0)

    def _instant(self, session: date) -> datetime:
        at = (
            self.calendar.session_open(session)
            if self.event is TradingEvent.OPEN
            else self.calendar.session_close(session)
        )
        return _require_aware(at, 'calendar').astimezone(UTC) + self.offset

    def next_fire(self, now: datetime, last_run: datetime | None) -> datetime | None:
        floor = _require_aware(now, 'now')
        if last_run is not None:
            floor = max(floor, _require_aware(last_run, 'last_run'))
        # The first session whose close is after `floor - offset`. The close is never before the open, so no
        # earlier session can fire after the floor, for either event.
        target = floor - self.offset
        last = self.calendar.last_completed_session(target)
        if last is None:
            session = self.calendar.next_session(target.date() - timedelta(days=1))
        else:
            session = self.calendar.next_session(last)
        for _ in range(_MAX_SESSIONS_SCANNED):
            fire = self._instant(session)
            if fire > floor:
                return fire
            session = self.calendar.next_session(session)
        raise RuntimeError('the calendar returned no session that fires after the floor')


@dataclass(frozen=True)
class Every:
    """Fire every `interval`, measured from the start of the last run, plus uniform jitter in [0, jitter).

    With no run yet it fires at `first`, or at once. A run that overran its interval fires once, at once.
    Jitter is added to every fire, the first and an overrun's included.
    """

    interval: timedelta
    first: datetime | None = None
    jitter: timedelta = timedelta(0)
    # Injectable random source: () -> float in [0, 1).
    rng: Callable[[], float] = random.random

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0):
            raise ValueError('interval must be positive')
        if self.jitter < timedelta(0):
            raise ValueError('jitter must not be negative')
        if self.first is not None:
            _require_aware(self.first, 'first')

    def next_fire(self, now: datetime, last_run: datetime | None) -> datetime | None:
        now = _require_aware(now, 'now')
        if last_run is None:
            base = now if self.first is None else self.first.astimezone(UTC)
        else:
            base = _require_aware(last_run, 'last_run') + self.interval
        # An overrun is one immediate fire, never one per missed interval.
        fire = max(base, now)
        if self.jitter > timedelta(0):
            fire += self.jitter * self.rng()
        return fire


def run_periodic(
    name: str,
    schedule: Schedule,
    job: Callable[[], Awaitable[None]],
    *,
    clock: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    wake: asyncio.Event | None = None,
    log: Logger | None = None,
    retry_delay: timedelta = timedelta(minutes=5),
) -> asyncio.Task[None]:
    """Start the loop for one periodic job and return its task (call from a running event loop).

    A run never overlaps the next, and the next delay is computed after a run finishes. `wake.set()` ends the sleep
    early; a wake that arrives during a run causes exactly one more run, not a queue. A job that raises is logged by
    exception type only, never its message, and the loop carries on. Cancellation is never swallowed: cancel the
    task at shutdown and await it.

    A schedule whose `next_fire` raises is logged by exception type only, then retried after `retry_delay`; a wake
    ends that wait early. The loop never ends because a fire time could not be computed. During the retry wait after
    a failing schedule, a wake ends the wait and asks the schedule again; it does not force a run. Outside that wait,
    a wake runs the job.

    Args:
        name (str): Names the job in the log and the task.
        schedule (Schedule): When to run.
        job (Callable[[], Awaitable[None]]): The work. Should be idempotent (see the module docstring).
        clock (Callable[[], datetime], optional): The current aware instant. Defaults to UTC now.
        sleep (Callable[[float], Awaitable[None]], optional): Sleeps for seconds. Defaults to asyncio.sleep.
        wake (asyncio.Event, optional): Set to run the job before its next fire. Cleared when a run starts.
        log (Logger, optional): Defaults to this module's logger.
        retry_delay (timedelta, optional): The wait before asking a failing schedule again. Must be positive.

    Returns:
        asyncio.Task[None]: Ends when the schedule returns None, or when cancelled.

    Raises:
        ValueError: If `retry_delay` is not positive.
    """
    if retry_delay <= timedelta(0):
        raise ValueError('retry_delay must be positive')
    return asyncio.create_task(
        _run_loop(name, schedule, job, clock, sleep, wake, log or get_logger(__name__), retry_delay),
        name=f'periodic:{name}',
    )


async def _sleep_or_wake(delay: float, sleep: Callable[[float], Awaitable[None]], wake: asyncio.Event | None) -> None:
    if wake is None:
        await sleep(delay)
        return
    sleeper = asyncio.ensure_future(sleep(delay))
    waker = asyncio.ensure_future(wake.wait())
    try:
        await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # Also reached on cancellation: neither helper outlives the sleep.
        for pending in (sleeper, waker):
            pending.cancel()
        await asyncio.gather(sleeper, waker, return_exceptions=True)


async def _run_loop(
    name: str,
    schedule: Schedule,
    job: Callable[[], Awaitable[None]],
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
    wake: asyncio.Event | None,
    log: Logger,
    retry_delay: timedelta,
) -> None:
    last_run: datetime | None = None
    while True:
        now = _require_aware(clock(), 'clock')
        try:
            fire = schedule.next_fire(now, last_run)
        except Exception as exc:
            # Type only (D8). Never end the task silently: wait a bounded delay and ask again.
            log.error('periodic schedule failed: name=%s error=%s retry_in=%s', name, type(exc).__name__, retry_delay)
            await _sleep_or_wake(retry_delay.total_seconds(), sleep, wake)
            if wake is not None:
                wake.clear()
            continue
        if fire is None:
            log.info('periodic job finished: name=%s', name)
            return
        delay = (fire - now).total_seconds()
        if delay > 0 and (wake is None or not wake.is_set()):
            await _sleep_or_wake(delay, sleep, wake)
        if wake is not None:
            # Cleared as the run starts, so a wake during the run is one more run.
            wake.clear()
        last_run = _require_aware(clock(), 'clock')
        try:
            await job()
        except Exception as exc:
            # The job is isolated (CancelledError is a BaseException and passes). The type only: a message can
            # carry a secret (D8).
            log.error('periodic job failed: name=%s error=%s', name, type(exc).__name__)
