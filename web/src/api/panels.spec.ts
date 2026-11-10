// @vitest-environment node
// The panel routes' typed client (web/src/api/panels.ts, tj-7zjov1.37) against the ruled contract (P24
// tj-7zjov1.36 ROUTE CONTRACT, ADR tj-26jt9w addendum 22): eight reads R1 to R8 (GET) and three writes W1 to
// W3 (POST, 201), every path under /api/store/ui/v1, {source} the proto DataSource NAME, and a write body
// whose optional fields are present exactly when the caller set them (an absent one is never read as zero or
// false; an absent source rate is no limit, ADR tj-2ayia2 D3). Only the network is replaced: global fetch is
// stubbed per test.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DataSource, DataSourceSchema, Granularity } from '@generated/trader_joe/proto/market/v1/enums_pb'
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
    settings: { enabled: true, cadence: 'PANEL_CADENCE_MONTHLY' },
    actor: 'operator',
    reason: 'first',
  },
  counts: { members: '3', collecting: '2', draining: '0', lagging: '1' },
}
const VERSION_JSON = { versionNo: 2, settings: { enabled: false }, actor: 'operator', reason: 'pause' }
const SOURCE_VERSION_JSON = {
  source: 'DATA_SOURCE_ALPACA_API',
  versionNo: 1,
  settings: { rampDatasetsPerHour: 5, requestCapPerHour: 200, corporateActionWindowPreDays: 90, corporateActionWindowPostDays: 90 },
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

  // ADR tj-2ayia2 D3: PanelSourceSettings ramp and cap are optional int32; unset is no limit and decodes as
  // undefined, never 0, while a window day of 0 is a real 0.
  it('R4 a version with unset rates decodes them as undefined (no limit), the window days as sent', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        items: [{ ...SOURCE_VERSION_JSON, settings: { corporateActionWindowPreDays: 0, corporateActionWindowPostDays: 30 } }],
      }),
    )
    const settings = (await getSourceSettingsVersions(DataSource.ALPACA_API, { limit: 1 })).items[0]!.settings!
    expect(settings.rampDatasetsPerHour).toBeUndefined()
    expect(settings.requestCapPerHour).toBeUndefined()
    expect(settings.corporateActionWindowPreDays).toBe(0)
    expect(settings.corporateActionWindowPostDays).toBe(30)
  })

  it('R4 a version with only a cap decodes the ramp as undefined and the cap as sent', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [{ ...SOURCE_VERSION_JSON, settings: { requestCapPerHour: 2 } }] }))
    const settings = (await getSourceSettingsVersions(DataSource.ALPACA_API)).items[0]!.settings!
    expect(settings.rampDatasetsPerHour).toBeUndefined()
    expect(settings.requestCapPerHour).toBe(2)
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
      allowSymbols: ['AAPL'],
      denySymbols: [],
      reason: 'first panel',
      rampDatasetsPerHour: 1,
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
      allowSymbols: ['AAPL'],
      reason: 'first panel',
      rampDatasetsPerHour: 1,
      requestCapPerHour: 200,
    })
    expect(detail.id).toBe(PANEL_ID)
  })

  // ADR tj-2ayia2 D3: an absent rate is no limit. The create form passes an empty field as an undefined
  // value, and the body must carry no key for it.
  it('W1 rates passed as undefined are absent from the body; a window-only create carries only the window keys', async () => {
    fetchMock.mockResolvedValue(jsonResponse(DETAIL_JSON, 201))
    await createPanel({
      name: 'n',
      source: DataSource.ALPACA_API,
      reason: 'r',
      rampDatasetsPerHour: undefined,
      requestCapPerHour: undefined,
      corporateActionWindowPreDays: 0,
      corporateActionWindowPostDays: undefined,
    })
    expect(sentBody()).toEqual({ name: 'n', source: 'DATA_SOURCE_ALPACA_API', reason: 'r', corporateActionWindowPreDays: 0 })
  })

  // ADR tj-rwoasb D3: the Bar Interval is sent as the market.v1 Granularity enum NAME, and read back the same way
  // on the detail and the list item.
  it.each([
    [Granularity.ONE_MINUTE, 'GRANULARITY_ONE_MINUTE'],
    [Granularity.ONE_HOUR, 'GRANULARITY_ONE_HOUR'],
    [Granularity.ONE_DAY, 'GRANULARITY_ONE_DAY'],
  ])('W1 createPanel: granularity %i is sent as %s', async (granularity, wire) => {
    fetchMock.mockResolvedValue(jsonResponse(DETAIL_JSON, 201))
    await createPanel({ name: 'n', source: DataSource.ALPACA_API, granularity, reason: 'r' })
    expect(sentBody()).toEqual({ name: 'n', source: 'DATA_SOURCE_ALPACA_API', granularity: wire, reason: 'r' })
  })

  it('R1 and R2 decode the granularity name on the list item and the detail, a member the form does not offer included', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ...DETAIL_JSON, granularity: 'GRANULARITY_ONE_HOUR' }))
    expect((await getPanel(PANEL_ID)).granularity).toBe(Granularity.ONE_HOUR)
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [{ id: PANEL_ID, name: 'n', source: 'DATA_SOURCE_ALPACA_API', granularity: 'GRANULARITY_ONE_MINUTE' }] }),
    )
    expect((await listPanels()).items[0]!.granularity).toBe(Granularity.ONE_MINUTE)
    fetchMock.mockResolvedValueOnce(jsonResponse({ ...DETAIL_JSON, granularity: 'GRANULARITY_ONE_WEEK' }))
    expect((await getPanel(PANEL_ID)).granularity).toBe(Granularity.ONE_WEEK)
  })

  it('W1 the int32 maximum is sent as a JSON number', async () => {
    fetchMock.mockResolvedValue(jsonResponse(DETAIL_JSON, 201))
    await createPanel({ name: 'n', source: DataSource.ALPACA_API, rampDatasetsPerHour: 2_147_483_647, requestCapPerHour: 2_147_483_647 })
    expect(sentBody()).toMatchObject({ rampDatasetsPerHour: 2_147_483_647, requestCapPerHour: 2_147_483_647 })
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

  it('W2 savePanelSettings: POST /panels/{id}/settings-versions; enabled false is present, no size', async () => {
    fetchMock.mockResolvedValue(jsonResponse(VERSION_JSON, 201))
    const version = await savePanelSettings(PANEL_ID, {
      enabled: false,
      cadence: PanelCadence.MONTHLY,
      allowSymbols: [],
      denySymbols: ['TSLA'],
      reason: 'pause',
    })
    const { url, init } = call()
    expect(init.method).toBe('POST')
    expect(url.pathname).toBe(`/api/store/ui/v1/panels/${PANEL_ID}/settings-versions`)
    expect(sentBody()).toEqual({ enabled: false, cadence: 'PANEL_CADENCE_MONTHLY', denySymbols: ['TSLA'], reason: 'pause' })
    expect(version.versionNo).toBe(2)
  })

  // tj-7zjov1.364.5 (ADR tj-ed4t8m D1, D6): include_otc is a plain proto3 bool (implicit presence), so canonical
  // JSON leaves a false out and the store reads the absence as false (off). A true is sent as includeOtc: true.
  it.each([
    ['W1 createPanel', () => createPanel({ name: 'n', source: DataSource.IB_API, includeOtc: true }), () => createPanel({ name: 'n', source: DataSource.IB_API, includeOtc: false })],
    [
      'W2 savePanelSettings',
      () => savePanelSettings(PANEL_ID, { enabled: true, cadence: PanelCadence.DAILY, includeOtc: true }),
      () => savePanelSettings(PANEL_ID, { enabled: true, cadence: PanelCadence.DAILY, includeOtc: false }),
    ],
  ])('%s: includeOtc true is in the body as true; false is absent from the body', async (_label, sendTrue, sendFalse) => {
    fetchMock.mockImplementation(async () => jsonResponse(DETAIL_JSON, 201))
    await sendTrue()
    expect((sentBody(0) as Record<string, unknown>).includeOtc).toBe(true)
    await sendFalse()
    expect(sentBody(1)).not.toHaveProperty('includeOtc')
  })

  it('a read of settings without includeOtc decodes false; with includeOtc true decodes true', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(DETAIL_JSON))
    expect((await getPanel(PANEL_ID)).currentVersion!.settings!.includeOtc).toBe(false)
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ ...DETAIL_JSON, currentVersion: { ...DETAIL_JSON.currentVersion, settings: { ...DETAIL_JSON.currentVersion.settings, includeOtc: true } } }),
    )
    expect((await getPanel(PANEL_ID)).currentVersion!.settings!.includeOtc).toBe(true)
  })

  // tj-7zjov1.362.4 (ADR tj-46jkey section 2): reason is an implicit-presence proto3 string, so the blank
  // reason the forms pass ('') is the field's default and canonical JSON leaves it out; the store reads an
  // absent reason as none given. Each write still posts.
  it('W1, W2, W3 with a blank reason: each posts, and the body carries no reason key', async () => {
    fetchMock.mockImplementation(async () => jsonResponse(DETAIL_JSON, 201))
    await createPanel({ name: 'n', source: DataSource.IB_API, reason: '' })
    expect(sentBody()).toEqual({ name: 'n', source: 'DATA_SOURCE_IB_API' })
    fetchMock.mockImplementation(async () => jsonResponse(VERSION_JSON, 201))
    await savePanelSettings(PANEL_ID, { enabled: true, cadence: PanelCadence.DAILY, reason: '' })
    expect(sentBody(1)).toEqual({ enabled: true, cadence: 'PANEL_CADENCE_DAILY' })
    fetchMock.mockImplementation(async () => jsonResponse(SOURCE_VERSION_JSON, 201))
    await saveSourceSettings(DataSource.ALPACA_API, { rampDatasetsPerHour: 1, requestCapPerHour: 2, reason: '' })
    expect(sentBody(2)).toEqual({ rampDatasetsPerHour: 1, requestCapPerHour: 2 })
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('W3 saveSourceSettings: POST /panel-sources/{NAME}/settings-versions; every number given is present, a window day of 0 included', async () => {
    fetchMock.mockResolvedValue(jsonResponse(SOURCE_VERSION_JSON, 201))
    const version = await saveSourceSettings(DataSource.ALPACA_API, {
      rampDatasetsPerHour: 1,
      requestCapPerHour: 2,
      corporateActionWindowPreDays: 0,
      corporateActionWindowPostDays: 90,
      reason: 'slow down',
    })
    const { url, init } = call()
    expect(init.method).toBe('POST')
    expect(url.pathname).toBe('/api/store/ui/v1/panel-sources/DATA_SOURCE_ALPACA_API/settings-versions')
    expect(sentBody()).toEqual({
      rampDatasetsPerHour: 1,
      requestCapPerHour: 2,
      corporateActionWindowPreDays: 0,
      corporateActionWindowPostDays: 90,
      reason: 'slow down',
    })
    expect(version.settings?.requestCapPerHour).toBe(200)
  })

  it('W3 rates passed as undefined (no limit) are absent from the body, the window days present', async () => {
    fetchMock.mockResolvedValue(jsonResponse(SOURCE_VERSION_JSON, 201))
    await saveSourceSettings(DataSource.ALPACA_API, {
      rampDatasetsPerHour: undefined,
      requestCapPerHour: undefined,
      corporateActionWindowPreDays: 90,
      corporateActionWindowPostDays: 0,
      reason: '',
    })
    expect(sentBody()).toEqual({ corporateActionWindowPreDays: 90, corporateActionWindowPostDays: 0 })
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
