<script setup lang="ts">
// The selected panel's settings panel (tj-7zjov1.37): the current settings version, an edit form and
// Enable or Disable, each a settings write (POST settings-versions) that needs a reason. A write appends a
// new version and replaces the settings as sent, so Enable or Disable sends the SAVED settings with only
// `enabled` flipped (unsaved edits in the form are not smuggled in), and Save Settings sends the form.
// The server stamps the actor and the time and decides whether the values are allowed; a refusal leaves
// the panel as it was and is worded in place. The new version shows as pending until a pass has run.
import { computed, ref, watch } from 'vue'
import type { PanelDetail } from '@generated/trader_joe/proto/ui/v1/panels_pb'

import { useFormatters } from '@/format/useFormatters'
import { timestampToInstant } from '@/format/formatters'
import { usePanelsStore } from '@/stores/panels'
import { titleCase } from '@/theme/copy'

import PanelFilterFields from './PanelFilterFields.vue'
import {
  cadenceLabel,
  draftFromSettings,
  emptyDraft,
  draftProblem,
  draftToSettingsInit,
  effectivePassText,
  panelWriteNotice,
  settingsInitWithEnabled,
} from './panelModel'
import type { PanelDraft, PanelWriteNotice, SettingsInit } from './panelModel'

const props = defineProps<{ detail: PanelDetail }>()

const store = usePanelsStore()
const { dateTime } = useFormatters()

const current = computed(() => props.detail.currentVersion)
const settings = computed(() => current.value?.settings)
const enabled = computed(() => settings.value?.enabled === true)

const draft = ref<PanelDraft>(settings.value === undefined ? emptyDraft() : draftFromSettings(settings.value))
const reason = ref('')
const notice = ref<PanelWriteNotice | null>(null)
const busy = computed(() => store.writing)

function reset(): void {
  if (settings.value !== undefined) draft.value = draftFromSettings(settings.value)
  reason.value = ''
  notice.value = null
}

// A different panel, or a newer saved version, starts the form over from what is saved.
watch(() => `${props.detail.id}:${current.value?.versionNo ?? 0}`, reset)

const problem = computed(() => draftProblem(draft.value, reason.value, { cadenceRequired: true }))
const reasonProblem = computed(() => (reason.value.trim() === '' ? 'Give a reason for the change.' : ''))

const versionText = computed(() => {
  const version = current.value
  if (version === undefined) return ''
  const saved = version.savedAt === undefined ? '' : ` saved ${dateTime(timestampToInstant(version.savedAt))}`
  return `Version ${version.versionNo}${saved} by ${version.actor}`
})

const passText = computed(() => {
  const pass = current.value?.effectivePass
  return pass === undefined ? 'Pending: no pass has run yet' : effectivePassText(pass, dateTime)
})

async function save(params: SettingsInit): Promise<void> {
  const outcome = await store.saveSettings({ ...params, reason: reason.value.trim() })
  notice.value = panelWriteNotice(outcome, 'settings')
  if (outcome.kind === 'ok') reset()
}

async function saveEdits(): Promise<void> {
  if (busy.value || problem.value !== '') return
  await save(draftToSettingsInit(draft.value))
}

async function toggleEnabled(): Promise<void> {
  const saved = settings.value
  if (busy.value || reasonProblem.value !== '' || saved === undefined) return
  await save(settingsInitWithEnabled(saved, !saved.enabled))
}
</script>

<template>
  <section
    class="pn-card"
    aria-labelledby="panel-settings-title"
    data-testid="panel-settings"
  >
    <h2
      id="panel-settings-title"
      class="pn-card__title"
    >
      {{ titleCase('Panel Settings') }}
    </h2>
    <dl
      v-if="settings"
      class="pn-facts"
    >
      <div>
        <dt>{{ titleCase('State') }}</dt>
        <dd data-fact="enabled">
          {{ enabled ? 'Enabled' : 'Disabled' }}
        </dd>
      </div>
      <div>
        <dt>{{ titleCase('Cadence') }}</dt>
        <dd data-fact="cadence">
          {{ cadenceLabel(settings.cadence) }}
        </dd>
      </div>
      <div>
        <dt>{{ titleCase('Size') }}</dt>
        <dd data-fact="size">
          {{ settings.size }}
        </dd>
      </div>
      <div>
        <dt>{{ titleCase('Version') }}</dt>
        <dd data-fact="version">
          {{ versionText }}
        </dd>
      </div>
      <div>
        <dt>{{ titleCase('In Effect From') }}</dt>
        <dd data-fact="effective">
          {{ passText }}
        </dd>
      </div>
    </dl>
    <p
      v-if="current && current.reason"
      class="pn-hint"
      data-testid="current-reason"
    >
      Reason: {{ current.reason }}
    </p>

    <form
      class="pn-form"
      @submit.prevent="saveEdits"
    >
      <PanelFilterFields
        v-model="draft"
        id-prefix="settings"
        :disabled="busy"
        :cadence-optional="false"
      />
      <div class="pn-field">
        <label
          class="pn-label"
          for="settings-reason"
        >{{ titleCase('Reason') }}</label>
        <textarea
          id="settings-reason"
          v-model="reason"
          class="pn-input"
          rows="2"
          :disabled="busy"
          placeholder="Why the settings change; recorded with the new version"
          data-testid="settings-reason"
        />
      </div>
      <p
        v-if="problem"
        class="pn-hint"
        data-testid="settings-problem"
      >
        {{ problem }}
      </p>
      <div class="pn-actions">
        <button
          type="submit"
          class="pn-btn pn-btn--primary"
          :disabled="busy || problem !== ''"
          data-testid="settings-save"
        >
          {{ titleCase('Save Settings') }}
        </button>
        <button
          type="button"
          class="pn-btn"
          :disabled="busy || reasonProblem !== ''"
          data-testid="settings-toggle"
          @click="toggleEnabled"
        >
          {{ titleCase(enabled ? 'Disable Panel' : 'Enable Panel') }}
        </button>
      </div>
      <p class="pn-hint">
        Each change is a new settings version, recorded with the reason. It takes effect from the next pass.
      </p>
    </form>

    <p
      v-if="notice"
      class="pn-notice pn-notice--error"
      role="alert"
      data-testid="settings-notice"
    >
      {{ notice.text }}
      <span
        v-if="notice.errorId"
        class="pn-error-id"
      >Error ID: {{ notice.errorId }}</span>
    </p>
  </section>
</template>
