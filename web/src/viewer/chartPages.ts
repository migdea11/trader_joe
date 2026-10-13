// The chart page model (decision tj-eozqhi, rulings tj-grna9p.118): the chart holds ONE page of at
// most PAGE_BARS raw bars, pages are located by the server's page index of the From/To window, and
// the scroll bar is the pager. No Vue in here: the screen wires it (tj-grna9p.124), the tests drive it.
//
// PAGES (fixed blocks, owner rulings 2026-10-13). The server cuts a window of N bars into blocks of
// PAGE_BARS counted back from the newest bar: with r = N mod PAGE_BARS the offsets (0-based from the
// window's first bar, oldest first) are [0 when r > 0], r, r + PAGE_BARS, ..., N - PAGE_BARS; a window
// of at most PAGE_BARS bars is the single page [0]. The earliest page (offset 0) is the display
// exception: it holds PAGE_BARS bars and so overlaps the next block. A page SHOWS its block plus a
// look-ahead of PAGE_OVERLAP_BARS bars of the NEXT (newer) block: its bars are read with
// start = pages[k].start, end = the window's end, limit = PAGE_BARS + PAGE_OVERLAP_BARS (the newest
// page has no look-ahead, so the read returns fewer): one index seek, any page.
//
// WARM-UP. A page's moving averages need the bars before it. Showing page k therefore also requests
// page k-1 at once (a companion, not after the settle delay) and the model hands the page the
// WARMUP_BARS bars just before its first bar (`CurrentPage.warmup`), so the averages start at the
// page's first bar and their start is off screen, except on the earliest page, which has no older
// neighbour. A page is never held back for its companion.
//
// CACHE (owner rule, tj-grna9p.118): a page that landed stays in a local LRU of CACHE_PAGES entries
// keyed by (dataset id, window start, window end, page offset). The current page and its two
// neighbours are never evicted, so stepping to a neighbour is never a wait. A new window or dataset
// (`open`) drops the whole cache, aborts what is in flight and fetches the index again.
//
// PRELOAD. After the current page LANDS and the user then settles for SETTLE_MS, both neighbours
// (the one on each side that exists; the older one is usually there already as the warm-up
// companion) are fetched in the background. Any new seek that changes page
// cancels the pending timer and aborts every preload in flight, so a preload is never ahead of a
// user-requested load; a seek that stays on the page only restarts the timer (the user is still
// moving). A preload that fails is dropped silently: a later seek asks again and shows the error then.
//
// MEMORY. A cached page holds its decoded `Bar` objects, about 312 B per bar measured (about 312 KB a
// page, about 6 MB at 20 pages). Objects, not columns: the chart, the SMAs and the CSV read `Bar`
// fields directly, a columnar copy (about 47 B per bar, about 0.9 MB at 20 pages) would be rebuilt on
// every read for no wait saved, and 6 MB is small next to the grid. The arrays are never made
// reactive: a holder wraps them in shallowRef/markRaw.
import { timestampDate } from '@bufbuild/protobuf/wkt'

import type { Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'
import type { BarPageIndex } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { getDatasetBarPages, getDatasetBars, int64ToNumber } from '@/api'
import { formatNumber } from '@/format/formatters'

/** Bars on one chart page: the owner's cap. */
export const PAGE_BARS = 1000
/** The look-ahead: bars of the next (newer) block a page shows after its own block. */
export const PAGE_OVERLAP_BARS = 50
/**
 * Bars before a page handed to its indicators. At least the longest SMA period minus one (SMA_PERIODS
 * in stores/datasetViewer.ts: 50), so the averages are full at the page's first bar.
 */
export const WARMUP_BARS = 50
/** Pages the cache keeps. */
export const CACHE_PAGES = 20
/** How long the user must be still, after a page landed, before the neighbours are preloaded. */
export const SETTLE_MS = 150

export interface ChartPageStart {
  /** The page's first bar; the `start` of its bars read. */
  start: Date
  /** The page's first bar as a 0-based position among the window's bars. */
  offset: number
}

export interface ChartPageIndex {
  /** Bars in the whole window. */
  barCount: number
  /** Bars per page the index was cut with. */
  pageSize: number
  /** The look-ahead bars a page shows from the next block. */
  overlap: number
  /** Ascending by offset; empty for an empty window. */
  pages: ChartPageStart[]
}

/** `page` is page navigation: the page holding the bar, shown whole (its block plus the look-ahead). */
export type SeekDirection = 'left' | 'right' | 'jump' | 'page'

/** The wire message as the model's numbers and Dates (int64 through `int64ToNumber`). */
export function toChartPageIndex(message: BarPageIndex): ChartPageIndex {
  const pages = message.pages.map((page) => {
    if (page.start === undefined) throw new Error('bar page index: a page has no start')
    return { start: timestampDate(page.start), offset: int64ToNumber(page.offset) }
  })
  return {
    barCount: int64ToNumber(message.barCount),
    pageSize: message.pageSize > 0 ? message.pageSize : PAGE_BARS,
    overlap: message.overlap,
    pages,
  }
}

/**
 * Bars page k shows: its block plus the look-ahead, up to the window's last bar. The earliest page
 * holds a full block too (it overlaps the next one), so only a window shorter than a block is short.
 */
export function pageLength(index: ChartPageIndex, k: number): number {
  const page = index.pages[k]
  if (page === undefined) return 0
  return Math.max(0, Math.min(index.pageSize + index.overlap, index.barCount - page.offset))
}

/**
 * The bars page k shows that the NEXT page shows too (the overlap), as PAGE-relative bar indices,
 * inclusive; null on the newest page and on a single page. The next page's first bar is where it
 * starts: the next block, so `pageSize` bars in for a normal page and r bars in for the earliest page
 * of a window with r = N mod pageSize > 0 (which holds a full block and so overlaps the next one).
 * It runs to the page's last shown bar, the look-ahead included.
 */
export function overlapRange(index: ChartPageIndex, k: number): { from: number; to: number } | null {
  const page = index.pages[k]
  const next = index.pages[k + 1]
  if (page === undefined || next === undefined) return null
  const length = pageLength(index, k)
  const from = next.offset - page.offset
  if (from < 0 || from > length - 1) return null
  return { from, to: length - 1 }
}

/**
 * The bars page k shows that the PREVIOUS (older) page shows too, as PAGE-relative bar indices,
 * inclusive; null on the first page and on a single page. They are page k's first bars: the older
 * page reads PAGE_BARS + overlap bars from its own start, so its look-ahead runs into this page's
 * block (and for the earliest page of a window with r = N mod pageSize > 0, which holds a full block,
 * through bar `pageSize`, so the overlap is pageSize - r + overlap bars). The mirror of `overlapRange`
 * on page k - 1; the bars are in both pages' reads, nothing is missing.
 */
export function overlapBeforeRange(index: ChartPageIndex, k: number): { from: number; to: number } | null {
  const page = index.pages[k]
  const older = index.pages[k - 1]
  if (page === undefined || older === undefined) return null
  const to = Math.min(older.offset + pageLength(index, k - 1) - 1 - page.offset, pageLength(index, k) - 1)
  if (to < 0) return null
  return { from: 0, to }
}

/** Global index of page k's last bar. */
function lastBar(index: ChartPageIndex, k: number): number {
  return index.pages[k].offset + pageLength(index, k) - 1
}

function clamp(value: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, value))
}

/** The greatest k whose offset is at or before `bar`, or -1 when none. */
function lastPageAtOrBefore(index: ChartPageIndex, bar: number): number {
  let lo = 0
  let hi = index.pages.length - 1
  let found = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (index.pages[mid].offset <= bar) {
      found = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return found
}

/**
 * The page to show for the global window [a, b] (bar indices; whole bars are taken, the ends clamp
 * into the window). The current page when the bars it shows hold the whole window. Otherwise the
 * page whose block holds the window's LEADING edge (a moving left or jumping, b moving right): the
 * latest page starting at or before it, so a bar in the overlap of the earliest page and the next one
 * belongs to the NEWER block. Never outside 0..M-1; 0 for a single page.
 */
export function pageForWindow(
  index: ChartPageIndex,
  current: number | null,
  a: number,
  b: number,
  direction: SeekDirection,
): number {
  const count = index.pages.length
  if (count <= 1) return 0
  const last = Math.max(index.barCount - 1, 0)
  const from = clamp(Math.round(Math.min(a, b)), 0, last)
  const to = clamp(Math.round(Math.max(a, b)), 0, last)
  // Page navigation always places the page holding the bar, even when the current page shows it too
  // (the bar after a page's block sits in its look-ahead, and still belongs to the next page).
  if (direction !== 'page' && current !== null && current >= 0 && current < count) {
    if (index.pages[current].offset <= from && to <= lastBar(index, current)) return current
  }
  const lead = direction === 'right' ? to : from
  return Math.max(lastPageAtOrBefore(index, lead), 0)
}

/** A global bar range on page k as the page-relative (logical) one the chart takes. */
export function toLocal(page: ChartPageStart, a: number, b: number): { from: number; to: number } {
  return { from: a - page.offset, to: b - page.offset }
}

/** A page-relative (logical) range as global bar indices. */
export function toGlobal(page: ChartPageStart, from: number, to: number): { from: number; to: number } {
  return { from: from + page.offset, to: to + page.offset }
}

/**
 * The window to show on page k for a request [a, b]: the width is kept (at most the page) and so is
 * the leading edge (b moving right, a otherwise); the window then shifts to lie inside the page.
 * Direction `page` ignores the request's width: the whole page. Global bar indices.
 */
export function placeWindow(
  index: ChartPageIndex,
  k: number,
  a: number,
  b: number,
  direction: SeekDirection,
): { from: number; to: number } {
  const page = index.pages[k]
  if (page === undefined) return { from: a, to: b }
  const first = page.offset
  const last = lastBar(index, k)
  // Page navigation shows the whole page: every bar it shows, the look-ahead included.
  if (direction === 'page') return { from: first, to: last }
  const width = Math.min(Math.abs(b - a), Math.max(last - first, 0))
  const low = Math.min(a, b)
  const high = Math.max(a, b)
  const start = direction === 'right' ? high - width : low
  const from = clamp(start, first, last - width)
  return { from, to: from + width }
}

/** The bars of `older` (page k-1, its first bar at global `olderOffset`) that sit just before page k. */
export function warmupTail(older: readonly Bar[], olderOffset: number, pageOffset: number): readonly Bar[] {
  const end = pageOffset - olderOffset
  if (end <= 0 || end > older.length) return []
  return older.slice(Math.max(end - WARMUP_BARS, 0), end)
}

/** "Page N of M" with thousands separators; null when the window is one page (or empty): no label. */
export function pageLabel(index: ChartPageIndex | null, k: number | null): string | null {
  if (index === null || k === null || index.pages.length <= 1) return null
  return `Page ${formatNumber(k + 1)} of ${formatNumber(index.pages.length)}`
}

// ---- the model -------------------------------------------------------------------------------------

/** The From/To window of a dataset: half-open [start, end), either end open. */
export interface PageWindow {
  datasetId: string
  start?: Date
  end?: Date
}

/** The two reads the model makes; the default is the real API, a test passes its own. */
export interface PageFetchers {
  fetchIndex(win: PageWindow, size: number, overlap: number, signal: AbortSignal): Promise<BarPageIndex>
  fetchBars(win: PageWindow, from: Date, limit: number, signal: AbortSignal): Promise<readonly Bar[]>
}

export const apiPageFetchers: PageFetchers = {
  fetchIndex: (win, size, overlap, signal) =>
    getDatasetBarPages(win.datasetId, { start: win.start, end: win.end, size, overlap, signal }),
  fetchBars: async (win, from, limit, signal) => {
    const page = await getDatasetBars(win.datasetId, { start: from, end: win.end, limit, signal })
    return page.bars
  },
}

export type PageStatus = 'loading' | 'ready' | 'error'
export type LoadOutcome = 'ready' | 'error' | 'aborted'
export type IndexStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface CurrentPage {
  /** 0-based page number. */
  k: number
  /** Global index of the page's first bar. */
  offset: number
  /** Bars the page shows: its block plus the look-ahead (whatever has landed). */
  length: number
  /** Where the page starts in time. */
  start: Date
  status: PageStatus
  /** The page's bars once ready, else empty. Never reactive. */
  bars: readonly Bar[]
  /**
   * The (up to WARMUP_BARS) bars just before the page's first bar, from its older neighbour once that
   * has landed; empty before, and on the earliest page. Same identity until the neighbour changes.
   */
  warmup: readonly Bar[]
}

/** What the screen reads; a new object after every change. */
export interface ChartPagesState {
  indexStatus: IndexStatus
  index: ChartPageIndex | null
  indexError: unknown
  current: CurrentPage | null
  /** Error of the current page when its status is `error`. */
  pageError: unknown
  /** Bars in the whole window (the navigator's totalBars). */
  totalBars: number
  /** Global offset of the current page (the navigator's pageOffset). */
  pageOffset: number
  /** Bars a navigator Page Up or Page Down moves (the navigator's pageSize): one block. */
  pageSize: number
  /** "Page N of M", or '' for one page. */
  label: string
  /** Bumps on every change, for a holder that needs a cheap trigger. */
  version: number
}

export interface SeekResult {
  /** The page the window now belongs to. */
  k: number
  /** The page changed (a load was started or answered from the cache). */
  changed: boolean
  /** The window to show, global bar indices: width and leading edge kept, inside the page. */
  from: number
  to: number
  /** Settles when the page is ready (immediately if cached); never rejects. */
  loaded: Promise<LoadOutcome>
}

export interface ChartPagesOptions {
  fetchers?: PageFetchers
  /** Pages kept; the current page and its neighbours are never evicted. */
  capacity?: number
  settleMs?: number
  /** Bars per page and look-ahead the index is asked for. */
  pageBars?: number
  overlap?: number
  /** Which page `open` shows: the newest by default (owner ruling). */
  initial?: 'newest' | 'oldest'
  /** One timer, cancellable; setTimeout by default (never an interval). */
  schedule?: (run: () => void, ms: number) => () => void
}

export interface ChartPages {
  /** Start a window: clears the cache, aborts everything, fetches the index, then the first page. */
  open(win: PageWindow): Promise<LoadOutcome>
  /** `open` again for the same window (a reload, a reconnect). */
  reload(): Promise<LoadOutcome>
  /** Show page k (user-requested): aborts other loads and preloads, answers from the cache if it can. */
  loadPage(k: number): Promise<LoadOutcome>
  /** Move to the page for the global window [a, b]; null before the index and first page exist. */
  seek(a: number, b: number, direction: SeekDirection): SeekResult | null
  /** Abort everything and cancel timers; the cache is kept. */
  cancel(): void
  state(): ChartPagesState
  subscribe(listener: () => void): () => void
  /** The status of page k, or undefined when it is not cached or in flight. */
  pageStatus(k: number): PageStatus | undefined
  /** Cached page keys, least recently used first (test hook). */
  cachedKeys(): string[]
  /** The cache key of page k in the open window (test hook). */
  keyOf(k: number): string
}

interface Entry {
  key: string
  k: number
  status: PageStatus
  bars: readonly Bar[]
  error: unknown
  controller: AbortController | null
  /** A user-requested load: not abandoned as a preload, shows its error. */
  user: boolean
  promise: Promise<LoadOutcome>
}

function defaultSchedule(run: () => void, ms: number): () => void {
  const handle = setTimeout(run, ms)
  return () => clearTimeout(handle)
}

export function createChartPages(options: ChartPagesOptions = {}): ChartPages {
  const fetchers = options.fetchers ?? apiPageFetchers
  const capacity = Math.max(options.capacity ?? CACHE_PAGES, 3)
  const settleMs = options.settleMs ?? SETTLE_MS
  const pageBars = options.pageBars ?? PAGE_BARS
  const overlap = options.overlap ?? PAGE_OVERLAP_BARS
  const schedule = options.schedule ?? defaultSchedule

  // Insertion order is recency: a touch deletes and sets the key again. Shallow by construction.
  const entries = new Map<string, Entry>()
  const listeners = new Set<() => void>()
  let win: PageWindow | null = null
  let generation = 0
  let version = 0
  let indexStatus: IndexStatus = 'idle'
  let index: ChartPageIndex | null = null
  let indexError: unknown = null
  let indexController: AbortController | null = null
  let currentK: number | null = null
  let cancelSettle: (() => void) | null = null

  function notify(): void {
    version += 1
    for (const listener of [...listeners]) listener()
  }

  function keyOf(k: number): string {
    const offset = index?.pages[k]?.offset ?? k
    const w = win
    const parts = [w?.datasetId ?? '', w?.start?.getTime() ?? '', w?.end?.getTime() ?? '', offset]
    return parts.join('|')
  }

  function protectedKeys(): Set<string> {
    const keys = new Set<string>()
    if (currentK === null) return keys
    for (const k of [currentK - 1, currentK, currentK + 1]) keys.add(keyOf(k))
    return keys
  }

  function evict(): void {
    if (entries.size <= capacity) return
    const keep = protectedKeys()
    for (const [key, entry] of entries) {
      if (entries.size <= capacity) return
      if (entry.status === 'loading' || keep.has(key)) continue
      entries.delete(key)
    }
  }

  function abortEntries(...except: string[]): void {
    for (const [key, entry] of [...entries]) {
      if (entry.status !== 'loading' || except.includes(key)) continue
      entry.controller?.abort()
      entries.delete(key)
    }
  }

  function clearSettle(): void {
    cancelSettle?.()
    cancelSettle = null
  }

  function currentEntry(): Entry | undefined {
    return currentK === null ? undefined : entries.get(keyOf(currentK))
  }

  function preloadNeighbours(): void {
    cancelSettle = null
    if (index === null || currentK === null) return
    if (currentEntry()?.status !== 'ready') return
    for (const k of [currentK - 1, currentK + 1]) {
      if (k < 0 || k >= index.pages.length) continue
      const existing = entries.get(keyOf(k))
      if (existing !== undefined && existing.status !== 'error') continue
      void load(k, false)
    }
  }

  function settle(): void {
    clearSettle()
    cancelSettle = schedule(preloadNeighbours, settleMs)
  }

  function load(k: number, user: boolean): Promise<LoadOutcome> {
    const idx = index
    const w = win
    if (idx === null || w === null || idx.pages[k] === undefined) return Promise.resolve('error')
    const key = keyOf(k)
    const existing = entries.get(key)
    if (existing !== undefined && existing.status !== 'error') {
      if (user) existing.user = true
      if (existing.status === 'ready') {
        entries.delete(key)
        entries.set(key, existing)
      }
      return existing.promise
    }
    if (existing !== undefined) entries.delete(key)

    const controller = new AbortController()
    const entry: Entry = {
      key,
      k,
      status: 'loading',
      bars: [],
      error: null,
      controller,
      user,
      promise: Promise.resolve('aborted'),
    }
    entries.set(key, entry)
    evict()
    entry.promise = fetchers
      .fetchBars(w, idx.pages[k].start, idx.pageSize + idx.overlap, controller.signal)
      .then((bars): LoadOutcome => {
        if (entries.get(key) !== entry || controller.signal.aborted) return 'aborted'
        entry.status = 'ready'
        entry.bars = bars
        entry.controller = null
        evict()
        notify()
        return 'ready'
      })
      .catch((error: unknown): LoadOutcome => {
        if (entries.get(key) !== entry || controller.signal.aborted) return 'aborted'
        entry.controller = null
        if (!entry.user) {
          // A preload nobody asked to see: forget it, a later seek asks again.
          entries.delete(key)
          return 'error'
        }
        entry.status = 'error'
        entry.error = error
        notify()
        return 'error'
      })
    return entry.promise
  }

  function showPage(k: number): Promise<LoadOutcome> {
    clearSettle()
    const key = keyOf(k)
    // The older neighbour rides along at once: its tail is the page's indicator warm-up. It is a
    // companion, not a user load, so a failure is silent and a new seek aborts both.
    const hasOlder = k > 0
    abortEntries(key, ...(hasOlder ? [keyOf(k - 1)] : []))
    currentK = k
    const promise = load(k, true)
    if (hasOlder) void load(k - 1, false)
    notify()
    void promise.then((outcome) => {
      if (outcome === 'ready' && currentK === k) settle()
    })
    return promise
  }

  async function open(next: PageWindow): Promise<LoadOutcome> {
    cancel()
    entries.clear()
    generation += 1
    const mine = generation
    win = { datasetId: next.datasetId, start: next.start, end: next.end }
    index = null
    indexError = null
    currentK = null
    indexStatus = 'loading'
    const controller = new AbortController()
    indexController = controller
    notify()
    let loaded: ChartPageIndex
    try {
      const message = await fetchers.fetchIndex(win, pageBars, overlap, controller.signal)
      if (mine !== generation) return 'aborted'
      loaded = toChartPageIndex(message)
    } catch (error) {
      if (mine !== generation || controller.signal.aborted) return 'aborted'
      indexStatus = 'error'
      indexError = error
      notify()
      return 'error'
    }
    indexController = null
    index = loaded
    indexStatus = 'ready'
    if (loaded.pages.length === 0) {
      notify()
      return 'ready'
    }
    return showPage(options.initial === 'oldest' ? 0 : loaded.pages.length - 1)
  }

  function cancel(): void {
    clearSettle()
    indexController?.abort()
    indexController = null
    abortEntries()
  }

  // The tail of the older neighbour, sliced once per neighbour array so its identity is stable.
  const NO_WARMUP: readonly Bar[] = []
  let warmMemo: { source: readonly Bar[]; offset: number; out: readonly Bar[] } | null = null

  function warmupOf(idx: ChartPageIndex, k: number): readonly Bar[] {
    const older = k > 0 ? entries.get(keyOf(k - 1)) : undefined
    if (older === undefined || older.status !== 'ready') return NO_WARMUP
    const offset = idx.pages[k].offset
    if (warmMemo === null || warmMemo.source !== older.bars || warmMemo.offset !== offset) {
      const out = warmupTail(older.bars, idx.pages[k - 1].offset, offset)
      warmMemo = { source: older.bars, offset, out: out.length === 0 ? NO_WARMUP : out }
    }
    return warmMemo.out
  }

  function state(): ChartPagesState {
    const entry = currentEntry()
    const page = currentK === null ? undefined : index?.pages[currentK]
    const current: CurrentPage | null =
      index !== null && currentK !== null && page !== undefined
        ? {
            k: currentK,
            offset: page.offset,
            length: pageLength(index, currentK),
            start: page.start,
            status: entry?.status ?? 'loading',
            bars: entry?.status === 'ready' ? entry.bars : [],
            warmup: warmupOf(index, currentK),
          }
        : null
    return {
      indexStatus,
      index,
      indexError,
      current,
      pageError: entry?.status === 'error' ? entry.error : null,
      totalBars: index?.barCount ?? 0,
      pageOffset: current?.offset ?? 0,
      pageSize: index?.pageSize ?? pageBars,
      label: pageLabel(index, currentK) ?? '',
      version,
    }
  }

  return {
    open,
    reload: () => (win === null ? Promise.resolve('error') : open(win)),
    loadPage: (k) => (index === null || k < 0 || k >= index.pages.length ? Promise.resolve('error') : showPage(k)),
    seek(a, b, direction) {
      if (index === null || currentK === null || index.pages.length === 0) return null
      const k = pageForWindow(index, currentK, a, b, direction)
      const placed = placeWindow(index, k, a, b, direction)
      if (k === currentK) {
        // Still on the page: the user is moving, so only the preload timer restarts.
        if (currentEntry()?.status === 'ready') settle()
        const entry = currentEntry()
        const outcome: LoadOutcome = entry?.status === 'error' ? 'error' : 'ready'
        return { k, changed: false, ...placed, loaded: entry?.promise ?? Promise.resolve(outcome) }
      }
      return { k, changed: true, ...placed, loaded: showPage(k) }
    },
    cancel,
    state,
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    pageStatus: (k) => entries.get(keyOf(k))?.status,
    cachedKeys: () => [...entries.keys()],
    keyOf,
  }
}
