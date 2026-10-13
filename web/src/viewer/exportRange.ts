// What the Export CSV popover asks for (tj-grna9p.126): the two date fields and the Earliest / Latest
// ticks become the half-open window [start, end) of the bars read, with the Viewer's own typed-day
// parser and rules (viewer/range.ts), so a malformed day is refused the way the sidebar refuses it.
//
// A ticked bound is not a date at all: Earliest leaves `start` off and Latest leaves `end` off, so the
// file holds every stored bar from the first / to the last (a last bar is INCLUDED; a typed To day is
// not, the app's half-open rule). The ticked field only SHOWS the first / last bar's day.
import { formatDate } from '@/format/formatters'

import { parseDay, rangeProblem, zonedDayStart } from './range'

export interface ExportRangeInput {
  /** The From field as typed (YYYY/MM/DD or YYYY-MM-DD); empty leaves the bound off. */
  fromText: string
  toText: string
  /** The Earliest / Latest ticks: that bound is the stored extent, whatever the field holds. */
  earliest: boolean
  latest: boolean
  timeZone: string
  /** The stored extent in epoch ms (the dataset's first and last bar), when known. */
  firstBar?: number
  lastBar?: number
}

export interface ExportPlan {
  start?: Date
  end?: Date
  /** The range's extent in epoch ms, for progress; unset when the stored extent is unknown. */
  spanStart?: number
  spanEnd?: number
  /** The bounds as file-safe days (YYYY-MM-DD), for the file name; '' when unknown. */
  fromTag: string
  toTag: string
}

export type ExportPlanResult = { ok: true; plan: ExportPlan } | { ok: false; message: string }

/** A day as the app's file-safe form: YYYY-MM-DD, never a slash. */
function fileDay(ms: number | undefined, timeZone: string): string {
  return ms === undefined ? '' : formatDate(ms, timeZone).replaceAll('/', '-')
}

export function planExport(input: ExportRangeInput): ExportPlanResult {
  const { timeZone } = input
  let from: string | null = null
  let to: string | null = null
  if (!input.earliest && input.fromText.trim() !== '') {
    from = parseDay(input.fromText)
    if (from === null) return { ok: false, message: 'From must be a date as YYYY/MM/DD.' }
  }
  if (!input.latest && input.toText.trim() !== '') {
    to = parseDay(input.toText)
    if (to === null) return { ok: false, message: 'To must be a date as YYYY/MM/DD.' }
  }
  const problem = rangeProblem({ from: from ?? undefined, to: to ?? undefined })
  if (problem !== undefined) return { ok: false, message: problem }

  const start = from === null ? undefined : zonedDayStart(from, timeZone)
  const end = to === null ? undefined : zonedDayStart(to, timeZone)
  return {
    ok: true,
    plan: {
      start,
      end,
      spanStart: start?.getTime() ?? input.firstBar,
      spanEnd: end?.getTime() ?? input.lastBar,
      fromTag: from ?? fileDay(input.firstBar, timeZone),
      toTag: to ?? fileDay(input.lastBar, timeZone),
    },
  }
}

/** SYMBOL_granularity_from_to.csv, e.g. VFV_1d_2020-01-02_2026-10-12.csv. */
export function exportFileName(symbol: string, granularity: string, plan: ExportPlan): string {
  const parts = [symbol, granularity, plan.fromTag, plan.toTag].filter((part) => part !== '')
  return `${parts.join('_')}.csv`
}
