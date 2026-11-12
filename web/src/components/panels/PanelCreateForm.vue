<script setup lang="ts">
// Create a panel (tj-7zjov1.37; owner ruling 2026-10-27: a UI action; the reason is optional since the ruling of 2026-11-06). Name, source,
// enabled, cadence (shown as Monthly (default) and sent UNSPECIFIED unless the operator picks one),
// the filters and an optional reason. The source's four settings fields show only while the source has no
// settings version (its versions page is empty): all four are optional then. An empty ramp or cap means no
// limit and an empty window is the store's default; an empty field is left out of the request. The write does not run the first pass: the page shows the new panel as first
// pass pending and re-reads it. Panels are UI-only; nothing here is a client or SDK surface.
import { computed, nextTick, ref, watch } from 'vue'
import { DataSource, Granularity } from '@generated/trader_joe/proto/market/v1/enums_pb'

import { getSourceSettingsVersions } from '@/api'
import { sourceLabel } from '@/catalog/labels'
import { usePanelsStore } from '@/stores/panels'
import { titleCase } from '@/theme/copy'

import PanelFilterFields from './PanelFilterFields.vue'
import {
  DEFAULT_PANEL_INTERVAL,
  PANEL_INTERVALS,
  createFieldErrors,
  draftToSettingsInit,
  emptyDraft,
  panelWriteNotice,
  parseCount,
} from './panelModel'
import type { PanelWriteNotice } from './panelModel'

const emit = defineEmits<{
  /** The panel was created (or the operator cancelled): the page closes the form. */
  close: []
}>()

const store = usePanelsStore()

const SOURCES = [DataSource.ALPACA_API, DataSource.IB_API, DataSource.MANUAL_ENTRY]

const name = ref('')
const source = ref<DataSource>(DataSource.UNSPECIFIED)
const granularity = ref<Granularity>(DEFAULT_PANEL_INTERVAL)
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

// The store's unique check is on the trimmed name, exact case; the loaded panel list is what the page knows.
const nameTaken = computed(() => {
  const proposed = name.value.trim()
  return proposed !== '' && store.panels.some((item) => item.name === proposed)
})

// Every empty or invalid field at once, from the one rule set in panelModel.ts: the click decides from this
// and the marks on the fields show it, so they cannot disagree.
const errors = computed(() =>
  createFieldErrors({
    name: name.value,
    nameTaken: nameTaken.value,
    source: source.value,
    granularity: granularity.value,
    sourceProblem: sourceProblem.value,
    sourceNeedsSettings: sourceNeedsSettings.value,
    ramp: ramp.value,
    cap: cap.value,
    pre: pre.value,
    post: post.value,
  }),
)
const blocked = computed(() => Object.keys(errors.value).length > 0)
// Validation runs on click (owner ruling 2026-11-06): nothing is marked until Create Panel is pressed once;
// after that the marks follow the fields live and each stays until its field is valid. Never reset.
const attempted = ref(false)
const shown = computed(() => (attempted.value ? errors.value : {}))
const formEl = ref<HTMLFormElement | null>(null)
const problem = computed(() => {
  const count = Object.keys(shown.value).length
  if (count === 0) return ''
  return `${count === 1 ? '1 marked field needs' : `${count} marked fields need`} a value before the panel can be created.`
})

function optionalCount(text: string): number | undefined {
  return text.trim() === '' ? undefined : (parseCount(text) ?? undefined)
}

async function submit(): Promise<void> {
  if (busy.value) return
  // Any press counts, so a name found taken after a sent create is marked too.
  attempted.value = true
  if (blocked.value) {
    await nextTick()
    formEl.value?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus()
    return
  }
  const sourceSettings = sourceNeedsSettings.value
    ? {
        rampDatasetsPerHour: optionalCount(ramp.value),
        requestCapPerHour: optionalCount(cap.value),
        corporateActionWindowPreDays: optionalCount(pre.value),
        corporateActionWindowPostDays: optionalCount(post.value),
      }
    : {}
  const outcome = await store.create({
    ...draftToSettingsInit(draft.value),
    ...sourceSettings,
    name: name.value.trim(),
    source: source.value,
    granularity: granularity.value,
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
      ref="formEl"
      class="pn-form"
      @submit.prevent="submit"
    >
      <p class="pn-hint">
        Fields marked * are required.
      </p>
      <div class="pn-grid">
        <div class="pn-field">
          <label
            class="pn-label"
            for="create-name"
          >{{ titleCase('Name') }}<span
            class="pn-required"
            :class="{ 'pn-required--marked': shown.name }"
            aria-hidden="true"
          > *</span></label>
          <input
            id="create-name"
            v-model="name"
            class="pn-input"
            type="text"
            aria-required="true"
            :aria-invalid="shown.name ? 'true' : undefined"
            :aria-describedby="shown.name ? 'create-name-error' : undefined"
            :disabled="busy"
            data-testid="create-name"
          >
          <p
            v-if="shown.name"
            id="create-name-error"
            class="pn-field-error"
            data-testid="create-name-error"
          >
            {{ shown.name }}
          </p>
        </div>
        <div class="pn-field">
          <label
            class="pn-label"
            for="create-source"
          >{{ titleCase('Source') }}<span
            class="pn-required"
            :class="{ 'pn-required--marked': shown.source }"
            aria-hidden="true"
          > *</span></label>
          <select
            id="create-source"
            v-model="source"
            class="pn-input"
            aria-required="true"
            :aria-invalid="shown.source ? 'true' : undefined"
            :aria-describedby="shown.source ? 'create-source-error' : undefined"
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
          <p
            v-if="shown.source"
            id="create-source-error"
            class="pn-field-error"
            data-testid="create-source-error"
          >
            {{ shown.source }}
          </p>
        </div>
        <div class="pn-field">
          <label
            class="pn-label"
            for="create-granularity"
          >{{ titleCase('Bar Interval') }}<span
            class="pn-required"
            :class="{ 'pn-required--marked': shown.granularity }"
            aria-hidden="true"
          > *</span></label>
          <select
            id="create-granularity"
            v-model="granularity"
            class="pn-input"
            aria-required="true"
            :aria-invalid="shown.granularity ? 'true' : undefined"
            :aria-describedby="shown.granularity ? 'create-granularity-error create-granularity-hint' : 'create-granularity-hint'"
            :disabled="busy"
            data-testid="create-granularity"
          >
            <option
              v-for="choice in PANEL_INTERVALS"
              :key="choice.value"
              :value="choice.value"
            >
              {{ choice.label }}
            </option>
          </select>
          <p
            v-if="shown.granularity"
            id="create-granularity-error"
            class="pn-field-error"
            data-testid="create-granularity-error"
          >
            {{ shown.granularity }}
          </p>
          <p
            id="create-granularity-hint"
            class="pn-hint"
            data-testid="create-granularity-hint"
          >
            Intraday bars arrive after the market close: the panel collects once a day, it is not a live feed.
            The interval cannot be changed later; another interval is another panel.
          </p>
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
              :aria-invalid="shown.ramp ? 'true' : undefined"
              :aria-describedby="shown.ramp ? 'create-ramp-error create-ramp-hint' : 'create-ramp-hint'"
              :disabled="busy"
              data-testid="create-ramp"
            >
            <p
              v-if="shown.ramp"
              id="create-ramp-error"
              class="pn-field-error"
              data-testid="create-ramp-error"
            >
              {{ shown.ramp }}
            </p>
            <p
              id="create-ramp-hint"
              class="pn-hint"
              data-testid="create-ramp-hint"
            >
              Leave empty for no limit.
            </p>
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
              :aria-invalid="shown.cap ? 'true' : undefined"
              :aria-describedby="shown.cap ? 'create-cap-error create-cap-hint' : 'create-cap-hint'"
              :disabled="busy"
              data-testid="create-cap"
            >
            <p
              v-if="shown.cap"
              id="create-cap-error"
              class="pn-field-error"
              data-testid="create-cap-error"
            >
              {{ shown.cap }}
            </p>
            <p
              id="create-cap-hint"
              class="pn-hint"
              data-testid="create-cap-hint"
            >
              Leave empty for no limit. Minimum 2: each new panel dataset costs 2 requests.
            </p>
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
              :aria-invalid="shown.pre ? 'true' : undefined"
              :aria-describedby="shown.pre ? 'create-pre-error' : undefined"
              :disabled="busy"
              data-testid="create-pre"
            >
            <p
              v-if="shown.pre"
              id="create-pre-error"
              class="pn-field-error"
              data-testid="create-pre-error"
            >
              {{ shown.pre }}
            </p>
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
              :aria-invalid="shown.post ? 'true' : undefined"
              :aria-describedby="shown.post ? 'create-post-error' : undefined"
              :disabled="busy"
              data-testid="create-post"
            >
            <p
              v-if="shown.post"
              id="create-post-error"
              class="pn-field-error"
              data-testid="create-post-error"
            >
              {{ shown.post }}
            </p>
          </div>
        </div>
      </template>

      <div class="pn-field">
        <label
          class="pn-label"
          for="create-reason"
        >{{ titleCase('Reason') }} (optional)</label>
        <textarea
          id="create-reason"
          v-model="reason"
          class="pn-input"
          rows="2"
          :disabled="busy"
          placeholder="Optional: why the panel is created"
          data-testid="create-reason"
        />
      </div>
      <p
        v-if="problem"
        id="create-problem"
        class="pn-hint"
        data-testid="create-problem"
      >
        {{ problem }}
      </p>
      <div class="pn-actions">
        <button
          type="submit"
          class="pn-btn pn-btn--primary"
          :disabled="busy"
          :aria-describedby="problem ? 'create-problem' : undefined"
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
