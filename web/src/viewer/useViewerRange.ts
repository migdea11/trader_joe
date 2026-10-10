// The Viewer's range as seen by the sidebar and the screen: read from the URL query, written back to
// it, so a reload or a shared link keeps the window (tj-grna9p.33). The URL is the one source.
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { parseRangeQuery, toRangeQuery } from './range'
import type { ViewerRange } from './range'

export function useViewerRange() {
  const route = useRoute()
  const router = useRouter()
  const range = computed<ViewerRange>(() => parseRangeQuery(route.query))

  /** Replace the range keys (other keys, such as the trading group, stay). */
  async function setRange(next: ViewerRange): Promise<void> {
    await router.replace({ query: toRangeQuery(next, route.query) })
  }

  return { range, setRange }
}
