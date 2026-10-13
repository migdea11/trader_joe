<script setup lang="ts">
// The grid's Coverage cell (tj-grna9p.77): the dataset's CoverageStrip, fetched lazily. AG Grid mounts
// a cell only for a row in (or just beside) the viewport, so a row the user never scrolls to is never
// asked for, a row that scrolls away aborts a request still in flight, and a strip already fetched is
// served from the store's cache (stores/datasetCoverage.ts). A dataset with no trading calendar (422)
// reads "No coverage": a neutral fact, not an error. After the numbers, in a slot of its own, sits the
// details icon (tj-grna9p.114): a real link to the Dataset detail, where this column's coverage is
// explained. Its click never reaches the row (which opens the Viewer).
import { computed, onBeforeUnmount, watch } from 'vue'
import { Info } from '@lucide/vue'
import { useRouter } from 'vue-router'
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { ICellRendererParams } from 'ag-grid-community'

import CoverageStrip from '@/components/charts/CoverageStrip.vue'
import { useDatasetCoverageStore } from '@/stores/datasetCoverage'

/** Buckets asked for per strip in the grid. The strip draws no per-bucket pieces; it only sums them. */
const GRID_BUCKETS = 60

const props = defineProps<{
  params: ICellRendererParams<DatasetSummary> & {
    /** The Dataset detail's URL for a dataset id. */
    detailHref: (id: string) => string
  }
}>()

const router = useRouter()
const store = useDatasetCoverageStore()
const id = computed(() => props.params.data?.id)
const entry = computed(() => (id.value === undefined ? undefined : store.entry(id.value, GRID_BUCKETS)))

// A recycled cell can be handed another row: let go of the old strip, hold the new one.
watch(
  id,
  (next, previous) => {
    if (previous !== undefined) store.release(previous, GRID_BUCKETS)
    if (next !== undefined) store.acquire(next, GRID_BUCKETS)
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  if (id.value !== undefined) store.release(id.value, GRID_BUCKETS)
})

function onDetailsClick(event: MouseEvent): void {
  // Never also the row's click (which opens the Viewer).
  event.stopPropagation()
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
  event.preventDefault()
  if (id.value !== undefined) void router.push(props.params.detailHref(id.value))
}
</script>

<template>
  <div
    v-if="id !== undefined"
    class="coverage-cell"
    :aria-busy="entry === undefined ? 'true' : undefined"
  >
    <div class="coverage-cell__body">
      <CoverageStrip
        v-if="entry?.state === 'ready'"
        :strip="entry.strip"
        compact
      />
      <span
        v-else-if="entry?.state === 'none'"
        class="coverage-cell__note"
        data-testid="coverage-none"
        title="No trading calendar covers this dataset"
      >No coverage</span>
      <span
        v-else-if="entry?.state === 'unavailable'"
        class="coverage-cell__note"
        data-testid="coverage-unavailable"
      >Coverage unavailable</span>
      <span
        v-else
        class="coverage-cell__loading"
        role="status"
        data-testid="coverage-loading"
      ><span class="coverage-cell__sr">Loading coverage</span></span>
    </div>
    <a
      class="details-link"
      :href="props.params.detailHref(id)"
      :aria-label="`Dataset details for ${props.params.data?.assetSymbol}`"
      title="Dataset details"
      data-testid="dataset-details-link"
      @click="onDetailsClick"
    >
      <Info
        :size="15"
        aria-hidden="true"
      />
    </a>
  </div>
</template>

<style scoped>
.coverage-cell {
  /* The one bar width for every row, and the slot the numbers sit in (CoverageStrip, compact). */
  --coverage-bar-width: 112px;
  --coverage-text-width: 9ch;
  /* The details icon's own slot, the same on every row. */
  --coverage-icon-width: 24px;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 100%;
  min-width: 0;
  overflow: hidden;
}

/* The bar and numbers shrink and clip as a unit; the icon's slot never does. In a narrow column the
   numbers ellipsize first (CoverageStrip), then the bar is clipped, and the icon stays reachable. */
.coverage-cell__body {
  display: flex;
  align-items: center;
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
}

.coverage-cell :deep(.coverage) {
  flex: 0 1 auto;
  min-width: 0;
}

.details-link {
  display: inline-flex;
  flex: 0 0 var(--coverage-icon-width);
  align-items: center;
  justify-content: center;
  height: var(--coverage-icon-width);
  border-radius: var(--tj-radius-control);
  color: var(--tj-text2);
  text-decoration: none;
}

.details-link:hover {
  color: var(--tj-text);
}

.coverage-cell__note {
  font-size: 12px;
  color: var(--tj-text3);
  white-space: nowrap;
}

.coverage-cell__loading {
  display: block;
  width: var(--coverage-bar-width);
  max-width: 100%;
  height: 14px;
  border-radius: 2px;
  background: var(--tj-line);
  opacity: 0.4;
}

.coverage-cell__sr {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}
</style>
