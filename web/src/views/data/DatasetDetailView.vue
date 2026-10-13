<script setup lang="ts">
// The Dataset detail (tj-grna9p.34, phase 2; canvas board "Shell · Dataset detail"): one dataset's
// Overview (owner, range, bars, last bar), Coverage (the strip and its gap list), History (its
// collection runs, newest first, Load More by cursor) and a read-only Retention stub. A breadcrumb
// trail (Datasets > dataset > Details) leads back up; the dataset crumb is the Viewer, and the
// screen has no Open in Viewer button of its own. The sidebar (DatasetDetailSidebar.vue) is the route's `sidebar` view.
//
// Not here yet, by ruling: Request the Gap and All Runs (their destinations are a later phase), a
// Quota Key column (the server does not record it per run), Usage, and any Retention control.
//
// Live: an IngestRunChanged for this dataset updates its run row in place and one burst of events
// refetches the overview and the coverage once (useDatasetDetailLive.ts). Nothing polls.
//
// Gap dates read in UTC. The client is not told the calendar's zone, so a gap boundary (the
// calendar's local midnight) is read as a UTC calendar day (catalog/coverage.ts), whatever display
// zone the settings hold. The Range of the overview and the run times follow the display zone.
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ExpiryType, DatasetState } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'
import { UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'

import { int64ToNumber } from '@/api'
import { coverageTotals } from '@/catalog/coverage'
import { dataTypeLabel, datasetIdentityLabel, granularityLabel, isIntraday, sourceFeedLabel, updateTypeLabel } from '@/catalog/labels'
import { VIEWER_ROUTE, rowBadge } from '@/catalog/rows'
import StatusBadge from '@/components/StatusBadge.vue'
import CoverageStrip from '@/components/charts/CoverageStrip.vue'
import PageBreadcrumbs from '@/components/nav/PageBreadcrumbs.vue'
import type { BreadcrumbItem } from '@/components/nav/PageBreadcrumbs.vue'
import IngestRunsTable from '@/components/runs/IngestRunsTable.vue'
import { MISSING, formatNumber, timestampToInstant } from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import { useDatasetCoverageStore } from '@/stores/datasetCoverage'
import type { CoverageEntry } from '@/stores/datasetCoverage'
import { useDatasetDetailStore } from '@/stores/datasetDetail'
import { titleCase } from '@/theme/copy'

import { DETAIL_COVERAGE_BUCKETS, useDatasetDetailLive } from './useDatasetDetailLive'

const route = useRoute()
const store = useDatasetDetailStore()
const coverage = useDatasetCoverageStore()
const { dataset, status, error, deleted, refreshProblem, runs, runsStatus, runsError, hasMoreRuns, loadingMore, moreError } =
  storeToRefs(store)
const { date, dateTime } = useFormatters()

const datasetId = computed(() => String(route.params.id))

watch(datasetId, (id) => void store.load(id), { immediate: true })
useDatasetDetailLive(datasetId)

// --- coverage: acquired while the dataset is on screen, so an event can drop and refetch it --------

let heldId: string | null = null
function hold(id: string | null): void {
  if (heldId === id) return
  if (heldId !== null) coverage.release(heldId, DETAIL_COVERAGE_BUCKETS)
  heldId = id
  if (id !== null) coverage.acquire(id, DETAIL_COVERAGE_BUCKETS)
}

watch(
  [datasetId, () => status.value === 'ready' && !deleted.value],
  ([id, on]) => hold(on ? id : null),
  { immediate: true },
)

// The strip on screen stays while an event refetches it, rather than blanking to Loading.
const shown = ref<{ id: string; entry: CoverageEntry } | null>(null)
const entry = computed<CoverageEntry | undefined>(() => {
  const fresh = coverage.entry(datasetId.value, DETAIL_COVERAGE_BUCKETS)
  if (fresh !== undefined) return fresh
  return shown.value?.id === datasetId.value ? shown.value.entry : undefined
})
watch(
  () => coverage.entry(datasetId.value, DETAIL_COVERAGE_BUCKETS),
  (fresh) => {
    if (fresh !== undefined) shown.value = { id: datasetId.value, entry: fresh }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  hold(null)
  store.reset()
})

// --- overview and retention ----------------------------------------------------------------------

const title = computed(() => {
  const d = dataset.value
  if (d === null) return titleCase('Dataset')
  return `${d.assetSymbol} · ${dataTypeLabel(d.dataType)} · ${granularityLabel(d.granularity)}`
})

// Datasets > <symbol> > Details: one dataset crumb (the unique key reads as one directory, not as
// nested levels), the Viewer, the screen's parent. Its label is the symbol only; the whole identity
// is the hover title. Until the dataset is known, or once it is deleted or not found, there is no
// Viewer to link to, so the trail is Datasets > Details. The first crumb carries no filter state,
// like Back to Datasets.
const crumbs = computed<BreadcrumbItem[]>(() => {
  const trail: BreadcrumbItem[] = [{ label: titleCase('Datasets'), to: { name: 'data-datasets' } }]
  if (status.value === 'ready' && dataset.value !== null && !deleted.value) {
    trail.push({
      label: dataset.value.assetSymbol,
      to: { name: VIEWER_ROUTE, params: { id: dataset.value.id } },
      hint: datasetIdentityLabel(dataset.value),
    })
  }
  trail.push({ label: titleCase('Details') })
  return trail
})

const barCount = computed(() => (dataset.value === null ? 0 : int64ToNumber(dataset.value.barCount)))

function stamp(ts: { seconds: bigint; nanos: number } | undefined, withTime: boolean): string {
  if (ts === undefined) return MISSING
  const instant = timestampToInstant(ts)
  return withTime ? dateTime(instant) : date(instant)
}

const lastBar = computed(() => {
  const d = dataset.value
  return d === null ? MISSING : stamp(d.lastBar, isIntraday(d.granularity))
})

// [start, end) for a bulk (STATIC) dataset. A subscription has no end: the server fills its end
// with the time of the read, so it reads "onward" rather than showing that instant as a range end.
function rangeText(d: DatasetSummary): string {
  const from = stamp(d.start, false)
  if (d.updateType !== UpdateType.STATIC) return `${from} onward`
  return `${from} to ${stamp(d.end, false)}`
}

const EXPIRY_TYPE_LABELS: Partial<Record<ExpiryType, string>> = {
  [ExpiryType.BULK]: 'Bulk',
  [ExpiryType.BUFFER_1K]: 'Buffer 1K',
  [ExpiryType.BUFFER_10K]: 'Buffer 10K',
  [ExpiryType.BUFFER_100K]: 'Buffer 100K',
  [ExpiryType.ROLLING]: 'Rolling',
}

const retention = computed(() => {
  const d = dataset.value
  if (d === null) return null
  return {
    type: EXPIRY_TYPE_LABELS[d.expiryType] ?? MISSING,
    // None, or the time the dataset is deleted.
    expiry: d.expiry === undefined ? 'None' : stamp(d.expiry, true),
    retired: d.state === DatasetState.RETIRED,
  }
})

// --- coverage section ------------------------------------------------------------------------

const totals = computed(() => (entry.value?.state === 'ready' ? coverageTotals(entry.value.strip) : null))
const noGaps = computed(
  () => entry.value?.state === 'ready' && totals.value !== null && totals.value.expected > 0 && entry.value.strip.gapCount === 0,
)

function retryCoverage(): void {
  coverage.invalidate(datasetId.value)
}
</script>

<template>
  <section class="detail">
    <PageBreadcrumbs :items="crumbs" />

    <div
      v-if="status === 'loading' || status === 'idle'"
      class="panel"
      role="status"
      data-testid="detail-loading"
    >
      Loading dataset
    </div>

    <div
      v-else-if="status === 'not-found'"
      class="panel"
      data-testid="detail-not-found"
    >
      <h1 class="detail__title">
        {{ titleCase('Dataset Not Found') }}
      </h1>
      <span>No dataset has the id {{ datasetId }}.</span>
      <RouterLink
        class="link"
        :to="{ name: 'data-datasets' }"
      >
        Back to Datasets
      </RouterLink>
    </div>

    <div
      v-else-if="status === 'error'"
      class="panel panel--error"
      role="alert"
      data-testid="detail-error"
    >
      <span>Could not load the dataset. {{ error?.message }}</span>
      <span
        v-if="error?.errorId"
        class="panel__id"
      >Error ID: {{ error.errorId }}</span>
      <button
        type="button"
        class="btn"
        @click="store.load(datasetId)"
      >
        {{ titleCase('Try Again') }}
      </button>
    </div>

    <div
      v-else-if="deleted"
      class="panel"
      role="status"
      data-testid="detail-deleted"
    >
      <h1 class="detail__title">
        {{ title }}
      </h1>
      <span>This dataset was deleted. Its bars and runs are no longer available.</span>
      <RouterLink
        class="link"
        :to="{ name: 'data-datasets' }"
      >
        Back to Datasets
      </RouterLink>
    </div>

    <template v-else-if="dataset">
      <header class="detail__head">
        <h1
          class="detail__title"
          data-testid="detail-title"
        >
          {{ title }}
        </h1>
        <StatusBadge
          :badge="rowBadge(dataset)"
          data-testid="detail-badge"
        />
      </header>

      <div
        v-if="refreshProblem"
        class="notice"
        role="status"
        data-testid="detail-refresh-problem"
      >
        Could not refresh. {{ refreshProblem.message }} What is shown is the last data that arrived.
      </div>

      <section
        id="detail-overview"
        class="block"
        aria-labelledby="detail-overview-title"
      >
        <h2
          id="detail-overview-title"
          class="block__title"
        >
          {{ titleCase('Overview') }}
        </h2>
        <dl
          class="meta"
          data-testid="detail-overview"
        >
          <div>
            <dt>{{ titleCase('Owner') }}</dt>
            <dd data-fact="owner">
              {{ dataset.owner === '' ? MISSING : dataset.owner }}
            </dd>
          </div>
          <div>
            <dt>{{ titleCase('Source') }}</dt>
            <dd>{{ sourceFeedLabel(dataset.source, dataset.feed) }}</dd>
          </div>
          <div>
            <dt>{{ titleCase('Update') }}</dt>
            <dd>{{ updateTypeLabel(dataset.updateType) }}</dd>
          </div>
          <div>
            <dt>{{ titleCase('Range') }}</dt>
            <dd
              data-fact="range"
              title="Half-open: the end is not included."
            >
              {{ rangeText(dataset) }}
            </dd>
          </div>
          <div>
            <dt>{{ titleCase('Bars') }}</dt>
            <dd data-fact="bars">
              {{ formatNumber(barCount) }}
            </dd>
          </div>
          <div>
            <dt>{{ titleCase('Last Bar') }}</dt>
            <dd data-fact="last-bar">
              {{ lastBar }}
            </dd>
          </div>
        </dl>
      </section>

      <section
        id="detail-coverage"
        class="block"
        aria-labelledby="detail-coverage-title"
      >
        <h2
          id="detail-coverage-title"
          class="block__title"
        >
          {{ titleCase('Coverage') }}
        </h2>
        <p
          v-if="entry === undefined"
          class="muted"
          role="status"
          data-testid="coverage-loading"
        >
          Loading coverage
        </p>
        <p
          v-else-if="entry.state === 'none'"
          class="muted"
          data-testid="coverage-none"
        >
          No trading calendar covers this dataset, so its coverage cannot be shown.
        </p>
        <div
          v-else-if="entry.state === 'unavailable'"
          class="inline-error"
          role="alert"
          data-testid="coverage-unavailable"
        >
          <span>Could not load coverage.</span>
          <button
            type="button"
            class="btn"
            @click="retryCoverage"
          >
            {{ titleCase('Try Again') }}
          </button>
        </div>
        <template v-else>
          <CoverageStrip
            :strip="entry.strip"
            variant="blocks"
            show-gaps
          />
          <p
            v-if="noGaps"
            class="muted"
            data-testid="coverage-no-gaps"
          >
            No gaps: every due session holds bars.
          </p>
          <p class="hint">
            Gap dates are shown as UTC calendar days. Ranges are half-open: the end is not included.
          </p>
        </template>
      </section>

      <section
        id="detail-history"
        class="block"
        aria-labelledby="detail-history-title"
      >
        <h2
          id="detail-history-title"
          class="block__title"
        >
          {{ titleCase('History') }}
        </h2>
        <p
          v-if="runsStatus === 'loading' || runsStatus === 'idle'"
          class="muted"
          role="status"
          data-testid="runs-loading"
        >
          Loading runs
        </p>
        <div
          v-else-if="runsStatus === 'error'"
          class="inline-error"
          role="alert"
          data-testid="runs-error"
        >
          <span>Could not load runs. {{ runsError?.message }}</span>
          <span
            v-if="runsError?.errorId"
            class="panel__id"
          >Error ID: {{ runsError.errorId }}</span>
          <button
            type="button"
            class="btn"
            @click="store.reloadRuns()"
          >
            {{ titleCase('Try Again') }}
          </button>
        </div>
        <p
          v-else-if="runs.length === 0"
          class="muted"
          data-testid="runs-empty"
        >
          No collection runs yet. This dataset has not been collected since runs were recorded.
        </p>
        <template v-else>
          <IngestRunsTable :runs="runs" />
          <div class="more">
            <button
              v-if="hasMoreRuns"
              type="button"
              class="btn"
              :disabled="loadingMore"
              data-testid="runs-load-more"
              @click="store.loadMoreRuns()"
            >
              {{ loadingMore ? 'Loading runs' : titleCase('Load More') }}
            </button>
            <span
              v-if="moreError"
              class="inline-error"
              role="alert"
              data-testid="runs-more-error"
            >Could not load more runs. {{ moreError.message }}</span>
          </div>
        </template>
      </section>

      <section
        id="detail-retention"
        class="block"
        aria-labelledby="detail-retention-title"
      >
        <h2
          id="detail-retention-title"
          class="block__title"
        >
          {{ titleCase('Retention') }}
        </h2>
        <dl
          v-if="retention"
          class="meta"
          data-testid="detail-retention"
        >
          <div>
            <dt>{{ titleCase('Expiry Type') }}</dt>
            <dd data-fact="expiry-type">
              {{ retention.type }}
            </dd>
          </div>
          <div>
            <dt>{{ titleCase('Expiry Time') }}</dt>
            <dd data-fact="expiry">
              {{ retention.expiry }}
            </dd>
          </div>
          <div v-if="retention.retired">
            <dt>{{ titleCase('State') }}</dt>
            <dd>Retired: no longer collecting</dd>
          </div>
        </dl>
        <p class="hint">
          Read only. Setting or clearing an expiry arrives with a later release.
        </p>
      </section>
    </template>
  </section>
</template>

<style scoped>
.detail {
  min-height: 100%;
  display: flex;
  flex-direction: column;
  gap: var(--tj-page-gap);
}

.detail__head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.detail__title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}

.block {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.block__title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
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
  text-decoration: none;
  cursor: pointer;
}

.btn:disabled {
  color: var(--tj-text3);
  cursor: not-allowed;
}

.link {
  color: var(--tj-accent-tint);
  font-size: 13px;
}

.panel {
  min-height: 160px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: var(--tj-surface);
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
  color: var(--tj-text2);
  font-size: 13px;
}

.panel--error {
  color: var(--tj-text);
}

.panel__id {
  font-family: var(--tj-font-mono);
  font-size: 12px;
  color: var(--tj-text3);
}

.notice {
  padding: 8px 12px;
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
  background: var(--tj-raised);
  font-size: 12px;
  color: var(--tj-text2);
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
  align-items: center;
  gap: 12px;
  font-size: 13px;
  color: var(--tj-text);
}

.more {
  display: flex;
  align-items: center;
  gap: 12px;
}
</style>
