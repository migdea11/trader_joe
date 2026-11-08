# Market data store

Owner of all persisted market data. Requests data from data-ingest over gRPC (`FetchDataset`), writes it to Postgres, and serves it over `/store/...`, `/internal/asset-data/...` and, to the web UI, the read-only `/ui/v1/...` routes as protobuf canonical JSON. Tables: `store_dataset_entry` (its nullable `collect` column is the collecting intent, below), `stock_market_activity`, `ingest_run`, `dataset_read`, `dataset_read_daily`, `dataset_override_log`, `dataset_deletion`.

## Architecture reference

`bd list -t arch_index --all` — child of `trader_joe system` (tj-mtwvnu). Design diagram: `docs/img/design.jpg`.

## Tech stack

Python 3.12, FastAPI, SQLAlchemy 2.0 async, asyncpg, Alembic, Postgres, `grpc.aio` client of data-ingest

## Key invariants

Sole owner of persisted market data. Every write goes through a repository; no raw SQL in routers. Migrations are additive and forward-only: `downgrade()` is not a recovery path (recovery is a restore; see Migration safety below), but every new revision still needs a working `downgrade()`, because the CI seed check downgrades each unmarked revision on a scratch database (checklist item 17), and nothing applies them on start: run `make migrate` after every deploy (it follows the running stack, dev, test or prod). The `collect` column (`e5c8b1d4f703`) is read by the catalog, the refresh and the writes, so a database one revision behind is not a state to run in.

**data_store runs ONE process: one uvicorn worker.** Six things are correct only because of it:

- the startup sweep of `RUNNING` ingest runs (`sweep_interrupted_runs` in `app/ingest/run_recording.py`, `fail_interrupted_runs` in `app/database/crud/ingest_run.py`) marks every `RUNNING` row `FAILED` / `INTERRUPTED`; a second worker starting would fail the run the first is executing;
- the events hub (`app/events.py`) lives in process memory, so an event reaches only the clients connected to the worker that published it;
- the usage buffer and the per-dataset-per-day id cap (`app/usage_recorder.py`): reads wait in this process's memory until a flush, and the 32-id cap is applied at flush against the stored ids, so two workers would each hold part of a day's reads and could together exceed the cap;
- the DAILY refresh loop (below): each worker would run its own pass and request the same missing tails. No cross-process lock exists; a Postgres advisory lock was considered for the scheduler and is not used: it is redundant while there is one worker, and it would be needed with two;
- the expiry reaper pass and its `ReaperPendingChanged` change detection (below): the previous pass's pending ids live in process memory, so a second worker would compare against its own copy and could announce a change that did not happen;
- the scheduler's store side (decision `tj-pd41mt`, proposed, not built) assumes the same.

`docker-compose.yaml` pins `SERVICE_WORKERS=1` as a literal in `environment:`, which outranks the operator's env file. A second worker would need all of these first:

- Postgres `LISTEN`/`NOTIFY`, so each worker republishes into its own events hub;
- a startup sweep keyed by process rather than by "every `RUNNING` row";
- a lock or leader election for the DAILY refresh and for the reaper, so two workers do not both run them;
- the reaper's change detection moved out of process memory;
- the usage buffer and the per-dataset-per-day id cap made safe across workers.

## Migration safety

The procedure is in the root README (Database migrations); the facts a store agent needs:

| Concern | Fact |
|---|---|
| Where the revisions live | In the data_store image (`/code/migrations`), not mounted from the checkout. An edited or new revision reaches a stack only after `make dev-build`, `make test-build` or `make prod-build`; `make migrate` against an older image runs that image's set and refuses an image with none. Both service source digests cover the revisions, so ingest's image goes stale too |
| Pre-migrate dump | `run_migrations.sh` dumps before any schema-moving command (`upgrade`, `downgrade`, `stamp`): `pg_dump` custom format, `.partial` then read back with `pg_restore --list` then renamed, mode 600, into `MIGRATE_BACKUP_DIR` (default: the backups folder beside the database's data directory, that is the parent of the host directory bind-mounted at the running postgres's `/var/lib/postgresql/data` plus `/backups`, read with `docker inspect`, so it follows whatever `DATA_DIR` the stack was started with; if there is not exactly one bind mount there with an absolute source, or the inspect fails, and `MIGRATE_BACKUP_DIR` is unset, the migrate is refused before alembic, so a postgres on a named volume needs `MIGRATE_BACKUP_DIR`; the mount source is a path on the Docker daemon's host, so run `make migrate` on that host), on the database's host, path printed on the dump line, never pruned. A failed dump refuses the migrate; there is no override |
| Production label | On the production definition a non-empty `DEPLOYMENT_LABEL` refuses the migrate. Dev and test are not asserted |
| Restore and rehearse | `make migrate-restore DUMP=<file>` and `make migrate-rehearse DUMP=<file>` (`restore_dump.sh`, `rehearse_migration.sh`) set `SYSTEM_TEST_DISPOSABLE_DB=1` in the recipe and refuse unless the running postgres is the `trader_joe_dev` test stack (`make system-launch`). Rehearsal prints `REHEARSAL OK` only at `(head)` and with data_store not unhealthy; the health line is the container's last status, with no restart, and the revisions applied are the running test image's (the checkout's only after `make test-build`) |
| Production recovery | A manual `pg_restore` of the pre-migrate dump; no script restores into production |
| No backup target | There is no standalone backup make target; `make migrate` takes the dump. Scheduled backups are `tj-oxwlda`; CI's `make migrate` takes the dump too, the agent-stack migrate tool (dev and test only) takes none |
| Lint gate | `make lint-migrations` (in `make lint`; `tools/migration_lint`) refuses non-additive store revisions outside `downgrade()` unless a line `# additive-exception: <bead-id>` marks them. It is a text-level check against honest non-additive changes, not a SQL parser: it refuses any raw SQL containing a dollar-quote delimiter (`$$` or `$tag$`; a positional parameter such as `$1` is not one), since it does not read dollar quoting; it does not understand `E'...'` backslash escapes or nested block comments, and can fail open on either; and it refuses an unterminated comment or quote and raw SQL it cannot resolve |

### Migration review checklist (m7)

A procedure, not a fact table. The architect applies it at the gate of every bead that adds a store revision (P15, P17, P18, P19, P20, P33 and any later one), against the revision file, its seeds and CI on the gated SHA. A "no" on any item is CHANGES NEEDED: to the builder for the revision, to the validator for a missing or wrong test. Items 22 to 25 are the deploy procedure, checked from the operator's transcript at the production sitting (P28), not from the diff.

Two points are flagged on purpose. NOT NULL on an existing column is a contract step that the lint refuses unless the revision is marked, so it can only appear in P19's marked revision (item 7). And whether an empty `downgrade()` passes the seed check's generic rule is untested (item 17): do not assume it does.

**Every revision (items 1 to 18)**

1. One new revision per bead; its down_revision is the head it was written on, and the chain still has a single head.
2. The revision passes make lint-migrations unmarked (CI Run Linters green); only P19 may carry the marker.
3. The marker, a line starting # additive-exception: <bead-id>, appears in PR 5 only on P19 and names tj-7zjov1.32; a marker anywhere else is a second exception the design does not have.
4. Nothing is dropped or renamed in upgrade(): no drop_table, drop_column, drop_constraint, drop_index, rename_table or alter_column new_column_name, and no raw DROP, DELETE, TRUNCATE or RENAME.
5. Nothing is retyped: no alter_column type_ and no raw ALTER COLUMN ... TYPE; an enum gains labels only by ALTER TYPE ... ADD VALUE.
6. Indexes and unique constraints are added, never dropped or replaced, and a unique index an ON CONFLICT upsert names is untouched.
7. A new NOT NULL column has a real server_default (an explicit None is no default); setting NOT NULL on an existing column, even after a backfill, is a contract step the lint refuses, so it appears only in the marked revision.
8. No alter_column removes a server default from a NOT NULL column outside the marked revision.
9. Every backfill is idempotent and re-runnable: each UPDATE or INSERT is guarded (for example WHERE col IS NULL, ON CONFLICT DO NOTHING) so a second run changes nothing, and the repeat-upgrade check (P05) is green.
10. A constraint added through op.create_unique_constraint, create_check_constraint or create_foreign_key passes the lint by ruling, so the reviewer confirms existing rows and every write the previous release makes still satisfy it.
11. Every op.execute and exec_driver_sql text is read by eye: the lint is a text-level check against honest non-additive changes, not a SQL parser. It refuses any raw SQL containing a dollar-quote delimiter ($$ or $tag$), so a revision that needs a function or DO body carries the additive-exception marker, and then every statement in that text, including each one after the body, is read by eye and must itself be additive (the destructive denylist does not name every DROP). It can still fail open on E-string backslash escapes and on nested block comments.
12. The revision runs in the one migration transaction: no autocommit_block, no transaction_per_migration, no CREATE INDEX CONCURRENTLY; any of them needs its own decision record before it is written.
13. A table created outside the models is added by exact name to OUT_OF_MODEL_TABLES in server/data/store/migrations/env.py in the same commit.
14. alembic check is clean at head (the system test pinning it is green); it says nothing about enum labels or server defaults, so those are compared by hand with the Python enums and the model defaults.
15. The canonical seed tests/system/seeds/<rev>.sql and <rev>.json is committed, made by the seed producer (agent-stack seed_dump or make seed-dump), synthetic and non-empty, and the seed guard (server/data/store/tests/test_seed_guard.py) is green.
16. CI's Migration Gate and System Testing are green on the gated SHA: every revision applies from empty, and every committed seed, the previous release's included, upgrades with every row kept (tj-vhboky.55 generic rule, P05).
17. downgrade() is exempt from the lint and is no recovery path, but the CI generic rule still downgrades each unmarked revision on a scratch database and expects the rows back, so the revision keeps a downgrade() that passes it.
18. CI built the data_store image from the gated SHA and applied the revision from it; no compose file mounts revisions, so the docs and the bead say a stack needs make dev-build, test-build or prod-build to see it.

**P19 only (items 19 to 21)**

19. P19 only: upgrade() checks first that every dataset has a listing and raises before any DDL or write when one does not, so a refusal changes nothing; it deletes no row and drops no data-bearing table.
20. P19 only: test_migration_with_data.py has an EXPECTATIONS entry for the revision, with a seed where every dataset has a listing (upgrade succeeds) and one with an unlisted dataset (upgrade refuses, revision unchanged, every column identical), and dev and test data are regenerated.
21. P19 only: spikes s4 and s5 (P27a) are answered before the gate, as O7 ruled.

**Deploy procedure (items 22 to 25)**

22. Before a production migrate: make prod-build from the reviewed SHA, and the production definition's DEPLOYMENT_LABEL is empty (non-empty refuses).
23. Before a production migrate: a production dump restored and rehearsed on the test stack after make test-build (make migrate-restore and make migrate-rehearse DUMP=<file>) printed REHEARSAL OK, then the stack was restarted and its health read.
24. The production make migrate printed its verified dump line (a failed dump refuses, with no override), and the operator copied the dump off the host.
25. Rollback story: code rollback across a migration boundary does not work, downgrade is not supported, and recovery is a manual pg_restore of that pre-migrate dump.

Sources: epic tj-7zjov1 m1 to m7; PR5-PLAN N1 STEP 2 and the P15 to P19 rows; REV1 N1 P33 row; tj-7zjov1.2 section 3 steps 5 and 10; tj-26jt9w section 6 and W3; gate rulings on tj-7zjov1.8, .9, .10, .45, .42, .44; ADR tj-yb1bxj addenda 3 and 5; ADR tj-x3ig38; tj-aw0tuk; tj-vhboky.55; tj-rk0w5i.

## Data freshness and completeness

Freshness and completeness are **computed on read and never stored**, by three pure modules (the clock and
the calendar are arguments) and composed with the queries in `app/dataset_catalog.py`. They
serve the `/ui/v1` routes. The modules depend one way, `app/trading_calendar.py` to `app/completeness.py` to
`app/health.py` to callers. The combined status enum is `FreshnessStatus` in `app/health.py`, and the proto message keeps the name `Freshness`; the module that judges is `health`. There is still no coverage ledger: no `missing_ranges` query, no `range_agg`
subtraction, and the older read routes (`/store/...`, `/internal/asset-data/...`) say nothing about
completeness, so a short answer from them is not distinguishable from a complete one.

| Concern | Where it lives |
|---|---|
| Status per dataset (`FRESH`, `LATE`, `OVERDUE`, `COMPLETE`, `GAPS`, `RETIRED`; `RETIRED` means a DAILY or STREAM dataset with `collect` false, shown as Stopped) | `evaluate_dataset` in `app/health.py` returns a `DatasetHealth`; its `FreshnessStatus` is the combined status the proto and UI see. The DAILY and STREAM recency rules and `RETIRED` live there, with `FreshnessConfig`; a STATIC dataset's status is mapped from `assess_completeness` in `app/completeness.py` (`COMPLETE`, `GAPS`) |
| Trading calendar | `app/trading_calendar.py`: one per data source (`SOURCE_CALENDARS`, offline `exchange_calendars`, via `calendar_for_source`; the `TradingCalendar` protocol and `ExchangeCalendarAdapter`), with `session_of`, `local_midnight` and `require_aware`; a source with none has no computed freshness |
| Status vocabulary the UI filters by | `StatusGroup` in `app/dataset_catalog.py`: healthy, late, failed, retired |
| Catalog listing, facets, bar paging | `app/dataset_catalog.py` over `app/database/crud/stock/dataset_catalog.py`; keyset cursors, opaque, bound to their sort |
| Bar page index (`GET /ui/v1/datasets/{id}/bar-pages`, the Viewer's paging) | `bar_page_index` in `app/dataset_catalog.py`, `read_bar_page_starts` in `app/database/crud/stock/dataset_catalog.py` |
| Coverage strip (`GET /ui/v1/datasets/{id}/coverage`): buckets and gap windows | `coverage_buckets` and `gap_intervals` in `app/completeness.py` and `local_midnight` in `app/trading_calendar.py`, composed by `dataset_coverage` in `app/dataset_catalog.py` |

Things that bite:

- **Completeness counts sessions; coverage is by period.** A session is covered when a bar covers its
  period: the session itself for 1-minute to 1-day, the Monday-Sunday week for 1-week, the calendar month
  for 1-month, in the trading calendar's time zone, whichever day of the period the vendor stamps. A period
  is expected when its first day's local midnight is in `[start, end)`, it has ended (its last session has
  closed) and it holds a session (`judged` in `app/completeness.py`), so the sessions of a partial first period
  are not expected. Gap counts and gap windows count only the expected sessions inside `[start, end)`: `_in_range`
  clips them, so a week or month whose only sessions fall past the range end owes nothing.
  A DAILY 1-week or 1-month dataset is FRESH when its last bar's period is at least the last ended period.
- **A bar is FINAL only if it was written at or after its period closed** (tj-grna9p.167). A vendor's bar fetched during an
  open period is part-way, so a DAILY dataset whose newest bar is of the expected period and was written before that
  period closed and settled is provisional: while it stays so, the dataset reads LATE, not FRESH, until the next session after the expected period opens, then OVERDUE (a failure). The refresh re-fetches FROM THAT BAR INCLUSIVELY; the bar upsert sets
  `updated_at` explicitly (`ON CONFLICT` skips `onupdate`), so the rewrite after the close makes it final and the dataset FRESH. The rule is `last_bar_is_provisional` in `app/health.py`. The close is
  the calendar's SESSION close plus the settle margin (`bar_period_close` in `app/health.py`), for every granularity up to 1day (an intraday bar is final only when written at or after its SESSION's close plus the margin, not its own period end); for 1week and 1month it is the close of the period's last session plus the margin. The margin is `DAILY_REFRESH_SETTLE_MINUTES` (`settle_margin_from_env`, default 30), the same value that paces the refresh. A STATIC dataset is unchanged. The write time is
  Postgres `now()`, the transaction start. **The write time is a lower bound on serve time.** The fetch transaction opens, with the own-overlap check, BEFORE the vendor request (`store_market_activity_worker`), and `updated_at` is `now()`, the transaction start. A bar is written before its stamp when `written_at < timestamp` (`written_before_its_stamp` in `app/health.py`, strict: equality is not before), the one predicate freshness and the refresh share. **A newest real bar written before its stamp is judged AT ITS WRITE TIME** (`_daily`, decision tj-7zjov1.120 addendum 1): the ordinary rule (period, provisional test, first missed period, deadline) runs on the write time, because the bar proves the dataset fetched through that instant and no further. A live 1min fetch whose last bar is stamped seconds after the transaction start therefore reads FRESH after the close, and a bar stamped a week ahead is owed at the first close after it was written (LATE after that close, OVERDUE from the next open, unless rewritten).
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
- **Expiry is a DELETION date only** (decision `tj-hhy3dk`). None means the dataset is never deleted by the reaper. Setting or clearing one retires and resumes nothing, for every update type, and a past expiry does not change health. A repeat `POST /store` still sets expiry from its body, so omitting it clears a stored one.
- **Collect is the collecting intent** (`store_dataset_entry.collect`, nullable bool, migration `e5c8b1d4f703`). A `DAILY` or `STREAM` row holds true (collecting) or false (Stopped, read as `RETIRED` on the wire: the enum names are kept); a `STATIC` row holds NULL, enforced by a CHECK, and has no intent. The migration backfilled `collect = (expiry IS NULL)` for subscriptions, leaving the expiry as it was. `DatasetSummary.collecting` is `is_collecting` in `app/dataset_catalog.py`: the intent for a subscription (NULL reads as true), and for a STATIC dataset whether its latest run is `RUNNING` (derived, never stored). `is_retired` (same module) is collect false on a subscription, and health, the status group, the refresh and `lock_active_entry` all judge by it.
- **`POST /store` carries an optional `collect`** (`StoreAssetDatasetBody`; a 422 on a `STATIC` request). On create an omitted `collect` is true for a subscription. On a repeat an omitted `collect` LEAVES THE STORED VALUE UNCHANGED and only an explicit one changes it, so an SDK retry of its original body cannot undo an operator's Stop. This differs from expiry on purpose: collect is the operator's intent, expiry the requester's own deletion date.

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
- **Events are notifications, not data.** Publishers call `publish()` strictly after their own commit. Published today: `IngestRunChanged` (start, acknowledgement, finish) and `DatasetChanged`: `UPDATED` (after `POST /store` that does not flip collect, after `PATCH .../expiry`, and after a DAILY refresh), `RETIRED` / `RESUMED` (a collect flip to false / true, from `PATCH .../collecting` or from a `POST /store` that flips it; a collecting PATCH that leaves the stored value as it was publishes nothing, and a `POST /store` that does is `UPDATED`) and `DELETED`. `CREATED` is never published: a new dataset arrives as `UPDATED`, because the upsert does not tell the two apart. `ReaperPendingChanged` is published by the expiry reaper (below). `RequestChanged` is typed in `app/events.py` and in `ui/v1/events.proto` and has no publisher.
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

## Operator writes (expiry, collecting and delete)

`PATCH /ui/v1/datasets/{id}/expiry`, `PATCH /ui/v1/datasets/{id}/collecting` (both 200, the updated `DatasetSummary`) and `DELETE /ui/v1/datasets/{id}` (204) are the UI's writes, in `routers/data_store/ui_dataset_writes.py`. All three need the instance secret; bodies are `SetExpiryRequest` / `SetCollectingRequest` / `DeleteDatasetRequest` parsed by `ProtoBody`, with no pydantic model.

| Concern | Where it lives |
|---|---|
| The operator-path writes (`operator_set_expiry`, `operator_set_collecting`, `operator_delete_entry`), beside the owner-scoped ones, which are unchanged | `app/database/crud/stock/store_dataset_entry.py` |
| The override decision, `OverrideRequired`, the INSERTs of the two logs | `app/database/crud/dataset_override.py` |
| Tables (no foreign key), migration `d7e2a9c35b18` | `app/database/models/dataset_override_log.py`, `dataset_deletion.py` |

- **Rule order** (expiry and delete): 422 bad body, 404 unknown id, 409 `OVERRIDE_REQUIRED` (owner is not `OPERATOR_PRINCIPAL` and the override is missing, unconfirmed or blank; no owner in the body), then the write. An override sent for the operator's own dataset is ignored.
- **One transaction per write**: the write, its `dataset_deletion` row (every delete) and its `dataset_override_log` row (only an override, reason stored trimmed; the log's action is `SET_EXPIRY`, `SET_COLLECTING` or `DELETE`). The `DatasetChanged` event is published after the commit: `UPDATED` after an expiry PATCH, `RETIRED` / `RESUMED` after a collecting PATCH that flipped the stored value, `DELETED` after a DELETE.
- **Both tables are append-only**: never an UPDATE or DELETE. `dataset_deletion.cause` is a string so the reaper (tj-grna9p.23) reuses the table.
- **Setting or clearing an expiry changes only the deletion date**; the expiry type is never writable. The expiry is stored as sent, and a past time makes the dataset reaper-pending, nothing else.
- **`PATCH .../collecting`** takes `{collect, override}`. Rule order: the instance secret; 422 when the body has no `collect` (presence: a missing key is never read as false), before anything else; 404 unknown id; 422 for a `STATIC` dataset (no intent); 409 `OVERRIDE_REQUIRED` for another owner's dataset without a confirmed override; then the write. It publishes `RETIRED` (to false) or `RESUMED` (to true) only when the stored value changed; a repeat of the stored value publishes nothing. The row is locked `FOR UPDATE` to tell a flip from a repeat.
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

- **DRY-RUN IS THE DEFAULT.** `REAPER_DELETES_ENABLED` must be the value `true` (any letter case, surrounding whitespace ignored) to delete anything; anything else, including unset, `1` and `yes`, is dry-run, and the flag is unset in every committed file (pinned by `server/common/tests/test_reaper_deletes_never_committed.py`). The default exists because STATIC rows created under the former now+1-day expiry default are all past expiry: the operator reads `GET /ui/v1/reaper/pending` before anything goes. Dry-run logs a count only, never an owner or a symbol.
- **The reaper deletes at expiry whatever `collect` says.** A Stopped subscription with a passed expiry is pending like any other dataset, and one with no expiry is never reaped.
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

- **The work is read from the database on every pass**, never stored or replayed: DAILY entries that are not retired (`is_retired`: `collect` is false; an expiry alone retires nothing), have no `RUNNING` ingest, and read LATE or OVERDUE under the freshness rule (last bar before the last completed session). A pass also runs once at start, so datasets requested earlier are backfilled.
- **Only the missing tail is requested** (`_fetch_start` in `app/daily_refresh.py`, tj-7zjov1.114). It starts just after the newest STORED bar, a placeholder (volume 0 and trade count 0) included, so a series the vendor keeps padding is not asked for again from its last real bar on every pass; freshness, the LATE or OVERDUE selection and the pass order still judge the newest REAL bar. It reaches back, so that no bar can stay stale, to the oldest of: the newest real bar when it is provisional (written before its period closed, `last_bar_is_provisional` in `app/health.py`), and every stored placeholder not yet SETTLED, that is written less than its period's close plus the settle margin plus `PLACEHOLDER_GRACE` (7 days) ago. The vendor's pad may be replaced by real data later; the evidence does not say, so a recent pad is asked for again until it has stood for the grace. Each re-fetch rewrites `updated_at` (the upsert sets it), so a placeholder leaves the window by itself and the window is constant, never growing. A placeholder written long after its close (a backfill) is settled at once. The cost for a dead series is one request per pass for about the grace window (under 10,000 points per call for 1day and for 1min), where it was one call for a tail growing by a session per close. The tail starts from the otherwise computed instant after the bar's period (`_after_bar`): for 1day the next local midnight of the calendar, for 1week the next Monday's local midnight (the week is taken from its Monday, whichever day the vendor stamped), and for 1hour and 1month the fixed step of one bar, because a local-midnight stamp moves an hour between winter and summer and a fixed offset could land past the next bar's stamp. It never starts before the declared start, the declared start is used when there are no bars at all, and a tail that starts at or after the declared end (or now) is not requested (counted as skipped; the end half is a guard, since a DAILY row's declared end does not validate as a request today). **Only a COUNTED bar advances the start** (`_can_be_real`, decision tj-7zjov1.120), because a bar that does not count would pass a session never requested, for good. A bar does not count when it is stamped at or after now; when it was WRITTEN BEFORE ITS OWN STAMP (`written_before_its_stamp`, `updated_at < timestamp`, equality counts: it was a future bar when fetched, so the sessions between that fetch and its stamp were never requested, and with the now test alone it would count once now passes it, leaving a permanent gap after downtime; only a rewrite after the stamp makes it count); when it is outside the declared range `[start, end)`; or, for a PLACEHOLDER, when its period holds no session (a weekend or holiday pad) or the calendar cannot place it. A bar that does not count stays stored, raw, and is ignored by the tail; a real bar (trades) in a no-session period still counts. The cost is a re-request of the sessions before an ignored bar. **Lost anchor:** when the newest real bar exists but does not count, the newest counted real bar (the anchor of the hole check) is unknown. If it fails ONLY by being written before its stamp (inside `[start, end)`, write time known), the tail starts at the local midnight of the period holding its WRITE TIME, or at the oldest unsettled counted placeholder if earlier, never before the declared start: everything before that write was requested by that fetch or an earlier one, and that period is requested whole. This is the normal live 1min case and costs at most one extra session (about 960 bars) inside the call the pass makes anyway. Only a newest real bar outside the declared range or with no write time falls back to the declared start, a full re-fetch for as long as it lasts (tj-7zjov1.122). **A hole inside the trailing run is reached back to** (`_skips_a_period`): if a period holding a session lies between two consecutive bars of the run (the newest real bar, then the pads after it) with no bar of its own, the tail starts after the earlier one, because the vendor padded around a session it never served. Placeholders are read per owed dataset from `recent_placeholder_bars`, newest first, in pages of `PLACEHOLDER_SCAN_LIMIT` (5000) from `PLACEHOLDER_LOOKBACK` before the newest real bar; the next older page is read only while the page just read ends in an unsettled placeholder, so a dead 1min series is covered however many bars a session holds (extended hours about 960). There is no iteration cap (a cap would silently drop pages); a progress guard raises `RuntimeError` when a full page does not end strictly older than where it began. **A truncated read** (the read stopped on a full page ending in a settled bar, `PlaceholderTail.truncated`) does not pair the newest real bar with the oldest placeholder read: the unread ones between them would look like a hole and the tail would reach back to the real bar every pass, the growing re-fetch this rule removes; pairs among the placeholders read are still checked. **The leading gap is not a hole:** the sessions between the declared start and the first stored bar of a placeholder-only dataset are not re-requested, because the first fetch of any dataset is one write transaction from its declared start, so any stored bar proves that range was requested once and answered empty (decision tj-7zjov1.120; a range answered empty is never re-verified, tj-7zjov1.121). Residuals, accepted: a placeholder still unsettled but older than the lookback floor (a dataset stopped for months), and one older than a whole page of settled pads newer than it, are not re-asked for. The entry keeps its declared range as identity, so the refresh extends the existing entry; `fetch_start` replaces only the start of the ingest request. The stored feed is named as the preference, so a tape ingest can no longer serve is a refused ack, never a second entry.
- **Bounded**: one dataset at a time, at most `DAILY_REFRESH_MAX_PER_PASS` REQUESTED per pass (skipped datasets do not count). A TRANSIENT failure is logged by exception type only, backs off (doubling, capped at 600 s), and the pass stops after 3 in a row or at once on an error that names `reset_at` (a rate limit). The rest is the next pass's. **No automatic retry** (decision tj-grna9p.55; Retry Now is manual): a dataset that failed transiently waits for the next session close or the catch-up at start; the backoff only paces the next dataset.
- **Permanent versus transient** is the error vocabulary's own outcome (`REASONS[reason].outcome`, `is_permanent`): REFUSED (a tape ingest cannot serve, an invalid request, a vendor 4xx, own overlap, a constraint violation) cannot succeed on retry; NOT_READY (peer or vendor unavailable, deadline, rate limit, rate budget, vendor auth, 5xx, database unavailable) and an error nobody typed are transient. A permanent refusal neither counts toward the stop nor backs off; it is counted in `PassResult.refused`. A stored row that no longer validates as a request is never sent (`skipped`).
- **Fair, from the database**: the order and the "tried already" state are the `ingest_run` history of trigger `SCHEDULE` (`finished_scheduled_runs`, last 7 days), with no job store. Never-attempted datasets go first (oldest newest-bar first), then least recently attempted. A dataset whose last scheduled attempt asked for the same tail and finished EMPTY or as a permanent FAILED since the last session close is not requested again until the next close; once due again it shares the pass with the rest, at most half the cap while others wait. A run that failed before the ack has no dataset id and is matched on symbol, asset type, data type, granularity and requested start; two owners of one symbol with the same tail therefore share that outcome.
- **The refresh never writes a lifecycle field.** `store_market_activity_worker(..., preserve_lifecycle=True)` locks the entry `FOR UPDATE` on the ack (`lock_active_entry`) and upserts with `preserve_lifecycle=True`, whose `ON CONFLICT` sets `updated_at` only, so an operator's expiry is never overwritten. A dataset stopped (`collect` set false) or deleted while the vendor call was in flight has the answer DROPPED (nothing stored, counted in `PassResult.discarded`, the run recorded EMPTY with no bars). POST /store is unchanged: a repeat sets expiry from its body, and leaves collect alone unless it carries one.
- **An unmigrated database** logs one warning naming `make migrate` per process and the pass does nothing.
- **Nothing counts vendor quota here** (ingest's counting, tj-grna9p.24, is not built). What bounds the spend is the cap, the serialisation, ingest's per-vendor rate budget (`<SOURCE>_RATE_LIMIT_PER_SEC`; unset means unthrottled) and the missing-tail-only request. Ingest treats a DAILY request as INTERACTIVE priority, the same as a UI request.
- **A fetch not yet acknowledged has no dataset id** (tj-jc635p), so the running-ingest check cannot see a request that is in flight; its bars write idempotently, so the cost is a duplicate vendor call, never duplicate bars.
- **`DAILY_REFRESH_ENABLED=false` is the kill switch** (also `0`, `no`, `off`), read at start: a change needs a restart. The default, and the cap that must exist before a penalising broker is enabled, are in the environment table below.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| DATABASE_URI | primary DSN | fails closed |
| FRESHNESS_STREAM_BAR_MULTIPLE | STREAM freshness: a last bar within this many bar-lengths of now is FRESH in hours (`app/health.py`, read by `FreshnessConfig.from_env`) | 3 |
| DEPLOYMENT_LABEL | display-only label in GET /ui/v1/config; nothing may branch on it (`app/ui_config.py`) | empty |
| SERVER_VERSION | server_version in GET /ui/v1/config; else the installed distribution version, else `unknown` | unset |
| EVENTS_CLIENT_QUEUE_SIZE | events a `/ui/v1/events` client may hold undelivered before the hub drops it and the socket closes 1013 (`app/events.py`, `QUEUE_SIZE_ENV`) | 256 |
| USAGE_FLUSH_INTERVAL_SECONDS | how often buffered reads are written (`app/usage_recorder.py`) | 5 |
| USAGE_FLUSH_BATCH_SIZE | buffered reads that wake the flush early | 500 |
| USAGE_BUFFER_MAX | reads held in process before the oldest are dropped | 10000 |
| USAGE_ROLLUP_AFTER_DAYS | per-read rows older than this are folded into daily summaries | 90 |
| USAGE_ROLLUP_INTERVAL_SECONDS | how often the rollup runs | 3600 |
| REAPER_INTERVAL_SECONDS | seconds between expiry-reaper passes; a non-positive or malformed value is the default (`app/reaper.py`) | 3600 |
| REAPER_DELETES_ENABLED | the reaper deletes only when this is `true` in any letter case, surrounding whitespace ignored (`strip().lower()`); anything else, unset, `1` and `yes` included, is dry-run (`app/reaper.py`) | unset (reads as dry-run) |
| DAILY_REFRESH_ENABLED | the interim DAILY refresh loop runs unless this is `false`, `0`, `no` or `off`; read at start (`app/daily_refresh.py`). ON BY DEFAULT, by the owner's ruling, accepted for the CURRENT broker only: its vendor errors when a limit is exceeded and does not penalise it. A robust cumulative cap (a quota counter that survives restarts, and admission control in the scheduler) must exist BEFORE any broker that penalises exceeding limits (IBKR) is enabled; no cumulative vendor-usage counter exists yet (tj-lenn8u). This row is the one place the default is written down | true |
| DAILY_REFRESH_SETTLE_MINUTES | minutes after a session's close before the refresh pass, and the settle margin that decides when a written bar is final (`health.settle_margin_from_env`, read by `FreshnessConfig.from_env` and the refresh); negative or malformed is the default | 30 |
| DAILY_REFRESH_MAX_PER_PASS | datasets one pass refreshes at most; non-positive or malformed is the default | 10 |
| DAILY_REFRESH_BACKOFF_SECONDS | wait after a first failed fetch, doubled per further failure in a row, capped at 600 | 30 |
| SERVICE_WORKERS | uvicorn workers; must stay 1 (see Key invariants). `docker-compose.yaml` pins it as a literal, so the env file cannot change it there | 1 |

## Known limits

Accepted and recorded, not bugs to fix in passing. Each names what bounds it.

| Limit | Detail |
|---|---|
| One usage batch can be counted twice at shutdown | A cancel that lands after the batch INSERT commits but before `flush` returns puts the batch back, so the final flush writes it again. It overcounts usage telemetry by at most one batch, only at shutdown (`app/usage_recorder.py`). |
| About 16,000 datasets per source on the stats routes | The usage and stale queries bind the dataset ids as parameters, and asyncpg allows 32,767 per statement, so a source with more than about 16,000 datasets exceeds it (`tj-ck7blk` replaces the id list with a subquery). |
| The DAILY refresh's skip is shared across owners | The "already tried this tail" rule keys on the identity label (symbol, asset type, data type, granularity and requested start) until `tj-jc635p` gives a fetch its dataset id at request time. Two owners of one symbol with the same tail share a refusal's skip for one session. |
| A dataset resumed after a mid-fetch stop waits | A stop (collect set false) during a fetch drops the answer and is never undone; if the dataset is resumed afterwards, it is refreshed at the next session close, not at once. |
| No automatic retry of a failed refresh | A transient failure waits for the next close or the catch-up at start (decision `tj-grna9p.55`). |
| An unacknowledged fetch has no dataset id | The running-ingest check and the reaper cannot see a fetch before ingest acknowledges it (`tj-jc635p`); a duplicate vendor call is the cost, never duplicate bars. |
| The trading calendars end about a year after the process starts | The store builds each calendar with the library's default end date, so a data_store process that runs longer than about a year without a restart hits an out-of-range error. The DAILY refresh then retries every few minutes without refreshing anything until the process restarts. A follow-up sets an explicit far-future end. |
| The owner string appears in access log lines | The web server logs the raw request path, so `DELETE /store/{id}?owner=...` and `GET /store?owner=...` log the owner from the query string. A follow-up adds a redacting log filter; authenticated per-client keys will move owner off the query string later. |
| The agent-mcp group's `mcp` is capped below 2 | `mcp>=1.30.0,<2` in `pyproject.toml`. Moving to 2.x is the owner's decision. |
| The security tooling group's own advisories are excluded from the audit | That group (semgrep) pins `mcp` 1.29.0 and `pyjwt` 2.13.0, which carry advisories. It is tooling only and in no image, so `make security` does not audit it by design; the findings clear when semgrep raises its pins. |

## Common pitfalls

| # | Pitfall | Do instead |
|---|---|---|
| 1 | A request session opened outside `async_db` / `session()` is never returned to the pool | Take the session from `async_db`; commit only through `write_transaction` (bug tj-vhboky.76, ADR tj-8z213c) |

A pitfall lands here when it is true of this component and nowhere else. If it generalises past
this project, it belongs in the kit's `lessons/` instead — and if it is a prohibition rather than
a hazard, it belongs in the agent definition, not here.

---
<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
