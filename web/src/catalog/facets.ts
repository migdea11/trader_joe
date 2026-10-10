// The sidebar and tile counts, from GET /ui/v1/datasets/facets (tj-grna9p.32). Counts are over the
// datasets matching the OTHER active filters; the server returns zero counts too, and an option
// with none is still listed. int64 counts decode as bigint and become numbers here.
import { DataSource, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { FreshnessStatus } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { DatasetFacets } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { int64ToNumber } from '@/api'
import type { StatusGroup } from '@/api'

export interface SourceCount {
  source: DataSource
  count: number
}

export interface FacetCounts {
  /** The All Datasets view. */
  all: number
  /** The Needs Attention view (late, overdue or gaps). */
  needsAttention: number
  /** Per Status filter value. A dataset with no health is in none of them. */
  status: Record<StatusGroup, number>
  sources: SourceCount[]
  /** Per update type; a type the server did not list is zero. */
  updateTypes: Record<UpdateType, number>
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
    status,
    sources: facets.sources
      .filter((entry) => entry.source !== DataSource.UNSPECIFIED)
      .map((entry) => ({ source: entry.source, count: int64ToNumber(entry.count) })),
    updateTypes,
  }
}
