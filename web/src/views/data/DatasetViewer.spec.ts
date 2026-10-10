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
import { DatasetSummarySchema, FreshnessStatus } from '@generated/trader_joe/proto/ui/v1/data_pb'
import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'

import { ApiError } from '@/api'
import PriceChart from '@/components/charts/PriceChart.vue'
import ZoomNavigator from '@/components/charts/ZoomNavigator.vue'
import BarTable from '@/components/viewer/BarTable.vue'
import router from '@/router'
import AppShell from '@/shell/AppShell.vue'
import { useSettingsStore } from '@/stores/settings'
import { barsToCsv } from '@/viewer/csv'

// tj-grna9p.33 Data Viewer, mounted in the real shell under the real router, with the API mocked at
// the typed client (@/api).
//
// WHAT THE STUBS HIDE. PriceChart and ZoomNavigator are stubbed on the screen (their own behaviour
// and the createChart mock live in PriceChart.spec.ts): the spec reads the props the screen hands
// them, so it sees which bars, overlays and pane the chart would get, but nothing is drawn. The bar
// table is stubbed on the screen too and tested on its own below over an AG Grid stub that captures
// the datasource; AG Grid's block scheduling and cell rendering are hidden there.
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

const mocks = vi.hoisted(() => ({
  getUiConfig: vi.fn(),
  getDataset: vi.fn(),
  getDatasetBars: vi.fn(),
  getDatasetFacets: vi.fn(),
  listDatasets: vi.fn(),
}))
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

// A bar server holding `total` bars that honours the cursor and the limit, like the store does.
function serveBars(total: number): void {
  mocks.getDatasetBars.mockImplementation(async (_id: string, params: { cursor?: string; limit?: number }) => {
    const offset = params.cursor === undefined ? 0 : Number(params.cursor)
    const count = Math.max(0, Math.min(params.limit ?? 1000, total - offset))
    const bars = Array.from({ length: count }, (_, i) => makeBar(offset + i))
    return { bars, nextCursor: offset + count < total ? String(offset + count) : '' }
  })
}

const mounted: VueWrapper[] = []

async function viewerAt(path: string): Promise<VueWrapper> {
  await router.push(path)
  await router.isReady()
  const shell = mount(AppShell, {
    global: {
      plugins: [createPinia(), router, PrimeVue],
      stubs: { PriceChart: true, ZoomNavigator: true, BarTable: true },
    },
    attachTo: document.body,
  })
  mounted.push(shell)
  await flushPromises()
  return shell
}

const chartCalls = () => mocks.getDatasetBars.mock.calls as Array<[string, Record<string, unknown>]>

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  setActivePinia(createPinia())
  useSettingsStore().setTimeZoneOverride('America/Toronto')
  grid.datasources.length = 0
  mocks.getUiConfig.mockResolvedValue({ allowedGroups: [AccountGroup.SIMULATION], deploymentLabel: '', serverVersion: '' })
  mocks.getDataset.mockResolvedValue(summary())
  serveBars(3)
})

afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount())
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
    expect(shell.find('[data-testid="viewer-meta"]').text()).toContain('—')
  })
})

describe('chart load', () => {
  it('hands the chart the loaded bars, never deeply reactive, with the overlays on', async () => {
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    const chart = shell.findComponent(PriceChart)
    const bars = chart.props('bars') as Bar[]
    expect(bars).toHaveLength(3)
    expect(isReactive(bars)).toBe(false)
    expect(chart.props('smaPeriods')).toEqual([20, 50])
    expect(chart.props('showVolume')).toBe(true)
    expect(shell.findComponent(ZoomNavigator).props('values')).toEqual(Float64Array.from([100.5, 101.5, 102.5]))
    expect(chartCalls()).toHaveLength(1)
    expect(chartCalls()[0]![1]).toMatchObject({ cursor: undefined, limit: 10_000, start: undefined, end: undefined })
  })

  it('stops at 50,000 bars in five requests and says the chart is truncated', async () => {
    mocks.getDataset.mockResolvedValue(summary({ barCount: 73_000n }))
    serveBars(73_000)
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    expect(chartCalls().map(([, p]) => p.limit)).toEqual([10_000, 10_000, 10_000, 10_000, 10_000])
    expect(chartCalls().map(([, p]) => p.cursor)).toEqual([undefined, '10000', '20000', '30000', '40000'])
    expect((shell.findComponent(PriceChart).props('bars') as Bar[]).length).toBe(50_000)
    expect(shell.find('[data-testid="viewer-truncated"]').text()).toContain('first 50,000 bars')
    expect(shell.find('[data-testid="footer-summary"]').text()).toBe('50,000 of 73,000 bars · 1d')
  })

  it('a range sends the half-open window as the zone day starts, and refetches once when it changes', async () => {
    const shell = await viewerAt('/data/datasets/ds-1/viewer?from=2026-09-01&to=2026-09-08')
    expect(chartCalls()).toHaveLength(1)
    const first = chartCalls()[0]![1]
    expect((first.start as Date).toISOString()).toBe('2026-09-01T04:00:00.000Z')
    expect((first.end as Date).toISOString()).toBe('2026-09-08T04:00:00.000Z')
    expect(shell.find('[data-testid="footer-summary"]').text()).toBe('3 bars in range · 1d')

    const to = shell.find('[data-testid="range-to"]')
    await to.setValue('2026/09/15')
    await to.trigger('change')
    await flushPromises()
    expect(router.currentRoute.value.query).toMatchObject({ from: '2026-09-01', to: '2026-09-15' })
    expect(chartCalls()).toHaveLength(2)
    expect((chartCalls()[1]![1].end as Date).toISOString()).toBe('2026-09-15T04:00:00.000Z')
  })

  it('end at or before start shows the problem and fetches nothing', async () => {
    const shell = await viewerAt('/data/datasets/ds-1/viewer?from=2026-09-08&to=2026-09-08')
    expect(chartCalls()).toHaveLength(0)
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
    expect(chartCalls()).toHaveLength(1)
  })

  it('a bar failure shows its error id and Try Again refetches', async () => {
    mocks.getDatasetBars.mockRejectedValueOnce(new ApiError({ status: 500, errorId: 'b-1' }))
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    const panel = shell.find('[data-testid="chart-error"]')
    expect(panel.text()).toContain('Error ID: b-1')
    await panel.find('button').trigger('click')
    await flushPromises()
    expect(chartCalls()).toHaveLength(2)
    expect(shell.findComponent(PriceChart).exists()).toBe(true)
  })

  it('a range with no bars says so', async () => {
    serveBars(0)
    const shell = await viewerAt('/data/datasets/ds-1/viewer?from=2030-01-01')
    expect(shell.find('[data-testid="chart-empty"]').text()).toBe('No bars in this range.')
    expect(shell.find('[data-testid="export-csv"]').attributes('disabled')).toBeDefined()
  })

  it('never polls: no interval is set, and nothing refetches while the screen sits', async () => {
    const interval = vi.spyOn(globalThis, 'setInterval')
    await viewerAt('/data/datasets/ds-1/viewer')
    const before = chartCalls().length
    await new Promise((resolve) => setTimeout(resolve, 50))
    await flushPromises()
    expect(interval).not.toHaveBeenCalled()
    expect(chartCalls()).toHaveLength(before)
    expect(mocks.getDataset).toHaveBeenCalledTimes(1)
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
    expect(router.currentRoute.value.query.from).toBe('2026-09-01')
    expect(mocks.getDataset).toHaveBeenLastCalledWith('ds-1h', expect.any(AbortSignal))
  })
})

describe('Export CSV and Show Request', () => {
  it('exports exactly the loaded bars with YYYY/MM/DD dates, without a request', async () => {
    const created: Blob[] = []
    Object.assign(URL, {
      createObjectURL: vi.fn((blob: Blob) => {
        created.push(blob)
        return 'blob:test'
      }),
      revokeObjectURL: vi.fn(),
    })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.download).toBe('VFV_1d.csv')
    })
    const shell = await viewerAt('/data/datasets/ds-1/viewer')
    const callsBefore = chartCalls().length
    await shell.find('[data-testid="export-csv"]').trigger('click')
    expect(click).toHaveBeenCalledTimes(1)
    expect(chartCalls()).toHaveLength(callsBefore)
    const text = await created[0]!.text()
    expect(text).toBe(barsToCsv([makeBar(0), makeBar(1), makeBar(2)], 'America/Toronto', false))
    expect(text.split('\n').slice(1, 4)).toEqual([
      '2026/09/01,100,101,99,100.5,1000',
      '2026/09/02,101,102,100,101.5,1001',
      '2026/09/03,102,103,101,102.5,1002',
    ])
  })

  it('Show Request shows the chart and rows calls for the window', async () => {
    const shell = await viewerAt('/data/datasets/ds-1/viewer?from=2026-09-01')
    await shell.findAll('button').find((b) => b.text() === 'Show Request')!.trigger('click')
    expect(shell.find('[data-testid="viewer-request"]').text()).toBe(
      'Chart  GET /api/store/ui/v1/datasets/ds-1/bars?start=2026-09-01T04%3A00%3A00.000Z&limit=10000\n' +
        'Rows   GET /api/store/ui/v1/datasets/ds-1/bars?start=2026-09-01T04%3A00%3A00.000Z&limit=500',
    )
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
