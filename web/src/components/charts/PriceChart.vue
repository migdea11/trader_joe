<script setup lang="ts">
// Candles + volume over TradingView Lightweight Charts (tj-grna9p.1, tj-grna9p.53). A thin wrapper:
// data comes in as props, nothing here calls the API.
//
// Reactivity (tj-x5yghe): a 10k-bar array must never be made deeply reactive. The parent passes the
// bars in a shallowRef (or markRaw); this component reads them through toRaw, and everything it
// derives (candles, volume, indicator values) is held in a shallowRef of a markRaw'd object. The
// chart instance and its series are plain variables, never refs.
//
// int64: protobuf-es decodes the bar timestamp's seconds as bigint. int64ToNumber (the only
// bigint-to-number conversion in the UI) is called here, at the chart boundary, and nowhere else.
import {
  computed,
  markRaw,
  onBeforeUnmount,
  onMounted,
  shallowRef,
  toRaw,
  isReactive,
  useTemplateRef,
  watch,
} from 'vue'
import {
  CandlestickSeries,
  createChart,
  HistogramSeries,
  LineSeries,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type LineData,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from 'lightweight-charts'

import type { Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'

import { int64ToNumber } from '@/api/int64'
import { useFormatters } from '@/format/useFormatters'
import { colors } from '@/theme/tokens'
import { titleCase } from '@/theme/copy'
import {
  lightweightCandlestickOptions,
  lightweightChartOptions,
  lightweightLineOptions,
} from '@/theme/charts'
import { simpleMovingAverage } from './indicators'
import { sameRange, type LogicalRangeLike } from './navigatorMath'
import { sessionStarts, visibleBreaks } from './sessionBreaks'

/** What the hover readout shows for the bar under the crosshair. */
export interface PriceHover {
  /** Bar open time, seconds since the epoch. */
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
  /** Value of each configured SMA at this bar, keyed by period; absent before its window fills. */
  sma: Record<number, number | undefined>
}

const props = withDefaults(
  defineProps<{
    /** Ascending by bar_start. Pass a shallowRef or markRaw array, never a deep ref. */
    readonly bars: readonly Bar[]
    /**
     * Bars just BEFORE `bars` (the older neighbour's tail, ascending), used only to warm the moving
     * averages up: SMA and volume average run over warm-up + bars and only `bars` is drawn, so the
     * lines start at the first bar. Never drawn, never counted in the visible range. Empty (the
     * default) or late: the averages start late and fill in when it arrives.
     */
    readonly warmupBars?: readonly Bar[]
    /** SMA overlay periods, in bars. */
    readonly smaPeriods?: readonly number[]
    /** Period of the average drawn over the volume pane. 0 hides it. */
    readonly volumeAveragePeriod?: number
    /** Show the volume pane (with its average line). */
    readonly showVolume?: boolean
    /** The visible logical range, bar-index units (two-way with ZoomNavigator). */
    readonly visibleRange?: LogicalRangeLike | null
    /** Chart height in px. */
    readonly height?: number
    /**
     * The shown bars the next page shows too, as bar indices of `bars` (inclusive): a tinted band
     * labelled Overlap on the plot. Null (the default): no band.
     */
    readonly overlapFrom?: number | null
    readonly overlapTo?: number | null
    /** Hover title of the overlap band's label. */
    readonly overlapHint?: string
    /**
     * The shown bars the PREVIOUS page shows too (its look-ahead runs into this page), as bar indices
     * of `bars` (inclusive): the same band on the left. Null (the default): no band.
     */
    readonly overlapBeforeFrom?: number | null
    readonly overlapBeforeTo?: number | null
    readonly overlapBeforeHint?: string
    /**
     * Mark where each trading day ends (intraday datasets only): a thin dashed line at every gap of
     * SESSION_GAP_SECONDS or more between consecutive bars (sessionBreaks.ts). Warm-up bars get none.
     */
    readonly sessionMarkers?: boolean
  }>(),
  {
    warmupBars: () => [],
    smaPeriods: () => [20, 50],
    volumeAveragePeriod: 20,
    showVolume: true,
    visibleRange: null,
    height: 420,
    overlapFrom: null,
    overlapTo: null,
    overlapHint: 'These bars are also on the next page',
    overlapBeforeFrom: null,
    overlapBeforeTo: null,
    overlapBeforeHint: 'These bars are also on the previous page',
    sessionMarkers: false,
  },
)

const emit = defineEmits<{
  'update:visibleRange': [range: LogicalRangeLike]
  hover: [readout: PriceHover | null]
}>()

interface ChartData {
  candles: CandlestickData<UTCTimestamp>[]
  volume: HistogramData<UTCTimestamp>[]
  /** Warm-up closes then the drawn bars' closes: what the averages run over. */
  closes: Float64Array
  volumes: Float64Array
  /** Entries of `closes` / `volumes` before the first drawn bar. */
  lead: number
  /** Candle indices that start a new session (see sessionBreaks.ts); only the drawn bars count. */
  sessions: number[]
}

// Bars without a bar_start cannot be placed on the time axis and are skipped; the server always
// sets it. The warm-up bars only feed the averages' input.
function buildData(bars: readonly Bar[], warmup: readonly Bar[]): ChartData {
  const candles: CandlestickData<UTCTimestamp>[] = []
  const volume: HistogramData<UTCTimestamp>[] = []
  const closes: number[] = []
  const volumes: number[] = []
  for (const bar of warmup) {
    if (!bar.barStart) continue
    closes.push(bar.close)
    volumes.push(bar.volume)
  }
  const lead = closes.length
  for (const bar of bars) {
    if (!bar.barStart) continue
    const time = int64ToNumber(bar.barStart.seconds) as UTCTimestamp
    const up = bar.close >= bar.open
    candles.push({ time, open: bar.open, high: bar.high, low: bar.low, close: bar.close })
    // Half-opaque up/down tint so the volume bars do not outshout the candles.
    volume.push({ time, value: bar.volume, color: `${up ? colors.up : colors.down}80` })
    closes.push(bar.close)
    volumes.push(bar.volume)
  }
  const sessions = sessionStarts(candles.map((c) => c.time))
  return { candles, volume, closes: Float64Array.from(closes), volumes: Float64Array.from(volumes), lead, sessions }
}

// An SMA as line data over the candle times; the warm-up points are whitespace.
function lineData(
  candles: readonly CandlestickData<UTCTimestamp>[],
  values: Float64Array,
): Array<LineData<UTCTimestamp> | WhitespaceData<UTCTimestamp>> {
  return candles.map((c, i) => (Number.isNaN(values[i]) ? { time: c.time } : { time: c.time, value: values[i] }))
}

function readBars(): readonly Bar[] {
  const raw = toRaw(props.bars)
  if (import.meta.env.DEV && isReactive(props.bars)) {
    console.warn('PriceChart: bars is deeply reactive; pass a shallowRef or markRaw array (tj-x5yghe).')
  }
  return raw
}

function buildAll(): ChartData {
  return buildData(readBars(), toRaw(props.warmupBars))
}

const data = shallowRef<ChartData>(markRaw(buildAll()))
// Times and the date label are shown in the settings zone (override or browser zone); the zone is
// re-applied to the live chart when it changes.
const { chartLocalization, chartTimeScale, timeZone } = useFormatters()
const container = useTemplateRef<HTMLDivElement>('container')

// Chart objects live outside Vue's reactivity.
let chart: IChartApi | null = null
let candleSeries: ISeriesApi<'Candlestick'> | null = null
let volumeSeries: ISeriesApi<'Histogram'> | null = null
let volumeAverageSeries: ISeriesApi<'Line'> | null = null
let smaSeries: Array<{ period: number; series: ISeriesApi<'Line'> }> = []
// The range the parent holds: the prop as last seen, or what this component last emitted. A chart
// range is news to the parent only when it differs from this. Comparing with the prop alone is wrong
// (the prop lags an emit by a render), and lost the final range of a page move: see onRangeChange.
let known: LogicalRangeLike | null = props.visibleRange ? { ...props.visibleRange } : null
// A range or fit is queued in the chart (setVisibleLogicalRange and fitContent apply on the next
// frame, not at the call) and has not been reported yet.
let applying = false
// True while this component itself reads the chart's range: the read can make the library report
// the range it has just recomputed, which is not a user's move.
let reading = false

// The plot's overlays (overlap bands, session lines) are not chart drawings: they need only the
// visible logical range (bar-index units), the loaded bar count, and how much of the container the
// price and time axes take. A range-change handler updates both refs.
const viewRange = shallowRef<LogicalRangeLike | null>(null)
const plotInset = shallowRef({ right: 0, bottom: 0 })

// The overlap band as fractions of the plot width. A bar i is centred on logical index i and the
// visible range [from, to] puts bar `from` half a bar in from the left edge, so the plot holds
// to - from + 1 bars and bar i spans [(i - from) / n, (i + 1 - from) / n]. Not clamped: the plot
// clips what is off screen.
function bandOf(first: number | null, last: number | null): { left: number; width: number } | null {
  const count = data.value.candles.length
  if (first === null || last === null || count === 0 || last < first) return null
  const range = viewRange.value ?? { from: 0, to: count - 1 }
  const bars = range.to - range.from + 1
  if (!(bars > 0)) return null
  const left = (first - range.from) / bars
  const right = (last + 1 - range.from) / bars
  if (right <= 0 || left >= 1) return null
  return { left: left * 100, width: (right - left) * 100 }
}
const overlapBand = computed(() => bandOf(props.overlapFrom, props.overlapTo))
const overlapBeforeBand = computed(() => bandOf(props.overlapBeforeFrom, props.overlapBeforeTo))

// Session boundaries inside the view, as percentages of the plot width. Cheap: one pass over the
// (few) boundaries on every range change, never over the bars.
const sessionLines = computed(() => {
  if (!props.sessionMarkers) return []
  const sessions = data.value.sessions
  const count = data.value.candles.length
  if (sessions.length === 0 || count === 0) return []
  const range = viewRange.value ?? { from: 0, to: count - 1 }
  return visibleBreaks(sessions, range.from, range.to).map((fraction) => fraction * 100)
})
const hasOverlay = computed(
  () => overlapBand.value !== null || overlapBeforeBand.value !== null || sessionLines.value.length > 0,
)

const overlapFill = `${colors.accentTint}1f`

// The chart's range now. While a programmatic range is queued the library still holds its previous
// (default) range and reports it when asked; that report is a transient, never the parent's news.
function readRange(): LogicalRangeLike | null {
  if (!chart) return null
  reading = true
  try {
    return chart.timeScale().getVisibleLogicalRange()
  } finally {
    reading = false
  }
}

function syncView(range?: LogicalRangeLike | null): void {
  // A queued range is the view to come; the library still reports its previous one until the frame.
  viewRange.value = range ?? (applying ? props.visibleRange : null) ?? readRange() ?? props.visibleRange ?? null
  if (!chart) return
  // The axes' sizes are known to the library only; jsdom and a not-yet-laid-out chart report none.
  const right = typeof chart.priceScale === 'function' ? chart.priceScale('right').width() : 0
  const bottom = chart.timeScale().height?.() ?? 0
  const cur = plotInset.value
  if (right !== cur.right || bottom !== cur.bottom) plotInset.value = { right, bottom }
}

// The averages run over warm-up + drawn bars; only the drawn bars' points are kept.
function applyIndicators(): void {
  const d = data.value
  for (const { period, series } of smaSeries) {
    series.setData(lineData(d.candles, simpleMovingAverage(d.closes, period).subarray(d.lead)))
  }
  volumeAverageSeries?.setData(
    lineData(d.candles, simpleMovingAverage(d.volumes, props.volumeAveragePeriod).subarray(d.lead)),
  )
}

function applyData(): void {
  if (!candleSeries) return
  const d = data.value
  candleSeries.setData(d.candles)
  volumeSeries?.setData(d.volume)
  applyIndicators()
}

function rebuildIndicators(): void {
  if (!chart) return
  for (const { series } of smaSeries) chart.removeSeries(series)
  smaSeries = []
  if (volumeAverageSeries) chart.removeSeries(volumeAverageSeries)
  volumeAverageSeries = null
  // The volume pane is the series on pane 1: removing its last series removes the pane.
  if (volumeSeries && !props.showVolume) {
    chart.removeSeries(volumeSeries)
    volumeSeries = null
  } else if (!volumeSeries && props.showVolume) {
    volumeSeries = chart.addSeries(
      HistogramSeries,
      { priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false },
      1,
    )
  }

  props.smaPeriods.forEach((period, i) => {
    const series = chart!.addSeries(LineSeries, {
      ...lightweightLineOptions,
      color: colors.series[i % colors.series.length],
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
      title: `SMA ${period}`,
    })
    smaSeries.push({ period, series })
  })
  if (props.showVolume && props.volumeAveragePeriod > 0) {
    volumeAverageSeries = chart.addSeries(
      LineSeries,
      {
        color: colors.text2,
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      },
      1,
    )
  }
  const panes = chart.panes()
  panes[0]?.setStretchFactor(3)
  panes[1]?.setStretchFactor(1)
  applyData()
}

function onCrosshairMove(param: MouseEventParams): void {
  const candle = (candleSeries ? param.seriesData.get(candleSeries) : undefined) as
    | CandlestickData<Time>
    | undefined
  if (!param.time || !candle) {
    emit('hover', null)
    return
  }
  const volume = (volumeSeries ? param.seriesData.get(volumeSeries) : undefined) as
    | HistogramData<Time>
    | undefined
  const sma: Record<number, number | undefined> = {}
  for (const { period, series } of smaSeries) {
    const point = param.seriesData.get(series) as LineData<Time> | undefined
    sma[period] = point?.value
  }
  emit('hover', {
    time: param.time as number,
    open: candle.open,
    high: candle.high,
    low: candle.low,
    close: candle.close,
    volume: volume?.value ?? 0,
    sma,
  })
}

function onRangeChange(range: LogicalRangeLike | null): void {
  if (!range) return
  syncView(range)
  // Our own read of the chart while a range is queued: the library's pre-apply state, not a move.
  if (reading && applying) return
  const next = { from: range.from, to: range.to }
  // Not echoing what the parent already holds. The library also reports transient ranges of its own
  // inside the frame that applies a queued range (its default range, then the applied one), so the
  // applied range can arrive AFTER a transient one was emitted: it must then be emitted too, which is
  // why this compares with what the parent was last told and not with the (stale) prop.
  if (sameRange(next, known)) return
  known = next
  emit('update:visibleRange', next)
}

onMounted(() => {
  const el = container.value
  if (!el) return
  chart = createChart(el, {
    ...lightweightChartOptions,
    layout: {
      ...lightweightChartOptions.layout,
      // Attribution lives in the About/Credits modal (credits.ts, tj-grna9p.54), not on the chart.
      attributionLogo: false,
    },
    // Dates read YYYY/MM/DD and times are in the viewer's zone (tj-mujie8 ruling 6).
    localization: chartLocalization(),
    timeScale: { ...lightweightChartOptions.timeScale, ...chartTimeScale() },
    // Lightweight Charts observes the container itself, so resizing needs no handler of ours.
    autoSize: true,
  })
  candleSeries = chart.addSeries(CandlestickSeries, lightweightCandlestickOptions)

  rebuildIndicators()
  chart.subscribeCrosshairMove(onCrosshairMove)
  chart.timeScale().subscribeVisibleLogicalRangeChange(onRangeChange)

  applying = true
  if (props.visibleRange) chart.timeScale().setVisibleLogicalRange({ ...props.visibleRange })
  else chart.timeScale().fitContent()
  syncView()
  // Both apply in the library's next frame, which reports the result; ours follows it.
  settleApply()
})

// Clears `applying` after the chart's next frame, where a queued range has been applied and reported
// (the library's frame callback is registered before ours, so it runs first). Only the latest apply
// clears it.
let applyToken = 0
function settleApply(): void {
  const token = (applyToken += 1)
  const done = (): void => {
    if (token === applyToken) applying = false
  }
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(done)
  else setTimeout(done, 0)
}

onBeforeUnmount(() => {
  if (!chart) return
  chart.unsubscribeCrosshairMove(onCrosshairMove)
  chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRangeChange)
  chart.remove()
  chart = null
  candleSeries = null
  volumeSeries = null
  volumeAverageSeries = null
  smaSeries = []
})

// A new bars array (new identity) replaces the data; mutating one in place is not observed, which is
// the point of keeping it out of deep reactivity.
watch(
  () => props.bars,
  () => {
    data.value = markRaw(buildAll())
    applyData()
    syncView()
  },
)

// The older neighbour landing (or changing): only the averages' input changed, so the candles and
// the visible range are left alone.
watch(
  () => props.warmupBars,
  () => {
    data.value = markRaw(buildAll())
    applyIndicators()
  },
)

watch(timeZone, () => {
  chart?.applyOptions({ localization: chartLocalization(), timeScale: chartTimeScale() })
})

watch(
  () => [props.smaPeriods, props.volumeAveragePeriod, props.showVolume] as const,
  () => rebuildIndicators(),
)

watch(
  () => props.visibleRange,
  (range) => {
    known = range ? { from: range.from, to: range.to } : null
    if (!chart || !range) return
    const current = readRange()
    if (sameRange(current, range)) {
      syncView(range)
      return
    }
    applying = true
    chart.timeScale().setVisibleLogicalRange({ from: range.from, to: range.to })
    syncView(range)
    settleApply()
  },
)
</script>

<template>
  <div
    class="price-chart-frame"
    :style="{ height: `${height}px` }"
  >
    <div
      ref="container"
      class="price-chart"
    />
    <!-- The plot's decorations: the overlap bands (the shown bars a neighbouring page shows too) and
         the session boundaries. Clipped to the plot; only a band's label takes pointer events (for
         the title). -->
    <div
      v-if="hasOverlay"
      class="price-chart__plot"
      :style="{ right: `${plotInset.right}px`, bottom: `${plotInset.bottom}px` }"
      aria-hidden="true"
    >
      <div
        v-for="(left, i) in sessionLines"
        :key="`s${i}`"
        class="price-chart__session"
        data-testid="price-chart-session"
        :style="{ left: `${left}%` }"
      />
      <div
        v-if="overlapBeforeBand"
        class="price-chart__overlap price-chart__overlap--before"
        data-testid="price-chart-overlap-before"
        :style="{ left: `${overlapBeforeBand.left}%`, width: `${overlapBeforeBand.width}%` }"
      >
        <span
          class="price-chart__overlap-label"
          :title="overlapBeforeHint"
        >{{ titleCase('Overlap') }}</span>
      </div>
      <div
        v-if="overlapBand"
        class="price-chart__overlap"
        data-testid="price-chart-overlap"
        :style="{ left: `${overlapBand.left}%`, width: `${overlapBand.width}%` }"
      >
        <span
          class="price-chart__overlap-label"
          :title="overlapHint"
        >{{ titleCase('Overlap') }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.price-chart-frame {
  position: relative;
  width: 100%;
  /* Lightweight Charts stacks its canvases at z-index 1 and 2 (and paints an opaque background), so
     anything laid over the plot must sit above them; isolating keeps those indices inside the frame. */
  isolation: isolate;
}
.price-chart {
  width: 100%;
  height: 100%;
}
/* The plot area, for clipping the overlap band; it takes no pointer events. */
.price-chart__plot {
  position: absolute;
  top: 0;
  left: 0;
  overflow: hidden;
  pointer-events: none;
  z-index: 5;
}
/* The overlap: tinted, a dashed accent border on the side facing the page's own bars. */
.price-chart__overlap {
  position: absolute;
  top: 0;
  bottom: 0;
  box-sizing: border-box;
  background: v-bind('overlapFill');
  border-left: 1px dashed v-bind('colors.accentTint');
}
.price-chart__overlap-label {
  position: absolute;
  top: 8px;
  left: 8px;
  max-width: calc(100% - 16px);
  overflow: hidden;
  white-space: nowrap;
  font-size: 11px;
  color: v-bind('colors.accentTint');
  pointer-events: auto;
}
/* A session boundary: a thin dashed vertical line between the last bar of one session and the first
   of the next, over the plot but never in the way of the crosshair. */
.price-chart__session {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 0;
  border-left: 1px dashed v-bind('colors.hatch');
}
/* The overlap on the left: the dashed border faces the page's own bars, so it is on the right. */
.price-chart__overlap--before {
  border-left: 0;
  border-right: 1px dashed v-bind('colors.accentTint');
}
</style>
