// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ESLint } from 'eslint'

import { create, toJson } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import { DataSource, Granularity, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import {
  ClientKind,
  DatasetState,
  DatasetSummarySchema,
  ExpiryType,
  UsageFilter,
  FreshnessStatus,
  IngestRunPageSchema,
  IngestRunState,
  RunTrigger,
} from '@generated/trader_joe/proto/ui/v1/data_pb'
import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'
import { CoverageStripSchema } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

import { deleteRequest, patchMessage } from './client'
import * as datasetsModule from './datasets'
import { clientQuery } from './datasets'
import * as usageStatsModule from './usageStats'
import {
  API_BASE,
  ApiError,
  buildQuery,
  DEFAULT_CLIENT_KINDS,
  EXPIRING_SOON_DAYS,
  OPERATOR_CLIENT_ID,
  STALE_DAYS,
  deleteDataset,
  getLastReadBuckets,
  getReadsPerDay,
  getStaleDatasets,
  getStorageCurrent,
  getUsageSummary,
  setDatasetCollecting,
  setDatasetExpiry,
  getDataset,
  getDatasetBarPages,
  getDatasetBarPagesRequest,
  getDatasetBars,
  getDatasetBarsRequest,
  getDatasetCoverage,
  getDatasetFacets,
  getDatasetRuns,
  getUiConfig,
  int64ToNumber,
  listDatasets,
  listDatasetsRequest,
} from './index'

// tj-grna9p.29: the typed REST client. The node environment is used so `Response` and `fetch`
// are the real WHATWG ones; only the network is replaced (global fetch is stubbed per test).
//
// THE WIRE FIXTURES BELOW ARE SERVER OUTPUT, NOT HAND-WRITTEN. They were rendered on 2026-10-10 by
// routers.common.proto_json.to_proto_json (the helper every /ui/v1 route answers through) over
// messages built with the generated Python classes from `make proto` -- so field names, enum names,
// int64-as-string and the omission of zero values are exactly what the server sends. The script
// built a DatasetSummary with every field set (bar_count 2**53 - 1), a DatasetPage holding it, a
// DatasetFacets with a zero count, a BarPage with an optional trade_count set on one bar only, a
// UiConfig, and a DatasetSummary with only id and symbol (health unset).
const SUMMARY_JSON = {
  id: '3f6c1d2e-0000-4000-8000-000000000001',
  assetSymbol: 'VFV',
  assetType: 'ASSET_TYPE_STOCK',
  dataType: 'DATA_TYPE_MARKET_ACTIVITY',
  source: 'DATA_SOURCE_ALPACA_API',
  feed: 'FEED_IEX',
  granularity: 'GRANULARITY_ONE_DAY',
  start: '2016-01-04T05:00:00Z',
  end: '2026-10-10T14:00:00Z',
  updateType: 'UPDATE_TYPE_DAILY',
  expiryType: 'EXPIRY_TYPE_ROLLING',
  expiry: '2027-01-01T00:00:00Z',
  owner: 'operator',
  state: 'DATASET_STATE_ACTIVE',
  firstBar: '2016-01-04T05:00:00Z',
  lastBar: '2026-10-09T04:00:00Z',
  barCount: '9007199254740991',
  freshness: {
    status: 'FRESHNESS_STATUS_LATE',
    expectedLastBar: '2026-10-09T04:00:00Z',
    asOf: '2026-10-10T14:00:00Z',
  },
  siblings: [{ id: '3f6c1d2e-0000-4000-8000-000000000002', granularity: 'GRANULARITY_ONE_HOUR' }],
}
const PAGE_JSON = { items: [SUMMARY_JSON], nextCursor: 'eyJzIjoiVkZWIn0' }
const FACETS_JSON = {
  all: '12',
  needsAttention: '3',
  sources: [{ source: 'DATA_SOURCE_ALPACA_API', count: '12' }],
  updateTypes: [
    { updateType: 'UPDATE_TYPE_STATIC', count: '4' },
    { updateType: 'UPDATE_TYPE_DAILY', count: '8' },
  ],
  statuses: [{ status: 'FRESHNESS_STATUS_FRESH', count: '9' }, { status: 'FRESHNESS_STATUS_GAPS' }],
}
const BARS_JSON = {
  bars: [
    {
      barStart: '2026-10-09T04:00:00Z',
      open: 101.5,
      high: 103.25,
      low: 100.0,
      close: 102.75,
      volume: 12345.0,
      tradeCount: '1099511627777',
      vwap: 102.1,
    },
    { barStart: '2026-10-10T04:00:00Z', open: 102.75, high: 104.0, low: 102.0, close: 103.0 },
  ],
}
const CONFIG_JSON = { allowedGroups: ['ACCOUNT_GROUP_SIMULATION'], deploymentLabel: 'dev', serverVersion: '0.4.0' }
const UNSET_SUMMARY_JSON = { id: 'ds-empty', assetSymbol: 'XYZ' }

const fetchMock = vi.fn<typeof fetch>()

function jsonResponse(body: unknown, status = 200, contentType = 'application/json'): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': contentType } })
}

function calledUrl(index = 0): string {
  return String(fetchMock.mock.calls[index]![0])
}

async function rejection(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise
  } catch (error) {
    return error
  }
  throw new Error('expected the call to reject')
}

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('fromJson decode of real server JSON', () => {
  it('DatasetPage', async () => {
    fetchMock.mockResolvedValue(jsonResponse(PAGE_JSON))
    const page = await listDatasets()
    expect(page.nextCursor).toBe('eyJzIjoiVkZWIn0')
    expect(page.items).toHaveLength(1)
    const item = page.items[0]!
    expect(item.assetSymbol).toBe('VFV')
    expect(item.source).toBe(DataSource.ALPACA_API)
    expect(item.updateType).toBe(UpdateType.DAILY)
    expect(item.barCount).toBe(9007199254740991n)
    expect(item.freshness?.status).toBe(FreshnessStatus.LATE)
  })

  it('DatasetSummary, every field', async () => {
    fetchMock.mockResolvedValue(jsonResponse(SUMMARY_JSON))
    const summary = await getDataset(SUMMARY_JSON.id)
    expect(summary.id).toBe(SUMMARY_JSON.id)
    expect(summary.granularity).toBe(Granularity.ONE_DAY)
    expect(summary.expiryType).toBe(ExpiryType.ROLLING)
    expect(summary.state).toBe(DatasetState.ACTIVE)
    expect(summary.owner).toBe('operator')
    expect(summary.start?.seconds).toBe(BigInt(Date.UTC(2016, 0, 4, 5) / 1000))
    expect(summary.end?.seconds).toBe(BigInt(Date.UTC(2026, 9, 10, 14) / 1000))
    expect(summary.expiry?.seconds).toBe(BigInt(Date.UTC(2027, 0, 1) / 1000))
    expect(summary.lastBar?.seconds).toBe(BigInt(Date.UTC(2026, 9, 9, 4) / 1000))
    expect(summary.freshness?.gapCount).toBe(0)
    expect(summary.siblings).toHaveLength(1)
    expect(summary.siblings[0]!.granularity).toBe(Granularity.ONE_HOUR)
  })

  it('DatasetSummary with health unset decodes freshness as absent', async () => {
    fetchMock.mockResolvedValue(jsonResponse(UNSET_SUMMARY_JSON))
    const summary = await getDataset('ds-empty')
    expect(summary.freshness).toBeUndefined()
    expect(summary.barCount).toBe(0n)
    expect(summary.firstBar).toBeUndefined()
  })

  it('DatasetFacets, with a zero count the server omits', async () => {
    fetchMock.mockResolvedValue(jsonResponse(FACETS_JSON))
    const facets = await getDatasetFacets()
    expect(facets.all).toBe(12n)
    expect(facets.needsAttention).toBe(3n)
    expect(facets.sources.map((s) => [s.source, s.count])).toEqual([[DataSource.ALPACA_API, 12n]])
    expect(facets.updateTypes.map((u) => [u.updateType, u.count])).toEqual([
      [UpdateType.STATIC, 4n],
      [UpdateType.DAILY, 8n],
    ])
    expect(facets.statuses.map((s) => [s.status, s.count])).toEqual([
      [FreshnessStatus.FRESH, 9n],
      [FreshnessStatus.GAPS, 0n],
    ])
  })

  // Hand-written in the canonical JSON names of ui/v1 data.proto fields 21 and 22 and DatasetFacets
  // 7..15 (tj-grna9p.88): not rendered by the server like the fixtures above.
  it('DatasetSummary usage and reaper_pending, and the DatasetFacets usage counts', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        ...SUMMARY_JSON,
        usage: {
          lastReadAt: '2026-10-01T12:00:00Z',
          reads30d: '4',
          readBy30d: ['CLIENT_KIND_UI'],
          clientKinds: ['CLIENT_KIND_SDK'],
        },
        reaperPending: true,
      }),
    )
    const summary = await getDataset(SUMMARY_JSON.id)
    expect(summary.reaperPending).toBe(true)
    expect(summary.usage?.lastReadAt?.seconds).toBe(BigInt(Date.UTC(2026, 9, 1, 12) / 1000))
    expect(summary.usage?.reads30d).toBe(4n)
    expect(summary.usage?.readBy30d).toEqual([ClientKind.UI])
    expect(summary.usage?.clientKinds).toEqual([ClientKind.SDK])

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ ...FACETS_JSON, stale: '5', read30d: '6', neverRead: '2', expiringSoon: '3', staleDays: 90 }),
    )
    const facets = await getDatasetFacets()
    expect([facets.stale, facets.read30d, facets.neverRead, facets.expiringSoon]).toEqual([5n, 6n, 2n, 3n])
    expect(facets.staleDays).toBe(90)
  })

  it('BarPage, with optional fields present on one bar and absent on the other', async () => {
    fetchMock.mockResolvedValue(jsonResponse(BARS_JSON))
    const page = await getDatasetBars('ds-1')
    expect(page.nextCursor).toBe('')
    expect(page.bars).toHaveLength(2)
    const [first, second] = page.bars
    expect(first!.barStart?.seconds).toBe(BigInt(Date.UTC(2026, 9, 9, 4) / 1000))
    expect([first!.open, first!.high, first!.low, first!.close, first!.volume]).toEqual([
      101.5, 103.25, 100, 102.75, 12345,
    ])
    expect(first!.tradeCount).toBe(1099511627777n)
    expect(first!.vwap).toBe(102.1)
    expect(second!.volume).toBe(0)
    expect(second!.tradeCount).toBeUndefined()
    expect(second!.vwap).toBeUndefined()
  })

  it('UiConfig', async () => {
    fetchMock.mockResolvedValue(jsonResponse(CONFIG_JSON))
    const config = await getUiConfig()
    expect(config.allowedGroups).toEqual([AccountGroup.SIMULATION])
    expect(config.deploymentLabel).toBe('dev')
    expect(config.serverVersion).toBe('0.4.0')
  })

  it('ignores a field a newer server adds', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ...CONFIG_JSON, addedLater: { x: 1 } }))
    const config = await getUiConfig()
    expect(config.serverVersion).toBe('0.4.0')
  })

  it('drops an enum value a newer server adds rather than failing (ignoreUnknownFields)', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ...CONFIG_JSON, allowedGroups: ['ACCOUNT_GROUP_SIMULATION', 'ACCOUNT_GROUP_MARS'] }))
    const config = await getUiConfig()
    expect(config.allowedGroups).toEqual([AccountGroup.SIMULATION])
  })
})

describe('request shape', () => {
  it('GETs same-origin under /api/store with an Accept header, X-Client-Id: operator and the caller signal', async () => {
    fetchMock.mockResolvedValue(jsonResponse(CONFIG_JSON))
    const controller = new AbortController()
    await getUiConfig(controller.signal)
    expect(API_BASE).toBe('/api/store')
    expect(calledUrl()).toBe('/api/store/ui/v1/config')
    const init = fetchMock.mock.calls[0]![1]!
    expect(init.method).toBe('GET')
    expect(init.headers).toEqual({ Accept: 'application/json', 'X-Client-Id': 'operator' })
    expect(init.signal).toBe(controller.signal)
  })

  // tj-grna9p.131 option R: the label the store records reads by, and derives kind UI from.
  it('every data_store call sends X-Client-Id: operator, from the one constant', async () => {
    fetchMock.mockImplementation(async () => jsonResponse({}))
    expect(OPERATOR_CLIENT_ID).toBe('operator')
    await listDatasets()
    await getDatasetFacets()
    await getDataset('ds-1')
    await getDatasetBars('ds-1')
    await getDatasetBarPages('ds-1')
    await getDatasetRuns('ds-1')
    await getDatasetCoverage('ds-1', 10)
    await getUiConfig()
    expect(fetchMock).toHaveBeenCalledTimes(8)
    for (const [, init] of fetchMock.mock.calls) {
      expect((init!.headers as Record<string, string>)['X-Client-Id']).toBe(OPERATOR_CLIENT_ID)
    }
  })

  // tj-grna9p.93: the list above predates .89's and .62's calls. This one is built from the modules'
  // exports, so a call added later is covered without editing the spec.
  it('EVERY exported network function of datasets.ts and usageStats.ts sends X-Client-Id: operator', async () => {
    fetchMock.mockImplementation(async () => jsonResponse({}))
    const PURE = new Set(['enumName', 'clientQuery'])
    const callers = Object.entries({ ...datasetsModule, ...usageStatsModule }).filter(
      ([name, value]) => typeof value === 'function' && !PURE.has(name) && !name.endsWith('Request'),
    )
    expect(callers.map(([name]) => name).sort()).toEqual(
      expect.arrayContaining([
        'deleteDataset',
        'getReadsPerDay',
        'getStorageCurrent',
        'getUsageSummary',
        'setDatasetCollecting',
        'setDatasetExpiry',
      ]),
    )
    for (const [name, call] of callers) {
      fetchMock.mockClear()
      await (call as (...args: unknown[]) => Promise<unknown>)('ds-1', {})
      expect(fetchMock, name).toHaveBeenCalledTimes(1)
      const headers = fetchMock.mock.calls[0]![1]!.headers as Record<string, string>
      expect(headers['X-Client-Id'], name).toBe(OPERATOR_CLIENT_ID)
    }
  })

  it('the client is the ONE place web/src talks HTTP; the events socket is the one WebSocket', () => {
    const SRC = fileURLToPath(new URL('..', import.meta.url))
    const files = (dir: string): string[] =>
      readdirSync(dir).flatMap((name) => {
        const path = join(dir, name)
        return statSync(path).isDirectory() ? files(path) : [path]
      })
    const sources = files(SRC).filter((p) => /\.(ts|vue)$/.test(p) && !p.endsWith('.spec.ts') && !p.includes('__tests__'))
    const using = (pattern: RegExp) => sources.filter((p) => pattern.test(readFileSync(p, 'utf8'))).map((p) => relative(SRC, p))
    expect(using(/\bfetch\(/)).toEqual([join('api', 'client.ts')])
    expect(using(/new WebSocket\b/)).toEqual([join('api', 'events.ts')])
    expect(using(/XMLHttpRequest|EventSource|sendBeacon/)).toEqual([])
  })

  it('listDatasets sends the usage parameters: client_kind repeated by member name, usage, stale_days, sort=last_read, expires_before', async () => {
    fetchMock.mockResolvedValue(jsonResponse(PAGE_JSON))
    const params = {
      clientKinds: [ClientKind.SDK, ClientKind.UI],
      usage: UsageFilter.READ_30D,
      staleDays: 90,
      expiresBefore: new Date(Date.UTC(2026, 9, 21, 6)),
      sort: 'last_read' as const,
    }
    await listDatasets(params)
    const url = new URL(calledUrl(), 'http://x')
    expect([...url.searchParams.entries()]).toEqual([
      ['client_kind', 'SDK'],
      ['client_kind', 'UI'],
      ['usage', 'READ_30D'],
      ['stale_days', '90'],
      ['expires_before', '2026-10-21T06:00:00.000Z'],
      ['sort', 'last_read'],
    ])
    expect(listDatasetsRequest(params)).toBe(calledUrl())
  })

  it.each([
    [UsageFilter.STALE, 'STALE'],
    [UsageFilter.NEVER_READ, 'NEVER_READ'],
  ])('usage %s goes out as %s; an empty clientKinds sends no client_kind', async (usage, name) => {
    fetchMock.mockResolvedValue(jsonResponse(FACETS_JSON))
    await getDatasetFacets({ usage, clientKinds: [] })
    expect(calledUrl()).toBe(`/api/store/ui/v1/datasets/facets?usage=${name}`)
  })

  it('getDatasetFacets sends the usage parameters too', async () => {
    fetchMock.mockResolvedValue(jsonResponse(FACETS_JSON))
    await getDatasetFacets({ clientKinds: [ClientKind.UI], staleDays: 90, expiresBefore: new Date(Date.UTC(2026, 0, 1)) })
    expect(calledUrl()).toBe(
      '/api/store/ui/v1/datasets/facets?client_kind=UI&stale_days=90&expires_before=2026-01-01T00%3A00%3A00.000Z',
    )
  })

  it('listDatasets sends every parameter under the server names, enums by member name', async () => {
    fetchMock.mockResolvedValue(jsonResponse(PAGE_JSON))
    const params = {
      assetSymbol: 'vf',
      source: DataSource.ALPACA_API,
      updateType: UpdateType.DAILY,
      status: 'failed' as const,
      needsAttention: true,
      sort: 'expires' as const,
      cursor: 'c/1',
      limit: 50,
    }
    await listDatasets(params)
    const url = new URL(calledUrl(), 'http://x')
    expect(url.pathname).toBe('/api/store/ui/v1/datasets')
    expect([...url.searchParams.entries()]).toEqual([
      ['asset_symbol', 'vf'],
      ['source', 'ALPACA_API'],
      ['update_type', 'DAILY'],
      ['status', 'failed'],
      ['needs_attention', 'true'],
      ['sort', 'expires'],
      ['cursor', 'c/1'],
      ['limit', '50'],
    ])
    expect(listDatasetsRequest(params)).toBe(calledUrl())
  })

  it('listDatasets with nothing set sends no query, so the server defaults apply', async () => {
    fetchMock.mockResolvedValue(jsonResponse(PAGE_JSON))
    await listDatasets()
    expect(calledUrl()).toBe('/api/store/ui/v1/datasets')
  })

  it('getDatasetFacets sends the filters only, never paging or sort', async () => {
    fetchMock.mockResolvedValue(jsonResponse(FACETS_JSON))
    await getDatasetFacets({ assetSymbol: 'V', source: DataSource.ALPACA_API, updateType: UpdateType.STATIC, status: 'late', needsAttention: true })
    expect(calledUrl()).toBe(
      '/api/store/ui/v1/datasets/facets?asset_symbol=V&source=ALPACA_API&update_type=STATIC&status=late&needs_attention=true',
    )
  })

  it('getDataset URL-encodes the id', async () => {
    fetchMock.mockResolvedValue(jsonResponse(SUMMARY_JSON))
    await getDataset('a/b c?d#e')
    expect(calledUrl()).toBe('/api/store/ui/v1/datasets/a%2Fb%20c%3Fd%23e')
  })

  it('getDatasetBars URL-encodes the id and sends start and end as UTC instants', async () => {
    fetchMock.mockResolvedValue(jsonResponse(BARS_JSON))
    const params = {
      start: new Date(Date.UTC(2026, 0, 2, 5)),
      end: new Date(Date.UTC(2026, 0, 3, 5)),
      cursor: 'k',
      limit: 10000,
    }
    await getDatasetBars('x/y', params)
    expect(calledUrl()).toBe(
      '/api/store/ui/v1/datasets/x%2Fy/bars?start=2026-01-02T05%3A00%3A00.000Z&end=2026-01-03T05%3A00%3A00.000Z&cursor=k&limit=10000',
    )
    expect(getDatasetBarsRequest('x/y', params)).toBe(calledUrl())
  })
})

// tj-grna9p.121: the chart's page index, GET /ui/v1/datasets/{id}/bar-pages (route tj-grna9p.120,
// decision tj-eozqhi). The JSON is canonical proto3 as the server's to_proto_json writes it: int64 as a
// string, and a zero value OMITTED, so the earliest page (offset 0) carries no offset key.
describe('bar pages (tj-grna9p.121)', () => {
  const BAR_PAGES_JSON = {
    barCount: '2500',
    pageSize: 1000,
    overlap: 50,
    pages: [
      { start: '2026-01-05T14:30:00Z' },
      { start: '2026-01-06T14:30:00Z', offset: '500' },
      { start: '2026-01-07T14:30:00Z', offset: '1500' },
    ],
  }

  it('sends the window, size and look-ahead under the server names and the caller signal', async () => {
    fetchMock.mockResolvedValue(jsonResponse(BAR_PAGES_JSON))
    const controller = new AbortController()
    const params = {
      start: new Date(Date.UTC(2026, 0, 2, 5)),
      end: new Date(Date.UTC(2026, 0, 3, 5)),
      size: 1000,
      overlap: 50,
      signal: controller.signal,
    }
    await getDatasetBarPages('x/y', params)
    expect(calledUrl()).toBe(
      '/api/store/ui/v1/datasets/x%2Fy/bar-pages?start=2026-01-02T05%3A00%3A00.000Z&end=2026-01-03T05%3A00%3A00.000Z&size=1000&overlap=50',
    )
    expect(fetchMock.mock.calls[0]![1]!.signal).toBe(controller.signal)
    expect(getDatasetBarPagesRequest('x/y', params)).toBe(calledUrl())
  })

  it('with nothing set sends no query, so the server defaults apply', async () => {
    fetchMock.mockResolvedValue(jsonResponse(BAR_PAGES_JSON))
    await getDatasetBarPages('ds-1')
    expect(calledUrl()).toBe('/api/store/ui/v1/datasets/ds-1/bar-pages')
    expect(getDatasetBarPagesRequest('ds-1')).toBe('/api/store/ui/v1/datasets/ds-1/bar-pages')
  })

  it('decodes the index: int64 as bigint, and the omitted zero offset as 0n', async () => {
    fetchMock.mockResolvedValue(jsonResponse(BAR_PAGES_JSON))
    const index = await getDatasetBarPages('ds-1')
    expect(index.barCount).toBe(2500n)
    expect(index.pageSize).toBe(1000)
    expect(index.overlap).toBe(50)
    expect(index.pages.map((p) => p.offset)).toEqual([0n, 500n, 1500n])
    expect(index.pages[0]!.start?.seconds).toBe(BigInt(Date.UTC(2026, 0, 5, 14, 30) / 1000))
  })

  it('an empty window decodes to no pages and a zero count', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ pageSize: 1000, overlap: 50 }))
    const index = await getDatasetBarPages('ds-1')
    expect(index.barCount).toBe(0n)
    expect(index.pages).toEqual([])
  })
})

// tj-grna9p.34 and .77: the runs page and the coverage strip. The JSON is rendered from messages built
// with create(Schema) by toJson, the canonical form the server's to_proto_json also writes (int64 as
// a string, enum member names, zero values omitted).
describe('runs and coverage (phase 2)', () => {
  const RUNS_JSON = toJson(
    IngestRunPageSchema,
    create(IngestRunPageSchema, {
      items: [
        {
          id: 'run-2',
          datasetId: 'ds-1',
          trigger: RunTrigger.REQUEST,
          state: IngestRunState.FAILED,
          barsWritten: 2n ** 53n + 1n,
          error: { domain: 'VENDOR', reason: 'RATE_LIMITED', message: 'slow down' },
          startedAt: timestampFromMs(Date.UTC(2026, 9, 12, 14)),
        },
      ],
      nextCursor: 'k/2',
    }),
  )
  const COVERAGE_JSON = toJson(
    CoverageStripSchema,
    create(CoverageStripSchema, {
      buckets: [{ expectedSessions: 5, sessionsWithBars: 4, start: timestampFromMs(Date.UTC(2026, 0, 5, 5)) }],
      gaps: [{ sessions: 1 }],
      gapCount: 3,
    }),
  )

  it('getDatasetRuns: GET /ui/v1/datasets/{id}/runs with cursor and limit, decoded with bigint intact', async () => {
    fetchMock.mockResolvedValue(jsonResponse(RUNS_JSON))
    const controller = new AbortController()
    const page = await getDatasetRuns('a/b', { cursor: 'k/1', limit: 50, signal: controller.signal })
    expect(calledUrl()).toBe('/api/store/ui/v1/datasets/a%2Fb/runs?cursor=k%2F1&limit=50')
    expect(fetchMock.mock.calls[0]![1]!.signal).toBe(controller.signal)
    expect(page.nextCursor).toBe('k/2')
    expect(page.items[0]!.barsWritten).toBe(2n ** 53n + 1n)
    expect(page.items[0]!.state).toBe(IngestRunState.FAILED)
    expect(page.items[0]!.error).toMatchObject({ domain: 'VENDOR', reason: 'RATE_LIMITED', message: 'slow down' })
  })

  it('getDatasetRuns with nothing set sends no query (the server pages 50 newest first)', async () => {
    fetchMock.mockResolvedValue(jsonResponse(RUNS_JSON))
    await getDatasetRuns('ds-1')
    expect(calledUrl()).toBe('/api/store/ui/v1/datasets/ds-1/runs')
  })

  it('getDatasetCoverage: GET /ui/v1/datasets/{id}/coverage?buckets=N with the signal, decoded', async () => {
    fetchMock.mockResolvedValue(jsonResponse(COVERAGE_JSON))
    const controller = new AbortController()
    const strip = await getDatasetCoverage('a/b', 60, controller.signal)
    expect(calledUrl()).toBe('/api/store/ui/v1/datasets/a%2Fb/coverage?buckets=60')
    expect(fetchMock.mock.calls[0]![1]!.signal).toBe(controller.signal)
    expect(strip.gapCount).toBe(3)
    expect(strip.buckets[0]).toMatchObject({ expectedSessions: 5, sessionsWithBars: 4 })
  })

  it('a 422 from the coverage route is an ApiError with status 422 (the "no calendar" case)', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ status: 422, reason: 'NO_TRADING_CALENDAR' }, 422, 'application/problem+json'),
    )
    const error = await rejection(getDatasetCoverage('ds-1', 120))
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(422)
  })
})

describe('failures surface as ApiError', () => {
  it('problem+json maps status, reason and error_id and never leaks the body text', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          type: 'about:blank',
          title: 'SECRET-TITLE',
          status: 422,
          detail: 'SECRET-DETAIL quoting AAPL',
          reason: 'INVALID_REQUEST',
          error_id: 'err-123',
          errors: [{ msg: 'SECRET-VALIDATION' }],
        },
        422,
        'application/problem+json',
      ),
    )
    const error = await rejection(listDatasets())
    expect(error).toBeInstanceOf(ApiError)
    const api = error as ApiError
    expect(api.status).toBe(422)
    expect(api.reason).toBe('INVALID_REQUEST')
    expect(api.errorId).toBe('err-123')
    expect(api.message).toBe('Request failed (HTTP 422)')
    const everything = `${api.message} ${String(api)} ${JSON.stringify(api)} ${api.stack ?? ''}`
    expect(everything).not.toMatch(/SECRET|AAPL/)
  })

  it('takes the first error_id when the server sends a list', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ reason: 'NOT_FOUND', error_id: ['e-1', 'e-2'] }, 404, 'application/problem+json'))
    const error = (await rejection(getDataset('x'))) as ApiError
    expect(error.status).toBe(404)
    expect(error.reason).toBe('NOT_FOUND')
    expect(error.errorId).toBe('e-1')
  })

  it.each([
    ['a non-JSON body', new Response('<html>SECRET</html>', { status: 502, headers: { 'content-type': 'text/html' } })],
    ['a JSON content type with a broken body', new Response('{not json', { status: 500, headers: { 'content-type': 'application/json' } })],
    ['a JSON body that is not an object', jsonResponse(['SECRET'], 503)],
    ['empty reason and error_id', jsonResponse({ reason: '', error_id: '' }, 500)],
  ])('%s keeps the status and no reason', async (_name, response) => {
    fetchMock.mockResolvedValue(response)
    const error = (await rejection(getUiConfig())) as ApiError
    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(response.status)
    expect(error.reason).toBeUndefined()
    expect(error.errorId).toBeUndefined()
    expect(error.message).not.toContain('SECRET')
  })

  it('a network failure is status 0', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    const error = (await rejection(getUiConfig())) as ApiError
    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(0)
    expect(error.reason).toBeUndefined()
  })

  it('an abort is rethrown as the AbortError, not an ApiError', async () => {
    const abort = new DOMException('The operation was aborted.', 'AbortError')
    fetchMock.mockRejectedValue(abort)
    const error = await rejection(getUiConfig())
    expect(error).toBe(abort)
    expect(error).not.toBeInstanceOf(ApiError)
  })

  it.each([
    ['a wrong type for a field', { allowedGroups: 'ACCOUNT_GROUP_SIMULATION' }],
    ['an int64 that is not a number', { ...SUMMARY_JSON, barCount: 'lots' }],
  ])('a 2xx body that does not decode (%s) is MALFORMED_RESPONSE', async (_name, body) => {
    fetchMock.mockResolvedValue(jsonResponse(body))
    const call = 'barCount' in body ? getDataset('x') : getUiConfig()
    const error = (await rejection(call)) as ApiError
    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(200)
    expect(error.reason).toBe('MALFORMED_RESPONSE')
  })

  it('a 2xx body that is not JSON at all is MALFORMED_RESPONSE', async () => {
    fetchMock.mockResolvedValue(new Response('<html>', { status: 200, headers: { 'content-type': 'text/html' } }))
    const error = (await rejection(getUiConfig())) as ApiError
    expect(error.reason).toBe('MALFORMED_RESPONSE')
  })
})

describe('int64ToNumber', () => {
  it.each([
    [0n, 0],
    [1n, 1],
    [-1n, -1],
    [BigInt(Number.MAX_SAFE_INTEGER), Number.MAX_SAFE_INTEGER],
    [BigInt(Number.MIN_SAFE_INTEGER), Number.MIN_SAFE_INTEGER],
  ])('%s converts exactly', (value, expected) => {
    expect(int64ToNumber(value)).toBe(expected)
  })

  it.each([BigInt(Number.MAX_SAFE_INTEGER) + 1n, BigInt(Number.MIN_SAFE_INTEGER) - 1n, 2n ** 63n - 1n])(
    '%s throws a RangeError rather than losing precision',
    (value) => {
      expect(() => int64ToNumber(value)).toThrow(RangeError)
    },
  )
})

// tj-grna9p.88: one module holds the usage constants (owner rulings tj-utqd77 Q3, tj-grna9p.131).
describe('usage constants', () => {
  it('stale 90 days fixed, expiring soon 7 days, Read By defaults to SDK alone, the UI is operator', () => {
    expect(STALE_DAYS).toBe(90)
    expect(EXPIRING_SOON_DAYS).toBe(7)
    expect(DEFAULT_CLIENT_KINDS).toEqual([ClientKind.SDK])
    expect(OPERATOR_CLIENT_ID).toBe('operator')
  })
})

describe('buildQuery', () => {
  it('is empty when nothing is set', () => {
    expect(buildQuery({})).toBe('')
    expect(buildQuery({ a: undefined, b: null, c: '' })).toBe('')
  })

  it('omits unset values, keeps false and 0, and keeps insertion order', () => {
    expect(buildQuery({ z: 'last', a: undefined, flag: false, n: 0, b: null, s: '' })).toBe('?z=last&flag=false&n=0')
  })

  it('writes a Date as a UTC instant with Z', () => {
    expect(buildQuery({ at: new Date(Date.UTC(2026, 9, 10, 4, 5, 6, 7)) })).toBe('?at=2026-10-10T04%3A05%3A06.007Z')
  })

  it('repeats the key for each array element, in order, and an empty array sends nothing', () => {
    expect(buildQuery({ a: ['x', 'y'], b: [], c: 1 })).toBe('?a=x&a=y&c=1')
    expect(buildQuery({ k: [new Date(Date.UTC(2026, 0, 1)), 2, false] })).toBe('?k=2026-01-01T00%3A00%3A00.000Z&k=2&k=false')
    expect(buildQuery({ only: [] })).toBe('')
  })

  it('encodes reserved characters', () => {
    expect(buildQuery({ q: 'a&b=c d+e' })).toBe('?q=a%26b%3Dc+d%2Be')
  })
})

describe('no polling', () => {
  it('lint refuses setInterval in web/src, bare and as a member call', async () => {
    const eslint = new ESLint({ cwd: `${import.meta.dirname}/../..` })
    const [bare] = await eslint.lintText('setInterval(() => undefined, 1000)\n', { filePath: 'src/probe.ts' })
    const [member] = await eslint.lintText('window.setInterval(() => undefined, 1000)\n', { filePath: 'src/probe.ts' })
    const [timeout] = await eslint.lintText('setTimeout(() => undefined, 1000)\n', { filePath: 'src/probe.ts' })
    for (const result of [bare!, member!]) {
      expect(result.messages.map((m) => m.message)).toContainEqual(expect.stringMatching(/No polling/))
    }
    expect(timeout!.messages.filter((m) => /No polling/.test(m.message))).toEqual([])
  }, 60_000)
})

// tj-grna9p.89: the two Retention writes and the Usage reads. The bodies are protobuf canonical JSON
// (SetExpiryRequest, DeleteDatasetRequest in data.proto); the reads-per-day parameters follow the
// .64 brief (days, dataset_id, client_id | client_kind; no source, no expires_before).
describe('writes (tj-grna9p.89)', () => {
  const init = (index = 0): RequestInit => fetchMock.mock.calls[index]![1]!
  const headers = (index = 0) => init(index).headers as Record<string, string>
  const body = (index = 0): unknown => {
    const text = init(index).body
    return text === undefined ? undefined : JSON.parse(String(text))
  }

  it('patchMessage PATCHes under API_BASE with a JSON body and the operator id, and decodes the answer', async () => {
    fetchMock.mockResolvedValue(jsonResponse(SUMMARY_JSON))
    const summary = await patchMessage(DatasetSummarySchema, '/ui/v1/x', { body: { a: 1 } })
    expect(calledUrl()).toBe(`${API_BASE}/ui/v1/x`)
    expect(init().method).toBe('PATCH')
    expect(headers()).toEqual({
      Accept: 'application/json',
      'X-Client-Id': OPERATOR_CLIENT_ID,
      'Content-Type': 'application/json',
    })
    expect(body()).toEqual({ a: 1 })
    expect(summary.assetSymbol).toBe('VFV')
  })

  it('patchMessage: a 2xx body that does not decode is MALFORMED_RESPONSE', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ barCount: 'lots' }))
    const error = (await rejection(patchMessage(DatasetSummarySchema, '/ui/v1/x'))) as ApiError
    expect(error.status).toBe(200)
    expect(error.reason).toBe('MALFORMED_RESPONSE')
  })

  it('deleteRequest with no body sends no body and no Content-Type, and resolves on 204', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }))
    await expect(deleteRequest('/ui/v1/x')).resolves.toBeUndefined()
    expect(init().method).toBe('DELETE')
    expect(init().body).toBeUndefined()
    expect(headers()).toEqual({ Accept: 'application/json', 'X-Client-Id': OPERATOR_CLIENT_ID })
  })

  it.each([
    ['409 OVERRIDE_REQUIRED', 409, 'OVERRIDE_REQUIRED'],
    ['503 DATABASE_UNAVAILABLE', 503, 'DATABASE_UNAVAILABLE'],
    ['404 NOT_FOUND', 404, 'NOT_FOUND'],
    ['422 INVALID_REQUEST', 422, 'INVALID_REQUEST'],
  ])('a write refused with %s is an ApiError with that status and reason, never the detail', async (_name, status, reason) => {
    const refusal = () =>
      jsonResponse({ reason, error_id: 'w-1', detail: 'SECRET held by someone-else: my reason' }, status, 'application/problem+json')
    fetchMock.mockResolvedValueOnce(refusal()).mockResolvedValueOnce(refusal())
    for (const call of [patchMessage(DatasetSummarySchema, '/ui/v1/x', { body: {} }), deleteRequest('/ui/v1/x')]) {
      const error = (await rejection(call)) as ApiError
      expect(error).toBeInstanceOf(ApiError)
      expect([error.status, error.reason, error.errorId]).toEqual([status, reason, 'w-1'])
      expect(`${error.message} ${String(error)}`).not.toMatch(/SECRET|someone-else|my reason/)
    }
  })

  it('a write: a network failure is status 0, an abort is rethrown as itself', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    expect(((await rejection(deleteRequest('/ui/v1/x'))) as ApiError).status).toBe(0)
    const abort = new DOMException('aborted', 'AbortError')
    fetchMock.mockRejectedValueOnce(abort)
    expect(await rejection(patchMessage(DatasetSummarySchema, '/ui/v1/x'))).toBe(abort)
  })

  it('setDatasetExpiry: PATCH .../{id}/expiry with the expiry as an RFC 3339 instant; the id is encoded', async () => {
    fetchMock.mockResolvedValue(jsonResponse(SUMMARY_JSON))
    const result = await setDatasetExpiry('a/b c', { expiry: new Date(Date.UTC(2026, 9, 15, 4)) })
    expect(calledUrl()).toBe(`${API_BASE}/ui/v1/datasets/a%2Fb%20c/expiry`)
    expect(init().method).toBe('PATCH')
    expect(body()).toEqual({ expiry: '2026-10-15T04:00:00.000Z' })
    expect(result.id).toBe(SUMMARY_JSON.id)
  })

  it('setDatasetExpiry: no expiry is the clear, an empty object (unset expiry), and no override key', async () => {
    fetchMock.mockResolvedValue(jsonResponse(SUMMARY_JSON))
    await setDatasetExpiry('ds-1', {})
    expect(body()).toEqual({})
  })

  it('setDatasetExpiry and deleteDataset carry the override as {reason, confirmed: true} when given', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(SUMMARY_JSON)).mockResolvedValueOnce(new Response(null, { status: 204 }))
    const override = { reason: 'cleanup', confirmed: true as const }
    await setDatasetExpiry('ds-1', { override })
    await deleteDataset('ds-1', { override })
    expect(body(0)).toEqual({ override: { reason: 'cleanup', confirmed: true } })
    expect(calledUrl(1)).toBe(`${API_BASE}/ui/v1/datasets/ds-1`)
    expect(init(1).method).toBe('DELETE')
    expect(body(1)).toEqual({ override: { reason: 'cleanup', confirmed: true } })
  })

  // tj-grna9p.177 (the F1 ruling on tj-grna9p.174): the collecting PATCH body ALWAYS carries collect, true
  // or false; the route answers a body without it with 422. The override is the expiry write's.
  it.each([
    ['collect true', true],
    ['collect false', false],
  ])('setDatasetCollecting (%s): PATCH .../{id}/collecting, the id encoded, the body exactly {collect}', async (_name, collect) => {
    fetchMock.mockResolvedValue(jsonResponse({ ...SUMMARY_JSON, collecting: collect }))
    const result = await setDatasetCollecting('a/b c', { collect })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(calledUrl()).toBe(`${API_BASE}/ui/v1/datasets/a%2Fb%20c/collecting`)
    expect(init().method).toBe('PATCH')
    expect(headers()).toEqual({ Accept: 'application/json', 'X-Client-Id': OPERATOR_CLIENT_ID, 'Content-Type': 'application/json' })
    // The raw text, not only the parsed object: the key is present, never dropped as a default.
    expect(init().body).toBe(JSON.stringify({ collect }))
    expect(body()).toEqual({ collect })
    expect(Object.keys(body() as object)).toEqual(['collect'])
    expect(result.id).toBe(SUMMARY_JSON.id)
    expect(result.collecting).toBe(collect)
  })

  it('setDatasetCollecting carries the override beside collect, and still never omits collect', async () => {
    fetchMock.mockImplementation(async () => jsonResponse(SUMMARY_JSON))
    const override = { reason: 'cleanup', confirmed: true as const }
    await setDatasetCollecting('ds-1', { collect: false, override })
    await setDatasetCollecting('ds-1', { collect: true, override })
    expect(body(0)).toEqual({ collect: false, override: { reason: 'cleanup', confirmed: true } })
    expect(body(1)).toEqual({ collect: true, override: { reason: 'cleanup', confirmed: true } })
    expect(calledUrl(1)).toBe(`${API_BASE}/ui/v1/datasets/ds-1/collecting`)
  })

  it('setDatasetCollecting with override undefined sends no override key at all', async () => {
    fetchMock.mockResolvedValue(jsonResponse(SUMMARY_JSON))
    await setDatasetCollecting('ds-1', { collect: false, override: undefined })
    expect(init().body).toBe('{"collect":false}')
  })

  it('setDatasetCollecting passes the caller signal', async () => {
    fetchMock.mockResolvedValue(jsonResponse(SUMMARY_JSON))
    const controller = new AbortController()
    await setDatasetCollecting('ds-1', { collect: true, signal: controller.signal })
    expect(init().signal).toBe(controller.signal)
  })

  it.each([
    ['409 OVERRIDE_REQUIRED', 409, 'OVERRIDE_REQUIRED'],
    ['422 INVALID_REQUEST', 422, 'INVALID_REQUEST'],
  ])('setDatasetCollecting refused with %s is an ApiError with that status and reason', async (_name, status, reason) => {
    fetchMock.mockResolvedValue(jsonResponse({ reason, error_id: 'c-1', detail: 'held by bot-7' }, status, 'application/problem+json'))
    const error = (await rejection(setDatasetCollecting('ds-1', { collect: false }))) as ApiError
    expect(error).toBeInstanceOf(ApiError)
    expect([error.status, error.reason, error.errorId]).toEqual([status, reason, 'c-1'])
    expect(String(error)).not.toContain('bot-7')
  })

  it('setDatasetExpiry never sends collect: the expiry write is the deletion date only', async () => {
    fetchMock.mockImplementation(async () => jsonResponse(SUMMARY_JSON))
    await setDatasetExpiry('ds-1', { expiry: new Date(Date.UTC(2026, 9, 15, 4)) })
    await setDatasetExpiry('ds-1', {})
    expect(Object.keys(body(0) as object)).toEqual(['expiry'])
    expect(body(1)).toEqual({})
    expect(calledUrl(0)).toBe(`${API_BASE}/ui/v1/datasets/ds-1/expiry`)
  })

  it('setDatasetCollecting is exported from the api index', () => {
    expect(datasetsModule.setDatasetCollecting).toBe(setDatasetCollecting)
  })

  it('deleteDataset with no override sends no body at all (the store reads none as an empty request)', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }))
    await deleteDataset('ds-1')
    expect(init().body).toBeUndefined()
    expect(headers()['Content-Type']).toBeUndefined()
  })
})

describe('client selection and reads per day (tj-grna9p.89)', () => {
  it('clientQuery: kinds by enum name; ids win over kinds; nothing chosen sends nothing', () => {
    expect(clientQuery({ clientKinds: [ClientKind.SDK, ClientKind.UI] })).toEqual({ client_id: undefined, client_kind: ['SDK', 'UI'] })
    expect(clientQuery({ clientKinds: [ClientKind.UI], clientIds: ['bot-1'] })).toEqual({ client_id: ['bot-1'], client_kind: undefined })
    expect(clientQuery({ clientIds: [], clientKinds: [ClientKind.SDK] }).client_kind).toEqual(['SDK'])
    expect(buildQuery(clientQuery())).toBe('')
  })

  it('getDataset sends the Read By choice as client_kind (a selection, not a filter)', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse(SUMMARY_JSON)))
    await getDataset('ds-1', undefined, { clientKinds: [ClientKind.SDK, ClientKind.UI] })
    expect(calledUrl()).toBe(`${API_BASE}/ui/v1/datasets/ds-1?client_kind=SDK&client_kind=UI`)
    await getDataset('ds-1')
    expect(calledUrl(1)).toBe(`${API_BASE}/ui/v1/datasets/ds-1`)
  })

  it('getReadsPerDay: days, dataset_id and client_kind; never source or expires_before', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        days: [{ day: '2026-10-13', clientId: 'bot-1', clientKind: 'CLIENT_KIND_SDK', reads: '7' }],
        clientKinds: ['CLIENT_KIND_SDK'],
      }),
    )
    const reads = await getReadsPerDay({ days: 30, datasetId: 'ds-1', clientKinds: [ClientKind.SDK] })
    const url = new URL(calledUrl(), 'http://x')
    expect(url.pathname).toBe(`${API_BASE}/ui/v1/stats/usage/reads-per-day`)
    expect([...url.searchParams.entries()]).toEqual([
      ['days', '30'],
      ['dataset_id', 'ds-1'],
      ['client_kind', 'SDK'],
    ])
    expect(reads.days[0]!.reads).toBe(7n)
    expect(reads.days[0]!.clientKind).toBe(ClientKind.SDK)
  })

  it('getReadsPerDay sends source by enum name only when given (tj-grna9p.62)', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({})))
    await getReadsPerDay({ days: 30, clientKinds: [ClientKind.SDK], source: DataSource.ALPACA_API })
    expect(calledUrl()).toBe(`${API_BASE}/ui/v1/stats/usage/reads-per-day?days=30&source=ALPACA_API&client_kind=SDK`)
    await getReadsPerDay({ days: 30, clientKinds: [ClientKind.SDK] })
    expect(calledUrl(1)).toBe(`${API_BASE}/ui/v1/stats/usage/reads-per-day?days=30&client_kind=SDK`)
  })

  it('getReadsPerDay with client ids sends client_id and no client_kind', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}))
    await getReadsPerDay({ clientIds: ['a', 'b'], clientKinds: [ClientKind.UI] })
    expect(calledUrl()).toBe(`${API_BASE}/ui/v1/stats/usage/reads-per-day?client_id=a&client_id=b`)
  })
})

// tj-grna9p.62: the Usage tab's stats routes (server/routers/data_store/ui_stats.py). Presence matters:
// the client selection always goes, stale_days is the threshold, source only when picked, storage
// takes no client parameter (a 422), and no stats route ever gets expires_before or dataset_id.
describe('usage stats routes (tj-grna9p.62)', () => {
  const params = (index = 0) => {
    const url = new URL(calledUrl(index), 'http://x')
    return { path: url.pathname, query: [...url.searchParams.entries()] }
  }
  const headers = (index = 0) => fetchMock.mock.calls[index]![1]!.headers as Record<string, string>
  beforeEach(() => {
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({})))
  })

  it('getUsageSummary: stale_days and the kinds; source only when picked; X-Client-Id operator', async () => {
    await getUsageSummary({ clientKinds: [ClientKind.SDK], staleDays: 90 })
    await getUsageSummary({ clientKinds: [ClientKind.SDK, ClientKind.UI], staleDays: 90, source: DataSource.ALPACA_API })
    await getUsageSummary({ clientIds: ['bot-1'], clientKinds: [ClientKind.SDK], staleDays: 90 })
    expect(params(0)).toEqual({
      path: `${API_BASE}/ui/v1/stats/usage/summary`,
      query: [['stale_days', '90'], ['client_kind', 'SDK']],
    })
    expect(params(1).query).toEqual([['stale_days', '90'], ['source', 'ALPACA_API'], ['client_kind', 'SDK'], ['client_kind', 'UI']])
    expect(params(2).query).toEqual([['stale_days', '90'], ['client_id', 'bot-1']])
    expect(headers()['X-Client-Id']).toBe(OPERATOR_CLIENT_ID)
  })

  it('getLastReadBuckets: the client selection and source only when picked; no stale_days', async () => {
    await getLastReadBuckets({ clientKinds: [ClientKind.UI] })
    await getLastReadBuckets({ clientIds: ['a', 'b'], source: DataSource.ALPACA_API })
    expect(params(0)).toEqual({ path: `${API_BASE}/ui/v1/stats/usage/last-read-buckets`, query: [['client_kind', 'UI']] })
    expect(params(1).query).toEqual([['source', 'ALPACA_API'], ['client_id', 'a'], ['client_id', 'b']])
  })

  it('getStaleDatasets: stale_days, cursor, limit and the selection; source only when picked', async () => {
    await getStaleDatasets({ clientKinds: [ClientKind.SDK], staleDays: 90, limit: 100 })
    await getStaleDatasets({ clientKinds: [ClientKind.SDK], staleDays: 90, cursor: 'k1', limit: 100, source: DataSource.ALPACA_API })
    expect(params(0)).toEqual({
      path: `${API_BASE}/ui/v1/stats/usage/stale`,
      query: [['stale_days', '90'], ['limit', '100'], ['client_kind', 'SDK']],
    })
    expect(params(1).query).toEqual([
      ['stale_days', '90'],
      ['source', 'ALPACA_API'],
      ['cursor', 'k1'],
      ['limit', '100'],
      ['client_kind', 'SDK'],
    ])
  })

  it('getStorageCurrent: source only when picked, and never a client parameter', async () => {
    await getStorageCurrent()
    await getStorageCurrent({ source: DataSource.ALPACA_API })
    expect(params(0)).toEqual({ path: `${API_BASE}/ui/v1/stats/storage/current`, query: [] })
    expect(params(1).query).toEqual([['source', 'ALPACA_API']])
    // Even a caller holding a selection cannot pass one through: the function takes none.
    await getStorageCurrent({ clientKinds: [ClientKind.SDK], clientIds: ['x'] } as Parameters<typeof getStorageCurrent>[0])
    expect(params(2).query).toEqual([])
  })

  it('no stats route ever carries expires_before or dataset_id', async () => {
    await getUsageSummary({ clientKinds: [ClientKind.SDK], staleDays: 90 })
    await getLastReadBuckets({ clientKinds: [ClientKind.SDK] })
    await getStaleDatasets({ clientKinds: [ClientKind.SDK], staleDays: 90 })
    await getStorageCurrent()
    for (let i = 0; i < 4; i += 1) {
      const keys = params(i).query.map(([key]) => key)
      expect(keys).not.toContain('expires_before')
      expect(keys).not.toContain('dataset_id')
    }
  })

  it('decodes the server messages: summary counts, buckets with an open last bucket, a stale page, storage', async () => {
    fetchMock.mockReset()
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ stale: '4', readLast30d: '2', total: '9', staleDays: 90, clientKinds: ['CLIENT_KIND_SDK'] }))
      .mockResolvedValueOnce(jsonResponse({ buckets: [{ minDays: 0, maxDays: 1, datasets: '3' }, { minDays: 91, datasets: '1' }], neverRead: '2' }))
      .mockResolvedValueOnce(jsonResponse({ items: [{ dataset: { id: 'ds-1', assetSymbol: 'VFV' }, estBytes: '1000' }], nextCursor: 'k2', staleDays: 90 }))
      .mockResolvedValueOnce(jsonResponse({ totalBytes: '5000', bySource: [{ source: 'DATA_SOURCE_ALPACA_API', feed: 'FEED_IEX', estBytes: '5000' }] }))
    const summary = await getUsageSummary()
    const buckets = await getLastReadBuckets()
    const stale = await getStaleDatasets()
    const storage = await getStorageCurrent()
    expect([summary.stale, summary.total, summary.staleDays]).toEqual([4n, 9n, 90])
    expect(buckets.buckets[1]!.maxDays).toBeUndefined()
    expect(buckets.neverRead).toBe(2n)
    expect([stale.items[0]!.dataset?.id, stale.items[0]!.estBytes, stale.nextCursor]).toEqual(['ds-1', 1000n, 'k2'])
    expect([storage.totalBytes, storage.bySource[0]!.source]).toEqual([5000n, DataSource.ALPACA_API])
  })
})
