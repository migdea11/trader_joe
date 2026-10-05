# web/

**The UI's home.** Empty on purpose: this task creates the directory and records why it exists and
what it will consume. There is no framework, no `package.json` and no `node_modules` yet.

## Why the UI lives in this public repo

This repository is the public, generic framework; strategies, targets and private configuration live
in a separate private repo that consumes it through an API and a typed client SDK. The UI is here
because **most of it is generic**. The expected surface is data management and performance
viewing — the kind of screens any deployment of this framework needs. Strategy-specific metrics are
add-ons, and only the add-ons are private. The generic part is framework, so it belongs beside the
rest of the framework; splitting it out would mean a private repo carrying a whole UI to add a few
panels to it.

## What it will consume

ADR tj-4k5s35, accepted 2026-09-30, rules the transport:

- **REST plus WebSocket, carrying JSON.** No gRPC on the browser wire — browsers cannot read HTTP/2
  trailers, which is how gRPC signals status. Binary protobuf on the UI wire is not adopted.
- **The `.proto` files are the single definition of the message types**, for both the Python server
  and the TypeScript UI. UI-facing REST and WebSocket payloads are the protobuf canonical JSON form
  of generated messages, not hand-written Pydantic and TypeScript twins.
- **Endpoint paths and methods stay described by FastAPI/OpenAPI.** Only message shapes are shared,
  not service definitions.
- Connect is in the backlog, not rejected. Using the same `.proto` messages keeps that migration
  cheap.

That is why `proto/` is a **root sibling** rather than a directory inside the server: the UI
generates a TypeScript client from the same contract, and proto under the server would make every
consumer depend on the server for shared types. Generated TypeScript will land at `gen/proto/ts/`,
the per-language committed tree described in `proto/README.md` — committed, lint-excluded, and never
hand-edited. Nothing generated is ever written into `web/`.

The UI also does **not** place or amend individual orders. It reads the order and fill log and asks
the server to act — approve a plan, pause a strategy, reallocate capital.

## Why no framework yet

A framework picked before there is a screen to build is a framework picked twice. The first slices —
the Data section and collection-health Ops — are planned in epic tj-grna9p, and the framework is
chosen there, with concrete requirements in hand.

Ruff excludes `web/**` (`pyproject.toml`, `[tool.ruff] exclude`): this tree is not Python, and the
lint gate should not start failing the day it acquires a helper script. `.dockerignore` excludes it
too — no service image reads anything under here, and `node_modules` in the build context would be
uploaded to the Docker daemon on every service build.
