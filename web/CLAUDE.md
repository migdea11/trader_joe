# Web UI

The UI: a static Vue 3 single-page app that reads data_store over same-origin REST. Today it holds the Data section: the dataset catalog and the Viewer. Run, build and make targets are in `web/README.md`.

## Architecture reference

`bd list -t arch_index --all` — the web component is a child of the system index. Design diagram: `docs/img/design.jpg`.

## Tech stack

Vue 3 with `<script setup>` and TypeScript, Vite, Pinia, Vue Router (history mode), PrimeVue 5 with a custom preset over `@primeuix/themes`, AG Grid Community (tables), Lightweight Charts (candles and volume) and Apache ECharts (registered in `src/components/charts/echarts.ts`; used by the dev showcase today), Lucide icons through `@lucide/vue`, `@bufbuild/protobuf` for the generated message schemas, Vitest with Vue Test Utils.

## Key invariants

| Invariant | Detail |
|---|---|
| One wire shape | Payloads are protobuf canonical JSON, decoded with `fromJson(Schema)` in `src/api/client.ts`. int64 arrives as `bigint`; `src/api/int64.ts` is the one place it becomes a number. |
| One origin | The browser calls `/api/store/...` only. Both proxies strip the prefix. Caddy in prod forwards only GET and HEAD under `/ui/v1` and adds the secret; the Vite dev proxy forwards all of `/api/store` and adds none. No `/api/ingest` route exists. |
| No secret in the browser | The instance secret is added at the proxy. The app issues GETs and never holds it. |
| Ranges are half-open | `[start, end)` everywhere: bar windows, dataset ranges, the Viewer's range controls. |
| Raw bars only | The Viewer shows unadjusted bars. Split, All and Compare Raw vs Adjusted are disabled with their reason. |
| Server decides | Freshness, status groups, facet counts and the allowed trading groups come from the server; the UI maps them to words and colours (`src/theme/semantics.ts`) and computes none. Market data is shared across trading groups, so Data pages ignore the toggle. |
| No polling | `setInterval` is banned by lint. Data refetches on a user action. |
| Settings stay in the browser | One versioned `localStorage` object (`src/stores/settings.ts`); storage that is missing or throws falls back to defaults in memory. |

## Generated code

`@generated` aliases the repo-root `gen/proto/ts/`, written by `make gen-proto-ts` and never committed. It sits outside `web/`, so `vite.config.ts` and `tsconfig.app.json` both pin `@bufbuild/protobuf` to `web/node_modules`; a new import path of that package needs the same pin in both. The tree is absent on a fresh clone: typecheck, test, build and dev regenerate it first.

## Visual tokens and accessibility

Dark only. `src/theme/tokens.ts` is the one source of colour, type and spacing; `cssVars.ts`, `preset.ts`, `charts.ts` and `semantics.ts` are built from it, and no other file in `src/` holds a hex colour.

The accent has two tokens on purpose, because the accent fill measures 2.9:1 on the surface, under the 3:1 minimum for graphics:

| Token | Use |
|---|---|
| `accentTint` | shape-only indicators (active bar, tab underline, chart lines, checkboxes, status dots) and accent-coloured text such as links |
| `accentFill` | only where a text label carries the meaning (primary button, selected toggle segment, logo mark), with `onAccent` text on it |

There is deliberately no plain `accent`. Status never rests on colour alone: a badge carries its word and colour only supports it. The Live group colour appears nowhere else.

## Copy conventions

Defined in `src/theme/copy.ts`:

- A screen's page title is exactly its tab name (Datasets, Requests, Health, Usage). A page about one object uses the object's name.
- Title Case for page and panel titles, tab names, buttons, column headers, filter group labels, saved view names, tile labels and field labels. Run those strings through `titleCase()`. Small words (and, by, in, of, per, the, to) stay lower case unless first or last; acronyms and tokens with digits are left alone.
- Sentence case for sentences, hints, messages and filter option text.
- Trading groups read Simulation, Paper and Live. Freshness statuses read Healthy, Late, Failed and Retired in the UI whatever the proto names are.

## Credits

The About and Credits modal is generated from data, not written by hand. `src/shell/credits.ts` lists the runtime dependencies that need attribution with each licence read from the installed package, and `src/components/charts/credits.ts` carries the chart libraries' notices. The vendored IBM Plex licence texts are imported raw from `src/theme/fonts/`. Lightweight Charts' on-chart logo is off, so the TradingView notice and link in that modal are the whole of the attribution: the modal stays reachable from every screen. A new runtime dependency that ships a notice is one more entry in `credits.ts`.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| VITE_PRIMEUI_LICENSE_KEY | PrimeUI licence key, read in `src/config/licence.ts`; build argument in prod, container environment in dev | empty (works, shows a licence notice) |
| VITE_DEV_PROXY_TARGET | where the Vite dev proxy forwards `/api/store`; absolute http(s) URL | `http://localhost:8000` |

## Common pitfalls

| # | Pitfall | Do instead |
|---|---|---|
| 1 | In the dev container only `web/src` is bind-mounted; a change to config, `package.json` or the lockfile is not seen | `make dev-build` |
| 2 | `npm run dev` runs `buf generate` first and the dev container has no buf | the compose file runs `vite` directly; regenerate on the host with `make gen-proto-ts` |
| 3 | A new `/ui/v1` message field is invisible until `gen/proto/ts` is regenerated | `make gen-proto-ts`, then restart a stuck dev server |
| 4 | A server field the UI does not know is ignored on decode, so a stale build keeps working but cannot show it | rebuild after a proto change |

A pitfall lands here when it is true of this component and nowhere else.
