import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import { create } from '@bufbuild/protobuf'
import type { MessageInitShape } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import { DataSource, DataType, Feed, Granularity, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import {
  ClientKind,
  DatasetState,
  DatasetSummarySchema,
  ExpiryType,
  FreshnessStatus,
  IngestRunPageSchema,
  IngestRunSchema,
  IngestRunState,
  RunTrigger,
} from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { IngestRun } from '@generated/trader_joe/proto/ui/v1/data_pb'
import { DatasetChange } from '@generated/trader_joe/proto/ui/v1/events_pb'
import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'
import { CoverageStripSchema, ReadsPerDaySchema } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { fakeEvents } from '@/__tests__/fakeEvents'
import { ApiError } from '@/api'
import { dataTypeLabel, granularityLabel, sourceFeedLabel } from '@/catalog/labels'
import { COALESCE_MS } from '@/catalog/useCatalogLiveRefresh'
import CoverageStrip from '@/components/charts/CoverageStrip.vue'
import { formatNumber } from '@/format/formatters'
import router from '@/router'
import AppShell from '@/shell/AppShell.vue'
import { useSettingsStore } from '@/stores/settings'
import { COVERAGE_UNIT, titleCase } from '@/theme/copy'
import { DETAIL_COVERAGE_BUCKETS } from './useDatasetDetailLive'

// tj-grna9p.34 phase 2 Dataset detail, mounted in the real shell under the real router at
// /data/datasets/:id. The API is mocked at the typed client (@/api) and the shared events socket is
// the fixture in src/__tests__/fakeEvents.ts; the stores, the live hook, CoverageStrip and the runs
// table are the real ones. Fixtures are create(Schema) from the generated ui/v1 messages.
//
// WHAT THE MOCKS HIDE: the store's real routes (system suite), a live socket, and a browser's layout
// (the sidebar's scroll-to-section is not exercised).
vi.mock('@/api/events', async (importOriginal) =>
  (await import('@/__tests__/fakeEvents')).fakeEventsModule(await importOriginal<object>()),
)
const mocks = vi.hoisted(() => ({
  getUiConfig: vi.fn(),
  getDataset: vi.fn(),
  getDatasetRuns: vi.fn(),
  getDatasetCoverage: vi.fn(),
  getDatasetFacets: vi.fn(),
  listDatasets: vi.fn(),
  getDatasetBars: vi.fn(),
  getReadsPerDay: vi.fn(),
  setDatasetExpiry: vi.fn(),
  deleteDataset: vi.fn(),
}))
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...mocks,
}))
// tj-grna9p.89: vue-echarts needs a canvas jsdom does not have. The stub keeps the option it is given
// (ReadsPerDayChart.spec.ts reads it); here it only has to mount.
vi.mock('@/components/charts/echarts', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    ECHARTS_THEME_NAME: 'trader_joe',
    VChart: defineComponent({
      name: 'VChartStub',
      props: { option: { type: Object, required: true } },
      setup(_props, { attrs }) {
        return () => h('div', { ...attrs, 'data-stub': 'vchart' })
      },
    }),
  }
})

const DAY = 86_400_000
// Local midnights in Toronto (EST, UTC-5).
const JAN_2 = Date.UTC(2026, 0, 2, 5)
const MAR_2 = Date.UTC(2026, 2, 2, 5)
const RUN_AT = Date.UTC(2026, 9, 12, 14, 0, 0)

function dataset(extra: MessageInitShape<typeof DatasetSummarySchema> = {}) {
  return create(DatasetSummarySchema, {
    id: 'ds-1',
    assetSymbol: 'VFV',
    dataType: DataType.MARKET_ACTIVITY,
    source: DataSource.ALPACA_API,
    feed: Feed.IEX,
    granularity: Granularity.ONE_DAY,
    updateType: UpdateType.STATIC,
    expiryType: ExpiryType.ROLLING,
    owner: 'operator',
    state: DatasetState.ACTIVE,
    start: timestampFromMs(JAN_2),
    end: timestampFromMs(MAR_2),
    lastBar: timestampFromMs(MAR_2 - 3 * DAY),
    barCount: 12345n,
    freshness: { status: FreshnessStatus.COMPLETE },
    ...extra,
  } as MessageInitShape<typeof DatasetSummarySchema>)
}

function run(id: string, minutes: number, extra: MessageInitShape<typeof IngestRunSchema> = {}): IngestRun {
  return create(IngestRunSchema, {
    id,
    datasetId: 'ds-1',
    trigger: RunTrigger.REQUEST,
    actor: 'operator',
    state: IngestRunState.SUCCEEDED,
    start: timestampFromMs(JAN_2),
    end: timestampFromMs(MAR_2),
    barsWritten: 40n,
    startedAt: timestampFromMs(RUN_AT + minutes * 60_000),
    finishedAt: timestampFromMs(RUN_AT + minutes * 60_000 + 12_000),
    ...extra,
  })
}

const page = (items: IngestRun[], nextCursor = '') => create(IngestRunPageSchema, { items, nextCursor })

// The strip's range [JAN_2, JAN_2 + 60 days) in two 30-day buckets. The detail draws one block per
// bucket (tj-grna9p.111), each titled with its half-open dates; the grid would break the same strip
// at its gap windows by time (tj-grna9p.108).
const STRIP = create(CoverageStripSchema, {
  start: timestampFromMs(JAN_2),
  end: timestampFromMs(JAN_2 + 60 * DAY),
  buckets: [
    { expectedSessions: 20, sessionsWithBars: 20, start: timestampFromMs(JAN_2), end: timestampFromMs(JAN_2 + 30 * DAY) },
    { expectedSessions: 20, sessionsWithBars: 17, start: timestampFromMs(JAN_2 + 30 * DAY), end: timestampFromMs(JAN_2 + 60 * DAY) },
  ],
  gaps: [
    { start: timestampFromMs(JAN_2 + 40 * DAY), end: timestampFromMs(JAN_2 + 41 * DAY), sessions: 1 },
    { start: timestampFromMs(JAN_2 + 44 * DAY), end: timestampFromMs(JAN_2 + 46 * DAY), sessions: 2 },
  ],
  gapCount: 4,
})

const mounted: VueWrapper[] = []

async function detailAt(id = 'ds-1'): Promise<VueWrapper> {
  await router.push(`/data/datasets/${encodeURIComponent(id)}`)
  await router.isReady()
  const shell = mount(AppShell, { global: { plugins: [createPinia(), router, PrimeVue] }, attachTo: document.body })
  mounted.push(shell)
  await flushPromises()
  return shell
}

const main = (shell: VueWrapper) => shell.find('main')
const runRows = (shell: VueWrapper) => shell.findAll('[data-testid="runs-table"] tbody tr')
const runIds = (shell: VueWrapper) => runRows(shell).map((tr) => tr.attributes('data-run-id'))

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  setActivePinia(createPinia())
  useSettingsStore().setTimeZoneOverride('America/Toronto')
  fakeEvents.reset()
  mocks.getUiConfig.mockResolvedValue({ allowedGroups: [AccountGroup.SIMULATION], deploymentLabel: '', serverVersion: '' })
  mocks.getDataset.mockResolvedValue(dataset())
  mocks.getDatasetRuns.mockResolvedValue(page([run('r2', 2), run('r1', 1)]))
  mocks.getDatasetCoverage.mockResolvedValue(STRIP)
  mocks.getDatasetFacets.mockResolvedValue({ all: 0n, needsAttention: 0n, running: 0n, sources: [], updateTypes: [], statuses: [] })
  mocks.listDatasets.mockResolvedValue({ items: [], nextCursor: '' })
  mocks.getReadsPerDay.mockResolvedValue(create(ReadsPerDaySchema, {}))
  mocks.setDatasetExpiry.mockImplementation((_id: string, params: { expiry?: Date }) =>
    Promise.resolve(dataset({ expiry: params.expiry === undefined ? undefined : timestampFromMs(params.expiry.getTime()) })),
  )
  mocks.deleteDataset.mockResolvedValue(undefined)
})

afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount())
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('load states', () => {
  it('shows Loading until the dataset arrives', async () => {
    mocks.getDataset.mockReturnValue(new Promise(() => undefined))
    const shell = await detailAt()
    expect(shell.find('[data-testid="detail-loading"]').text()).toBe('Loading dataset')
    expect(shell.find('[data-testid="detail-loading"]').attributes('role')).toBe('status')
  })

  it('an unknown id (404) is Dataset Not Found with a way back, not an error', async () => {
    mocks.getDataset.mockRejectedValue(new ApiError({ status: 404 }))
    const shell = await detailAt('nope')
    const panel = shell.find('[data-testid="detail-not-found"]')
    expect(panel.find('h1').text()).toBe('Dataset Not Found')
    expect(panel.text()).toContain('No dataset has the id nope.')
    expect(panel.find('a').attributes('href')).toMatch(/^\/data\/datasets(\?|$)/)
    expect(shell.find('[data-testid="detail-error"]').exists()).toBe(false)
  })

  it('a failure shows the message and error id, and Try Again loads again', async () => {
    mocks.getDataset.mockRejectedValueOnce(new ApiError({ status: 503, errorId: 'e-3' }))
    const shell = await detailAt()
    const panel = shell.find('[data-testid="detail-error"]')
    expect(panel.attributes('role')).toBe('alert')
    expect(panel.text()).toContain('Could not load the dataset. Request failed (HTTP 503)')
    expect(panel.text()).toContain('Error ID: e-3')
    await panel.find('button').trigger('click')
    await flushPromises()
    expect(shell.find('[data-testid="detail-error"]').exists()).toBe(false)
    expect(shell.find('[data-testid="detail-title"]').text()).toBe('VFV · bars · 1d')
  })
})

describe('overview', () => {
  // tj-grna9p.113 (owner 2026-10-13) removed Open in Viewer: it made the workflow circular. The way
  // back to the Viewer is now the dataset crumb (the 'breadcrumbs' block below).
  it('title is the dataset name, with its Status badge and no Open in Viewer', async () => {
    const shell = await detailAt()
    expect(shell.find('[data-testid="detail-title"]').text()).toBe('VFV · bars · 1d')
    expect(shell.find('[data-testid="detail-badge"]').text()).toBe('Healthy')
    expect(shell.find('[data-testid="open-viewer"]').exists()).toBe(false)
    expect(main(shell).text()).not.toContain('Open in Viewer')
    // tj-grna9p.112 leaves the Detail's text alone: no table header there, so it keeps the unit.
    expect(shell.find('#detail-coverage [data-testid="coverage-summary"]').text()).toBe(`37/40 ${COVERAGE_UNIT}`)
  })

  it('the Detail screen has no button or link of its own back to the Viewer: only the crumb', async () => {
    const shell = await detailAt()
    // The screen's own section; the sidebar has no way to the Viewer either ('sidebar' below).
    const toViewer = shell
      .find('section.detail')
      .findAll('a')
      .filter((a) => a.attributes('href') === '/data/datasets/ds-1/viewer')
    expect(toViewer.map((a) => a.attributes('data-testid'))).toEqual(['breadcrumb-link'])
  })

  it('a running last run reads Running in the header badge', async () => {
    mocks.getDataset.mockResolvedValue(dataset({ lastRun: { state: IngestRunState.RUNNING } }))
    const shell = await detailAt()
    expect(shell.find('[data-testid="detail-badge"]').text()).toBe('Running')
  })

  it('facts: Owner, Range half-open, Bars, Last Bar', async () => {
    const shell = await detailAt()
    const fact = (name: string) => shell.find(`[data-fact="${name}"]`).text()
    expect(fact('owner')).toBe('operator')
    expect(fact('range')).toBe('2026/01/02 to 2026/03/02')
    expect(shell.find('[data-fact="range"]').attributes('title')).toContain('Half-open')
    expect(fact('bars')).toBe(formatNumber(12345))
    expect(fact('last-bar')).toBe('2026/02/27')
  })

  it('a subscription has no end: its Range reads from its start onward', async () => {
    mocks.getDataset.mockResolvedValue(dataset({ updateType: UpdateType.DAILY }))
    const shell = await detailAt()
    expect(shell.find('[data-fact="range"]').text()).toBe('2026/01/02 onward')
  })

  it('a missing owner is the dash', async () => {
    mocks.getDataset.mockResolvedValue(dataset({ owner: '' }))
    const shell = await detailAt()
    expect(shell.find('[data-fact="owner"]').text()).toBe('—')
  })
})

// tj-grna9p.113 (owner 2026-10-13): a hierarchy to go back up without the browser's Back button,
// Datasets > <dataset> (the Viewer) > Details. Each ancestor is a real link, the current page is
// plain text marked aria-current=page. The owner then ruled the dataset crumb is ONE crumb labelled
// with the asset symbol only (the unique key VFV · bars · 1d read as nested levels); the whole
// identity is its hover title, and it carries no aria-label, so its accessible name is the visible
// symbol. The page title is unchanged ('VFV · bars · 1d', pinned in 'overview'). Where there is no
// dataset to name (loading, not found, an error, deleted) there is no Viewer to link to, so the
// trail is Datasets > Details. PageBreadcrumbs' own markup is pinned in
// src/components/nav/PageBreadcrumbs.spec.ts.
describe('breadcrumbs', () => {
  const crumbs = (shell: VueWrapper) => shell.find('section.detail [data-testid="breadcrumbs"]')
  const trail = (shell: VueWrapper) => crumbs(shell).findAll('li').map((li) => li.text())
  const links = (shell: VueWrapper) => crumbs(shell).findAll('[data-testid="breadcrumb-link"]')
  const current = (shell: VueWrapper) => crumbs(shell).find('[data-testid="breadcrumb-current"]')
  // The fixture's whole identity, built from the label helpers rather than restating their words.
  const identity = () => {
    const d = dataset()
    return [d.assetSymbol, dataTypeLabel(d.dataType), granularityLabel(d.granularity), sourceFeedLabel(d.source, d.feed)].join(' · ')
  }

  it('reads Datasets > <symbol> > Details, in a nav labelled Breadcrumb, while the title keeps the full name', async () => {
    const shell = await detailAt()
    expect(crumbs(shell).element.tagName).toBe('NAV')
    expect(crumbs(shell).attributes('aria-label')).toBe('Breadcrumb')
    expect(trail(shell)).toEqual([titleCase('Datasets'), 'VFV', titleCase('Details')])
    expect(shell.find('[data-testid="detail-title"]').text()).toBe('VFV · bars · 1d')
  })

  it('Datasets links the catalog and the dataset crumb links the Viewer; Details is the current page', async () => {
    const shell = await detailAt()
    expect(links(shell).map((a) => [a.element.tagName, a.text(), a.attributes('href')])).toEqual([
      ['A', 'Datasets', '/data/datasets'],
      ['A', 'VFV', '/data/datasets/ds-1/viewer'],
    ])
    expect(current(shell).element.tagName).toBe('SPAN')
    expect(current(shell).text()).toBe('Details')
    expect(current(shell).attributes('aria-current')).toBe('page')
    expect(crumbs(shell).findAll('[aria-current]')).toHaveLength(1)
  })

  it('the dataset crumb\'s hover title is the whole identity; it has no aria-label, so its name is the symbol', async () => {
    const shell = await detailAt()
    const crumb = links(shell)[1]!
    expect(crumb.attributes('title')).toBe(identity())
    expect(crumb.attributes('aria-label')).toBeUndefined()
    expect(crumb.attributes('aria-labelledby')).toBeUndefined()
    expect(crumb.text()).toBe('VFV')
    // Only the dataset crumb carries a hint: Datasets and the current Details crumb have no title.
    expect(links(shell)[0]!.attributes('title')).toBeUndefined()
    expect(current(shell).attributes('title')).toBeUndefined()
  })

  it('following the dataset crumb opens the Viewer for this dataset', async () => {
    const shell = await detailAt()
    await links(shell)[1]!.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('data-dataset-viewer')
    expect(router.currentRoute.value.params.id).toBe('ds-1')
  })

  it('following the Datasets crumb opens the catalog', async () => {
    const shell = await detailAt()
    await links(shell)[0]!.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('data-datasets')
  })

  it('the dataset crumb escapes an id that is not URL-safe', async () => {
    mocks.getDataset.mockResolvedValue(dataset({ id: 'a/b' }))
    const shell = await detailAt('a/b')
    expect(links(shell)[1]!.attributes('href')).toBe('/data/datasets/a%2Fb/viewer')
  })

  it('while the dataset is loading the trail is Datasets > Details', async () => {
    mocks.getDataset.mockReturnValue(new Promise(() => undefined))
    const shell = await detailAt()
    expect(trail(shell)).toEqual(['Datasets', 'Details'])
    expect(links(shell).map((a) => a.attributes('href'))).toEqual(['/data/datasets'])
    expect(current(shell).attributes('aria-current')).toBe('page')
  })

  it('a dataset not found (404) has the trail Datasets > Details', async () => {
    mocks.getDataset.mockRejectedValue(new ApiError({ status: 404 }))
    const shell = await detailAt('nope')
    expect(trail(shell)).toEqual(['Datasets', 'Details'])
    expect(links(shell)).toHaveLength(1)
  })

  it('a failed load has the trail Datasets > Details', async () => {
    mocks.getDataset.mockRejectedValue(new ApiError({ status: 503 }))
    const shell = await detailAt()
    expect(shell.find('[data-testid="detail-error"]').exists()).toBe(true)
    expect(trail(shell)).toEqual(['Datasets', 'Details'])
    expect(links(shell)).toHaveLength(1)
  })

  it('a deleted dataset drops the Viewer crumb: Datasets > Details', async () => {
    const shell = await detailAt()
    fakeEvents.emit({ event: { case: 'datasetChanged', value: { id: 'ds-1', change: DatasetChange.DELETED } } })
    await flushPromises()
    expect(shell.find('[data-testid="detail-deleted"]').exists()).toBe(true)
    expect(trail(shell)).toEqual(['Datasets', 'Details'])
    expect(links(shell).map((a) => a.attributes('href'))).toEqual(['/data/datasets'])
  })
})

// n consecutive one-day buckets from JAN_2, the first `short` of them missing one session of two.
function stripOf(n: number, short = 0) {
  return create(CoverageStripSchema, {
    start: timestampFromMs(JAN_2),
    end: timestampFromMs(JAN_2 + n * DAY),
    buckets: Array.from({ length: n }, (_, i) => ({
      expectedSessions: 2,
      sessionsWithBars: i < short ? 1 : 2,
      start: timestampFromMs(JAN_2 + i * DAY),
      end: timestampFromMs(JAN_2 + (i + 1) * DAY),
    })),
  })
}

describe('coverage', () => {
  // tj-grna9p.115 (owner 2026-10-13): the detail's blocks are "limited to 10-20"; a dataset with
  // over 2000 sessions drew one block per 120 buckets. The detail now asks for DETAIL_COVERAGE_BUCKETS
  // (20), and the server returns min(buckets, sessions) near-equal groups (dataset_catalog.py), so the
  // request IS the block cap. The spec pins the request through the constant and the owner's range,
  // rather than the literal, so the number can move inside 10-20 without a spec edit.
  it('asks for the strip at DETAIL_COVERAGE_BUCKETS (10-20) buckets and lists its gaps, with "and N more"', async () => {
    const shell = await detailAt()
    expect(DETAIL_COVERAGE_BUCKETS).toBeGreaterThanOrEqual(10)
    expect(DETAIL_COVERAGE_BUCKETS).toBeLessThanOrEqual(20)
    expect(mocks.getDatasetCoverage).toHaveBeenCalledTimes(1)
    expect(mocks.getDatasetCoverage).toHaveBeenCalledWith('ds-1', DETAIL_COVERAGE_BUCKETS, expect.any(AbortSignal))
    const section = shell.find('#detail-coverage')
    // tj-grna9p.111 (owner 2026-10-12): the detail has one strip on screen, so it keeps a block per
    // bucket rather than the grid's one track broken at gap windows. Still one role=img track.
    expect(section.findAll('[data-testid="coverage-track"]')).toHaveLength(1)
    expect(section.findAll('[data-testid="coverage-break"]')).toHaveLength(0)
    const blocks = section.findAll('[data-testid="coverage-block"]')
    expect(blocks.map((b) => b.attributes('data-state'))).toEqual(['covered', 'partial'])
    expect(blocks.map((b) => b.attributes('title'))).toEqual([
      `2026/01/02 to 2026/01/31 · 20 of 20 due ${COVERAGE_UNIT}`,
      `2026/02/01 to 2026/03/02 · 17 of 20 due ${COVERAGE_UNIT}`,
    ])
    // 37 of 40 due sessions hold bars: exactly "x/y <unit>", the unit from its one constant.
    expect(section.find('[data-testid="coverage-summary"]').text()).toBe(`37/40 ${COVERAGE_UNIT}`)
    expect(section.find('[data-testid="coverage-track"]').attributes('aria-label')).toBe(`37/40 ${COVERAGE_UNIT} covered, 4 gaps`)
    expect(section.findAll('[data-testid="coverage-gaps"] li').map((li) => li.text())).toEqual([
      '2026/02/11 · 1 session',
      '2026/02/15 to 2026/02/16 · 2 sessions',
      'and 2 more gaps not listed',
    ])
    expect(section.find('[data-testid="coverage-no-gaps"]').exists()).toBe(false)
    expect(section.findComponent(CoverageStrip).props('variant')).toBe('blocks')
    // No percent anywhere in the section, visible or in a hover.
    expect(section.text()).not.toContain('%')
    expect(blocks.map((b) => b.attributes('title')).join(' ')).not.toContain('%')
  })

  it('a full answer to the request draws one block per bucket: never more blocks than were asked for', async () => {
    mocks.getDatasetCoverage.mockResolvedValue(stripOf(DETAIL_COVERAGE_BUCKETS, 3))
    const shell = await detailAt()
    const asked = mocks.getDatasetCoverage.mock.calls[0]![1] as number
    const blocks = shell.findAll('#detail-coverage [data-testid="coverage-block"]')
    expect(blocks).toHaveLength(DETAIL_COVERAGE_BUCKETS)
    expect(blocks.length).toBeLessThanOrEqual(asked)
    expect(blocks.length).toBeLessThanOrEqual(20)
    expect(blocks.map((b) => b.attributes('data-state'))).toEqual([
      ...Array<string>(3).fill('partial'),
      ...Array<string>(DETAIL_COVERAGE_BUCKETS - 3).fill('covered'),
    ])
  })

  it('a small dataset (5 sessions, so 5 buckets back) draws 5 blocks, one per session', async () => {
    mocks.getDatasetCoverage.mockResolvedValue(stripOf(5, 1))
    const shell = await detailAt()
    const blocks = shell.findAll('#detail-coverage [data-testid="coverage-block"]')
    expect(blocks).toHaveLength(5)
    expect(blocks.map((b) => b.attributes('data-state'))).toEqual(['partial', 'covered', 'covered', 'covered', 'covered'])
    expect(blocks[0]!.attributes('title')).toBe(`2026/01/02 · 1 of 2 due ${COVERAGE_UNIT}`)
  })

  it('a strip with no gaps says every due session holds bars', async () => {
    mocks.getDatasetCoverage.mockResolvedValue(create(CoverageStripSchema, { buckets: [{ expectedSessions: 5, sessionsWithBars: 5 }] }))
    const shell = await detailAt()
    expect(shell.find('[data-testid="coverage-no-gaps"]').text()).toBe('No gaps: every due session holds bars.')
  })

  it('a 422 says no trading calendar covers this dataset, neutrally', async () => {
    mocks.getDatasetCoverage.mockRejectedValue(new ApiError({ status: 422 }))
    const shell = await detailAt()
    const none = shell.find('#detail-coverage [data-testid="coverage-none"]')
    expect(none.text()).toBe('No trading calendar covers this dataset, so its coverage cannot be shown.')
    expect(shell.find('#detail-coverage [role="alert"]').exists()).toBe(false)
  })

  it('a failure is an inline error, and Try Again asks again', async () => {
    mocks.getDatasetCoverage.mockRejectedValueOnce(new ApiError({ status: 500 }))
    const shell = await detailAt()
    const error = shell.find('#detail-coverage [data-testid="coverage-unavailable"]')
    expect(error.text()).toContain('Could not load coverage.')
    await error.find('button').trigger('click')
    await flushPromises()
    expect(mocks.getDatasetCoverage).toHaveBeenCalledTimes(2)
    expect(shell.find('#detail-coverage [data-testid="coverage-summary"]').exists()).toBe(true)
  })

  it('there is no Request the Gap: its destination is a later phase', async () => {
    const shell = await detailAt()
    expect(main(shell).text()).not.toMatch(/request the gap/i)
  })
})

describe('history (runs)', () => {
  it('columns, and no Quota Key column (the server does not record it per run)', async () => {
    const shell = await detailAt()
    const headers = shell.findAll('[data-testid="runs-table"] th').map((th) => th.text())
    expect(headers).toEqual(['Started', 'Finished', 'Duration', 'Trigger', 'Actor', 'Range', 'Bars Written', 'State'])
    expect(main(shell).text()).not.toMatch(/quota/i)
  })

  it('there is no All Runs link: the Requests screen is a later phase; more runs come by Load More', async () => {
    const shell = await detailAt()
    expect(main(shell).text()).not.toMatch(/all runs/i)
    expect(shell.findAll('main a').map((a) => a.attributes('href'))).not.toContain('/data/requests')
  })

  it('a finished run shows its times, duration, trigger, actor, half-open range, bars and state', async () => {
    mocks.getDatasetRuns.mockResolvedValue(page([run('r1', 0)]))
    const shell = await detailAt()
    const cells = runRows(shell)[0]!.findAll('td').map((td) => td.text())
    expect(cells).toEqual([
      '2026/10/12 10:00',
      '2026/10/12 10:00',
      '12 s',
      'Request',
      'operator',
      '2026/01/02 to 2026/03/02',
      '40',
      'Succeeded',
    ])
  })

  it('a Running run shows no finish and no duration', async () => {
    mocks.getDatasetRuns.mockResolvedValue(page([run('r1', 0, { state: IngestRunState.RUNNING, finishedAt: undefined })]))
    const shell = await detailAt()
    const cells = runRows(shell)[0]!.findAll('td').map((td) => td.text())
    expect(cells[1]).toBe('—')
    expect(cells[2]).toBe('—')
    expect(cells[7]).toBe('Running')
  })

  it('a failed run shows its reason and domain in words, and the server message', async () => {
    mocks.getDatasetRuns.mockResolvedValue(
      page([run('r1', 0, { state: IngestRunState.FAILED, error: { domain: 'VENDOR', reason: 'RATE_LIMITED', message: '429 from vendor' } })]),
    )
    const shell = await detailAt()
    const state = runRows(shell)[0]!.findAll('td').at(-1)!
    expect(state.find('[data-testid="run-error"]').text()).toBe('Rate limited (Vendor)')
    expect(state.text()).toContain('Failed')
    expect(state.text()).toContain('429 from vendor')
  })

  it('no runs yet says so', async () => {
    mocks.getDatasetRuns.mockResolvedValue(page([]))
    const shell = await detailAt()
    expect(shell.find('[data-testid="runs-empty"]').text()).toContain('No collection runs yet.')
    expect(shell.find('[data-testid="runs-table"]').exists()).toBe(false)
  })

  it('a runs failure is the section own error; Try Again reloads the runs only', async () => {
    mocks.getDatasetRuns.mockRejectedValueOnce(new ApiError({ status: 500, errorId: 'r-9' }))
    const shell = await detailAt()
    const error = shell.find('[data-testid="runs-error"]')
    expect(error.text()).toContain('Could not load runs. Request failed (HTTP 500)')
    expect(error.text()).toContain('Error ID: r-9')
    expect(shell.find('[data-testid="detail-title"]').exists()).toBe(true)
    await error.find('button').trigger('click')
    await flushPromises()
    expect(runIds(shell)).toEqual(['r2', 'r1'])
    expect(mocks.getDataset).toHaveBeenCalledTimes(1)
  })

  it('Load More follows next_cursor and appends; it goes when the cursor is empty', async () => {
    mocks.getDatasetRuns.mockResolvedValueOnce(page([run('r9', 9), run('r8', 8)], 'k1'))
    const shell = await detailAt()
    expect(mocks.getDatasetRuns).toHaveBeenLastCalledWith('ds-1', { limit: 50, signal: expect.any(AbortSignal) })
    mocks.getDatasetRuns.mockResolvedValueOnce(page([run('r7', 7)], ''))
    const more = shell.find('[data-testid="runs-load-more"]')
    expect(more.text()).toBe('Load More')
    await more.trigger('click')
    await flushPromises()
    expect(mocks.getDatasetRuns).toHaveBeenLastCalledWith('ds-1', { cursor: 'k1', limit: 50, signal: expect.any(AbortSignal) })
    expect(runIds(shell)).toEqual(['r9', 'r8', 'r7'])
    expect(shell.find('[data-testid="runs-load-more"]').exists()).toBe(false)
  })

  it('a failed Load More keeps the rows and says so', async () => {
    mocks.getDatasetRuns.mockResolvedValueOnce(page([run('r9', 9)], 'k1'))
    const shell = await detailAt()
    mocks.getDatasetRuns.mockRejectedValueOnce(new ApiError({ status: 500 }))
    await shell.find('[data-testid="runs-load-more"]').trigger('click')
    await flushPromises()
    expect(shell.find('[data-testid="runs-more-error"]').text()).toContain('Could not load more runs.')
    expect(runIds(shell)).toEqual(['r9'])
  })
})

// tj-grna9p.89 replaced the phase 2 read-only stub (which pinned 'no control of any kind' and 'Read
// only.') with the live card. The expiry type stays locked: it is shown, never offered as a control.
describe('retention: the card', () => {
  it('shows the real expiry type, locked, and the expiry time; no control edits the type', async () => {
    mocks.getDataset.mockResolvedValue(dataset({ expiryType: ExpiryType.BULK, expiry: timestampFromMs(Date.UTC(2027, 0, 1, 17)) }))
    const shell = await detailAt()
    const section = shell.find('#detail-retention')
    expect(section.find('[data-fact="expiry-type"]').text()).toBe('Bulk')
    expect(section.find('[data-fact="expiry"]').text()).toContain('2027/01/01')
    expect(section.text()).toContain('The expiry type is fixed when the dataset is created.')
    expect(section.findAll('select')).toHaveLength(0)
    expect(section.findAll('input').map((i) => i.attributes('data-testid'))).toEqual(['expiry-input'])
    expect(section.findAll('a')).toHaveLength(0)
  })

  it('no expiry reads None; a retired dataset says it no longer collects', async () => {
    mocks.getDataset.mockResolvedValue(dataset({ expiry: undefined, state: DatasetState.RETIRED }))
    const shell = await detailAt()
    expect(shell.find('[data-fact="expiry"]').text()).toBe('None')
    expect(shell.find('[data-fact="expiry-type"]').text()).toBe('Rolling')
    expect(shell.find('#detail-retention').text()).toContain('Retired: no longer collecting')
  })

  it('the Expiry Time field is empty for Keep Forever, labelled, with Tomorrow and Clear', async () => {
    const shell = await detailAt()
    const input = shell.find('[data-testid="expiry-input"]')
    expect((input.element as HTMLInputElement).value).toBe('')
    expect(input.attributes('placeholder')).toBe('Keep Forever')
    expect(shell.find(`label[for="${input.attributes('id')}"]`).text()).toBe('Expiry Time')
    expect(shell.find('[data-testid="expiry-tomorrow"]').text()).toBe('Tomorrow')
    expect(shell.find('[data-testid="expiry-clear"]').text()).toBe('Clear')
  })

  it('STATIC: the write is Set Expiry, disabled while the field is empty and there is nothing to clear', async () => {
    const shell = await detailAt()
    const apply = shell.find('[data-testid="expiry-apply"]')
    expect(apply.text()).toBe('Set Expiry')
    expect(apply.attributes('disabled')).toBeDefined()
    expect(shell.find('#detail-retention').text()).toContain('The dataset is deleted on that date. Clearing the expiry keeps it.')
    expect(shell.find('#detail-retention').text()).not.toContain('retires this subscription')
  })

  it.each([
    ['DAILY', UpdateType.DAILY],
    ['STREAM', UpdateType.STREAM],
  ])('%s is a subscription: Retire Subscription, and the retire and resume explanation', async (_name, updateType) => {
    mocks.getDataset.mockResolvedValue(dataset({ updateType }))
    const shell = await detailAt()
    expect(shell.find('[data-testid="expiry-apply"]').text()).toBe('Retire Subscription')
    expect(shell.find('#detail-retention').text()).toContain(
      'Setting an expiry retires this subscription at once: it stops updating, stays readable, and is deleted on that date. Clearing the expiry before then resumes it.',
    )
  })
})

// The writes, through the real store and card; the API is mocked at @/api (setDatasetExpiry,
// deleteDataset). Display zone America/Toronto (the beforeEach).
describe('retention: writes', () => {
  // 22:00 on 2026-10-14 in Toronto (EDT), already 2026-10-15 in UTC: Tomorrow is the display zone's.
  const EVENING = Date.UTC(2026, 9, 15, 2)
  const OCT_15_TORONTO = Date.UTC(2026, 9, 15, 4)
  const OCT_16_TORONTO = OCT_15_TORONTO + DAY

  async function at(now: number, d = dataset()) {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(now)
    mocks.getDataset.mockResolvedValue(d)
    return detailAt()
  }
  const input = (shell: VueWrapper) => shell.find('[data-testid="expiry-input"]')
  const value = (shell: VueWrapper) => (input(shell).element as HTMLInputElement).value
  const apply = (shell: VueWrapper) => shell.find('[data-testid="expiry-apply"]')
  const notice = (shell: VueWrapper) => shell.find('[data-testid="retention-notice"]')
  async function click(shell: VueWrapper, testid: string) {
    await shell.find(`[data-testid="${testid}"]`).trigger('click')
    await flushPromises()
  }

  it('Tomorrow fills the next calendar day in the display zone; Set Expiry sends its midnight there', async () => {
    const shell = await at(EVENING)
    await click(shell, 'expiry-tomorrow')
    expect(value(shell)).toBe('2026/10/15')
    await click(shell, 'expiry-apply')
    expect(mocks.setDatasetExpiry).toHaveBeenCalledTimes(1)
    const [id, params] = mocks.setDatasetExpiry.mock.calls[0]!
    expect(id).toBe('ds-1')
    expect((params.expiry as Date).getTime()).toBe(OCT_15_TORONTO)
    expect(params.override).toBeUndefined()
    // 200: the card re-renders from the dataset the server answered with, and the field empties.
    expect(shell.find('[data-fact="expiry"]').text()).toBe('2026/10/15 00:00')
    expect(value(shell)).toBe('')
    expect(notice(shell).exists()).toBe(false)
  })

  it('Tomorrow then Clear round-trips: set, then the empty field clears the expiry (Clear Expiry)', async () => {
    const shell = await at(EVENING)
    await click(shell, 'expiry-tomorrow')
    await click(shell, 'expiry-apply')
    expect(shell.find('[data-fact="expiry"]').text()).toBe('2026/10/15 00:00')
    await click(shell, 'expiry-tomorrow')
    await click(shell, 'expiry-clear')
    expect(value(shell)).toBe('')
    expect(apply(shell).text()).toBe('Clear Expiry')
    expect(apply(shell).attributes('disabled')).toBeUndefined()
    await click(shell, 'expiry-apply')
    expect(mocks.setDatasetExpiry).toHaveBeenCalledTimes(2)
    expect(mocks.setDatasetExpiry.mock.calls[1]![1].expiry).toBeUndefined()
    expect(shell.find('[data-fact="expiry"]').text()).toBe('None')
  })

  it('a retired subscription with an expiry: the empty field reads Resume Subscription and sends the clear', async () => {
    const shell = await at(EVENING, dataset({ updateType: UpdateType.DAILY, state: DatasetState.RETIRED, expiry: timestampFromMs(OCT_16_TORONTO) }))
    expect(apply(shell).text()).toBe('Resume Subscription')
    await click(shell, 'expiry-apply')
    expect(mocks.setDatasetExpiry.mock.calls[0]![1]).toEqual({ expiry: undefined, override: undefined })
  })

  it('a typed day that is not a real YYYY/MM/DD says so, marks the field invalid and sends nothing', async () => {
    const shell = await at(EVENING)
    await input(shell).setValue('2026/02/30')
    expect(shell.find('[data-testid="expiry-problem"]').text()).toContain('YYYY/MM/DD')
    expect(input(shell).attributes('aria-invalid')).toBe('true')
    expect(apply(shell).attributes('disabled')).toBeDefined()
    await input(shell).trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(mocks.setDatasetExpiry).not.toHaveBeenCalled()
  })

  it('the operator own dataset: no owner warning, no reason prompt, and never an override', async () => {
    const shell = await at(EVENING)
    expect(shell.find('[data-testid="override-warning"]').exists()).toBe(false)
    expect(shell.find('[data-testid="override-prompt"]').exists()).toBe(false)
    await click(shell, 'expiry-tomorrow')
    await click(shell, 'expiry-apply')
    await click(shell, 'delete-open')
    await shell.find('[data-testid="delete-typed"]').setValue('VFV 1d')
    await click(shell, 'delete-submit')
    expect(mocks.setDatasetExpiry.mock.calls[0]![1].override).toBeUndefined()
    expect(mocks.deleteDataset).toHaveBeenCalledWith('ds-1', { override: undefined })
  })

  describe('another owner dataset', () => {
    const held = () => dataset({ owner: 'bot-7' })

    it('shows the owner and a warning, and a labelled reason with a confirmation naming the owner', async () => {
      const shell = await at(EVENING, held())
      expect(shell.find('[data-testid="override-warning"]').text()).toContain('Held by bot-7.')
      const reason = shell.find('[data-testid="override-reason"]')
      expect(shell.find(`label[for="${reason.attributes('id')}"]`).text()).toBe('Reason')
      expect(shell.find('[data-testid="override-prompt"]').text()).toContain('I confirm I am acting on a dataset held by bot-7.')
    })

    it('cannot submit without a non-blank reason AND the confirmation', async () => {
      const shell = await at(EVENING, held())
      await click(shell, 'expiry-tomorrow')
      expect(apply(shell).attributes('disabled')).toBeDefined()
      await shell.find('[data-testid="override-reason"]').setValue('   ')
      await shell.find('[data-testid="override-confirm"]').setValue(true)
      expect(apply(shell).attributes('disabled')).toBeDefined()
      await shell.find('[data-testid="override-reason"]').setValue('owner asked')
      await shell.find('[data-testid="override-confirm"]').setValue(false)
      expect(apply(shell).attributes('disabled')).toBeDefined()
      await input(shell).trigger('keydown', { key: 'Enter' })
      await flushPromises()
      expect(mocks.setDatasetExpiry).not.toHaveBeenCalled()
    })

    it('with both, sends override {reason (trimmed), confirmed: true}; the prompt then empties', async () => {
      mocks.setDatasetExpiry.mockResolvedValue(held())
      const shell = await at(EVENING, held())
      await click(shell, 'expiry-tomorrow')
      await shell.find('[data-testid="override-reason"]').setValue('  owner asked  ')
      await shell.find('[data-testid="override-confirm"]').setValue(true)
      await click(shell, 'expiry-apply')
      expect(mocks.setDatasetExpiry.mock.calls[0]![1].override).toEqual({ reason: 'owner asked', confirmed: true })
      expect((shell.find('[data-testid="override-reason"]').element as HTMLTextAreaElement).value).toBe('')
      expect((shell.find('[data-testid="override-confirm"]').element as HTMLInputElement).checked).toBe(false)
    })

    it('Delete Now needs the typed full name and the reason and confirmation, then sends the override', async () => {
      const shell = await at(EVENING, held())
      await click(shell, 'delete-open')
      await shell.find('[data-testid="delete-typed"]').setValue('VFV 1d')
      expect(shell.find('[data-testid="delete-submit"]').attributes('disabled')).toBeDefined()
      await shell.find('[data-testid="override-reason"]').setValue('decommissioned')
      await shell.find('[data-testid="override-confirm"]').setValue(true)
      expect(shell.find('[data-testid="delete-submit"]').attributes('disabled')).toBeUndefined()
      await click(shell, 'delete-submit')
      expect(mocks.deleteDataset).toHaveBeenCalledWith('ds-1', { override: { reason: 'decommissioned', confirmed: true } })
    })
  })

  describe('Delete Now', () => {
    // tj-grna9p.89 round 3 (architect): the typed confirmation names ONE dataset, the symbol and its
    // granularity label ('VFV 1d'), not the bare symbol every granularity of VFV shares.
    it('opens a typed confirmation naming the full name and the bar count; prompt, field and dialog show the same string', async () => {
      const shell = await at(EVENING)
      expect(shell.find('[data-testid="delete-confirm"]').exists()).toBe(false)
      await click(shell, 'delete-open')
      const confirm = shell.find('[data-testid="delete-confirm"]')
      expect(confirm.text()).toContain(`This deletes VFV 1d and its ${formatNumber(12345)} bars at once.`)
      expect(confirm.text()).toContain('It cannot be undone. Type VFV 1d to confirm.')
      expect(confirm.find('code').text()).toBe('VFV 1d')
      expect(shell.find('[data-testid="delete-typed"]').attributes('aria-label')).toBe('Type VFV 1d to confirm')
      expect(confirm.attributes('aria-label')).toBe('Delete VFV 1d')
    })

    it.each([
      ['the bare symbol', 'VFV'],
      ['another granularity of the symbol', 'VFV 1h'],
      ['the wrong case', 'vfv 1d'],
      ['the right case of the symbol, wrong case of the unit', 'VFV 1D'],
      ['no space', 'VFV1d'],
      ['two spaces inside', 'VFV  1d'],
    ])('%s (%s) does not enable Delete Now, and Enter sends nothing', async (_name, text) => {
      const shell = await at(EVENING)
      await click(shell, 'delete-open')
      await shell.find('[data-testid="delete-typed"]').setValue(text)
      expect(shell.find('[data-testid="delete-submit"]').attributes('disabled')).toBeDefined()
      expect(shell.find('[data-testid="delete-hint"]').text()).toBe('Type the full name shown, VFV 1d, to enable Delete Now.')
      await shell.find('[data-testid="delete-typed"]').trigger('keydown', { key: 'Enter' })
      await shell.find('[data-testid="delete-submit"]').trigger('click')
      await flushPromises()
      expect(mocks.deleteDataset).not.toHaveBeenCalled()
    })

    it('the full name with surrounding whitespace confirms (trimmed), and the delete is sent', async () => {
      const shell = await at(EVENING)
      await click(shell, 'delete-open')
      await shell.find('[data-testid="delete-typed"]').setValue('  VFV 1d \t')
      expect(shell.find('[data-testid="delete-submit"]').attributes('disabled')).toBeUndefined()
      await click(shell, 'delete-submit')
      expect(mocks.deleteDataset).toHaveBeenCalledTimes(1)
    })

    it.each([
      ['1d', Granularity.ONE_DAY],
      ['1h', Granularity.ONE_HOUR],
    ])('the name uses the same granularity label as the page heading (%s)', async (label, granularity) => {
      const shell = await at(EVENING, dataset({ granularity }))
      const headingGranularity = shell.find('[data-testid="detail-title"]').text().split(' · ').at(-1)
      expect(headingGranularity).toBe(label)
      await click(shell, 'delete-open')
      expect(shell.find('[data-testid="delete-confirm"]').attributes('aria-label')).toBe(`Delete VFV ${headingGranularity}`)
      await shell.find('[data-testid="delete-typed"]').setValue(`VFV ${headingGranularity}`)
      expect(shell.find('[data-testid="delete-submit"]').attributes('disabled')).toBeUndefined()
    })

    it('Cancel closes it and sends nothing; reopening starts with an empty field', async () => {
      const shell = await at(EVENING)
      await click(shell, 'delete-open')
      await shell.find('[data-testid="delete-typed"]').setValue('VFV 1d')
      await click(shell, 'delete-cancel')
      expect(shell.find('[data-testid="delete-confirm"]').exists()).toBe(false)
      await click(shell, 'delete-open')
      expect((shell.find('[data-testid="delete-typed"]').element as HTMLInputElement).value).toBe('')
      expect(mocks.deleteDataset).not.toHaveBeenCalled()
    })

    it('a labelled dialog: opening focuses the typed field, Cancel gives focus back to Delete Now', async () => {
      const shell = await at(EVENING)
      await shell.find('[data-testid="delete-open"]').trigger('click')
      await flushPromises()
      const dialog = shell.find('[data-testid="delete-confirm"]')
      expect(dialog.attributes('role')).toBe('dialog')
      expect(dialog.attributes('aria-label')).toBe('Delete VFV 1d')
      expect(document.activeElement).toBe(shell.find('[data-testid="delete-typed"]').element)
      await shell.find('[data-testid="delete-cancel"]').trigger('click')
      await flushPromises()
      expect(document.activeElement).toBe(shell.find('[data-testid="delete-open"]').element)
    })

    it('says what still blocks Delete Now, tied to the button; nothing once it is enabled', async () => {
      const shell = await at(EVENING, dataset({ owner: 'bot-7' }))
      await click(shell, 'delete-open')
      const hint = () => shell.find('[data-testid="delete-hint"]')
      const submit = () => shell.find('[data-testid="delete-submit"]')
      expect(hint().text()).toBe('Type the full name shown, VFV 1d, to enable Delete Now.')
      expect(submit().attributes('aria-describedby')).toBe(hint().attributes('id'))
      // The bare symbol is still 'type the full name', ahead of the reason and the confirmation.
      await shell.find('[data-testid="delete-typed"]').setValue('VFV')
      expect(hint().text()).toBe('Type the full name shown, VFV 1d, to enable Delete Now.')
      await shell.find('[data-testid="delete-typed"]').setValue('VFV 1d')
      expect(hint().text()).toBe('Give a reason above to enable Delete Now.')
      await shell.find('[data-testid="override-reason"]').setValue('  ')
      expect(hint().text()).toBe('Give a reason above to enable Delete Now.')
      await shell.find('[data-testid="override-reason"]').setValue('decommissioned')
      expect(hint().text()).toBe('Tick the confirmation above to enable Delete Now.')
      expect(submit().attributes('aria-describedby')).toBe(hint().attributes('id'))
      await shell.find('[data-testid="override-confirm"]').setValue(true)
      expect(hint().exists()).toBe(false)
      expect(submit().attributes('aria-describedby')).toBeUndefined()
      expect(submit().attributes('disabled')).toBeUndefined()
    })

    it('the operator own dataset: once the full name is typed nothing blocks it', async () => {
      const shell = await at(EVENING)
      await click(shell, 'delete-open')
      await shell.find('[data-testid="delete-typed"]').setValue('VFV 1d')
      expect(shell.find('[data-testid="delete-hint"]').exists()).toBe(false)
    })

    it('204: the screen goes back to Datasets', async () => {
      const shell = await at(EVENING)
      await click(shell, 'delete-open')
      await shell.find('[data-testid="delete-typed"]').setValue('VFV 1d')
      await click(shell, 'delete-submit')
      expect(mocks.deleteDataset).toHaveBeenCalledTimes(1)
      expect(router.currentRoute.value.name).toBe('data-datasets')
    })
  })

  describe('refusals leave the card unchanged', () => {
    const REASON = 'my-private-reason'

    async function heldWithChoice() {
      const shell = await at(EVENING, dataset({ owner: 'bot-7', expiry: timestampFromMs(OCT_16_TORONTO) }))
      await click(shell, 'expiry-tomorrow')
      await shell.find('[data-testid="override-reason"]').setValue(REASON)
      await shell.find('[data-testid="override-confirm"]').setValue(true)
      return shell
    }
    function unchanged(shell: VueWrapper) {
      expect(shell.find('[data-fact="expiry"]').text()).toBe('2026/10/16 00:00')
      expect(value(shell)).toBe('2026/10/15')
      expect((shell.find('[data-testid="override-reason"]').element as HTMLTextAreaElement).value).toBe(REASON)
      // Neither the reason nor the owner is ever echoed in a refusal.
      expect(notice(shell).text()).not.toContain(REASON)
      expect(notice(shell).text()).not.toContain('bot-7')
      expect(notice(shell).attributes('role')).toBe('alert')
    }

    it('409 OVERRIDE_REQUIRED re-prompts for the reason, refetches the dataset, and never names the owner', async () => {
      const shell = await heldWithChoice()
      mocks.setDatasetExpiry.mockRejectedValueOnce(new ApiError({ status: 409, reason: 'OVERRIDE_REQUIRED' }))
      const before = mocks.getDataset.mock.calls.length
      await click(shell, 'expiry-apply')
      expect(notice(shell).text()).toBe('This dataset is held by another owner. Give a reason and confirm to continue.')
      expect(shell.find('[data-testid="override-prompt"]').exists()).toBe(true)
      expect(mocks.getDataset.mock.calls.length).toBe(before + 1)
      unchanged(shell)
    })

    it('409 on the operator own dataset that changed hands: the refetch brings the owner and the prompt', async () => {
      const shell = await at(EVENING)
      await click(shell, 'expiry-tomorrow')
      mocks.setDatasetExpiry.mockRejectedValueOnce(new ApiError({ status: 409, reason: 'OVERRIDE_REQUIRED' }))
      mocks.getDataset.mockResolvedValue(dataset({ owner: 'bot-7' }))
      await click(shell, 'expiry-apply')
      expect(shell.find('[data-testid="override-warning"]').text()).toContain('Held by bot-7.')
      expect(shell.find('[data-testid="override-prompt"]').exists()).toBe(true)
      expect(apply(shell).attributes('disabled')).toBeDefined()
    })

    it('503 DATABASE_UNAVAILABLE says run make migrate, with the error id, and changes nothing', async () => {
      const shell = await heldWithChoice()
      mocks.setDatasetExpiry.mockRejectedValueOnce(new ApiError({ status: 503, reason: 'DATABASE_UNAVAILABLE', errorId: 'e-503' }))
      await click(shell, 'expiry-apply')
      expect(notice(shell).text()).toContain('nothing was changed. Run make migrate, then try again.')
      expect(notice(shell).text()).toContain('Error ID: e-503')
      unchanged(shell)
    })

    it('422 is shown as a bug in the UI, with the error id', async () => {
      const shell = await heldWithChoice()
      mocks.setDatasetExpiry.mockRejectedValueOnce(new ApiError({ status: 422, reason: 'INVALID_REQUEST', errorId: 'e-422' }))
      await click(shell, 'expiry-apply')
      expect(notice(shell).text()).toContain('This is a bug in the UI; nothing was changed.')
      expect(notice(shell).text()).toContain('Error ID: e-422')
      unchanged(shell)
    })

    it('any other failure says it could not complete and nothing changed', async () => {
      const shell = await heldWithChoice()
      mocks.setDatasetExpiry.mockRejectedValueOnce(new ApiError({ status: 500, errorId: 'e-500' }))
      await click(shell, 'expiry-apply')
      expect(notice(shell).text()).toContain('Could not complete the change. Request failed (HTTP 500) Nothing was changed.')
      unchanged(shell)
    })

    it('a refused delete keeps the confirmation open and the dataset on screen', async () => {
      const shell = await heldWithChoice()
      mocks.deleteDataset.mockRejectedValueOnce(new ApiError({ status: 503, reason: 'DATABASE_UNAVAILABLE' }))
      await click(shell, 'delete-open')
      await shell.find('[data-testid="delete-typed"]').setValue('VFV 1d')
      await click(shell, 'delete-submit')
      expect(router.currentRoute.value.name).toBe('data-dataset-detail')
      expect(shell.find('[data-testid="delete-confirm"]').exists()).toBe(true)
      expect(notice(shell).text()).toContain('Run make migrate')
      expect(shell.find('[data-testid="detail-title"]').exists()).toBe(true)
    })

    it('404 on a write: the dataset is gone, the screen shows the deleted state with the way back', async () => {
      const shell = await heldWithChoice()
      mocks.setDatasetExpiry.mockRejectedValueOnce(new ApiError({ status: 404, reason: 'NOT_FOUND' }))
      await click(shell, 'expiry-apply')
      const panel = shell.find('[data-testid="detail-deleted"]')
      expect(panel.text()).toContain('This dataset was deleted.')
      expect(panel.find('a').attributes('href')).toMatch(/^\/data\/datasets(\?|$)/)
    })
  })

  // A write answer for a dataset the page has left is the store's 'stale' outcome: the card ignores
  // it (no navigation, no notice) and the page keeps the dataset it now shows.
  describe('late answers after leaving for another dataset', () => {
    const DS2 = () => dataset({ id: 'ds-2', assetSymbol: 'XIU' })

    async function leaveWhileWriting(start: (shell: VueWrapper) => Promise<void>) {
      const shell = await at(EVENING)
      await start(shell)
      mocks.getDataset.mockResolvedValue(DS2())
      await router.push('/data/datasets/ds-2')
      await flushPromises()
      expect(shell.find('[data-testid="detail-title"]').text()).toBe('XIU · bars · 1d')
      return shell
    }

    it('a late Delete Now success does not navigate, and says nothing', async () => {
      let answer: (value: unknown) => void = () => undefined
      mocks.deleteDataset.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)))
      const shell = await leaveWhileWriting(async (s) => {
        await click(s, 'delete-open')
        await s.find('[data-testid="delete-typed"]').setValue('VFV 1d')
        await click(s, 'delete-submit')
      })
      answer(undefined)
      await flushPromises()
      expect(router.currentRoute.value.name).toBe('data-dataset-detail')
      expect(router.currentRoute.value.params.id).toBe('ds-2')
      expect(shell.find('[data-testid="retention-notice"]').exists()).toBe(false)
      expect(shell.find('[data-testid="detail-title"]').text()).toBe('XIU · bars · 1d')
    })

    it('a late Set Expiry answer leaves ds-2 on screen; a late refusal shows no notice and no deleted state', async () => {
      let answer: (value: unknown) => void = () => undefined
      mocks.setDatasetExpiry.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)))
      const shell = await leaveWhileWriting(async (s) => {
        await click(s, 'expiry-tomorrow')
        await click(s, 'expiry-apply')
      })
      answer(dataset({ expiry: timestampFromMs(OCT_15_TORONTO) }))
      await flushPromises()
      expect(shell.find('[data-testid="detail-title"]').text()).toBe('XIU · bars · 1d')
      expect(shell.find('[data-fact="expiry"]').text()).toBe('None')
      expect(shell.find('[data-testid="retention-notice"]').exists()).toBe(false)
    })

    it('a late 404 for the left dataset does not mark the new one deleted', async () => {
      let fail: (error: unknown) => void = () => undefined
      mocks.setDatasetExpiry.mockImplementationOnce(() => new Promise((_resolve, reject) => (fail = reject)))
      const shell = await leaveWhileWriting(async (s) => {
        await click(s, 'expiry-tomorrow')
        await click(s, 'expiry-apply')
      })
      fail(new ApiError({ status: 404, reason: 'NOT_FOUND' }))
      await flushPromises()
      expect(shell.find('[data-testid="detail-deleted"]').exists()).toBe(false)
      expect(shell.find('[data-testid="retention-notice"]').exists()).toBe(false)
    })
  })

  it('under Read By UI, a Set Expiry keeps the Usage facts and shows the answer expiry and reaper banner', async () => {
    const usage = { lastReadAt: timestampFromMs(EVENING - 2 * DAY), readByClients30d: ['operator'] }
    const shell = await at(EVENING, dataset({ usage }))
    await shell.find('#detail-usage [data-read-by="UI"] input').trigger('change')
    await flushPromises()
    await shell.find('#detail-usage [data-read-by="SDK"] input').trigger('change')
    await flushPromises()
    expect(shell.find('#detail-usage [data-fact="read-by"]').text()).toBe('operator')
    mocks.setDatasetExpiry.mockResolvedValueOnce(
      dataset({ expiry: timestampFromMs(OCT_15_TORONTO), reaperPending: true, usage: { readByClients30d: ['bot-sdk'] } }),
    )
    await click(shell, 'expiry-tomorrow')
    await click(shell, 'expiry-apply')
    expect(shell.find('[data-fact="expiry"]').text()).toBe('2026/10/15 00:00')
    expect(shell.find('[data-testid="reaper-banner"]').exists()).toBe(true)
    expect(shell.find('#detail-usage [data-fact="read-by"]').text()).toBe('operator')
    expect(shell.find('#detail-usage [data-fact="last-read"]').text()).toBe('2 days ago')
  })

  it('while a write is in flight every control of the card waits', async () => {
    const shell = await at(EVENING)
    mocks.setDatasetExpiry.mockReturnValue(new Promise(() => undefined))
    await click(shell, 'expiry-tomorrow')
    await click(shell, 'expiry-apply')
    const section = shell.find('#detail-retention')
    for (const el of section.findAll('button, input')) expect(el.attributes('disabled')).toBeDefined()
  })
})

describe('reaper banner', () => {
  it('shows when the server says the reaper is pending, and says what dry-run means', async () => {
    mocks.getDataset.mockResolvedValue(dataset({ reaperPending: true }))
    const shell = await detailAt()
    const banner = shell.find('[data-testid="reaper-banner"]')
    expect(banner.attributes('role')).toBe('status')
    expect(banner.text()).toBe(
      'This dataset is past its expiry and the reaper will delete it. While deletes are disabled (dry-run) nothing is deleted.',
    )
  })

  it('is absent otherwise; the page never computes it from the expiry', async () => {
    mocks.getDataset.mockResolvedValue(dataset({ expiry: timestampFromMs(Date.UTC(2020, 0, 1)), reaperPending: false }))
    const shell = await detailAt()
    expect(shell.find('[data-testid="reaper-banner"]').exists()).toBe(false)
  })
})

// The Usage section (tj-grna9p.89, owner ruling tj-grna9p.131): Last Read and Read By from the
// dataset's own usage under the Read By choice, and Reads per Day (30 d) per client.
describe('usage', () => {
  const NOW = Date.UTC(2026, 9, 14, 12)
  const fact = (shell: VueWrapper, name: string) => shell.find(`#detail-usage [data-fact="${name}"]`).text()

  async function usageAt(usage: MessageInitShape<typeof DatasetSummarySchema>['usage']) {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
    mocks.getDataset.mockResolvedValue(dataset({ usage }))
    return detailAt()
  }

  it('Last Read is the age in UTC days of the last read by the selected clients; never the word Stale', async () => {
    const shell = await usageAt({ lastReadAt: timestampFromMs(NOW - 200 * DAY), readByClients30d: [] })
    expect(fact(shell, 'last-read')).toBe('200 days ago')
    expect(shell.find('#detail-usage').text()).not.toMatch(/stale/i)
  })

  it('a read today reads today; one day ago reads 1 day ago', async () => {
    const today = await usageAt({ lastReadAt: timestampFromMs(NOW - 60_000) })
    expect(fact(today, 'last-read')).toBe('today')
    mounted.splice(0).forEach((w) => w.unmount())
    const yesterday = await usageAt({ lastReadAt: timestampFromMs(NOW - DAY) })
    expect(fact(yesterday, 'last-read')).toBe('1 day ago')
  })

  it('no read by the selected clients is Never read', async () => {
    const shell = await usageAt(undefined)
    expect(fact(shell, 'last-read')).toBe('Never read')
    expect(fact(shell, 'read-by')).toBe('No reads in the last 30 days')
  })

  it('Read By lists the clients the server names for the last 30 days, as served', async () => {
    const shell = await usageAt({ lastReadAt: timestampFromMs(NOW), readByClients30d: ['bot-1', 'operator'] })
    expect(fact(shell, 'read-by')).toBe('bot-1, operator')
  })

  it('the section has the Read By toggle in its header, default SDK; the sidebar jumps to it', async () => {
    const shell = await detailAt()
    const head = shell.find('#detail-usage .block__head')
    expect(head.find('h2').text()).toBe('Usage')
    expect((head.find('[data-read-by="SDK"] input').element as HTMLInputElement).checked).toBe(true)
    expect((head.find('[data-read-by="UI"] input').element as HTMLInputElement).checked).toBe(false)
    // The unit is never title-cased: '30 d', not '30 D'.
    expect(shell.find('#detail-usage h3').text()).toBe('Reads per Day (30 d)')
  })

  it('the mount asks for this dataset reads over 30 days under SDK, and the dataset under SDK', async () => {
    await detailAt()
    expect(mocks.getDataset).toHaveBeenCalledWith('ds-1', expect.any(AbortSignal), { clientKinds: [ClientKind.SDK] })
    expect(mocks.getReadsPerDay).toHaveBeenCalledWith({
      days: 30,
      datasetId: 'ds-1',
      clientKinds: [ClientKind.SDK],
      signal: expect.any(AbortSignal),
    })
  })

  it('ticking UI asks for the dataset usage and the reads again under SDK and UI, softly', async () => {
    const shell = await detailAt()
    vi.clearAllMocks()
    mocks.getDataset.mockResolvedValue(dataset({ usage: { lastReadAt: timestampFromMs(Date.now()), readByClients30d: ['operator'] } }))
    mocks.getReadsPerDay.mockResolvedValue(create(ReadsPerDaySchema, {}))
    await shell.find('#detail-usage [data-read-by="UI"] input').trigger('change')
    await flushPromises()
    const kinds = mocks.getDataset.mock.calls[0]![2].clientKinds as ClientKind[]
    expect([...kinds].sort()).toEqual([ClientKind.SDK, ClientKind.UI].sort())
    expect([...(mocks.getReadsPerDay.mock.calls[0]![0].clientKinds as ClientKind[])].sort()).toEqual(kinds.sort())
    expect(mocks.getDatasetRuns).not.toHaveBeenCalled()
    expect(shell.find('[data-testid="detail-loading"]').exists()).toBe(false)
    expect(fact(shell, 'read-by')).toBe('operator')
  })

  it('reads with entries draw the chart; none says so in a line', async () => {
    mocks.getReadsPerDay.mockResolvedValue(
      create(ReadsPerDaySchema, { days: [{ day: new Date().toISOString().slice(0, 10), clientId: 'bot-1', reads: 2n }] }),
    )
    const chart = await detailAt()
    expect(chart.find('#detail-usage [data-testid="reads-chart"]').exists()).toBe(true)
    mounted.splice(0).forEach((w) => w.unmount())
    mocks.getReadsPerDay.mockResolvedValue(create(ReadsPerDaySchema, {}))
    const empty = await detailAt()
    expect(empty.find('#detail-usage [data-testid="reads-empty"]').exists()).toBe(true)
  })

  it('while the reads load it says so; a failure is the section own alert with the error id', async () => {
    mocks.getReadsPerDay.mockReturnValue(new Promise(() => undefined))
    const loading = await detailAt()
    expect(loading.find('[data-testid="reads-loading"]').text()).toBe('Loading reads')
    mounted.splice(0).forEach((w) => w.unmount())
    mocks.getReadsPerDay.mockRejectedValue(new ApiError({ status: 500, errorId: 'rd-9' }))
    const failed = await detailAt()
    const error = failed.find('[data-testid="reads-error"]')
    expect(error.attributes('role')).toBe('alert')
    expect(error.text()).toContain('Could not load reads. Request failed (HTTP 500)')
    expect(error.text()).toContain('Error ID: rd-9')
    expect(failed.find('[data-testid="detail-title"]').exists()).toBe(true)
  })
})

// The spec mounts AppShell and the router's root record is AppShell too, so the screen renders in
// the inner (last) shell; the outer one's sidebar is the root record's, which is empty.
const sidebar = (shell: VueWrapper) => shell.findAll('aside.app-shell__sidebar').at(-1)!
const VIEWER_HREF = /\/viewer(\?|$)/

// tj-grna9p.116 (owner 2026-10-13): the sidebar's Rows item led back to the Viewer, the Detail's
// parent: a loop. It is gone, reversing the keep recorded on tj-grna9p.113. The sidebar goes up (All
// Datasets) and within the page (the sections) only; the way back to the Viewer is the crumb.
describe('sidebar', () => {
  // tj-grna9p.89 made Usage live: it was a disabled 'Not built yet' item in phase 2.
  it('All Datasets and the five sections, Usage now live; no Rows, nothing to the Viewer', async () => {
    const shell = await detailAt()
    const side = sidebar(shell)
    expect(side.find('[data-testid="back-to-datasets"]').text()).toBe('All Datasets')
    expect(side.find('[data-testid="back-to-datasets"]').attributes('href')).toMatch(/^\/data\/datasets(\?|$)/)
    expect(side.findAll('[data-section]').map((s) => s.text())).toEqual([
      'Overview',
      'Coverage',
      'History',
      'Retention',
      'Usage',
    ])
    expect(side.find('[data-section="rows"]').exists()).toBe(false)
    expect(side.text()).not.toMatch(/\brows\b/i)
    expect(side.findAll('a').filter((a) => VIEWER_HREF.test(a.attributes('href') ?? ''))).toHaveLength(0)
    const usage = side.find('[data-section="usage"]')
    expect(usage.attributes('disabled')).toBeUndefined()
    expect(usage.attributes('title')).toBeUndefined()
    expect(side.text()).not.toContain('Not built yet')
    // The section it jumps to exists on the page.
    expect(shell.find('#detail-usage').exists()).toBe(true)
  })

  it('lists exactly All Datasets and the five sections: one link, five live buttons', async () => {
    const shell = await detailAt()
    const side = sidebar(shell)
    expect(side.findAll('a, button').map((el) => [el.element.tagName, el.text()])).toEqual([
      ['A', 'All Datasets'],
      ['BUTTON', 'Overview'],
      ['BUTTON', 'Coverage'],
      ['BUTTON', 'History'],
      ['BUTTON', 'Retention'],
      ['BUTTON', 'Usage'],
    ])
    // All five are live in-page jumps; none is disabled.
    expect(side.findAll('button[disabled]')).toHaveLength(0)
  })

  it('following All Datasets opens the catalog', async () => {
    const shell = await detailAt()
    await sidebar(shell).find('[data-testid="back-to-datasets"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('data-datasets')
  })
})

// Every way out of the Detail (tj-grna9p.113, .116): up to the catalog by the Datasets crumb or the
// sidebar's All Datasets, and to the Viewer by the dataset crumb only, which exists only while the
// dataset is ready. Collected over the whole mounted shell, so a Viewer link anywhere is caught.
describe('ways out', () => {
  const screenLinks = (shell: VueWrapper) =>
    [...shell.find('section.detail').findAll('a'), ...sidebar(shell).findAll('a')].map((a) => [
      a.attributes('data-testid'),
      a.attributes('href'),
    ])
  const viewerLinks = (shell: VueWrapper) => shell.findAll('a').filter((a) => VIEWER_HREF.test(a.attributes('href') ?? ''))

  it('a ready dataset: the Datasets crumb and All Datasets go to the catalog, the symbol crumb to the Viewer, nothing else', async () => {
    const shell = await detailAt()
    expect(screenLinks(shell)).toEqual([
      ['breadcrumb-link', '/data/datasets'],
      ['breadcrumb-link', '/data/datasets/ds-1/viewer'],
      ['back-to-datasets', '/data/datasets'],
    ])
    expect(viewerLinks(shell).map((a) => [a.attributes('data-testid'), a.text()])).toEqual([['breadcrumb-link', 'VFV']])
  })

  it('while loading there is no way to the Viewer at all', async () => {
    mocks.getDataset.mockReturnValue(new Promise(() => undefined))
    const shell = await detailAt()
    expect(shell.find('[data-testid="detail-loading"]').exists()).toBe(true)
    expect(viewerLinks(shell)).toHaveLength(0)
    expect(sidebar(shell).find('[data-testid="back-to-datasets"]').attributes('href')).toBe('/data/datasets')
  })

  it('a dataset not found has no way to the Viewer', async () => {
    mocks.getDataset.mockRejectedValue(new ApiError({ status: 404 }))
    const shell = await detailAt('nope')
    expect(shell.find('[data-testid="detail-not-found"]').exists()).toBe(true)
    expect(viewerLinks(shell)).toHaveLength(0)
  })

  it('a deleted dataset has no way to the Viewer', async () => {
    const shell = await detailAt()
    fakeEvents.emit({ event: { case: 'datasetChanged', value: { id: 'ds-1', change: DatasetChange.DELETED } } })
    await flushPromises()
    expect(shell.find('[data-testid="detail-deleted"]').exists()).toBe(true)
    expect(viewerLinks(shell)).toHaveLength(0)
  })
})

describe('live updates', () => {
  async function liveDetail(): Promise<VueWrapper> {
    const shell = await detailAt()
    vi.clearAllMocks()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    return shell
  }

  const runEvent = (r: MessageInitShape<typeof IngestRunSchema>) =>
    fakeEvents.emit({ event: { case: 'ingestRunChanged', value: { run: r } } })
  const datasetEvent = (id: string, change: DatasetChange) =>
    fakeEvents.emit({ event: { case: 'datasetChanged', value: { id, change } } })

  it('an IngestRunChanged for this dataset puts its row in place at once, without a runs fetch', async () => {
    const shell = await liveDetail()
    runEvent(run('r3', 3, { state: IngestRunState.RUNNING, finishedAt: undefined }))
    await flushPromises()
    expect(runIds(shell)).toEqual(['r3', 'r2', 'r1'])
    expect(runRows(shell)[0]!.attributes('data-state')).toBe('Running')
    runEvent(run('r3', 3, { state: IngestRunState.SUCCEEDED }))
    await flushPromises()
    expect(runIds(shell)).toEqual(['r3', 'r2', 'r1'])
    expect(runRows(shell)[0]!.attributes('data-state')).toBe('Succeeded')
    expect(mocks.getDatasetRuns).not.toHaveBeenCalled()
  })

  it('a burst of events refetches the overview and the coverage ONCE, after COALESCE_MS', async () => {
    await liveDetail()
    runEvent(run('r3', 3, { state: IngestRunState.RUNNING }))
    runEvent(run('r3', 3, { state: IngestRunState.SUCCEEDED }))
    datasetEvent('ds-1', DatasetChange.UPDATED)
    vi.advanceTimersByTime(COALESCE_MS - 1)
    await flushPromises()
    expect(mocks.getDataset).not.toHaveBeenCalled()
    expect(mocks.getDatasetCoverage).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    await flushPromises()
    expect(mocks.getDataset).toHaveBeenCalledTimes(1)
    expect(mocks.getDatasetCoverage).toHaveBeenCalledTimes(1)
    // The refetch asks for the screen's own bucket count, the same cache entry the mount filled.
    expect(mocks.getDatasetCoverage).toHaveBeenCalledWith('ds-1', DETAIL_COVERAGE_BUCKETS, expect.any(AbortSignal))
    vi.advanceTimersByTime(COALESCE_MS * 10)
    await flushPromises()
    expect(mocks.getDataset).toHaveBeenCalledTimes(1)
  })

  it('the refetch is soft: the overview and the strip stay on screen while it is in flight', async () => {
    const shell = await liveDetail()
    mocks.getDataset.mockReturnValue(new Promise(() => undefined))
    mocks.getDatasetCoverage.mockReturnValue(new Promise(() => undefined))
    datasetEvent('ds-1', DatasetChange.UPDATED)
    vi.advanceTimersByTime(COALESCE_MS)
    await flushPromises()
    expect(shell.find('[data-testid="detail-loading"]').exists()).toBe(false)
    expect(shell.find('[data-testid="detail-title"]').text()).toBe('VFV · bars · 1d')
    expect(shell.find('#detail-coverage [data-testid="coverage-summary"]').exists()).toBe(true)
    expect(shell.find('#detail-coverage [data-testid="coverage-loading"]').exists()).toBe(false)
  })

  it('a soft refetch that fails keeps the data and shows the last-data notice', async () => {
    const shell = await liveDetail()
    mocks.getDataset.mockRejectedValue(new ApiError({ status: 503 }))
    datasetEvent('ds-1', DatasetChange.UPDATED)
    vi.advanceTimersByTime(COALESCE_MS)
    await flushPromises()
    expect(shell.find('[data-testid="detail-title"]').exists()).toBe(true)
    expect(shell.find('[data-testid="detail-refresh-problem"]').text()).toBe(
      'Could not refresh. Request failed (HTTP 503) What is shown is the last data that arrived.',
    )
  })

  it('events for ANOTHER dataset do nothing: no row, no refetch, no deleted state', async () => {
    const shell = await liveDetail()
    runEvent(run('x1', 9, { datasetId: 'ds-2' }))
    runEvent(run('x2', 9, { datasetId: '' }))
    datasetEvent('ds-2', DatasetChange.UPDATED)
    datasetEvent('ds-2', DatasetChange.DELETED)
    vi.advanceTimersByTime(COALESCE_MS * 4)
    await flushPromises()
    expect(runIds(shell)).toEqual(['r2', 'r1'])
    expect(mocks.getDataset).not.toHaveBeenCalled()
    expect(mocks.getDatasetCoverage).not.toHaveBeenCalled()
    expect(shell.find('[data-testid="detail-deleted"]').exists()).toBe(false)
  })

  it('a DELETED DatasetChanged for this id shows the deleted state with the way back, and refetches nothing', async () => {
    const shell = await liveDetail()
    datasetEvent('ds-1', DatasetChange.DELETED)
    await flushPromises()
    const panel = shell.find('[data-testid="detail-deleted"]')
    expect(panel.text()).toContain('This dataset was deleted.')
    expect(panel.find('h1').text()).toBe('VFV · bars · 1d')
    expect(panel.find('a').attributes('href')).toMatch(/^\/data\/datasets(\?|$)/)
    runEvent(run('r3', 3))
    vi.advanceTimersByTime(COALESCE_MS * 2)
    await flushPromises()
    expect(mocks.getDataset).not.toHaveBeenCalled()
    expect(mocks.getDatasetCoverage).not.toHaveBeenCalled()
  })

  it('a refetch that finds the dataset gone (404) shows the deleted state too', async () => {
    const shell = await liveDetail()
    mocks.getDataset.mockRejectedValue(new ApiError({ status: 404 }))
    datasetEvent('ds-1', DatasetChange.UPDATED)
    vi.advanceTimersByTime(COALESCE_MS)
    await flushPromises()
    expect(shell.find('[data-testid="detail-deleted"]').exists()).toBe(true)
    expect(shell.find('[data-testid="detail-not-found"]').exists()).toBe(false)
  })

  it('a reconnect refetches the overview, the runs and the coverage once each, softly', async () => {
    const shell = await liveDetail()
    fakeEvents.reconnect()
    await flushPromises()
    expect(mocks.getDataset).toHaveBeenCalledTimes(1)
    expect(mocks.getDatasetRuns).toHaveBeenCalledTimes(1)
    expect(mocks.getDatasetCoverage).toHaveBeenCalledTimes(1)
    expect(shell.find('[data-testid="detail-loading"]').exists()).toBe(false)
    expect(runIds(shell)).toEqual(['r2', 'r1'])
  })

  it('leaving the screen unsubscribes and cancels a pending refetch', async () => {
    const shell = await liveDetail()
    datasetEvent('ds-1', DatasetChange.UPDATED)
    shell.unmount()
    mounted.splice(0)
    expect(fakeEvents.subscribers('ingestRunChanged')).toBe(0)
    expect(fakeEvents.subscribers('datasetChanged')).toBe(0)
    vi.advanceTimersByTime(COALESCE_MS * 2)
    await flushPromises()
    expect(mocks.getDataset).not.toHaveBeenCalled()
  })

  it('never polls: no setInterval while it is mounted and receiving events', async () => {
    const interval = vi.spyOn(globalThis, 'setInterval')
    await liveDetail()
    runEvent(run('r3', 3))
    vi.advanceTimersByTime(COALESCE_MS)
    fakeEvents.reconnect()
    await flushPromises()
    expect(interval).not.toHaveBeenCalled()
  })
})
