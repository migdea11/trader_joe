import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import { defineComponent, h, isReactive, onMounted } from 'vue'
import { create, type MessageInitShape } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'
import type { ColDef, IDatasource, IGetRowsParams } from 'ag-grid-community'

import { BarSchema, type Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'
import { DataSource, DataType, Feed, Granularity, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { BarPageIndexSchema, DatasetSummarySchema, FreshnessStatus } from '@generated/trader_joe/proto/ui/v1/data_pb'
import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'

import { fakeEvents } from '@/__tests__/fakeEvents'
import { ApiError } from '@/api'
import { dataTypeLabel, granularityLabel, sourceFeedLabel } from '@/catalog/labels'
import PriceChart from '@/components/charts/PriceChart.vue'
import ZoomNavigator from '@/components/charts/ZoomNavigator.vue'
import BarTable from '@/components/viewer/BarTable.vue'
import router from '@/router'
import AppShell from '@/shell/AppShell.vue'
import { useSettingsStore } from '@/stores/settings'
import { titleCase } from '@/theme/copy'
import { barsToCsv } from '@/viewer/csv'

// tj-grna9p.33 Data Viewer, mounted in the real shell under the real router, with the API mocked at
// the typed client (@/api). The chart is PAGED (tj-grna9p.124, decision tj-eozqhi with the owner's
// fixed-block rulings of 2026-10-13 and tj-grna9p.118): the screen reads the window's page index
// (getDatasetBarPages, size 1,000, look-ahead 50), opens on the NEWEST page, reads each page with
// getDatasetBars (start = the page's first bar, limit 1,050) together with its older neighbour, and
// the zoom navigator is the pager. The 50,000-bar cap, its truncated notice and the footer counts are
// gone. Export CSV is a popover over a date range (tj-grna9p.126), detailed in ExportPopover.spec.ts.
//
// WHAT THE STUBS HIDE. PriceChart and ZoomNavigator are stubbed on the screen (their own behaviour
// and the createChart mock live in PriceChart.spec.ts and ZoomNavigator.spec.ts): the spec reads the
// props the screen hands them and emits their events, so it sees which page, overlays and
// label they would get, but nothing is drawn. The bar table is stubbed on the screen too and tested
// on its own below over an AG Grid stub that captures the datasource; AG Grid's block scheduling and
// cell rendering are hidden there. setTimeout is faked (the 150 ms seek debounce and the model's
// 150 ms settle before preloading the neighbours); setImmediate is not, so flushPromises still works.
const grid = vi.hoisted(() => ({ props: null as Record<string, unknown> | null, datasources: [] as IDatasource[] }))
vi.mock('ag-grid-vue3', () => ({
  AgGridVue: defineComponent({
    name: 'AgGridVueStub',
    inheritAttrs: false,
    props: {
      columnDefs: { type: Array, default: undefined },
      rowModelType: { type: String, default: undefined },
      cacheBlockSize: { type: Number, default: undefined },
    },
    emits: ['grid-ready'],
    setup(props, { emit }) {
      grid.props = props as Record<string, unknown>
      onMounted(() =>
        emit('grid-ready', {
          api: {
            setGridOption: (key: string, value: unknown) => {
              if (key === 'datasource') grid.datasources.push(value as IDatasource)
            },
            refreshCells: vi.fn(),
          },
        }),
      )
      return () => h('div')
    },
  }),
}))

// Only the 2b6e122 test mounts the real PriceChart (every other test stubs it); jsdom has no canvas, so
// createChart is a recorder that keeps the range handler, as in PriceChart.spec.ts. Hidden: the library's
// own range arithmetic and its frame; the test plays the frame's reports itself.
interface ViewerFakeChart {
  rangeHandler: ((range: { from: number; to: number } | null) => void) | null
  visible: { from: number; to: number } | null
  setVisibleLogicalRange: ReturnType<typeof vi.fn>
}
const lwc = vi.hoisted(() => ({ charts: [] as ViewerFakeChart[] }))
vi.mock('lightweight-charts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('lightweight-charts')>()),
  createChart: vi.fn(() => {
    const chart: ViewerFakeChart = { rangeHandler: null, visible: null, setVisibleLogicalRange: vi.fn() }
    chart.setVisibleLogicalRange.mockImplementation((range: { from: number; to: number }) => {
      chart.visible = range
    })
    lwc.charts.push(chart)
    const timeScale = {
      subscribeVisibleLogicalRangeChange: (handler: ViewerFakeChart['rangeHandler']) => {
        chart.rangeHandler = handler
      },
      unsubscribeVisibleLogicalRangeChange: vi.fn(),
      setVisibleLogicalRange: chart.setVisibleLogicalRange,
      getVisibleLogicalRange: () => chart.visible,
      fitContent: vi.fn(),
    }
    return {
      addSeries: vi.fn(() => ({ setData: vi.fn() })),
      removeSeries: vi.fn(),
      applyOptions: vi.fn(),
      remove: vi.fn(),
      panes: () => [{ setStretchFactor: vi.fn() }, { setStretchFactor: vi.fn() }],
      subscribeCrosshairMove: vi.fn(),
      unsubscribeCrosshairMove: vi.fn(),
      timeScale: () => timeScale,
    }
  }),
}))

const mocks = vi.hoisted(() => ({
  getUiConfig: vi.fn(),
  getDataset: vi.fn(),
  getDatasetBars: vi.fn(),
  getDatasetBarPages: vi.fn(),
  getDatasetFacets: vi.fn(),
  listDatasets: vi.fn(),
}))
// The shell opens the shared events socket on mount (tj-grna9p.76): replaced so no real WebSocket opens.
vi.mock('@/api/events', async (importOriginal) =>
  (await import('@/__tests__/fakeEvents')).fakeEventsModule(await importOriginal<object>()),
)
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...mocks,
}))

const DAY = 86_400_000
const T0 = Date.UTC(2026, 8, 1, 4)

function makeBar(i: number): Bar {
  return create(BarSchema, { barStart: timestampFromMs(T0 + i * DAY), open: 100 + i, high: 101 + i, low: 99 + i, close: 100.5 + i, volume: 1000 + i })
}

function summary(extra: MessageInitShape<typeof DatasetSummarySchema> = {}) {
  return create(DatasetSummarySchema, {
    id: 'ds-1',
    assetSymbol: 'VFV',
    dataType: DataType.MARKET_ACTIVITY,
    source: DataSource.ALPACA_API,
    feed: Feed.IEX,
    granularity: Granularity.ONE_DAY,
    updateType: UpdateType.DAILY,
    owner: 'operator',
    barCount: 3n,
    firstBar: timestampFromMs(T0),
    lastBar: timestampFromMs(T0 + 2 * DAY),
    freshness: { status: FreshnessStatus.FRESH },
    siblings: [{ id: 'ds-1h', granularity: Granularity.ONE_HOUR }],
    ...extra,
  } as MessageInitShape<typeof DatasetSummarySchema>)
}

// A store of `total` daily bars from T0 (bar i at T0 + i days), answering the two reads the way the
// routes do. bars: the half-open [start, end) window, from the cursor or else the first bar at or
// after start, at most `limit`. bar-pages: the window's fixed 1,000-bar blocks counted back from the
// newest bar (the server rule of tj-grna9p.120, which the client only reads). A read whose first bar
// is in `hold` waits for release() (or its abort); one in `failOnce` rejects once with that error.
const server = {
  total: 3,
  hold: new Set<number>(),
  waiting: [] as Array<() => void>,
  failOnce: new Map<number, unknown>(),
  release(): void {
    this.hold.clear()
    this.waiting.splice(0).forEach((resume) => resume())
  },
}

const indexOf = (date: Date | undefined): number | undefined =>
  date === undefined ? undefined : Math.ceil((date.getTime() - T0) / DAY)

function windowBars(start?: Date, end?: Date): [number, number] {
  const a = Math.max(0, indexOf(start) ?? 0)
  const b = Math.min(server.total, indexOf(end) ?? server.total)
  return [a, Math.max(a, b)]
}

function blockOffsets(n: number): number[] {
  if (n === 0) return []
  if (n <= 1000) return [0]
  const r = n % 1000
  const offsets = r > 0 ? [0] : []
  for (let offset = r; offset <= n - 1000; offset += 1000) offsets.push(offset)
  return offsets
}

interface BarsParams {
  start?: Date
  end?: Date
  cursor?: string
  limit?: number
  signal?: AbortSignal
}

function serveBars(total: number): void {
  server.total = total
  mocks.getDatasetBars.mockImplementation(async (_id: string, params: BarsParams) => {
    const [a, b] = windowBars(params.start, params.end)
    const offset = params.cursor === undefined ? a : Number(params.cursor)
    const failure = server.failOnce.get(offset)
    if (failure !== undefined) {
      server.failOnce.delete(offset)
      throw failure
    }
    if (server.hold.has(offset)) {
      await new Promise<void>((resolve, reject) => {
        server.waiting.push(resolve)
        params.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
      })
    }
    const count = Math.max(0, Math.min(params.limit ?? 1000, b - offset))
    const bars = Array.from({ length: count }, (_, i) => makeBar(offset + i))
    return { bars, nextCursor: offset + count < b ? String(offset + count) : '' }
  })
  mocks.getDatasetBarPages.mockImplementation(async (_id: string, params: BarsParams & { size?: number; overlap?: number }) => {
    const [a, b] = windowBars(params.start, params.end)
    return create(BarPageIndexSchema, {
      barCount: BigInt(b - a),
      pageSize: params.size ?? 1000,
      overlap: params.overlap ?? 50,
      pages: blockOffsets(b - a).map((offset) => ({ start: timestampFromMs(T0 + (a + offset) * DAY), offset: BigInt(offset) })),
    })
  })
}

/** A dataset of `total` bars, served. */
function dataset(total: number): void {
  mocks.getDataset.mockResolvedValue(summary({ barCount: BigInt(total), lastBar: timestampFromMs(T0 + (total - 1) * DAY) }))
  serveBars(total)
}

const mounted: VueWrapper[] = []

async function viewerAt(path: string, options: { realChart?: boolean; realNavigator?: boolean } = {}): Promise<VueWrapper> {
  await router.push(path)
  await router.isReady()
  const shell = mount(AppShell, {
    global: {
      plugins: [createPinia(), router, PrimeVue],
      stubs: { PriceChart: !options.realChart, ZoomNavigator: !options.realNavigator, BarTable: true },
    },
    attachTo: document.body,
  })
  mounted.push(shell)
  await flushPromises()
  return shell
}

const chartCalls = () => mocks.getDatasetBars.mock.calls as Array<[string, BarsParams]>
const pagesCalls = () => mocks.getDatasetBarPages.mock.calls as Array<[string, BarsParams & { size?: number; overlap?: number }]>
/** The first bar of every bar read, as a dataset index, in order. */
const barReads = () => chartCalls().map(([, p]) => (p.cursor === undefined ? indexOf(p.start) ?? 0 : Number(p.cursor)))
const closesOf = (bars: readonly Bar[]) => bars.map((bar) => bar.close - 100.5)
const span = (from: number, to: number) => Array.from({ length: to - from }, (_, i) => from + i)

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  window.localStorage.clear()
  setActivePinia(createPinia())
  fakeEvents.reset()
  useSettingsStore().setTimeZoneOverride('America/Toronto')
  grid.datasources.length = 0
  server.hold.clear()
  server.waiting.length = 0
  server.failOnce.clear()
  mocks.getUiConfig.mockResolvedValue({ allowedGroups: [AccountGroup.SIMULATION], deploymentLabel: '', serverVersion: '' })
  mocks.getDataset.mockResolvedValue(summary())
  serveBars(3)
})

afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount())
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('header and states', () => {
  it('titles the page with the dataset name and shows the detail', async () => {
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    expect(mocks.getDataset).toHaveBeenCalledWith('ds-1', expect.any(AbortSignal))
    expect(shell.find('[data-testid="viewer-title"]').text()).toBe('VFV · bars · 1d')
    expect(shell.find('[data-testid="viewer-badge"]').text()).toBe('Healthy')
    const meta = shell.find('[data-testid="viewer-meta"]').text()
    expect(meta).toContain('Alpaca · IEX')
    expect(meta).toContain('Daily')
    expect(meta).toContain('2026/09/01')
    expect(meta).toContain('2026/09/03')
    expect(shell.find('[data-testid="viewer-symbol"]').text()).toBe('VFV')
  })

  // tj-grna9p.109: the Viewer is now where a Datasets row lands, so its header links on to the
  // Dataset detail. tj-grna9p.113 keeps this link; the way back from the detail is its breadcrumb.
  it('the header\'s Details link is a real link to the Dataset detail, and following it opens the detail', async () => {
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    const details = shell.find('[data-testid="open-details"]')
    expect(details.element.tagName).toBe('A')
    expect(details.text()).toBe('Details')
    expect(details.attributes('href')).toBe('/data/datasets/ds-1')
    expect(details.attributes('tabindex')).toBeUndefined()
    await details.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('data-dataset-detail')
    expect(router.currentRoute.value.params.id).toBe('ds-1')
  })

  it('the Details link escapes an id that is not URL-safe', async () => {
    mocks.getDataset.mockResolvedValue(summary({ id: 'a/b' }))
    const shell = await viewerAt('/data/datasets/a%2Fb/viewer')
    expect(shell.find('[data-testid="open-details"]').attributes('href')).toBe('/data/datasets/a%2Fb')
  })

  // tj-grna9p.113 (owner 2026-10-13): the Viewer is the dataset's own page in the hierarchy
  // Datasets > <dataset> > Details, so its trail is Datasets > <dataset>, the dataset the current
  // page. The owner then ruled the dataset crumb reads the asset symbol only, the same crumb as on the
  // Detail, with the whole identity as its hover title and no aria-label (its accessible name is the
  // visible symbol). The page title keeps the full name. Its Details link (above) is unchanged.
  const crumbNav = (shell: VueWrapper) => shell.find('section.viewer [data-testid="breadcrumbs"]')
  const crumbTrail = (shell: VueWrapper) => crumbNav(shell).findAll('li').map((li) => li.text())

  it('the breadcrumb trail is Datasets > <symbol>: Datasets links the catalog, the symbol is the current page', async () => {
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    const nav = crumbNav(shell)
    expect(nav.element.tagName).toBe('NAV')
    expect(nav.attributes('aria-label')).toBe('Breadcrumb')
    expect(crumbTrail(shell)).toEqual([titleCase('Datasets'), 'VFV'])
    expect(shell.find('[data-testid="viewer-title"]').text()).toBe('VFV · bars · 1d')
    const links = nav.findAll('[data-testid="breadcrumb-link"]')
    expect(links.map((a) => [a.element.tagName, a.text(), a.attributes('href')])).toEqual([['A', 'Datasets', '/data/datasets']])
    const current = nav.find('[data-testid="breadcrumb-current"]')
    expect(current.element.tagName).toBe('SPAN')
    expect(current.text()).toBe('VFV')
    expect(current.attributes('aria-current')).toBe('page')
    expect(nav.findAll('[aria-current]')).toHaveLength(1)
    await links[0]!.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('data-datasets')
  })

  it('the current crumb\'s hover title is the whole identity; it has no aria-label, so its name is the symbol', async () => {
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    const d = summary()
    const identity = [d.assetSymbol, dataTypeLabel(d.dataType), granularityLabel(d.granularity), sourceFeedLabel(d.source, d.feed)].join(' · ')
    const current = crumbNav(shell).find('[data-testid="breadcrumb-current"]')
    expect(current.attributes('title')).toBe(identity)
    expect(current.attributes('aria-label')).toBeUndefined()
    expect(current.attributes('aria-labelledby')).toBeUndefined()
    expect(current.text()).toBe('VFV')
    expect(crumbNav(shell).find('[data-testid="breadcrumb-link"]').attributes('title')).toBeUndefined()
  })

  it('while the dataset is loading the trail is Datasets > Viewer, with no hover title', async () => {
    mocks.getDataset.mockReturnValue(new Promise(() => undefined))
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    expect(crumbTrail(shell)).toEqual([titleCase('Datasets'), titleCase('Viewer')])
    const current = crumbNav(shell).find('[data-testid="breadcrumb-current"]')
    expect(current.attributes('aria-current')).toBe('page')
    expect(current.attributes('title')).toBeUndefined()
  })

  it('a dataset not found (404) has the trail Datasets > Viewer', async () => {
    mocks.getDataset.mockRejectedValue(new ApiError({ status: 404, reason: 'NOT_FOUND' }))
    const shell = await viewerAt('/data/datasets/missing/viewer')
    expect(shell.find('[data-testid="viewer-not-found"]').exists()).toBe(true)
    expect(crumbTrail(shell)).toEqual([titleCase('Datasets'), titleCase('Viewer')])
    expect(crumbNav(shell).findAll('[data-testid="breadcrumb-link"]').map((a) => a.attributes('href'))).toEqual(['/data/datasets'])
  })

  it('a failed load has the trail Datasets > Viewer, and Datasets > <symbol> once Try Again succeeds', async () => {
    mocks.getDataset.mockRejectedValueOnce(new ApiError({ status: 503, errorId: 'v-6' }))
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    const panel = shell.find('[data-testid="viewer-error"]')
    expect(panel.exists()).toBe(true)
    expect(crumbTrail(shell)).toEqual([titleCase('Datasets'), titleCase('Viewer')])
    await panel.find('button').trigger('click')
    await flushPromises()
    expect(crumbTrail(shell)).toEqual([titleCase('Datasets'), 'VFV'])
  })

  it('an unset health shows the Unknown badge', async () => {
    mocks.getDataset.mockResolvedValue(summary({ freshness: undefined }))
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    expect(shell.find('[data-testid="viewer-badge"]').text()).toBe('Unknown')
  })

  it('a 404 is the not-found state, not an error to retry', async () => {
    mocks.getDataset.mockRejectedValue(new ApiError({ status: 404, reason: 'NOT_FOUND' }))
    const shell = await viewerAt('/data/datasets/missing/viewer')
    expect(shell.find('[data-testid="viewer-not-found"]').text()).toContain('No dataset has the id missing.')
    expect(shell.find('[data-testid="viewer-error"]').exists()).toBe(false)
    expect(mocks.getDatasetBars).not.toHaveBeenCalled()
    expect(mocks.getDatasetBarPages).not.toHaveBeenCalled()
  })

  it('any other failure shows the error id and Try Again reloads the detail', async () => {
    mocks.getDataset.mockRejectedValueOnce(new ApiError({ status: 503, errorId: 'v-5' }))
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    const panel = shell.find('[data-testid="viewer-error"]')
    expect(panel.text()).toContain('Request failed (HTTP 503)')
    expect(panel.text()).toContain('Error ID: v-5')
    await panel.find('button').trigger('click')
    await flushPromises()
    expect(mocks.getDataset).toHaveBeenCalledTimes(2)
    expect(shell.find('[data-testid="viewer-title"]').exists()).toBe(true)
  })

  it('an empty dataset says so and fetches no bars', async () => {
    mocks.getDataset.mockResolvedValue(summary({ barCount: 0n, firstBar: undefined, lastBar: undefined }))
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    expect(shell.find('[data-testid="viewer-empty"]').text()).toBe('This dataset has no bars yet.')
    expect(mocks.getDatasetBars).not.toHaveBeenCalled()
    expect(mocks.getDatasetBarPages).not.toHaveBeenCalled()
    expect(shell.find('[data-testid="export-csv"]').attributes('disabled')).toBeDefined()
    expect(shell.find('[data-testid="viewer-meta"]').text()).toContain('—')
  })
})

// The chart as found on the screen (a stub but for the 2b6e122 test): its props and its attributes.
interface ChartLike {
  props(): object
  attributes(): Record<string, string>
}

// Both overlap bands as the chart receives them: [from, to] of the left (previous page) and the right
// (next page) band.
function overlapsOf(chart: ChartLike): { before: unknown[]; after: unknown[] } {
  const p = chart.props() as Record<string, unknown>
  return {
    before: [p.overlapBeforeFrom, p.overlapBeforeTo],
    after: [p.overlapFrom, p.overlapTo],
  }
}

// Any continues-* the screen binds on the chart. PriceChart declares none since 0ede08c, so a binding
// left behind reaches the (stubbed) chart as an attribute, kebab- or camel-case.
function continuesAttrs(chart: ChartLike): string[] {
  return Object.keys(chart.attributes()).filter((name) => /continues/i.test(name))
}

describe('chart load', () => {
  it('one page: one index read and one bar read of limit 1,050; no label, no overlap, no counts', async () => {
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    expect(pagesCalls()).toHaveLength(1)
    expect(pagesCalls()[0]![1]).toMatchObject({ start: undefined, end: undefined, size: 1000, overlap: 50 })
    expect(pagesCalls()[0]![1].signal).toBeInstanceOf(AbortSignal)
    expect(chartCalls()).toHaveLength(1)
    expect(chartCalls()[0]![1]).toMatchObject({ limit: 1050, start: new Date(T0), end: undefined })
    expect(chartCalls()[0]![1].cursor).toBeUndefined()
    const chart = shell.findComponent(PriceChart)
    const bars = chart.props('bars') as Bar[]
    expect(bars).toHaveLength(3)
    expect(isReactive(bars)).toBe(false)
    expect(chart.props('warmupBars')).toEqual([])
    expect(chart.props('smaPeriods')).toEqual([20, 50])
    expect(chart.props('showVolume')).toBe(true)
    expect(overlapsOf(chart)).toEqual({ before: [null, null], after: [null, null] })
    expect(continuesAttrs(chart)).toEqual([])
    const nav = shell.findComponent(ZoomNavigator)
    expect(nav.props('values')).toEqual(Float64Array.from([100.5, 101.5, 102.5]))
    expect(nav.props('totalBars')).toBeUndefined()
    expect(nav.props('label')).toBe('')
    expect(shell.find('[data-testid="footer-summary"]').exists()).toBe(false)
    expect(shell.text()).not.toMatch(/truncat|first [\d,]+ bars/i)
    // A one-page window has no neighbour: nothing more is read after the settle.
    await vi.advanceTimersByTimeAsync(1000)
    expect(chartCalls()).toHaveLength(1)
  })

  it('opens on the NEWEST page with its older neighbour; Page 3 of 3; an overlap on the left only', async () => {
    dataset(2500)
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    // The newest page starts at bar 1,500; its older neighbour (bar 500) is read with it, both 1,050.
    expect(barReads()).toEqual([1500, 500])
    expect(chartCalls().map(([, p]) => p.limit)).toEqual([1050, 1050])
    const chart = shell.findComponent(PriceChart)
    expect(closesOf(chart.props('bars') as Bar[])).toEqual(span(1500, 2500))
    // The averages warm up on the neighbour's last 50 bars, which are not drawn.
    expect(closesOf(chart.props('warmupBars') as Bar[])).toEqual(span(1450, 1500))
    expect(overlapsOf(chart)).toEqual({ before: [0, 49], after: [null, null] })
    expect(continuesAttrs(chart)).toEqual([])
    const nav = shell.findComponent(ZoomNavigator)
    expect(nav.props()).toMatchObject({ totalBars: 2500, pageOffset: 1500, pageSize: 1000, label: 'Page 3 of 3', loading: false })
    expect((nav.props('values') as Float64Array).length).toBe(1000)
    expect(shell.find('[data-testid="footer-summary"]').exists()).toBe(false)
  })

  it('the label carries thousands separators', async () => {
    dataset(1_234_000)
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    expect(shell.findComponent(ZoomNavigator).props('label')).toBe('Page 1,234 of 1,234')
    expect(barReads()).toEqual([1_233_000, 1_232_000])
  })

  it('a range sends the half-open window as the zone day starts to both reads, and a change refetches the index once', async () => {
    dataset(30)
    const shell = await viewerAt('/data/datasets/ds-1/viewer?from=2026-09-01&to=2026-09-08')
    expect(pagesCalls()).toHaveLength(1)
    const index = pagesCalls()[0]![1]
    expect(index.start!.toISOString()).toBe('2026-09-01T04:00:00.000Z')
    expect(index.end!.toISOString()).toBe('2026-09-08T04:00:00.000Z')
    expect(chartCalls()[0]![1].end!.toISOString()).toBe('2026-09-08T04:00:00.000Z')
    expect((shell.findComponent(PriceChart).props('bars') as Bar[]).length).toBe(7)
    expect(shell.find('[data-testid="footer-summary"]').exists()).toBe(false)

    const to = shell.find('[data-testid="range-to"]')
    await to.setValue('2026/09/15')
    await to.trigger('change')
    await flushPromises()
    expect(router.currentRoute.value.query).toMatchObject({ from: '2026-09-01', to: '2026-09-15' })
    expect(pagesCalls()).toHaveLength(2)
    expect(pagesCalls()[1]![1].end!.toISOString()).toBe('2026-09-15T04:00:00.000Z')
    expect((shell.findComponent(PriceChart).props('bars') as Bar[]).length).toBe(14)
  })

  it('a From/To change mid-load aborts what is in flight; the old window\'s page is never shown', async () => {
    dataset(2500)
    server.hold.add(1500).add(500)
    const shell = await viewerAt('/data/datasets/ds-1/viewer?from=2026-09-01')
    const stale = chartCalls().map(([, p]) => p.signal!)
    expect(stale).toHaveLength(2)
    expect(shell.find('[data-testid="chart-loading"]').exists()).toBe(true)
    const to = shell.find('[data-testid="range-to"]')
    await to.setValue('2026/09/11')
    await to.trigger('change')
    await flushPromises()
    expect(stale.every((signal) => signal.aborted)).toBe(true)
    expect(pagesCalls()).toHaveLength(2)
    server.release()
    await flushPromises()
    // The new window [bar 0, bar 10) is one page; nothing of the 2,500-bar window reached the chart.
    const chart = shell.findComponent(PriceChart)
    expect(closesOf(chart.props('bars') as Bar[])).toEqual(span(0, 10))
    expect(shell.findComponent(ZoomNavigator).props('label')).toBe('')
  })

  it('end at or before start shows the problem and fetches nothing', async () => {
    const shell = await viewerAt('/data/datasets/ds-1/viewer?from=2026-09-08&to=2026-09-08')
    expect(chartCalls()).toHaveLength(0)
    expect(pagesCalls()).toHaveLength(0)
    expect(shell.find('[data-testid="range-problem"]').text()).toBe('To must be after From (To is not included).')
    expect(shell.find('[data-testid="chart-empty"]').exists()).toBe(true)
  })

  it('a malformed typed day is refused in place and not written to the URL', async () => {
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    const from = shell.find('[data-testid="range-from"]')
    await from.setValue('2026/02/30')
    await from.trigger('change')
    await flushPromises()
    expect(shell.find('[data-testid="range-problem"]').text()).toBe('From must be a date as YYYY/MM/DD.')
    expect(router.currentRoute.value.query.from).toBeUndefined()
    expect(pagesCalls()).toHaveLength(1)
    expect(chartCalls()).toHaveLength(1)
  })

  it('a first page that fails shows its error id in the chart panel and Try Again reads it again', async () => {
    server.failOnce.set(0, new ApiError({ status: 500, errorId: 'b-1' }))
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    const panel = shell.find('[data-testid="chart-error"]')
    expect(panel.text()).toContain('Error ID: b-1')
    expect(panel.find('button').text()).toBe('Try Again')
    await panel.find('button').trigger('click')
    await flushPromises()
    expect(chartCalls()).toHaveLength(2)
    expect(pagesCalls()).toHaveLength(1)
    expect(shell.find('[data-testid="chart-error"]').exists()).toBe(false)
    expect((shell.findComponent(PriceChart).props('bars') as Bar[]).length).toBe(3)
  })

  it('an index that fails shows its error id and Try Again fetches the index again', async () => {
    mocks.getDatasetBarPages.mockRejectedValueOnce(new ApiError({ status: 503, errorId: 'p-1' }))
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    const panel = shell.find('[data-testid="chart-error"]')
    expect(panel.text()).toContain('Error ID: p-1')
    expect(chartCalls()).toHaveLength(0)
    await panel.find('button').trigger('click')
    await flushPromises()
    expect(pagesCalls()).toHaveLength(2)
    expect(shell.findComponent(PriceChart).exists()).toBe(true)
  })

  it('a range with no bars says so and reads no bars', async () => {
    const shell = await viewerAt('/data/datasets/ds-1/viewer?from=2030-01-01')
    expect(pagesCalls()).toHaveLength(1)
    expect(chartCalls()).toHaveLength(0)
    expect(shell.find('[data-testid="chart-empty"]').text()).toBe('No bars in this range.')
    expect(shell.findComponent(PriceChart).exists()).toBe(false)
    // The export is a date range of the stored bars, not of this window: it stays available.
    expect(shell.find('[data-testid="export-csv"]').attributes('disabled')).toBeUndefined()
  })

  it('a reconnect of the events socket reloads the page model: the index and the newest page again', async () => {
    dataset(2500)
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    expect(pagesCalls()).toHaveLength(1)
    fakeEvents.reconnect()
    await flushPromises()
    expect(pagesCalls()).toHaveLength(2)
    expect(barReads()).toEqual([1500, 500, 1500, 500])
    expect(mocks.getDataset).toHaveBeenCalledTimes(1)
    expect(closesOf(shell.findComponent(PriceChart).props('bars') as Bar[])).toEqual(span(1500, 2500))
  })

  it('never polls: no interval is set, and after the neighbour preload nothing is read while the screen sits', async () => {
    const interval = vi.spyOn(globalThis, 'setInterval')
    dataset(2500)
    await viewerAt('/data/datasets/ds-1/viewer')
    await vi.advanceTimersByTimeAsync(150)
    // The newest page's neighbours: the older one rode along, the newer one does not exist.
    expect(barReads()).toEqual([1500, 500])
    await vi.advanceTimersByTimeAsync(60_000)
    await flushPromises()
    expect(interval).not.toHaveBeenCalled()
    expect(barReads()).toEqual([1500, 500])
    expect(pagesCalls()).toHaveLength(1)
    expect(mocks.getDataset).toHaveBeenCalledTimes(1)
  })
})

describe('paging (tj-grna9p.124)', () => {
  const nav = (shell: VueWrapper) => shell.findComponent(ZoomNavigator)
  const chart = (shell: VueWrapper) => shell.findComponent(PriceChart)
  const shownCloses = (shell: VueWrapper) => closesOf(chart(shell).props('bars') as Bar[])

  async function newest(): Promise<VueWrapper> {
    dataset(2500)
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    expect(barReads()).toEqual([1500, 500])
    return shell
  }

  it('a navigator drag is debounced 150 ms: only where it rests is read, once', async () => {
    const shell = await newest()
    nav(shell).vm.$emit('seek', { from: 900, to: 1100, direction: 'left' })
    await vi.advanceTimersByTimeAsync(100)
    nav(shell).vm.$emit('seek', { from: 400, to: 600, direction: 'left' })
    await vi.advanceTimersByTimeAsync(100)
    nav(shell).vm.$emit('seek', { from: 100, to: 300, direction: 'left' })
    await flushPromises()
    expect(nav(shell).props('loading')).toBe(true)
    await vi.advanceTimersByTimeAsync(149)
    expect(barReads()).toEqual([1500, 500])
    await vi.advanceTimersByTimeAsync(1)
    await flushPromises()
    // [100, 300] is page 0, the earliest: read once, with no older neighbour.
    expect(barReads()).toEqual([1500, 500, 0])
    expect(chartCalls().at(-1)![1].limit).toBe(1050)
    expect(shownCloses(shell)).toEqual(span(0, 1050))
    expect(chart(shell).props('warmupBars')).toEqual([])
    expect(chart(shell).props('visibleRange')).toEqual({ from: 100, to: 300 })
    // The earliest page: no previous page, the next one (r = 500) shares bars 500..1,049.
    expect(overlapsOf(chart(shell))).toEqual({ before: [null, null], after: [500, 1049] })
    expect(nav(shell).props()).toMatchObject({ pageOffset: 0, label: 'Page 1 of 3', loading: false })
  })

  it('stepping back to a page already read makes no request', async () => {
    const shell = await newest()
    nav(shell).vm.$emit('seek', { from: 100, to: 300, direction: 'jump' })
    await flushPromises()
    expect(barReads()).toEqual([1500, 500, 0])
    nav(shell).vm.$emit('seek', { from: 1600, to: 1800, direction: 'right' })
    await vi.advanceTimersByTimeAsync(150)
    await flushPromises()
    expect(barReads()).toEqual([1500, 500, 0])
    expect(shownCloses(shell)).toEqual(span(1500, 2500))
    expect(chart(shell).props('visibleRange')).toEqual({ from: 100, to: 300 })
    expect(nav(shell).props('label')).toBe('Page 3 of 3')
  })

  it('the chart scrolled past its left edge moves page at once, keeping the leading edge', async () => {
    const shell = await newest()
    chart(shell).vm.$emit('update:visibleRange', { from: -20, to: 180 })
    await flushPromises()
    // Global [1480, 1680] belongs to page 1 (bars 500..1549), cached as the warm-up companion: shown
    // with no read of its own; its own companion, page 0, is read with it.
    expect(barReads()).toEqual([1500, 500, 0])
    expect(shownCloses(shell)).toEqual(span(500, 1550))
    expect(closesOf(chart(shell).props('warmupBars') as Bar[])).toEqual(span(450, 500))
    expect(chart(shell).props('visibleRange')).toEqual({ from: 849, to: 1049 })
    // A middle page: both neighbours share bars with it.
    expect(overlapsOf(chart(shell))).toEqual({ before: [0, 549], after: [1000, 1049] })
    expect(nav(shell).props('label')).toBe('Page 2 of 3')
  })

  it('a scroll inside the page only moves the window', async () => {
    const shell = await newest()
    chart(shell).vm.$emit('update:visibleRange', { from: 10, to: 210 })
    await flushPromises()
    expect(barReads()).toEqual([1500, 500])
    expect(nav(shell).props('visibleRange')).toEqual({ from: 10, to: 210 })
    expect(nav(shell).props('pageOffset')).toBe(1500)
  })

  it('while a page loads the navigator shows it loading and the plot keeps the old page', async () => {
    const shell = await newest()
    server.hold.add(0)
    nav(shell).vm.$emit('seek', { from: 100, to: 300, direction: 'jump' })
    await flushPromises()
    expect(nav(shell).props('loading')).toBe(true)
    expect(shownCloses(shell)).toEqual(span(1500, 2500))
    expect(shell.find('[data-testid="chart-loading"]').exists()).toBe(false)
    server.release()
    await flushPromises()
    expect(nav(shell).props('loading')).toBe(false)
    expect(shownCloses(shell)).toEqual(span(0, 1050))
  })

  it('a rapid second jump aborts the first page read; only the last page is shown', async () => {
    const shell = await newest()
    server.hold.add(0)
    nav(shell).vm.$emit('seek', { from: 100, to: 300, direction: 'jump' })
    await flushPromises()
    const first = chartCalls().at(-1)![1].signal!
    chart(shell).vm.$emit('update:visibleRange', { from: 800, to: 1100 })
    await flushPromises()
    expect(first.aborted).toBe(true)
    server.release()
    await flushPromises()
    expect(shownCloses(shell)).toEqual(span(1500, 2500))
  })

  it('a page that fails keeps the old plot, says why with its error id, and Try Again reads it again', async () => {
    const shell = await newest()
    server.failOnce.set(0, new ApiError({ status: 500, errorId: 'b-2' }))
    nav(shell).vm.$emit('seek', { from: 100, to: 300, direction: 'jump' })
    await flushPromises()
    const notice = shell.find('[data-testid="chart-page-error"]')
    expect(notice.text()).toContain('Could not load bars.')
    expect(notice.text()).toContain('Error ID: b-2')
    expect(shell.find('[data-testid="chart-error"]').exists()).toBe(false)
    expect(shownCloses(shell)).toEqual(span(1500, 2500))
    await notice.find('button').trigger('click')
    await flushPromises()
    expect(barReads()).toEqual([1500, 500, 0, 0])
    expect(shell.find('[data-testid="chart-page-error"]').exists()).toBe(false)
    expect(shownCloses(shell)).toEqual(span(0, 1050))
  })

  it('after the page settles (150 ms) the missing neighbour is preloaded, so the next step needs no wait', async () => {
    dataset(5000) // pages at 0, 1,000, 2,000, 3,000 and 4,000
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    await vi.advanceTimersByTimeAsync(150) // the newest page's settle: page 3 is already there
    nav(shell).vm.$emit('seek', { from: 1100, to: 1300, direction: 'jump' })
    await flushPromises()
    expect(barReads()).toEqual([4000, 3000, 1000, 0])
    await vi.advanceTimersByTimeAsync(149)
    expect(barReads()).toHaveLength(4)
    await vi.advanceTimersByTimeAsync(1)
    await flushPromises()
    // Page 1's neighbours: page 0 rode along as its warm-up, page 2 is preloaded now.
    expect(barReads()).toEqual([4000, 3000, 1000, 0, 2000])
    expect(nav(shell).props('loading')).toBe(false)
    nav(shell).vm.$emit('seek', { from: 2100, to: 2300, direction: 'jump' })
    await flushPromises()
    expect(barReads()).toHaveLength(5)
    expect(shownCloses(shell)).toEqual(span(2000, 3050))
  })

  it('Show Request lists the Pages, Chart and Rows requests; Chart names the shown page, limit 1,050', async () => {
    dataset(2500)
    const shell = await viewerAt('/data/datasets/ds-1/viewer?from=2026-09-01')
    await shell.findAll('button').find((b) => b.text() === 'Show Request')!.trigger('click')
    const pageStart = encodeURIComponent(new Date(T0 + 1500 * DAY).toISOString())
    expect(shell.find('[data-testid="viewer-request"]').text()).toBe(
      'Pages  GET /api/store/ui/v1/datasets/ds-1/bar-pages?start=2026-09-01T04%3A00%3A00.000Z&size=1000&overlap=50\n' +
        `Chart  GET /api/store/ui/v1/datasets/ds-1/bars?start=${pageStart}&limit=1050\n` +
        'Rows   GET /api/store/ui/v1/datasets/ds-1/bars?start=2026-09-01T04%3A00%3A00.000Z&limit=500',
    )
  })
})

// tj-grna9p.129 (owner 2026-10-13, canvas 'A middle page'): PAGE navigation (the navigator's arrows,
// hatch click and Page Up / Page Down / Home / End -> a seek with direction 'page'; since f8b620b the
// chart has no page controls of its own) loads the target page and shows it WHOLE, its look-ahead included; the
// page on screen stays, dimmed, until the target lands, and only then is the range applied. Moving
// inside the page keeps the zoom. The overlap the next page shares reaches both chart and navigator.
describe('page navigation (tj-grna9p.129)', () => {
  const nav = (shell: VueWrapper) => shell.findComponent(ZoomNavigator)
  const chart = (shell: VueWrapper) => shell.findComponent(PriceChart)
  const shownCloses = (shell: VueWrapper) => closesOf(chart(shell).props('bars') as Bar[])
  const wrap = (shell: VueWrapper) => shell.find('[data-testid="chart-wrap"]')

  async function newest(): Promise<VueWrapper> {
    dataset(2500) // pages at 0, 500 and 1,500
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    expect(barReads()).toEqual([1500, 500])
    return shell
  }

  // f8b620b removed the chart's chevrons and its page-navigate event (owner: "Only the scroll bar should
  // have them"). The same page-move rules are driven here through the REAL navigator: its Previous / Next
  // arrows and Page Up / Page Down on its window, end to end into the screen's page model.
  async function newestWithNavigator(): Promise<VueWrapper> {
    dataset(2500) // pages at 0, 500 and 1,500
    const shell = await viewerAt('/data/datasets/ds-1/viewer', { realNavigator: true })
    expect(barReads()).toEqual([1500, 500])
    return shell
  }
  const navWindow = (shell: VueWrapper) => shell.find('.zoom-navigator__window')

  it('the screen no longer listens for a chart page-navigate: the chart has no page controls', async () => {
    const shell = await newest()
    // A listener the stub does not declare as an emit would land in $attrs; the screen passes none.
    expect(Object.keys(chart(shell).vm.$attrs)).not.toContain('onPageNavigate')
    expect(Object.keys(chart(shell).vm.$attrs)).toContain('onHover')
    chart(shell).vm.$emit('update:visibleRange', { from: 300, to: 400 })
    await flushPromises()
    chart(shell).vm.$emit('page-navigate', { direction: 'before' })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(0)
    expect(barReads()).toEqual([1500, 500])
    expect(chart(shell).props('visibleRange')).toEqual({ from: 300, to: 400 })
    expect(nav(shell).props('label')).toBe('Page 3 of 3')
  })

  it('the navigator\'s Previous arrow, zoomed in, shows page 2 of 3 whole, look-ahead included, at once', async () => {
    const shell = await newestWithNavigator()
    chart(shell).vm.$emit('update:visibleRange', { from: 300, to: 400 }) // zoomed in first
    await flushPromises()
    await shell.find('[data-testid="zoom-navigator-prev"]').trigger('click')
    await flushPromises() // no timer advanced: a page move is not debounced
    // Page 1 was cached as the warm-up companion; its own companion (page 0) is read with it.
    expect(barReads()).toEqual([1500, 500, 0])
    expect(shownCloses(shell)).toEqual(span(500, 1550))
    expect(chart(shell).props('visibleRange')).toEqual({ from: 0, to: 1049 })
    expect(nav(shell).props('visibleRange')).toEqual({ from: 0, to: 1049 })
    expect(nav(shell).props('label')).toBe('Page 2 of 3')
  })

  it('Page Up on the navigator\'s window does the same as its Previous arrow', async () => {
    const shell = await newestWithNavigator()
    chart(shell).vm.$emit('update:visibleRange', { from: 300, to: 400 })
    await flushPromises()
    await navWindow(shell).trigger('keydown', { key: 'PageUp' })
    await flushPromises()
    expect(barReads()).toEqual([1500, 500, 0])
    expect(shownCloses(shell)).toEqual(span(500, 1550))
    expect(chart(shell).props('visibleRange')).toEqual({ from: 0, to: 1049 })
    expect(nav(shell).props('label')).toBe('Page 2 of 3')
  })

  it('from the earliest page (r = 500) the Next arrow and Page Down each move one page, whole', async () => {
    const shell = await newestWithNavigator()
    await navWindow(shell).trigger('keydown', { key: 'Home' })
    await flushPromises()
    expect(nav(shell).props('label')).toBe('Page 1 of 3')
    expect(shownCloses(shell)).toEqual(span(0, 1050))
    await shell.find('[data-testid="zoom-navigator-next"]').trigger('click')
    await flushPromises()
    expect(shownCloses(shell)).toEqual(span(500, 1550))
    expect(chart(shell).props('visibleRange')).toEqual({ from: 0, to: 1049 })
    expect(nav(shell).props('label')).toBe('Page 2 of 3')
    await navWindow(shell).trigger('keydown', { key: 'PageDown' })
    await flushPromises()
    expect(shownCloses(shell)).toEqual(span(1500, 2500))
    expect(chart(shell).props('visibleRange')).toEqual({ from: 0, to: 999 })
    expect(nav(shell).props('label')).toBe('Page 3 of 3')
  })

  it('no page after the newest: Next is disabled and Page Down reads nothing and keeps the zoom', async () => {
    const shell = await newestWithNavigator()
    chart(shell).vm.$emit('update:visibleRange', { from: 300, to: 400 })
    await flushPromises()
    expect(shell.find('[data-testid="zoom-navigator-next"]').attributes('disabled')).toBeDefined()
    expect(shell.find('[data-testid="zoom-navigator-prev"]').attributes('disabled')).toBeUndefined()
    await navWindow(shell).trigger('keydown', { key: 'PageDown' })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(0)
    expect(barReads()).toEqual([1500, 500])
    expect(chart(shell).props('visibleRange')).toEqual({ from: 300, to: 400 })
    expect(nav(shell).props('label')).toBe('Page 3 of 3')
  })

  it('a navigator page seek is not debounced: the page is shown whole at once', async () => {
    const shell = await newest()
    nav(shell).vm.$emit('seek', { from: 1499, to: 1499, direction: 'page' })
    await flushPromises() // no timer advanced
    expect(shownCloses(shell)).toEqual(span(500, 1550))
    expect(chart(shell).props('visibleRange')).toEqual({ from: 0, to: 1049 })
  })

  it('a page seek onto the page on screen (Home / End) zooms out to the whole page with no read', async () => {
    const shell = await newest()
    chart(shell).vm.$emit('update:visibleRange', { from: 300, to: 400 })
    await flushPromises()
    nav(shell).vm.$emit('seek', { from: 2499, to: 2499, direction: 'page' })
    await flushPromises()
    expect(barReads()).toEqual([1500, 500])
    expect(chart(shell).props('visibleRange')).toEqual({ from: 0, to: 999 })
    expect(nav(shell).props('pageOffset')).toBe(1500)
  })

  it('the page on screen stays, dimmed, with its own range until the target lands; then the whole page', async () => {
    const shell = await newest()
    chart(shell).vm.$emit('update:visibleRange', { from: 300, to: 400 })
    await flushPromises()
    expect(wrap(shell).classes()).not.toContain('chart-wrap--loading')
    server.hold.add(0)
    nav(shell).vm.$emit('seek', { from: 100, to: 100, direction: 'page' }) // the hatch, page 0
    await flushPromises()
    expect(wrap(shell).classes()).toContain('chart-wrap--loading')
    expect(nav(shell).props('loading')).toBe(true)
    // No flash: neither the bars nor the range of the target reach the chart before it lands.
    expect(shownCloses(shell)).toEqual(span(1500, 2500))
    expect(chart(shell).props('visibleRange')).toEqual({ from: 300, to: 400 })
    server.release()
    await flushPromises()
    expect(wrap(shell).classes()).not.toContain('chart-wrap--loading')
    expect(shownCloses(shell)).toEqual(span(0, 1050))
    expect(chart(shell).props('visibleRange')).toEqual({ from: 0, to: 1049 })
  })

  it('moving inside the page keeps the zoom; a drag past the edge keeps the width (not the whole page)', async () => {
    const shell = await newest()
    nav(shell).vm.$emit('update:visibleRange', { from: 10, to: 60 })
    await flushPromises()
    expect(chart(shell).props('visibleRange')).toEqual({ from: 10, to: 60 })
    expect(barReads()).toEqual([1500, 500])
    nav(shell).vm.$emit('seek', { from: 1400, to: 1450, direction: 'left' })
    await vi.advanceTimersByTimeAsync(150)
    await flushPromises()
    expect(nav(shell).props('label')).toBe('Page 2 of 3')
    expect(chart(shell).props('visibleRange')).toEqual({ from: 900, to: 950 })
  })

  it('the overlap the next page shares reaches the chart and the navigator; none on the newest page', async () => {
    const shell = await newest()
    const overlapOf = (c: { props(): object }) => {
      const props = c.props() as { overlapFrom?: number | null; overlapTo?: number | null }
      return [props.overlapFrom, props.overlapTo]
    }
    expect(overlapOf(chart(shell))).toEqual([null, null])
    expect(overlapOf(nav(shell))).toEqual([null, null])
    // The navigator's Previous arrow from page 3 (offset 1,500) asks for bar 1,499.
    nav(shell).vm.$emit('seek', { from: 1499, to: 1499, direction: 'page' })
    await flushPromises()
    expect(overlapOf(chart(shell))).toEqual([1000, 1049])
    expect(overlapOf(nav(shell))).toEqual([1000, 1049])
    nav(shell).vm.$emit('seek', { from: 499, to: 499, direction: 'page' })
    await flushPromises()
    // The earliest page of a window with r = 500: from bar 500 to its last.
    expect(overlapOf(chart(shell))).toEqual([500, 1049])
    expect(overlapOf(nav(shell))).toEqual([500, 1049])
  })

  // f8b620b (owner: "only the right (latest) side of the graph has the overlap highlight"): the bars the
  // PREVIOUS page shows too reach the chart as overlapBeforeFrom / overlapBeforeTo (overlapBeforeRange).
  it('the overlap the previous page shares reaches the chart on the left; none on the earliest page', async () => {
    const shell = await newest()
    const beforeOf = () => [chart(shell).props('overlapBeforeFrom'), chart(shell).props('overlapBeforeTo')]
    // Page 3 (offset 1,500): page 2 (500..1,549) reads 50 bars into it.
    expect(beforeOf()).toEqual([0, 49])
    nav(shell).vm.$emit('seek', { from: 1499, to: 1499, direction: 'page' })
    await flushPromises()
    // Page 2 (offset 500): the earliest page (r = 500) shows bars 0..1,049, so 550 of them.
    expect(beforeOf()).toEqual([0, 549])
    expect([chart(shell).props('overlapFrom'), chart(shell).props('overlapTo')]).toEqual([1000, 1049])
    nav(shell).vm.$emit('seek', { from: 0, to: 0, direction: 'page' })
    await flushPromises()
    expect(beforeOf()).toEqual([null, null])
  })

  it('session markers are on for every intraday granularity and off for daily, weekly and monthly', async () => {
    const cases: Array<[Granularity, boolean]> = [
      [Granularity.ONE_MINUTE, true],
      [Granularity.FIVE_MINUTES, true],
      [Granularity.THIRTY_MINUTES, true],
      [Granularity.ONE_HOUR, true],
      [Granularity.ONE_DAY, false],
      [Granularity.ONE_WEEK, false],
      [Granularity.ONE_MONTH, false],
    ]
    for (const [granularity, expected] of cases) {
      mocks.getDataset.mockResolvedValue(summary({ granularity }))
      const shell = await viewerAt(`/data/datasets/ds-g${granularity}/viewer`)
      expect(chart(shell).props('sessionMarkers'), Granularity[granularity]).toBe(expected)
      shell.unmount()
      mounted.splice(mounted.indexOf(shell), 1)
    }
  })

  // 2b6e122 (owner, real app): a page move ended zoomed in. The page lands on a NEW chart (keyed), whose
  // library reports a transient range of its own in the frame that applies the page's range, then the
  // applied one. Here the real PriceChart runs over the recording createChart and the test plays that
  // frame; the navigator (the parent's one range) must end on the whole page, not the transient.
  it('a page seek ends on the whole page after the chart reports a transient then the applied range', async () => {
    lwc.charts.length = 0
    dataset(2500) // pages at 0, 500 and 1,500
    const shell = await viewerAt('/data/datasets/ds-1/viewer', { realChart: true })
    nav(shell).vm.$emit('seek', { from: 1499, to: 1499, direction: 'page' })
    await flushPromises()
    const pageLength = (chart(shell).props('bars') as Bar[]).length
    expect(pageLength).toBe(1050)
    const whole = { from: 0, to: pageLength - 1 }
    const fake = lwc.charts.at(-1)!
    expect(fake.setVisibleLogicalRange).toHaveBeenLastCalledWith(whole)
    // The library's frame: its default bar spacing (inside the page, so no seek), then the applied range.
    const transient = { from: pageLength - 101, to: pageLength - 1 }
    fake.visible = transient
    fake.rangeHandler!(transient)
    fake.visible = whole
    fake.rangeHandler!(whole)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(200)
    expect(nav(shell).props('visibleRange')).toEqual(whole)
    expect(chart(shell).props('visibleRange')).toEqual(whole)
    expect(fake.visible).toEqual(whole)
    expect(nav(shell).props('label')).toBe('Page 2 of 3')
  })

  // 389fa4f: the scroll bar mirrors the chart's left band. The navigator gets the same
  // overlapBeforeRange(index, k) bounds as the chart on every page >= 1, and null on page 0.
  it('the overlap the previous page shares reaches the navigator too; none on the earliest page', async () => {
    const shell = await newest()
    const navBefore = () => [nav(shell).props('overlapBeforeFrom'), nav(shell).props('overlapBeforeTo')]
    const chartBefore = () => [chart(shell).props('overlapBeforeFrom'), chart(shell).props('overlapBeforeTo')]
    // Page 3 (offset 1,500): page 2 (500..1,549) reads 50 bars into it.
    expect(navBefore()).toEqual([0, 49])
    expect(navBefore()).toEqual(chartBefore())
    nav(shell).vm.$emit('seek', { from: 1499, to: 1499, direction: 'page' })
    await flushPromises()
    expect(nav(shell).props('label')).toBe('Page 2 of 3')
    // Page 2 (offset 500): the earliest page (r = 500) shows bars 0..1,049, so 550 of them.
    expect(navBefore()).toEqual([0, 549])
    expect(navBefore()).toEqual(chartBefore())
    // The right sliver is fed independently on the same page.
    expect([nav(shell).props('overlapFrom'), nav(shell).props('overlapTo')]).toEqual([1000, 1049])
    nav(shell).vm.$emit('seek', { from: 0, to: 0, direction: 'page' })
    await flushPromises()
    expect(nav(shell).props('label')).toBe('Page 1 of 3')
    expect(navBefore()).toEqual([null, null])
  })

  it('the real navigator draws the left sliver on the newest page and both on a middle page', async () => {
    const shell = await newestWithNavigator()
    const left = () => shell.find('[data-testid="zoom-navigator-overlap-before"]')
    const right = () => shell.find('[data-testid="zoom-navigator-overlap"]')
    expect(left().exists()).toBe(true)
    expect(right().exists()).toBe(false)
    await shell.find('[data-testid="zoom-navigator-prev"]').trigger('click')
    await flushPromises()
    expect(left().exists()).toBe(true)
    expect(right().exists()).toBe(true)
    await navWindow(shell).trigger('keydown', { key: 'Home' })
    await flushPromises()
    expect(nav(shell).props('label')).toBe('Page 1 of 3')
    expect(left().exists()).toBe(false)
    expect(right().exists()).toBe(true)
  })

  it('a one-page window has no overlap on either side', async () => {
    dataset(800)
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    expect([chart(shell).props('overlapFrom'), chart(shell).props('overlapTo')]).toEqual([null, null])
    expect([chart(shell).props('overlapBeforeFrom'), chart(shell).props('overlapBeforeTo')]).toEqual([null, null])
    expect([nav(shell).props('overlapBeforeFrom'), nav(shell).props('overlapBeforeTo')]).toEqual([null, null])
    expect([nav(shell).props('overlapFrom'), nav(shell).props('overlapTo')]).toEqual([null, null])
    expect(continuesAttrs(chart(shell))).toEqual([])
  })

  // 0ede08c (owner: "there should only be one overlap highlight"): the chart's hatched cut edge is gone,
  // and the screen hands it no continues-before / continues-after on any page (PriceChart declares
  // neither, so a binding left behind would fall through as an attribute).
  it('no page hands the chart a continues-before / continues-after', async () => {
    const shell = await newest()
    expect(continuesAttrs(chart(shell))).toEqual([])
    for (const bar of [1499, 499]) {
      nav(shell).vm.$emit('seek', { from: bar, to: bar, direction: 'page' })
      await flushPromises()
      expect(continuesAttrs(chart(shell)), String(bar)).toEqual([])
    }
    expect(nav(shell).props('label')).toBe('Page 1 of 3')
  })
})

describe('sidebar controls', () => {
  it('Split, All and Compare cannot be activated and each shows the reason', async () => {
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    const reason = 'Corporate actions are not collected yet.'
    for (const which of ['split', 'all']) {
      const chip = shell.find(`[data-adjustment="${which}"]`)
      expect(chip.attributes('disabled')).toBeDefined()
      expect(chip.attributes('title')).toBe(reason)
    }
    expect(shell.find('[data-adjustment="raw"]').attributes('aria-pressed')).toBe('true')
    expect((shell.find('[data-overlay="compare"]').element as HTMLInputElement).disabled).toBe(true)
    expect(shell.find('[data-testid="adjustment-reason"]').text()).toContain(reason)
  })

  it('SMA and Volume toggles reach the chart', async () => {
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    await shell.find('[data-overlay="sma-20"]').setValue(false)
    await shell.find('[data-overlay="volume"]').setValue(false)
    const chart = shell.findComponent(PriceChart)
    expect(chart.props('smaPeriods')).toEqual([50])
    expect(chart.props('showVolume')).toBe(false)
  })

  it('granularity switches to a sibling dataset keeping the range; widths with no sibling are disabled', async () => {
    const shell = await viewerAt('/data/datasets/ds-1/viewer?from=2026-09-01')
    expect(shell.find('[data-granularity="1d"]').attributes('aria-pressed')).toBe('true')
    expect(shell.find('[data-granularity="1m"]').attributes('disabled')).toBeDefined()
    expect(shell.find('[data-granularity="1m"]').attributes('title')).toBe('No dataset at this width.')
    await shell.find('[data-granularity="1h"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.params.id).toBe('ds-1h')
    // tj-grna9p.109: the switch stays on the Viewer; it never lands on the sibling's detail.
    expect(router.currentRoute.value.name).toBe('data-dataset-viewer')
    expect(router.currentRoute.value.query.from).toBe('2026-09-01')
    expect(mocks.getDataset).toHaveBeenLastCalledWith('ds-1h', expect.any(AbortSignal))
  })
})

// tj-grna9p.126 on the screen: the header button opens the popover on the Viewer's own window and the
// export walks the bars by cursor, not the chart's page. The popover's own rules are pinned in
// ExportPopover.spec.ts.
describe('Export CSV', () => {
  it('opens on the current window and downloads exactly [From, To) by cursor, not the chart page', async () => {
    const created: Blob[] = []
    Object.assign(URL, {
      createObjectURL: vi.fn((blob: Blob) => {
        created.push(blob)
        return 'blob:test'
      }),
      revokeObjectURL: vi.fn(),
    })
    const names: string[] = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      names.push(this.download)
    })
    dataset(2500)
    const shell = await viewerAt('/data/datasets/ds-1/viewer?from=2026-09-02&to=2026-09-05')
    const chartReads = chartCalls().length
    await shell.find('[data-testid="export-csv"]').trigger('click')
    expect((shell.find('[data-testid="export-from"]').element as HTMLInputElement).value).toBe('2026/09/02')
    expect((shell.find('[data-testid="export-to"]').element as HTMLInputElement).value).toBe('2026/09/05')
    await shell.find('[data-testid="export-run"]').trigger('click')
    await flushPromises()
    const exportCalls = chartCalls().slice(chartReads)
    expect(exportCalls.map(([, p]) => [p.limit, p.cursor, p.start!.toISOString(), p.end!.toISOString()])).toEqual([
      [50_000, undefined, '2026-09-02T04:00:00.000Z', '2026-09-05T04:00:00.000Z'],
    ])
    expect(names).toEqual(['VFV_1d_2026-09-02_2026-09-05.csv'])
    const text = await created[0]!.text()
    expect(text).toBe(barsToCsv([makeBar(1), makeBar(2), makeBar(3)], 'America/Toronto', false))
    expect(text.split('\n').slice(1, 4)).toEqual([
      '2026/09/02,101,102,100,101.5,1001',
      '2026/09/03,102,103,101,102.5,1002',
      '2026/09/04,103,104,102,103.5,1003',
    ])
    expect(shell.find('[data-testid="export-popover"]').exists()).toBe(false)
  })
})

describe('bar table', () => {
  function getRows(datasource: IDatasource, startRow: number) {
    return new Promise<{ rows?: Bar[]; lastRow?: number }>((resolve) => {
      datasource.getRows({
        startRow,
        endRow: startRow + 500,
        successCallback: (rows: Bar[], lastRow?: number) => resolve({ rows, lastRow }),
        failCallback: () => resolve({}),
      } as unknown as IGetRowsParams)
    })
  }

  it('pages the whole window 500 rows at a time and starts over when the window changes', async () => {
    serveBars(1200)
    const window = { start: new Date(T0) }
    const table = mount(BarTable, {
      props: { datasetId: 'ds-1', timeWindow: window, intraday: false },
      global: { plugins: [createPinia()] },
    })
    mounted.push(table)
    expect(grid.props).toMatchObject({ rowModelType: 'infinite', cacheBlockSize: 500 })
    await getRows(grid.datasources[0]!, 0)
    await getRows(grid.datasources[0]!, 500)
    const last = await getRows(grid.datasources[0]!, 1000)
    expect(last.lastRow).toBe(1200)
    expect(chartCalls().map(([, p]) => [p.cursor, p.limit, p.start])).toEqual([
      [undefined, 500, window.start],
      ['500', 500, window.start],
      ['1000', 500, window.start],
    ])
    expect(table.emitted('loaded')!.at(-1)).toEqual([{ loadedRows: 1200, done: true }])

    await table.setProps({ timeWindow: { start: new Date(T0 + DAY) } })
    expect(grid.datasources).toHaveLength(2)
    await getRows(grid.datasources[1]!, 0)
    expect(chartCalls().at(-1)![1].cursor).toBeUndefined()
  })

  it('dates read YYYY/MM/DD in the zone, with HH:mm for intraday bars', () => {
    const table = mount(BarTable, {
      props: { datasetId: 'ds-1', timeWindow: {}, intraday: true },
      global: { plugins: [createPinia()] },
    })
    mounted.push(table)
    useSettingsStore().setTimeZoneOverride('America/Toronto')
    const date = (grid.props!.columnDefs as ColDef<Bar>[])[0]!
    const getter = date.valueGetter as (p: { data: Bar }) => string
    expect(getter({ data: makeBar(0) })).toBe('2026/09/01 00:00')
    expect(getter({ data: create(BarSchema, {}) })).toBe('—')
  })
})
