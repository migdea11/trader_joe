import { describe, expect, it, vi } from 'vitest'
import { create } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'
import type { IGetRowsParams } from 'ag-grid-community'

import { DataSource, DataType, Feed, Granularity, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import {
  ClientKind,
  DatasetFacetsSchema,
  DatasetSummarySchema,
  FreshnessStatus,
  IngestRunState,
} from '@generated/trader_joe/proto/ui/v1/data_pb'

import { ApiError } from '@/api'
import { MISSING } from '@/format/formatters'
import { RUNNING_BADGE } from '@/theme/semantics'
import { createCursorDatasource } from './cursorDatasource'
import type { CursorPage } from './cursorDatasource'
import { facetCounts, usageCount } from './facets'
import {
  filtersKey,
  hasActiveFilters,
  parseCatalogQuery,
  toApiFilters,
  toCatalogQuery,
  withView,
} from './filters'
import { dataTypeLabel, datasetIdentityLabel, granularityLabel, isIntraday, sourceFeedLabel, updateTypeLabel } from './labels'
import { DETAIL_ROUTE, VIEWER_ROUTE, isRunning, rowBadge } from './rows'

// tj-grna9p.32 phase 1 Datasets: the pure modules behind the sidebar, tiles and grid; tj-grna9p.77
// phase 2 added the Running filter, the Collection badge and the datasource's in-place invalidate.

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
  // tj-grna9p.88: the Read By kinds and the fixed stale threshold always go out, so every usage
  // figure answers for the Read By choice; nothing else is sent under All.
  it('the Needs Attention view sends needsAttention true, All sends only the usage parameters', () => {
    expect(toApiFilters({ view: 'needs-attention' }).needsAttention).toBe(true)
    expect(toApiFilters({ view: 'all' })).toStrictEqual({
      status: undefined,
      source: undefined,
      updateType: undefined,
      needsAttention: undefined,
      running: undefined,
      usage: undefined,
      expiresBefore: undefined,
      clientKinds: [ClientKind.SDK],
      staleDays: 90,
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

describe('the Running filter (tj-grna9p.77)', () => {
  it('?running=true reads as running, any other value as unset', () => {
    expect(parseCatalogQuery({ running: 'true' }).running).toBe(true)
    expect(parseCatalogQuery({ running: ['true', 'false'] }).running).toBe(true)
    for (const value of ['false', '1', 'TRUE', '']) expect(parseCatalogQuery({ running: value }).running).toBeUndefined()
    expect(parseCatalogQuery({}).running).toBeUndefined()
  })

  it('is written as running=true, combines with a status, and an unset one leaves no key', () => {
    expect(toCatalogQuery({ view: 'all', status: 'late', running: true })).toEqual({ status: 'late', running: 'true' })
    expect(toCatalogQuery({ view: 'all', running: undefined })).toEqual({})
    expect(toCatalogQuery({ view: 'all' }, { group: 'paper', running: 'true' })).toEqual({ group: 'paper' })
  })

  it('round trips through the URL', () => {
    const filters = { view: 'needs-attention' as const, status: 'failed' as const, running: true }
    const query = toCatalogQuery(filters) as Record<string, string>
    expect(parseCatalogQuery(query)).toMatchObject(filters)
  })

  it('sends running: true to the API only when set, and counts as an active filter', () => {
    expect(toApiFilters({ view: 'all', running: true }).running).toBe(true)
    expect(toApiFilters({ view: 'all' }).running).toBeUndefined()
    expect(hasActiveFilters({ view: 'all', running: true })).toBe(true)
    expect(hasActiveFilters({ view: 'all', running: undefined })).toBe(false)
  })

  it('filtersKey changes with it, so the grid re-queries', () => {
    expect(filtersKey({ view: 'all', running: true })).not.toBe(filtersKey({ view: 'all' }))
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

  it('the Running count is DatasetFacets.running as a number (tj-grna9p.77), never derived from the statuses', () => {
    expect(facetCounts(create(DatasetFacetsSchema, { ...facets, running: 4n })).running).toBe(4)
    expect(facetCounts(facets).running).toBe(0)
    expect(() => facetCounts(create(DatasetFacetsSchema, { running: 2n ** 60n }))).toThrow(RangeError)
  })

  // Phase 2 (tj-grna9p.77) added the Running filter value, so the empty-facets shape carries its count
  // too: a zero the server omits must still read 0 (the sidebar shows "0", not a dash), exactly like
  // the other counts in this object.
  it('an empty response is all zeros, not missing', () => {
    const counts = facetCounts(create(DatasetFacetsSchema, {}))
    expect(counts).toEqual({
      all: 0,
      needsAttention: 0,
      running: 0,
      status: { healthy: 0, late: 0, failed: 0, retired: 0 },
      sources: [],
      updateTypes: { 0: 0, 1: 0, 2: 0, 3: 0 },
      stale: 0,
      read30d: 0,
      neverRead: 0,
      expiringSoon: 0,
    })
  })

  it('the threshold and Expiring Soon bound echoes are carried, unset when the server sent none', () => {
    const bound = Date.UTC(2026, 9, 21, 6, 30)
    const counts = facetCounts(create(DatasetFacetsSchema, { ...facets, staleDays: 30, expiresBefore: timestampFromMs(bound) }))
    expect(counts.staleDays).toBe(30)
    expect(counts.expiresBefore).toEqual(new Date(bound))
    const none = facetCounts(create(DatasetFacetsSchema, {}))
    expect(none.staleDays).toBeUndefined()
    expect(none.expiresBefore).toBeUndefined()
  })

  it('usageCount is the one usage-group-to-count mapping', () => {
    const counts = facetCounts(create(DatasetFacetsSchema, { stale: 9n, read30d: 21n, neverRead: 4n, all: 40n }))
    expect(usageCount(counts, 'read-30d')).toBe(21)
    expect(usageCount(counts, 'stale')).toBe(9)
    expect(usageCount(counts, 'never-read')).toBe(4)
  })

  it('the usage counts (tj-grna9p.88) are DatasetFacets.stale, read_30d, never_read and expiring_soon as numbers', () => {
    const counts = facetCounts(create(DatasetFacetsSchema, { ...facets, stale: 9n, read30d: 21n, neverRead: 4n, expiringSoon: 3n }))
    expect([counts.stale, counts.read30d, counts.neverRead, counts.expiringSoon]).toEqual([9, 21, 4, 3])
    expect(() => facetCounts(create(DatasetFacetsSchema, { stale: 2n ** 60n }))).toThrow(RangeError)
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
    // tj-grna9p.177 (decision tj-hhy3dk): RETIRED is collect off, and the UI reads it Stopped.
    [FreshnessStatus.RETIRED, 'Stopped'],
    [FreshnessStatus.UNSPECIFIED, 'Unknown'],
  ])('freshness %s renders %s', (status, label) => {
    expect(rowBadge(create(DatasetSummarySchema, { freshness: { status } })).label).toBe(label)
  })

  it('a row with no freshness at all is Unknown, never Healthy', () => {
    expect(rowBadge(create(DatasetSummarySchema, {})).label).toBe('Unknown')
  })

  // tj-grna9p.77: the Collection badge is Running while the latest run is in flight, WHATEVER the
  // freshness says (a Late or Failed dataset may be running its catch-up); otherwise the freshness word.
  it.each([
    FreshnessStatus.FRESH,
    FreshnessStatus.LATE,
    FreshnessStatus.GAPS,
    FreshnessStatus.RETIRED,
    FreshnessStatus.UNSPECIFIED,
  ])('a RUNNING last run reads Running over freshness %s', (status) => {
    const row = create(DatasetSummarySchema, { freshness: { status }, lastRun: { state: IngestRunState.RUNNING } })
    expect(isRunning(row)).toBe(true)
    expect(rowBadge(row)).toEqual(RUNNING_BADGE)
  })

  it('Running overlays a row with no health at all, too', () => {
    expect(rowBadge(create(DatasetSummarySchema, { lastRun: { state: IngestRunState.RUNNING } })).label).toBe('Running')
  })

  it.each([
    [IngestRunState.SUCCEEDED, 'Late'],
    [IngestRunState.EMPTY, 'Late'],
    [IngestRunState.FAILED, 'Late'],
    [IngestRunState.UNSPECIFIED, 'Late'],
  ])('a finished last run (%s) leaves the freshness word: %s', (state, label) => {
    const row = create(DatasetSummarySchema, { freshness: { status: FreshnessStatus.LATE }, lastRun: { state } })
    expect(isRunning(row)).toBe(false)
    expect(rowBadge(row).label).toBe(label)
  })

  it('no last run is not running', () => {
    expect(isRunning(create(DatasetSummarySchema, { freshness: { status: FreshnessStatus.FRESH } }))).toBe(false)
  })

  it('the row and its Symbol open the Dataset detail; the Viewer keeps its own route', () => {
    expect(DETAIL_ROUTE).toBe('data-dataset-detail')
    expect(VIEWER_ROUTE).toBe('data-dataset-viewer')
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

  // tj-grna9p.113: the dataset crumb's hover title, every field of the identity on one line. An
  // unknown value reads the way the other helpers read it: an unknown source, data type or width is
  // the dash (MISSING); a feed that does not apply is left out, as sourceFeedLabel leaves it.
  describe('datasetIdentityLabel', () => {
    const vfv = {
      assetSymbol: 'VFV',
      dataType: DataType.MARKET_ACTIVITY,
      granularity: Granularity.ONE_DAY,
      source: DataSource.ALPACA_API,
      feed: Feed.IEX,
    }

    it('joins symbol, data type, width and source with feed, in that order', () => {
      expect(datasetIdentityLabel(vfv)).toBe(
        ['VFV', dataTypeLabel(vfv.dataType), granularityLabel(vfv.granularity), sourceFeedLabel(vfv.source, vfv.feed)].join(' · '),
      )
      expect(datasetIdentityLabel(vfv).split(' · ')).toEqual(['VFV', 'bars', '1d', 'Alpaca', 'IEX'])
    })

    it('every field reaches the line', () => {
      const other = {
        assetSymbol: 'XIC',
        dataType: DataType.QUOTE,
        granularity: Granularity.ONE_HOUR,
        source: DataSource.IB_API,
        feed: Feed.SIP,
      }
      expect(datasetIdentityLabel(other)).toBe(
        ['XIC', dataTypeLabel(DataType.QUOTE), granularityLabel(Granularity.ONE_HOUR), sourceFeedLabel(DataSource.IB_API, Feed.SIP)].join(' · '),
      )
      // Each field changed alone changes the line.
      for (const key of ['assetSymbol', 'dataType', 'granularity', 'source', 'feed'] as const) {
        expect(datasetIdentityLabel({ ...vfv, [key]: other[key] })).not.toBe(datasetIdentityLabel(vfv))
      }
    })

    it('a feed that does not apply is left out; an unknown source, data type or width is the dash', () => {
      expect(datasetIdentityLabel({ ...vfv, feed: Feed.UNSPECIFIED })).toBe(
        ['VFV', 'bars', '1d', sourceFeedLabel(DataSource.ALPACA_API, Feed.UNSPECIFIED)].join(' · '),
      )
      expect(datasetIdentityLabel({ ...vfv, feed: Feed.UNSPECIFIED }).split(' · ')).toEqual(['VFV', 'bars', '1d', 'Alpaca'])
      expect(datasetIdentityLabel({ ...vfv, source: DataSource.UNSPECIFIED, feed: Feed.UNSPECIFIED })).toBe(
        ['VFV', 'bars', '1d', MISSING].join(' · '),
      )
      expect(datasetIdentityLabel({ ...vfv, source: DataSource.UNSPECIFIED })).toBe(['VFV', 'bars', '1d', MISSING, 'IEX'].join(' · '))
      expect(datasetIdentityLabel({ ...vfv, dataType: DataType.UNSPECIFIED, granularity: Granularity.UNSPECIFIED })).toBe(
        ['VFV', MISSING, MISSING, 'Alpaca', 'IEX'].join(' · '),
      )
    })
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

  // tj-grna9p.77: a live event reloads the rows IN PLACE (grid.refreshInfiniteCache on the same
  // datasource), so invalidate must forget the cached pages: the next getRows walks the cursor chain
  // from page one again and serves what the server says now, not the rows fetched before the event.
  it('invalidate serves fresh rows: every cursor is fetched again, from the first page', async () => {
    const pages: Record<string, CursorPage<Row>> = {
      '<first>': { items: ['a', 'b'], nextCursor: 'c1' },
      c1: { items: ['c'], nextCursor: '' },
    }
    const h = harness(pages)
    await h.request(0)
    await h.request(2)
    pages['<first>'] = { items: ['a', 'b2'], nextCursor: 'c1' }
    pages.c1 = { items: ['c2', 'd'], nextCursor: '' }
    h.source.invalidate()
    expect(await h.request(2)).toEqual({ rows: ['c2', 'd'], lastRow: 4 })
    expect(await h.request(0)).toEqual({ rows: ['a', 'b2'], lastRow: undefined })
    expect(h.cursors()).toEqual([undefined, 'c1', undefined, 'c1'])
  })

  it('an answer to a request made before invalidate is dropped, not handed to the grid', async () => {
    let release: (page: CursorPage<Row>) => void = () => undefined
    const fetchPage = vi.fn(
      () =>
        new Promise<CursorPage<Row>>((resolve) => {
          release = resolve
        }),
    )
    const onPage = vi.fn()
    const success = vi.fn()
    const source = createCursorDatasource<Row>({ fetchPage, pageSize: 2, onPage })
    source.getRows({ startRow: 0, endRow: 2, successCallback: success, failCallback: vi.fn() } as unknown as IGetRowsParams)
    source.invalidate()
    release({ items: ['stale'], nextCursor: '' })
    await Promise.resolve()
    await Promise.resolve()
    expect(success).not.toHaveBeenCalled()
    expect(onPage).not.toHaveBeenCalled()
  })
})
