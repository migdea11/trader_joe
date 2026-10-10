// The one formatter module for grids, charts and inputs (owner ruling 6, tj-mujie8, canvas page
// "Visual language"). The server sends UTC instants; a display zone is always passed in, never read
// from a global here, so every function is pure. useFormatters() binds the settings store's zone.
//
// Rules, as drawn on the canvas Data boards:
//   dates     YYYY/MM/DD                       2026/10/06
//   times     HH:mm, or HH:mm:ss when asked    08:12, 06:10:04
//   numbers   comma thousands                  1,944
//   bytes     412 MB, 18.4 GB                  (B, KB and MB whole; GB and above one decimal)
//   durations 3.8 s, 38 s                      (under 10 s one decimal, then whole seconds)
// A missing value renders as the board's dash.
//
// Values the canvas does not give (byte base, durations over a minute, compact thresholds, age
// wording beyond the drawn examples) are flagged "open" below and listed on tj-grna9p.30.

export const MISSING = '—'

/** An instant: a Date, or epoch milliseconds. */
export type Instant = Date | number

const formatterCache = new Map<string, Intl.DateTimeFormat>()

function partsFormatter(timeZone: string): Intl.DateTimeFormat {
  let formatter = formatterCache.get(timeZone)
  if (formatter === undefined) {
    // en-CA with hourCycle h23 gives numeric parts we reassemble ourselves; the locale's own
    // date layout is never used.
    formatter = new Intl.DateTimeFormat('en-CA', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    })
    formatterCache.set(timeZone, formatter)
  }
  return formatter
}

interface Fields {
  year: string
  month: string
  day: string
  hour: string
  minute: string
  second: string
}

function fields(instant: Instant, timeZone: string): Fields | null {
  const ms = instant instanceof Date ? instant.getTime() : instant
  if (!Number.isFinite(ms)) return null
  const out: Record<string, string> = {}
  try {
    for (const part of partsFormatter(timeZone).formatToParts(ms)) out[part.type] = part.value
  } catch {
    return null
  }
  return out as unknown as Fields
}

/** YYYY/MM/DD in `timeZone`. */
export function formatDate(instant: Instant, timeZone: string): string {
  const f = fields(instant, timeZone)
  return f === null ? MISSING : `${f.year}/${f.month}/${f.day}`
}

/** HH:mm in `timeZone`, or HH:mm:ss with `seconds`. */
export function formatTime(instant: Instant, timeZone: string, options: { seconds?: boolean } = {}): string {
  const f = fields(instant, timeZone)
  if (f === null) return MISSING
  return options.seconds ? `${f.hour}:${f.minute}:${f.second}` : `${f.hour}:${f.minute}`
}

/** YYYY/MM/DD HH:mm in `timeZone` (seconds with `seconds`). */
export function formatDateTime(instant: Instant, timeZone: string, options: { seconds?: boolean } = {}): string {
  const f = fields(instant, timeZone)
  if (f === null) return MISSING
  const time = options.seconds ? `${f.hour}:${f.minute}:${f.second}` : `${f.hour}:${f.minute}`
  return `${f.year}/${f.month}/${f.day} ${time}`
}

/** A protobuf Timestamp (seconds as bigint or number, optional nanos) as an instant. */
export function timestampToInstant(ts: { seconds: bigint | number; nanos?: number }): number {
  return Number(ts.seconds) * 1000 + Math.floor((ts.nanos ?? 0) / 1e6)
}

function finite(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

const integerFormat = new Intl.NumberFormat('en-US', { useGrouping: true, maximumFractionDigits: 0 })
const decimalFormats = new Map<number, Intl.NumberFormat>()

/** Comma thousands, `decimals` fixed places (default none): 1,944 and 132.39. */
export function formatNumber(value: number | null | undefined, decimals = 0): string {
  if (!finite(value)) return MISSING
  if (decimals === 0) return integerFormat.format(value)
  let format = decimalFormats.get(decimals)
  if (format === undefined) {
    format = new Intl.NumberFormat('en-US', {
      useGrouping: true,
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    })
    decimalFormats.set(decimals, format)
  }
  return format.format(value)
}

/**
 * 84k style volumes. Open: the canvas shows only whole thousands (84k, 155k); one decimal under
 * 10k and the M and B steps are this module's choice.
 */
export function formatCompact(value: number | null | undefined): string {
  if (!finite(value)) return MISSING
  const abs = Math.abs(value)
  const step = (divisor: number, suffix: string): string => {
    const scaled = value / divisor
    const text = Math.abs(scaled) < 10 ? scaled.toFixed(1).replace(/\.0$/, '') : String(Math.round(scaled))
    return `${text}${suffix}`
  }
  if (abs >= 1e9) return step(1e9, 'B')
  if (abs >= 1e6) return step(1e6, 'M')
  if (abs >= 1e3) return step(1e3, 'k')
  return integerFormat.format(value)
}

/** A 0..1 fraction as a whole percent: 61%. */
export function formatPercent(fraction: number | null | undefined): string {
  return finite(fraction) ? `${Math.round(fraction * 100)}%` : MISSING
}

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB'] as const

/**
 * 412 MB, 18.4 GB. Open: the canvas does not say decimal or binary; this uses 1000 steps. B, KB and
 * MB are whole, GB and TB take one decimal, which reproduces every size drawn on the boards.
 */
export function formatBytes(bytes: number | null | undefined): string {
  if (!finite(bytes) || bytes < 0) return MISSING
  let unit = 0
  let value = bytes
  while (value >= 1000 && unit < BYTE_UNITS.length - 1) {
    value /= 1000
    unit += 1
  }
  const text = unit >= 3 ? value.toFixed(1).replace(/\.0$/, '') : String(Math.round(value))
  return `${text} ${BYTE_UNITS[unit]}`
}

/**
 * 3.8 s, 38 s. Open: only seconds are drawn. Under a second is shown in ms, a minute or more as
 * "2 min 5 s" and an hour or more as "1 h 5 min" (this module's choice).
 */
export function formatDuration(ms: number | null | undefined): string {
  if (!finite(ms) || ms < 0) return MISSING
  if (ms < 1000) return `${Math.round(ms)} ms`
  const seconds = ms / 1000
  if (seconds < 10) return `${seconds.toFixed(1)} s`
  const whole = Math.round(seconds)
  if (whole < 60) return `${whole} s`
  const minutes = Math.floor(whole / 60)
  if (minutes < 60) {
    const rest = whole % 60
    return rest === 0 ? `${minutes} min` : `${minutes} min ${rest} s`
  }
  const hours = Math.floor(minutes / 60)
  const restMinutes = minutes % 60
  return restMinutes === 0 ? `${hours} h` : `${hours} h ${restMinutes} min`
}

/**
 * Whole days since something: "1 d ago" in tables (`short`), "97 days ago" in tiles and detail
 * (`long`, the default). Open: the canvas draws both wordings; which screen uses which is left to
 * the screen.
 */
export function formatDaysAgo(days: number | null | undefined, style: 'short' | 'long' = 'long'): string {
  if (!finite(days) || days < 0) return MISSING
  const whole = Math.floor(days)
  if (style === 'short') return `${formatNumber(whole)} d ago`
  return `${formatNumber(whole)} ${whole === 1 ? 'day' : 'days'} ago`
}

/** A day count with the short unit: "97 d". */
export function formatDays(days: number | null | undefined): string {
  return finite(days) ? `${formatNumber(Math.floor(days))} d` : MISSING
}
