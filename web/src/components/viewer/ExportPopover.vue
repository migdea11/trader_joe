<script setup lang="ts">
// The Viewer's Export CSV (tj-grna9p.126, canvas board "Export CSV popover"): the header button and
// the popover it opens. From and To start as the Viewer's current window; ticking Earliest or Latest
// locks that side to the stored extent and dims its field (40%, not editable). Export walks the bars
// by cursor and downloads one file (viewer/exportCsv.ts); while it runs the popover stays open with a
// progress bar and Stop. Stop aborts every request and drops the partial; Cancel closes. An error shows
// in place with its Error ID and Try Again. A plain popover, not a menu: a dialog that is not modal,
// closed by Escape, Cancel or a click outside (never mid-export), and focus returns to the button.
//
// Both date fields live in ONE three-column grid (label, field, tick), so they share a width and the
// ticks line up. On a narrow screen the grid stays: the popover is capped at the viewport's width and
// the fields shrink (min-width 0) while the labels and ticks keep their size.
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Download } from '@lucide/vue'
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { isIntraday, granularityLabel } from '@/catalog/labels'
import { formatNumber, timestampToInstant } from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import { describeError } from '@/stores/datasetCatalog'
import type { CatalogError } from '@/stores/datasetCatalog'
import { titleCase } from '@/theme/copy'
import { exportBarsCsv } from '@/viewer/exportCsv'
import { exportFileName, planExport } from '@/viewer/exportRange'
import { displayDay } from '@/viewer/range'
import type { ViewerRange } from '@/viewer/range'

const props = defineProps<{
  dataset: DatasetSummary
  /** The Viewer's current window (the From and To the sidebar shows). */
  range: ViewerRange
  /** Nothing stored, nothing to export. */
  disabled?: boolean
}>()

const { date, timeZone } = useFormatters()

const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const open = ref(false)

const fromText = ref('')
const toText = ref('')
const earliest = ref(false)
const latest = ref(false)

type Phase = 'idle' | 'busy' | 'error'
const phase = ref<Phase>('idle')
const progress = ref(0)
const problem = ref<string | undefined>()
const failure = ref<CatalogError | null>(null)
let controller: AbortController | null = null

const intraday = computed(() => isIntraday(props.dataset.granularity))
const granularity = computed(() => granularityLabel(props.dataset.granularity))
const firstBar = computed(() => (props.dataset.firstBar ? timestampToInstant(props.dataset.firstBar) : undefined))
const lastBar = computed(() => (props.dataset.lastBar ? timestampToInstant(props.dataset.lastBar) : undefined))

// What a ticked field shows: the stored extent's day. The typed text is kept, so unticking restores it.
const fromShown = computed(() =>
  earliest.value ? (firstBar.value === undefined ? '' : date(firstBar.value)) : fromText.value,
)
const toShown = computed(() =>
  latest.value ? (lastBar.value === undefined ? '' : date(lastBar.value)) : toText.value,
)

const busy = computed(() => phase.value === 'busy')
const percent = computed(() => Math.round(progress.value * 100))

const note = computed(() => {
  if (busy.value) return 'The file downloads when it is ready.'
  if (earliest.value && latest.value) {
    return 'Everything stored for this dataset. A long export can take a while; you can cancel.'
  }
  // Latest omits the end bound, so the export reads to the last stored bar and that bar is included.
  // A typed To day is exclusive (the app's half-open rule), and the note says so only where it applies.
  if (latest.value) return 'Exports the bars from this date to the last stored bar at this granularity.'
  return 'Exports the bars between these dates at this granularity. To is not included.'
})

function openPopover(): void {
  // Each opening starts from the Viewer's window as it is now; an unset side is the stored extent.
  fromText.value = displayDay(props.range.from)
  toText.value = displayDay(props.range.to)
  earliest.value = props.range.from === undefined
  latest.value = props.range.to === undefined
  phase.value = 'idle'
  progress.value = 0
  problem.value = undefined
  failure.value = null
  open.value = true
}

function abortRun(): void {
  controller?.abort()
  controller = null
}

function close(returnFocus = true): void {
  abortRun()
  open.value = false
  phase.value = 'idle'
  if (returnFocus) void nextTick(() => trigger.value?.focus())
}

function toggle(): void {
  if (open.value) close()
  else openPopover()
}

// A click outside closes an idle popover; a running export keeps it (Stop and Escape end that).
function onPointerDown(event: PointerEvent): void {
  if (busy.value) return
  if (root.value !== null && event.target instanceof Node && !root.value.contains(event.target)) close(false)
}

watch(open, (isOpen) => {
  if (isOpen) {
    document.addEventListener('pointerdown', onPointerDown)
    void nextTick(() => root.value?.querySelector<HTMLElement>('.export__pop input:not(:disabled)')?.focus())
  } else {
    document.removeEventListener('pointerdown', onPointerDown)
  }
})

onBeforeUnmount(() => {
  abortRun()
  document.removeEventListener('pointerdown', onPointerDown)
})

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape' && open.value) {
    event.stopPropagation()
    close()
  }
}

function stop(): void {
  abortRun()
  phase.value = 'idle'
  progress.value = 0
}

function download(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  URL.revokeObjectURL(url)
}

async function run(): Promise<void> {
  if (busy.value) return
  problem.value = undefined
  failure.value = null
  const planned = planExport({
    fromText: fromText.value,
    toText: toText.value,
    earliest: earliest.value,
    latest: latest.value,
    timeZone: timeZone.value,
    firstBar: firstBar.value,
    lastBar: lastBar.value,
  })
  if (!planned.ok) {
    problem.value = planned.message
    return
  }
  const { plan } = planned
  const mine = new AbortController()
  controller = mine
  phase.value = 'busy'
  progress.value = 0
  try {
    const result = await exportBarsCsv({
      datasetId: props.dataset.id,
      start: plan.start,
      end: plan.end,
      timeZone: timeZone.value,
      intraday: intraday.value,
      spanStart: plan.spanStart,
      spanEnd: plan.spanEnd,
      signal: mine.signal,
      onProgress: (fraction) => {
        if (controller === mine) progress.value = fraction
      },
    })
    // Stopped or closed: the partial file is dropped and nothing is shown.
    if (result === null || controller !== mine) return
    controller = null
    if (result.bars === 0) {
      phase.value = 'idle'
      problem.value = 'There are no bars between these dates.'
      return
    }
    download(result.blob, exportFileName(props.dataset.assetSymbol, granularity.value, plan))
    close()
  } catch (caught) {
    if (controller !== mine) return
    controller = null
    failure.value = describeError(caught)
    phase.value = 'error'
  }
}
</script>

<template>
  <span
    ref="root"
    class="export"
    @keydown="onKeydown"
  >
    <button
      ref="trigger"
      type="button"
      class="btn"
      :disabled="disabled"
      aria-haspopup="dialog"
      :aria-expanded="open"
      aria-controls="export-popover"
      data-testid="export-csv"
      @click="toggle"
    >
      <Download
        :size="16"
        aria-hidden="true"
      />
      Export CSV
    </button>

    <div
      v-if="open"
      id="export-popover"
      class="export__pop"
      role="dialog"
      aria-labelledby="export-title"
      data-testid="export-popover"
    >
      <h2
        id="export-title"
        class="export__title"
      >
        {{ titleCase('Export range') }}
      </h2>

      <div class="export__grid">
        <label
          class="export__label"
          for="export-from"
        >From</label>
        <input
          id="export-from"
          :value="fromShown"
          :disabled="earliest || busy"
          :class="{ 'is-locked': earliest }"
          type="text"
          inputmode="numeric"
          placeholder="YYYY/MM/DD"
          data-testid="export-from"
          @input="fromText = ($event.target as HTMLInputElement).value"
          @keydown.enter="run"
        >
        <label class="export__tick">
          <input
            v-model="earliest"
            type="checkbox"
            :disabled="busy"
            data-testid="export-earliest"
          >
          Earliest
        </label>

        <label
          class="export__label"
          for="export-to"
        >To</label>
        <input
          id="export-to"
          :value="toShown"
          :disabled="latest || busy"
          :class="{ 'is-locked': latest }"
          type="text"
          inputmode="numeric"
          placeholder="YYYY/MM/DD"
          data-testid="export-to"
          @input="toText = ($event.target as HTMLInputElement).value"
          @keydown.enter="run"
        >
        <label class="export__tick">
          <input
            v-model="latest"
            type="checkbox"
            :disabled="busy"
            data-testid="export-latest"
          >
          Latest
        </label>
      </div>

      <p class="export__note">
        {{ note }}
      </p>

      <div
        v-if="busy"
        class="export__progress"
      >
        <div
          class="export__bar"
          role="progressbar"
          aria-label="Export progress"
          aria-valuemin="0"
          aria-valuemax="100"
          :aria-valuenow="percent"
          data-testid="export-progress"
        >
          <div
            class="export__fill"
            :style="{ width: `${percent}%` }"
          />
        </div>
        <span class="export__text">Preparing the file: {{ formatNumber(percent) }}%</span>
      </div>

      <div
        v-if="problem"
        class="export__alert"
        role="alert"
        data-testid="export-problem"
      >
        {{ problem }}
      </div>

      <div
        v-if="failure"
        class="export__alert"
        role="alert"
        data-testid="export-error"
      >
        <span>Could not export. {{ failure.message }}</span>
        <span
          v-if="failure.errorId"
          class="export__id"
        >Error ID: {{ failure.errorId }}</span>
      </div>

      <div class="export__actions">
        <button
          v-if="busy"
          type="button"
          class="btn"
          data-testid="export-stop"
          @click="stop"
        >
          Stop
        </button>
        <button
          v-else
          type="button"
          class="btn"
          data-testid="export-cancel"
          @click="close()"
        >
          Cancel
        </button>
        <button
          type="button"
          class="btn btn--primary"
          :disabled="busy"
          data-testid="export-run"
          @click="run"
        >
          {{ busy ? 'Exporting' : phase === 'error' ? 'Try Again' : 'Export' }}
        </button>
      </div>
    </div>
  </span>
</template>

<style scoped>
.export {
  position: relative;
  display: inline-flex;
}

.btn {
  height: var(--tj-control-height);
  padding: 0 12px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
  background: transparent;
  color: var(--tj-text);
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}

.btn:disabled {
  color: var(--tj-text3);
  cursor: not-allowed;
}

.btn--primary {
  border-color: transparent;
  background: var(--tj-accent-fill);
  color: var(--tj-on-accent);
}

.btn--primary:disabled {
  color: var(--tj-on-accent);
  opacity: 0.6;
}

.export__pop {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 20;
  width: 340px;
  max-width: calc(100vw - 2 * var(--tj-main-padding));
  box-sizing: border-box;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: var(--tj-surface);
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-card);
  color: var(--tj-text);
}

.export__title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}

/* One grid for both rows: the fields share a width and the ticks line up. */
.export__grid {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
}

.export__label {
  font-size: 12px;
  color: var(--tj-text2);
}

.export__grid input[type='text'] {
  min-width: 0;
  height: var(--tj-control-height);
  box-sizing: border-box;
  padding: 0 10px;
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
  background: var(--tj-raised);
  color: var(--tj-text);
  font-family: var(--tj-font-mono);
  font-size: 13px;
}

.export__grid input[type='text'].is-locked {
  opacity: 0.4;
  cursor: not-allowed;
}

.export__tick {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  white-space: nowrap;
}

.export__tick input {
  margin: 0;
  accent-color: var(--tj-accent-tint);
}

.export__note {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--tj-text2);
}

.export__progress {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.export__bar {
  height: 6px;
  border-radius: 3px;
  background: var(--tj-raised);
  overflow: hidden;
}

.export__fill {
  height: 6px;
  background: var(--tj-accent-tint);
}

.export__text {
  font-size: 12px;
  color: var(--tj-text2);
}

.export__alert {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border: 1px solid var(--tj-status-fail);
  border-radius: var(--tj-radius-control);
  font-size: 12px;
}

.export__id {
  font-family: var(--tj-font-mono);
  color: var(--tj-text3);
}

.export__actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
