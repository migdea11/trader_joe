import { describe, expect, it, vi } from 'vitest'

import { DAY_MS, daysSinceUtc } from './days'

// tj-grna9p.88 (architect ruling B): the one day-length constant and the one "UTC calendar days
// since" helper, on the same basis as the server's usage groups (the UTC date of the last read).
describe('daysSinceUtc', () => {
  const MIDNIGHT = Date.UTC(2026, 9, 14)

  it('DAY_MS is one day', () => {
    expect(DAY_MS).toBe(86_400_000)
  })

  it('23:59:59Z the day before is one day ago at 00:00:00Z; the same day is zero', () => {
    expect(daysSinceUtc(MIDNIGHT - 1000, MIDNIGHT)).toBe(1)
    expect(daysSinceUtc(MIDNIGHT, MIDNIGHT)).toBe(0)
    expect(daysSinceUtc(MIDNIGHT, MIDNIGHT + DAY_MS - 1000)).toBe(0)
    expect(daysSinceUtc(MIDNIGHT - DAY_MS, MIDNIGHT + DAY_MS - 1)).toBe(1)
    expect(daysSinceUtc(MIDNIGHT - DAY_MS - 1, MIDNIGHT)).toBe(2)
  })

  it('counts calendar dates, not 24-hour spans', () => {
    // 2 hours apart, across midnight: one day.
    expect(daysSinceUtc(MIDNIGHT - 3_600_000, MIDNIGHT + 3_600_000)).toBe(1)
    // 47 hours apart, inside two dates' span: one day.
    expect(daysSinceUtc(MIDNIGHT + 1000, MIDNIGHT + DAY_MS + 23 * 3_600_000)).toBe(1)
  })

  it('an instant in the future is zero, never negative', () => {
    expect(daysSinceUtc(MIDNIGHT + 5 * DAY_MS, MIDNIGHT)).toBe(0)
  })

  it('defaults now to the clock', () => {
    vi.useFakeTimers({ now: MIDNIGHT + 1000 })
    try {
      expect(daysSinceUtc(MIDNIGHT - 1000)).toBe(1)
    } finally {
      vi.useRealTimers()
    }
  })
})
