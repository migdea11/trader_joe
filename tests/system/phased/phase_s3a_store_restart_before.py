"""P26 S3, phase A: work queued in the RUNNING ingest just before data_store is stopped and started (tj-7zjov1.286).

THE SCENARIO (P26 tj-7zjov1.38 acceptance: "a store restart: ingest keeps its queue and redelivers, nothing
duplicated"; ADR tj-26jt9w 2026-10-24 ack ruling: after every attempt ingest reports outcome and spend, and a retry
does not run until the store acknowledges). Phase A puts COUNT slow bars requests on ingest's queue through the store's
own watcher (scenario.py says how and why) and checks that they were all sent to the one live instance and that at
least LEFT_OPEN are still open when it ends. Then the orchestrator stops data_store at once, waits at least 120 s and
starts it again: a deterministic outage, as in S5 but at ingest's default buffer and workers (tj-7zjov1.353; a plain
restart's 10 s fell between deliveries, so no delivery failed and the redelivery went untested). While the store is
down, ingest keeps working its queue; the reports it cannot deliver stay in its outgoing buffer and are redelivered,
same (request_id, attempt_no), until the started store acknowledges them. Phase B (phase_s3b_store_restart_after.py)
judges the result, and fails saying so when the outage was under scenario.MIN_OUTAGE_S (98 s) or no attempt begun
before it was delivered after it.

WHY 120 s (tj-7zjov1.350 F-A). Ingest's delivery call waits for a stopped store (wait_for_ready) for up to its 60 s
deadline (common/rpc/clients/store_delivery.py DEFAULT_DELIVERY_DEADLINE_S), so an outage shorter than that fails no
delivery: the first call simply succeeds when the store returns, and the redelivery under test never runs (the stack
run on tj-7zjov1.350: a 66 s outage left M3r alive, a 125 s one killed it). Phase B needs the outage, dated from the
last delivery before the stop, to reach MIN_OUTAGE_S = 60 s deadline + 8 s slow delay + 30 s margin = 98 s (the
arithmetic is at scenario.MIN_OUTAGE_S); 120 s of wait after the stop clears it with room for the stop's own lag. Keep
it under about 240 s, so the oldest held report stays clear of ALPACA_STALL_TIMEOUT_S (300 s) and its stall ERROR.
COST against the former 30 s step: about 90 s more wall time per S3 run, no extra stack call.

TIMING. COUNT slow attempts at WORKERS at once and the fake's 8 s each is about 96 s of work from the moment they are
queued, so the stop must follow this phase without delay: if the queue drains first, phase B fails saying so. Most of
that work now finishes inside the 120 s outage; its reports wait in the outgoing buffer (64 slots, above COUNT) and
are delivered once the store is back, inside phase B's FINISH_S (300 s).

ORCHESTRATOR STEPS (the hand-back of tj-7zjov1.286 has the whole list; steps 2 to 4 replaced by tj-7zjov1.353):
  1. run_system_tests paths ['tests/system/phased/phase_s3a_store_restart_before.py']  -> 1 passed
  2. test_stop service data_store, IMMEDIATELY
  3. wait at least 120 s (phase B needs scenario.MIN_OUTAGE_S, 98 s, dated from the last delivery before the stop:
     past the 60 s delivery deadline, see WHY 120 s), then test_start service data_store
  4. logs service data_ingest tail 400: expect WARNING lines 'Handle ALPACA_DATA: delivery of request <id> attempt 1
     failed (...); redelivering the same attempt in <n>s' (the reports held across the outage); no ERROR
     'Store delivery stalled' unless the outage outlasted ALPACA_STALL_TIMEOUT_S (300 s by default)
  5. run_system_tests paths ['tests/system/phased/phase_s3b_store_restart_after.py']  -> 1 passed (polls up to 300 s)
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
