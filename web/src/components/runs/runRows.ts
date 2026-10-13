// What a collection run reads as in the Dataset detail's History table (tj-grna9p.34): the words for
// its trigger and state, its range, its duration and its typed error. The server decides all of it
// (state, trigger, error); this only turns the enum and the error vocabulary into words.
import { IngestRunState, RunTrigger } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { IngestRun, IngestRunError } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { MISSING, timestampToInstant } from '@/format/formatters'
import { RUNNING_BADGE, REQUEST_BADGES, STALE_BADGE } from '@/theme/semantics'
import type { BadgeSpec } from '@/theme/semantics'

const TRIGGER_LABELS: Partial<Record<RunTrigger, string>> = {
  [RunTrigger.REQUEST]: 'Request',
  [RunTrigger.SCHEDULE]: 'Schedule',
  [RunTrigger.RETRY]: 'Retry',
}

export function triggerLabel(trigger: RunTrigger): string {
  return TRIGGER_LABELS[trigger] ?? MISSING
}

// Running is its own state. Empty (the vendor held no bars) is not a failure and not a success with
// data, so it takes the neutral colour; its word carries the meaning.
const STATE_BADGES: Partial<Record<IngestRunState, BadgeSpec>> = {
  [IngestRunState.RUNNING]: RUNNING_BADGE,
  [IngestRunState.SUCCEEDED]: { label: 'Succeeded', color: REQUEST_BADGES.done.color },
  [IngestRunState.EMPTY]: { label: 'Empty', color: STALE_BADGE.color },
  [IngestRunState.FAILED]: REQUEST_BADGES.failed,
}

const UNKNOWN_STATE: BadgeSpec = { label: 'Unknown', color: STALE_BADGE.color }

export function runStateBadge(state: IngestRunState): BadgeSpec {
  return STATE_BADGES[state] ?? UNKNOWN_STATE
}

/** "RATE_LIMITED" as "Rate limited": the server's reason vocabulary in a sentence's first words. */
export function humanizeToken(token: string): string {
  const words = token.trim().toLowerCase().replace(/[_\-.]+/g, ' ').trim()
  return words === '' ? '' : words.charAt(0).toUpperCase() + words.slice(1)
}

/** The typed error in words: the reason, then the domain it came from, then the server's message. */
export function describeRunError(error: IngestRunError | undefined): { headline: string; message: string } | null {
  if (error === undefined) return null
  const reason = humanizeToken(error.reason)
  const domain = humanizeToken(error.domain)
  const headline =
    reason !== '' && domain !== '' ? `${reason} (${domain})` : reason !== '' ? reason : domain !== '' ? domain : 'Failed'
  return { headline, message: error.message }
}

/** The run's wall time in ms, or null while it is running (no finish) or when a time is missing. */
export function runDurationMs(run: IngestRun): number | null {
  if (run.startedAt === undefined || run.finishedAt === undefined) return null
  const ms = timestampToInstant(run.finishedAt) - timestampToInstant(run.startedAt)
  return ms >= 0 ? ms : null
}
