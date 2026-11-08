<script setup lang="ts">
// Create a panel (tj-7zjov1.37; owner ruling 2026-10-27: a UI action with a recorded reason). Name, source,
// enabled, size, cadence (shown as Monthly (default) and sent UNSPECIFIED unless the operator picks one),
// the filters and a required reason. The source's four settings fields show only while the source has no
// settings version (its versions page is empty): ramp and cap are required then, the window days optional
// (the store defaults them). The write does not run the first pass: the page shows the new panel as first
// pass pending and re-reads it. Panels are UI-only; nothing here is a client or SDK surface.
import { computed, ref, watch } from 'vue'
import { DataSource } from '@generated/trader_joe/proto/market/v1/enums_pb'

import { getSourceSettingsVersions } from '@/api'
import { sourceLabel } from '@/catalog/labels'
import { usePanelsStore } from '@/stores/panels'
import { PANEL_REFUSAL, titleCase } from '@/theme/copy'

import PanelFilterFields from './PanelFilterFields.vue'
import { draftProblem, draftToSettingsInit, emptyDraft, panelWriteNotice, parseCount } from './panelModel'
import type { PanelWriteNotice } from './panelModel'

const emit = defineEmits<{
  /** The panel was created (or the operator cancelled): the page closes the form. */
  close: []
}>()

const store = usePanelsStore()

const SOURCES = [DataSource.ALPACA_API, DataSource.IB_API, DataSource.MANUAL_ENTRY]

const name = ref('')
const source = ref<DataSource>(DataSource.UNSPECIFIED)
const draft = ref(emptyDraft())
const reason = ref('')
const ramp = ref('')
const cap = ref('')
const pre = ref('')
const post = ref('')
const notice = ref<PanelWriteNotice | null>(null)
const busy = computed(() => store.writing)

// null: not known yet (no source chosen, or the read is out); true: the source has no settings version.
const sourceNeedsSettings = ref<boolean | null>(null)
const sourceProblem = ref(false)
let probe = 0

watch(source, async (chosen) => {
  const mine = ++probe
  sourceNeedsSettings.value = null
  sourceProblem.value = false
  if (chosen === DataSource.UNSPECIFIED) return
  try {
    const page = await getSourceSettingsVersions(chosen, { limit: 1 })
    if (mine === probe) sourceNeedsSettings.value = page.items.length === 0
  } catch {
    if (mine === probe) sourceProblem.value = true
  }
})

const windowText = (text: string): boolean => text.trim() === '' || parseCount(text) !== null

// The store's unique check is on the trimmed name, exact case; the loaded panel list is what the page knows.
const nameTaken = computed(() => {
  const proposed = name.value.trim()
  return proposed !== '' && store.panels.some((item) => item.name === proposed)
})

const problem = computed(() => {
  if (name.value.trim() === '') return 'Give the panel a name.'
  if (nameTaken.value) return PANEL_REFUSAL.nameTaken(name.value.trim())
  if (source.value === DataSource.UNSPECIFIED) return 'Choose a source.'
  if (sourceProblem.value) return 'The source settings could not be read. Choose the source again.'
  if (sourceNeedsSettings.value === null) return 'Reading the source settings.'
  const base = draftProblem(draft.value, reason.value, { cadenceRequired: false })
  if (base !== '') return base
  if (sourceNeedsSettings.value) {
    if (parseCount(ramp.value) === null || parseCount(cap.value) === null) {
      return 'This source has no settings yet: give the new datasets per hour and the requests per hour.'
    }
    if (!windowText(pre.value) || !windowText(post.value)) return 'Give each corporate action day count as a whole number.'
  }
  return ''
})

function optionalCount(text: string): number | undefined {
  return text.trim() === '' ? undefined : (parseCount(text) ?? undefined)
}

async function submit(): Promise<void> {
  if (busy.value || problem.value !== '') return
  const sourceSettings = sourceNeedsSettings.value
    ? {
        rampDatasetsPerHour: parseCount(ramp.value) ?? 0,
        requestCapPerHour: parseCount(cap.value) ?? 0,
        corporateActionWindowPreDays: optionalCount(pre.value),
        corporateActionWindowPostDays: optionalCount(post.value),
      }
    : {}
  const outcome = await store.create({
    ...draftToSettingsInit(draft.value),
    ...sourceSettings,
    name: name.value.trim(),
    source: source.value,
    reason: reason.value.trim(),
  })
  notice.value = panelWriteNotice(outcome, 'create')
  // The source has settings now: the fields that asked for them go, as a re-read of the source would.
  if (outcome.kind === 'moved-on' && outcome.what === 'source-has-settings') sourceNeedsSettings.value = false
  if (outcome.kind === 'created') emit('close')
}
</script>

<template>
  <section
    class="pn-card"
    aria-labelledby="panel-create-title"
    data-testid="panel-create"
  >
    <h2
      id="panel-create-title"
      class="pn-card__title"
    >
      {{ titleCase('Create Panel') }}
    </h2>
    <form
      class="pn-form"
      @submit.prevent="submit"
    >
      <div class="pn-grid">
        <div class="pn-field">
          <label
            class="pn-label"
            for="create-name"
          >{{ titleCase('Name') }}</label>
          <input
            id="create-name"
            v-model="name"
            class="pn-input"
            type="text"
            :disabled="busy"
            data-testid="create-name"
          >
        </div>
        <div class="pn-field">
          <label
            class="pn-label"
            for="create-source"
          >{{ titleCase('Source') }}</label>
          <select
            id="create-source"
            v-model="source"
            class="pn-input"
            :disabled="busy"
            data-testid="create-source"
          >
            <option
              :value="DataSource.UNSPECIFIED"
              disabled
            >
              Choose a source
            </option>
            <option
              v-for="choice in SOURCES"
              :key="choice"
              :value="choice"
            >
              {{ sourceLabel(choice) }}
            </option>
          </select>
        </div>
      </div>
      <label class="pn-check">
        <input
          v-model="draft.enabled"
          type="checkbox"
          :disabled="busy"
          data-testid="create-enabled"
        >
        Enabled
      </label>
      <PanelFilterFields
        v-model="draft"
        id-prefix="create"
        :disabled="busy"
        :cadence-optional="true"
      />

      <template v-if="sourceNeedsSettings">
        <p class="pn-hint">
          {{ sourceLabel(source) }} has no source settings yet. They are shared by every panel on it.
        </p>
        <div class="pn-grid">
          <div class="pn-field">
            <label
              class="pn-label"
              for="create-ramp"
            >{{ titleCase('New Datasets per Hour') }}</label>
            <input
              id="create-ramp"
              v-model="ramp"
              class="pn-input"
              type="text"
              inputmode="numeric"
              :disabled="busy"
              data-testid="create-ramp"
            >
          </div>
          <div class="pn-field">
            <label
              class="pn-label"
              for="create-cap"
            >{{ titleCase('Requests per Hour') }}</label>
            <input
              id="create-cap"
              v-model="cap"
              class="pn-input"
              type="text"
              inputmode="numeric"
              :disabled="busy"
              data-testid="create-cap"
            >
          </div>
          <div class="pn-field">
            <label
              class="pn-label"
              for="create-pre"
            >{{ titleCase('Corporate Action Days Before') }}</label>
            <input
              id="create-pre"
              v-model="pre"
              class="pn-input"
              type="text"
              inputmode="numeric"
              placeholder="Store default"
              :disabled="busy"
              data-testid="create-pre"
            >
          </div>
          <div class="pn-field">
            <label
              class="pn-label"
              for="create-post"
            >{{ titleCase('Corporate Action Days After') }}</label>
            <input
              id="create-post"
              v-model="post"
              class="pn-input"
              type="text"
              inputmode="numeric"
              placeholder="Store default"
              :disabled="busy"
              data-testid="create-post"
            >
          </div>
        </div>
      </template>

      <div class="pn-field">
        <label
          class="pn-label"
          for="create-reason"
        >{{ titleCase('Reason') }}</label>
        <textarea
          id="create-reason"
          v-model="reason"
          class="pn-input"
          rows="2"
          :disabled="busy"
          placeholder="Why the panel is created; recorded with its first settings"
          data-testid="create-reason"
        />
      </div>
      <p
        v-if="problem"
        class="pn-hint"
        data-testid="create-problem"
      >
        {{ problem }}
      </p>
      <div class="pn-actions">
        <button
          type="submit"
          class="pn-btn pn-btn--primary"
          :disabled="busy || problem !== ''"
          data-testid="create-submit"
        >
          {{ titleCase('Create Panel') }}
        </button>
        <button
          type="button"
          class="pn-btn"
          :disabled="busy"
          data-testid="create-cancel"
          @click="emit('close')"
        >
          {{ titleCase('Cancel') }}
        </button>
      </div>
    </form>
    <p
      v-if="notice"
      class="pn-notice pn-notice--error"
      role="alert"
      data-testid="create-notice"
    >
      {{ notice.text }}
      <span
        v-if="notice.errorId"
        class="pn-error-id"
      >Error ID: {{ notice.errorId }}</span>
    </p>
  </section>
</template>
