"""P26 S5, phase A: a long slow queue in an ingest with a 2-report outgoing buffer, just before data_store stops.

THE SCENARIO (P26 tj-7zjov1.38 acceptance BUFFER (R2): "with the store down, ingest stops dequeuing at a full buffer,
nothing is lost, the alarm fires, and it resumes on recovery"; ADR tj-26jt9w 2026-10-24 R2 ruling: ingest stops taking
work while its outgoing buffer of unacknowledged attempts is full; the bound caps the quota an ingest crash can burn;
a stall alarm when the store is unreachable). Before this phase the orchestrator recreates data_ingest with
ALPACA_OUTGOING_BUFFER_SIZE 2 and ALPACA_STALL_TIMEOUT_S 5 (test_env, which mints a new instance). Phase A puts COUNT
slow bars requests on that ingest's queue through the store's own watcher (scenario.py) and checks they were all sent
to the one live instance with at least LEFT_OPEN still open. Then the orchestrator stops data_store at once, waits,
reads the stall alarm in ingest's log, and starts data_store again. With the store down, each worker finishes its
attempt and then holds its report until a buffer slot frees, so at most BUFFER_SIZE + WORKERS (6) attempts are begun
before the store returns and nothing more is taken from the queue. Phase B (phase_s5b_buffer_store_down_after.py)
judges it.

TIMING. COUNT slow attempts are about 128 s of work at 4 workers and 8 s each; the stop must follow this phase without
delay, and the store must stay down at least 60 s (phase B refuses an outage shorter than 30 s as not discriminating).

ORCHESTRATOR STEPS (the hand-back of tj-7zjov1.286 has the whole list):
  0. test_env service data_ingest overrides {"ALPACA_OUTGOING_BUFFER_SIZE": "2", "ALPACA_STALL_TIMEOUT_S": "5"}
  1. run_system_tests paths ['tests/system/phased/phase_s5a_buffer_store_down_before.py']  -> 1 passed
  2. test_stop service data_store, IMMEDIATELY
  3. wait at least 60 s (a wall-clock wait, no call)
  4. logs service data_ingest tail 400: expect ERROR 'Store delivery stalled on handle ALPACA_DATA: the oldest
     unacknowledged attempt, request_id=<id> attempt_no=1, has waited <n>s (timeout 5s, <r> redeliveries, 2 of 2
     buffer slots held)', repeated about every 5 s, and WARNING 'Handle ALPACA_DATA: delivery of request <id>
     attempt 1 failed (...); redelivering the same attempt in <n>s'. The ERROR names '2 of 2 buffer slots held':
     the buffer is full, which is the state in which no worker takes from the queue
  5. test_start service data_store
  6. run_system_tests paths ['tests/system/phased/phase_s5b_buffer_store_down_after.py']  -> 1 passed (polls up to
     600 s); then logs service data_ingest tail 400: expect INFO 'Store delivery on handle ALPACA_DATA is no longer
     stalled'
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

SCENARIO = Scenario('S5')
COUNT = 64
LEFT_OPEN = 24


@pytest.mark.asyncio
async def test_s5a_a_long_slow_queue_waits_in_the_live_ingest(
    pg_engine, pg_async_engine, run_identity, data_store
) -> None:
    """COUNT requests sent to the live instance by the watcher, at least LEFT_OPEN of them still open in ingest."""
    sweep_stale(pg_engine, SCENARIO)

    ids = await seed_pending(pg_async_engine, pg_engine, SCENARIO, run_identity.run_id, COUNT)
    rows = wait_until_sent(pg_engine, ids)

    holder = assert_left_open(rows, at_least=LEFT_OPEN, scenario=SCENARIO)
    assert holder.instance_id == live_instance(pg_engine).instance_id, (
        'the requests were queued under a superseded instance'
    )
