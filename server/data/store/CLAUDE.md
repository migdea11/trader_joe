# Market data store

Owner of all persisted market data. Requests data from data-ingest over gRPC (`FetchDataset`), writes it to Postgres, and serves it over `/store/...` and `/internal/asset-data/...`. Tables: `store_dataset_entry`, `stock_market_activity`.

## Architecture reference

`bd list -t arch_index --all` — child of `trader_joe system` (tj-mtwvnu). Design diagram: `docs/img/design.jpg`.

## Tech stack

Python 3.12, FastAPI, SQLAlchemy 2.0 async, asyncpg, Alembic, Postgres, `grpc.aio` client of data-ingest

## Key invariants

Sole owner of persisted market data. Every write goes through a repository; no raw SQL in routers. Migrations are additive.

## Data freshness and completeness

Removing Kafka removed consumer-group lag, which was this system's only free liveness signal.
**Nothing has replaced it yet.** `tj-3mk3u5.4` and `tj-3mk3u5.5` are still open, and the tree today
has no coverage ledger: no `missing_ranges` query, no `range_agg` subtraction, no freshness or
completeness metric. Do not rely on one, and do not describe one as available.

What can actually be asked right now, both straight against Postgres:

| Question | Where the answer is |
|---|---|
| How fresh is a series? | the greatest `end` across that symbol's `store_dataset_entry` rows, measured against `now()` |
| What was actually stored | the `stock_market_activity` rows falling inside that range |

A short answer from a read path is **not** yet distinguishable from a complete one, so never infer
completeness from a row count. Closing that is exactly what `tj-3mk3u5.5` is for: when the ledger
lands, freshness becomes `now()` minus the upper bound of the newest covered range per series, and
completeness becomes the `range_agg` subtraction over the coverage rows, with reads returning an
explicit envelope naming the missing ranges instead of quietly returning fewer rows.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| DATABASE_URI | primary DSN | fails closed |
| FRESHNESS_STREAM_BAR_MULTIPLE | STREAM freshness: a last bar within this many bar-lengths of now is FRESH in hours (`app/freshness.py`, read by `FreshnessConfig.from_env`) | 3 |
| DEPLOYMENT_LABEL | display-only label in GET /ui/v1/config; nothing may branch on it (`app/ui_config.py`) | empty |
| SERVER_VERSION | server_version in GET /ui/v1/config; else the installed distribution version, else `unknown` | unset |

## Common pitfalls

| # | Pitfall | Do instead |
|---|---|---|
| 1 | A request session opened outside `async_db` / `session()` is never returned to the pool | Take the session from `async_db`; commit only through `write_transaction` (bug tj-vhboky.76, ADR tj-8z213c) |

A pitfall lands here when it is true of this component and nowhere else. If it generalises past
this project, it belongs in the kit's `lessons/` instead — and if it is a prohibition rather than
a hazard, it belongs in the agent definition, not here.

---
<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
