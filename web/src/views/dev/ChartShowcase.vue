<script setup lang="ts">
// Dev-only chart showcase for the owner's visual check (tj-grna9p.53). Registered by the router
// only when import.meta.env.DEV, so neither this file nor the charting libraries it pulls in reach
// the production build. All data is synthetic and deterministic.
import { computed, markRaw, ref, shallowRef } from 'vue'
import { create } from '@bufbuild/protobuf'
import { TimestampSchema } from '@bufbuild/protobuf/wkt'
import { BarSchema, type Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'

import PriceChart, { type PriceHover } from '@/components/charts/PriceChart.vue'
import ZoomNavigator from '@/components/charts/ZoomNavigator.vue'
import { VChart } from '@/components/charts/echarts'
import type { LogicalRangeLike, SeekRequest } from '@/components/charts/navigatorMath'
import { CHART_CREDITS } from '@/components/charts/credits'
import { colors } from '@/theme/tokens'

const BAR_COUNT = 10_000
const DAY = 86_400

// A small deterministic generator (mulberry32) so the showcase looks the same on every load.
function prng(seed: number): () => number {
  let a = seed
  return () => {
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function syntheticBars(count: number): Bar[] {
  const rand = prng(7)
  const bars: Bar[] = []
  let price = 100
  // 1986-01-01 UTC, stepping one calendar day per bar (weekends are not skipped: it is only a shape).
  let time = 504_921_600
  for (let i = 0; i < count; i++) {
    const open = price
    const close = Math.max(1, open * (1 + (rand() - 0.495) * 0.03))
    const high = Math.max(open, close) * (1 + rand() * 0.01)
    const low = Math.min(open, close) * (1 - rand() * 0.01)
    price = close
    bars.push(
      create(BarSchema, {
        barStart: create(TimestampSchema, { seconds: BigInt(time), nanos: 0 }),
        open,
        high,
        low,
        close,
        volume: Math.round(1e6 * (0.5 + rand())),
      }),
    )
    time += DAY
  }
  return bars
}

// The tj-x5yghe rule applied: a 10k-bar array goes in a shallowRef, and markRaw keeps it out of
// any deep ref it is later put in.
const bars = shallowRef<readonly Bar[]>(markRaw(syntheticBars(BAR_COUNT)))
const closes = computed(() => Float64Array.from(bars.value, (bar) => bar.close))

const visibleRange = ref<LogicalRangeLike | null>(null)
const hover = ref<PriceHover | null>(null)

// The pager: 189 pages of 1000 bars with 50 overlap (a 180,000-bar window), the real page rule of
// tj-eozqhi. The page's closes are synthetic; a seek loads the page holding the leading edge.
const PAGE_SIZE = 1000
const PAGE_STEP = 950
const WINDOW_BARS = 180_000
const pageStarts: number[] = []
for (let at = 0; at + PAGE_SIZE < WINDOW_BARS; at += PAGE_STEP) pageStarts.push(at)
pageStarts.push(WINDOW_BARS - PAGE_SIZE)
const pagesAll = pageStarts.length

const pageIndex = ref(pagesAll - 1)
const pageRange = ref<LogicalRangeLike | null>({ from: 300, to: 700 })
const pageOffset = computed(() => pageStarts[pageIndex.value])
const pageCloses = computed(() => {
  const rand = prng(pageIndex.value + 1)
  let price = 100 + (pageIndex.value % 40)
  return Float64Array.from({ length: PAGE_SIZE }, () => (price *= 1 + (rand() - 0.5) * 0.01))
})
const pageLabel = computed(
  () => `Page ${(pageIndex.value + 1).toLocaleString('en-US')} of ${pagesAll.toLocaleString('en-US')}`,
)

function onSeek(request: SeekRequest): void {
  // The page holding the leading edge (the edge in the direction of travel).
  const edge = request.direction === 'left' ? request.from : request.to
  let k = pageStarts.findIndex((start) => edge < start + PAGE_SIZE)
  if (k < 0) k = pagesAll - 1
  pageIndex.value = k
  const offset = pageStarts[k]
  if (request.direction === 'page') {
    // Page navigation: the whole page.
    pageRange.value = { from: 0, to: PAGE_SIZE - 1 }
    return
  }
  const width = request.to - request.from
  const from = Math.min(Math.max(request.from - offset, 0), PAGE_SIZE - 1 - width)
  pageRange.value = { from, to: from + width }
}

// A bare vue-echarts example in the app theme: stacked bars with a line and a marked cap.
const days = Array.from({ length: 14 }, (_, i) => `2026/09/${String(i + 1).padStart(2, '0')}`)
const rand = prng(11)
const scheduled = days.map(() => Math.round(40 + rand() * 30))
const manual = days.map(() => Math.round(5 + rand() * 20))
const option = {
  grid: { left: 40, right: 16, top: 36, bottom: 28 },
  tooltip: { trigger: 'axis' },
  legend: { top: 0 },
  xAxis: { type: 'category', data: days },
  yAxis: { type: 'value' },
  series: [
    { name: 'Scheduled', type: 'bar', stack: 'bars', data: scheduled },
    { name: 'Manual', type: 'bar', stack: 'bars', data: manual },
    {
      name: 'Total',
      type: 'line',
      data: scheduled.map((v, i) => v + manual[i]),
      markLine: { symbol: 'none', data: [{ yAxis: 90, name: 'Cap' }] },
    },
  ],
}
</script>

<template>
  <main class="showcase">
    <h1>Chart Showcase</h1>
    <p class="note">
      Synthetic data, dev only. {{ BAR_COUNT.toLocaleString() }} bars; drag or resize the strip, or
      scroll and zoom the chart.
    </p>

    <section>
      <h2>PriceChart + ZoomNavigator</h2>
      <p class="readout">
        <template v-if="hover">
          O {{ hover.open.toFixed(2) }} H {{ hover.high.toFixed(2) }} L {{ hover.low.toFixed(2) }} C
          {{ hover.close.toFixed(2) }} V {{ hover.volume.toLocaleString() }}
        </template>
        <template v-else>
          Hover the chart for a readout.
        </template>
      </p>
      <PriceChart
        v-model:visible-range="visibleRange"
        :bars="bars"
        @hover="hover = $event"
      />
      <ZoomNavigator
        v-model:visible-range="visibleRange"
        :values="closes"
      />
    </section>

    <section>
      <h2>ZoomNavigator as the pager (189 pages)</h2>
      <p class="note">
        Drag the window past the block's edge, press Page Up, Page Down, Home or End, or click the
        hatched area: each seeks another page.
      </p>
      <ZoomNavigator
        v-model:visible-range="pageRange"
        :values="pageCloses"
        :total-bars="WINDOW_BARS"
        :page-offset="pageOffset"
        :page-size="PAGE_SIZE"
        :label="pageLabel"
        @seek="onSeek"
      />
    </section>

    <section>
      <h2>ECharts through vue-echarts</h2>
      <VChart
        class="echart"
        :option="option"
        autoresize
      />
    </section>

    <section>
      <h2>Credits text (rendered by the About/Credits modal)</h2>
      <p
        v-for="credit in CHART_CREDITS"
        :key="credit.name"
        class="credit"
      >
        {{ credit.notice }}
      </p>
    </section>
  </main>
</template>

<style scoped>
.showcase {
  max-width: 1100px;
  margin: 0 auto;
  padding: 24px;
  color: v-bind('colors.text');
}
.note,
.readout,
.credit {
  color: v-bind('colors.text2');
  white-space: pre-line;
}
.readout {
  min-height: 1.5em;
  font-variant-numeric: tabular-nums;
}
section {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 28px;
}
.echart {
  height: 280px;
}
</style>
