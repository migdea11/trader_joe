// The Dataset detail screen's state (tj-grna9p.34): one dataset (overview and retention facts) and
// its collection runs, newest first, paged by keyset cursor. The coverage strip is NOT here: it is
// the lazily fetched strip in stores/datasetCoverage.ts, which the screen acquires for its id.
//
// Nothing polls. A fetch happens on mount, on Try Again and Load More, on an event for this dataset
// (views/data/useDatasetDetailLive.ts), and on a reconnect. A refetch caused by an event is SOFT: the
// data on screen stays until the new data replaces it, and a failure leaves it with a notice.
import { computed, ref, shallowRef } from 'vue'
import { defineStore } from 'pinia'
import type { DatasetSummary, IngestRun } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { ApiError, getDataset, getDatasetRuns } from '@/api'
import { describeError } from '@/stores/datasetCatalog'
import type { CatalogError } from '@/stores/datasetCatalog'

/** idle (nothing asked), loading (first fetch), ready, not-found (unknown id, 404), error. */
export type DetailLoadStatus = 'idle' | 'loading' | 'ready' | 'not-found' | 'error'
export type RunsLoadStatus = 'idle' | 'loading' | 'ready' | 'error'

/** How many runs a page asks for. The server's default and maximum are 50 and 500. */
export const RUNS_PAGE_SIZE = 50

function startedMs(run: IngestRun): number {
  return run.startedAt === undefined ? 0 : Number(run.startedAt.seconds) * 1000 + Math.floor(run.startedAt.nanos / 1e6)
}

/** Newest first; the run id breaks a tie so the order is stable. */
function newestFirst(a: IngestRun, b: IngestRun): number {
  const delta = startedMs(b) - startedMs(a)
  if (delta !== 0) return delta
  return a.id < b.id ? 1 : a.id > b.id ? -1 : 0
}

/** `incoming` over `held`, by run id, in newest-first order. */
export function mergeRuns(held: readonly IngestRun[], incoming: readonly IngestRun[]): IngestRun[] {
  const byId = new Map<string, IngestRun>()
  for (const run of held) byId.set(run.id, run)
  for (const run of incoming) byId.set(run.id, run)
  return [...byId.values()].sort(newestFirst)
}

export const useDatasetDetailStore = defineStore('datasetDetail', () => {
  // Protobuf messages: held shallow, never made deeply reactive.
  const dataset = shallowRef<DatasetSummary | null>(null)
  const status = ref<DetailLoadStatus>('idle')
  const error = ref<CatalogError | null>(null)
  /** The dataset was deleted while the screen was open (an event, or a refetch that found it gone). */
  const deleted = ref(false)
  /** A soft refetch failed: what is on screen is the last data that arrived. */
  const refreshProblem = ref<CatalogError | null>(null)

  const runs = shallowRef<readonly IngestRun[]>([])
  const runsStatus = ref<RunsLoadStatus>('idle')
  const runsError = ref<CatalogError | null>(null)
  const runsCursor = ref('')
  const loadingMore = ref(false)
  const moreError = ref<CatalogError | null>(null)

  const hasMoreRuns = computed(() => runsCursor.value !== '')

  let currentId = ''
  let datasetInflight: AbortController | null = null
  let runsInflight: AbortController | null = null
  let moreInflight: AbortController | null = null

  function abortAll(): void {
    datasetInflight?.abort()
    runsInflight?.abort()
    moreInflight?.abort()
    datasetInflight = runsInflight = moreInflight = null
  }

  /** The first fetch for an id, and Try Again: everything on screen is dropped and asked for again. */
  async function load(id: string): Promise<void> {
    abortAll()
    currentId = id
    dataset.value = null
    error.value = null
    deleted.value = false
    refreshProblem.value = null
    runs.value = []
    runsCursor.value = ''
    runsError.value = null
    moreError.value = null
    loadingMore.value = false
    status.value = 'loading'
    runsStatus.value = 'loading'
    await Promise.all([fetchDataset(id, false), fetchRuns(id, false)])
  }

  async function fetchDataset(id: string, soft: boolean): Promise<void> {
    datasetInflight?.abort()
    const controller = new AbortController()
    datasetInflight = controller
    try {
      const result = await getDataset(id, controller.signal)
      if (controller.signal.aborted) return
      dataset.value = result
      status.value = 'ready'
      refreshProblem.value = null
    } catch (caught) {
      if (controller.signal.aborted) return
      const notFound = caught instanceof ApiError && caught.status === 404
      if (soft && dataset.value !== null) {
        // It was here and now is not: deleted. Anything else keeps the data on screen with a notice.
        if (notFound) deleted.value = true
        else refreshProblem.value = describeError(caught)
        return
      }
      error.value = describeError(caught)
      // An unknown id is its own state, not a failure to retry.
      status.value = notFound ? 'not-found' : 'error'
    }
  }

  async function fetchRuns(id: string, soft: boolean): Promise<void> {
    runsInflight?.abort()
    const controller = new AbortController()
    runsInflight = controller
    try {
      const page = await getDatasetRuns(id, { limit: RUNS_PAGE_SIZE, signal: controller.signal })
      if (controller.signal.aborted) return
      if (soft && runs.value.length > page.items.length && runsCursor.value !== '') {
        // Older pages are loaded: keep them and the cursor that continues after them.
        runs.value = mergeRuns(runs.value, page.items)
      } else {
        runs.value = mergeRuns(soft ? runs.value : [], page.items)
        runsCursor.value = page.nextCursor
      }
      runsStatus.value = 'ready'
      runsError.value = null
    } catch (caught) {
      if (controller.signal.aborted) return
      // A soft refetch that fails leaves the rows on screen; the dataset's own refetch carries the notice.
      if (soft && runsStatus.value === 'ready') {
        refreshProblem.value = describeError(caught)
        return
      }
      runsError.value = describeError(caught)
      runsStatus.value = 'error'
    }
  }

  /** The runs list alone, again from the first page (Try Again on the History section). */
  async function reloadRuns(): Promise<void> {
    if (currentId === '') return
    runs.value = []
    runsCursor.value = ''
    runsStatus.value = 'loading'
    await fetchRuns(currentId, false)
  }

  /** Load More: the next page, appended. */
  async function loadMoreRuns(): Promise<void> {
    if (currentId === '' || runsCursor.value === '' || loadingMore.value) return
    moreInflight?.abort()
    const controller = new AbortController()
    moreInflight = controller
    const id = currentId
    loadingMore.value = true
    moreError.value = null
    try {
      const page = await getDatasetRuns(id, {
        cursor: runsCursor.value,
        limit: RUNS_PAGE_SIZE,
        signal: controller.signal,
      })
      if (controller.signal.aborted) return
      runs.value = mergeRuns(runs.value, page.items)
      runsCursor.value = page.nextCursor
    } catch (caught) {
      if (controller.signal.aborted) return
      moreError.value = describeError(caught)
    } finally {
      if (moreInflight === controller) {
        loadingMore.value = false
        moreInflight = null
      }
    }
  }

  /** An IngestRunChanged for this dataset: the run's row is added or replaced in place. */
  function upsertRun(run: IngestRun): void {
    if (currentId === '' || run.datasetId !== currentId) return
    runs.value = mergeRuns(runs.value, [run])
    if (runsStatus.value !== 'ready') runsStatus.value = 'ready'
  }

  /** The dataset was deleted (a DatasetChanged with change DELETED). */
  function markDeleted(): void {
    deleted.value = true
  }

  /** Refetch the overview softly (an event, or a reconnect). */
  async function refresh(): Promise<void> {
    if (currentId === '' || status.value === 'loading') return
    await fetchDataset(currentId, true)
  }

  /** A reconnect: the same fetches the mount ran, softly, so the screen keeps what it shows. */
  async function reload(): Promise<void> {
    if (currentId === '') return
    if (status.value === 'ready') await Promise.all([fetchDataset(currentId, true), fetchRuns(currentId, true)])
    else await load(currentId)
  }

  function reset(): void {
    abortAll()
    currentId = ''
    dataset.value = null
    error.value = null
    deleted.value = false
    refreshProblem.value = null
    runs.value = []
    runsCursor.value = ''
    runsError.value = null
    moreError.value = null
    loadingMore.value = false
    status.value = 'idle'
    runsStatus.value = 'idle'
  }

  return {
    dataset,
    status,
    error,
    deleted,
    refreshProblem,
    runs,
    runsStatus,
    runsError,
    runsCursor,
    loadingMore,
    moreError,
    hasMoreRuns,
    load,
    reload,
    refresh,
    reloadRuns,
    loadMoreRuns,
    upsertRun,
    markDeleted,
    reset,
  }
})
