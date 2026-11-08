# Runbook: the queue, the acknowledgement and the restarts

How a request moves from `data_store` through `data_ingest`'s queue and back, what each log and table shows while it
does, and what the system does when either service restarts or the store goes down. This is what the P26 system
session observed on the fake stack (bead `tj-7zjov1.38`, closed at `59ea5a2`), restated against the code. The module
itself is described in [`../scheduler.md`](../scheduler.md); the wire contract is [`../API.md`](../API.md) section I1.

Log lines are quoted from the format strings in the code. Ids, counts and ages in your logs will differ. A service's
container log is dominated by `GET /ping` health lines, so filter them out before you read for anything else.

## The path, and where to look

```
POST /store -> ingest_request (PENDING, written first) -> Submit -> QUEUED under an instance
   -> ingest queue -> worker -> attempt -> outgoing buffer -> DeliverAttempt -> store writes, commits, acknowledges
```

| To see | Look at |
|---|---|
| Where a request stands | `ingest_request.state`: `PENDING` (written, not answered by ingest), `QUEUED` (held by an ingest instance), `NOT_DELIVERED` (its instance was replaced before its result arrived), then terminal `SUCCEEDED`, `EMPTY` or `FAILED` |
| Why a request failed | `ingest_request.failure_cause`: `EXPIRED`, `PERMANENT`, `ATTEMPTS_EXHAUSTED`, `VENDOR_ASSET_CHANGED`; an `EMPTY` request with `DISCARDED` is a delivery for a dataset deleted or stopped meanwhile |
| What each attempt did | `ingest_run`: one row per reported attempt (`attempt_no`, `instance_id`, `quota_key`, `verdict`, `state`, the served range and the `vendor_*` tally), shown to users by `GET /ui/v1/datasets/{id}/runs` |
| The durable count | `ingest_request.attempts`: the store's count, which seeds the next attempt number. A lost attempt is not in it |
| Which ingest process is live | `ingest_instance` (the instance marker) and the `Instance` call |

An attempt that was never delivered, an `EXPIRED` report and an `EXHAUSTED` report all leave **no** `ingest_run` row.

## S1. A request on a running stack leaves its trace

The baseline check that the path works end to end (`tests/system/test_http_ingest_runs.py`).

- **Expect:** a served POST leaves one attempt row, naming its dataset, carrying the served range (`served_start`,
  `served_end`) and the id of the live ingest instance, and the request ends `SUCCEEDED` or `EMPTY`.
- **If it does not:** read the request row first. A request still `PENDING` means `Submit` never got an answer: the
  instance watcher re-sends it (below). A request `QUEUED` for long means ingest holds it: read the ingest log and the
  stall alarm (S5).
- **Do not** delete a request row to "clear" it while ingest may still hold it. Ingest's report for a request that no
  longer exists is refused and redelivered for ever.

## S2. Ingest restarts

What happens: the new instance mints an id and announces it; the store supersedes the old instance and marks its open
requests not delivered; the store's instance watcher re-submits them, flagged recovered, to the new instance; the new
instance charges each its quota estimate before queuing it.

1. **The new instance announces itself** (ingest, INFO):
   `Instance <instance id> announced to the store`
2. **The store marks the old one superseded** (store, INFO):
   `Ingest instance registered: <n> older instance(s) superseded, <m> request(s) marked not delivered`
   Those requests are `NOT_DELIVERED`. That state is **never a failure and never an attempt**: the lost attempt is not
   counted, writes no row and is not shown as failed.
3. **The watcher waits, then re-submits.** It re-submits a request only once its old instance has been superseded for
   the delivery deadline (60 s) plus `INSTANCE_WATCH_RESUBMIT_MARGIN_SECONDS` (15 s), so the old instance's last
   in-flight report has committed and is in the count; the watcher itself runs every
   `INSTANCE_WATCH_INTERVAL_SECONDS` (15 s). Expect to wait about 90 s. When it acts (store, INFO):
   `Instance watch: <a> pending re-sent, <b> recovered re-submitted, <c> failed, <d> skipped`
4. **Each recovered request is charged its estimate** to the quota scope (ingest, INFO, once per request):
   `Recovered request <request id> (<kind>) charged its estimate of <n> calls to quota scope <scope> after an ingest restart`
   The number of these lines equals `<b>` in the watcher's line. A fresh instance therefore starts its rate window with
   the right count rather than a full one.
5. **The request finishes under the new instance**, numbering its next attempt from the store's durable count plus one.

- **Check:** the requests that were open at the restart go `NOT_DELIVERED`, then `QUEUED` under the new instance's id,
  then terminal; `ingest_request.attempts` is what it was before the restart plus attempts actually delivered.
- **Do not** re-submit by hand inside the watcher's wait: a re-submission that beats the old
  instance's last report reuses an attempt number, and the new instance's report would be refused for ever. A warning
  that `Instance watch could not re-submit a request: <ErrorType> <reason>` leaves the request to the next pass; it
  does not need a hand.
- **Do not** read `NOT_DELIVERED` rows as errors, and do not count them against a failure budget.

## S3. The store restarts

Ingest keeps its queue and its outgoing buffer, so nothing is lost.

- A report ingest could not deliver while the store was down is **redelivered with the same attempt number**, after a
  wait that starts at 1 s and doubles to 30 s (ingest, WARNING, once per failed delivery):
  `Handle <handle>: delivery of request <request id> attempt <n> failed (<ErrorType>); redelivering the same attempt in <s>s (redelivery <k>)`
  A redelivery is not a broker retry and makes no vendor call.
  The delivery call has a 60 s deadline and waits for the store to be ready (`wait_for_ready`,
  `server/common/rpc/clients/store_delivery.py`), so a store outage shorter than about a minute normally logs **no**
  redelivery WARNING: the call simply waits and succeeds. A store crash while a delivery is in flight can still log one,
  because that call fails instead of waiting. Do not read the absence of the line as a failure to redeliver.
- The store is idempotent on `(request_id, attempt_no)`: a duplicate is answered from the stored row and writes nothing,
  so **nothing is duplicated**.
- **Check:** every request that was in flight reaches a terminal state, with one `ingest_run` row per `attempt_no`.
- **Do not** restart ingest to speed this up. While the store is down the buffer holds the only copy of unacknowledged
  reports.

## S5. The store is down while ingest runs

The bounded-buffer case (`tests/system/phased/phase_s5a_buffer_store_down_before.py` and `..._after.py`).

1. Reports pile up unacknowledged in the outgoing buffer. Past `ALPACA_STALL_TIMEOUT_S` (300 s) the handle raises its
   alarm and repeats it about once per timeout (ingest, ERROR):
   `Store delivery stalled on handle <handle>: the oldest unacknowledged attempt, request_id=<id> attempt_no=<n>, has waited <age>s (timeout <timeout>s, <k> redeliveries, <held> of <capacity> buffer slots held)`
   and its gRPC health service `trader_joe.ingest.delivery.ALPACA_DATA` reads `NOT_SERVING`.
2. When the buffer holds `ALPACA_OUTGOING_BUFFER_SIZE` reports (64) the handle **stops dequeuing**: new requests wait in
   the queue and no new vendor call is made. Nothing is dropped.
3. The same attempt is redelivered over and over (S3's WARNING).
4. When the store returns the oldest report is acknowledged, the alarm clears (ingest, INFO), the health service reads
   `SERVING`, and dequeuing resumes:
   `Store delivery on handle <handle> is no longer stalled`

- **Check:** during the outage, `held` stays at or under the capacity and never grows past it; after it, every request
  reaches a terminal state and no attempt appears twice.
- **Do not** restart ingest during an outage. A crash loses at most `buffer size + workers` in-flight attempts (64 + 4
  by default; [`../scheduler.md`](../scheduler.md)), and the store then has to re-request them. Bring the store back
  instead. Do not raise the buffer to "give it room": a larger buffer is a larger loss at a crash.

## S4. Queue expiry, end to end

Requests are checked against their `ready_by` only when taken off the queue (`tests/system/test_queue_expiry_e2e.py`).

- **Expired at dequeue.** A request whose `ready_by` has passed when a worker takes it is **never sent to the vendor**:
  the request ends `FAILED` with `failure_cause` `EXPIRED`, with no `ingest_run` row, attempts and spend unchanged, and no
  dataset. A synchronous POST whose `ready_by` passes while queued is answered with a failure. A request with no
  `ready_by` never expires and is taken after every dated one.
- **A retry expires on re-entry.** A retry re-enters the queue and is checked again, so a request that fails once and
  then waits past its `ready_by` ends `EXPIRED`, not `ATTEMPTS_EXHAUSTED`.
- **Exhausted.** A request that reaches ingest with its durable attempt count already at the budget (`ALPACA_MAX_ATTEMPTS`,
  default 5) is not queued and makes no vendor call; it ends `FAILED` with `failure_cause` `ATTEMPTS_EXHAUSTED`, with no
  attempt row and the count unchanged.
- **The tape is on the report.** A bars request that expired or was exhausted still names the resolved tape in its
  report header; the store accepts the report, which is how you can tell it landed. A bars report with no feed would be
  refused and redelivered for ever.
- **Do not** treat an `EXPIRED` or `ATTEMPTS_EXHAUSTED` request as a vendor fault; no vendor was called. Resubmitting is a
  new request with a new `ready_by`.

## S6 and S7. The panel manager

The kill switch and the dry run (S6-A and S6-B) and the smoke test that a running store's own loop writes a finished
pass for an enabled panel (S7, `tests/system/test_panel_loop_smoke.py`) are in
[`panel-manager.md`](panel-manager.md).

## The scenario names

The S-numbers are the P26 plan's (bead `tj-7zjov1.38`). Each maps to a test, and the restart cases run in two phases
with the stack disturbed between them.

| Scenario | Test |
|---|---|
| S1 running bind: served range and live instance on the attempt row | `tests/system/test_http_ingest_runs.py` |
| S2 ingest restart | `tests/system/phased/phase_s2a_ingest_restart_before.py`, `phase_s2b_ingest_restart_after.py` |
| S3 store restart | `tests/system/phased/phase_s3a_store_restart_before.py`, `phase_s3b_store_restart_after.py` |
| S4 queue expiry and exhausted | `tests/system/test_queue_expiry_e2e.py` |
| S5 buffer, store down | `tests/system/phased/phase_s5a_buffer_store_down_before.py`, `phase_s5b_buffer_store_down_after.py` |
| S6 kill switch and dry run | `tests/system/phased/phase_s6a_panel_kill_switch.py`, `phase_s6b_panel_dry_run.py` |
| S7 wiring smoke: the running panel loop writes a finished pass | `tests/system/test_panel_loop_smoke.py` |
