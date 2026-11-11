# API reference — the declared interface surface

**This document describes the declared interface surface of the current tree.** It pins no commit SHA: what
each entry cites instead is a file and a symbol. Check those before trusting an entry: if the file or symbol an
entry names no longer exists, the entry is stale. The server code lives under `server/`, so file paths in this
document are relative to it, and the manifest is at `server/routers/tests/interface_manifest/`.

**Checked on real Postgres.** Every status code and behaviour below was checked in
process, against the code and its tests. The system suite (`tests/system`, through
`make test-system SYSTEM_TEST_DISPOSABLE_DB=1`) runs on the host against a wiped, freshly migrated stack. It
covers, on real Postgres:

- the migrations applied by `make migrate` from empty, with the recorded revision equal to the single
  head (the baseline is also covered by CI's Migration Gate and the catalogue test
  `tests/system/test_alpha_baseline_schema_pg.py`);
- both unique constraints rejecting exact duplicates, and `owner` refusing an explicit NULL;
- the bar upsert keeping one row on a repeat and refreshing it;
- the delete cascade removing one entry's bars and leaving another entry's whole;
- an oversized batch landing whole in one transaction, and a failure on a later chunk leaving
  nothing;
- the instance secret rejecting a write over real HTTP, with nothing changed;
- the 409 on an overlap with the caller's own dataset;
- sequential reads beyond the connection pool's size all answering.

## Read this first

**Writes require the deployment's instance secret; reads require nothing.** The write routes
(`POST /store/...`, `DELETE /store/{id}`, `POST /internal/asset-data/...`, and the operator and panel
writes under `/ui/v1`, W1 to W3 and PW1 to PW3) reject any request that
does not carry the `X-Instance-Secret` header matching `INSTANCE_WRITE_SECRET`, with a 401. The
check **fails closed**: an unset or empty secret rejects every write. Every read route stays open.

**That secret authenticates the deployment, not the caller.** With one key there is effectively one
principal, so "only the owner may edit a dataset" is **not** enforced against anyone holding the key:
whoever has it may declare any `owner` they like. What the owner check buys is protection against
*mistakes* — an honest caller cannot accidentally overwrite or delete another principal's dataset. It
is not access control. It also leaves every surface other than the write
routes answering without credentials. What narrows who can reach them is the network model: prod publishes no host port, and data_store is reachable only from
containers on its internal networks, including any client on `store_api`; dev publishes on loopback
only. That is a perimeter, not an identity. `GET /latency/{latency_type}` is still an
unauthenticated request amplifier when switched on. See gap **G4**.

**This is the market-data surface, and only that.** Nothing here touches accounts, portfolios,
orders or fills. A reader arriving from the project description — a trading platform with an
append-only order/fill event log and account-type-aware turnover limits — will look for those
interfaces and not find them. They are not built, by scope and not by oversight. See gap **G9**.

**The manifest is authoritative; this document is downstream of it.** The machine-checked surface
lives in `routers/tests/interface_manifest/*.manifest`, and `routers/tests/test_interface_surface.py`
asserts those files as an *equality* against the live code: it imports the router packages,
enumerates what they actually expose, and fails if the manifest and the code disagree in either
direction. Adding a route therefore costs a manifest line in the same diff, and there is deliberately
no regenerate command. **This document has no such
test and can drift.** If you find a disagreement between the two, trust the manifest and file a bug
against this document.

## How to read an entry

**Entries are keyed on address, not on file path.** Every file path in this document is relative to
`server/` and can go stale in one diff while the addresses survive. File and symbol are still given — they
are what makes an entry checkable — but they are deliberately never woven into prose, so updating them is a
field edit rather than a rewrite.

data_ingest's interface is the gRPC `IngestService`, which lives outside `routers/` and so is pinned where
it lives — `data/ingest/tests/test_grpc_host.py` — instead of in this manifest.

**One known blind spot in the manifest: matching is by path, not by method.** An interface enum
member carries a path and no method, so the manifest cannot tell `GET /store/{id}` from
`DELETE /store/{id}`. Binding the DELETE is enough to make the path count as bound, and the unserved
GET at the same path never reaches the manifest's `unbound-path` list. This is the honest limit of
what a declaration says. It hides nothing today: the GET at `/store/{id}` is served and has its own
manifest line (S6), but a declaration added later that shares a path with a bound method would be
hidden the same way.

### Stability markers

Every entry carries one. This is the field that carries most of this document's value — a flat
reference table without it would be actively misleading, because the entries differ in how settled they are.

| Marker | Meaning |
|---|---|
| **Stable** | Expected to survive in substance. |
| **Fate undecided** | Genuinely not yet decided. Do not guess, and confirm before building on it. |

## The surface at a glance

**Thirty-eight** interfaces are visible to the manifest: three in `common.manifest` and thirty-five in
`data_store.manifest`. The count is taken from `routers/tests/interface_manifest/*.manifest`, not from
the prose below. `data_ingest.manifest` holds a single `none` line,
which is a declaration that the component exposes nothing this manifest can enumerate, not an
interface; its real surface is the gRPC `IngestService`, pinned outside `routers/`.

| # | Address | Component | Kind | Auth | Stability |
|---|---|---|---|---|---|
| C1 | `GET /ping` | common | http | none | **Stable** |
| C2 | `/latency/{latency_type}` | common | unbound-path | none | **Stable** |
| C3 | `/latency_internal` | common | unbound-path | none | **Stable** |
| S1 | `POST /store/{asset_type}/{data_type}/{asset_symbol}` | data_store | http | instance secret | **Stable** |
| S2 | `GET /store/{asset_type}/{data_type}/{asset_symbol}` | data_store | http | none | **Stable** |
| S3 | `DELETE /store/{id}` | data_store | http | instance secret | **Stable** |
| S4 | `POST /internal/asset-data/{asset_type}/{data_type}` | data_store | http | instance secret | **Fate undecided** |
| S5 | `GET /internal/asset-data/{asset_type}/{data_type}` | data_store | http | none | **Stable** |
| S6 | `GET /store/{id}` | data_store | http | none | **Stable** |
| U1 | `GET /ui/v1/config` | data_store | http (protobuf JSON) | none | **Stable** |
| U2 | `GET /ui/v1/datasets` | data_store | http (protobuf JSON) | none | **Stable** |
| U3 | `GET /ui/v1/datasets/facets` | data_store | http (protobuf JSON) | none | **Stable** |
| U4 | `GET /ui/v1/datasets/{dataset_id}` | data_store | http (protobuf JSON) | none | **Stable** |
| U5 | `GET /ui/v1/datasets/{dataset_id}/bars` | data_store | http (protobuf JSON) | none | **Stable** |
| U6 | `GET /ui/v1/datasets/{dataset_id}/runs` | data_store | http (protobuf JSON) | none | **Stable** |
| U7 | `GET /ui/v1/datasets/{dataset_id}/coverage` | data_store | http (protobuf JSON) | none | **Stable** |
| U8 | `WS /ui/v1/events` | data_store | ws (protobuf JSON frames) | none | **Stable** |
| U9 | `GET /ui/v1/datasets/{dataset_id}/bar-pages` | data_store | http (protobuf JSON) | none | **Stable** |
| W1 | `PATCH /ui/v1/datasets/{dataset_id}/expiry` | data_store | http (protobuf JSON) | instance secret | **Stable** |
| W2 | `DELETE /ui/v1/datasets/{dataset_id}` | data_store | http | instance secret | **Stable** |
| W3 | `PATCH /ui/v1/datasets/{dataset_id}/collecting` | data_store | http (protobuf JSON) | instance secret | **Stable** |
| U10 | `GET /ui/v1/reaper/pending` | data_store | http (protobuf JSON) | none | **Stable** |
| U11 | `GET /ui/v1/stats/usage/summary` | data_store | http (protobuf JSON) | none | **Stable** |
| U12 | `GET /ui/v1/stats/usage/last-read-buckets` | data_store | http (protobuf JSON) | none | **Stable** |
| U13 | `GET /ui/v1/stats/usage/reads-per-day` | data_store | http (protobuf JSON) | none | **Stable** |
| U14 | `GET /ui/v1/stats/usage/stale` | data_store | http (protobuf JSON) | none | **Stable** |
| U15 | `GET /ui/v1/stats/storage/current` | data_store | http (protobuf JSON) | none | **Stable** |
| PR1 | `GET /ui/v1/panels` | data_store | http (protobuf JSON) | none | **Stable** |
| PR2 | `GET /ui/v1/panels/{panel_id}` | data_store | http (protobuf JSON) | none | **Stable** |
| PR3 | `GET /ui/v1/panels/{panel_id}/settings-versions` | data_store | http (protobuf JSON) | none | **Stable** |
| PR4 | `GET /ui/v1/panel-sources/{source}/settings-versions` | data_store | http (protobuf JSON) | none | **Stable** |
| PR5 | `GET /ui/v1/panels/{panel_id}/boundaries` | data_store | http (protobuf JSON) | none | **Stable** |
| PR6 | `GET /ui/v1/panels/{panel_id}/members` | data_store | http (protobuf JSON) | none | **Stable** |
| PR7 | `GET /ui/v1/panels/{panel_id}/asset-list-history` | data_store | http (protobuf JSON) | none | **Stable** |
| PR8 | `GET /ui/v1/datasets/{dataset_id}/corporate-actions` | data_store | http (protobuf JSON) | none | **Stable** |
| PW1 | `POST /ui/v1/panels` | data_store | http (protobuf JSON) | instance secret | **Stable** |
| PW2 | `POST /ui/v1/panels/{panel_id}/settings-versions` | data_store | http (protobuf JSON) | instance secret | **Stable** |
| PW3 | `POST /ui/v1/panel-sources/{source}/settings-versions` | data_store | http (protobuf JSON) | instance secret | **Stable** |

data_ingest has no row. Its interface is the gRPC `IngestService` (`Submit` and `Instance` are served, `Quote` is declared
only), declared in `proto/trader_joe/proto/internal/ingest/v1/ingest.proto`, handled by
`routers/data_ingest/submit_handler.py` and `instance_handler.py` and registered in
`data/ingest/app/grpc_host.py`; its results come back on the store's `StoreService.DeliverAttempt` (see
[I1](#i1--ingestservice-and-the-push-result-path-grpc)). It is outside this document's REST-shaped inventory, and pinned by `data/ingest/tests/test_grpc_host.py`.

**One row, one manifest line.** `GET /store/{id}` is served as S6 and has its own manifest line.

**`unbound-path` is a two-entry kind, and both are the latency pair.** That is the state the
kind exists to describe: written code that mounts only under an environment flag. No
data_store or data_ingest declaration sits unserved.

### The four kinds

- **`http`** — a FastAPI route registered on a module-scope `APIRouter` while the module body ran.
- **`ws`** — a FastAPI WebSocket route on a module-scope `APIRouter`. One today, U8. It has no
  response model, so the manifest's `response` field is `-`; its frame type is pinned by the events
  tests.
- **`none`** — the component exposes nothing this manifest can enumerate, said out loud. A single
  `none` line is a declaration, which an accidentally empty file is not.
- **`unbound-path`** — a path declared in an interface enum that no import-time route serves. This
  kind exists because the import-time surface is not the whole declared surface, and that gap was
  invisible before the manifest existed.

---

# The data model behind the store surface

Read this before S1–S5: every data_store route is a view onto two tables, `store_dataset_entry` (one
row per dataset) and `stock_market_activity` (the bars). This section states what the
code does, and why.

## A bar belongs to exactly one dataset

`stock_market_activity.dataset_id` is a non-null foreign key to the entry with `ON DELETE CASCADE`,
and it **leads** the bar's natural key, `(dataset_id, asset_symbol, source, feed, granularity,
timestamp)`, constraint `uq_stock_market_activity_natural_key`. There is no membership table. Two
datasets that cover the same symbol and minute each hold **their own copy** of that bar; deleting a
dataset deletes exactly its bars by cascade.

**Why.** Sharing one bar row between datasets made a bar's ownership last-write-wins and turned every
delete into a question about who else referenced each row. Duplication makes a dataset's bars its
own, the delete a cascade, and a dataset-scoped read one contiguous index scan. The choice turns on
one asymmetry: being wrong about duplication costs an index and a statement, recoverable at any time;
being wrong about sharing costs provenance that was never durable anywhere else.

**What it costs, measured on paper and not yet on a real database.** Storage crosses above the
shared design once bars average more than **1.26 covering entries**. The entry listing (S2) counts
bars through an outer join onto the full bar row, so it aggregates a wider row than a membership row
would have been. A membership table over duplicated rows is purely additive if either cost bites.

## A dataset is identified by every field it was requested with

The entry's unique key, `uq_store_dataset_entry_identity`, is **eleven columns**: `owner`,
`asset_symbol`, `asset_type`, `data_type`, `source`, `granularity`, `expiry_type`, `update_type`,
`feed`, `start`, `end`. Two requests differing in **any** of them — the range included — are two datasets.
Nothing is merged on conflict.

- `feed` **is** part of the entry's identity. The entry is
  written by the first delivered attempt, with the feed that attempt's header reports, so an IEX request and a SIP request over
  the same window are two datasets. The
  create body takes an optional `feed`; the value stored is the feed the handle resolved, which is not
  always the body's (a body naming none is stored as `SIP`). The bar carries `feed` too.
- `expiry` (when the dataset's data dies) is a column on the entry, not on the bar, and is **not**
  identity. It is nullable: none means the dataset never expires.
- `end` omitted means open-ended; it is stored as a 1970-01-01 sentinel so that two open-ended
  requests still collide on the key.
- `owner`, `expiry_type` and `update_type` are `NOT NULL`. A null in a unique key is distinct from
  every other null in Postgres, which would silently turn an exact repeat into a second row.

## What a create does

`POST /store/...` (S1) resolves the request against the caller's own datasets:

| Request | Outcome |
|---|---|
| Every identity field equal to an existing entry, range included | **No-op.** The existing entry's id is reused; only its `expiry` and `updated_at` are refreshed, and `expiry` is taken from the request, so omitting it clears a stored one. This is what makes a retried POST safe. |
| Same owner and same non-range fields, range overlapping but not equal (ranges are half-open, so ranges that only touch do not overlap) | **409**, with the overlapping entries' ids in the top-level `colliding_ids` member of the problem+json (`detail` is a sentence). Nothing is written. |
| Anything else, including another owner's overlapping dataset | A new entry. Owner is identity, so a different owner's dataset is never a collision. |

**The overlap refusal is feed-blind, and that is current behaviour.** The 409 keys on the identity
**minus `feed`** (`OverlapKey` and `check_own_overlap` in `data/store/app/database/crud/stock/store_dataset_entry.py`;
the POST path calls it through `_check_overlap` in `data/store/app/ingest/data_action_request.py`, and the panel manager
through the same check, as the cause `own_overlap`). `feed` is identity but not an overlap term, and the
range-equality exclusion ignores it too. So one owner may hold a SIP and an IEX dataset for the same symbol,
granularity and window; a request naming IEX over a window that **partly** overlaps an owned SIP dataset is a
**409** with `colliding_ids`; and a request over an **identical** window on the other feed is accepted as a second
dataset, because a different feed is a different identity and so not an exact repeat.

**There is no route to extend a dataset.** The 409 hands the caller an id, and the store's crud
layer has a growth-only, id-preserving range update (`update_entry`) — but no HTTP route calls it at
the pin, so the collide-then-extend sequence stops at the 409. Over HTTP, a dataset's range cannot be
changed in place today.

## Ownership on writes

Every entry has an `owner`, the caller's **declared principal**. It is a required field on the
create body and has no default. On `DELETE /store/{id}` it is an optional query parameter; a missing
or wrong owner answers **403**, and the body never names the real owner. Reads are open: `owner` is
returned by S2 and is a filter there, not a restriction.

Read the warning at the top of this document again before building on this. The owner check guards
against a caller's mistakes, not against anyone holding the instance secret.

`owner` is kept out of the text the store renders, in three layers (`common/sensitive.py`):

| Where | How |
|---|---|
| A request model's `repr()`/`str()` | the field is declared with `SensitiveStr`/`OptionalSensitiveStr`, which leaves it out |
| The bound parameters of a SQL statement, as SQLAlchemy's error text, engine echo and uvicorn's traceback render them | the column is `SensitiveString` (`common/database/sql_alchemy_sensitive_string.py`), which binds each value as a `RedactedStr` whose `repr()` is `<redacted>`; the driver still sends and stores the real value |
| The entry row's `repr()` and the search filter's log line | both print `<redacted>` in place of the value |

`model_dump()` and JSON are unchanged, since the open read returns `owner`. **Two renderings still
carry it:** Postgres's own `DETAIL` text on a constraint violation, which the server writes before
SQLAlchemy sees it, and any `str()`, `format()` or f-string of the value itself — `RedactedStr`
overrides `repr()` only. A pydantic `ValidationError` also renders the rejected input. Because the
open GET returns `owner` anyway, this is log hygiene, not secrecy.

## Bars are raw; adjustment is not built

The bar row carries no `split_factor` and no `dividends_factor`. Bars are stored raw, as the vendor serves them; a repeat fetch refreshes a bar in place on its natural key,
and nothing else changes it. Corporate actions are stored in their own table (`corporate_action`, collected as the vendor
publishes them and read through PR8), **but the adjustment calculation and its cache do not exist**: nothing applies an action to a bar. Do not read the absence of the columns as "adjustment is handled": every bar this surface
returns is unadjusted.

## The feed on a bar

Every bar carries a required `feed` (`IEX`, `SIP`, or `NOT_APPLICABLE` for a source with no tape
distinction). There is no `UNKNOWN` member. The Alpaca handle resolves the feed **per request**
(`resolve_feed` in `data/ingest/app/brokers/alpaca/broker_api.py`):

| The request names | Served on |
|---|---|
| no feed | `SIP`, the constant `ALPACA_DEFAULT_FEED` |
| `SIP` | `SIP` |
| `IEX` | `IEX`, only because the request named it |
| `NOT_APPLICABLE` | refused `FEED_NOT_AVAILABLE` (422 at Submit, `PERMANENT`, no attempt row) |

There is no setting and no environment read: the default is a constant. The feed a bar and its dataset
record is the feed that was requested or resolved and sent to the vendor on every call, because Alpaca's
bars response reports no feed. SIP history is served from 2016 on the Basic plan; no code constant pins that start. As an observation, not a guarantee: on the owner's key, AAPL's SIP
daily history starts 2016-01-04 and its IEX daily history starts 2020-07-27 (an observation from a probe). SIP's recent edge is refused, never clamped silently: a
request whose end, or whose start when it has no end, is later than now minus 16 minutes (`SIP_RECENT_WINDOW`
15 minutes plus `SIP_EDGE_MARGIN` 1 minute, `data/ingest/app/brokers/alpaca/read.py`) is refused
`RANGE_TOO_RECENT` with the `reset_at` at which the same request becomes servable. An open-ended SIP
request is sent with that edge as its end, and `served_range` names it. IEX is never edge-checked.

---

# Conventions across the surface

These hold on every route unless the entry says otherwise.

## Error bodies: RFC 9457 problem+json

**Every error this surface returns on purpose is problem+json.** The full catalogue — every reason,
its HTTP status, its gRPC code and whether it is retryable — is [`docs/errors.md`](errors.md), which
is **generated** by `make errors-doc` from the single `Reason` table in `common/errors`. CI fails on
a stale copy. Do not hand-edit it, and do not restate the per-reason table here; this section gives
only the rules a client needs.

- **`type` is always `about:blank`,** and `title` is the HTTP status phrase — which is what RFC 9457
  asks for when the type is `about:blank`. No type URI namespace is published, so there is no URL to
  resolve and nothing to branch on in either member.
- **An error's identity is (`domain`, `reason`), with `domain` always `trader-joe`.** Clients branch
  on **`reason`** and on nothing else — never on `detail`, never on `title`. `detail` is human text
  and is never parsed. A client must survive a reason it has never heard of.
- **`Retry-After`, `retry_after` and `reset_at` appear together,** whenever the error carries a time
  at which the condition is expected to clear. `retry_after` is derived from `reset_at` at the moment
  the answer is written, so a relayed error never carries a delay that went stale in transit. The
  header and the body member always say the same number.

**One known gap: an unknown or misspelled symbol is not an error.** It is served empty — HTTP
**200**, `data_points` 0, and a `served_range` covering the window that was asked for. That is
deliberate, not an oversight. Because `served_range` is in
the response body, the gap is at least *visible* to a client: a caller that needs to tell "no such
symbol" from "no data in this window" must compare what it asked for against what it was served,
and cannot rely on an error.

| Convention | Behaviour |
|---|---|
| **Timezone-aware datetimes only** | Every datetime a caller sends — the create body's `start`, `end` and `expiry`; S2's `start`, `end`, `created_at`, `updated_at`; S4's bar `timestamp`; and S5's `start`, `end` — must carry an offset or `Z`. A naive value is a **422** naming the field (type `timezone_aware`). It is refused, never assumed to be UTC. |
| **Unknown fields and parameters are refused** | Request models reject what they do not declare (`extra='forbid'`), so a misspelt field or query parameter is a **422** (type `extra_forbidden`) rather than silently ignored. On S2 and S5 this holds for query parameters because the query model is bound with `Query()`. A strict receiver means that when a field is **added** to a contract, the receiving side must deploy first. |
| **Enum names on the wire** | `expiry_type` and `update_type` travel as member **names** — `BULK`, `BUFFER_1K`, `BUFFER_10K`, `BUFFER_100K`, `ROLLING`; `STATIC`, `DAILY`, `STREAM` — and the OpenAPI document declares them as string enums of those names, with defaults `BULK` and `STATIC`. An integer is still accepted on input but is undocumented. Over gRPC these travel as protobuf enums, declared in `proto/trader_joe/proto/market/v1/enums.proto`. |
| **No orphan OpenAPI components** | The store app prunes any `components.schemas` entry no `$ref` reaches, so the leftover integer-enum components `ExpiryType` and `UpdateType` are no longer in the document. A client generated from it gets one type per component actually used. |
| **Instance secret before schema validation** | On a guarded route the 401 is answered before path, query and body values are validated against their models, so an unauthenticated request with, say, a bad path value or a body that fails the schema is a 401, not a 422. **Except for a body that is not valid JSON:** FastAPI parses the JSON body before it runs the secret dependency, so an unparseable body is a 422 whether or not the secret is present. |
| **One database session per request** | Every store route gets its session from the `async_db` dependency (`data/store/app/database/database.py`), which opens it through `PostgresSessionFactory.AsyncSessionHandle.session()` and closes it when the request ends, including when the request raises. Closing rolls back any transaction still open and returns the connection to the pool. There is no `get_session` and no task-scoped registry behind it. |
| **Database errors** | A write rolls back once, is logged once at ERROR with its traceback, and **re-raises the original SQLAlchemy error**, which the error handlers render as a 500 problem+json with an `error_id`. |

**Status codes the store routes can answer by design:**

| Status | Meaning | Routes |
|---|---|---|
| 401 | Instance secret missing, wrong, or not configured on the server. One fixed message for all three causes. | S1, S3, S4, W1, W2, W3 |
| 403 | The declared `owner` does not own the entry (a missing owner is treated as a wrong one) | S3 |
| 404 | No entry or dataset with that id | S3, S6, U4, U5, U6, U7, U9, W1, W2, W3 |
| 409 | The create overlaps the same owner's existing dataset; the top-level `colliding_ids` member of the problem+json lists them | S1 |
| 422 | Validation: a naive datetime, an unknown field or parameter, a missing required field, a bars query naming no selector or a blank symbol, a `validate_fields` rule on the create body, an `asset_type`/`data_type` pair other than `stock`/`market_activity` on the internal asset-data path, or a malformed single-bar body; a `STREAM` create or a naive `ready_by` on S1; a coverage read for a dataset no trading calendar covers (U7); on S1, a non-stock `asset_type` (`UNSUPPORTED_ASSET_TYPE`: data_ingest refuses the bars request when it is submitted, after the name-change check has run) | all |
| 409 | `OVERRIDE_REQUIRED`: the dataset belongs to another owner and the operator override is missing, unconfirmed or blank. The body never names the owner | W1, W2, W3 |
| 503 | `DATABASE_UNAVAILABLE`: the audit tables are missing and the write needs one (run `make migrate`) | W1, W2, W3 |
| 500 | Anything unmapped, such as a database error | all |

---

# routers/common

## C1 · `GET /ping`

| | |
|---|---|
| **Stability** | **Stable.** REST stays for admin, debug and health. |
| **Auth** | none |
| **Implementation** | `routers/common/ping.py :: ping` |
| **Request** | none |
| **Response** | undeclared — no `response_model`; the handler returns an ad-hoc dict |
| **Touches** | nothing — no Postgres, no gRPC, no broker |

**Contract: a 2xx status.** Treat the status as the contract and not the body. `docker-compose.yaml`
makes this route the container healthcheck for *both* services, and the probe reads and discards the
response body — so a body assertion pins a string nothing reads.

It is served by both applications but declared once, here, because the manifest enumerates routers
and nothing enumerates applications. One interface, one implementation, one entry.

Because the handler touches nothing, a 2xx from it says only that the web server is up. Each container's
healthcheck also probes its gRPC listener's health service after this route, and `make prod-launch` and CI's
`up --wait` block on both. See gap **G3**.

## C2 · `/latency/{latency_type}`

| | |
|---|---|
| **Stability** | **Stable.** It outlived the transport it was built to measure; see below. |
| **Auth** | none |
| **Declared at** | `routers/common/app_endpoints.py :: InterfaceRest.LATENCY` |
| **Implemented in** | `routers/common/latency.py`, inside `initialize_latency_client()` |
| **Request** | `schemas.common.latency.LatencyRequest` |
| **Response** | undeclared — ad-hoc dict |
| **Touches** | a gRPC channel to data_ingest, and HTTP to the service's own `/latency_internal` |

**This is the one interface in this document whose existence depends on an environment variable.**
The router is built *inside* `initialize_latency_client()`, which runs only when
`LATENCY_TEST_ENABLED` is set. That flag is off by default and production runs with it off, so the
manifest records the path as `unbound-path`. Here that kind means *conditionally mounted, off in
production* — **not** *never written*. The code exists.

**Before re-enabling this anywhere, know what it exposes.** The handler takes an unauthenticated,
caller-supplied `payload_size` and `iterations`, allocates `os.urandom(payload_size * 1024)`, and
fans that many requests out concurrently. That is a request amplifier, not merely an idle endpoint.

**What it measures.** The harness compares REST with gRPC: `LatencyRequest.LatencyType` is exactly
`REST` and `GRPC`, and `routers/common/app_endpoints.py` declares no `InterfaceRpc`.

## C3 · `/latency_internal`

| | |
|---|---|
| **Stability** | **Stable**, with C2 — it exists only as C2's echo target. |
| **Auth** | none |
| **Declared at** | `routers/common/app_endpoints.py :: InterfaceRest.INTERNAL_LATENCY` |
| **Implemented in** | `routers/common/latency.py`, inside `initialize_latency_server()` |
| **Request** | `schemas.common.latency.InternalLatencyRequest` |
| **Response** | undeclared — ad-hoc dict |
| **Touches** | nothing beyond the request — it is a plain FastAPI POST handler that echoes the payload |

Same environment-variable gate as C2, same category: written, switched off. One guard and one
initialiser pair control both.

---

# routers/data_store

Every route in this component reaches Postgres. `POST /store/...` also reaches data_ingest over gRPC,
and is the only interface in the component that crosses two external boundaries.

## S1 · `POST /store/{asset_type}/{data_type}/{asset_symbol}`

| | |
|---|---|
| **Stability** | **Stable.** Its outbound hop to data_ingest is a gRPC call. |
| **Auth** | **instance secret** (`X-Instance-Secret`), fail-closed; 401 otherwise |
| **Implementation** | `routers/data_store/asset_dataset_store.py :: store_data` |
| **Request** | `schemas.data_store.asset_dataset_store.StoreAssetDatasetPath` + `...StoreAssetDatasetBody` |
| **Response** | `schemas.data_store.asset_dataset_store.StoreAssetDatasetResponse`: `message`, `data_points` (bars written) and `served_range` |
| **Touches** | Postgres (`AsyncSession`) **and** gRPC (`common.rpc.clients.ingest_submit.IngestSubmitClient`) |

This is the write path, and the result comes back on a push, not on the call. `store_market_activity_worker`
(`data/store/app/ingest/data_action_request.py`) refuses an own-overlap, writes the request row, submits
it to data_ingest (`IngestService.Submit`, [I1](#i1--ingestservice-and-the-push-result-path-grpc)) and waits for the verdict, up to
`STORE_VERDICT_DEADLINE_SECONDS` (default 300 s). Ingest reports each attempt to the store's own
`StoreService.DeliverAttempt`, and the delivery writes the dataset entry, the bars and the `ingest_run` row in one
transaction. A POST that outlives the deadline answers the deadline error and the request keeps running; its
outcome is in the run history (U6).

**The body is the dataset's identity.** `owner` (required, no default), `source`, `granularity`,
`start` (required), `end` (optional; omitted means open-ended), `expiry_type` (default `BULK`) and
`update_type` (default `STATIC`), plus `expiry` (optional; omitted or `null` means the dataset
**never expires**). `feed` is optional (`IEX`, `SIP` or `NOT_APPLICABLE`): it is a request to the handle, which resolves the tape (see [The feed on a bar](#the-feed-on-a-bar)), and the entry records the resolved one. The entry is resolved as described in
[What a create does](#what-a-create-does): an exact repeat reuses the existing entry, an overlap with
the same owner's dataset is a **409** carrying `colliding_ids`, anything else creates.

**`expiry` defaults to none**, so a dataset never lapses unless the caller sends an explicit `expiry`. Expiry is not
identity, so an exact repeat of a request reuses the entry and **sets its expiry from the new body**: a
repeat POST that omits `expiry` clears a stored one, and a repeat that carries an `expiry` sets it. An
expiry is the deletion date only and retires nothing, on any update type.

**`collect` (optional boolean) is the subscription's collecting intent, an additive field of the public
contract**. It applies to `DAILY` and `STREAM` only; on a `STATIC` request it is a
422 (*collect applies to subscriptions only*). On a create an omitted `collect` is `true`. On an exact
repeat an **omitted `collect` leaves the stored value unchanged** and only an explicit one changes it, so
a client retrying its original body cannot undo an operator's Stop. This deliberately differs from
`expiry`: collect is the operator's intent, expiry is the requester's own deletion date.

**Validation, all 422:** `update_type` must be `STATIC` when `end` is given, and must be `STATIC` when
`expiry_type` is `BULK`; and a given `end` must be later than `start`, since ranges are half-open and
an empty or inverted one can never be filled. The messages name members, e.g. *"The 'update_type'
field must be 'STATIC' when 'end' is provided."*

**A `STREAM` create is refused** with a 422 (reason `INVALID_REQUEST`) while streaming is not built.
The refusal runs on this body only, so a dataset already stored as `STREAM` still reads, and `STREAM`
is still accepted as a filter on S2.

**`ready_by` is the caller's deadline; `best_effort` is carried and unused.** `ready_by` is optional and must
be timezone-aware (a naive value is a 422). Ingest checks it when it takes the request off its queue: a request
taken after `ready_by` is reported `EXPIRED` and never sent to the vendor (see [`scheduler.md`](scheduler.md)), and the
POST answers that as a failure. No `ready_by` means no deadline. `best_effort` is a boolean, default `false`, that
nothing reads. Neither is part of the dataset's identity, and neither appears on S2 or S6.

**Every call is recorded as a request and its attempts.** S1 writes an `ingest_request` row before it
sends anything (trigger `REQUEST`), submits it, and answers from that row. The result comes back on
the push path: the delivery intake writes the bars, the dataset entry (on the first delivered
attempt) and **one `ingest_run` row per reported attempt**, all in the delivery's transaction.
**Every POST first sends its `name_change` clamp query**, a corporate-actions request for the symbol
(see [What a create does](#what-a-create-does)), so the first attempt a POST records is that query's.
The runs are read through U6. What S1 publishes to U8, each frame after the commit it announces:

| Case | Frames, in order |
|---|---|
| Served | the clamp's attempt frame (`EMPTY` when the vendor holds no name change; labelled with the symbol alone, no dataset), then ONE bars frame, `SUCCEEDED`, naming the dataset and the request, then `DatasetChanged` `UPDATED` |
| The vendor fails | the clamp's frame, then one `FAILED` bars frame per attempt (trigger `REQUEST`, then `RETRY` for each later one), no dataset; the POST answers the failure |
| The feed is refused (`FEED_NOT_AVAILABLE`, a 422) | the clamp's frame only: the request is refused at Submit and fails `PERMANENT` with no attempt row, so there is no bars frame and no `DatasetChanged` |

S1 writes no `RUNNING` row. A `RUNNING` row left in the table is marked `FAILED` with reason `INTERRUPTED` at the next start.

**Two things a retry does not avoid.** An exact repeat reuses the entry and the bar upsert is
idempotent on the bar's natural key, so the stored data does not duplicate — but the vendor fetch is
repeated on every call, and a batch carrying two bars at the same timestamp fails the whole write with
a 500. A batch is written in chunks sized under the tighter of two argument limits — asyncpg's
32,767 per statement, below Postgres's own 65,535 — all inside one transaction: it lands whole or not
at all.

`start` is required on the body, so a missing one is a 422 at the edge.

It is the highest-value interface in this component: the cost of a duplicate call is a wasted vendor
fetch. See gap **G10** (idempotency).

## S2 · `GET /store/{asset_type}/{data_type}/{asset_symbol}`

| | |
|---|---|
| **Stability** | **Stable.** REST stays for read and debug. |
| **Auth** | none |
| **Implementation** | `routers/data_store/asset_dataset_store.py :: get_data` |
| **Request** | `...StoreAssetDatasetPath` + `...StoreAssetDatasetQuery`, bound with `Query()` |
| **Response** | `list[schemas.data_store.asset_dataset_store.AssetDatasetStore]` |
| **Touches** | Postgres |

Lists the dataset entries for one symbol, each with its `item_count` (bars held, 0 for an entry with
none) and its own `expiry`. Returns a **list**; to read a single entry by id, use `GET /store/{id}` (S6).

Optional query filters, each an exact match on the entry's column: `owner`, `source`, `feed`, `granularity`,
`start`, `end`, `expiry_type`, `update_type`, `created_at`, `updated_at`. The datetimes must be
timezone-aware; `expiry_type` and `update_type` take member names. `feed` takes `IEX`, `SIP` or `NOT_APPLICABLE`. An unknown parameter is a 422. It has no `limit` or `offset`; see gap
**G5**.

It is the one route on this surface that declares its response through a **return annotation** rather
than the `response_model=` keyword. Both are equally binding to FastAPI and the manifest records them
identically, so do not read the difference as significant — it is noted only so that grepping for
`response_model=` does not make this route look undeclared.

## S3 · `DELETE /store/{id}`

| | |
|---|---|
| **Stability** | **Stable** as REST. Its *address* is contested — see the note below. |
| **Auth** | **instance secret** (`X-Instance-Secret`), fail-closed; then the owner check |
| **Implementation** | `routers/data_store/asset_dataset_store.py :: delete_data` |
| **Request** | `schemas.data_store.asset_dataset_store.AssetDatasetStoreDelete` — path `id`, query `owner` |
| **Response** | **undeclared** — ad-hoc dict |
| **Touches** | Postgres |

Deletes one entry and, by `ON DELETE CASCADE`, exactly that entry's bars, then publishes a
`DatasetChanged` `DELETED` on U8. The dataset's run history is not deleted with it, but it is no longer
reachable: U6 answers 404 for an unknown dataset. **401** without the
secret; **403** when `owner` is missing or does not match the entry's owner (the body does not reveal
the real one); **404** for an unknown id. `owner` is optional at the schema level on purpose: an
absent owner is the degenerate case of a wrong one and gets the same 403, not a 422. That refusal
holds only because `store_dataset_entry.owner` is `NOT NULL`; if that column ever became nullable, a
missing owner would start matching rows.

**Address.** `GET /store/{id}` is served at the same path as this route (S6). They are different
methods on one declared path, and the manifest holds each as its own line.

## S4 · `POST /internal/asset-data/{asset_type}/{data_type}`

| | |
|---|---|
| **Stability** | **Fate undecided — do not guess.** See below. |
| **Auth** | **instance secret** (`X-Instance-Secret`), fail-closed; 401 otherwise |
| **Implementation** | `routers/data_store/internal_asset_data.py :: create_stock_market_activity_data` |
| **Request** | `schemas.data_store.asset_data_interface.AssetDataPath` + **an untyped `dict` body** |
| **Response** | `schemas.data_store.stock.market_activity_data.StockDataMarketActivity` |
| **Touches** | Postgres, plus that untyped body |

Writes one bar. The handler persists it inside the store's write transaction, refreshes the row and
returns it, including its `feed`.

**The body is an untyped `dict`** at the route signature, passed as
`StockDataMarketActivityCreate(**asset_data)` inside the handler. That is why the manifest's
`touches` field for this route reads `dict` — there is no schema to name. The model it is splatted
into is strict: it requires `dataset_id` and `feed`, requires a timezone-aware `timestamp`, and
rejects unknown fields, so a renamed producer field is an error rather than a missing column. A body
that fails it is a **422** in FastAPI's usual shape, each `loc` prefixed with `body`: the handler
re-raises pydantic's error as a request validation error. As on every guarded route, the secret is
checked first, so without it the answer is a 401. See gap **G7**.

**The path is validated as a pair.** `AssetDataPath` is bound with `Path()` and refuses any
`asset_type`/`data_type` pair outside `SUPPORTED_ASSET_DATA_PAIRS` — today only `stock` with
`market_activity` — with a **422** naming the supported pairs. OpenAPI still lists every enum value
for each field, since a cross-field rule is invisible there.

**Why the fate is undecided and nobody should guess it:** nothing in the repository calls this route.
It may count as REST kept for admin and debug, but it is also exactly the data_store↔data_ingest write
path that the gRPC surface carries. Confirm before building on it.

## S5 · `GET /internal/asset-data/{asset_type}/{data_type}`

| | |
|---|---|
| **Stability** | **Stable.** The one bar read outside `/ui/v1` (U5 is the UI's); there is no unfiltered read beside it, and REST stays for read. |
| **Auth** | none |
| **Implementation** | `routers/data_store/internal_asset_data.py :: read_stock_market_activity_data` |
| **Request** | `schemas.data_store.asset_data_interface.AssetDataPath` + `schemas.data_store.stock.market_activity_data.StockDataMarketActivityQuery`, bound with `Query()` |
| **Response** | `list[schemas.data_store.stock.market_activity_data.StockDataMarketActivity]` |
| **Touches** | Postgres |

**The filtering bar read.** Query parameters, each optional and each an exact match when given:

| Parameter | Filters on |
|---|---|
| `dataset_id` | the one dataset the bars belong to |
| `asset_symbol` | the symbol (uppercased before matching) |
| `source` | the vendor |
| `feed` | the tape: `IEX`, `SIP`, `NOT_APPLICABLE` |
| `granularity` | the bar size |
| `start`, `end` | the bar `timestamp`, **half-open** (`timestamp >= start`, `timestamp < end`): a bar stamped exactly at `end` is not returned and a bar at `start` is; timezone-aware only |

**A selector is required.** A query must name `dataset_id` or `asset_symbol`; one naming neither is a
**422** (loc `['query']`, type `value_error`), so an unbounded read of the whole table cannot be
expressed. A **blank** `asset_symbol` (empty or whitespace only) is refused the same way, even when
`dataset_id` is given — it names no symbol. A padded but non-blank symbol is not trimmed.

**Other 422s, all before the handler runs:** an unknown parameter (type `extra_forbidden`), and a
naive `start` or `end` (type `timezone_aware`). A bar has no expiry, and a nested model cannot be a query
parameter, so sending `expiry` or `query` is an unknown-parameter 422.

**Order:** `ORDER BY timestamp, dataset_id`, and nothing else. Without `dataset_id`, bars from every
dataset of the symbol interleave in timestamp order and the same minute can appear once per dataset —
duplication is per dataset by design. Every returned bar carries its `dataset_id` and `feed`, so the
rows stay distinguishable.

It still has no `limit` or `offset`: a selector bounds the read to one symbol or one dataset, not to
a page. See gap **G5**.

An unsupported `asset_type`/`data_type` pair is a **422** here too, from the same `Path()`-bound
`AssetDataPath` as S4.

**Each call is recorded as one read of every dataset it returned bars of** (and a query by `dataset_id`
that returns none records the dataset with 0 bars). The reader is the optional `X-Client-Id` header; see
[the read label](#the-x-client-id-read-label). A read records no more than that and never changes the
answer. `GET /store/...` returns dataset entries, not bars, and records nothing.

## S6 · `GET /store/{id}`

| | |
|---|---|
| **Stability** | **Stable.** |
| **Auth** | none |
| **Implementation** | `routers/data_store/asset_dataset_store.py :: get_data_by_id` |
| **Request** | `schemas.data_store.asset_dataset_store.AssetDatasetStoreGetById` — path `id` |
| **Response** | `schemas.data_store.asset_dataset_store.AssetDatasetStore` |
| **Touches** | Postgres |

One dataset entry by id, shaped like one element of S2: the entry whole, plus `item_count` (bars held,
0 for an entry with none). An unknown id is a **404** problem+json (reason `NOT_FOUND`). It shares its
path with S3 and answers a different method; the manifest records the two as separate lines.

---

# routers/data_store — the UI routes

The web UI is served under `/ui/v1` by twenty-two read routes (fourteen dataset, usage and storage reads, then the eight panel reads PR1 to PR8), one socket and six writes (the dataset writes W1, W2, W3 and the panel writes PW1, PW2, PW3). The reads are open like the other reads. The six writes need the instance secret. In a deployment the web container's proxy is what a browser reaches: it forwards the reads and exactly those six writes, and adds the secret itself. The panel routes are in *The panel routes* below.
See the README's *The web UI*.

**Protobuf canonical JSON, not Pydantic.** The response of every `/ui/v1` route is the protobuf
canonical JSON of a message declared under `proto/trader_joe/proto/ui/v1/`: lowerCamelCase field
names, enums by name, `int64` as a string, timestamps as RFC 3339 `Z`. The `.proto` file is the schema;
OpenAPI records only the message's full name under the `x-proto-message` extension, and the manifest
pins it (`trader_joe.proto.ui.v1.DatasetPage`, and so on). A client decodes with the generated schema
and ignores a field it does not know, since proto changes are additive. The rendering is
`routers/common/proto_json.py`: a handler returns `ProtoJSONResponse(message)`, a route declares its
message with `proto_route`, and a body, when a route takes one, is parsed with `ProtoBody`, which
refuses an unknown field or a non-object with a 422 that names the message type and never echoes the
body. Only the writes (W1, W2, W3, PW1, PW2, PW3) take a body, and W2's is optional.

**Common behaviour.**

| Topic | Rule |
|---|---|
| Ranges | Half-open `[start, end)`: `start` included, `end` excluded. Datetimes must be timezone-aware. |
| Paging | Keyset, never offset. `next_cursor` is opaque and empty on the last page; pass it back as `cursor`. A dataset added or removed between pages never makes a client see one twice or skip one that was ahead. A cursor that does not decode, or was issued under another sort, is a 422. |
| Unknown parameter | A 422 (`extra_forbidden`), not ignored. |
| Enum filters | Take the member name, in any case, with or without the wire prefix: `source=ALPACA_API` and `source=DATA_SOURCE_ALPACA_API` are the same filter. |
| Errors | problem+json as everywhere (see the conventions): 404 `NOT_FOUND` for an unknown dataset, 422 for a bad parameter. |
| Credentials | The reads carry none. The writes need the instance secret (`X-Instance-Secret`). `owner` and the read label `client_id` are free-text labels, never keys. |
| The read label | A bar read (U5, S5) is recorded under the caller's `X-Client-Id` label. See [the read label](#the-x-client-id-read-label). |

### U1 · `GET /ui/v1/config`

What the shell needs to know about the deployment: `UiConfig` with `allowedGroups` (only
`ACCOUNT_GROUP_SIMULATION`), `deploymentLabel` (display only; empty when unset, and nothing may
branch on it; from `DEPLOYMENT_LABEL`) and `serverVersion` (`SERVER_VERSION`, else the installed
package version, else `unknown`). Nothing secret is read or returned.

### U2 · `GET /ui/v1/datasets`

One page of the catalog, a `DatasetPage` of `DatasetSummary` items: identity, range, `updateType`,
`expiryType`, `expiry` (the deletion date only), `state` (`ACTIVE`, or `RETIRED`: a `DAILY` or `STREAM` dataset whose collect intent is false), `collecting` (derived for every update type: the intent for a subscription; for a `STATIC` dataset true only while a run is in flight), the stored extent (`firstBar`, `lastBar`,
`barCount`), `health`, the `siblings` (the same series at other granularities) and `lastRun`: the
dataset's newest collection run, whatever its state, unset when it has never run (an `IngestRun`, see
U6), `usage` (a `DatasetUsage`, always set, see [Usage on the list](#usage-on-the-list-the-facets-and-the-detail)) and
`reaperPending`: true when the dataset's expiry has passed and the reaper would delete it (true in
dry-run too, where nothing is deleted; see U10).

**Running means a run is in flight and ingest has acknowledged it.** `lastRun.state` is `RUNNING`, and
a run is attached to a dataset only from ingest's acknowledgement, so a first-time request is not
visible as Running while the vendor is still answering. Running is not a health status.

| Parameter | Meaning |
|---|---|
| `asset_symbol` | symbols starting with this, case-insensitive |
| `source`, `update_type` | exact, by member name |
| `status` | the UI group: `healthy`, `late`, `failed` or `retired` |
| `needs_attention` | only late and failed datasets |
| `running` | `true`: only datasets with a run in flight. Orthogonal to `status`: a dataset can be Late and Running at once |
| `sort` | `symbol` (default); `expires`: soonest first, no-expiry datasets last; or `last_read`: most recent read first, never-read last, by the selected clients |
| `usage` | `READ_30D`, `STALE` or `NEVER_READ`, a **filter** over the selected clients (the `USAGE_FILTER_` prefix and any case are accepted) |
| `stale_days` | a **threshold**, not a filter: how many days unread makes `STALE`; 1 to 3650, default 90 |
| `expires_before` | a **filter**: the Expiring Soon view, the datasets whose expiry is before this timezone-aware instant, plus every dataset the reaper would delete |
| `client_id`, `client_kind` | select **whose reads** the usage figures and the `usage` and `last_read` rules count; see the usage section below |
| `cursor`, `limit` | page; `limit` 1 to 500, default 100 |

**Dataset health is computed on read, never stored**, over the source's trading calendar (a holiday or an
early close moves what is expected). A source with no calendar has no computed health: its status is
unspecified and it matches no `status` filter.

| Update type | Status | Meaning |
|---|---|---|
| `DAILY` | `FRESH` | the last completed session in range is stored |
| `DAILY` | `LATE` | not stored yet and the next session has not opened; pending, not a failure |
| `DAILY` | `OVERDUE` | not stored by the next session's open; a failure |
| `STREAM` | `FRESH` / `OVERDUE` | in hours, last bar within a few bar-lengths of now (`HEALTH_STREAM_BAR_MULTIPLE`, default 3); outside hours, relative to the last close |
| `STATIC` | `COMPLETE` | every session in `[start, end)` has at least one bar |
| `STATIC` | `GAPS` | some do not; `gapCount` is the number of missing sessions |
| `DAILY` or `STREAM`, with `collect` false | `RETIRED` | no health is computed; an expiry does not retire a dataset |

The UI groups these as Healthy (`FRESH`, `COMPLETE`), Late (`LATE`), Failed (`OVERDUE`, `GAPS`) and
Retired (the UI says Stopped). Completeness counts sessions, and a session is covered when a bar covers its period: the session
for 1min to 1day, the Monday-Sunday week for 1week, the calendar month for 1month, in the trading calendar's
time zone. A weekly or monthly dataset therefore reads `COMPLETE` when every expected period has its bar, and
`gapCount` stays a count of sessions. Only the sessions inside `[start, end)` are counted.

### U3 · `GET /ui/v1/datasets/facets`

The sidebar's counts as a `DatasetFacets`, under the same filters U2 takes minus the page. Each count
equals the total of the list it describes: `all` is the total with every filter except
`needs_attention`, `needsAttention` is the total with every filter and `needs_attention` forced on,
and each source, update type and status count is the total with every filter except that facet's own.
`running` is the number of datasets with a run in flight, under every filter except `running` itself,
so with `running=true` it equals the list's total.

The facets add the usage counts, under the same client filter and `stale_days`: `stale`, `read30d` and
`neverRead` (each equals the total of the list filtered by the matching `usage` value), `expiringSoon`,
`clients` (every client that has read anything, with the datasets each read in the last 30 days; it
ignores the client filter, so a picker never empties itself) and the echoes `clientIds`, `clientKinds`,
`staleDays` and `expiresBefore`. `expiringSoon` is expiry-based and ignores the client filter. The
facets route takes `expires_before` like the list; when it is absent the server uses now plus seven days.

### U4 · `GET /ui/v1/datasets/{dataset_id}`

One `DatasetSummary`, as in U2, with `lastRun`. It takes the client filter (`client_id`, `client_kind`) and nothing else; its `usage` is counted under it. **404** for an unknown id.

### U5 · `GET /ui/v1/datasets/{dataset_id}/bars`

One `BarPage` of the dataset's bars, ascending by bar start, in `[start, end)`. `start` and `end` are
each optional, and unset leaves that side open. `limit` is 1 to 50000, default 1000 (the Viewer keeps reading 1,000-bar pages, and the larger maximum is for
programmatic readers and the Viewer's CSV export, which reads 50,000 at a time). The cursor is the
last bar's timestamp, unique within a dataset, so paging is exact while bars are inserted. An empty or
inverted window is an empty page, not an error. **404** for an unknown dataset, 422 for a bad cursor.
Bars are raw (unadjusted), as on S5.

**A call without a `cursor` is recorded as one read** of the dataset, labelled by the `X-Client-Id`
header (the web UI sends `operator`, which makes it a UI read; a missing or invalid header is
`_unattributed`). A call **with** a cursor records nothing, so paging through or exporting a dataset is
one read, not one per page. Recording is buffered in the server and never changes the answer.

### U9 · `GET /ui/v1/datasets/{dataset_id}/bar-pages`

The page index of a bar window: where each fixed-size block of the window starts, so a client can
read any page of U5 with one index seek instead of walking a cursor from the start. One
`BarPageIndex`: `bar_count` (bars in the window), `page_size`, `overlap` (both echoed, so the answer
is self-describing) and `pages`, oldest first, each a `BarPageStart` of `start` (the page's first
bar) and `offset` (its 0-based position among the window's bars).

| Parameter | Rule |
|---|---|
| `start`, `end` | the window, exactly as U5: optional, timezone-aware, half-open `[start, end)` |
| `size` | 1 to 1000, default 1000. 422 `INVALID_REQUEST` outside that |
| `overlap` | 0 to `size` - 1, default 50. 422 `INVALID_REQUEST` outside that |

**The page rule.** Let N be the bars in the window and r = N mod `size`. Blocks of `size` bars are
counted back from the **newest** bar, so the newest page is always full and the remainder is at the
old end. The offsets, oldest first, are `[0 when r > 0]`, then r, r + `size`, ..., N - `size`, which
is M = ceil(N / `size`) pages. N = 0 has no pages and N <= `size` is the one page `[0]`. The earliest
page, when r > 0, holds a full `size` bars and so overlaps its newer neighbour; every page holds the
same number of bars.

`overlap` is a look-ahead and never moves an offset: page k is read with U5 using
`start = pages[k].start`, the window's own `end`, and `limit = size + overlap`, which adds the first
`overlap` bars of the next (newer) block; the newest page has none. Whatever `overlap` is, the pages
and offsets are the same.

**Offset 0 is absent from the JSON.** Protobuf canonical JSON omits a field at its default, so the
earliest page's `offset` is missing and a client reads a missing `offset` as 0. A client that
indexes `pages[k].offset` without that default reads `undefined`.

**404** for an unknown dataset; **422** `INVALID_REQUEST` for a `size` or `overlap` out of range, a
naive `start` or `end`, or a parameter the route does not know. An empty or inverted window is an
empty index (`bar_count` 0, no pages), not an error, as in U5.

**Cost.** Two statements: the dataset's entry, then one read that scans the window twice (a count and
the ranking of its bars). Measured on Postgres 17 with one-minute bars: about 30 ms at 100,000 bars,
0.3 s at 1,000,000 and 0.7 s at 2,400,000. The Viewer pays it once per From or
To change, never per page. The index is a snapshot: bars appended to an open-ended window do not
show until the next read of it.

### U6 · `GET /ui/v1/datasets/{dataset_id}/runs`

One `IngestRunPage` of the dataset's collection runs, **newest first**: keyset on `(started_at, id)`
with an opaque `next_cursor` (empty on the last page), so a run that starts while a client pages never
shifts a page it has not read. `limit` is 1 to 500, default 50. **404** for an unknown dataset, 422
for a cursor that does not decode.

An `IngestRun` carries `id`, `datasetId` (empty when the attempt names no dataset: a clamp query, or a POST's attempt that failed before any delivery created the entry), `label` (symbol and
granularity), `trigger`, `actor` (the request body's `owner`, a label and never a credential), the
requested `start` and `end`, `state` (`RUNNING`, `SUCCEEDED`, `EMPTY` or `FAILED`), `barsWritten`,
`error` (set only when `FAILED`: `domain`, `reason`, `message`; see below), `startedAt` and `finishedAt` (unset
while `RUNNING`). `trigger` is the request's own trigger on its first attempt (`REQUEST` for a POST, `SCHEDULE` for the daily
refresh) and `RETRY` on each later attempt of either. `requestId` names the request the attempt belongs to (empty on a legacy run); `quotaKey` is always empty. A run swept at startup reads
`FAILED` with `reason` `INTERRUPTED`, which is a marker of the run record and not a member of the
error vocabulary in [`errors.md`](errors.md).

**The error object of a failed attempt.** A `FAILED` attempt reported on the push path carries `error.domain`
`trader-joe` and `error.reason` equal to the vocabulary reason ingest put on `DeliveryTrailer.reason`. `domain` and `reason` are both **empty** when the failure is untyped: ingest
sent no reason (an asset the broker does not know, and the crash path where the attempt runner itself raised), and the
store never derives a reason from `error_class`. They are empty on a non-`FAILED` attempt too, where `error` is unset.
The store's own markers outrank the report and keep an **empty** `domain`: `INTERRUPTED` (a run swept at startup) and
`VENDOR_ASSET_CHANGED` (a bars delivery the store refused because its vendor asset id differs from the dataset's last
accepted one). A client branches on `reason` and treats an empty one as
"no typed reason".

### U7 · `GET /ui/v1/datasets/{dataset_id}/coverage`

A `CoverageStrip`: the dataset's range over its trading calendar, bucketed for a strip, and its gaps.
Computed on read, never stored, from the same calendar and completed-session rule as the dataset health
status (a session counts as covered when it has at least one bar). It records no usage.

| Part | Rule |
|---|---|
| `buckets` (query) | at most this many; default 120, 1 to 400, and 422 above 400. Fewer when the range holds fewer sessions |
| `start`, `end` | the range, half-open: the dataset's `start` and `end` as `DatasetSummary` reads them at `asOf` |
| `buckets` (field) | contiguous groups of near-equal session counts that tile `[start, end)`. `expectedSessions` counts completed sessions only, so a bucket with none is not yet due; `sessionsWithBars` is how many of those hold a bar |
| `gaps` | at most the first 500, ascending. A gap is a maximal run of missing completed sessions; weekends and holidays never split or form one |
| gap window | **whole sessions**: `start` is the local midnight of the first missing session, `end` the local midnight of the day after the last. It can begin before or end after the dataset's range when the range starts or ends mid-session; buckets are clipped to the range, gaps are not |
| `gapCount` | the number of gap **intervals**, which may exceed `gaps`. `DatasetHealth.gapCount` (U2) counts missing **sessions**; for a `STATIC` dataset the sessions of all gaps add up to it |

**404** for an unknown dataset. **422** (`INVALID_REQUEST`) when no trading calendar covers the dataset
(its source has none, or its range starts before the calendar does): an error, never an empty strip,
which would read as no gaps. A read costs two statements. A retired `DAILY` or `STREAM` dataset is
computed over its whole range, because when its retirement began is not recorded.

### U8 · `WS /ui/v1/events`

The UI's one live socket. Each frame is one text message of protobuf canonical JSON of a `UiEvent`
(`serverTime`, and one of the oneof members). The route is open, like the GETs, and takes no inbound
message: anything a client sends is read and ignored.

**Events are notifications, not data.** A client treats each one as a hint to reload over REST, and
reloads on every reconnect, so a frame that is dropped or missed is never the only copy of anything.
The event is published after the commit it announces.

| Event | Published when |
|---|---|
| `IngestRunChanged` (carries the `IngestRun`) | once per reported attempt, after the delivery that wrote its `ingest_run` row commits: `SUCCEEDED`, `EMPTY` or `FAILED`. A POST announces its `name_change` clamp's attempt and then its bars attempts (S1 lists the cases); a refused feed announces the clamp's frame only. No `RUNNING` frame is published |
| `DatasetChanged` `UPDATED` | after the delivery that resolved the dataset commits (S1's bars attempt, a daily refresh's), unless it flipped the collect intent, and after W1 commits |
| `DatasetChanged` `RETIRED` / `RESUMED` | after W3 commits a change of collect to false / true, after S1's delivery flips it the same way, or (`RETIRED`) when the store stops a dataset by its own rule or the panel manager stops an exited member. A W3 that leaves the stored value as it was publishes nothing |
| `DatasetChanged` `DELETED` | after S3, W2 or a reaper deletion commits |
| `ReaperPendingChanged` | the reaper's pending set differs from the previous pass's, after any deletions of an enabled pass, and once on the first pass after start; in dry-run too. A notification: reload U10 or the affected lists |

`DatasetChanged` is **never `CREATED`**: a new dataset arrives as `UPDATED`,
because the upsert does not tell a create from a repeat. `RequestChanged`
is in the proto and typed, and **nothing publishes it**: it is reserved. An attempt frame is published
after the commit that wrote the entry it names, so a reload by id finds it.

**One process, one worker.** The hub that fans events out lives in data_store's memory, so the socket
is correct only while data_store runs one worker; `docker-compose.yaml` pins `SERVICE_WORKERS=1`. With
more, an event would reach only the clients connected to the worker that published it.

**A slow client is dropped, not waited for.** Each client has a bounded queue,
`EVENTS_CLIENT_QUEUE_SIZE` events (default 256). When it fills, the client is closed with WebSocket
code **1013** (try again later) and is expected to reconnect and reload.

**Through the web container's proxy** the upgrade is a `GET` under `/api/store/ui/v1/`, which is what
Caddy forwards, so the socket passes with no change to the filter. The dev proxy forwards it with
`ws: true`.

## The operator writes

The web UI's two writes. Both need the instance secret (`X-Instance-Secret`, a 401 otherwise); the web container's proxy adds it, so a browser never holds it. Bodies are
protobuf canonical JSON. Neither route changes the owner-scoped `/store` writes (S1, S3), which are
unchanged. As everywhere in this document the secret authenticates the deployment, not the caller.

**The operator override.** A dataset belongs to an `owner`. When that owner is not `operator`, the
write must carry `override: {reason, confirmed}` with a non-blank `reason` and `confirmed: true`; the
reason is stored trimmed. An override sent for the operator's own dataset is ignored.

### W1 · `PATCH /ui/v1/datasets/{dataset_id}/expiry`

Body `SetExpiryRequest`: `expiry` (a timezone-aware timestamp; **unset clears it**) and `override`.
The expiry type and update type are not writable, and an unknown field (`expiryType` included) is a 422.

| Answer | When |
|---|---|
| **200**, the updated `DatasetSummary` | the write committed |
| **422** | a malformed body, an unknown field, a naive timestamp |
| **404** | no such dataset |
| **409** `OVERRIDE_REQUIRED` | another owner's dataset and the override is missing, unconfirmed or blank. The body, detail and metadata never name the owner |
| **503** `DATABASE_UNAVAILABLE` | the dataset needs an override and the audit tables are missing (`make migrate`). Refused before any write. An operator-owned PATCH needs no audit table and goes through |

Rules run in that order: body, existence, override, audit tables, then the write. **The expiry is the
deletion date only, for every update type: setting it retires nothing and clearing it resumes nothing.**
It is stored as sent, so a past time makes the dataset pending for the reaper and nothing else. The write, and its override-log row when an override was used, commit in one transaction;
`DatasetChanged` `UPDATED` is published after the commit.

### W3 · `PATCH /ui/v1/datasets/{dataset_id}/collecting`

Body `SetCollectingRequest`: `collect` (**required**: a body without it is a 422, never read as `false`) and
`override`. `DAILY` and `STREAM` subscriptions only. `true` starts collecting, `false` stops it (the dataset
reads `RETIRED`, shown as Stopped, and the daily refresh skips it).

| Answer | When |
|---|---|
| **200**, the updated `DatasetSummary` | the write committed (a repeat of the stored value too) |
| **422** | a malformed body, an unknown field, a body with no `collect`, or a `STATIC` dataset (it has no collect intent) |
| **404** | no such dataset |
| **409** `OVERRIDE_REQUIRED`, **503** `DATABASE_UNAVAILABLE` | as W1; the override-log row records the action `SET_COLLECTING` |

Rules run in that order: body (a missing `collect` first), existence, `STATIC` refusal, override, audit
tables, then the write, in one transaction with its override-log row. After the commit it publishes
`DatasetChanged` `RETIRED` (collect went to false) or `RESUMED` (to true), and nothing when the stored value
was already that. The reaper is unaffected: it deletes at the expiry whatever `collect` says.

### W2 · `DELETE /ui/v1/datasets/{dataset_id}`

Deletes the dataset and its bars. The body is optional: the operator's own dataset needs none, and a
body that is sent is parsed strictly (`DeleteDatasetRequest`, `override` only).

| Answer | When |
|---|---|
| **204**, no body | deleted |
| **422**, **404**, **409** `OVERRIDE_REQUIRED` | as W1 |
| **503** `DATABASE_UNAVAILABLE` | the audit tables are missing. **Every** delete is audited, so it is refused before any write |

**The audit records are append-only tables, never updated or deleted from.** `dataset_deletion` gets
one row for every deletion (the operator's, and the reaper's, whose cause and actor are `REAPER` and
`reaper`); `dataset_override_log` gets one row for each write that used an override, so the reaper,
a system action, writes none. Neither has a foreign key, so a record outlives its dataset. Runs and
usage rows likewise outlive a deleted dataset but are no longer reachable through it.
`DatasetChanged` `DELETED` is published after the commit.

## The expiry reaper

An expired dataset is deleted by a periodic pass of the store (`REAPER_INTERVAL_SECONDS`, default 3600).
It keeps no job list: after a restart the next pass reads the database again. **It is dry-run unless
`REAPER_DELETES_ENABLED` is the value `true`** (any letter case, surrounding whitespace ignored; unset, `1`,
`yes` and anything else stay in dry-run). In dry-run it logs a count and deletes nothing. The
default is dry-run because datasets created under the former default, which was an expiry one day after
the request, are all past expiry: read U10 before enabling deletes. An enabled pass deletes each
pending dataset in its own transaction, records a `dataset_deletion` row for it, skips a dataset with a
fetch in flight until the next pass, and deletes nothing at all while the audit tables are missing.

### U10 · `GET /ui/v1/reaper/pending`

A `ReaperPendingPage`: the datasets the reaper would delete (or is deleting), as `DatasetSummary`
items with `reaperPending` true, oldest expiry first (keyset on expiry and id, an opaque `next_cursor`,
empty on the last page). `deletesEnabled` is false in dry-run. `cursor` and `limit` (1 to 500,
default 100) are the only parameters; a cursor that does not decode is a 422. A stopped `DAILY` or
`STREAM` dataset is pending only once its expiry has passed, like any other; the reaper ignores `collect`.

## The `X-Client-Id` read label

Usage is recorded per reader, and **a reader identifies itself with the `X-Client-Id` request header
on a bar read** (S5, and U5 without a cursor). This is part of the public client contract.

| Rule | Detail |
|---|---|
| What to send | Your **owner string**, the one you use on the dataset write routes. Send the same value on every read |
| What it is | A **label** for usage statistics. It is self-asserted and **never authentication**: nothing is allowed or refused on it, and it is not a security boundary. Anyone can send any value |
| Valid values | 1 to 128 characters, no control characters, not starting with `_`. Anything else is recorded as `_unattributed`, never truncated, and the read still succeeds |
| Kind | `UI` if and only if the label equals the operator principal, `operator`, which is what the web UI sends. Every other label, `_unattributed` and `_overflow` included, is kind `SDK` |
| Reserved | `_unattributed` (header missing, empty, too long or malformed) and `_overflow`. A caller cannot send either |
| The cap | At most 32 distinct labels are kept per dataset per UTC day. Later labels that day are recorded as `_overflow`. `operator` and the reserved values are exempt |
| Not on writes | A write route neither reads nor accepts it |
| Not the `owner` filter | The `owner` query parameter on `GET /store/...` searches by owner and is unrelated to the label |

The label is never logged and is kept out of reprs. It does appear in the responses of the usage
routes, as `client_id`.

## Usage on the list, the facets and the detail

The usage fields come from the bar reads recorded under the label above. This section separates **what
selects readers**, **what is a threshold**, **what is a filter** and **what is a window**.

| Parameter | Role | Default |
|---|---|---|
| `client_id` | selects **whose reads count**: exact ids, repeatable; **wins** over `client_kind` | none; at most 32 ids |
| `client_kind` | selects whose reads count: `UI`, `SDK` (the `CLIENT_KIND_` prefix and any case accepted), repeatable | `SDK` when neither is sent |
| `stale_days` | a **threshold**: unread this long is stale | 90 (1 to 3650) |
| `usage` | a **filter** on the list and facets: `READ_30D`, `STALE`, `NEVER_READ` | none |
| `expires_before` | a **filter** (Expiring Soon) | none on the list; the facets use now plus seven days for their `expiringSoon` count |
| `source`, `dataset_id` | **filters** on the statistics routes | none |
| `days` | a **window** (reads per day) | 30 (1 to 365) |

**The echo rule.** Every response that applies a client filter echoes what was applied, and exactly
one of `clientIds` and `clientKinds` is non-empty: `clientIds` when `client_id` was sent, otherwise
`clientKinds`, written out as `[SDK]` by default.

`DatasetSummary.usage` is a `DatasetUsage`: `lastReadAt` (the latest read by the selected clients; unset
means never), `reads30d` (their reads in the last 30 days), `readBy30d` and `readByClients30d` (who
read it in the last 30 days, **regardless of the filter**, so a screen can say "UI only") and the echo.

**The definitions are day-aligned** on the UTC date of the last read, and a stream session counts as a read:

| Group | A dataset is in it when |
|---|---|
| `READ_30D` | the selected clients read it within the last 30 UTC days |
| `STALE` | they did not read it within the last `stale_days` UTC days; **never-read is stale** |
| `NEVER_READ` | they never read it |

`READ_30D` and `STALE` are complements at `stale_days=30`. The list, facets and statistics decide through
one rule, so for one filter a facet count equals the filtered list's total and the stale counts of the
summary, the facets and the stale list agree. On a database that has not run the usage migration, usage
reads as zero.

## The usage statistics

Five open GETs under `/ui/v1/stats`, each a protobuf message, none recording usage. Every usage route
takes the client filter above (`client_id`, `client_kind`) and echoes it; the storage route takes none,
because what the store holds does not depend on who read it. An unknown parameter is a 422.

### U11 · `GET /ui/v1/stats/usage/summary`

A `UsageSummary`: `stale`, `readLast30d`, `neverRead`, `total`, `readsToday`, `distinctClientsToday`
(today is the UTC date), the echo and `staleDays`. Parameters: `stale_days` (threshold) and `source`
(filter).

### U12 · `GET /ui/v1/stats/usage/last-read-buckets`

`LastReadBuckets`: datasets by whole UTC days since the last read, in the half-open buckets
[0,1), [1,8), [8,31), [31,91) and [91, open), plus `neverRead`; the buckets and `neverRead` sum to the
total. Parameter: `source` (filter).

### U13 · `GET /ui/v1/stats/usage/reads-per-day`

`ReadsPerDay`: one entry per UTC day, client id and kind with a read, ascending by day. Per-read rows
and the older daily rollup are one series, so a day split across the rollup boundary is one entry.
Parameters: `days` (the window, today included), `dataset_id` and `source` (filters).

### U14 · `GET /ui/v1/stats/usage/stale`

A `StaleDatasetPage`: the stale datasets, never-read first, then the oldest last read, then id. Each row
carries the `DatasetSummary`, `estBytes` (an estimate) and the last read per client. Parameters:
`stale_days` (threshold), `source` (filter), `cursor` and `limit` (1 to 500, default 100). The cursor is
opaque and bound to `stale_days`, `source` and the client filter; one issued under another is a 422.

### U15 · `GET /ui/v1/stats/storage/current`

A `StorageCurrent`: what the bar table holds now, with no history. `bySource` splits it per source and
feed as **estimates** (each share is the bars' count times the table's average row size). Without
`source`, `totalBytes` is the table's measured size; with a `source` it is the sum of that source's
shares, so the split sums to it. Parameter: `source` (filter) only.

**Known limit:** a source with more than about 16,000 datasets exceeds the database's bound-parameter
limit on the usage statistics. Request-quota use is not served.


## The panel routes

A **panel** is a named selection of one source's assets whose datasets the store's panel manager creates and keeps (see [`runbooks/panel-manager.md`](runbooks/panel-manager.md)). Panels are UI-only: these eleven routes are the whole panel surface, with no client or SDK panel API. Messages are declared in `proto/trader_joe/proto/ui/v1/panels.proto`; paths in `UiPanelsInterface` and `UiPanelWritesInterface` (`routers/data_store/app_endpoints.py`), handlers in `routers/data_store/ui_panel.py`.

`{panel_id}` and `{dataset_id}` are UUIDs (a malformed one is a 422, an unknown one a 404). `{source}` is the `DataSource` member name, with or without the `DATA_SOURCE_` prefix; `UNSPECIFIED` is a 422. As on every `/ui/v1` route, an unknown query parameter is a 422.

### The reads (PR1 to PR8)

Open, read-only, and none records usage. Paged reads take `cursor` and `limit` (1 to 500, default 100); a cursor is opaque and bound to its route and parent, and a foreign or undecodable one is a 422.

| Id | Route | Answer (200) | Notes |
|---|---|---|---|
| PR1 | `GET /ui/v1/panels` | `PanelList` | Every panel by name then id, with its Bar Interval (`granularity`) and the enabled flag of its latest settings version. No parameters. |
| PR2 | `GET /ui/v1/panels/{panel_id}` | `PanelDetail` | The panel with its Bar Interval (`granularity`), its current settings and source settings versions (each with its effective pass) and its counts. Member health is judged at the request time. 404 for an unknown panel. |
| PR3 | `GET /ui/v1/panels/{panel_id}/settings-versions` | `PanelSettingsVersionPage` | Newest first, keyset on the version number, each with its effective pass. 404 for an unknown panel. |
| PR4 | `GET /ui/v1/panel-sources/{source}/settings-versions` | `PanelSourceSettingsVersionPage` | Newest first. A source with no version is an empty page, never a 404: that is how the create form learns whether it must offer the source's first settings. In a version's `PanelSourceSettings` an absent `rampDatasetsPerHour` or `requestCapPerHour` key means no limit; a zero never appears. |
| PR5 | `GET /ui/v1/panels/{panel_id}/boundaries` | `PanelBoundaries` | Every marker in `[start, end)`, oldest first: settings versions and operator collect stops and resumes. `start` and `end` are both required, timezone-aware, `start` before `end` (a 422 otherwise). 404 for an unknown panel. |
| PR6 | `GET /ui/v1/panels/{panel_id}/members` | `PanelMemberPage` | Keyset over (symbol, vendor asset id); each member with its dataset's last bar and health. 404 for an unknown panel. |
| PR7 | `GET /ui/v1/panels/{panel_id}/asset-list-history` | `AssetListHistory` | One entry per session in `[start, end)`, ascending, never per asset, with whether a day is due or missing. `start` and `end` required, `start` before `end`, at most 366 days. A 422 for a bad window or a source with no trading calendar; 404 for an unknown panel. |
| PR8 | `GET /ui/v1/datasets/{dataset_id}/corporate-actions` | `CorporateActionEventPage` | The dataset's corporate-action observations, ordered by the date each takes effect, newest first: `ex_date`, else `effective_date`, else `process_date`, with undated events last, then `as_of` newest first, then id newest first. A vendor revision is its own item. Matched by the dataset's source and symbol, whichever request delivered them, and not filtered by any window: every stored event is shown. The cursor is opaque and its width is not a contract; a cursor from another route or dataset, or a malformed one, is a 422. 404 for an unknown dataset. |

### The writes (PW1 to PW3)

POST only: each appends a row and a repeat appends another, so none is PUT or PATCH. Each needs the instance secret (`X-Instance-Secret`, a 401 otherwise) and takes a body parsed strictly (an unknown field or a missing required one is a 422). Each answers **201** with the new row. The actor is the operator principal. The `reason` is **optional**: a blank one (after trimming) means none, is stored as NULL and reads back as an empty string, so a client that has no reason sends none or an empty string.

**The version row is the audit**: it is append-only and carries who, why and when, so a panel write adds no `dataset_override_log` row and needs no override. Nothing is published on the events socket, since there is no panel event type; the web UI re-reads over REST. A create does not start a pass: the panel manager's next tick runs the first.

| Id | Route | Body | 201 answer | Refusals |
|---|---|---|---|---|
| PW1 | `POST /ui/v1/panels` | `CreatePanelRequest`: `name` (required, trimmed, unique), `source` (required, fixed for the panel's life), `granularity` (required, the panel's Bar Interval, fixed for the panel's life: `GRANULARITY_ONE_MINUTE`, `GRANULARITY_ONE_HOUR` or `GRANULARITY_ONE_DAY`, the full enum member name in a body, no default; the reads PR1 and PR2 answer the same spelling), the first settings as flat fields (`enabled` required; `cadence`, unspecified means the default MONTHLY; `allowSymbols`, `denySymbols`, `assetClass`, `exchange`, `active`, `tradable`, `includeOtc`; a plain boolean, an absent value is false), an optional `reason`, and optionally the source's first settings (`rampDatasetsPerHour`, `requestCapPerHour`, `corporateActionWindowPreDays`, `corporateActionWindowPostDays`; each may be left out). On a source with **no settings version** the create always writes that source's version 1 from whatever these four carry: an absent rate is no limit (stored NULL), an absent window day count is 90, and all four absent writes no limits and 90/90 | `PanelDetail`, version 1, `effectivePass` unset and zero counts | 422: a bad body, **a missing, UNSPECIFIED or other `granularity`** (any value but the three above; nothing is written), **a body carrying `size`** (the field is gone: an unknown field is refused, see below); a blank name; a name already used (creates of one name are serialised, so the loser of two concurrent creates is refused); a negative number; a ramp under 1 (so 0) or a cap under 2, or a value above 2147483647; `includeOtc` false with `exchange` OTC (it would select nothing); source settings (any of the four fields) given for a source that **already has a version** (they change only through PW3), which includes the case where a concurrent create wrote the source's version 1 first even though the caller saw no version: save the settings through PW3 instead. Nothing is written |
| PW2 | `POST /ui/v1/panels/{panel_id}/settings-versions` | `SavePanelSettingsRequest`: the complete new settings (replaced as sent, an absent optional filter clears it; `enabled` and `cadence` required; `includeOtc` is a plain boolean, so an absent value is false) and an optional `reason`. Enable and disable are this write with `enabled` changed | `PanelSettingsVersion`, `effectivePass` unset | 422 for a bad body (one carrying `size` included) or a store refusal, `includeOtc` false with `exchange` OTC included; 404 for an unknown panel |
| PW3 | `POST /ui/v1/panel-sources/{source}/settings-versions` | `SaveSourceSettingsRequest`: the four numbers and an optional `reason`. The two rates are optional: absent means no limit (stored NULL), a present `rampDatasetsPerHour` is 1 to 2147483647 and a present `requestCapPerHour` 2 to 2147483647 (each new panel dataset costs two requests, so a cap of 1 could never create one); zero is refused. The two window day counts are still required (0 to 2147483647) | `PanelSourceSettingsVersion`, `effectivePass` unset. It applies to every panel on the source | 422 for a bad source, body or store refusal, including a ramp under 1, a cap under 2 or a missing window day count |

Rules run in order: the secret (401), the body (422), existence (404, PW2 only), the store's refusals (422), then the write. Every store refusal is `INVALID_REQUEST` with the store's message.

**A panel has one Bar Interval.** `granularity` is set on create and never changes, like `source`: no write takes it, so another interval is another panel (the same symbols can be held by a 1 Minute and a 1 Day panel at once, each with its own datasets). The panel's datasets are `DAILY` subscriptions at that granularity, and the daily refresh keeps them current once a session close, so intraday bars arrive after the close and a minute panel holds data up to the most recent completed session once its post-close refresh has landed, and up to the session before until then. A creation and a refresh make the same number of vendor requests per dataset whatever the interval, but each bars request costs one market-data token per vendor page of up to 10,000 bars: a one-session tail (at most about 960 one-minute bars) is one page at every interval, while a 1 Minute tail longer than about ten sessions (after an outage, a resumed dataset or a member deferred for days) takes several pages. The ramp, the request cap and the refresh's per-pass cap are shared by every panel on the source.

**A panel has no size.** It selects **every** asset of its source's newest active list that passes its filters, ordered by symbol then vendor asset id (the order members are created in under the ramp). There is no cap and no per-panel count: the field was removed from the three messages (field 3 is reserved), so a body that still carries `size` is a 422 like any unknown field. To limit what a panel holds, use its filters (allow and deny lists, asset class, exchange, active, tradable, include OTC). `includeOtc` is off by default: with it off an asset whose exchange is exactly OTC is not selected, because the current market-data feed (SIP, the consolidated tape of US exchanges) carries no bars for OTC securities. The read message `PanelSettings` carries it. To **pause** a panel, save a version with `enabled` false: that stops its passes only. Its members stay as recorded and their datasets keep collecting through the daily refresh; stopping them as well is a backlog item ("Panel: disabling a panel should pause its members' datasets"), not built. Each member's dataset starts at the local midnight, in the source's trading calendar, of the local date of the member's entry: the snapshot the panel first knew it from. The first pass therefore starts every dataset at the snapshot's local date (an evening New York snapshot lands on that New York day, not the UTC day), a later entrant starts at its own entry date, and a member that exits and re-enters keeps its dataset and start. No earlier history is requested. A filter that matches nothing makes every member exit, which stops those datasets unless another enabled panel selects them.

**The reason is optional end to end.** Every `reason` field on the wire is still a plain string: empty means none. The store trims it and stores a blank one as NULL (the one spelling of none), and every read answers an empty string for it. A version or marker with no reason shows no reason text in the web UI (the history strip reads "By <actor>" alone), never a placeholder. The actor stays required. The override reason on a dataset write (the `override` object on the dataset writes) is a different field and is still required.

---

# routers/data_ingest

data_ingest declares **no HTTP interface at all**, and `data_ingest.manifest` says so with a single
`none` line. Its interface is gRPC: the `IngestService` it serves, and the `StoreService` it calls.

## I1 · `IngestService` and the push result path (gRPC)

| | |
|---|---|
| **Stability** | **Stable.** |
| **Auth** | none |
| **Contract** | `proto/trader_joe/proto/internal/ingest/v1/ingest.proto` (the calls store makes on ingest) and `proto/trader_joe/proto/internal/store/v1/store.proto` (the calls ingest makes on store) — the source of truth; the Python stubs under `gen/proto/python/` are generated by `make proto` and never committed |
| **Implementation** | `routers/data_ingest/submit_handler.py :: IngestSubmitHandler` and `routers/data_ingest/instance_handler.py`, registered in `data/ingest/app/grpc_host.py :: registered_services`; the store side is `data/store/app/ingest/delivery_intake.py` |
| **Touches** | the broker handles (one per vendor, each with its own queue, executor and outgoing buffer, see [`scheduler.md`](scheduler.md)) |
| **Pinned by** | `data/ingest/tests/test_grpc_host.py`, which asserts in a fresh interpreter that `registered_services()` returns exactly this service |

**`IngestService` has three methods; two are served.** All three are calls from data_store to data_ingest.

| Method | Shape | State |
|---|---|---|
| `Submit` | unary | Served. Hands ingest one request of any kind (bars, asset list, corporate actions) and is answered **at once**. It is idempotent on `request_id`: an id ingest already holds is answered again and queued once. |
| `Instance` | unary | Served. The restart marker: the id this ingest process minted at start, and when it started. |
| `Quote` | unary | Declared, with no server and no caller (`proto/README.md`): every call is answered `UNIMPLEMENTED`. |

**The Submit answer is an ack, never data.** Bars: accepted with the **resolved feed** (never
`FEED_UNSPECIFIED`) and the quota key, or refused for its feed (`FetchRefused`, reason `FEED_NOT_AVAILABLE`,
the one refusal carried in-band). Asset list and corporate actions: accepted with `FEED_UNSPECIFIED`, since
they have no tape, and refused only by a gRPC status. Every other failure on the hop is a gRPC status with a
`google.rpc.Status` and `ErrorInfo`. What is knowable before the vendor is checked before the ack.
The `SubmitRequest` envelope carries `request_id`, `owner`, `ready_by`, `best_effort`, `recovered` and
`attempt_count`; `ready_by` unset means no deadline, and a set one is checked when the request is taken off the queue.
`recovered` marks a re-submission after an ingest restart, and `attempt_count` is the store's durable count, so a restart
never resets the attempt budget.

**The result comes back on `StoreService`**, which data_store serves and data_ingest calls:

| Method | Shape | What it does |
|---|---|---|
| `DeliverAttempt` | client-streaming | One call per attempt. In order: ONE `DeliveryHeader`, then zero or more payload messages (`bar_page`, `asset_records` or `corporate_action_records`, all of the one kind the request was), then ONE `DeliveryTrailer`. A stream that ends without a trailer is incomplete: the store writes nothing for it and ingest redelivers the whole attempt. The answer is the acknowledgement, `DeliverAttemptResponse` (`verdict` `CONTINUE` or `FINAL`, and the store's durable `attempt_count`). |
| `RegisterInstance` | unary | Ingest announces the instance id it minted at start and when it started. Idempotent; retried by ingest with backoff and never blocks its start. |

**The acknowledgement gate is part of the contract.** The store writes the attempt record, any data, the durable
attempt count, the summed spend and the verdict in one transaction, commits, and only then answers. Ingest runs no retry
and makes no further vendor call for a request until its report is acknowledged. A report that does not reach the
store, or whose answer does not reach ingest, is **redelivered with the same attempt number**: that is not a broker
retry and costs no vendor call. `DeliverAttempt` is idempotent on `(request_id, attempt_no)`, and a duplicate is
answered from the stored row and writes nothing. A report from a superseded instance is still written (its data is
valid at its receipt time) and answered `FINAL`.

**How an attempt ends** (`DeliveryOutcome` on the trailer):

| Outcome | Meaning |
|---|---|
| `SUCCEEDED` | The vendor answered and the payload, possibly empty, is complete. |
| `FAILED` | A vendor call was made and the attempt did not succeed. `error_class` says how: `TRANSIENT`, `RATE_LIMITED` or `PERMANENT`. |
| `EXPIRED` | Taken off the queue after its `ready_by`, so never sent to the vendor: no stats, no payload, no attempt row; the store writes the request `FAILED` with cause expired. |
| `EXHAUSTED` | Arrived with its attempt budget already spent, so never sent to the vendor; shaped like `EXPIRED`, and the store writes the request `FAILED` with cause attempts exhausted. |

**The trailer's typed reason.** `DeliveryTrailer.reason` is the contract text, quoted from `store.proto`: "The
vocabulary Reason value of a FAILED attempt (common/errors; the domain is always the vocabulary's ERROR_DOMAIN, so it
is not carried). Empty on any other outcome, and empty when ingest has no typed reason. Additive. A receiver that does
not recognise the value treats it as empty and never refuses the delivery: a refused delivery is redelivered and lost
at an ingest restart." The store records it on the attempt row and U6 shows it (see [U6](#u6--get-uiv1datasetsdataset_idruns)).

**The feed on the header.** For a bars attempt the header's `feed` is the resolved tape; for an asset list or
corporate actions it is always `FEED_UNSPECIFIED`. An `EXPIRED` or `EXHAUSTED` report of a bars request still names
the resolved tape. Downstream, `feed` is part of the dataset entry's identity, which is why an IEX
request and a SIP request over the same window are two datasets and not one.

**This entry is not in the manifest, and that is deliberate.** This document's inventory is
REST-shaped and keyed on path; a gRPC method has no path to key on. The generated package lives
outside `routers/` and each surface is pinned where it lives, so this one is pinned by its own test rather than by `routers/tests/interface_manifest`.

**Three asset classes are accepted; one is served, and the refusal is typed.** A non-`STOCK`
`asset_type` is refused up front with `UNSUPPORTED_ASSET_TYPE`, as are `QUOTE` and `TRADE` data
types. The surface *accepts* three classes and serves one, so gap **G8** stands.

**A vendor failure is never an empty result.**

`BrokerRead.get_bars` returns either a `BarsResponse` — served, possibly empty, carrying its
`served_range` — or a typed `BarsFailure`, and raises only for a bug. A typed failure is reported on the
trailer as `FAILED` with its `error_class` and its `reason`, and an empty result means an empty interval, and nothing
else. **No sentinel survives.**

Gaps **G1** and **G2** still apply to what happens *after* a failure is reported. G1 is narrowed:
U7 answers what a dataset holds and what is missing, per session. G2 is narrowed too: U6 shows each
failed run and its typed error. A failed attempt is retried by ingest within its attempt budget (see
[`scheduler.md`](scheduler.md)).

data_ingest declares **no REST interface at all** — not merely none that is bound: it has no
`InterfaceRest` enum. Its entire interface is the gRPC surface above.

---

# Not implemented: written, switched off

Nothing on the declared surface is declared and unserved. One situation remains, written code that is
switched off:

| Sense | Which | What it means for you |
|---|---|---|
| **Written, switched off** | C2, C3 | The code exists in `routers/common/latency.py` and mounts only when `LATENCY_TEST_ENABLED` is set. `unbound-path` here means *conditionally mounted, off in production*. The initialisers are also single-shot, so a client and a server cannot both initialise in one process. |

The `unbound-path` kind is empty for data_store and data_ingest and holds only the two latency
entries. `data_store.manifest` carries thirty-four `http` lines and one `ws` line; `data_ingest.manifest`
carries a single `none` line.

---

# Gaps in the interface

The limits of the surface as it stands. Each is labelled **LIMIT** (a reader might expect otherwise, and the
surface does not do it) or **OUT OF SCOPE** (not built, by scope).

## G1 · The older read routes say nothing about completeness — **LIMIT**

S2 and S5 return datasets and rows and nothing about what is missing: an absent interval is ambiguous, and on an
IEX-only tape empty intervals are normal. U7 answers coverage per dataset, computed on read over the trading
calendar. It reports which sessions hold at least one bar, not what was fetched.

## G2 · No caller-initiated retry — **LIMIT**

No route retries a failed fetch. Every store-driven fetch is recorded as an ingest run with its typed error, and
U6 and U8 expose it. A run's history is per dataset, so a request that failed before any dataset existed is listed
under no dataset.

## G3 · No readiness check beyond `/ping` and the gRPC health service — **LIMIT**

`GET /ping` touches nothing: no Postgres, no gRPC, no broker. Each service's container healthcheck probes it and
then the gRPC listener's standard health service (`docker-compose.yaml`), and `make prod-launch` and CI's
`up --wait` block on that. Nothing probes Postgres or broker credentials, so a container with a dead database
pool or exhausted credentials can report healthy.

## G4 · No caller authentication on any route — **LIMIT**

The write routes require the deployment's instance secret, and nothing else on the surface requires anything. The
secret authenticates the deployment, not the caller: with one key there is one effective principal, and the
per-dataset owner check stops mistakes rather than anyone holding the key. Every other surface answers without
credentials. Prod publishing no host port and dev binding loopback narrow who can reach them; both are a
perimeter, not an identity.

- `GET /latency/{latency_type}` is an unauthenticated request amplifier when switched on (C2).
- `DELETE /store/{id}` is destructive and guarded only by the shared secret plus a declared owner that any key
  holder can name (S3).
- Every read, including the one that returns every entry's `owner`, is open.

## G5 · S2 and S5 do not paginate — **LIMIT**

S5 requires a `dataset_id` or `asset_symbol` selector, so the widest read it can express is one symbol across all
its datasets and all time. S2 is scoped to one symbol. Neither takes `limit` or `offset`. S2 returns dataset entries; S5 returns bars, so one liquid symbol at
minute granularity is a large S5 answer returned in one 200 response. The UI routes page: U2 and U5 take a keyset
`cursor` and a `limit`.

## G6 · Routes that declare no response model — **LIMIT**

`DELETE /store/{id}` (S3) and `GET /ping` (C1) return ad-hoc dicts, as do the latency pair (C2, C3). The generated
OpenAPI documents no response for them, and this document marks each such entry "undeclared" rather than
transcribing the dict shape as a contract.

## G7 · One untyped request body — **LIMIT**

`POST /internal/asset-data/{asset_type}/{data_type}` (S4) takes a plain `dict` and builds
`StockDataMarketActivityCreate` from it inside the handler, so the OpenAPI document describes no body and the
manifest's `touches` field for that route reads `dict`. The target model is strict (unknown fields rejected,
`dataset_id` and `feed` required), and a body that fails it is a 422 in the usual shape.

## G8 · The surface accepts three asset classes and serves one — **LIMIT**

`AssetType` has `STOCK`, `CRYPTO` and `OPTION`; `DataType` has `MARKET_ACTIVITY`, `QUOTE` and `TRADE`. Stock market
activity is the only pair served. The ingest handlers refuse any other asset type with `UNSUPPORTED_ASSET_TYPE`,
and S4 and S5 refuse an unsupported pair at the path model with a 422 before the handler runs. S1's path model
accepts any `asset_type`, and it is ingest that refuses it.

## G9 · Nothing here touches accounts, portfolios, orders or fills — **OUT OF SCOPE**

This is the market-data surface and only that. A reader arriving from the project description will look for an
account model, a portfolio model and an append-only order/fill event log, and will find a market-data pipeline.
Those surfaces are not built, by scope and not by omission.

## G10 · The write path is idempotent in data, not in work — **LIMIT**

`POST /store/...` (S1) is idempotent in what it stores: an exact repeat resolves to the same dataset entry, and the
bar upsert refreshes existing rows on the bar's natural key. It is not idempotent in what it does: every call
records a request and submits it to data_ingest. Concurrent identical vendor calls collapse onto one at ingest
(single-flight, `data/ingest/app/brokers/single_flight.py`); a sequential repeat fetches again.

## G11 · Versioning covers the UI paths only — **LIMIT**

The `/ui/v1/...` routes carry a version in the path. `/store/...`, `/internal/asset-data/...`, `/ping` and
`/latency/...` carry none, and there is no version header, so a change to those lands under the same paths.

---

# Keeping this document honest

When this document and the manifest disagree, **the manifest is right**. File a bug against this
document.

**The manifest test makes the surface impossible to change silently; it does nothing to make this
document move when the surface does.** That gap is routing, not detection. This document pins no SHA,
because a regroup of a branch rewrites every SHA on it; it cites a file and symbol per entry, both of
which survive a regroup.

Until something mechanical exists, the working rule is: **a diff that touches `routers/`, `schemas/`,
an interface enum, or `routers/tests/interface_manifest/` obliges a pass over this file before the
branch reaches a PR.**
