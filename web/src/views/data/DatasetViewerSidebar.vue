<script setup lang="ts">
// The Viewer sidebar (tj-grna9p.33), the `sidebar` view of the data-dataset-viewer route, per the
// canvas board "Shell · Viewer": Dataset, Range (From and To, half-open), Granularity (a switch to
// the SIBLING dataset at that width; no client resampling), Adjustment (Raw only: Split, All and
// Compare Raw vs Adjusted are disabled with the reason, owner ruling tj-kqsnxb) and Overlays and
// Panes (SMA 20, SMA 50, Volume pane).
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'

import { GRANULARITY_OPTIONS, dataTypeLabel, granularityLabel, sourceFeedLabel } from '@/catalog/labels'
import { VIEWER_ROUTE } from '@/catalog/rows'
import { SMA_PERIODS, useDatasetViewerStore } from '@/stores/datasetViewer'
import { CORPORATE_ACTIONS_REASON } from '@/viewer/copy'
import { displayDay, parseDay, rangeProblem } from '@/viewer/range'
import type { ViewerRange } from '@/viewer/range'
import { useViewerRange } from '@/viewer/useViewerRange'

const router = useRouter()
const store = useDatasetViewerStore()
const { dataset } = storeToRefs(store)
const { range, setRange } = useViewerRange()

// Siblings by width: the dataset itself, and the others that exist.
const options = computed(() =>
  GRANULARITY_OPTIONS.map((granularity) => {
    const sibling = dataset.value?.siblings.find((s) => s.granularity === granularity)
    return {
      granularity,
      label: granularityLabel(granularity),
      current: dataset.value?.granularity === granularity,
      id: sibling?.id,
      enabled: sibling !== undefined,
    }
  }),
)

function switchGranularity(id: string | undefined): void {
  if (id === undefined) return
  // The window carries over: the same days at another width.
  void router.push({ name: VIEWER_ROUTE, params: { id }, query: router.currentRoute.value.query })
}

// The typed text of each bound; an input the user left unparsed shows its message instead of being
// written to the URL.
const fromText = ref(displayDay(range.value.from))
const toText = ref(displayDay(range.value.to))
const textError = ref<string | undefined>()

watch(range, (next) => {
  fromText.value = displayDay(next.from)
  toText.value = displayDay(next.to)
  textError.value = undefined
})

const problem = computed(() => textError.value ?? rangeProblem(range.value))

function apply(): void {
  const next: ViewerRange = {}
  for (const [key, text] of [
    ['from', fromText.value],
    ['to', toText.value],
  ] as const) {
    if (text.trim() === '') continue
    const day = parseDay(text)
    if (day === null) {
      textError.value = `${key === 'from' ? 'From' : 'To'} must be a date as YYYY/MM/DD.`
      return
    }
    next[key] = day
  }
  textError.value = undefined
  void setRange(next)
}

function clearRange(): void {
  void setRange({})
}

const hasRange = computed(() => range.value.from !== undefined || range.value.to !== undefined)
</script>

<template>
  <section
    class="side-section"
    aria-label="Dataset"
  >
    <span class="side-label">Dataset</span>
    <template v-if="dataset">
      <span
        class="side-symbol"
        data-testid="viewer-symbol"
      >{{ dataset.assetSymbol }}</span>
      <span class="side-meta">
        {{ sourceFeedLabel(dataset.source, dataset.feed) }} · {{ dataTypeLabel(dataset.dataType) }} · {{ dataset.owner }}
      </span>
    </template>
    <span
      v-else
      class="side-meta"
    >—</span>
    <RouterLink
      class="side-link"
      :to="{ name: 'data-datasets' }"
    >
      Choose Another Dataset
    </RouterLink>
  </section>

  <section
    class="side-section"
    aria-label="Range"
  >
    <div class="side-heading">
      <span class="side-label">Range</span>
      <button
        v-if="hasRange"
        type="button"
        class="side-clear"
        @click="clearRange"
      >
        Clear
      </button>
    </div>
    <label class="side-field">
      From
      <input
        v-model="fromText"
        type="text"
        inputmode="numeric"
        placeholder="YYYY/MM/DD"
        data-testid="range-from"
        @change="apply"
        @keydown.enter="apply"
      >
    </label>
    <label class="side-field">
      To
      <input
        v-model="toText"
        type="text"
        inputmode="numeric"
        placeholder="YYYY/MM/DD"
        data-testid="range-to"
        @change="apply"
        @keydown.enter="apply"
      >
    </label>
    <span class="side-hint">The range is half-open: To is not included.</span>
    <span
      v-if="problem"
      class="side-problem"
      role="alert"
      data-testid="range-problem"
    >{{ problem }}</span>
  </section>

  <section
    class="side-section"
    aria-label="Granularity"
  >
    <span class="side-label">Granularity</span>
    <div class="side-chips">
      <button
        v-for="option in options"
        :key="option.granularity"
        type="button"
        class="side-chip"
        :class="{ 'is-active': option.current }"
        :aria-pressed="option.current"
        :disabled="!option.enabled && !option.current"
        :title="option.enabled || option.current ? undefined : 'No dataset at this width.'"
        :data-granularity="option.label"
        @click="option.current ? undefined : switchGranularity(option.id)"
      >
        {{ option.label }}
      </button>
    </div>
  </section>

  <section
    class="side-section"
    aria-label="Adjustment"
  >
    <span class="side-label">Adjustment</span>
    <div class="side-chips">
      <button
        type="button"
        class="side-chip is-active"
        aria-pressed="true"
        data-adjustment="raw"
      >
        Raw
      </button>
      <button
        type="button"
        class="side-chip"
        disabled
        :title="CORPORATE_ACTIONS_REASON"
        data-adjustment="split"
      >
        Split
      </button>
      <button
        type="button"
        class="side-chip"
        disabled
        :title="CORPORATE_ACTIONS_REASON"
        data-adjustment="all"
      >
        All
      </button>
    </div>
    <span
      class="side-hint"
      data-testid="adjustment-reason"
    >Split and All need corporate actions. {{ CORPORATE_ACTIONS_REASON }}</span>
  </section>

  <section
    class="side-section"
    aria-label="Overlays and Panes"
  >
    <span class="side-label">Overlays and Panes</span>
    <label
      v-for="period in SMA_PERIODS"
      :key="period"
      class="side-option"
    >
      <input
        v-model="store.sma[period]"
        type="checkbox"
        :data-overlay="`sma-${period}`"
      >
      SMA {{ period }}
    </label>
    <label class="side-option">
      <input
        v-model="store.showVolume"
        type="checkbox"
        data-overlay="volume"
      >
      Volume pane
    </label>
    <label class="side-option is-disabled">
      <input
        type="checkbox"
        disabled
        data-overlay="compare"
      >
      Compare Raw vs Adjusted
    </label>
    <span class="side-hint">{{ CORPORATE_ACTIONS_REASON }}</span>
  </section>
</template>

<style scoped>
.side-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.side-heading {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}

.side-label {
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--tj-text3);
}

.side-symbol {
  font-size: 15px;
  font-weight: 600;
}

.side-meta,
.side-hint {
  font-size: 12px;
  color: var(--tj-text2);
}

.side-hint {
  color: var(--tj-text3);
}

.side-problem {
  font-size: 12px;
  color: var(--tj-status-fail);
}

.side-link,
.side-clear {
  border: 0;
  padding: 0;
  background: transparent;
  color: var(--tj-accent-tint);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  text-decoration: none;
}

.side-field {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  font-size: 13px;
  color: var(--tj-text2);
}

.side-field input {
  width: 120px;
  height: var(--tj-control-height);
  box-sizing: border-box;
  padding: 0 10px;
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
  background: var(--tj-surface);
  color: var(--tj-text);
  font-family: var(--tj-font-mono);
  font-size: 12px;
}

.side-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.side-chip {
  height: var(--tj-control-height);
  padding: 0 10px;
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
  background: transparent;
  color: var(--tj-text);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.side-chip.is-active {
  background: var(--tj-raised);
}

.side-chip:disabled {
  color: var(--tj-text3);
  cursor: not-allowed;
}

.side-option {
  display: flex;
  align-items: center;
  gap: 10px;
  height: var(--tj-filter-row-height);
  font-size: 13px;
  color: var(--tj-text);
}

.side-option.is-disabled {
  color: var(--tj-text3);
}

.side-option input {
  margin: 0;
}
</style>
