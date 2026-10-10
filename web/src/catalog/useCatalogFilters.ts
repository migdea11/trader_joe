// The catalog filters as seen by the sidebar and the page: read from the URL query, written back to
// it, so the filters survive a reload and a shared link (tj-grna9p.32). The URL is the one source;
// the sidebar and the page are separate router views and both read it here.
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { parseCatalogQuery, toCatalogQuery } from './filters'
import type { CatalogFilters } from './filters'

export function useCatalogFilters() {
  const route = useRoute()
  const router = useRouter()
  const filters = computed<CatalogFilters>(() => parseCatalogQuery(route.query))

  /** Replace the screen's query keys (other keys, such as the trading group, stay). */
  async function setFilters(next: CatalogFilters): Promise<void> {
    await router.replace({ query: toCatalogQuery(next, route.query) })
  }

  return { filters, setFilters }
}
