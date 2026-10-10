import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import StatusBadge from '@/components/StatusBadge.vue'
import { DATA_SUB_TABS, SECTIONS } from '@/shell/navigation'
import { echartsTheme, lightweightCandlestickOptions, lightweightChartOptions, lightweightLineOptions } from './charts'
import { DATA_TABS, isTitleCase, titleCase } from './copy'
import { tokenCssVariables } from './cssVars'
import { traderJoePreset } from './preset'
import {
  ACCOUNT_GROUP_VALUES,
  FRESHNESS_BADGES,
  FRESHNESS_STATUS_VALUES,
  GROUP_SPECS,
  RUNNING_BADGE,
  STALE_BADGE,
  collectionBadge,
  freshnessBadge,
  needsAttention,
  requestBadge,
} from './semantics'
import { colors, fonts, spacing } from './tokens'

// tj-grna9p.60: the design tokens, the PrimeVue preset, the chart themes and the status maps.
// Every expected colour below is copied from the owner-approved values on tj-mujie8 (palette B2,
// accent Option 3) as restated in the tj-grna9p.60 body, never from tokens.ts itself, so a drifted
// token fails here.

// Read from disk: Vitest does not process CSS, so a ?raw import of a stylesheet comes back empty.
const baseCss = readFileSync(resolve(import.meta.dirname, 'base.css'), 'utf8')
const fontsCss = readFileSync(resolve(import.meta.dirname, 'fonts/fonts.css'), 'utf8')

describe('colour tokens equal the tj-mujie8 values', () => {
  it('neutrals, accent and gain/loss', () => {
    expect(colors.ground).toBe('#0d1117')
    expect(colors.surface).toBe('#141a22')
    expect(colors.raised).toBe('#1b232e')
    expect(colors.line).toBe('#283241')
    expect(colors.text).toBe('#e6edf3')
    expect(colors.text2).toBe('#a3b3c5')
    expect(colors.text3).toBe('#8494a8')
    expect(colors.accentFill).toBe('#6e4bc8')
    expect(colors.onAccent).toBe('#ffffff')
    expect(colors.accentTint).toBe('#a58aec')
    expect(colors.up).toBe('#3fb950')
    expect(colors.down).toBe('#f85149')
  })

  it('status colours', () => {
    expect(colors.status).toEqual({
      ok: '#3fb950',
      warn: '#d29922',
      fail: '#f85149',
      info: '#58a6ff',
      retired: '#8494a8',
      running: '#a58aec',
    })
  })

  it('trading groups, with dark text on Live', () => {
    expect(colors.group).toEqual({ simulation: '#2fc4b2', paper: '#58a6ff', live: '#f0883e' })
    expect(colors.onLive).toBe('#0d1117')
  })

  it('chart series in order', () => {
    expect(colors.series).toEqual(['#a58aec', '#58a6ff', '#2fc4b2', '#d29922', '#f778ba', '#3fb950'])
  })

  it('has no plain accent token, so a component must pick the fill or the tint', () => {
    expect(Object.keys(colors)).not.toContain('accent')
  })

  it('keeps the 248px sidebar and tabular numbers', () => {
    expect(spacing.sidebarWidth).toBe(248)
    expect(fonts.numeric).toBe('tabular-nums')
    expect(fonts.sans).toMatch(/^'IBM Plex Sans'/)
    expect(fonts.mono).toMatch(/^'IBM Plex Mono'/)
  })
})

describe('CSS variables are generated from the tokens', () => {
  it('names each colour --tj-<kebab>', () => {
    const vars = tokenCssVariables()
    expect(vars['--tj-accent-tint']).toBe('#a58aec')
    expect(vars['--tj-accent-fill']).toBe('#6e4bc8')
    expect(vars['--tj-status-running']).toBe('#a58aec')
    expect(vars['--tj-group-live']).toBe('#f0883e')
    expect(vars['--tj-on-live']).toBe('#0d1117')
    expect(vars['--tj-series-1']).toBe('#a58aec')
    expect(vars['--tj-series-6']).toBe('#3fb950')
    expect(vars['--tj-sidebar-width']).toBe('248px')
    expect(vars['--tj-table-cell-padding']).toBe('9px 12px')
  })
})

describe('accessibility rule: shape-only marks use the tint, labelled fills use the fill', () => {
  it('the preset primary (focus ring, checkbox, radio, tab bar) is the tint', () => {
    const primary = traderJoePreset.semantic?.primary as Record<string, string>
    expect(primary.color).toBe(colors.accentTint)
    expect(primary['500']).toBe(colors.accentTint)
    expect(Object.values(primary)).not.toContain(colors.accentFill)
  })

  it('the primary button and the selected toggle segment carry the fill with white text', () => {
    const components = traderJoePreset.components as Record<string, Record<string, Record<string, unknown>>>
    const button = components.button!.root!.primary as Record<string, string>
    expect(button.background).toBe(colors.accentFill)
    expect(button.color).toBe(colors.onAccent)
    expect(components.togglebutton!.content!.checkedBackground).toBe(colors.accentFill)
    expect(components.togglebutton!.root!.checkedColor).toBe(colors.onAccent)
  })

  it('chart lines and the first series are the tint', () => {
    expect(lightweightLineOptions.color).toBe(colors.accentTint)
    expect(echartsTheme.color[0]).toBe(colors.accentTint)
    expect(echartsTheme.color).not.toContain(colors.accentFill)
  })

  it('the running status dot is the tint', () => {
    expect(RUNNING_BADGE.color).toBe(colors.accentTint)
  })

  it('a checked sidebar checkbox is painted with the tint, never the fill, and unchecked is not white', () => {
    const checked = /input\[type='checkbox'\]:checked\s*\{([^}]*)\}/.exec(baseCss)?.[1] ?? ''
    expect(checked).toContain('var(--tj-accent-tint)')
    expect(baseCss).not.toContain('--tj-accent-fill')
    const unchecked = /input\[type='checkbox'\]\s*\{([^}]*)\}/.exec(baseCss)?.[1] ?? ''
    expect(unchecked).toContain('background: var(--tj-raised)')
  })

  it('links and the focus ring use the tint', () => {
    expect(/\ba\s*\{[^}]*color: var\(--tj-accent-tint\)/.test(baseCss)).toBe(true)
    expect(/:focus-visible\s*\{[^}]*var\(--tj-accent-tint\)/.test(baseCss)).toBe(true)
  })
})

describe('chart themes are built from the same tokens', () => {
  it('candles up and down are the gain and loss colours', () => {
    expect(lightweightCandlestickOptions).toEqual({
      upColor: colors.up,
      downColor: colors.down,
      borderUpColor: colors.up,
      borderDownColor: colors.down,
      wickUpColor: colors.up,
      wickDownColor: colors.down,
    })
    expect(echartsTheme.candlestick.itemStyle.color).toBe(colors.up)
    expect(echartsTheme.candlestick.itemStyle.color0).toBe(colors.down)
  })

  it('the Lightweight Charts surface, grid and text are token colours', () => {
    expect(lightweightChartOptions.layout.background.color).toBe(colors.surface)
    expect(lightweightChartOptions.layout.textColor).toBe(colors.text2)
    expect(lightweightChartOptions.grid.horzLines.color).toBe(colors.line)
  })
})

describe('fonts are self-hosted', () => {
  it('every @font-face source is a local woff2, with no CDN', () => {
    const sources = [...fontsCss.matchAll(/url\(([^)]*)\)/g)].map((m) => m[1])
    expect(sources).toHaveLength(5)
    for (const source of sources) expect(source).toMatch(/^'\.\/ibm-plex-(sans|mono)-latin-\d{3}-normal\.woff2'$/)
    expect(fontsCss).not.toMatch(/googleapis|gstatic|https?:/)
  })
})

describe('freshness status map', () => {
  it('covers exactly the seven proto values', () => {
    expect(Object.keys(FRESHNESS_BADGES).sort()).toEqual([...FRESHNESS_STATUS_VALUES].sort())
    expect(FRESHNESS_STATUS_VALUES).toHaveLength(7)
  })

  it.each([
    ['FRESHNESS_STATUS_FRESH', 'Healthy', colors.status.ok],
    ['FRESHNESS_STATUS_COMPLETE', 'Healthy', colors.status.ok],
    ['FRESHNESS_STATUS_LATE', 'Late', colors.status.warn],
    ['FRESHNESS_STATUS_OVERDUE', 'Failed', colors.status.fail],
    ['FRESHNESS_STATUS_GAPS', 'Failed', colors.status.fail],
    ['FRESHNESS_STATUS_RETIRED', 'Retired', colors.status.retired],
    ['FRESHNESS_STATUS_UNSPECIFIED', 'Unknown', colors.text3],
  ])('%s is %s', (status, label, color) => {
    expect(freshnessBadge(status)).toEqual({ label, color })
  })

  it.each([null, undefined, '', 'FRESHNESS_STATUS_STALE', 'fresh', 'HEALTHY', 'toString', '__proto__'])(
    'an unset or unknown value (%j) is Unknown, never Healthy',
    (status) => {
      const badge = freshnessBadge(status as string | null | undefined)
      expect(badge.label).toBe('Unknown')
      expect(badge.color).not.toBe(colors.status.ok)
    },
  )

  it('needs attention is LATE, OVERDUE and GAPS only', () => {
    const flagged = FRESHNESS_STATUS_VALUES.filter((s) => needsAttention(s))
    expect(flagged.sort()).toEqual(['FRESHNESS_STATUS_GAPS', 'FRESHNESS_STATUS_LATE', 'FRESHNESS_STATUS_OVERDUE'])
    expect(needsAttention(null)).toBe(false)
    expect(needsAttention(undefined)).toBe(false)
  })

  it('a running run overlays Running, otherwise the freshness badge shows', () => {
    expect(collectionBadge('FRESHNESS_STATUS_GAPS', { running: true })).toBe(RUNNING_BADGE)
    expect(collectionBadge('FRESHNESS_STATUS_GAPS').label).toBe('Failed')
    expect(collectionBadge(null).label).toBe('Unknown')
  })

  it('the Stale usage badge shares no colour with any collection health state', () => {
    const healthColours = new Set([...Object.values(FRESHNESS_BADGES).map((b) => b.color), RUNNING_BADGE.color])
    expect(STALE_BADGE.label).toBe('Stale')
    expect(healthColours.has(STALE_BADGE.color)).toBe(false)
  })
})

describe('request state map', () => {
  it('a state with no entry, such as REFUSED, renders its raw label in the fail colour', () => {
    expect(requestBadge('REFUSED')).toEqual({ label: 'REFUSED', color: colors.status.fail })
    expect(requestBadge('toString')).toEqual({ label: 'toString', color: colors.status.fail })
  })

  it('known states keep their words', () => {
    expect(requestBadge('queued').label).toBe('Queued')
    expect(requestBadge('running')).toBe(RUNNING_BADGE)
    expect(requestBadge('done').label).toBe('Done')
    expect(requestBadge('failed').label).toBe('Failed')
  })
})

describe('trading group specs', () => {
  it('cover the three groups', () => {
    expect(Object.keys(GROUP_SPECS).sort()).toEqual([...ACCOUNT_GROUP_VALUES].sort())
  })

  it('Simulation and Paper select with the accent fill and white text', () => {
    for (const group of ['ACCOUNT_GROUP_SIMULATION', 'ACCOUNT_GROUP_PAPER'] as const) {
      expect(GROUP_SPECS[group].selectedFill).toBe(colors.accentFill)
      expect(GROUP_SPECS[group].onSelected).toBe(colors.onAccent)
    }
  })

  it('Live selects with orange and DARK text, never white', () => {
    expect(GROUP_SPECS.ACCOUNT_GROUP_LIVE.selectedFill).toBe('#f0883e')
    expect(GROUP_SPECS.ACCOUNT_GROUP_LIVE.onSelected).toBe('#0d1117')
    expect(GROUP_SPECS.ACCOUNT_GROUP_LIVE.onSelected).not.toBe(colors.onAccent)
  })

  it('labels', () => {
    expect(ACCOUNT_GROUP_VALUES.map((g) => GROUP_SPECS[g].label)).toEqual(['Simulation', 'Paper', 'Live'])
  })
})

describe('StatusBadge', () => {
  it('renders the label as text and colours the dot and text from the spec', () => {
    const wrapper = mount(StatusBadge, { props: { badge: freshnessBadge('FRESHNESS_STATUS_LATE') } })
    expect(wrapper.text()).toBe('Late')
    // jsdom normalises hex to rgb(): #d29922 is rgb(210, 153, 34).
    expect(wrapper.attributes('style')).toContain('rgb(210, 153, 34)')
    expect(wrapper.find('.status-badge__dot').attributes('style')).toContain('rgb(210, 153, 34)')
  })
})

describe('titleCase', () => {
  it.each([
    ['bars written per day', 'Bars Written per Day'],
    ['the end of the road', 'The End of the Road'],
    ['what it belongs to', 'What It Belongs To'],
    ['needs attention', 'Needs Attention'],
    ['run duration p50 and p95', 'Run Duration p50 and p95'],
    ['export CSV', 'Export CSV'],
    ['stale: unread 90+ days', 'Stale: Unread 90+ Days'],
    ['source · feed', 'Source · Feed'],
    ['read-only mode', 'Read-Only Mode'],
    ['up-to-date data', 'Up-to-Date Data'],
    ['(in progress)', '(In Progress)'],
    ['an iOS build', 'An iOS Build'],
    ['  two  spaces ', '  Two  Spaces '],
    ['', ''],
  ])('%j -> %j', (input, expected) => {
    expect(titleCase(input)).toBe(expected)
    expect(isTitleCase(expected)).toBe(true)
  })

  it.each(['Bars Written Per Day', 'needs Attention', 'The end', 'Of The Road'])('%j is not title case', (text) => {
    expect(isTitleCase(text)).toBe(false)
  })

  it('the unit-checked string table: tabs, sections and sub-tabs are title case', () => {
    expect(DATA_TABS).toEqual(['Datasets', 'Requests', 'Health', 'Usage'])
    for (const label of [...DATA_TABS, ...SECTIONS.map((s) => s.label), ...DATA_SUB_TABS.map((t) => t.label)]) {
      expect(isTitleCase(label), label).toBe(true)
    }
  })
})
