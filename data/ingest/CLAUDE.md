# Market data ingest

Broker-facing ingest service. Fetches market data from Alpaca and answers data-store's Kafka RPC request; a request may also be a feed subscription that this service streams to Kafka. Stock only today — crypto and option paths are stubs.

## Architecture reference

`bd list -t arch_index --all` — child of `trader_joe system` (tj-mtwvnu). Design diagram: `docs/img/design.jpg`.

## Tech stack

Python 3.12, FastAPI, alpaca-py, Kafka RPC server

## Key invariants

Broker-facing layer only; data-store owns the data. Blocking SDK calls go through the shared worker pool, never inline in an async handler.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| ALPACA_API_KEY / ALPACA_API_SECRET | broker credentials | unset |

## Common pitfalls

| # | Pitfall | Do instead |
|---|---|---|
| 1 | The Alpaca client is built at import time, which breaks tests and secret rotation | Build it lazily |

A pitfall lands here when it is true of this component and nowhere else. If it generalises past
this project, it belongs in the kit's `lessons/` instead — and if it is a prohibition rather than
a hazard, it belongs in the agent definition, not here.

---
<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
