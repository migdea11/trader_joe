# Market data store

Owner of all persisted market data. Requests data from data-ingest over gRPC (`FetchDataset`), writes it to Postgres, and serves it over `/store/...`, `/internal/asset-data/...` and, to the web UI, the read-only `/ui/v1/...` routes as protobuf canonical JSON. Tables: `store_dataset_entry`, `stock_market_activity`.

## Architecture reference

`bd list -t arch_index --all` — child of `trader_joe system` (tj-mtwvnu). Design diagram: `docs/img/design.jpg`.

## Tech stack

Python 3.12, FastAPI, SQLAlchemy 2.0 async, asyncpg, Alembic, Postgres, `grpc.aio` client of data-ingest

## Key invariants

Sole owner of persisted market data. Every write goes through a repository; no raw SQL in routers. Migrations are additive.

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
- **Expiry is optional.** None means the dataset never expires; setting one on a `DAILY` or `STREAM`
  dataset retires it, and a repeat request sets expiry from its body, so omitting it clears a stored one.

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
