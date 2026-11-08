<script setup lang="ts">
// The selected panel's members (tj-7zjov1.37): symbol, the latest transition (with the cause of a Deferred or
// Refused one), the dataset's health with the shared health badges (Unknown when the member has no dataset
// yet), its last bar, and the days the asset appeared in and vanished from the asset list. A member with a
// dataset links to the Dataset detail and opens its corporate-action events in place. Read-only: the server
// decides membership; paged by keyset cursor.
import { ref } from 'vue'
import type { PanelMember } from '@generated/trader_joe/proto/ui/v1/panels_pb'

import StatusBadge from '@/components/StatusBadge.vue'
import { MISSING, timestampToInstant } from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import { titleCase } from '@/theme/copy'

import PanelCorporateActions from './PanelCorporateActions.vue'
import { memberHealthBadge, transitionLabel } from './panelModel'

defineProps<{
  members: readonly PanelMember[]
  hasMore: boolean
  loadingMore: boolean
}>()
const emit = defineEmits<{ more: [] }>()

const { dateTime } = useFormatters()

const COLUMNS = ['Symbol', 'Status', 'Health', 'Last Bar', 'Appeared', 'Vanished', 'Corporate Actions'].map(titleCase)

// The dataset ids whose events are open.
const open = ref<ReadonlySet<string>>(new Set())

function toggle(datasetId: string): void {
  const next = new Set(open.value)
  if (!next.delete(datasetId)) next.add(datasetId)
  open.value = next
}

function lastBar(member: PanelMember): string {
  return member.lastBar === undefined ? MISSING : dateTime(timestampToInstant(member.lastBar))
}
</script>

<template>
  <div
    class="members"
    data-testid="panel-members-table"
  >
    <div class="scroll">
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
          <template
            v-for="member in members"
            :key="member.vendorAssetId"
          >
            <tr data-testid="member-row">
              <td>
                <RouterLink
                  v-if="member.datasetId"
                  :to="{ name: 'data-dataset-detail', params: { id: member.datasetId } }"
                >
                  {{ member.symbol }}
                </RouterLink>
                <template v-else>
                  {{ member.symbol }}
                </template>
              </td>
              <td data-cell="status">
                {{ transitionLabel(member.status) }}
                <span
                  v-if="member.cause"
                  class="cause"
                >{{ member.cause }}</span>
              </td>
              <td data-cell="health">
                <StatusBadge :badge="memberHealthBadge(member.health)" />
              </td>
              <td class="mono">
                {{ lastBar(member) }}
              </td>
              <td class="mono">
                {{ member.appearedOn || MISSING }}
              </td>
              <td class="mono">
                {{ member.vanishedOn || MISSING }}
              </td>
              <td>
                <button
                  v-if="member.datasetId"
                  type="button"
                  class="pn-btn"
                  :aria-expanded="open.has(member.datasetId)"
                  :data-testid="`member-actions-${member.symbol}`"
                  @click="toggle(member.datasetId)"
                >
                  {{ titleCase(open.has(member.datasetId) ? 'Hide Events' : 'Show Events') }}
                </button>
                <template v-else>
                  {{ MISSING }}
                </template>
              </td>
            </tr>
            <tr
              v-if="member.datasetId && open.has(member.datasetId)"
              data-testid="member-actions-row"
            >
              <td :colspan="COLUMNS.length">
                <PanelCorporateActions :dataset-id="member.datasetId" />
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
    <button
      v-if="hasMore"
      type="button"
      class="pn-btn"
      :disabled="loadingMore"
      data-testid="members-more"
      @click="emit('more')"
    >
      {{ titleCase('Load More') }}
    </button>
  </div>
</template>

<style scoped>
.members {
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
  padding: 8px 12px 8px 0;
  vertical-align: top;
  border-bottom: 1px solid var(--tj-line);
  white-space: nowrap;
}

.mono {
  font-family: var(--tj-font-mono);
  font-size: 12px;
}

.cause {
  display: block;
  font-size: 12px;
  color: var(--tj-text3);
  white-space: normal;
}
</style>
