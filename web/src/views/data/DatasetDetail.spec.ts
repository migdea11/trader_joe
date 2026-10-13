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
import { CoverageStripSchema } from '@generated/trader_joe/proto/ui/v1/stats_pb'

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
}))
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...mocks,
}))

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

describe('retention (read-only stub)', () => {
  it('shows the real expiry type and time, and has no control of any kind', async () => {
    mocks.getDataset.mockResolvedValue(dataset({ expiryType: ExpiryType.BULK, expiry: timestampFromMs(Date.UTC(2027, 0, 1, 17)) }))
    const shell = await detailAt()
    const section = shell.find('#detail-retention')
    expect(section.find('[data-fact="expiry-type"]').text()).toBe('Bulk')
    expect(section.find('[data-fact="expiry"]').text()).toContain('2027/01/01')
    expect(section.findAll('button, input, select, textarea, a')).toHaveLength(0)
    expect(section.text()).toContain('Read only.')
  })

  it('no expiry reads None; a retired dataset says it no longer collects', async () => {
    mocks.getDataset.mockResolvedValue(dataset({ expiry: undefined, state: DatasetState.RETIRED }))
    const shell = await detailAt()
    expect(shell.find('[data-fact="expiry"]').text()).toBe('None')
    expect(shell.find('[data-fact="expiry-type"]').text()).toBe('Rolling')
    expect(shell.find('#detail-retention').text()).toContain('Retired: no longer collecting')
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
  it('All Datasets, the four sections, and Usage disabled with its reason; no Rows, nothing to the Viewer', async () => {
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
    expect(usage.attributes('disabled')).toBeDefined()
    expect(usage.attributes('title')).toBe('Not built yet')
  })

  it('lists exactly All Datasets, the four sections and the disabled Usage: one link, five buttons', async () => {
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
    // Only Usage is disabled; the four sections are live in-page jumps.
    expect(side.findAll('button[disabled]').map((b) => b.attributes('data-section'))).toEqual(['usage'])
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
