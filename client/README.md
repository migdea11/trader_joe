# client/

**The one installable distribution: the typed client SDK.** A placeholder: the directory
exists and nothing else does.

The private repo that holds strategies, targets and private configuration consumes this framework
through the HTTP API and this SDK. `client/` is what it installs. Everything else in this
repository — the services, `common/`, `schemas/`, `routers/` — is deployed, not distributed.

**Not built.** This directory holds only this file: there is no `pyproject.toml`, no `src/` layout
and no package yet.

Two rules already bind whatever lands here:

- **`trader_joe` is a PEP 420 namespace.** No distribution ships `trader_joe/__init__.py`.
  `trader_joe.client`, `trader_joe.common` and the generated `trader_joe.proto` share the name.
- **Generated code is consumed, never vendored here.** `trader_joe.client` is a sanctioned importer
  of the generated `trader_joe.proto` tree at `gen/proto/python/`; it does not get a copy of its
  own. See `proto/README.md`.

`.dockerignore` excludes this directory: no service image reads it, so its build artefacts have no
business in the build context.
