// The Dataset detail screen's state (tj-grna9p.34): one dataset (overview and retention facts) and
// its collection runs, newest first, paged by keyset cursor. The coverage strip is NOT here: it is
// the lazily fetched strip in stores/datasetCoverage.ts, which the screen acquires for its id.
//
// Nothing polls. A fetch happens on mount, on Try Again and Load More, on an event for this dataset
// (views/data/useDatasetDetailLive.ts), and on a reconnect. A refetch caused by an event is SOFT: the
// data on screen stays until the new data replaces it, and a failure leaves it with a notice.
import { computed, ref, shallowRef } from 'vue'
import { clone } from '@bufbuild/protobuf'
import { defineStore } from 'pinia'
import { DatasetSummarySchema } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { ClientKind, DatasetSummary, IngestRun } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { ReadsPerDay } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import {
  ApiError,
  DEFAULT_CLIENT_KINDS,
  OPERATOR_CLIENT_ID,
  deleteDataset,
  getDataset,
  getDatasetRuns,
  getReadsPerDay,
  setDatasetCollecting,
  setDatasetExpiry,
} from '@/api'
import type { OperatorOverride } from '@/api'
import { describeError } from '@/stores/datasetCatalog'
import type { CatalogError } from '@/stores/datasetCatalog'

/** idle (nothing asked), loading (first fetch), ready, not-found (unknown id, 404), error. */
export type DetailLoadStatus = 'idle' | 'loading' | 'ready' | 'not-found' | 'error'
export type RunsLoadStatus = 'idle' | 'loading' | 'ready' | 'error'

/** The reads-per-day window of the Usage section, in UTC days. */
export const READS_WINDOW_DAYS = 30

/**
 * What a Retention write came to. Every refusal leaves the card and the dataset as they were:
 * `override-required` is the 409 (the reason prompt re-opens), `not-found` the 404 (the dataset is
 * gone), `unavailable` the 503 (the audit tables are missing: Run make migrate), `invalid` the 422
 * (a bug), `error` anything else.
 */
export type WriteOutcome =
  | { kind: 'ok' }
  /** The page left this dataset while the write was in flight: the answer was dropped. */
  | { kind: 'stale' }
  | { kind: 'override-required' }
  | { kind: 'not-found' }
  | { kind: 'unavailable'; error: CatalogError }
  | { kind: 'invalid'; error: CatalogError }
  | { kind: 'error'; error: CatalogError }

/** A write's caught error as its outcome (the Usage tab's Set Expiry reads refusals the same way). */
export function writeOutcome(caught: unknown): WriteOutcome {
  if (caught instanceof ApiError) {
    if (caught.status === 404) return { kind: 'not-found' }
    if (caught.status === 409 && caught.reason === 'OVERRIDE_REQUIRED') return { kind: 'override-required' }
    if (caught.status === 422) return { kind: 'invalid', error: describeError(caught) }
    if (caught.status === 503 && caught.reason === 'DATABASE_UNAVAILABLE') {
      return { kind: 'unavailable', error: describeError(caught) }
    }
  }
  return { kind: 'error', error: describeError(caught) }
}

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

  /** Whose reads the Usage section counts: Read By, SDK by default. */
  const clientKinds = ref<readonly ClientKind[]>([...DEFAULT_CLIENT_KINDS])
  const reads = shallowRef<ReadsPerDay | null>(null)
  const readsError = ref<CatalogError | null>(null)
  /** A write is in flight: the Retention card's buttons wait. */
  const writing = ref(false)

  const runs = shallowRef<readonly IngestRun[]>([])
  const runsStatus = ref<RunsLoadStatus>('idle')
  const runsError = ref<CatalogError | null>(null)
  const runsCursor = ref('')
  const loadingMore = ref(false)
  const moreError = ref<CatalogError | null>(null)

  const hasMoreRuns = computed(() => runsCursor.value !== '')

  let currentId = ''
  /** Bumped by every load and reset: a write answer from an earlier one is dropped. */
  let generation = 0
  let datasetInflight: AbortController | null = null
  let runsInflight: AbortController | null = null
  let readsInflight: AbortController | null = null
  let moreInflight: AbortController | null = null

  function abortAll(): void {
    datasetInflight?.abort()
    runsInflight?.abort()
    moreInflight?.abort()
    readsInflight?.abort()
    datasetInflight = runsInflight = moreInflight = readsInflight = null
  }

  /** The first fetch for an id, and Try Again: everything on screen is dropped and asked for again. */
  async function load(id: string): Promise<void> {
    abortAll()
    generation += 1
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
    reads.value = null
    readsError.value = null
    await Promise.all([fetchDataset(id, false), fetchRuns(id, false), fetchReads(id)])
  }

  async function fetchDataset(id: string, soft: boolean): Promise<void> {
    datasetInflight?.abort()
    const controller = new AbortController()
    datasetInflight = controller
    try {
      const result = await getDataset(id, controller.signal, { clientKinds: clientKinds.value })
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

  /** This dataset's reads per day and client over the window, for the Read By choice. */
  async function fetchReads(id: string): Promise<void> {
    readsInflight?.abort()
    const controller = new AbortController()
    readsInflight = controller
    try {
      const result = await getReadsPerDay({
        days: READS_WINDOW_DAYS,
        datasetId: id,
        clientKinds: clientKinds.value,
        signal: controller.signal,
      })
      if (controller.signal.aborted) return
      reads.value = result
      readsError.value = null
    } catch (caught) {
      if (controller.signal.aborted) return
      // The chart already on screen stays next to the notice.
      readsError.value = describeError(caught)
    }
  }

  /** Read By changed: the usage figures and the chart are asked for again under the new kinds. */
  async function setClientKinds(kinds: readonly ClientKind[]): Promise<void> {
    clientKinds.value = kinds
    if (currentId === '' || status.value !== 'ready') return
    await Promise.all([fetchDataset(currentId, true), fetchReads(currentId)])
  }

  /** The answer of a write replaces the dataset; under another Read By choice the held usage stays. */
  function adopt(result: DatasetSummary): void {
    // The answer's usage is counted for the default (SDK) filter. Under another Read By choice the
    // answer's retention facts are kept and the usage held for that choice stays.
    const kinds = clientKinds.value
    const isDefault = kinds.length === DEFAULT_CLIENT_KINDS.length && kinds.every((k) => DEFAULT_CLIENT_KINDS.includes(k))
    if (isDefault || dataset.value === null) {
      dataset.value = result
    } else {
      const merged = clone(DatasetSummarySchema, result)
      merged.usage = dataset.value.usage
      dataset.value = merged
    }
  }

  /** One dataset write: the guards, the stale-answer drop and the busy flag, shared by the two PATCHes. */
  async function patchDataset(send: (id: string, override: OperatorOverride | undefined) => Promise<DatasetSummary>, override?: OperatorOverride): Promise<WriteOutcome> {
    if (currentId === '' || dataset.value === null || writing.value) return { kind: 'error', error: describeError(null) }
    const id = currentId
    const started = generation
    writing.value = true
    try {
      const result = await send(id, dataset.value.owner === OPERATOR_CLIENT_ID ? undefined : override)
      if (generation !== started || currentId !== id) return { kind: 'stale' }
      adopt(result)
      return { kind: 'ok' }
    } catch (caught) {
      return generation !== started || currentId !== id ? { kind: 'stale' } : writeOutcome(caught)
    } finally {
      writing.value = false
    }
  }

  /**
   * Set the expiry, the deletion date (clear it when `expiry` is undefined). On 200 the card re-renders
   * from the dataset the server answers with. `override` is sent only for another owner's dataset, and
   * never for the operator's own.
   */
  function setExpiry(expiry: Date | undefined, override?: OperatorOverride): Promise<WriteOutcome> {
    return patchDataset((id, held) => setDatasetExpiry(id, { expiry, override: held }), override)
  }

  /** Start or stop collecting a subscription. The override rule is the expiry write's. */
  function setCollecting(collect: boolean, override?: OperatorOverride): Promise<WriteOutcome> {
    return patchDataset((id, held) => setDatasetCollecting(id, { collect, override: held }), override)
  }

  /** Delete the dataset. On 204 the caller leaves the screen; a refusal leaves everything as it was. */
  async function deleteNow(override?: OperatorOverride): Promise<WriteOutcome> {
    if (currentId === '' || dataset.value === null || writing.value) return { kind: 'error', error: describeError(null) }
    const id = currentId
    const started = generation
    writing.value = true
    try {
      await deleteDataset(id, {
        override: dataset.value.owner === OPERATOR_CLIENT_ID ? undefined : override,
      })
      return generation !== started || currentId !== id ? { kind: 'stale' } : { kind: 'ok' }
    } catch (caught) {
      return generation !== started || currentId !== id ? { kind: 'stale' } : writeOutcome(caught)
    } finally {
      writing.value = false
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
    if (status.value === 'ready') {
      await Promise.all([fetchDataset(currentId, true), fetchRuns(currentId, true), fetchReads(currentId)])
    }
    else await load(currentId)
  }

  function reset(): void {
    abortAll()
    generation += 1
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
    reads.value = null
    readsError.value = null
    writing.value = false
    clientKinds.value = [...DEFAULT_CLIENT_KINDS]
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
    clientKinds,
    reads,
    readsError,
    writing,
    load,
    setClientKinds,
    setExpiry,
    setCollecting,
    deleteNow,
    reload,
    refresh,
    reloadRuns,
    loadMoreRuns,
    upsertRun,
    markDeleted,
    reset,
  }
})
