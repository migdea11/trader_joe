# API reference — the declared interface surface

**This document describes `release/dataset-model` as of its docs pass for the dataset-model PR
(2026-09-29)**: the tree after the per-dataset bar model, owner-scoped writes, the filtering bar
read, per-request database sessions and owner redaction landed. **It pins no commit SHA.** This
branch is regrouped before review, and a regroup rewrites every SHA on it — both earlier pins
(`a96ede9`, then `5ed8eb1`) stopped resolving that way. What each entry cites instead is a file and
a symbol. Check those before trusting an entry: if the file or symbol an entry names no longer
exists, the entry is stale.

**Updated for the UI read routes (2026-10).** Since that pass the tree gained the `/ui/v1` read
routes the web UI uses (U1 to U5 below), the by-id read `GET /store/{id}` (S6), half-open ranges, and an
optional `expiry` that defaults to none. Those entries and the entries they changed say so. The
server code now lives under `server/`: file paths in this document are relative to it, and the
manifest is at `server/routers/tests/interface_manifest/`.

**Updated for the collection record and live events (2026-10).** The tree then gained the dataset's
run history and coverage strip (U6, U7), the live-event socket (U8), the Viewer's page index (U9), the
`ingest_run` record behind S1, and two request-body fields that are accepted and ignored. The U5
`limit` maximum went from 10,000 to 50,000. Those entries and the ones they changed say so. At that
pass there was no request log, queue or scheduler, no write route under `/ui/v1`, and no ops or
collection-statistics read; the next paragraph says what has changed since.

**Updated for retention and usage (2026-10).** The tree then gained the operator writes, set or clear
a dataset's expiry (W1) and delete a dataset (W2), with an append-only override log and deletion
record; the expiry reaper and its pending read (U10); usage tracking, with usage fields and filters on
the dataset list, facets and detail and five statistics routes (U11 to U15); and the `X-Client-Id`
read label that SDK clients send ([the read label](#the-x-client-id-read-label)). `GET /internal/asset-data/...` (S5) and U5 now record a
read. Still not built: a request log or queue (the store drives a fetch per `POST /store`), vendor
quota counting, and any history of storage growth.

**Updated for collect (2026-10, decision `tj-hhy3dk`).** An expiry is now the deletion date only: it no longer
retires a `DAILY` or `STREAM` dataset. Whether such a subscription is collected is its own `collect` intent,
set by an optional `collect` on S1 and by a new write, W3 (`PATCH /ui/v1/datasets/{dataset_id}/collecting`).
`DatasetSummary` gained `collecting`; `RETIRED` keeps its name on the wire and now means collect is false.

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

That rule used to break in one place: **R1's address was a Kafka topic**, not a path. The Kafka
removal (`tj-3mk3u5`) settled it by retiring the entry outright rather than re-keying it. data_ingest's
interface is now the gRPC `IngestService`, which by ADR `tj-8konfu` D3 lives outside `routers/` and so
is pinned where it lives — `data/ingest/tests/test_grpc_host.py` — instead of in this manifest.

**One known blind spot in the manifest: matching is by path, not by method.** An interface enum
member carries a path and no method, so the manifest cannot tell `GET /store/{id}` from
`DELETE /store/{id}`. Binding the DELETE is enough to make the path count as bound, and the unserved
GET at the same path never reaches the manifest's `unbound-path` list. This is the honest limit of
what a declaration says. It hides nothing today: the GET at `/store/{id}` is served and has its own
manifest line (S6), but a declaration added later that shares a path with a bound method would be
hidden the same way.

### Stability markers

Every entry carries one. This is the field that carries most of this document's value — a flat
reference table without it would be actively misleading, because most of this surface is scheduled to
change.

| Marker | Meaning |
|---|---|
| **Stable** | Expected to survive the migrations in flight, in substance. Its implementing file still moves at the monorepo split. |
| **Route survives, outbound hop changes** | Callers keep the same address; what the handler calls downstream is replaced. No entry carries it today — S1 was the only one, and its hop has already changed. |
| **Fate undecided** | Genuinely not yet decided. Do not guess, and confirm before building on it. |
| **Filed for implementation** | Decided and filed as work. Not built yet; the entry says which task owns it. |

Two markers that appeared in the first version of this document — *Proposed for deletion* and
*Proposed for implementation* — are gone. Both meant "the architect recommends, the user has not
ruled". **The user ruled on 2026-09-23**: the three deletion recommendations were taken, and the one
implementation recommendation was filed and has since been built (S6). Nothing on this surface is waiting on a
ruling any more. The deleted declarations are recorded under
[Declarations deleted by ruling](#declarations-deleted-by-ruling-2026-09-23) rather than dropped, so
the reasoning survives the deletion.

A third marker, **Dies with Kafka**, has also been retired — and it is worth saying why, because it
is the one marker in this document that turned out to be **wrong**. It meant "scheduled for deletion
along with the Kafka transport", and it was applied to C2 and C3. The Kafka transport is now gone and
**C2 and C3 are still here**: what died was the latency harness's Kafka *arm*, not the routes. A
reader who had acted on the marker would have deleted live code. The two entries are marked **Stable**
below, and the harness's own entry says what actually changed.

Every implementing file on this surface moved at the monorepo split (`tj-iontkq`) and is now under
`server/`, so that is not repeated per entry.

## The surface at a glance

**Twenty-seven** interfaces are visible to the manifest: three in `common.manifest` and twenty-four in
`data_store.manifest`. The count is taken from `routers/tests/interface_manifest/*.manifest`, not from
the prose below. `data_ingest.manifest` holds a single `none` line,
which is a declaration that the component exposes nothing this manifest can enumerate, not an
interface; its real surface is the gRPC `IngestService`, pinned outside `routers/`.

It was nine until the Kafka removal, when R1 left the table.

| # | Address | Component | Kind | Auth | Stability |
|---|---|---|---|---|---|
| C1 | `GET /ping` | common | http | none | **Stable** |
| C2 | `/latency/{latency_type}` | common | unbound-path | none | **Stable** |
| C3 | `/latency_internal` | common | unbound-path | none | **Stable** |
| S1 | `POST /store/{asset_type}/{data_type}/{asset_symbol}` | data_store | http | instance secret | **Stable** (outbound hop already changed) |
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

data_ingest has no row. Its interface is the gRPC `IngestService.FetchDataset`, declared in
`proto/trader_joe/proto/internal/ingest/v1/ingest.proto`, handled by
`routers/data_ingest/fetch_dataset_handler.py :: IngestFetchHandler` and registered in
`data/ingest/app/grpc_host.py`. It is outside this document's REST-shaped inventory by ADR
`tj-8konfu` D3, and pinned by `data/ingest/tests/test_grpc_host.py`.

**One row, one manifest line.** The method-blind blind spot that once hid `GET /store/{id}` is closed
by serving it: it is S6 and has its own manifest line.

**`unbound-path` is now a two-entry kind, and both are the latency pair.** That is the state the
kind was invented to describe: written code that mounts only under an environment flag. No
data_store or data_ingest declaration sits unserved any more.

**Five entries have left this surface since the inventory behind this document was taken.** One was a
real route: `DELETE /internal/asset-data/{asset_type}/{data_type}`, removed under `tj-h7ikz2` — handler,
enum member and manifest line in one diff, which is what the manifest's rule demands. Three
were *declarations* that no code ever served, deleted under `tj-wc4pe8` and `tj-427x50` by user ruling; see
[Declarations deleted by ruling](#declarations-deleted-by-ruling-2026-09-23). The fifth is R1, the
Kafka RPC handler, deleted with the transport under `tj-3mk3u5.11`; the `rpc` kind that enumerated it
went with it on `tj-3mk3u5.32`, since no registration was left for it to describe. If you are reading an
analysis or task note that says thirteen interfaces, it predates that route's removal; one that
says twelve predates the first deletion by ruling; one that says nine predates the Kafka removal.

### The four kinds

- **`http`** — a FastAPI route registered on a module-scope `APIRouter` while the module body ran.
- **`ws`** — a FastAPI WebSocket route on a module-scope `APIRouter`. One today, U8. It has no
  response model, so the manifest's `response` field is `-`; its frame type is pinned by the events
  tests.
- **`none`** — the component exposes nothing this manifest can enumerate, said out loud. A single
  `none` line is a declaration, which an accidentally empty file is not (decision `tj-3wgh03`).
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

**One ruled known gap: an unknown or misspelled symbol is not an error.** It is served empty — HTTP
**200**, `data_points` 0, and a `served_range` covering the window that was asked for. That is a
user ruling, not an oversight, and the refusal is filed as `tj-lldllr`. Because `served_range` is in
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
| **One database session per request** | Every store route gets its session from the `async_db` dependency (`data/store/app/database/database.py`), which opens it through `PostgresSessionFactory.AsyncSessionHandle.session()` and closes it when the request ends, including when the request raises. Closing rolls back any transaction still open and returns the connection to the pool. There is no `get_session` and no task-scoped registry behind it; the registry it replaced leaked one pooled connection per read request (decision record `tj-8z213c`). |
| **Database errors** | A write rolls back once, is logged once at ERROR with its traceback, and **re-raises the original SQLAlchemy error**. No exception handler is registered, so over HTTP it is a bare **500**, as before. A typed conversion (a 503-class status with a structured body) is planned at the store app's HTTP boundary under an error-handling standard that is proposed and **not yet accepted** (`tj-fa1rpu`). |

**Status codes the store routes can answer by design:**

| Status | Meaning | Routes |
|---|---|---|
| 401 | Instance secret missing, wrong, or not configured on the server. One fixed message for all three causes. | S1, S3, S4, W1, W2, W3 |
| 403 | The declared `owner` does not own the entry (a missing owner is treated as a wrong one) | S3 |
| 404 | No entry or dataset with that id | S3, S6, U4, U5, U6, U7, U9, W1, W2, W3 |
| 409 | The create overlaps the same owner's existing dataset; the top-level `colliding_ids` member of the problem+json lists them | S1 |
| 422 | Validation: a naive datetime, an unknown field or parameter, a missing required field, a bars query naming no selector or a blank symbol, a `validate_fields` rule on the create body, an `asset_type`/`data_type` pair other than `stock`/`market_activity` on the internal asset-data path, or a malformed single-bar body; a `STREAM` create or a naive `ready_by` on S1; a coverage read for a dataset no trading calendar covers (U7) | all |
| 409 | `OVERRIDE_REQUIRED`: the dataset belongs to another owner and the operator override is missing, unconfirmed or blank. The body never names the owner | W1, W2, W3 |
| 503 | `DATABASE_UNAVAILABLE`: the audit tables are missing and the write needs one (run `make migrate`) | W1, W2, W3 |
| 500 | Anything unmapped: a database error, a non-stock `asset_type` on S1 (gap **G8**), a duplicate timestamp inside one ingested batch | all |

---

# routers/common

## C1 · `GET /ping`

| | |
|---|---|
| **Stability** | **Stable.** The gRPC work (`tj-8konfu`, landed) keeps REST for admin, debug and health rather than re-hosting it. |
| **Auth** | none |
| **Implementation** | `routers/common/ping.py :: ping` *(as of the dataset-model PR)* |
| **Request** | none |
| **Response** | undeclared — no `response_model`; the handler returns an ad-hoc dict |
| **Touches** | nothing — no Postgres, no gRPC, no broker |

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
| **Stability** | **Stable.** It outlived the transport it was built to measure; see below. |
| **Auth** | none |
| **Declared at** | `routers/common/app_endpoints.py :: InterfaceRest.LATENCY` *(as of the dataset-model PR)* |
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
Recorded on `tj-a0s7vl`.

**It used to be marked "Dies with Kafka". That was wrong.** It was a Kafka-versus-REST latency
harness, and the reasoning was that it would die with the transport it compared against. What
actually happened on `tj-3mk3u5.35` is that the **Kafka arm** was removed and the harness kept: the
gRPC cutover (`tj-8konfu`) added gRPC as an arm, and `LatencyRequest.LatencyType` is now exactly
`REST` and `GRPC`. `routers/common/app_endpoints.py` no longer declares `InterfaceRpc` at all. So the
harness now measures REST against gRPC, which is a live comparison rather than a retrospective one,
and there is no scheduled deletion for it.

## C3 · `/latency_internal`

| | |
|---|---|
| **Stability** | **Stable**, with C2 — it exists only as C2's echo target. |
| **Auth** | none |
| **Declared at** | `routers/common/app_endpoints.py :: InterfaceRest.INTERNAL_LATENCY` *(as of the dataset-model PR)* |
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
| **Stability** | **Stable.** The address never changed, and its outbound hop already has: the Kafka forward to data_ingest became a gRPC call on `tj-3mk3u5.10`. |
| **Auth** | **instance secret** (`X-Instance-Secret`), fail-closed; 401 otherwise |
| **Implementation** | `routers/data_store/asset_dataset_store.py :: store_data` *(as of the dataset-model PR)* |
| **Request** | `schemas.data_store.asset_dataset_store.StoreAssetDatasetPath` + `...StoreAssetDatasetBody` |
| **Response** | `schemas.data_store.asset_dataset_store.StoreAssetDatasetResponse`: `message`, `data_points` (bars written) and `served_range` |
| **Touches** | Postgres (`AsyncSession`) **and** gRPC (`common.rpc.clients.ingest_fetch.IngestFetchClient`) |

This is the write path: it creates (or resolves) the dataset entry, triggers a fetch, forwards the
request to data_ingest through `store_market_activity_worker`, and writes the returned bars against
that entry. When the transport moves to gRPC (`tj-8konfu`), the seam that survives is
`store_market_activity_worker`'s contract, not the gRPC client — anything built against the
transport is rewritten at the cutover.

**The body is the dataset's identity.** `owner` (required, no default), `source`, `granularity`,
`start` (required), `end` (optional; omitted means open-ended), `expiry_type` (default `BULK`) and
`update_type` (default `STATIC`), plus `expiry` (optional; omitted or `null` means the dataset
**never expires**). There is no `feed` field. The entry is resolved as described in
[What a create does](#what-a-create-does): an exact repeat reuses the existing entry, an overlap with
the same owner's dataset is a **409** carrying `colliding_ids`, anything else creates.

**`expiry` changed meaning in this release, a public contract change.** It used to default to one day
after the request; it now defaults to none, and a caller that relied on the old default now gets a
dataset that never lapses. Send an explicit `expiry` to get a dataset that does. Expiry is not
identity, so an exact repeat of a request reuses the entry and **sets its expiry from the new body**: a
repeat POST that omits `expiry` clears a stored one, and a repeat that carries an `expiry` sets it. An
expiry is the deletion date only and retires nothing, on any update type.

**`collect` (optional boolean) is the subscription's collecting intent, an additive field of the public
contract** (decision `tj-hhy3dk`). It applies to `DAILY` and `STREAM` only; on a `STATIC` request it is a
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

**`ready_by` and `best_effort` are accepted, validated and ignored.** `ready_by` is optional and must
be timezone-aware (a naive value is a 422); `best_effort` is a boolean, default `false`. Nothing
stores or reads either, so they change nothing about the request, are not part of the dataset's
identity, and do not appear on S2 or S6. They are on the body so a client can send them ahead of the
scheduler that will use them.

**Every call is recorded as an ingest run.** S1 writes one `ingest_run` row around the fetch, in its
own transactions: `RUNNING` before the stream opens, the dataset id at ingest's acknowledgement, and
`SUCCEEDED` (bars written), `EMPTY` (the vendor held none) or `FAILED` (with the typed error) at the
end. The record is best effort: a failure to write it is logged and the fetch goes ahead, and the
error S1 answers with is the fetch's own. A run left `RUNNING` by a process that stopped is marked
`FAILED` with reason `INTERRUPTED` at the next start. The runs are read through U6, and S1 also
publishes to U8: an `IngestRunChanged` at each of the three points and a `DatasetChanged` `UPDATED`
after the commit.

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
none) and its own `expiry`. Returns a **list**; to read a single entry by id, use `GET /store/{id}` (S6).

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
| `start`, `end` | the bar `timestamp`, **half-open** (`timestamp >= start`, `timestamp < end`): a bar stamped exactly at `end` is not returned and a bar at `start` is; timezone-aware only |

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

The web UI is served under `/ui/v1` by fourteen read routes, one socket and three writes (W1, W2, W3). The reads are open like the other reads. The three writes need the instance secret. In a deployment the web container's proxy is what a browser reaches: it forwards the reads and exactly those three writes, and adds the secret itself.
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
body. Only the writes (W1, W2, W3) take a body, and W2's is optional.

**Common behaviour.**

| Topic | Rule |
|---|---|
| Ranges | Half-open `[start, end)`: `start` included, `end` excluded. Datetimes must be timezone-aware. |
| Paging | Keyset, never offset. `next_cursor` is opaque and empty on the last page; pass it back as `cursor`. A dataset added or removed between pages never makes a client see one twice or skip one that was ahead. A cursor that does not decode, or was issued under another sort, is a 422. |
| Unknown parameter | A 422 (`extra_forbidden`), not ignored. |
| Enum filters | Take the member name, in any case, with or without the wire prefix: `source=ALPACA_API` and `source=DATA_SOURCE_ALPACA_API` are the same filter. |
| Errors | problem+json as everywhere (see the conventions): 404 `NOT_FOUND` for an unknown dataset, 422 for a bad parameter. |
| Credentials | The reads carry none. The two writes need the instance secret (`X-Instance-Secret`). `owner` and the read label `client_id` are free-text labels, never keys. |
| The read label | A bar read (U5, S5) is recorded under the caller's `X-Client-Id` label. See [the read label](#the-x-client-id-read-label). |

### U1 · `GET /ui/v1/config`

What the shell needs to know about the deployment: `UiConfig` with `allowedGroups` (only
`ACCOUNT_GROUP_SIMULATION` for now), `deploymentLabel` (display only; empty when unset, and nothing may
branch on it; from `DEPLOYMENT_LABEL`) and `serverVersion` (`SERVER_VERSION`, else the installed
package version, else `unknown`). Nothing secret is read or returned.

### U2 · `GET /ui/v1/datasets`

One page of the catalog, a `DatasetPage` of `DatasetSummary` items: identity, range, `updateType`,
`expiryType`, `expiry` (the deletion date only), `state` (`ACTIVE`, or `RETIRED`: a `DAILY` or `STREAM` dataset whose collect intent is false), `collecting` (derived for every update type: the intent for a subscription; for a `STATIC` dataset true only while a run is in flight), the stored extent (`firstBar`, `lastBar`,
`barCount`), `freshness`, the `siblings` (the same series at other granularities) and `lastRun`: the
dataset's newest collection run, whatever its state, unset when it has never run (an `IngestRun`, see
U6), `usage` (a `DatasetUsage`, always set, see [Usage on the list](#usage-on-the-list-the-facets-and-the-detail)) and
`reaperPending`: true when the dataset's expiry has passed and the reaper would delete it (true in
dry-run too, where nothing is deleted; see U10).

**Running means a run is in flight and ingest has acknowledged it.** `lastRun.state` is `RUNNING`, and
a run is attached to a dataset only from ingest's acknowledgement, so a first-time request is not
visible as Running while the vendor is still answering. Running is not a freshness status.

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

**Freshness is computed on read, never stored**, over the source's trading calendar (a holiday or an
early close moves what is expected). A source with no calendar has no computed freshness: its status is
unspecified and it matches no `status` filter.

| Update type | Status | Meaning |
|---|---|---|
| `DAILY` | `FRESH` | the last completed session in range is stored |
| `DAILY` | `LATE` | not stored yet and the next session has not opened; pending, not a failure |
| `DAILY` | `OVERDUE` | not stored by the next session's open; a failure |
| `STREAM` | `FRESH` / `OVERDUE` | in hours, last bar within a few bar-lengths of now (`FRESHNESS_STREAM_BAR_MULTIPLE`, default 3); outside hours, relative to the last close |
| `STATIC` | `COMPLETE` | every session in `[start, end)` has at least one bar |
| `STATIC` | `GAPS` | some do not; `gapCount` is the number of missing sessions |
| `DAILY` or `STREAM`, with `collect` false | `RETIRED` | no freshness is computed; an expiry does not retire a dataset |

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
each optional, and unset leaves that side open. `limit` is 1 to 50000, default 1000 (the maximum was
10000 before 2026-10-13; the Viewer keeps reading 1,000-bar pages, and the larger maximum is for
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

An `IngestRun` carries `id`, `datasetId` (empty until ingest's acknowledgement), `label` (symbol and
granularity), `trigger`, `actor` (the request body's `owner`, a label and never a credential), the
requested `start` and `end`, `state` (`RUNNING`, `SUCCEEDED`, `EMPTY` or `FAILED`), `barsWritten`,
`error` (set only when `FAILED`: `domain`, `reason`, `message`), `startedAt` and `finishedAt` (unset
while `RUNNING`). Only `REQUEST` runs exist today: `SCHEDULE` and `RETRY` are in the vocabulary and
nothing writes them. `requestId` and `quotaKey` are always empty. A run swept at startup reads
`FAILED` with `reason` `INTERRUPTED`, which is a marker of the run record and not a member of the
error vocabulary in [`errors.md`](errors.md).

### U7 · `GET /ui/v1/datasets/{dataset_id}/coverage`

A `CoverageStrip`: the dataset's range over its trading calendar, bucketed for a strip, and its gaps.
Computed on read, never stored, from the same calendar and completed-session rule as the freshness
status (a session counts as covered when it has at least one bar). It records no usage.

| Part | Rule |
|---|---|
| `buckets` (query) | at most this many; default 120, 1 to 400, and 422 above 400. Fewer when the range holds fewer sessions |
| `start`, `end` | the range, half-open: the dataset's `start` and `end` as `DatasetSummary` reads them at `asOf` |
| `buckets` (field) | contiguous groups of near-equal session counts that tile `[start, end)`. `expectedSessions` counts completed sessions only, so a bucket with none is not yet due; `sessionsWithBars` is how many of those hold a bar |
| `gaps` | at most the first 500, ascending. A gap is a maximal run of missing completed sessions; weekends and holidays never split or form one |
| gap window | **whole sessions**: `start` is the local midnight of the first missing session, `end` the local midnight of the day after the last. It can begin before or end after the dataset's range when the range starts or ends mid-session; buckets are clipped to the range, gaps are not |
| `gapCount` | the number of gap **intervals**, which may exceed `gaps`. `Freshness.gapCount` (U2) counts missing **sessions**; for a `STATIC` dataset the sessions of all gaps add up to it |

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
| `IngestRunChanged` (carries the `IngestRun`) | a run starts, is acknowledged by ingest, and finishes |
| `DatasetChanged` `UPDATED` | after S1 or W1 commits, unless S1 flipped the collect intent, and after a daily refresh fetch commits |
| `DatasetChanged` `RETIRED` / `RESUMED` | after W3 commits a change of collect to false / true, or after S1 flips it the same way. A W3 that leaves the stored value as it was publishes nothing |
| `DatasetChanged` `DELETED` | after S3, W2 or a reaper deletion commits |
| `ReaperPendingChanged` | the reaper's pending set differs from the previous pass's, after any deletions of an enabled pass, and once on the first pass after start; in dry-run too. A notification: reload U10 or the affected lists |

`DatasetChanged` is **never `CREATED`**: a new dataset arrives as `UPDATED`,
because the upsert does not tell a create from a repeat. `RequestChanged`
is in the proto and typed, and **nothing publishes it**: it is reserved. An acknowledgement's
`IngestRunChanged` can name a dataset whose entry is not committed yet, so a reload by id may 404
briefly.

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

---

# routers/data_ingest

data_ingest declares **no HTTP interface at all**, and `data_ingest.manifest` says so with a single
`none` line. Its interface is one gRPC method.

## I1 · `IngestService.FetchDataset` (gRPC, server-streaming)

| | |
|---|---|
| **Stability** | **Stable.** This replaced the Kafka RPC entry (R1) when the transport was removed. |
| **Auth** | none |
| **Contract** | `proto/trader_joe/proto/internal/ingest/v1/ingest.proto` — the source of truth; the Python stubs under `gen/proto/python/` are generated by `make proto` and never committed |
| **Implementation** | `routers/data_ingest/fetch_dataset_handler.py :: IngestFetchHandler.fetch`, registered in `data/ingest/app/grpc_host.py :: registered_services` |
| **Request** | `FetchDatasetRequest` |
| **Response** | `stream FetchDatasetResponse` — a `oneof` over `FetchAccepted`, `FetchRefused`, `BarPage` and `FetchDone` |
| **Touches** | the broker (injected, as a `readers` mapping) |
| **Pinned by** | `data/ingest/tests/test_grpc_host.py`, which asserts in a fresh interpreter that `registered_services()` returns exactly this service |

**The shape is ack-first.** An acknowledgement is *always* the first message on the stream:
`FetchAccepted`, carrying the **resolved feed**, or `FetchRefused`, which is then the stream's only
event. Bar pages follow, each non-empty and capped at `MAX_PAGE_BARS`, and `FetchDone` terminates an
accepted stream — so a stream that merely stops is distinguishable from one that finished. That
distinction is the point of the design; do not treat an ended stream as a completed one without
`FetchDone`.

**This entry is not in the manifest, and that is deliberate.** This document's inventory is
REST-shaped and keyed on path; a gRPC method has no path to key on. ADR `tj-8konfu` D3 keeps the
generated package outside `routers/`, and decision `tj-3wgh03` D4 pins each surface where it lives,
so this one is pinned by its own test rather than by `routers/tests/interface_manifest`.

**Why R1 is gone rather than re-keyed.** The earlier version of this document said R1's address — a
Kafka topic name — would be *re-keyed* to a gRPC method, so the inventory would not read as though a
second interface had appeared. That is not what happened: `tj-3mk3u5.11` deleted the handler,
`tj-3mk3u5.32` deleted the `rpc` kind that enumerated it, and the replacement lives outside the
manifest entirely. The entry left the table rather than changing its key.

**The request carries the dataset's identity and principal.** `FetchDatasetRequest` carries `owner`,
the dataset identity fields from S1, and the window. The caller **may** name a `feed`, but the
adapter alone decides whether that tape can be served; `BarsResponse.feed` is the resolved one, and
it is what `FetchAccepted` reports back. A feed that cannot be served is refused with
`FEED_NOT_AVAILABLE` rather than quietly substituted.

**The resolved feed is stamped once per fetch, never per bar.** Every bar on the stream takes the one
feed resolved for the whole call; the encoder raises rather than overwrite a mismatch. Downstream,
`feed` is part of the dataset entry's identity (`tj-3mk3u5.31`), which is why an IEX request and a SIP
request over the same window are two datasets and not one.

**Three asset classes are accepted; one is served — and the refusal is now typed.** A non-`STOCK`
`asset_type` is refused up front with `UNSUPPORTED_ASSET_TYPE`, as are `QUOTE` and `TRADE` data
types. This is a real improvement over the Kafka path, where crypto and option were empty `pass`
callbacks that returned `None` into an `await` and surfaced as
`TypeError: object NoneType can't be used in 'await' expression`. The check sits in the handler
rather than in the reader on purpose: `BrokerRead`'s contract does not require a reader to refuse an
asset type it does not serve, so leaving it to the reader would make the answer depend on which
reader is installed. The surface still *accepts* three classes and serves one, so gap **G8** stands —
but it now fails honestly.

**A vendor failure used to arrive as success. It no longer does.** The old defect (`tj-fe19tu`) was
that the broker call swallowed every exception and returned an empty result, so a vendor error, a
rate-limit rejection and a vendor-side auth rejection all reached the caller as an *empty dataset*,
indistinguishable from a legitimately empty interval — and the swallow returned a bare `{}` rather
than the declared batch, so the observable symptom downstream was an `AttributeError` in data_store:
silent at the broker layer, misattributed at the store layer.

`BrokerRead.get_bars` now returns either a `BarsResponse` — served, possibly empty, carrying its
`served_range` — or a typed `BarsFailure`, and raises only for a bug. `tj-3mk3u5.11` deleted the
Kafka edge that turned a failure into a bare `{}`, so every typed failure reaches the gRPC servicer
and is rendered there as a `google.rpc.Status` with `ErrorInfo`. **No sentinel survives.** An empty
result now means an empty interval, and nothing else.

**Declared, and served by nothing yet.** `ingest.proto` also declares `IngestService.Quote`, and
`internal/store/v1/store.proto` declares `StoreService.Bootstrap`. Neither has a server and neither
has a caller: they are reserved for the scheduler work and `Bootstrap` is due to be rewritten.
`FetchDatasetRequest` gained `request_id`, `ready_by` and `best_effort`, which are carried and unused,
and `FetchAccepted.quota_key` and `FetchDone.vendor_stats` are on the wire but not yet mapped into
the domain.

Gaps **G1** and **G2** still apply to what happens *after* a failure is reported. G1 is narrowed:
U7 answers what a dataset holds and what is missing, per session. G2 is narrowed too: U6 shows each
failed run and its typed error. There is still no retry.

data_ingest declares **no REST interface at all** — not merely none that is bound. `tj-427x50`
deleted the last member of its `InterfaceRest` enum, and with it the empty enum class and its `Enum`
import. Its entire interface is the gRPC method above. See
[Declarations deleted by ruling](#declarations-deleted-by-ruling-2026-09-23).

---

# Not implemented: two different things

"Not implemented" used to pool three situations here. It now holds one: *never written* was emptied
by the deletions below, and *declared and unserved* (`GET /store/{id}`) was emptied when that route
was built (S6).

| Sense | Which | What it means for you |
|---|---|---|
| **Written, switched off** | C2, C3 | The code exists in `routers/common/latency.py` and mounts only when `LATENCY_TEST_ENABLED` is set. `unbound-path` here means *conditionally mounted, off in production*. The initialisers are also single-shot, so a client and a server cannot both initialise in one process. |

**The verdicts were taken, and this is now the state, not a forecast.** The `unbound-path` kind is
empty for data_store and data_ingest and survives only for the two latency entries — which is the
state that kind was invented to describe. `data_store.manifest` carries thirteen `http` lines and one `ws`
line (the five original routes, S6, the seven UI reads and the events socket); `data_ingest.manifest` carries a single
`none` line.

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
assert coverage that was never fetched. The ledger must be authoritative about what is missing, and
writable from outside it is not authoritative.

**Note the sequencing, because this document used to state it the other way round.** It read
"removing Kafka is only safe *because* the ledger is authoritative". Kafka has in fact been removed
and **the ledger has not landed** — `tj-3mk3u5.4` and `tj-3mk3u5.5` are still open. So this is an
outstanding obligation, not a satisfied precondition: until the ledger exists there is no
authoritative answer to what is missing, and no replacement for the consumer-group lag the removal
took away.

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
as the ingest fetch call, but reachable by a human or a script instead of by data_store. *Deleted as a defer, not a no —
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

**Narrowed since:** U7 answers the question for the UI, per dataset, computed on read over the trading
calendar. It is not the ledger: it says which sessions hold at least one bar, not what was fetched,
and the older read routes (S2, S5) still say nothing about completeness.

## G2 · No way to see or retry a failed fetch — **REAL GAP**, and worse than merely missing

The broker call swallows every exception and returns an empty result (`tj-fe19tu`), so a vendor error,
a rate-limit rejection and an auth failure all arrive at the caller as an empty dataset —
indistinguishable from a legitimately empty interval. There is no error surface, no fetch status and
no retry anywhere on the declared surface.

The bug is filed; the **interface** consequence is not. Even once the bug is fixed, nothing in this
surface reports fetch outcome, and a swallowed error becomes a permanent silent gap that the ledger
will record as fetched.

*Recommendation:* one status read answers G1 and G2 together. Design them as one interface, not two.

**Narrowed since:** every store-driven fetch is now recorded as an ingest run, with its typed error,
and U6 and U8 expose that. A failed fetch is therefore visible. A retry is not built, and a run's
history is per dataset: a request that failed before any dataset existed is listed under no dataset.

## G3 · No health/readiness distinction beyond `/ping` — **REAL GAP**, with a dated trigger

`GET /ping` is answered by a handler that touches nothing — no Postgres, no gRPC, no broker — and it
is the container healthcheck for *both* services, which `make prod-launch` and CI's `up --wait`
block on. So a container with a dead Postgres pool, unreachable peer or exhausted broker credentials
reports healthy, and the launch reports success. (data_store's own start waits on postgres being
healthy, through its own probe, not on data_ingest.)

The sharper version of this is now live, not forecast: both healthchecks probe HTTP `/ping`, so a
container whose **gRPC** listener is dead still reports healthy — and since `tj-8konfu` landed, the
gRPC listener is the one that carries all inter-service traffic. The gap got wider when the transport
moved, because `/ping` says even less about the service than it used to.

*Recommendation:* a readiness endpoint that actually checks dependencies, the gRPC listener included.
`/ping` stays as liveness and keeps its current shape. Still unfiled.

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
symbol. Neither takes `limit` or `offset`. The bar table is unbounded with no retention policy, so one
liquid symbol at minute granularity is still a large answer, returned 200 in one response. The UI's
routes do page (U2 and U5 take a keyset `cursor` and a `limit`); the gap is that S2 and S5 do not.

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

**The ingest half of this is fixed.** It used to be that the RPC handler re-validated into a stock,
crypto or option request and the crypto and option callbacks were empty synchronous stubs returning
`None` into an `await`, so the caller got a `TypeError` rather than a clean rejection. The gRPC
handler now refuses a non-`STOCK` `asset_type` up front with `UNSUPPORTED_ASSET_TYPE`, and refuses
`QUOTE` and `TRADE` data types the same way.

The data_store internal family is **fixed**: S4 and S5 refuse an unsupported pair at the path model
with a 422, before the handler runs. Their `UnsupportedAssetType` branch is still in the handler but
is no longer reachable over HTTP.

Stock-only is the correct **scope** today. But an interface that accepts an argument it will 500 on is
a contract defect regardless of scope.

*Recommendation:* state stock-only in terms — done, here. The ingest side now rejects cleanly and S4
and S5 refuse at the path model. **What is left is S1**, which still accepts an `asset_type` it
cannot serve and discovers it only downstream; it should refuse with a 4xx of its own. Cheap.

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

This is a snapshot with named successors, not a stable reference. Two of the four pieces of work this
section used to forecast have now **landed**, and they are kept here with what they actually did —
because in both cases the prediction and the outcome differ, which is the most useful thing this
table can record:

| Work | What it changed here | Status |
|---|---|---|
| `tj-3mk3u5` — Kafka removal | **Predicted:** deletes C2 and C3, re-keys R1's address, changes S1's outbound hop. **Actually:** C2 and C3 *survived* — only the harness's Kafka arm went; R1 was *retired*, not re-keyed, and its replacement sits outside this manifest; S1's hop did change, as forecast. | **Landed** |
| `tj-8konfu` — gRPC surface | Re-hosted the fetch call as `IngestService.FetchDataset` and added a contract this document does not fully cover (see `proto/README.md`). It did **not** settle versioning (G11) or readiness (G3); both are still open. | **Landed** (accepted, not "proposed") |
| `tj-iontkq` — monorepo split | Moved the server code under `server/`, internals unchanged. Every file path above is now relative to `server/`; addresses survived. | **Landed** |
| `tj-a0s7vl` — authentication | Adds a real credential to every entry, replacing both `auth: none` and the deployment-level instance secret. | Pending |

The two landed rows stay in the table rather than being deleted: a reader who finds an older analysis
predicting a re-keyed R1 or a doomed C2 needs to be able to see that those predictions were not borne
out.

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
- `GET /store/{id}` has landed as S6; no entry in this document is declared and unserved any more.

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
