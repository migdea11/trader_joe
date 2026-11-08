"""P26 S3, phase B: after a data_store outage, ingest redelivered what it held and every report landed exactly once.

Runs after phase_s3a_store_restart_before.py and the orchestrator's test_stop data_store, wait of at least 120 s,
test_start data_store (the step list is in that file; tj-7zjov1.353 replaced the plain restart, whose 10 s fell
between deliveries so that no delivery ever failed; tj-7zjov1.350 F-A raised the outage from 30 s to past the delivery
deadline, scenario.MIN_OUTAGE_S, 98 s, because a shorter one is waited out by the delivery call). It fails loudly when phase A never ran, and when
data_store was not stopped and started after phase A (its oldest connection is older than phase A's seeding:
scenario.data_store_connected_since). S3 runs at ingest's default buffer and workers; that is what separates it from
S5, which shrinks the buffer to judge its bound.

DATING THE OUTAGE FROM THE DATABASE (scenario.outage_evidence, the same rows S5-B reads): up_at is when the started
data_store opened its oldest connection; the last delivery of a phase A request before up_at stands in for the stop,
which followed it by at most one slow delay.

WHAT IT PINS, from the database and the store's own REST ping, never by joining ingest's network:
  * the store came back: the data_store fixture's ping answers 200, and its connections are all newer than phase A;
  * nothing lost: every request SUCCEEDED, each holding one entry with exactly the generator's bars;
  * nothing duplicated: attempt rows numbered 1..k once each (the partial unique index on (request_id, attempt_no) is
    the arbiter of a redelivery), one SUCCEEDED, no FAILED, the durable count equal to the rows, the request's spend
    equal to its attempt rows' (a redelivered report counted once), one entry per symbol;
  * ingest kept its process and its queue and REDELIVERED: one instance made every attempt and it is still live, and
    at least one attempt begun before the outage (before the last delivery that preceded it) was delivered after
    data_store came back. Its first delivery call began at most one slow delay after that last delivery, and the
    outage is at least MIN_OUTAGE_S (the 60 s deadline that call waits out with wait_for_ready, plus the 8 s slow
    delay and a 30 s margin: the arithmetic is at scenario.MIN_OUTAGE_S), so that call failed by its deadline before
    the store returned: the report reached the store only because ingest redelivered it. The guard names how many
    did. Under MIN_OUTAGE_S the same rows prove nothing (the call may have waited and succeeded first time), which is
    why the outage guard comes first.
  * the running bind (P26 note 16:14 UTC 2026-10-24), as far as this phase checks it: every attempt row names the
    live instance, the bars are under the entry, all written by the RUNNING data_store over data-store-grpc, and each
    SUCCEEDED attempt row has a served_start. Only its PRESENCE is checked here (scenario.assert_each_reported_once),
    not its value: the served range's value is pinned by S1 (tests/system/test_http_ingest_runs.py, mutant M1a).
    Checking the value here would make M1a red this scenario's sibling S5-B too, in the batch where M5b must be S5-B's
    only red (W4 on tj-7zjov1.38, ruled 00:28 UTC 2026-11-04).

NOT PINNABLE HERE: that a particular report was redelivered with the same attempt number. Ingest's WARNING
'redelivering the same attempt' in its log is that evidence (phase A's orchestrator step 4); the database shows only that each
attempt number was written once.

Reds on (P26 mutant plan, tj-7zjov1.38; tj-7zjov1.321, tj-7zjov1.353, tj-7zjov1.350 F-A): M3r (a failed delivery is
abandoned instead of redelivered, data/ingest/app/scheduler/executor.py 'continue' made 'return' after the redelivery
sleep) in wait_until_finished, with an outage of at least MIN_OUTAGE_S: the abandoned reports' requests never finish
within FINISH_S (stack run on tj-7zjov1.350, outage about 125 s: 'not every request finished within 300 s: {QUEUED: 36,
SUCCEEDED: 12}'). The outage-length guard is checked before that wait and holds under M3r; the crossing guard is never
reached there, since no held report is ever delivered. With the former 30 s minimum a 66 s outage let M3r SURVIVE
(no delivery failed, both guards held); the outage guard now refuses that run instead. Guards ('did not exercise'): the
outage-length assertion (scenario.assert_outage_long_enough) and 'no attempt begun before the outage was delivered
after it'; when one fails the run proves nothing and reruns from phase A.
"""

import pytest

from tests.system.phased.scenario import (
    Scenario,
    assert_each_reported_once,
    assert_one_entry_each,
    assert_outage_long_enough,
    attempt_rows,
    cleanup_if_finished,
    data_store_connected_since,
    live_instance,
    outage_evidence,
    phase_a_run,
    request_rows,
    wait_until_finished,
)


pytestmark = pytest.mark.data_store

SCENARIO = Scenario('S3')
PHASE_A = 'tests/system/phased/phase_s3a_store_restart_before.py'
# Counted from this phase's start, after test_start: 48 requests at 4 workers and 8 s each (about 96 s, mostly done
# inside a 120 s outage, the reports held in the 64-slot buffer), plus a redelivery backoff that is still 1 to 2 s
# after one or two 60 s failed calls (it reaches its 30 s cap only after five), well inside this.
FINISH_S = 300.0


def test_s3b_the_restarted_store_holds_every_report_once(pg_engine, data_store) -> None:
    run = phase_a_run(pg_engine, SCENARIO, PHASE_A)
    try:
        up_at = data_store_connected_since(pg_engine)
        assert up_at > run.seeded_at, (
            f'data_store holds a connection from {up_at}, before phase A seeded at {run.seeded_at}: '
            'it was not stopped and started between the phases'
        )

        # Every delivery before up_at is already committed, so the outage can be dated before waiting.
        early = outage_evidence(request_rows(pg_engine, run.request_ids), {}, up_at, run.seeded_at)
        assert_outage_long_enough(early)

        requests = wait_until_finished(pg_engine, run, FINISH_S)
        attempts = attempt_rows(pg_engine, run.request_ids)
        assert_each_reported_once(requests, attempts)
        assert_one_entry_each(pg_engine, run, requests)

        live = live_instance(pg_engine).instance_id
        made_by = {row.instance_id for rows in attempts.values() for row in rows}
        assert made_by == {live}, f'attempts were made by {len(made_by)} instance(s), not only the live one'

        evidence = outage_evidence(requests, attempts, up_at, run.seeded_at)
        assert evidence.crossed, (
            f'0 of {len(requests)} attempts were begun before the outage (last delivery {evidence.last_before}) and '
            f'delivered after data_store came back at {up_at}: no delivery met the stopped store, so this run did not '
            'exercise redelivery. Rerun from phase A and test_stop data_store immediately after it.'
        )
    finally:
        cleanup_if_finished(pg_engine, run)
