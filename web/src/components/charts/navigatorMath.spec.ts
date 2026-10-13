import { describe, expect, it } from 'vitest'

import { simpleMovingAverage } from './indicators'
import {
  CLOSES_MIN_PX,
  HANDLE_PX,
  HANDLES_MIN_WIDTHS,
  KEY_WINDOW_FRACTION,
  MIN_THUMB_PX,
  MIN_WINDOW_BARS,
  blockWindow,
  centreWindow,
  clampToBlock,
  closesVisible,
  handlesVisible,
  keyDelta,
  rangeInBlock,
  seekRequest,
  stripBuckets,
  barAtFraction,
  pageSeek,
  pageStepBar,
  toGlobalRange,
  toLocalRange,
  widenThumb,
  downsampleMinMax,
  minWindowFraction,
  moveWindow,
  rangeToWindow,
  resizeEnd,
  resizeStart,
  sameRange,
  windowToRange,
} from './navigatorMath'

// tj-grna9p.53: the navigator's pure geometry and the SMA overlay maths.

function close(a: { start: number; end: number }, b: { start: number; end: number }): void {
  expect(a.start).toBeCloseTo(b.start, 12)
  expect(a.end).toBeCloseTo(b.end, 12)
}

describe('rangeToWindow and windowToRange', () => {
  it('the full data extent is the whole strip, and back', () => {
    expect(rangeToWindow({ from: 0, to: 99 }, 100)).toEqual({ start: 0, end: 1 })
    expect(windowToRange({ start: 0, end: 1 }, 100)).toEqual({ from: 0, to: 99 })
  })

  it.each([
    [{ start: 0.25, end: 0.75 }, 100],
    [{ start: 0.1, end: 0.1000001 }, 10_000],
    [{ start: 0, end: 0.5 }, 2],
    [{ start: 0.3, end: 0.9 }, 50_000],
  ])('round trips %j over %i bars', (win, count) => {
    close(rangeToWindow(windowToRange(win, count), count), win)
  })

  it('clamps a range scrolled past either end into the strip', () => {
    expect(rangeToWindow({ from: -20, to: 150 }, 101)).toEqual({ start: 0, end: 1 })
    expect(rangeToWindow({ from: 120, to: 150 }, 101)).toEqual({ start: 1, end: 1 })
    expect(rangeToWindow({ from: -50, to: -10 }, 101)).toEqual({ start: 0, end: 0 })
  })

  it('never returns start after end', () => {
    const win = rangeToWindow({ from: 80, to: 20 }, 101)
    expect(win.start).toBeLessThanOrEqual(win.end)
  })

  it.each([0, 1])('does not divide by zero with %i bars', (count) => {
    const win = rangeToWindow({ from: 0, to: 0.5 }, count)
    expect(Number.isFinite(win.start) && Number.isFinite(win.end)).toBe(true)
    expect(windowToRange({ start: 0, end: 1 }, count)).toEqual({ from: 0, to: 1 })
  })

  it('two bars span one logical unit', () => {
    expect(windowToRange({ start: 0, end: 1 }, 2)).toEqual({ from: 0, to: 1 })
    expect(rangeToWindow({ from: 0.5, to: 1 }, 2)).toEqual({ start: 0.5, end: 1 })
  })
})

describe('minimum window', () => {
  it('is five bars', () => {
    expect(MIN_WINDOW_BARS).toBe(5)
    expect(minWindowFraction(101)).toBeCloseTo(5 / 100, 12)
    expect(minWindowFraction(7)).toBeCloseTo(5 / 6, 12)
  })

  it.each([0, 1, 2, 3, 6])('is never wider than the strip (%i bars)', (count) => {
    expect(minWindowFraction(count)).toBe(1)
  })
})

describe('moveWindow', () => {
  it('keeps the width', () => {
    close(moveWindow({ start: 0.2, end: 0.5 }, 0.1), { start: 0.3, end: 0.6 })
  })

  it('stops at the right edge with the width kept', () => {
    close(moveWindow({ start: 0.2, end: 0.5 }, 0.9), { start: 0.7, end: 1 })
  })

  it('stops at the left edge with the width kept', () => {
    close(moveWindow({ start: 0.2, end: 0.5 }, -0.9), { start: 0, end: 0.3 })
  })

  it('cannot move a full-width window', () => {
    expect(moveWindow({ start: 0, end: 1 }, 0.3)).toEqual({ start: 0, end: 1 })
  })
})

describe('resizeStart and resizeEnd', () => {
  it('the left handle keeps the right edge and the minimum width', () => {
    close(resizeStart({ start: 0.2, end: 0.5 }, 0.5, 0.1), { start: 0.4, end: 0.5 })
    close(resizeStart({ start: 0.2, end: 0.5 }, -0.9, 0.1), { start: 0, end: 0.5 })
  })

  it('the right handle keeps the left edge and the minimum width', () => {
    close(resizeEnd({ start: 0.2, end: 0.5 }, -0.9, 0.1), { start: 0.2, end: 0.3 })
    close(resizeEnd({ start: 0.2, end: 0.5 }, 0.9, 0.1), { start: 0.2, end: 1 })
  })

  it('a window already narrower than the minimum at an edge stays in the strip', () => {
    close(resizeStart({ start: 0, end: 0.05 }, 0.5, 0.1), { start: 0, end: 0.05 })
    close(resizeEnd({ start: 0.95, end: 1 }, -0.5, 0.1), { start: 0.95, end: 1 })
  })
})

describe('sameRange', () => {
  it('compares within epsilon, and null only equals null', () => {
    expect(sameRange(null, null)).toBe(true)
    expect(sameRange(null, { from: 0, to: 1 })).toBe(false)
    expect(sameRange({ from: 0, to: 1 }, null)).toBe(false)
    expect(sameRange({ from: 1, to: 2 }, { from: 1 + 1e-9, to: 2 - 1e-9 })).toBe(true)
    expect(sameRange({ from: 1, to: 2 }, { from: 1, to: 2.001 })).toBe(false)
  })
})

describe('downsampleMinMax', () => {
  it('is empty for no values or no buckets', () => {
    expect(downsampleMinMax([], 10)).toEqual([])
    expect(downsampleMinMax([1, 2, 3], 0)).toEqual([])
  })

  it('gives one bucket per value when there are fewer values than buckets', () => {
    expect(downsampleMinMax([3, 1, 2], 400)).toEqual([
      [3, 3],
      [1, 1],
      [2, 2],
    ])
  })

  it('keeps a one-bar spike and dip that an average would flatten', () => {
    const values = new Float64Array(10_000).fill(100)
    values[5_437] = 900
    values[12] = -40
    const buckets = downsampleMinMax(values, 400)
    expect(buckets).toHaveLength(400)
    expect(Math.max(...buckets.map(([, max]) => max))).toBe(900)
    expect(Math.min(...buckets.map(([min]) => min))).toBe(-40)
    expect(buckets[0]).toEqual([-40, 100])
    expect(buckets[Math.floor((5_437 * 400) / 10_000)]).toEqual([100, 900])
  })

  it('covers every value exactly once across the buckets', () => {
    const values = Array.from({ length: 1003 }, (_, i) => i)
    const buckets = downsampleMinMax(values, 7)
    expect(buckets[0]![0]).toBe(0)
    expect(buckets[6]![1]).toBe(1002)
    for (let b = 1; b < buckets.length; b++) expect(buckets[b]![0]).toBe(buckets[b - 1]![1] + 1)
  })
})

describe('simpleMovingAverage', () => {
  it('averages the trailing window, with NaN for the warm-up', () => {
    expect(Array.from(simpleMovingAverage([1, 2, 3, 4, 5], 2))).toEqual([NaN, 1.5, 2.5, 3.5, 4.5])
    expect(Array.from(simpleMovingAverage([2, 4, 6], 3))).toEqual([NaN, NaN, 4])
  })

  it('period 1 is the series itself', () => {
    expect(Array.from(simpleMovingAverage([5, 6, 7], 1))).toEqual([5, 6, 7])
  })

  it('SMA 20 and SMA 50 over closes 1..60 match the fixture', () => {
    const closes = Array.from({ length: 60 }, (_, i) => i + 1)
    const sma20 = simpleMovingAverage(closes, 20)
    const sma50 = simpleMovingAverage(closes, 50)
    expect(Number.isNaN(sma20[18])).toBe(true)
    expect(sma20[19]).toBe(10.5)
    expect(sma20[59]).toBe(50.5)
    expect(Number.isNaN(sma50[48])).toBe(true)
    expect(sma50[49]).toBe(25.5)
    expect(sma50[59]).toBe(35.5)
  })

  it.each([0, -1, 2.5, Number.NaN, 4])('an invalid period (%s on 3 values) gives all NaN and the same length', (period) => {
    const out = simpleMovingAverage([1, 2, 3], period)
    expect(out).toHaveLength(3)
    expect(Array.from(out).every(Number.isNaN)).toBe(true)
  })

  it('an empty series gives an empty result', () => {
    expect(simpleMovingAverage([], 20)).toHaveLength(0)
  })
})

// tj-grna9p.122 (decision tj-eozqhi, fixed blocks of 2026-10-13): with paging the strip is the WHOLE
// window in bar-index units [0, N - 1] and the loaded page is a block inside it. A window of N = 2,500
// has pages at offsets 0, 500 and 1,500; a page holds 1,050 bars (block + look-ahead), the newest 1,000.
describe('paging geometry', () => {
  it('the constants: a 24 px minimum thumb, closes from 60 px, 10 px handles shown from 3 widths, 10% keys', () => {
    expect([MIN_THUMB_PX, CLOSES_MIN_PX, HANDLE_PX, HANDLES_MIN_WIDTHS, KEY_WINDOW_FRACTION]).toEqual([24, 60, 10, 3, 0.1])
  })

  it('blockWindow places the loaded page at its true fraction of the whole window', () => {
    close(blockWindow({ total: 2500, offset: 1500, length: 1000 }), { start: 1500 / 2499, end: 1 })
    close(blockWindow({ total: 2500, offset: 500, length: 1050 }), { start: 500 / 2499, end: 1549 / 2499 })
  })

  it('the earliest page sits at [0, its length): it overlaps the next block, not shifted to the remainder', () => {
    // N = 2,020: r = 20, the next block starts at 20, yet the earliest page still starts at bar 0.
    close(blockWindow({ total: 2020, offset: 0, length: 1050 }), { start: 0, end: 1049 / 2019 })
    close(blockWindow({ total: 2020, offset: 20, length: 1050 }), { start: 20 / 2019, end: 1069 / 2019 })
  })

  it('widenThumb draws a block of a 190-page window at the minimum width, centred, inside the strip', () => {
    const total = 190_000
    const middle = blockWindow({ total, offset: 94_000, length: 1050 })
    expect((middle.end - middle.start) * 1000).toBeLessThan(MIN_THUMB_PX)
    const drawn = widenThumb(middle, 1000)
    expect(drawn.end - drawn.start).toBeCloseTo(MIN_THUMB_PX / 1000, 12)
    expect((drawn.start + drawn.end) / 2).toBeCloseTo((middle.start + middle.end) / 2, 12)
    close(widenThumb(blockWindow({ total, offset: 0, length: 1050 }), 1000), { start: 0, end: 0.024 })
    close(widenThumb(blockWindow({ total, offset: 189_000, length: 1000 }), 1000), { start: 0.976, end: 1 })
  })

  it('widenThumb leaves a wide block, and any block on an unmeasured track, as it is', () => {
    const wide = blockWindow({ total: 2500, offset: 500, length: 1050 })
    expect(widenThumb(wide, 1000)).toEqual(wide)
    const thin = blockWindow({ total: 190_000, offset: 94_000, length: 1050 })
    expect(widenThumb(thin, 0)).toEqual(thin)
  })

  it('toGlobalRange and toLocalRange are the page offset apart', () => {
    expect(toGlobalRange({ from: 10, to: 210 }, 1500)).toEqual({ from: 1510, to: 1710 })
    expect(toLocalRange({ from: 1510, to: 1710 }, 1500)).toEqual({ from: 10, to: 210 })
  })

  it('rangeInBlock holds the loaded bars give or take half a bar', () => {
    const extent = { total: 2500, offset: 500, length: 1050 }
    expect(rangeInBlock({ from: 499.5, to: 1549.5 }, extent)).toBe(true)
    expect(rangeInBlock({ from: 499.4, to: 900 }, extent)).toBe(false)
    expect(rangeInBlock({ from: 900, to: 1549.6 }, extent)).toBe(false)
  })

  it('clampToBlock keeps a resized window inside the block', () => {
    close(clampToBlock({ start: 0.1, end: 0.9 }, { start: 0.2, end: 0.6 }), { start: 0.2, end: 0.6 })
    close(clampToBlock({ start: 0.3, end: 0.4 }, { start: 0.2, end: 0.6 }), { start: 0.3, end: 0.4 })
  })

  it('centreWindow keeps the width and stops at the ends', () => {
    close(centreWindow({ start: 0.4, end: 0.5 }, 0.2), { start: 0.15, end: 0.25 })
    close(centreWindow({ start: 0.4, end: 0.5 }, 0.99), { start: 0.9, end: 1 })
  })

  it('seekRequest is whole bars of the same width inside [0, N - 1], with its direction', () => {
    expect(seekRequest({ from: 1350.1, to: 1550.1 }, 2500, 'left')).toEqual({ from: 1350, to: 1550, direction: 'left' })
    expect(seekRequest({ from: -40, to: 160 }, 2500, 'left')).toEqual({ from: 0, to: 200, direction: 'left' })
    expect(seekRequest({ from: 2400, to: 2600 }, 2500, 'right')).toEqual({ from: 2299, to: 2499, direction: 'right' })
    expect(seekRequest({ from: 0, to: 9000 }, 2500, 'jump')).toEqual({ from: 0, to: 2499, direction: 'jump' })
  })

  it('keyDelta: arrows 10% of the window, Page Up and Down one page, Home and End to the ends', () => {
    const total = 2500
    const win = { start: 1600 / 2499, end: 1800 / 2499 } // 200 bars
    expect(keyDelta('ArrowRight', win, total, 1000)! * 2499).toBeCloseTo(20, 9)
    expect(keyDelta('ArrowLeft', win, total, 1000)! * 2499).toBeCloseTo(-20, 9)
    expect(keyDelta('PageDown', win, total, 1000)! * 2499).toBeCloseTo(1000, 9)
    expect(keyDelta('PageUp', win, total, 1000)! * 2499).toBeCloseTo(-1000, 9)
    expect(keyDelta('Home', win, total, 1000)).toBe(-1)
    expect(keyDelta('End', win, total, 1000)).toBe(1)
    expect(keyDelta('Enter', win, total, 1000)).toBeNull()
  })

  it('keyDelta moves at least one bar for a very narrow window', () => {
    const win = { start: 0.5, end: 0.5 + 2 / 189_999 } // 2 bars of 190,000
    expect(keyDelta('ArrowRight', win, 190_000, 1000)! * 189_999).toBeCloseTo(1, 9)
  })

  it('handles show from three handle widths; the closes from 60 px; both when the track is unmeasured', () => {
    expect(handlesVisible({ start: 0, end: 0.03 }, 1000)).toBe(true)
    expect(handlesVisible({ start: 0, end: 0.029 }, 1000)).toBe(false)
    expect(handlesVisible({ start: 0, end: 0.001 }, 0)).toBe(true)
    expect(closesVisible({ start: 0, end: 0.06 }, 1000)).toBe(true)
    expect(closesVisible({ start: 0, end: 0.059 }, 1000)).toBe(false)
    expect(closesVisible({ start: 0, end: 0.001 }, 0)).toBe(true)
  })

  it('stripBuckets is about one per two pixels of block, within [1, max]', () => {
    expect(stripBuckets(400, 400)).toBe(200)
    expect(stripBuckets(1, 400)).toBe(1)
    expect(stripBuckets(5000, 400)).toBe(400)
    expect(stripBuckets(0, 400)).toBe(400)
  })
})

// tj-grna9p.129 (owner 2026-10-13): page navigation names ONE bar of the target page; the parent shows
// that page whole. N = 2,500 has pages at 0, 500 and 1,500 (1,000-bar blocks, the earliest a full page
// overlapping the next block).
describe('page navigation', () => {
  it('pageStepBar before is the bar just before the loaded page; none on the earliest page', () => {
    expect(pageStepBar({ total: 2500, offset: 1500, length: 1000 }, 1000, 'before')).toBe(1499)
    expect(pageStepBar({ total: 2500, offset: 500, length: 1050 }, 1000, 'before')).toBe(499)
    expect(pageStepBar({ total: 2500, offset: 0, length: 1050 }, 1000, 'before')).toBeNull()
  })

  it('pageStepBar after is offset + pageBars (the next block\'s first bar); none past the window', () => {
    expect(pageStepBar({ total: 2500, offset: 500, length: 1050 }, 1000, 'after')).toBe(1500)
    // The earliest page with r = 500: bar 1,000 sits in page 1's block [500, 1500), so it is page 1.
    expect(pageStepBar({ total: 2500, offset: 0, length: 1050 }, 1000, 'after')).toBe(1000)
    expect(pageStepBar({ total: 2500, offset: 1500, length: 1000 }, 1000, 'after')).toBeNull()
    // Exactly the last bar is still a page; one past it is not.
    expect(pageStepBar({ total: 2000, offset: 999, length: 1001 }, 1000, 'after')).toBe(1999)
    expect(pageStepBar({ total: 1999, offset: 999, length: 1000 }, 1000, 'after')).toBeNull()
  })

  it('pageStepBar never steps by less than one bar', () => {
    expect(pageStepBar({ total: 10, offset: 3, length: 3 }, 0, 'after')).toBe(4)
  })

  it('pageSeek is one whole bar, from = to, direction page, clamped into [0, N - 1]', () => {
    expect(pageSeek(1499, 2500)).toEqual({ from: 1499, to: 1499, direction: 'page' })
    expect(pageSeek(249.6, 2500)).toEqual({ from: 250, to: 250, direction: 'page' })
    expect(pageSeek(-3, 2500)).toEqual({ from: 0, to: 0, direction: 'page' })
    expect(pageSeek(9000, 2500)).toEqual({ from: 2499, to: 2499, direction: 'page' })
    expect(pageSeek(5, 0)).toEqual({ from: 0, to: 0, direction: 'page' })
  })

  it('barAtFraction maps a strip fraction to the whole bar there, inside the window', () => {
    expect(barAtFraction(0, 2500)).toBe(0)
    expect(barAtFraction(1, 2500)).toBe(2499)
    expect(barAtFraction(0.1, 2500)).toBe(250) // round(249.9)
    expect(barAtFraction(0.5, 2501)).toBe(1250)
    expect(barAtFraction(1.2, 2500)).toBe(2499)
    expect(barAtFraction(-0.2, 2500)).toBe(0)
    expect(barAtFraction(0.7, 1)).toBe(0)
  })
})
