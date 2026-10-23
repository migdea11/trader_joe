import { afterEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { create } from '@bufbuild/protobuf'
import { getInstanceByDom, use } from 'echarts/core'
import { SVGRenderer } from 'echarts/renderers'
import { INIT_OPTIONS_KEY } from 'vue-echarts'

import { ReadsPerDaySchema } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { echartsTheme } from '@/theme/charts'

import { ECHARTS_THEME_NAME } from './echarts'
import ReadsPerDayChart from './ReadsPerDayChart.vue'

// tj-grna9p.93, the carried .53/.80 gate item: ONE chart renders through the REAL vue-echarts (not the
// VChart stub the other chart specs use) into a real ECharts instance, with the app theme and the
// option the component builds. jsdom has no canvas, so the spec registers ECharts' SVG renderer and
// asks for it through vue-echarts' own INIT_OPTIONS_KEY, with a fixed size (jsdom has no layout).
// WHAT THIS DOES NOT SHOW: the chart's look in a browser (screenshots, tj-grna9p.94).
use([SVGRenderer])

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

const Host = defineComponent({
  props: { reads: { type: Object, required: true } },
  setup(props) {
    return () => h(ReadsPerDayChart, { reads: props.reads as never, days: 30 })
  },
})

describe('Reads per Day renders through vue-echarts', () => {
  it('a real ECharts instance holds the app theme, the stacked series per client and the day axis', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(Date.UTC(2026, 9, 14, 12))
    // jsdom has no ResizeObserver (the chart's autoresize) and no 2D canvas (ECharts measures text
    // with one): both stand-ins do nothing, the drawing itself is SVG.
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe(): void {}
        unobserve(): void {}
        disconnect(): void {}
      },
    )
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
    const reads = create(ReadsPerDaySchema, {
      days: [
        { day: '2026-10-13', clientId: 'bot-1', reads: 4n },
        { day: '2026-10-14', clientId: 'operator', reads: 1n },
      ],
    })
    const wrapper = mount(Host, {
      props: { reads },
      attachTo: document.body,
      global: { provide: { [INIT_OPTIONS_KEY as symbol]: { renderer: 'svg', width: 600, height: 240 } } },
    })
    await flushPromises()

    const host = wrapper.find('[data-testid="reads-chart"]')
    expect(host.exists()).toBe(true)
    const dom = host.element.querySelector('div') ?? host.element
    const instance = getInstanceByDom(dom as HTMLElement) ?? getInstanceByDom(host.element as HTMLElement)
    expect(instance).toBeDefined()
    // The app theme (theme/charts.ts, registered by echarts.ts) is what the instance was built with.
    // (_theme is ECharts' own field: there is no public getter for the theme.)
    expect(ECHARTS_THEME_NAME).toBe('trader_joe')
    expect((instance as unknown as { _theme?: { color?: unknown } })._theme?.color).toEqual(echartsTheme.color)
    // The series colours on the drawn model are the token palette's.
    expect((instance!.getOption() as { color?: unknown }).color).toEqual(echartsTheme.color)

    const option = instance!.getOption() as {
      series: { name: string; type: string; stack: string; data: number[] }[]
      xAxis: { data: string[] }[]
    }
    expect(option.series.map((s) => [s.name, s.type, s.stack])).toEqual([
      ['bot-1', 'bar', 'reads'],
      ['operator', 'bar', 'reads'],
    ])
    expect(option.xAxis[0]!.data).toHaveLength(30)
    expect(option.series[0]!.data.slice(-2)).toEqual([4, 0])

    // The SVG renderer drew into the page: the legend names the clients.
    const svg = host.element.querySelector('svg')
    expect(svg).not.toBeNull()
    expect(svg!.textContent).toContain('bot-1')
    expect(svg!.textContent).toContain('operator')
    wrapper.unmount()
  })
})
