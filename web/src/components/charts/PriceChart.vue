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
import { markRaw, onBeforeUnmount, onMounted, shallowRef, toRaw, isReactive, useTemplateRef, watch } from 'vue'
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
import {
  lightweightCandlestickOptions,
  lightweightChartOptions,
  lightweightLineOptions,
} from '@/theme/charts'
import { simpleMovingAverage } from './indicators'
import { sameRange, type LogicalRangeLike } from './navigatorMath'

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
  }>(),
  {
    smaPeriods: () => [20, 50],
    volumeAveragePeriod: 20,
    showVolume: true,
    visibleRange: null,
    height: 420,
  },
)

const emit = defineEmits<{
  'update:visibleRange': [range: LogicalRangeLike]
  hover: [readout: PriceHover | null]
}>()

interface ChartData {
  candles: CandlestickData<UTCTimestamp>[]
  volume: HistogramData<UTCTimestamp>[]
  closes: Float64Array
  volumes: Float64Array
}

// Bars without a bar_start cannot be placed on the time axis and are skipped; the server always
// sets it.
function buildData(bars: readonly Bar[]): ChartData {
  const candles: CandlestickData<UTCTimestamp>[] = []
  const volume: HistogramData<UTCTimestamp>[] = []
  const closes: number[] = []
  const volumes: number[] = []
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
  return { candles, volume, closes: Float64Array.from(closes), volumes: Float64Array.from(volumes) }
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

const data = shallowRef<ChartData>(markRaw(buildData(readBars())))
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
let lastEmitted: LogicalRangeLike | null = null

function applyData(): void {
  if (!candleSeries) return
  const d = data.value
  candleSeries.setData(d.candles)
  volumeSeries?.setData(d.volume)
  for (const { period, series } of smaSeries) {
    series.setData(lineData(d.candles, simpleMovingAverage(d.closes, period)))
  }
  volumeAverageSeries?.setData(
    lineData(d.candles, simpleMovingAverage(d.volumes, props.volumeAveragePeriod)),
  )
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
  const next = { from: range.from, to: range.to }
  // Echo suppression: a range we just applied from the prop is not news to the parent.
  if (sameRange(next, lastEmitted) || sameRange(next, props.visibleRange)) return
  lastEmitted = next
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

  if (props.visibleRange) chart.timeScale().setVisibleLogicalRange(props.visibleRange)
  else chart.timeScale().fitContent()
})

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
    data.value = markRaw(buildData(readBars()))
    applyData()
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
    if (!chart || !range) return
    const current = chart.timeScale().getVisibleLogicalRange()
    if (sameRange(current, range)) return
    lastEmitted = { from: range.from, to: range.to }
    chart.timeScale().setVisibleLogicalRange(range)
  },
)
</script>

<template>
  <div
    ref="container"
    class="price-chart"
    :style="{ height: `${height}px` }"
  />
</template>

<style scoped>
.price-chart {
  width: 100%;
}
</style>
