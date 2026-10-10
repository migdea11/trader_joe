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

/** No view or sidebar filter is set, so the facets response is the whole catalog's. */
function isUnfiltered(filters: CatalogFilters): boolean {
  return Object.values(toApiFilters(filters)).every((value) => value === undefined)
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
      // Under no filter these ARE the whole-catalog counts the tiles show: no second request.
      if (isUnfiltered(filters)) {
        tiles.value = facets.value
        tilesError.value = null
      }
    } catch (error) {
      if (controller.signal.aborted) return
      // The previous counts stay on screen next to the error rather than being blanked.
      facetsError.value = describeError(error)
      if (isUnfiltered(filters)) tilesError.value = facetsError.value
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

  /**
   * Load the sidebar and tile counts: one request when no filter is active (the same response
   * serves both), otherwise the filtered facets and the unfiltered tiles in parallel.
   */
  function loadCounts(filters: CatalogFilters): void {
    void loadFacets(filters)
    if (!isUnfiltered(filters)) void loadTiles()
  }

  /**
   * The filters changed: refetch the counts under them. The tiles do not follow the filters, so they
   * are fetched again only if no whole-catalog counts have arrived yet (the first request was
   * superseded by this filter change before it answered).
   */
  function filtersChanged(filters: CatalogFilters): void {
    void loadFacets(filters)
    if (!isUnfiltered(filters) && tiles.value === null) void loadTiles()
  }

  /** Reload the list from its first page and the counts. The caller passes the current filters. */
  function refresh(filters: CatalogFilters): void {
    refreshKey.value += 1
    loadCounts(filters)
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
    loadCounts,
    filtersChanged,
    listLoading,
    listPage,
    listFailed,
    refresh,
  }
})
