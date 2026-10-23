<script setup lang="ts">
// Storage by Source, Current (tj-grna9p.62): estimated bytes now, one bar per source and feed, from the
// server's StorageCurrent. An estimate, in decimal units (kB, MB, GB), and said so.
import { computed } from 'vue'
import type { StorageCurrent } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { ECHARTS_THEME_NAME, VChart } from '@/components/charts/echarts'
import { formatBytes } from '@/format/formatters'

import { storageBars } from './usageCharts'

const props = defineProps<{ storage: StorageCurrent }>()

const bars = computed(() => storageBars(props.storage))

const option = computed(() => ({
  grid: { left: 96, right: 24, top: 8, bottom: 24 },
  tooltip: {
    trigger: 'axis',
    axisPointer: { type: 'shadow' },
    valueFormatter: (value: number) => `${formatBytes(value)} (estimated)`,
  },
  // Horizontal bars, largest first at the top.
  yAxis: { type: 'category', inverse: true, data: bars.value.map((bar) => bar.label) },
  xAxis: { type: 'value', axisLabel: { formatter: (value: number) => formatBytes(value) } },
  series: [{ name: 'Estimated size', type: 'bar', data: bars.value.map((bar) => bar.value) }],
}))

const label = computed(
  () => `Estimated storage by source: ${bars.value.map((bar) => `${bar.label} ${formatBytes(bar.value)}`).join(', ')}.`,
)
</script>

<template>
  <p
    v-if="bars.length === 0"
    class="chart-empty"
    data-testid="storage-empty"
  >
    No storage is reported.
  </p>
  <VChart
    v-else
    class="usage-chart"
    :option="option"
    :theme="ECHARTS_THEME_NAME"
    :aria-label="label"
    role="img"
    autoresize
    data-testid="storage-chart"
  />
</template>

<style scoped>
.usage-chart {
  width: 100%;
  height: 200px;
}

.chart-empty {
  margin: 0;
  font-size: 13px;
  color: var(--tj-text2);
}
</style>
