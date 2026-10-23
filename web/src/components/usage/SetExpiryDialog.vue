<script setup lang="ts">
// Set Expiry from the Usage tab's Stale Datasets table (tj-grna9p.62). It ALWAYS asks: the row's
// button opens this dialog and nothing is sent until its Set Expiry button is pressed. It follows the
// Dataset detail's Retention card (components/retention/RetentionCard.vue): the confirm names the
// dataset ("VFV 1d"); a dataset another owner holds shows its owner with a warning and needs a
// non-blank reason, and only then sends the confirmed override; the operator's own dataset never
// sends one. A typed-name confirmation is Delete's, not this write's: Set Expiry is not destructive
// at once. The server decides whether the write is allowed; every refusal leaves the dialog open and
// says why in place (the fixed words, components/retention/expiryWrite.ts): 409 asks for the reason
// again, 503 says to run make migrate, 422 is a bug. 404 closes the dialog: the store dropped the row.
//
// Focus goes into the dialog on open; Escape and Cancel close it and the screen puts focus back on
// the row's button. Tab stays inside while it is open.
import { computed, nextTick, onMounted, ref } from 'vue'
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { OPERATOR_CLIENT_ID } from '@/api'
import type { OperatorOverride } from '@/api'
import { granularityLabel } from '@/catalog/labels'
import { MISSING } from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import { useUsageStore } from '@/stores/usage'
import { titleCase } from '@/theme/copy'
import { parseDay, zonedDayStart } from '@/viewer/range'

import { refusalNotice, tomorrowText } from '@/components/retention/expiryWrite'
import type { WriteNotice } from '@/components/retention/expiryWrite'

const props = defineProps<{ dataset: DatasetSummary }>()
const emit = defineEmits<{
  /** Cancel, Escape, or the write went through: the screen restores focus to the row's button. */
  close: []
}>()

const store = useUsageStore()
const { timeZone } = useFormatters()

/** The one dataset, named as the operator knows it: symbol and granularity (VFV 1d). */
const name = computed(() => `${props.dataset.assetSymbol} ${granularityLabel(props.dataset.granularity)}`)
const heldElsewhere = computed(() => props.dataset.owner !== OPERATOR_CLIENT_ID)
const owner = computed(() => (props.dataset.owner === '' ? MISSING : props.dataset.owner))

const dayText = ref('')
// undefined: empty; null: not a day; string: the day (YYYY-MM-DD).
const day = computed(() => (dayText.value.trim() === '' ? undefined : (parseDay(dayText.value) ?? null)))
const dayProblem = computed(() => day.value === null)

const reason = ref('')
const notice = ref<WriteNotice | null>(null)
const busy = computed(() => store.writingId !== null)

// What still blocks Set Expiry, said next to the button (aria-describedby).
const hint = computed(() => {
  if (typeof day.value !== 'string') return `Type the expiry day as YYYY/MM/DD to enable ${titleCase('Set Expiry')}.`
  if (heldElsewhere.value && reason.value.trim() === '') {
    return `Give a reason above to enable ${titleCase('Set Expiry')}.`
  }
  return ''
})
const canSubmit = computed(() => !busy.value && hint.value === '')

function override(): OperatorOverride | undefined {
  return heldElsewhere.value ? { reason: reason.value.trim(), confirmed: true } : undefined
}

async function submit(): Promise<void> {
  const chosen = day.value
  if (!canSubmit.value || typeof chosen !== 'string') return
  const outcome = await store.setExpiry(
    props.dataset.id,
    zonedDayStart(chosen, timeZone.value),
    override(),
  )
  if (outcome.kind === 'ok' || outcome.kind === 'not-found') {
    notice.value = null
    emit('close')
    return
  }
  // 'stale': the selection changed while the answer was out; the page was reloaded, so there is
  // nothing to confirm against any more.
  if (outcome.kind === 'stale') {
    emit('close')
    return
  }
  notice.value = refusalNotice(outcome)
}

const root = ref<HTMLElement | null>(null)
const dayField = ref<HTMLInputElement | null>(null)

onMounted(async () => {
  await nextTick()
  dayField.value?.focus()
})

function focusable(): HTMLElement[] {
  const found = root.value?.querySelectorAll<HTMLElement>('button, input, textarea')
  return [...(found ?? [])].filter((element) => !element.hasAttribute('disabled'))
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.stopPropagation()
    if (!busy.value) emit('close')
    return
  }
  if (event.key !== 'Tab') return
  const items = focusable()
  const first = items[0]
  const last = items[items.length - 1]
  if (first === undefined || last === undefined) return
  const active = document.activeElement
  if (event.shiftKey && active === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
  }
}
</script>

<template>
  <div
    class="scrim"
    data-testid="expiry-scrim"
  >
    <div
      ref="root"
      class="dialog"
      role="dialog"
      aria-modal="true"
      :aria-label="`Set Expiry for ${name}`"
      data-testid="expiry-dialog"
      @keydown="onKeydown"
    >
      <h2 class="dialog__title">
        {{ titleCase('Set Expiry') }}: {{ name }}
      </h2>
      <p class="text">
        Set an expiry for {{ name }}. The dataset is deleted on that date. Choosing Never on the dataset's page
        keeps it.
      </p>

      <div
        v-if="heldElsewhere"
        class="warning"
        role="note"
        data-testid="override-warning"
      >
        <strong>Held by {{ owner }}.</strong>
        This dataset belongs to another owner, not the operator. Changing it is recorded in the operator
        override log, and needs a reason.
      </div>

      <div class="field">
        <label
          class="field__label"
          for="expiry-day"
        >{{ titleCase('Expiry Day') }}</label>
        <div class="field__row">
          <input
            id="expiry-day"
            ref="dayField"
            v-model="dayText"
            type="text"
            inputmode="numeric"
            placeholder="YYYY/MM/DD"
            :disabled="busy"
            :aria-invalid="dayProblem"
            data-testid="expiry-day"
            @keydown.enter="submit"
          >
          <button
            type="button"
            class="btn"
            :disabled="busy"
            data-testid="expiry-tomorrow"
            @click="dayText = tomorrowText(timeZone)"
          >
            {{ titleCase('Tomorrow') }}
          </button>
        </div>
        <p class="hint">
          The day starts at midnight in {{ timeZone }}.
        </p>
        <p
          v-if="dayProblem"
          class="hint hint--error"
          data-testid="expiry-problem"
        >
          Type the day as YYYY/MM/DD.
        </p>
      </div>

      <div
        v-if="heldElsewhere"
        class="field"
        data-testid="override-prompt"
      >
        <label
          class="field__label"
          for="expiry-reason"
        >{{ titleCase('Reason') }}</label>
        <textarea
          id="expiry-reason"
          v-model="reason"
          rows="2"
          :disabled="busy"
          placeholder="Why you are acting on another owner's dataset"
          data-testid="override-reason"
        />
      </div>

      <p
        v-if="hint"
        id="expiry-dialog-hint"
        class="hint"
        data-testid="expiry-hint"
      >
        {{ hint }}
      </p>

      <p
        v-if="notice"
        class="notice"
        role="alert"
        data-testid="expiry-notice"
      >
        {{ notice.text }}
        <span
          v-if="notice.errorId"
          class="notice__id"
        >Error ID: {{ notice.errorId }}</span>
      </p>

      <div class="actions">
        <button
          type="button"
          class="btn btn--primary"
          :disabled="!canSubmit"
          :aria-describedby="hint ? 'expiry-dialog-hint' : undefined"
          data-testid="expiry-submit"
          @click="submit"
        >
          {{ titleCase('Set Expiry') }}
        </button>
        <button
          type="button"
          class="btn"
          :disabled="busy"
          data-testid="expiry-cancel"
          @click="emit('close')"
        >
          {{ titleCase('Cancel') }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.scrim {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: color-mix(in srgb, var(--tj-ground), transparent 30%);
}

.dialog {
  width: min(520px, 100%);
  max-height: 100%;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  background: var(--tj-surface);
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-card);
}

.dialog__title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}

.text {
  margin: 0;
  font-size: 13px;
  color: var(--tj-text);
}

.hint {
  margin: 0;
  font-size: 12px;
  color: var(--tj-text3);
}

.hint--error {
  color: var(--tj-status-fail);
}

.warning {
  padding: 8px 12px;
  border: 1px solid var(--tj-status-warn);
  border-radius: var(--tj-radius-control);
  background: color-mix(in srgb, var(--tj-status-warn), transparent 88%);
  font-size: 13px;
  color: var(--tj-text);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field__label {
  font-size: 12px;
  color: var(--tj-text2);
}

.field__row,
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

input[type='text'],
textarea {
  min-width: 0;
  padding: 0 10px;
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
  background: var(--tj-ground);
  color: var(--tj-text);
  font: inherit;
  font-size: 13px;
}

input[type='text'] {
  height: var(--tj-control-height);
}

textarea {
  padding: 8px 10px;
  resize: vertical;
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

.btn--primary {
  border-color: transparent;
  background: var(--tj-accent-fill);
  color: var(--tj-on-accent);
}

.btn:disabled {
  border-color: var(--tj-line);
  background: transparent;
  color: var(--tj-text3);
  cursor: not-allowed;
}

.notice {
  margin: 0;
  padding: 8px 12px;
  border: 1px solid var(--tj-status-fail);
  border-radius: var(--tj-radius-control);
  background: var(--tj-raised);
  font-size: 13px;
  color: var(--tj-text);
}

.notice__id {
  margin-left: 8px;
  font-family: var(--tj-font-mono);
  font-size: 12px;
  color: var(--tj-text3);
}
</style>
