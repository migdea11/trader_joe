import { describe, expect, it, vi } from 'vitest'
import { create } from '@bufbuild/protobuf'
import type { IGetRowsParams } from 'ag-grid-community'

import { DataSource, Feed, Granularity, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import {
  DatasetFacetsSchema,
  DatasetSummarySchema,
  FreshnessStatus,
} from '@generated/trader_joe/proto/ui/v1/data_pb'

import { ApiError } from '@/api'
import { createCursorDatasource } from './cursorDatasource'
import type { CursorPage } from './cursorDatasource'
import { facetCounts } from './facets'
import {
  filtersKey,
  hasActiveFilters,
  parseCatalogQuery,
  toApiFilters,
  toCatalogQuery,
  withView,
} from './filters'
import { dataTypeLabel, granularityLabel, isIntraday, sourceFeedLabel, updateTypeLabel } from './labels'
import { rowBadge } from './rows'

// tj-grna9p.32 phase 1 Datasets: the pure modules behind the sidebar, tiles and grid.

describe('parseCatalogQuery', () => {
  it('reads every filter from the URL', () => {
    expect(parseCatalogQuery({ view: 'needs-attention', status: 'failed', source: 'alpaca_api', update: 'daily' })).toEqual({
      view: 'needs-attention',
      status: 'failed',
      source: DataSource.ALPACA_API,
      updateType: UpdateType.DAILY,
    })
  })

  it('defaults to All with nothing set', () => {
    expect(parseCatalogQuery({})).toEqual({ view: 'all', status: undefined, source: undefined, updateType: undefined })
  })

  it.each([
    [{ view: 'everything' }, 'view'],
    [{ status: 'stale' }, 'status'],
    [{ status: 'HEALTHY' }, 'status'],
    [{ source: 'unspecified' }, 'source'],
    [{ source: 'nasdaq' }, 'source'],
    [{ update: 'stream' }, 'updateType'],
    [{ update: 'unspecified' }, 'updateType'],
    [{ update: '' }, 'updateType'],
  ])('ignores an unknown or later-phase value %j', (query, key) => {
    const parsed = parseCatalogQuery(query) as unknown as Record<string, unknown>
    expect(parsed[key]).toBe(key === 'view' ? 'all' : undefined)
  })

  it('takes the first of a repeated key, case-insensitively for enum names', () => {
    expect(parseCatalogQuery({ status: ['late', 'failed'], source: ['ALPACA_API'], update: 'STATIC' })).toMatchObject({
      status: 'late',
      source: DataSource.ALPACA_API,
      updateType: UpdateType.STATIC,
    })
  })
})

describe('toCatalogQuery', () => {
  it('writes the filters in lower case and drops unset keys for a clean default URL', () => {
    expect(
      toCatalogQuery({ view: 'needs-attention', status: 'late', source: DataSource.ALPACA_API, updateType: UpdateType.STATIC }),
    ).toEqual({ view: 'needs-attention', status: 'late', source: 'alpaca_api', update: 'static' })
    expect(toCatalogQuery({ view: 'all' })).toEqual({})
  })

  it('keeps keys it does not own (the trading group) and replaces its own', () => {
    expect(toCatalogQuery({ view: 'all' }, { group: 'paper', status: 'late', view: 'needs-attention' })).toEqual({
      group: 'paper',
    })
  })

  it.each([
    { view: 'all' as const },
    { view: 'needs-attention' as const, status: 'retired' as const },
    { view: 'all' as const, source: DataSource.IB_API, updateType: UpdateType.DAILY },
  ])('round trips through the URL: %j', (filters) => {
    const query = toCatalogQuery(filters) as Record<string, string>
    expect(parseCatalogQuery(query)).toEqual({ status: undefined, source: undefined, updateType: undefined, ...filters })
  })
})

describe('toApiFilters and helpers', () => {
  it('the Needs Attention view sends needsAttention true, All sends nothing', () => {
    expect(toApiFilters({ view: 'needs-attention' }).needsAttention).toBe(true)
    expect(toApiFilters({ view: 'all' })).toEqual({
      status: undefined,
      source: undefined,
      updateType: undefined,
      needsAttention: undefined,
    })
    expect(toApiFilters({ view: 'all', status: 'failed', source: DataSource.ALPACA_API })).toMatchObject({
      status: 'failed',
      source: DataSource.ALPACA_API,
    })
  })

  it('hasActiveFilters ignores the view', () => {
    expect(hasActiveFilters({ view: 'needs-attention' })).toBe(false)
    expect(hasActiveFilters({ view: 'all', updateType: UpdateType.DAILY })).toBe(true)
  })

  it('withView keeps the filters, and filtersKey changes exactly with them', () => {
    const base = { view: 'all' as const, status: 'late' as const }
    expect(withView(base, 'needs-attention')).toEqual({ view: 'needs-attention', status: 'late' })
    expect(filtersKey(base)).toBe(filtersKey({ ...base }))
    expect(filtersKey(base)).not.toBe(filtersKey(withView(base, 'needs-attention')))
    expect(filtersKey(base)).not.toBe(filtersKey({ ...base, source: DataSource.ALPACA_API }))
  })
})

describe('facetCounts', () => {
  const facets = create(DatasetFacetsSchema, {
    all: 40n,
    needsAttention: 5n,
    sources: [
      { source: DataSource.ALPACA_API, count: 38n },
      { source: DataSource.IB_API, count: 0n },
      { source: DataSource.UNSPECIFIED, count: 2n },
    ],
    updateTypes: [
      { updateType: UpdateType.DAILY, count: 26n },
      { updateType: UpdateType.STATIC, count: 12n },
    ],
    statuses: [
      { status: FreshnessStatus.FRESH, count: 20n },
      { status: FreshnessStatus.COMPLETE, count: 7n },
      { status: FreshnessStatus.LATE, count: 2n },
      { status: FreshnessStatus.OVERDUE, count: 1n },
      { status: FreshnessStatus.GAPS, count: 3n },
      { status: FreshnessStatus.RETIRED, count: 0n },
      { status: FreshnessStatus.UNSPECIFIED, count: 7n },
    ],
  })

  it('FRESH + COMPLETE is healthy, OVERDUE + GAPS is failed, and unset health is in no status', () => {
    const counts = facetCounts(facets)
    expect(counts.status).toEqual({ healthy: 27, late: 2, failed: 4, retired: 0 })
    const statusTotal = Object.values(counts.status).reduce((a, b) => a + b, 0)
    expect(statusTotal).toBe(counts.all - 7)
  })

  it('keeps zero counts and drops the UNSPECIFIED source', () => {
    const counts = facetCounts(facets)
    expect(counts.sources).toEqual([
      { source: DataSource.ALPACA_API, count: 38 },
      { source: DataSource.IB_API, count: 0 },
    ])
    expect(counts.updateTypes[UpdateType.STREAM]).toBe(0)
    expect(counts.updateTypes[UpdateType.DAILY]).toBe(26)
    expect(counts.all).toBe(40)
    expect(counts.needsAttention).toBe(5)
  })

  it('an empty response is all zeros, not missing', () => {
    const counts = facetCounts(create(DatasetFacetsSchema, {}))
    expect(counts).toEqual({
      all: 0,
      needsAttention: 0,
      status: { healthy: 0, late: 0, failed: 0, retired: 0 },
      sources: [],
      updateTypes: { 0: 0, 1: 0, 2: 0, 3: 0 },
    })
  })

  it('a count beyond the safe-integer range is a RangeError, not a rounded number', () => {
    expect(() => facetCounts(create(DatasetFacetsSchema, { all: 2n ** 60n }))).toThrow(RangeError)
  })
})

describe('row labels and badge', () => {
  it.each([
    [FreshnessStatus.FRESH, 'Healthy'],
    [FreshnessStatus.COMPLETE, 'Healthy'],
    [FreshnessStatus.LATE, 'Late'],
    [FreshnessStatus.OVERDUE, 'Failed'],
    [FreshnessStatus.GAPS, 'Failed'],
    [FreshnessStatus.RETIRED, 'Retired'],
    [FreshnessStatus.UNSPECIFIED, 'Unknown'],
  ])('freshness %s renders %s', (status, label) => {
    expect(rowBadge(create(DatasetSummarySchema, { freshness: { status } })).label).toBe(label)
  })

  it('a row with no freshness at all is Unknown, never Healthy', () => {
    expect(rowBadge(create(DatasetSummarySchema, {})).label).toBe('Unknown')
  })

  it('source and feed, granularity, update and data type words', () => {
    expect(sourceFeedLabel(DataSource.ALPACA_API, Feed.IEX)).toBe('Alpaca · IEX')
    expect(sourceFeedLabel(DataSource.ALPACA_API, Feed.UNSPECIFIED)).toBe('Alpaca')
    expect(granularityLabel(Granularity.ONE_DAY)).toBe('1d')
    expect(granularityLabel(Granularity.UNSPECIFIED)).toBe('—')
    expect(updateTypeLabel(UpdateType.STATIC)).toBe('Bulk')
    expect(updateTypeLabel(UpdateType.DAILY)).toBe('Daily')
    expect(dataTypeLabel(1)).toBe('bars')
    expect(isIntraday(Granularity.ONE_HOUR)).toBe(true)
    expect(isIntraday(Granularity.ONE_DAY)).toBe(false)
  })
})

describe('cursor datasource', () => {
  type Row = string

  function harness(pages: Record<string, CursorPage<Row> | Error>, pageSize = 2) {
    const fetchPage = vi.fn(async (cursor: string | undefined) => {
      const page = pages[cursor ?? '<first>']
      if (page === undefined) throw new Error(`unexpected cursor ${cursor}`)
      if (page instanceof Error) throw page
      return page
    })
    const onPage = vi.fn()
    const onError = vi.fn()
    const source = createCursorDatasource<Row>({ fetchPage, pageSize, onPage, onError })
    const request = (startRow: number) =>
      new Promise<{ rows?: Row[]; lastRow?: number; failed?: true }>((resolve) => {
        source.getRows({
          startRow,
          endRow: startRow + pageSize,
          successCallback: (rows: Row[], lastRow?: number) => resolve({ rows, lastRow }),
          failCallback: () => resolve({ failed: true }),
        } as unknown as IGetRowsParams)
      })
    return { fetchPage, onPage, onError, source, request, cursors: () => fetchPage.mock.calls.map(([c]) => c) }
  }

  it('fetches page one without a cursor and with the page size as the limit', async () => {
    const h = harness({ '<first>': { items: ['a', 'b'], nextCursor: 'c1' } })
    expect(await h.request(0)).toEqual({ rows: ['a', 'b'], lastRow: undefined })
    expect(h.fetchPage).toHaveBeenCalledWith(undefined, 2, expect.any(AbortSignal))
    expect(h.onPage).toHaveBeenLastCalledWith({ loadedRows: 2, done: false })
  })

  it('reports the real row count on the last page', async () => {
    const h = harness({ '<first>': { items: ['a', 'b'], nextCursor: 'c1' }, c1: { items: ['c'], nextCursor: '' } })
    await h.request(0)
    expect(await h.request(2)).toEqual({ rows: ['c'], lastRow: 3 })
    expect(h.onPage).toHaveBeenLastCalledWith({ loadedRows: 3, done: true })
  })

  it('an empty catalog is done at zero rows', async () => {
    const h = harness({ '<first>': { items: [], nextCursor: '' } })
    expect(await h.request(0)).toEqual({ rows: [], lastRow: 0 })
    expect(h.onPage).toHaveBeenLastCalledWith({ loadedRows: 0, done: true })
  })

  it('fetches each cursor exactly once, even when a block is asked for again or concurrently', async () => {
    const h = harness({
      '<first>': { items: ['a', 'b'], nextCursor: 'c1' },
      c1: { items: ['c', 'd'], nextCursor: 'c2' },
      c2: { items: ['e'], nextCursor: '' },
    })
    await Promise.all([h.request(4), h.request(2), h.request(4)])
    await h.request(0)
    await h.request(2)
    expect(h.cursors()).toEqual([undefined, 'c1', 'c2'])
  })

  it('a block past the last page is empty and fetches nothing', async () => {
    const h = harness({ '<first>': { items: ['a', 'b'], nextCursor: '' } })
    await h.request(0)
    expect(await h.request(2)).toEqual({ rows: [], lastRow: 2 })
    expect(h.cursors()).toEqual([undefined])
  })

  it('a next-page failure keeps the loaded rows, and a retry refetches only the failed cursor', async () => {
    const pages: Record<string, CursorPage<Row> | Error> = {
      '<first>': { items: ['a', 'b'], nextCursor: 'c1' },
      c1: new ApiError({ status: 503, errorId: 'e-9' }),
    }
    const h = harness(pages)
    await h.request(0)
    expect(await h.request(2)).toEqual({ failed: true })
    expect(h.onError).toHaveBeenCalledWith(expect.any(ApiError), { loadedRows: 2 })

    pages.c1 = { items: ['c'], nextCursor: '' }
    expect(await h.request(2)).toEqual({ rows: ['c'], lastRow: 3 })
    expect(h.cursors()).toEqual([undefined, 'c1', 'c1'])
  })

  it('after destroy nothing reports and the in-flight request is aborted', async () => {
    let signal: AbortSignal | undefined
    let release: (page: CursorPage<Row>) => void = () => undefined
    const fetchPage = vi.fn(
      (_cursor: string | undefined, _limit: number, s: AbortSignal) =>
        new Promise<CursorPage<Row>>((resolve) => {
          signal = s
          release = resolve
        }),
    )
    const onPage = vi.fn()
    const success = vi.fn()
    const source = createCursorDatasource<Row>({ fetchPage, pageSize: 2, onPage })
    source.getRows({ startRow: 0, endRow: 2, successCallback: success, failCallback: vi.fn() } as unknown as IGetRowsParams)
    source.destroy!()
    expect(signal?.aborted).toBe(true)
    release({ items: ['a'], nextCursor: '' })
    await Promise.resolve()
    await Promise.resolve()
    expect(onPage).not.toHaveBeenCalled()
    expect(success).not.toHaveBeenCalled()
  })
})
