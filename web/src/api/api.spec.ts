// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ESLint } from 'eslint'

import { create, toJson } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import { DataSource, Granularity, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import {
  DatasetState,
  ExpiryType,
  FreshnessStatus,
  IngestRunPageSchema,
  IngestRunState,
  RunTrigger,
} from '@generated/trader_joe/proto/ui/v1/data_pb'
import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'
import { CoverageStripSchema } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import {
  API_BASE,
  ApiError,
  buildQuery,
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
  it('GETs same-origin under /api/store with an Accept header and the caller signal', async () => {
    fetchMock.mockResolvedValue(jsonResponse(CONFIG_JSON))
    const controller = new AbortController()
    await getUiConfig(controller.signal)
    expect(API_BASE).toBe('/api/store')
    expect(calledUrl()).toBe('/api/store/ui/v1/config')
    const init = fetchMock.mock.calls[0]![1]!
    expect(init.method).toBe('GET')
    expect(init.headers).toEqual({ Accept: 'application/json' })
    expect(init.signal).toBe(controller.signal)
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
