// The Usage tab's state (tj-grna9p.62): the selection (Read By kinds, picked clients, Source), what
// the five stats routes answered under it, the client picker's facets, and the Stale Datasets page
// with its Set Expiry write. The selection lives here because the sidebar and the page are separate
// router views that both read it.
//
// The server decides everything shown: which datasets are stale, every count, every size. The page
// sends the Read By choice always (client ids when clients are picked, they win), the fixed 90 day
// threshold as stale_days, and `source` ONLY when the operator picked one. Storage takes no client
// parameters. Nothing polls: a load happens on mount, on a selection change, on Refresh, on a
// dataset event and once per reconnect (views/data/useUsageLive.ts).
import { computed, ref, shallowRef } from 'vue'
import { clone } from '@bufbuild/protobuf'
import { defineStore } from 'pinia'
import { DataSource } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { DatasetSummarySchema } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { ClientCount, ClientKind } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type {
  LastReadBuckets,
  ReadsPerDay,
  StaleDatasetRow,
  StorageCurrent,
  UsageSummary,
} from '@generated/trader_joe/proto/ui/v1/stats_pb'

import {
  DEFAULT_CLIENT_KINDS,
  OPERATOR_CLIENT_ID,
  STALE_DAYS,
  getDatasetFacets,
  getLastReadBuckets,
  getReadsPerDay,
  getStaleDatasets,
  getStorageCurrent,
  getUsageSummary,
  setDatasetExpiry,
} from '@/api'
import type { ClientSelection, OperatorOverride } from '@/api'
import { facetCounts } from '@/catalog/facets'
import type { SourceCount } from '@/catalog/facets'
import { effectiveClientKinds } from '@/catalog/filters'
import { granularityLabel } from '@/catalog/labels'
import { describeError } from '@/stores/datasetCatalog'
import type { CatalogError } from '@/stores/datasetCatalog'
import { READS_WINDOW_DAYS, writeOutcome } from '@/stores/datasetDetail'
import type { WriteOutcome } from '@/stores/datasetDetail'

/** The stale list's page size (the server's default). */
export const STALE_PAGE_SIZE = 100
/** The most the server returns in one page; a refetch that keeps the loaded rows asks for up to this. */
const STALE_PAGE_MAX = 500

export type StaleLoadStatus = 'idle' | 'loading' | 'ready' | 'error'

/** The sections that load on their own, so each fails on its own. */
export type UsageSection = 'facets' | 'summary' | 'buckets' | 'reads' | 'storage' | 'stale'

export const useUsageStore = defineStore('usage', () => {
  // --- the selection ---
  const clientKinds = ref<readonly ClientKind[]>([...DEFAULT_CLIENT_KINDS])
  const clientIds = ref<readonly string[]>([])
  const source = ref<DataSource | undefined>(undefined)

  // --- the picker's facets (unfiltered: they ignore the client filter by design) ---
  const clients = shallowRef<readonly ClientCount[]>([])
  const sources = shallowRef<readonly SourceCount[]>([])

  // --- the figures ---
  const summary = shallowRef<UsageSummary | null>(null)
  const buckets = shallowRef<LastReadBuckets | null>(null)
  const reads = shallowRef<ReadsPerDay | null>(null)
  const storage = shallowRef<StorageCurrent | null>(null)
  const errors = ref<Partial<Record<UsageSection, CatalogError>>>({})

  // --- the Stale Datasets page ---
  const rows = shallowRef<readonly StaleDatasetRow[]>([])
  const cursor = ref('')
  const staleStatus = ref<StaleLoadStatus>('idle')
  const staleDays = ref<number | undefined>(undefined)
  const loadingMore = ref(false)
  const moreError = ref<CatalogError | null>(null)
  /** The dataset a Set Expiry is in flight for (one at a time). */
  const writingId = ref<string | null>(null)
  /** Said once a write found its dataset gone (404): the row was dropped. */
  const droppedNotice = ref<string | null>(null)
  /** Bumped when a figure was refetched, so the footer's "Updated" ticks. */
  const loadedTick = ref(0)

  const hasMore = computed(() => cursor.value !== '')
  const clientsPicked = computed(() => clientIds.value.length > 0)

  /** Bumped by every selection change and reset: a write answer from an earlier one is dropped. */
  let generation = 0
  const inflight: Partial<Record<UsageSection | 'more', AbortController>> = {}

  function selection(): ClientSelection {
    return clientIds.value.length > 0
      ? { clientIds: [...clientIds.value] }
      : { clientKinds: effectiveClientKinds({ clientKinds: [...clientKinds.value] }) }
  }

  function begin(section: UsageSection | 'more'): AbortController {
    inflight[section]?.abort()
    const controller = new AbortController()
    inflight[section] = controller
    return controller
  }

  function fail(section: UsageSection, caught: unknown): void {
    errors.value = { ...errors.value, [section]: describeError(caught) }
  }

  function ok(section: UsageSection): void {
    if (errors.value[section] === undefined) return
    const next = { ...errors.value }
    delete next[section]
    errors.value = next
  }

  async function fetchFacets(): Promise<void> {
    const controller = begin('facets')
    try {
      // Whole catalog, no filter: the picker lists every client seen and every source held.
      const result = await getDatasetFacets({ signal: controller.signal })
      if (controller.signal.aborted) return
      clients.value = result.clients
      sources.value = facetCounts(result).sources
      ok('facets')
    } catch (caught) {
      if (controller.signal.aborted) return
      fail('facets', caught)
    }
  }

  async function fetchSummary(): Promise<void> {
    const controller = begin('summary')
    try {
      const result = await getUsageSummary({
        ...selection(),
        staleDays: STALE_DAYS,
        source: source.value,
        signal: controller.signal,
      })
      if (controller.signal.aborted) return
      summary.value = result
      ok('summary')
    } catch (caught) {
      if (controller.signal.aborted) return
      fail('summary', caught)
    }
  }

  async function fetchBuckets(): Promise<void> {
    const controller = begin('buckets')
    try {
      const result = await getLastReadBuckets({ ...selection(), source: source.value, signal: controller.signal })
      if (controller.signal.aborted) return
      buckets.value = result
      ok('buckets')
    } catch (caught) {
      if (controller.signal.aborted) return
      fail('buckets', caught)
    }
  }

  async function fetchReads(): Promise<void> {
    const controller = begin('reads')
    try {
      // Never dataset_id here: this is every dataset's reads.
      const result = await getReadsPerDay({
        ...selection(),
        days: READS_WINDOW_DAYS,
        source: source.value,
        signal: controller.signal,
      })
      if (controller.signal.aborted) return
      reads.value = result
      ok('reads')
    } catch (caught) {
      if (controller.signal.aborted) return
      fail('reads', caught)
    }
  }

  async function fetchStorage(): Promise<void> {
    const controller = begin('storage')
    try {
      // No client parameters: storage is not a usage figure (the route refuses them).
      const result = await getStorageCurrent({ source: source.value, signal: controller.signal })
      if (controller.signal.aborted) return
      storage.value = result
      ok('storage')
    } catch (caught) {
      if (controller.signal.aborted) return
      fail('storage', caught)
    }
  }

  /**
   * The stale page's first page. `keepLoaded` asks for as many rows as are held (up to the server's
   * maximum) so a refetch after an event or a write does not drop pages the operator already loaded.
   */
  async function fetchStale(keepLoaded: boolean): Promise<void> {
    const controller = begin('stale')
    inflight.more?.abort()
    loadingMore.value = false
    const limit = keepLoaded ? Math.min(STALE_PAGE_MAX, Math.max(STALE_PAGE_SIZE, rows.value.length)) : STALE_PAGE_SIZE
    if (!keepLoaded) staleStatus.value = 'loading'
    try {
      const page = await getStaleDatasets({
        ...selection(),
        staleDays: STALE_DAYS,
        source: source.value,
        limit,
        signal: controller.signal,
      })
      if (controller.signal.aborted) return
      rows.value = page.items
      cursor.value = page.nextCursor
      staleDays.value = page.staleDays > 0 ? page.staleDays : undefined
      staleStatus.value = 'ready'
      moreError.value = null
      ok('stale')
    } catch (caught) {
      if (controller.signal.aborted) return
      // A soft refetch leaves the rows on screen next to the error.
      if (!keepLoaded || staleStatus.value !== 'ready') staleStatus.value = 'error'
      fail('stale', caught)
    }
  }

  function touched(): void {
    loadedTick.value += 1
  }

  /** Mount, Try Again and Refresh: everything is asked for again; what is on screen stays until it is replaced. */
  async function load(): Promise<void> {
    await Promise.all([fetchFacets(), fetchSummary(), fetchBuckets(), fetchReads(), fetchStorage(), fetchStale(false)])
    touched()
  }

  /** A dataset event or a reconnect: the same fetches, softly, keeping the pages already loaded. */
  async function reloadLive(): Promise<void> {
    await Promise.all([fetchFacets(), fetchSummary(), fetchBuckets(), fetchReads(), fetchStorage(), fetchStale(true)])
    touched()
  }

  /** What depends on whose reads count: everything but the storage and the picker. */
  async function selectionChanged(): Promise<void> {
    generation += 1
    droppedNotice.value = null
    await Promise.all([fetchSummary(), fetchBuckets(), fetchReads(), fetchStale(false)])
    touched()
  }

  async function setClientKinds(kinds: readonly ClientKind[]): Promise<void> {
    clientKinds.value = kinds
    await selectionChanged()
  }

  async function setClientIds(ids: readonly string[]): Promise<void> {
    clientIds.value = ids
    await selectionChanged()
  }

  /** Source is a filter on every route, storage included. */
  async function setSource(value: DataSource | undefined): Promise<void> {
    source.value = value
    generation += 1
    droppedNotice.value = null
    await Promise.all([fetchSummary(), fetchBuckets(), fetchReads(), fetchStorage(), fetchStale(false)])
    touched()
  }

  async function clearFilters(): Promise<void> {
    clientKinds.value = [...DEFAULT_CLIENT_KINDS]
    clientIds.value = []
    source.value = undefined
    generation += 1
    droppedNotice.value = null
    await Promise.all([fetchSummary(), fetchBuckets(), fetchReads(), fetchStorage(), fetchStale(false)])
    touched()
  }

  /** Load More: the next page of stale datasets, appended. */
  async function loadMoreStale(): Promise<void> {
    if (cursor.value === '' || loadingMore.value) return
    const controller = begin('more')
    loadingMore.value = true
    moreError.value = null
    try {
      const page = await getStaleDatasets({
        ...selection(),
        staleDays: STALE_DAYS,
        source: source.value,
        cursor: cursor.value,
        limit: STALE_PAGE_SIZE,
        signal: controller.signal,
      })
      if (controller.signal.aborted) return
      const held = new Set(rows.value.map((row) => row.dataset?.id))
      rows.value = [...rows.value, ...page.items.filter((row) => !held.has(row.dataset?.id))]
      cursor.value = page.nextCursor
    } catch (caught) {
      if (controller.signal.aborted) return
      moreError.value = describeError(caught)
    } finally {
      if (inflight.more === controller) {
        loadingMore.value = false
        delete inflight.more
      }
    }
  }

  function nameOf(row: StaleDatasetRow): string {
    const dataset = row.dataset
    return dataset === undefined ? '' : `${dataset.assetSymbol} ${granularityLabel(dataset.granularity)}`
  }

  /**
   * Set the expiry of a stale row's dataset. `override` is sent only for another owner's dataset and
   * never for the operator's own. On 200 the row takes the answer's retention facts (its usage stays:
   * the answer counts the default clients, not this selection) and the page is refetched, because
   * whether the dataset still belongs in the list is the server's call. A 404 drops the row; a 409
   * refetches the page so the row's current owner is shown before the reason is asked for.
   */
  async function setExpiry(id: string, expiry: Date, override?: OperatorOverride): Promise<WriteOutcome> {
    const row = rows.value.find((entry) => entry.dataset?.id === id)
    if (row?.dataset === undefined || writingId.value !== null) {
      return { kind: 'error', error: describeError(null) }
    }
    const started = generation
    writingId.value = id
    try {
      const result = await setDatasetExpiry(id, {
        expiry,
        override: row.dataset.owner === OPERATOR_CLIENT_ID ? undefined : override,
      })
      if (generation !== started) return { kind: 'stale' }
      const index = rows.value.findIndex((entry) => entry.dataset?.id === id)
      if (index < 0) return { kind: 'stale' }
      const merged = clone(DatasetSummarySchema, result)
      merged.usage = rows.value[index]?.dataset?.usage
      const next = [...rows.value]
      const current = rows.value[index]
      if (current !== undefined) next[index] = { ...current, dataset: merged }
      rows.value = next
      void fetchSummary()
      void fetchStale(true)
      return { kind: 'ok' }
    } catch (caught) {
      if (generation !== started) return { kind: 'stale' }
      const outcome = writeOutcome(caught)
      if (outcome.kind === 'not-found') {
        droppedNotice.value = `${nameOf(row)} no longer exists, so it was removed from the list.`
        rows.value = rows.value.filter((entry) => entry.dataset?.id !== id)
        void fetchSummary()
      }
      if (outcome.kind === 'override-required') {
        // The dataset changed hands after this page loaded: refetch so the row shows its current owner
        // and the dialog re-asks with the reason field (RetentionCard does the same with refresh()).
        await fetchStale(true)
        if (generation !== started) return { kind: 'stale' }
      }
      return outcome
    } finally {
      writingId.value = null
    }
  }

  function dismissDroppedNotice(): void {
    droppedNotice.value = null
  }

  function reset(): void {
    for (const controller of Object.values(inflight)) controller?.abort()
    generation += 1
    clientKinds.value = [...DEFAULT_CLIENT_KINDS]
    clientIds.value = []
    source.value = undefined
    clients.value = []
    sources.value = []
    summary.value = null
    buckets.value = null
    reads.value = null
    storage.value = null
    errors.value = {}
    rows.value = []
    cursor.value = ''
    staleStatus.value = 'idle'
    staleDays.value = undefined
    loadingMore.value = false
    moreError.value = null
    writingId.value = null
    droppedNotice.value = null
  }

  return {
    clientKinds,
    clientIds,
    source,
    clients,
    sources,
    summary,
    buckets,
    reads,
    storage,
    errors,
    rows,
    cursor,
    staleStatus,
    staleDays,
    loadingMore,
    moreError,
    writingId,
    droppedNotice,
    loadedTick,
    hasMore,
    clientsPicked,
    load,
    reloadLive,
    setClientKinds,
    setClientIds,
    setSource,
    clearFilters,
    loadMoreStale,
    setExpiry,
    dismissDroppedNotice,
    reset,
  }
})
