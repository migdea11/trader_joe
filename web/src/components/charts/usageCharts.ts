// The Usage tab's two bar charts' data (tj-grna9p.62): the server's last-read buckets and its
// current-storage shares laid out as labelled bars. Plain data, no Vue and no ECharts. The server
// counts and sizes; the bucket bounds arrive with each bucket (whole UTC days, [min, max) with the
// last one open), so nothing here recomputes which dataset belongs where.
import type { LastReadBuckets, StorageCurrent } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { int64ToNumber } from '@/api'
import { sourceFeedLabel } from '@/catalog/labels'

export interface Bar {
  label: string
  value: number
}

/** "Today" for [0, 1), "1-7 d" for [1, 8), and "91+ d" for the open last bucket. */
export function bucketLabel(minDays: number, maxDays: number | undefined): string {
  if (maxDays === undefined) return `${minDays}+ d`
  if (minDays === 0 && maxDays === 1) return 'Today'
  return maxDays - 1 === minDays ? `${minDays} d` : `${minDays}-${maxDays - 1} d`
}

/** The buckets in the order the server sent them (youngest first), then the datasets never read. */
export function lastReadBars(message: LastReadBuckets): Bar[] {
  const bars = message.buckets.map((bucket) => ({
    label: bucketLabel(bucket.minDays, bucket.maxDays),
    value: int64ToNumber(bucket.datasets),
  }))
  bars.push({ label: 'Never', value: int64ToNumber(message.neverRead) })
  return bars
}

/** One bar per (source, feed), largest first, in estimated bytes. */
export function storageBars(message: StorageCurrent): Bar[] {
  return message.bySource
    .map((entry) => ({ label: sourceFeedLabel(entry.source, entry.feed), value: int64ToNumber(entry.estBytes) }))
    .sort((a, b) => b.value - a.value || a.label.localeCompare(b.label))
}
