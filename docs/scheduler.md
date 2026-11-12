# The ingest scheduler module

What a broker handle in `data_ingest` does between accepting a request and the store acknowledging its result: the
queue, the ready-by check, the acknowledgement-gated executor, the retry budget, pacing and the outgoing buffer.
The code is `server/data/ingest/app/scheduler/` (`scheduler.py`, `queue.py`, `executor.py`, `outbox.py`, `retry.py`,
`limits.py`, `credentials.py`). This file is the module's README and lives here, not in the package, so it ships in no
image. The wire contract it serves is in [`API.md`](API.md) section I1; the operator's view of its failure modes is
[`runbooks/queue-ack-restart.md`](runbooks/queue-ack-restart.md).

**What is built, and what is not.** One module serves every broker handle; each handle builds its own instance with
`SchedulerBuilder`. Built: a FIFO queue, the ready-by check at dequeue, pacing and counting, the acknowledged retry
and the bounded outgoing buffer with its stall alarm. Not built: a planner or admission control, a cumulative quota cap that survives a restart, and more than one credential
(the provider seam exists; `StaticCredentialProvider` serves the one configured key).

## A handle, end to end

There is one handle per broker (`BrokerHandle`; the Alpaca one is `brokers/alpaca/handle.py`). `Submit` hands it a
request, which it holds and answers at once; nothing in `Submit` calls the vendor or waits for the store.

1. **Intake.** The handle's intake for the request's kind resolves the feed (bars only), refuses what it cannot serve
   (`FEED_NOT_AVAILABLE` for a feed it cannot serve, `UNSUPPORTED_ASSET_TYPE` for a non-stock asset list), and chooses
   the quota scope. `Submit` is idempotent while a request is held: the same `request_id` is answered again and queued
   once.
2. **Queue.** The request goes to the handle's `FifoQueue`.
3. **Worker.** A worker waits for a free buffer slot, takes the next request, runs ONE attempt and puts the attempt's
   report in the outgoing buffer.
4. **Delivery.** The report is delivered to the store (`StoreService.DeliverAttempt`). The store's answer is the
   acknowledgement. Only the acknowledgement can schedule a retry.

## The queue

Two arrival-ordered tiers: requests that carry a `ready_by` first, in arrival order, then requests with no `ready_by`
in arrival order. The queue is unbounded and admits everything; there is no priority
beyond that order. A retry re-enters at the tail of its tier through the same `submit`.

## The ready-by check at dequeue

Expiry is checked only when a request is taken off the queue (`Scheduler.take`); a request waiting in the queue is not
swept. A request whose `ready_by` is earlier than now when it is taken is **not run**: it goes to the expired sink and
`take` moves on. A request with no `ready_by` never expires. A retry is checked again at its next take, and a request
already being attempted is not cut short.

The sink is `AckGatedExecutor.report_expired`: the request goes out on the same outgoing buffer as an `EXPIRED`
report. No vendor call was made, so it carries no stats, no payload and no served range, and an empty quota key. Its
header still follows the request kind's feed rule: a bars request that expired **names the resolved tape**
(`ScheduledRequest.feed`); an asset list or corporate-actions request carries `FEED_UNSPECIFIED`. The
store writes the request `FAILED` with cause expired and no attempt row, and answers `FINAL`.

**`EXPIRED` versus `EXHAUSTED`.** Two reports share a shape and mean different things:

| Report | Trigger | Where it is made |
|---|---|---|
| `EXPIRED` | The request is taken off the queue after its `ready_by` has passed. | `Scheduler.take`, then `report_expired` |
| `EXHAUSTED` | The request arrives at `Submit` with its attempt count already at or past the retry budget (`attempt_count` at or past `max_attempts`), so it is never queued. | `BrokerHandle.submit`, then `report_exhausted` |

Neither is an attempt, neither spends quota, and neither is a vendor failure. `FAILED` means a vendor call was made.

## The executor: acknowledgement-gated

Per handle, `AckGatedExecutor` runs `workers` workers (default 4). Its rule is **no retry of a request runs until its
report is acknowledged**:

- Every report, success or failure, is delivered by its own task. A delivery that fails (the store is down, the
  answer is lost) is **redelivered with the same attempt**: the same `AttemptDelivery`, so the same
  `(request_id, attempt_no)`. That is not a broker retry and makes no vendor call. The wait starts at 1 s, doubles and
  caps at 30 s. The store is idempotent on the pair and answers a duplicate from its stored row.
- The acknowledgement carries a verdict. `FINAL` drops the request. `CONTINUE` on a `FAILED` report schedules the next
  attempt: after the backoff for a `TRANSIENT` failure, after the vendor's reset for a `RATE_LIMITED` one, never for a
  `PERMANENT` one. A permanent refusal, and an attempt that spent the budget, are never retried whatever the store
  answers. The retry re-enters through `Scheduler.submit` with the attempt count the store acknowledged, never one ingest
  kept, so a request recovered after a restart resumes from the store's count.
- A request that finishes (no retry follows) is released from the handle.

### Retry and EXHAUSTED

Ingest holds the budget (`RetryPolicy`, `retry.py`), the store holds the count. `ALPACA_MAX_ATTEMPTS` (default 5)
attempts per request, the first included. The wait after a failed attempt is `1 s * 2 ** (attempt - 1)`, capped at 60 s;
a rate-limited attempt that names a reset waits until it, never longer than 3600 s. The attempt numbered `max_attempts`
is the last: ingest sets `last_attempt` on its `FAILED` report, and the store closes the request instead of leaving it
queued. When the attempt count in a request that reaches ingest is already at or past the budget (a restart across a
changed budget), the request is not attempted and is reported `EXHAUSTED`, as above.

A raise inside the attempt runner is a bug. The executor reports it as a `FAILED` `PERMANENT` attempt with no typed
reason (so the run history shows an empty `reason`) and logs the exception; a worker never dies silently.

## Pacing and counting

Every Alpaca market-data HTTP request, a bars page, a retry inside an attempt or a corporate-actions page, takes one
scheduler token first (`Scheduler.acquire`, called at each request in `brokers/alpaca/broker_api.py`), and every
request that reaches the vendor is counted on the attempt: `pages`, `retries`, `throttled_429` and `calls` (a 429
included) are the attempt's `vendor_stats` (`CallTally`). A call collapsed onto another caller's in
flight, or refused before the vendor, spends and counts nothing.

- **Scopes.** The Alpaca handle declares two, each with its own ceiling of 200 calls a minute (Alpaca's documented
  Basic-plan limit): `ALPACA_DATA` for the market-data host (bars and corporate actions) and `ALPACA_TRADING` for the
  trading host (the asset reads), so an asset read never spends from the data scope and a bars attempt's tally never
  counts one.
- **Configuration may only lower.** `ALPACA_RATE_LIMIT_PER_MINUTE` is read when the handle is built; a value above
  the ceiling is a `SchedulerConfigError`.
- **The bucket.** A limit of N calls in an interval T is a token bucket sized `max(1, min(N per second, N / 10))` that
  refills at `(N - size + 1) / T`, so no half-open window of length T holds more than N spent tokens. A request that
  must wait is held until a token exists. With a `ready_by`, one whose wait would pass it fails fast with `RATE_BUDGET`
  and spends nothing.
- **Priority.** A bars request's priority is its update type's (`priority_for_update_type`): `STREAM` is live,
  `STATIC` is backfill, anything else, a DAILY refresh included, is interactive. Backfill takes only what live and
  interactive calls leave, since half the bucket is held back from it by default.

## The outgoing buffer

The buffer holds the reports the store has not yet acknowledged. It is bounded: `ALPACA_OUTGOING_BUFFER_SIZE`
(default 64) unacknowledged reports.

- **At a full buffer the handle stops dequeuing.** A worker waits for a free slot before it takes anything off the
  queue (`OutgoingBuffer.wait_for_space`), and a report waiting to enter holds its worker until a slot frees. Nothing
  is dropped.
- **The bound caps what an ingest crash can lose.** The stated bound, from `executor.py`: a crash loses at most the
  in-flight attempts, the reports in the buffer plus the attempt each worker is making, which is
  `buffer_size + workers` attempts (64 + 4 by default). Requests still in the queue, and retries waiting out a backoff,
  never started or spent nothing at the vendor since their last acknowledged report; the restart charges each its
  registered cost estimate and the store re-requests them (below).

### The stall alarm

The executor looks at the oldest unacknowledged report every `stall_check_interval_s` (5 s). When it has waited longer
than `ALPACA_STALL_TIMEOUT_S` (default 300 s) the handle is **stalled**: one ERROR line, and the state is reported on a
named gRPC health service for the handle, `trader_joe.ingest.delivery.ALPACA_DATA` (the fixed prefix plus the quota
scope, never an account or a credential): `SERVING` while not stalled, `NOT_SERVING` while stalled.
The ERROR line repeats once per timeout while the stall lasts, and its text is (`executor.py`):

```
Store delivery stalled on handle <handle>: the oldest unacknowledged attempt, request_id=<id> attempt_no=<n>, has waited <age>s (timeout <timeout>s, <k> redeliveries, <held> of <capacity> buffer slots held)
```

When the report is acknowledged the alarm clears with one INFO line:

```
Store delivery on handle <handle> is no longer stalled
```

Each failed delivery logs a WARNING (`Handle <handle>: delivery of request <id> attempt <n> failed (<ErrorType>);
redelivering the same attempt in <s>s (redelivery <k>)`).

## The restart

Ingest keeps nothing durable; the store holds the request record and the attempt count. A restarted ingest mints a new
instance id and announces it (`RegisterInstance`); the store marks the old instance superseded and its in-flight
requests not delivered, and its instance watcher (`app/ingest/instance_watch.py`) re-submits them to the new instance
flagged `recovered`, with the durable attempt count.

For a `recovered` request the handle **charges the request's registered cost estimate to its quota scope** before it
queues the request (`Scheduler.charge_restart`, the cost function's only caller; `estimate_request_calls`: one call for
an asset list or a corporate-actions read, otherwise a high count of pages for the range), so a fresh instance starts
its window with the right count. It logs once:

```
Recovered request <id> (<kind>) charged its estimate of <n> calls to quota scope <scope> after an ingest restart
```

The lost attempt is not counted, is not a broker failure and is not shown as failed (`NOT_DELIVERED`). Calls the old
process made before a restart and that are not re-requested are not modelled.
