<script setup lang="ts">
// Overview strip for PriceChart (tj-grna9p.53): the full range as a small area chart with a window
// you drag to pan and resize to zoom. Lightweight Charts has no navigator, so this is ours. The
// window and the chart's visible logical range are the same thing seen two ways: dragging emits a
// new range for the chart, and the chart's own scroll or zoom arrives back through `visibleRange`.
//
// Pure SVG and DOM, no chart library. The values arrive as plain numbers (the parent has already
// crossed the int64 boundary); they are read through toRaw and never made reactive.
import { computed, toRaw, useTemplateRef } from 'vue'

import { colors } from '@/theme/tokens'
import {
  downsampleMinMax,
  minWindowFraction,
  moveWindow,
  rangeToWindow,
  resizeEnd,
  resizeStart,
  windowToRange,
  type LogicalRangeLike,
  type NavWindow,
} from './navigatorMath'

const props = withDefaults(
  defineProps<{
    /** The series drawn in the strip, one value per bar (typically the closes). */
    readonly values: ArrayLike<number>
    /** The chart's visible logical range; null shows the whole range. */
    readonly visibleRange?: LogicalRangeLike | null
    /** Strip height in px. */
    readonly height?: number
  }>(),
  { visibleRange: null, height: 56 },
)

const emit = defineEmits<{
  'update:visibleRange': [range: LogicalRangeLike]
}>()

// Horizontal resolution of the drawn strip; the SVG stretches to the element width.
const STRIP_BUCKETS = 400
// Arrow-key step, as a fraction of the full range.
const KEY_STEP = 0.02

type DragMode = 'move' | 'start' | 'end'

const track = useTemplateRef<HTMLDivElement>('track')

const count = computed(() => toRaw(props.values).length)

const windowFraction = computed<NavWindow>(() =>
  props.visibleRange ? rangeToWindow(props.visibleRange, count.value) : { start: 0, end: 1 },
)

// The strip as one closed polygon: the highest value of each bucket left to right, then the lowest
// right to left. y is inverted (larger value, higher on screen) in a 0..100 box.
const stripPath = computed(() => {
  const buckets = downsampleMinMax(toRaw(props.values), STRIP_BUCKETS)
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

function publish(win: NavWindow): void {
  emit('update:visibleRange', windowToRange(win, count.value))
}

function apply(mode: DragMode, win: NavWindow, delta: number): NavWindow {
  const min = minWindowFraction(count.value)
  if (mode === 'move') return moveWindow(win, delta)
  return mode === 'start' ? resizeStart(win, delta, min) : resizeEnd(win, delta, min)
}

let drag: { mode: DragMode; startX: number; width: number; origin: NavWindow } | null = null

function onPointerDown(mode: DragMode, event: PointerEvent): void {
  const el = track.value
  if (!el || count.value < 2) return
  const width = el.getBoundingClientRect().width
  if (width <= 0) return
  drag = { mode, startX: event.clientX, width, origin: windowFraction.value }
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}

function onPointerMove(event: PointerEvent): void {
  if (!drag) return
  publish(apply(drag.mode, drag.origin, (event.clientX - drag.startX) / drag.width))
}

function onPointerUp(): void {
  drag = null
}

function onKeydown(mode: DragMode, event: KeyboardEvent): void {
  const sign = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0
  if (sign === 0 || count.value < 2) return
  event.preventDefault()
  publish(apply(mode, windowFraction.value, sign * KEY_STEP))
}

const pct = (fraction: number) => `${fraction * 100}%`
</script>

<template>
  <div
    ref="track"
    class="zoom-navigator"
    :style="{ height: `${height}px` }"
  >
    <svg
      class="zoom-navigator__strip"
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
    <template v-if="count >= 2">
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
        aria-label="Visible range window: drag, or use the arrow keys, to pan"
        @pointerdown="onPointerDown('move', $event)"
        @pointermove="onPointerMove"
        @pointerup="onPointerUp"
        @pointercancel="onPointerUp"
        @keydown="onKeydown('move', $event)"
      />
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
  </div>
</template>

<style scoped>
.zoom-navigator {
  position: relative;
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
