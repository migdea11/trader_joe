import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import { create } from '@bufbuild/protobuf'
import type { MessageInitShape } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import { DataSource, Feed, Granularity, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { ClientKind, DatasetSummarySchema } from '@generated/trader_joe/proto/ui/v1/data_pb'
import { DatasetChange } from '@generated/trader_joe/proto/ui/v1/events_pb'
import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'
import {
  LastReadBucketsSchema,
  ReadsPerDaySchema,
  StaleDatasetPageSchema,
  StaleDatasetRowSchema,
  StorageCurrentSchema,
  UsageSummarySchema,
} from '@generated/trader_joe/proto/ui/v1/stats_pb'
import type { StaleDatasetRow } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { fakeEvents } from '@/__tests__/fakeEvents'
import { ApiError } from '@/api'
import { int64ToNumber as realInt64ToNumber } from '@/api/int64'
import { COALESCE_MS } from '@/catalog/useCatalogLiveRefresh'
import { formatBytes } from '@/format/formatters'
import router from '@/router'
import AppShell from '@/shell/AppShell.vue'
import { useSettingsStore } from '@/stores/settings'
import { useUsageStore } from '@/stores/usage'

// tj-grna9p.62: the Usage tab, mounted in the real shell under the real router at /data/usage, with
// its sidebar. The API is mocked at the typed client (@/api), the events socket is the shared fixture,
// and VChart is a stub that keeps its props (no canvas in jsdom). The store, the dialog, the live hook
// and the charts' data are the real ones. Display zone America/Toronto.
//
// WHAT THE MOCKS HIDE: the store's real routes (system suite), a live socket, and a browser's drawing
// of the charts (screenshots, tj-grna9p.94).
vi.mock('@/api/events', async (importOriginal) =>
  (await import('@/__tests__/fakeEvents')).fakeEventsModule(await importOriginal<object>()),
)
const mocks = vi.hoisted(() => ({
  getUiConfig: vi.fn(),
  getDatasetFacets: vi.fn(),
  listDatasets: vi.fn(),
  getUsageSummary: vi.fn(),
  getLastReadBuckets: vi.fn(),
  getReadsPerDay: vi.fn(),
  getStaleDatasets: vi.fn(),
  getStorageCurrent: vi.fn(),
  setDatasetExpiry: vi.fn(),
  // A pass-through spy (beforeEach): the one int64 conversion (src/api/int64.ts, which throws past
  // 2^53 rather than rounding; pinned in api.spec.ts), so a spec can see a count went through it.
  int64ToNumber: vi.fn(),
}))
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...mocks,
}))
vi.mock('@/components/charts/echarts', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    ECHARTS_THEME_NAME: 'trader_joe',
    VChart: defineComponent({
      name: 'VChartStub',
      props: { option: { type: Object, required: true }, theme: { type: [String, Object], default: undefined } },
      setup(_props, { attrs }) {
        return () => h('div', { ...attrs, 'data-stub': 'vchart' })
      },
    }),
  }
})

const DAY = 86_400_000
// 22:00 on 2026-10-14 in Toronto (EDT), already 2026-10-15 in UTC.
const EVENING = Date.UTC(2026, 9, 15, 2)
const OCT_15_TORONTO = Date.UTC(2026, 9, 15, 4)

function row(id: string, symbol: string, extra: MessageInitShape<typeof DatasetSummarySchema> = {}, rowExtra: Partial<MessageInitShape<typeof StaleDatasetRowSchema>> = {}): StaleDatasetRow {
  return create(StaleDatasetRowSchema, {
    dataset: {
      id,
      assetSymbol: symbol,
      granularity: Granularity.ONE_DAY,
      updateType: UpdateType.STATIC,
      owner: 'operator',
      ...extra,
    },
    estBytes: 2_500_000n,
    ...rowExtra,
  } as MessageInitShape<typeof StaleDatasetRowSchema>)
}

const ROWS = () => [
  row(
    'ds-a',
    'VFV',
    { usage: { lastReadAt: timestampFromMs(EVENING - 200 * DAY) }, expiry: timestampFromMs(Date.UTC(2027, 0, 1, 5)) },
    { lastReadByClient: [{ clientId: 'bot-1', lastReadAt: timestampFromMs(EVENING - 200 * DAY) }, { clientId: 'bot-2' }] },
  ),
  row('ds-b', 'XIU', { granularity: Granularity.ONE_HOUR, owner: 'bot-7', reaperPending: true }),
]
const stalePage = (items: StaleDatasetRow[], nextCursor = '') =>
  create(StaleDatasetPageSchema, { items, nextCursor, staleDays: 90, clientKinds: [ClientKind.SDK] })

const mounted: VueWrapper[] = []

async function usageAt(): Promise<VueWrapper> {
  await router.push('/data/usage')
  await router.isReady()
  const shell = mount(AppShell, { global: { plugins: [createPinia(), router, PrimeVue] }, attachTo: document.body })
  mounted.push(shell)
  await flushPromises()
  return shell
}

const sidebar = (shell: VueWrapper) => shell.findAll('aside.app-shell__sidebar').at(-1)!
const tile = (shell: VueWrapper, key: string) => shell.find(`[data-tile="${key}"]`)
const tableRow = (shell: VueWrapper, id: string) => shell.find(`[data-testid="stale-table"] tr[data-dataset-id="${id}"]`)
const dialog = (shell: VueWrapper) => shell.find('[data-testid="expiry-dialog"]')
// A cell's own text, without its child elements' (the expires cell also holds the reaper flag).
const ownText = (cell: { element: Element }) =>
  Array.from(cell.element.childNodes)
    .filter((node) => node.nodeType === Node.TEXT_NODE)
    .map((node) => node.textContent ?? '')
    .join('')
    .trim()
async function click(shell: VueWrapper, selector: string) {
  await shell.find(selector).trigger('click')
  await flushPromises()
}
async function open(shell: VueWrapper, id: string) {
  await tableRow(shell, id).find('[data-testid="set-expiry-open"]').trigger('click')
  await flushPromises()
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.int64ToNumber.mockImplementation(realInt64ToNumber)
  window.localStorage.clear()
  setActivePinia(createPinia())
  useSettingsStore().setTimeZoneOverride('America/Toronto')
  fakeEvents.reset()
  mocks.getUiConfig.mockResolvedValue({ allowedGroups: [AccountGroup.SIMULATION], deploymentLabel: '', serverVersion: '' })
  mocks.getDatasetFacets.mockResolvedValue({
    all: 0n,
    needsAttention: 0n,
    running: 0n,
    sources: [{ source: DataSource.ALPACA_API, count: 4n }],
    updateTypes: [],
    statuses: [],
    clients: [
      { clientId: 'bot-1', clientKind: ClientKind.SDK, datasets: 3n },
      { clientId: 'operator', clientKind: ClientKind.UI, datasets: 5n },
    ],
  })
  mocks.listDatasets.mockResolvedValue({ items: [], nextCursor: '' })
  mocks.getUsageSummary.mockResolvedValue(
    create(UsageSummarySchema, {
      stale: 4n,
      readLast30d: 2n,
      total: 9n,
      neverRead: 1n,
      readsToday: 12n,
      distinctClientsToday: 3n,
      staleDays: 90,
      clientKinds: [ClientKind.SDK],
    }),
  )
  mocks.getLastReadBuckets.mockResolvedValue(
    create(LastReadBucketsSchema, { buckets: [{ minDays: 0, maxDays: 1, datasets: 1n }, { minDays: 91, datasets: 4n }], neverRead: 1n }),
  )
  mocks.getReadsPerDay.mockResolvedValue(create(ReadsPerDaySchema, { days: [{ day: '2026-10-14', clientId: 'bot-1', reads: 2n }] }))
  mocks.getStaleDatasets.mockResolvedValue(stalePage(ROWS()))
  mocks.getStorageCurrent.mockResolvedValue(
    create(StorageCurrentSchema, { totalBytes: 7_200_000n, bySource: [{ source: DataSource.ALPACA_API, feed: Feed.IEX, estBytes: 7_200_000n }] }),
  )
  mocks.setDatasetExpiry.mockImplementation((id: string, params: { expiry?: Date }) =>
    Promise.resolve(
      create(DatasetSummarySchema, {
        id,
        assetSymbol: 'VFV',
        owner: 'operator',
        expiry: params.expiry === undefined ? undefined : timestampFromMs(params.expiry.getTime()),
      }),
    ),
  )
})

afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount())
  useUsageStore().reset()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('page', () => {
  it('the route is the Usage screen with its sidebar; the title is Usage', async () => {
    const shell = await usageAt()
    expect(router.currentRoute.value.name).toBe('data-usage')
    expect(shell.find('main h1').text()).toBe('Usage')
    expect(sidebar(shell).find('[data-testid="read-by"]').exists()).toBe(true)
  })

  it('has no quota chart and no quota tile (tj-lenn8u)', async () => {
    const shell = await usageAt()
    expect(shell.find('main').text()).not.toMatch(/quota/i)
  })
})

describe('tiles', () => {
  it('Stale: the label from the server echo, its count, and whose reads count', async () => {
    const shell = await usageAt()
    expect(tile(shell, 'stale').find('.tile__label').text()).toBe('Stale: 90 d')
    expect(tile(shell, 'stale').find('.tile__value').text()).toBe('4')
    expect(tile(shell, 'stale').find('.tile__note').text()).toBe('unread by SDK 90+ days')
  })

  it('Read Last 30 Days of the total; Reads Today with the distinct clients', async () => {
    const shell = await usageAt()
    expect(tile(shell, 'read-30d').find('.tile__label').text()).toBe('Read Last 30 Days')
    expect(tile(shell, 'read-30d').find('.tile__value').text()).toBe('2')
    expect(tile(shell, 'read-30d').find('.tile__note').text()).toBe('of 9 datasets')
    expect(tile(shell, 'reads-today').find('.tile__value').text()).toBe('12')
    expect(tile(shell, 'reads-today').find('.tile__note').text()).toBe('by 3 clients')
  })

  it('one distinct client today is said in the singular', async () => {
    mocks.getUsageSummary.mockResolvedValue(create(UsageSummarySchema, { readsToday: 1n, distinctClientsToday: 1n, staleDays: 90 }))
    const shell = await usageAt()
    expect(tile(shell, 'reads-today').find('.tile__note').text()).toBe('by 1 client')
  })

  it('Storage: the total in decimal units, said to be current and estimated', async () => {
    const shell = await usageAt()
    expect(tile(shell, 'storage').find('.tile__value').text()).toBe(formatBytes(7_200_000))
    expect(tile(shell, 'storage').find('.tile__note').text()).toBe('current, estimated')
  })

  it('storage elements render only when the server reports them', async () => {
    mocks.getStorageCurrent.mockReturnValue(new Promise(() => undefined))
    const shell = await usageAt()
    expect(tile(shell, 'storage').exists()).toBe(false)
    expect(shell.find('#usage-storage-title').exists()).toBe(false)
  })

  it('the Stale label follows the server echo, not a browser setting', async () => {
    mocks.getUsageSummary.mockResolvedValue(create(UsageSummarySchema, { stale: 1n, staleDays: 120 }))
    const shell = await usageAt()
    expect(tile(shell, 'stale').find('.tile__label').text()).toBe('Stale: 120 d')
  })

  it('with clients picked the Stale note uses the echoed days (30), and STALE_DAYS (90) when there is no echo', async () => {
    mocks.getUsageSummary.mockResolvedValue(create(UsageSummarySchema, { stale: 1n, staleDays: 30 }))
    const shell = await usageAt()
    await sidebar(shell).find('[data-client="bot-1"] input').trigger('change')
    await flushPromises()
    expect(tile(shell, 'stale').find('.tile__note').text()).toBe('unread by the selected clients 30+ days')
    expect(tile(shell, 'stale').find('.tile__label').text()).toBe('Stale: 30 d')
    mocks.getUsageSummary.mockResolvedValue(create(UsageSummarySchema, { stale: 1n }))
    await sidebar(shell).find('[data-client="operator"] input').trigger('change')
    await flushPromises()
    expect(tile(shell, 'stale').find('.tile__note').text()).toBe('unread by the selected clients 90+ days')
  })

  it('a summary failure says so in the count tiles, not storage', async () => {
    mocks.getUsageSummary.mockRejectedValue(new ApiError({ status: 500 }))
    const shell = await usageAt()
    expect(tile(shell, 'stale').find('.tile__note').text()).toBe('Could not load counts')
    expect(tile(shell, 'storage').find('.tile__note').text()).toBe('current, estimated')
  })
})

describe('charts', () => {
  it('three charts in the app theme: last read, reads per day by client (30 d), storage by source', async () => {
    const shell = await usageAt()
    expect(shell.find('#usage-last-read-title').text()).toBe('When Each Dataset Was Last Read')
    expect(shell.find('#usage-reads-title').text()).toBe('Reads per Day by Client (30 d)')
    expect(shell.find('#usage-storage-title').text()).toBe('Storage by Source, Current')
    const stubs = shell.findAllComponents({ name: 'VChartStub' })
    expect(stubs).toHaveLength(3)
    for (const stub of stubs) expect(stub.props('theme')).toBe('trader_joe')
    expect(shell.find('[data-testid="last-read-chart"]').exists()).toBe(true)
    expect(shell.find('[data-testid="reads-chart"]').exists()).toBe(true)
    expect(shell.find('[data-testid="storage-chart"]').exists()).toBe(true)
  })

  it('a chart failure is that chart own alert with the error id', async () => {
    mocks.getLastReadBuckets.mockRejectedValue(new ApiError({ status: 500, errorId: 'b-9' }))
    const shell = await usageAt()
    const error = shell.find('[data-testid="buckets-error"]')
    expect(error.attributes('role')).toBe('alert')
    expect(error.text()).toContain('Error ID: b-9')
    expect(shell.find('[data-testid="reads-chart"]').exists()).toBe(true)
  })
})

describe('Stale Datasets table', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(EVENING)
  })

  it('the columns, in order', async () => {
    const shell = await usageAt()
    const heads = shell.findAll('[data-testid="stale-table"] thead th').map((th) => th.text())
    expect(heads).toEqual(['Dataset', 'Last Read', 'Read By', 'Size (Est.)', 'Owner', 'Expires', 'Actions'])
  })

  it('a row: the dataset name links to its detail; Last Read as an age; Read By per client; size, owner, expires', async () => {
    const shell = await usageAt()
    const a = tableRow(shell, 'ds-a')
    expect(a.find('a').text()).toBe('VFV 1d')
    expect(a.find('a').attributes('href')).toBe('/data/datasets/ds-a')
    expect(a.find('[data-cell="last-read"]').text()).toBe('200 d ago')
    expect(a.findAll('[data-cell="read-by"] .chip').map((c) => c.text())).toEqual(['bot-1 · 200 d ago', 'bot-2 · Never read'])
    expect(a.find('[data-cell="size"]').text()).toBe(formatBytes(2_500_000))
    expect(a.find('[data-cell="owner"]').text()).toBe('operator')
    expect(a.find('[data-cell="expires"]').text()).toBe('2027/01/01')
  })

  it('never says Stale in a row; never-read and no expiry read as such; the reaper flag is the server one', async () => {
    const shell = await usageAt()
    const b = tableRow(shell, 'ds-b')
    expect(b.find('a').text()).toBe('XIU 1h')
    expect(b.find('[data-cell="last-read"]').text()).toBe('Never read')
    expect(b.find('[data-cell="read-by"]').text()).toBe('—')
    // tj-grna9p.144: a dataset with no expiry reads Never, as the grid and the card do; the flag is apart.
    expect(ownText(b.find('[data-cell="expires"]'))).toBe('Never')
    expect(b.find('[data-cell="expires"]').text()).not.toContain('—')
    expect(b.find('[data-testid="would-be-deleted"]').text()).toBe('Would be deleted')
    expect(tableRow(shell, 'ds-a').find('[data-testid="would-be-deleted"]').exists()).toBe(false)
    expect(shell.find('[data-testid="stale-table"] tbody').text()).not.toMatch(/stale/i)
  })

  it('a row with no dataset at all keeps the dash in Expires: Never is said only of a dataset', async () => {
    mocks.getStaleDatasets.mockResolvedValueOnce(stalePage([...ROWS(), create(StaleDatasetRowSchema, { estBytes: 1n })]))
    const shell = await usageAt()
    const orphan = shell.find('[data-testid="stale-table"] tbody tr:not([data-dataset-id])')
    expect(orphan.exists()).toBe(true)
    expect(orphan.find('[data-cell="expires"]').text()).toBe('—')
    expect(ownText(tableRow(shell, 'ds-b').find('[data-cell="expires"]'))).toBe('Never')
    expect(ownText(tableRow(shell, 'ds-a').find('[data-cell="expires"]'))).toBe('2027/01/01')
  })

  it('Load More asks for the next page and appends it', async () => {
    mocks.getStaleDatasets.mockResolvedValueOnce(stalePage(ROWS(), 'k1'))
    const shell = await usageAt()
    mocks.getStaleDatasets.mockResolvedValueOnce(stalePage([row('ds-c', 'ZSP')]))
    await click(shell, '[data-testid="stale-load-more"]')
    expect(mocks.getStaleDatasets).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: 'k1', limit: 100 }))
    expect(shell.findAll('[data-testid="stale-table"] tbody tr')).toHaveLength(3)
    expect(shell.find('[data-testid="stale-load-more"]').exists()).toBe(false)
  })

  it('an empty list says so; a failure is an alert with Try Again', async () => {
    mocks.getStaleDatasets.mockResolvedValueOnce(stalePage([]))
    const empty = await usageAt()
    expect(empty.find('[data-testid="stale-empty"]').exists()).toBe(true)
    mounted.splice(0).forEach((w) => w.unmount())
    useUsageStore().reset()
    mocks.getStaleDatasets.mockRejectedValueOnce(new ApiError({ status: 500, errorId: 's-1' }))
    const failed = await usageAt()
    const error = failed.find('[data-testid="stale-error"]')
    expect(error.attributes('role')).toBe('alert')
    expect(error.text()).toContain('Error ID: s-1')
    await error.find('button').trigger('click')
    await flushPromises()
    expect(failed.find('[data-testid="stale-table"]').exists()).toBe(true)
  })
})

describe('Set Expiry dialog', () => {
  async function evening() {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(EVENING)
    return usageAt()
  }
  const submit = (shell: VueWrapper) => shell.find('[data-testid="expiry-submit"]')
  const hint = (shell: VueWrapper) => shell.find('[data-testid="expiry-hint"]')

  it('opening sends nothing: a modal dialog named for the dataset, with focus in the day field', async () => {
    const shell = await evening()
    await open(shell, 'ds-a')
    const d = dialog(shell)
    expect(d.attributes('role')).toBe('dialog')
    expect(d.attributes('aria-modal')).toBe('true')
    expect(d.attributes('aria-label')).toBe('Set Expiry for VFV 1d')
    expect(d.find('h2').text()).toBe('Set Expiry: VFV 1d')
    expect(document.activeElement).toBe(shell.find('[data-testid="expiry-day"]').element)
    expect(mocks.setDatasetExpiry).not.toHaveBeenCalled()
    expect(tableRow(shell, 'ds-a').find('[data-testid="set-expiry-open"]').attributes('aria-label')).toBe('Set Expiry for VFV 1d')
  })

  // tj-grna9p.144: the dialog's explanation names the control the operator sees on the dataset's page, Never.
  // Runs of whitespace are one space: the v-if branch leaves a second one where it joins the first sentence.
  const explanation = (shell: VueWrapper) => dialog(shell).find('p.text').text().replace(/\s+/g, ' ')

  it('STATIC: the explanation says the dataset is deleted on that date and Never on its page keeps it', async () => {
    const shell = await evening()
    await open(shell, 'ds-a')
    expect(explanation(shell)).toBe(
      "Set an expiry for VFV 1d. The dataset is deleted on that date. Choosing Never on the dataset's page keeps it.",
    )
  })

  it.each([
    ['DAILY', UpdateType.DAILY],
    ['STREAM', UpdateType.STREAM],
  ])('%s is a subscription: the explanation is the same deletion wording, with no retire or resume', async (_name, updateType) => {
    // tj-grna9p.177 (decision tj-hhy3dk): expiry is the deletion date only, for every update type.
    mocks.getStaleDatasets.mockResolvedValue(stalePage([row('ds-s', 'XEQT', { updateType })]))
    const shell = await evening()
    await open(shell, 'ds-s')
    expect(explanation(shell)).toBe(
      "Set an expiry for XEQT 1d. The dataset is deleted on that date. Choosing Never on the dataset's page keeps it.",
    )
    expect(dialog(shell).text()).not.toMatch(/retire|resume/i)
  })

  // The architect's .144 nit: no double space anywhere in the rendered explanation, raw, before any collapsing.
  it.each([
    ['STATIC', UpdateType.STATIC],
    ['DAILY', UpdateType.DAILY],
  ])('%s: the raw explanation has no run of two spaces and no line break left in it', async (_name, updateType) => {
    mocks.getStaleDatasets.mockResolvedValue(stalePage([row('ds-s', 'XEQT', { updateType })]))
    const shell = await evening()
    await open(shell, 'ds-s')
    const raw = (dialog(shell).find('p.text').element.textContent ?? '').trim()
    expect(raw).toBe("Set an expiry for XEQT 1d. The dataset is deleted on that date. Choosing Never on the dataset's page keeps it.")
  })

  it('Set Expiry waits for a real day, saying so by aria-describedby', async () => {
    const shell = await evening()
    await open(shell, 'ds-a')
    expect(submit(shell).attributes('disabled')).toBeDefined()
    expect(hint(shell).text()).toBe('Type the expiry day as YYYY/MM/DD to enable Set Expiry.')
    expect(submit(shell).attributes('aria-describedby')).toBe(hint(shell).attributes('id'))
    await shell.find('[data-testid="expiry-day"]').setValue('2026/02/30')
    expect(shell.find('[data-testid="expiry-problem"]').exists()).toBe(true)
    expect(shell.find('[data-testid="expiry-day"]').attributes('aria-invalid')).toBe('true')
    await shell.find('[data-testid="expiry-day"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(mocks.setDatasetExpiry).not.toHaveBeenCalled()
  })

  it('the operator own dataset: no reason, no warning; Tomorrow then Set Expiry sends the display-zone midnight and NO override', async () => {
    const shell = await evening()
    await open(shell, 'ds-a')
    expect(shell.find('[data-testid="override-warning"]').exists()).toBe(false)
    expect(shell.find('[data-testid="override-prompt"]').exists()).toBe(false)
    await click(shell, '[data-testid="expiry-tomorrow"]')
    expect((shell.find('[data-testid="expiry-day"]').element as HTMLInputElement).value).toBe('2026/10/15')
    expect(hint(shell).exists()).toBe(false)
    expect(submit(shell).attributes('aria-describedby')).toBeUndefined()
    await click(shell, '[data-testid="expiry-submit"]')
    expect(mocks.setDatasetExpiry).toHaveBeenCalledTimes(1)
    const [id, params] = mocks.setDatasetExpiry.mock.calls[0]!
    expect(id).toBe('ds-a')
    expect((params.expiry as Date).getTime()).toBe(OCT_15_TORONTO)
    expect(params.override).toBeUndefined()
    // 200: the dialog closes and focus goes back to the row's button.
    expect(dialog(shell).exists()).toBe(false)
    expect(document.activeElement).toBe(tableRow(shell, 'ds-a').find('[data-testid="set-expiry-open"]').element)
  })

  it('another owner dataset: the owner with a warning, and a non-blank reason before the override is sent', async () => {
    mocks.setDatasetExpiry.mockResolvedValue(create(DatasetSummarySchema, { id: 'ds-b', owner: 'bot-7' }))
    const shell = await evening()
    await open(shell, 'ds-b')
    expect(shell.find('[data-testid="override-warning"]').text()).toContain('Held by bot-7.')
    const reason = shell.find('[data-testid="override-reason"]')
    expect(shell.find(`label[for="${reason.attributes('id')}"]`).text()).toBe('Reason')
    await click(shell, '[data-testid="expiry-tomorrow"]')
    expect(submit(shell).attributes('disabled')).toBeDefined()
    expect(hint(shell).text()).toBe('Give a reason above to enable Set Expiry.')
    await reason.setValue('   ')
    expect(submit(shell).attributes('disabled')).toBeDefined()
    await shell.find('[data-testid="expiry-day"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(mocks.setDatasetExpiry).not.toHaveBeenCalled()
    await reason.setValue('  owner asked  ')
    await click(shell, '[data-testid="expiry-submit"]')
    expect(mocks.setDatasetExpiry.mock.calls[0]![1].override).toEqual({ reason: 'owner asked', confirmed: true })
  })

  it('Cancel and Escape close it, send nothing, and give focus back to the row button', async () => {
    const shell = await evening()
    await open(shell, 'ds-a')
    await click(shell, '[data-testid="expiry-cancel"]')
    expect(dialog(shell).exists()).toBe(false)
    const trigger = tableRow(shell, 'ds-a').find('[data-testid="set-expiry-open"]').element
    expect(document.activeElement).toBe(trigger)
    await open(shell, 'ds-a')
    await dialog(shell).trigger('keydown', { key: 'Escape' })
    await flushPromises()
    expect(dialog(shell).exists()).toBe(false)
    expect(document.activeElement).toBe(trigger)
    expect(mocks.setDatasetExpiry).not.toHaveBeenCalled()
  })

  it('Tab stays inside the open dialog', async () => {
    const shell = await evening()
    await open(shell, 'ds-a')
    const buttons = dialog(shell).findAll('button:not([disabled]), input:not([disabled]), textarea:not([disabled])')
    const last = buttons.at(-1)!.element as HTMLElement
    last.focus()
    await dialog(shell).trigger('keydown', { key: 'Tab' })
    expect(document.activeElement).toBe(buttons[0]!.element)
    await dialog(shell).trigger('keydown', { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(last)
  })

  describe('refusals leave the dialog open with fixed words, never echoing the reason or owner', () => {
    async function heldAndReady(error: ApiError) {
      const shell = await evening()
      await open(shell, 'ds-b')
      await click(shell, '[data-testid="expiry-tomorrow"]')
      await shell.find('[data-testid="override-reason"]').setValue('my-private-reason')
      mocks.setDatasetExpiry.mockRejectedValueOnce(error)
      await click(shell, '[data-testid="expiry-submit"]')
      const notice = shell.find('[data-testid="expiry-notice"]')
      expect(dialog(shell).exists()).toBe(true)
      expect(notice.attributes('role')).toBe('alert')
      expect(notice.text()).not.toContain('my-private-reason')
      expect(notice.text()).not.toContain('bot-7')
      expect((shell.find('[data-testid="override-reason"]').element as HTMLTextAreaElement).value).toBe('my-private-reason')
      return { shell, notice }
    }

    it('503 says run make migrate, with the error id', async () => {
      const { notice } = await heldAndReady(new ApiError({ status: 503, reason: 'DATABASE_UNAVAILABLE', errorId: 'e-503' }))
      expect(notice.text()).toContain('nothing was changed. Run make migrate, then try again.')
      expect(notice.text()).toContain('Error ID: e-503')
    })

    it('422 is a bug in the UI, with the error id', async () => {
      const { notice } = await heldAndReady(new ApiError({ status: 422, reason: 'INVALID_REQUEST', errorId: 'e-422' }))
      expect(notice.text()).toContain('This is a bug in the UI; nothing was changed.')
      expect(notice.text()).toContain('Error ID: e-422')
    })

    it('409 asks for the reason again; the prompt stays', async () => {
      const { shell, notice } = await heldAndReady(new ApiError({ status: 409, reason: 'OVERRIDE_REQUIRED' }))
      expect(notice.text()).toContain('This dataset is held by another owner.')
      expect(shell.find('[data-testid="override-prompt"]').exists()).toBe(true)
    })
  })

  describe('409 on a row shown as the operator own, whose dataset changed hands', () => {
    async function ownRowThen409(refetched: StaleDatasetRow[]) {
      const shell = await evening()
      await open(shell, 'ds-a')
      expect(shell.find('[data-testid="override-prompt"]').exists()).toBe(false)
      await click(shell, '[data-testid="expiry-tomorrow"]')
      mocks.setDatasetExpiry.mockRejectedValueOnce(new ApiError({ status: 409, reason: 'OVERRIDE_REQUIRED' }))
      mocks.getStaleDatasets.mockResolvedValue(stalePage(refetched))
      const calls = mocks.getStaleDatasets.mock.calls.length
      await click(shell, '[data-testid="expiry-submit"]')
      expect(mocks.getStaleDatasets.mock.calls.length).toBe(calls + 1)
      return shell
    }

    it('the page is refetched; the dialog stays, now with the owner warning and the reason; the next submit sends the override', async () => {
      const changed = ROWS()
      changed[0]!.dataset!.owner = 'bot-7'
      const shell = await ownRowThen409(changed)
      expect(dialog(shell).exists()).toBe(true)
      expect(shell.find('[data-testid="expiry-notice"]').text()).toContain('This dataset is held by another owner.')
      expect(shell.find('[data-testid="override-warning"]').text()).toContain('Held by bot-7.')
      expect(tableRow(shell, 'ds-a').find('[data-cell="owner"]').text()).toBe('bot-7')
      // The chosen day is kept; the reason is now what blocks it, said by aria-describedby.
      expect((shell.find('[data-testid="expiry-day"]').element as HTMLInputElement).value).toBe('2026/10/15')
      expect(shell.find('[data-testid="expiry-hint"]').text()).toBe('Give a reason above to enable Set Expiry.')
      expect(shell.find('[data-testid="expiry-submit"]').attributes('aria-describedby')).toBe('expiry-dialog-hint')
      // Focus is still inside the dialog (it never went back to the page behind it).
      expect(dialog(shell).element.contains(document.activeElement)).toBe(true)
      mocks.setDatasetExpiry.mockResolvedValueOnce(create(DatasetSummarySchema, { id: 'ds-a', owner: 'bot-7' }))
      await shell.find('[data-testid="override-reason"]').setValue('owner left')
      await click(shell, '[data-testid="expiry-submit"]')
      expect(mocks.setDatasetExpiry).toHaveBeenCalledTimes(2)
      expect(mocks.setDatasetExpiry.mock.calls[0]![1].override).toBeUndefined()
      expect(mocks.setDatasetExpiry.mock.calls[1]![1].override).toEqual({ reason: 'owner left', confirmed: true })
      expect(dialog(shell).exists()).toBe(false)
    })

    it('a refetch that no longer lists the dataset closes the dialog and the row leaves the list', async () => {
      const shell = await ownRowThen409([ROWS()[1]!])
      expect(dialog(shell).exists()).toBe(false)
      expect(tableRow(shell, 'ds-a').exists()).toBe(false)
      expect(tableRow(shell, 'ds-b').exists()).toBe(true)
    })
  })

  it('404: the dialog closes, the row is dropped and a notice names the dataset', async () => {
    const shell = await evening()
    await open(shell, 'ds-a')
    await click(shell, '[data-testid="expiry-tomorrow"]')
    mocks.setDatasetExpiry.mockRejectedValueOnce(new ApiError({ status: 404, reason: 'NOT_FOUND' }))
    mocks.getStaleDatasets.mockResolvedValue(stalePage([ROWS()[1]!]))
    await click(shell, '[data-testid="expiry-submit"]')
    expect(dialog(shell).exists()).toBe(false)
    expect(tableRow(shell, 'ds-a').exists()).toBe(false)
    expect(shell.find('[data-testid="dropped-notice"]').text()).toContain('VFV 1d no longer exists, so it was removed from the list.')
  })

  it('while a write is in flight every row button and the dialog controls wait', async () => {
    const shell = await evening()
    await open(shell, 'ds-a')
    await click(shell, '[data-testid="expiry-tomorrow"]')
    mocks.setDatasetExpiry.mockReturnValue(new Promise(() => undefined))
    await click(shell, '[data-testid="expiry-submit"]')
    for (const button of shell.findAll('[data-testid="set-expiry-open"]')) expect(button.attributes('disabled')).toBeDefined()
    for (const el of dialog(shell).findAll('button, input')) expect(el.attributes('disabled')).toBeDefined()
  })

  it('the row leaving the list while the dialog is open (a refetch) closes the dialog', async () => {
    const shell = await evening()
    await open(shell, 'ds-a')
    mocks.getStaleDatasets.mockResolvedValue(stalePage([ROWS()[1]!]))
    await useUsageStore().reloadLive()
    await flushPromises()
    expect(dialog(shell).exists()).toBe(false)
  })
})

describe('sidebar', () => {
  it('Read By (SDK default), a Client picker from the facets, a Source filter; NO Stale After control', async () => {
    const shell = await usageAt()
    const side = sidebar(shell)
    expect((side.find('[data-read-by="SDK"] input').element as HTMLInputElement).checked).toBe(true)
    expect(side.findAll('[data-testid="usage-clients"] [data-client]').map((c) => c.attributes('data-client'))).toEqual(['bot-1', 'operator'])
    expect(side.find('[data-testid="usage-clients"] legend').text()).toBe('Client')
    expect(side.find('[data-testid="usage-sources"] [data-source="ALPACA_API"]').exists()).toBe(true)
    expect(side.text()).not.toMatch(/stale after/i)
    expect(side.findAll('input[type="number"], select')).toHaveLength(0)
    // The picker's facets are unfiltered.
    expect(mocks.getDatasetFacets).toHaveBeenCalledWith({ signal: expect.any(AbortSignal) })
    expect(side.find('[data-testid="usage-clear"]').exists()).toBe(false)
  })

  it('picking a client sends client_id (it wins over Read By) and says so; Clear puts everything back', async () => {
    const shell = await usageAt()
    vi.clearAllMocks()
    await sidebar(shell).find('[data-client="bot-1"] input').trigger('change')
    await flushPromises()
    for (const mock of [mocks.getUsageSummary, mocks.getLastReadBuckets, mocks.getReadsPerDay, mocks.getStaleDatasets]) {
      expect(mock).toHaveBeenLastCalledWith(expect.objectContaining({ clientIds: ['bot-1'] }))
      expect(mock.mock.calls.at(-1)![0]).not.toHaveProperty('clientKinds')
    }
    expect(mocks.getStorageCurrent).not.toHaveBeenCalled()
    expect(sidebar(shell).find('[data-testid="read-by-overridden"]').text()).toBe('The clients chosen below count instead of Read By.')
    expect(tile(shell, 'stale').find('.tile__note').text()).toBe('unread by the selected clients 90+ days')
    await click(shell, '[data-testid="usage-clear"]')
    expect(mocks.getUsageSummary).toHaveBeenLastCalledWith(expect.objectContaining({ clientKinds: [ClientKind.SDK] }))
    expect(sidebar(shell).find('[data-testid="read-by-overridden"]').exists()).toBe(false)
  })

  it('a Source is sent on every route, storage included, and unpicked by a second click', async () => {
    const shell = await usageAt()
    vi.clearAllMocks()
    const box = () => sidebar(shell).find('[data-source="ALPACA_API"] input')
    await box().trigger('change')
    await flushPromises()
    expect(mocks.getStorageCurrent).toHaveBeenLastCalledWith({ source: DataSource.ALPACA_API, signal: expect.any(AbortSignal) })
    expect(mocks.getUsageSummary).toHaveBeenLastCalledWith(expect.objectContaining({ source: DataSource.ALPACA_API, staleDays: 90 }))
    await box().trigger('change')
    await flushPromises()
    expect(mocks.getStorageCurrent).toHaveBeenLastCalledWith({ source: undefined, signal: expect.any(AbortSignal) })
  })

  it('each client count goes through the one int64 conversion (int64ToNumber), never Number()', async () => {
    mocks.getDatasetFacets.mockResolvedValue({
      sources: [],
      updateTypes: [],
      statuses: [],
      clients: [{ clientId: 'bot-9', clientKind: ClientKind.SDK, datasets: 7_654_321n }],
    })
    const shell = await usageAt()
    expect(mocks.int64ToNumber).toHaveBeenCalledWith(7_654_321n)
    expect(sidebar(shell).find('[data-client="bot-9"] .side-count').text()).toBe('7,654,321')
  })

  it('Read By UI refetches the figures under SDK and UI', async () => {
    const shell = await usageAt()
    vi.clearAllMocks()
    await sidebar(shell).find('[data-read-by="UI"] input').trigger('change')
    await flushPromises()
    const kinds = mocks.getUsageSummary.mock.calls.at(-1)![0].clientKinds as ClientKind[]
    expect([...kinds].sort()).toEqual([ClientKind.SDK, ClientKind.UI].sort())
    expect(tile(shell, 'stale').find('.tile__note').text()).toMatch(/^unread by (SDK and UI|UI and SDK) 90\+ days$/)
  })

  it('the selection is kept across leaving the tab and coming back', async () => {
    const shell = await usageAt()
    await sidebar(shell).find('[data-client="bot-1"] input').trigger('change')
    await flushPromises()
    await router.push('/data/datasets')
    await flushPromises()
    vi.clearAllMocks()
    await router.push('/data/usage')
    await flushPromises()
    expect((sidebar(shell).find('[data-client="bot-1"] input').element as HTMLInputElement).checked).toBe(true)
    expect(mocks.getUsageSummary).toHaveBeenLastCalledWith(expect.objectContaining({ clientIds: ['bot-1'] }))
  })
})

describe('live', () => {
  async function live() {
    const shell = await usageAt()
    vi.clearAllMocks()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    return shell
  }
  const datasetEvent = (id: string) =>
    fakeEvents.emit({ event: { case: 'datasetChanged', value: { id, change: DatasetChange.UPDATED } } })

  it('a burst of dataset events refetches everything ONCE after COALESCE_MS, softly', async () => {
    const shell = await live()
    datasetEvent('ds-a')
    datasetEvent('ds-b')
    vi.advanceTimersByTime(COALESCE_MS - 1)
    await flushPromises()
    expect(mocks.getUsageSummary).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    await flushPromises()
    for (const mock of [mocks.getUsageSummary, mocks.getLastReadBuckets, mocks.getReadsPerDay, mocks.getStorageCurrent, mocks.getStaleDatasets]) {
      expect(mock).toHaveBeenCalledTimes(1)
    }
    expect(shell.find('[data-testid="stale-loading"]').exists()).toBe(false)
  })

  it('a reconnect refetches everything once', async () => {
    await live()
    fakeEvents.reconnect()
    await flushPromises()
    expect(mocks.getUsageSummary).toHaveBeenCalledTimes(1)
    expect(mocks.getStaleDatasets).toHaveBeenCalledTimes(1)
  })

  it('leaving the tab unsubscribes and cancels a pending refetch; nothing polls', async () => {
    const interval = vi.spyOn(globalThis, 'setInterval')
    const shell = await live()
    datasetEvent('ds-a')
    shell.unmount()
    mounted.splice(0)
    expect(fakeEvents.subscribers('datasetChanged')).toBe(0)
    vi.advanceTimersByTime(COALESCE_MS * 2)
    await flushPromises()
    expect(mocks.getUsageSummary).not.toHaveBeenCalled()
    expect(interval).not.toHaveBeenCalled()
  })
})
