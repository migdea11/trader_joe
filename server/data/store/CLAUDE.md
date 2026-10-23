# Market data store

Owner of all persisted market data. Requests data from data-ingest over gRPC (`FetchDataset`), writes it to Postgres, and serves it over `/store/...`, `/internal/asset-data/...` and, to the web UI, the read-only `/ui/v1/...` routes as protobuf canonical JSON. Tables: `store_dataset_entry`, `stock_market_activity`, `ingest_run`, `dataset_read`, `dataset_read_daily`, `dataset_override_log`, `dataset_deletion`.

## Architecture reference

`bd list -t arch_index --all` — child of `trader_joe system` (tj-mtwvnu). Design diagram: `docs/img/design.jpg`.

## Tech stack

Python 3.12, FastAPI, SQLAlchemy 2.0 async, asyncpg, Alembic, Postgres, `grpc.aio` client of data-ingest

## Key invariants

Sole owner of persisted market data. Every write goes through a repository; no raw SQL in routers. Migrations are additive.

**data_store runs ONE process: one uvicorn worker.** Three things are correct only because of it:

- the startup sweep of `RUNNING` ingest runs (`sweep_interrupted_runs` in `app/ingest/run_recording.py`, `fail_interrupted_runs` in `app/database/crud/ingest_run.py`) marks every `RUNNING` row `FAILED` / `INTERRUPTED`; a second worker starting would fail the run the first is executing;
- the events hub (`app/events.py`) lives in process memory, so an event reaches only the clients connected to the worker that published it;
- the DAILY refresh loop (below) and the scheduler's store side (decision `tj-pd41mt`, proposed, not built) assume the same.

`docker-compose.yaml` pins `SERVICE_WORKERS=1` as a literal in `environment:`, which outranks the operator's env file. Running more than one needs two changes first: Postgres `LISTEN`/`NOTIFY` so each worker republishes into its own hub, and a sweep keyed by process rather than by "every `RUNNING` row".

## Data freshness and completeness

Freshness and completeness are **computed on read and never stored**, by `app/freshness.py` (pure: the
clock and the calendar are arguments) and composed with the queries in `app/dataset_catalog.py`. They
serve the `/ui/v1` routes. There is still no coverage ledger: no `missing_ranges` query, no `range_agg`
subtraction, and the older read routes (`/store/...`, `/internal/asset-data/...`) say nothing about
completeness, so a short answer from them is not distinguishable from a complete one.

| Concern | Where it lives |
|---|---|
| Status per dataset (`FRESH`, `LATE`, `OVERDUE`, `COMPLETE`, `GAPS`, `RETIRED`) | `evaluate_dataset` in `app/freshness.py` |
| Trading calendar | one per data source (`SOURCE_CALENDARS`, offline `exchange_calendars`); a source with none has no computed freshness |
| Status vocabulary the UI filters by | `StatusGroup` in `app/dataset_catalog.py`: healthy, late, failed, retired |
| Catalog listing, facets, bar paging | `app/dataset_catalog.py` over `app/database/crud/stock/dataset_catalog.py`; keyset cursors, opaque, bound to their sort |
| Bar page index (`GET /ui/v1/datasets/{id}/bar-pages`, the Viewer's paging) | `bar_page_index` in `app/dataset_catalog.py`, `read_bar_page_starts` in `app/database/crud/stock/dataset_catalog.py` |
| Coverage strip (`GET /ui/v1/datasets/{id}/coverage`): buckets and gap windows | `coverage_buckets` and `local_midnight` in `app/freshness.py`, composed by `dataset_coverage` in `app/dataset_catalog.py` |

Things that bite:

- **Completeness is per session**: a session counts as covered if it has at least one bar. That is right
  for 1-minute to 1-day granularities; a weekly or monthly dataset would read as full of gaps.
- **A bar belongs to the session whose local date, in the calendar's time zone, its timestamp falls on.**
- **Ranges are half-open `[start, end)`** in the store, the bar reads and the overlap check: a bar at
  `end` is in the next range, and ranges that only touch do not collide. A declared `end` not after
  `start` is refused at the edge.
- **Filtering on status needs freshness for every matching dataset**, not one page, so that a facet count
  equals the filtered list's total. A request with no status filter evaluates the page only. The
  request's statement count is constant, never one per dataset.
- **Two gap counts, in different units.** `CoverageStrip.gap_count` counts gap *intervals* (maximal runs of missing completed sessions); `Freshness.gap_count` counts missing *sessions*. For a `STATIC` dataset the sessions of all gaps add up to the freshness count. The strip lists at most 500 gaps (`MAX_COVERAGE_GAPS`), so `gap_count` may exceed the list.
- **A gap window is whole sessions**: from the local midnight of its first session to the local midnight after its last, so it can start before or end after the dataset's range by the part of a session. Buckets and gaps both count only the sessions inside the range: a 1week or 1month period straddling the range end still decides covered or not, but only its in-range sessions are reported missing, so `Freshness.gap_count` equals the buckets' expected sessions without a bar. A coverage read costs two statements and records no usage. With no trading calendar covering the dataset it is a 422, never an empty strip, which would read as no gaps.
- **The page index is fixed blocks counted back from the newest bar**, so the earliest page, not the newest, takes the remainder and overlaps its neighbour. Offsets are `[0 if r > 0], r, r + size, ..., N - size` with r = N mod size; `overlap` is the client's look-ahead and never moves one. It is two statements (the entry, then one read that scans the window twice: a count and the ranks), about 0.7 s at 2.4M bars, and it is a snapshot of the window it was read for.
- **Two maximums, kept apart.** `MAX_BAR_LIMIT` (the UI bars read) is 50,000 and `MAX_PAGE_INDEX_SIZE` (the page index `size`) is 1,000. Raising the first does not raise the second.
- **Expiry is optional.** None means the dataset never expires; setting one on a `DAILY` or `STREAM`
  dataset retires it, and a repeat request sets expiry from its body, so omitting it clears a stored one.

## Collection record and live events

Every fetch the store drives (`POST /store`) is recorded as one `ingest_run` row, by the run recorder
(`app/ingest/run_recording.py`). The row is what the UI's run history, `last_run` and Running state read.
It is separate from the entry and its bars, and is not part of the fetch's transaction.

| Concern | Where it lives |
|---|---|
| The table: one row per store-driven fetch; no foreign key to the dataset, so runs outlive a deletion | `app/database/models/ingest_run.py`, migration `6f5a798746a1` |
| Reads and writes of the table (start, acknowledge, finish, sweep, runs list, latest run per dataset, running ids) | `app/database/crud/ingest_run.py` |
| The recorder: a `RUNNING` row before the stream opens, the dataset id at ingest's acknowledgement, a terminal row at the end | `recorded_run` in `app/ingest/run_recording.py` |
| The events hub: bounded queue per client, drop on overflow | `app/events.py` |
| The socket `WS /ui/v1/events`, one `UiEvent` text frame per event | `routers/data_store/ui_events.py`, mapped in `routers/data_store/ui_mapping.py` |
| Runs list, coverage strip, Running filter and facet | `run_page`, `dataset_coverage`, `list_page` in `app/dataset_catalog.py` |

- **A run's states** are `RUNNING`, `SUCCEEDED` (bars written), `EMPTY` (the vendor answered and held none; not a failure) and `FAILED`, which carries the typed `(domain, reason)`, the detail and the error id. The startup sweep writes `FAILED` with reason `INTERRUPTED`, a run-record marker that is not a member of the error vocabulary and never reaches problem+json.
- **Each write is its own transaction on its own session** (`own_session`), so the `RUNNING` row is visible while the fetch is in flight and a rolled-back fetch still leaves its `FAILED` row.
- **The record is best effort.** A failure to write it is logged and the fetch carries on unrecorded; the cost is a missing or stuck row, which the startup sweep bounds. The exception of the fetch itself propagates unchanged.
- **`dataset_id` is NULL until ingest acknowledges the fetch**: a new dataset's entry is written on the acknowledgement. So a dataset is `Running` only from the acknowledgement, and a first-time request is not visible as Running before it. A run that fails before its acknowledgement keeps a NULL dataset id.
- **The actor** is the request body's `owner`. It is a `SensitiveString` and is never logged.
- **Events are notifications, not data.** Publishers call `publish()` strictly after their own commit. Published today: `IngestRunChanged` (start, acknowledgement, finish) and `DatasetChanged` `UPDATED` (after `POST /store`) and `DELETED`. `CREATED` is never published: a new dataset arrives as `UPDATED`, because the upsert does not tell the two apart. `ReaperPendingChanged` is published by the expiry reaper (below). `RequestChanged` is typed in `app/events.py` and in `ui/v1/events.proto` and has no publisher.
- **An event can name a dataset whose entry is not committed yet**: the acknowledgement's `IngestRunChanged` is published before the fetch commits, so a reload by id may 404 briefly. REST is the truth.
- **A slow client is dropped, never waited for.** Its queue is bounded (`EVENTS_CLIENT_QUEUE_SIZE`); when it fills the hub drops the client and the router closes the socket with 1013. The client reloads over REST on reconnect.

## Usage record (consumption)

Every bar read is recorded as one `dataset_read` row by the usage recorder (`app/usage_recorder.py`): buffered in process, inserted in batches, so the read path issues no INSERT. Rows past the rollup window are folded into `dataset_read_daily` and deleted. Distinct from `ingest_run`, which is collection.

| Concern | Where it lives |
|---|---|
| Tables (no foreign key: usage outlives a dataset), migration `b3d9e1a47c62` | `app/database/models/dataset_read.py`, `dataset_read_daily.py` |
| Writes, the rollup (one data-modifying CTE) and the usage query helper (`read_dataset_usage`, `read_daily_reads`) | `app/database/crud/dataset_read.py` |
| Buffer, flush loop, rollup loop, stream-session open/close API | `app/usage_recorder.py`, started from the lifespan |
| The reader's label, its kind and `OPERATOR_PRINCIPAL` | `app/client_label.py` |

- **The label is the dataset `owner` string the caller already uses, sent in the `X-Client-Id` header** (tj-grna9p.131). It is self-asserted: a label, never an authority. The `owner` QUERY filter on `/store` GETs is a search filter and is never the label. Missing, empty, over 128 characters, a control character or a leading `_` is `_unattributed`; never truncated.
- **Kind is UI iff the label equals `OPERATOR_PRINCIPAL` (`operator`), else SDK.**
- **At most 32 distinct ids per dataset per UTC day**; later ones are `_overflow`. Applied at flush against the stored ids. The operator and the reserved values are exempt.
- **One read per call** on `GET /internal/asset-data`, per dataset it returned bars of (a query by dataset id that returns none records 0 bars). Listing, metadata, bar-page and coverage reads record nothing. There is no bar GET under `/store`: those routes return dataset entries.
- **Best effort.** A failed flush is logged by type and retried from the buffer; the buffer is bounded and drops its oldest past `USAGE_BUFFER_MAX`. On an unmigrated database one warning names `make migrate` and the rows are dropped.
- **`client_id` is a `SensitiveString`**: never logged, never in a repr.
- **`dataset_read_daily.last_read_at`** keeps the latest read of a folded day, so staleness stays exact after the rows are gone.
- **`GET /ui/v1/datasets/{id}/bars` records one read per call WITHOUT a cursor** (the `X-Client-Id` label; `operator` is kind UI, no header is `_unattributed`); a call with a cursor, and every other `/ui/v1` route, records nothing.
- **Usage on the catalog** (`app/dataset_catalog.py`): `DatasetSummary.usage` on list and detail, the `client_id` (wins, repeatable) / `client_kind` (default SDK) filter, `usage` = `READ_30D` / `STALE` / `NEVER_READ` with `stale_days` (1..3650, default 90, never-read is stale; day-aligned on the UTC date of the last read, stream sessions count, so READ_30D and STALE are complements at 30; the detail route takes the same client filter), `sort=last_read`, and `expires_before` (Expiring Soon: expiry before it, plus the reaper's pending set; ignores the client filter; the facets default it to now + `EXPIRING_SOON_DAYS`). The `owner` query filter is never the reader. Each facet count equals the filtered list's total. The window functions refuse a naive `now`. An unmigrated database reads as zero usage with one warning (`make migrate`).

## Usage statistics (`/ui/v1/stats/...`)

Five open GETs in `routers/data_store/ui_stats.py`, composed in `app/dataset_catalog.py` (`usage_summary`, `last_read_buckets`, `reads_per_day`, `stale_page`, `storage_current`). None records usage; each runs a constant number of statements.

| Route | What it serves |
|---|---|
| `usage/summary?stale_days&source` | `UsageSummary`: stale, read_last_30d, never_read, total, reads_today, distinct_clients_today |
| `usage/last-read-buckets?source` | `LastReadBuckets`: [0,1) [1,8) [8,31) [31,91) [91,open) whole UTC days, plus never_read; sum = total |
| `usage/reads-per-day?days&dataset_id&source` | `ReadsPerDay`: one entry per UTC day, client id and kind, continuous over the rollup |
| `usage/stale?stale_days&source&cursor&limit` | `StaleDatasetPage`: never-read first, then oldest last read, then id; est_bytes and last read per client |
| `storage/current?source` | `StorageCurrent`: the bar table's size, split per (source, feed) as estimates |

- **Every usage route takes the list's client filter** (`UiClientFilterQuery`, `_client_filter`) and echoes it. Storage takes none.
- **`read_within` in `app/dataset_catalog.py` is the one day-aligned staleness rule**: the list's usage filter, the facets, the summary and the stale list all decide through it, so their stale counts agree for one filter.
- **Estimates**: a share is `bar_count * total_bytes // total_bars` (`estimate_bytes`); with a `source` the storage total is the sum of that source's shares, so the split always sums to it. Quota burn is not served (tj-lenn8u).
- **The stale cursor** is bound to stale_days, source and the client filter by a digest, so it carries no client id.

## Operator writes (expiry and delete)

`PATCH /ui/v1/datasets/{id}/expiry` (200, the updated `DatasetSummary`) and `DELETE /ui/v1/datasets/{id}` (204) are the UI's writes, in `routers/data_store/ui_dataset_writes.py`. Both need the instance secret; bodies are `SetExpiryRequest` / `DeleteDatasetRequest` parsed by `ProtoBody`, with no pydantic model.

| Concern | Where it lives |
|---|---|
| The operator-path writes (`operator_set_expiry`, `operator_delete_entry`), beside the owner-scoped ones, which are unchanged | `app/database/crud/stock/store_dataset_entry.py` |
| The override decision, `OverrideRequired`, the INSERTs of the two logs | `app/database/crud/dataset_override.py` |
| Tables (no foreign key), migration `d7e2a9c35b18` | `app/database/models/dataset_override_log.py`, `dataset_deletion.py` |

- **Rule order**: 422 bad body, 404 unknown id, 409 `OVERRIDE_REQUIRED` (owner is not `OPERATOR_PRINCIPAL` and the override is missing, unconfirmed or blank; no owner in the body), then the write. An override sent for the operator's own dataset is ignored.
- **One transaction per write**: the write, its `dataset_deletion` row (every delete) and its `dataset_override_log` row (only an override, reason stored trimmed). The `DatasetChanged` event (`UPDATED` after a PATCH, `DELETED` after a DELETE) is published after the commit.
- **Both tables are append-only**: never an UPDATE or DELETE. `dataset_deletion.cause` is a string so the reaper (tj-grna9p.23) reuses the table.
- **Setting an expiry on a `DAILY`/`STREAM` dataset retires it, clearing it resumes it**; the expiry type is never writable. The expiry is stored as sent: a past time retires it as the freshness read already judges it.
- **An unmigrated database** refuses the audited writes: an overriding PATCH and every DELETE answer 503 `DATABASE_UNAVAILABLE` (detail names `make migrate`; no owner, no metadata) after the 404/409 checks and before any write; an operator-owned PATCH still goes through. One process-wide warning names `make migrate` (`audit_tables_exist`, `AuditUnavailable`).
- **A DELETE may carry no body** (the operator's own dataset needs no override); a body that is sent is parsed strictly.

## Expiry reaper (dry-run by default)

A periodic pass (`app/reaper.py`, the shared `run_periodic` with `Every`, started and stopped in the lifespan beside the usage loops) over the datasets whose expiry has passed. It stores no jobs: catch-up after a restart is the next pass, which reads the database each time.

| Concern | Where it lives |
|---|---|
| The pending-set query (`expiry <= now`), written once: `list_pending` (keyset over expiry, id) and `pending_ids` (all, or those among a page's ids: what `DatasetSummary.reaper_pending` reads) | `app/database/crud/stock/dataset_catalog.py` |
| The pass, the env flags, the events, the loop | `app/reaper.py` |
| The delete (`reaper_delete_entry`, its own transaction, the cascade and record of the operator delete) | `app/database/crud/stock/store_dataset_entry.py` |
| `GET /ui/v1/reaper/pending` (open; `ReaperPendingPage`) | `routers/data_store/ui_reaper.py`, `reaper_pending_page` in `app/dataset_catalog.py` |

- **DRY-RUN IS THE DEFAULT.** `REAPER_DELETES_ENABLED` must be exactly `true` to delete anything. The default exists because STATIC rows created under the former now+1-day expiry default are all past expiry: the operator reads `GET /ui/v1/reaper/pending` before anything goes. Dry-run logs a count only, never an owner or a symbol.
- **A retired subscription is pending only once its expiry has passed**, like any other dataset.
- **Enabled**: each pending dataset is deleted in its own transaction on its own session, recorded in `dataset_deletion` with cause `REAPER` and actor `reaper`, with NO override-log row (a system action). `DatasetChanged` `DELETED` is published after each commit. A failure on one dataset is logged by exception type only and the pass carries on. The row is read `FOR UPDATE` and its expiry and running-ingest state re-checked under that lock, so a PATCH that extends the expiry either commits first (the dataset is left alone) or waits and gets its own 404. A fetch not yet acknowledged has no dataset id (tj-jc635p) and is not seen: if its dataset is deleted the fetch recreates the entry.
- **A missing audit table means an enabled pass deletes nothing** (one warning, `audit_tables_exist`): a deletion is never unrecorded. Dry-run is unaffected.
- **A dataset with a `RUNNING` ingest is skipped** and retried on the next pass.
- **`ReaperPendingChanged`** is published when the pending ids differ from the previous pass's, after enabled-mode deletions, and once on the first pass after start, in dry-run too. Whether deletions are on is `ReaperPendingPage.deletes_enabled`. The previous ids live in process memory (one worker).

## DAILY refresh (interim scheduler)

An INTERIM store loop (`app/daily_refresh.py`, tj-grna9p.22) that keeps DAILY datasets current until ingest's own scheduler (tj-pd41mt Addendum 1) lands; then it is removed. The store triggers, and ingest sees an ordinary `FetchDataset` request: no new message type and no request queue. One shared `run_periodic` per source in `SOURCE_CALENDARS`, on `AfterTradingEvent(calendar, CLOSE, settle)`, started after the fetch client in the lifespan and stopped before its channel closes.

| Concern | Where it lives |
|---|---|
| The pass, the env, the loops | `app/daily_refresh.py` |
| The fetch it drives: the same worker and run recorder as `POST /store`, trigger `SCHEDULE`, actor `scheduler` | `store_market_activity_worker(..., fetch_start=)` in `app/ingest/data_action_request.py`, `recorded_run(..., actor=, requested_start=)` in `app/ingest/run_recording.py` |

- **The work is read from the database on every pass**, never stored or replayed: DAILY entries that are not retired (`is_retired`: an expiry retires at once, clearing it resumes), have no `RUNNING` ingest, and read LATE or OVERDUE under the freshness rule (last bar before the last completed session). A pass also runs once at start, so datasets requested earlier are backfilled.
- **Only the missing tail is requested**: from the last bar plus one bar (the declared start when there are no bars). The entry keeps its declared range as identity, so the refresh extends the existing entry; `fetch_start` replaces only the start of the ingest request. The stored feed is named as the preference, so a tape ingest can no longer serve is a refused ack, never a second entry.
- **Bounded**: one dataset at a time, at most `DAILY_REFRESH_MAX_PER_PASS` REQUESTED per pass (skipped datasets do not count). A TRANSIENT failure is logged by exception type only, backs off (doubling, capped at 600 s), and the pass stops after 3 in a row or at once on an error that names `reset_at` (a rate limit). The rest is the next pass's. **No automatic retry** (decision tj-grna9p.55; Retry Now is manual): a dataset that failed transiently waits for the next session close or the catch-up at start; the backoff only paces the next dataset.
- **Permanent versus transient** is the error vocabulary's own outcome (`REASONS[reason].outcome`, `is_permanent`): REFUSED (a tape ingest cannot serve, an invalid request, a vendor 4xx, own overlap, a constraint violation) cannot succeed on retry; NOT_READY (peer or vendor unavailable, deadline, rate limit, rate budget, vendor auth, 5xx, database unavailable) and an error nobody typed are transient. A permanent refusal neither counts toward the stop nor backs off; it is counted in `PassResult.refused`. A stored row that no longer validates as a request is never sent (`skipped`).
- **Fair, from the database**: the order and the "tried already" state are the `ingest_run` history of trigger `SCHEDULE` (`finished_scheduled_runs`, last 7 days), with no job store. Never-attempted datasets go first (oldest newest-bar first), then least recently attempted. A dataset whose last scheduled attempt asked for the same tail and finished EMPTY or as a permanent FAILED since the last session close is not requested again until the next close; once due again it shares the pass with the rest, at most half the cap while others wait. A run that failed before the ack has no dataset id and is matched on symbol, asset type, data type, granularity and requested start; two owners of one symbol with the same tail therefore share that outcome.
- **The refresh never writes a lifecycle field.** `store_market_activity_worker(..., preserve_lifecycle=True)` locks the entry `FOR UPDATE` on the ack (`lock_active_entry`) and upserts with `preserve_lifecycle=True`, whose `ON CONFLICT` sets `updated_at` only, so an operator's expiry is never overwritten. A dataset retired or deleted while the vendor call was in flight has the answer DROPPED (nothing stored, counted in `PassResult.discarded`, the run recorded EMPTY with no bars). POST /store is unchanged: a repeat sets expiry from its body.
- **An unmigrated database** logs one warning naming `make migrate` per process and the pass does nothing.
- **Nothing counts vendor quota here** (ingest's counting, tj-grna9p.24, is not built). What bounds the spend is the cap, the serialisation, ingest's per-vendor rate budget (`<SOURCE>_RATE_LIMIT_PER_SEC`; unset means unthrottled) and the missing-tail-only request. Ingest treats a DAILY request as INTERACTIVE priority, the same as a UI request.
- **A fetch not yet acknowledged has no dataset id** (tj-jc635p), so the running-ingest check cannot see a request that is in flight; its bars write idempotently, so the cost is a duplicate vendor call, never duplicate bars.
- **`DAILY_REFRESH_ENABLED=false` is the kill switch**, read at start: a change needs a restart.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| DATABASE_URI | primary DSN | fails closed |
| FRESHNESS_STREAM_BAR_MULTIPLE | STREAM freshness: a last bar within this many bar-lengths of now is FRESH in hours (`app/freshness.py`, read by `FreshnessConfig.from_env`) | 3 |
| DEPLOYMENT_LABEL | display-only label in GET /ui/v1/config; nothing may branch on it (`app/ui_config.py`) | empty |
| SERVER_VERSION | server_version in GET /ui/v1/config; else the installed distribution version, else `unknown` | unset |
| EVENTS_CLIENT_QUEUE_SIZE | events a `/ui/v1/events` client may hold undelivered before the hub drops it and the socket closes 1013 (`app/events.py`, `QUEUE_SIZE_ENV`) | 256 |
| USAGE_FLUSH_INTERVAL_SECONDS | how often buffered reads are written (`app/usage_recorder.py`) | 5 |
| USAGE_FLUSH_BATCH_SIZE | buffered reads that wake the flush early | 500 |
| USAGE_BUFFER_MAX | reads held in process before the oldest are dropped | 10000 |
| USAGE_ROLLUP_AFTER_DAYS | per-read rows older than this are folded into daily summaries | 90 |
| USAGE_ROLLUP_INTERVAL_SECONDS | how often the rollup runs | 3600 |
| REAPER_INTERVAL_SECONDS | seconds between expiry-reaper passes; a non-positive or malformed value is the default (`app/reaper.py`) | 3600 |
| REAPER_DELETES_ENABLED | the reaper deletes only when this is exactly `true` (any case); anything else is dry-run (`app/reaper.py`) | false |
| DAILY_REFRESH_ENABLED | the interim DAILY refresh loop runs unless this is `false`, `0`, `no` or `off`; read at start (`app/daily_refresh.py`) | true |
| DAILY_REFRESH_SETTLE_MINUTES | minutes after a session's close before the refresh pass; negative or malformed is the default | 30 |
| DAILY_REFRESH_MAX_PER_PASS | datasets one pass refreshes at most; non-positive or malformed is the default | 10 |
| DAILY_REFRESH_BACKOFF_SECONDS | wait after a first failed fetch, doubled per further failure in a row, capped at 600 | 30 |
| SERVICE_WORKERS | uvicorn workers; must stay 1 (see Key invariants). `docker-compose.yaml` pins it as a literal, so the env file cannot change it there | 1 |

## Common pitfalls

| # | Pitfall | Do instead |
|---|---|---|
| 1 | A request session opened outside `async_db` / `session()` is never returned to the pool | Take the session from `async_db`; commit only through `write_transaction` (bug tj-vhboky.76, ADR tj-8z213c) |

A pitfall lands here when it is true of this component and nowhere else. If it generalises past
this project, it belongs in the kit's `lessons/` instead — and if it is a prohibition rather than
a hazard, it belongs in the agent definition, not here.

---
<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
