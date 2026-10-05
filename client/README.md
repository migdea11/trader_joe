# client/

**The one installable distribution: the typed client SDK.** A placeholder — this task creates the
home and nothing else.

The private repo that holds strategies, targets and private configuration consumes this framework
through the HTTP API and this SDK. `client/` is what it installs. Everything else in this
repository — the services, `common/`, `schemas/`, `routers/` — is deployed, not distributed.

**Not built yet.** Task tj-iontkq.7 builds it: the `pyproject.toml`, the `src/` layout, `py.typed`
so the types reach the consumer, and a test that installs the package and imports it as an installed
distribution rather than from the source tree. Packaging mechanics for the `trader_joe` namespace
are decided in ADR tj-yw8cok.

Two things are already settled and bind whatever lands here:

- **`trader_joe` is a PEP 420 namespace.** No distribution ships `trader_joe/__init__.py`.
  `trader_joe.client`, `trader_joe.common` and the generated `trader_joe.proto` share the name
  (decision tj-3mk3u5.42, addendum F1, rule 5).
- **Generated code is consumed, never vendored here.** `trader_joe.client` is a sanctioned importer
  of the generated `trader_joe.proto` tree at `gen/proto/python/`; it does not get a copy of its
  own. See `proto/README.md`.

`.dockerignore` excludes this directory: no service image reads it, so its build artefacts have no
business in the build context.
