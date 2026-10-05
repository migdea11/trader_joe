# proto/

**The source of truth for everything that crosses the gRPC wire.** The `.proto` files here are
written by hand, and every consumer generates its own code from them. Nothing in this directory is
generated, and nothing in it is Python. (ADR tj-8konfu D1, with this root home from addendum A1.)

Pydantic models stay the internal domain representation and keep the REST surface. Neither side is
generated from the other: each consumer maps between generated messages and its own types by hand,
in one place.

## Layout

```
proto/<package path>/<version>/<name>.proto
```

A file's path mirrors its proto package, and every package is versioned. `package trader_joe.ping.v1;`
lives in `trader_joe/ping/v1/ping.proto`.

`trader_joe/ping/v1/ping.proto` proves the codegen pipeline end to end. It is **not a contract**:
nothing depends on its shape, and the first real contract does not extend it.

## Consumers own their generated code

Each consumer owns a **committed, lint-excluded `generated/` directory** of its own, written by its
own make target. Today there is one consumer:

| Consumer | Generated directory | Regenerate |
|---|---|---|
| the server (both services) | `common/rpc/generated/` (`server/common/rpc/generated/` after the monorepo split) | `make proto` |

The rules for a generated directory:

- **Never hand-edit it.** If a merge conflicts inside it, run `make proto` again.
- **Commit it in the same commit as the `.proto` change.** CI regenerates it and fails on any
  difference: a changed file, a deleted one or an untracked one.
- **Only its own package imports it.** On the server, nothing outside `common/rpc/` may import
  `common.rpc.generated`. Ruff rule TID251 enforces that, and the hand-written code in `common/rpc/`
  is the interface everything else uses.
- The generator is pinned exactly (`grpcio-tools` in `pyproject.toml`), because its version is
  written into every file it emits. Bumping it is a deliberate change that regenerates the tree.

## How the server generates, and the constraint that comes with it

protoc writes a generated module's imports from the `.proto` path **as protoc sees it**, not from
where the output lands. With a plain `-Iproto`, `_pb2_grpc.py` would import
`from trader_joe.ping.v1 import ping_pb2`. That package does not exist on the path under pytest or in
the image, and the server's rule is that nothing adds it to `sys.path`.

So `make proto` gives protoc a virtual include root, `-Icommon/rpc/generated=proto`. protoc then sees
each file as `common/rpc/generated/<path>.proto`, and the imports it writes resolve as they are.
That has two costs:

1. **A `.proto` here cannot import another of this repository's `.proto` files by its canonical path**
   (`import "trader_joe/<x>/v1/<y>.proto";` fails with "File not found"). Well-known types such as
   `google/protobuf/timestamp.proto` still work. Until the architect decides how multi-file contracts
   are handled, keep each contract to one file plus well-known imports.
2. The file names recorded in the server's descriptors carry its prefix
   (`common/rpc/generated/trader_joe/ping/v1/ping.proto`). The wire is unaffected: method paths and
   message names come from the proto package (`/trader_joe.ping.v1.PingService/Ping`).

## Compatibility

Until the private repository consumes a released version, the contract is pre-release and may be
broken freely. Get field numbers and names wrong cheaply now. After that window closes the ordinary
rules apply: changes are additive only, a field number is never reused or renumbered, and every
removed number and name is `reserved`.

## The one exception: errors

The error vocabulary is not defined here. The closed `(domain, reason)` enum and the exception
hierarchy are canonical in Python in `common/` (ADR tj-fa1rpu, U1). They cross the wire as
`google.rpc.Status` with an `ErrorInfo` carrying that `(reason, domain)` pair. Do not define reason
enums in a `.proto`.
