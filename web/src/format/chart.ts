// Time formatting for the charts, in the viewer's zone. PriceChart currently shows UTC; to apply the
// settings zone it must spread chartLocalization(zone) into the chart's `localization` option and
// chartTimeScale(zone) into `timeScale` (and re-apply both with applyOptions when the zone changes).
// That is a change inside PriceChart, which tj-grna9p.30 does not make: it is handed to the Viewer
// bead (tj-grna9p.33) or to whoever the orchestrator routes it to.
//
// This file imports no charting library, so the parameter types are structural: a Lightweight
// Charts `Time` is a UTC timestamp in seconds (number) for the intraday and daily series used
// here; anything else (a business-day object or string) renders as the missing dash.
import { MISSING, formatDate, formatDateTime, formatTime } from './formatters'

/** Lightweight Charts date pattern for the crosshair label, the same YYYY/MM/DD as everywhere. */
export const CHART_DATE_FORMAT = 'yyyy/MM/dd'

// Values of Lightweight Charts' TickMarkType enum.
const TICK_YEAR = 0
const TICK_MONTH = 1
const TICK_DAY_OF_MONTH = 2
const TICK_TIME = 3
const TICK_TIME_WITH_SECONDS = 4

function toMs(time: unknown): number | null {
  return typeof time === 'number' && Number.isFinite(time) ? time * 1000 : null
}

/** Crosshair label: YYYY/MM/DD HH:mm in `timeZone`. */
export function chartTimeFormatter(timeZone: string): (time: unknown) => string {
  return (time) => {
    const ms = toMs(time)
    return ms === null ? MISSING : formatDateTime(ms, timeZone)
  }
}

/** Time-axis tick label by tick kind: year, month, day, time. */
export function chartTickMarkFormatter(timeZone: string): (time: unknown, tickMarkType: number) => string {
  return (time, tickMarkType) => {
    const ms = toMs(time)
    if (ms === null) return MISSING
    switch (tickMarkType) {
      case TICK_YEAR:
        return formatDate(ms, timeZone).slice(0, 4)
      case TICK_MONTH:
        return formatDate(ms, timeZone).slice(0, 7)
      case TICK_DAY_OF_MONTH:
        return formatDate(ms, timeZone).slice(5)
      case TICK_TIME:
        return formatTime(ms, timeZone)
      case TICK_TIME_WITH_SECONDS:
        return formatTime(ms, timeZone, { seconds: true })
      default:
        return formatDateTime(ms, timeZone)
    }
  }
}

/** The `localization` option for createChart / applyOptions. */
export function chartLocalization(timeZone: string) {
  return { dateFormat: CHART_DATE_FORMAT, timeFormatter: chartTimeFormatter(timeZone) }
}

/** The `timeScale` option carrying the tick formatter. */
export function chartTimeScale(timeZone: string) {
  return { tickMarkFormatter: chartTickMarkFormatter(timeZone) }
}
