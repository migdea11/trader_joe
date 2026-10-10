// Export CSV for the Viewer (tj-grna9p.33): exactly the bars the chart holds, built client-side with
// no request. The first column is the bar's start in the viewer's zone, YYYY/MM/DD (with HH:mm for
// bars narrower than a day). Every other value is a number, written as is.
import type { Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'

import { int64ToNumber } from '@/api/int64'
import { formatDate, formatDateTime } from '@/format/formatters'

export const CSV_HEADER = 'date,open,high,low,close,volume'

export function barsToCsv(bars: readonly Bar[], timeZone: string, intraday: boolean): string {
  const lines = [CSV_HEADER]
  for (const bar of bars) {
    if (!bar.barStart) continue
    const ms = int64ToNumber(bar.barStart.seconds) * 1000
    const when = intraday ? formatDateTime(ms, timeZone) : formatDate(ms, timeZone)
    lines.push(`${when},${bar.open},${bar.high},${bar.low},${bar.close},${bar.volume}`)
  }
  return `${lines.join('\n')}\n`
}
