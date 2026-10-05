# proto/

**The source of truth for everything that crosses the gRPC wire.** The `.proto` files here are
written by hand, and every consumer generates its code from them. Nothing in this directory is
generated, and nothing in it is Python. (ADR tj-8konfu D1, with this root home from addendum A1; the
generated layout is decision tj-3mk3u5.42, addendum F1.)

Pydantic models stay the internal domain representation and keep the REST surface. Neither side is
generated from the other: each consumer maps between generated messages and its own types by hand,
in one place.

## Layout

```
proto/trader_joe/proto/<domain>/v1/<name>.proto
```

A file's path mirrors its proto package, and every package is versioned. `package
trader_joe.proto.ping.v1;` lives in `trader_joe/proto/ping/v1/ping.proto`.

**The `trader_joe.proto` root is reserved.** Every package is `trader_joe.proto.<domain>.v1`, and an
internal contract is `trader_joe.proto.internal.<service>.v1`. `make proto` refuses any `.proto`
outside `proto/trader_joe/proto/`. The reserved child keeps generated code apart from the hand-written
`trader_joe.common` and `trader_joe.client` packages that will share the `trader_joe` name, the way
OpenTelemetry keeps `opentelemetry.proto` apart from its API and SDK.

`trader_joe/proto/ping/v1/ping.proto` proves the codegen pipeline end to end. It is **not a
contract**: nothing depends on its shape, and the first real contract does not extend it.

## Imports and hierarchy

**One `.proto` imports another by its canonical path**, the path under `proto/`:

```proto
import "trader_joe/proto/market/v1/<file>.proto";
```

Domain messages are imported, never copied: duplicated structures drift out of sync. The hierarchy,
accepted as a starting point (tj-3mk3u5.42 F1, rule 8):

| Package | Holds |
|---|---|
| `trader_joe.proto.market.v1` | The shared vocabulary: `Bar` and the market-data enums. Imports `google/protobuf` only. |
| `trader_joe.proto.internal.ingest.v1` | `FetchDataset`, the data_store to data_ingest contract. |
| `trader_joe.proto.data.v1` | The external streaming contract. The name is provisional. |
| `trader_joe.proto.ui.v1` | The UI's messages. |
| `trader_joe.proto.ping.v1` | The pipeline proof. |

The rules:

- Every contract may import `market/v1`.
- **No contract imports another contract.**
- **An external contract never imports an internal one.**
- A transport shape wraps `Bar`. It does not restate `Bar`'s fields.
- Errors get no package (see the last section).

**Enum values carry their enum's name as a prefix** (F1, rule 9). protoc scopes an enum's values as
siblings of the enum, within the package, so two enums in one package cannot both declare
`UNSPECIFIED`. Every value is therefore `<ENUM_NAME_UPPER_SNAKE>_<NAME>`, and the zero value is
`<ENUM_NAME_UPPER_SNAKE>_UNSPECIFIED`. A hand-written mapper strips the prefix where a value maps to
a Python member name.

Imports from outside this repository:

- `google/protobuf/*.proto` (the well-known types) work as they are. protoc's bundled include
  supplies them.
- `google/rpc/*.proto` needs an extra include root onto `googleapis-common-protos`, which ships the
  `.proto` files, and that package at runtime. No contract needs it yet.

## Generated code: one committed tree per language

Generated code lives at **`gen/proto/<language>/`** at the repository root. Each language has one
tree, and every consumer in that language uses it. Python is at `gen/proto/python/` today, and
TypeScript will follow at `gen/proto/ts/`.

| Language | Tree | Regenerate |
|---|---|---|
| Python (both services; later the SDK and Python clients) | `gen/proto/python/trader_joe/proto/<domain>/v1/` | `make proto` |

The rules for a generated tree:

- **Never hand-edit it.** If a merge conflicts inside it, run `make proto` again.
- **Nothing post-processes it either.** What is committed is protoc's output, byte for byte: no
  import rewrite and no other edit. When a contract changes, the hand-written code that uses it is
  changed by hand, in review.
- **Commit it in the same commit as the `.proto` change.** CI regenerates it and fails on any
  difference: a changed file, a deleted one or an untracked one.
- **Every consumer generates from the plain `proto/` include root** (`-Iproto`). protoc names every
  generated module, writes every import in it and records every descriptor file name after the
  `.proto` path under the include root. From the plain root those names are canonical
  (`trader_joe/proto/ping/v1/ping.proto`), so every consumer records the same names, and their
  generated code can share one descriptor pool.
- The generator is pinned exactly (`grpcio-tools` in `pyproject.toml`), because its version is
  written into every file it emits. Bumping it is a deliberate change that regenerates the tree.

## Python: how the server finds and uses the tree

The Python tree mirrors the proto packages, so protoc's imports resolve as it writes them
(`from trader_joe.proto.ping.v1 import ping_pb2`) once `gen/proto/python` is on the import path.
**That is configuration, never code**: no code changes `sys.path`. The root is added in three places:

- `pytest.ini`'s `pythonpath`, for every pytest run;
- the image: `ENV PYTHONPATH=/code:/code/gen/proto/python` and one `COPY` of `./gen/proto/python`
  (`Dockerfile`);
- the compose bind mounts, one beside every `./common` mount, plus the test client's `PYTHONPATH`.

A process outside pytest that imports `common.rpc` needs `gen/proto/python` on its `PYTHONPATH`, as
the image has it.

**`trader_joe` is a PEP 420 namespace.** `gen/proto/python/trader_joe/` has no `__init__.py`, and
**no distribution may ever ship `trader_joe/__init__.py`**: not this tree, not `trader_joe.common`,
not `trader_joe.client`. If one portion of a namespace ships it, the other portions stop being
importable. `gen/proto/python/trader_joe/proto/__init__.py` is committed by hand: `trader_joe.proto`
is a regular package that only generated code owns, and that file is `make proto`'s guard.

**Only `common/rpc/` imports generated code.** On the server, nothing outside `common/rpc/` may
import `trader_joe.proto`. Ruff rule TID251 enforces that, and the hand-written code in
`common/rpc/` is the interface everything else uses.

## Compatibility

Until the private repository consumes a released version, the contract is pre-release and may be
broken freely. Get field numbers and names wrong cheaply now. After that window closes the ordinary
rules apply: changes are additive only, a field number is never reused or renumbered, and every
removed number and name is `reserved`. A proto package name is part of the wire: it is in every
method path (`/trader_joe.proto.ping.v1.PingService/Ping`) and every message's full name, so
renaming a package breaks every caller.

## The one exception: errors

The error vocabulary is not defined here. The closed `(domain, reason)` enum and the exception
hierarchy are canonical in Python in `common/` (ADR tj-fa1rpu, U1). They cross the wire as
`google.rpc.Status` with an `ErrorInfo` carrying that `(reason, domain)` pair. Do not define reason
enums in a `.proto`.
