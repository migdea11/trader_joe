import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { DOMWrapper, VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { create } from '@bufbuild/protobuf'
import type { MessageInitShape } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import { DatasetSummarySchema } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'
import { CoverageStripSchema } from '@generated/trader_joe/proto/ui/v1/stats_pb'
import type { CoverageStrip as Strip } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { ApiError } from '@/api'
import { coverageLabel } from '@/catalog/coverage'
import CoverageCell from '@/components/catalog/CoverageCell.vue'
import { COVERAGE_UNIT } from '@/theme/copy'
import CoverageStrip from './CoverageStrip.vue'

// tj-grna9p.77, reshaped by tj-grna9p.108 (owner feedback 2026-10-12): the CoverageStrip is ONE
// continuous track, broken only where the server reports a gap, with the text "x/y <unit>" beside
// it; reused by the Dataset detail. Also the grid's lazily fetched Coverage cell.
//
// Fixtures are create(CoverageStripSchema). For the cell the API is mocked at the typed client
// (@/api) and each request is held open until the spec answers it; the coverage store is the real
// one. jsdom does no layout, so a break's position is read from its inline left/width (percent of
// the track), and its 2px minimum, which is CSS, is read from the component's stylesheet on disk.
// Expected text is built from COVERAGE_UNIT so the owner can change the word without a red spec.
const pending = vi.hoisted(() => [] as { id: string; buckets: number; signal: AbortSignal; settle: (r: unknown, ok: boolean) => void }[])
const getDatasetCoverage = vi.hoisted(() => vi.fn())
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  getDatasetCoverage,
}))

const DAY = 86_400_000
const START = Date.UTC(2026, 0, 5, 5)
// The strip's range in the fixtures below: 80 days, so a day is 1.25% of the track.
const SPAN_DAYS = 80

const xy = (x: number, y: number) => `${x}/${y} ${COVERAGE_UNIT}`

function gapAt(fromDay: number, toDay: number, sessions: number) {
  return { start: timestampFromMs(START + fromDay * DAY), end: timestampFromMs(START + toDay * DAY), sessions }
}

/** Buckets of [expected, withBars] split evenly over [START, START + SPAN_DAYS). */
function strip(buckets: [number, number][], extra: MessageInitShape<typeof CoverageStripSchema> = {}): Strip {
  const width = buckets.length === 0 ? 0 : (SPAN_DAYS * DAY) / buckets.length
  return create(CoverageStripSchema, {
    start: timestampFromMs(START),
    end: timestampFromMs(START + SPAN_DAYS * DAY),
    buckets: buckets.map(([expectedSessions, sessionsWithBars], i) => ({
      expectedSessions,
      sessionsWithBars,
      start: timestampFromMs(START + i * width),
      end: timestampFromMs(START + (i + 1) * width),
    })),
    ...extra,
  })
}

// tj-grna9p.114: the Coverage cell owns the details icon, a real link to the Dataset detail, so it is
// handed detailHref by the grid and navigates with the router. Here a memory router with the one
// route stands in; the link's behaviour under the real router is pinned in DatasetsCatalog.spec.ts.
const cellRouter = createRouter({
  history: createMemoryHistory(),
  routes: [{ path: '/data/datasets/:id', name: 'data-dataset-detail', component: { render: () => null } }],
})
const detailHref = (id: string) => cellRouter.resolve({ name: 'data-dataset-detail', params: { id } }).href
function mountCell(data: DatasetSummary | undefined): VueWrapper {
  return mount(CoverageCell, { props: { params: { data, detailHref } as never }, global: { plugins: [cellRouter] } })
}

const mounted: VueWrapper[] = []
function render(props: Record<string, unknown>): VueWrapper {
  const wrapper = mount(CoverageStrip, { props: props as never })
  mounted.push(wrapper)
  return wrapper
}

const track = (w: VueWrapper) => w.find('[data-testid="coverage-track"]')
const breaks = (w: VueWrapper) => w.findAll('[data-testid="coverage-break"]')
const summary = (w: VueWrapper) => w.find('[data-testid="coverage-summary"]').text()
function box(el: DOMWrapper<Element>): { left: number; width: number } {
  const style = (el.element as HTMLElement).style
  expect(style.left).toMatch(/%$/)
  expect(style.width).toMatch(/%$/)
  return { left: parseFloat(style.left), width: parseFloat(style.width) }
}

beforeEach(() => {
  setActivePinia(createPinia())
  pending.length = 0
  getDatasetCoverage.mockReset()
  getDatasetCoverage.mockImplementation(
    (id: string, buckets: number, signal: AbortSignal) =>
      new Promise((resolve, reject) => {
        pending.push({ id, buckets, signal, settle: (r, ok) => (ok ? resolve(r) : reject(r)) })
      }),
  )
})

afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount())
})

describe('CoverageStrip: one continuous track', () => {
  it('a fully covered dataset is ONE covered track with no breaks and no per-bucket pieces', () => {
    const wrapper = render({ strip: strip([[20, 20], [20, 20], [20, 20], [20, 20]]) })
    expect(wrapper.findAll('[data-testid="coverage-track"]')).toHaveLength(1)
    expect(track(wrapper).attributes('data-state')).toBe('covered')
    // Four buckets, yet the track holds nothing: the buckets are only summed.
    expect(track(wrapper).element.children).toHaveLength(0)
    expect(wrapper.findAll('.coverage__segment')).toHaveLength(0)
    expect(summary(wrapper)).toBe(xy(80, 80))
  })

  it('partly covered buckets draw no break of their own: only the server\'s gaps break the track', () => {
    // Bucket 2 is short of bars, but the gap is what the server reports; adjacent covered buckets
    // around it are not told apart.
    const wrapper = render({ strip: strip([[20, 20], [20, 17], [20, 20], [20, 20]], { gaps: [gapAt(30, 33, 3)], gapCount: 1 }) })
    expect(breaks(wrapper)).toHaveLength(1)
    expect(track(wrapper).element.children).toHaveLength(1)
  })

  it('a strip with buckets short of bars but no gap windows has no break at all', () => {
    const wrapper = render({ strip: strip([[20, 20], [20, 15], [20, 20]]) })
    expect(breaks(wrapper)).toHaveLength(0)
    expect(track(wrapper).element.children).toHaveLength(0)
  })

  it('one break per gap window, each placed by TIME across [start, end) in percent of the track', () => {
    // Days 20-30 of 80 are 25%..37.5%; day 60-61 is 75%..76.25%. Uneven sessions per gap and uneven
    // gaps per bucket, so a position by index or by session count would land elsewhere.
    const wrapper = render({
      strip: strip([[40, 30], [40, 39]], { gaps: [gapAt(20, 30, 9), gapAt(60, 61, 1)], gapCount: 2 }),
    })
    const [first, second] = breaks(wrapper).map(box)
    expect(breaks(wrapper)).toHaveLength(2)
    expect(first!.left).toBeCloseTo(25, 6)
    expect(first!.width).toBeCloseTo(12.5, 6)
    expect(second!.left).toBeCloseTo(75, 6)
    expect(second!.width).toBeCloseTo(1.25, 6)
  })

  it('a gap at the very start or running past the end is clamped to the track', () => {
    const wrapper = render({
      strip: strip([[80, 70]], { gaps: [gapAt(-5, 2, 2), gapAt(76, 90, 4)], gapCount: 2 }),
    })
    const [first, last] = breaks(wrapper).map(box)
    expect(first!.left).toBeCloseTo(0, 6)
    expect(first!.width).toBeCloseTo(2.5, 6)
    expect(last!.left).toBeCloseTo(95, 6)
    expect(last!.width).toBeCloseTo(5, 6)
  })

  it('a break is at least 2px wide (CSS), absolutely placed over a relatively placed track', () => {
    // jsdom does no layout and Vitest does not process component CSS, so read the rule on disk.
    // A one-session gap of a multi-year range is far under a pixel by time alone.
    const source = readFileSync(resolve(import.meta.dirname, 'CoverageStrip.vue'), 'utf8')
    const rule = (selector: string) => {
      const match = new RegExp(`\\n${selector.replace('.', '\\.')}\\s*\\{([^}]*)\\}`).exec(source)
      expect(match, `${selector} rule`).not.toBeNull()
      return match![1]!
    }
    expect(rule('.coverage__break')).toMatch(/(^|;|\s)min-width:\s*2px\s*;/)
    expect(rule('.coverage__break')).toMatch(/position:\s*absolute/)
    expect(rule('.coverage__track')).toMatch(/position:\s*relative/)
  })

  it('a break\'s hover names its dates and session count', () => {
    const wrapper = render({ strip: strip([[80, 70]], { gaps: [gapAt(20, 30, 9), gapAt(60, 61, 1)], gapCount: 2 }) })
    expect(breaks(wrapper).map((b) => b.attributes('title'))).toEqual([
      '2026/01/25 to 2026/02/03 · 9 sessions',
      '2026/03/06 · 1 session',
    ])
  })

  it('an empty dataset (due, nothing stored) is a wholly hollow track reading 0/y, with no breaks', () => {
    const wrapper = render({ strip: strip([[125, 0], [125, 0]], { gaps: [gapAt(0, 80, 250)], gapCount: 1 }) })
    expect(track(wrapper).attributes('data-state')).toBe('empty')
    expect(track(wrapper).classes()).toContain('coverage__track--empty')
    expect(breaks(wrapper)).toHaveLength(0)
    expect(summary(wrapper)).toBe(xy(0, 250))
  })

  it('nothing due yet is a faint track reading 0/0', () => {
    const wrapper = render({ strip: strip([[0, 0], [0, 0]]) })
    expect(track(wrapper).attributes('data-state')).toBe('not-due')
    expect(track(wrapper).classes()).toContain('coverage__track--not-due')
    expect(breaks(wrapper)).toHaveLength(0)
    expect(summary(wrapper)).toBe(xy(0, 0))
  })

  it('no buckets at all is also not due, 0/0', () => {
    const wrapper = render({ strip: create(CoverageStripSchema, {}) })
    expect(track(wrapper).attributes('data-state')).toBe('not-due')
    expect(summary(wrapper)).toBe(xy(0, 0))
  })

  it('the text beside the strip is exactly x/y with the unit: no percent, gap count or "Fully covered"', () => {
    const wrapper = render({ strip: strip([[125, 124], [125, 124]], { gaps: [gapAt(10, 11, 1), gapAt(50, 51, 1)], gapCount: 2 }) })
    expect(summary(wrapper)).toBe(xy(248, 250))
  })

  it('the track is role=img labelled with the same facts in words; the visible text is hidden from AT', () => {
    const s = strip([[125, 124], [125, 124]], { gaps: [gapAt(10, 11, 1), gapAt(50, 51, 1)], gapCount: 2 })
    const wrapper = render({ strip: s })
    expect(track(wrapper).attributes('role')).toBe('img')
    expect(track(wrapper).attributes('aria-label')).toBe(`${xy(248, 250)} covered, 2 gaps`)
    expect(track(wrapper).attributes('aria-label')).toBe(coverageLabel(s))
    expect(wrapper.find('[data-testid="coverage-summary"]').attributes('aria-hidden')).toBe('true')
  })

  it('a fully covered track\'s label says no gaps', () => {
    const wrapper = render({ strip: strip([[5, 5]]) })
    expect(track(wrapper).attributes('aria-label')).toBe(`${xy(5, 5)} covered, no gaps`)
  })
})

// tj-grna9p.111 (owner 2026-10-12): variant 'blocks', used by the Dataset detail where one strip
// sits on the screen, keeps one block per bucket. A block's HEIGHT is its covered share (the fill),
// so a partial block is shorter and an empty one hollow whatever the colour; its hover names its
// dates and "x of y due <unit>", never a percent. The text beside it and the role=img label are the
// same as the continuous strip's. The default stays 'continuous' (the grid), pinned above.
describe('CoverageStrip: variant blocks (the Dataset detail)', () => {
  const blocks = (w: VueWrapper) => w.findAll('[data-testid="coverage-block"]')
  const fill = (b: DOMWrapper<Element>) => b.find('.coverage__fill')
  function fillHeight(b: DOMWrapper<Element>): number {
    const height = (fill(b).element as HTMLElement).style.height
    expect(height).toMatch(/%$/)
    return parseFloat(height)
  }
  const css = () => readFileSync(resolve(import.meta.dirname, 'CoverageStrip.vue'), 'utf8')
  function rule(selector: string): string {
    const match = new RegExp(`\\n${selector.replace(/\./g, '\\.')}\\s*\\{([^}]*)\\}`).exec(css())
    expect(match, `${selector} rule`).not.toBeNull()
    return match![1]!
  }

  // 80 days in four 20-day buckets: covered, partial (17 of 20), empty (0 of 20), not due (0 of 0).
  const MIXED: [number, number][] = [[20, 20], [20, 17], [20, 0], [0, 0]]

  it('one block per bucket, in order, each with its state', () => {
    const wrapper = render({ strip: strip(MIXED), variant: 'blocks' })
    expect(blocks(wrapper).map((b) => b.attributes('data-state'))).toEqual(['covered', 'partial', 'empty', 'not-due'])
    expect(blocks(wrapper).map((b) => b.classes())).toEqual([
      expect.arrayContaining(['coverage__block', 'coverage__block--covered']),
      expect.arrayContaining(['coverage__block', 'coverage__block--partial']),
      expect.arrayContaining(['coverage__block', 'coverage__block--empty']),
      expect.arrayContaining(['coverage__block', 'coverage__block--not-due']),
    ])
    expect(track(wrapper).classes()).toContain('coverage__track--blocks')
  })

  it('a covered block is full height; a partial one is shorter by its covered share', () => {
    const [covered, partial] = blocks(render({ strip: strip(MIXED), variant: 'blocks' }))
    expect(fillHeight(covered!)).toBeCloseTo(100, 6)
    expect(fillHeight(partial!)).toBeCloseTo(85, 6)
  })

  it('a block with more bars than due sessions is still only full height', () => {
    const [over] = blocks(render({ strip: strip([[5, 9]]), variant: 'blocks' }))
    expect(over!.attributes('data-state')).toBe('covered')
    expect(fillHeight(over!)).toBeCloseTo(100, 6)
  })

  it('an empty block is hollow (no fill at all) and a not-due block has no fill and is faint', () => {
    const [, , empty, notDue] = blocks(render({ strip: strip(MIXED), variant: 'blocks' }))
    expect(fill(empty!).exists()).toBe(false)
    expect(fill(notDue!).exists()).toBe(false)
    // Hollow: the block is an outline with no background of its own; faint: not-due is dimmed. CSS
    // read from disk because jsdom does no layout and Vitest does not process component CSS.
    expect(rule('.coverage__block')).toMatch(/border:\s*1px solid/)
    expect(rule('.coverage__block')).not.toMatch(/background/)
    expect(rule('.coverage__block--not-due')).toMatch(/opacity:\s*0\.\d+/)
    expect(rule('.coverage__fill')).toMatch(/background:/)
  })

  it('each block\'s hover names its half-open dates and "x of y due <unit>", or that none is due yet', () => {
    const wrapper = render({ strip: strip(MIXED), variant: 'blocks' })
    expect(blocks(wrapper).map((b) => b.attributes('title'))).toEqual([
      `2026/01/05 to 2026/01/24 · 20 of 20 due ${COVERAGE_UNIT}`,
      `2026/01/25 to 2026/02/13 · 17 of 20 due ${COVERAGE_UNIT}`,
      `2026/02/14 to 2026/03/05 · 0 of 20 due ${COVERAGE_UNIT}`,
      `2026/03/06 to 2026/03/25 · no ${COVERAGE_UNIT} due yet`,
    ])
  })

  it('no percent anywhere: not in the text, not in a hover, not in the label', () => {
    const wrapper = render({ strip: strip([[3, 1], [7, 5], [9, 0]], { gaps: [gapAt(10, 11, 1)], gapCount: 1 }), variant: 'blocks', showGaps: true })
    expect(wrapper.text()).not.toContain('%')
    for (const b of blocks(wrapper)) expect(b.attributes('title')).not.toContain('%')
    expect(track(wrapper).attributes('aria-label')).not.toContain('%')
  })

  it('draws no gap breaks, even where the server reports gap windows', () => {
    const wrapper = render({ strip: strip(MIXED, { gaps: [gapAt(20, 23, 3), gapAt(40, 60, 20)], gapCount: 2 }), variant: 'blocks' })
    expect(breaks(wrapper)).toHaveLength(0)
    expect(blocks(wrapper)).toHaveLength(4)
  })

  it('the text beside it and the role=img label are the same as the continuous strip\'s', () => {
    const s = strip(MIXED, { gaps: [gapAt(20, 23, 3), gapAt(40, 60, 20)], gapCount: 2 })
    const asBlocks = render({ strip: s, variant: 'blocks' })
    const asTrack = render({ strip: s, variant: 'continuous' })
    expect(summary(asBlocks)).toBe(xy(37, 60))
    expect(summary(asBlocks)).toBe(summary(asTrack))
    expect(track(asBlocks).attributes('role')).toBe('img')
    expect(track(asBlocks).attributes('aria-label')).toBe(coverageLabel(s))
    expect(track(asBlocks).attributes('aria-label')).toBe(track(asTrack).attributes('aria-label'))
  })

  it('still lists the gap windows in words with showGaps', () => {
    const wrapper = render({ strip: strip([[10, 6]], { gaps: [gapAt(0, 1, 1)], gapCount: 2 }), variant: 'blocks', showGaps: true })
    expect(wrapper.findAll('[data-testid="coverage-gaps"] li').map((li) => li.text())).toEqual([
      '2026/01/05 · 1 session',
      'and 1 more gap not listed',
    ])
  })

  it('no buckets at all draws no block and keeps the not-due track, 0/0', () => {
    const wrapper = render({ strip: create(CoverageStripSchema, {}), variant: 'blocks' })
    expect(blocks(wrapper)).toHaveLength(0)
    expect(track(wrapper).attributes('data-state')).toBe('not-due')
    expect(track(wrapper).classes()).toContain('coverage__track--not-due')
    expect(summary(wrapper)).toBe(xy(0, 0))
  })

  it('the continuous variant (and the default) draws no blocks', () => {
    for (const props of [{ strip: strip(MIXED) }, { strip: strip(MIXED), variant: 'continuous' }]) {
      expect(blocks(render(props))).toHaveLength(0)
    }
  })
})

describe('CoverageStrip: the gap list', () => {
  it('showGaps lists each gap window in words, then "and N more" when the server listed fewer', () => {
    const gaps = [gapAt(0, 1, 1), gapAt(7, 10, 3)]
    const wrapper = render({ strip: strip([[10, 6]], { gaps, gapCount: 5 }), showGaps: true })
    const items = wrapper.findAll('[data-testid="coverage-gaps"] li').map((li) => li.text())
    expect(items).toEqual(['2026/01/05 · 1 session', '2026/01/12 to 2026/01/14 · 3 sessions', 'and 3 more gaps not listed'])
  })

  it('compact (a grid cell) shows no visible gap list and no "not listed" note, but keeps the windows for a screen reader', () => {
    const gaps = [gapAt(0, 1, 1)]
    const wrapper = render({ strip: strip([[5, 4]], { gaps, gapCount: 3 }), compact: true })
    expect(wrapper.find('[data-testid="coverage-gaps"]').exists()).toBe(false)
    expect(wrapper.find('.coverage__sr').text()).toBe('Gap: 2026/01/05 · 1 session')
    expect(wrapper.text()).not.toContain('not listed')
    expect(wrapper.classes()).toContain('coverage--compact')
  })

  it('a strip with no gaps lists nothing', () => {
    const wrapper = render({ strip: strip([[5, 5]]), showGaps: true })
    expect(wrapper.find('[data-testid="coverage-gaps"]').exists()).toBe(false)
    expect(wrapper.find('.coverage__sr').exists()).toBe(false)
  })
})

describe('CoverageCell', () => {
  function cell(id: string | undefined): VueWrapper {
    const wrapper = mountCell(id === undefined ? undefined : create(DatasetSummarySchema, { id, assetSymbol: 'VFV' }))
    mounted.push(wrapper)
    return wrapper
  }

  it('asks for its row strip at 60 buckets on mount and shows Loading until it arrives', async () => {
    const wrapper = cell('ds-1')
    expect(pending.map((p) => [p.id, p.buckets])).toEqual([['ds-1', 60]])
    expect(wrapper.find('[data-testid="coverage-loading"]').attributes('role')).toBe('status')
    expect(wrapper.find('[data-testid="coverage-loading"]').text()).toBe('Loading coverage')
    expect(wrapper.find('.coverage-cell').attributes('aria-busy')).toBe('true')
    pending[0]!.settle(strip([[5, 5]]), true)
    await flushPromises()
    expect(wrapper.find('[data-testid="coverage-strip"]').exists()).toBe(true)
    // tj-grna9p.112: the grid cell reads the numbers alone; its column header carries the unit.
    expect(wrapper.find('[data-testid="coverage-summary"]').text()).toBe('5/5')
    expect(wrapper.find('.coverage-cell').attributes('aria-busy')).toBeUndefined()
  })

  it('a gappy strip reads x/y only, with a break and no "not listed" note in the cell', async () => {
    const wrapper = cell('ds-1')
    pending[0]!.settle(strip([[10, 9]], { gaps: [gapAt(40, 41, 1)], gapCount: 4 }), true)
    await flushPromises()
    // tj-grna9p.112: numbers only in the cell, no unit word.
    expect(wrapper.find('[data-testid="coverage-summary"]').text()).toBe('9/10')
    expect(wrapper.find('[data-testid="coverage-summary"]').text()).not.toContain(COVERAGE_UNIT)
    expect(wrapper.findAll('[data-testid="coverage-break"]')).toHaveLength(1)
    expect(wrapper.find('.coverage').classes()).toContain('coverage--compact')
    expect(wrapper.text()).not.toContain('not listed')
  })

  // tj-grna9p.111: the grid keeps the one continuous track (many strips stack there); only the
  // Dataset detail asks for blocks.
  it('the grid cell draws the continuous strip, never per-bucket blocks', async () => {
    const wrapper = cell('ds-1')
    pending[0]!.settle(strip([[10, 9], [10, 10], [10, 0]], { gaps: [gapAt(40, 41, 1)], gapCount: 1 }), true)
    await flushPromises()
    expect(wrapper.findComponent(CoverageStrip).props('variant')).toBe('continuous')
    expect(wrapper.findAll('[data-testid="coverage-block"]')).toHaveLength(0)
    expect(wrapper.findAll('[data-testid="coverage-track"]')).toHaveLength(1)
  })

  it('a 422 reads "No coverage", neutral, with the reason in its title', async () => {
    const wrapper = cell('ds-1')
    pending[0]!.settle(new ApiError({ status: 422 }), false)
    await flushPromises()
    const none = wrapper.find('[data-testid="coverage-none"]')
    expect(none.text()).toBe('No coverage')
    expect(none.attributes('title')).toBe('No trading calendar covers this dataset')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('a failure reads "Coverage unavailable"', async () => {
    const wrapper = cell('ds-1')
    pending[0]!.settle(new ApiError({ status: 500 }), false)
    await flushPromises()
    expect(wrapper.find('[data-testid="coverage-unavailable"]').text()).toBe('Coverage unavailable')
  })

  it('scrolling out (unmount) aborts the request still in flight', () => {
    const wrapper = cell('ds-1')
    wrapper.unmount()
    mounted.splice(mounted.indexOf(wrapper), 1)
    expect(pending[0]!.signal.aborted).toBe(true)
  })

  it('a recycled cell handed another row lets go of the old strip and asks for the new one', async () => {
    const wrapper = cell('ds-1')
    await wrapper.setProps({ params: { data: create(DatasetSummarySchema, { id: 'ds-2' }), detailHref } as never })
    expect(pending[0]!.signal.aborted).toBe(true)
    expect(pending.map((p) => p.id)).toEqual(['ds-1', 'ds-2'])
  })

  it('a row still loading (no data) renders nothing and asks for nothing', () => {
    const wrapper = cell(undefined)
    expect(wrapper.html()).not.toContain('coverage-cell')
    expect(getDatasetCoverage).not.toHaveBeenCalled()
  })
})

// tj-grna9p.112 (owner feedback 2026-10-13): in the Datasets grid the bar is the SAME width on every
// row whatever the digits, the text beside it is the numbers alone "x/y" in their own fixed-width,
// right-aligned, tabular slot, and the unit moved to the column header (DatasetsCatalog.spec.ts).
// The strip's role=img label keeps the unit in words, and outside the grid (the Dataset detail) the
// text keeps "x/y <unit>".
//
// jsdom does no layout and Vitest does not process component CSS, so the fixed widths are read from
// the two components' stylesheets on disk, as the 2px break minimum above is: the cell defines the
// one bar width and the text slot width as custom properties, and the compact strip sizes its track
// and its text from them with no grow. What jsdom can see is pinned too: two cells with very
// different digit counts render the bar with identical classes and no inline width of its own.
describe('the grid cell: fixed-width bar, numbers only (tj-grna9p.112)', () => {
  const stripCss = () => readFileSync(resolve(import.meta.dirname, 'CoverageStrip.vue'), 'utf8')
  const cellCss = () => readFileSync(resolve(import.meta.dirname, '../catalog/CoverageCell.vue'), 'utf8')
  function ruleIn(source: string, selector: string): string {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/ /g, '\\s+')
    const match = new RegExp(`\\n${escaped}\\s*\\{([^}]*)\\}`).exec(source)
    expect(match, `${selector} rule`).not.toBeNull()
    return match![1]!
  }

  function cell(id: string): VueWrapper {
    const wrapper = mountCell(create(DatasetSummarySchema, { id, assetSymbol: id }))
    mounted.push(wrapper)
    return wrapper
  }

  async function cells(): Promise<[VueWrapper, VueWrapper]> {
    const small = cell('ds-small')
    const large = cell('ds-large')
    pending.find((p) => p.id === 'ds-small')!.settle(strip([[5, 5]]), true)
    pending.find((p) => p.id === 'ds-large')!.settle(strip([[125, 124], [125, 124]], { gaps: [gapAt(10, 11, 1), gapAt(50, 51, 1)], gapCount: 2 }), true)
    await flushPromises()
    return [small, large]
  }

  it('the visible text is exactly x/y: no unit word, no percent', async () => {
    const [small, large] = await cells()
    expect(summary(small)).toBe('5/5')
    expect(summary(large)).toBe('248/250')
    for (const w of [small, large]) {
      expect(summary(w)).toMatch(/^\d+\/\d+$/)
      expect(summary(w)).not.toContain(COVERAGE_UNIT)
      expect(w.find('[data-testid="coverage-strip"]').text()).not.toContain('%')
    }
  })

  it('a compact strip and the edge cases read x/y alone: 0/0 and 0/250', () => {
    expect(summary(render({ strip: strip([[0, 0], [0, 0]]), compact: true }))).toBe('0/0')
    expect(summary(render({ strip: create(CoverageStripSchema, {}), compact: true }))).toBe('0/0')
    expect(summary(render({ strip: strip([[125, 0], [125, 0]]), compact: true }))).toBe('0/250')
  })

  it('outside the grid (not compact) the same strip keeps the unit: "x/y <unit>"', () => {
    const s = strip([[125, 124], [125, 124]])
    expect(summary(render({ strip: s }))).toBe(xy(248, 250))
    expect(summary(render({ strip: s, variant: 'blocks' }))).toBe(xy(248, 250))
    expect(summary(render({ strip: s, compact: true }))).toBe('248/250')
  })

  it('the bar\'s role=img label still names the unit in words in the grid cell', async () => {
    const [small, large] = await cells()
    expect(track(small).attributes('role')).toBe('img')
    expect(track(small).attributes('aria-label')).toBe(`${xy(5, 5)} covered, no gaps`)
    expect(track(large).attributes('aria-label')).toBe(`${xy(248, 250)} covered, 2 gaps`)
    expect(track(large).attributes('aria-label')).toBe(coverageLabel(strip([[125, 124], [125, 124]], { gapCount: 2 })))
    expect(small.find('[data-testid="coverage-summary"]').attributes('aria-hidden')).toBe('true')
  })

  it('rows reading 5/5 and 248/250 render the bar identically: same classes, no inline width of its own', async () => {
    const [small, large] = await cells()
    expect(track(small).classes()).toEqual(track(large).classes())
    expect(track(small).classes()).toContain('coverage__track')
    expect(track(small).attributes('style')).toBeUndefined()
    expect(track(large).attributes('style')).toBeUndefined()
    expect(small.find('.coverage').classes()).toEqual(large.find('.coverage').classes())
    expect(small.find('.coverage').classes()).toContain('coverage--compact')
    expect(small.find('.coverage-cell').attributes('style')).toBe(large.find('.coverage-cell').attributes('style'))
    // The text slot is the same element with the same classes on both rows: only its digits differ.
    expect(small.find('[data-testid="coverage-summary"]').classes()).toEqual(['coverage__summary'])
    expect(large.find('[data-testid="coverage-summary"]').classes()).toEqual(['coverage__summary'])
  })

  it('the cell defines ONE fixed bar width and one fixed text slot width (CSS custom properties)', () => {
    const rule = ruleIn(cellCss(), '.coverage-cell')
    // A fixed length, never a percent, auto or a calc on the content.
    expect(rule).toMatch(/--coverage-bar-width:\s*\d+(\.\d+)?px\s*;/)
    expect(rule).toMatch(/--coverage-text-width:\s*\d+(\.\d+)?ch\s*;/)
  })

  it('the compact track neither grows nor shrinks: flex 0 0 at the bar width', () => {
    const rule = ruleIn(stripCss(), '.coverage--compact .coverage__track')
    expect(rule).toMatch(/(^|;|\s)flex:\s*0\s+0\s+var\(--coverage-bar-width\b[^;]*\)\s*;/)
    expect(rule).not.toMatch(/flex-grow|flex-shrink|(^|;|\s)width:/)
  })

  it('the numbers sit in their own fixed-width slot, right aligned, with tabular digits', () => {
    const slot = ruleIn(stripCss(), '.coverage--compact .coverage__summary')
    // Never grows past the slot (grow 0); its basis is the slot width, not the content.
    expect(slot).toMatch(/(^|;|\s)flex:\s*0\s+\d\s+var\(--coverage-text-width\b[^;]*\)\s*;/)
    expect(slot).toMatch(/text-align:\s*right\s*;/)
    expect(ruleIn(stripCss(), '.coverage__summary')).toMatch(/font-variant-numeric:\s*tabular-nums\s*;/)
  })
})

// tj-grna9p.114 (owner feedback 2026-10-13): the details icon moved from the Symbol cell into the
// Coverage cell, because it opens the details of this column. It sits AFTER the numbers in a slot of
// its own that never grows or shrinks, so the bar (--coverage-bar-width) and the numbers' slot are
// the same on every row whatever the cell is showing; and it is there in every state of the cell
// (loading, no calendar, unavailable, ready), so a row's details are always one click away. Its
// link behaviour (href, label, click never reaching the row) is pinned under the real router in
// DatasetsCatalog.spec.ts. jsdom does no layout, so the slot is read from the stylesheet on disk.
describe('the Coverage cell\'s details icon (tj-grna9p.114)', () => {
  const cellCss = () => readFileSync(resolve(import.meta.dirname, '../catalog/CoverageCell.vue'), 'utf8')
  function ruleIn(source: string, selector: string): string {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/ /g, '\\s+')
    const match = new RegExp(`\\n${escaped}\\s*\\{([^}]*)\\}`).exec(source)
    expect(match, `${selector} rule`).not.toBeNull()
    return match![1]!
  }

  function cell(id: string, symbol = 'VFV'): VueWrapper {
    const wrapper = mountCell(create(DatasetSummarySchema, { id, assetSymbol: symbol }))
    mounted.push(wrapper)
    return wrapper
  }

  /** The icon is the cell's last child, after the body that holds the bar and the numbers. */
  function expectIconLast(wrapper: VueWrapper, id: string, symbol = 'VFV'): void {
    const children = Array.from(wrapper.find('.coverage-cell').element.children)
    expect(children.map((c) => c.className)).toEqual(['coverage-cell__body', 'details-link'])
    const icon = wrapper.find('[data-testid="dataset-details-link"]')
    expect(icon.element).toBe(children[1])
    expect(icon.element.tagName).toBe('A')
    expect(icon.attributes('href')).toBe(detailHref(id))
    expect(icon.attributes('aria-label')).toBe(`Dataset details for ${symbol}`)
    expect(icon.attributes('title')).toBe('Dataset details')
  }

  it('is present while the strip is loading, after the loading placeholder', () => {
    const wrapper = cell('ds-1')
    expect(wrapper.find('[data-testid="coverage-loading"]').exists()).toBe(true)
    expectIconLast(wrapper, 'ds-1')
  })

  it('is present, after the numbers, once the strip is ready', async () => {
    const wrapper = cell('a/b', 'XIC')
    pending[0]!.settle(strip([[5, 5]]), true)
    await flushPromises()
    expectIconLast(wrapper, 'a/b', 'XIC')
    expect(wrapper.find('[data-testid="dataset-details-link"]').attributes('href')).toBe('/data/datasets/a%2Fb')
    // After the numbers in document order: the summary precedes the icon.
    const summaryEl = wrapper.find('[data-testid="coverage-summary"]').element
    const iconEl = wrapper.find('[data-testid="dataset-details-link"]').element
    expect(summaryEl.compareDocumentPosition(iconEl) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('is present when no trading calendar covers the dataset (422)', async () => {
    const wrapper = cell('ds-1')
    pending[0]!.settle(new ApiError({ status: 422 }), false)
    await flushPromises()
    expect(wrapper.find('[data-testid="coverage-none"]').exists()).toBe(true)
    expectIconLast(wrapper, 'ds-1')
  })

  it('is present when coverage is unavailable', async () => {
    const wrapper = cell('ds-1')
    pending[0]!.settle(new ApiError({ status: 500 }), false)
    await flushPromises()
    expect(wrapper.find('[data-testid="coverage-unavailable"]').exists()).toBe(true)
    expectIconLast(wrapper, 'ds-1')
  })

  it('a recycled cell points its icon at the new row', async () => {
    const wrapper = cell('ds-1', 'VFV')
    await wrapper.setProps({ params: { data: create(DatasetSummarySchema, { id: 'ds-2', assetSymbol: 'XEQT' }), detailHref } as never })
    expectIconLast(wrapper, 'ds-2', 'XEQT')
  })

  it('rows reading 5/5 and 248/250 carry the same icon slot: same element, same classes, no inline size', async () => {
    const small = cell('ds-small')
    const large = cell('ds-large')
    pending.find((p) => p.id === 'ds-small')!.settle(strip([[5, 5]]), true)
    pending.find((p) => p.id === 'ds-large')!.settle(strip([[125, 124], [125, 124]]), true)
    await flushPromises()
    for (const w of [small, large]) {
      const icon = w.find('[data-testid="dataset-details-link"]')
      expect(icon.classes()).toEqual(['details-link'])
      expect(icon.attributes('style')).toBeUndefined()
    }
  })

  it('the icon has its own fixed slot: flex 0 0 at a fixed px width, never growing or shrinking', () => {
    expect(ruleIn(cellCss(), '.coverage-cell')).toMatch(/--coverage-icon-width:\s*\d+(\.\d+)?px\s*;/)
    const slot = ruleIn(cellCss(), '.details-link')
    expect(slot).toMatch(/(^|;|\s)flex:\s*0\s+0\s+var\(--coverage-icon-width\b[^;]*\)\s*;/)
    expect(slot).not.toMatch(/flex-grow|flex-shrink|(^|;|\s)width:/)
  })

  it('the bar and numbers sit in a body that never grows, so the icon follows the numbers directly', () => {
    // Grow 0: a wide column leaves its slack after the icon, never between the numbers and the icon
    // or inside the bar, so the bar width is the same on every row.
    const body = ruleIn(cellCss(), '.coverage-cell__body')
    expect(body).toMatch(/(^|;|\s)flex:\s*0\s+\d\s+auto\s*;/)
    expect(body).not.toMatch(/flex-grow/)
  })
})
