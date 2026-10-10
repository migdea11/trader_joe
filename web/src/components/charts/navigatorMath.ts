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
