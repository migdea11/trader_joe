<script setup lang="ts">
// The Viewer's bar table (tj-grna9p.33, ADR tj-grna9p.2): AG Grid Community, infinite row model over
// the keyset-paged /ui/v1/datasets/{id}/bars (catalog/cursorDatasource.ts: block N's cursor is block
// N-1's next_cursor). It pages the WHOLE selected window, not just the bars the chart holds. A new
// datasource (so an empty cache and no cursor) is made when the dataset or the window changes; the
// old one aborts its requests. Dates and times show in the settings zone and re-render when it changes.
import { computed, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { AgGridVue } from 'ag-grid-vue3'
import type { ColDef, GridApi, GridReadyEvent } from 'ag-grid-community'

import type { Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'

import { getDatasetBars } from '@/api'
import { gridTheme, registerGridModules } from '@/catalog/agGrid'
import { createCursorDatasource } from '@/catalog/cursorDatasource'
import { int64ToNumber } from '@/api/int64'
import { MISSING, formatCompact, formatNumber } from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import { describeError } from '@/stores/datasetCatalog'
import type { CatalogError } from '@/stores/datasetCatalog'
import { TABLE_PAGE_SIZE } from '@/viewer/barWindow'
import type { ApiWindow } from '@/viewer/range'

const props = defineProps<{
  datasetId: string
  timeWindow: ApiWindow
  /** Bars narrower than a day show the time of day too. */
  intraday: boolean
}>()

const emit = defineEmits<{
  /** Rows the table has loaded so far, and whether it reached the end of the window. */
  loaded: [info: { loadedRows: number; done: boolean }]
}>()

registerGridModules()

const { date, dateTime, timeZone } = useFormatters()

const api = shallowRef<GridApi<Bar> | null>(null)
const error = ref<CatalogError | null>(null)
const retryKey = ref(0)

const price = (value: number | null | undefined): string => formatNumber(value, 2)

const columnDefs = computed<ColDef<Bar>[]>(() => [
  {
    colId: 'date',
    headerName: 'Date',
    valueGetter: ({ data }) => {
      if (!data) return undefined
      if (!data.barStart) return MISSING
      const ms = int64ToNumber(data.barStart.seconds) * 1000
      return props.intraday ? dateTime(ms) : date(ms)
    },
    flex: 1.4,
    minWidth: 130,
  },
  { colId: 'open', headerName: 'Open', field: 'open', valueFormatter: (p) => price(p.value), type: 'rightAligned', flex: 1 },
  { colId: 'high', headerName: 'High', field: 'high', valueFormatter: (p) => price(p.value), type: 'rightAligned', flex: 1 },
  { colId: 'low', headerName: 'Low', field: 'low', valueFormatter: (p) => price(p.value), type: 'rightAligned', flex: 1 },
  { colId: 'close', headerName: 'Close', field: 'close', valueFormatter: (p) => price(p.value), type: 'rightAligned', flex: 1 },
  { colId: 'volume', headerName: 'Vol', field: 'volume', valueFormatter: (p) => formatCompact(p.value), type: 'rightAligned', flex: 1 },
])

const defaultColDef: ColDef<Bar> = { sortable: false, suppressMovable: true, resizable: true }

function install(): void {
  if (api.value === null) return
  error.value = null
  const { start, end } = props.timeWindow
  api.value.setGridOption(
    'datasource',
    createCursorDatasource<Bar>({
      pageSize: TABLE_PAGE_SIZE,
      fetchPage: async (cursor, limit, signal) => {
        const page = await getDatasetBars(props.datasetId, { start, end, cursor, limit, signal })
        return { items: page.bars, nextCursor: page.nextCursor }
      },
      onPage: (info) => emit('loaded', info),
      onError: (caught) => {
        error.value = describeError(caught)
      },
    }),
  )
}

function onGridReady(event: GridReadyEvent<Bar>): void {
  api.value = event.api
  install()
}

// One new datasource per (dataset, window, retry).
watch(
  () => [props.datasetId, props.timeWindow.start?.getTime(), props.timeWindow.end?.getTime(), retryKey.value],
  install,
)

// The zone changed: the rendered date cells are read again.
watch([timeZone, () => props.intraday], () => api.value?.refreshCells({ force: true }))

onBeforeUnmount(() => {
  api.value = null
})
</script>

<template>
  <div class="bar-table">
    <AgGridVue
      class="bar-table__grid"
      :theme="gridTheme"
      :column-defs="columnDefs"
      :default-col-def="defaultColDef"
      row-model-type="infinite"
      :cache-block-size="TABLE_PAGE_SIZE"
      :cache-overflow-size="1"
      :max-concurrent-datasource-requests="1"
      @grid-ready="onGridReady"
    />
    <div
      v-if="error"
      class="bar-table__error"
      role="alert"
      data-testid="bars-table-error"
    >
      <span>Could not load bars. {{ error.message }}</span>
      <span
        v-if="error.errorId"
        class="bar-table__id"
      >Error ID: {{ error.errorId }}</span>
      <button
        type="button"
        class="bar-table__retry"
        @click="retryKey += 1"
      >
        Try Again
      </button>
    </div>
  </div>
</template>

<style scoped>
.bar-table {
  position: relative;
  flex: 1;
  min-height: 240px;
  background: var(--tj-surface);
}

.bar-table__grid {
  height: 100%;
}

.bar-table__error {
  position: absolute;
  inset: auto 0 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 10px 16px;
  border-top: 1px solid var(--tj-line);
  background: var(--tj-surface);
  font-size: 13px;
}

.bar-table__id {
  font-family: var(--tj-font-mono);
  font-size: 12px;
  color: var(--tj-text3);
}

.bar-table__retry {
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
