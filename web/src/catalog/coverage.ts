// What a coverage strip says, in words (tj-grna9p.77; reused by the Dataset detail, tj-grna9p.34).
//
// The server decides what is expected and what is covered (CoverageBucket.expected_sessions counts
// only completed sessions, so a bucket with none is not yet due). This module only turns those
// counts into the sentences a person or a screen reader gets, so status never rests on colour.
//
// Dates. A bucket or gap boundary is the calendar's local midnight, a half-open [start, end). The
// client is not told the calendar's zone, and the display zone setting would move a midnight onto
// the neighbouring day, so a boundary is read in UTC: a local midnight of any zone west of UTC
// (every calendar in use) falls on the same UTC calendar date as its local date.
import type { CoverageBucket, CoverageGap, CoverageStrip } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { formatDate, MISSING, timestampToInstant } from '@/format/formatters'
import { COVERAGE_UNIT } from '@/theme/copy'

const DAY_MS = 24 * 60 * 60 * 1000

export interface CoverageTotals {
  /** Sessions due (completed) over the whole strip. */
  expected: number
  /** Of those, the ones that hold at least one bar. */
  withBars: number
}

export function coverageTotals(strip: CoverageStrip): CoverageTotals {
  let expected = 0
  let withBars = 0
  for (const bucket of strip.buckets) {
    expected += bucket.expectedSessions
    withBars += Math.min(bucket.sessionsWithBars, bucket.expectedSessions)
  }
  return { expected, withBars }
}

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? '' : 's'}`
}

/**
 * How one bucket reads on the per-bucket (blocks) strip: every due session holds bars, some do,
 * none does, or none is due yet.
 */
export type BucketState = 'covered' | 'partial' | 'empty' | 'not-due'

export function bucketState(bucket: CoverageBucket): BucketState {
  if (bucket.expectedSessions <= 0) return 'not-due'
  if (bucket.sessionsWithBars >= bucket.expectedSessions) return 'covered'
  return bucket.sessionsWithBars <= 0 ? 'empty' : 'partial'
}

/** The share of the bucket's due sessions that hold bars, 0..1; 0 where none is due. */
export function bucketFill(bucket: CoverageBucket): number {
  if (bucket.expectedSessions <= 0) return 0
  return Math.min(1, Math.max(0, bucket.sessionsWithBars / bucket.expectedSessions))
}

/**
 * A block's hover text, in words and with no percent: its dates and how many of its due sessions
 * hold bars, "2026/03/02 to 2026/03/06 · 4 of 5 sessions", or "... · no sessions due yet".
 * The window is half-open, so its last day is the day before its end.
 */
export function describeBucket(bucket: CoverageBucket): string {
  let dates = MISSING
  if (bucket.start !== undefined) {
    const first = formatDate(timestampToInstant(bucket.start), 'UTC')
    const last =
      bucket.end === undefined ? first : formatDate(timestampToInstant(bucket.end) - DAY_MS, 'UTC')
    dates = first === last || last < first ? first : `${first} to ${last}`
  }
  if (bucket.expectedSessions <= 0) return `${dates} · no ${COVERAGE_UNIT} due yet`
  const held = Math.min(bucket.sessionsWithBars, bucket.expectedSessions)
  return `${dates} · ${held} of ${bucket.expectedSessions} due ${COVERAGE_UNIT}`
}

/** The numbers alone, "x/y": sessions with bars over sessions due. The grid's cell shows this; its column header names the unit. */
export function coverageFraction(strip: CoverageStrip): string {
  const { expected, withBars } = coverageTotals(strip)
  return `${withBars}/${expected}`
}

/** The text beside a strip, exactly "x/y sessions": the fraction and the unit. */
export function coverageSummary(strip: CoverageStrip): string {
  return `${coverageFraction(strip)} ${COVERAGE_UNIT}`
}

/** The strip's text alternative: "248/250 sessions covered, 2 gaps". Words, so colour carries nothing. */
export function coverageLabel(strip: CoverageStrip): string {
  const gaps = strip.gapCount > 0 ? plural(strip.gapCount, 'gap') : 'no gaps'
  return `${coverageSummary(strip)} covered, ${gaps}`
}

/** "2026/03/04 to 2026/03/05 · 2 sessions", or "2026/03/04 · 1 session" for a single day. */
export function describeGap(gap: CoverageGap): string {
  const count = plural(gap.sessions, 'session')
  if (gap.start === undefined || gap.end === undefined) return count
  // The window is half-open, so its last day is the day before its end.
  const first = formatDate(timestampToInstant(gap.start), 'UTC')
  const last = formatDate(timestampToInstant(gap.end) - DAY_MS, 'UTC')
  return first === last ? `${first} · ${count}` : `${first} to ${last} · ${count}`
}

/** "and 5 more gaps" where the server sent fewer gap windows than gap_count. */
export function moreGapsNote(strip: CoverageStrip): string | undefined {
  const more = strip.gapCount - strip.gaps.length
  return more > 0 ? `and ${plural(more, 'more gap')} not listed` : undefined
}
