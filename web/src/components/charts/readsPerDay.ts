// The Reads per Day chart's data (tj-grna9p.89): the server's entries (one per UTC day and client)
// made into a continuous day axis and one stacked series per client. Plain data, no Vue and no
// ECharts, so it can be read and pinned on its own. Counting is the server's; this only lays the
// numbers out, and a day with no entry is a zero.
import type { ReadsPerDay } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { DAY_MS } from '@/format/days'

export interface ReadsSeries {
  clientId: string
  /** One count per axis day, in `days` order. */
  reads: number[]
  total: number
}

export interface ReadsPerDayModel {
  /** UTC calendar days, 'YYYY-MM-DD', oldest first, `window` of them ending today (or the last entry). */
  days: string[]
  /** Clients by total reads, most first, so the biggest sits at the base of the stack. */
  series: ReadsSeries[]
}

function dayText(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10)
}

/** The UTC day `text` ('YYYY-MM-DD') as a millisecond instant. */
function dayMs(text: string): number {
  return Date.parse(`${text}T00:00:00Z`)
}

/** Lay `message`'s entries over the last `window` UTC days up to `now`. */
export function readsPerDayModel(message: ReadsPerDay, window: number, now: number = Date.now()): ReadsPerDayModel {
  let last = Math.floor(now / DAY_MS) * DAY_MS
  for (const entry of message.days) {
    const at = dayMs(entry.day)
    if (Number.isFinite(at)) last = Math.max(last, at)
  }
  const days = Array.from({ length: window }, (_, i) => dayText(last - (window - 1 - i) * DAY_MS))
  const index = new Map(days.map((day, i) => [day, i]))

  const byClient = new Map<string, ReadsSeries>()
  for (const entry of message.days) {
    const at = index.get(entry.day)
    if (at === undefined) continue
    let series = byClient.get(entry.clientId)
    if (series === undefined) {
      series = { clientId: entry.clientId, reads: new Array<number>(window).fill(0), total: 0 }
      byClient.set(entry.clientId, series)
    }
    const reads = Number(entry.reads)
    series.reads[at] = (series.reads[at] ?? 0) + reads
    series.total += reads
  }
  const series = [...byClient.values()].sort((a, b) => b.total - a.total || a.clientId.localeCompare(b.clientId))
  return { days, series }
}
