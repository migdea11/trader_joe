# web/

The UI: a static single-page app. Vue 3, TypeScript and Vite, with PrimeVue 5 for components, Pinia,
Vue Router, AG Grid Community for tables, Lightweight Charts and Apache ECharts for charts, and Lucide
for icons. It shows the Data section: the dataset catalog (**Datasets**) and a per-dataset **Viewer**
with a candle chart and a paged bar table. Requests, Health and Usage are placeholders until their
routes exist.

## Why the UI lives in this public repo

This repository is the generic framework; strategies, targets and private configuration live in a
separate private repo that consumes it through an API and a typed client SDK. The UI is here because
most of it is generic: data management and performance viewing are screens any deployment needs.
Strategy-specific metrics are add-ons, and only the add-ons are private.

## What it consumes

- **REST carrying JSON**, same-origin, GET only today. No gRPC on the browser wire: browsers cannot
  read HTTP/2 trailers, which is how gRPC signals status.
- **The `.proto` files are the one definition of the message types**, for the Python server and for
  this app. A UI payload is the protobuf canonical JSON of a generated message, decoded with
  `fromJson` from the generated schema, never a hand-written TypeScript twin. Endpoint paths and
  methods are described by the server's OpenAPI document; only message shapes are shared.
- **The browser calls `/api/store/...`.** data_store serves the same route without the prefix, so
  `/api/store/ui/v1/datasets` reaches `/ui/v1/datasets`. In production Caddy strips the prefix,
  forwards only GET and HEAD under `/api/store/ui/v1/`, and adds the instance secret; the browser never
  holds it. The dev proxy (`vite.config.ts`) is not that filter: it forwards everything under
  `/api/store`, any method and any path, strips the prefix and never adds the secret, so a write from
  the dev server reaches the store without it and is refused.

The routes in use are listed in [`docs/API.md`](../docs/API.md) under *The UI read routes*. Every
range is half-open, `[start, end)`.

The UI does not place or amend orders. It reads, and asks the server to act.

## Running it

Everything runs through the root `Makefile`; see the top-level README for the stack as a whole.

| Goal | Command | Result |
|---|---|---|
| Dev, hot reload | `make dev-build` then `make dev-launch` | Vite dev server at `http://localhost:${WEB_PORT}/` (default `8088`), in the foreground. An edit under `web/src/` shows at once. |
| Prod, static | `make prod-build` then `make prod-launch` | The built app served by Caddy at the same `http://localhost:${WEB_PORT}/`, detached. |

Both publish on `127.0.0.1` only, and they share `WEB_PORT`, so only one stack runs at a time. Config
files and the lockfile are baked into the dev image: after changing one, `make dev-build` again.

Before the first build, install the pinned Node and the dependencies:

```
make node-install   # the Node version in the Makefile, checksum-verified
make web-install    # npm ci
```

### The PrimeUI licence key

PrimeVue 5 needs a PrimeUI licence key. Without one the app builds and runs, logs a console warning
and shows a small "Invalid PrimeUI License" notice. The key is read from `VITE_PRIMEUI_LICENSE_KEY`,
which the root env file supplies (copy `.env.default`, which carries the name with an empty value, and
put the key in your own file; never commit it).

- **Prod:** a build argument of the image, so it is baked into the JavaScript bundle by
  `make prod-build`. The Community licence allows the key to appear in the bundle, and it may not be
  published for others to use. **Never push an image built with a key to a public registry.**
- **Dev:** read from the container's environment when the Vite dev server starts.

## Generated TypeScript

The messages are generated into the repo-root `gen/proto/ts/` and are **never committed**: `gen/` is
gitignored. `make gen-proto-ts` writes them (`npm run gen:proto` from `web/` does the same), and
`make dev-build`, `make prod-build` and the `typecheck`, `test` and `build` scripts run it first.
After a `.proto` change, run it on the host; a running dev server sees the result. Nothing generated
is written into `web/`, and nothing under `gen/` is edited by hand.

## Make targets

| Target | Runs |
|---|---|
| `make web-install` | `npm ci` |
| `make gen-proto-ts` | `buf generate` into `gen/proto/ts` |
| `make web-lint` | eslint |
| `make web-typecheck` | vue-tsc |
| `make web-test` | vitest (`make test PATHS=web` is the same) |
| `make web-build` | `vite build` into `web/dist` |
| `make web-audit` | full `npm audit` (report only), then a blocking gate on runtime dependencies at high severity |
| `make web-check` | all of the above, in CI's order |

`make lint` also runs eslint and vue-tsc when `PATHS` is `.` or under `web/`. CI runs the same chain in
its Web job.

## Layout

| Path | Holds |
|---|---|
| `src/api/` | the fetch wrapper (`client.ts`), typed dataset reads, error mapping |
| `src/stores/` | Pinia stores: catalog, viewer, settings, trading group, UI config |
| `src/shell/` | the frame: top bar, sub-tabs, status footer, settings panel, About and Credits |
| `src/views/data/` | the Datasets and Viewer screens and their sidebars |
| `src/catalog/`, `src/viewer/` | pure logic behind those screens: filters, facets, cursor paging, range and bar windows |
| `src/components/` | grid, tiles, charts, bar table, badges |
| `src/theme/` | tokens and everything derived from them; see its README |
| `src/format/` | number, price, date and time-zone formatting |

Tests sit beside their code as `*.spec.ts`.

## Settings

Settings live in this browser (`localStorage`), not on the server: the display time zone and the
number of days without a read after which a dataset counts as stale. The stale threshold is stored but
no phase 1 route takes it: the dataset routes refuse a `stale_days` parameter today. There is no
settings route.

## Why no polling

`setInterval` is banned in `web/src`. The UI refetches on a user action, and later on a server event.
