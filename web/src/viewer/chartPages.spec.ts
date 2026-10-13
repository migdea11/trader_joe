import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { create, fromJson, toJson } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import type { Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'
import { BarPageIndexSchema } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { BarPageIndex } from '@generated/trader_joe/proto/ui/v1/data_pb'

import {
  CACHE_PAGES,
  PAGE_BARS,
  PAGE_OVERLAP_BARS,
  SETTLE_MS,
  WARMUP_BARS,
  apiPageFetchers,
  createChartPages,
  overlapBeforeRange,
  overlapRange,
  pageForWindow,
  pageLabel,
  pageLength,
  placeWindow,
  toChartPageIndex,
  toGlobal,
  toLocal,
  warmupTail,
} from './chartPages'
import type { ChartPageIndex, PageFetchers, PageWindow } from './chartPages'

// Only apiPageFetchers reaches the typed client; every model test injects its own fetchers.
const api = vi.hoisted(() => ({ getDatasetBarPages: vi.fn(), getDatasetBars: vi.fn() }))
vi.mock('@/api', async (importOriginal) => ({ ...(await importOriginal<typeof import('@/api')>()), ...api }))

// tj-grna9p.121 (and the warm-up of .124): the chart page model, decision tj-eozqhi with the owner's
// FIXED-BLOCK rulings of 2026-10-13 (the note at the end of tj-eozqhi wins) and the cache and preload
// rule of tj-grna9p.118.
//
// THE RULE UNDER TEST. A window of N bars is cut into 1,000-bar blocks counted back from the newest
// bar: with r = N mod 1000 the page offsets, oldest first, are [0 if r > 0], r, r + 1000, ..., N - 1000,
// and N <= 1000 is the single page [0]. A page SHOWS its block plus a 50-bar look-ahead of the next
// block (read limit 1,050; the newest page has none). The earliest page holds 1,000 bars too, so it
// overlaps the next block. The model opens on the NEWEST page, requests the OLDER neighbour with it
// and hands the page that neighbour's last 50 bars as warm-up for the averages. 20 pages are cached
// by (dataset, window start, window end, offset); the current page and both neighbours are never
// evicted. Both neighbours are preloaded only after a 150 ms settle, cancelled by a new seek.
//
// The offsets come from the server (tj-grna9p.120); here a fake server answers the index with
// literal offsets and every bar read with the bars [offset, offset + limit) of the window, so a page's
// contents identify exactly which read the model made. Bars are bare objects whose `close` is their
// global index: the model only slices and hands them on.

const MIN = 60_000
const T0 = Date.UTC(2026, 0, 5, 14, 30)
const W_START = new Date(T0)
const W_END = new Date(T0 + 400_000 * MIN)
const WIN: PageWindow = { datasetId: 'ds-1', start: W_START, end: W_END }

function barAt(i: number): Bar {
  return { barStart: { seconds: BigInt((T0 + i * MIN) / 1000), nanos: 0 }, close: i } as unknown as Bar
}

const ids = (bars: readonly Bar[]): number[] => bars.map((bar) => bar.close)
const span = (from: number, to: number): number[] => Array.from({ length: to - from }, (_, i) => from + i)

function indexMessage(total: number, offsets: readonly number[], omitZero = false): BarPageIndex {
  const message = create(BarPageIndexSchema, {
    barCount: BigInt(total),
    pageSize: PAGE_BARS,
    overlap: PAGE_OVERLAP_BARS,
    pages: offsets.map((offset) => ({ start: timestampFromMs(T0 + offset * MIN), offset: BigInt(offset) })),
  })
  // Through canonical JSON, as the wire carries it: a zero offset is not on the wire at all.
  return omitZero ? fromJson(BarPageIndexSchema, toJson(BarPageIndexSchema, message)) : message
}

function modelIndex(total: number, offsets: readonly number[]): ChartPageIndex {
  return toChartPageIndex(indexMessage(total, offsets))
}

/** N multiple of 1,000: the blocks are exactly k * 1,000. */
const evenBlocks = (pages: number): number[] => Array.from({ length: pages }, (_, k) => k * PAGE_BARS)

interface BarsCall {
  win: PageWindow
  offset: number
  limit: number
  signal: AbortSignal
  resolve(): void
  reject(error: unknown): void
}

function fakeServer(total: number, offsets: readonly number[]) {
  const calls: BarsCall[] = []
  const failOnce = new Set<number>()
  const state = { manual: false }
  const fetchers = {
    fetchIndex: vi.fn<PageFetchers['fetchIndex']>(async () => indexMessage(total, offsets, true)),
    fetchBars: vi.fn<PageFetchers['fetchBars']>(
      (win, from, limit, signal) =>
        new Promise<readonly Bar[]>((resolve, reject) => {
          const offset = Math.round((from.getTime() - T0) / MIN)
          const call: BarsCall = {
            win,
            offset,
            limit,
            signal,
            resolve: () => resolve(span(offset, Math.min(offset + limit, total)).map(barAt)),
            reject,
          }
          calls.push(call)
          signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
          if (failOnce.delete(offset)) queueMicrotask(() => reject(new Error(`bars at ${offset} failed`)))
          else if (!state.manual) queueMicrotask(call.resolve)
        }),
    ),
  }
  return {
    calls,
    fetchers,
    failOnce,
    state,
    /** [offset, limit] of every bar read, in order. */
    reads: () => calls.map((c) => [c.offset, c.limit]),
    offsets: () => calls.map((c) => c.offset),
    pending: (offset: number) => calls.find((c) => c.offset === offset && !c.signal.aborted)!,
  }
}

/** A scheduler the test fires by hand: `settle` timers never run on their own. */
function manualTimers() {
  const timers: Array<{ run: () => void; ms: number; cancelled: boolean }> = []
  return {
    schedule(run: () => void, ms: number): () => void {
      const timer = { run, ms, cancelled: false }
      timers.push(timer)
      return () => {
        timer.cancelled = true
      }
    },
    live: () => timers.filter((t) => !t.cancelled),
    fire(): void {
      const due = timers.filter((t) => !t.cancelled)
      timers.length = 0
      for (const t of due) t.run()
    },
  }
}

async function tick(): Promise<void> {
  for (let i = 0; i < 20; i += 1) await Promise.resolve()
}

// ---- the pure functions -----------------------------------------------------------------------------

describe('toChartPageIndex', () => {
  it('reads the wire index as numbers and Dates; a zero offset absent from the JSON reads as 0', () => {
    const wire = toJson(BarPageIndexSchema, indexMessage(2500, [0, 500, 1500]))
    // proto3 canonical JSON omits a zero int64: the earliest page carries no offset key at all.
    expect((wire as { pages: Array<Record<string, unknown>> }).pages[0]).not.toHaveProperty('offset')
    const index = toChartPageIndex(fromJson(BarPageIndexSchema, wire))
    expect(index).toEqual({
      barCount: 2500,
      pageSize: 1000,
      overlap: 50,
      pages: [
        { start: new Date(T0), offset: 0 },
        { start: new Date(T0 + 500 * MIN), offset: 500 },
        { start: new Date(T0 + 1500 * MIN), offset: 1500 },
      ],
    })
  })

  it('an empty window has no pages; an unset page size falls back to 1,000', () => {
    const index = toChartPageIndex(create(BarPageIndexSchema, {}))
    expect(index).toEqual({ barCount: 0, pageSize: PAGE_BARS, overlap: 0, pages: [] })
  })

  it('a page with no start is refused, never placed at the epoch', () => {
    const message = create(BarPageIndexSchema, { barCount: 10n, pages: [{ offset: 0n }] })
    expect(() => toChartPageIndex(message)).toThrow('a page has no start')
  })

  it('the constants are the owner rule: 1,000-bar pages, 50 look-ahead, 50 warm-up, 20 cached, 150 ms', () => {
    expect([PAGE_BARS, PAGE_OVERLAP_BARS, WARMUP_BARS, CACHE_PAGES, SETTLE_MS]).toEqual([1000, 50, 50, 20, 150])
  })
})

describe('pageLength', () => {
  it('a page shows its block plus the 50-bar look-ahead; the newest page has none', () => {
    const index = modelIndex(2500, [0, 500, 1500])
    expect([0, 1, 2].map((k) => pageLength(index, k))).toEqual([1050, 1050, 1000])
  })

  it('the earliest page holds a full block even when the remainder is short', () => {
    // N = 2,020: r = 20, the earliest page [0, 1000) overlaps the next block [20, 1020).
    const index = modelIndex(2020, [0, 20, 1020])
    expect([0, 1, 2].map((k) => pageLength(index, k))).toEqual([1050, 1050, 1000])
  })

  it('a look-ahead never reaches past the window: N = 1,030 has pages [0, 30]', () => {
    const index = modelIndex(1030, [0, 30])
    expect([pageLength(index, 0), pageLength(index, 1)]).toEqual([1030, 1000])
  })

  it('one page of at most 1,000 bars is the whole window; an unknown page is 0', () => {
    expect(pageLength(modelIndex(800, [0]), 0)).toBe(800)
    expect(pageLength(modelIndex(1000, [0]), 0)).toBe(1000)
    expect(pageLength(modelIndex(800, [0]), 1)).toBe(0)
  })
})

describe('pageForWindow', () => {
  const index = modelIndex(2500, [0, 500, 1500])

  it('the current page keeps a window its bars hold, its look-ahead included', () => {
    expect(pageForWindow(index, 1, 600, 1540, 'right')).toBe(1)
    expect(pageForWindow(index, 0, 10, 1040, 'jump')).toBe(0)
  })

  it('otherwise the page whose block holds the leading edge: from moving left, to moving right', () => {
    expect(pageForWindow(index, 2, 400, 1600, 'left')).toBe(0)
    expect(pageForWindow(index, 0, 400, 1600, 'right')).toBe(2)
    expect(pageForWindow(index, null, 1499, 1600, 'jump')).toBe(1)
    expect(pageForWindow(index, null, 1500, 1600, 'jump')).toBe(2)
  })

  it('inside the earliest page\'s overlap the NEWER block wins when the window must move', () => {
    // Bars 500..1049 are on page 0 (it overlaps) and in page 1's block: a jump there is page 1.
    expect(pageForWindow(index, null, 520, 560, 'jump')).toBe(1)
    expect(pageForWindow(index, 2, 520, 560, 'left')).toBe(1)
    expect(pageForWindow(index, null, 499, 560, 'jump')).toBe(0)
  })

  it('clamps into 0..M-1 and a single page is always 0', () => {
    expect(pageForWindow(index, 1, -300, -10, 'left')).toBe(0)
    expect(pageForWindow(index, 1, 9000, 9999, 'right')).toBe(2)
    expect(pageForWindow(modelIndex(800, [0]), 0, 5000, 6000, 'right')).toBe(0)
  })
})

describe('placeWindow', () => {
  const index = modelIndex(2500, [0, 500, 1500])

  it('keeps the width and the leading edge, then shifts the window inside the page', () => {
    // Moving right onto page 2 [1500, 2499]: to = 1600 kept, the 200-bar window starts at the page.
    expect(placeWindow(index, 2, 1400, 1600, 'right')).toEqual({ from: 1500, to: 1700 })
    // Moving left onto page 0 [0, 1049]: from = 1040 would overflow the page, so it shifts back.
    expect(placeWindow(index, 0, 1040, 1100, 'left')).toEqual({ from: 989, to: 1049 })
    expect(placeWindow(index, 1, 700, 900, 'jump')).toEqual({ from: 700, to: 900 })
  })

  it('a window wider than the page is cut to the page', () => {
    expect(placeWindow(index, 2, 0, 3000, 'left')).toEqual({ from: 1500, to: 2499 })
  })
})

// tj-grna9p.129 (owner 2026-10-13, canvas 'A middle page'): the bars a page shows that the NEXT page
// shows too, page-relative and inclusive: [next.offset - offset, length - 1]. For a normal page that is
// its 50-bar look-ahead [1000, 1049]; for the earliest page of a window with r = N mod 1000 > 0 it starts
// at r and runs contiguously to the page's right edge (the builder's reading, pinned here: every bar
// from r on is also on page 1). None on the newest page, none on a single page.
describe('overlapRange', () => {
  it('a normal page: its look-ahead, [pageSize, pageSize + overlap)', () => {
    const index = modelIndex(2500, [0, 500, 1500])
    expect(overlapRange(index, 1)).toEqual({ from: 1000, to: 1049 })
    expect(overlapRange(modelIndex(10_000, evenBlocks(10)), 4)).toEqual({ from: 1000, to: 1049 })
  })

  it('the earliest page with r > 0: from bar r to its last shown bar', () => {
    expect(overlapRange(modelIndex(2500, [0, 500, 1500]), 0)).toEqual({ from: 500, to: 1049 })
    expect(overlapRange(modelIndex(2020, [0, 20, 1020]), 0)).toEqual({ from: 20, to: 1049 })
    // A window shorter than a page plus its look-ahead: the earliest page ends at the window's last bar.
    expect(overlapRange(modelIndex(1030, [0, 30]), 0)).toEqual({ from: 30, to: 1029 })
  })

  it('the earliest page with r = 0 is a normal page: its look-ahead only', () => {
    expect(overlapRange(modelIndex(3000, [0, 1000, 2000]), 0)).toEqual({ from: 1000, to: 1049 })
  })

  it('none on the newest page, on a single page, or for a page that does not exist', () => {
    expect(overlapRange(modelIndex(2500, [0, 500, 1500]), 2)).toBeNull()
    expect(overlapRange(modelIndex(800, [0]), 0)).toBeNull()
    expect(overlapRange(modelIndex(1000, [0]), 0)).toBeNull()
    expect(overlapRange(modelIndex(2500, [0, 500, 1500]), 3)).toBeNull()
    expect(overlapRange(modelIndex(2500, [0, 500, 1500]), -1)).toBeNull()
  })
})

// f8b620b (owner: "only the right (latest) side of the graph has the overlap highlight"): the bars page
// k shows that the PREVIOUS page shows too, page-relative and inclusive: always from 0, to where the
// older page's read ends, clamped to page k. The mirror of overlapRange(k - 1).
describe('overlapBeforeRange', () => {
  it('none on the first page, on a single page, or for a page that does not exist', () => {
    expect(overlapBeforeRange(modelIndex(2500, [0, 500, 1500]), 0)).toBeNull()
    expect(overlapBeforeRange(modelIndex(800, [0]), 0)).toBeNull()
    expect(overlapBeforeRange(modelIndex(1000, [0]), 0)).toBeNull()
    expect(overlapBeforeRange(modelIndex(2500, [0, 500, 1500]), 3)).toBeNull()
    expect(overlapBeforeRange(modelIndex(2500, [0, 500, 1500]), -1)).toBeNull()
  })

  it('a page after a normal page: the older page\'s look-ahead, its first 50 bars', () => {
    expect(overlapBeforeRange(modelIndex(3000, [0, 1000, 2000]), 1)).toEqual({ from: 0, to: 49 })
    expect(overlapBeforeRange(modelIndex(3000, [0, 1000, 2000]), 2)).toEqual({ from: 0, to: 49 })
    expect(overlapBeforeRange(modelIndex(2500, [0, 500, 1500]), 2)).toEqual({ from: 0, to: 49 })
    expect(overlapBeforeRange(modelIndex(10_000, evenBlocks(10)), 5)).toEqual({ from: 0, to: 49 })
  })

  it('the page after the earliest page with r > 0: pageSize - r + overlap bars', () => {
    // N = 1,500, r = 500: page 0 shows bars 0..1,049, page 1 starts at 500, so 550 shared bars.
    expect(overlapBeforeRange(modelIndex(1500, [0, 500]), 1)).toEqual({ from: 0, to: 549 })
    expect(overlapBeforeRange(modelIndex(2500, [0, 500, 1500]), 1)).toEqual({ from: 0, to: 549 })
    expect(overlapBeforeRange(modelIndex(2020, [0, 20, 1020]), 1)).toEqual({ from: 0, to: 1029 })
  })

  it('a short newest page ends where the window ends: never past the page\'s last bar', () => {
    // N = 1,030, r = 30: page 0 shows bars 0..1,029, which is all of page 1 (30..1,029).
    const index = modelIndex(1030, [0, 30])
    expect(overlapBeforeRange(index, 1)).toEqual({ from: 0, to: 999 })
    expect(overlapBeforeRange(index, 1)!.to).toBe(pageLength(index, 1) - 1)
  })

  it('the mirror of overlapRange on the page before: the same global bars from both sides', () => {
    const cases: Array<[number, number[]]> = [
      [2500, [0, 500, 1500]],
      [3000, [0, 1000, 2000]],
      [2020, [0, 20, 1020]],
      [1030, [0, 30]],
      [1500, [0, 500]],
      [10_000, evenBlocks(10)],
    ]
    for (const [total, offsets] of cases) {
      const index = modelIndex(total, offsets)
      for (let k = 1; k < offsets.length; k += 1) {
        const before = overlapBeforeRange(index, k)!
        const after = overlapRange(index, k - 1)!
        const page = offsets[k]!
        const older = offsets[k - 1]!
        expect([page + before.from, page + before.to], `N=${total} k=${k}`).toEqual([older + after.from, older + after.to])
      }
    }
  })
})

// tj-grna9p.129: direction `page` is page navigation. The page is the one whose block holds the bar,
// ALWAYS (the 'already holds' shortcut is skipped: a bar in a page's look-ahead belongs to the next
// page), and the window is the whole page: from = offset, to = offset + its shown length - 1.
describe('direction page', () => {
  const index = modelIndex(2500, [0, 500, 1500])

  it('pageForWindow: the page whose block holds the bar, even when the current page shows it too', () => {
    // From the earliest page (bars 0..1049) bar 1,000 is on screen, yet it belongs to page 1's block.
    expect(pageForWindow(index, 0, 1000, 1000, 'jump')).toBe(0)
    expect(pageForWindow(index, 0, 1000, 1000, 'page')).toBe(1)
    // From page 1 a bar of its look-ahead (1,520) is page 2.
    expect(pageForWindow(index, 1, 1520, 1520, 'right')).toBe(1)
    expect(pageForWindow(index, 1, 1520, 1520, 'page')).toBe(2)
  })

  it('pageForWindow: a bar of the current block stays on it; the ends and beyond clamp', () => {
    expect(pageForWindow(index, 1, 700, 700, 'page')).toBe(1)
    expect(pageForWindow(index, 2, 1499, 1499, 'page')).toBe(1)
    expect(pageForWindow(index, 2, 0, 0, 'page')).toBe(0)
    expect(pageForWindow(index, 0, 2499, 2499, 'page')).toBe(2)
    expect(pageForWindow(index, 0, 9999, 9999, 'page')).toBe(2)
    expect(pageForWindow(index, 2, -5, -5, 'page')).toBe(0)
  })

  it('placeWindow: the whole page, its look-ahead included, whatever the request', () => {
    expect(placeWindow(index, 0, 1000, 1000, 'page')).toEqual({ from: 0, to: 1049 })
    expect(placeWindow(index, 1, 1499, 1499, 'page')).toEqual({ from: 500, to: 1549 })
    expect(placeWindow(index, 2, 1600, 1800, 'page')).toEqual({ from: 1500, to: 2499 })
    expect(placeWindow(modelIndex(800, [0]), 0, 10, 20, 'page')).toEqual({ from: 0, to: 799 })
  })
})

describe('toLocal and toGlobal', () => {
  it('are the page offset apart and round-trip', () => {
    const page = { start: new Date(T0 + 1500 * MIN), offset: 1500 }
    expect(toLocal(page, 1600, 1700)).toEqual({ from: 100, to: 200 })
    expect(toGlobal(page, 100, 200)).toEqual({ from: 1600, to: 1700 })
    const local = toLocal(page, 1512.25, 1987.5)
    expect(toGlobal(page, local.from, local.to)).toEqual({ from: 1512.25, to: 1987.5 })
  })
})

describe('pageLabel', () => {
  it('reads Page N of M with thousands separators', () => {
    const index = modelIndex(1_890_000, evenBlocks(1890))
    expect(pageLabel(index, 1233)).toBe('Page 1,234 of 1,890')
    expect(pageLabel(modelIndex(2500, [0, 500, 1500]), 2)).toBe('Page 3 of 3')
  })

  it('no label for one page, an empty window or before the index', () => {
    expect(pageLabel(modelIndex(800, [0]), 0)).toBeNull()
    expect(pageLabel(modelIndex(0, []), null)).toBeNull()
    expect(pageLabel(null, 0)).toBeNull()
  })
})

describe('warmupTail', () => {
  it('is the 50 bars of the older page just before the page', () => {
    // Page 0 read [0, 1050); page 1 starts at 500: the warm-up is 450..499, not the look-ahead.
    const older = span(0, 1050).map(barAt)
    expect(ids(warmupTail(older, 0, 500))).toEqual(span(450, 500))
    const middle = span(500, 1550).map(barAt)
    expect(ids(warmupTail(middle, 500, 1500))).toEqual(span(1450, 1500))
  })

  it('fewer when the older page starts closer than 50 bars; none when it does not reach', () => {
    expect(ids(warmupTail(span(0, 1050).map(barAt), 0, 20))).toEqual(span(0, 20))
    expect(warmupTail(span(0, 10).map(barAt), 0, 500)).toEqual([])
    expect(warmupTail(span(0, 1050).map(barAt), 0, 0)).toEqual([])
  })
})

// ---- the model --------------------------------------------------------------------------------------

describe('createChartPages: opening a window', () => {
  it('fetches the index with size 1,000 and look-ahead 50, then opens on the NEWEST page', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    const timers = manualTimers()
    const model = createChartPages({ fetchers: server.fetchers, schedule: timers.schedule })
    expect(await model.open(WIN)).toBe('ready')
    expect(server.fetchers.fetchIndex).toHaveBeenCalledTimes(1)
    expect(server.fetchers.fetchIndex).toHaveBeenCalledWith(WIN, 1000, 50, expect.any(AbortSignal))
    // The newest page (offset 1,500) and its older neighbour (offset 500), each with limit 1,050.
    expect(server.reads()).toEqual([
      [1500, 1050],
      [500, 1050],
    ])
    expect(server.calls.every((c) => c.win.end === W_END && c.win.datasetId === 'ds-1')).toBe(true)
    const state = model.state()
    expect(state.current).toMatchObject({ k: 2, offset: 1500, length: 1000, status: 'ready' })
    expect(ids(state.current!.bars)).toEqual(span(1500, 2500))
    expect(state).toMatchObject({ totalBars: 2500, pageOffset: 1500, pageSize: 1000, label: 'Page 3 of 3' })
  })

  it('requests the older neighbour WITH the page, before anything lands and with no timer', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    server.state.manual = true
    const timers = manualTimers()
    const model = createChartPages({ fetchers: server.fetchers, schedule: timers.schedule })
    void model.open(WIN)
    await tick()
    expect(server.offsets()).toEqual([1500, 500])
    expect(timers.live()).toHaveLength(0)
    expect(model.state().current).toMatchObject({ k: 2, status: 'loading', bars: [] })
  })

  it('the page carries the older neighbour\'s last 50 bars as warm-up, once that lands', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    server.state.manual = true
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    void model.open(WIN)
    await tick()
    server.pending(1500).resolve()
    await tick()
    // The page is shown without waiting for its companion: no warm-up yet.
    expect(model.state().current!.status).toBe('ready')
    expect(model.state().current!.warmup).toEqual([])
    server.pending(500).resolve()
    await tick()
    const warmup = model.state().current!.warmup
    expect(ids(warmup)).toEqual(span(1450, 1500))
    // Same identity on every read until the neighbour changes: the chart does not recompute.
    expect(model.state().current!.warmup).toBe(warmup)
  })

  it('a middle page shows 1,050 bars and warms up from page k-1; the earliest page has no warm-up', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    await model.open(WIN)
    await tick()
    await model.loadPage(1)
    await tick()
    let current = model.state().current!
    expect(current).toMatchObject({ k: 1, offset: 500, length: 1050 })
    expect(ids(current.bars)).toEqual(span(500, 1550))
    expect(ids(current.warmup)).toEqual(span(450, 500))
    expect(model.state().label).toBe('Page 2 of 3')

    const before = server.calls.length
    await model.loadPage(0)
    await tick()
    current = model.state().current!
    expect(server.calls.length).toBe(before)
    expect(ids(current.bars)).toEqual(span(0, 1050))
    expect(current.warmup).toEqual([])
  })

  it('a window of at most 1,000 bars is one page: one read, no neighbour, no label, no preload', async () => {
    const server = fakeServer(800, [0])
    const timers = manualTimers()
    const model = createChartPages({ fetchers: server.fetchers, schedule: timers.schedule })
    await model.open(WIN)
    await tick()
    timers.fire()
    await tick()
    expect(server.reads()).toEqual([[0, 1050]])
    expect(model.state()).toMatchObject({ label: '', totalBars: 800 })
    expect(model.state().current!.bars).toHaveLength(800)
  })

  it('an empty window is ready with no page and no bar read', async () => {
    const server = fakeServer(0, [])
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    expect(await model.open(WIN)).toBe('ready')
    expect(server.calls).toHaveLength(0)
    expect(model.state()).toMatchObject({ indexStatus: 'ready', current: null, label: '', totalBars: 0 })
    expect(model.seek(0, 10, 'jump')).toBeNull()
  })

  it('an index failure is the index error, and reload asks again', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    const failure = new Error('index down')
    server.fetchers.fetchIndex.mockRejectedValueOnce(failure)
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    expect(await model.open(WIN)).toBe('error')
    expect(model.state()).toMatchObject({ indexStatus: 'error', indexError: failure, current: null })
    expect(await model.reload()).toBe('ready')
    expect(server.fetchers.fetchIndex).toHaveBeenCalledTimes(2)
    expect(model.state().current!.k).toBe(2)
  })
})

describe('createChartPages: the cache', () => {
  it('is keyed by dataset, window start, window end and page offset', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    await model.open(WIN)
    await tick()
    expect(model.keyOf(2)).toBe(`ds-1|${W_START.getTime()}|${W_END.getTime()}|1500`)
    expect(model.cachedKeys().sort()).toEqual([model.keyOf(1), model.keyOf(2)].sort())
  })

  it('a cached page is shown with NO request', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    await model.open(WIN)
    await tick()
    await model.loadPage(0)
    await tick()
    const before = server.calls.length
    expect(await model.loadPage(2)).toBe('ready')
    expect(await model.loadPage(1)).toBe('ready')
    await tick()
    expect(server.calls.length).toBe(before)
    expect(ids(model.state().current!.bars)).toEqual(span(500, 1550))
  })

  it('keeps at most 20 pages, dropping the least recently used', async () => {
    const server = fakeServer(40_000, evenBlocks(40))
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    await model.open(WIN) // page 39 and 38
    await tick()
    for (const k of [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20]) {
      await model.loadPage(k) // k and k-1
      await tick()
    }
    const keys = model.cachedKeys()
    expect(keys).toHaveLength(CACHE_PAGES)
    // 39, 38 and 0 were the oldest three; 39 and 38 went first, then 0.
    expect(keys).not.toContain(model.keyOf(39))
    expect(keys).not.toContain(model.keyOf(38))
    expect(keys).not.toContain(model.keyOf(0))
    expect(keys).toContain(model.keyOf(1))
    // Returning to an evicted page reads it again.
    const before = server.calls.length
    await model.loadPage(39)
    expect(server.offsets().slice(before)).toEqual([39_000, 38_000])
  })

  it('never evicts the current page or either neighbour, even when a neighbour is the least recently used', async () => {
    const server = fakeServer(40_000, evenBlocks(40))
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    await model.open(WIN)
    await tick()
    // 32 then 31 first, then fill to 20 entries elsewhere: 32 and 31 are the two oldest.
    for (const k of [32, 0, 2, 4, 6, 8, 10, 12, 14, 16]) {
      await model.loadPage(k)
      await tick()
    }
    // open() put 39 and 38 in first; push them out so 32 and 31 lead the LRU.
    await model.loadPage(17)
    await tick()
    let keys = model.cachedKeys()
    expect(keys).toHaveLength(CACHE_PAGES)
    expect(keys.slice(0, 2)).toEqual([model.keyOf(32), model.keyOf(31)])
    // Jump to 30: 30 and 29 come in, so two must go. 32 is not protected and goes; 31 is page 30's
    // newer neighbour, protected although it is now the least recently used; the next one goes.
    await model.loadPage(30)
    await tick()
    keys = model.cachedKeys()
    expect(keys).toHaveLength(CACHE_PAGES)
    expect(keys).not.toContain(model.keyOf(32))
    expect(keys).toContain(model.keyOf(31))
    expect(keys).toContain(model.keyOf(30))
    expect(keys).toContain(model.keyOf(29))
  })
})

describe('createChartPages: seeks and aborts', () => {
  it('a new page aborts the loads in flight and drops them from the cache', async () => {
    const server = fakeServer(10_000, evenBlocks(10))
    server.state.manual = true
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    void model.open(WIN)
    await tick()
    server.pending(9000).resolve()
    server.pending(8000).resolve()
    await tick()
    const first = model.loadPage(4)
    await tick()
    const stale = server.calls.filter((c) => c.offset === 4000 || c.offset === 3000)
    expect(stale).toHaveLength(2)
    const second = model.loadPage(6)
    expect(stale.every((c) => c.signal.aborted)).toBe(true)
    expect(model.pageStatus(4)).toBeUndefined()
    expect(model.pageStatus(3)).toBeUndefined()
    expect(await first).toBe('aborted')
    server.pending(6000).resolve()
    expect(await second).toBe('ready')
    expect(model.state().current!.k).toBe(6)
    expect(model.cachedKeys()).not.toContain(model.keyOf(4))
  })

  it('seek inside the page changes nothing and makes no request; past its edge it moves page', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    const timers = manualTimers()
    const model = createChartPages({ fetchers: server.fetchers, schedule: timers.schedule })
    await model.open(WIN)
    await tick()
    const before = server.calls.length
    const inside = model.seek(1700, 1900, 'right')!
    expect(inside).toMatchObject({ k: 2, changed: false, from: 1700, to: 1900 })
    expect(await inside.loaded).toBe('ready')
    expect(server.calls.length).toBe(before)
    // Past the left edge: page 1 is cached (the warm-up companion), so it shows without a request.
    const left = model.seek(1400, 1600, 'left')!
    // The leading edge 1,400 is kept as far as the page allows: page 1 ends at 1,549.
    expect(left).toMatchObject({ k: 1, changed: true, from: 1349, to: 1549 })
    expect(await left.loaded).toBe('ready')
    expect(server.offsets().slice(before)).toEqual([0])
    expect(model.seek(0, 10, 'jump')).toMatchObject({ k: 0, changed: true })
  })

  it('a failed user load is the page error, and loading it again retries', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    await model.open(WIN)
    await tick()
    server.failOnce.add(0)
    expect(await model.loadPage(0)).toBe('error')
    expect(model.state().current).toMatchObject({ k: 0, status: 'error' })
    expect(String(model.state().pageError)).toContain('bars at 0 failed')
    expect(await model.loadPage(0)).toBe('ready')
    expect(server.offsets().filter((o) => o === 0)).toHaveLength(2)
    expect(model.state().pageError).toBeNull()
  })

  it('a new window clears the cache, aborts what is in flight and fetches the index again', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    server.state.manual = true
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    void model.open(WIN)
    await tick()
    const inFlight = [...server.calls]
    const next: PageWindow = { datasetId: 'ds-1', start: new Date(T0 + 100 * MIN), end: W_END }
    void model.open(next)
    expect(inFlight.every((c) => c.signal.aborted)).toBe(true)
    expect(model.cachedKeys()).toEqual([])
    await tick()
    expect(server.fetchers.fetchIndex).toHaveBeenCalledTimes(2)
    expect(server.fetchers.fetchIndex.mock.calls[1]![0]).toEqual(next)
    expect(model.cachedKeys().every((key) => key.startsWith(`ds-1|${T0 + 100 * MIN}|`))).toBe(true)
  })

  it('an index that answers after the window changed is never used', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    let late: (value: BarPageIndex) => void = () => undefined
    server.fetchers.fetchIndex.mockImplementationOnce(() => new Promise((resolve) => (late = resolve)))
    server.fetchers.fetchIndex.mockImplementationOnce(async () => indexMessage(800, [0]))
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    const first = model.open(WIN)
    expect(await model.open({ ...WIN, start: new Date(T0 + 7 * MIN) })).toBe('ready')
    late(indexMessage(2500, [0, 500, 1500]))
    expect(await first).toBe('aborted')
    expect(model.state().totalBars).toBe(800)
  })

  it('reload asks for the same window again (a reconnect), from an empty cache', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    await model.open(WIN)
    await tick()
    expect(await model.reload()).toBe('ready')
    expect(server.fetchers.fetchIndex).toHaveBeenCalledTimes(2)
    expect(server.fetchers.fetchIndex.mock.calls[1]![0]).toEqual(WIN)
    expect(server.offsets()).toEqual([1500, 500, 1500, 500])
  })
})

describe('createChartPages: seek with direction page (tj-grna9p.129)', () => {
  it('Previous from the newest page: page 1 from the cache, shown whole, its own companion read', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    await model.open(WIN)
    await tick()
    const result = model.seek(1499, 1499, 'page')!
    expect(result).toMatchObject({ k: 1, changed: true, from: 500, to: 1549 })
    expect(await result.loaded).toBe('ready')
    await tick()
    // Page 1 rode along as the newest page's warm-up: only its own older neighbour (page 0) is read.
    expect(server.reads()).toEqual([
      [1500, 1050],
      [500, 1050],
      [0, 1050],
    ])
    const current = model.state().current!
    expect(current).toMatchObject({ k: 1, offset: 500, length: 1050, status: 'ready' })
    expect(ids(current.bars)).toEqual(span(500, 1550))
    expect(ids(current.warmup)).toEqual(span(450, 500))
    expect(model.state().label).toBe('Page 2 of 3')
  })

  it('Next from the earliest page with r > 0 moves to page 1, where a jump to the same bar stays', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    await model.open(WIN)
    await tick()
    await model.loadPage(0)
    await tick()
    // Bar 1,000 is on the earliest page's screen (0..1049): the shortcut keeps a window move there.
    expect(model.seek(1000, 1000, 'jump')).toMatchObject({ k: 0, changed: false })
    const next = model.seek(1000, 1000, 'page')!
    expect(next).toMatchObject({ k: 1, changed: true, from: 500, to: 1549 })
    expect(await next.loaded).toBe('ready')
    expect(model.state().current!.k).toBe(1)
  })

  it('a page seek onto the page already shown places it whole with no request', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    const model = createChartPages({ fetchers: server.fetchers, schedule: manualTimers().schedule })
    await model.open(WIN)
    await tick()
    const before = server.calls.length
    const same = model.seek(2499, 2499, 'page')!
    expect(same).toMatchObject({ k: 2, changed: false, from: 1500, to: 2499 })
    expect(await same.loaded).toBe('ready')
    expect(server.calls.length).toBe(before)
  })

  it('a page seek to a page not cached reads it and its older neighbour, limit 1,050, and lands whole', async () => {
    const server = fakeServer(10_000, evenBlocks(10))
    const timers = manualTimers()
    const model = createChartPages({ fetchers: server.fetchers, schedule: timers.schedule })
    await model.open(WIN)
    await tick()
    const before = server.calls.length
    const result = model.seek(4500, 4500, 'page')!
    expect(result).toMatchObject({ k: 4, changed: true, from: 4000, to: 5049 })
    expect(await result.loaded).toBe('ready')
    expect(server.reads().slice(before)).toEqual([
      [4000, 1050],
      [3000, 1050],
    ])
    expect(ids(model.state().current!.bars)).toEqual(span(4000, 5050))
    // The page landed: the settle timer for its neighbours runs as for any other move.
    expect(timers.live().map((t) => t.ms)).toEqual([150])
  })
})

describe('createChartPages: neighbour preload', () => {
  it('preloads both neighbours only after the page landed and the user settled', async () => {
    const server = fakeServer(10_000, evenBlocks(10))
    const timers = manualTimers()
    const model = createChartPages({ fetchers: server.fetchers, schedule: timers.schedule })
    await model.open(WIN)
    await tick()
    server.failOnce.add(4000) // the warm-up companion of page 5 fails, silently
    await model.loadPage(5)
    await tick()
    expect(model.state().pageError).toBeNull()
    expect(model.pageStatus(4)).toBeUndefined()
    const before = server.calls.length
    expect(timers.live().map((t) => t.ms)).toEqual([150])
    // Nothing more until the settle timer runs.
    expect(server.calls.length).toBe(before)
    timers.fire()
    await tick()
    expect(server.offsets().slice(before).sort()).toEqual([4000, 6000])
    expect(model.pageStatus(4)).toBe('ready')
    expect(model.pageStatus(6)).toBe('ready')
  })

  it('a neighbour already cached is not read again', async () => {
    const server = fakeServer(2500, [0, 500, 1500])
    const timers = manualTimers()
    const model = createChartPages({ fetchers: server.fetchers, schedule: timers.schedule })
    await model.open(WIN)
    await tick()
    await model.loadPage(1) // page 0 rides along
    await tick()
    const before = server.calls.length
    timers.fire()
    await tick()
    expect(server.calls.length).toBe(before)
  })

  it('a new seek cancels the pending settle and aborts preloads in flight', async () => {
    const server = fakeServer(10_000, evenBlocks(10))
    const timers = manualTimers()
    const model = createChartPages({ fetchers: server.fetchers, schedule: timers.schedule })
    await model.open(WIN)
    await tick()
    await model.loadPage(5)
    await tick()
    const pendingSettle = timers.live()
    expect(pendingSettle).toHaveLength(1)
    server.state.manual = true
    void model.loadPage(2)
    expect(pendingSettle[0]!.cancelled).toBe(true)
    // Back on 5 (cached), settle and preload: page 6 is in flight when the user moves on.
    server.state.manual = false
    await model.loadPage(5)
    await tick()
    server.state.manual = true
    timers.fire()
    await tick()
    const preload = server.calls.find((c) => c.offset === 6000)!
    expect(preload.signal.aborted).toBe(false)
    void model.loadPage(8)
    expect(preload.signal.aborted).toBe(true)
    expect(model.pageStatus(6)).toBeUndefined()
  })

  it('a seek that stays on the page restarts the settle timer rather than preloading', async () => {
    const server = fakeServer(10_000, evenBlocks(10))
    const timers = manualTimers()
    const model = createChartPages({ fetchers: server.fetchers, schedule: timers.schedule })
    await model.open(WIN)
    await tick()
    await model.loadPage(5)
    await tick()
    const first = timers.live()[0]!
    model.seek(5100, 5300, 'right')
    expect(first.cancelled).toBe(true)
    expect(timers.live()).toHaveLength(1)
  })

  it('a preload that fails is silent: no error shown, and a later seek asks again', async () => {
    const server = fakeServer(10_000, evenBlocks(10))
    const timers = manualTimers()
    const model = createChartPages({ fetchers: server.fetchers, schedule: timers.schedule })
    await model.open(WIN)
    await tick()
    await model.loadPage(5)
    await tick()
    server.failOnce.add(6000)
    timers.fire()
    await tick()
    expect(model.state()).toMatchObject({ pageError: null })
    expect(model.state().current!.status).toBe('ready')
    expect(model.pageStatus(6)).toBeUndefined()
    expect(await model.loadPage(6)).toBe('ready')
    expect(server.offsets().filter((o) => o === 6000)).toHaveLength(2)
  })

  describe('with the real timer', () => {
    beforeEach(() => {
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    })
    afterEach(() => {
      vi.useRealTimers()
    })

    it('the default schedule is a 150 ms setTimeout, never an interval', async () => {
      const interval = vi.spyOn(globalThis, 'setInterval')
      const server = fakeServer(10_000, evenBlocks(10))
      const model = createChartPages({ fetchers: server.fetchers })
      await model.open(WIN)
      await tick()
      await model.loadPage(5)
      await tick()
      const before = server.calls.length
      await vi.advanceTimersByTimeAsync(149)
      expect(server.calls.length).toBe(before)
      await vi.advanceTimersByTimeAsync(1)
      expect(server.offsets().slice(before)).toEqual([6000])
      expect(interval).not.toHaveBeenCalled()
    })
  })
})

describe('apiPageFetchers', () => {
  it('reads the index and a page through the typed client, the window end kept', async () => {
    api.getDatasetBarPages.mockResolvedValue(indexMessage(1, [0]))
    api.getDatasetBars.mockResolvedValue({ bars: [barAt(0)], nextCursor: '' })
    const signal = new AbortController().signal
    await apiPageFetchers.fetchIndex(WIN, 1000, 50, signal)
    expect(api.getDatasetBarPages).toHaveBeenCalledWith('ds-1', { start: W_START, end: W_END, size: 1000, overlap: 50, signal })
    const read = await apiPageFetchers.fetchBars(WIN, new Date(T0 + 5 * MIN), 1050, signal)
    expect(api.getDatasetBars).toHaveBeenCalledWith('ds-1', { start: new Date(T0 + 5 * MIN), end: W_END, limit: 1050, signal })
    expect(ids(read)).toEqual([0])
  })
})
