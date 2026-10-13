import { describe, expect, it } from 'vitest'
import { create } from '@bufbuild/protobuf'
import type { MessageInitShape } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import { CoverageBucketSchema, CoverageGapSchema, CoverageStripSchema } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { MISSING } from '@/format/formatters'
import { COVERAGE_UNIT } from '@/theme/copy'
import {
  bucketFill,
  bucketState,
  coverageFraction,
  coverageLabel,
  coverageSummary,
  coverageTotals,
  describeBucket,
  describeGap,
  moreGapsNote,
} from './coverage'

// tj-grna9p.77, reshaped by tj-grna9p.108 (reused by tj-grna9p.34): what a coverage strip says in
// words. The server decides what is due (expected_sessions counts completed sessions only) and what
// is covered; these functions only put the counts into words, so status never rests on colour.
// Fixtures are built with create(Schema) from the generated ui/v1 messages.
//
// The text beside the strip is exactly "x/y <unit>" (owner feedback 2026-10-12): no percent, no gap
// count, no "Fully covered". The unit word is COVERAGE_UNIT, imported rather than spelled here,
// because the owner may change it (sessions, trading days) and these specs must not redden for that.

const DAY = 86_400_000
// A calendar's local midnight in New York is 04:00Z or 05:00Z: it must read as the same UTC day.
const MAR_2 = Date.UTC(2026, 2, 2, 5)

function strip(buckets: [number, number][], extra: MessageInitShape<typeof CoverageStripSchema> = {}) {
  return create(CoverageStripSchema, {
    buckets: buckets.map(([expected, withBars]) => ({ expectedSessions: expected, sessionsWithBars: withBars })),
    ...extra,
  })
}

const xy = (x: number, y: number) => `${x}/${y} ${COVERAGE_UNIT}`

describe('coverageTotals', () => {
  it('x is the sum of sessions_with_bars and y the sum of expected_sessions over every bucket', () => {
    expect(coverageTotals(strip([[5, 5], [5, 2], [0, 0], [3, 1]]))).toEqual({ expected: 13, withBars: 8 })
  })

  it('counts a bucket with more bars than due sessions only up to what was due', () => {
    // A bucket holding bars for sessions not yet due must not push x past y.
    expect(coverageTotals(strip([[5, 5], [3, 9], [0, 4]]))).toEqual({ expected: 8, withBars: 8 })
  })

  it('no buckets, or nothing due, is 0 of 0', () => {
    expect(coverageTotals(strip([]))).toEqual({ expected: 0, withBars: 0 })
    expect(coverageTotals(strip([[0, 0], [0, 0]]))).toEqual({ expected: 0, withBars: 0 })
  })
})

describe('coverageSummary is exactly "x/y <unit>"', () => {
  it.each([
    ['fully covered', [[125, 125], [125, 125]], 250, 250],
    ['gappy', [[125, 124], [125, 124]], 248, 250],
    ['one session', [[1, 1]], 1, 1],
    ['empty dataset: nothing stored of what is due', [[100, 0], [150, 0]], 0, 250],
    ['nothing due yet', [[0, 0], [0, 0]], 0, 0],
    ['no buckets at all', [], 0, 0],
  ] as [string, [number, number][], number, number][])('%s', (_name, buckets, x, y) => {
    expect(coverageSummary(strip(buckets, { gapCount: 3 }))).toBe(xy(x, y))
  })

  it('carries no percent, no gap count and no "Fully covered", whatever the gap count', () => {
    for (const s of [strip([[200, 199]], { gapCount: 1 }), strip([[5, 5]]), strip([[5, 0]], { gapCount: 1 })]) {
      const text = coverageSummary(s)
      expect(text).toMatch(new RegExp(`^\\d+/\\d+ ${COVERAGE_UNIT}$`))
      expect(text).not.toMatch(/%|gap|Fully/i)
    }
  })

  it('the unit word is the COVERAGE_UNIT constant', () => {
    expect(coverageSummary(strip([[3, 2]])).endsWith(` ${COVERAGE_UNIT}`)).toBe(true)
    expect(COVERAGE_UNIT.length).toBeGreaterThan(0)
  })
})

// tj-grna9p.112 (owner 2026-10-13): the Datasets grid cell shows the numbers alone, the unit moving to
// the column header. coverageFraction is that text; coverageSummary (the Dataset detail and the
// label) is the fraction followed by the unit, so the two can never disagree on the numbers.
describe('coverageFraction is exactly "x/y", no unit', () => {
  it.each([
    ['fully covered', [[125, 125], [125, 125]], '250/250'],
    ['gappy', [[125, 124], [125, 124]], '248/250'],
    ['small', [[5, 5]], '5/5'],
    ['empty dataset: nothing stored of what is due', [[100, 0], [150, 0]], '0/250'],
    ['nothing due yet', [[0, 0], [0, 0]], '0/0'],
    ['no buckets at all', [], '0/0'],
    ['more bars than due counts only what was due', [[5, 5], [3, 9]], '8/8'],
  ] as [string, [number, number][], string][])('%s', (_name, buckets, text) => {
    expect(coverageFraction(strip(buckets, { gapCount: 2 }))).toBe(text)
  })

  it('carries no unit word, no percent and no gap count', () => {
    for (const s of [strip([[200, 199]], { gapCount: 1 }), strip([[5, 5]]), strip([[5, 0]], { gapCount: 1 })]) {
      const text = coverageFraction(s)
      expect(text).toMatch(/^\d+\/\d+$/)
      expect(text).not.toContain(COVERAGE_UNIT)
    }
  })

  it('coverageSummary is the fraction, a space, then the unit', () => {
    for (const s of [strip([[125, 124], [125, 124]]), strip([[0, 0]]), strip([[250, 0]]), strip([])]) {
      expect(coverageSummary(s)).toBe(`${coverageFraction(s)} ${COVERAGE_UNIT}`)
    }
  })
})

describe('coverageLabel (the strip\'s text alternative)', () => {
  it('names x/y covered and the gap count in words', () => {
    expect(coverageLabel(strip([[125, 124], [125, 124]], { gapCount: 2 }))).toBe(`${xy(248, 250)} covered, 2 gaps`)
  })

  it('a single gap is singular', () => {
    expect(coverageLabel(strip([[10, 9]], { gapCount: 1 }))).toBe(`${xy(9, 10)} covered, 1 gap`)
  })

  it('no gaps says so', () => {
    expect(coverageLabel(strip([[5, 5]]))).toBe(`${xy(5, 5)} covered, no gaps`)
    expect(coverageLabel(strip([]))).toBe(`${xy(0, 0)} covered, no gaps`)
  })

  it('counts gap_count, not only the gap windows the server listed', () => {
    const gaps = [{ sessions: 1 }]
    expect(coverageLabel(strip([[10, 5]], { gaps, gapCount: 4 }))).toBe(`${xy(5, 10)} covered, 4 gaps`)
  })
})

describe('describeGap', () => {
  it('a gap of one day is that day; the half-open end is not shown as a gap day', () => {
    const gap = create(CoverageGapSchema, { start: timestampFromMs(MAR_2), end: timestampFromMs(MAR_2 + DAY), sessions: 1 })
    expect(describeGap(gap)).toBe('2026/03/02 · 1 session')
  })

  it('a longer gap reads first day to last day (the day before its end)', () => {
    const gap = create(CoverageGapSchema, {
      start: timestampFromMs(MAR_2),
      end: timestampFromMs(MAR_2 + 5 * DAY),
      sessions: 4,
    })
    expect(describeGap(gap)).toBe('2026/03/02 to 2026/03/06 · 4 sessions')
  })

  it('a boundary at a calendar midnight west of UTC reads as its local date, in UTC', () => {
    // 2026-03-09 is the first Monday of US daylight time: New York midnight is 04:00Z.
    const start = Date.UTC(2026, 2, 9, 4)
    const gap = create(CoverageGapSchema, { start: timestampFromMs(start), end: timestampFromMs(start + DAY), sessions: 1 })
    expect(describeGap(gap)).toBe('2026/03/09 · 1 session')
  })

  it('a gap without its window still says its session count', () => {
    expect(describeGap(create(CoverageGapSchema, { sessions: 2 }))).toBe('2 sessions')
  })
})

// tj-grna9p.111: the per-bucket helpers the Dataset detail's blocks strip needs, restored without
// the percent tj-grna9p.108 removed.
function bucket(expected: number, withBars: number, extra: MessageInitShape<typeof CoverageBucketSchema> = {}) {
  return create(CoverageBucketSchema, { expectedSessions: expected, sessionsWithBars: withBars, ...extra })
}

describe('bucketState', () => {
  it.each([
    ['every due session holds bars', 5, 5, 'covered'],
    ['more bars than due sessions', 3, 9, 'covered'],
    ['some due sessions hold bars', 5, 4, 'partial'],
    ['one of many', 20, 1, 'partial'],
    ['none of the due sessions', 5, 0, 'empty'],
    ['nothing due yet', 0, 0, 'not-due'],
    ['nothing due, though bars are held', 0, 4, 'not-due'],
  ] as [string, number, number, string][])('%s', (_name, expected, withBars, state) => {
    expect(bucketState(bucket(expected, withBars))).toBe(state)
  })
})

describe('bucketFill (the covered share, 0..1)', () => {
  it('is sessions with bars over sessions due', () => {
    expect(bucketFill(bucket(20, 17))).toBeCloseTo(0.85, 10)
    expect(bucketFill(bucket(5, 5))).toBe(1)
    expect(bucketFill(bucket(5, 0))).toBe(0)
  })

  it('never passes 1, and is 0 where nothing is due', () => {
    expect(bucketFill(bucket(3, 9))).toBe(1)
    expect(bucketFill(bucket(0, 0))).toBe(0)
    expect(bucketFill(bucket(0, 4))).toBe(0)
  })
})

describe('describeBucket (a block\'s hover, no percent)', () => {
  it('a multi-day bucket reads first day to last day (the day before its half-open end) and x of y due', () => {
    const b = bucket(5, 4, { start: timestampFromMs(MAR_2), end: timestampFromMs(MAR_2 + 5 * DAY) })
    expect(describeBucket(b)).toBe(`2026/03/02 to 2026/03/06 · 4 of 5 due ${COVERAGE_UNIT}`)
  })

  it('a one-day bucket is that day', () => {
    const b = bucket(1, 1, { start: timestampFromMs(MAR_2), end: timestampFromMs(MAR_2 + DAY) })
    expect(describeBucket(b)).toBe(`2026/03/02 · 1 of 1 due ${COVERAGE_UNIT}`)
  })

  it('a boundary at a calendar midnight west of UTC reads as its local date', () => {
    const start = Date.UTC(2026, 2, 9, 4)
    const b = bucket(2, 0, { start: timestampFromMs(start), end: timestampFromMs(start + 2 * DAY) })
    expect(describeBucket(b)).toBe(`2026/03/09 to 2026/03/10 · 0 of 2 due ${COVERAGE_UNIT}`)
  })

  it('counts held sessions only up to what was due', () => {
    const b = bucket(3, 9, { start: timestampFromMs(MAR_2), end: timestampFromMs(MAR_2 + 3 * DAY) })
    expect(describeBucket(b)).toBe(`2026/03/02 to 2026/03/04 · 3 of 3 due ${COVERAGE_UNIT}`)
  })

  it('a bucket with nothing due says so', () => {
    const b = bucket(0, 0, { start: timestampFromMs(MAR_2), end: timestampFromMs(MAR_2 + 2 * DAY) })
    expect(describeBucket(b)).toBe(`2026/03/02 to 2026/03/03 · no ${COVERAGE_UNIT} due yet`)
  })

  it('a bucket without its window still says its counts, after the dash', () => {
    expect(describeBucket(bucket(5, 2))).toBe(`${MISSING} · 2 of 5 due ${COVERAGE_UNIT}`)
  })

  it('never carries a percent', () => {
    for (const b of [bucket(3, 1), bucket(7, 5), bucket(200, 199), bucket(0, 0), bucket(5, 0)]) {
      expect(describeBucket(b)).not.toContain('%')
    }
  })
})

describe('moreGapsNote', () => {
  it('counts the gaps the server did not list', () => {
    const gaps = [{ sessions: 1 }, { sessions: 2 }]
    expect(moreGapsNote(strip([], { gaps, gapCount: 7 }))).toBe('and 5 more gaps not listed')
    expect(moreGapsNote(strip([], { gaps, gapCount: 3 }))).toBe('and 1 more gap not listed')
  })

  it('is absent when every gap is listed', () => {
    expect(moreGapsNote(strip([], { gaps: [{ sessions: 1 }], gapCount: 1 }))).toBeUndefined()
    expect(moreGapsNote(strip([]))).toBeUndefined()
  })
})
