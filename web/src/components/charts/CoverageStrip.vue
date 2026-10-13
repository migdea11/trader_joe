<script setup lang="ts">
// The coverage strip (tj-grna9p.77, reshaped by tj-grna9p.108; reused by the Dataset detail, .34):
// by default ONE continuous track in the covered colour over the strip's whole range, broken only
// where the server reports a gap: adjacent covered buckets are not told apart. (`variant` below.)
//
// A break is positioned by TIME across [start, end) and is at least 2px wide so a one-session gap
// shows. It is hollow (the empty track shows through), so it never rests on colour alone, and its
// hover names its dates and session count in words. An empty dataset is a wholly hollow track and
// "nothing due yet" a faint one. The text beside the strip is exactly "x/y sessions"; the track is
// role=img with the same facts as its label.
//
// `variant` picks the rendering. 'continuous' (default, the Datasets grid cell, where many strips
// stack) is the one track above. 'blocks' (the Dataset detail, one strip on the screen) keeps one
// block per bucket: its HEIGHT is the share of the bucket's due sessions that hold bars, so a
// partial block is shorter and an empty one hollow whatever the colour, and its hover names its
// dates and "x of y due sessions", with no percent. The text beside it is "x/y sessions" in both.
//
// `compact` (a grid cell) shows the numbers "x/y" only (the column header carries the unit), the
// track at ONE fixed width on every row and the numbers in their own fixed-width slot, so the bar
// never grows or shrinks with the digits. It keeps the gap windows for
// assistive technology. Otherwise `showGaps` lists every gap window the server sent, each as its
// dates and session count in words, and the note for gaps the server did not list.
import { computed } from 'vue'

import type { CoverageStrip } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import {
  bucketFill,
  bucketState,
  coverageFraction,
  coverageLabel,
  coverageSummary,
  coverageTotals,
  describeBucket,
  describeGap,
  moreGapsNote,
} from '@/catalog/coverage'
import { timestampToInstant } from '@/format/formatters'

const props = withDefaults(
  defineProps<{ strip: CoverageStrip; compact?: boolean; showGaps?: boolean; variant?: 'continuous' | 'blocks' }>(),
  { compact: false, showGaps: false, variant: 'continuous' },
)

// A grid cell has the unit in its column header, so there the text is the numbers alone; the
// aria-label keeps the unit in words either way, because a graphic has no column header.
const summary = computed(() => (props.compact ? coverageFraction(props.strip) : coverageSummary(props.strip)))
const label = computed(() => coverageLabel(props.strip))
const note = computed(() => moreGapsNote(props.strip))
const gaps = computed(() => props.strip.gaps.map(describeGap))
const totals = computed(() => coverageTotals(props.strip))
// Nothing due: a faint track. Due but nothing stored: a hollow one. Otherwise filled, with breaks.
const state = computed(() => {
  if (totals.value.expected <= 0) return 'not-due'
  return totals.value.withBars <= 0 ? 'empty' : 'covered'
})

function share(time: number, from: number, span: number): number {
  return Math.min(1, Math.max(0, (time - from) / span))
}

const blocks = computed(() =>
  props.variant !== 'blocks'
    ? []
    : props.strip.buckets.map((bucket, index) => ({
        index,
        state: bucketState(bucket),
        fill: bucketFill(bucket) * 100,
        title: describeBucket(bucket),
      })),
)

const breaks = computed(() => {
  if (props.variant === 'blocks') return []
  const { start, end } = props.strip
  if (state.value !== 'covered' || start === undefined || end === undefined) return []
  const from = timestampToInstant(start)
  const span = timestampToInstant(end) - from
  if (!(span > 0)) return []
  return props.strip.gaps.flatMap((gap, index) => {
    if (gap.start === undefined || gap.end === undefined) return []
    const left = share(timestampToInstant(gap.start), from, span)
    const right = share(timestampToInstant(gap.end), from, span)
    return [{ index, left: left * 100, width: (right - left) * 100, title: describeGap(gap) }]
  })
})
</script>

<template>
  <div
    class="coverage"
    :class="{ 'coverage--compact': compact }"
    data-testid="coverage-strip"
  >
    <div class="coverage__row">
      <div
        class="coverage__track"
        :class="variant === 'blocks' && strip.buckets.length > 0 ? 'coverage__track--blocks' : `coverage__track--${state}`"
        :data-state="state"
        role="img"
        :aria-label="label"
        data-testid="coverage-track"
      >
        <span
          v-for="block in blocks"
          :key="`block-${block.index}`"
          class="coverage__block"
          :class="`coverage__block--${block.state}`"
          :data-state="block.state"
          data-testid="coverage-block"
          :title="block.title"
        >
          <span
            v-if="block.fill > 0"
            class="coverage__fill"
            :style="{ height: `${block.fill}%` }"
          />
        </span>
        <span
          v-for="gap in breaks"
          :key="gap.index"
          class="coverage__break"
          data-testid="coverage-break"
          :title="gap.title"
          :style="{ left: `${gap.left}%`, width: `${gap.width}%` }"
        />
      </div>
      <span
        class="coverage__summary"
        data-testid="coverage-summary"
        aria-hidden="true"
      >{{ summary }}</span>
    </div>

    <ul
      v-if="showGaps && (gaps.length > 0 || note)"
      class="coverage__gaps"
      data-testid="coverage-gaps"
    >
      <li
        v-for="(gap, index) in gaps"
        :key="index"
      >
        {{ gap }}
      </li>
      <li v-if="note">
        {{ note }}
      </li>
    </ul>
    <!-- The same windows for assistive technology where the cell has no room to list them. -->
    <ul
      v-else-if="!showGaps && gaps.length > 0"
      class="coverage__sr"
    >
      <li
        v-for="(gap, index) in gaps"
        :key="index"
      >
        Gap: {{ gap }}
      </li>
    </ul>
  </div>
</template>

<style scoped>
.coverage {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.coverage__row {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.coverage__track {
  position: relative;
  flex: 1 1 auto;
  min-width: 60px;
  height: 14px;
  box-sizing: border-box;
  border-radius: 2px;
}

.coverage--compact .coverage__track {
  flex: 0 0 var(--coverage-bar-width, 112px);
  min-width: 0;
  height: 10px;
}

/* The numbers' own slot: fixed width, tabular digits, right aligned. A narrow column clips this
   slot first (ellipsis); the bar keeps its width, so bars stay identical across rows. */
.coverage--compact .coverage__summary {
  flex: 0 1 var(--coverage-text-width, 9ch);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  text-align: right;
}

/* One continuous run of the covered colour; the breaks sit over it. */
.coverage__track--covered {
  background: var(--tj-status-ok);
}

/* Hollow: only an outline, nothing stored. */
.coverage__track--empty {
  border: 1px solid var(--tj-line);
}

/* Faint and hollow: nothing is due yet. */
.coverage__track--not-due {
  border: 1px solid var(--tj-line);
  opacity: 0.4;
}

/* Blocks: one per bucket, side by side with a hairline between. Height carries the covered share;
   the colour only supports it. A track that has no buckets stays a hollow outline. */
.coverage__track--blocks {
  display: flex;
  align-items: stretch;
  gap: 1px;
}

.coverage__block {
  position: relative;
  flex: 1 1 0;
  min-width: 1px;
  display: flex;
  align-items: flex-end;
  box-sizing: border-box;
  border: 1px solid var(--tj-line);
  border-radius: 1px;
}

.coverage__block--not-due {
  opacity: 0.4;
}

.coverage__fill {
  display: block;
  width: 100%;
  background: var(--tj-status-ok);
}

.coverage__block--partial .coverage__fill {
  background: var(--tj-status-warn);
}

/* A gap: hollow, the empty track showing through, never narrower than 2px. */
.coverage__break {
  position: absolute;
  top: 0;
  bottom: 0;
  min-width: 2px;
  box-sizing: border-box;
  background: var(--tj-surface);
  border: 1px solid var(--tj-line);
}

.coverage__summary {
  flex: 0 0 auto;
  font-size: 12px;
  color: var(--tj-text2);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.coverage__gaps {
  margin: 0;
  padding: 0 0 0 18px;
  font-size: 12px;
  color: var(--tj-text2);
  font-variant-numeric: tabular-nums;
}

.coverage__sr {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
  list-style: none;
}
</style>
