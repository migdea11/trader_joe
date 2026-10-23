<script setup lang="ts">
// The Dataset detail's Retention card (tj-grna9p.89): the expiry type (locked: fixed when the dataset
// is created), the Expiry Time (empty is Keep Forever) with Tomorrow and Clear, and the two writes,
// Retire Subscription or Set Expiry (PATCH expiry) and Delete Now (DELETE, behind a typed
// confirmation). A dataset another owner holds shows its owner and a warning, and both writes then
// need a non-blank reason and a confirmation before they send `override`; the operator's own dataset
// never sends one. The server decides what a write does and whether it is allowed: the card sends
// what the operator chose and renders what comes back.
//
// Every refusal leaves the card as it was and says why, in place: 409 re-opens the reason prompt (the
// answer never names the owner, the card already shows it), 503 says to run make migrate, 422 is a
// bug and is shown as one, and 404 hands the screen the news that the dataset is gone.
import { computed, nextTick, ref, watch } from 'vue'
import { ExpiryType, DatasetState } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'
import { UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'

import { OPERATOR_CLIENT_ID, int64ToNumber } from '@/api'
import type { OperatorOverride } from '@/api'
import { granularityLabel } from '@/catalog/labels'
import { MISSING, formatNumber, timestampToInstant } from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import { useDatasetDetailStore } from '@/stores/datasetDetail'
import type { WriteOutcome } from '@/stores/datasetDetail'
import { titleCase } from '@/theme/copy'
import { parseDay, zonedDayStart } from '@/viewer/range'

import { refusalNotice, tomorrowText } from './expiryWrite'

const props = defineProps<{ dataset: DatasetSummary }>()
const emit = defineEmits<{
  /** The delete went through (204): the screen leaves for Datasets. */
  deleted: []
  /** A write found the dataset gone (404). */
  gone: []
}>()

const store = useDatasetDetailStore()
const { dateTime, timeZone } = useFormatters()

const EXPIRY_TYPE_LABELS: Partial<Record<ExpiryType, string>> = {
  [ExpiryType.BULK]: 'Bulk',
  [ExpiryType.BUFFER_1K]: 'Buffer 1K',
  [ExpiryType.BUFFER_10K]: 'Buffer 10K',
  [ExpiryType.BUFFER_100K]: 'Buffer 100K',
  [ExpiryType.ROLLING]: 'Rolling',
}

const expiryType = computed(() => EXPIRY_TYPE_LABELS[props.dataset.expiryType] ?? MISSING)
const currentExpiry = computed(() =>
  props.dataset.expiry === undefined ? 'None' : dateTime(timestampToInstant(props.dataset.expiry)),
)
const retired = computed(() => props.dataset.state === DatasetState.RETIRED)
// A DAILY or STREAM dataset is a subscription: an expiry retires it. A bulk (STATIC) one just has a date.
const subscription = computed(() => props.dataset.updateType !== UpdateType.STATIC)
const heldElsewhere = computed(() => props.dataset.owner !== OPERATOR_CLIENT_ID)
const owner = computed(() => (props.dataset.owner === '' ? MISSING : props.dataset.owner))

// --- the Expiry Time field -----------------------------------------------------------------------

const expiryText = ref('')
const day = computed(() => (expiryText.value.trim() === '' ? undefined : (parseDay(expiryText.value) ?? null)))
const keepForever = computed(() => day.value === undefined)
const dayProblem = computed(() => day.value === null)

function setTomorrow(): void {
  expiryText.value = tomorrowText(timeZone.value)
}

function clearField(): void {
  expiryText.value = ''
}

// --- the override prompt -------------------------------------------------------------------------

const reason = ref('')
const confirmed = ref(false)
const overrideReady = computed(() => !heldElsewhere.value || (reason.value.trim() !== '' && confirmed.value))

function override(): OperatorOverride | undefined {
  return heldElsewhere.value ? { reason: reason.value.trim(), confirmed: true } : undefined
}

// --- the writes ----------------------------------------------------------------------------------

type Notice = { tone: 'error' | 'info'; text: string; errorId?: string }
const notice = ref<Notice | null>(null)

const deleting = ref(false)
const typed = ref('')
// What the operator types to confirm: the symbol and the granularity label (VFV 1d), so the answer names
// the one dataset and not every granularity of the symbol. Exact after trimming; the symbol is case-sensitive.
const confirmName = computed(() => `${props.dataset.assetSymbol} ${granularityLabel(props.dataset.granularity)}`)
const typedMatches = computed(() => typed.value.trim() === confirmName.value)

const busy = computed(() => store.writing)
const hasExpiry = computed(() => props.dataset.expiry !== undefined)

// What the primary button does with the field as it stands.
const applyLabel = computed(() => {
  if (keepForever.value && hasExpiry.value) return titleCase(subscription.value ? 'Resume Subscription' : 'Clear Expiry')
  return titleCase(subscription.value ? 'Retire Subscription' : 'Set Expiry')
})
const canApply = computed(
  () => !busy.value && !dayProblem.value && (keepForever.value ? hasExpiry.value : true) && overrideReady.value,
)

function settle(outcome: WriteOutcome): boolean {
  switch (outcome.kind) {
    case 'ok':
      notice.value = null
      return true
    case 'stale':
      // The screen moved to another dataset: this answer is not for what is shown.
      return false
    case 'not-found':
      emit('gone')
      return false
    default: {
      // A refusal: the fixed words (components/retention/expiryWrite.ts), shown in place.
      const refusal = refusalNotice(outcome)
      notice.value = refusal === null ? null : { tone: 'error', ...refusal }
      // The prompt is on screen for another owner's dataset; a dataset that has changed hands since the
      // last read is refetched so the prompt appears with the owner it now has.
      if (outcome.kind === 'override-required') void store.refresh()
      return false
    }
  }
}

async function apply(): Promise<void> {
  if (!canApply.value) return
  const chosen = day.value
  const expiry = typeof chosen === 'string' ? zonedDayStart(chosen, timeZone.value) : undefined
  if (settle(await store.setExpiry(expiry, override()))) {
    expiryText.value = ''
    reason.value = ''
    confirmed.value = false
  }
}

const typedField = ref<HTMLInputElement | null>(null)
const deleteTrigger = ref<HTMLButtonElement | null>(null)

// Focus follows the confirmation: into the typed field on open, back to the trigger on Cancel.
async function openDelete(): Promise<void> {
  deleting.value = true
  typed.value = ''
  notice.value = null
  await nextTick()
  typedField.value?.focus()
}

async function cancelDelete(): Promise<void> {
  deleting.value = false
  typed.value = ''
  await nextTick()
  deleteTrigger.value?.focus()
}

// What still blocks Delete Now, said next to the button (aria-describedby).
const deleteHint = computed(() => {
  if (!typedMatches.value) return `Type the full name shown, ${confirmName.value}, to enable Delete Now.`
  if (heldElsewhere.value && reason.value.trim() === '') return 'Give a reason above to enable Delete Now.'
  if (heldElsewhere.value && !confirmed.value) return 'Tick the confirmation above to enable Delete Now.'
  return ''
})

async function deleteNow(): Promise<void> {
  if (busy.value || !typedMatches.value || !overrideReady.value) return
  if (settle(await store.deleteNow(override()))) emit('deleted')
}

// A different dataset (or a new owner) starts the card over.
watch(
  () => props.dataset.id,
  () => {
    expiryText.value = ''
    reason.value = ''
    confirmed.value = false
    notice.value = null
    deleting.value = false
    typed.value = ''
  },
)
</script>

<template>
  <div
    class="retention"
    data-testid="detail-retention"
  >
    <dl class="meta">
      <div>
        <dt>{{ titleCase('Expiry Type') }}</dt>
        <dd data-fact="expiry-type">
          {{ expiryType }}
        </dd>
      </div>
      <div>
        <dt>{{ titleCase('Expiry') }}</dt>
        <dd data-fact="expiry">
          {{ currentExpiry }}
        </dd>
      </div>
      <div v-if="retired">
        <dt>{{ titleCase('State') }}</dt>
        <dd data-fact="state">
          Retired: no longer collecting
        </dd>
      </div>
    </dl>
    <p class="hint">
      The expiry type is fixed when the dataset is created.
    </p>

    <div
      v-if="heldElsewhere"
      class="warning"
      role="note"
      data-testid="override-warning"
    >
      <strong>Held by {{ owner }}.</strong>
      This dataset belongs to another owner, not the operator. Changing or deleting it is recorded in the
      operator override log, and needs a reason and your confirmation.
    </div>

    <div class="field">
      <label
        class="field__label"
        for="retention-expiry"
      >{{ titleCase('Expiry Time') }}</label>
      <div class="field__row">
        <input
          id="retention-expiry"
          v-model="expiryText"
          type="text"
          inputmode="numeric"
          placeholder="Keep Forever"
          :disabled="busy"
          :aria-invalid="dayProblem"
          data-testid="expiry-input"
          @keydown.enter="apply"
        >
        <button
          type="button"
          class="btn"
          :disabled="busy"
          data-testid="expiry-tomorrow"
          @click="setTomorrow"
        >
          {{ titleCase('Tomorrow') }}
        </button>
        <button
          type="button"
          class="btn"
          :disabled="busy"
          data-testid="expiry-clear"
          @click="clearField"
        >
          {{ titleCase('Clear') }}
        </button>
      </div>
      <p
        v-if="dayProblem"
        class="hint hint--error"
        data-testid="expiry-problem"
      >
        Type the day as YYYY/MM/DD, or leave it empty to keep the dataset forever.
      </p>
      <p class="hint">
        The day starts at midnight in {{ timeZone }}.
        <template v-if="subscription">
          Setting an expiry retires this subscription at once: it stops updating, stays readable, and is
          deleted on that date. Clearing the expiry before then resumes it.
        </template>
        <template v-else>
          The dataset is deleted on that date. Clearing the expiry keeps it.
        </template>
      </p>
    </div>

    <div
      v-if="heldElsewhere"
      class="override"
      data-testid="override-prompt"
    >
      <label
        class="field__label"
        for="retention-reason"
      >{{ titleCase('Reason') }}</label>
      <textarea
        id="retention-reason"
        v-model="reason"
        rows="2"
        :disabled="busy"
        placeholder="Why you are acting on another owner's dataset"
        data-testid="override-reason"
      />
      <label class="check">
        <input
          v-model="confirmed"
          type="checkbox"
          :disabled="busy"
          data-testid="override-confirm"
        >
        I confirm I am acting on a dataset held by {{ owner }}.
      </label>
    </div>

    <div class="actions">
      <button
        type="button"
        class="btn btn--primary"
        :disabled="!canApply"
        data-testid="expiry-apply"
        @click="apply"
      >
        {{ applyLabel }}
      </button>
      <button
        v-if="!deleting"
        ref="deleteTrigger"
        type="button"
        class="btn btn--danger"
        :disabled="busy"
        data-testid="delete-open"
        @click="openDelete"
      >
        {{ titleCase('Delete Now') }}
      </button>
    </div>

    <div
      v-if="deleting"
      class="confirm"
      role="dialog"
      :aria-label="`Delete ${confirmName}`"
      data-testid="delete-confirm"
    >
      <p class="confirm__text">
        This deletes {{ confirmName }} and its {{ formatNumber(int64ToNumber(dataset.barCount)) }} bars at once.
        It cannot be undone. Type <code>{{ confirmName }}</code> to confirm.
      </p>
      <input
        ref="typedField"
        v-model="typed"
        type="text"
        :disabled="busy"
        :aria-label="`Type ${confirmName} to confirm`"
        data-testid="delete-typed"
        @keydown.enter="deleteNow"
      >
      <p
        v-if="deleteHint"
        id="retention-delete-hint"
        class="hint"
        data-testid="delete-hint"
      >
        {{ deleteHint }}
      </p>
      <div class="actions">
        <button
          type="button"
          class="btn btn--danger"
          :disabled="busy || !typedMatches || !overrideReady"
          :aria-describedby="deleteHint ? 'retention-delete-hint' : undefined"
          data-testid="delete-submit"
          @click="deleteNow"
        >
          {{ titleCase('Delete Now') }}
        </button>
        <button
          type="button"
          class="btn"
          :disabled="busy"
          data-testid="delete-cancel"
          @click="cancelDelete"
        >
          {{ titleCase('Cancel') }}
        </button>
      </div>
    </div>

    <p
      v-if="notice"
      class="notice"
      :class="{ 'notice--error': notice.tone === 'error' }"
      role="alert"
      data-testid="retention-notice"
    >
      {{ notice.text }}
      <span
        v-if="notice.errorId"
        class="notice__id"
      >Error ID: {{ notice.errorId }}</span>
    </p>
  </div>
</template>

<style scoped>
.retention {
  display: flex;
  flex-direction: column;
  gap: 12px;
  align-items: flex-start;
  max-width: 640px;
}

.meta {
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 28px;
}

.meta dt {
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--tj-text3);
}

.meta dd {
  margin: 2px 0 0;
  font-size: 13px;
  font-family: var(--tj-font-mono);
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

.field,
.override,
.confirm {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-self: stretch;
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
  background: var(--tj-surface);
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

.check {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--tj-text);
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

.btn--danger {
  border-color: var(--tj-status-fail);
  color: var(--tj-status-fail);
}

.btn:disabled {
  border-color: var(--tj-line);
  background: transparent;
  color: var(--tj-text3);
  cursor: not-allowed;
}

.confirm {
  padding: 10px 12px;
  border: 1px solid var(--tj-status-fail);
  border-radius: var(--tj-radius-control);
}

.confirm__text {
  margin: 0;
  font-size: 13px;
}

.notice {
  margin: 0;
  padding: 8px 12px;
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
  background: var(--tj-raised);
  font-size: 13px;
  color: var(--tj-text);
}

.notice--error {
  border-color: var(--tj-status-fail);
}

.notice__id {
  margin-left: 8px;
  font-family: var(--tj-font-mono);
  font-size: 12px;
  color: var(--tj-text3);
}
</style>
