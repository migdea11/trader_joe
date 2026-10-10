// ECharts setup, done once (tj-grna9p.1: ECharts 6.x through vue-echarts 8.x for every non-price
// chart; no hand-written wrapper). Importing this module registers the parts the screens use and
// the app theme; a chart component imports VChart from here, never from 'vue-echarts' directly, so
// nothing can render before registration.
//
// Only what the Data screens draw is registered, so the bundle carries the tree-shaken subset
// rather than all of ECharts: line and bar series (stacked bars, bucket bars, percentile lines,
// quota burn, source bars), the grid, tooltip, legend and dataZoom components, a mark line for the
// quota cap, and the canvas renderer. Add a part here when a screen needs it, not before.
import { BarChart, LineChart } from 'echarts/charts'
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
} from 'echarts/components'
import { registerTheme, use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'

import { ECHARTS_THEME_NAME, echartsTheme } from '@/theme/charts'

/** Everything registered with ECharts, by export name; the single place to read what is in use. */
export const REGISTERED_ECHARTS_PARTS = {
  BarChart,
  LineChart,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  CanvasRenderer,
} as const

use(Object.values(REGISTERED_ECHARTS_PARTS))
registerTheme(ECHARTS_THEME_NAME, echartsTheme)

export { VChart, ECHARTS_THEME_NAME }
