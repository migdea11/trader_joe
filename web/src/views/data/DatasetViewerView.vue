<script setup lang="ts">
// The Data Viewer (tj-grna9p.33, canvas board "Shell · Viewer", phase 1): the dataset's header, the
// candles and volume chart with its zoom navigator and hover readout, and the paged bar table. Raw
// bars only (owner ruling, tj-kqsnxb). The sidebar (DatasetViewerSidebar.vue) is the route's
// `sidebar` view. Reached from a Datasets row click (the default destination; its Details link goes
// on to the Dataset detail); the header badge comes from the dataset detail's freshness. No polling
// and no events: it loads when the dataset or the range changes.
//
// The chart holds ONE page of raw bars: a 1,000-bar block plus a 50-bar look-ahead of the next one
// (viewer/chartPages.ts, decision tj-eozqhi): the server's page index locates the blocks of the
// From/To window, the zoom navigator is the pager, and the table below pages the whole window by
// itself. Page bars live in shallowRefs, never
// deep-reactive (tj-x5yghe).
import { computed, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { Code } from '@lucide/vue'

import type { Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'

import { getDatasetBarPagesRequest, getDatasetBarsRequest, int64ToNumber } from '@/api'
import { dataTypeLabel, datasetIdentityLabel, granularityLabel, isIntraday, sourceFeedLabel, updateTypeLabel } from '@/catalog/labels'
import { DETAIL_ROUTE, rowBadge } from '@/catalog/rows'
import StatusBadge from '@/components/StatusBadge.vue'
import PageBreadcrumbs from '@/components/nav/PageBreadcrumbs.vue'
import type { BreadcrumbItem } from '@/components/nav/PageBreadcrumbs.vue'
import PriceChart from '@/components/charts/PriceChart.vue'
import type { PriceHover } from '@/components/charts/PriceChart.vue'
import ZoomNavigator from '@/components/charts/ZoomNavigator.vue'
import type { LogicalRangeLike, SeekRequest } from '@/components/charts/navigatorMath'
import BarTable from '@/components/viewer/BarTable.vue'
import ExportPopover from '@/components/viewer/ExportPopover.vue'
import { MISSING, formatCompact, formatNumber, timestampToInstant } from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import { titleCase } from '@/theme/copy'
import { useFooterSummary } from '@/shell/useFooterSummary'
import { useEventsReload } from '@/shell/useEventsReload'
import { describeError } from '@/stores/datasetCatalog'
import { useDatasetViewerStore } from '@/stores/datasetViewer'
import { TABLE_PAGE_SIZE } from '@/viewer/barWindow'
import { PAGE_BARS, PAGE_OVERLAP_BARS, createChartPages, overlapBeforeRange, overlapRange, toLocal } from '@/viewer/chartPages'
import type { ChartPages, ChartPagesState, SeekDirection } from '@/viewer/chartPages'
import { NO_BARS_TEXT } from '@/viewer/copy'
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
// Datasets > <symbol>; the current page is the last crumb. The label is the symbol only (the Detail
// trail's dataset crumb reads the same), the whole identity is the hover title. Until the dataset is
// known, or when it is not found or failed to load, there is no dataset to name, so the last crumb
// is the screen's own name, as the Detail's is: Datasets > Viewer. Like the Back to Datasets link,
// the first crumb carries no filter state.
const crumbs = computed<BreadcrumbItem[]>(() => {
  const trail: BreadcrumbItem[] = [{ label: titleCase('Datasets'), to: { name: 'data-datasets' } }]
  const d = dataset.value
  if (status.value === 'ready' && d) {
    trail.push({ label: d.assetSymbol, hint: datasetIdentityLabel(d) })
  } else {
    trail.push({ label: titleCase('Viewer') })
  }
  return trail
})
const barCount = computed(() => (dataset.value ? int64ToNumber(dataset.value.barCount) : 0))
const hasBars = computed(() => barCount.value > 0)
const NO_BARS: readonly Bar[] = []
const problem = computed(() => rangeProblem(range.value))

// The window the range names, in the viewer's zone: the half-open [start, end) both fetches use.
const timeWindow = computed(() => toApiWindow(range.value, timeZone.value))
const windowKey = computed(() => `${timeWindow.value.start?.getTime()}|${timeWindow.value.end?.getTime()}`)

// --- the chart's page ----------------------------------------------------------------------------

type ChartStatus = 'loading' | 'ready' | 'empty' | 'error'

/** The page the chart shows: it stays on screen while the next one loads or fails. */
interface ShownPage {
  k: number
  /** Global index of the page's first bar. */
  offset: number
  start: Date
  /** The page's own bars: its block plus the look-ahead. The chart range and the navigator use these. */
  bars: readonly Bar[]
  /** The tail of the older neighbour, for the averages only; empty until it lands. */
  warmup: readonly Bar[]
}

/** How long a drag over the navigator must rest before its page is requested. */
const SEEK_DEBOUNCE_MS = 150
/**
 * Float noise allowed before a chart range counts as past the loaded page. The edge-seek test also
 * assumes lightweight-charts' default timeScale `rightOffset: 0`: a non-default offset would put the
 * library's transient range past the last bar and read as a right-edge seek that loads the next page.
 */
const EDGE_EPSILON = 1e-6

const shown = shallowRef<ShownPage | null>(null)
const bars = computed<readonly Bar[]>(() => shown.value?.bars ?? NO_BARS)
const closes = computed(() => Float64Array.from(bars.value, (bar) => bar.close))
const pageState = shallowRef<ChartPagesState | null>(null)
/** Remounts the chart for each page, so it fits (or places) its new bars. */
const chartKey = ref(0)
const visibleRange = ref<LogicalRangeLike | null>(null)
const hover = ref<PriceHover | null>(null)
const seekPending = ref(false)

let model: ChartPages | null = null
let unsubscribe: (() => void) | null = null
let seekTimer: ReturnType<typeof setTimeout> | null = null
let flushQueued = false
/** Where the window goes on page `k` once it lands (global bar indices; the leading edge is kept). */
let placement: { k: number; from: number; to: number } | null = null

const pageCount = computed(() => pageState.value?.index?.pages.length ?? 0)
const paged = computed(() => pageCount.value > 1)

// The shown bars the next page shows too, as bar indices of the page on screen; none on the newest
// page and on a single page.
const overlap = computed(() => {
  const index = pageState.value?.index
  const k = shown.value?.k
  if (!paged.value || !index || k === undefined) return null
  return overlapRange(index, k)
})

// The same, as the page on screen sees the previous page: its first bars, which the older page shows
// as its look-ahead. None on the first page and on a single page.
const overlapBefore = computed(() => {
  const index = pageState.value?.index
  const k = shown.value?.k
  if (!paged.value || !index || k === undefined) return null
  return overlapBeforeRange(index, k)
})

// The first failure that matters: the index, else the current page. The chart keeps the page it has.
const loadError = computed(() => {
  const state = pageState.value
  if (state === null) return null
  if (state.indexStatus === 'error') return describeError(state.indexError)
  if (state.current?.status === 'error') return describeError(state.pageError)
  return null
})

const chartStatus = computed<ChartStatus>(() => {
  if (problem.value !== undefined) return 'empty'
  if (shown.value !== null) return 'ready'
  const state = pageState.value
  if (state === null) return 'loading'
  if (loadError.value !== null) return 'error'
  if (state.indexStatus === 'ready' && state.index !== null && state.index.pages.length === 0) return 'empty'
  return 'loading'
})

// The navigator pulses while a page is requested: the index, the current page, or a drag waiting out
// its debounce. Preloads of the neighbours never count.
const pageLoading = computed(() => {
  const state = pageState.value
  if (seekPending.value) return true
  return state !== null && (state.indexStatus === 'loading' || state.current?.status === 'loading')
})

function clearSeekTimer(): void {
  if (seekTimer !== null) clearTimeout(seekTimer)
  seekTimer = null
  seekPending.value = false
}

/** Read the model: remember its state and put its current page on screen once it is ready. */
function syncFromModel(): void {
  if (model === null) return
  const state = model.state()
  pageState.value = state
  const page = state.current
  if (page === null || page.status !== 'ready') return
  const want = placement !== null && placement.k === page.k ? placement : null
  const index = state.index
  const local = want !== null && index !== null ? toLocal(index.pages[page.k], want.from, want.to) : null
  const current = shown.value
  if (current === null || current.k !== page.k || current.bars !== page.bars) {
    shown.value = { k: page.k, offset: page.offset, start: page.start, bars: page.bars, warmup: page.warmup }
    visibleRange.value = local
    hover.value = null
    chartKey.value += 1
    placement = null
  } else {
    // The older neighbour landed after the page: only the averages' input changes, no remount.
    if (current.warmup !== page.warmup) shown.value = { ...current, warmup: page.warmup }
    if (local !== null) {
      // Back on the page already shown (a cached page the user returned to): only the window moves.
      visibleRange.value = local
      placement = null
    }
  }
}

// The model notifies synchronously, inside the call that asked (a seek sets `placement` only after
// it returns), so the screen reads it on the next microtask.
function onModelChange(): void {
  if (flushQueued) return
  flushQueued = true
  void Promise.resolve().then(() => {
    flushQueued = false
    syncFromModel()
  })
}

function closeModel(): void {
  clearSeekTimer()
  unsubscribe?.()
  unsubscribe = null
  model?.cancel()
  model = null
  pageState.value = null
  placement = null
}

function clearPage(): void {
  shown.value = null
  visibleRange.value = null
  hover.value = null
}

function openModel(): void {
  closeModel()
  clearPage()
  const { start, end } = timeWindow.value
  const next = createChartPages()
  model = next
  unsubscribe = next.subscribe(onModelChange)
  pageState.value = next.state()
  void next.open({ datasetId: datasetId.value, start, end })
}

// Once per (dataset, window): the detail has arrived, the dataset has bars and the range is usable. A
// new window or dataset starts a new model: its cache, its index and every request in flight go with
// the old one, so a page of the old window is never shown for the new.
watch(
  [() => status.value === 'ready' && hasBars.value, datasetId, windowKey],
  ([ready]) => {
    if (!ready || problem.value !== undefined) {
      closeModel()
      clearPage()
      return
    }
    openModel()
  },
  { immediate: true },
)

// A reconnect of the events socket: the Viewer reloads its page model, drops the cache and the page
// index, and returns to the newest page (the index may have grown). Keeping the current page and
// zoom is not done yet.
useEventsReload(() => {
  void model?.reload()
})

/** Move the model to the page for the global window [from, to]; the page shown follows when it lands. */
function runSeek(from: number, to: number, direction: SeekDirection): void {
  clearSeekTimer()
  const result = model?.seek(from, to, direction)
  if (!result) return
  // Page navigation shows the whole page, so it also applies when the page is the one on screen.
  const whole = direction === 'page'
  if (result.changed || (whole && shown.value?.k !== result.k)) {
    // The range is applied when the page lands: the page on screen keeps its range until then.
    placement = { k: result.k, from: result.from, to: result.to }
  } else if (whole && shown.value !== null) {
    visibleRange.value = { from: result.from - shown.value.offset, to: result.to - shown.value.offset }
  }
}

/** The navigator's window left the loaded block (or its hatched area was clicked). */
function onSeek(request: SeekRequest): void {
  if (request.direction === 'jump' || request.direction === 'page') {
    runSeek(request.from, request.to, request.direction)
    return
  }
  // A drag fires on every pointer move: only the place it rests on is requested.
  clearSeekTimer()
  seekPending.value = true
  seekTimer = setTimeout(() => runSeek(request.from, request.to, request.direction), SEEK_DEBOUNCE_MS)
}

/**
 * The window moved (the navigator inside the page, or the chart's own scroll and zoom). Past an edge of
 * the page it is a seek, at once; inside it only tells the model the user is still moving, which holds
 * the neighbour preload back.
 */
function onRange(range: LogicalRangeLike): void {
  visibleRange.value = range
  const page = shown.value
  if (page === null || model === null) return
  clearSeekTimer()
  const last = page.bars.length - 1
  const before = range.from < -0.5 - EDGE_EPSILON
  const after = range.to > last + 0.5 + EDGE_EPSILON
  if (before && after) {
    // Zoomed out wider than a page: no page holds the window, so there is nothing to move to. The
    // model is told the user is still moving and stays on its page.
    runSeek(page.offset, page.offset + last, 'jump')
  } else if (before || after) {
    runSeek(range.from + page.offset, range.to + page.offset, before ? 'left' : 'right')
  } else {
    const from = Math.max(range.from, 0) + page.offset
    const to = Math.min(range.to, last) + page.offset
    runSeek(from, to, 'jump')
  }
}

function retry(): void {
  const state = model?.state()
  if (model === null || state === undefined) return
  if (state.indexStatus === 'error' || state.current === null) void model.reload()
  else void model.loadPage(state.current.k)
}

// --- the readout, Show Request ---------------------------------------------------------

const tableLoaded = ref(0)

// The footer carries no bar counts for the Viewer (owner ruling, tj-grna9p.118): the navigator's
// label says where the window is.
setFooterSummary(null)

onBeforeUnmount(() => {
  closeModel()
  store.reset()
  setFooterSummary(null)
})

function clock(seconds: number): string {
  return intraday.value ? dateTime(seconds * 1000) : date(seconds * 1000)
}

const showRequest = ref(false)
const requestText = computed(() => {
  const { start, end } = timeWindow.value
  const id = datasetId.value
  const pages = getDatasetBarPagesRequest(id, { start, end, size: PAGE_BARS, overlap: PAGE_OVERLAP_BARS })
  const chart = getDatasetBarsRequest(id, { start: shown.value?.start ?? start, end, limit: PAGE_BARS + PAGE_OVERLAP_BARS })
  const rows = getDatasetBarsRequest(id, { start, end, limit: TABLE_PAGE_SIZE })
  return `Pages  GET ${pages}\nChart  GET ${chart}\nRows   GET ${rows}`
})

const extent = computed(() => {
  const d = dataset.value
  if (!d?.firstBar || !d.lastBar) return null
  return { first: date(timestampToInstant(d.firstBar)), last: date(timestampToInstant(d.lastBar)) }
})
</script>

<template>
  <section class="viewer">
    <PageBreadcrumbs :items="crumbs" />

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
          <RouterLink
            class="btn"
            :to="{ name: DETAIL_ROUTE, params: { id: dataset.id } }"
            data-testid="open-details"
          >
            {{ titleCase('Details') }}
          </RouterLink>
          <ExportPopover
            :dataset="dataset"
            :range="range"
            :disabled="!hasBars"
          />
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
            <span>Could not load bars. {{ loadError?.message }}</span>
            <span
              v-if="loadError?.errorId"
              class="panel__id"
            >Error ID: {{ loadError.errorId }}</span>
            <button
              type="button"
              class="btn"
              @click="retry"
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
            <!-- A page that failed after another is on screen: the plot stays, this says why. -->
            <div
              v-if="loadError"
              class="notice notice--error"
              role="alert"
              data-testid="chart-page-error"
            >
              <span>Could not load bars. {{ loadError.message }}</span>
              <span
                v-if="loadError.errorId"
                class="panel__id"
              >Error ID: {{ loadError.errorId }}</span>
              <button
                type="button"
                class="btn"
                @click="retry"
              >
                Try Again
              </button>
            </div>
            <!-- The SMAs run over the older neighbour's last 50 bars plus the page and only the page is
                 drawn (owner rule, tj-eozqhi): the neighbour is requested with the page, so an
                 average starts at the page's first bar, off screen to the left of the first view. It
                 is not waited for: until it lands the averages' first bars are missing and fill in.
                 The earliest page has no older neighbour and its averages start late (accepted). -->
            <!-- The page on screen stays, dimmed, while another loads; its range is applied when it lands. -->
            <div
              class="chart-wrap"
              :class="{ 'chart-wrap--loading': pageLoading }"
              data-testid="chart-wrap"
            >
              <PriceChart
                :key="chartKey"
                :visible-range="visibleRange"
                :bars="bars"
                :warmup-bars="shown?.warmup ?? NO_BARS"
                :sma-periods="smaPeriods"
                :show-volume="showVolume"
                :overlap-from="overlap?.from ?? null"
                :overlap-to="overlap?.to ?? null"
                :overlap-before-from="overlapBefore?.from ?? null"
                :overlap-before-to="overlapBefore?.to ?? null"
                :session-markers="intraday"
                @update:visible-range="onRange"
                @hover="hover = $event"
              />
            </div>
            <ZoomNavigator
              :visible-range="visibleRange"
              :values="closes"
              :total-bars="paged ? pageState?.totalBars : undefined"
              :page-offset="shown?.offset ?? 0"
              :page-size="pageState?.pageSize"
              :label="paged ? (pageState?.label ?? '') : ''"
              :overlap-from="overlap?.from ?? null"
              :overlap-to="overlap?.to ?? null"
              :overlap-before-from="overlapBefore?.from ?? null"
              :overlap-before-to="overlapBefore?.to ?? null"
              :loading="pageLoading"
              @update:visible-range="onRange"
              @seek="onSeek"
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
  text-decoration: none;
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

.notice--error {
  display: flex;
  align-items: center;
  gap: 12px;
  color: var(--tj-text);
}

.chart-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.chart-wrap {
  transition: opacity 0.15s ease;
}

/* Another page is loading: the one on screen is dimmed until it lands. */
.chart-wrap--loading {
  opacity: 0.6;
}

@media (prefers-reduced-motion: reduce) {
  .chart-wrap {
    transition: none;
  }
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
