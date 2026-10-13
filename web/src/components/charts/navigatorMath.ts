// Pure geometry for ZoomNavigator: the mapping between the price chart's visible LOGICAL range and
// the navigator's window, and the three drag operations. No DOM, no chart library.
//
// A logical range is in bar-index units: bar i is centred on logical index i, so the full data
// spans [0, count - 1]. A window is the same range as fractions of that span, each in [0, 1].
// The chart may be scrolled past either end (whitespace); the window then clamps to the strip.

export interface LogicalRangeLike {
  from: number
  to: number
}

export interface NavWindow {
  /** Fraction of the full span, 0..1. */
  start: number
  /** Fraction of the full span, 0..1, never below start. */
  end: number
}

/** The narrowest window a drag may produce, in bars. */
export const MIN_WINDOW_BARS = 5

const clamp = (value: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, value))

// The span in bar-index units; at least 1 so a one-bar series does not divide by zero.
const spanOf = (count: number): number => Math.max(count - 1, 1)

/** The narrowest window as a fraction: MIN_WINDOW_BARS bars, but never wider than the full strip. */
export function minWindowFraction(count: number): number {
  return Math.min(MIN_WINDOW_BARS / spanOf(count), 1)
}

/** The chart's logical range as a window on the strip, clamped to the data extent. */
export function rangeToWindow(range: LogicalRangeLike, count: number): NavWindow {
  const span = spanOf(count)
  const start = clamp(range.from / span, 0, 1)
  const end = clamp(range.to / span, 0, 1)
  return { start: Math.min(start, end), end }
}

/** The window as the logical range to set on the chart. */
export function windowToRange(win: NavWindow, count: number): LogicalRangeLike {
  const span = spanOf(count)
  return { from: win.start * span, to: win.end * span }
}

/** Drag the window body: the width is kept, the window stops at either edge. */
export function moveWindow(win: NavWindow, delta: number): NavWindow {
  const width = win.end - win.start
  const start = clamp(win.start + delta, 0, 1 - width)
  return { start, end: start + width }
}

/** Drag the left handle: the right edge stays, the width never drops below `minWidth`. */
export function resizeStart(win: NavWindow, delta: number, minWidth: number): NavWindow {
  const floor = Math.min(minWidth, win.end)
  return { start: clamp(win.start + delta, 0, win.end - floor), end: win.end }
}

/** Drag the right handle: the left edge stays, the width never drops below `minWidth`. */
export function resizeEnd(win: NavWindow, delta: number, minWidth: number): NavWindow {
  const floor = Math.min(minWidth, 1 - win.start)
  return { start: win.start, end: clamp(win.end + delta, win.start + floor, 1) }
}

/** True when two logical ranges differ by less than `epsilon` bars at both ends. */
export function sameRange(a: LogicalRangeLike | null, b: LogicalRangeLike | null, epsilon = 1e-6): boolean {
  if (a === null || b === null) return a === b
  return Math.abs(a.from - b.from) < epsilon && Math.abs(a.to - b.to) < epsilon
}

// ---- Paging (tj-eozqhi, tj-grna9p.122) ------------------------------------------------------------
// With paging the strip is the WHOLE window in bar-index units [0, total - 1], the loaded page is a
// block at [offset, offset + length - 1], and the draggable window is a global range inside it.
// Everything below is in global bar indices or fractions of that strip.

/** A scroll-bar thumb never draws narrower than this, however many pages the window has. */
export const MIN_THUMB_PX = 24
/** The closes are drawn inside the block only when it is at least this wide. */
export const CLOSES_MIN_PX = 60
/** Width of a resize handle. */
export const HANDLE_PX = 10
/** Handles show only when the window is at least this many handle widths wide. */
export const HANDLES_MIN_WIDTHS = 3
/** Arrow keys move the window by this fraction of its own width. */
export const KEY_WINDOW_FRACTION = 0.1

/** Where the loaded page sits in the whole window. */
export interface PagedExtent {
  /** Bars in the whole window. */
  total: number
  /** Global index of the loaded page's first bar. */
  offset: number
  /** Bars in the loaded page. */
  length: number
}

/**
 * `left` and `right` are a drag past the block's edge (the width is kept), `jump` a move of the
 * window by the chart's own scroll. `page` is page navigation (an end arrow, the hatched area, Page
 * Up and Page Down, Home and End): the page holding `from` is shown zoomed out to the whole page.
 */
export type SeekDirection = 'left' | 'right' | 'jump' | 'page'

/** A request to load the page holding this GLOBAL bar range; the component never fetches. */
export interface SeekRequest extends LogicalRangeLike {
  direction: SeekDirection
}

/** The loaded page as a fraction window of the strip (true position, before the minimum thumb). */
export function blockWindow(extent: PagedExtent): NavWindow {
  const span = spanOf(extent.total)
  const start = clamp(extent.offset / span, 0, 1)
  const end = clamp((extent.offset + Math.max(extent.length - 1, 0)) / span, start, 1)
  return { start, end }
}

/**
 * The block as drawn: at least `minPx` wide like a scroll-bar thumb, centred on its true position
 * and kept inside the strip. Unchanged when the track width is unknown.
 */
export function widenThumb(block: NavWindow, trackPx: number, minPx = MIN_THUMB_PX): NavWindow {
  if (!(trackPx > 0) || minPx >= trackPx) return block
  const minFraction = minPx / trackPx
  const width = block.end - block.start
  if (width >= minFraction) return block
  const centre = (block.start + block.end) / 2
  const start = clamp(centre - minFraction / 2, 0, 1 - minFraction)
  return { start, end: start + minFraction }
}

/** A local logical range (the chart's, page-relative) as a global one. */
export function toGlobalRange(range: LogicalRangeLike, offset: number): LogicalRangeLike {
  return { from: range.from + offset, to: range.to + offset }
}

/** A global range as the chart's page-relative one. */
export function toLocalRange(range: LogicalRangeLike, offset: number): LogicalRangeLike {
  return { from: range.from - offset, to: range.to - offset }
}

/** True when the global range lies inside the loaded page, within `epsilon` bars. */
export function rangeInBlock(range: LogicalRangeLike, extent: PagedExtent, epsilon = 0.5): boolean {
  const first = extent.offset
  const last = extent.offset + Math.max(extent.length - 1, 0)
  return range.from >= first - epsilon && range.to <= last + epsilon
}

/** Keep a window inside the loaded block (resizing cannot leave the page). */
export function clampToBlock(win: NavWindow, block: NavWindow): NavWindow {
  const start = clamp(win.start, block.start, block.end)
  const end = clamp(win.end, start, block.end)
  return { start, end }
}

/** A window of the same width centred on `fraction`, stopping at either end of the strip. */
export function centreWindow(win: NavWindow, fraction: number): NavWindow {
  return moveWindow(win, fraction - (win.start + win.end) / 2)
}

/** A seek for a global range: whole bars, the width kept, inside [0, total - 1]. */
export function seekRequest(range: LogicalRangeLike, total: number, direction: SeekDirection): SeekRequest {
  const last = Math.max(total - 1, 0)
  const width = Math.min(Math.round(range.to - range.from), last)
  const from = clamp(Math.round(range.from), 0, last - width)
  return { from, to: from + width, direction }
}

/**
 * A bar of the page one step before or after the loaded one, or null when there is none. The
 * previous page always holds the bar just before the loaded page; the next block starts `pageBars`
 * after the loaded page's first bar (the earliest page included: its block ends there too).
 */
export function pageStepBar(extent: PagedExtent, pageBars: number, side: 'before' | 'after'): number | null {
  if (side === 'before') return extent.offset > 0 ? extent.offset - 1 : null
  const bar = extent.offset + Math.max(pageBars, 1)
  return bar <= extent.total - 1 ? bar : null
}

/** A page navigation request for the page that holds this GLOBAL bar: one bar, direction `page`. */
export function pageSeek(bar: number, total: number): SeekRequest {
  const b = clamp(Math.round(bar), 0, Math.max(total - 1, 0))
  return { from: b, to: b, direction: 'page' }
}

/** The bar a click at `fraction` of the strip stands for. */
export function barAtFraction(fraction: number, total: number): number {
  return clamp(Math.round(fraction * spanOf(total)), 0, Math.max(total - 1, 0))
}

/**
 * The key's move as a fraction of the strip, or null for a key the window ignores. Arrows move 10%
 * of the window's width (at least one bar), Page Up and Page Down one page, Home and End to the
 * ends (the width is kept, the move clamps).
 */
export function keyDelta(key: string, win: NavWindow, total: number, pageBars: number): number | null {
  const span = spanOf(total)
  const step = Math.max((win.end - win.start) * KEY_WINDOW_FRACTION, 1 / span)
  switch (key) {
    case 'ArrowLeft':
      return -step
    case 'ArrowRight':
      return step
    case 'PageUp':
      return -pageBars / span
    case 'PageDown':
      return pageBars / span
    case 'Home':
      return -1
    case 'End':
      return 1
    default:
      return null
  }
}

/** Resize handles show only when the window is wide enough to grab them. */
export function handlesVisible(win: NavWindow, trackPx: number): boolean {
  if (!(trackPx > 0)) return true
  return (win.end - win.start) * trackPx >= HANDLES_MIN_WIDTHS * HANDLE_PX
}

/** The closes are drawn when the block is wide enough to read (or its width is not known yet). */
export function closesVisible(block: NavWindow, trackPx: number): boolean {
  if (!(trackPx > 0)) return true
  return (block.end - block.start) * trackPx >= CLOSES_MIN_PX
}

/** Buckets for the drawn closes: about one per two pixels of block, within [1, max]. */
export function stripBuckets(blockPx: number, max: number): number {
  if (!(blockPx > 0)) return max
  return clamp(Math.ceil(blockPx / 2), 1, max)
}

/**
 * Reduce a series to at most `buckets` points for the strip, keeping the lowest and highest value of
 * each bucket so spikes survive. Returns [min, max] pairs in bucket order.
 */
export function downsampleMinMax(values: ArrayLike<number>, buckets: number): Array<[number, number]> {
  const n = values.length
  if (n === 0 || buckets < 1) return []
  const count = Math.min(buckets, n)
  const out: Array<[number, number]> = []
  for (let b = 0; b < count; b++) {
    const lo = Math.floor((b * n) / count)
    const hi = Math.max(lo + 1, Math.floor(((b + 1) * n) / count))
    let min = Infinity
    let max = -Infinity
    for (let i = lo; i < hi; i++) {
      const v = values[i]
      if (v < min) min = v
      if (v > max) max = v
    }
    out.push([min, max])
  }
  return out
}
