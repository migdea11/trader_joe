# Market data store

Owner of all persisted market data. Requests data from data-ingest over Kafka RPC, writes it to Postgres, and serves it over `/store/...` and `/internal/asset-data/...`. Tables: `store_dataset_entry`, `stock_market_activity`.

## Architecture reference

`bd list -t arch_index --all` — child of `trader_joe system` (tj-mtwvnu). Design diagram: `docs/img/design.jpg`.

## Tech stack

Python 3.12, FastAPI, SQLAlchemy 2.0 async, asyncpg, Alembic, Postgres

## Key invariants

Sole owner of persisted market data. Every write goes through a repository; no raw SQL in routers. Migrations are additive.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| DATABASE_URI | primary DSN | fails closed |

## Common pitfalls

| # | Pitfall | Do instead |
|---|---|---|
| 1 | Compose falls back to a hard-coded password and host 'db' | Fail on missing variables (bug tj-70ovb3) |

A pitfall lands here when it is true of this component and nowhere else. If it generalises past
this project, it belongs in the kit's `lessons/` instead — and if it is a prohibition rather than
a hazard, it belongs in the agent definition, not here.

---
<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
