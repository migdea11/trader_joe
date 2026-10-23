// How a dataset's reads are worded (tj-grna9p.88): the Usage column, the Read By choice and the
// threshold labels. The server decides what is stale and counts the reads; this maps them to words.
import { ClientKind } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { STALE_DAYS } from '@/api'
import { daysSinceUtc } from '@/format/days'
import { formatDays, formatDaysAgo, timestampToInstant } from '@/format/formatters'
import { titleCase } from '@/theme/copy'

import { effectiveClientKinds } from './filters'
import type { CatalogFilters, UsageGroup } from './filters'

const KIND_LABELS: Record<ClientKind, string> = {
  [ClientKind.UNSPECIFIED]: 'Unknown',
  [ClientKind.UI]: 'UI',
  [ClientKind.SDK]: 'SDK',
}

export function clientKindLabel(kind: ClientKind): string {
  return KIND_LABELS[kind]
}

/** "SDK", "UI" or "SDK and UI": whose reads the usage figures count. */
export function readByLabel(filters: Pick<CatalogFilters, 'clientKinds'>): string {
  return effectiveClientKinds(filters).map(clientKindLabel).join(' and ')
}

/**
 * The threshold, as the Stale view, option and tile name it: "Stale: 90 d". `days` is the server's
 * echo (facets.staleDays); the unit is never title-cased.
 */
export function staleLabel(days: number = STALE_DAYS): string {
  return `${titleCase('Stale')}: ${formatDays(days)}`
}

/** The label before any response has echoed the threshold. */
export const STALE_LABEL = staleLabel()

/** "unread by SDK 90+ days": the Stale tile's note and the Usage group's Stale option. */
export function staleNote(filters: Pick<CatalogFilters, 'clientKinds'>, days: number = STALE_DAYS): string {
  return `unread by ${readByLabel(filters)} ${days}+ days`
}

/** The Usage group's option text (sentence case), with the Read By choice in the stale one. */
export function usageOptionLabel(
  group: UsageGroup,
  filters: Pick<CatalogFilters, 'clientKinds'>,
  days: number = STALE_DAYS,
): string {
  if (group === 'read-30d') return 'Read in the last 30 days'
  if (group === 'never-read') return `Never read by ${readByLabel(filters)}`
  return `Stale: ${staleNote(filters, days)}`
}

export type UsageCellState = { kind: 'never' } | { kind: 'age'; days: number }

/**
 * One row's Usage cell: never read by the selected clients, or the age of the last read in whole
 * UTC calendar days. Whether that is stale is the server's call (the Stale view, group and tile).
 */
export function usageCell(row: DatasetSummary, now: number = Date.now()): UsageCellState {
  const lastRead = row.usage?.lastReadAt
  if (lastRead === undefined) return { kind: 'never' }
  return { kind: 'age', days: daysSinceUtc(timestampToInstant(lastRead), now) }
}

/** A cell's words: "Never read", "today" or "3 d ago". Never "Stale": that is the server's call. */
export function usageCellText(cell: UsageCellState): string {
  if (cell.kind === 'never') return 'Never read'
  return cell.days === 0 ? 'today' : formatDaysAgo(cell.days, 'short')
}

/**
 * Read in the last 30 days by the UI alone while the filter counts only other kinds ("UI only"):
 * the dataset looks unused to the filter, but the operator has been looking at it.
 */
export function isUiOnly(row: DatasetSummary, filters: Pick<CatalogFilters, 'clientKinds'>): boolean {
  const kinds = row.usage?.readBy30d ?? []
  if (kinds.length === 0 || !kinds.every((kind) => kind === ClientKind.UI)) return false
  return !effectiveClientKinds(filters).includes(ClientKind.UI)
}
