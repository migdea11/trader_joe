import { describe, expect, it } from 'vitest'

import { simpleMovingAverage } from './indicators'
import {
  MIN_WINDOW_BARS,
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
