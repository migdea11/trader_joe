<script setup lang="ts">
// Overview strip for PriceChart (tj-grna9p.53), and the pager of a paged window (tj-eozqhi,
// tj-grna9p.122). Lightweight Charts has no navigator, so this is ours.
//
// Without paging props the strip is the loaded closes and the window is the chart's visible
// logical range: dragging emits a new range for the chart, and the chart's own scroll or zoom
// arrives back through `visibleRange`.
//
// With `totalBars` the strip is the WHOLE window in bar-index units, the loaded page is a filled
// block at `pageOffset` (drawing its closes when wide enough, never narrower than a scroll-bar
// thumb), and everything else is hatched. A move inside the block still emits update:visibleRange
// (page-relative, as today); a move that would leave it emits `seek` in GLOBAL bar indices, as
// does a click on the hatched area. The component never fetches: the parent loads the page.
//
// Page navigation (an arrow button at each end, a click on the hatched area, Page Up, Page Down,
// Home, End) emits `seek` with direction `page` and one bar of the target page: the parent shows
// that page zoomed out to the whole page and its look-ahead. Moving inside the block (drag, arrow
// keys) keeps the user's zoom. A darker sliver at each end of the block marks the bars shared with a
// neighbour: the look-ahead the next page shows (right), the first bars the previous page shows as its
// look-ahead (left). Both are non-interactive.
//
// Pure SVG and DOM, no chart library. The values arrive as plain numbers (the parent has already
// crossed the int64 boundary); they are read through toRaw and never made reactive.
import { computed, onBeforeUnmount, onMounted, ref, toRaw, useTemplateRef, watch } from 'vue'
import { ChevronLeft, ChevronRight } from '@lucide/vue'

import { colors, fonts, fontSizes } from '@/theme/tokens'
import {
  barAtFraction,
  blockWindow,
  clampToBlock,
  closesVisible,
  downsampleMinMax,
  handlesVisible,
  keyDelta,
  minWindowFraction,
  moveWindow,
  pageSeek,
  pageStepBar,
  rangeInBlock,
  rangeToWindow,
  resizeEnd,
  resizeStart,
  seekRequest,
  stripBuckets,
  toGlobalRange,
  toLocalRange,
  widenThumb,
  windowToRange,
  type LogicalRangeLike,
  type NavWindow,
  type PagedExtent,
  type SeekDirection,
  type SeekRequest,
} from './navigatorMath'

const props = withDefaults(
  defineProps<{
    /** The series drawn in the strip, one value per bar (the loaded page's closes when paged). */
    readonly values: ArrayLike<number>
    /** The chart's visible logical range, PAGE-relative; null shows the whole loaded block. */
    readonly visibleRange?: LogicalRangeLike | null
    /** Strip height in px. */
    readonly height?: number
    /** Paging: bars in the whole window. Absent = no paging, the strip is `values`. */
    readonly totalBars?: number
    /** Paging: global index of the loaded page's first bar. */
    readonly pageOffset?: number
    /** Paging: bars a Page Up or Page Down moves (default: the loaded page's length). */
    readonly pageSize?: number
    /** The one label, e.g. "Page 3 of 189"; empty hides it. Also the text alternative. */
    readonly label?: string
    /** A page is loading: the block pulses and the control is busy. */
    readonly loading?: boolean
    /** Paging: the look-ahead the next page shares, PAGE-relative bar indices, inclusive. Null: none. */
    readonly overlapFrom?: number | null
    readonly overlapTo?: number | null
    /** Paging: the first bars the previous page shows as its look-ahead, PAGE-relative, inclusive. Null: none. */
    readonly overlapBeforeFrom?: number | null
    readonly overlapBeforeTo?: number | null
  }>(),
  {
    visibleRange: null,
    height: 56,
    totalBars: undefined,
    pageOffset: 0,
    pageSize: undefined,
    label: '',
    loading: false,
    overlapFrom: null,
    overlapTo: null,
    overlapBeforeFrom: null,
    overlapBeforeTo: null,
  },
)

const emit = defineEmits<{
  'update:visibleRange': [range: LogicalRangeLike]
  /** The window left the loaded block (or the hatched area was clicked): GLOBAL bar indices. */
  seek: [request: SeekRequest]
}>()

// The root element is the track (pointer positions are measured against it); the box inside holds
// the strip and the label sits under it.
// Horizontal resolution of the drawn strip; the SVG stretches to the element width.
const STRIP_BUCKETS = 400
// Arrow-key step without paging, as a fraction of the full range.
const KEY_STEP = 0.02

type DragMode = 'move' | 'start' | 'end'

const track = useTemplateRef<HTMLDivElement>('track')
const box = useTemplateRef<HTMLDivElement>('box')
// Pointer positions are measured against the strip itself when paged (the arrow buttons sit beside
// it) and against the root otherwise, as before.
const measured = (): HTMLDivElement | null => (props.totalBars !== undefined ? box.value : track.value)

const paged = computed(() => props.totalBars !== undefined)
const count = computed(() => toRaw(props.values).length)
const total = computed(() => (paged.value ? Math.max(props.totalBars ?? 0, 0) : count.value))
const extent = computed<PagedExtent>(() => ({
  total: total.value,
  offset: paged.value ? props.pageOffset : 0,
  length: count.value,
}))

// The track's width in px, for the minimum thumb and the handle and closes thresholds. Unknown
// (0) outside a browser layout; the geometry then falls back to plain fractions.
const trackPx = ref(0)
let observer: ResizeObserver | null = null
function measure(): void {
  trackPx.value = measured()?.getBoundingClientRect().width ?? 0
}
onMounted(() => {
  measure()
  if (typeof ResizeObserver !== 'undefined' && box.value) {
    observer = new ResizeObserver(measure)
    observer.observe(box.value)
  }
})
onBeforeUnmount(() => observer?.disconnect())

// The loaded block, true and as drawn.
const block = computed<NavWindow>(() => blockWindow(extent.value))
const thumb = computed<NavWindow>(() =>
  paged.value ? widenThumb(block.value, trackPx.value) : { start: 0, end: 1 },
)
const thumbPx = computed(() => (thumb.value.end - thumb.value.start) * trackPx.value)

// While dragging, the window follows the pointer even when the parent has not moved the chart
// (a seek is pending); it clears when the parent moves the range or the page.
const preview = ref<NavWindow | null>(null)
watch(
  () => [props.visibleRange, props.pageOffset, props.totalBars] as const,
  () => {
    preview.value = null
  },
)

const windowFraction = computed<NavWindow>(() => {
  if (preview.value) return preview.value
  if (!props.visibleRange) return paged.value ? block.value : { start: 0, end: 1 }
  return rangeToWindow(toGlobalRange(props.visibleRange, extent.value.offset), total.value)
})

const showHandles = computed(() => !paged.value || handlesVisible(windowFraction.value, trackPx.value))
const showCloses = computed(() => !paged.value || closesVisible(thumb.value, trackPx.value))

// The strip as one closed polygon: the highest value of each bucket left to right, then the lowest
// right to left. y is inverted (larger value, higher on screen) in a 0..100 box.
const stripPath = computed(() => {
  const maxBuckets = paged.value ? stripBuckets(thumbPx.value, STRIP_BUCKETS) : STRIP_BUCKETS
  const buckets = downsampleMinMax(toRaw(props.values), maxBuckets)
  if (buckets.length === 0) return ''
  let lo = Infinity
  let hi = -Infinity
  for (const [min, max] of buckets) {
    if (min < lo) lo = min
    if (max > hi) hi = max
  }
  const range = hi - lo || 1
  const x = (i: number) => ((i + 0.5) / buckets.length) * 1000
  const y = (v: number) => 96 - ((v - lo) / range) * 92
  const top = buckets.map(([, max], i) => `${x(i).toFixed(1)},${y(max).toFixed(1)}`)
  const bottom = buckets.map(([min], i) => `${x(i).toFixed(1)},${y(min).toFixed(1)}`).reverse()
  return `M${top.join('L')}L${bottom.join('L')}Z`
})

// The window as a global range, for the slider value and the data attributes.
const globalRange = computed(() => windowToRange(windowFraction.value, total.value))

let lastSeek: SeekRequest | null = null
let seekSent = false

// A window (fractions of the whole strip) goes to the chart when it is inside the loaded block,
// and to the parent as a seek when it is not.
function publish(win: NavWindow, direction: SeekDirection): void {
  const range = windowToRange(win, total.value)
  if (rangeInBlock(range, extent.value)) {
    const local = toLocalRange(range, extent.value.offset)
    // Fractions of a large window leave float noise at the 1e-11 level; a paged range is rounded.
    emit(
      'update:visibleRange',
      paged.value ? { from: Math.round(local.from * 1e6) / 1e6, to: Math.round(local.to * 1e6) / 1e6 } : local,
    )
    return
  }
  const request = seekRequest(range, total.value, direction)
  seekSent = true
  // A drag past the edge fires on every pointer move; the same target is one seek.
  if (lastSeek && lastSeek.from === request.from && lastSeek.to === request.to) return
  lastSeek = request
  emit('seek', request)
}

function apply(mode: DragMode, win: NavWindow, delta: number): NavWindow {
  const min = minWindowFraction(total.value)
  if (mode === 'move') return moveWindow(win, delta)
  const resized = mode === 'start' ? resizeStart(win, delta, min) : resizeEnd(win, delta, min)
  // The chart holds one page: a resize stays inside the loaded block.
  return paged.value ? clampToBlock(resized, block.value) : resized
}

const directionOf = (delta: number): SeekDirection => (delta < 0 ? 'left' : 'right')

let drag: { mode: DragMode; startX: number; width: number; origin: NavWindow } | null = null

function onPointerDown(mode: DragMode, event: PointerEvent): void {
  const el = measured()
  if (!el || total.value < 2) return
  const width = el.getBoundingClientRect().width
  if (width <= 0) return
  drag = { mode, startX: event.clientX, width, origin: windowFraction.value }
  lastSeek = null
  seekSent = false
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}

function onPointerMove(event: PointerEvent): void {
  if (!drag) return
  const delta = (event.clientX - drag.startX) / drag.width
  const win = apply(drag.mode, drag.origin, delta)
  if (paged.value) preview.value = win
  publish(win, directionOf(delta))
}

function onPointerUp(): void {
  drag = null
  if (!seekSent) preview.value = null
}

// Page navigation: one `page` seek, never a range move. The block stays where it is until the
// parent's page lands.
function goToBar(bar: number | null): void {
  if (bar === null) return
  lastSeek = null
  seekSent = true
  emit('seek', pageSeek(bar, total.value))
}
const pageBars = (): number => props.pageSize ?? count.value
const goBefore = (): void => goToBar(pageStepBar(extent.value, pageBars(), 'before'))
const goAfter = (): void => goToBar(pageStepBar(extent.value, pageBars(), 'after'))
const hasBefore = computed(() => paged.value && pageStepBar(extent.value, pageBars(), 'before') !== null)
const hasAfter = computed(() => paged.value && pageStepBar(extent.value, pageBars(), 'after') !== null)

const PAGE_KEYS = ['PageUp', 'PageDown', 'Home', 'End']

function onKeydown(mode: DragMode, event: KeyboardEvent): void {
  if (total.value < 2) return
  if (paged.value && mode === 'move' && PAGE_KEYS.includes(event.key)) {
    event.preventDefault()
    if (event.key === 'PageUp') goBefore()
    else if (event.key === 'PageDown') goAfter()
    else goToBar(event.key === 'Home' ? 0 : total.value - 1)
    return
  }
  const win = windowFraction.value
  let delta: number | null
  if (paged.value) {
    delta = keyDelta(event.key, win, total.value, props.pageSize ?? count.value)
    // A handle only resizes, and only with the arrows; the paging keys belong to the window.
    if (mode !== 'move' && event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') delta = null
  } else {
    delta = event.key === 'ArrowRight' ? KEY_STEP : event.key === 'ArrowLeft' ? -KEY_STEP : null
  }
  if (delta === null) return
  event.preventDefault()
  lastSeek = null
  const next = apply(mode, win, delta)
  if (paged.value) preview.value = next
  publish(next, directionOf(delta))
}

// A click on the hatched (not loaded) area is page navigation to the page holding that bar.
function onHatchDown(event: PointerEvent): void {
  const el = measured()
  if (!el || total.value < 2) return
  const rect = el.getBoundingClientRect()
  if (rect.width <= 0) return
  const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width))
  goToBar(barAtFraction(fraction, total.value))
}

// An overlap sliver inside the drawn block, as a fraction of the strip; `from` and `to` are
// PAGE-relative bar indices, inclusive.
function sliverOf(from: number | null, to: number | null): { left: number; width: number } | null {
  const length = count.value
  if (!paged.value || from === null || to === null || length < 1 || to < from) return null
  const span = thumb.value.end - thumb.value.start
  const start = thumb.value.start + (Math.max(from, 0) / length) * span
  const end = thumb.value.start + (Math.min(to + 1, length) / length) * span
  return end > start ? { left: start, width: end - start } : null
}
const overlapSliver = computed(() => sliverOf(props.overlapFrom, props.overlapTo))
const overlapBeforeSliver = computed(() => sliverOf(props.overlapBeforeFrom, props.overlapBeforeTo))
const overlapFill = `${colors.accentTint}80`

const pct = (fraction: number) => `${fraction * 100}%`
const windowLabel = computed(() =>
  paged.value
    ? 'Chart page position: drag, or use the arrow keys; Page Up and Page Down change page, Home and End go to the first and last page'
    : 'Visible range window: drag, or use the arrow keys, to pan',
)
</script>

<template>
  <div
    ref="track"
    class="zoom-navigator"
    :class="{ 'zoom-navigator--paged': paged, 'zoom-navigator--loading': loading }"
    :aria-busy="loading ? 'true' : undefined"
  >
    <div class="zoom-navigator__row">
      <button
        v-if="paged"
        type="button"
        class="zoom-navigator__arrow"
        :style="{ height: `${height}px` }"
        aria-label="Previous page"
        data-testid="zoom-navigator-prev"
        :disabled="!hasBefore"
        @click="goBefore"
      >
        <ChevronLeft
          :size="16"
          aria-hidden="true"
        />
      </button>
      <div
        ref="box"
        class="zoom-navigator__box"
        :style="{ height: `${height}px` }"
      >
        <template v-if="paged">
          <div
            v-if="thumb.start > 0"
            class="zoom-navigator__hatch zoom-navigator__hatch--before"
            :style="{ left: 0, width: pct(thumb.start) }"
            aria-hidden="true"
            @pointerdown="onHatchDown"
          />
          <div
            v-if="thumb.end < 1"
            class="zoom-navigator__hatch zoom-navigator__hatch--after"
            :style="{ left: pct(thumb.end), right: 0 }"
            aria-hidden="true"
            @pointerdown="onHatchDown"
          />
          <div
            class="zoom-navigator__block"
            :style="{ left: pct(thumb.start), width: pct(thumb.end - thumb.start) }"
            aria-hidden="true"
          />
        </template>
        <svg
          v-if="showCloses"
          class="zoom-navigator__strip"
          :style="paged ? { left: pct(thumb.start), width: pct(thumb.end - thumb.start) } : undefined"
          viewBox="0 0 1000 100"
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <path
            :d="stripPath"
            :fill="colors.series[0]"
            fill-opacity="0.35"
            :stroke="colors.series[0]"
            stroke-width="1"
            vector-effect="non-scaling-stroke"
          />
        </svg>
        <div
          v-if="overlapBeforeSliver"
          class="zoom-navigator__overlap"
          data-testid="zoom-navigator-overlap-before"
          :style="{ left: pct(overlapBeforeSliver.left), width: pct(overlapBeforeSliver.width) }"
          title="Overlap with the previous page"
          aria-hidden="true"
        />
        <div
          v-if="overlapSliver"
          class="zoom-navigator__overlap"
          data-testid="zoom-navigator-overlap"
          :style="{ left: pct(overlapSliver.left), width: pct(overlapSliver.width) }"
          title="Overlap with the next page"
          aria-hidden="true"
        />
        <template v-if="total >= 2">
          <div
            class="zoom-navigator__dim"
            :style="{ left: 0, width: pct(windowFraction.start) }"
          />
          <div
            class="zoom-navigator__dim"
            :style="{ left: pct(windowFraction.end), right: 0 }"
          />
          <button
            type="button"
            class="zoom-navigator__window"
            :style="{ left: pct(windowFraction.start), width: pct(windowFraction.end - windowFraction.start) }"
            :role="paged ? 'slider' : undefined"
            :aria-label="windowLabel"
            :aria-orientation="paged ? 'horizontal' : undefined"
            :aria-valuemin="paged ? 0 : undefined"
            :aria-valuemax="paged ? total - 1 : undefined"
            :aria-valuenow="paged ? Math.round(globalRange.from) : undefined"
            :aria-valuetext="paged && label ? label : undefined"
            :data-global-from="paged ? Math.round(globalRange.from) : undefined"
            :data-global-to="paged ? Math.round(globalRange.to) : undefined"
            @pointerdown="onPointerDown('move', $event)"
            @pointermove="onPointerMove"
            @pointerup="onPointerUp"
            @pointercancel="onPointerUp"
            @keydown="onKeydown('move', $event)"
          />
          <template v-if="showHandles">
            <button
              type="button"
              class="zoom-navigator__handle"
              :style="{ left: pct(windowFraction.start) }"
              aria-label="Window start: drag, or use the arrow keys, to resize"
              @pointerdown.stop="onPointerDown('start', $event)"
              @pointermove="onPointerMove"
              @pointerup="onPointerUp"
              @pointercancel="onPointerUp"
              @keydown="onKeydown('start', $event)"
            />
            <button
              type="button"
              class="zoom-navigator__handle"
              :style="{ left: pct(windowFraction.end) }"
              aria-label="Window end: drag, or use the arrow keys, to resize"
              @pointerdown.stop="onPointerDown('end', $event)"
              @pointermove="onPointerMove"
              @pointerup="onPointerUp"
              @pointercancel="onPointerUp"
              @keydown="onKeydown('end', $event)"
            />
          </template>
        </template>
      </div>
      <button
        v-if="paged"
        type="button"
        class="zoom-navigator__arrow"
        :style="{ height: `${height}px` }"
        aria-label="Next page"
        data-testid="zoom-navigator-next"
        :disabled="!hasAfter"
        @click="goAfter"
      >
        <ChevronRight
          :size="16"
          aria-hidden="true"
        />
      </button>
    </div>
    <p
      v-if="label"
      class="zoom-navigator__label"
      aria-live="polite"
    >
      {{ label }}
    </p>
  </div>
</template>

<style scoped>
.zoom-navigator {
  width: 100%;
}
.zoom-navigator__row {
  display: flex;
  align-items: stretch;
  gap: 8px;
}
.zoom-navigator__arrow {
  flex: 0 0 32px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: v-bind('colors.raised');
  border: 1px solid v-bind('colors.line');
  border-radius: 6px;
  color: v-bind('colors.text');
  cursor: pointer;
}
.zoom-navigator__arrow:disabled {
  color: v-bind('colors.text3');
  cursor: not-allowed;
}
.zoom-navigator__arrow:focus-visible {
  outline: 2px solid v-bind('colors.text');
  outline-offset: 1px;
}
/* The bars a neighbour page shares: a darker sliver at the block's newer end (the look-ahead the
   next page shows) and at its older end (the first bars the previous page shows as its look-ahead). */
.zoom-navigator__overlap {
  position: absolute;
  top: 0;
  bottom: 0;
  min-width: 2px;
  background: v-bind('overlapFill');
  pointer-events: none;
}
.zoom-navigator--paged .zoom-navigator__label {
  margin-left: 40px;
}
.zoom-navigator__box {
  position: relative;
  flex: 1 1 auto;
  min-width: 0;
  width: 100%;
  background: v-bind('colors.surface');
  border: 1px solid v-bind('colors.line');
  border-radius: 6px;
  overflow: hidden;
  touch-action: none;
  user-select: none;
}
.zoom-navigator__strip {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}
/* The loaded page's own area is drawn solid; the stripes are the part that is not loaded. */
.zoom-navigator__hatch {
  position: absolute;
  top: 0;
  bottom: 0;
  background-image: repeating-linear-gradient(
    135deg,
    v-bind('colors.hatch') 0,
    v-bind('colors.hatch') 1px,
    transparent 1px,
    transparent 6px
  );
  cursor: pointer;
}
.zoom-navigator__block {
  position: absolute;
  top: 0;
  bottom: 0;
  background: v-bind('colors.raised');
  pointer-events: none;
}
.zoom-navigator--loading .zoom-navigator__block {
  animation: zoom-navigator-pulse 1.2s ease-in-out infinite alternate;
}
@keyframes zoom-navigator-pulse {
  from {
    opacity: 1;
  }
  to {
    opacity: 0.45;
  }
}
@media (prefers-reduced-motion: reduce) {
  .zoom-navigator--loading .zoom-navigator__block {
    animation: none;
    opacity: 0.6;
  }
}
.zoom-navigator__label {
  margin: 4px 0 0;
  color: v-bind('colors.text2');
  font-family: v-bind('fonts.sans');
  font-size: v-bind('`${fontSizes.small}px`');
  font-variant-numeric: tabular-nums;
}
.zoom-navigator__dim {
  position: absolute;
  top: 0;
  bottom: 0;
  background: v-bind('colors.ground');
  opacity: 0.6;
  pointer-events: none;
}
.zoom-navigator__window {
  position: absolute;
  top: 0;
  bottom: 0;
  min-width: 3px;
  padding: 0;
  background: transparent;
  border: 1px solid v-bind('colors.accentTint');
  cursor: grab;
}
.zoom-navigator__handle {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 10px;
  margin-left: -5px;
  padding: 0;
  background: v-bind('colors.accentTint');
  border: 0;
  border-radius: 3px;
  opacity: 0.85;
  cursor: ew-resize;
}
.zoom-navigator__window:focus-visible,
.zoom-navigator__handle:focus-visible {
  outline: 2px solid v-bind('colors.text');
  outline-offset: 1px;
}
</style>
