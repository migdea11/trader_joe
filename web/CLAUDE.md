# Web UI

The UI: a static Vue 3 single-page app that reads data_store over same-origin REST. Today it holds the Data section: the dataset catalog (with usage, Expiring Soon and Stale views), the Dataset detail (with its Retention card for the expiry, collecting and delete writes), the Viewer and the Usage tab. Run, build and make targets are in `web/README.md`.

## Architecture reference

`bd list -t arch_index --all` — the web component is a child of the system index. Design diagram: `docs/img/design.jpg`.

## Tech stack

Vue 3 with `<script setup>` and TypeScript, Vite, Pinia, Vue Router (history mode), PrimeVue 5 with a custom preset over `@primeuix/themes`, AG Grid Community (tables), Lightweight Charts (candles and volume) and Apache ECharts (registered in `src/components/charts/echarts.ts`; the Usage tab's charts), Lucide icons through `@lucide/vue`, `@bufbuild/protobuf` for the generated message schemas, Vitest with Vue Test Utils.

## Key invariants

| Invariant | Detail |
|---|---|
| One wire shape | Payloads are protobuf canonical JSON, decoded with `fromJson(Schema)` in `src/api/client.ts`. int64 arrives as `bigint`; `src/api/int64.ts` is the one place it becomes a number. |
| One origin | The browser calls `/api/store/ui/v1/...` only. Both proxies strip the `/api/store` prefix and forward exactly four rules: GET and HEAD under `/api/store/ui/v1/` (the events socket's upgrade is a GET), PATCH on `.../datasets/{uuid}/expiry` and `.../datasets/{uuid}/collecting` (two rules, one per path), and DELETE on `.../datasets/{uuid}`. Anything else is a 404. In prod, Caddy's `@traversal` rule (400) never fires under `/api`, because `/api/*` is evaluated first: a traversal is resolved by Caddy's own path cleaning and never forwarded: while the cleaned path stays under `/api` it gets the `/api/*` 404, not 400; once it climbs off `/api` it gets the SPA's `index.html`; only a cleaned path that still holds a literal `..` (`/x..y`) gets the 400. The dev proxy answers 400 for a path containing `..` and is stricter than prod. No `/api/ingest` route exists. |
| No secret in the browser | The instance secret authenticates the three writes (expiry, collecting, delete) and the browser never holds it. Caddy adds it in prod; in dev the Vite dev SERVER adds it, from its own `process.env.INSTANCE_WRITE_SECRET`, replacing any `X-Instance-Secret` the browser sent. It is never read through `import.meta.env` and never has a `VITE_` name, because Vite bundles every `VITE_*` into the client. Unset or blank, it sets nothing and data_store refuses the write. Every call carries `X-Client-Id: operator`, which is a usage label and not a credential. |
| The dev server mirrors the prod edge | `vite.config.ts` (`isAllowedStoreRequest`, `storeBypass`, `applyInstanceSecret`) applies the Caddyfile's four rules and is stricter than prod about what reaches data_store: it never forwards a request prod would not forward, but may answer 400 to a spelling prod forwards. It is not a superset of prod's refusals: a path the dev proxy does not own (`/api/ingest/...`, `//api/...`) falls through to Vite's SPA fallback and answers `index.html` 200 where prod answers 404 (a known gap). It decides on the DECODED, CLEANED path, because the dev proxy resolves `%2e%2e` itself: any percent-escape, `//` or `./`, a `..`, a backslash or an encoded separator, a malformed escape, or a path still changing after three decode passes is a 400, so nothing reaches data_store in a spelling the allow-list did not see. Keep the two files in step: a change to one rule is a change to both. |
| Expiry is a deletion date; collecting is separate | The Retention card shows the expiry as `Deleted On` (empty is Never) and sets it with Set Expiry (PATCH expiry): it stops nothing. A DAILY or STREAM subscription has a Stop Collecting / Start Collecting control (PATCH collecting, `store.setCollecting`) and reads `Collecting` or `Stopped: not collecting`; a STATIC dataset has no switch and reads `A fetch is running` only while a run is in flight. The UI treats `collecting === false` or the server's `RETIRED` state as Stopped. |
| Ranges are half-open | `[start, end)` everywhere: bar windows, dataset ranges, the Viewer's range controls. |
| Raw bars only | The Viewer shows unadjusted bars. Split, All and Compare Raw vs Adjusted are disabled with their reason. |
| Server decides | Freshness, status groups, facet counts, usage figures, staleness, `reaperPending` and the allowed trading groups come from the server; the UI maps them to words and colours (`src/theme/semantics.ts`) and computes none. It never decides that a dataset is stale or expiring; it sends `stale_days`, `usage` and `expires_before` and shows what comes back. Market data is shared across trading groups, so Data pages ignore the toggle. |
| No polling | `setInterval` is banned by lint. Data refetches on a user action, on a server event, and once per reconnect of the events socket. |
| Events are hints | `src/api/events.ts` holds the one shared socket to `/api/store/ui/v1/events` and delivers typed `UiEvent` messages. An event only triggers a REST refetch; a screen never builds state from one alone, except placing a run's own row in the detail's history. A reload signal is emitted after every re-open and never on the first, because screens fetch on mount. |
| Screens opt in to reload | A screen calls `useEventsReload(refetch)` (`src/shell/useEventsReload.ts`) to refetch after a reconnect. Datasets, Dataset detail and the Viewer do; the Viewer reloads its chart page model (`chartPages.reload()`), not the bar table. |
| Chart paging, no aggregation | The Viewer holds at most the cached pages (`CACHE_PAGES`) and draws every bar it holds: no aggregation, no cursor walk for the chart. Page size 1,000 and look-ahead 50 are constants in `src/viewer/chartPages.ts`; the page model there has no Vue, and its arrays are never reactive (`shallowRef`/`markRaw`). |
| Coverage dates are UTC | A strip or gap boundary is the calendar's local midnight, read as a UTC date (`src/catalog/coverage.ts`), because the client is not told the zone. Correct west of UTC. |
| Settings stay in the browser | One versioned `localStorage` object (`src/stores/settings.ts`), holding the time zone override only; storage that is missing or throws falls back to defaults in memory. |
| Stale threshold is fixed | `STALE_DAYS` is 90 in `src/api/usage.ts` and sent as `stale_days`. There is no Settings control for it and none is stored. |
| Read By is a filter | Whose reads the usage figures count (SDK, UI) is a filter on the Datasets sidebar and the Usage tab, not a setting and not stored. Untouched, it is SDK (`DEFAULT_CLIENT_KINDS`), and at least one kind stays ticked. |

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
- Trading groups read Simulation, Paper and Live. Freshness statuses read Healthy, Late, Failed and Stopped (the server's RETIRED: collection is off) in the UI whatever the proto names are.

## Credits

The About and Credits modal is generated from data, not written by hand. `src/shell/credits.ts` lists the runtime dependencies that need attribution with each licence read from the installed package, and `src/components/charts/credits.ts` carries the chart libraries' notices. The vendored IBM Plex licence texts are imported raw from `src/theme/fonts/`. Lightweight Charts' on-chart logo is off, so the TradingView notice and link in that modal are the whole of the attribution: the modal stays reachable from every screen. A new runtime dependency that ships a notice is one more entry in `credits.ts`.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| VITE_PRIMEUI_LICENSE_KEY | PrimeUI licence key, read in `src/config/licence.ts`; build argument in prod, container environment in dev | empty (works, shows a licence notice) |
| VITE_DEV_PROXY_TARGET | where the Vite dev proxy forwards `/api/store`; absolute http(s) URL | `http://localhost:8000` |
| INSTANCE_WRITE_SECRET | NOT a `VITE_` name: read from `process.env` by the Vite dev server (node side) and sent as `X-Instance-Secret`. The dev compose passes it in at launch (see the README's dev section) | unset in a host `npm run dev`; the dev compose passes `unused` when none is configured, so writes are refused |

## Common pitfalls

| # | Pitfall | Do instead |
|---|---|---|
| 1 | In the dev container only `web/src` is bind-mounted; a change to config, `package.json` or the lockfile is not seen | `make dev-build` |
| 2 | `npm run dev` runs `buf generate` first and the dev container has no buf | the compose file runs `vite` directly; regenerate on the host with `make gen-proto-ts` |
| 3 | A new `/ui/v1` message field is invisible until `gen/proto/ts` is regenerated | `make gen-proto-ts`, then restart a stuck dev server |
| 4 | A server field the UI does not know is ignored on decode, so a stale build keeps working but cannot show it | rebuild after a proto change |
| 5 | The vitest `testTimeout` is 20 s (`vitest.config.ts`), set globally because whole-app specs are CPU-bound and exceed the 5 s default under load | The timeout is global, so a slow spec under load is not fixed per spec |
| 6 | Every component test that mounts the shell opens an events socket unless the events client is mocked | `vi.mock('@/api/events', ...)` with the fixture in `src/__tests__/fakeEvents.ts`, as `shell.spec.ts` does |

A pitfall lands here when it is true of this component and nowhere else.
