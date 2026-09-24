# The interface manifest

One file per component — `common.manifest`, `data_store.manifest`, `data_ingest.manifest` — listing
every interface that component exposes. `routers/tests/test_interface_surface.py` imports the three
router packages, enumerates their interfaces from the live code, and asserts that what it finds
**equals** what is written here.

So a new route costs a line here, in the same diff. That friction is the point, and it is a ruling
(user, 2026-09-23: "Keep the manifest"): enumeration without a committed manifest keeps the smoke
test and loses the only thing that stops this inventory drifting silently out of date. There is
deliberately **no regenerate command** — a generator would turn the friction back into a keystroke.
When the test goes red it prints the exact lines to add or remove.

These files are also the machine-readable interface inventory that tj-dwkjg9 reads, which is why
every line carries the implementing file and symbol and the schemas by fully qualified name: it
should never be necessary to re-read the code to know what the surface is.

## Format

One interface per line, seven `|`-separated fields. Blank lines and `#` comments are ignored. Lines
are kept sorted, so a new interface is a one-line diff. A file with a malformed line, a duplicate
line, an unsorted line or no lines at all fails its component's test with the line number.

```
kind | address | file | symbol | request | response | touches
```

| Field | Meaning |
|---|---|
| `kind` | `http`, `rpc` or `unbound-path` — see below |
| `address` | `METHOD /path` for `http`, the Kafka RPC topic for `rpc`, the declared path for `unbound-path` |
| `file` | Repository-relative path of the module whose body defines the endpoint or handler |
| `symbol` | The endpoint or handler function, or `EnumClass.MEMBER` for a declared path |
| `request` | Every parameter annotated with a Pydantic model, fully qualified, sorted |
| `response` | The route's response model or the handler's declared response model, fully qualified |
| `touches` | Every other parameter annotation, fully qualified, sorted |

`-` means the entry has no value for that field; no field is ever empty.

`request` and `touches` are split on "is it a Pydantic model", because in this repository every
cross-boundary schema is one and everything else is injected plumbing. That makes `touches` the
"what it touches" field of ADR tj-fdb9gz — `AsyncSession` says the route reaches Postgres,
`KafkaRpcFactory.RpcClients` says it reaches Kafka — and it is what decides the tier of the test
each interface eventually gets. An untyped body lands in `touches` as `dict`, which is the honest
answer: there is no schema to name.

## The three kinds

- **`http`** — a FastAPI route registered on a module-scope `APIRouter` while the module body ran.
- **`rpc`** — a handler registered through `KafkaRpcFactory.add_server()` while the module body ran.
  Enumerating one is proof the registration actually happened at import; if it stops happening, the
  service starts and answers nothing.
- **`unbound-path`** — a path declared in an interface enum that no import-time route serves.

`unbound-path` exists because the import-time surface is not the whole declared surface, and the gap
was invisible before this manifest. Two sit here today, and they are the latency pair:
`routers/common/latency.py` builds its `APIRouter` *inside*
`initialize_latency_client()`/`initialize_latency_server()`, so `/latency/{latency_type}` and
`/latency_internal` appear only when `LATENCY_TEST_ENABLED` is set and a live Kafka factory is
available — which a test that touches nothing external may not do. Both paths are real and served by
committed code; they are simply not enumerable at import. That is the kind doing the job it was
invented for.

A line leaves this kind in one of two ways, and neither is quiet: implement the path and its
`unbound-path` line has to become an `http` line in the same diff, or delete the declaration and the
line goes with it in the same diff. Three entries left by that second route on 2026-09-23 — one in
`routers/data_ingest`, two in `routers/data_store` — and each was a declaration no code ever served,
not a route anyone could call. See "Declarations deleted by ruling" in `docs/API.md` for what each
was and what would bring it back; it is not restated here.

Matching is by path, not by method, because an enum member carries no method: `DELETE /store/{id}`
is therefore enough to count `GET_STORE_ASSET_DATASET_BY_ID` as bound. That coarseness is the
honest limit of what the declaration says.
