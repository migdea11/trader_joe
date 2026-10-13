import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import { defineComponent, h, onMounted } from 'vue'
import { create, type MessageInitShape } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'
import type { ColDef, IDatasource, IGetRowsParams } from 'ag-grid-community'

import { DataSource, Feed, Granularity, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import {
  DatasetSummarySchema,
  FreshnessStatus,
  IngestRunState,
  type DatasetSummary,
} from '@generated/trader_joe/proto/ui/v1/data_pb'
import { DatasetChange } from '@generated/trader_joe/proto/ui/v1/events_pb'
import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'
import { CoverageStripSchema } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { fakeEvents } from '@/__tests__/fakeEvents'
import { ApiError } from '@/api'
import { COALESCE_MS } from '@/catalog/useCatalogLiveRefresh'
import CoverageCell from '@/components/catalog/CoverageCell.vue'
import StatusCell from '@/components/catalog/StatusCell.vue'
import SymbolCell from '@/components/catalog/SymbolCell.vue'
import router from '@/router'
import AppShell from '@/shell/AppShell.vue'
import { useDatasetCoverageStore } from '@/stores/datasetCoverage'
import { useSettingsStore } from '@/stores/settings'
import { COVERAGE_UNIT, titleCase } from '@/theme/copy'
import { colors } from '@/theme/tokens'

// tj-grna9p.32 phase 1 Datasets, mounted in the real shell under the real router.
//
// WHAT THE MOCKS HIDE. The API is mocked at the typed client (@/api). AG Grid is replaced by a stub
// (jsdom has no layout, so the real grid never asks for a row): the stub records the props the
// screen passes (column definitions, row model options) and hands the screen a fake GridApi whose
// setGridOption('datasource', ...) is captured, and the spec then drives the datasource the way
// the grid would (getRows by block). Hidden: AG Grid's own block scheduling and scrolling, its
// rendering of cells (the column value getters and the Status cell are exercised directly), and
// AG Grid destroying the datasource it replaces.
//
// Phase 2 (tj-grna9p.77): the fake GridApi also counts refreshInfiniteCache, which is how a live event
// reloads the rows in place (the same datasource, invalidated); the events socket is the fixture in
// src/__tests__/fakeEvents.ts.
const grid = vi.hoisted(() => ({
  props: null as Record<string, unknown> | null,
  datasources: [] as IDatasource[],
  refreshes: 0,
}))

vi.mock('ag-grid-vue3', () => ({
  AgGridVue: defineComponent({
    name: 'AgGridVueStub',
    inheritAttrs: false,
    props: {
      columnDefs: { type: Array, default: undefined },
      rowModelType: { type: String, default: undefined },
      cacheBlockSize: { type: Number, default: undefined },
      cacheOverflowSize: { type: Number, default: undefined },
      maxConcurrentDatasourceRequests: { type: Number, default: undefined },
      theme: { type: Object, default: undefined },
      defaultColDef: { type: Object, default: undefined },
      getRowId: { type: Function, default: undefined },
    },
    emits: ['grid-ready', 'row-clicked'],
    setup(props, { emit }) {
      grid.props = props as Record<string, unknown>
      onMounted(() =>
        emit('grid-ready', {
          api: {
            setGridOption: (key: string, value: unknown) => {
              if (key === 'datasource') grid.datasources.push(value as IDatasource)
            },
            refreshInfiniteCache: () => {
              grid.refreshes += 1
            },
          },
        }),
      )
      return () => h('div', { class: 'ag-grid-stub' })
    },
  }),
}))

const mocks = vi.hoisted(() => ({
  getUiConfig: vi.fn(),
  getDatasetFacets: vi.fn(),
  listDatasets: vi.fn(),
  getDataset: vi.fn(),
  getDatasetBars: vi.fn(),
  getDatasetCoverage: vi.fn(),
}))
// The shell opens the shared events socket on mount (tj-grna9p.76): replaced so no real WebSocket opens,
// and driven below to deliver dataset and run events and a reconnect (tj-grna9p.77).
vi.mock('@/api/events', async (importOriginal) =>
  (await import('@/__tests__/fakeEvents')).fakeEventsModule(await importOriginal<object>()),
)
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...mocks,
}))

const EXPIRY = Date.UTC(2027, 0, 1, 3)

function row(id: string, symbol: string, extra: MessageInitShape<typeof DatasetSummarySchema> = {}): DatasetSummary {
  return create(DatasetSummarySchema, {
    id,
    assetSymbol: symbol,
    source: DataSource.ALPACA_API,
    feed: Feed.IEX,
    granularity: Granularity.ONE_DAY,
    updateType: UpdateType.DAILY,
    freshness: { status: FreshnessStatus.FRESH },
    ...extra,
  } as MessageInitShape<typeof DatasetSummarySchema>)
}

const FACETS = {
  all: 12n,
  needsAttention: 3n,
  running: 2n,
  sources: [
    { source: DataSource.ALPACA_API, count: 12n },
    { source: DataSource.IB_API, count: 0n },
  ],
  updateTypes: [
    { updateType: UpdateType.DAILY, count: 8n },
    { updateType: UpdateType.STATIC, count: 4n },
  ],
  statuses: [
    { status: FreshnessStatus.FRESH, count: 5n },
    { status: FreshnessStatus.COMPLETE, count: 2n },
    { status: FreshnessStatus.LATE, count: 1n },
    { status: FreshnessStatus.OVERDUE, count: 1n },
    { status: FreshnessStatus.GAPS, count: 1n },
    { status: FreshnessStatus.UNSPECIFIED, count: 2n },
  ],
}

const mounted: VueWrapper[] = []

async function screenAt(path: string): Promise<VueWrapper> {
  await router.push(path)
  await router.isReady()
  const shell = mount(AppShell, { global: { plugins: [createPinia(), router, PrimeVue] }, attachTo: document.body })
  mounted.push(shell)
  await flushPromises()
  return shell
}

function getRows(datasource: IDatasource, startRow: number) {
  return new Promise<{ rows?: DatasetSummary[]; lastRow?: number; failed?: true }>((resolve) => {
    datasource.getRows({
      startRow,
      endRow: startRow + 100,
      successCallback: (rows: DatasetSummary[], lastRow?: number) => resolve({ rows, lastRow }),
      failCallback: () => resolve({ failed: true }),
    } as unknown as IGetRowsParams)
  })
}

const latest = (): IDatasource => grid.datasources[grid.datasources.length - 1]!

function listArgs(call = -1): Record<string, unknown> {
  return mocks.listDatasets.mock.calls.at(call)![0] as Record<string, unknown>
}

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  setActivePinia(createPinia())
  grid.props = null
  grid.datasources.length = 0
  grid.refreshes = 0
  fakeEvents.reset()
  mocks.getDatasetCoverage.mockResolvedValue(create(CoverageStripSchema, {}))
  mocks.getUiConfig.mockResolvedValue({ allowedGroups: [AccountGroup.SIMULATION], deploymentLabel: '', serverVersion: '' })
  mocks.getDatasetFacets.mockResolvedValue(FACETS)
  mocks.listDatasets.mockResolvedValue({ items: [row('a', 'AAA'), row('b', 'BBB')], nextCursor: '' })
})

afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount())
})

describe('grid wiring', () => {
  it('uses the infinite row model with 100-row blocks, one request at a time', async () => {
    await screenAt('/data/datasets')
    expect(grid.props).toMatchObject({
      rowModelType: 'infinite',
      cacheBlockSize: 100,
      cacheOverflowSize: 1,
      maxConcurrentDatasourceRequests: 1,
    })
    expect(grid.datasources).toHaveLength(1)
  })

  it('AG Grid never loads a Google font: no theme parameter names one and the option is not set', async () => {
    // The bundle carries AG Grid's Google Fonts loader (fonts.googleapis.com); it only runs for a theme
    // parameter of the form { googleFont } AND loadThemeGoogleFonts true. themeQuartz's own default
    // font is { googleFont: 'IBM Plex Sans' }, so the override to the token stack is what keeps the
    // loader idle. _getModeParams is AG Grid's internal read of the merged parameters (ag-stack).
    const { gridTheme } = await import('@/catalog/agGrid')
    const modes = (gridTheme as unknown as { _getModeParams(): Record<string, Record<string, unknown>> })._getModeParams()
    const values = Object.values(modes).flatMap((mode) => Object.values(mode))
    const googleFonts = values.flat().filter((v) => typeof v === 'object' && v !== null && 'googleFont' in v)
    expect(googleFonts).toEqual([])
    await screenAt('/data/datasets')
    const stub = mounted[0]!.findComponent({ name: 'AgGridVueStub' })
    expect(Object.keys(stub.vm.$attrs).some((k) => /googlefont/i.test(k))).toBe(false)
  })

  it('page one is fetched with no cursor, limit 100 and the URL filters', async () => {
    await screenAt('/data/datasets?status=failed&source=alpaca_api&update=static')
    await getRows(latest(), 0)
    expect(listArgs()).toMatchObject({
      status: 'failed',
      source: DataSource.ALPACA_API,
      updateType: UpdateType.STATIC,
      cursor: undefined,
      limit: 100,
    })
    expect(listArgs().needsAttention).toBeUndefined()
  })

  it('Needs Attention sends needsAttention true to the list and the facets', async () => {
    await screenAt('/data/datasets?view=needs-attention')
    await getRows(latest(), 0)
    expect(listArgs().needsAttention).toBe(true)
    const facetFilters = mocks.getDatasetFacets.mock.calls.map(([f]) => (f as Record<string, unknown>).needsAttention)
    expect(facetFilters).toContain(true)
  })

  it('each cursor is fetched once across pages', async () => {
    mocks.listDatasets
      .mockResolvedValueOnce({ items: Array.from({ length: 100 }, (_, i) => row(`r${i}`, `S${i}`)), nextCursor: 'k1' })
      .mockResolvedValueOnce({ items: [row('z', 'ZZZ')], nextCursor: '' })
    await screenAt('/data/datasets')
    const first = await getRows(latest(), 0)
    expect(first.lastRow).toBeUndefined()
    const second = await getRows(latest(), 100)
    expect(second.lastRow).toBe(101)
    await getRows(latest(), 100)
    expect(mocks.listDatasets.mock.calls.map(([p]) => (p as Record<string, unknown>).cursor)).toEqual([undefined, 'k1'])
  })
})

describe('filters', () => {
  it('a sidebar filter writes the URL, refetches the counts and starts the cursor over', async () => {
    mocks.listDatasets.mockResolvedValue({ items: [row('a', 'AAA')], nextCursor: 'k1' })
    const shell = await screenAt('/data/datasets?group=simulation')
    await getRows(latest(), 0)
    const before = grid.datasources.length
    const facetCalls = mocks.getDatasetFacets.mock.calls.length

    await shell.find('[data-status="failed"] input').trigger('change')
    await flushPromises()

    expect(router.currentRoute.value.query).toEqual({ group: 'simulation', status: 'failed' })
    expect(grid.datasources.length).toBe(before + 1)
    expect(mocks.getDatasetFacets.mock.calls.length).toBeGreaterThan(facetCalls)
    expect(mocks.getDatasetFacets.mock.calls.at(-1)![0]).toMatchObject({ status: 'failed' })
    await getRows(latest(), 0)
    expect(listArgs()).toMatchObject({ status: 'failed', cursor: undefined })
  })

  it('ticking the chosen option again clears the group, and Clear keeps the view', async () => {
    const shell = await screenAt('/data/datasets?view=needs-attention&status=late&update=daily')
    await shell.find('[data-status="late"] input').trigger('change')
    await flushPromises()
    expect(router.currentRoute.value.query).toMatchObject({ view: 'needs-attention', update: 'daily' })
    expect(router.currentRoute.value.query.status).toBeUndefined()
    await shell.find('.side-clear').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query.update).toBeUndefined()
    expect(router.currentRoute.value.query.view).toBe('needs-attention')
  })

  it('filters survive a reload: a fresh mount at the URL shows them checked and sends them', async () => {
    const shell = await screenAt('/data/datasets?status=retired&update=static')
    expect((shell.find('[data-status="retired"] input').element as HTMLInputElement).checked).toBe(true)
    expect((shell.find('[data-update="STATIC"] input').element as HTMLInputElement).checked).toBe(true)
    await getRows(latest(), 0)
    expect(listArgs()).toMatchObject({ status: 'retired', updateType: UpdateType.STATIC })
  })

  // tj-grna9p.117 (owner 2026-10-13): the group reads Status, not Collection. 'Collection' came from a
  // two-badge idea (Collection and Usage) and Usage does not exist, so Status is the plain word. The
  // data-collection hook is an identifier and is kept.
  it('the Status group lists Running first, its count is DatasetFacets.running', async () => {
    const shell = await screenAt('/data/datasets')
    const group = shell.find('[data-collection="running"]').element.closest('fieldset')!
    expect(group.querySelector('legend')!.textContent!.trim()).toBe(titleCase('Status'))
    // Running is the group's first option, before the freshness words.
    expect(group.querySelector('label')!.getAttribute('data-collection')).toBe('running')
    expect(shell.find('[data-collection="running"]').text()).toContain('Running')
    expect(shell.find('[data-collection="running"] .side-count').text()).toBe('2')
  })

  it('the Status group holds Running as its own toggle beside the freshness words, and ticking it writes ?running=true', async () => {
    const shell = await screenAt('/data/datasets')
    const legends = shell.findAll('fieldset legend').map((l) => l.text())
    expect(legends).toContain(titleCase('Status'))
    expect(legends).not.toContain('Collection')
    const group = shell.findAll('fieldset').find((f) => f.find('legend').text() === titleCase('Status'))!
    // Running is inside the Status group, not a group of its own or outside it.
    expect(group.find('[data-collection="running"]').exists()).toBe(true)
    expect(shell.findAll('[data-collection="running"]')).toHaveLength(1)
    expect(group.findAll('label').map((l) => l.attributes('data-collection') ?? l.attributes('data-status'))).toEqual([
      'running',
      'healthy',
      'late',
      'failed',
      'retired',
    ])
    // Its own checkbox: ticking it sets running, not status.
    await group.find('[data-collection="running"] input').trigger('change')
    await flushPromises()
    // The router is shared across specs and may carry the trading group; the filter keys are exact.
    expect(router.currentRoute.value.query.running).toBe('true')
    expect(router.currentRoute.value.query.status).toBeUndefined()
    expect(Object.keys(router.currentRoute.value.query).filter((k) => k !== 'group')).toEqual(['running'])
  })

  it('ticking Running writes ?running=true, keeps the status, and sends running to the list and the counts', async () => {
    const shell = await screenAt('/data/datasets?status=late')
    const before = grid.datasources.length
    await shell.find('[data-collection="running"] input').trigger('change')
    await flushPromises()
    expect(router.currentRoute.value.query).toMatchObject({ status: 'late', running: 'true' })
    expect(grid.datasources.length).toBe(before + 1)
    expect(mocks.getDatasetFacets.mock.calls.at(-1)![0]).toMatchObject({ status: 'late', running: true })
    await getRows(latest(), 0)
    expect(listArgs()).toMatchObject({ status: 'late', running: true, cursor: undefined })
    // Ticking it again clears it.
    await shell.find('[data-collection="running"] input').trigger('change')
    await flushPromises()
    expect(router.currentRoute.value.query.running).toBeUndefined()
    expect(router.currentRoute.value.query.status).toBe('late')
  })

  it('a URL with running=true shows Running checked and sends it; Clear removes it', async () => {
    const shell = await screenAt('/data/datasets?running=true')
    expect((shell.find('[data-collection="running"] input').element as HTMLInputElement).checked).toBe(true)
    await getRows(latest(), 0)
    expect(listArgs().running).toBe(true)
    await shell.find('.side-clear').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query.running).toBeUndefined()
  })

  it('a URL naming update=stream is ignored, and Stream is shown disabled', async () => {
    const shell = await screenAt('/data/datasets?update=stream')
    await getRows(latest(), 0)
    expect(listArgs().updateType).toBeUndefined()
    const stream = shell.find('[data-update="STREAM"] input')
    expect((stream.element as HTMLInputElement).disabled).toBe(true)
    expect(shell.find('[data-update="STREAM"]').text()).toContain('Stream (later)')
  })
})

describe('counts', () => {
  it('views and filters show facet counts, zero counts included, unset health in no status', async () => {
    const shell = await screenAt('/data/datasets')
    const count = (selector: string) => shell.find(`${selector} .side-count`).text()
    expect(count('[data-view="all"]')).toBe('12')
    expect(count('[data-view="needs-attention"]')).toBe('3')
    expect(count('[data-status="healthy"]')).toBe('7')
    expect(count('[data-status="late"]')).toBe('1')
    expect(count('[data-status="failed"]')).toBe('2')
    expect(count('[data-status="retired"]')).toBe('0')
    expect(count('[data-source="IB_API"]')).toBe('0')
    expect(count('[data-update="STREAM"]')).toBe('0')
  })

  it('the tiles count the whole catalog and do not follow a filter', async () => {
    mocks.getDatasetFacets.mockImplementation(async (filters: Record<string, unknown>) =>
      filters.status === undefined ? FACETS : { ...FACETS, all: 1n, statuses: [], updateTypes: [] },
    )
    const shell = await screenAt('/data/datasets?status=late')
    expect(shell.find('[data-tile="datasets"]').text()).toContain('12')
    expect(shell.find('[data-tile="datasets"]').text()).toContain('8 daily · 4 bulk')
    expect(shell.find('[data-tile="failed"] .tile__value').text()).toBe('2')
    expect(shell.find('[data-tile="late"] .tile__value').text()).toBe('1')
    expect(shell.find('[data-view="all"] .side-count').text()).toBe('1')
  })

  it('a failed counts request shows on the tiles without erasing them', async () => {
    mocks.getDatasetFacets.mockRejectedValue(new ApiError({ status: 500, errorId: 'f-1' }))
    const shell = await screenAt('/data/datasets')
    expect(shell.find('[data-tile="failed"] .tile__note').text()).toBe('Could not load counts')
    expect(shell.find('footer').text()).toContain('error id f-1')
  })
})

describe('Refresh and Show Request', () => {
  it('Refresh makes a new datasource whose first fetch has no cursor, and reloads the counts', async () => {
    mocks.listDatasets.mockResolvedValue({ items: Array.from({ length: 100 }, (_, i) => row(`r${i}`, `S${i}`)), nextCursor: 'k1' })
    const shell = await screenAt('/data/datasets')
    await getRows(latest(), 0)
    await getRows(latest(), 100)
    const facetCalls = mocks.getDatasetFacets.mock.calls.length
    const refresh = shell.findAll('button').find((b) => b.text() === 'Refresh')!
    await refresh.trigger('click')
    await flushPromises()
    expect(grid.datasources).toHaveLength(2)
    expect(mocks.getDatasetFacets.mock.calls.length).toBe(facetCalls + 1)
    mocks.listDatasets.mockClear()
    await getRows(latest(), 0)
    expect(listArgs().cursor).toBeUndefined()
  })

  it('Show Request shows the first-page call under the filters', async () => {
    const shell = await screenAt('/data/datasets?view=needs-attention&status=failed')
    const button = shell.findAll('button').find((b) => b.text() === 'Show Request')!
    await button.trigger('click')
    expect(shell.find('[data-testid="catalog-request"]').text()).toBe(
      'GET /api/store/ui/v1/datasets?status=failed&needs_attention=true&limit=100',
    )
    expect(button.attributes('aria-pressed')).toBe('true')
  })
})

describe('rows', () => {
  // tj-grna9p.109 (owner feedback 2026-10-12) reverses tj-grna9p.77 item 4: the data is the default
  // destination, so a plain row click opens the Viewer at /data/datasets/:id/viewer. The Dataset
  // detail stays at /data/datasets/:id, reached by the details icon instead (in the Coverage cell
  // since tj-grna9p.114).
  it('a plain row click opens the Viewer; a ctrl, meta, shift or alt click is left to the browser', async () => {
    await screenAt('/data/datasets')
    const gridStub = mounted[0]!.findComponent({ name: 'AgGridVueStub' })
    for (const modifier of ['metaKey', 'ctrlKey', 'shiftKey', 'altKey']) {
      gridStub.vm.$emit('row-clicked', { data: row('ds 1', 'AAA'), event: new MouseEvent('click', { [modifier]: true }) })
      await flushPromises()
      expect(router.currentRoute.value.name, modifier).toBe('data-datasets')
    }
    gridStub.vm.$emit('row-clicked', { data: row('ds 1', 'AAA'), event: new MouseEvent('click') })
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('data-dataset-viewer')
    expect(router.currentRoute.value.path).toBe('/data/datasets/ds 1/viewer')
    expect(router.currentRoute.value.params.id).toBe('ds 1')
    expect(router.resolve({ name: 'data-dataset-detail', params: { id: 'ds 1' } }).href).toBe('/data/datasets/ds%201')
  })

  // A real grid would never deliver the icon's click to the row (CoverageCell stops it), but the row
  // guards as well: a click whose target sits inside the details icon opens nothing from the row.
  it('a row click that started on the details icon does not open the Viewer', async () => {
    await screenAt('/data/datasets')
    const gridStub = mounted[0]!.findComponent({ name: 'AgGridVueStub' })
    const icon = document.createElement('a')
    icon.className = 'details-link'
    const glyph = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
    icon.append(glyph)
    document.body.append(icon)
    const event = new MouseEvent('click', { bubbles: true })
    glyph.dispatchEvent(event)
    icon.remove()
    gridStub.vm.$emit('row-clicked', { data: row('ds 1', 'AAA'), event })
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('data-datasets')
  })

  // Phase 2 (tj-grna9p.77 items 1 and 2): the status column shows Running over the freshness word
  // and a Coverage column sits after it; tj-grna9p.117 (owner 2026-10-13) named its header Status
  // (it was Collection; colId 'collection' is an identifier and is kept). tj-grna9p.109: the Symbol
  // link follows the row to the Viewer. tj-grna9p.114: the details icon moved into the Coverage cell,
  // so the Coverage column is handed the detail URL and the Symbol column only the Viewer's.
  it('columns: symbol links the Viewer and the Coverage cell\'s icon the Dataset detail, source and feed, granularity, update, Status, Coverage, and Expires date or dash', async () => {
    useSettingsStore().setTimeZoneOverride('America/Toronto')
    await screenAt('/data/datasets')
    const cols = grid.props!.columnDefs as ColDef<DatasetSummary>[]
    expect(cols.map((c) => c.headerName)).toEqual([
      'Symbol',
      'Source · Feed',
      'Gran.',
      'Update',
      titleCase('Status'),
      // tj-grna9p.112: the cell shows only x/y, so the unit moved into the header. Built from the
      // same constant and titleCase the screen uses, so renaming the unit word moves both together.
      `${titleCase('Coverage')} (${titleCase(COVERAGE_UNIT)})`,
      'Expires',
    ])
    // The status column (colId kept as 'collection') is headed Status; no header anywhere reads Collection.
    expect(cols.find((c) => c.colId === 'collection')!.headerName).toBe(titleCase('Status'))
    expect(cols.filter((c) => /\bcollection\b/i.test(c.headerName ?? ''))).toEqual([])
    expect(cols.find((c) => c.colId === 'collection')!.cellRenderer).toBe(StatusCell)
    expect(cols.find((c) => c.colId === 'coverage')!.cellRenderer).toBe(CoverageCell)
    const value = (colId: string, data: DatasetSummary) =>
      (cols.find((c) => c.colId === colId)!.valueGetter as (p: { data: DatasetSummary }) => unknown)({ data })
    const withExpiry = row('x', 'X', { expiry: timestampFromMs(EXPIRY), updateType: UpdateType.STATIC })
    expect(value('source', withExpiry)).toBe('Alpaca · IEX')
    expect(value('granularity', withExpiry)).toBe('1d')
    expect(value('update', withExpiry)).toBe('Bulk')
    // 2027-01-01T03:00Z is still 2026/12/31 in Toronto.
    expect(value('expires', withExpiry)).toBe('2026/12/31')
    expect(value('expires', row('y', 'Y'))).toBe('—')
    const symbol = cols.find((c) => c.colId === 'symbol')!
    expect(symbol.cellRenderer).toBe(SymbolCell)
    const params = symbol.cellRendererParams as { href: (id: string) => string }
    expect(Object.keys(params)).toEqual(['href'])
    expect(params.href('a/b')).toBe('/data/datasets/a%2Fb/viewer')
    const coverageParams = cols.find((c) => c.colId === 'coverage')!.cellRendererParams as { detailHref: (id: string) => string }
    expect(Object.keys(coverageParams)).toEqual(['detailHref'])
    expect(coverageParams.detailHref('a/b')).toBe('/data/datasets/a%2Fb')
  })

  it('the Status cell reads Running while the last run is in flight, whatever the freshness', () => {
    const cell = (extra: MessageInitShape<typeof DatasetSummarySchema>) =>
      mount(StatusCell, { props: { params: { data: row('r', 'R', extra) } as never } }).text()
    expect(cell({ freshness: { status: FreshnessStatus.LATE }, lastRun: { state: IngestRunState.RUNNING } })).toBe('Running')
    expect(cell({ freshness: { status: FreshnessStatus.GAPS }, lastRun: { state: IngestRunState.RUNNING } })).toBe('Running')
    expect(cell({ freshness: { status: FreshnessStatus.LATE }, lastRun: { state: IngestRunState.SUCCEEDED } })).toBe('Late')
    expect(cell({ freshness: { status: FreshnessStatus.FRESH } })).toBe('Healthy')
  })

  it('the Status cell shows Unknown for a row with no health, and nothing for a row still loading', () => {
    const unknown = mount(StatusCell, { props: { params: { data: row('u', 'U', { freshness: undefined }) } as never } })
    expect(unknown.text()).toBe('Unknown')
    const failed = mount(StatusCell, {
      props: { params: { data: row('g', 'G', { freshness: { status: FreshnessStatus.GAPS } }) } as never },
    })
    expect(failed.text()).toBe('Failed')
    expect(mount(StatusCell, { props: { params: { data: undefined } as never } }).text()).toBe('')
  })
})

// tj-grna9p.109, reshaped by tj-grna9p.114 (owner feedback 2026-10-13): the Symbol cell holds only
// the Symbol link (to the Viewer, styled as text); the details icon lives in the Coverage cell. Each
// cell is mounted alone under the real router with the params the grid hands it, inside a parent that
// stands in for the AG Grid row (its click listener is the row's click). Hidden: AG Grid's own event
// delegation, and a browser following an unprevented link (jsdom does not navigate), so a click left
// to the browser is shown by its default NOT being prevented.
describe('Symbol cell and its details icon', () => {
  const viewerHref = (id: string) => router.resolve({ name: 'data-dataset-viewer', params: { id } }).href
  const detailHref = (id: string) => router.resolve({ name: 'data-dataset-detail', params: { id } }).href

  async function inRow(component: typeof SymbolCell | typeof CoverageCell, params: Record<string, unknown>) {
    await router.push('/data/datasets')
    await router.isReady()
    const rowEl = document.createElement('div')
    const rowClick = vi.fn()
    rowEl.addEventListener('click', rowClick)
    document.body.append(rowEl)
    const host = document.createElement('div')
    rowEl.append(host)
    const wrapper = mount(component as never, {
      props: { params } as never,
      global: { plugins: [router] },
      attachTo: host,
    }) as VueWrapper
    mounted.push(wrapper)
    return { wrapper, rowClick }
  }

  async function symbolCell(symbol = 'VFV', id = 'ds 1') {
    return inRow(SymbolCell, { data: row(id, symbol), href: viewerHref })
  }

  async function coverageCell(symbol = 'VFV', id = 'ds 1') {
    const { wrapper, rowClick } = await inRow(CoverageCell, { data: row(id, symbol), detailHref })
    return { wrapper, rowClick, icon: wrapper.find('[data-testid="dataset-details-link"]') }
  }

  // Dispatched by hand so the spec can read defaultPrevented after the handlers ran.
  function click(el: Element, init: MouseEventInit = {}): MouseEvent {
    const event = new MouseEvent('click', { bubbles: true, cancelable: true, ...init })
    el.dispatchEvent(event)
    return event
  }

  it('the Symbol is a real link to the Viewer and the only thing in its cell: no details icon there', async () => {
    const { wrapper } = await symbolCell('VFV', 'a/b')
    const symbol = wrapper.find('.symbol-link')
    expect(symbol.element.tagName).toBe('A')
    expect(symbol.text()).toBe('VFV')
    expect(symbol.attributes('href')).toBe('/data/datasets/a%2Fb/viewer')
    expect(wrapper.findAll('a')).toHaveLength(1)
    expect(wrapper.find('[data-testid="dataset-details-link"]').exists()).toBe(false)
    expect(wrapper.find('svg').exists()).toBe(false)
  })

  it('the Coverage cell\'s icon is a real link to the detail, labelled with the symbol', async () => {
    const { icon } = await coverageCell('VFV', 'a/b')
    expect(icon.element.tagName).toBe('A')
    expect(icon.attributes('href')).toBe('/data/datasets/a%2Fb')
    expect(icon.attributes('aria-label')).toBe('Dataset details for VFV')
    expect(icon.attributes('title')).toBe('Dataset details')
    // The glyph is decorative: the link's label is its whole accessible name.
    expect(icon.find('svg').attributes('aria-hidden')).toBe('true')
    expect(icon.text()).toBe('')
  })

  it('both links are keyboard reachable: real hrefs, never taken out of the tab order', async () => {
    const { wrapper } = await symbolCell()
    const { icon } = await coverageCell()
    for (const link of [wrapper.find('.symbol-link'), icon]) {
      expect(link.attributes('href')).toBeTruthy()
      expect(link.attributes('tabindex')).toBeUndefined()
      ;(link.element as HTMLElement).focus()
      expect(document.activeElement).toBe(link.element)
    }
  })

  it('a plain click on the icon opens the Dataset detail in the app and never reaches the row', async () => {
    const { icon, rowClick } = await coverageCell()
    const event = click(icon.element)
    await flushPromises()
    expect(rowClick).not.toHaveBeenCalled()
    expect(event.defaultPrevented).toBe(true)
    // The detail, never the Viewer.
    expect(router.currentRoute.value.name).toBe('data-dataset-detail')
    expect(router.currentRoute.value.params.id).toBe('ds 1')
  })

  it('a click on the icon\'s glyph is the icon\'s click too', async () => {
    const { icon, rowClick } = await coverageCell()
    click(icon.find('svg').element)
    await flushPromises()
    expect(rowClick).not.toHaveBeenCalled()
    expect(router.currentRoute.value.name).toBe('data-dataset-detail')
  })

  it.each(['metaKey', 'ctrlKey', 'shiftKey', 'altKey'])('a %s click on the icon is left to the browser and still never reaches the row', async (modifier) => {
    const { icon, rowClick } = await coverageCell()
    const event = click(icon.element, { [modifier]: true })
    await flushPromises()
    expect(event.defaultPrevented).toBe(false)
    expect(rowClick).not.toHaveBeenCalled()
    expect(router.currentRoute.value.name).toBe('data-datasets')
  })

  it('a plain click on the Symbol is cancelled for the row to handle; a modified one is the browser\'s', async () => {
    const { wrapper, rowClick } = await symbolCell()
    const plain = click(wrapper.find('.symbol-link').element)
    expect(plain.defaultPrevented).toBe(true)
    expect(rowClick).toHaveBeenCalledTimes(1)
    const modified = click(wrapper.find('.symbol-link').element, { ctrlKey: true })
    expect(modified.defaultPrevented).toBe(false)
    await flushPromises()
    // The Symbol itself never navigates: the row (DatasetGrid) opens the Viewer.
    expect(router.currentRoute.value.name).toBe('data-datasets')
  })

  it('a row still loading (no data) renders nothing in either cell', () => {
    const symbol = mount(SymbolCell, {
      props: { params: { data: undefined, href: viewerHref } as never },
      global: { plugins: [router] },
    })
    const coverage = mount(CoverageCell, {
      props: { params: { data: undefined, detailHref } as never },
      global: { plugins: [router] },
    })
    mounted.push(symbol, coverage)
    expect(symbol.find('a').exists()).toBe(false)
    expect(coverage.find('a').exists()).toBe(false)
  })
})

// tj-grna9p.114 (owner feedback 2026-10-13): the whole row is the click target, so the Symbol must not
// look like a link of its own: the text colour, semibold, no accent and no underline at rest; the
// underline shows on hover and on keyboard focus, and the global :focus-visible ring stays. The row
// shows that it is clickable instead: the grid theme's row hover (the raised surface) and a pointer.
// jsdom does not compute scoped CSS and the grid is a stub here, so the rules are read from the
// components' stylesheets on disk and the theme's parameters from the theme object (as the Google
// font check above does). Unverified without a real browser: the hover paint, the pointer, the
// clipping of a narrow Coverage column, and Tab reaching a link inside an AG Grid cell.
describe('the row is the click target; the Symbol reads as text (tj-grna9p.114)', () => {
  const source = (path: string) => readFileSync(resolve(import.meta.dirname, path), 'utf8')
  const symbolSource = () => source('../../components/catalog/SymbolCell.vue')
  const styleOf = (sfc: string) => /<style scoped>([\s\S]*)<\/style>/.exec(sfc)![1]!
  /** Every rule whose selector list names `.symbol-link`, as [selector, body]. */
  function symbolRules(): [string, string][] {
    return [...styleOf(symbolSource()).replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^}]*)\}/g)]
      .map((m) => [m[1]!.trim(), m[2]!] as [string, string])
      .filter(([selector]) => selector.includes('.symbol-link'))
  }
  const rest = () => symbolRules().find(([s]) => s === '.symbol-link')![1]

  it('at rest the Symbol is the text colour, semibold, with no underline', () => {
    expect(rest()).toMatch(/(^|;|\s)color:\s*var\(--tj-text\)\s*;/)
    expect(rest()).toMatch(/font-weight:\s*600\s*;/)
    expect(rest()).toMatch(/text-decoration:\s*none\s*;/)
  })

  it('no rule gives the Symbol the accent colour', () => {
    for (const [selector, body] of symbolRules()) {
      expect(body, selector).not.toMatch(/accent/)
      // Only the rest rule sets a colour, and it is the text colour.
      if (selector !== '.symbol-link') expect(body, selector).not.toMatch(/(^|;|\s)color:/)
    }
    expect(symbolSource()).not.toMatch(/--tj-accent|accentTint|accentFill/)
  })

  it('the underline shows on hover and on keyboard focus, and only there', () => {
    const underlined = symbolRules().filter(([, body]) => /text-decoration:\s*underline/.test(body))
    expect(underlined).toHaveLength(1)
    const selectors = underlined[0]![0].split(',').map((s) => s.trim()).sort()
    expect(selectors).toEqual(['.symbol-link:focus-visible', '.symbol-link:hover'])
  })

  it('the global :focus-visible ring is untouched and the Symbol does not override it', () => {
    const base = source('../../theme/base.css')
    expect(base).toMatch(/\n:focus-visible\s*\{\s*outline:\s*2px solid var\(--tj-accent-tint\);\s*outline-offset:\s*2px;\s*\}/)
    for (const [selector, body] of symbolRules()) expect(body, selector).not.toMatch(/outline/)
  })

  it('the row hover is the raised surface and a row shows a pointer', async () => {
    const { gridTheme } = await import('@/catalog/agGrid')
    const modes = (gridTheme as unknown as { _getModeParams(): Record<string, Record<string, unknown>> })._getModeParams()
    const hover = Object.values(modes).map((mode) => mode.rowHoverColor)
    expect(hover.length).toBeGreaterThan(0)
    for (const value of hover) expect(value).toBe(colors.raised)
    const grid = styleOf(source('../../components/catalog/DatasetGrid.vue'))
    expect(grid).toMatch(/\.grid\s+:deep\(\.ag-row\)\s*\{[^}]*cursor:\s*pointer\s*;[^}]*\}/)
  })

  // The Coverage column must fit bar + gap + numbers + gap + icon + the cell's own padding at its
  // narrowest. Every part is read from where it is defined: the widths and gaps from the two
  // stylesheets, the padding from the grid theme, minWidth from the column the screen builds. The
  // numbers' slot is in ch, which jsdom cannot measure; it is counted at 0.5em of its 12px font, a
  // floor (a digit is never narrower than half an em in the app's sans), so this pins "nothing was
  // forgotten", not the last pixel (see the verdict note on tj-grna9p.114).
  it('the Coverage column\'s minWidth fits the bar, the numbers, the icon, their gaps and the padding', async () => {
    await screenAt('/data/datasets')
    const cols = grid.props!.columnDefs as ColDef<DatasetSummary>[]
    const minWidth = cols.find((c) => c.colId === 'coverage')!.minWidth!
    const cell = styleOf(source('../../components/catalog/CoverageCell.vue'))
    const strip = styleOf(source('../../components/charts/CoverageStrip.vue'))
    const px = (css: string, re: RegExp) => Number(re.exec(css)![1])
    const bar = px(cell, /--coverage-bar-width:\s*(\d+)px/)
    const icon = px(cell, /--coverage-icon-width:\s*(\d+)px/)
    const textCh = px(cell, /--coverage-text-width:\s*(\d+)ch/)
    const cellGap = px(cell, /\n\.coverage-cell\s*\{[^}]*\sgap:\s*(\d+)px/)
    const rowGap = px(strip, /\n\.coverage__row\s*\{[^}]*\sgap:\s*(\d+)px/)
    const summaryFont = px(strip, /\n\.coverage__summary\s*\{[^}]*font-size:\s*(\d+)px/)
    const { gridTheme } = await import('@/catalog/agGrid')
    const modes = (gridTheme as unknown as { _getModeParams(): Record<string, Record<string, unknown>> })._getModeParams()
    const padding = Number(Object.values(modes)[0]!.cellHorizontalPadding)
    expect(padding).toBeGreaterThan(0)
    const needed = bar + rowGap + textCh * summaryFont * 0.5 + cellGap + icon + 2 * padding
    expect(minWidth).toBeGreaterThanOrEqual(Math.ceil(needed))
  })
})

describe('load states', () => {
  it('an empty catalog says so', async () => {
    mocks.listDatasets.mockResolvedValue({ items: [], nextCursor: '' })
    const shell = await screenAt('/data/datasets')
    expect(shell.find('[data-testid="catalog-loading"]').exists()).toBe(true)
    await getRows(latest(), 0)
    await flushPromises()
    expect(shell.find('[data-testid="catalog-empty"]').text()).toBe('There are no datasets yet.')
  })

  it('an empty filtered list says no match', async () => {
    mocks.listDatasets.mockResolvedValue({ items: [], nextCursor: '' })
    const shell = await screenAt('/data/datasets?status=retired')
    await getRows(latest(), 0)
    await flushPromises()
    expect(shell.find('[data-testid="catalog-empty"]').text()).toBe('No datasets match these filters.')
  })

  it('a first-page failure shows the error with its id and never the body text; Try Again reloads', async () => {
    mocks.listDatasets.mockRejectedValue(new ApiError({ status: 503, reason: 'UNAVAILABLE', errorId: 'e-7' }))
    const shell = await screenAt('/data/datasets')
    await getRows(latest(), 0)
    await flushPromises()
    const panel = shell.find('[data-testid="catalog-error"]')
    expect(panel.text()).toContain('Could not load datasets. Request failed (HTTP 503)')
    expect(panel.text()).toContain('Error ID: e-7')
    expect(panel.classes()).not.toContain('grid-panel--banner')
    expect(shell.find('footer').text()).toContain('Could not load datasets: Request failed (HTTP 503) (error id e-7)')

    mocks.listDatasets.mockResolvedValue({ items: [row('a', 'AAA')], nextCursor: '' })
    await panel.find('button').trigger('click')
    await flushPromises()
    expect(grid.datasources).toHaveLength(2)
    await getRows(latest(), 0)
    await flushPromises()
    expect(shell.find('[data-testid="catalog-error"]').exists()).toBe(false)
  })

  it('a next-page failure keeps the loaded rows: the error is a banner, not a panel over them', async () => {
    mocks.listDatasets
      .mockResolvedValueOnce({ items: Array.from({ length: 100 }, (_, i) => row(`r${i}`, `S${i}`)), nextCursor: 'k1' })
      .mockRejectedValueOnce(new ApiError({ status: 500 }))
    mocks.getDatasetFacets.mockResolvedValue({ ...FACETS, all: 250n })
    const shell = await screenAt('/data/datasets')
    const first = await getRows(latest(), 0)
    expect(first.rows).toHaveLength(100)
    expect(await getRows(latest(), 100)).toEqual({ failed: true })
    await flushPromises()
    expect(shell.find('[data-testid="catalog-error"]').classes()).toContain('grid-panel--banner')
    expect(shell.find('footer [data-testid="footer-summary"]').text()).toBe('100 of 250 datasets · sorted by symbol')
  })

  // tj-grna9p.117: the word Collection is gone from the Datasets page. The rendered page (shell, tiles,
  // sidebar, grid area, footer) is read as the user sees it: its text and its accessible names. The
  // capitalised word is what a label would be; lower-case 'collection' in prose is allowed. The grid is
  // a stub here, so its headers are checked through the column definitions ('columns:' above).
  it('no visible text or accessible name on the Datasets page is the word Collection', async () => {
    const shell = await screenAt('/data/datasets?running=true&status=late')
    await getRows(latest(), 0)
    await flushPromises()
    const page = shell.element as HTMLElement
    // The page really rendered: the Status legend, a tile and the footer are in the text read below.
    expect(page.textContent).toContain(titleCase('Status'))
    expect(page.querySelector('[data-tile="failed"]')).not.toBeNull()
    expect(page.querySelector('[data-testid="footer-summary"]')).not.toBeNull()
    expect(page.textContent).not.toMatch(/\bCollection\b/)
    const walker = document.createTreeWalker(page, NodeFilter.SHOW_TEXT)
    const words: string[] = []
    for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) words.push(node.textContent!.trim())
    expect(words.filter((w) => w.toLowerCase() === 'collection')).toEqual([])
    const named = [...page.querySelectorAll('[aria-label], [title], [placeholder]')].flatMap((el) =>
      ['aria-label', 'title', 'placeholder'].map((a) => el.getAttribute(a) ?? ''),
    )
    expect(named.filter((n) => /\bCollection\b/.test(n))).toEqual([])
  })

  it('the footer counts loaded rows over the view total, then the true total at the end', async () => {
    const shell = await screenAt('/data/datasets')
    await getRows(latest(), 0)
    await flushPromises()
    expect(shell.find('[data-testid="footer-summary"]').text()).toBe('2 of 2 datasets · sorted by symbol')
  })
})

// tj-grna9p.77 item 5: an event is a hint to refetch over REST. A burst is coalesced into ONE reload
// of the counts and of the rows the grid holds (in place: same datasource, refreshInfiniteCache), and
// only the coverage strips of the datasets the burst named are dropped. A reconnect reloads the same
// way and drops every strip. Only setTimeout and clearTimeout are faked (flushPromises needs the rest).
describe('live refresh', () => {
  const coverageIds = () => mocks.getDatasetCoverage.mock.calls.map(([id]) => id as string)

  async function liveScreen() {
    const shell = await screenAt('/data/datasets')
    await getRows(latest(), 0)
    await flushPromises()
    const coverage = useDatasetCoverageStore()
    for (const id of ['a', 'b', 'held']) coverage.acquire(id, 60)
    await flushPromises()
    expect(coverageIds().sort()).toEqual(['a', 'b', 'held'])
    mocks.getDatasetCoverage.mockClear()
    mocks.listDatasets.mockClear()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    return { shell, facetCalls: mocks.getDatasetFacets.mock.calls.length, datasources: grid.datasources.length }
  }

  afterEach(() => {
    vi.useRealTimers()
  })

  it('subscribes to DatasetChanged and IngestRunChanged while mounted, and lets go on unmount', async () => {
    const { shell } = await liveScreen()
    expect(fakeEvents.subscribers('datasetChanged')).toBe(1)
    expect(fakeEvents.subscribers('ingestRunChanged')).toBe(1)
    shell.unmount()
    mounted.splice(0)
    expect(fakeEvents.subscribers('datasetChanged')).toBe(0)
    expect(fakeEvents.subscribers('ingestRunChanged')).toBe(0)
  })

  it('a burst of five events reloads the counts and the rows ONCE, after COALESCE_MS, in place', async () => {
    const { facetCalls, datasources } = await liveScreen()
    fakeEvents.emit({ event: { case: 'datasetChanged', value: { id: 'a', change: DatasetChange.UPDATED } } })
    fakeEvents.emit({ event: { case: 'ingestRunChanged', value: { run: { id: 'r1', datasetId: 'b', state: IngestRunState.RUNNING } } } })
    fakeEvents.emit({ event: { case: 'ingestRunChanged', value: { run: { id: 'r1', datasetId: 'b', state: IngestRunState.SUCCEEDED } } } })
    fakeEvents.emit({ event: { case: 'datasetChanged', value: { id: 'a', change: DatasetChange.UPDATED } } })
    // A run not yet acknowledged has no dataset id: the rows still reload.
    fakeEvents.emit({ event: { case: 'ingestRunChanged', value: { run: { id: 'r2', datasetId: '' } } } })
    vi.advanceTimersByTime(COALESCE_MS - 1)
    await flushPromises()
    expect(grid.refreshes).toBe(0)
    expect(mocks.getDatasetFacets.mock.calls.length).toBe(facetCalls)

    vi.advanceTimersByTime(1)
    await flushPromises()
    expect(grid.refreshes).toBe(1)
    const oneReload = mocks.getDatasetFacets.mock.calls.length - facetCalls
    expect(oneReload).toBeGreaterThanOrEqual(1)
    // In place: no new datasource (so no scroll reset and no loading panel), and its next getRows starts
    // the cursor chain over.
    expect(grid.datasources.length).toBe(datasources)
    await getRows(latest(), 0)
    expect(mocks.listDatasets).toHaveBeenCalledTimes(1)
    expect(listArgs().cursor).toBeUndefined()
    // Only the named datasets' strips are fetched again, once each; an untouched strip is kept.
    expect(coverageIds().sort()).toEqual(['a', 'b'])

    // A later single event is its own window: one more reload, the same counts fetch as before.
    vi.advanceTimersByTime(COALESCE_MS * 10)
    await flushPromises()
    expect(grid.refreshes).toBe(1)
    fakeEvents.emit({ event: { case: 'datasetChanged', value: { id: 'held', change: DatasetChange.UPDATED } } })
    vi.advanceTimersByTime(COALESCE_MS)
    await flushPromises()
    expect(grid.refreshes).toBe(2)
    expect(mocks.getDatasetFacets.mock.calls.length - facetCalls).toBe(2 * oneReload)
  })

  it('a steady trickle cannot postpone the reload: the window closes COALESCE_MS after its FIRST event', async () => {
    await liveScreen()
    for (let i = 0; i < 4; i += 1) {
      fakeEvents.emit({ event: { case: 'datasetChanged', value: { id: 'a', change: DatasetChange.UPDATED } } })
      vi.advanceTimersByTime(COALESCE_MS / 4)
    }
    await flushPromises()
    expect(grid.refreshes).toBe(1)
  })

  it('a reconnect reloads the counts and rows once, at once, and drops every coverage strip', async () => {
    const { facetCalls, datasources } = await liveScreen()
    fakeEvents.reconnect()
    await flushPromises()
    expect(grid.refreshes).toBe(1)
    expect(mocks.getDatasetFacets.mock.calls.length).toBeGreaterThan(facetCalls)
    expect(grid.datasources.length).toBe(datasources)
    expect(coverageIds().sort()).toEqual(['a', 'b', 'held'])
  })

  it('leaving the screen inside a window cancels its reload', async () => {
    const { shell } = await liveScreen()
    fakeEvents.emit({ event: { case: 'datasetChanged', value: { id: 'a', change: DatasetChange.UPDATED } } })
    shell.unmount()
    mounted.splice(0)
    vi.advanceTimersByTime(COALESCE_MS * 2)
    await flushPromises()
    expect(grid.refreshes).toBe(0)
    expect(mocks.getDatasetCoverage).not.toHaveBeenCalled()
  })

  it('the screen never polls: no setInterval while it is mounted and receiving events', async () => {
    const interval = vi.spyOn(globalThis, 'setInterval')
    await liveScreen()
    fakeEvents.emit({ event: { case: 'datasetChanged', value: { id: 'a', change: DatasetChange.UPDATED } } })
    vi.advanceTimersByTime(COALESCE_MS)
    fakeEvents.reconnect()
    await flushPromises()
    expect(interval).not.toHaveBeenCalled()
  })
})
