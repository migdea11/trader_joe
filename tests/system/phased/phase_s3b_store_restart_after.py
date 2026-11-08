"""P26 S3, phase B: after the data_store restart, every report of phase A's work reached the store exactly once.

Runs after phase_s3a_store_restart_before.py and the orchestrator's `restart data_store` (the step list is in that file and in
the hand-back of tj-7zjov1.286). It fails loudly when phase A never ran, and when data_store was not restarted after
phase A (its oldest connection is older than phase A's seeding: scenario.data_store_connected_since).

WHAT IT PINS, from the database and the store's own REST ping, never by joining ingest's network:
  * the store came back: the data_store fixture's ping answers 200, and its connections are all newer than phase A;
  * nothing lost: every request SUCCEEDED, each holding one entry with exactly the generator's bars;
  * nothing duplicated: attempt rows numbered 1..k once each (the partial unique index on (request_id, attempt_no) is
    the arbiter of a redelivery), one SUCCEEDED, no FAILED, the durable count equal to the rows, the request's spend
    equal to its attempt rows' (a redelivered report counted once), one entry per symbol;
  * ingest kept its process and its queue: one instance made every attempt and it is still live, and at least one
    request queued before the restart was delivered after it. A run where every report landed before the restart
    did not exercise the restart, and fails saying so (rerun from phase A, restarting at once).
  * the running bind (P26 note 16:14 UTC 2026-10-24): each delivered attempt row carries the served range and the live
    instance id, and its bars are under the entry, all written by the RUNNING data_store over data-store-grpc.

NOT PINNABLE HERE: that a particular report was redelivered with the same attempt number. Ingest's WARNING
'redelivering the same attempt' in its log is that evidence (orchestrator step 3); the database shows only that each
attempt number was written once.
"""

import pytest

from tests.system.phased.scenario import (
    Scenario,
    assert_each_reported_once,
    assert_one_entry_each,
    attempt_rows,
    cleanup_if_finished,
    data_store_connected_since,
    live_instance,
    phase_a_run,
    wait_until_finished,
)


pytestmark = pytest.mark.data_store

SCENARIO = Scenario('S3')
PHASE_A = 'tests/system/phased/phase_s3a_store_restart_before.py'
# 48 requests at 4 workers and 8 s each, plus a redelivery backoff of at most 30 s, well inside this.
FINISH_S = 300.0


def test_s3b_the_restarted_store_holds_every_report_once(pg_engine, data_store) -> None:
    run = phase_a_run(pg_engine, SCENARIO, PHASE_A)
    try:
        restarted_at = data_store_connected_since(pg_engine)
        assert restarted_at > run.seeded_at, (
            f'data_store holds a connection from {restarted_at}, before phase A seeded at {run.seeded_at}: '
            'it was not restarted between the phases'
        )

        requests = wait_until_finished(pg_engine, run, FINISH_S)
        attempts = attempt_rows(pg_engine, run.request_ids)
        assert_each_reported_once(requests, attempts)
        assert_one_entry_each(pg_engine, run, requests)

        live = live_instance(pg_engine).instance_id
        made_by = {row.instance_id for rows in attempts.values() for row in rows}
        assert made_by == {live}, f'attempts were made by {len(made_by)} instance(s), not only the live one'

        after = [request for request in requests.values() if request.finished_at >= restarted_at]
        assert after, (
            f'every one of the {len(requests)} requests was delivered before data_store restarted at {restarted_at}: '
            'the queue drained before the restart, so this run did not exercise it. Rerun from phase A and restart '
            'data_store immediately after it.'
        )
    finally:
        cleanup_if_finished(pg_engine, run)
