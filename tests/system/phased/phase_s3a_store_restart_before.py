"""P26 S3, phase A: work queued in the RUNNING ingest just before data_store is restarted (tj-7zjov1.286).

THE SCENARIO (P26 tj-7zjov1.38 acceptance: "a store restart: ingest keeps its queue and redelivers, nothing
duplicated"; ADR tj-26jt9w 2026-10-24 ack ruling: after every attempt ingest reports outcome and spend, and a retry
does not run until the store acknowledges). Phase A puts COUNT slow bars requests on ingest's queue through the store's
own watcher (scenario.py says how and why) and checks that they were all sent to the one live instance and that at
least LEFT_OPEN are still open when it ends. Then the orchestrator restarts data_store at once. While the store is
down, ingest keeps working its queue; the reports it cannot deliver stay in its outgoing buffer and are redelivered,
same (request_id, attempt_no), until the restarted store acknowledges them. Phase B
(phase_s3b_store_restart_after.py) judges the result.

TIMING. COUNT slow attempts at WORKERS at once and the fake's 8 s each is about 96 s of work from the moment they are
queued, so the restart must follow this phase without delay: if the queue drains first, phase B fails saying so.

ORCHESTRATOR STEPS (the hand-back of tj-7zjov1.286 has the whole list):
  1. run_system_tests paths ['tests/system/phased/phase_s3a_store_restart_before.py']  -> 1 passed
  2. restart service data_store, IMMEDIATELY
  3. logs service data_ingest tail 400: expect WARNING lines 'Handle ALPACA_DATA: delivery of request <id> attempt 1
     failed (...); redelivering the same attempt in <n>s' (the reports held across the restart); no ERROR
     'Store delivery stalled' unless the restart outlasted ALPACA_STALL_TIMEOUT_S (300 s by default)
  4. run_system_tests paths ['tests/system/phased/phase_s3b_store_restart_after.py']  -> 1 passed (polls up to 300 s)
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

SCENARIO = Scenario('S3')
COUNT = 48
LEFT_OPEN = 16


@pytest.mark.asyncio
async def test_s3a_slow_work_is_queued_in_the_live_ingest(pg_engine, pg_async_engine, run_identity, data_store) -> None:
    """COUNT requests sent to the live instance by the watcher, at least LEFT_OPEN of them still open in ingest."""
    sweep_stale(pg_engine, SCENARIO)

    ids = await seed_pending(pg_async_engine, pg_engine, SCENARIO, run_identity.run_id, COUNT)
    rows = wait_until_sent(pg_engine, ids)

    holder = assert_left_open(rows, at_least=LEFT_OPEN, scenario=SCENARIO)
    assert holder.instance_id == live_instance(pg_engine).instance_id, (
        'the requests were queued under a superseded instance'
    )
