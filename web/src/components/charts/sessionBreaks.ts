// Where a trading day ends on an intraday chart. Lightweight Charts places bars by index, so the
// overnight and weekend gaps collapse and a session boundary reads as an odd bar; a marker there makes
// it plain. No Vue in here.
//
// The rule is gap-based, because the client is not told the exchange time zone (web/CLAUDE.md: coverage
// dates are UTC): a bar starts a new session when the time since the previous bar's start is at least
// SESSION_GAP_SECONDS. Known failure cases:
//   - a stretch of at least 4 hours with no bar inside a session (a halt, or an illiquid symbol on a
//     thin feed) is marked as if it were a boundary;
//   - a market with no gap that long between sessions (24 hour markets, a 24/5 overnight session) gets
//     no marker, which is right for the former and a miss for the latter;
//   - two sessions of one symbol with nothing between them but a short gap are not told apart.
// A UTC date change was considered and rejected: with extended hours the overnight gap (20:00 to 04:00
// New York) falls inside one UTC day in winter, and a post-market bar can sit on either side of UTC
// midnight without a gap at all.

/** The gap between two consecutive bars' starts that counts as a session boundary. */
export const SESSION_GAP_SECONDS = 4 * 60 * 60

/**
 * Indices i (>= 1) of the bars that start a new session: `times[i] - times[i - 1] >= gapSeconds`.
 * `times` is ascending seconds. One pass, no allocation per bar.
 */
export function sessionStarts(times: ArrayLike<number>, gapSeconds: number = SESSION_GAP_SECONDS): number[] {
  const starts: number[] = []
  for (let i = 1; i < times.length; i += 1) {
    if (times[i] - times[i - 1] >= gapSeconds) starts.push(i)
  }
  return starts
}

/**
 * The boundaries inside the view as fractions (0..1) of the plot width. The boundary before bar i sits
 * on the left edge of bar i: with the visible range [from, to] holding to - from + 1 bars, that is
 * (i - from) / (to - from + 1). `starts` is ascending, so the scan stops at the first one past the view.
 */
export function visibleBreaks(starts: readonly number[], from: number, to: number): number[] {
  const bars = to - from + 1
  if (!(bars > 0)) return []
  const out: number[] = []
  for (const i of starts) {
    const fraction = (i - from) / bars
    if (fraction >= 1) break
    if (fraction > 0) out.push(fraction)
  }
  return out
}
