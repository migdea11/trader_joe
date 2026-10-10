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
import { DatasetSummarySchema, FreshnessStatus, type DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'
import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'

import { ApiError } from '@/api'
import StatusCell from '@/components/catalog/StatusCell.vue'
import router from '@/router'
import AppShell from '@/shell/AppShell.vue'
import { useSettingsStore } from '@/stores/settings'

// tj-grna9p.32 phase 1 Datasets, mounted in the real shell under the real router.
//
// WHAT THE MOCKS HIDE. The API is mocked at the typed client (@/api). AG Grid is replaced by a stub
// (jsdom has no layout, so the real grid never asks for a row): the stub records the props the
// screen passes (column definitions, row model options) and hands the screen a fake GridApi whose
// setGridOption('datasource', ...) is captured, and the spec then drives the datasource the way
// the grid would (getRows by block). Hidden: AG Grid's own block scheduling and scrolling, its
// rendering of cells (the column value getters and the Status cell are exercised directly), and
// AG Grid destroying the datasource it replaces.
const grid = vi.hoisted(() => ({
  props: null as Record<string, unknown> | null,
  datasources: [] as IDatasource[],
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
}))
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
  it('a plain row click opens the Viewer; a modified click is left to the browser', async () => {
    await screenAt('/data/datasets')
    const gridStub = mounted[0]!.findComponent({ name: 'AgGridVueStub' })
    gridStub.vm.$emit('row-clicked', { data: row('ds 1', 'AAA'), event: new MouseEvent('click', { metaKey: true }) })
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('data-datasets')
    gridStub.vm.$emit('row-clicked', { data: row('ds 1', 'AAA'), event: new MouseEvent('click', { ctrlKey: true }) })
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('data-datasets')
    gridStub.vm.$emit('row-clicked', { data: row('ds 1', 'AAA'), event: new MouseEvent('click') })
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('data-dataset-viewer')
    expect(router.currentRoute.value.params.id).toBe('ds 1')
  })

  it('columns: symbol links the Viewer, source and feed, granularity, update, and Expires date or dash', async () => {
    useSettingsStore().setTimeZoneOverride('America/Toronto')
    await screenAt('/data/datasets')
    const cols = grid.props!.columnDefs as ColDef<DatasetSummary>[]
    expect(cols.map((c) => c.headerName)).toEqual(['Symbol', 'Source · Feed', 'Gran.', 'Update', 'Status', 'Expires'])
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
    expect((symbol.cellRendererParams as { href: (id: string) => string }).href('a/b')).toBe('/data/datasets/a%2Fb/viewer')
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

  it('the footer counts loaded rows over the view total, then the true total at the end', async () => {
    const shell = await screenAt('/data/datasets')
    await getRows(latest(), 0)
    await flushPromises()
    expect(shell.find('[data-testid="footer-summary"]').text()).toBe('2 of 2 datasets · sorted by symbol')
  })
})
