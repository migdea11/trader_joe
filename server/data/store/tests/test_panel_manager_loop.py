"""The panel manager's loop and its lifespan wiring (validator, tj-7zjov1.180, P22v).

THE DESIGN checked against: the 14:16 UTC note on P22 tj-7zjov1.34 (ONE loop, run_periodic Every(5 min), the first
tick at once as the catch-up, never a long Every), its 19:16 UTC note (no witness argument), and the lifespan order of
the builder's commit b03edd4 (started after the Submit client and the instance watcher, stopped before the watcher and
the channel close).

  L1 PANEL_MANAGER_ENABLED off (false, 0, no, off, any case) starts nothing; anything else is on;
     PANEL_MANAGER_DRY_RUN on only for true, 1, yes, on;
  L2 the loop is run_periodic('panel-manager', Every(5 min)) over PanelManager.tick, whose first tick runs at once;
     the manager is built with the dry-run switch; no calendar, no loop;
  L3 stop cancels and awaits the task; stop(None) is a no-op;
  L4 the lifespan starts the loop after the instance watcher and stops it before the watcher.
"""

import asyncio
import inspect
from datetime import timedelta

import pytest

from common.scheduling import Every
from data.store.app import app_depends, panel_manager


pytestmark = [pytest.mark.data_store]


@pytest.mark.parametrize('value', ['false', 'FALSE', ' 0 ', 'no', 'Off'])
def test_an_explicit_false_turns_the_loop_off(monkeypatch, value):
    monkeypatch.setenv(panel_manager.ENABLED_ENV, value)
    assert panel_manager.is_enabled() is False


@pytest.mark.parametrize('value', [None, 'true', '1', 'yes', 'anything'])
def test_anything_else_leaves_the_loop_on(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(panel_manager.ENABLED_ENV, raising=False)
    else:
        monkeypatch.setenv(panel_manager.ENABLED_ENV, value)
    assert panel_manager.is_enabled() is True


@pytest.mark.parametrize(
    ('value', 'dry'), [(None, False), ('false', False), ('2', False), ('TRUE', True), ('on', True)]
)
def test_dry_run_only_for_an_explicit_true(monkeypatch, value, dry):
    if value is None:
        monkeypatch.delenv(panel_manager.DRY_RUN_ENV, raising=False)
    else:
        monkeypatch.setenv(panel_manager.DRY_RUN_ENV, value)
    assert panel_manager.is_dry_run() is dry


@pytest.mark.asyncio
async def test_off_starts_nothing(monkeypatch):
    monkeypatch.setenv(panel_manager.ENABLED_ENV, 'false')
    built = []
    monkeypatch.setattr(panel_manager, 'PanelManager', lambda *a, **k: built.append(a))
    assert await panel_manager.start_panel_manager_loop(object()) is None
    assert built == []


@pytest.mark.asyncio
async def test_no_calendar_no_loop(monkeypatch):
    monkeypatch.delenv(panel_manager.ENABLED_ENV, raising=False)
    monkeypatch.setattr(panel_manager, 'SOURCE_CALENDARS', {})
    assert await panel_manager.start_panel_manager_loop(object()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('dry', [False, True])
async def test_one_five_minute_loop_whose_first_tick_runs_at_once_and_stops_on_cancel(monkeypatch, dry):
    monkeypatch.delenv(panel_manager.ENABLED_ENV, raising=False)
    monkeypatch.setenv(panel_manager.DRY_RUN_ENV, 'true' if dry else 'false')
    ticked = asyncio.Event()
    built: list[dict] = []
    started: list[tuple] = []

    class Recording(panel_manager.PanelManager):
        def __init__(self, client, calendars, **kw):
            built.append({'client': client, 'calendars': calendars, **kw})
            super().__init__(client, calendars, **kw)

        async def tick(self) -> None:
            ticked.set()

    real_run_periodic = panel_manager.run_periodic

    def recording_run_periodic(name, schedule, job, **kw):
        started.append((name, schedule, kw))
        return real_run_periodic(name, schedule, job, **kw)

    monkeypatch.setattr(panel_manager, 'PanelManager', Recording)
    monkeypatch.setattr(panel_manager, 'run_periodic', recording_run_periodic)
    client = object()

    task = await panel_manager.start_panel_manager_loop(client)
    try:
        await asyncio.wait_for(ticked.wait(), timeout=5)
        ((name, schedule, kw),) = started
        assert name == 'panel-manager'
        assert schedule == Every(timedelta(minutes=5))
        assert 'witness' not in kw
        ((made),) = built
        assert made['client'] is client and made['dry_run'] is dry
        assert set(made['calendars']) == set(panel_manager.SOURCE_CALENDARS)
        assert not task.done()
    finally:
        await panel_manager.stop_panel_manager_loop(task)
    assert task.done() and task.cancelled()


@pytest.mark.asyncio
async def test_stop_of_no_task_is_a_no_op():
    await panel_manager.stop_panel_manager_loop(None)


def test_the_start_takes_only_the_client():
    assert list(inspect.signature(panel_manager.start_panel_manager_loop).parameters) == ['client']


def test_the_lifespan_starts_after_the_watcher_and_stops_before_it():
    source = inspect.getsource(app_depends.lifespan)
    start, stop = source.index('start_panel_manager_loop('), source.index('stop_panel_manager_loop(')
    assert source.index('start_instance_watch_loop(') < start
    assert stop < source.index('stop_instance_watch_loop(')
    assert start < stop
