<script setup lang="ts">
// The Data Viewer (tj-grna9p.33, canvas board "Shell · Viewer", phase 1): the dataset's header, the
// candles and volume chart with its zoom navigator and hover readout, and the paged bar table. Raw
// bars only (owner ruling, tj-kqsnxb). The sidebar (DatasetViewerSidebar.vue) is the route's
// `sidebar` view. Reached from a Datasets row click; the header badge comes from the dataset
// detail's freshness. No polling and no events: it loads when the dataset or the range changes.
//
// The chart holds a bounded number of bars (viewer/barWindow.ts: 10,000 per page, at most 50,000);
// the table pages the whole window. Bars live in a shallowRef of a markRaw'd array (tj-x5yghe).
import { computed, markRaw, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { Code, Download } from '@lucide/vue'

import type { Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'

import { getDatasetBars, getDatasetBarsRequest, int64ToNumber } from '@/api'
import { dataTypeLabel, granularityLabel, isIntraday, sourceFeedLabel, updateTypeLabel } from '@/catalog/labels'
import { rowBadge } from '@/catalog/rows'
import StatusBadge from '@/components/StatusBadge.vue'
import PriceChart from '@/components/charts/PriceChart.vue'
import type { PriceHover } from '@/components/charts/PriceChart.vue'
import ZoomNavigator from '@/components/charts/ZoomNavigator.vue'
import type { LogicalRangeLike } from '@/components/charts/navigatorMath'
import BarTable from '@/components/viewer/BarTable.vue'
import { MISSING, formatCompact, formatNumber, timestampToInstant } from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import { useFooterSummary } from '@/shell/useFooterSummary'
import { describeError, type CatalogError } from '@/stores/datasetCatalog'
import { useDatasetViewerStore } from '@/stores/datasetViewer'
import { CHART_PAGE_SIZE, MAX_CHART_BARS, TABLE_PAGE_SIZE, loadBarWindow } from '@/viewer/barWindow'
import { NO_BARS_TEXT } from '@/viewer/copy'
import { barsToCsv } from '@/viewer/csv'
import { rangeProblem, toApiWindow } from '@/viewer/range'
import { useViewerRange } from '@/viewer/useViewerRange'

const route = useRoute()
const store = useDatasetViewerStore()
const { dataset, status, error, smaPeriods, showVolume } = storeToRefs(store)
const { range } = useViewerRange()
const { date, dateTime, timeZone } = useFormatters()
const { setFooterSummary } = useFooterSummary()

const datasetId = computed(() => String(route.params.id))

// The dataset detail: loaded when the id changes (and cleared when the screen goes).
watch(datasetId, (id) => void store.load(id), { immediate: true })

const intraday = computed(() => (dataset.value ? isIntraday(dataset.value.granularity) : false))
const title = computed(() =>
  dataset.value
    ? `${dataset.value.assetSymbol} · ${dataTypeLabel(dataset.value.dataType)} · ${granularityLabel(dataset.value.granularity)}`
    : 'Viewer',
)
const barCount = computed(() => (dataset.value ? int64ToNumber(dataset.value.barCount) : 0))
const hasBars = computed(() => barCount.value > 0)
const problem = computed(() => rangeProblem(range.value))

// The window the range names, in the viewer's zone: the half-open [start, end) both fetches use.
const timeWindow = computed(() => toApiWindow(range.value, timeZone.value))
const windowKey = computed(() => `${timeWindow.value.start?.getTime()}|${timeWindow.value.end?.getTime()}`)

// --- the chart's bars --------------------------------------------------------------------------

type ChartStatus = 'loading' | 'ready' | 'empty' | 'error'

const bars = shallowRef<readonly Bar[]>(markRaw([]))
const closes = shallowRef<Float64Array>(new Float64Array(0))
const chartStatus = ref<ChartStatus>('loading')
const chartError = ref<CatalogError | null>(null)
const truncated = ref(false)
/** Remounts the chart for each load, so it fits its new bars. */
const chartKey = ref(0)
const visibleRange = ref<LogicalRangeLike | null>(null)
const hover = ref<PriceHover | null>(null)
const retryKey = ref(0)

let inflight: AbortController | null = null

async function loadChart(): Promise<void> {
  inflight?.abort()
  const controller = new AbortController()
  inflight = controller
  const id = datasetId.value
  const { start, end } = timeWindow.value
  chartStatus.value = 'loading'
  chartError.value = null
  try {
    const result = await loadBarWindow({
      signal: controller.signal,
      fetchPage: async (cursor, limit, signal) => {
        const page = await getDatasetBars(id, { start, end, cursor, limit, signal })
        return { bars: page.bars, nextCursor: page.nextCursor }
      },
    })
    if (controller.signal.aborted) return
    bars.value = markRaw(result.bars)
    closes.value = Float64Array.from(result.bars, (bar) => bar.close)
    truncated.value = result.truncated
    visibleRange.value = null
    hover.value = null
    chartKey.value += 1
    chartStatus.value = result.bars.length === 0 ? 'empty' : 'ready'
  } catch (caught) {
    if (controller.signal.aborted) return
    chartError.value = describeError(caught)
    chartStatus.value = 'error'
  }
}

// Once per (dataset, window): the detail has arrived, the dataset has bars and the range is usable.
watch(
  [() => status.value === 'ready' && hasBars.value, datasetId, windowKey, retryKey],
  ([ready]) => {
    if (!ready) return
    if (problem.value !== undefined) {
      inflight?.abort()
      bars.value = markRaw([])
      closes.value = new Float64Array(0)
      chartStatus.value = 'empty'
      return
    }
    void loadChart()
  },
  { immediate: true },
)

// --- the footer, the readout, Export CSV, Show Request ---------------------------------------

const tableLoaded = ref(0)

watch(
  [() => status.value === 'ready', barCount, () => bars.value.length, truncated, () => range.value],
  () => {
    if (status.value !== 'ready' || dataset.value === undefined || dataset.value === null) return setFooterSummary(null)
    const ranged = range.value.from !== undefined || range.value.to !== undefined
    const loaded = formatNumber(bars.value.length)
    // The total is the dataset's bar count; inside a range only "loaded" is known.
    const text =
      !ranged && hasBars.value
        ? `${loaded} of ${formatNumber(barCount.value)} bars · ${granularityLabel(dataset.value.granularity)}`
        : `${loaded}${truncated.value ? '+' : ''} bars in range · ${granularityLabel(dataset.value.granularity)}`
    setFooterSummary(text)
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  inflight?.abort()
  store.reset()
  setFooterSummary(null)
})

function clock(seconds: number): string {
  return intraday.value ? dateTime(seconds * 1000) : date(seconds * 1000)
}

function exportCsv(): void {
  if (!dataset.value || bars.value.length === 0) return
  const csv = barsToCsv(bars.value, timeZone.value, intraday.value)
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `${dataset.value.assetSymbol}_${granularityLabel(dataset.value.granularity)}.csv`
  link.click()
  URL.revokeObjectURL(url)
}

const showRequest = ref(false)
const requestText = computed(() => {
  const { start, end } = timeWindow.value
  const chart = getDatasetBarsRequest(datasetId.value, { start, end, limit: CHART_PAGE_SIZE })
  const rows = getDatasetBarsRequest(datasetId.value, { start, end, limit: TABLE_PAGE_SIZE })
  return `Chart  GET ${chart}\nRows   GET ${rows}`
})

const extent = computed(() => {
  const d = dataset.value
  if (!d?.firstBar || !d.lastBar) return null
  return { first: date(timestampToInstant(d.firstBar)), last: date(timestampToInstant(d.lastBar)) }
})
</script>

<template>
  <section class="viewer">
    <div
      v-if="status === 'loading' || status === 'idle'"
      class="panel"
      role="status"
      data-testid="viewer-loading"
    >
      Loading dataset
    </div>

    <div
      v-else-if="status === 'not-found'"
      class="panel"
      data-testid="viewer-not-found"
    >
      <h1 class="viewer__title">
        Dataset Not Found
      </h1>
      <span>No dataset has the id {{ datasetId }}.</span>
      <RouterLink
        class="link"
        :to="{ name: 'data-datasets' }"
      >
        Back to Datasets
      </RouterLink>
    </div>

    <div
      v-else-if="status === 'error'"
      class="panel panel--error"
      role="alert"
      data-testid="viewer-error"
    >
      <span>Could not load the dataset. {{ error?.message }}</span>
      <span
        v-if="error?.errorId"
        class="panel__id"
      >Error ID: {{ error.errorId }}</span>
      <button
        type="button"
        class="btn"
        @click="store.load(datasetId)"
      >
        Try Again
      </button>
    </div>

    <template v-else-if="dataset">
      <header class="viewer__head">
        <h1
          class="viewer__title"
          data-testid="viewer-title"
        >
          {{ title }}
        </h1>
        <StatusBadge
          :badge="rowBadge(dataset)"
          data-testid="viewer-badge"
        />
        <div class="viewer__actions">
          <button
            type="button"
            class="btn"
            :disabled="bars.length === 0"
            data-testid="export-csv"
            @click="exportCsv"
          >
            <Download
              :size="16"
              aria-hidden="true"
            />
            Export CSV
          </button>
        </div>
      </header>

      <dl
        class="meta"
        data-testid="viewer-meta"
      >
        <div>
          <dt>Source</dt>
          <dd>{{ sourceFeedLabel(dataset.source, dataset.feed) }}</dd>
        </div>
        <div>
          <dt>Granularity</dt>
          <dd>{{ granularityLabel(dataset.granularity) }}</dd>
        </div>
        <div>
          <dt>Update</dt>
          <dd>{{ updateTypeLabel(dataset.updateType) }}</dd>
        </div>
        <div>
          <dt>First Bar</dt>
          <dd>{{ extent?.first ?? MISSING }}</dd>
        </div>
        <div>
          <dt>Last Bar</dt>
          <dd>{{ extent?.last ?? MISSING }}</dd>
        </div>
        <div>
          <dt>Bars</dt>
          <dd>{{ formatNumber(barCount) }}</dd>
        </div>
      </dl>

      <div
        v-if="!hasBars"
        class="panel"
        data-testid="viewer-empty"
      >
        {{ NO_BARS_TEXT }}
      </div>

      <template v-else>
        <div
          v-if="problem"
          class="notice"
          role="alert"
        >
          {{ problem }}
        </div>

        <div class="chart-card">
          <p
            class="readout"
            data-testid="viewer-readout"
          >
            <template v-if="hover">
              <span>{{ clock(hover.time) }}</span>
              <span>O {{ formatNumber(hover.open, 2) }}</span>
              <span>H {{ formatNumber(hover.high, 2) }}</span>
              <span>L {{ formatNumber(hover.low, 2) }}</span>
              <span>C {{ formatNumber(hover.close, 2) }}</span>
              <span>Vol {{ formatCompact(hover.volume) }}</span>
              <span
                v-for="period in smaPeriods"
                :key="period"
              >SMA {{ period }} {{ formatNumber(hover.sma[period], 2) }}</span>
            </template>
            <template v-else>
              Hover the chart for O H L C and volume.
            </template>
          </p>

          <div
            v-if="truncated"
            class="notice"
            role="status"
            data-testid="viewer-truncated"
          >
            The chart shows the first {{ formatNumber(MAX_CHART_BARS) }} bars of this range. Narrow the range
            with From and To to see the rest; the table below pages every bar.
          </div>

          <div
            v-if="chartStatus === 'loading'"
            class="panel panel--chart"
            role="status"
            data-testid="chart-loading"
          >
            Loading bars
          </div>
          <div
            v-else-if="chartStatus === 'error'"
            class="panel panel--chart panel--error"
            role="alert"
            data-testid="chart-error"
          >
            <span>Could not load bars. {{ chartError?.message }}</span>
            <span
              v-if="chartError?.errorId"
              class="panel__id"
            >Error ID: {{ chartError.errorId }}</span>
            <button
              type="button"
              class="btn"
              @click="retryKey += 1"
            >
              Try Again
            </button>
          </div>
          <div
            v-else-if="chartStatus === 'empty'"
            class="panel panel--chart"
            data-testid="chart-empty"
          >
            No bars in this range.
          </div>
          <template v-else>
            <PriceChart
              :key="chartKey"
              v-model:visible-range="visibleRange"
              :bars="bars"
              :sma-periods="smaPeriods"
              :show-volume="showVolume"
              @hover="hover = $event"
            />
            <ZoomNavigator
              v-model:visible-range="visibleRange"
              :values="closes"
            />
          </template>
        </div>

        <div class="rows-head">
          <h2 class="rows-title">
            Rows
          </h2>
          <span class="rows-count">{{ formatNumber(tableLoaded) }} loaded</span>
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
        </div>
        <pre
          v-if="showRequest"
          class="request"
          data-testid="viewer-request"
        >{{ requestText }}</pre>

        <BarTable
          :dataset-id="datasetId"
          :time-window="timeWindow"
          :intraday="intraday"
          @loaded="tableLoaded = $event.loadedRows"
        />
      </template>
    </template>
  </section>
</template>

<style scoped>
.viewer {
  min-height: 100%;
  display: flex;
  flex-direction: column;
  gap: var(--tj-page-gap);
}

.viewer__head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.viewer__title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}

.viewer__actions {
  margin-left: auto;
  display: flex;
  gap: var(--tj-control-gap);
}

.meta {
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 28px;
}

.meta dt {
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--tj-text3);
}

.meta dd {
  margin: 2px 0 0;
  font-size: 13px;
  font-family: var(--tj-font-mono);
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

.btn:disabled {
  color: var(--tj-text3);
  cursor: not-allowed;
}

.btn[aria-pressed='true'] {
  background: var(--tj-raised);
}

.link {
  color: var(--tj-accent-tint);
  font-size: 13px;
}

.panel {
  min-height: 160px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: var(--tj-surface);
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
  color: var(--tj-text2);
  font-size: 13px;
}

.panel--chart {
  min-height: 420px;
  border: 0;
}

.panel--error {
  color: var(--tj-text);
}

.panel__id {
  font-family: var(--tj-font-mono);
  font-size: 12px;
  color: var(--tj-text3);
}

.notice {
  padding: 8px 12px;
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
  background: var(--tj-raised);
  font-size: 12px;
  color: var(--tj-text2);
}

.chart-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.readout {
  margin: 0;
  min-height: 1.5em;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  font-family: var(--tj-font-mono);
  font-size: 12px;
  color: var(--tj-text2);
}

.rows-head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.rows-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}

.rows-count {
  margin-right: auto;
  font-family: var(--tj-font-mono);
  font-size: 12px;
  color: var(--tj-text3);
}

.request {
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
