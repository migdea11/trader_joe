// Live refresh for the Dataset detail (tj-grna9p.34): an event is a hint to refetch over REST.
//
// IngestRunChanged for THIS dataset (run.datasetId equals the screen's id) puts the run's row in
// place at once, from the event itself. The overview and the coverage strip are refetched once per
// burst: events from one run arrive together, so the first opens a short window and the refetch runs
// when it closes (a setTimeout; setInterval is banned, nothing here polls). DatasetChanged for this
// id refetches the same way, and a DELETED one marks the dataset deleted instead. Events for any
// other dataset do nothing. A reconnect (the shell's hook) runs the mount's own fetches again.
import { DatasetChange } from '@generated/trader_joe/proto/ui/v1/events_pb'
import { onScopeDispose } from 'vue'
import type { Ref } from 'vue'

import { subscribeEvent } from '@/api'
import { COALESCE_MS } from '@/catalog/useCatalogLiveRefresh'
import { useEventsReload } from '@/shell/useEventsReload'
import { useDatasetCoverageStore } from '@/stores/datasetCoverage'
import { useDatasetDetailStore } from '@/stores/datasetDetail'

/**
 * Coverage strip buckets on this screen: the most blocks the strip draws (one block per bucket).
 *
 * The strip is a skim; the gap list below it stays exact. Few blocks stay wide enough to read and to
 * hover. The server returns min(buckets, sessions) near-equal groups, so a small dataset gets one block
 * per session and a large one about sessions / 20 each. The Datasets grid asks for its own 60.
 */
export const DETAIL_COVERAGE_BUCKETS = 20

export function useDatasetDetailLive(datasetId: Ref<string>): void {
  const detail = useDatasetDetailStore()
  const coverage = useDatasetCoverageStore()
  let timer: ReturnType<typeof setTimeout> | undefined

  function flush(): void {
    timer = undefined
    if (detail.deleted) return
    coverage.invalidate(datasetId.value)
    void detail.refresh()
  }

  function schedule(): void {
    timer ??= setTimeout(flush, COALESCE_MS)
  }

  const unsubscribe = [
    subscribeEvent('ingestRunChanged', (message) => {
      const run = message.run
      if (run === undefined || run.datasetId !== datasetId.value) return
      detail.upsertRun(run)
      schedule()
    }),
    subscribeEvent('datasetChanged', (message) => {
      if (message.id !== datasetId.value) return
      if (message.change === DatasetChange.DELETED) detail.markDeleted()
      else schedule()
    }),
  ]

  useEventsReload(() => {
    coverage.invalidate(datasetId.value)
    return detail.reload()
  })

  onScopeDispose(() => {
    for (const off of unsubscribe) off()
    if (timer !== undefined) clearTimeout(timer)
    timer = undefined
  })
}
