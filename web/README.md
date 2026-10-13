# web/

The UI: a static single-page app. Vue 3, TypeScript and Vite, with PrimeVue 5 for components, Pinia,
Vue Router, AG Grid Community for tables, Lightweight Charts and Apache ECharts for charts, and Lucide
for icons. It shows the Data section: the dataset catalog (**Datasets**), a per-dataset **Dataset
detail** (overview, coverage and gaps, run history) and a **Viewer** with a candle chart and a paged
bar table. Requests, Health and Usage are placeholders: their tabs render disabled ("Not built yet")
until their routes exist.

## Why the UI lives in this public repo

This repository is the generic framework; strategies, targets and private configuration live in a
separate private repo that consumes it through an API and a typed client SDK. The UI is here because
most of it is generic: data management and performance viewing are screens any deployment needs.
Strategy-specific metrics are add-ons, and only the add-ons are private.

## What it consumes

- **REST carrying JSON**, same-origin, GET only today. No gRPC on the browser wire: browsers cannot
  read HTTP/2 trailers, which is how gRPC signals status.
- **One WebSocket**, `/api/store/ui/v1/events`, which pushes notifications that something changed. It
  carries no data: an event is a hint to refetch over REST. See [Live events](#live-events).
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

## Live events

The app holds one WebSocket to `/api/store/ui/v1/events` (`src/api/events.ts`), opened by the first
subscriber and kept for the app's life. Each text frame is the protobuf canonical JSON of a `UiEvent`,
decoded with `fromJson`; a frame that does not decode is logged and dropped. A drop reconnects with a
backoff of 1 second doubling to 30, and a close with 1013 (the server dropped a slow client)
reconnects the same way.

- **Published today:** `IngestRunChanged` and `DatasetChanged`. `RequestChanged` and
  `ReaperPendingChanged` are typed and have no publisher, so nothing consumes them.
- **The connection indicator** in the top bar reads Connecting (before the first open), Connected, or
  Reconnecting (after a drop). There is no Offline state: a drop is Reconnecting.
- **Reload on reconnect.** After every re-open, never the first, each mounted **Datasets** and
  **Dataset detail** screen refetches over REST, since events may have been missed while the socket
  was down. It is a refetch, not a page reload: filters, selection and scroll stay. The **Viewer is
  not live yet**: it neither refetches on events nor reloads on reconnect.
- **Bursts are coalesced.** A run writes several state changes, so a screen waits 500 ms after the
  first event and refetches once. Datasets refetches the counts, the rows it holds and the coverage
  strips of the datasets that changed; Dataset detail updates the run's row in place and refetches
  the overview and coverage once.
- **Events are hints.** An event can name a dataset the store has not committed yet, so a reload by
  id may briefly 404. REST is the truth.

## The Data screens

| Screen | Route | Holds |
|---|---|---|
| Datasets | `/data/datasets` | the catalog grid. The **Collection** column reads Running while the dataset's latest run is in flight and its freshness word otherwise; the **Coverage** column is a strip fetched lazily for the rows in view. The sidebar's Collection group has Running as its own toggle (`?running=true`), combinable with one freshness choice. A row opens the detail |
| Dataset detail | `/data/datasets/:id` | **Overview** (owner, range, bars, last bar, a link to the Viewer), **Coverage** (the strip and its gap list), **History** (the dataset's runs, newest first, Load More by cursor) and **Retention**, which is read-only: the expiry type and time, with no control |
| Viewer | `/data/datasets/:id/viewer` | candles, volume and a paged bar table |

- **Gap and strip dates are UTC days.** The client is not told the calendar's time zone, so a
  boundary, which is the calendar's local midnight, is read as a UTC date, whatever the display zone
  setting says. That is right for calendars west of UTC, and the only calendar mapped is NYSE.
- **Running appears only once ingest has acknowledged the fetch**, so a first-time request is not
  shown as Running while the vendor is still answering.
- **Not built:** Requests, Health and Usage (disabled tabs; their routes show a not-built page); any retention control; Request the Gap
  and All Runs; a Quota Key column, since the server does not record one.

## Layout

| Path | Holds |
|---|---|
| `src/api/` | the fetch wrapper (`client.ts`), typed dataset reads, error mapping, the events client (`events.ts`) |
| `src/stores/` | Pinia stores: catalog, dataset detail, dataset coverage, viewer, settings, trading group, UI config |
| `src/shell/` | the frame: top bar, sub-tabs, status footer, settings panel, About and Credits, the events indicator and reload hook |
| `src/views/data/` | the Datasets, Dataset detail and Viewer screens and their sidebars |
| `src/catalog/`, `src/viewer/` | pure logic behind those screens: filters, facets, cursor paging, coverage wording, range and bar windows |
| `src/components/` | grid, tiles, charts (including the coverage strip), runs table, bar table, badges |
| `src/theme/` | tokens and everything derived from them; see its README |
| `src/format/` | number, price, date and time-zone formatting |

Tests sit beside their code as `*.spec.ts`.

## Settings

Settings live in this browser (`localStorage`), not on the server: the display time zone and the
number of days without a read after which a dataset counts as stale. The stale threshold is stored but
no phase 1 route takes it: the dataset routes refuse a `stale_days` parameter today. There is no
settings route.

## Why no polling

`setInterval` is banned in `web/src`. The UI refetches on a user action, on a server event (see
[Live events](#live-events)) and once after a reconnect. Nothing polls.
