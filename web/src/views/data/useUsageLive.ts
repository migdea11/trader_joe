// Live refresh for the Usage tab (tj-grna9p.62): an event is a hint to refetch over REST.
//
// DatasetChanged events arrive in bursts, so they are coalesced like the Datasets screen's: the first
// opens a short window and the screen refetches ONCE when it closes (a setTimeout; setInterval is
// banned, nothing here polls). A reconnect (the shell's hook) refetches everything the mount did,
// since events may have been missed while the socket was down.
import { onScopeDispose } from 'vue'

import { subscribeEvent } from '@/api'
import { COALESCE_MS } from '@/catalog/useCatalogLiveRefresh'
import { useEventsReload } from '@/shell/useEventsReload'
import { useUsageStore } from '@/stores/usage'

export function useUsageLive(): void {
  const store = useUsageStore()
  let timer: ReturnType<typeof setTimeout> | undefined

  function flush(): void {
    timer = undefined
    void store.reloadLive()
  }

  const unsubscribe = subscribeEvent('datasetChanged', () => {
    timer ??= setTimeout(flush, COALESCE_MS)
  })

  useEventsReload(() => store.reloadLive())

  onScopeDispose(() => {
    unsubscribe()
    if (timer !== undefined) clearTimeout(timer)
    timer = undefined
  })
}
