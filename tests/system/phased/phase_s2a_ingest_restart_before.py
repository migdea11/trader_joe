"""P26 S2, phase A: work queued in the RUNNING ingest just before data_ingest is restarted (tj-7zjov1.286).

THE SCENARIO (P26 tj-7zjov1.38 acceptance: "an ingest restart (re-submission, NOT_DELIVERED never counted as failure,
the charge)"; ADR tj-26jt9w 2026-10-24 restart ruling: a new ingest instance; everything pending that never reached the
store is treated as not delivered, re-requested, and counted against quota with the conservative estimate). Phase A
puts COUNT slow bars requests on ingest's queue through the store's own watcher (scenario.py) and checks that they were
all sent to the one live instance and that at least LEFT_OPEN are still open in it when this phase ends. Then the
orchestrator restarts data_ingest at once: the new process mints a new instance id and announces it, the store's
sighting supersedes the old instance and marks the requests QUEUED under it NOT_DELIVERED, and the instance watcher
re-submits them, flagged recovered with their durable attempt count, once the old instance has been superseded for the
60 s delivery deadline plus the 15 s margin. Phase B (phase_s2b_ingest_restart_after.py) judges the result.

TIMING. About 96 s of slow work from the moment it is queued; the restart must follow this phase without delay.

ORCHESTRATOR STEPS (the hand-back of tj-7zjov1.286 has the whole list):
  1. run_system_tests paths ['tests/system/phased/phase_s2a_ingest_restart_before.py']  -> 1 passed
  2. restart service data_ingest, IMMEDIATELY
  3. logs service data_ingest tail 400: expect INFO 'Instance <new hex id> announced to the store' after the restart
     and the 'FAKE BROKER:' banner again
  4. logs service data_store tail 400, after at least 90 s: expect INFO 'Instance watch: 0 pending re-sent, <n>
     recovered re-submitted, 0 failed, 0 skipped' with n >= 1
  4b. logs service data_ingest tail 400, right after step 4: expect, after the restart's announcement, INFO lines of
     the fixed text 'Recovered request <request_id> (bars) charged its estimate of <N> calls to quota scope <scope>
     after an ingest restart', one per recovered request, count >= 1 (the charge itself, tj-7zjov1.290)
  5. run_system_tests paths ['tests/system/phased/phase_s2b_ingest_restart_after.py']  -> 1 passed (polls up to 600 s)
"""

import pytest

from tests.system.phased.scenario import (
    Scenario,
    assert_left_open,
    live_instance,
    seed_pending,
    sweep_stale,
    wait_until_sent,
)


pytestmark = pytest.mark.data_store

SCENARIO = Scenario('S2')
COUNT = 48
LEFT_OPEN = 16


@pytest.mark.asyncio
async def test_s2a_slow_work_is_queued_in_the_live_ingest(pg_engine, pg_async_engine, run_identity, data_store) -> None:
    """COUNT requests sent to the live instance by the watcher, at least LEFT_OPEN of them still open in ingest."""
    sweep_stale(pg_engine, SCENARIO)

    ids = await seed_pending(pg_async_engine, pg_engine, SCENARIO, run_identity.run_id, COUNT)
    rows = wait_until_sent(pg_engine, ids)

    holder = assert_left_open(rows, at_least=LEFT_OPEN, scenario=SCENARIO)
    assert holder.instance_id == live_instance(pg_engine).instance_id, (
        'the requests were queued under a superseded instance'
    )
