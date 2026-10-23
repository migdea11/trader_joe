<script setup lang="ts">
// Reads per Day (30 d) on the Dataset detail (tj-grna9p.89): a stacked ECharts bar per client id,
// one column per UTC day, through vue-echarts and the app theme (echarts.ts). The server counts the
// reads; the chart lays them out (readsPerDay.ts). A dataset with no reads in the window shows a
// line of text, not an empty plot.
import { computed } from 'vue'
import type { ReadsPerDay } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { ECHARTS_THEME_NAME, VChart } from '@/components/charts/echarts'
import { formatNumber } from '@/format/formatters'

import { readsPerDayModel } from './readsPerDay'

const props = defineProps<{
  reads: ReadsPerDay
  /** The window, in UTC days. */
  days: number
}>()

const model = computed(() => readsPerDayModel(props.reads, props.days))
const empty = computed(() => model.value.series.length === 0)

const option = computed(() => ({
  grid: { left: 44, right: 12, top: 32, bottom: 28 },
  tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
  legend: { top: 0, type: 'scroll' },
  xAxis: { type: 'category', data: model.value.days, axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value', minInterval: 1 },
  series: model.value.series.map((series) => ({
    name: series.clientId,
    type: 'bar',
    stack: 'reads',
    data: series.reads,
  })),
}))

const label = computed(() => {
  const total = model.value.series.reduce((sum, series) => sum + series.total, 0)
  const count = model.value.series.length
  return `Reads per day over the last ${props.days} days: ${formatNumber(total)} in all, by ${count} ${count === 1 ? 'client' : 'clients'}.`
})
</script>

<template>
  <p
    v-if="empty"
    class="chart-empty"
    data-testid="reads-empty"
  >
    No reads in the last {{ days }} days by the selected clients.
  </p>
  <VChart
    v-else
    class="reads-chart"
    :option="option"
    :theme="ECHARTS_THEME_NAME"
    :aria-label="label"
    role="img"
    autoresize
    data-testid="reads-chart"
  />
</template>

<style scoped>
.reads-chart {
  width: 100%;
  height: 240px;
}

.chart-empty {
  margin: 0;
  font-size: 13px;
  color: var(--tj-text2);
}
</style>
