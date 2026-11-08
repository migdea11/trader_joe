"""P26 S2, phase B: after the data_ingest restart, the new instance was handed what the old one never reported.

Runs after phase_s2a_ingest_restart_before.py and the orchestrator's `restart data_ingest` (the step list is in that
file and in the hand-back of tj-7zjov1.286). It fails loudly when phase A never ran.

WHAT IT PINS, from the database:
  * a fresh instance registered: exactly one instance was live when phase A seeded and was superseded after it, and the
    live instance now is another one, first seen after phase A;
  * re-requested as not delivered: at least one request queued under the old instance was finished by an attempt the
    NEW instance made, delivered after the old one was superseded (the watcher's recovered re-submission);
  * NOT_DELIVERED never counted as a failure: every request SUCCEEDED with attempt numbers 1..k once each, no FAILED
    attempt row, the durable count equal to the rows (the report the old instance never sent is not an attempt the
    store counts, so the new instance's numbering continues from the durable count), and the spend equal to the
    attempt rows';
  * no duplicate dataset rows: one entry per symbol, exactly the generator's bars.

THE CHARGE IS NOT ASSERTED HERE, said plainly. The ruling counts a re-requested request against quota with the
conservative estimate: the new instance charges it to its scope's in-process rate window before queuing it
(server/data/ingest/app/brokers/handle.py, Scheduler.charge_restart). That window lives only in ingest's memory and
test_client cannot reach ingest, so this file cannot read it. Each charge leaves one INFO line in data_ingest's log
('Recovered request <id> (<kind>) charged its estimate of <N> calls to quota scope <scope> after an ingest restart',
tj-7zjov1.290); the orchestrator reads it with `logs data_ingest` between the restart and this phase (step 4b in
phase_s2a_ingest_restart_before.py). What this tier does show is the store's side of the same rule: the charge is
never written as spend, so a recovered request's spend is exactly its delivered attempt's. The charge and its line
are pinned in ingest's unit tier (server/data/ingest/tests/test_restart_intake.py).
"""

import pytest

from tests.system.phased.scenario import (
    Scenario,
    assert_each_reported_once,
    assert_one_entry_each,
    attempt_rows,
    cleanup_if_finished,
    instances,
    live_instance,
    phase_a_run,
    wait_until_finished,
)


pytestmark = pytest.mark.data_store

SCENARIO = Scenario('S2')
PHASE_A = 'tests/system/phased/phase_s2a_ingest_restart_before.py'
# The watcher re-submits 60 s + 15 s after the supersession, at a 15 s pass; then 48 slow attempts at 4 workers.
FINISH_S = 600.0


def test_s2b_the_new_instance_finishes_what_the_old_one_never_reported(pg_engine, data_store) -> None:
    run = phase_a_run(pg_engine, SCENARIO, PHASE_A)
    try:
        requests = wait_until_finished(pg_engine, run, FINISH_S)
        attempts = attempt_rows(pg_engine, run.request_ids)

        old = [
            row
            for row in instances(pg_engine)
            if row.superseded_at is not None and row.first_seen_at < run.seeded_at <= row.superseded_at
        ]
        assert len(old) == 1, (
            f'{len(old)} instances were live at phase A ({run.seeded_at}) and superseded after it, not one: '
            'data_ingest was not restarted between the phases'
        )
        old_instance = old[0]
        new_instance = live_instance(pg_engine)
        assert new_instance.instance_id != old_instance.instance_id
        assert new_instance.first_seen_at > run.seeded_at, 'the live instance is older than phase A: no fresh instance'

        assert_each_reported_once(requests, attempts)
        assert_one_entry_each(pg_engine, run, requests)

        made_by = {row.instance_id for rows in attempts.values() for row in rows}
        assert made_by <= {old_instance.instance_id, new_instance.instance_id}, 'an attempt by a third instance'
        recovered = [
            request_id for request_id, rows in attempts.items() if rows[-1].instance_id == new_instance.instance_id
        ]
        assert recovered, (
            'no request was finished by the new instance: every report landed before the restart, so this run did not '
            'exercise it. Rerun from phase A and restart data_ingest immediately after it.'
        )
        early = [
            request_id for request_id in recovered if requests[request_id].finished_at <= old_instance.superseded_at
        ]
        assert not early, f'{len(early)} recovered request(s) finished before the old instance was superseded'
        assert all(requests[request_id].instance_id == new_instance.instance_id for request_id in recovered), (
            'a recovered request is not held by the new instance'
        )
    finally:
        cleanup_if_finished(pg_engine, run)
