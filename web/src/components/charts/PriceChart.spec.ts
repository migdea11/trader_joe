import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h, isReactive, nextTick, reactive, shallowRef } from 'vue'
import { create } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import { BarSchema, type Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'

import { useSettingsStore } from '@/stores/settings'
import { colors } from '@/theme/tokens'
import PriceChart from './PriceChart.vue'
import ZoomNavigator from './ZoomNavigator.vue'

// tj-grna9p.53 PriceChart and ZoomNavigator.
//
// WHAT THE MOCK HIDES: jsdom has no canvas, so lightweight-charts' createChart is replaced by a
// recorder. The series definitions (CandlestickSeries, ...) are the real exports. Hidden: the real
// drawing, autoSize's ResizeObserver, the library's own logical-range arithmetic (the fake returns
// whatever range was last set), and the library removing a pane when its last series goes -- the
// spec checks the series removal that triggers it, not the pane's disappearance.
interface FakeSeries {
  kind: string
  options: Record<string, unknown>
  pane: number | undefined
  setData: ReturnType<typeof vi.fn>
}

interface FakeChart {
  options: Record<string, unknown>
  series: FakeSeries[]
  removed: FakeSeries[]
  crosshair: ((param: unknown) => void) | null
  rangeHandler: ((range: { from: number; to: number } | null) => void) | null
  visible: { from: number; to: number } | null
  addSeries: ReturnType<typeof vi.fn>
  removeSeries: ReturnType<typeof vi.fn>
  applyOptions: ReturnType<typeof vi.fn>
  remove: ReturnType<typeof vi.fn>
  setVisibleLogicalRange: ReturnType<typeof vi.fn>
  fitContent: ReturnType<typeof vi.fn>
  unsubscribeCrosshairMove: ReturnType<typeof vi.fn>
  unsubscribeVisibleLogicalRangeChange: ReturnType<typeof vi.fn>
}

const charts: FakeChart[] = []

vi.mock('lightweight-charts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('lightweight-charts')>()
  const kindOf = (definition: unknown): string =>
    definition === actual.CandlestickSeries
      ? 'candle'
      : definition === actual.HistogramSeries
        ? 'histogram'
        : definition === actual.LineSeries
          ? 'line'
          : 'other'
  return {
    ...actual,
    createChart: vi.fn((_el: HTMLElement, options: Record<string, unknown>) => {
      const chart: FakeChart = {
        options,
        series: [],
        removed: [],
        crosshair: null,
        rangeHandler: null,
        visible: null,
        addSeries: vi.fn(),
        removeSeries: vi.fn(),
        applyOptions: vi.fn(),
        remove: vi.fn(),
        setVisibleLogicalRange: vi.fn(),
        fitContent: vi.fn(),
        unsubscribeCrosshairMove: vi.fn(),
        unsubscribeVisibleLogicalRangeChange: vi.fn(),
      }
      chart.addSeries.mockImplementation((definition: unknown, opts: Record<string, unknown>, pane?: number) => {
        const series: FakeSeries = { kind: kindOf(definition), options: opts, pane, setData: vi.fn() }
        chart.series.push(series)
        return series
      })
      chart.removeSeries.mockImplementation((series: FakeSeries) => {
        chart.series = chart.series.filter((s) => s !== series)
        chart.removed.push(series)
      })
      chart.setVisibleLogicalRange.mockImplementation((range: { from: number; to: number }) => {
        chart.visible = range
      })
      const timeScale = {
        subscribeVisibleLogicalRangeChange: (handler: FakeChart['rangeHandler']) => {
          chart.rangeHandler = handler
        },
        unsubscribeVisibleLogicalRangeChange: chart.unsubscribeVisibleLogicalRangeChange,
        setVisibleLogicalRange: chart.setVisibleLogicalRange,
        getVisibleLogicalRange: () => chart.visible,
        fitContent: chart.fitContent,
      }
      charts.push(chart)
      return {
        addSeries: chart.addSeries,
        removeSeries: chart.removeSeries,
        applyOptions: chart.applyOptions,
        remove: chart.remove,
        panes: () => [{ setStretchFactor: vi.fn() }, { setStretchFactor: vi.fn() }],
        subscribeCrosshairMove: (handler: FakeChart['crosshair']) => {
          chart.crosshair = handler
        },
        unsubscribeCrosshairMove: chart.unsubscribeCrosshairMove,
        timeScale: () => timeScale,
      }
    }),
  }
})

const DAY = 86_400_000
const T0 = Date.UTC(2026, 0, 5, 5)

function bars(count: number): Bar[] {
  return Array.from({ length: count }, (_, i) =>
    create(BarSchema, {
      barStart: timestampFromMs(T0 + i * DAY),
      open: 100 + i,
      high: 102 + i,
      low: 99 + i,
      close: i % 2 === 0 ? 101 + i : 99.5 + i,
      volume: 1000 + i,
    }),
  )
}

const mounted: VueWrapper[] = []

function mountChart(props: Record<string, unknown>): { wrapper: VueWrapper; chart: FakeChart } {
  const wrapper = mount(PriceChart, { props: props as never, attachTo: document.body })
  mounted.push(wrapper)
  return { wrapper, chart: charts[charts.length - 1]! }
}

function only(chart: FakeChart, kind: string): FakeSeries[] {
  return chart.series.filter((s) => s.kind === kind)
}

beforeEach(() => {
  try {
    window.localStorage.clear()
  } catch {
    // jsdom has storage
  }
  setActivePinia(createPinia())
  charts.length = 0
})

afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount())
  vi.restoreAllMocks()
})

describe('PriceChart creation', () => {
  it('turns the attribution logo off, dates read yyyy/MM/dd, and the chart sizes itself', () => {
    const { chart } = mountChart({ bars: bars(3) })
    const layout = chart.options.layout as Record<string, unknown>
    expect(layout.attributionLogo).toBe(false)
    expect((chart.options.localization as Record<string, unknown>).dateFormat).toBe('yyyy/MM/dd')
    expect(chart.options.autoSize).toBe(true)
    expect(layout.background).toEqual({ color: colors.surface })
  })

  it('draws candles, a volume pane with its average, and SMA 20 and 50 in tint-first series colours', () => {
    const { chart } = mountChart({ bars: bars(60) })
    expect(only(chart, 'candle')).toHaveLength(1)
    const [volume] = only(chart, 'histogram')
    expect(volume!.pane).toBe(1)
    const lines = only(chart, 'line')
    const sma = lines.filter((l) => typeof l.options.title === 'string')
    expect(sma.map((l) => l.options.title)).toEqual(['SMA 20', 'SMA 50'])
    expect(sma.map((l) => l.options.color)).toEqual([colors.series[0], colors.series[1]])
    expect(sma[0]!.options.color).toBe(colors.accentTint)
    const average = lines.find((l) => l.pane === 1)
    expect(average).toBeDefined()
  })

  it('crosses int64 at the boundary: candle times are numbers in seconds', () => {
    const { chart } = mountChart({ bars: bars(2) })
    const data = only(chart, 'candle')[0]!.setData.mock.lastCall![0] as Array<{ time: unknown }>
    expect(data.map((c) => c.time)).toEqual([T0 / 1000, (T0 + DAY) / 1000])
    expect(typeof data[0]!.time).toBe('number')
  })

  it('the SMA line data has whitespace for the warm-up and the fixture values after it', () => {
    const { chart } = mountChart({ bars: bars(60), smaPeriods: [20] })
    const sma = only(chart, 'line').find((l) => l.options.title === 'SMA 20')!
    const points = sma.setData.mock.lastCall![0] as Array<{ time: number; value?: number }>
    expect(points).toHaveLength(60)
    expect(points[18]!.value).toBeUndefined()
    const closes = bars(60).map((b) => b.close)
    const expected = closes.slice(0, 20).reduce((a, b) => a + b, 0) / 20
    expect(points[19]!.value).toBeCloseTo(expected, 10)
  })

  it('a bar time beyond the safe-integer range is a RangeError, never a rounded time', () => {
    const bad = bars(1)
    bad[0]!.barStart = { ...bad[0]!.barStart!, seconds: BigInt(Number.MAX_SAFE_INTEGER) + 1n }
    expect(() => mount(PriceChart, { props: { bars: bad } })).toThrow(RangeError)
  })
})

describe('PriceChart reactivity', () => {
  it('a 10k-bar array stays non-reactive and the data handed to the chart is not reactive', () => {
    // Mounted through a parent that holds the bars as the Viewer store does (shallowRef), because
    // @vue/test-utils' own props object is deeply reactive and would hide the point.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const held = shallowRef(bars(10_000))
    const Parent = defineComponent(() => () => h(PriceChart, { bars: held.value }))
    const parent = mount(Parent)
    mounted.push(parent)
    const wrapper = parent.findComponent(PriceChart)
    const chart = charts[charts.length - 1]!
    expect(isReactive(wrapper.props('bars'))).toBe(false)
    expect(isReactive(held.value)).toBe(false)
    const data = only(chart, 'candle')[0]!.setData.mock.lastCall![0]
    expect(isReactive(data)).toBe(false)
    expect(data).toHaveLength(10_000)
    expect(warn).not.toHaveBeenCalled()
  })

  it('warns in development when handed a deeply reactive array', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    mountChart({ bars: reactive(bars(3)) })
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('deeply reactive'))
  })

  it('calls setData again only for a new array identity, not for an in-place change', async () => {
    const first = bars(5)
    const { wrapper, chart } = mountChart({ bars: first })
    const candles = only(chart, 'candle')[0]!
    const before = candles.setData.mock.calls.length

    first.push(...bars(1))
    await wrapper.setProps({ bars: first })
    expect(candles.setData.mock.calls.length).toBe(before)

    await wrapper.setProps({ bars: bars(7) })
    expect(candles.setData.mock.calls.length).toBe(before + 1)
    expect(candles.setData.mock.lastCall![0]).toHaveLength(7)
  })
})

describe('PriceChart options and lifecycle', () => {
  it('re-applies the time zone through applyOptions when the setting changes', async () => {
    const { chart } = mountChart({ bars: bars(3) })
    expect(chart.applyOptions).not.toHaveBeenCalled()
    useSettingsStore().setTimeZoneOverride('Asia/Tokyo')
    await Promise.resolve()
    expect(chart.applyOptions).toHaveBeenCalledTimes(1)
    const options = chart.applyOptions.mock.lastCall![0] as {
      localization: { dateFormat: string; timeFormatter: (t: number) => string }
      timeScale: { tickMarkFormatter: (t: number, kind: number) => string }
    }
    expect(options.localization.dateFormat).toBe('yyyy/MM/dd')
    // 2026-01-05T05:00Z is 14:00 in Tokyo.
    expect(options.localization.timeFormatter(T0 / 1000)).toBe('2026/01/05 14:00')
    expect(options.timeScale.tickMarkFormatter(T0 / 1000, 3)).toBe('14:00')
  })

  it('showVolume false removes the volume series and its average; true adds them back', async () => {
    const { wrapper, chart } = mountChart({ bars: bars(30) })
    const volume = only(chart, 'histogram')[0]!
    await wrapper.setProps({ showVolume: false })
    expect(chart.removed).toContain(volume)
    expect(only(chart, 'histogram')).toHaveLength(0)
    expect(chart.series.some((s) => s.pane === 1)).toBe(false)

    await wrapper.setProps({ showVolume: true })
    expect(only(chart, 'histogram')).toHaveLength(1)
    expect(chart.series.filter((s) => s.pane === 1)).toHaveLength(2)
  })

  it('mounted with showVolume false never creates a volume pane', () => {
    const { chart } = mountChart({ bars: bars(30), showVolume: false })
    expect(chart.series.some((s) => s.pane === 1)).toBe(false)
  })

  it('disposes the chart and its subscriptions on unmount', () => {
    const { wrapper, chart } = mountChart({ bars: bars(3) })
    mounted.pop()
    wrapper.unmount()
    expect(chart.remove).toHaveBeenCalledTimes(1)
    expect(chart.unsubscribeCrosshairMove).toHaveBeenCalledWith(chart.crosshair)
    expect(chart.unsubscribeVisibleLogicalRangeChange).toHaveBeenCalledWith(chart.rangeHandler)
  })

  it('emits a hover readout for the bar under the crosshair, and null off the data', () => {
    const { wrapper, chart } = mountChart({ bars: bars(30), smaPeriods: [20] })
    const candle = only(chart, 'candle')[0]!
    const volume = only(chart, 'histogram')[0]!
    const sma = only(chart, 'line').find((l) => l.options.title === 'SMA 20')!
    const seriesData = new Map<unknown, unknown>([
      [candle, { time: 1, open: 1, high: 4, low: 0.5, close: 3 }],
      [volume, { time: 1, value: 777 }],
      [sma, { time: 1, value: 2.5 }],
    ])
    chart.crosshair!({ time: 1, seriesData })
    chart.crosshair!({ time: undefined, seriesData: new Map() })
    expect(wrapper.emitted('hover')).toEqual([
      [{ time: 1, open: 1, high: 4, low: 0.5, close: 3, volume: 777, sma: { 20: 2.5 } }],
      [null],
    ])
  })
})

describe('PriceChart visible range, two-way', () => {
  it('fits content without a range, and starts at the given range with one', () => {
    const fitted = mountChart({ bars: bars(30) }).chart
    expect(fitted.fitContent).toHaveBeenCalled()
    const ranged = mountChart({ bars: bars(30), visibleRange: { from: 3, to: 9 } }).chart
    expect(ranged.setVisibleLogicalRange).toHaveBeenCalledWith({ from: 3, to: 9 })
  })

  it('scrolling the chart emits the new range once; the echo of a range it was given is not re-emitted', async () => {
    const { wrapper, chart } = mountChart({ bars: bars(30) })
    chart.rangeHandler!({ from: 2, to: 12 })
    chart.rangeHandler!({ from: 2, to: 12 })
    expect(wrapper.emitted('update:visibleRange')).toEqual([[{ from: 2, to: 12 }]])

    await wrapper.setProps({ visibleRange: { from: 5, to: 15 } })
    expect(chart.setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: 5, to: 15 })
    chart.rangeHandler!({ from: 5, to: 15 })
    expect(wrapper.emitted('update:visibleRange')).toHaveLength(1)
    chart.rangeHandler!(null)
    expect(wrapper.emitted('update:visibleRange')).toHaveLength(1)
  })

  it('the chart echoing the range it was mounted with is not emitted back', () => {
    const { wrapper, chart } = mountChart({ bars: bars(30), visibleRange: { from: 3, to: 9 } })
    chart.rangeHandler!({ from: 3, to: 9 })
    expect(wrapper.emitted('update:visibleRange')).toBeUndefined()
    chart.rangeHandler!({ from: 4, to: 9 })
    expect(wrapper.emitted('update:visibleRange')).toEqual([[{ from: 4, to: 9 }]])
  })

  it('a prop range equal to the chart range is not set again', async () => {
    const { wrapper, chart } = mountChart({ bars: bars(30) })
    chart.visible = { from: 1, to: 8 }
    await wrapper.setProps({ visibleRange: { from: 1, to: 8 } })
    expect(chart.setVisibleLogicalRange).not.toHaveBeenCalled()
  })

  it('the navigator drives the chart and the chart drives the navigator', async () => {
    // Parent wiring as in the Viewer: one range shared by v-model on both.
    const values = Array.from({ length: 101 }, (_, i) => i)
    const nav = mount(ZoomNavigator, { props: { values, visibleRange: { from: 0, to: 100 } } })
    const { wrapper, chart } = mountChart({ bars: bars(101), visibleRange: { from: 0, to: 100 } })

    await nav.find('.zoom-navigator__handle').trigger('keydown', { key: 'ArrowRight' })
    const fromNav = nav.emitted('update:visibleRange')![0]![0] as { from: number; to: number }
    expect(fromNav.from).toBeCloseTo(2, 10)
    expect(fromNav.to).toBeCloseTo(100, 10)
    await wrapper.setProps({ visibleRange: fromNav })
    expect(chart.setVisibleLogicalRange).toHaveBeenLastCalledWith(fromNav)

    chart.rangeHandler!({ from: 50, to: 75 })
    const fromChart = wrapper.emitted('update:visibleRange')!.at(-1)![0] as { from: number; to: number }
    await nav.setProps({ visibleRange: fromChart })
    const win = nav.find('.zoom-navigator__window').attributes('style')
    expect(win).toContain('left: 50%')
    expect(win).toContain('width: 25%')
    nav.unmount()
  })
})

// jsdom has no PointerEvent constructor; a MouseEvent of the pointer type carries clientX, which is
// all the navigator reads (pointerId only feeds setPointerCapture, stubbed per element).
async function pointer(el: Element, type: string, clientX: number): Promise<void> {
  el.dispatchEvent(new MouseEvent(type, { clientX, bubbles: true }))
  await nextTick()
}

describe('ZoomNavigator', () => {
  const values = Array.from({ length: 101 }, (_, i) => i)

  it('shows the whole range with no visible range', () => {
    const nav = mount(ZoomNavigator, { props: { values } })
    const style = nav.find('.zoom-navigator__window').attributes('style')
    expect(style).toContain('left: 0%')
    expect(style).toContain('width: 100%')
  })

  it('arrow keys pan the window and resize from either handle', async () => {
    const nav = mount(ZoomNavigator, { props: { values, visibleRange: { from: 20, to: 40 } } })
    await nav.find('.zoom-navigator__window').trigger('keydown', { key: 'ArrowLeft' })
    const [startHandle, endHandle] = nav.findAll('.zoom-navigator__handle')
    await endHandle!.trigger('keydown', { key: 'ArrowRight' })
    await startHandle!.trigger('keydown', { key: 'Enter' })
    const ranges = nav.emitted('update:visibleRange')!.map(([r]) => r as { from: number; to: number })
    expect(ranges).toHaveLength(2)
    expect(ranges[0]!.from).toBeCloseTo(18, 10)
    expect(ranges[0]!.to).toBeCloseTo(38, 10)
    expect(ranges[1]!.from).toBeCloseTo(20, 10)
    expect(ranges[1]!.to).toBeCloseTo(42, 10)
  })

  it('a pointer drag of the window moves it by the dragged fraction', async () => {
    const nav = mount(ZoomNavigator, {
      props: { values, visibleRange: { from: 20, to: 40 } },
      attachTo: document.body,
    })
    vi.spyOn(nav.element, 'getBoundingClientRect').mockReturnValue({ width: 1000 } as DOMRect)
    const windowEl = nav.find('.zoom-navigator__window')
    ;(windowEl.element as HTMLElement).setPointerCapture = vi.fn()
    await pointer(windowEl.element, 'pointerdown', 100)
    await pointer(windowEl.element, 'pointermove', 200)
    await pointer(windowEl.element, 'pointerup', 200)
    await pointer(windowEl.element, 'pointermove', 900)
    const ranges = nav.emitted('update:visibleRange')!.map(([r]) => r as { from: number; to: number })
    expect(ranges).toHaveLength(1)
    expect(ranges[0]!.from).toBeCloseTo(30, 10)
    expect(ranges[0]!.to).toBeCloseTo(50, 10)
    nav.unmount()
  })

  it('a drag of the start handle never narrows below five bars', async () => {
    const nav = mount(ZoomNavigator, {
      props: { values, visibleRange: { from: 20, to: 40 } },
      attachTo: document.body,
    })
    vi.spyOn(nav.element, 'getBoundingClientRect').mockReturnValue({ width: 1000 } as DOMRect)
    const handle = nav.findAll('.zoom-navigator__handle')[0]!
    ;(handle.element as HTMLElement).setPointerCapture = vi.fn()
    await pointer(handle.element, 'pointerdown', 200)
    await pointer(handle.element, 'pointermove', 900)
    const range = nav.emitted('update:visibleRange')!.at(-1)![0] as { from: number; to: number }
    expect(range.to).toBeCloseTo(40, 10)
    expect(range.to - range.from).toBeCloseTo(5, 10)
    nav.unmount()
  })

  it.each([0, 1])('with %i values draws no window and does not respond', async (count) => {
    const nav = mount(ZoomNavigator, { props: { values: Array.from({ length: count }, () => 1) } })
    expect(nav.find('.zoom-navigator__window').exists()).toBe(false)
    expect(nav.emitted('update:visibleRange')).toBeUndefined()
  })

  it('draws a strip for two values and none for zero', () => {
    expect(mount(ZoomNavigator, { props: { values: [1, 2] } }).find('path').attributes('d')).toMatch(/^M.*Z$/)
    expect(mount(ZoomNavigator, { props: { values: [] } }).find('path').attributes('d')).toBe('')
  })
})
