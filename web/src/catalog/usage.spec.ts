import { describe, expect, it, vi } from 'vitest'
import { create } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import { ClientKind, DatasetSummarySchema, UsageFilter } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'

import {
  CLIENT_KIND_OPTIONS,
  QUERY_KEYS,
  USAGE_GROUPS,
  effectiveClientKinds,
  expiringBefore,
  filtersKey,
  hasActiveFilters,
  listSort,
  parseCatalogQuery,
  toApiFilters,
  toCatalogQuery,
  toUsageFilters,
} from './filters'
import type { CatalogFilters, UsageGroup } from './filters'
import {
  STALE_LABEL,
  clientKindLabel,
  isUiOnly,
  readByLabel,
  staleLabel,
  staleNote,
  usageCell,
  usageCellText,
  usageOptionLabel,
} from './usage'

// tj-grna9p.88: the Datasets usage and retention filters (the Stale and Expiring Soon views, the
// Usage group, Read By) and the words of the Usage column. The server decides staleness for the
// filters and counts; these modules only carry the choice to it and word what comes back.

const DAY = 86_400_000
const NOW = Date.UTC(2026, 9, 14, 12)

function row(usage?: { lastReadAt?: number; readBy30d?: ClientKind[] }): DatasetSummary {
  return create(DatasetSummarySchema, {
    id: 'ds-1',
    assetSymbol: 'VFV',
    usage:
      usage === undefined
        ? undefined
        : {
            lastReadAt: usage.lastReadAt === undefined ? undefined : timestampFromMs(usage.lastReadAt),
            readBy30d: usage.readBy30d ?? [],
          },
  })
}

describe('the usage and Read By URL keys', () => {
  it('are owned by the screen', () => {
    expect(QUERY_KEYS).toContain('usage')
    expect(QUERY_KEYS).toContain('readby')
  })

  it.each(USAGE_GROUPS)('?usage=%s round trips', (group) => {
    expect(parseCatalogQuery({ usage: group }).usage).toBe(group)
    expect(toCatalogQuery({ view: 'all', usage: group })).toEqual({ usage: group })
  })

  it.each(['STALE', 'read_30d', 'unused', ''])('ignores an unknown usage %j', (value) => {
    expect(parseCatalogQuery({ usage: value }).usage).toBeUndefined()
  })

  it('takes the first of a repeated usage key', () => {
    expect(parseCatalogQuery({ usage: ['never-read', 'stale'] }).usage).toBe('never-read')
  })

  it('?view=expiring-soon reads as the Expiring Soon view and is written back', () => {
    expect(parseCatalogQuery({ view: 'expiring-soon' }).view).toBe('expiring-soon')
    expect(toCatalogQuery({ view: 'expiring-soon' })).toEqual({ view: 'expiring-soon' })
    // The Stale view is the usage value, never a view of its own.
    expect(parseCatalogQuery({ view: 'stale' }).view).toBe('all')
  })

  it.each([
    ['sdk,ui', [ClientKind.SDK, ClientKind.UI]],
    ['ui,sdk', [ClientKind.SDK, ClientKind.UI]],
    ['UI', [ClientKind.UI]],
    ['ui,bogus', [ClientKind.UI]],
  ])('?readby=%s reads as %j, in display order, any case, unknown names dropped', (text, kinds) => {
    expect(parseCatalogQuery({ readby: text }).clientKinds).toEqual(kinds)
  })

  it.each(['sdk', 'SDK', 'bogus', '', 'unspecified'])('?readby=%j is the default (unset), so the URL stays clean', (text) => {
    expect(parseCatalogQuery({ readby: text }).clientKinds).toBeUndefined()
  })

  it('writes readby only when it differs from the default, lower case, comma-joined', () => {
    expect(toCatalogQuery({ view: 'all', clientKinds: [ClientKind.SDK] })).toEqual({})
    expect(toCatalogQuery({ view: 'all', clientKinds: [] })).toEqual({})
    expect(toCatalogQuery({ view: 'all', clientKinds: [ClientKind.UI] })).toEqual({ readby: 'ui' })
    expect(toCatalogQuery({ view: 'all', clientKinds: [ClientKind.UI, ClientKind.SDK] })).toEqual({ readby: 'sdk,ui' })
  })

  it('replaces its own keys and keeps the trading group', () => {
    expect(toCatalogQuery({ view: 'all' }, { group: 'paper', usage: 'stale', readby: 'ui' })).toEqual({ group: 'paper' })
  })

  it.each<CatalogFilters>([
    { view: 'expiring-soon', usage: 'never-read' },
    { view: 'all', usage: 'stale', clientKinds: [ClientKind.SDK, ClientKind.UI] },
    { view: 'needs-attention', clientKinds: [ClientKind.UI] },
  ])('round trips through the URL: %j', (filters) => {
    const query = toCatalogQuery(filters) as Record<string, string>
    expect(parseCatalogQuery(query)).toMatchObject(filters)
  })
})

describe('effectiveClientKinds', () => {
  it('is the default, SDK alone, when Read By is unset or empty', () => {
    expect(effectiveClientKinds({})).toEqual([ClientKind.SDK])
    expect(effectiveClientKinds({ clientKinds: [] })).toEqual([ClientKind.SDK])
  })

  it('keeps display order (SDK, UI) and drops anything not offered', () => {
    expect(CLIENT_KIND_OPTIONS).toEqual([ClientKind.SDK, ClientKind.UI])
    expect(effectiveClientKinds({ clientKinds: [ClientKind.UI, ClientKind.SDK] })).toEqual([ClientKind.SDK, ClientKind.UI])
    expect(effectiveClientKinds({ clientKinds: [ClientKind.UNSPECIFIED] })).toEqual([ClientKind.SDK])
  })

  it('returns a copy: changing it never changes the default', () => {
    effectiveClientKinds({}).push(ClientKind.UI)
    expect(effectiveClientKinds({})).toEqual([ClientKind.SDK])
  })
})

describe('the API filters for usage and retention', () => {
  it('toUsageFilters is the Read By kinds and the fixed threshold, and nothing else', () => {
    expect(toUsageFilters({ view: 'needs-attention', status: 'late', usage: 'stale' })).toStrictEqual({
      clientKinds: [ClientKind.SDK],
      staleDays: 90,
    })
    expect(toUsageFilters({ view: 'all', clientKinds: [ClientKind.UI] }).clientKinds).toEqual([ClientKind.UI])
  })

  it.each<[UsageGroup, UsageFilter]>([
    ['read-30d', UsageFilter.READ_30D],
    ['stale', UsageFilter.STALE],
    ['never-read', UsageFilter.NEVER_READ],
  ])('usage %s goes out as UsageFilter %s', (group, filter) => {
    expect(toApiFilters({ view: 'all', usage: group }).usage).toBe(filter)
  })

  it('expires_before is sent only in Expiring Soon, as now plus EXPIRING_SOON_DAYS', () => {
    vi.useFakeTimers({ now: NOW })
    try {
      expect(expiringBefore(NOW)).toEqual(new Date(NOW + 7 * DAY))
      expect(expiringBefore()).toEqual(new Date(NOW + 7 * DAY))
      expect(toApiFilters({ view: 'expiring-soon' }).expiresBefore).toEqual(new Date(NOW + 7 * DAY))
      expect(toApiFilters({ view: 'all' }).expiresBefore).toBeUndefined()
      expect(toApiFilters({ view: 'needs-attention' }).expiresBefore).toBeUndefined()
      expect(toApiFilters({ view: 'expiring-soon' }).needsAttention).toBeUndefined()
    } finally {
      vi.useRealTimers()
    }
  })

  it('listSort: last read under a usage group, soonest expiry in Expiring Soon, the server default otherwise', () => {
    expect(listSort({ view: 'all' })).toBeUndefined()
    expect(listSort({ view: 'needs-attention', status: 'late' })).toBeUndefined()
    expect(listSort({ view: 'all', usage: 'stale' })).toBe('last_read')
    expect(listSort({ view: 'needs-attention', usage: 'read-30d' })).toBe('last_read')
    expect(listSort({ view: 'expiring-soon' })).toBe('expires')
    expect(listSort({ view: 'expiring-soon', usage: 'never-read' })).toBe('expires')
  })

  it('a usage group and a Read By other than the default are active filters; the default Read By is not', () => {
    expect(hasActiveFilters({ view: 'all', usage: 'never-read' })).toBe(true)
    expect(hasActiveFilters({ view: 'all', clientKinds: [ClientKind.UI] })).toBe(true)
    expect(hasActiveFilters({ view: 'all', clientKinds: [ClientKind.SDK] })).toBe(false)
    expect(hasActiveFilters({ view: 'expiring-soon' })).toBe(false)
  })

  it('filtersKey changes with the view, the usage group and the effective Read By, not with its spelling', () => {
    const base: CatalogFilters = { view: 'all' }
    expect(filtersKey({ ...base, usage: 'stale' })).not.toBe(filtersKey(base))
    expect(filtersKey({ ...base, view: 'expiring-soon' })).not.toBe(filtersKey(base))
    expect(filtersKey({ ...base, clientKinds: [ClientKind.UI] })).not.toBe(filtersKey(base))
    expect(filtersKey({ ...base, clientKinds: [ClientKind.SDK] })).toBe(filtersKey(base))
    expect(filtersKey({ ...base, clientKinds: [ClientKind.UI, ClientKind.SDK] })).toBe(
      filtersKey({ ...base, clientKinds: [ClientKind.SDK, ClientKind.UI] }),
    )
  })
})

describe('Read By and threshold words', () => {
  it('names the kinds', () => {
    expect(clientKindLabel(ClientKind.SDK)).toBe('SDK')
    expect(clientKindLabel(ClientKind.UI)).toBe('UI')
    expect(clientKindLabel(ClientKind.UNSPECIFIED)).toBe('Unknown')
  })

  it('readByLabel names the effective kinds in display order', () => {
    expect(readByLabel({})).toBe('SDK')
    expect(readByLabel({ clientKinds: [ClientKind.UI] })).toBe('UI')
    expect(readByLabel({ clientKinds: [ClientKind.UI, ClientKind.SDK] })).toBe('SDK and UI')
  })

  it('the threshold labels carry the fixed 90 days and name the Read By choice', () => {
    expect(STALE_LABEL).toBe('Stale: 90 d')
    expect(staleNote({})).toBe('unread by SDK 90+ days')
    expect(staleNote({ clientKinds: [ClientKind.SDK, ClientKind.UI] })).toBe('unread by SDK and UI 90+ days')
  })

  // Ruling (A): built from the server's echo facets.staleDays; the unit is never title-cased.
  it('the labels take the echoed threshold over the constant', () => {
    expect(staleLabel(30)).toBe('Stale: 30 d')
    expect(staleLabel(1200)).toBe('Stale: 1,200 d')
    expect(staleLabel()).toBe('Stale: 90 d')
    expect(staleLabel(undefined)).toBe('Stale: 90 d')
    expect(staleNote({}, 30)).toBe('unread by SDK 30+ days')
    expect(usageOptionLabel('stale', {}, 30)).toBe('Stale: unread by SDK 30+ days')
  })

  it('usageOptionLabel words the Usage group in sentence case, with the Read By choice', () => {
    const ui = { clientKinds: [ClientKind.UI] }
    expect(usageOptionLabel('read-30d', ui)).toBe('Read in the last 30 days')
    expect(usageOptionLabel('stale', ui)).toBe('Stale: unread by UI 90+ days')
    expect(usageOptionLabel('never-read', ui)).toBe('Never read by UI')
    expect(usageOptionLabel('never-read', {})).toBe('Never read by SDK')
  })
})

describe('usageCell', () => {
  it('no usage, or usage without a last read, is never read', () => {
    expect(usageCell(row(), NOW)).toEqual({ kind: 'never' })
    expect(usageCell(row({}), NOW)).toEqual({ kind: 'never' })
  })

  // Architect ruling (B), tj-grna9p.88: the age only, in whole UTC calendar days; the cell makes no
  // stale decision (that is the server's Stale view, group and tile).
  it('the age is whole UTC calendar days since the last read, never negative, and carries no stale flag', () => {
    expect(usageCell(row({ lastReadAt: NOW - 3 * DAY - 1000 }), NOW)).toStrictEqual({ kind: 'age', days: 3 })
    expect(usageCell(row({ lastReadAt: NOW - 1000 }), NOW)).toStrictEqual({ kind: 'age', days: 0 })
    // A read stamped a little ahead of this clock is today, not -1 days.
    expect(usageCell(row({ lastReadAt: NOW + 5 * 60_000 }), NOW)).toStrictEqual({ kind: 'age', days: 0 })
    expect(usageCell(row({ lastReadAt: NOW + 3 * DAY }), NOW)).toStrictEqual({ kind: 'age', days: 0 })
  })

  it('counts calendar days, not 24-hour spans: one second across UTC midnight is one day', () => {
    const midnight = Date.UTC(2026, 9, 14)
    expect(usageCell(row({ lastReadAt: midnight - 1000 }), midnight)).toStrictEqual({ kind: 'age', days: 1 })
    expect(usageCell(row({ lastReadAt: midnight }), midnight + DAY - 1000)).toStrictEqual({ kind: 'age', days: 0 })
    // 90 days and a few hours by the clock, read late on its day: 91 calendar days.
    expect(usageCell(row({ lastReadAt: Date.UTC(2026, 6, 15, 23) }), Date.UTC(2026, 9, 14, 2))).toStrictEqual({
      kind: 'age',
      days: 91,
    })
    // Read early on its day, 90 calendar days and a few hours ago: 90.
    expect(usageCell(row({ lastReadAt: Date.UTC(2026, 6, 16, 1) }), Date.UTC(2026, 9, 14, 4))).toStrictEqual({
      kind: 'age',
      days: 90,
    })
  })

  it('defaults now to the clock', () => {
    vi.useFakeTimers({ now: NOW })
    try {
      expect(usageCell(row({ lastReadAt: NOW - 2 * DAY }))).toStrictEqual({ kind: 'age', days: 2 })
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('isUiOnly', () => {
  it('read in 30 days by the UI alone, under a Read By without UI', () => {
    expect(isUiOnly(row({ readBy30d: [ClientKind.UI] }), {})).toBe(true)
    expect(isUiOnly(row({ readBy30d: [ClientKind.UI, ClientKind.UI] }), { clientKinds: [ClientKind.SDK] })).toBe(true)
  })

  it('not when UI is among the kinds read by', () => {
    expect(isUiOnly(row({ readBy30d: [ClientKind.UI] }), { clientKinds: [ClientKind.UI] })).toBe(false)
    expect(isUiOnly(row({ readBy30d: [ClientKind.UI] }), { clientKinds: [ClientKind.SDK, ClientKind.UI] })).toBe(false)
  })

  it('not when an SDK read too, or nothing, or no usage', () => {
    expect(isUiOnly(row({ readBy30d: [ClientKind.UI, ClientKind.SDK] }), {})).toBe(false)
    expect(isUiOnly(row({ readBy30d: [ClientKind.SDK] }), {})).toBe(false)
    expect(isUiOnly(row({ readBy30d: [] }), {})).toBe(false)
    expect(isUiOnly(row(), {})).toBe(false)
  })
})

// tj-grna9p.62: the cell's words, shared by the grid's Usage cell and the Usage tab's Stale Datasets.
describe('usageCellText', () => {
  it.each([
    [{ kind: 'never' }, 'Never read'],
    [{ kind: 'age', days: 0 }, 'today'],
    [{ kind: 'age', days: 1 }, '1 d ago'],
    [{ kind: 'age', days: 200 }, '200 d ago'],
  ] as const)('%o reads %s, and never the word Stale', (cell, text) => {
    expect(usageCellText(cell)).toBe(text)
    expect(usageCellText(cell)).not.toMatch(/stale/i)
  })
})
