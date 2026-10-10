// The Viewer's range selector (tj-grna9p.33): From and To are calendar days in the viewer's time zone
// and become the half-open window [start, end) the bars endpoint takes. To is EXCLUDED: a window
// From 2026/09/01 To 2026/09/08 holds the bars of September 1 to 7. An unset side leaves that bound
// off the request, so an empty range is the whole dataset.
//
// The URL keeps the days as YYYY-MM-DD (a slash would be escaped); the inputs show and accept
// YYYY/MM/DD, the app's date format (tj-mujie8 ruling 6).
import type { LocationQuery, LocationQueryRaw } from 'vue-router'

/** The URL query keys this screen owns. Others (the trading group) are not touched. */
export const RANGE_QUERY_KEYS = ['from', 'to'] as const

/** A day as YYYY-MM-DD. */
export interface ViewerRange {
  from?: string
  to?: string
}

export interface ApiWindow {
  start?: Date
  end?: Date
}

const DAY = /^(\d{4})[-/](\d{2})[-/](\d{2})$/

/** Normalise typed text (YYYY/MM/DD or YYYY-MM-DD) to YYYY-MM-DD, or null when it is not a real day. */
export function parseDay(text: string): string | null {
  const match = DAY.exec(text.trim())
  if (match === null) return null
  const [, y, m, d] = match
  const probe = new Date(Date.UTC(Number(y), Number(m) - 1, Number(d)))
  const real =
    probe.getUTCFullYear() === Number(y) && probe.getUTCMonth() === Number(m) - 1 && probe.getUTCDate() === Number(d)
  return real ? `${y}-${m}-${d}` : null
}

/** YYYY-MM-DD as the displayed YYYY/MM/DD. */
export function displayDay(day: string | undefined): string {
  return day === undefined ? '' : day.replaceAll('-', '/')
}

function first(value: LocationQuery[string] | undefined): string | undefined {
  const text = Array.isArray(value) ? value[0] : value
  return typeof text === 'string' ? (parseDay(text) ?? undefined) : undefined
}

/** Read the range from the URL query; anything malformed is ignored. */
export function parseRangeQuery(query: LocationQuery): ViewerRange {
  return { from: first(query.from), to: first(query.to) }
}

/** The query to navigate to: the base query with the range keys replaced; unset keys are removed. */
export function toRangeQuery(range: ViewerRange, base: LocationQuery): LocationQueryRaw {
  const query: LocationQueryRaw = { ...base }
  for (const key of RANGE_QUERY_KEYS) delete query[key]
  if (range.from !== undefined) query.from = range.from
  if (range.to !== undefined) query.to = range.to
  return query
}

// local time minus UTC, in ms, at the instant utcMs, for the zone.
function zoneOffsetMs(utcMs: number, timeZone: string): number {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(utcMs)
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value)
  const local = Date.UTC(get('year'), get('month') - 1, get('day'), get('hour'), get('minute'), get('second'))
  return local - Math.floor(utcMs / 1000) * 1000
}

/** The instant a calendar day (YYYY-MM-DD) starts in `timeZone`. */
export function zonedDayStart(day: string, timeZone: string): Date {
  const [y, m, d] = day.split('-').map(Number)
  const naive = Date.UTC(y, m - 1, d)
  const guess = naive - zoneOffsetMs(naive, timeZone)
  // The offset at the first guess may differ from the offset at midnight itself across a DST change.
  return new Date(naive - zoneOffsetMs(guess, timeZone))
}

/** The half-open window the range names in `timeZone`. */
export function toApiWindow(range: ViewerRange, timeZone: string): ApiWindow {
  return {
    start: range.from === undefined ? undefined : zonedDayStart(range.from, timeZone),
    end: range.to === undefined ? undefined : zonedDayStart(range.to, timeZone),
  }
}

/** Why the range cannot be used, or undefined: the server rejects a window whose end is not after its start. */
export function rangeProblem(range: ViewerRange): string | undefined {
  if (range.from !== undefined && range.to !== undefined && range.to <= range.from) {
    return 'To must be after From (To is not included).'
  }
  return undefined
}
