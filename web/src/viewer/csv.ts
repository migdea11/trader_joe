// Export CSV for the Viewer (tj-grna9p.33, tj-grna9p.126): the text of a bar list, built client-side.
// viewer/exportCsv.ts writes a whole window with it, page by page.
// The first column is the bar's start in the viewer's zone, YYYY/MM/DD (with HH:mm for bars narrower
// than a day). Every other value is a number, written as is.
import type { Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'

import { int64ToNumber } from '@/api/int64'
import { formatDate, formatDateTime } from '@/format/formatters'

export const CSV_HEADER = 'date,open,high,low,close,volume'

/** The data lines of `bars`, each ending in a newline and no header: a chunk of a file written in pieces. */
export function barsToCsvRows(bars: readonly Bar[], timeZone: string, intraday: boolean): string {
  let out = ''
  for (const bar of bars) {
    if (!bar.barStart) continue
    const ms = int64ToNumber(bar.barStart.seconds) * 1000
    const when = intraday ? formatDateTime(ms, timeZone) : formatDate(ms, timeZone)
    out += `${when},${bar.open},${bar.high},${bar.low},${bar.close},${bar.volume}\n`
  }
  return out
}

export function barsToCsv(bars: readonly Bar[], timeZone: string, intraday: boolean): string {
  return `${CSV_HEADER}\n${barsToCsvRows(bars, timeZone, intraday)}`
}
