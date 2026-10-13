import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'

import ZoomNavigator from './ZoomNavigator.vue'

// tj-grna9p.122: the zoom navigator as the PAGER (decision tj-eozqhi, fixed blocks of 2026-10-13).
// With `totalBars` the strip is the whole From/To window in bars, the loaded page a block at
// `pageOffset`, everything else hatched; a move inside the block emits update:visibleRange (LOCAL to
// the page), a move out of it emits `seek` (GLOBAL bars, with a direction). Its behaviour without
// paging props is pinned in PriceChart.spec.ts and again below.
//
// WHAT THE STUBS HIDE: jsdom has no layout. getBoundingClientRect is stubbed to a 1,000 px track at
// x = 0 (so a pixel is a thousandth of the strip), there is no ResizeObserver, and pointer events are
// MouseEvents of the pointer type (jsdom has no PointerEvent). Nothing is drawn; the spec reads the
// inline styles and attributes the component computes.

const TRACK_PX = 1000
const mounted: VueWrapper[] = []

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    width: TRACK_PX,
    left: 0,
    right: TRACK_PX,
    top: 0,
    bottom: 56,
    height: 56,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect)
})

afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount())
  vi.restoreAllMocks()
})

const closes = (n: number) => Array.from({ length: n }, (_, i) => 100 + Math.sin(i / 10))

function nav(props: Record<string, unknown>): VueWrapper {
  const wrapper = mount(ZoomNavigator, { props: props as never, attachTo: document.body })
  mounted.push(wrapper)
  return wrapper
}

/** The newest page of a 2,500-bar window: offset 1,500, 1,000 bars, window local [100, 300]. */
function newestPage(extra: Record<string, unknown> = {}): VueWrapper {
  return nav({
    values: closes(1000),
    totalBars: 2500,
    pageOffset: 1500,
    pageSize: 1000,
    visibleRange: { from: 100, to: 300 },
    label: 'Page 3 of 3',
    ...extra,
  })
}

function leftPct(style: string | undefined): number {
  return Number(/left: ([\d.e-]+)%/.exec(style ?? '')![1])
}
function widthPct(style: string | undefined): number {
  return Number(/width: ([\d.e-]+)%/.exec(style ?? '')![1])
}

async function pointer(el: Element, type: string, clientX: number): Promise<void> {
  el.dispatchEvent(new MouseEvent(type, { clientX, bubbles: true }))
  await nextTick()
}

function capture(el: Element): void {
  ;(el as HTMLElement).setPointerCapture = vi.fn()
}

const seeks = (w: VueWrapper) => (w.emitted('seek') ?? []).map(([s]) => s)
const ranges = (w: VueWrapper) => (w.emitted('update:visibleRange') ?? []).map(([r]) => r as { from: number; to: number })

describe('the strip is the whole window', () => {
  it('places the loaded block and the window at their fractions of all N bars', () => {
    const w = newestPage()
    const block = w.find('.zoom-navigator__block').attributes('style')
    expect(leftPct(block)).toBeCloseTo((1500 / 2499) * 100, 9)
    expect(widthPct(block)).toBeCloseTo((999 / 2499) * 100, 9)
    const win = w.find('.zoom-navigator__window').attributes('style')
    expect(leftPct(win)).toBeCloseTo((1600 / 2499) * 100, 9)
    expect(widthPct(win)).toBeCloseTo((200 / 2499) * 100, 9)
  })

  it('hatches the not-loaded area on each side that has one, and draws the closes inside the block', () => {
    const middle = nav({ values: closes(1050), totalBars: 2500, pageOffset: 500, visibleRange: { from: 0, to: 200 } })
    expect(middle.find('.zoom-navigator__hatch--before').exists()).toBe(true)
    expect(middle.find('.zoom-navigator__hatch--after').exists()).toBe(true)
    const strip = middle.find('svg.zoom-navigator__strip').attributes('style')
    expect(leftPct(strip)).toBeCloseTo((500 / 2499) * 100, 9)
    // The strip's own path: the arrow buttons' chevron icons are SVG paths too, and come first.
    expect(middle.find('svg.zoom-navigator__strip path').attributes('d')).toMatch(/^M.*Z$/)
    const newest = newestPage()
    expect(newest.find('.zoom-navigator__hatch--before').exists()).toBe(true)
    expect(newest.find('.zoom-navigator__hatch--after').exists()).toBe(false)
  })

  it('the earliest page sits at the strip start, [0, its length)', () => {
    const w = nav({ values: closes(1050), totalBars: 2500, pageOffset: 0, visibleRange: { from: 0, to: 200 } })
    const block = w.find('.zoom-navigator__block').attributes('style')
    expect(leftPct(block)).toBe(0)
    expect(widthPct(block)).toBeCloseTo((1049 / 2499) * 100, 9)
    expect(w.find('.zoom-navigator__hatch--before').exists()).toBe(false)
  })

  // The track is measured on mount, so the thresholds apply from the next render.
  it('a page of a 190-page window is drawn at the minimum thumb width, without closes or handles', async () => {
    const w = nav({
      values: closes(1050),
      totalBars: 190_000,
      pageOffset: 94_000,
      visibleRange: { from: 0, to: 1049 },
      label: 'Page 95 of 190',
    })
    await nextTick()
    const block = w.find('.zoom-navigator__block').attributes('style')
    expect(widthPct(block)).toBeCloseTo(2.4, 9)
    expect(w.find('svg.zoom-navigator__strip').exists()).toBe(false)
    expect(w.findAll('.zoom-navigator__handle')).toHaveLength(0)
  })

  it('handles show only when the window is at least three handle widths wide', async () => {
    const wide = nav({ values: closes(1050), totalBars: 2500, pageOffset: 500, visibleRange: { from: 0, to: 1049 } })
    const narrow = nav({ values: closes(1050), totalBars: 2500, pageOffset: 500, visibleRange: { from: 0, to: 50 } })
    await nextTick()
    expect(wide.findAll('.zoom-navigator__handle')).toHaveLength(2)
    expect(wide.find('svg.zoom-navigator__strip').exists()).toBe(true)
    // 50 bars of 2,500 on 1,000 px is 20 px, under 30.
    expect(narrow.findAll('.zoom-navigator__handle')).toHaveLength(0)
  })
})

describe('label, loading and the slider', () => {
  it('shows the label politely live, and none when it is empty', () => {
    const w = newestPage()
    const label = w.find('.zoom-navigator__label')
    expect(label.text()).toBe('Page 3 of 3')
    expect(label.attributes('aria-live')).toBe('polite')
    expect(newestPage({ label: '' }).find('.zoom-navigator__label').exists()).toBe(false)
  })

  it('the window is a horizontal slider over all N bars, its value text the label', () => {
    const win = newestPage().find('.zoom-navigator__window')
    expect(win.attributes()).toMatchObject({
      role: 'slider',
      'aria-orientation': 'horizontal',
      'aria-valuemin': '0',
      'aria-valuemax': '2499',
      'aria-valuenow': '1600',
      'aria-valuetext': 'Page 3 of 3',
      'data-global-from': '1600',
      'data-global-to': '1800',
    })
  })

  it('loading marks the control busy and pulses the block', async () => {
    const w = newestPage()
    expect(w.attributes('aria-busy')).toBeUndefined()
    await w.setProps({ loading: true })
    expect(w.attributes('aria-busy')).toBe('true')
    expect(w.classes()).toContain('zoom-navigator--loading')
  })
})

describe('keys', () => {
  it('arrows move 10% of the window inside the page: update:visibleRange, page-relative', async () => {
    const w = newestPage()
    const win = w.find('.zoom-navigator__window')
    await win.trigger('keydown', { key: 'ArrowRight' })
    await w.setProps({ visibleRange: { from: 100, to: 300 } })
    await win.trigger('keydown', { key: 'ArrowLeft' })
    const [right, left] = ranges(w)
    expect(right!.from).toBeCloseTo(120, 6)
    expect(right!.to).toBeCloseTo(320, 6)
    expect(left!.from).toBeCloseTo(80, 6)
    expect(left!.to).toBeCloseTo(280, 6)
    expect(seeks(w)).toEqual([])
  })

  // tj-grna9p.129 (owner 2026-10-13, canvas 'A middle page'): Page Up, Page Down, Home and End are PAGE
  // navigation, not a window move: one `page` seek of a single bar of the target page, which the parent
  // shows whole. The window's width does not travel with it (it was a 'left' / 'right' seek of the same
  // width before .129).
  it('Page Up is page navigation: a page seek of the bar just before the loaded page', async () => {
    const w = newestPage()
    await w.find('.zoom-navigator__window').trigger('keydown', { key: 'PageUp' })
    expect(seeks(w)).toEqual([{ from: 1499, to: 1499, direction: 'page' }])
    expect(ranges(w)).toEqual([])
  })

  it('Page Down seeks the first bar of the next block, Home bar 0 and End bar N - 1, all as page seeks', async () => {
    const w = nav({
      values: closes(1050),
      totalBars: 2500,
      pageOffset: 500,
      pageSize: 1000,
      visibleRange: { from: 100, to: 300 },
    })
    const win = w.find('.zoom-navigator__window')
    await win.trigger('keydown', { key: 'PageDown' })
    await w.setProps({ visibleRange: { from: 100, to: 300 } })
    await win.trigger('keydown', { key: 'Home' })
    await w.setProps({ visibleRange: { from: 100, to: 300 } })
    await win.trigger('keydown', { key: 'End' })
    expect(seeks(w)).toEqual([
      { from: 1500, to: 1500, direction: 'page' },
      { from: 0, to: 0, direction: 'page' },
      { from: 2499, to: 2499, direction: 'page' },
    ])
    expect(ranges(w)).toEqual([])
  })

  // Before .129 Home here was a range move keeping the zoom. Now Home is page navigation everywhere, so
  // on the earliest page it asks for that same page whole (the parent zooms out to it).
  it('Home inside the earliest page is still a page seek of bar 0, never a range move', async () => {
    const w = nav({ values: closes(1050), totalBars: 2500, pageOffset: 0, visibleRange: { from: 300, to: 500 } })
    await w.find('.zoom-navigator__window').trigger('keydown', { key: 'Home' })
    expect(seeks(w)).toEqual([{ from: 0, to: 0, direction: 'page' }])
    expect(ranges(w)).toEqual([])
  })

  it('a handle only resizes with the arrows; the paging keys do nothing on it', async () => {
    const w = nav({ values: closes(1050), totalBars: 2500, pageOffset: 500, pageSize: 1000, visibleRange: { from: 0, to: 1049 } })
    const [start] = w.findAll('.zoom-navigator__handle')
    await start!.trigger('keydown', { key: 'PageUp' })
    await start!.trigger('keydown', { key: 'Home' })
    expect(w.emitted('seek')).toBeUndefined()
    expect(w.emitted('update:visibleRange')).toBeUndefined()
    await start!.trigger('keydown', { key: 'ArrowRight' })
    const [resized] = ranges(w)
    expect(resized!.to).toBeCloseTo(1049, 6)
    expect(resized!.from).toBeGreaterThan(0)
  })
})

describe('pointer', () => {
  it('a drag inside the page emits update:visibleRange only', async () => {
    const w = newestPage()
    const win = w.find('.zoom-navigator__window')
    capture(win.element)
    await pointer(win.element, 'pointerdown', 700)
    await pointer(win.element, 'pointermove', 740) // +40 px = +99.96 bars
    await pointer(win.element, 'pointerup', 740)
    expect(seeks(w)).toEqual([])
    const [moved] = ranges(w)
    expect(moved!.from).toBeCloseTo(100 + 0.04 * 2499, 5)
    expect(moved!.to - moved!.from).toBeCloseTo(200, 5)
  })

  it('a drag past the page edge emits one seek of the same width with its direction, deduplicated', async () => {
    const w = newestPage()
    const win = w.find('.zoom-navigator__window')
    capture(win.element)
    await pointer(win.element, 'pointerdown', 700)
    await pointer(win.element, 'pointermove', 600) // -100 px = -249.9 bars: global [1350.1, 1550.1]
    await pointer(win.element, 'pointermove', 600)
    expect(seeks(w)).toEqual([{ from: 1350, to: 1550, direction: 'left' }])
    // The window follows the pointer while the parent loads the page.
    expect(leftPct(w.find('.zoom-navigator__window').attributes('style'))).toBeCloseTo(((1600 - 249.9) / 2499) * 100, 6)
  })

  // .129: a click on the hatched part is page navigation to the page holding the clicked bar (a 'jump'
  // centred on the click, of the window's width, before). 100 px of 1,000 is bar round(0.1 * 2,499).
  it('a click on the hatched area is a page seek of the clicked bar: from = to, direction page', async () => {
    const w = newestPage()
    await pointer(w.find('.zoom-navigator__hatch--before').element, 'pointerdown', 100)
    expect(seeks(w)).toEqual([{ from: 250, to: 250, direction: 'page' }])
    expect(ranges(w)).toEqual([])
  })

  it('the hatch after the block seeks a bar there too, clamped into the window', async () => {
    const w = nav({ values: closes(1050), totalBars: 2500, pageOffset: 0, pageSize: 1000, visibleRange: { from: 0, to: 200 } })
    await pointer(w.find('.zoom-navigator__hatch--after').element, 'pointerdown', 1200)
    expect(seeks(w)).toEqual([{ from: 2499, to: 2499, direction: 'page' }])
  })
})

// tj-grna9p.129 (owner 2026-10-13, canvas 'A middle page'): a real arrow button at each end of the scroll
// bar, outside the strip. Previous seeks the bar just before the loaded page (offset - 1), Next the first
// bar of the next block (offset + pageSize; on the earliest page with r > 0 that bar is inside the next
// page too, the model resolves it). Disabled where no page exists. DOM order is visual order.
describe('the end arrows', () => {
  const prev = (w: VueWrapper) => w.find('[data-testid="zoom-navigator-prev"]')
  const next = (w: VueWrapper) => w.find('[data-testid="zoom-navigator-next"]')

  function middlePage(extra: Record<string, unknown> = {}): VueWrapper {
    return nav({ values: closes(1050), totalBars: 2500, pageOffset: 500, pageSize: 1000, visibleRange: { from: 100, to: 300 }, ...extra })
  }

  it('are real buttons named Previous page and Next page', () => {
    const w = middlePage()
    for (const [el, name] of [
      [prev(w), 'Previous page'],
      [next(w), 'Next page'],
    ] as const) {
      expect(el.element.tagName).toBe('BUTTON')
      expect(el.attributes('type')).toBe('button')
      expect(el.attributes('aria-label')).toBe(name)
      expect(el.attributes('aria-hidden')).toBeUndefined()
    }
  })

  it('Previous emits a page seek of bar offset - 1, Next of bar offset + pageSize; no range move', async () => {
    const w = middlePage()
    await prev(w).trigger('click')
    await next(w).trigger('click')
    expect(seeks(w)).toEqual([
      { from: 499, to: 499, direction: 'page' },
      { from: 1500, to: 1500, direction: 'page' },
    ])
    expect(ranges(w)).toEqual([])
  })

  it('on the earliest page Next is bar pageSize, whatever the remainder', async () => {
    // N = 2,500: pages at 0, 500, 1,500. Bar 1,000 is in page 1's block [500, 1500).
    const w = nav({ values: closes(1050), totalBars: 2500, pageOffset: 0, pageSize: 1000, visibleRange: { from: 0, to: 200 } })
    await next(w).trigger('click')
    expect(seeks(w)).toEqual([{ from: 1000, to: 1000, direction: 'page' }])
  })

  it('Previous is disabled on the first page and Next on the last, and a disabled arrow emits nothing', async () => {
    const first = nav({ values: closes(1050), totalBars: 2500, pageOffset: 0, pageSize: 1000, visibleRange: { from: 0, to: 200 } })
    expect(prev(first).attributes('disabled')).toBeDefined()
    expect(next(first).attributes('disabled')).toBeUndefined()
    await prev(first).trigger('click')
    expect(first.emitted('seek')).toBeUndefined()

    const last = newestPage()
    expect(prev(last).attributes('disabled')).toBeUndefined()
    expect(next(last).attributes('disabled')).toBeDefined()
    await next(last).trigger('click')
    expect(last.emitted('seek')).toBeUndefined()

    const middle = middlePage()
    expect(prev(middle).attributes('disabled')).toBeUndefined()
    expect(next(middle).attributes('disabled')).toBeUndefined()
  })

  it('Page Down on the newest page and Page Up on the earliest do nothing, like the disabled arrows', async () => {
    const last = newestPage()
    await last.find('.zoom-navigator__window').trigger('keydown', { key: 'PageDown' })
    expect(last.emitted('seek')).toBeUndefined()
    const first = nav({ values: closes(1050), totalBars: 2500, pageOffset: 0, pageSize: 1000, visibleRange: { from: 0, to: 200 } })
    await first.find('.zoom-navigator__window').trigger('keydown', { key: 'PageUp' })
    expect(first.emitted('seek')).toBeUndefined()
  })

  it('Tab order is visual order: Previous, the window, its handles, Next', async () => {
    const w = nav({ values: closes(1050), totalBars: 2500, pageOffset: 500, pageSize: 1000, visibleRange: { from: 0, to: 1049 } })
    await nextTick()
    const order = [...w.element.querySelectorAll('button')].map(
      (b) => b.getAttribute('data-testid') ?? b.className,
    )
    expect(order).toEqual([
      'zoom-navigator-prev',
      'zoom-navigator__window',
      'zoom-navigator__handle',
      'zoom-navigator__handle',
      'zoom-navigator-next',
    ])
    // Left of the strip, then right of it: the strip box sits between them in the row.
    const row = w.find('.zoom-navigator__row').element
    expect([...row.children].map((el) => el.getAttribute('data-testid') ?? el.className)).toEqual([
      'zoom-navigator-prev',
      'zoom-navigator__box',
      'zoom-navigator-next',
    ])
    expect(w.findAll('[tabindex="-1"]')).toHaveLength(0)
  })
})

// tj-grna9p.129: a darker sliver inside the block marks the look-ahead the next page shares, at the
// block's NEWER (right) end. The props are page-relative bar indices, inclusive.
describe('the overlap sliver', () => {
  const sliver = (w: VueWrapper) => w.find('[data-testid="zoom-navigator-overlap"]')

  it('sits at [from, to + 1) of the page\'s bars inside the drawn block', () => {
    // A middle page: 1,050 bars at 500, the block spans [500, 1549] of 2,499; overlap bars 1,000..1,049.
    const w = nav({ values: closes(1050), totalBars: 2500, pageOffset: 500, pageSize: 1000, overlapFrom: 1000, overlapTo: 1049, visibleRange: { from: 0, to: 200 } })
    const style = sliver(w).attributes('style')
    const start = 500 / 2499
    const span = 1049 / 2499
    expect(leftPct(style)).toBeCloseTo((start + (1000 / 1050) * span) * 100, 9)
    expect(widthPct(style)).toBeCloseTo((50 / 1050) * span * 100, 9)
    // It ends where the block ends: the right side, never the left.
    expect(leftPct(style) + widthPct(style)).toBeCloseTo(((500 + 1049) / 2499) * 100, 9)
    expect(sliver(w).attributes('aria-hidden')).toBe('true')
  })

  it('on the earliest page with r > 0 it runs from bar r to the page\'s last bar', () => {
    const w = nav({ values: closes(1050), totalBars: 2500, pageOffset: 0, pageSize: 1000, overlapFrom: 500, overlapTo: 1049, visibleRange: { from: 0, to: 200 } })
    const style = sliver(w).attributes('style')
    const span = 1049 / 2499
    expect(leftPct(style)).toBeCloseTo((500 / 1050) * span * 100, 9)
    expect(leftPct(style) + widthPct(style)).toBeCloseTo(span * 100, 9)
  })

  it('none without overlap props, with null, or without paging', () => {
    expect(sliver(newestPage()).exists()).toBe(false)
    expect(sliver(newestPage({ overlapFrom: null, overlapTo: null })).exists()).toBe(false)
    expect(sliver(nav({ values: closes(101), overlapFrom: 50, overlapTo: 100 })).exists()).toBe(false)
  })
})

// 389fa4f: the mirror sliver at the block's OLDER (left) end marks the first bars the previous page shows
// as its look-ahead (overlapBeforeFrom / overlapBeforeTo, page-relative, inclusive). Both slivers go
// through one helper; the right one must behave as before. Neither takes a pointer or a key.
describe('the overlap-before sliver', () => {
  const before = (w: VueWrapper) => w.find('[data-testid="zoom-navigator-overlap-before"]')
  const after = (w: VueWrapper) => w.find('[data-testid="zoom-navigator-overlap"]')

  /** A middle page: 1,050 bars at 500 of 2,500, so the block spans [500, 1549] of 2,499. */
  function middlePage(extra: Record<string, unknown> = {}): VueWrapper {
    return nav({ values: closes(1050), totalBars: 2500, pageOffset: 500, pageSize: 1000, visibleRange: { from: 100, to: 300 }, ...extra })
  }
  const MID_START = 500 / 2499
  const MID_SPAN = 1049 / 2499

  it('exists with overlapBeforeFrom 0 and overlapBeforeTo 49', () => {
    const w = newestPage({ overlapBeforeFrom: 0, overlapBeforeTo: 49 })
    expect(before(w).exists()).toBe(true)
    expect(before(w).classes()).toContain('zoom-navigator__overlap')
  })

  it('sits at block start + (from / L) * span, width (to + 1 - from) / L * span; from 0 is the block\'s left edge', () => {
    // The newest page: 1,000 bars at 1,500, the block spans [1500, 2499] of 2,499.
    const newest = newestPage({ overlapBeforeFrom: 0, overlapBeforeTo: 49 })
    const style = before(newest).attributes('style')
    const block = newest.find('.zoom-navigator__block').attributes('style')
    const start = 1500 / 2499
    const span = 999 / 2499
    expect(leftPct(style)).toBeCloseTo(start * 100, 9)
    expect(leftPct(style)).toBeCloseTo(leftPct(block), 9)
    expect(widthPct(style)).toBeCloseTo((50 / 1000) * span * 100, 9)

    // Page 2 of an r = 500 window: the earliest page reads 550 bars into it.
    const middle = middlePage({ overlapBeforeFrom: 0, overlapBeforeTo: 549 })
    const mid = before(middle).attributes('style')
    expect(leftPct(mid)).toBeCloseTo(MID_START * 100, 9)
    expect(widthPct(mid)).toBeCloseTo((550 / 1050) * MID_SPAN * 100, 9)

    // A from that is not 0 follows the same formula (the helper is shared, nothing pins it to the edge).
    const inner = middlePage({ overlapBeforeFrom: 10, overlapBeforeTo: 59 })
    const s = before(inner).attributes('style')
    expect(leftPct(s)).toBeCloseTo((MID_START + (10 / 1050) * MID_SPAN) * 100, 9)
    expect(widthPct(s)).toBeCloseTo((50 / 1050) * MID_SPAN * 100, 9)
  })

  it('is titled Overlap with the previous page and hidden from assistive tech', () => {
    const w = newestPage({ overlapBeforeFrom: 0, overlapBeforeTo: 49 })
    expect(before(w).attributes('title')).toBe('Overlap with the previous page')
    expect(before(w).attributes('aria-hidden')).toBe('true')
  })

  it('is absent with null or missing props, without paging, when to < from, and with no values', () => {
    expect(before(newestPage()).exists()).toBe(false)
    expect(before(newestPage({ overlapBeforeFrom: null, overlapBeforeTo: null })).exists()).toBe(false)
    expect(before(newestPage({ overlapBeforeFrom: 0, overlapBeforeTo: null })).exists()).toBe(false)
    expect(before(newestPage({ overlapBeforeFrom: null, overlapBeforeTo: 49 })).exists()).toBe(false)
    expect(before(nav({ values: closes(101), overlapBeforeFrom: 0, overlapBeforeTo: 49 })).exists()).toBe(false)
    expect(before(newestPage({ overlapBeforeFrom: 49, overlapBeforeTo: 0 })).exists()).toBe(false)
    expect(
      before(nav({ values: [], totalBars: 2500, pageOffset: 1500, pageSize: 1000, overlapBeforeFrom: 0, overlapBeforeTo: 49 })).exists(),
    ).toBe(false)
  })

  it('is independent of the right sliver: each alone, and both together at their own places', () => {
    const left = { overlapBeforeFrom: 0, overlapBeforeTo: 549 }
    const right = { overlapFrom: 1000, overlapTo: 1049 }

    const leftOnly = middlePage(left)
    expect(before(leftOnly).exists()).toBe(true)
    expect(after(leftOnly).exists()).toBe(false)

    const rightOnly = middlePage(right)
    expect(after(rightOnly).exists()).toBe(true)
    expect(before(rightOnly).exists()).toBe(false)
    // The right sliver through the shared helper: unchanged, ending where the block ends.
    const r = after(rightOnly).attributes('style')
    expect(leftPct(r)).toBeCloseTo((MID_START + (1000 / 1050) * MID_SPAN) * 100, 9)
    expect(widthPct(r)).toBeCloseTo((50 / 1050) * MID_SPAN * 100, 9)
    expect(leftPct(r) + widthPct(r)).toBeCloseTo((MID_START + MID_SPAN) * 100, 9)
    expect(after(rightOnly).attributes('title')).toBe('Overlap with the next page')

    const both = middlePage({ ...left, ...right })
    expect(both.findAll('.zoom-navigator__overlap')).toHaveLength(2)
    expect(after(both).attributes('style')).toBe(r)
    expect(before(both).attributes('style')).toBe(before(leftOnly).attributes('style'))
    expect(leftPct(before(both).attributes('style'))).toBeCloseTo(MID_START * 100, 9)
  })

  it('takes no pointer, click or key, and is not focusable', async () => {
    const w = newestPage({ overlapBeforeFrom: 0, overlapBeforeTo: 49 })
    const el = before(w).element as HTMLElement
    expect(el.tagName).toBe('DIV')
    expect(el.getAttribute('tabindex')).toBeNull()
    expect(el.getAttribute('role')).toBeNull()
    await pointer(el, 'pointerdown', 610)
    await pointer(el, 'pointermove', 650)
    await pointer(el, 'pointerup', 650)
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    el.dispatchEvent(new KeyboardEvent('keydown', { key: 'PageUp', bubbles: true }))
    await nextTick()
    expect(w.emitted('seek')).toBeUndefined()
    expect(w.emitted('update:visibleRange')).toBeUndefined()
  })

  it('with both slivers present, the hatch click, the window drag and the arrows behave as before', async () => {
    const w = newestPage({ overlapBeforeFrom: 0, overlapBeforeTo: 49 })
    await pointer(w.find('.zoom-navigator__hatch--before').element, 'pointerdown', 100)
    expect(seeks(w)).toEqual([{ from: 250, to: 250, direction: 'page' }])

    const win = w.find('.zoom-navigator__window')
    capture(win.element)
    await pointer(win.element, 'pointerdown', 700)
    await pointer(win.element, 'pointermove', 740)
    await pointer(win.element, 'pointerup', 740)
    expect(seeks(w)).toHaveLength(1)
    const [moved] = ranges(w)
    expect(moved!.from).toBeCloseTo(100 + 0.04 * 2499, 5)
    expect(moved!.to - moved!.from).toBeCloseTo(200, 5)

    const middle = middlePage({ overlapBeforeFrom: 0, overlapBeforeTo: 549, overlapFrom: 1000, overlapTo: 1049 })
    await middle.find('[data-testid="zoom-navigator-prev"]').trigger('click')
    await middle.find('[data-testid="zoom-navigator-next"]').trigger('click')
    expect(seeks(middle)).toEqual([
      { from: 499, to: 499, direction: 'page' },
      { from: 1500, to: 1500, direction: 'page' },
    ])
    // The slivers add no control: the buttons are the arrows, the window and its handles, in order.
    expect([...middle.element.querySelectorAll('button')].map((b) => b.getAttribute('data-testid') ?? b.className)).toEqual([
      'zoom-navigator-prev',
      'zoom-navigator__window',
      'zoom-navigator__handle',
      'zoom-navigator__handle',
      'zoom-navigator-next',
    ])
  })
})

describe('without paging props the old behaviour is unchanged', () => {
  it('no block, hatch, label or slider role; arrows move 2% of the strip; the strip spans the values', async () => {
    const w = nav({ values: closes(101), visibleRange: { from: 20, to: 40 } })
    expect(w.find('.zoom-navigator__block').exists()).toBe(false)
    expect(w.find('.zoom-navigator__hatch--before').exists()).toBe(false)
    expect(w.find('.zoom-navigator__label').exists()).toBe(false)
    // .129: no arrow buttons and no overlap sliver without paging.
    expect(w.find('[data-testid="zoom-navigator-prev"]').exists()).toBe(false)
    expect(w.find('[data-testid="zoom-navigator-next"]').exists()).toBe(false)
    const win = w.find('.zoom-navigator__window')
    expect(win.attributes('role')).toBeUndefined()
    expect(win.attributes('aria-valuenow')).toBeUndefined()
    expect(leftPct(win.attributes('style'))).toBeCloseTo(20, 9)
    await win.trigger('keydown', { key: 'ArrowRight' })
    await win.trigger('keydown', { key: 'PageUp' })
    await win.trigger('keydown', { key: 'Home' })
    await win.trigger('keydown', { key: 'End' })
    const emitted = ranges(w)
    expect(emitted).toHaveLength(1)
    expect(emitted[0]!.from).toBeCloseTo(22, 10)
    expect(emitted[0]!.to).toBeCloseTo(42, 10)
    expect(w.emitted('seek')).toBeUndefined()
  })

  it('a drag past the end clamps at the edge and never seeks', async () => {
    const w = nav({ values: closes(101), visibleRange: { from: 20, to: 40 } })
    const win = w.find('.zoom-navigator__window')
    capture(win.element)
    await pointer(win.element, 'pointerdown', 300)
    await pointer(win.element, 'pointermove', 1300)
    expect(w.emitted('seek')).toBeUndefined()
    const last = ranges(w).at(-1)!
    expect(last.from).toBeCloseTo(80, 10)
    expect(last.to).toBeCloseTo(100, 10)
  })
})
