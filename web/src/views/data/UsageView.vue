<script setup lang="ts">
// The Usage tab (tj-grna9p.62, canvas board "Data · Usage"): consumption of the datasets, distinct from
// collection health. Tiles (Stale, Read Last 30 Days, Reads Today, Storage), three charts (when each
// dataset was last read, reads per day by client, estimated storage by source) and the Stale
// Datasets table with a Set Expiry that always asks first. The sidebar (UsageSidebar.vue) is the
// route's `sidebar` view; the selection lives in stores/usage.ts, which both read.
//
// The server decides what is stale and counts every figure, so no status or count is computed here.
// The threshold is fixed (STALE_DAYS); the word "Stale" appears only for what the server returned.
// No polling: the screen loads on mount, on a selection change, on Refresh, on a dataset event and
// once per reconnect (useUsageLive.ts).
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { RefreshCw } from '@lucide/vue'
import type { StaleDatasetRow } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { STALE_DAYS, int64ToNumber } from '@/api'
import { effectiveClientKinds } from '@/catalog/filters'
import { granularityLabel } from '@/catalog/labels'
import { staleLabel, staleNote, usageCell, usageCellText } from '@/catalog/usage'
import type { UsageCellState } from '@/catalog/usage'
import LastReadChart from '@/components/charts/LastReadChart.vue'
import ReadsPerDayChart from '@/components/charts/ReadsPerDayChart.vue'
import StorageBySourceChart from '@/components/charts/StorageBySourceChart.vue'
import SetExpiryDialog from '@/components/usage/SetExpiryDialog.vue'
import { daysSinceUtc } from '@/format/days'
import { MISSING, formatBytes, formatNumber, timestampToInstant } from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import { useFooterSummary } from '@/shell/useFooterSummary'
import { READS_WINDOW_DAYS } from '@/stores/datasetDetail'
import { useUsageStore } from '@/stores/usage'
import { colors } from '@/theme/tokens'
import { titleCase } from '@/theme/copy'

import { useUsageLive } from './useUsageLive'

const store = useUsageStore()
const {
  summary,
  buckets,
  reads,
  storage,
  errors,
  rows,
  staleStatus,
  hasMore,
  loadingMore,
  moreError,
  writingId,
  droppedNotice,
  clientKinds,
  clientsPicked,
  loadedTick,
} = storeToRefs(store)
const { date } = useFormatters()
const { setFooterUpdated, setFooterProblem } = useFooterSummary()

onMounted(() => void store.load())
useUsageLive()

// --- tiles ---------------------------------------------------------------------------------------

function count(value: bigint | undefined): string {
  return value === undefined ? MISSING : formatNumber(int64ToNumber(value))
}

const tiles = computed(() => {
  const message = summary.value
  const days = message !== null && message.staleDays > 0 ? message.staleDays : undefined
  const note = clientsPicked.value
    ? `unread by the selected clients ${days ?? STALE_DAYS}+ days`
    : staleNote({ clientKinds: effectiveClientKinds({ clientKinds: [...clientKinds.value] }) }, days)
  const clients = message === null ? undefined : int64ToNumber(message.distinctClientsToday)
  return [
    { key: 'stale', label: staleLabel(days), value: count(message?.stale), note, color: colors.text2 },
    {
      key: 'read-30d',
      label: titleCase('Read Last 30 Days'),
      value: count(message?.readLast30d),
      note: message === null ? '' : `of ${count(message.total)} datasets`,
      color: colors.text,
    },
    {
      key: 'reads-today',
      label: titleCase('Reads Today'),
      value: count(message?.readsToday),
      note: clients === undefined ? '' : `by ${formatNumber(clients)} ${clients === 1 ? 'client' : 'clients'}`,
      color: colors.text,
    },
    // Only when the server reports it.
    ...(storage.value === null
      ? []
      : [
          {
            key: 'storage',
            label: titleCase('Storage'),
            value: formatBytes(int64ToNumber(storage.value.totalBytes)),
            note: 'current, estimated',
            color: colors.text,
          },
        ]),
  ]
})

// --- the table -----------------------------------------------------------------------------------

function datasetName(row: StaleDatasetRow): string {
  const dataset = row.dataset
  return dataset === undefined ? MISSING : `${dataset.assetSymbol} ${granularityLabel(dataset.granularity)}`
}

function lastRead(row: StaleDatasetRow): string {
  return row.dataset === undefined ? MISSING : usageCellText(usageCell(row.dataset))
}

/** One entry per client that has read it: the client and the age of its last read. */
function readBy(row: StaleDatasetRow): { key: string; text: string }[] {
  return row.lastReadByClient.map((entry) => {
    const cell: UsageCellState =
      entry.lastReadAt === undefined
        ? { kind: 'never' }
        : { kind: 'age', days: daysSinceUtc(timestampToInstant(entry.lastReadAt)) }
    return { key: entry.clientId, text: `${entry.clientId} · ${usageCellText(cell)}` }
  })
}

function size(row: StaleDatasetRow): string {
  return formatBytes(int64ToNumber(row.estBytes))
}

function expires(row: StaleDatasetRow): string {
  // A row with no dataset has no expiry to speak of; a dataset with none is kept forever.
  if (row.dataset === undefined) return MISSING
  const expiry = row.dataset.expiry
  return expiry === undefined ? 'Never' : date(timestampToInstant(expiry))
}

const COLUMNS = ['Dataset', 'Last Read', 'Read By', 'Size (Est.)', 'Owner', 'Expires'] as const

// --- Set Expiry ----------------------------------------------------------------------------------

const dialogId = ref<string | null>(null)
let opener: HTMLElement | null = null

const dialogDataset = computed(() =>
  dialogId.value === null ? undefined : rows.value.find((row) => row.dataset?.id === dialogId.value)?.dataset,
)

function openDialog(row: StaleDatasetRow, event: Event): void {
  if (row.dataset === undefined) return
  opener = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  dialogId.value = row.dataset.id
}

async function closeDialog(): Promise<void> {
  dialogId.value = null
  const target = opener
  opener = null
  await nextTick()
  // The row may be gone (dropped, or no longer stale): then there is nothing to return to.
  if (target?.isConnected === true) target.focus()
}

// The row left the list while the dialog was open (a refetch, a selection change): close it.
watch(dialogDataset, (dataset) => {
  if (dialogId.value !== null && dataset === undefined) void closeDialog()
})

// --- the footer ----------------------------------------------------------------------------------

watch(loadedTick, () => setFooterUpdated(Date.now()), { immediate: true })

watch(
  errors,
  (current) => {
    const failed = Object.values(current)[0]
    if (failed === undefined) return setFooterProblem(null)
    const id = failed.errorId === undefined ? '' : ` (error id ${failed.errorId})`
    setFooterProblem(`Could not load usage: ${failed.message}${id}`)
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  setFooterUpdated(null)
  setFooterProblem(null)
})
</script>

<template>
  <section class="usage">
    <div class="usage__head">
      <h1 class="usage__title">
        Usage
      </h1>
      <button
        type="button"
        class="btn usage__refresh"
        data-testid="usage-refresh"
        @click="store.load()"
      >
        <RefreshCw
          :size="16"
          aria-hidden="true"
        />
        Refresh
      </button>
    </div>

    <div class="tiles">
      <div
        v-for="tile in tiles"
        :key="tile.key"
        class="tile"
        :data-tile="tile.key"
      >
        <span class="tile__label">{{ tile.label }}</span>
        <span
          class="tile__value"
          :style="{ color: tile.color }"
        >{{ tile.value }}</span>
        <span class="tile__note">{{ errors.summary && tile.key !== 'storage' ? 'Could not load counts' : tile.note }}</span>
      </div>
    </div>

    <div class="charts">
      <section
        class="block"
        aria-labelledby="usage-last-read-title"
      >
        <h2
          id="usage-last-read-title"
          class="block__title"
        >
          {{ titleCase('When Each Dataset Was Last Read') }}
        </h2>
        <p
          v-if="errors.buckets"
          class="inline-error"
          role="alert"
          data-testid="buckets-error"
        >
          Could not load last-read counts. {{ errors.buckets.message }}
          <span
            v-if="errors.buckets.errorId"
            class="error-id"
          >Error ID: {{ errors.buckets.errorId }}</span>
        </p>
        <LastReadChart
          v-if="buckets"
          :buckets="buckets"
        />
        <p
          v-else-if="!errors.buckets"
          class="muted"
          role="status"
        >
          Loading last-read counts
        </p>
      </section>

      <section
        class="block"
        aria-labelledby="usage-reads-title"
      >
        <h2
          id="usage-reads-title"
          class="block__title"
        >
          {{ titleCase('Reads per Day by Client') }} ({{ READS_WINDOW_DAYS }} d)
        </h2>
        <p
          v-if="errors.reads"
          class="inline-error"
          role="alert"
          data-testid="reads-error"
        >
          Could not load reads. {{ errors.reads.message }}
          <span
            v-if="errors.reads.errorId"
            class="error-id"
          >Error ID: {{ errors.reads.errorId }}</span>
        </p>
        <ReadsPerDayChart
          v-if="reads"
          :reads="reads"
          :days="READS_WINDOW_DAYS"
        />
        <p
          v-else-if="!errors.reads"
          class="muted"
          role="status"
        >
          Loading reads
        </p>
      </section>

      <section
        v-if="storage || errors.storage"
        class="block"
        aria-labelledby="usage-storage-title"
      >
        <h2
          id="usage-storage-title"
          class="block__title"
        >
          {{ titleCase('Storage by Source, Current') }}
        </h2>
        <p
          v-if="errors.storage"
          class="inline-error"
          role="alert"
          data-testid="storage-error"
        >
          Could not load storage. {{ errors.storage.message }}
          <span
            v-if="errors.storage.errorId"
            class="error-id"
          >Error ID: {{ errors.storage.errorId }}</span>
        </p>
        <template v-if="storage">
          <StorageBySourceChart :storage="storage" />
          <p class="hint">
            Estimated sizes, in decimal units, as of now.
          </p>
        </template>
      </section>
    </div>

    <section
      class="block"
      aria-labelledby="usage-stale-title"
    >
      <h2
        id="usage-stale-title"
        class="block__title"
      >
        {{ titleCase('Stale Datasets') }}
      </h2>

      <p
        v-if="droppedNotice"
        class="notice"
        role="status"
        data-testid="dropped-notice"
      >
        {{ droppedNotice }}
        <button
          type="button"
          class="link"
          @click="store.dismissDroppedNotice()"
        >
          Dismiss
        </button>
      </p>

      <div
        v-if="staleStatus === 'error' || errors.stale"
        class="inline-error"
        role="alert"
        data-testid="stale-error"
      >
        <span>Could not load stale datasets. {{ errors.stale?.message }}</span>
        <span
          v-if="errors.stale?.errorId"
          class="error-id"
        >Error ID: {{ errors.stale.errorId }}</span>
        <button
          type="button"
          class="btn"
          @click="store.load()"
        >
          Try Again
        </button>
      </div>

      <p
        v-if="staleStatus === 'loading' && rows.length === 0"
        class="muted"
        role="status"
        data-testid="stale-loading"
      >
        Loading stale datasets
      </p>
      <p
        v-else-if="staleStatus === 'ready' && rows.length === 0"
        class="muted"
        data-testid="stale-empty"
      >
        No datasets are in this list under the current choice.
      </p>

      <div
        v-if="rows.length > 0"
        class="table-wrap"
      >
        <table
          class="table"
          data-testid="stale-table"
        >
          <thead>
            <tr>
              <th
                v-for="column in COLUMNS"
                :key="column"
                scope="col"
                :class="{ num: column === 'Size (Est.)' }"
              >
                {{ column === 'Size (Est.)' ? column : titleCase(column) }}
              </th>
              <th scope="col">
                <span class="sr-only">{{ titleCase('Actions') }}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in rows"
              :key="row.dataset?.id"
              :data-dataset-id="row.dataset?.id"
            >
              <td>
                <RouterLink
                  v-if="row.dataset"
                  :to="{ name: 'data-dataset-detail', params: { id: row.dataset.id } }"
                  class="link"
                >
                  {{ datasetName(row) }}
                </RouterLink>
              </td>
              <td
                class="mono"
                data-cell="last-read"
              >
                {{ lastRead(row) }}
              </td>
              <td data-cell="read-by">
                <template v-if="readBy(row).length > 0">
                  <span
                    v-for="entry in readBy(row)"
                    :key="entry.key"
                    class="chip"
                  >{{ entry.text }}</span>
                </template>
                <template v-else>
                  {{ MISSING }}
                </template>
              </td>
              <td
                class="num mono"
                data-cell="size"
              >
                {{ size(row) }}
              </td>
              <td data-cell="owner">
                {{ row.dataset?.owner === '' ? MISSING : row.dataset?.owner }}
              </td>
              <td
                class="mono"
                data-cell="expires"
              >
                {{ expires(row) }}
                <span
                  v-if="row.dataset?.reaperPending"
                  class="flag"
                  data-testid="would-be-deleted"
                >Would be deleted</span>
              </td>
              <td>
                <button
                  type="button"
                  class="btn"
                  :disabled="writingId !== null"
                  :aria-label="`Set Expiry for ${datasetName(row)}`"
                  data-testid="set-expiry-open"
                  @click="openDialog(row, $event)"
                >
                  Set Expiry…
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div
        v-if="hasMore"
        class="more"
      >
        <button
          type="button"
          class="btn"
          :disabled="loadingMore"
          data-testid="stale-load-more"
          @click="store.loadMoreStale()"
        >
          {{ loadingMore ? 'Loading' : titleCase('Load More') }}
        </button>
        <span
          v-if="moreError"
          class="inline-error"
          role="alert"
        >Could not load more. {{ moreError.message }}</span>
      </div>
    </section>

    <SetExpiryDialog
      v-if="dialogDataset"
      :key="dialogDataset.id"
      :dataset="dialogDataset"
      @close="closeDialog"
    />
  </section>
</template>

<style scoped>
.usage {
  min-height: 100%;
  display: flex;
  flex-direction: column;
  gap: var(--tj-page-gap);
}

.usage__head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.usage__title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}

.usage__refresh {
  margin-left: auto;
}

.tiles {
  display: flex;
  gap: var(--tj-tile-gap);
}

.tile {
  flex: 1;
  min-width: 0;
  padding: var(--tj-card-padding);
  background: var(--tj-surface);
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-card);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.tile__label {
  font-size: 12px;
  color: var(--tj-text2);
}

.tile__value {
  font-family: var(--tj-font-mono);
  font-size: 22px;
}

.tile__note {
  font-size: 12px;
  color: var(--tj-text3);
}

.charts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: var(--tj-page-gap);
}

.block {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}

.block__title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}

.muted {
  margin: 0;
  font-size: 13px;
  color: var(--tj-text2);
}

.hint {
  margin: 0;
  font-size: 12px;
  color: var(--tj-text3);
}

.inline-error {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin: 0;
  padding: 8px 12px;
  border: 1px solid var(--tj-status-fail);
  border-radius: var(--tj-radius-control);
  background: var(--tj-raised);
  font-size: 13px;
  color: var(--tj-text);
}

.error-id {
  font-family: var(--tj-font-mono);
  font-size: 12px;
  color: var(--tj-text3);
}

.notice {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 0;
  padding: 8px 12px;
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
  background: var(--tj-raised);
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

.btn:disabled {
  color: var(--tj-text3);
  cursor: not-allowed;
}

.link {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--tj-accent-tint);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}

.table-wrap {
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

.num {
  text-align: right;
}

.chip {
  display: inline-block;
  margin-right: 8px;
  font-size: 12px;
  color: var(--tj-text2);
}

.flag {
  margin-left: 8px;
  font-family: var(--tj-font-sans);
  font-size: 12px;
  color: var(--tj-status-warn);
}

.more {
  display: flex;
  align-items: center;
  gap: 12px;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
</style>
