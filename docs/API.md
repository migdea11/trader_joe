# API reference — the declared interface surface

**This document describes `release/dataset-model` as of its docs pass for the dataset-model PR
(2026-09-29)**: the tree after the per-dataset bar model, owner-scoped writes, the filtering bar
read, per-request database sessions and owner redaction landed. **It pins no commit SHA.** This
branch is regrouped before review, and a regroup rewrites every SHA on it — both earlier pins
(`a96ede9`, then `5ed8eb1`) stopped resolving that way. What each entry cites instead is a file and
a symbol. Check those before trusting an entry: if the file or symbol an entry names no longer
exists, the entry is stale.

**Checked on real Postgres, with named gaps.** Every status code and behaviour below was checked in
process, against the code and its tests. On 2026-09-29 the owner ran the system suite
(`tests/system`, through `make test-system SYSTEM_TEST_DISPOSABLE_DB=1`) on the host against a
wiped, freshly migrated stack, twice, and it passed both times. The host verification record is
`tj-vhboky.14`. That suite covers, on real Postgres:

- revision `eec8f88a7443` as it stands, applied by `make migrate`, with the recorded revision equal
  to the single head;
- both unique constraints rejecting exact duplicates, and `owner` refusing an explicit NULL;
- the bar upsert keeping one row on a repeat and refreshing it;
- the delete cascade removing one entry's bars and leaving another entry's whole;
- an oversized batch landing whole in one transaction, and a failure on a later chunk leaving
  nothing;
- the instance secret rejecting a write over real HTTP, with nothing changed;
- the 409 on an overlap with the caller's own dataset;
- sequential reads beyond the connection pool's size all answering.

That run came **before** the owner-redaction change landed (`SensitiveString` on the `owner`
column, `RedactedStr` in its bound parameters). The suite has not been run against a stack built
with it.

**Not yet run on real Postgres:**

- the revision's downgrade, and its intended failure on a non-empty bar table (the downgrade waits
  on seeded data, `tj-vhboky.62`; CI does not run it either);
- the suite shown red against a deliberately broken schema;
- one real ingest from the vendor end to end, and query plans on real volume;
- the System Testing job in CI, whose first real run is this PR's.

Read those as designed and unit-tested, not as proven.

## Read this first

**Writes require the deployment's instance secret; reads require nothing.** The three write routes
(`POST /store/...`, `DELETE /store/{id}`, `POST /internal/asset-data/...`) reject any request that
does not carry the `X-Instance-Secret` header matching `INSTANCE_WRITE_SECRET`, with a 401. The
check **fails closed**: an unset or empty secret rejects every write. Every read route stays open.

**That secret authenticates the deployment, not the caller.** With one key there is effectively one
principal, so "only the owner may edit a dataset" is **not** enforced against anyone holding the key:
whoever has it may declare any `owner` they like. What the owner check buys is protection against
*mistakes* — an honest caller cannot accidentally overwrite or delete another principal's dataset. It
is not access control. It also does not close `tj-glqs4r`: every surface other than the three write
routes answers without credentials. What narrows who can reach them is the network model (decision
record `tj-q9ae5u`, addendum 1): prod publishes no host port, and data_store is reachable only from
containers on its internal networks, including any client on `store_api`; dev publishes on loopback
only. That is a perimeter, not an identity. `GET /latency/{latency_type}` is still an
unauthenticated request amplifier when switched on. See gap **G4**.

**This is the market-data surface, and only that.** Nothing here touches accounts, portfolios,
orders or fills. A reader arriving from the project description — a trading platform with an
append-only order/fill event log and account-type-aware turnover limits — will look for those
interfaces and not find them. They do not exist yet. That is roadmap (`tj-pznkbx`, `tj-qqdo3j`,
`tj-jmrqkf`), not oversight. See gap **G9**.

**The manifest is authoritative; this document is downstream of it.** The machine-checked surface
lives in `routers/tests/interface_manifest/*.manifest`, and `routers/tests/test_interface_surface.py`
asserts those files as an *equality* against the live code: it imports the router packages,
enumerates what they actually expose, and fails if the manifest and the code disagree in either
direction. Adding a route therefore costs a manifest line in the same diff, and there is deliberately
no regenerate command (user ruling, 2026-09-23: "Keep the manifest"). **This document has no such
test and can drift.** If you find a disagreement between the two, trust the manifest and file a bug
against this document.

## How to read an entry

**Entries are keyed on address, not on file path.** The monorepo split (`tj-iontkq`) moves the server
code under a `server/` prefix with its internal layout unchanged — it superseded the earlier
`src/trader_joe/` restructure, which was closed without being done — so every file path in this
document goes stale in one diff while the addresses survive. File and symbol are still given — they are what makes an
entry checkable — but they are marked *as of the dataset-model PR* and are deliberately never woven into prose,
so updating them is a field edit rather than a rewrite.

There is one place that rule breaks, and it is flagged inline at the entry: **R1's address is a Kafka
topic**, and a topic name does not survive the Kafka removal. That entry is re-keyed at the cutover
rather than replaced, so the inventory does not read as though a second interface appeared.

**One known blind spot in the manifest: matching is by path, not by method.** An interface enum
member carries a path and no method, so the manifest cannot tell `GET /store/{id}` from
`DELETE /store/{id}`. Binding the DELETE is enough to make the path count as bound, and the unserved
GET at the same path never reaches the manifest's `unbound-path` list. This is the honest limit of
what a declaration says, and it is the only known gap in an otherwise exact inventory — but a reader
who trusts "bound" without it will draw the wrong conclusion. See
[GET /store/{id}](#get-storeid--declared-unserved-and-invisible-to-the-manifest).

### Stability markers

Every entry carries one. This is the field that carries most of this document's value — a flat
reference table without it would be actively misleading, because most of this surface is scheduled to
change.

| Marker | Meaning |
|---|---|
| **Stable** | Expected to survive the migrations in flight, in substance. Its implementing file still moves at the monorepo split. |
| **Route survives, outbound hop changes** | Callers keep the same address; what the handler calls downstream is replaced. |
| **Address is re-keyed** | The address itself — the thing this document keys on — changes at the cutover. |
| **Dies with Kafka** | Scheduled for deletion along with the Kafka transport, and not before. |
| **Fate undecided** | Genuinely not yet decided. Do not guess, and confirm before building on it. |
| **Filed for implementation** | Decided and filed as work. Not built yet; the entry says which task owns it. |

Two markers that appeared in the first version of this document — *Proposed for deletion* and
*Proposed for implementation* — are gone. Both meant "the architect recommends, the user has not
ruled". **The user ruled on 2026-09-23**: the three deletion recommendations were taken, and the one
implementation recommendation was filed as `tj-2h1q3k`. Nothing on this surface is waiting on a
ruling any more. The deleted declarations are recorded under
[Declarations deleted by ruling](#declarations-deleted-by-ruling-2026-09-23) rather than dropped, so
the reasoning survives the deletion.

Every implementing file on this surface moves at the monorepo split (`tj-iontkq`), so that is not
repeated per entry.

## The surface at a glance

**Nine** interfaces are visible to the manifest as of the dataset-model PR, plus one declaration the manifest
cannot see (`GET /store/{id}`, per the method-blind matching limit above). Four of the nine are
stable. The count is taken from `routers/tests/interface_manifest/*.manifest` — three lines in
`common.manifest`, five in `data_store.manifest`, one in `data_ingest.manifest` — not from the prose
below.

| # | Address | Component | Kind | Auth | Stability |
|---|---|---|---|---|---|
| C1 | `GET /ping` | common | http | none | **Stable** |
| C2 | `/latency/{latency_type}` | common | unbound-path | none | **Dies with Kafka** |
| C3 | `/latency_internal` | common | unbound-path | none | **Dies with Kafka** |
| S1 | `POST /store/{asset_type}/{data_type}/{asset_symbol}` | data_store | http | instance secret | **Route survives, outbound hop changes** |
| S2 | `GET /store/{asset_type}/{data_type}/{asset_symbol}` | data_store | http | none | **Stable** |
| S3 | `DELETE /store/{id}` | data_store | http | instance secret | **Stable** |
| S4 | `POST /internal/asset-data/{asset_type}/{data_type}` | data_store | http | instance secret | **Fate undecided** |
| S5 | `GET /internal/asset-data/{asset_type}/{data_type}` | data_store | http | none | **Stable** |
| R1 | `stock_market_activity_rpc` | data_ingest | rpc | none | **Address is re-keyed** |
| — | `GET /store/{id}` | data_store | *invisible to the manifest* | n/a | **Filed for implementation** (`tj-2h1q3k`) |

**Why nine and not ten.** The last row is the one entry in this table that is not a manifest line,
and it is marked so. The manifest matches by path and not by method, so `GET /store/{id}` is counted
as bound by the `DELETE` at the same path and never appears as its own line. Every other row here is
a manifest line, one for one. That single discrepancy is deliberate, and it is the reason a count
taken from this table and a count taken from the manifest differ by exactly one.

**`unbound-path` is now a two-entry kind, and both are the latency pair.** That is the state the
kind was invented to describe: written code that mounts only under an environment flag. No
data_store or data_ingest declaration sits unserved any more.

**Four entries have left this surface since the inventory behind this document was taken.** One was a
real route: `DELETE /internal/asset-data/{asset_type}/{data_type}`, removed under `tj-h7ikz2` — handler,
enum member and manifest line in one diff, which is what the manifest's rule demands. The other three
were *declarations* that no code ever served, deleted under `tj-wc4pe8` and `tj-427x50` by user ruling; see
[Declarations deleted by ruling](#declarations-deleted-by-ruling-2026-09-23). If you are reading an
analysis or task note that says thirteen interfaces, it predates that route's removal; one that
says twelve predates the first deletion by ruling.

### The three kinds

- **`http`** — a FastAPI route registered on a module-scope `APIRouter` while the module body ran.
- **`rpc`** — a handler registered through `KafkaRpcFactory.add_server()` while the module body ran.
  Enumerating one is proof the registration actually happened at import; if it stops happening, the
  service starts and answers nothing.
- **`unbound-path`** — a path declared in an interface enum that no import-time route serves. This
  kind exists because the import-time surface is not the whole declared surface, and that gap was
  invisible before the manifest existed.

---

# The data model behind the store surface

Read this before S1–S5: every data_store route is a view onto two tables, `store_dataset_entry` (one
row per dataset) and `stock_market_activity` (the bars). The reasoning, including the options
rejected and the costs accepted, is the decision record `tj-vhboky.1`; this section states what the
code at the pin does.

## A bar belongs to exactly one dataset

`stock_market_activity.dataset_id` is a non-null foreign key to the entry with `ON DELETE CASCADE`,
and it **leads** the bar's natural key, `(dataset_id, asset_symbol, source, feed, granularity,
timestamp)`, constraint `uq_stock_market_activity_natural_key`. There is no membership table. Two
datasets that cover the same symbol and minute each hold **their own copy** of that bar; deleting a
dataset deletes exactly its bars by cascade.

**Why.** Sharing one bar row between datasets made a bar's ownership last-write-wins and turned every
delete into a question about who else referenced each row. Duplication makes a dataset's bars its
own, the delete a cascade, and a dataset-scoped read one contiguous index scan. The record turns on
one asymmetry: being wrong about duplication costs an index and a statement, recoverable at any time;
being wrong about sharing costs provenance that was never durable anywhere else.

**What it costs, measured on paper and not yet on a real database.** Storage crosses above the
shared design once bars average more than **1.26 covering entries**. The entry listing (S2) counts
bars through an outer join onto the full bar row, so it aggregates a wider row than a membership row
would have been. A membership table over duplicated rows is purely additive if either cost bites.

## A dataset is identified by every field it was requested with

The entry's unique key, `uq_store_dataset_entry_identity`, is **ten columns**: `owner`,
`asset_symbol`, `asset_type`, `data_type`, `source`, `granularity`, `expiry_type`, `update_type`,
`start`, `end`. Two requests differing in **any** of them — the range included — are two datasets.
Nothing is merged on conflict: the earlier behaviour of reconciling `expiry_type` and `update_type`
to the higher-ranked value is gone.

- `feed` is **not** yet part of the entry's identity. The entry is written before ingest has resolved
  a tape, so feed on the entry is deferred to the gRPC transport work (`tj-rh4b7f`). Today one
  deployment serves one feed, so no two entries can differ by feed. The bar does carry `feed`.
- `expiry` (when the dataset's data dies) is a column on the entry, not on the bar, and is **not**
  identity.
- `end` omitted means open-ended; it is stored as a 1970-01-01 sentinel so that two open-ended
  requests still collide on the key.
- `owner`, `expiry_type` and `update_type` are `NOT NULL`. A null in a unique key is distinct from
  every other null in Postgres, which would silently turn an exact repeat into a second row.

## What a create does

`POST /store/...` (S1) resolves the request against the caller's own datasets:

| Request | Outcome |
|---|---|
| Every identity field equal to an existing entry, range included | **No-op.** The existing entry's id is reused; only its `expiry` and `updated_at` are refreshed. This is what makes a retried POST safe. |
| Same owner and same non-range fields, range overlapping but not equal | **409**, `detail.colliding_ids` listing the overlapping entries. Nothing is written. |
| Anything else, including another owner's overlapping dataset | A new entry. Owner is identity, so a different owner's dataset is never a collision. |

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

The bar row carries no `split_factor` and no `dividends_factor`. Bars are stored raw and are meant to
be immutable; corporate actions are to be applied **on read** from a separate events table. **That
table, the adjustment calculation and its cache do not exist yet** — only the schema decision has
landed. Do not read the absence of the columns as "adjustment is handled": every bar this surface
returns is unadjusted.

## The feed on a bar

Every bar carries a required `feed` (`IEX`, `SIP`, or `NOT_APPLICABLE` for a source with no tape
distinction). There is no `UNKNOWN` member. The ingest adapter stamps it from the deployment's
entitlement (`ALPACA_SIP_ENABLED`), not from the vendor's response, which reports no feed; no caller
can select a feed yet.

---

# Conventions across the surface

These hold on every route unless the entry says otherwise.

| Convention | Behaviour |
|---|---|
| **Timezone-aware datetimes only** | Every datetime a caller sends — the create body's `start`, `end` and `expiry`; S2's `start`, `end`, `created_at`, `updated_at`; S4's bar `timestamp`; S5's `start`, `end`; and the RPC request R1 — must carry an offset or `Z`. A naive value is a **422** naming the field (type `timezone_aware`). It is refused, never assumed to be UTC. |
| **Unknown fields and parameters are refused** | Request models reject what they do not declare (`extra='forbid'`), so a misspelt field or query parameter is a **422** (type `extra_forbidden`) rather than silently ignored. On S2 and S5 this holds for query parameters because the query model is bound with `Query()`. A strict receiver means that when a field is **added** to a contract, the receiving side must deploy first. |
| **Enum names on the wire** | `expiry_type` and `update_type` travel as member **names** — `BULK`, `BUFFER_1K`, `BUFFER_10K`, `BUFFER_100K`, `ROLLING`; `STATIC`, `DAILY`, `STREAM` — and the OpenAPI document declares them as string enums of those names, with defaults `BULK` and `STATIC`. An integer is still accepted on input but is undocumented. The Kafka RPC request (R1) still carries these two as integers. |
| **No orphan OpenAPI components** | The store app prunes any `components.schemas` entry no `$ref` reaches, so the leftover integer-enum components `ExpiryType` and `UpdateType` are no longer in the document. A client generated from it gets one type per component actually used. |
| **Instance secret before schema validation** | On a guarded route the 401 is answered before path, query and body values are validated against their models, so an unauthenticated request with, say, a bad path value or a body that fails the schema is a 401, not a 422. **Except for a body that is not valid JSON:** FastAPI parses the JSON body before it runs the secret dependency, so an unparseable body is a 422 whether or not the secret is present. |
| **One database session per request** | Every store route gets its session from the `async_db` dependency (`data/store/app/database/database.py`), which opens it through `PostgresSessionFactory.AsyncSessionHandle.session()` and closes it when the request ends, including when the request raises. Closing rolls back any transaction still open and returns the connection to the pool. There is no `get_session` and no task-scoped registry behind it; the registry it replaced leaked one pooled connection per read request (decision record `tj-8z213c`). |
| **Database errors** | A write rolls back once, is logged once at ERROR with its traceback, and **re-raises the original SQLAlchemy error**. No exception handler is registered, so over HTTP it is a bare **500**, as before. A typed conversion (a 503-class status with a structured body) is planned at the store app's HTTP boundary under an error-handling standard that is proposed and **not yet accepted** (`tj-fa1rpu`). |

**Status codes the store routes can answer by design:**

| Status | Meaning | Routes |
|---|---|---|
| 401 | Instance secret missing, wrong, or not configured on the server. One fixed message for all three causes. | S1, S3, S4 |
| 403 | The declared `owner` does not own the entry (a missing owner is treated as a wrong one) | S3 |
| 404 | No entry with that id | S3 |
| 409 | The create overlaps the same owner's existing dataset; `detail.colliding_ids` lists them | S1 |
| 422 | Validation: a naive datetime, an unknown field or parameter, a missing required field, a bars query naming no selector or a blank symbol, a `validate_fields` rule on the create body, an `asset_type`/`data_type` pair other than `stock`/`market_activity` on the internal asset-data path, or a malformed single-bar body | all |
| 500 | Anything unmapped: a database error, a non-stock `asset_type` on S1 (gap **G8**), a duplicate timestamp inside one ingested batch | all |

---

# routers/common

## C1 · `GET /ping`

| | |
|---|---|
| **Stability** | **Stable.** Not Kafka-borne, and the gRPC work (`tj-8konfu`) keeps REST for admin, debug and health rather than re-hosting it. |
| **Auth** | none |
| **Implementation** | `routers/common/ping.py :: ping` *(as of the dataset-model PR)* |
| **Request** | none |
| **Response** | undeclared — no `response_model`; the handler returns an ad-hoc dict |
| **Touches** | nothing — no Postgres, no Kafka, no broker |

**Contract: a 2xx status.** Treat the status as the contract and not the body. `docker-compose.yaml`
makes this route the container healthcheck for *both* services, and the probe reads and discards the
response body — so a body assertion pins a string nothing reads.

It is served by both applications but declared once, here, because the manifest enumerates routers
and nothing enumerates applications. One interface, one implementation, one entry.

Because the handler touches nothing, a 2xx from it says only that the web server is up. See gap
**G3** — this is the entire health story today: it is what `make prod-launch` and CI's `up --wait`
block on for both services.

## C2 · `/latency/{latency_type}`

| | |
|---|---|
| **Stability** | **Dies with Kafka** — after one last job. See below; this is *not* dead code to delete today. |
| **Auth** | none |
| **Declared at** | `routers/common/app_endpoints.py :: InterfaceRest.LATENCY` *(as of the dataset-model PR)* |
| **Implemented in** | `routers/common/latency.py`, inside `initialize_latency_client()` |
| **Request** | `schemas.common.latency.LatencyRequest` |
| **Response** | undeclared — one of three ad-hoc dicts |
| **Touches** | Kafka, and HTTP to the service's own `/latency_internal` |

**This is the one interface in this document whose existence depends on an environment variable.**
The router is built *inside* `initialize_latency_client()`, which runs only when
`LATENCY_TEST_ENABLED` is set. That flag is off by default and production runs with it off, so the
manifest records the path as `unbound-path`. Here that kind means *conditionally mounted, off in
production* — **not** *never written*. The code exists.

**Before re-enabling this anywhere, know what it exposes.** The handler takes an unauthenticated,
caller-supplied `payload_size` and `iterations`, allocates `os.urandom(payload_size * 1024)`, and
fans that many requests out concurrently. That is a request amplifier, not merely an idle endpoint.
Recorded on `tj-a0s7vl`.

**Why it is not deleted now.** It is a Kafka-versus-REST latency measurement harness, and it has one
scheduled job left: the staged gRPC cutover (`tj-8konfu`) adds gRPC as a third latency arm beside
REST and Kafka-RPC, and says in terms that the REST arm — unlike the Kafka arm — is not deleted for
it. The harness is the instrument for the cutover measurement, and it dies with the transport it was
built to compare against.

## C3 · `/latency_internal`

| | |
|---|---|
| **Stability** | **Dies with Kafka**, with C2 — it exists only as C2's echo target. |
| **Auth** | none |
| **Declared at** | `routers/common/app_endpoints.py :: InterfaceRest.INTERNAL_LATENCY` *(as of the dataset-model PR)* |
| **Implemented in** | `routers/common/latency.py`, inside `initialize_latency_server()` |
| **Request** | `schemas.common.latency.InternalLatencyRequest` |
| **Response** | undeclared — ad-hoc dict |
| **Touches** | Kafka (it also registers an RPC server) |

Same environment-variable gate as C2, same category: written, switched off. One guard and one
initialiser pair control both.

---

# routers/data_store

Every route in this component reaches Postgres. `POST /store/...` also reaches Kafka, and is the only
interface in the component that crosses two external boundaries.

## S1 · `POST /store/{asset_type}/{data_type}/{asset_symbol}`

| | |
|---|---|
| **Stability** | **Route survives, outbound hop changes.** The address stays; its Kafka forward to data_ingest becomes a gRPC call. |
| **Auth** | **instance secret** (`X-Instance-Secret`), fail-closed; 401 otherwise |
| **Implementation** | `routers/data_store/asset_dataset_store.py :: store_data` *(as of the dataset-model PR)* |
| **Request** | `schemas.data_store.asset_dataset_store.StoreAssetDatasetPath` + `...StoreAssetDatasetBody` |
| **Response** | **undeclared** — ad-hoc dict: `message` and `data_points` (bars written) |
| **Touches** | Postgres (`AsyncSession`) **and** Kafka (`KafkaRpcFactory.RpcClients`) |

This is the write path: it creates (or resolves) the dataset entry, triggers a fetch, forwards the
request to data_ingest through `store_market_activity_worker`, and writes the returned bars against
that entry. When the transport moves to gRPC (`tj-8konfu`), the seam that survives is
`store_market_activity_worker`'s contract, not the Kafka client — anything built against the
transport is rewritten at the cutover.

**The body is the dataset's identity.** `owner` (required, no default), `source`, `granularity`,
`start` (required), `end` (optional; omitted means open-ended), `expiry_type` (default `BULK`) and
`update_type` (default `STATIC`), plus `expiry` (optional; defaults to one day from the request, and
an explicit `null` is a 422). There is no `feed` field. The entry is resolved as described in
[What a create does](#what-a-create-does): an exact repeat reuses the existing entry, an overlap with
the same owner's dataset is a **409** carrying `colliding_ids`, anything else creates.

**`validate_fields` rules, both 422:** `update_type` must be `STATIC` when `end` is given, and must be
`STATIC` when `expiry_type` is `BULK`. The messages name members, e.g. *"The 'update_type' field must
be 'STATIC' when 'end' is provided."*

**Two things a retry does not avoid.** An exact repeat reuses the entry and the bar upsert is
idempotent on the bar's natural key, so the stored data does not duplicate — but the vendor fetch is
repeated on every call, and a batch carrying two bars at the same timestamp fails the whole write with
a 500. A batch is written in chunks sized under the tighter of two argument limits — asyncpg's
32,767 per statement, below Postgres's own 65,535 — all inside one transaction: it lands whole or not
at all.

The `start` 500 recorded against this route at the previous pin (`tj-6yk4qs`) is fixed: `start` is
required on the body, so a missing one is a 422 at the edge.

It is the highest-value interface in this component and the one whose priority changes most: today
the cost of a duplicate call is a wasted vendor fetch, but this is the shape of route the order/fill
event log will eventually sit behind. See gaps **G6** (undeclared response) and **G10**
(idempotency).

## S2 · `GET /store/{asset_type}/{data_type}/{asset_symbol}`

| | |
|---|---|
| **Stability** | **Stable.** The Kafka removal (`tj-3mk3u5`) explicitly keeps REST for read and debug. |
| **Auth** | none |
| **Implementation** | `routers/data_store/asset_dataset_store.py :: get_data` *(as of the dataset-model PR)* |
| **Request** | `...StoreAssetDatasetPath` + `...StoreAssetDatasetQuery`, bound with `Query()` |
| **Response** | `list[schemas.data_store.asset_dataset_store.AssetDatasetStore]` |
| **Touches** | Postgres |

Lists the dataset entries for one symbol, each with its `item_count` (bars held, 0 for an entry with
none) and its own `expiry`. Returns a **list**, and has no way to address a single entry — which is
the argument for implementing `GET /store/{id}`, below.

Optional query filters, each an exact match on the entry's column: `owner`, `source`, `granularity`,
`start`, `end`, `expiry_type`, `update_type`, `created_at`, `updated_at`. The datetimes must be
timezone-aware; `expiry_type` and `update_type` take member names. There is no `feed` filter, since
the entry has no feed column. An unknown parameter is a 422. It has no `limit` or `offset`; see gap
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
| **Implementation** | `routers/data_store/asset_dataset_store.py :: delete_data` *(as of the dataset-model PR)* |
| **Request** | `schemas.data_store.asset_dataset_store.AssetDatasetStoreDelete` — path `id`, query `owner` |
| **Response** | **undeclared** — ad-hoc dict |
| **Touches** | Postgres |

Deletes one entry and, by `ON DELETE CASCADE`, exactly that entry's bars. **401** without the
secret; **403** when `owner` is missing or does not match the entry's owner (the body does not reveal
the real one); **404** for an unknown id. `owner` is optional at the schema level on purpose: an
absent owner is the degenerate case of a wrong one and gets the same 403, not a 422. That refusal
holds only because `store_dataset_entry.owner` is `NOT NULL`; if that column ever became nullable, a
missing owner would start matching rows.

**Address collision.** `AssetDatasetStoreInterface` declares both `GET_STORE_ASSET_DATASET_BY_ID` and
`DELETE_STORE_ASSET_DATASET_BY_ID` at the same path, `/store/{id}`, and only the DELETE is bound. The
manifest cannot see the collision, because an enum member carries no method. `tj-wc4pe8` closed
without resolving it — the two PUT declarations it also covered were deleted, but this pair is
resolved by *implementing* the GET, which is now `tj-2h1q3k`.

**Correction to an earlier version of this document.** The first version filed `tj-9dqfjo` —
`AssetDataDeleteById` using `Field()` without being a Pydantic model — as a defect *in this route's
request model*. **That was wrong.** This route's request model is
`schemas.data_store.asset_dataset_store.AssetDatasetStoreDelete`, which is a well-formed `BaseModel`.
`AssetDataDeleteById` lives in `schemas/data_store/asset_data_interface.py` and belongs to the
internal asset-data family. The defect is real and `tj-9dqfjo` is still live, but it is a **schema**
defect with no bound route behind it: its only consumer was `DELETE /internal/asset-data/...`, removed
under `tj-h7ikz2`. Nothing on the served surface reaches it today.

Deletion is also the one operation the append-only event-log rule (`tj-qqdo3j`) will forbid outright
on the order path. When that lands, the question worth asking of this route is whether it can reach
event-log rows at all.

## S4 · `POST /internal/asset-data/{asset_type}/{data_type}`

| | |
|---|---|
| **Stability** | **Fate undecided — do not guess.** See below. |
| **Auth** | **instance secret** (`X-Instance-Secret`), fail-closed; 401 otherwise |
| **Implementation** | `routers/data_store/internal_asset_data.py :: create_stock_market_activity_data` *(as of the dataset-model PR)* |
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

**Why the fate is undecided and nobody should guess it:** nothing in the repository calls this route
and no accepted decision record names it. `tj-3mk3u5` keeps REST for admin and debug, which this
plausibly is — but it is also exactly the data_store↔data_ingest write path that `tj-8konfu` moves to
gRPC. Confirm before building on it.

## S5 · `GET /internal/asset-data/{asset_type}/{data_type}`

| | |
|---|---|
| **Stability** | **Stable.** It is the one bar read, by ruling: the unfiltered read it used to call was deleted rather than kept beside it, and the Kafka removal keeps REST for read. |
| **Auth** | none |
| **Implementation** | `routers/data_store/internal_asset_data.py :: read_stock_market_activity_data` *(as of the dataset-model PR)* |
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
| `start`, `end` | the bar `timestamp`, **inclusive** at both ends (`timestamp >= start`, `timestamp <= end`); timezone-aware only |

**A selector is required.** A query must name `dataset_id` or `asset_symbol`; one naming neither is a
**422** (loc `['query']`, type `value_error`), so an unbounded read of the whole table cannot be
expressed. A **blank** `asset_symbol` (empty or whitespace only) is refused the same way, even when
`dataset_id` is given — it names no symbol. A padded but non-blank symbol is not trimmed.

**Other 422s, all before the handler runs:** an unknown parameter (type `extra_forbidden`), and a
naive `start` or `end` (type `timezone_aware`). The parameters `expiry` and `query` that the model
used to declare are gone — a bar has no expiry, and a nested model cannot be a query parameter — so
sending either is now an unknown-parameter 422.

**Order:** `ORDER BY timestamp, dataset_id`, and nothing else. Without `dataset_id`, bars from every
dataset of the symbol interleave in timestamp order and the same minute can appear once per dataset —
duplication is per dataset by design. Every returned bar carries its `dataset_id` and `feed`, so the
rows stay distinguishable.

It still has no `limit` or `offset`: a selector bounds the read to one symbol or one dataset, not to
a page. See gap **G5**.

An unsupported `asset_type`/`data_type` pair is a **422** here too, from the same `Path()`-bound
`AssetDataPath` as S4.

## `GET /store/{id}` — declared, unserved, and invisible to the manifest

| | |
|---|---|
| **Stability** | **Filed for implementation** — `tj-2h1q3k`, user ruling 2026-09-23. Not built yet. |
| **Auth** | n/a — nothing is served **yet** |
| **Declared at** | `routers/data_store/app_endpoints.py :: AssetDatasetStoreInterface.GET_STORE_ASSET_DATASET_BY_ID` *(as of the dataset-model PR)* |
| **Request / Response / Touches** | none — nothing is bound |

**This is the only entry in this document that is not implemented, and it is the only one left.** The
other three unserved declarations were deleted (below). This one was ruled the other way on the same
day and is filed as `tj-2h1q3k`, assigned to `builder-store`. It is scheduled work, not an open
question — do not re-propose it, and do not treat its absence from the manifest as evidence it was
forgotten.

**Why this one is not in the manifest at all, and why it is the reason the counts differ.** The enum
declares GET and DELETE at the same path, `/store/{id}`, and only the DELETE is bound. **The manifest
matches by path, not by method**, so the path already counts as bound and this declaration never
reaches the `unbound-path` list. It is the one declared, unserved interface the machine-checked
surface cannot see — which is exactly why the glance table above has ten rows against the manifest's
nine. The architect found it by reading the enum, not by reading the manifest.

When `tj-2h1q3k` lands, the manifest **gains** a line (the path becomes `GET` *and* `DELETE`, two
interfaces where it recorded one) and the recorded GET/DELETE collision dissolves.

**Architect's verdict, ruled and taken — implement it.** It is the only read-by-id on the store
surface.
`GET /store/{asset_type}/{data_type}/{asset_symbol}` returns a list and cannot address one entry,
while `DELETE /store/{id}` already proves the id is a first-class address. **So the surface today lets
you delete an entry by id but never read it by id.** It survives every migration in flight —
unaffected by the Kafka removal, the gRPC re-host and the Phase 1 re-path, which is what separated it
from the three declarations deleted on the same day —
`tj-3mk3u5` keeps REST for read and debug — and it becomes *more* useful once the entry row carries
the fetch-ledger columns, because "what is the state of this fetch" is a read-by-id question (gap
**G1**).

**If it is implemented:** declare a response model (`schemas.data_store.asset_dataset_store.AssetDatasetStore`)
and a real 404. Several routes on this surface already declare no response model and the not-found
path is untested across the whole component — do not add another ad-hoc dict.

---

# routers/data_ingest

data_ingest declares **no HTTP interface at all** today. It answers on one Kafka RPC topic.

## R1 · `stock_market_activity_rpc` (Kafka RPC topic)

| | |
|---|---|
| **Stability** | **Address is re-keyed.** See the caveat below — this is the one entry whose key changes. |
| **Auth** | none |
| **Implementation** | `routers/data_ingest/get_dataset_request.py :: store_data` *(as of the dataset-model PR)*, registered by the `add_server` decorator while the module body runs |
| **Request** | `schemas.data_ingest.get_dataset_request.GetDatasetRequest` |
| **Response** | `schemas.data_store.stock.market_activity_data.BatchStockDataMarketActivityCreate` |
| **Touches** | Kafka (the transport itself) and the broker, reached through a module-level import rather than injection |

**The keying rule breaks here, and this is the flag.** This document keys entries on address because
an HTTP path survives the monorepo re-path. **A Kafka topic name does not survive the Kafka removal.**
When the gRPC re-host lands (`tj-8konfu`), this entry is *re-keyed* to the gRPC method name. That
re-key is the entry's migration event and belongs here as an addendum to this entry — not as a new
entry, or the reference reads as though a second interface appeared.

This is the most consequential migration line on the surface. The transport is deleted by `tj-3mk3u5`
and the method is re-hosted on gRPC by `tj-8konfu`; what survives the cutover is `store_data`'s own
contract — `GetDatasetRequest` in, `BatchStockDataMarketActivityCreate` out — because the gRPC work
decides the surface while the message shapes stay put. **Build against the handler signature, not the
transport.**

**The request now carries the dataset's identity and principal.** `GetDatasetRequest` carries
`owner`, `dataset_id` and the other identity fields from S1, rejects unknown fields, and requires
timezone-aware `start`, `end` and `expiry`. It also declares an optional `feed`, which **nothing in
data_ingest reads**: the adapter resolves the tape once per fetch from `ALPACA_SIP_ENABLED` and stamps
it on the returned batch. The response's `feed` is required, one per batch. `split_factor` and
`dividends_factor` are gone from the bar data, and because the receiving model rejects unknown
fields, a sender still emitting them fails loudly.

**Three asset classes are accepted; one is served.** The handler re-validates into
`StockDatasetRequest`, `CryptoDatasetRequest` or `OptionDatasetRequest` on a caller-supplied
`asset_type`. The crypto and option callbacks are **empty synchronous functions whose body is `pass`**
(`data/ingest/app/ingest_control.py`). They return `None`, and the handler awaits the result — so the
failure is a `TypeError: object NoneType can't be used in 'await' expression`, not a raise from the
stub itself. A non-Alpaca `source` on the *stock* path does raise directly:
`NotImplementedError('Data source not implemented')`. Stock-only is the correct scope today;
accepting an argument that will 500 is not. See gap **G8**.

**A vendor failure currently arrives as success.** The broker call swallows every exception and
returns an empty result (`tj-fe19tu`), so a vendor error, a rate-limit rejection and a vendor-side
auth rejection all reach the caller as an *empty dataset* — indistinguishable from a legitimately
empty interval. Two narrowings worth knowing, neither of which retracts the defect:

- **A missing credential now escapes.** The client is resolved once, before any task is spawned, so a
  *configuration* failure raises `MissingCredentialsError` by name rather than dying inside a gathered
  task. A vendor-side auth *rejection* is still swallowed.
- **The swallow returns a bare `{}`**, not the declared `BatchStockDataMarketActivityCreate`. The
  caller in data_store then reads `.dataset` off it, so the observable symptom downstream is an
  `AttributeError` — the failure is silent at the broker layer and *misattributed* at the store layer.

See gaps **G1** and **G2**, which compound here.

data_ingest declares **no REST interface at all** now — not merely none that is bound. `tj-427x50`
deleted the last member of its `InterfaceRest` enum, and with it the empty enum class and its `Enum`
import. The component's entire declared surface is the one RPC topic above. See
[Declarations deleted by ruling](#declarations-deleted-by-ruling-2026-09-23).

---

# Not implemented: two different things

"Not implemented" used to pool three situations here. It now pools two, because the third — *never
written* — was emptied by the deletions below.

| Sense | Which | What it means for you |
|---|---|---|
| **Written, switched off** | C2, C3 | The code exists in `routers/common/latency.py` and mounts only when `LATENCY_TEST_ENABLED` is set. `unbound-path` here means *conditionally mounted, off in production*. The initialisers are also single-shot, so a client and a server cannot both initialise in one process. |
| **Declared, unserved, invisible to the manifest** | `GET /store/{id}` | Declared in the enum, never bound, and absent from the `unbound-path` list because the manifest matches by path and the DELETE at the same path is bound. Filed as `tj-2h1q3k`. |

**The verdicts were taken, and this is now the state, not a forecast.** The `unbound-path` kind is
empty for data_store and data_ingest and survives only for the two latency entries — which is the
state that kind was invented to describe. `data_store.manifest` carries five `http` lines and nothing
else; `data_ingest.manifest` carries one `rpc` line and nothing else.

---

# Declarations deleted by ruling (2026-09-23)

These three were **declarations, not routes** — enum members naming a path that no code ever served.
Deleting one removed an entry from the declared surface and changed nothing a caller could call. They
are recorded here, rather than dropped, so that the next person to want one of these finds the
reasoning instead of re-proposing it blind.

The architect recommended deletion on `tj-mj84d8`; **the user ruled to follow the verdicts on
2026-09-23**; `builder-store` and `builder-ingest` executed; validator and architect gates both
returned PASS on `tj-wc4pe8` and `tj-427x50`, and both beads are closed. Each deletion moved the enum
member and its manifest line in one diff, which is what the manifest's equality test requires — so a
silent reintroduction turns that test red.

| Was | Declared at | Bead |
|---|---|---|
| `/internal/asset-data/{asset_type}/{data_type}/{id}` | `AssetDataInterface.PUT_ASSET_DATA` | `tj-wc4pe8` |
| `/store/{asset_type}/{data_type}/{asset_symbol}/{id}` | `AssetDatasetStoreInterface.PUT_STORE_ASSET_DATASET` | `tj-wc4pe8` |
| `/broker/{asset_type}/{symbol}/{data_type}` | `routers/data_ingest` `InterfaceRest.POST_STORE_DATASET` | `tj-427x50` |

## What each was for, and what would bring it back

**`PUT /internal/asset-data/.../{id}` — an update-by-id for a market-activity row.** Symmetric CRUD: a
fourth verb beside POST and GET on the internal asset-data family. *Deleted because the need does not
survive.* Market-activity rows are vendor data, and the correction primitive for vendor data is a
re-fetch — a fetch ledger says what is missing, a range request asks for exactly that, the vendor
serves it (`tj-3mk3u5`). An operator PUT is nowhere in that story, and it is at odds with archive
sealing (`tj-j0bx0w`): a PUT that mutates rows inside a sealed chunk has no defined interaction with
sealing, and inventing one is a decision record, not an endpoint.

> **What would bring it back:** a need for a *correction* primitive with a **defined ledger
> interaction** and a defined behaviour against sealed chunks. Not a general-purpose row update. Cost
> of having been wrong: one enum line.

**`PUT /store/.../{id}` — an update for one `store_dataset_entry` row.** That row is dataset and
coverage *bookkeeping*, not market data. *Deleted, but the underlying need is real and is arriving* —
and this is the entry where the reasoning matters more than the verdict. `tj-3mk3u5` puts the fetch
ledger on `store_dataset_entry`, which makes the entry row mutable state with a lifecycle, and
something must advance it. **That something is the fetch pipeline itself, internally.** A
caller-writable ledger row is the exact failure the ledger exists to prevent: it would let a client
assert coverage that was never fetched, and removing Kafka is only safe *because* the ledger is
authoritative about what is missing. Writable from outside, it is not authoritative.

> **What would bring it back:** it comes back as *internal pipeline* mutation, which needs no
> interface. If anyone proposes it as REST again, it needs **its own decision record** first, because
> a caller-writable ledger row can falsify coverage. The next person to want this will be right that
> the need exists and wrong about the shape.

> **Addendum, 2026-09-25 — ruled back in, not yet built.** The per-dataset decision record
> (`tj-vhboky.1`) records a user ruling that create and update are separate routes and that an update
> route must exist, because the 409 on an overlapping create hands the caller an id with nowhere to
> send it. That update is narrower than the row update deleted here: it changes only `start` and/or
> `end`, only by growth, keeps the id, and is owner-checked — it is not a caller-writable ledger. The
> crud function exists (`update_entry`), but **no route, endpoint constant or manifest line exists as of
> the dataset-model PR**. See [What a create does](#what-a-create-does).

**`POST /broker/{asset_type}/{symbol}/{data_type}` — a REST door into the fetch path.** The same job
as R1, but reachable by a human or a script instead of by data_store. *Deleted as a defer, not a no —
the capability survives, this shape of it does not.* After the gRPC move the supported way to ask
data_ingest for data is the gRPC batch call, and a second unauthenticated REST door into the same
fetch path would bypass the single-flight collapse and the rate budget that exist to stop vendor
thrash. Route-around-the-limiter is the wrong shape for the one operation on this surface that costs
money at the vendor.

> **What would bring it back — a named trigger.** When the fetch ledger lands and gap rows become
> visible, file an admin force-refetch endpoint as its own task: taking a **gap identity**, not an
> asset triple; going **through** single-flight and the rate budget; and scoped behind a credential
> (`tj-a0s7vl`). Because it takes a different argument it is a different interface — which is why
> keeping the enum member would have saved no work.

---

# Gaps in the interface

Each gap is labelled **REAL GAP** — a reader would expect it, it is missing, and nothing owns it — or
**NOT-YET** — absent on purpose and scheduled elsewhere. The label is the point: an interface
reference's worst failure is letting a reader conclude that an absence was an oversight.

## G1 · No way to ask what a dataset's coverage is — **REAL GAP**, the largest here

A caller can POST a fetch and GET rows back. Nothing answers *"what do you already hold for this
symbol over this window, and what is missing?"* The only way to find out is to read the rows and infer
from absence — and that inference is unsafe: an absent interval is ambiguous, and on an IEX-only tape
empty intervals are normal. The whole recovery design exists to remove that ambiguity.

The **storage** is scheduled — two columns on `store_dataset_entry`, coverage derived over the fetch
rows (`tj-3mk3u5`). The **read interface** onto it is named nowhere.

*Recommendation:* file the interface against the ledger work, not just the columns.

## G2 · No way to see or retry a failed fetch — **REAL GAP**, and worse than merely missing

The broker call swallows every exception and returns an empty result (`tj-fe19tu`), so a vendor error,
a rate-limit rejection and an auth failure all arrive at the caller as an empty dataset —
indistinguishable from a legitimately empty interval. There is no error surface, no fetch status and
no retry anywhere on the declared surface.

The bug is filed; the **interface** consequence is not. Even once the bug is fixed, nothing in this
surface reports fetch outcome, and a swallowed error becomes a permanent silent gap that the ledger
will record as fetched.

*Recommendation:* one status read answers G1 and G2 together. Design them as one interface, not two.

## G3 · No health/readiness distinction beyond `/ping` — **REAL GAP**, with a dated trigger

`GET /ping` is answered by a handler that touches nothing — no Postgres, no Kafka, no broker — and it
is the container healthcheck for *both* services, which `make prod-launch` and CI's `up --wait`
block on. So a container with a dead Postgres pool, unreachable peer or exhausted broker credentials
reports healthy, and the launch reports success. (data_store's own start waits on postgres and kafka
being healthy, through their own probes, not on data_ingest.)

The gRPC work (`tj-8konfu`) already names the sharper version of this: both healthchecks probe HTTP
`/ping`, so a container whose gRPC listener is dead still reports healthy.

*Recommendation:* a readiness endpoint that actually checks dependencies, landing with the gRPC work.
`/ping` stays as liveness and keeps its current shape.

## G4 · No caller authentication on any route — **REAL GAP**, the blocking one

The three write routes now require the deployment's instance secret, and nothing else on the surface
requires anything. **The secret is not caller authentication**: it authenticates the deployment, so
with one key there is one effective principal, and the per-dataset owner check stops mistakes rather
than anyone holding the key. Every other surface answers without credentials (`tj-glqs4r`, open);
prod publishing no host port narrows who can reach them to containers on the stack's networks, which
is not the same thing. `tj-a0s7vl` owns real authentication at P1 and is explicit that loopback binding
is a perimeter, not an identity. The platform design wants scoped keys (`read:accounts`,
`read:events`, `read:market`, `trade`) from day one, because the private strategy repo consumes this
surface from outside the container; per-principal keys would reuse the same owner comparison with
more rows.

Specifics worth naming rather than leaving to inference:

- `GET /latency/{latency_type}` is an unauthenticated **request amplifier** (C2).
- `DELETE /store/{id}` is **destructive** and guarded only by the shared secret plus a declared
  owner that any key holder can name (S3).
- Every read, including the one that returns every entry's `owner`, is open.

*Recommendation:* this is why the document opens with the warning and why every entry carries an
`auth` field.

## G5 · No pagination on reads — **REAL GAP**, narrowed

`GET /internal/asset-data/{asset_type}/{data_type}` (S5) no longer returns every row: it requires a
`dataset_id` or `asset_symbol` selector, so the widest read it can express is one symbol across all
its datasets and all time. `GET /store/{asset_type}/{data_type}/{asset_symbol}` (S2) is scoped to one
symbol. Neither takes `limit` or `offset`, and there is no pagination anywhere on the surface. The bar
table is unbounded with no retention policy, so one liquid symbol at minute granularity is still a
large answer, returned 200 in one response.

## G6 · Routes that declare no response model — **REAL GAP**, cheap, and it compounds

As of the dataset-model PR, three HTTP routes return undeclared dicts: `POST /store/...` (S1),
`DELETE /store/{id}` (S3) and `GET /ping` (C1). The latency pair (C2, C3) returns ad-hoc dicts too.
*(The analysis behind this document counted four HTTP routes; the fourth was
`DELETE /internal/asset-data/...`, removed under `tj-h7ikz2`.)*

The generated OpenAPI therefore documents nothing for them — and a **versioned typed client SDK** is
planned for the private repo (`tj-d2mhru`), which has nothing to generate from where the response
model is absent.

*Recommendation:* one chore covering all of them. Meanwhile this document marks each such entry
"undeclared" rather than transcribing the dict shape as if it were a contract.

## G7 · One untyped request body — **REAL GAP**, currently low-impact

`POST /internal/asset-data/{asset_type}/{data_type}` takes a plain `dict` and splats it into
`StockDataMarketActivityCreate` inside the handler. It is the reason the manifest's `touches` field
for that route reads `dict` — there is no schema to name, so the OpenAPI document describes no body.

The target model is strict (unknown fields rejected, `dataset_id` and `feed` required), and a body
that fails it is now a 422 in the usual shape, re-raised by the handler. What remains is the typing
gap itself: nothing documents the body to a generated client.

## G8 · The surface promises three asset classes and serves one — **REAL GAP in the contract**

The RPC handler re-validates into a stock, crypto or option request on a caller-supplied `asset_type`,
and the crypto and option callbacks are empty synchronous stubs that return `None`, which the handler
then awaits — so the caller gets a `TypeError`, not a clean rejection (R1). No exception handler is
registered anywhere in `routers/` or `data/store/app/`, so that reaches an S1 caller as a 500.

The data_store internal family is **fixed**: S4 and S5 refuse an unsupported pair at the path model
with a 422, before the handler runs. Their `UnsupportedAssetType` branch is still in the handler but
is no longer reachable over HTTP.

Stock-only is the correct **scope** today. But an interface that accepts an argument it will 500 on is
a contract defect regardless of scope.

*Recommendation:* state stock-only in terms — done, here — and reject an unsupported `asset_type` on
S1 and R1 with a 4xx instead of raising, the way S4 and S5 now do. Cheap.

## G9 · Nothing here touches accounts, portfolios, orders or fills — **NOT-YET**, unambiguously

This is stated at the top of the document as well, because it is the absence most likely to be
misread. A reader arriving from the project description will look for an account model, a portfolio
model and an append-only order/fill event log, and will find a market-data pipeline. Those surfaces
are roadmap — `tj-pznkbx`, `tj-qqdo3j`, `tj-jmrqkf` — not omissions.

## G10 · The write path is idempotent in data, not in work — **REAL**, not yet urgent

`POST /store/...` is now idempotent in what it **stores**: an exact repeat resolves to the same
dataset entry, and the bar upsert refreshes existing rows on the bar's natural key rather than adding
new ones. It is not idempotent in what it **does**: every call, repeat or not, triggers a fetch and
forwards to data_ingest, so a retried or duplicated call repeats the vendor hop. The planned answer
is single-flight collapse *inside* the pipeline (`tj-3mk3u5`), which is not the same as a
caller-facing idempotency key — the caller cannot tell a collapsed duplicate from a served one.

Today the cost of a duplicate is a wasted fetch. It is worth naming now because this is the write
path: the moment the order/fill event log sits behind a route of this shape, a duplicate stops being a
wasted fetch and starts being money.

## G11 · No versioning on any path — **REAL GAP**, cheap to decide and expensive to retrofit

No `/v1`, no version header, bare paths throughout — while a **versioned** typed SDK (`tj-d2mhru`)
is planned to consume this from another repo. This release changed the contract under the same bare
paths (S1's required `owner`, S5's query, the enum wire documentation), which is the cost this gap
names.

*Recommendation:* decide it alongside the gRPC surface rather than separately. `tj-8konfu` is already
reasoning about a pre-release proto window and a first released version, and one versioning answer
should cover both surfaces.

## Deliberately not filed as gaps

Recorded so the next reader does not re-raise them:

| Not a gap | Why |
|---|---|
| Rate limiting | Belongs to the rate budget inside the pipeline (`tj-3mk3u5`), not to the route surface. |
| Bulk / batch endpoints | The gRPC batch call is the answer (`tj-8konfu`). |
| Streaming | Server-streaming subscribe, same record. |
| A notification transport for fills | Narrowed to a cursor **read** on purpose (`tj-pznkbx`) — an absence by decision. |

---

# What will invalidate this document

This is a snapshot with named successors, not a stable reference. Four pieces of scheduled work
rewrite it, and knowing which is more useful than any individual entry above:

| Work | What it changes here |
|---|---|
| `tj-3mk3u5` — Kafka removal | Deletes C2 and C3; re-keys R1's address; changes S1's outbound hop. Rewrites the transport story. |
| `tj-8konfu` — gRPC surface (**proposed, not accepted**) | Re-hosts R1, adds a surface this document does not cover, and should settle versioning (G11) and readiness (G3). |
| `tj-iontkq` — monorepo split | Moves the server code under `server/`, internals unchanged. Every file path above goes stale; addresses survive. |
| `tj-a0s7vl` — authentication | Adds a real credential to every entry, replacing both `auth: none` and the deployment-level instance secret. |

Pending at the time of writing, beyond those four:

- `tj-vhboky.14` — the host checks the top of this document lists as not yet run. Until they run,
  those behaviours are designed and unit-tested, not proven against Postgres.
- The asyncpg bind check against the shipped `RedactedStr` and `SensitiveString`. The host run above
  proved a stand-in subclass; `tests/system/test_asyncpg_bind_spike.py` was re-pointed at the real
  classes afterwards and has not been run on a host since (`tj-vhboky.14`).
- `tj-fa1rpu` — the error-handling standard, **proposed, not accepted**. When accepted, database
  errors on the store's routes get a typed response at the HTTP boundary instead of a bare 500.
- `tj-rh4b7f` — `feed` on the dataset entry and in its identity, with the gRPC transport work.
- The dataset update route (see the addendum under
  [Declarations deleted by ruling](#declarations-deleted-by-ruling-2026-09-23)) — ruled, not built.
- The corporate-action events table and adjustment on read — decided, not built.
- `tj-2h1q3k` — implements `GET /store/{id}`. When it lands, the last unimplemented entry in this
  document becomes a served route, the manifest gains a line, and the GET/DELETE collision recorded
  against S3 dissolves.

`tj-6z03hd` (S5 ignoring its query) is **closed**: S5 is the filtering read described above.
`tj-wc4pe8` and `tj-427x50` are **closed**, and their effect is already reflected above.

## Keeping this document honest

When this document and the manifest disagree, **the manifest is right**. File a bug against this
document.

This document has drifted before, and the way it happened is worth recording. It was written
pinned to a commit on the `tj-6z03hd` work; the next two commits deleted three of the declarations
it described. Both of those commits were correctly gated and the manifest test was green throughout, because the manifest
moved with the code exactly as designed. Nothing was broken — the document simply was not in anyone's
path. **The manifest test makes the surface impossible to change silently; it does nothing to make
this document move when the surface does.** That gap is routing, not detection, and a proposal for
closing it is recorded on `tj-d6e128`.

It drifted a second way on the per-dataset release: the branch was regrouped for review, and the
commit the document was pinned to stopped being an ancestor of the line it described — so the one
instruction a reader was given, "check the commit pin", became impossible to follow. It happened
twice, to `a96ede9` and then `5ed8eb1`. **So this document pins no SHA.** It names the release it
describes and cites a file and symbol per entry, both of which survive a regroup. The SHAs a
regroup does not touch are the ones already on `main`.

Until something mechanical exists, the working rule is: **a diff that touches `routers/`, `schemas/`,
an interface enum, or `routers/tests/interface_manifest/` obliges a pass over this file before the
branch reaches a PR.**
