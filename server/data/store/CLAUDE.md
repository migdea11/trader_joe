# Market data store

Owner of all persisted market data. Requests data from data-ingest over gRPC (`FetchDataset`), writes it to Postgres, and serves it over `/store/...`, `/internal/asset-data/...` and, to the web UI, the read-only `/ui/v1/...` routes as protobuf canonical JSON. Tables: `store_dataset_entry`, `stock_market_activity`, `ingest_run`, `dataset_read`, `dataset_read_daily`.

## Architecture reference

`bd list -t arch_index --all` — child of `trader_joe system` (tj-mtwvnu). Design diagram: `docs/img/design.jpg`.

## Tech stack

Python 3.12, FastAPI, SQLAlchemy 2.0 async, asyncpg, Alembic, Postgres, `grpc.aio` client of data-ingest

## Key invariants

Sole owner of persisted market data. Every write goes through a repository; no raw SQL in routers. Migrations are additive.

**data_store runs ONE process: one uvicorn worker.** Three things are correct only because of it:

- the startup sweep of `RUNNING` ingest runs (`sweep_interrupted_runs` in `app/ingest/run_recording.py`, `fail_interrupted_runs` in `app/database/crud/ingest_run.py`) marks every `RUNNING` row `FAILED` / `INTERRUPTED`; a second worker starting would fail the run the first is executing;
- the events hub (`app/events.py`) lives in process memory, so an event reaches only the clients connected to the worker that published it;
- the scheduler's store side (decision `tj-pd41mt`, proposed, not built) assumes the same.

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
- **A gap window is whole sessions**: from the local midnight of its first session to the local midnight after its last, so it can start before or end after the dataset's range. Buckets are clipped to the range; gaps are not. A coverage read costs two statements and records no usage. With no trading calendar covering the dataset it is a 422, never an empty strip, which would read as no gaps.
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
- **Events are notifications, not data.** Publishers call `publish()` strictly after their own commit. Published today: `IngestRunChanged` (start, acknowledgement, finish) and `DatasetChanged` `UPDATED` (after `POST /store`) and `DELETED`. `CREATED` is never published: a new dataset arrives as `UPDATED`, because the upsert does not tell the two apart. `RequestChanged` and `ReaperPendingChanged` are typed in `app/events.py` and in `ui/v1/events.proto` and have no publisher.
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
