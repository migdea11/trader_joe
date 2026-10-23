// What the two places that set an expiry share (the Dataset detail's Retention card and the Usage
// tab's Set Expiry dialog): the day the operator types and the words a refused write is shown in. The
// server decides what a write does; this only words what came back.
import { formatDate } from '@/format/formatters'
import type { WriteOutcome } from '@/stores/datasetDetail'
import { displayDay } from '@/viewer/range'

/** Tomorrow's calendar day in `timeZone`, as the field spells it (YYYY/MM/DD): day arithmetic on the date, never on hours. */
export function tomorrowText(timeZone: string, now: number = Date.now()): string {
  const [y, m, d] = formatDate(now, timeZone).split('/').map(Number)
  const next = new Date(Date.UTC(y ?? 1970, (m ?? 1) - 1, (d ?? 1) + 1))
  return displayDay(next.toISOString().slice(0, 10))
}

export interface WriteNotice {
  text: string
  /** The server's error id, for the refusals that carry one. */
  errorId?: string
}

/**
 * The words a refused write is shown in; null for an outcome that is not a refusal shown in place
 * (ok, a dropped late answer, and 404, which the screen handles as news that the dataset is gone).
 */
export function refusalNotice(outcome: WriteOutcome): WriteNotice | null {
  switch (outcome.kind) {
    case 'override-required':
      return { text: 'This dataset is held by another owner. Give a reason and confirm to continue.' }
    case 'unavailable':
      return {
        text: 'The database is not ready for this change, so nothing was changed. Run make migrate, then try again.',
        errorId: outcome.error.errorId,
      }
    case 'invalid':
      return {
        text: 'The server refused the request as invalid. This is a bug in the UI; nothing was changed.',
        errorId: outcome.error.errorId,
      }
    case 'error':
      return {
        text: `Could not complete the change. ${outcome.error.message} Nothing was changed.`,
        errorId: outcome.error.errorId,
      }
    default:
      return null
  }
}
