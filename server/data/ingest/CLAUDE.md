# Market data ingest

Broker-facing ingest service. Fetches market data from Alpaca and answers data-store's `FetchDataset` gRPC request. Stock only today — crypto and option paths are stubs.

## Architecture reference

`bd list -t arch_index --all` — child of `trader_joe system` (tj-mtwvnu). Design diagram: `docs/img/design.jpg`.

## Tech stack

Python 3.12, FastAPI, alpaca-py, grpc.aio server (health service and `FetchDataset`)

## Key invariants

Broker-facing layer only; data-store owns the data. Blocking SDK calls go through the shared worker pool, never inline in an async handler. `BrokerRead.get_bars` returns a `BarsResponse` (served, possibly empty, with its `served_range`) or a `BarsFailure` and raises only for a bug; tj-3mk3u5.11 deleted the Kafka edge that turned a failure into a bare `{}`, so every typed failure now reaches the gRPC servicer and is rendered there — no sentinel survives.

**The asset list is refused whole when it is malformed** (decision `tj-7zjov1.218`, owner ruling 2026-10-31; `brokers/alpaca/read.py` `get_assets`, `assets.py`, `broker_api.py`). Concrete signals only, never a size or percentage threshold, and every check lives here, not in the store (a delivery the store refuses is redelivered for ever and lost at an ingest restart). Checks on the list read (the by-symbol lookup keeps its own rules): G1 HTTP status exactly 200; G2 media type `application/json`; G3 `Content-Length`, when present and no `Content-Encoding` is applied (any value skips it, `identity` included), equals the body length; G4 one UTF-8 JSON array and nothing after it but whitespace; G5 per record the documented required fields with their types (`id` a UUID in CANONICAL form (lowercase, hyphenated 8-4-4-4-12, `str(uuid.UUID(id)) == id`; upper or mixed case, braces, a `urn:uuid:` or `uuid:` prefix, bare hex, misplaced hyphens and surrounding whitespace are violations; owner ruling 2026-11-01, commit `141899f`), `symbol`, `class` equal to the requested class, `status` equal to the requested list, `exchange`, the four booleans, `name` a string, empty allowed); G6 no vendor id twice in one response (a repeated symbol is not a signal); G7 each element a JSON object. Not checked, on purpose: exchange and attribute values (an open set), unknown extra keys, optional fields, an empty array. A violation is `AssetListFailure(ExogenousError(VENDOR_UNAVAILABLE, detail))`, error class TRANSIENT: retried up to `ALPACA_MAX_ATTEMPTS`, then the request fails `ATTEMPTS_EXHAUSTED`, and the snapshot schedule asks again at its next trigger. The detail is `asset list refused as malformed: <check> (<count>)` (the check phrase is fixed, e.g. `G6 duplicate vendor id`; counts only, never a vendor value) and reaches `ingest_run.error_message` and the UI's run history. No snapshot and no event is written, because the store's asset-list arm runs only on SUCCEEDED. **Residue accepted:** a complete, well-formed, internally consistent but SHORT list cannot be detected by format (the vendor documents no total or completeness marker), and no plausibility warning is logged, since any "too few" test is a size threshold by another name. Confirmed by the owner 2026-11-01 (addendum 2 of `tj-7zjov1.218`): values are not checked against a fixed list, and the refusal is told apart by its detail text only (no stored distinct reason). G6 compares ids as served strings, which is safe because G5 leaves an id one spelling (the store derives asset-list events by the raw id string, so a respelled id would write a false vanished and appeared).

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| ALPACA_API_KEY / ALPACA_API_SECRET | broker credentials | unset |
| APP_INTERNAL_GRPC_HOST / APP_INTERNAL_GRPC_PORT | where the gRPC server binds; startup fails if either is unset | unset, by design (compose sets them) |
| DATA_STORE_GRPC_TARGET | host:port of the store's gRPC server, where attempt reports go; the production app's startup fails if it is unset | unset, by design (compose sets it) |
| ALPACA_TRADING_BASE_URL | host of the Alpaca trading API, read by the asset list and asset-by-symbol reads; paper is `https://paper-api.alpaca.markets`. The key must match the host (paper key with the paper host, live key with the live host). Unset, the asset reads fail closed with a VENDOR_AUTH failure. A host selector, not an enable flag | unset, by design (commented in `.env.default`) |
| ALPACA_RATE_LIMIT_PER_MINUTE | Alpaca calls per minute; may only lower the hard-coded ceiling of 200. Replaces the retired ALPACA_RATE_LIMIT_PER_SEC, ALPACA_RATE_BURST and ALPACA_BACKFILL_RESERVE, which nothing reads now | 200 |
| ALPACA_OUTGOING_BUFFER_SIZE | unacknowledged attempt reports the Alpaca handle holds before it stops taking work from its queue | 64 |
| ALPACA_STALL_TIMEOUT_S | seconds the oldest unacknowledged report may wait before the stall alarm (ERROR log, `stalled`) | 300 |
| ALPACA_MAX_ATTEMPTS | attempts one request gets, the first included | 5 |

## Common pitfalls

| # | Pitfall | Do instead |
|---|---|---|
| 1 | The Alpaca client is built at import time, which breaks tests and secret rotation | Build it lazily |
| 2 | A grpc.aio server still running when the event loop closes hangs process exit | Host it only with `async with` around the lifespan's yield (`app/grpc_host.py`); every test that starts one stops it in a `finally` |
| 3 | Any test that enters the real lifespan raises from `BindAddress.from_env()` with the gRPC env unset | Set both variables to `127.0.0.1` and a free port, or patch `app_depends.build_grpc_host` |
| 4 | Alpaca's error bodies carry no `code` field, so `APIError.code` raises `KeyError`; and `str(APIError)` is the raw vendor body | Classify on `APIError.status_code` only (`alpaca/classify.py`), write your own `detail`, and keep the exception as the error's `__cause__` |

A pitfall lands here when it is true of this component and nowhere else. If it generalises past
this project, it belongs in the kit's `lessons/` instead — and if it is a prohibition rather than
a hazard, it belongs in the agent definition, not here.

---
<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
