"""The stall alarm's schedule on run_periodic: when it first looks, what a raising check does, and how stop() ends it.

Bead tj-7zjov1.188 against builder-ingest's b6711be. The design: the bead (AckGatedExecutor's stall watcher runs
through common.scheduling.run_periodic with an Every(stall_check_interval_s) schedule, sleep injected as before, the
task cancelled and awaited at executor stop, no witness), decision tj-tkj14t s8 (periodic work goes through
run_periodic), and run_periodic's own contract (a job that raises is logged by exception type only and the loop
carries on). test_stall_health.py already drives the alarm's loop end to end and pins its task name and that stop()
cancels it; this file pins what that one leaves open:

* the FIRST check waits one interval, as the hand-rolled loop's did (sleep, then look) -- a schedule that fires at
  once on start reds here and nowhere else;
* a check_stall that raises is logged, by type only, and the alarm keeps ticking (the hand-rolled loop died);
* stop() does not return until the alarm task has actually finished, not merely been asked to.

HOW IT IS DRIVEN. A real AckGatedExecutor over a real Scheduler with no requests, so the alarm is the only thing that
sleeps. The sleep records every duration it is asked for and holds each one until the test ticks it. The clock is
fixed. check_stall is observed by wrapping it on the instance, which the alarm's job looks up on every tick.
Every wait is bounded by GUARD_S, so a regression fails rather than hangs.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from data.ingest.app.scheduler import AckGatedExecutor, ExecutorSettings, Limit, RetryPolicy
from data.ingest.tests.scheduler_doubles import real_scheduler


pytestmark = pytest.mark.data_ingest

SCOPE = 'ALPACA_API_ALARM_SCHEDULE'
T0 = datetime(2026, 10, 27, 12, tzinfo=UTC)
GUARD_S = 5.0
WATCH_S = 7.0
ALARM_NAME = f'{SCOPE}-stall-alarm'
SECRET = 'sk-the-check-message-must-not-reach-the-log'


async def settle() -> None:
    """Let every task that can run, run."""
    for _ in range(200):
        await asyncio.sleep(0)


class HeldSleep:
    """Records every requested duration; each sleep returns only when the test ticks it.

    With `hold_cancel` set, a cancelled sleep does not end until `release_cancel` is set, so a stop() that cancels
    the alarm without awaiting it is seen returning early.
    """

    def __init__(self, *, hold_cancel: bool = False) -> None:
        self.requested: list[float] = []
        self.hold_cancel = hold_cancel
        self.release_cancel = asyncio.Event()
        self.cancelled = 0
        self.__ticks: asyncio.Queue[None] = asyncio.Queue()

    async def __call__(self, seconds: float) -> None:
        self.requested.append(seconds)
        try:
            await self.__ticks.get()
        except asyncio.CancelledError:
            self.cancelled += 1
            if self.hold_cancel:
                await asyncio.wait_for(self.release_cancel.wait(), GUARD_S)
            raise

    async def tick(self) -> None:
        self.__ticks.put_nowait(None)
        await settle()


class CountingCheck:
    """Wraps the executor's check_stall on the instance: counts the calls, and raises on the ones asked to."""

    def __init__(self, executor: AckGatedExecutor, raise_on: frozenset[int] = frozenset()) -> None:
        self.calls = 0
        self.__real = executor.check_stall
        self.__raise_on = raise_on
        executor.check_stall = self  # type: ignore[method-assign]

    def __call__(self) -> bool:
        self.calls += 1
        if self.calls in self.__raise_on:
            raise RuntimeError(SECRET)
        return self.__real()


def make_executor(sleep: HeldSleep) -> AckGatedExecutor:
    return AckGatedExecutor(
        name=SCOPE,
        scheduler=real_scheduler({SCOPE: Limit(timedelta(minutes=1), 6000)}),
        run_attempt=Mock(name='run_attempt, never called: no request is submitted'),
        settings=ExecutorSettings(
            buffer_size=4, workers=1, stall_timeout_s=300.0, stall_check_interval_s=WATCH_S, retry=RetryPolicy()
        ),
        clock=lambda: T0,
        sleep=sleep,
    )


def alarm_tasks() -> list[asyncio.Task]:
    return [task for task in asyncio.all_tasks() if task.get_name() == ALARM_NAME]


# ---------------------------------------------------------------------------------------------
# The first check waits one interval
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_stall_check_waits_one_interval_then_every_check_is_one_interval_apart():
    """Sleep, then look: nothing is checked at start, and every sleep the alarm asks for is the check interval.

    Mutation: the schedule's first fire at start (Every(interval) with no `first`, or first=clock()) reds the first
    assertion pair: check_stall runs before any tick and the first sleep is not the interval.
    """
    sleep = HeldSleep()
    executor = make_executor(sleep)
    check = CountingCheck(executor)
    executor.start(Mock(name='store, never called'), 'instance', None)
    try:
        await settle()
        assert check.calls == 0, 'the stall alarm checked at start instead of one interval out'
        assert sleep.requested == [WATCH_S], f'the first wait was not one interval: {sleep.requested}'

        for n in range(1, 4):
            await sleep.tick()
            assert check.calls == n, f'tick {n} ran {check.calls} checks'
            assert sleep.requested == [WATCH_S] * (n + 1), f'after check {n} the waits were {sleep.requested}'
    finally:
        await asyncio.wait_for(executor.stop(), GUARD_S)


# ---------------------------------------------------------------------------------------------
# A raising check is logged and the alarm carries on
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_check_stall_that_raises_is_logged_by_type_and_the_alarm_keeps_ticking(caplog):
    """New in b6711be: run_periodic isolates the job, so one bad check no longer ends the alarm for the process.

    Mutation: the hand-rolled loop (b6711be^), or a job that lets the exception end the task, reds at
    'the alarm task ended'.
    """
    caplog.set_level(logging.INFO)
    sleep = HeldSleep()
    executor = make_executor(sleep)
    check = CountingCheck(executor, raise_on=frozenset({1, 2}))
    executor.start(Mock(name='store, never called'), 'instance', None)
    try:
        await settle()
        alarms = alarm_tasks()
        assert len(alarms) == 1

        await sleep.tick()
        await sleep.tick()
        assert check.calls == 2
        assert not alarms[0].done(), 'the alarm task ended when check_stall raised'
        failures = [record for record in caplog.records if 'periodic job failed' in record.getMessage()]
        assert len(failures) == 2, 'each raising check is logged once'
        assert all(record.levelno == logging.ERROR for record in failures)
        assert all(ALARM_NAME in r.getMessage() and 'RuntimeError' in r.getMessage() for r in failures)
        assert not any(SECRET in record.getMessage() for record in caplog.records), 'the exception message was logged'

        # The third check is the real one again, and it still runs on the interval.
        await sleep.tick()
        assert check.calls == 3
        assert not alarms[0].done()
        assert set(sleep.requested) == {WATCH_S}
    finally:
        await asyncio.wait_for(executor.stop(), GUARD_S)


# ---------------------------------------------------------------------------------------------
# stop() cancels the alarm and waits for it to end
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_cancels_the_alarm_and_does_not_return_until_it_has_ended():
    """The alarm's sleep holds its cancellation; stop() must still be waiting on it until it lets go.

    Mutations: the alarm left out of the cancelled set reds at 'the alarm was not cancelled'; cancelled but not
    awaited reds at 'stop() returned while the alarm was still ending'.
    """
    sleep = HeldSleep(hold_cancel=True)
    executor = make_executor(sleep)
    check = CountingCheck(executor)
    executor.start(Mock(name='store, never called'), 'instance', None)
    await settle()
    alarms = alarm_tasks()
    assert len(alarms) == 1
    alarm = alarms[0]

    stopping = asyncio.create_task(executor.stop())
    try:
        await settle()
        assert sleep.cancelled == 1, 'the alarm was not cancelled'
        assert not alarm.done()
        assert not stopping.done(), 'stop() returned while the alarm was still ending'
    finally:
        sleep.release_cancel.set()
        await asyncio.wait_for(stopping, GUARD_S)
    assert alarm.cancelled(), 'the alarm ended some other way than by cancellation'
    assert alarm_tasks() == []
    assert check.calls == 0, 'stop() ran a check on its way out'
