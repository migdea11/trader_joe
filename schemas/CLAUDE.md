# Shared schemas

Pydantic v2 models shared across services — the cross-service data contract, separate from the transport contracts in `routers/*/app_endpoints.py`.

## Architecture reference

`bd list -t arch_index --all` — child of `trader_joe system` (tj-mtwvnu). Design diagram: `docs/img/design.jpg`.

## Tech stack

Pydantic v2

## Key invariants

Models are the cross-service contract. Annotations are evaluated at runtime, so imports used in them must not move into TYPE_CHECKING blocks.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| — | — | — |

## Common pitfalls

| # | Pitfall | Do instead |
|---|---|---|
| 1 | schemas imports routers constants, which read env at import time — wrong direction | Do not deepen it |

A pitfall lands here when it is true of this component and nowhere else. If it generalises past
this project, it belongs in the kit's `lessons/` instead — and if it is a prohibition rather than
a hazard, it belongs in the agent definition, not here.

---
<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
