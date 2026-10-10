// Chart theming built from the same tokens as the components, so charts and components never
// diverge (tj-grna9p.60). Both objects are plain data: this file imports neither charting library
// (they are added with the chart tasks). Whoever mounts a chart registers the ECharts theme once
// (`echarts.registerTheme(ECHARTS_THEME_NAME, echartsTheme)`) and passes the Lightweight Charts
// options to createChart.
//
// Accent rule (tokens.ts): chart lines are shape-only, so the first series colour is the accent
// TINT, never the fill; colors.series[0] is that tint.
import { colors, fonts } from './tokens'

export const ECHARTS_THEME_NAME = 'trader_joe'

const axis = {
  axisLine: { lineStyle: { color: colors.line } },
  axisTick: { lineStyle: { color: colors.line } },
  axisLabel: { color: colors.text2 },
  splitLine: { lineStyle: { color: colors.line } },
  nameTextStyle: { color: colors.text2 },
}

export const echartsTheme = {
  color: [...colors.series],
  backgroundColor: 'transparent',
  textStyle: { color: colors.text2, fontFamily: fonts.sans },
  title: {
    textStyle: { color: colors.text, fontFamily: fonts.sans },
    subtextStyle: { color: colors.text2 },
  },
  legend: { textStyle: { color: colors.text2 } },
  tooltip: {
    backgroundColor: colors.raised,
    borderColor: colors.line,
    textStyle: { color: colors.text },
  },
  categoryAxis: axis,
  valueAxis: axis,
  timeAxis: axis,
  logAxis: axis,
  // Candles: up and down are the gain and loss colours.
  candlestick: {
    itemStyle: {
      color: colors.up,
      color0: colors.down,
      borderColor: colors.up,
      borderColor0: colors.down,
    },
  },
}

export const lightweightChartOptions = {
  layout: {
    background: { color: colors.surface },
    textColor: colors.text2,
    fontFamily: fonts.sans,
  },
  grid: {
    vertLines: { color: colors.line },
    horzLines: { color: colors.line },
  },
  rightPriceScale: { borderColor: colors.line },
  timeScale: { borderColor: colors.line },
  crosshair: {
    vertLine: { color: colors.text3, labelBackgroundColor: colors.raised },
    horzLine: { color: colors.text3, labelBackgroundColor: colors.raised },
  },
}

export const lightweightCandlestickOptions = {
  upColor: colors.up,
  downColor: colors.down,
  borderUpColor: colors.up,
  borderDownColor: colors.down,
  wickUpColor: colors.up,
  wickDownColor: colors.down,
}

export const lightweightLineOptions = {
  color: colors.series[0],
  lineWidth: 2,
}
