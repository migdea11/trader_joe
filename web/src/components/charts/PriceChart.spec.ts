import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
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
  // With fakeLibrary.defer: a set range waits here for libraryFrame(), as the real one waits a frame.
  pending: { from: number; to: number } | null
  // Called inside getVisibleLogicalRange: the real library recomputes a stale range on read and fires
  // its range-change handlers synchronously, inside the read.
  onRead: (() => void) | null
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
// Off by default: the fake applies a set range at the call, which every older test relies on.
const fakeLibrary = { defer: false }

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
        pending: null,
        onRead: null,
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
        if (fakeLibrary.defer) chart.pending = range
        else chart.visible = range
      })
      const timeScale = {
        subscribeVisibleLogicalRangeChange: (handler: FakeChart['rangeHandler']) => {
          chart.rangeHandler = handler
        },
        unsubscribeVisibleLogicalRangeChange: chart.unsubscribeVisibleLogicalRangeChange,
        setVisibleLogicalRange: chart.setVisibleLogicalRange,
        getVisibleLogicalRange: () => {
          chart.onRead?.()
          return chart.visible
        },
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

// Intraday hourly bars, one session per entry of `sizes`, each session on its own day from 13:00 UTC:
// consecutive bars 1 h apart inside a session, at least 14 h apart across a boundary.
const HOUR = 3_600_000
function sessionBars(sizes: readonly number[]): Bar[] {
  const out: Bar[] = []
  sizes.forEach((size, day) => {
    for (let j = 0; j < size; j += 1) {
      const i = out.length
      out.push(
        create(BarSchema, {
          barStart: timestampFromMs(Date.UTC(2026, 0, 5 + day, 13) + j * HOUR),
          open: 100 + i,
          high: 102 + i,
          low: 99 + i,
          close: 101 + i,
          volume: 1000 + i,
        }),
      )
    }
  })
  return out
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
  fakeLibrary.defer = false
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

// 2b6e122 (owner, real app 2026-10-13): a page move ended zoomed in. The library applies a set range in
// its next animation frame and, inside that frame, reports a transient range of its own (its default
// bar spacing) BEFORE the applied one. The chart compared reports with the prop, which lags an emit by a
// render, so the applied range was swallowed as an echo and the parent kept the transient. The fix
// compares with `known` (what the parent was last given or told), drops a report the component's own
// read provokes while an apply is queued, and clears that state one frame later (settleApply).
//
// requestAnimationFrame is stubbed with a queue so the frame is explicit; libraryFrame() plays the
// library's frame (transient, then the queued range) and then our callbacks, in the order the browser
// runs them (the library's callback was registered first).
describe('PriceChart page move: transient then applied range (2b6e122)', () => {
  type Range = { from: number; to: number }
  let frames: Array<() => void> = []

  beforeEach(() => {
    frames = []
    vi.stubGlobal('requestAnimationFrame', (cb: () => void) => frames.push(cb))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  /** The library's state becomes `range` and it reports it. */
  function report(chart: FakeChart, range: Range): void {
    chart.visible = range
    chart.rangeHandler!(range)
  }
  /** Runs the frame callbacks queued so far (not ones they queue). */
  function ourFrame(): void {
    frames.splice(0).forEach((cb) => cb())
  }
  /** The library's frame: its transient, then the queued range; then ours. */
  function libraryFrame(chart: FakeChart, transient: Range): void {
    report(chart, transient)
    if (chart.pending) report(chart, chart.pending)
    chart.pending = null
    ourFrame()
  }
  const emits = (w: VueWrapper) => (w.emitted('update:visibleRange') ?? []).map(([r]) => r as Range)

  it('mounted with a range: the transient then the applied report leaves the parent on the applied range', () => {
    const { wrapper, chart } = mountChart({ bars: bars(30), visibleRange: { from: 0, to: 29 } })
    chart.rangeHandler!({ from: 10, to: 29 })
    chart.rangeHandler!({ from: 0, to: 29 })
    expect(emits(wrapper).at(-1)).toEqual({ from: 0, to: 29 })
  })

  it('the parent taking the last emitted range does not re-apply the transient', async () => {
    fakeLibrary.defer = true
    const { wrapper, chart } = mountChart({ bars: bars(30), visibleRange: { from: 0, to: 29 } })
    libraryFrame(chart, { from: 10, to: 29 })
    expect(chart.visible).toEqual({ from: 0, to: 29 })
    // Vue batches the parent's v-model writes: it renders once, with the last emitted range.
    await wrapper.setProps({ visibleRange: emits(wrapper).at(-1)! })
    expect(chart.setVisibleLogicalRange.mock.calls).toEqual([[{ from: 0, to: 29 }]])
    expect(chart.setVisibleLogicalRange).not.toHaveBeenCalledWith({ from: 10, to: 29 })
    expect(chart.visible).toEqual({ from: 0, to: 29 })
  })

  it('a report the component\'s own read provokes while an apply is queued is not emitted', async () => {
    fakeLibrary.defer = true
    const { wrapper, chart } = mountChart({ bars: bars(30), visibleRange: { from: 0, to: 29 } })
    // The mount's apply is still queued (no frame yet). The next prop change reads the chart first,
    // and the library answers that read by reporting its pre-apply (default) range.
    chart.onRead = () => {
      chart.onRead = null
      report(chart, { from: 10, to: 29 })
    }
    await wrapper.setProps({ visibleRange: { from: 5, to: 20 } })
    expect(chart.onRead).toBeNull() // the read did happen
    expect(wrapper.emitted('update:visibleRange')).toBeUndefined()
    expect(chart.setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: 5, to: 20 })
  })

  it('a user move after the frame is emitted: the suppression ends with the apply', () => {
    fakeLibrary.defer = true
    const { wrapper, chart } = mountChart({ bars: bars(30), visibleRange: { from: 0, to: 29 } })
    libraryFrame(chart, { from: 0, to: 29 })
    expect(wrapper.emitted('update:visibleRange')).toBeUndefined()
    // The user scrolls: nothing is queued, so it is news to the parent.
    report(chart, { from: 3, to: 12 })
    expect(emits(wrapper)).toEqual([{ from: 3, to: 12 }])
  })

  it('rapid successive moves: only the latest apply\'s frame ends the suppression', async () => {
    fakeLibrary.defer = true
    const { wrapper, chart } = mountChart({ bars: bars(30), visibleRange: { from: 0, to: 29 } })
    await wrapper.setProps({ visibleRange: { from: 5, to: 20 } })
    expect(frames).toHaveLength(2)
    // The mount's frame callback runs first; the second apply is still queued.
    frames.shift()!()
    chart.onRead = () => {
      chart.onRead = null
      report(chart, { from: 10, to: 29 })
    }
    await wrapper.setProps({ visibleRange: { from: 6, to: 21 } })
    expect(chart.onRead).toBeNull()
    expect(wrapper.emitted('update:visibleRange')).toBeUndefined()
  })

  it('unmounted before the frame: the late callback is harmless and nothing is emitted', () => {
    fakeLibrary.defer = true
    const { wrapper, chart } = mountChart({ bars: bars(30), visibleRange: { from: 0, to: 29 } })
    mounted.pop()
    wrapper.unmount()
    expect(() => ourFrame()).not.toThrow()
    expect(chart.remove).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('update:visibleRange')).toBeUndefined()
  })

  it('on mount the data is set before the range, and a mount either sets a range or fits, never both', () => {
    const ranged = mountChart({ bars: bars(30), visibleRange: { from: 0, to: 29 } }).chart
    const candles = only(ranged, 'candle')[0]!
    expect(candles.setData).toHaveBeenCalled()
    expect(ranged.setVisibleLogicalRange).toHaveBeenCalledTimes(1)
    expect(candles.setData.mock.invocationCallOrder[0]!).toBeLessThan(
      ranged.setVisibleLogicalRange.mock.invocationCallOrder[0]!,
    )
    expect(ranged.fitContent).not.toHaveBeenCalled()

    const fitted = mountChart({ bars: bars(30) }).chart
    expect(fitted.fitContent).toHaveBeenCalledTimes(1)
    expect(fitted.setVisibleLogicalRange).not.toHaveBeenCalled()
    expect(only(fitted, 'candle')[0]!.setData.mock.invocationCallOrder[0]!).toBeLessThan(
      fitted.fitContent.mock.invocationCallOrder[0]!,
    )
  })
})

// 2b6e122: the overlays sat UNDER the library's canvases, which it stacks at z-index 1 and 2 with an
// opaque background. jsdom cannot paint or stack, and Vitest does not process a component's CSS, so
// this is a contract on the source's <style> block, read from disk: the frame isolates a stacking
// context and every overlay sits above 2. Whether they are visible is only checkable in a browser.
describe('PriceChart overlay stacking (style contract)', () => {
  const source = readFileSync(resolve(import.meta.dirname, 'PriceChart.vue'), 'utf8')
  const style = /<style[^>]*>([\s\S]*?)<\/style>/.exec(source)![1]!
  function rule(selector: string): Record<string, string> {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const body = new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(style)
    expect(body, `no rule for ${selector}`).not.toBeNull()
    const declarations: Record<string, string> = {}
    for (const line of body![1]!.replace(/\/\*[\s\S]*?\*\//g, '').split(';')) {
      const at = line.indexOf(':')
      if (at > 0) declarations[line.slice(0, at).trim()] = line.slice(at + 1).trim()
    }
    return declarations
  }

  it('the frame isolates its own stacking context', () => {
    expect(rule('.price-chart-frame').isolation).toBe('isolate')
  })

  it('.price-chart__plot is positioned and stacks above the library\'s canvases (z-index >= 3)', () => {
    const declarations = rule('.price-chart__plot')
    expect(declarations.position).toBe('absolute')
    expect(Number(declarations['z-index'])).toBeGreaterThanOrEqual(3)
  })

  // f8b620b (owner: "remove the previous and next buttons from the graph") dropped the chevron badge;
  // 0ede08c (owner: "there should only be one overlap highlight") dropped the hatched cut edge. The plot
  // layer lets every pointer through to the crosshair, and neither rule is left behind.
  it('the plot layer takes no pointer events, and no cut-edge or badge rule is left', () => {
    expect(rule('.price-chart__plot')['pointer-events']).toBe('none')
    expect(style).not.toMatch(/price-chart__cut/)
    expect(style).not.toMatch(/price-chart__badge/)
    expect(style).not.toMatch(/repeating-linear-gradient/)
  })

  // The only rule painting with the hatch colour is the session line: the hatch itself lives on the
  // navigator (ZoomNavigator.spec.ts), never on the plot.
  it('colors.hatch is used by the session line only', () => {
    const uses = style.match(/colors\.hatch/g) ?? []
    expect(uses).toHaveLength(1)
    expect(rule('.price-chart__session')['border-left']).toContain('colors.hatch')
  })

  it('a session line and the left band are inside the raised plot layer', () => {
    const { wrapper } = mountChart({
      bars: sessionBars([10, 10]),
      sessionMarkers: true,
      overlapBeforeFrom: 0,
      overlapBeforeTo: 4,
      visibleRange: { from: 0, to: 19 },
    })
    for (const id of ['price-chart-session', 'price-chart-overlap-before']) {
      const el = wrapper.find(`[data-testid="${id}"]`)
      expect(el.exists(), id).toBe(true)
      expect(el.element.parentElement!.classList.contains('price-chart__plot'), id).toBe(true)
    }
  })

  it('the overlap band is inside the raised plot layer', () => {
    const { wrapper } = mountChart({ bars: bars(30), overlapFrom: 20, overlapTo: 29, visibleRange: { from: 0, to: 29 } })
    const band = wrapper.find('[data-testid="price-chart-overlap"]')
    expect(band.element.parentElement!.classList.contains('price-chart__plot')).toBe(true)
  })
})

// 0ede08c (owner 2026-10-13: "You removed the next/prev buttons from the graph, but left the hashed dark
// area, there should only be one overlap highlight"): the hatched cut edge of tj-grna9p.123 is gone,
// with its props continuesBefore / continuesAfter. The plot's only decorations are the overlap bands and
// the session lines; the hatch lives on the navigator alone (ZoomNavigator.spec.ts). f8b620b had already
// removed the .129 chevron strip and badge: the chart is no pager of any kind, the navigator's arrows and
// keys are (DatasetViewer.spec.ts).
describe('PriceChart has no cut edge (one overlap highlight)', () => {
  const cuts = (w: VueWrapper) => w.findAll('[data-testid^="price-chart-cut"], .price-chart__cut')
  const removedBadges = (w: VueWrapper) =>
    w.findAll('[data-testid="price-chart-more-before"], [data-testid="price-chart-more-after"], .price-chart__badge')
  // Every element on the frame that is not the library's container: the overlays, and what is in them.
  const frameChildren = (w: VueWrapper) => Array.from(w.element.children) as HTMLElement[]

  // The ranges the cut edge used to show on: both edge bars in view, the first only, the last only,
  // scrolled past either end, zoomed in past both, and the default (no range yet).
  const ranges: Array<{ from: number; to: number } | null> = [
    null,
    { from: -2, to: 31 },
    { from: 0, to: 20 },
    { from: 10, to: 29 },
    { from: 12, to: 31.5 },
    { from: -20, to: 5 },
    { from: 5, to: 20 },
  ]

  it('declares no continuesBefore / continuesAfter (nor their hints)', () => {
    const declared = Object.keys((PriceChart as { props?: Record<string, unknown> }).props ?? {})
    expect(declared).toContain('overlapFrom') // the read works: props are declared as an object
    for (const name of ['continuesBefore', 'continuesAfter', 'continuesBeforeHint', 'continuesAfterHint']) {
      expect(declared, name).not.toContain(name)
    }
    const { wrapper } = mountChart({ bars: bars(30) })
    expect(Object.keys(wrapper.props())).not.toContain('continuesBefore')
    expect(Object.keys(wrapper.props())).not.toContain('continuesAfter')
  })

  it('no cut strip under any range, with or without bands, session lines or warm-up, nor on an empty chart', async () => {
    const all = sessionBars([10, 10, 10])
    const setups: Array<Record<string, unknown>> = [
      { bars: bars(30) },
      { bars: bars(30), overlapFrom: 20, overlapTo: 29, overlapBeforeFrom: 0, overlapBeforeTo: 4 },
      { bars: all.slice(5), warmupBars: all.slice(0, 5), sessionMarkers: true, overlapFrom: 15, overlapTo: 24 },
      { bars: [] },
    ]
    for (const setup of setups) {
      for (const visibleRange of ranges) {
        const { wrapper, chart } = mountChart({ ...setup, visibleRange })
        expect(cuts(wrapper), JSON.stringify(visibleRange)).toHaveLength(0)
        // And after the user scrolls the chart to each of the other ranges.
        for (const next of ranges) {
          if (!next) continue
          chart.rangeHandler!(next)
          await nextTick()
          expect(cuts(wrapper), `${JSON.stringify(visibleRange)} -> ${JSON.stringify(next)}`).toHaveLength(0)
          expect(removedBadges(wrapper)).toHaveLength(0)
        }
      }
    }
  })

  it('a stray continues-before / continues-after from a caller draws nothing', () => {
    const { wrapper } = mountChart({ bars: bars(30), continuesBefore: true, continuesAfter: true, visibleRange: { from: -2, to: 31 } })
    expect(cuts(wrapper)).toHaveLength(0)
    expect(frameChildren(wrapper)).toHaveLength(1) // the library's container alone: no overlay at all
  })

  it('the frame holds the library\'s container and the plot layer, nothing else', () => {
    const { wrapper } = mountChart({
      bars: sessionBars([10, 10, 10]),
      sessionMarkers: true,
      overlapBeforeFrom: 0,
      overlapBeforeTo: 4,
      overlapFrom: 25,
      overlapTo: 29,
      visibleRange: { from: -2, to: 31 },
    })
    expect(frameChildren(wrapper).map((el) => el.className)).toEqual(['price-chart', 'price-chart__plot'])
  })

  it('the only decorations in .price-chart__plot are the two overlap bands and the session lines', async () => {
    const { wrapper, chart } = mountChart({
      bars: sessionBars([10, 10, 10]),
      sessionMarkers: true,
      overlapBeforeFrom: 0,
      overlapBeforeTo: 4,
      overlapFrom: 25,
      overlapTo: 29,
      visibleRange: { from: -2, to: 31 },
    })
    const kinds = () =>
      Array.from(wrapper.find('.price-chart__plot').element.children).map(
        (el) => (el as HTMLElement).dataset.testid,
      )
    expect(kinds()).toEqual(['price-chart-session', 'price-chart-session', 'price-chart-overlap-before', 'price-chart-overlap'])
    // Each band holds its label and nothing more.
    for (const id of ['price-chart-overlap-before', 'price-chart-overlap']) {
      const band = wrapper.find(`[data-testid="${id}"]`)
      expect(Array.from(band.element.children).map((el) => el.className), id).toEqual(['price-chart__overlap-label'])
    }
    // Zoomed in past both edges (where the cut edge used to hide too): the bands are off screen, the
    // session lines stay, nothing else; zoomed between the boundaries the layer itself goes.
    chart.rangeHandler!({ from: 8, to: 22 })
    await nextTick()
    expect(kinds()).toEqual(['price-chart-session', 'price-chart-session'])
    chart.rangeHandler!({ from: 12, to: 18 })
    await nextTick()
    expect(wrapper.find('.price-chart__plot').exists()).toBe(false)
  })

  it('no button anywhere on the chart, and nothing to click: no page-navigate, no range change', async () => {
    const { wrapper } = mountChart({ bars: bars(30), overlapFrom: 20, overlapTo: 29, overlapBeforeFrom: 0, overlapBeforeTo: 4, visibleRange: { from: -2, to: 31 } })
    expect(wrapper.findAll('button')).toHaveLength(0)
    expect(wrapper.findAll('[tabindex]')).toHaveLength(0)
    await wrapper.find('.price-chart__plot').trigger('click')
    expect(wrapper.emitted('page-navigate')).toBeUndefined()
    expect(wrapper.emitted('update:visibleRange')).toBeUndefined()
    // The event is not even declared, so a parent listener could never fire.
    expect((PriceChart as { emits?: unknown }).emits).not.toContain('page-navigate')
  })

  it('the hint props are the overlap bands\' only: the chevron hints are gone', () => {
    const { wrapper } = mountChart({ bars: bars(30) })
    expect(wrapper.props()).toMatchObject({
      overlapHint: 'These bars are also on the next page',
      overlapBeforeHint: 'These bars are also on the previous page',
      overlapFrom: null,
      overlapTo: null,
      overlapBeforeFrom: null,
      overlapBeforeTo: null,
      sessionMarkers: false,
    })
    expect(Object.keys(wrapper.props())).not.toContain('continuesBeforeHint')
    expect(Object.keys(wrapper.props())).not.toContain('continuesAfterHint')
  })
})

// f8b620b (owner: "only the right (latest) side of the graph has the overlap highlight"): the bars the
// PREVIOUS page shows too get the same band on the left (testid price-chart-overlap-before), from
// overlapBeforeFrom / overlapBeforeTo, with the same geometry as the right band.
describe('PriceChart overlap band on the left', () => {
  const left = (w: VueWrapper) => w.find('[data-testid="price-chart-overlap-before"]')
  const right = (w: VueWrapper) => w.find('[data-testid="price-chart-overlap"]')
  const pct = (style: string | undefined, prop: 'left' | 'width') =>
    Number(new RegExp(`${prop}: ([\\d.e-]+)%`).exec(style ?? '')![1])

  it('none by default, and the right band alone does not draw it', () => {
    expect(left(mountChart({ bars: bars(30) }).wrapper).exists()).toBe(false)
    const { wrapper } = mountChart({ bars: bars(30), overlapFrom: 20, overlapTo: 29, visibleRange: { from: 0, to: 29 } })
    expect(right(wrapper).exists()).toBe(true)
    expect(left(wrapper).exists()).toBe(false)
  })

  it('only when both overlapBefore props are set; the right band is then absent unless set too', () => {
    expect(left(mountChart({ bars: bars(30), overlapBeforeFrom: 0 }).wrapper).exists()).toBe(false)
    expect(left(mountChart({ bars: bars(30), overlapBeforeTo: 9 }).wrapper).exists()).toBe(false)
    const { wrapper } = mountChart({ bars: bars(30), overlapBeforeFrom: 0, overlapBeforeTo: 9, visibleRange: { from: 0, to: 29 } })
    expect(left(wrapper).exists()).toBe(true)
    expect(right(wrapper).exists()).toBe(false)
  })

  it('sits over the page\'s first bars, from the visible range, and follows a scroll', async () => {
    const { wrapper, chart } = mountChart({ bars: bars(30), overlapBeforeFrom: 0, overlapBeforeTo: 9, visibleRange: { from: 0, to: 29 } })
    let style = left(wrapper).attributes('style')
    expect(pct(style, 'left')).toBeCloseTo(0, 9)
    expect(pct(style, 'width')).toBeCloseTo((10 / 30) * 100, 9)
    chart.rangeHandler!({ from: 5, to: 24 })
    await nextTick()
    style = left(wrapper).attributes('style')
    expect(pct(style, 'left')).toBeCloseTo((-5 / 20) * 100, 9)
    expect(pct(style, 'width')).toBeCloseTo(50, 9)
    chart.rangeHandler!({ from: 10, to: 29 })
    await nextTick()
    expect(left(wrapper).exists()).toBe(false)
  })

  it('both bands at once on a middle page, each with its own hint naming its page', () => {
    const { wrapper } = mountChart({
      bars: bars(30),
      overlapBeforeFrom: 0,
      overlapBeforeTo: 4,
      overlapFrom: 25,
      overlapTo: 29,
      visibleRange: { from: 0, to: 29 },
    })
    expect(left(wrapper).text()).toBe('Overlap')
    expect(left(wrapper).find('span').attributes('title')).toBe('These bars are also on the previous page')
    expect(right(wrapper).find('span').attributes('title')).toBe('These bars are also on the next page')
    expect(left(wrapper).classes()).toContain('price-chart__overlap--before')
    expect(right(wrapper).classes()).not.toContain('price-chart__overlap--before')
    expect(left(wrapper).element.parentElement!.getAttribute('aria-hidden')).toBe('true')
  })

  it('no left band on an empty chart or for a backwards range', () => {
    expect(left(mountChart({ bars: [], overlapBeforeFrom: 0, overlapBeforeTo: 5 }).wrapper).exists()).toBe(false)
    expect(left(mountChart({ bars: bars(30), overlapBeforeFrom: 9, overlapBeforeTo: 0 }).wrapper).exists()).toBe(false)
  })
})

// f8b620b (owner: "session transitions, so those should have some marker to make it clear where a day
// ends"): with sessionMarkers a dashed line (testid price-chart-session) at the left edge of every DRAWN
// bar that starts a session (a gap of >= 4 h from the bar before, sessionBreaks.ts), placed with the
// overlap bands' geometry: bar i's left edge is (i - from) / (to - from + 1) of the plot. Warm-up bars
// never contribute. Off by default, because a daily chart would otherwise mark every bar.
describe('PriceChart session markers', () => {
  const lines = (w: VueWrapper) => w.findAll('[data-testid="price-chart-session"]')
  const leftOf = (style: string | undefined) => Number(/left: ([\d.e-]+)%/.exec(style ?? '')![1])
  const positions = (w: VueWrapper) => lines(w).map((l) => leftOf(l.attributes('style')))

  it('none when sessionMarkers is false (the default), even with session gaps in the bars', () => {
    expect(lines(mountChart({ bars: sessionBars([10, 10, 10]), visibleRange: { from: 0, to: 29 } }).wrapper)).toHaveLength(0)
    expect(lines(mountChart({ bars: sessionBars([10, 10, 10]), sessionMarkers: false }).wrapper)).toHaveLength(0)
  })

  it('one line per session boundary in view, at the left edge of the session\'s first bar', () => {
    const { wrapper } = mountChart({ bars: sessionBars([10, 10, 10]), sessionMarkers: true, visibleRange: { from: 0, to: 29 } })
    expect(lines(wrapper)).toHaveLength(2)
    const at = positions(wrapper)
    expect(at[0]).toBeCloseTo((10 / 30) * 100, 9)
    expect(at[1]).toBeCloseTo((20 / 30) * 100, 9)
  })

  it('fewer lines after the range narrows, and they move with it; back when it widens', async () => {
    const { wrapper, chart } = mountChart({ bars: sessionBars([10, 10, 10]), sessionMarkers: true, visibleRange: { from: 0, to: 29 } })
    chart.rangeHandler!({ from: 12, to: 29 })
    await nextTick()
    expect(positions(wrapper)).toHaveLength(1)
    expect(positions(wrapper)[0]).toBeCloseTo((8 / 18) * 100, 9)
    // A boundary exactly at the view's left edge is the edge, not a line.
    chart.rangeHandler!({ from: 20, to: 29 })
    await nextTick()
    expect(lines(wrapper)).toHaveLength(0)
    chart.rangeHandler!({ from: 0, to: 29 })
    await nextTick()
    expect(lines(wrapper)).toHaveLength(2)
  })

  it('a daily series would mark every bar: the gating is the caller\'s (the Viewer passes intraday only)', () => {
    const { wrapper } = mountChart({ bars: bars(5), sessionMarkers: true, visibleRange: { from: 0, to: 4 } })
    expect(lines(wrapper)).toHaveLength(4)
  })

  it('a prop toggle adds and removes the lines without recreating the chart', async () => {
    const { wrapper } = mountChart({ bars: sessionBars([10, 10]), visibleRange: { from: 0, to: 19 } })
    expect(lines(wrapper)).toHaveLength(0)
    await wrapper.setProps({ sessionMarkers: true })
    expect(lines(wrapper)).toHaveLength(1)
    await wrapper.setProps({ sessionMarkers: false })
    expect(lines(wrapper)).toHaveLength(0)
    expect(charts).toHaveLength(1)
  })

  it('a boundary at a band\'s edge sits exactly on that edge, on both sides', () => {
    // Sessions start at bars 10 and 20; the left band ends at bar 9, the right band starts at bar 20.
    const { wrapper } = mountChart({
      bars: sessionBars([10, 10, 10]),
      sessionMarkers: true,
      overlapBeforeFrom: 0,
      overlapBeforeTo: 9,
      overlapFrom: 20,
      overlapTo: 29,
      visibleRange: { from: 3, to: 26 },
    })
    const pct = (style: string | undefined, prop: 'left' | 'width') =>
      Number(new RegExp(`${prop}: ([\\d.e-]+)%`).exec(style ?? '')![1])
    const leftBand = wrapper.find('[data-testid="price-chart-overlap-before"]').attributes('style')
    const rightBand = wrapper.find('[data-testid="price-chart-overlap"]').attributes('style')
    const [first, second] = positions(wrapper)
    expect(first).toBeCloseTo(pct(leftBand, 'left') + pct(leftBand, 'width'), 9)
    expect(second).toBeCloseTo(pct(rightBand, 'left'), 9)
  })

  it('warm-up bars never make a line: not for their own gaps, not for the gap into the page', () => {
    // The warm-up is two sessions of 5 bars, the page two sessions of 10 that start a session after it.
    const all = sessionBars([5, 5, 10, 10])
    const { wrapper } = mountChart({
      bars: all.slice(10),
      warmupBars: all.slice(0, 10),
      sessionMarkers: true,
      visibleRange: { from: 0, to: 19 },
    })
    // Only the page's own boundary, at page bar 10 (not 20, as it would be counted over warm-up + page).
    expect(positions(wrapper)).toHaveLength(1)
    expect(positions(wrapper)[0]).toBeCloseTo((10 / 20) * 100, 9)
  })

  it('no lines on an empty chart', () => {
    expect(lines(mountChart({ bars: [], sessionMarkers: true }).wrapper)).toHaveLength(0)
  })
})

// tj-grna9p.129: the shown bars the NEXT page shows too are a tinted band labelled Overlap on the plot.
// Its position follows the visible logical range: bar i spans [(i - from) / n, (i + 1 - from) / n] of the
// plot, n = to - from + 1. Hidden when none of its bars is on screen.
describe('PriceChart overlap band', () => {
  const band = (w: VueWrapper) => w.find('[data-testid="price-chart-overlap"]')
  const pct = (style: string | undefined, prop: 'left' | 'width') =>
    Number(new RegExp(`${prop}: ([\\d.e-]+)%`).exec(style ?? '')![1])

  it('none by default', () => {
    expect(band(mountChart({ bars: bars(30) }).wrapper).exists()).toBe(false)
  })

  it('sits over its bars at the right of the page, from the visible range', async () => {
    const { wrapper, chart } = mountChart({ bars: bars(30), overlapFrom: 20, overlapTo: 29, visibleRange: { from: 0, to: 29 } })
    let style = band(wrapper).attributes('style')
    expect(pct(style, 'left')).toBeCloseTo((20 / 30) * 100, 9)
    expect(pct(style, 'width')).toBeCloseTo((10 / 30) * 100, 9)
    expect(pct(style, 'left') + pct(style, 'width')).toBeCloseTo(100, 9)
    chart.rangeHandler!({ from: 10, to: 29 })
    await nextTick()
    style = band(wrapper).attributes('style')
    expect(pct(style, 'left')).toBeCloseTo(50, 9)
    expect(pct(style, 'width')).toBeCloseTo(50, 9)
  })

  it('carries the label Overlap with its hint, and the band itself is decorative', () => {
    const { wrapper } = mountChart({ bars: bars(30), overlapFrom: 20, overlapTo: 29, visibleRange: { from: 0, to: 29 } })
    expect(band(wrapper).text()).toBe('Overlap')
    expect(band(wrapper).find('span').attributes('title')).toBe('These bars are also on the next page')
    expect(band(wrapper).element.parentElement!.getAttribute('aria-hidden')).toBe('true')
  })

  it('hidden while its bars are off screen, back when they return', async () => {
    const { wrapper, chart } = mountChart({ bars: bars(30), overlapFrom: 20, overlapTo: 29, visibleRange: { from: 0, to: 29 } })
    chart.rangeHandler!({ from: 0, to: 15 })
    await nextTick()
    expect(band(wrapper).exists()).toBe(false)
    chart.rangeHandler!({ from: 0, to: 19.6 })
    await nextTick()
    expect(band(wrapper).exists()).toBe(true)
  })

  it('no band on an empty chart or for a backwards range of bars', () => {
    expect(band(mountChart({ bars: [], overlapFrom: 0, overlapTo: 5 }).wrapper).exists()).toBe(false)
    expect(band(mountChart({ bars: bars(30), overlapFrom: 10, overlapTo: 5 }).wrapper).exists()).toBe(false)
  })
})

// tj-grna9p.124 (owner ruling 2026-10-13 on tj-eozqhi): the averages of a page run over the older
// neighbour's last 50 bars plus the page, and only the page is drawn, so they start at the page's
// first bar. Without the warm-up an SMA 50 starts at bar 49.
describe('PriceChart warm-up bars', () => {
  // 110 consecutive daily bars: the first 50 are the warm-up, the last 60 the page.
  const series = bars(110)
  const warmup = series.slice(0, 50)
  const page = series.slice(50)
  const mean = (values: number[]) => values.reduce((a, b) => a + b, 0) / values.length
  const closes = series.map((b) => b.close)
  const volumes = series.map((b) => b.volume)
  type Point = { time: number; value?: number }
  const sma = (chart: FakeChart, period: number) =>
    only(chart, 'line').find((l) => l.options.title === `SMA ${period}`)!
  const volumeAverage = (chart: FakeChart) => only(chart, 'line').find((l) => l.pane === 1)!

  it('the averages are computed over warm-up + page; only the page is drawn', () => {
    const { chart } = mountChart({ bars: page, warmupBars: warmup, smaPeriods: [20, 50], volumeAveragePeriod: 20 })
    const candles = only(chart, 'candle')[0]!.setData.mock.lastCall![0] as Point[]
    expect(candles).toHaveLength(60)
    expect(candles[0]!.time).toBe(int(page[0]!))
    const sma50 = sma(chart, 50).setData.mock.lastCall![0] as Point[]
    expect(sma50).toHaveLength(60)
    expect(sma50.map((p) => p.time)).toEqual(candles.map((c) => c.time))
    // At the page's first bar the SMA 50 already holds bars 1..50 of the series.
    expect(sma50[0]!.value).toBeCloseTo(mean(closes.slice(1, 51)), 10)
    expect(sma50[59]!.value).toBeCloseTo(mean(closes.slice(60, 110)), 10)
    const sma20 = sma(chart, 20).setData.mock.lastCall![0] as Point[]
    expect(sma20[0]!.value).toBeCloseTo(mean(closes.slice(31, 51)), 10)
    const vol = volumeAverage(chart).setData.mock.lastCall![0] as Point[]
    expect(vol).toHaveLength(60)
    expect(vol[0]!.value).toBeCloseTo(mean(volumes.slice(31, 51)), 10)
  })

  it('with no warm-up the SMA 50 starts at the page\'s bar 49', () => {
    const { chart } = mountChart({ bars: page, smaPeriods: [50] })
    const sma50 = sma(chart, 50).setData.mock.lastCall![0] as Point[]
    expect(sma50.slice(0, 49).every((p) => p.value === undefined)).toBe(true)
    expect(sma50[49]!.value).toBeCloseTo(mean(closes.slice(50, 100)), 10)
  })

  it('a warm-up that arrives late resets only the indicator series: no candle reset, no range move, no remount', async () => {
    const { wrapper, chart } = mountChart({ bars: page, smaPeriods: [50], visibleRange: { from: 10, to: 40 } })
    const candles = only(chart, 'candle')[0]!
    const volume = only(chart, 'histogram')[0]!
    const candleCalls = candles.setData.mock.calls.length
    const volumeCalls = volume.setData.mock.calls.length
    const rangeCalls = chart.setVisibleLogicalRange.mock.calls.length
    await wrapper.setProps({ warmupBars: warmup })
    expect(charts).toHaveLength(1)
    expect(candles.setData.mock.calls.length).toBe(candleCalls)
    expect(volume.setData.mock.calls.length).toBe(volumeCalls)
    expect(chart.setVisibleLogicalRange.mock.calls.length).toBe(rangeCalls)
    expect(chart.fitContent).not.toHaveBeenCalled()
    const sma50 = sma(chart, 50).setData.mock.lastCall![0] as Point[]
    expect(sma50).toHaveLength(60)
    expect(sma50[0]!.value).toBeCloseTo(mean(closes.slice(1, 51)), 10)
    expect((volumeAverage(chart).setData.mock.lastCall![0] as Point[])[0]!.value).toBeDefined()
  })

  // Was "towards the cut edge" until 0ede08c removed it; the overlap band's default geometry (no range
  // yet: bars 0..count - 1) is the same count, so it pins the same thing. Counted over warm-up + page
  // (110), bars 50..59 would sit at 50/110 of the plot, not at the page's right end.
  it('the warm-up bars never count towards the overlap band or the drawn length', () => {
    const { wrapper } = mountChart({ bars: page, warmupBars: warmup, overlapFrom: 50, overlapTo: 59 })
    const style = wrapper.find('[data-testid="price-chart-overlap"]').attributes('style') ?? ''
    const pct = (prop: 'left' | 'width') => Number(new RegExp(`${prop}: ([\\d.e-]+)%`).exec(style)![1])
    expect(pct('left')).toBeCloseTo((50 / 60) * 100, 9)
    expect(pct('width')).toBeCloseTo((10 / 60) * 100, 9)
  })
})

function int(bar: Bar): number {
  return Number(bar.barStart!.seconds)
}

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
