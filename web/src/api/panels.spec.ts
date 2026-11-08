// @vitest-environment node
// The panel routes' typed client (web/src/api/panels.ts, tj-7zjov1.37) against the ruled contract (P24
// tj-7zjov1.36 ROUTE CONTRACT, ADR tj-26jt9w addendum 22): eight reads R1 to R8 (GET) and three writes W1 to
// W3 (POST, 201), every path under /api/store/ui/v1, {source} the proto DataSource NAME, and a write body
// whose optional fields are present exactly when the caller set them (an absent one is refused 422 by the
// store, never read as zero or false). Only the network is replaced: global fetch is stubbed per test.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DataSource, DataSourceSchema } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { AssetType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { PanelCadence, PanelPassKind } from '@generated/trader_joe/proto/ui/v1/panels_pb'

import {
  ApiError,
  createPanel,
  dataSourceRouteName,
  getAssetListHistory,
  getCorporateActions,
  getPanel,
  getPanelBoundaries,
  getPanelMembers,
  getPanelSettingsVersions,
  getSourceSettingsVersions,
  listPanels,
  savePanelSettings,
  saveSourceSettings,
} from '@/api'

const PANEL_ID = '0f8fad5b-d9cb-469f-a165-70867728950e'
const DATASET_ID = '7c9e6679-7425-40de-944b-e07fc1f90ae7'

const fetchMock = vi.fn<typeof fetch>()

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

function call(index = 0): { url: URL; init: RequestInit } {
  const [input, init] = fetchMock.mock.calls[index]!
  return { url: new URL(String(input), 'http://edge.test'), init: init ?? {} }
}

function sentBody(index = 0): unknown {
  return JSON.parse(String(call(index).init.body))
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

const DETAIL_JSON = {
  id: PANEL_ID,
  name: 'Large caps',
  source: 'DATA_SOURCE_ALPACA_API',
  currentVersion: {
    versionNo: 1,
    settings: { enabled: true, cadence: 'PANEL_CADENCE_MONTHLY', size: 50 },
    actor: 'operator',
    reason: 'first',
  },
  counts: { members: '3', collecting: '2', draining: '0', lagging: '1' },
}
const VERSION_JSON = { versionNo: 2, settings: { enabled: false, size: 50 }, actor: 'operator', reason: 'pause' }
const SOURCE_VERSION_JSON = {
  source: 'DATA_SOURCE_ALPACA_API',
  versionNo: 1,
  settings: { rampDatasetsPerHour: 0, requestCapPerHour: 200, corporateActionWindowPreDays: 90, corporateActionWindowPostDays: 90 },
}

describe('dataSourceRouteName: the {source} path segment is the proto enum NAME', () => {
  it.each([
    [DataSource.ALPACA_API, 'DATA_SOURCE_ALPACA_API'],
    [DataSource.IB_API, 'DATA_SOURCE_IB_API'],
    [DataSource.MANUAL_ENTRY, 'DATA_SOURCE_MANUAL_ENTRY'],
    [DataSource.UNSPECIFIED, 'DATA_SOURCE_UNSPECIFIED'],
  ])('%i is %s', (source, name) => {
    expect(dataSourceRouteName(source)).toBe(name)
  })

  it('every DataSource value round-trips by its full name, never the TS member name or the number', () => {
    for (const value of DataSourceSchema.values) {
      expect(dataSourceRouteName(value.number as DataSource)).toBe(value.name)
      expect(value.name.startsWith('DATA_SOURCE_')).toBe(true)
    }
  })

  it('an unknown number falls back to UNSPECIFIED, which the route refuses 422', () => {
    expect(dataSourceRouteName(99 as DataSource)).toBe('DATA_SOURCE_UNSPECIFIED')
  })
})

describe('the eight reads: GET on their ruled path, query as given, decoded as their message', () => {
  it('R1 listPanels: GET /api/store/ui/v1/panels', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [{ id: PANEL_ID, name: 'A', source: 'DATA_SOURCE_ALPACA_API', enabled: true }] }))
    const list = await listPanels()
    expect(call().url.pathname).toBe('/api/store/ui/v1/panels')
    expect(call().url.search).toBe('')
    expect(call().init.method ?? 'GET').toBe('GET')
    expect(list.items[0]).toMatchObject({ id: PANEL_ID, name: 'A', source: DataSource.ALPACA_API, enabled: true })
  })

  it('R2 getPanel: GET /panels/{id}, counts decoded as int64', async () => {
    fetchMock.mockResolvedValue(jsonResponse(DETAIL_JSON))
    const detail = await getPanel(PANEL_ID)
    expect(call().url.pathname).toBe(`/api/store/ui/v1/panels/${PANEL_ID}`)
    expect(detail.counts).toMatchObject({ members: 3n, collecting: 2n, draining: 0n, lagging: 1n })
    expect(detail.currentVersion?.settings?.cadence).toBe(PanelCadence.MONTHLY)
  })

  it('R2 encodes the id as one path segment', async () => {
    fetchMock.mockResolvedValue(jsonResponse(DETAIL_JSON))
    await getPanel('a/b?c')
    expect(call().url.pathname).toBe('/api/store/ui/v1/panels/a%2Fb%3Fc')
    expect(call().url.search).toBe('')
  })

  it('R3 getPanelSettingsVersions: GET /panels/{id}/settings-versions with cursor and limit', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [VERSION_JSON], nextCursor: 'n' }))
    const page = await getPanelSettingsVersions(PANEL_ID, { cursor: 'c1', limit: 500 })
    expect(call().url.pathname).toBe(`/api/store/ui/v1/panels/${PANEL_ID}/settings-versions`)
    expect(Object.fromEntries(call().url.searchParams)).toEqual({ cursor: 'c1', limit: '500' })
    expect(page.items[0]?.versionNo).toBe(2)
  })

  it('R3 sends no query when none is given (the server defaults apply)', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [] }))
    await getPanelSettingsVersions(PANEL_ID)
    expect(call().url.search).toBe('')
  })

  it('R4 getSourceSettingsVersions: GET /panel-sources/DATA_SOURCE_ALPACA_API/settings-versions', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [SOURCE_VERSION_JSON] }))
    const page = await getSourceSettingsVersions(DataSource.ALPACA_API, { limit: 1 })
    expect(call().url.pathname).toBe('/api/store/ui/v1/panel-sources/DATA_SOURCE_ALPACA_API/settings-versions')
    expect(Object.fromEntries(call().url.searchParams)).toEqual({ limit: '1' })
    expect(page.items[0]?.settings?.requestCapPerHour).toBe(200)
  })

  it('R4 an empty page decodes to no items (the create form test), never an error', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}))
    expect((await getSourceSettingsVersions(DataSource.IB_API)).items).toEqual([])
    expect(call().url.pathname).toBe('/api/store/ui/v1/panel-sources/DATA_SOURCE_IB_API/settings-versions')
  })

  it('R5 getPanelBoundaries: GET /panels/{id}/boundaries?start&end as ISO instants', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ markers: [{ kind: 'PANEL_MARKER_KIND_PANEL_SETTINGS', versionNo: 2 }] }))
    const start = new Date('2026-08-01T00:00:00Z')
    const end = new Date('2026-11-01T00:00:00Z')
    const boundaries = await getPanelBoundaries(PANEL_ID, { start, end })
    expect(call().url.pathname).toBe(`/api/store/ui/v1/panels/${PANEL_ID}/boundaries`)
    expect(Object.fromEntries(call().url.searchParams)).toEqual({ start: start.toISOString(), end: end.toISOString() })
    expect(boundaries.markers[0]?.versionNo).toBe(2)
  })

  it('R6 getPanelMembers: GET /panels/{id}/members with cursor and limit', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ items: [{ vendorAssetId: 'v1', symbol: 'AAPL', health: 'DATASET_HEALTH_STATUS_LATE' }], nextCursor: 'm2' }),
    )
    const page = await getPanelMembers(PANEL_ID, { cursor: 'm1', limit: 100 })
    expect(call().url.pathname).toBe(`/api/store/ui/v1/panels/${PANEL_ID}/members`)
    expect(Object.fromEntries(call().url.searchParams)).toEqual({ cursor: 'm1', limit: '100' })
    expect(page.nextCursor).toBe('m2')
    expect(page.items[0]?.symbol).toBe('AAPL')
  })

  it('R7 getAssetListHistory: GET /panels/{id}/asset-list-history?start&end', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ days: [{ day: '2026-10-30', state: 'ASSET_LIST_DAY_STATE_SAVED', changedCount: 4 }] }))
    const start = new Date('2026-08-03T00:00:00Z')
    const end = new Date('2026-11-02T00:00:00Z')
    const history = await getAssetListHistory(PANEL_ID, { start, end })
    expect(call().url.pathname).toBe(`/api/store/ui/v1/panels/${PANEL_ID}/asset-list-history`)
    expect(Object.fromEntries(call().url.searchParams)).toEqual({ start: start.toISOString(), end: end.toISOString() })
    expect(history.days[0]).toMatchObject({ day: '2026-10-30', changedCount: 4 })
  })

  it('R8 getCorporateActions: GET /datasets/{id}/corporate-actions with cursor', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [{ vendorActionId: 'a1', actionType: 'forward_split' }], nextCursor: '' }))
    const page = await getCorporateActions(DATASET_ID, { cursor: 'k' })
    expect(call().url.pathname).toBe(`/api/store/ui/v1/datasets/${DATASET_ID}/corporate-actions`)
    expect(Object.fromEntries(call().url.searchParams)).toEqual({ cursor: 'k' })
    expect(page.items[0]?.actionType).toBe('forward_split')
  })

  it('a read carries no body and no instance secret, and passes its signal', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [] }))
    const controller = new AbortController()
    await getPanelMembers(PANEL_ID, { signal: controller.signal })
    const { init } = call()
    expect(init.body).toBeUndefined()
    expect(JSON.stringify(init.headers ?? {})).not.toMatch(/secret/i)
    expect(init.signal).toBe(controller.signal)
  })
})

describe('the three writes: POST on their ruled path with a canonical JSON body', () => {
  it('W1 createPanel: POST /panels; optional fields present only when set; source by its proto name', async () => {
    fetchMock.mockResolvedValue(jsonResponse(DETAIL_JSON, 201))
    const detail = await createPanel({
      name: 'Large caps',
      source: DataSource.ALPACA_API,
      enabled: false,
      cadence: PanelCadence.UNSPECIFIED,
      size: 0,
      allowSymbols: ['AAPL'],
      denySymbols: [],
      reason: 'first panel',
      rampDatasetsPerHour: 0,
      requestCapPerHour: 200,
    })
    const { url, init } = call()
    expect(init.method).toBe('POST')
    expect(url.pathname).toBe('/api/store/ui/v1/panels')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    expect(sentBody()).toEqual({
      name: 'Large caps',
      source: 'DATA_SOURCE_ALPACA_API',
      enabled: false,
      size: 0,
      allowSymbols: ['AAPL'],
      reason: 'first panel',
      rampDatasetsPerHour: 0,
      requestCapPerHour: 200,
    })
    expect(detail.id).toBe(PANEL_ID)
  })

  it('W1 an UNSPECIFIED cadence and unset filters and window days are absent from the body', async () => {
    fetchMock.mockResolvedValue(jsonResponse(DETAIL_JSON, 201))
    await createPanel({ name: 'n', source: DataSource.IB_API, reason: 'r' })
    expect(sentBody()).toEqual({ name: 'n', source: 'DATA_SOURCE_IB_API', reason: 'r' })
  })

  it('W1 a picked cadence and the filters are sent by their enum names', async () => {
    fetchMock.mockResolvedValue(jsonResponse(DETAIL_JSON, 201))
    await createPanel({
      name: 'n',
      source: DataSource.ALPACA_API,
      cadence: PanelCadence.WEEKLY,
      assetClass: AssetType.STOCK,
      exchange: 'NYSE',
      active: true,
      tradable: false,
      reason: 'r',
      corporateActionWindowPreDays: 30,
    })
    expect(sentBody()).toMatchObject({
      cadence: 'PANEL_CADENCE_WEEKLY',
      assetClass: 'ASSET_TYPE_STOCK',
      exchange: 'NYSE',
      active: true,
      tradable: false,
      corporateActionWindowPreDays: 30,
    })
    expect(sentBody()).not.toHaveProperty('corporateActionWindowPostDays')
  })

  it('W2 savePanelSettings: POST /panels/{id}/settings-versions; enabled false and size 0 are present', async () => {
    fetchMock.mockResolvedValue(jsonResponse(VERSION_JSON, 201))
    const version = await savePanelSettings(PANEL_ID, {
      enabled: false,
      cadence: PanelCadence.MONTHLY,
      size: 0,
      allowSymbols: [],
      denySymbols: ['TSLA'],
      reason: 'pause',
    })
    const { url, init } = call()
    expect(init.method).toBe('POST')
    expect(url.pathname).toBe(`/api/store/ui/v1/panels/${PANEL_ID}/settings-versions`)
    expect(sentBody()).toEqual({ enabled: false, cadence: 'PANEL_CADENCE_MONTHLY', size: 0, denySymbols: ['TSLA'], reason: 'pause' })
    expect(version.versionNo).toBe(2)
  })

  it('W3 saveSourceSettings: POST /panel-sources/{NAME}/settings-versions; every number present, 0 included', async () => {
    fetchMock.mockResolvedValue(jsonResponse(SOURCE_VERSION_JSON, 201))
    const version = await saveSourceSettings(DataSource.ALPACA_API, {
      rampDatasetsPerHour: 0,
      requestCapPerHour: 200,
      corporateActionWindowPreDays: 0,
      corporateActionWindowPostDays: 90,
      reason: 'pause creation',
    })
    const { url, init } = call()
    expect(init.method).toBe('POST')
    expect(url.pathname).toBe('/api/store/ui/v1/panel-sources/DATA_SOURCE_ALPACA_API/settings-versions')
    expect(sentBody()).toEqual({
      rampDatasetsPerHour: 0,
      requestCapPerHour: 200,
      corporateActionWindowPreDays: 0,
      corporateActionWindowPostDays: 90,
      reason: 'pause creation',
    })
    expect(version.settings?.requestCapPerHour).toBe(200)
  })

  it('a write never sends PUT or PATCH and never carries the instance secret', async () => {
    fetchMock.mockImplementation(async () => jsonResponse(DETAIL_JSON, 201))
    await createPanel({ name: 'n', source: DataSource.ALPACA_API, reason: 'r' })
    await savePanelSettings(PANEL_ID, { reason: 'r' })
    await saveSourceSettings(DataSource.ALPACA_API, { reason: 'r' })
    expect(fetchMock.mock.calls.map(([, init]) => init?.method)).toEqual(['POST', 'POST', 'POST'])
    for (const [, init] of fetchMock.mock.calls) expect(JSON.stringify(init?.headers ?? {})).not.toMatch(/secret/i)
  })

  it('a 201 body decodes with the effective pass, unknown fields ignored', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ ...VERSION_JSON, effectivePass: { passId: 'p', kind: 'PANEL_PASS_KIND_MEMBERSHIP' }, addedLater: 1 }, 201),
    )
    const version = await savePanelSettings(PANEL_ID, { reason: 'r' })
    expect(version.effectivePass?.kind).toBe(PanelPassKind.MEMBERSHIP)
  })

  it('a 422 refusal is an ApiError with status 422 (the page words it; the server text is not shown)', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ error: { reason: 'INVALID_REQUEST', message: 'name taken' } }, 422))
    const error = await rejection(createPanel({ name: 'n', source: DataSource.ALPACA_API, reason: 'r' }))
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(422)
  })

  it('a 2xx body that is not the message is MALFORMED_RESPONSE', async () => {
    fetchMock.mockResolvedValue(new Response('not json', { status: 201 }))
    const error = await rejection(saveSourceSettings(DataSource.ALPACA_API, { reason: 'r' }))
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).reason).toBe('MALFORMED_RESPONSE')
  })
})
