// The sidebar and tile counts, from GET /ui/v1/datasets/facets (tj-grna9p.32). Counts are over the
// datasets matching the OTHER active filters; the server returns zero counts too, and an option
// with none is still listed. int64 counts decode as bigint and become numbers here.
import { DataSource, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { FreshnessStatus } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { DatasetFacets } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { int64ToNumber } from '@/api'
import type { StatusGroup } from '@/api'
import type { UsageGroup } from '@/catalog/filters'
import { timestampToInstant } from '@/format/formatters'

export interface SourceCount {
  source: DataSource
  count: number
}

export interface FacetCounts {
  /** The All Datasets view. */
  all: number
  /** The Needs Attention view (late, overdue or gaps). */
  needsAttention: number
  /** Datasets with a collection run in flight (the Running filter value). */
  running: number
  /** Per Status filter value. A dataset with no health is in none of them. */
  status: Record<StatusGroup, number>
  sources: SourceCount[]
  /** Per update type; a type the server did not list is zero. */
  updateTypes: Record<UpdateType, number>
  /** The Stale view and the Usage group's Stale option: unread by the selected clients for the threshold. */
  stale: number
  /** The Usage group: read by the selected clients in the last 30 days. */
  read30d: number
  /** The Usage group: never read by the selected clients. */
  neverRead: number
  /** The Expiring Soon view: expiring inside the window, plus everything the reaper would delete. */
  expiringSoon: number
  /** The stale threshold the server counted under (its echo); undefined when it sent none. */
  staleDays: number | undefined
  /** The Expiring Soon bound the server counted under (its echo); undefined when it sent none. */
  expiresBefore: Date | undefined
}

// A status filter value covers these freshness states (the server's mapping, tj-grna9p.8).
const STATUS_OF: Partial<Record<FreshnessStatus, StatusGroup>> = {
  [FreshnessStatus.FRESH]: 'healthy',
  [FreshnessStatus.COMPLETE]: 'healthy',
  [FreshnessStatus.LATE]: 'late',
  [FreshnessStatus.OVERDUE]: 'failed',
  [FreshnessStatus.GAPS]: 'failed',
  [FreshnessStatus.RETIRED]: 'retired',
}

/** The count the server gives a Usage group (the sidebar option and the footer total). */
export function usageCount(facets: FacetCounts, group: UsageGroup): number {
  return { 'read-30d': facets.read30d, stale: facets.stale, 'never-read': facets.neverRead }[group]
}

export function facetCounts(facets: DatasetFacets): FacetCounts {
  const status: Record<StatusGroup, number> = { healthy: 0, late: 0, failed: 0, retired: 0 }
  for (const entry of facets.statuses) {
    const group = STATUS_OF[entry.status]
    if (group !== undefined) status[group] += int64ToNumber(entry.count)
  }
  const updateTypes: Record<UpdateType, number> = {
    [UpdateType.UNSPECIFIED]: 0,
    [UpdateType.STATIC]: 0,
    [UpdateType.DAILY]: 0,
    [UpdateType.STREAM]: 0,
  }
  for (const entry of facets.updateTypes) updateTypes[entry.updateType] += int64ToNumber(entry.count)
  return {
    all: int64ToNumber(facets.all),
    needsAttention: int64ToNumber(facets.needsAttention),
    running: int64ToNumber(facets.running),
    status,
    sources: facets.sources
      .filter((entry) => entry.source !== DataSource.UNSPECIFIED)
      .map((entry) => ({ source: entry.source, count: int64ToNumber(entry.count) })),
    updateTypes,
    stale: int64ToNumber(facets.stale),
    read30d: int64ToNumber(facets.read30d),
    neverRead: int64ToNumber(facets.neverRead),
    expiringSoon: int64ToNumber(facets.expiringSoon),
    staleDays: facets.staleDays > 0 ? facets.staleDays : undefined,
    expiresBefore:
      facets.expiresBefore === undefined ? undefined : new Date(timestampToInstant(facets.expiresBefore)),
  }
}
