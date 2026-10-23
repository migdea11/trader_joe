// Day arithmetic the catalog's screens share. A day is a UTC calendar day, the basis the server
// uses for its usage groups and last-read buckets (tj-grna9p.88).

/** One day in milliseconds. */
export const DAY_MS = 24 * 60 * 60 * 1000

/** Whole UTC calendar days from `instant` to `now` (0 for today, and for an instant in the future). */
export function daysSinceUtc(instant: number, now: number = Date.now()): number {
  return Math.max(0, Math.floor(now / DAY_MS) - Math.floor(instant / DAY_MS))
}
