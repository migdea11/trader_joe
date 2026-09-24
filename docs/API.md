# API reference — the declared interface surface

**This document describes commit `a96ede9`** (2026-09-23). The surface it covers is moving quickly:
of the nine interfaces catalogued here, three are expected to survive the migrations already in
flight unchanged in substance. Four entries left this surface on 2026-09-23 — one route removed just
before this document was first written, and three declarations deleted by user ruling just after.
Check the commit pin before trusting anything below.

## Read this first

**Nothing on this surface requires a credential.** Every interface in this document is
unauthenticated, including a destructive one (`DELETE /store/{id}`) and, when it is switched on, a
request amplifier (`GET /latency/{latency_type}`). Each entry carries an explicit `auth` field so
this cannot be missed route by route. See gap **G4** — this is the blocking gap, owned at P1 by
`tj-a0s7vl`.

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

**Entries are keyed on address, not on file path.** The Phase 1 restructure (`tj-55cczk`) moves every
implementing module into `src/trader_joe/`, so every file path in this document goes stale in one
diff while the addresses mostly survive. File and symbol are still given — they are what makes an
entry checkable — but they are marked *as of `a96ede9`* and are deliberately never woven into prose,
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
| **Stable** | Expected to survive the migrations in flight, in substance. Its path still moves at Phase 1. |
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

Every path on this surface moves at Phase 1 (`tj-55cczk`), so that is not repeated per entry.

## The surface at a glance

**Nine** interfaces are visible to the manifest at `a96ede9`, plus one declaration the manifest
cannot see (`GET /store/{id}`, per the method-blind matching limit above). Three of the nine are
stable. The count is taken from `routers/tests/interface_manifest/*.manifest` — three lines in
`common.manifest`, five in `data_store.manifest`, one in `data_ingest.manifest` — not from the prose
below.

| # | Address | Component | Kind | Stability |
|---|---|---|---|---|
| C1 | `GET /ping` | common | http | **Stable** |
| C2 | `/latency/{latency_type}` | common | unbound-path | **Dies with Kafka** |
| C3 | `/latency_internal` | common | unbound-path | **Dies with Kafka** |
| S1 | `POST /store/{asset_type}/{data_type}/{asset_symbol}` | data_store | http | **Route survives, outbound hop changes** |
| S2 | `GET /store/{asset_type}/{data_type}/{asset_symbol}` | data_store | http | **Stable** |
| S3 | `DELETE /store/{id}` | data_store | http | **Stable** |
| S4 | `POST /internal/asset-data/{asset_type}/{data_type}` | data_store | http | **Fate undecided** |
| S5 | `GET /internal/asset-data/{asset_type}/{data_type}` | data_store | http | **Fate undecided** |
| R1 | `stock_market_activity_rpc` | data_ingest | rpc | **Address is re-keyed** |
| — | `GET /store/{id}` | data_store | *invisible to the manifest* | **Filed for implementation** (`tj-2h1q3k`) |

**Why nine and not ten.** The last row is the one entry in this table that is not a manifest line,
and it is marked so. The manifest matches by path and not by method, so `GET /store/{id}` is counted
as bound by the `DELETE` at the same path and never appears as its own line. Every other row here is
a manifest line, one for one. That single discrepancy is deliberate, and it is the reason a count
taken from this table and a count taken from the manifest differ by exactly one.

**`unbound-path` is now a two-entry kind, and both are the latency pair.** That is the state the
kind was invented to describe: written code that mounts only under an environment flag. No
data_store or data_ingest declaration sits unserved any more.

**Four entries have left this surface since the inventory behind this document was taken.** One was a
real route: `DELETE /internal/asset-data/{asset_type}/{data_type}`, removed at `3cb5bea` — handler,
enum member and manifest line in one diff, which is what the manifest's rule demands. The other three
were *declarations* that no code ever served, deleted at `fc377e4` and `ab42ec4` by user ruling; see
[Declarations deleted by ruling](#declarations-deleted-by-ruling-2026-09-23). If you are reading an
analysis or task note that says thirteen interfaces, it predates `3cb5bea`; one that says twelve
predates `fc377e4`.

### The three kinds

- **`http`** — a FastAPI route registered on a module-scope `APIRouter` while the module body ran.
- **`rpc`** — a handler registered through `KafkaRpcFactory.add_server()` while the module body ran.
  Enumerating one is proof the registration actually happened at import; if it stops happening, the
  service starts and answers nothing.
- **`unbound-path`** — a path declared in an interface enum that no import-time route serves. This
  kind exists because the import-time surface is not the whole declared surface, and that gap was
  invisible before the manifest existed.

---

# routers/common

## C1 · `GET /ping`

| | |
|---|---|
| **Stability** | **Stable.** Not Kafka-borne, and the gRPC work (`tj-8konfu`) keeps REST for admin, debug and health rather than re-hosting it. |
| **Auth** | none |
| **Implementation** | `routers/common/ping.py :: ping` *(as of `a96ede9`)* |
| **Request** | none |
| **Response** | undeclared — no `response_model`; the handler returns an ad-hoc dict |
| **Touches** | nothing — no Postgres, no Kafka, no broker |

**Contract: a 2xx status.** Treat the status as the contract and not the body. `docker-compose.yaml`
makes this route the container healthcheck for *both* services, and the probe reads and discards the
response body — so a body assertion pins a string nothing reads.

It is served by both applications but declared once, here, because the manifest enumerates routers
and nothing enumerates applications. One interface, one implementation, one entry.

Because the handler touches nothing, a 2xx from it says only that the web server is up. See gap
**G3** — this is the entire health story today, and the dependency ordering between the two services
is built on it.

## C2 · `/latency/{latency_type}`

| | |
|---|---|
| **Stability** | **Dies with Kafka** — after one last job. See below; this is *not* dead code to delete today. |
| **Auth** | none |
| **Declared at** | `routers/common/app_endpoints.py :: InterfaceRest.LATENCY` *(as of `a96ede9`)* |
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
| **Declared at** | `routers/common/app_endpoints.py :: InterfaceRest.INTERNAL_LATENCY` *(as of `a96ede9`)* |
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
| **Auth** | none |
| **Implementation** | `routers/data_store/asset_dataset_store.py :: store_data` *(as of `a96ede9`)* |
| **Request** | `schemas.data_store.asset_dataset_store.StoreAssetDatasetPath` + `...StoreAssetDatasetBody` |
| **Response** | **undeclared** — ad-hoc dict |
| **Touches** | Postgres (`AsyncSession`) **and** Kafka (`KafkaRpcFactory.RpcClients`) |

This is the write path: it triggers a fetch and forwards the request to data_ingest through
`store_market_activity_worker`. When the transport moves to gRPC (`tj-8konfu`), the seam that
survives is `store_market_activity_worker`'s contract, not the Kafka client — anything built against
the transport is rewritten at the cutover.

**Known defect, live at this commit:** a request missing `start` answers 500, not 422 (`tj-6yk4qs`).
The mechanism is an asymmetry between two schemas: `StoreAssetDatasetBody` makes `start` optional, so
the request passes validation and is accepted; the handler then forwards it into `GetDatasetRequest`,
where `start` is **required**, and the resulting `ValidationError` is raised *inside* an
already-accepted request. No exception handler is registered anywhere, so it surfaces as a 500. Only
`start` behaves this way — `expiry` is required downstream too, but the body supplies a
`default_factory`, so it never trips.

It is the highest-value interface in this component and the one whose priority changes most: today
the cost of a duplicate or malformed call is a wasted vendor fetch, but this is the shape of route the
order/fill event log will eventually sit behind. See gaps **G6** (undeclared response) and **G10**
(no idempotency).

## S2 · `GET /store/{asset_type}/{data_type}/{asset_symbol}`

| | |
|---|---|
| **Stability** | **Stable.** The Kafka removal (`tj-3mk3u5`) explicitly keeps REST for read and debug. |
| **Auth** | none |
| **Implementation** | `routers/data_store/asset_dataset_store.py :: get_data` *(as of `a96ede9`)* |
| **Request** | `...StoreAssetDatasetPath` + `...StoreAssetDatasetQuery` |
| **Response** | `list[schemas.data_store.asset_dataset_store.AssetDatasetStore]` |
| **Touches** | Postgres |

Returns a **list**, and has no way to address a single entry — which is the argument for implementing
`GET /store/{id}`, below.

It accepts query parameters but no `limit` or `offset`; see gap **G5**.

It is the one route on this surface that declares its response through a **return annotation** rather
than the `response_model=` keyword. Both are equally binding to FastAPI and the manifest records them
identically, so do not read the difference as significant — it is noted only so that grepping for
`response_model=` does not make this route look undeclared.

## S3 · `DELETE /store/{id}`

| | |
|---|---|
| **Stability** | **Stable** as REST. Its *address* is contested — see the note below. |
| **Auth** | **none — and this is a destructive route** |
| **Implementation** | `routers/data_store/asset_dataset_store.py :: delete_data` *(as of `a96ede9`)* |
| **Request** | `schemas.data_store.asset_dataset_store.AssetDatasetStoreDelete` |
| **Response** | **undeclared** — ad-hoc dict |
| **Touches** | Postgres |

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
at `3cb5bea`. Nothing on the served surface reaches it today.

Deletion is also the one operation the append-only event-log rule (`tj-qqdo3j`) will forbid outright
on the order path. When that lands, the question worth asking of this route is whether it can reach
event-log rows at all.

## S4 · `POST /internal/asset-data/{asset_type}/{data_type}`

| | |
|---|---|
| **Stability** | **Fate undecided — do not guess.** See below. |
| **Auth** | none |
| **Implementation** | `routers/data_store/internal_asset_data.py :: create_stock_market_activity_data` *(as of `a96ede9`)* |
| **Request** | `schemas.data_store.asset_data_interface.AssetDataPath` + **an untyped `dict` body** |
| **Response** | `schemas.data_store.stock.market_activity_data.StockDataMarketActivity` |
| **Touches** | Postgres, plus that untyped body |

**Recently fixed:** until `2ea730b` this handler read a field its path model does not have, so every
request raised. It now stores the row it creates — the handler persists, commits and refreshes the
row, and returns it. Re-verified against the file at `a96ede9`; the route is now driven end to end by
a smoke test rather than carrying an xfail.

**The body is an untyped `dict`**, passed as `StockDataMarketActivityCreate(**asset_data)`. That is
why the manifest's `touches` field for this route reads `dict` — there is no schema to name. A
renamed producer field becomes a missing column rather than an error. See gap **G7**.

**Why the fate is undecided and nobody should guess it:** nothing in the repository calls this route
and no accepted decision record names it. `tj-3mk3u5` keeps REST for admin and debug, which this
plausibly is — but it is also exactly the data_store↔data_ingest write path that `tj-8konfu` moves to
gRPC. Confirm before building on it.

A non-stock `asset_type` raises `UnsupportedAssetType` (a `ValueError`) rather than returning a 4xx —
the same contract defect described in gap **G8**.

## S5 · `GET /internal/asset-data/{asset_type}/{data_type}`

| | |
|---|---|
| **Stability** | **Fate undecided** — same route family and same reasoning as S4. |
| **Auth** | none |
| **Implementation** | `routers/data_store/internal_asset_data.py :: read_stock_market_activity_data` *(as of `a96ede9`)* |
| **Request** | `schemas.data_store.asset_data_interface.AssetDataPath` |
| **Response** | `list[schemas.data_store.stock.market_activity_data.StockDataMarketActivity]` |
| **Touches** | Postgres |

**Being fixed — do not treat current behaviour as the contract.** This route returns **every row** of
the market-activity table, with no filtering and no bound. Until `315a043` it carried a dead query
branch that made it look as though filtering existed; that branch was removed, so the route now
plainly does what it always did. Real filtering is `tj-6z03hd`, which is **in progress and
escalated**: the query model cannot currently express "no filter", which is a fix in `schemas/`, and
adding a query parameter also changes this route's manifest line.

The intended contract is a filtered, bounded read. Cite `tj-6z03hd`, not this paragraph, for what it
will become. See gap **G5**.

A non-stock `asset_type` raises `UnsupportedAssetType` here too — gap **G8**.

## `GET /store/{id}` — declared, unserved, and invisible to the manifest

| | |
|---|---|
| **Stability** | **Filed for implementation** — `tj-2h1q3k`, user ruling 2026-09-23. Not built yet. |
| **Auth** | n/a — nothing is served **yet** |
| **Declared at** | `routers/data_store/app_endpoints.py :: AssetDatasetStoreInterface.GET_STORE_ASSET_DATASET_BY_ID` *(as of `a96ede9`)* |
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
| **Implementation** | `routers/data_ingest/get_dataset_request.py :: store_data` *(as of `a96ede9`)*, registered by the `add_server` decorator while the module body runs |
| **Request** | `schemas.data_ingest.get_dataset_request.GetDatasetRequest` |
| **Response** | `schemas.data_store.stock.market_activity_data.BatchStockDataMarketActivityCreate` |
| **Touches** | Kafka (the transport itself) and the broker, reached through a module-level import rather than injection |

**The keying rule breaks here, and this is the flag.** This document keys entries on address because
an HTTP path survives the Phase 1 re-path. **A Kafka topic name does not survive the Kafka removal.**
When the gRPC re-host lands (`tj-8konfu`), this entry is *re-keyed* to the gRPC method name. That
re-key is the entry's migration event and belongs here as an addendum to this entry — not as a new
entry, or the reference reads as though a second interface appeared.

This is the most consequential migration line on the surface. The transport is deleted by `tj-3mk3u5`
and the method is re-hosted on gRPC by `tj-8konfu`; what survives the cutover is `store_data`'s own
contract — `GetDatasetRequest` in, `BatchStockDataMarketActivityCreate` out — because the gRPC work
decides the surface while the message shapes stay put. **Build against the handler signature, not the
transport.**

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

data_ingest declares **no REST interface at all** now — not merely none that is bound. `ab42ec4`
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

| Was | Declared at | Deleted by | Bead |
|---|---|---|---|
| `/internal/asset-data/{asset_type}/{data_type}/{id}` | `AssetDataInterface.PUT_ASSET_DATA` | `fc377e4` | `tj-wc4pe8` |
| `/store/{asset_type}/{data_type}/{asset_symbol}/{id}` | `AssetDatasetStoreInterface.PUT_STORE_ASSET_DATASET` | `fc377e4` | `tj-wc4pe8` |
| `/broker/{asset_type}/{symbol}/{data_type}` | `routers/data_ingest` `InterfaceRest.POST_STORE_DATASET` | `ab42ec4` | `tj-427x50` |

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
is the container healthcheck for *both* services, with data_store's startup gated on data_ingest
reporting healthy. So a container with a dead Postgres pool, unreachable peer or exhausted broker
credentials reports healthy, and the dependency gate opens.

The gRPC work (`tj-8konfu`) already names the sharper version of this: both healthchecks probe HTTP
`/ping`, so a container whose gRPC listener is dead still reports healthy.

*Recommendation:* a readiness endpoint that actually checks dependencies, landing with the gRPC work.
`/ping` stays as liveness and keeps its current shape.

## G4 · No authentication on any route — **REAL GAP**, the blocking one

Every interface in this document is unauthenticated. `tj-a0s7vl` owns this at P1 and is explicit that
loopback binding is a perimeter, not an identity. The platform design wants scoped keys
(`read:accounts`, `read:events`, `read:market`, `trade`) from day one, because the private strategy
repo consumes this surface from outside the container.

Two specifics worth naming rather than leaving to inference:

- `GET /latency/{latency_type}` is an unauthenticated **request amplifier** (C2).
- `DELETE /store/{id}` is an unauthenticated **destructive** route (S3).

*Recommendation:* this is why the document opens with the warning and why every entry carries an
`auth` field.

## G5 · No pagination or bound on reads that can return every row — **REAL GAP**

`GET /internal/asset-data/{asset_type}/{data_type}` returns every row of the market-activity table.
`GET /store/{asset_type}/{data_type}/{asset_symbol}` takes query parameters but no `limit` or
`offset`. There is no pagination anywhere on the surface, and the tick table is unbounded with no
retention policy — so this is the shape of an accidental full-table scan on the largest table the
system will own, answered 200 with a valid schema.

**This one is mid-flight.** `tj-6z03hd` is in progress and escalated; see S5. Describe the shape, cite
the task, and do not pin today's behaviour as the contract.

## G6 · Routes that declare no response model — **REAL GAP**, cheap, and it compounds

At `a96ede9` three HTTP routes return undeclared dicts: `POST /store/...` (S1),
`DELETE /store/{id}` (S3) and `GET /ping` (C1). The latency pair (C2, C3) returns ad-hoc dicts too.
*(The analysis behind this document counted four HTTP routes; the fourth was
`DELETE /internal/asset-data/...`, removed at `3cb5bea`.)*

The generated OpenAPI therefore documents nothing for them — and a **versioned typed client SDK** is
planned for the private repo (`tj-d2mhru`), which has nothing to generate from where the response
model is absent.

*Recommendation:* one chore covering all of them. Meanwhile this document marks each such entry
"undeclared" rather than transcribing the dict shape as if it were a contract.

## G7 · One untyped request body — **REAL GAP**, currently low-impact

`POST /internal/asset-data/{asset_type}/{data_type}` takes a plain `dict` and splats it into
`StockDataMarketActivityCreate`, so a renamed producer field becomes a missing column rather than an
error. It is the reason the manifest's `touches` field for that route reads `dict` — there is no
schema to name.

Low-impact only because the route had a separate defect until recently; the typing gap is unaffected
by that fix.

## G8 · The surface promises three asset classes and serves one — **REAL GAP in the contract**

The RPC handler re-validates into a stock, crypto or option request on a caller-supplied `asset_type`,
and the crypto and option callbacks are empty synchronous stubs that return `None`, which the handler
then awaits — so the caller gets a `TypeError`, not a clean rejection (R1). The data_store internal
family raises `UnsupportedAssetType` — a plain `ValueError`, not an `HTTPException` — on the same
input (S4, S5). No exception handler is registered anywhere in `routers/` or `data/store/app/`, so
each of these reaches the caller as a 500.

Stock-only is the correct **scope** today. But an interface that accepts an argument it will 500 on is
a contract defect regardless of scope.

*Recommendation:* state stock-only in terms — done, here — and reject an unsupported `asset_type` with
a 4xx instead of raising. Cheap.

## G9 · Nothing here touches accounts, portfolios, orders or fills — **NOT-YET**, unambiguously

This is stated at the top of the document as well, because it is the absence most likely to be
misread. A reader arriving from the project description will look for an account model, a portfolio
model and an append-only order/fill event log, and will find a market-data pipeline. Those surfaces
are roadmap — `tj-pznkbx`, `tj-qqdo3j`, `tj-jmrqkf` — not omissions.

## G10 · No idempotency on the write path — **REAL**, not yet urgent

`POST /store/...` triggers a fetch and forwards to data_ingest; a retried or duplicated call repeats
the vendor hop. The planned answer is single-flight collapse *inside* the pipeline (`tj-3mk3u5`),
which is not the same as a caller-facing idempotency key — the caller cannot tell a collapsed
duplicate from a served one.

Today the cost of a duplicate is a wasted fetch. It is worth naming now because this is the write
path: the moment the order/fill event log sits behind a route of this shape, a duplicate stops being a
wasted fetch and starts being money.

## G11 · No versioning on any path — **REAL GAP**, cheap to decide and expensive to retrofit

No `/v1`, no version header, bare paths throughout — while the paths are about to move wholesale at
Phase 1 and a **versioned** typed SDK (`tj-d2mhru`) is planned to consume this from another repo.

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
| `tj-55cczk` — Phase 1 restructure | Moves every implementing file into `src/trader_joe/`. Every file path above goes stale; addresses mostly survive. |
| `tj-a0s7vl` — authentication | Adds a credential to every entry, replacing the `auth: none` field throughout. |

Mid-flight at the time of writing, beyond those four:

- `tj-6z03hd` — filtering and a bound on S5. **In progress and escalated**: the query model cannot
  currently express "no filter", which is a fix in `schemas/`, and adding a query parameter also
  changes S5's manifest line.
- `tj-2h1q3k` — implements `GET /store/{id}`. When it lands, the last unimplemented entry in this
  document becomes a served route, the manifest gains a line, and the GET/DELETE collision recorded
  against S3 dissolves.

`tj-wc4pe8` and `tj-427x50` are **closed**, and their effect is already reflected above.

## Keeping this document honest

When this document and the manifest disagree, **the manifest is right**. File a bug against this
document.

This document has drifted once already, and the way it happened is worth recording. It was written
pinned to `315a043`; the next two commits deleted three of the declarations it described. Both of
those commits were correctly gated and the manifest test was green throughout, because the manifest
moved with the code exactly as designed. Nothing was broken — the document simply was not in anyone's
path. **The manifest test makes the surface impossible to change silently; it does nothing to make
this document move when the surface does.** That gap is routing, not detection, and a proposal for
closing it is recorded on `tj-d6e128`.

Until something mechanical exists, the working rule is: **a diff that touches `routers/`, `schemas/`,
an interface enum, or `routers/tests/interface_manifest/` obliges a pass over this file before the
branch reaches a PR.**
