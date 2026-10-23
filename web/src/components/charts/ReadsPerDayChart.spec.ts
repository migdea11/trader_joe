import { afterEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { mount } from '@vue/test-utils'
import { create } from '@bufbuild/protobuf'

import { ReadsPerDaySchema } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import ReadsPerDayChart from './ReadsPerDayChart.vue'

// tj-grna9p.89: Reads per Day (30 d). vue-echarts needs a canvas jsdom does not have, so VChart is
// replaced by a stub that keeps its props: the spec reads the ECharts option the component builds.
// WHAT THE STUB HIDES: the chart actually drawing (screenshots, tj-grna9p.94).
vi.mock('@/components/charts/echarts', () => ({
  ECHARTS_THEME_NAME: 'trader_joe',
  VChart: defineComponent({
    name: 'VChartStub',
    props: { option: { type: Object, required: true }, theme: { type: [String, Object], default: undefined } },
    setup(props, { attrs }) {
      return () => h('div', { ...attrs, 'data-stub': 'vchart' })
    },
  }),
}))

afterEach(() => {
  vi.useRealTimers()
})

function reads(entries: [string, string, bigint][]) {
  return create(ReadsPerDaySchema, { days: entries.map(([day, clientId, count]) => ({ day, clientId, reads: count })) })
}

describe('ReadsPerDayChart', () => {
  it('no reads in the window is a line of text, not an empty plot', () => {
    const chart = mount(ReadsPerDayChart, { props: { reads: reads([]), days: 30 } })
    expect(chart.find('[data-testid="reads-empty"]').text()).toBe('No reads in the last 30 days by the selected clients.')
    expect(chart.find('[data-stub="vchart"]').exists()).toBe(false)
  })

  it('a stacked bar per client over the day axis, labelled for assistive tech', () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(Date.UTC(2026, 9, 14, 12))
    const chart = mount(ReadsPerDayChart, {
      props: {
        reads: reads([
          ['2026-10-13', 'bot-1', 4n],
          ['2026-10-14', 'operator', 1n],
        ]),
        days: 30,
      },
    })
    const stub = chart.findComponent({ name: 'VChartStub' })
    const option = stub.props('option') as {
      xAxis: { type: string; data: string[] }
      series: { name: string; type: string; stack: string; data: number[] }[]
    }
    expect(option.xAxis.type).toBe('category')
    expect(option.xAxis.data).toHaveLength(30)
    expect(option.xAxis.data.at(-1)).toBe('2026-10-14')
    expect(option.series.map((s) => [s.name, s.type, s.stack])).toEqual([
      ['bot-1', 'bar', 'reads'],
      ['operator', 'bar', 'reads'],
    ])
    expect(option.series[0]!.data.slice(-2)).toEqual([4, 0])
    expect(option.series[1]!.data.slice(-2)).toEqual([0, 1])
    const el = chart.find('[data-testid="reads-chart"]')
    expect(el.attributes('role')).toBe('img')
    expect(el.attributes('aria-label')).toBe('Reads per day over the last 30 days: 5 in all, by 2 clients.')
  })

  it('draws in the app ECharts theme (built from the tokens), never the library default', () => {
    const chart = mount(ReadsPerDayChart, { props: { reads: reads([['2026-10-14', 'bot-1', 1n]]), days: 30 } })
    expect(chart.findComponent({ name: 'VChartStub' }).props('theme')).toBe('trader_joe')
  })

  it('one client is said in the singular', () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(Date.UTC(2026, 9, 14, 12))
    const chart = mount(ReadsPerDayChart, { props: { reads: reads([['2026-10-14', 'bot-1', 1n]]), days: 30 } })
    expect(chart.find('[data-testid="reads-chart"]').attributes('aria-label')).toBe(
      'Reads per day over the last 30 days: 1 in all, by 1 client.',
    )
  })
})
