// Per-row mappings for the catalog grid (tj-grna9p.32, collection overlay tj-grna9p.77).
import { DatasetHealthStatus, IngestRunState } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { collectionBadge } from '@/theme/semantics'
import type { BadgeSpec } from '@/theme/semantics'

/** The dataset's latest collection run is in flight (the server's last_run, whatever the health). */
export function isRunning(row: DatasetSummary): boolean {
  return row.lastRun?.state === IngestRunState.RUNNING
}

/**
 * The row's Status badge: Running while the latest run is in flight, otherwise the health
 * word. A dataset with no health (health absent, or its status unspecified: a source without a
 * calendar, or before 1990) is Unknown, never Healthy; the badge map owns that.
 */
export function rowBadge(row: DatasetSummary): BadgeSpec {
  const status = row.health?.status
  const name =
    status === undefined || status === DatasetHealthStatus.UNSPECIFIED ? null : `DATASET_HEALTH_STATUS_${DatasetHealthStatus[status]}`
  return collectionBadge(name, { running: isRunning(row) })
}

/** The Viewer route for a dataset (the Viewer is tj-grna9p.33): the default destination of a row and its Symbol link. */
export const VIEWER_ROUTE = 'data-dataset-viewer'

/** The Dataset detail route (tj-grna9p.34 builds the screen; the grid's details icon opens it, and the Viewer's Details link). */
export const DETAIL_ROUTE = 'data-dataset-detail'
