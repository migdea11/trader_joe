// The Data Viewer's shared state (tj-grna9p.33): the dataset detail the header, sidebar and screen
// read, and the overlay toggles the sidebar sets and the chart reads. The range lives in the URL
// (viewer/range.ts). Nothing refetches by itself: a dataset id change loads, and Try Again retries
// (no polling, no events in phase 1).
import { computed, ref, shallowRef } from 'vue'
import { defineStore } from 'pinia'
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { ApiError, getDataset } from '@/api'
import { describeError } from '@/stores/datasetCatalog'
import type { CatalogError } from '@/stores/datasetCatalog'

export type ViewerLoadStatus = 'idle' | 'loading' | 'ready' | 'not-found' | 'error'

/** The moving averages the sidebar offers, in bars. */
export const SMA_PERIODS = [20, 50] as const
export type SmaPeriod = (typeof SMA_PERIODS)[number]

export const useDatasetViewerStore = defineStore('datasetViewer', () => {
  // The detail is a protobuf message: held shallow, never made deeply reactive.
  const dataset = shallowRef<DatasetSummary | null>(null)
  const status = ref<ViewerLoadStatus>('idle')
  const error = ref<CatalogError | null>(null)

  const sma = ref<Record<SmaPeriod, boolean>>({ 20: true, 50: true })
  const showVolume = ref(true)

  /** The SMA periods switched on, for PriceChart. */
  const smaPeriods = computed(() => SMA_PERIODS.filter((period) => sma.value[period]))

  let inflight: AbortController | null = null

  /** Load the dataset detail. A call still in flight is cancelled; the previous dataset is dropped. */
  async function load(id: string): Promise<void> {
    inflight?.abort()
    const controller = new AbortController()
    inflight = controller
    dataset.value = null
    error.value = null
    status.value = 'loading'
    try {
      const result = await getDataset(id, controller.signal)
      if (controller.signal.aborted) return
      dataset.value = result
      status.value = 'ready'
    } catch (caught) {
      if (controller.signal.aborted) return
      error.value = describeError(caught)
      // An unknown id is its own state, not a failure to retry.
      status.value = caught instanceof ApiError && caught.status === 404 ? 'not-found' : 'error'
    }
  }

  function reset(): void {
    inflight?.abort()
    inflight = null
    dataset.value = null
    error.value = null
    status.value = 'idle'
  }

  return { dataset, status, error, sma, showVolume, smaPeriods, load, reset }
})
