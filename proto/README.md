# proto/

**The source of truth for everything that crosses the gRPC wire.** The `.proto` files here are
written by hand, and every consumer generates its code from them. Nothing in this directory is
generated, and nothing in it is Python. (ADR tj-8konfu D1, with this root home from addendum A1; the
generated layout is decision tj-3mk3u5.42, addendum F1.)

Pydantic models stay the internal domain representation and keep the REST surface, with one
exception: the `/ui/v1` routes answer with the protobuf canonical JSON of a `trader_joe.proto.ui.v1`
message, so the web UI and the server share one definition of each shape. Neither side is generated
from the other: each consumer maps between generated messages and its own types by hand, in one
place.

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
| `trader_joe.proto.internal.latency.v1` | The latency harness probe, `LatencyService.Probe`. Internal: both ends deploy together. |
| `trader_joe.proto.data.v1` | The external streaming contract. The name is provisional. |
| `trader_joe.proto.ui.v1` | The UI's messages: datasets, facets and bar pages (`data.proto`), and the shell configuration (`shell.proto`). Served as protobuf canonical JSON over REST, never as binary. Ranges in it are half-open `[start, end)`. |
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
  `.proto` files, and that package at runtime. Buf needs it too: a Buf Schema Registry dependency (a
  `buf.lock`, and the network) or a vendored copy. No contract needs it yet; decide when one does.

## Generated code: one generated tree per language, never committed

Generated code lives at **`gen/proto/<language>/`** at the repository root, and `gen/` is
gitignored. Each language has one tree, and every consumer in that language uses it.

| Language | Tree | Regenerate |
|---|---|---|
| Python (both services; later the SDK and Python clients) | `gen/proto/python/trader_joe/proto/<domain>/v1/` | `make proto` |
| TypeScript (the web UI) | `gen/proto/ts/` | `make gen-proto-ts` |

The rules for a generated tree:

- **Never hand-edit it.** If a merge conflicts inside it, run `make proto` again.
- **Nothing post-processes it either.** What is generated is protoc's output, byte for byte: no
  import rewrite and no other edit. When a contract changes, the hand-written code that uses it is
  changed by hand, in review.
- **Never commit it.** Every target that needs a tree generates it first (the test, build and launch
  targets run `make proto`, and the web targets run `make gen-proto-ts`), so a fresh clone has no
  `gen/` until it builds. A change to a `.proto` commits only the `.proto`.
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
**That is configuration, never code**: no code changes `sys.path`. The root is added in four places:

- `pytest.ini`'s `pythonpath`, for every pytest run;
- the image: `ENV PYTHONPATH=/code:/code/gen/proto/python` and one `COPY` of `./gen/proto/python`
  (`Dockerfile`);
- the compose bind mounts, one beside every `./common` mount, plus the test client's `PYTHONPATH`;
- `docker-compose.yaml`'s `environment:`, which sets the same `PYTHONPATH`, literally, on every
  service built from the `Dockerfile`'s service stages. It is what makes the image's value hold
  under compose: the root env file, copied from `.env.default`, carries a legacy, host-side-only
  `PYTHONPATH=./`, every service loads it through `env_file:`, and `env_file:` outranks the
  image's `ENV`.
  `environment:` outranks both (decision tj-3mk3u5.42, addendum F1-A).

A process outside pytest that imports `common.rpc` needs `gen/proto/python` on its `PYTHONPATH`, as
the image has it.

**`trader_joe` is a PEP 420 namespace.** `gen/proto/python/trader_joe/` has no `__init__.py`, and
**no distribution may ever ship `trader_joe/__init__.py`**: not this tree, not `trader_joe.common`,
not `trader_joe.client`. If one portion of a namespace ships it, the other portions stop being
importable. `gen/proto/python/trader_joe/proto/__init__.py` is written by `make proto`, not by protoc:
`trader_joe.proto` is a regular package that only generated code owns, and that file is `make proto`'s
guard against clearing a directory that is not the generated package.

**Only `common/rpc/` imports generated code.** On the server, nothing outside `common/rpc/` may
import `trader_joe.proto`. Ruff rule TID251 enforces that, and the hand-written code in
`common/rpc/` is the interface everything else uses.

## Compatibility

Until the private repository consumes a released version, the contract is pre-release and may be
broken freely. Get field numbers and names wrong cheaply now. After that window closes the ordinary
rules apply: changes are additive only, a field number is never reused or renumbered, and every
removed number and name is `reserved`. A proto package name is part of the wire: it is in every
method path (`/trader_joe.proto.ping.v1.PingService/Ping`) and every message's full name, so
renaming a package breaks every caller. `buf breaking` reports breaks against `main` (next section):
printed only while the window is open, failing once it closes.

## Lint and breaking checks

`make lint` checks `proto/` with [Buf](https://buf.build) whenever `PATHS` covers it: `PATHS=.` (the
default) or a path under `proto/`, such as `make lint PATHS=proto`. Any other scope (`PATHS=common`,
`PATHS=data/ingest`, ...) prints one `lint-proto: not run` line and passes without looking for buf.
Buf only checks. Code is still generated by `make proto`, with protoc.

When it runs, it runs three checks, in order, always over the whole module, whatever part of
`proto/` `PATHS` names:

| Check | What it enforces | How to fix a finding |
|---|---|---|
| `buf lint` | Buf's `STANDARD` rules | By hand: there is no autofix |
| `buf format --diff --exit-code` | Buf's formatting | `make lint-fix PATHS=proto` runs `buf format -w` |
| `buf breaking` | Buf's `FILE` rules, against `main` | Report-only for now (below) |

The configuration is `buf.yaml` at the repository root:

- **The module root is `proto/`**, the same include root `make proto` gives protoc. That is what lets
  the package-directory rule hold: `trader_joe.proto.ping.v1` lives in `trader_joe/proto/ping/v1/`.
- **`STANDARD`, with no exceptions, and comment ignores are off**: a `// buf:lint:ignore` comment
  suppresses nothing. The only way to exempt a rule is a reviewed change to `buf.yaml`, and that needs
  a `Q:@architect` first. In practice `STANDARD` asks for what the sections above already say, and a
  little more: the package matches the directory and ends in a version; every enum value carries the
  enum's prefix and the zero value ends `_UNSPECIFIED`; every RPC takes `<Rpc>Request` and returns
  `<Rpc>Response`, unique to it; every service name ends in `Service`.

`buf breaking` compares the working tree with `main`:

- **`FILE` rules**, Buf's strictest set: wire, JSON and generated-source compatibility. The SDK and the
  web consume generated source, and canonical JSON consumes field names.
- **`internal/` is ignored.** `trader_joe.proto.internal.*` contracts deploy both ends together and may
  break freely.
- **The baseline is the local `main` branch** (`BUF_AGAINST_REF=<ref>` picks another). Its `buf.yaml`
  and `proto/` are extracted with `git archive`, which works the same in the shared checkout, in an
  agent's worktree and in CI, which compares against a fresh `origin/main`. A local `main` can lag
  `origin/main`. That is acceptable while the check is report-only. While `main` has no `proto/`, the
  check says `nothing to compare` and passes.
- **Report-only until the first SDK release** (tj-d2mhru), while the compatibility window above is
  open. A break is printed, under a `REPORT-ONLY` banner, and in CI it is annotated on the pull request,
  but it does not fail. At the release, one Makefile line, `BUF_BREAKING_BLOCKING := 1`, makes it
  blocking. Report-only covers findings only: a check that cannot run fails in both modes. That means
  a ref that does not resolve, a baseline with `proto/` but no `buf.yaml`, a side that does not build,
  or a bad configuration.

What buf does **not** enforce:

- **The reserved `trader_joe.proto` root.** `make proto`'s guard enforces it.
- **The import rules under "Imports and hierarchy"**: no contract imports another, and an external
  contract never imports an internal one. `STANDARD` has no import-direction rule, so these stay with
  review.

Where buf comes from. Its version and checksums are pinned in the `Makefile` (`BUF_VERSION`,
`BUF_SHA256_*`), and a missing buf or a buf at another version fails `make lint` whenever `PATHS`
covers `proto/`. It is never skipped.

- **The agent image** carries it at `/usr/local/bin/buf`, from its next rebuild.
- **CI** installs it with `make buf-install`.
- **Anywhere else, and in an agent container until that rebuild**: `make buf-install` downloads the
  release binary for Linux x86_64 or aarch64, verifies its SHA-256 against the pin, refuses a
  mismatch, and installs it into `~/.local/bin` (`BUF_INSTALL_DIR=` to change).

## The one exception: errors

The error vocabulary is not defined here. The closed `(domain, reason)` enum and the exception
hierarchy are canonical in Python in `common/` (ADR tj-fa1rpu, U1). They cross the wire as
`google.rpc.Status` with an `ErrorInfo` carrying that `(reason, domain)` pair. Do not define reason
enums in a `.proto`.
