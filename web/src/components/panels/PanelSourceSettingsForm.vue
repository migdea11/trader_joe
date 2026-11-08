<script setup lang="ts">
// The source's settings for the selected panel (tj-7zjov1.37): shared by every panel on that source, so a
// change is worded as the source's, not the panel's. A write appends a new source settings version (POST
// panel-sources/{source}/settings-versions); all four numbers are sent, none may be negative, and the
// reason is optional. Ramp 0 pauses creation of new panel datasets. The server decides what is allowed.
import { computed, ref, watch } from 'vue'
import type { PanelDetail } from '@generated/trader_joe/proto/ui/v1/panels_pb'

import { sourceLabel } from '@/catalog/labels'
import { timestampToInstant } from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import { usePanelsStore } from '@/stores/panels'
import { titleCase } from '@/theme/copy'

import { effectivePassText, panelWriteNotice, parseCount } from './panelModel'
import type { PanelWriteNotice } from './panelModel'

const props = defineProps<{ detail: PanelDetail }>()

const store = usePanelsStore()
const { dateTime } = useFormatters()

const version = computed(() => props.detail.currentSourceVersion)
const name = computed(() => sourceLabel(props.detail.source))

function fieldsFromVersion(): { ramp: string; cap: string; pre: string; post: string } {
  const settings = version.value?.settings
  return {
    ramp: settings === undefined ? '' : String(settings.rampDatasetsPerHour),
    cap: settings === undefined ? '' : String(settings.requestCapPerHour),
    pre: settings === undefined ? '' : String(settings.corporateActionWindowPreDays),
    post: settings === undefined ? '' : String(settings.corporateActionWindowPostDays),
  }
}

const fields = ref(fieldsFromVersion())
const reason = ref('')
const notice = ref<PanelWriteNotice | null>(null)
const busy = computed(() => store.writing)

function reset(): void {
  fields.value = fieldsFromVersion()
  reason.value = ''
  notice.value = null
}

watch(() => `${props.detail.id}:${version.value?.versionNo ?? 0}`, reset)

const problem = computed(() => {
  const counts = [fields.value.ramp, fields.value.cap, fields.value.pre, fields.value.post]
  if (counts.some((text) => parseCount(text) === null)) return 'Give each number as a whole number, 0 or more.'
  return ''
})

const versionText = computed(() => {
  const current = version.value
  if (current === undefined) return ''
  const saved = current.savedAt === undefined ? '' : ` saved ${dateTime(timestampToInstant(current.savedAt))}`
  const pass =
    current.effectivePass === undefined ? 'pending: no pass has run yet' : effectivePassText(current.effectivePass, dateTime)
  return `Version ${current.versionNo}${saved} by ${current.actor}; ${pass}`
})

async function save(): Promise<void> {
  if (busy.value || problem.value !== '') return
  const outcome = await store.saveSource(props.detail.source, {
    rampDatasetsPerHour: parseCount(fields.value.ramp) ?? 0,
    requestCapPerHour: parseCount(fields.value.cap) ?? 0,
    corporateActionWindowPreDays: parseCount(fields.value.pre) ?? 0,
    corporateActionWindowPostDays: parseCount(fields.value.post) ?? 0,
    reason: reason.value.trim(),
  })
  notice.value = panelWriteNotice(outcome, 'source')
  if (outcome.kind === 'ok') reset()
}
</script>

<template>
  <section
    class="pn-card"
    aria-labelledby="source-settings-title"
    data-testid="source-settings"
  >
    <h2
      id="source-settings-title"
      class="pn-card__title"
    >
      {{ titleCase('Source Settings') }}: {{ name }}
    </h2>
    <p class="pn-hint">
      Shared by every panel on {{ name }}.
    </p>
    <p
      v-if="version"
      class="pn-hint"
      data-testid="source-version"
    >
      {{ versionText }}
    </p>
    <form
      class="pn-form"
      @submit.prevent="save"
    >
      <div class="pn-grid">
        <div class="pn-field">
          <label
            class="pn-label"
            for="source-ramp"
          >{{ titleCase('New Datasets per Hour') }}</label>
          <input
            id="source-ramp"
            v-model="fields.ramp"
            class="pn-input"
            type="text"
            inputmode="numeric"
            :disabled="busy"
            data-testid="source-ramp"
          >
        </div>
        <div class="pn-field">
          <label
            class="pn-label"
            for="source-cap"
          >{{ titleCase('Requests per Hour') }}</label>
          <input
            id="source-cap"
            v-model="fields.cap"
            class="pn-input"
            type="text"
            inputmode="numeric"
            :disabled="busy"
            data-testid="source-cap"
          >
        </div>
        <div class="pn-field">
          <label
            class="pn-label"
            for="source-pre"
          >{{ titleCase('Corporate Action Days Before') }}</label>
          <input
            id="source-pre"
            v-model="fields.pre"
            class="pn-input"
            type="text"
            inputmode="numeric"
            :disabled="busy"
            data-testid="source-pre"
          >
        </div>
        <div class="pn-field">
          <label
            class="pn-label"
            for="source-post"
          >{{ titleCase('Corporate Action Days After') }}</label>
          <input
            id="source-post"
            v-model="fields.post"
            class="pn-input"
            type="text"
            inputmode="numeric"
            :disabled="busy"
            data-testid="source-post"
          >
        </div>
      </div>
      <div class="pn-field">
        <label
          class="pn-label"
          for="source-reason"
        >{{ titleCase('Reason') }} (optional)</label>
        <textarea
          id="source-reason"
          v-model="reason"
          class="pn-input"
          rows="2"
          :disabled="busy"
          placeholder="Optional: why the source settings change"
          data-testid="source-reason"
        />
      </div>
      <p class="pn-hint">
        New datasets per hour 0 pauses creating panel datasets. The corporate action days are the span before a
        dataset's start and after its end that actions are pulled for.
      </p>
      <p
        v-if="problem"
        class="pn-hint"
        data-testid="source-problem"
      >
        {{ problem }}
      </p>
      <div class="pn-actions">
        <button
          type="submit"
          class="pn-btn pn-btn--primary"
          :disabled="busy || problem !== ''"
          data-testid="source-save"
        >
          {{ titleCase('Save Source Settings') }}
        </button>
      </div>
    </form>
    <p
      v-if="notice"
      class="pn-notice pn-notice--error"
      role="alert"
      data-testid="source-notice"
    >
      {{ notice.text }}
      <span
        v-if="notice.errorId"
        class="pn-error-id"
      >Error ID: {{ notice.errorId }}</span>
    </p>
  </section>
</template>
