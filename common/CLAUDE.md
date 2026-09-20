# Shared library

Shared library imported by both services: Kafka producer/consumer and the hand-written RPC layer, the topic registry, the Postgres session factory, custom SQLAlchemy types, and env/logging/worker-pool helpers.

## Architecture reference

`bd list -t arch_index --all` — child of `trader_joe system` (tj-mtwvnu). Design diagram: `docs/img/design.jpg`.

## Tech stack

Kafka producer/consumer and hand-written Kafka RPC, Postgres session factory, custom SQLAlchemy types, env/logging/worker pool

## Key invariants

Changing anything here affects both services. The Kafka topic registry is the inter-service contract.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| RUN_MODE | dev enables the debugger | defaults to DEV (bug tj-g1qqf1) |

## Common pitfalls

| # | Pitfall | Do instead |
|---|---|---|
| 1 | Env vars are read at import time, so importing a module requires them set | Keep reads lazy in new code |

A pitfall lands here when it is true of this component and nowhere else. If it generalises past
this project, it belongs in the kit's `lessons/` instead — and if it is a prohibition rather than
a hazard, it belongs in the agent definition, not here.

---
<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
