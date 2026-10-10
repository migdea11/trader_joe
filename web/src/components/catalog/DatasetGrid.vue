<script setup lang="ts">
// The catalog grid (tj-grna9p.32, ADR tj-grna9p.2): AG Grid Community, infinite row model over the
// keyset-paged /ui/v1/datasets (catalog/cursorDatasource.ts). Columns: Symbol, Source and Feed,
// Gran., Update, Status, Expires. A new datasource (so an empty block cache and no cursor) is made
// when the filters change or the store's refreshKey is bumped; the old one aborts its requests.
// A row click opens the Viewer. Loading, empty and error states are panels over the grid.
import { computed, onBeforeUnmount, shallowRef, watch } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { AgGridVue } from 'ag-grid-vue3'
import type { ColDef, GetRowIdParams, GridApi, GridReadyEvent, RowClickedEvent } from 'ag-grid-community'

import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { listDatasets } from '@/api'
import { gridTheme, registerGridModules } from '@/catalog/agGrid'
import { createCursorDatasource } from '@/catalog/cursorDatasource'
import { filtersKey, toApiFilters } from '@/catalog/filters'
import type { CatalogFilters } from '@/catalog/filters'
import { granularityLabel, sourceFeedLabel, updateTypeLabel } from '@/catalog/labels'
import { VIEWER_ROUTE } from '@/catalog/rows'
import SymbolCell from '@/components/catalog/SymbolCell.vue'
import StatusCell from '@/components/catalog/StatusCell.vue'
import { MISSING, timestampToInstant } from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import { useDatasetCatalogStore } from '@/stores/datasetCatalog'

const props = defineProps<{ filters: CatalogFilters }>()

registerGridModules()

/** Rows per page: the server's default limit and the grid's block size. */
const PAGE_SIZE = 100

const router = useRouter()
const store = useDatasetCatalogStore()
const { listStatus, listError, loadedRows, refreshKey } = storeToRefs(store)
const { date } = useFormatters()

const api = shallowRef<GridApi<DatasetSummary> | null>(null)

function viewerHref(id: string): string {
  return router.resolve({ name: VIEWER_ROUTE, params: { id } }).href
}

const columnDefs: ColDef<DatasetSummary>[] = [
  {
    colId: 'symbol',
    headerName: 'Symbol',
    cellRenderer: SymbolCell,
    cellRendererParams: { href: viewerHref },
    flex: 1.2,
    minWidth: 110,
  },
  {
    colId: 'source',
    headerName: 'Source · Feed',
    valueGetter: ({ data }) => (data ? sourceFeedLabel(data.source, data.feed) : undefined),
    flex: 1.4,
    minWidth: 140,
  },
  {
    colId: 'granularity',
    headerName: 'Gran.',
    valueGetter: ({ data }) => (data ? granularityLabel(data.granularity) : undefined),
    width: 90,
  },
  {
    colId: 'update',
    headerName: 'Update',
    valueGetter: ({ data }) => (data ? updateTypeLabel(data.updateType) : undefined),
    flex: 1,
    minWidth: 100,
  },
  {
    colId: 'status',
    headerName: 'Status',
    cellRenderer: StatusCell,
    flex: 1.2,
    minWidth: 120,
  },
  {
    colId: 'expires',
    headerName: 'Expires',
    // Nothing scheduled: a dash, as on the canvas.
    valueGetter: ({ data }) => {
      if (!data) return undefined
      return data.expiry === undefined ? MISSING : date(timestampToInstant(data.expiry))
    },
    flex: 1,
    minWidth: 110,
  },
]

const defaultColDef: ColDef<DatasetSummary> = { sortable: false, suppressMovable: true, resizable: true }

function getRowId(params: GetRowIdParams<DatasetSummary>): string {
  return params.data.id
}

// One datasource per (filters, refresh): the cursor chain starts over, the old one is destroyed
// by the grid (which aborts its in-flight requests).
function install(): void {
  if (api.value === null) return
  store.listLoading()
  api.value.setGridOption(
    'datasource',
    createCursorDatasource<DatasetSummary>({
      pageSize: PAGE_SIZE,
      fetchPage: async (cursor, limit, signal) => {
        const page = await listDatasets({ ...toApiFilters(props.filters), cursor, limit, signal })
        return { items: page.items, nextCursor: page.nextCursor }
      },
      onPage: (info) => store.listPage(info),
      onError: (error, info) => store.listFailed(error, info),
    }),
  )
}

function onGridReady(event: GridReadyEvent<DatasetSummary>): void {
  api.value = event.api
  install()
}

watch([() => filtersKey(props.filters), refreshKey], install)

function onRowClicked(event: RowClickedEvent<DatasetSummary>): void {
  const mouse = event.event as MouseEvent | null | undefined
  // A modified click is the browser's (new tab on the Symbol link); the row only handles a plain one.
  if (mouse && (mouse.metaKey || mouse.ctrlKey || mouse.shiftKey || mouse.altKey)) return
  if (event.data) void router.push({ name: VIEWER_ROUTE, params: { id: event.data.id } })
}

onBeforeUnmount(() => {
  api.value = null
})

const showLoading = computed(() => listStatus.value === 'loading')
const showEmpty = computed(() => listStatus.value === 'empty')
const showError = computed(() => listStatus.value === 'error')
const isFiltered = computed(() => filtersKey(props.filters) !== filtersKey({ view: 'all' }))
</script>

<template>
  <div class="grid-wrap">
    <AgGridVue
      class="grid"
      :theme="gridTheme"
      :column-defs="columnDefs"
      :default-col-def="defaultColDef"
      row-model-type="infinite"
      :cache-block-size="PAGE_SIZE"
      :cache-overflow-size="1"
      :max-concurrent-datasource-requests="1"
      :get-row-id="getRowId"
      @grid-ready="onGridReady"
      @row-clicked="onRowClicked"
    />
    <div
      v-if="showLoading"
      class="grid-panel"
      role="status"
      data-testid="catalog-loading"
    >
      Loading datasets
    </div>
    <div
      v-else-if="showEmpty"
      class="grid-panel"
      data-testid="catalog-empty"
    >
      {{ isFiltered ? 'No datasets match these filters.' : 'There are no datasets yet.' }}
    </div>
    <div
      v-else-if="showError"
      class="grid-panel grid-panel--error"
      :class="{ 'grid-panel--banner': loadedRows > 0 }"
      role="alert"
      data-testid="catalog-error"
    >
      <span>Could not load datasets. {{ listError?.message }}</span>
      <span
        v-if="listError?.errorId"
        class="grid-panel__id"
      >Error ID: {{ listError.errorId }}</span>
      <button
        type="button"
        class="grid-panel__retry"
        @click="store.refresh(filters)"
      >
        Try Again
      </button>
    </div>
  </div>
</template>

<style scoped>
.grid-wrap {
  position: relative;
  flex: 1;
  min-height: 240px;
  background: var(--tj-surface);
}

.grid {
  height: 100%;
}

.grid :deep(.ag-row) {
  cursor: pointer;
}

.grid-panel {
  position: absolute;
  inset: 36px 0 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: var(--tj-surface);
  color: var(--tj-text2);
  font-size: 13px;
}

.grid-panel--error {
  color: var(--tj-text);
}

/* A later page failed: the rows already loaded stay visible above a slim bar. */
.grid-panel--banner {
  inset: auto 0 0;
  flex-direction: row;
  justify-content: center;
  padding: 10px 16px;
  border-top: 1px solid var(--tj-line);
}

.grid-panel__id {
  font-family: var(--tj-font-mono);
  font-size: 12px;
  color: var(--tj-text3);
}

.grid-panel__retry {
  height: var(--tj-control-height);
  padding: 0 12px;
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
  background: transparent;
  color: var(--tj-text);
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}
</style>
