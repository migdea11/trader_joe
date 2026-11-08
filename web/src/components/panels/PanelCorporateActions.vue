<script setup lang="ts">
// A dataset's corporate-action events, as the vendor sent them (tj-7zjov1.37; GET datasets/{id}/corporate-actions).
// The minimal event view: the vendor's wording for the action, its five dates and when the observation was
// made (a vendor revision is another observation). Nothing here interprets an action. Fetched when the row
// is opened, paged by keyset cursor; a failure is worded in place and Try Again reads it once more.
import { onMounted, ref } from 'vue'
import type { CorporateActionEvent } from '@generated/trader_joe/proto/ui/v1/panels_pb'

import { getCorporateActions } from '@/api'
import { MISSING, timestampToInstant } from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import { describeError } from '@/stores/datasetCatalog'
import type { CatalogError } from '@/stores/datasetCatalog'
import { titleCase } from '@/theme/copy'

const props = defineProps<{ datasetId: string }>()

const { date, dateTime } = useFormatters()

const COLUMNS = ['Action', 'Ex Date', 'Record Date', 'Payable Date', 'Process Date', 'Effective Date', 'Observed'].map(
  titleCase,
)

const events = ref<readonly CorporateActionEvent[]>([])
const cursor = ref('')
const loaded = ref(false)
const loading = ref(false)
const error = ref<CatalogError | null>(null)

type Stamp = CorporateActionEvent['exDate']

function day(stamp: Stamp): string {
  return stamp === undefined ? MISSING : date(timestampToInstant(stamp))
}

async function load(more: boolean): Promise<void> {
  if (loading.value) return
  loading.value = true
  try {
    const page = await getCorporateActions(props.datasetId, more ? { cursor: cursor.value } : {})
    events.value = more ? [...events.value, ...page.items] : page.items
    cursor.value = page.nextCursor
    error.value = null
    loaded.value = true
  } catch (caught) {
    error.value = describeError(caught)
  } finally {
    loading.value = false
  }
}

onMounted(() => void load(false))
</script>

<template>
  <div
    class="actions"
    data-testid="corporate-actions"
  >
    <p
      v-if="error"
      class="pn-notice pn-notice--error"
      role="alert"
      data-testid="corporate-actions-error"
    >
      Could not load corporate actions. {{ error.message }}
      <span
        v-if="error.errorId"
        class="pn-error-id"
      >Error ID: {{ error.errorId }}</span>
      <button
        type="button"
        class="pn-btn"
        @click="load(false)"
      >
        {{ titleCase('Try Again') }}
      </button>
    </p>
    <p
      v-else-if="!loaded"
      class="pn-muted"
      role="status"
    >
      Loading corporate actions
    </p>
    <p
      v-else-if="events.length === 0"
      class="pn-muted"
      data-testid="corporate-actions-empty"
    >
      No corporate actions recorded for this dataset.
    </p>
    <div
      v-else
      class="scroll"
    >
      <table class="table">
        <thead>
          <tr>
            <th
              v-for="column in COLUMNS"
              :key="column"
              scope="col"
            >
              {{ column }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="event in events"
            :key="`${event.vendorActionId}:${event.asOf?.seconds}:${event.asOf?.nanos}`"
            data-testid="corporate-action-row"
          >
            <td>{{ event.actionType }}</td>
            <td class="mono">
              {{ day(event.exDate) }}
            </td>
            <td class="mono">
              {{ day(event.recordDate) }}
            </td>
            <td class="mono">
              {{ day(event.payableDate) }}
            </td>
            <td class="mono">
              {{ day(event.processDate) }}
            </td>
            <td class="mono">
              {{ day(event.effectiveDate) }}
            </td>
            <td class="mono">
              {{ event.asOf === undefined ? MISSING : dateTime(timestampToInstant(event.asOf)) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <button
      v-if="cursor !== ''"
      type="button"
      class="pn-btn"
      :disabled="loading"
      data-testid="corporate-actions-more"
      @click="load(true)"
    >
      {{ titleCase('Load More') }}
    </button>
  </div>
</template>

<style scoped>
.actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
}

.scroll {
  align-self: stretch;
  overflow-x: auto;
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.table th {
  padding: 6px 12px 6px 0;
  text-align: left;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--tj-text3);
  border-bottom: 1px solid var(--tj-line);
  white-space: nowrap;
}

.table td {
  padding: 6px 12px 6px 0;
  border-bottom: 1px solid var(--tj-line);
  white-space: nowrap;
}

.mono {
  font-family: var(--tj-font-mono);
  font-size: 12px;
}
</style>
