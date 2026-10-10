<script setup lang="ts">
// The Datasets catalog (tj-grna9p.32, canvas board "Shell · Datasets", phase 1): title, Refresh and
// Show Request, the Datasets, Failed and Late tiles, and the grid. The sidebar (DatasetsSidebar.vue)
// is the route's `sidebar` view. The filters live in the URL; this page and the sidebar both read
// them. No polling and no events in phase 1: the page reloads on a filter change and on Refresh.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { Code, RefreshCw } from '@lucide/vue'

import { listDatasetsRequest } from '@/api'
import { filtersKey, toApiFilters } from '@/catalog/filters'
import { useCatalogFilters } from '@/catalog/useCatalogFilters'
import DatasetGrid from '@/components/catalog/DatasetGrid.vue'
import DatasetTiles from '@/components/catalog/DatasetTiles.vue'
import { formatNumber } from '@/format/formatters'
import { useFooterSummary } from '@/shell/useFooterSummary'
import { useDatasetCatalogStore } from '@/stores/datasetCatalog'

const { filters } = useCatalogFilters()
const store = useDatasetCatalogStore()
const { facets, loadedRows, listDone, listStatus } = storeToRefs(store)
const { setFooterSummary } = useFooterSummary()

const showRequest = ref(false)

// The call behind the view: the first page under the current filters, as the browser sends it.
const requestText = computed(() => `GET ${listDatasetsRequest({ ...toApiFilters(filters.value), limit: 100 })}`)

// The counts under the filters, for the sidebar. A filter change refetches them.
watch(
  () => filtersKey(filters.value),
  () => void store.loadFacets(filters.value),
)

onMounted(() => {
  void store.loadFacets(filters.value)
  void store.loadTiles()
})

// "9 of 38 datasets · sorted by symbol": rows loaded so far over the total under the filters (the
// view's own count from the facets; the loaded count itself once the last page is in).
const total = computed(() => {
  if (listDone.value) return loadedRows.value
  if (facets.value === null) return null
  return filters.value.view === 'needs-attention' ? facets.value.needsAttention : facets.value.all
})

watch(
  [loadedRows, total, listStatus],
  () => {
    if (listStatus.value === 'loading') return setFooterSummary(null)
    const loaded = formatNumber(loadedRows.value)
    const of = total.value === null ? '' : ` of ${formatNumber(total.value)}`
    setFooterSummary(`${loaded}${of} datasets · sorted by symbol`)
  },
  { immediate: true },
)

onBeforeUnmount(() => setFooterSummary(null))
</script>

<template>
  <section class="catalog">
    <div class="catalog__head">
      <h1 class="catalog__title">
        Datasets
      </h1>
      <div class="catalog__actions">
        <button
          type="button"
          class="btn"
          :aria-pressed="showRequest"
          @click="showRequest = !showRequest"
        >
          <Code
            :size="16"
            aria-hidden="true"
          />
          Show Request
        </button>
        <button
          type="button"
          class="btn"
          @click="store.refresh(filters)"
        >
          <RefreshCw
            :size="16"
            aria-hidden="true"
          />
          Refresh
        </button>
      </div>
    </div>

    <pre
      v-if="showRequest"
      class="catalog__request"
      data-testid="catalog-request"
    >{{ requestText }}</pre>

    <DatasetTiles />

    <DatasetGrid :filters="filters" />
  </section>
</template>

<style scoped>
.catalog {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: var(--tj-page-gap);
}

.catalog__head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.catalog__title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}

.catalog__actions {
  margin-left: auto;
  display: flex;
  gap: var(--tj-control-gap);
}

.btn {
  height: var(--tj-control-height);
  padding: 0 12px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
  background: transparent;
  color: var(--tj-text);
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}

.btn[aria-pressed='true'] {
  background: var(--tj-raised);
}

.catalog__request {
  margin: 0;
  padding: 10px 14px;
  background: var(--tj-surface);
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
  color: var(--tj-text2);
  font-family: var(--tj-font-mono);
  font-size: 12px;
  overflow-x: auto;
}
</style>
