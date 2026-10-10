// Per-row mappings for the catalog grid (tj-grna9p.32).
import { FreshnessStatus } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { freshnessBadge } from '@/theme/semantics'
import type { BadgeSpec } from '@/theme/semantics'

/**
 * The row's Status badge. A dataset with no health (freshness absent, or its status unspecified:
 * a source without a calendar, or before 1990) is Unknown, never Healthy; the badge map owns that.
 */
export function rowBadge(row: DatasetSummary): BadgeSpec {
  const status = row.freshness?.status
  if (status === undefined || status === FreshnessStatus.UNSPECIFIED) return freshnessBadge(null)
  return freshnessBadge(`FRESHNESS_STATUS_${FreshnessStatus[status]}`)
}

/** The Viewer route for a dataset (the Viewer is tj-grna9p.33). */
export const VIEWER_ROUTE = 'data-dataset-viewer'
