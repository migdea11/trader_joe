// Live refresh for the Datasets screen (tj-grna9p.77): an event is a hint to refetch over REST.
//
// DatasetChanged and IngestRunChanged arrive in bursts (a run writes several state changes, a request
// creates several datasets), so events are COALESCED: the first one opens a short window, the ones
// inside it only note which dataset changed, and when it closes the screen refetches ONCE: the
// counts, the rows the grid holds, and the coverage strips of the datasets that changed. The window
// closes on a fixed time, so a steady trickle of events cannot postpone the refetch for ever, and it
// is a setTimeout (setInterval is banned: nothing here polls).
//
// A reconnect (the events client's `reload`, via the shell's hook) refetches everything the same way
// the mount did, since events may have been missed while the socket was down.
import { onScopeDispose } from 'vue'
import type { Ref } from 'vue'

import { subscribeEvent } from '@/api'
import { useEventsReload } from '@/shell/useEventsReload'
import { useDatasetCatalogStore } from '@/stores/datasetCatalog'
import { useDatasetCoverageStore } from '@/stores/datasetCoverage'

import type { CatalogFilters } from './filters'

/** How long a burst of events is gathered before the one refetch, in ms. */
export const COALESCE_MS = 500

export function useCatalogLiveRefresh(filters: Ref<CatalogFilters>): void {
  const catalog = useDatasetCatalogStore()
  const coverage = useDatasetCoverageStore()
  const changed = new Set<string>()
  let timer: ReturnType<typeof setTimeout> | undefined

  function flush(): void {
    timer = undefined
    const ids = [...changed]
    changed.clear()
    for (const id of ids) coverage.invalidate(id)
    catalog.reloadLive(filters.value)
  }

  function note(datasetId: string | undefined): void {
    // A run's dataset id is empty until the run's acknowledgement; the rows still reload.
    if (datasetId !== undefined && datasetId !== '') changed.add(datasetId)
    timer ??= setTimeout(flush, COALESCE_MS)
  }

  const unsubscribe = [
    subscribeEvent('datasetChanged', (message) => note(message.id)),
    subscribeEvent('ingestRunChanged', (message) => note(message.run?.datasetId)),
  ]

  // Reconnect: the same fetches the mount ran (counts and rows), and every coverage strip over again.
  useEventsReload(() => {
    coverage.invalidateAll()
    catalog.reloadLive(filters.value)
  })

  onScopeDispose(() => {
    for (const off of unsubscribe) off()
    if (timer !== undefined) clearTimeout(timer)
    timer = undefined
  })
}
