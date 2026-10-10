// The ONE source of colour, type and spacing tokens (tj-mujie8, tj-grna9p.60). Dark only: there
// is no light scheme. preset.ts, charts.ts, semantics.ts and cssVars.ts are all built from this
// file; nothing else in web/src may hold a hex colour.
//
// Colour values: the owner-approved Harbor palette with the Option 3 purple accent (design canvas,
// page "Visual language", palette B2). Type and spacing: the canvas Data boards (owner ruling 4:
// keep the existing density).

// Accent rule (owner ruling): the accent FILL measures 2.9:1 on the surface, under the 3:1
// graphics minimum. There is deliberately NO plain `accent` token: a component must say which it
// means. accentFill only where a text label carries the meaning (primary buttons, the selected
// toggle segment, the logo mark), with onAccent text on it. accentTint for every shape-only
// indicator (active sidebar bar, tab underline, chart lines, checkboxes, radio dots, the running
// status dot) and for accent-coloured text such as links.
export const colors = {
  ground: '#0d1117',
  surface: '#141a22',
  raised: '#1b232e',
  line: '#283241',
  text: '#e6edf3',
  text2: '#a3b3c5',
  text3: '#8494a8',

  accentFill: '#6e4bc8',
  onAccent: '#ffffff',
  accentTint: '#a58aec',

  up: '#3fb950',
  down: '#f85149',

  status: {
    ok: '#3fb950',
    warn: '#d29922',
    fail: '#f85149',
    info: '#58a6ff',
    retired: '#8494a8',
    running: '#a58aec',
  },

  // Trading groups. Live is orange and appears nowhere else; white on that orange fails contrast,
  // so text on a live-filled surface is onLive (the ground colour).
  group: {
    simulation: '#2fc4b2',
    paper: '#58a6ff',
    live: '#f0883e',
  },
  onLive: '#0d1117',

  // Chart series, in order.
  series: ['#a58aec', '#58a6ff', '#2fc4b2', '#d29922', '#f778ba', '#3fb950'],
} as const

// Self-hosted (src/theme/fonts); no font CDN. Numbers use `numeric` (font-variant-numeric).
export const fonts = {
  sans: "'IBM Plex Sans', system-ui, sans-serif",
  mono: "'IBM Plex Mono', ui-monospace, monospace",
  numeric: 'tabular-nums',
} as const

export const fontWeights = {
  regular: 400,
  medium: 500,
  semibold: 600,
} as const

// Sizes in px, as drawn on the Data boards.
export const fontSizes = {
  // Uppercase section labels (sidebar "Views", "Filters"); see labelLetterSpacing.
  label: 11,
  small: 12,
  body: 13,
  nav: 14,
  panelTitle: 14,
  objectTitle: 15,
  pageTitle: 20,
  tileValue: 22,
} as const

export const labelLetterSpacing = '0.08em'

export const radii = {
  tag: 4,
  control: 6,
  card: 8,
  pill: 13,
} as const

// Density, px, as drawn on the Data boards (owner ruling 4).
export const spacing = {
  topBarHeight: 56,
  subTabsHeight: 46,
  sidebarWidth: 248,
  controlHeight: 34,
  segmentHeight: 28,
  sidebarRowHeight: 32,
  filterRowHeight: 28,
  tagHeight: 24,
  pillHeight: 26,
  tableCellPadding: '9px 12px',
  cardPadding: '14px 16px',
  panelHeaderPadding: '12px 16px',
  sidebarPadding: '18px 16px',
  mainPadding: '22px 28px',
  barPadding: '0 28px',
  pageGap: 18,
  tileGap: 12,
  controlGap: 8,
  statusDot: 7,
} as const

export type ColorTokens = typeof colors
