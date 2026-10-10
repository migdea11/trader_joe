// The Datasets screen's shared state (tj-grna9p.32): the facet counts the sidebar and the tiles both
// read, and the grid's load state the footer and the empty and error panels read. The filters
// themselves live in the URL (catalog/filters.ts), not here. Nothing refetches by itself: a filter
// change or the Refresh button does (no polling, no events in phase 1).
import { ref } from 'vue'
import { defineStore } from 'pinia'

import { ApiError, getDatasetFacets } from '@/api'
import { facetCounts } from '@/catalog/facets'
import type { FacetCounts } from '@/catalog/facets'
import { toApiFilters } from '@/catalog/filters'
import type { CatalogFilters } from '@/catalog/filters'

export type CatalogLoadStatus = 'loading' | 'ready' | 'empty' | 'error'

/** What the UI may say about a failed call: the typed message and the id to quote, never body text. */
export interface CatalogError {
  message: string
  errorId: string | undefined
}

export function describeError(error: unknown): CatalogError {
  if (error instanceof ApiError) return { message: error.message, errorId: error.errorId }
  return { message: 'Request failed', errorId: undefined }
}

export const useDatasetCatalogStore = defineStore('datasetCatalog', () => {
  const facets = ref<FacetCounts | null>(null)
  const facetsError = ref<CatalogError | null>(null)
  /** The tiles' counts: always the whole catalog, so a filter does not change them. */
  const tiles = ref<FacetCounts | null>(null)
  const tilesError = ref<CatalogError | null>(null)
  /** Bumped by refresh(): the grid makes a new datasource (cursors reset) when it changes. */
  const refreshKey = ref(0)

  const listStatus = ref<CatalogLoadStatus>('loading')
  const listError = ref<CatalogError | null>(null)
  const loadedRows = ref(0)
  /** The last page was reached, so loadedRows is the true count. */
  const listDone = ref(false)

  let inflight: AbortController | null = null
  let tilesInflight: AbortController | null = null

  /** Fetch the counts under the filters. A call still in flight is cancelled. */
  async function loadFacets(filters: CatalogFilters): Promise<void> {
    inflight?.abort()
    const controller = new AbortController()
    inflight = controller
    try {
      const result = await getDatasetFacets({ ...toApiFilters(filters), signal: controller.signal })
      if (controller.signal.aborted) return
      facets.value = facetCounts(result)
      facetsError.value = null
    } catch (error) {
      if (controller.signal.aborted) return
      // The previous counts stay on screen next to the error rather than being blanked.
      facetsError.value = describeError(error)
    }
  }

  /** Fetch the tile counts (no filters). A call still in flight is cancelled. */
  async function loadTiles(): Promise<void> {
    tilesInflight?.abort()
    const controller = new AbortController()
    tilesInflight = controller
    try {
      const result = await getDatasetFacets({ signal: controller.signal })
      if (controller.signal.aborted) return
      tiles.value = facetCounts(result)
      tilesError.value = null
    } catch (error) {
      if (controller.signal.aborted) return
      tilesError.value = describeError(error)
    }
  }

  /** The grid starts a new load (first mount, a filter change, a refresh). */
  function listLoading(): void {
    listStatus.value = 'loading'
    listError.value = null
    loadedRows.value = 0
    listDone.value = false
  }

  function listPage(info: { loadedRows: number; done: boolean }): void {
    loadedRows.value = info.loadedRows
    listDone.value = info.done
    listStatus.value = info.loadedRows === 0 ? 'empty' : 'ready'
    listError.value = null
  }

  function listFailed(error: unknown, info: { loadedRows: number }): void {
    loadedRows.value = info.loadedRows
    listStatus.value = 'error'
    listError.value = describeError(error)
  }

  /** Reload the list from its first page and the counts. The caller passes the current filters. */
  function refresh(filters: CatalogFilters): void {
    refreshKey.value += 1
    void loadFacets(filters)
    void loadTiles()
  }

  return {
    facets,
    facetsError,
    tiles,
    tilesError,
    refreshKey,
    listStatus,
    listError,
    loadedRows,
    listDone,
    loadFacets,
    loadTiles,
    listLoading,
    listPage,
    listFailed,
    refresh,
  }
})
