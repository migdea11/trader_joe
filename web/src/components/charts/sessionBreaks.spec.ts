import { describe, expect, it } from 'vitest'

import { SESSION_GAP_SECONDS, sessionStarts, visibleBreaks } from './sessionBreaks'

// f8b620b (owner 2026-10-13: "session transitions, so those should have some marker to make it clear
// where a day ends"). The rule is gap-based: bar i (>= 1) starts a session when its start is at least
// SESSION_GAP_SECONDS (4 h) after bar i - 1's start. visibleBreaks places boundary i on the left edge of
// bar i in a view holding to - from + 1 bars: (i - from) / (to - from + 1), kept only strictly inside
// (0, 1). Pure functions, no Vue.

const H = 3600
const M = 60

describe('sessionStarts', () => {
  it('the gap is four hours', () => {
    expect(SESSION_GAP_SECONDS).toBe(4 * H)
  })

  it('a gap of exactly 4 h counts; 3 h 59 m does not', () => {
    expect(sessionStarts([0, 4 * H])).toEqual([1])
    expect(sessionStarts([0, 4 * H - M])).toEqual([])
    expect(sessionStarts([0, 4 * H - 1])).toEqual([])
  })

  it('fewer than two bars have no boundary', () => {
    expect(sessionStarts([])).toEqual([])
    expect(sessionStarts([1_700_000_000])).toEqual([])
  })

  it('returns the index of the LATER bar of each gap, ascending', () => {
    // Two sessions of hourly bars and a third after a weekend; the first bar is never a boundary.
    const day = [0, H, 2 * H, 3 * H]
    const times = [...day, ...day.map((t) => t + 24 * H), ...day.map((t) => t + 96 * H)]
    expect(sessionStarts(times)).toEqual([4, 8])
  })

  it('a 1 m session with a 3 h lull inside it is one session; a 5 h lull is marked (a known false boundary)', () => {
    const lull = (hours: number) => [0, M, 2 * M, 2 * M + hours * H, 3 * M + hours * H]
    expect(sessionStarts(lull(3))).toEqual([])
    expect(sessionStarts(lull(5))).toEqual([3])
  })

  it('every 1 h intraday granularity bar stays inside its session: only the overnight gaps count', () => {
    // US regular hours as hourly bars 13:00..20:00 UTC, three days running.
    const session = Array.from({ length: 8 }, (_, j) => 13 * H + j * H)
    const times = [0, 1, 2].flatMap((d) => session.map((t) => t + d * 24 * H))
    expect(sessionStarts(times)).toEqual([8, 16])
  })

  it('takes any ArrayLike and a custom gap', () => {
    expect(sessionStarts(Float64Array.from([0, 10, 30, 31]), 20)).toEqual([2])
  })
})

describe('visibleBreaks', () => {
  it('a boundary is at (i - from) / (to - from + 1) of the plot', () => {
    expect(visibleBreaks([10, 20], 0, 29)).toEqual([10 / 30, 20 / 30])
    expect(visibleBreaks([20], 12, 29)).toEqual([8 / 18])
    // A fractional range from the library keeps the same arithmetic.
    expect(visibleBreaks([10], 2.5, 21.5)[0]).toBeCloseTo(7.5 / 20, 12)
  })

  it('excludes boundaries at or left of the view\'s left edge and at or right of its right edge', () => {
    // n = 10 bars: i = 5 is 0 (the edge itself), i = 15 is 1 (past the last bar).
    expect(visibleBreaks([3, 5, 6, 14, 15, 16], 5, 14)).toEqual([1 / 10, 9 / 10])
  })

  it('no boundaries, or an empty or backwards view, give none', () => {
    expect(visibleBreaks([], 0, 10)).toEqual([])
    expect(visibleBreaks([3], 5, 4)).toEqual([])
    expect(visibleBreaks([3], 5, 3)).toEqual([])
  })
})
