<script setup lang="ts">
// When Each Dataset Was Last Read (tj-grna9p.62): one bar per bucket of whole UTC days since the last
// read by the selected clients, plus the datasets never read. The server returns the buckets; the
// chart only draws them (usageCharts.ts), through vue-echarts and the app theme.
import { computed } from 'vue'
import type { LastReadBuckets } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { ECHARTS_THEME_NAME, VChart } from '@/components/charts/echarts'
import { formatNumber } from '@/format/formatters'

import { lastReadBars } from './usageCharts'

const props = defineProps<{ buckets: LastReadBuckets }>()

const bars = computed(() => lastReadBars(props.buckets))
const total = computed(() => bars.value.reduce((sum, bar) => sum + bar.value, 0))

const option = computed(() => ({
  grid: { left: 44, right: 12, top: 16, bottom: 28 },
  tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
  xAxis: { type: 'category', data: bars.value.map((bar) => bar.label) },
  yAxis: { type: 'value', minInterval: 1 },
  series: [{ name: 'Datasets', type: 'bar', data: bars.value.map((bar) => bar.value) }],
}))

const label = computed(
  () =>
    `Datasets by time since last read: ${bars.value.map((bar) => `${bar.label} ${formatNumber(bar.value)}`).join(', ')}.`,
)
</script>

<template>
  <p
    v-if="total === 0"
    class="chart-empty"
    data-testid="last-read-empty"
  >
    No datasets to place by last read.
  </p>
  <VChart
    v-else
    class="usage-chart"
    :option="option"
    :theme="ECHARTS_THEME_NAME"
    :aria-label="label"
    role="img"
    autoresize
    data-testid="last-read-chart"
  />
</template>

<style scoped>
.usage-chart {
  width: 100%;
  height: 240px;
}

.chart-empty {
  margin: 0;
  font-size: 13px;
  color: var(--tj-text2);
}
</style>
