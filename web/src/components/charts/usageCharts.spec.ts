import { afterEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { mount } from '@vue/test-utils'
import { create } from '@bufbuild/protobuf'

import { DataSource, Feed } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { LastReadBucketsSchema, StorageCurrentSchema } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { sourceFeedLabel } from '@/catalog/labels'
import { formatBytes } from '@/format/formatters'

import LastReadChart from './LastReadChart.vue'
import StorageBySourceChart from './StorageBySourceChart.vue'
import { bucketLabel, lastReadBars, storageBars } from './usageCharts'

// tj-grna9p.62: the Usage tab's two bar charts. The data (usageCharts.ts) is plain; the components are
// mounted with VChart replaced by a stub that keeps its props (no canvas in jsdom).
// WHAT THE STUB HIDES: the charts actually drawing (screenshots, tj-grna9p.94).
vi.mock('@/components/charts/echarts', () => ({
  ECHARTS_THEME_NAME: 'trader_joe',
  VChart: defineComponent({
    name: 'VChartStub',
    props: { option: { type: Object, required: true }, theme: { type: [String, Object], default: undefined } },
    setup(_props, { attrs }) {
      return () => h('div', { ...attrs, 'data-stub': 'vchart' })
    },
  }),
}))

afterEach(() => {
  vi.restoreAllMocks()
})

// The server's buckets (store tj-grna9p.64): [0,1), [1,8), [8,31), [31,91), [91, open).
const BUCKETS = create(LastReadBucketsSchema, {
  buckets: [
    { minDays: 0, maxDays: 1, datasets: 3n },
    { minDays: 1, maxDays: 8, datasets: 5n },
    { minDays: 8, maxDays: 31, datasets: 0n },
    { minDays: 31, maxDays: 91, datasets: 2n },
    { minDays: 91, datasets: 7n },
  ],
  neverRead: 4n,
})

const STORAGE = create(StorageCurrentSchema, {
  totalBytes: 6_000_000n,
  bySource: [
    { source: DataSource.ALPACA_API, feed: Feed.IEX, estBytes: 1_000_000n },
    { source: DataSource.ALPACA_API, feed: Feed.SIP, estBytes: 5_000_000n },
  ],
})

describe('bucketLabel', () => {
  it.each([
    [0, 1, 'Today'],
    [1, 2, '1 d'],
    [1, 8, '1-7 d'],
    [8, 31, '8-30 d'],
    [31, 91, '31-90 d'],
    [91, undefined, '91+ d'],
  ] as const)('[%s, %s) reads %s: the server bounds, half-open, never a word like Stale', (min, max, label) => {
    expect(bucketLabel(min, max)).toBe(label)
  })
})

describe('lastReadBars', () => {
  it('the buckets in the server order, then Never; counts become numbers; nothing recomputed', () => {
    expect(lastReadBars(BUCKETS)).toEqual([
      { label: 'Today', value: 3 },
      { label: '1-7 d', value: 5 },
      { label: '8-30 d', value: 0 },
      { label: '31-90 d', value: 2 },
      { label: '91+ d', value: 7 },
      { label: 'Never', value: 4 },
    ])
  })

  it('no buckets is still a Never bar', () => {
    expect(lastReadBars(create(LastReadBucketsSchema, {}))).toEqual([{ label: 'Never', value: 0 }])
  })
})

describe('storageBars', () => {
  it('one bar per source and feed, by the shared label, largest first', () => {
    expect(storageBars(STORAGE)).toEqual([
      { label: sourceFeedLabel(DataSource.ALPACA_API, Feed.SIP), value: 5_000_000 },
      { label: sourceFeedLabel(DataSource.ALPACA_API, Feed.IEX), value: 1_000_000 },
    ])
  })

  it('a tie is ordered by label, so the order is stable', () => {
    const tie = create(StorageCurrentSchema, {
      bySource: [
        { source: DataSource.ALPACA_API, feed: Feed.SIP, estBytes: 1n },
        { source: DataSource.ALPACA_API, feed: Feed.IEX, estBytes: 1n },
      ],
    })
    const labels = storageBars(tie).map((bar) => bar.label)
    expect(labels).toEqual([...labels].sort((a, b) => a.localeCompare(b)))
  })
})

describe('LastReadChart', () => {
  it('draws the bars in the app theme, labelled with every bucket for assistive tech', () => {
    const chart = mount(LastReadChart, { props: { buckets: BUCKETS } })
    const stub = chart.findComponent({ name: 'VChartStub' })
    expect(stub.props('theme')).toBe('trader_joe')
    const option = stub.props('option') as { xAxis: { data: string[] }; series: { type: string; data: number[] }[] }
    expect(option.xAxis.data).toEqual(['Today', '1-7 d', '8-30 d', '31-90 d', '91+ d', 'Never'])
    expect(option.series[0]!.type).toBe('bar')
    expect(option.series[0]!.data).toEqual([3, 5, 0, 2, 7, 4])
    const el = chart.find('[data-testid="last-read-chart"]')
    expect(el.attributes('role')).toBe('img')
    expect(el.attributes('aria-label')).toBe(
      'Datasets by time since last read: Today 3, 1-7 d 5, 8-30 d 0, 31-90 d 2, 91+ d 7, Never 4.',
    )
  })

  it('nothing to place is a line of text, not an empty plot', () => {
    const chart = mount(LastReadChart, { props: { buckets: create(LastReadBucketsSchema, {}) } })
    expect(chart.find('[data-testid="last-read-empty"]').text()).toBe('No datasets to place by last read.')
    expect(chart.find('[data-stub="vchart"]').exists()).toBe(false)
  })
})

describe('StorageBySourceChart', () => {
  it('draws estimated bytes per source in the app theme, in decimal units, labelled', () => {
    const chart = mount(StorageBySourceChart, { props: { storage: STORAGE } })
    const stub = chart.findComponent({ name: 'VChartStub' })
    expect(stub.props('theme')).toBe('trader_joe')
    const option = stub.props('option') as {
      yAxis: { data: string[] }
      series: { data: number[] }[]
      tooltip: { valueFormatter: (v: number) => string }
    }
    expect(option.series[0]!.data).toEqual([5_000_000, 1_000_000])
    expect(option.tooltip.valueFormatter(5_000_000)).toBe(`${formatBytes(5_000_000)} (estimated)`)
    const label = chart.find('[data-testid="storage-chart"]').attributes('aria-label')
    expect(label).toContain('Estimated storage by source:')
    expect(label).toContain(`${sourceFeedLabel(DataSource.ALPACA_API, Feed.SIP)} ${formatBytes(5_000_000)}`)
  })

  it('no source reported is a line of text', () => {
    const chart = mount(StorageBySourceChart, { props: { storage: create(StorageCurrentSchema, {}) } })
    expect(chart.find('[data-testid="storage-empty"]').text()).toBe('No storage is reported.')
  })
})
