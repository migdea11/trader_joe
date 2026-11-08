"""P26 S5, phase B: the buffer bounded the work taken while data_store was down; every report then landed once.

On recovery the queue resumed. Runs after phase_s5a_buffer_store_down_before.py and the orchestrator's test_stop / wait / logs / test_start of
data_store (the step list is in that file and in the hand-back of tj-7zjov1.286). It fails loudly when phase A never
ran, and when data_store was not started again after phase A.

DATING THE OUTAGE FROM THE DATABASE. up_at is when the restarted data_store opened its oldest connection
(scenario.data_store_connected_since): nothing could be acknowledged between the stop and then. The stop itself
leaves no row; the last delivery of a phase A request before up_at stands in for it. While the store is up and the
queue is full of slow work a report lands about every delay / WORKERS (2 s), so that proxy precedes the stop by at
most one slow delay. An attempt row's started_at is the vendor's first call (the attempt's stats), and a request's
finished_at is the commit of the delivery that finished it.

WHAT IT PINS:
  * the bound (R2): the attempts begun before up_at whose delivery committed at or after it, the reports held
    across the outage, number at least one and at most BUFFER_SIZE + WORKERS. An ingest that kept dequeuing while
    its buffer was full would have begun about one attempt per 2 s of the outage, which is why the outage must be at
    least MIN_OUTAGE_S;
  * it resumes: at least one request was first attempted at or after up_at (it waited in the queue the whole outage);
  * every report delivered once, nothing lost: as in S3, every request SUCCEEDED with attempt numbers 1..k once
    each, no FAILED row, the durable count equal to the rows, one entry per symbol with exactly the generator's bars;
  * the quota counts are right: each request's spend equals its attempt rows' (calls and pages), with no attempt of
    unknown spend, however many times its report was redelivered;
  * one instance, still live, made every attempt (the outage did not restart ingest).

NOT PINNABLE HERE: the stall alarm and the stalled health state (ingest's log and its gRPC health, which test_client
cannot reach). The orchestrator reads the alarm in ingest's log (steps 4 and 6 of the step list).

BUFFER_SIZE is the value the step list gives test_env (step 0); no test can read ingest's environment.
"""

import pytest

from tests.system.phased.scenario import (
    WORKERS,
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

SCENARIO = Scenario('S5')
PHASE_A = 'tests/system/phased/phase_s5a_buffer_store_down_before.py'
BUFFER_SIZE = 2
MIN_OUTAGE_S = 30.0
# 64 slow attempts at 4 workers, the redelivery backoff (at most 30 s), and the drain of the held reports.
FINISH_S = 600.0


def test_s5b_the_buffer_bounded_the_outage_and_every_report_landed_once(pg_engine, data_store) -> None:
    run = phase_a_run(pg_engine, SCENARIO, PHASE_A)
    try:
        up_at = data_store_connected_since(pg_engine)
        assert up_at > run.seeded_at, (
            f'data_store holds a connection from {up_at}, before phase A seeded at {run.seeded_at}: '
            'it was not stopped and started between the phases'
        )

        requests = wait_until_finished(pg_engine, run, FINISH_S)
        attempts = attempt_rows(pg_engine, run.request_ids)
        assert_each_reported_once(requests, attempts)
        assert_one_entry_each(pg_engine, run, requests)

        live = live_instance(pg_engine).instance_id
        made_by = {row.instance_id for rows in attempts.values() for row in rows}
        assert made_by == {live}, f'attempts were made by {len(made_by)} instance(s), not only the live one'

        # One attempt row per request (assert_each_reported_once: one SUCCEEDED, no FAILED, numbered from 1).
        served = {request_id: rows[-1] for request_id, rows in attempts.items()}
        delivered_before = [request.finished_at for request in requests.values() if request.finished_at < up_at]
        last_before = max(delivered_before) if delivered_before else run.seeded_at
        outage_s = (up_at - last_before).total_seconds()
        assert outage_s >= MIN_OUTAGE_S, (
            f'data_store was down about {outage_s:.0f} s (last delivery {last_before}, back at {up_at}), under '
            f'{MIN_OUTAGE_S:.0f} s: too short for the bound to tell a gated ingest from one that keeps dequeuing. '
            'Rerun from phase A with a longer wait between test_stop and test_start.'
        )

        held = [
            request_id
            for request_id, attempt in served.items()
            if attempt.started_at < up_at <= requests[request_id].finished_at
        ]
        assert held, (
            'no attempt was begun before data_store came back and delivered after it: the queue drained before the '
            'stop. Rerun from phase A and stop data_store immediately after it.'
        )
        assert len(held) <= BUFFER_SIZE + WORKERS, (
            f'{len(held)} attempts were begun while data_store was down (or before its stop) and delivered after its '
            f'return, more than the buffer ({BUFFER_SIZE}) plus the workers ({WORKERS}) can hold: ingest kept taking '
            'work while its outgoing buffer was full'
        )

        resumed = [request_id for request_id, attempt in served.items() if attempt.started_at >= up_at]
        assert resumed, (
            'no request was first attempted after data_store came back: the queue held no work through the outage, '
            'so this run did not show the dequeue stop and resume. Rerun from phase A.'
        )
    finally:
        cleanup_if_finished(pg_engine, run)
