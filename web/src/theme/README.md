# theme/

The visual language (tj-mujie8), dark only. `tokens.ts` is the one source of colour, type and
spacing values; everything else here is built from it.

| File | Role |
|---|---|
| `tokens.ts` | colours, fonts, sizes, radii, density. `accentFill` and `accentTint` are separate on purpose: the tint for every shape-only indicator and for accent text, the fill only where a text label carries the meaning |
| `cssVars.ts` | `--tj-*` CSS custom properties generated from the tokens, set on `:root` at startup |
| `base.css` | page basics (ground colour, Plex Sans, `tabular-nums`, `color-scheme: dark`) |
| `preset.ts` | the PrimeVue 5 preset: Aura restyled with the tokens |
| `charts.ts` | the ECharts theme object and the Lightweight Charts options, from the same tokens |
| `semantics.ts` | status, freshness and trading-group labels and colours, consumed by every badge |
| `copy.ts` | `titleCase()` and the Data tab names; the copy conventions are in its header |
| `fonts/` | self-hosted IBM Plex (OFL), see `fonts/README.md` |

Icons: Lucide through `@lucide/vue` (ISC; icons derived from Feather are MIT), named imports only.
ESLint bans other icon sets and `primeicons` classes.

The dev-only token showcase is at `/dev/tokens` under `npm run dev`; it is not in the production
build.
