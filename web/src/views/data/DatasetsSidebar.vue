<script setup lang="ts">
// The Datasets sidebar (tj-grna9p.32), the `sidebar` view of the data-datasets route: Views (All
// Datasets, Needs Attention) and Filters (Status, Source, Update) with counts from the facets.
// The server filters by one value per group, so each group is a single choice: ticking an option
// replaces the group's current one, and ticking it again clears the group. Running in the Status
// group is the exception: it is orthogonal to the freshness words (a Late dataset can be running a
// retry), so it is its own toggle and combines with one of them. Stream is shown disabled
// (a later phase). Phase 3 adds the Stale and Expiring Soon views, the Usage group (Read in the last
// 30 days, Stale, Never read) and the Read By group (SDK and UI, at least one ticked, default SDK):
// whose reads the usage counts and the Stale view follow. The Stale view is the Usage group's Stale
// option under another name; the one filter value backs both. Everything is written to the URL
// (catalog/useCatalogFilters.ts).
import { computed } from 'vue'
import { storeToRefs } from 'pinia'

import { DataSource, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'

import type { StatusGroup } from '@/api'
import { ClientKind } from '@generated/trader_joe/proto/ui/v1/data_pb'

import {
  CLIENT_KIND_OPTIONS,
  effectiveClientKinds,
  hasActiveFilters,
  STATUS_GROUPS,
  USAGE_GROUPS,
  withView,
} from '@/catalog/filters'
import type { CatalogFilters, CatalogView, UsageGroup } from '@/catalog/filters'
import { sourceLabel } from '@/catalog/labels'
import { usageCount } from '@/catalog/facets'
import { clientKindLabel, staleLabel, usageOptionLabel } from '@/catalog/usage'
import { useCatalogFilters } from '@/catalog/useCatalogFilters'
import { useDatasetCatalogStore } from '@/stores/datasetCatalog'
import { titleCase } from '@/theme/copy'

const { filters, setFilters } = useCatalogFilters()
const { facets } = storeToRefs(useDatasetCatalogStore())

// The Stale view is not a CatalogView of its own: it is the Usage filter's stale value, so its
// entry is active on that value.
interface ViewEntry {
  key: string
  label: string
  view: CatalogView | 'stale'
}

const VIEWS = computed<readonly ViewEntry[]>(() => [
  { key: 'all', view: 'all', label: titleCase('All Datasets') },
  { key: 'needs-attention', view: 'needs-attention', label: titleCase('Needs Attention') },
  { key: 'stale', view: 'stale', label: staleLabel(facets.value?.staleDays) },
  { key: 'expiring-soon', view: 'expiring-soon', label: titleCase('Expiring Soon') },
])

const STATUS_LABELS: Record<StatusGroup, string> = {
  healthy: 'Healthy',
  late: 'Late',
  failed: 'Failed',
  retired: 'Stopped',
}

function viewCount(view: ViewEntry['view']): number | null {
  if (facets.value === null) return null
  if (view === 'stale') return facets.value.stale
  if (view === 'expiring-soon') return facets.value.expiringSoon
  return view === 'all' ? facets.value.all : facets.value.needsAttention
}

function viewActive(view: ViewEntry['view']): boolean {
  const staleView = filters.value.usage === 'stale' && filters.value.view === 'all'
  if (view === 'stale') return staleView
  return filters.value.view === view && !(view === 'all' && staleView)
}

// Choosing a view leaves the Usage group alone except for the Stale view, which is that group's
// Stale value (so the other views drop it).
function selectView(view: ViewEntry['view']): void {
  const next: CatalogFilters =
    view === 'stale'
      ? { ...filters.value, view: 'all', usage: 'stale' }
      : { ...withView(filters.value, view), usage: filters.value.usage === 'stale' ? undefined : filters.value.usage }
  void setFilters(next)
}

const usageCounts = computed<Record<UsageGroup, number | undefined>>(() => ({
  'read-30d': facets.value === null ? undefined : usageCount(facets.value, 'read-30d'),
  stale: facets.value === null ? undefined : usageCount(facets.value, 'stale'),
  'never-read': facets.value === null ? undefined : usageCount(facets.value, 'never-read'),
}))

function toggleUsage(value: UsageGroup): void {
  void setFilters({ ...filters.value, usage: filters.value.usage === value ? undefined : value })
}

const readBy = computed(() => effectiveClientKinds(filters.value))

// At least one kind stays ticked: the last one cannot be cleared.
function toggleReadBy(kind: ClientKind): void {
  const current = readBy.value
  const next = current.includes(kind) ? current.filter((entry) => entry !== kind) : [...current, kind]
  if (next.length === 0) return
  void setFilters({ ...filters.value, clientKinds: next })
}

const updateOptions = computed(() => [
  { value: UpdateType.STATIC, label: 'Bulk', disabled: false },
  { value: UpdateType.DAILY, label: 'Daily', disabled: false },
  { value: UpdateType.STREAM, label: 'Stream (later)', disabled: true },
])

// An option's checked state toggles: choosing the chosen option clears the group.
function toggleStatus(value: StatusGroup): void {
  void setFilters({ ...filters.value, status: filters.value.status === value ? undefined : value })
}

function toggleRunning(): void {
  void setFilters({ ...filters.value, running: filters.value.running === true ? undefined : true })
}

function toggleSource(value: DataSource): void {
  void setFilters({ ...filters.value, source: filters.value.source === value ? undefined : value })
}

function toggleUpdate(value: UpdateType): void {
  void setFilters({ ...filters.value, updateType: filters.value.updateType === value ? undefined : value })
}

function clearFilters(): void {
  void setFilters(withView({ view: 'all' }, filters.value.view))
}
</script>

<template>
  <nav
    class="side-section"
    aria-label="Views"
  >
    <span class="side-label">Views</span>
    <div class="side-views">
      <button
        v-for="entry in VIEWS"
        :key="entry.key"
        type="button"
        class="side-view"
        :class="{ 'is-active': viewActive(entry.view) }"
        :aria-current="viewActive(entry.view) ? 'true' : undefined"
        :data-view="entry.key"
        @click="selectView(entry.view)"
      >
        <span>{{ entry.label }}</span>
        <span class="side-count">{{ viewCount(entry.view) ?? '—' }}</span>
      </button>
    </div>
  </nav>

  <section
    class="side-section"
    aria-label="Filters"
  >
    <div class="side-heading">
      <span class="side-label">Filters</span>
      <button
        v-if="hasActiveFilters(filters)"
        type="button"
        class="side-clear"
        @click="clearFilters"
      >
        Clear
      </button>
    </div>

    <fieldset class="side-group">
      <legend class="side-group__title">
        {{ titleCase('Status') }}
      </legend>
      <label
        class="side-option"
        data-collection="running"
      >
        <input
          type="checkbox"
          :checked="filters.running === true"
          @change="toggleRunning"
        >
        Running
        <span class="side-count">{{ facets?.running ?? '—' }}</span>
      </label>
      <label
        v-for="status in STATUS_GROUPS"
        :key="status"
        class="side-option"
        :data-status="status"
      >
        <input
          type="checkbox"
          :checked="filters.status === status"
          @change="toggleStatus(status)"
        >
        {{ STATUS_LABELS[status] }}
        <span class="side-count">{{ facets?.status[status] ?? '—' }}</span>
      </label>
    </fieldset>

    <fieldset class="side-group">
      <legend class="side-group__title">
        Source
      </legend>
      <label
        v-for="entry in facets?.sources ?? []"
        :key="entry.source"
        class="side-option"
        :data-source="DataSource[entry.source]"
      >
        <input
          type="checkbox"
          :checked="filters.source === entry.source"
          @change="toggleSource(entry.source)"
        >
        {{ sourceLabel(entry.source) }}
        <span class="side-count">{{ entry.count }}</span>
      </label>
    </fieldset>

    <fieldset class="side-group">
      <legend class="side-group__title">
        Update
      </legend>
      <label
        v-for="option in updateOptions"
        :key="option.value"
        class="side-option"
        :class="{ 'is-disabled': option.disabled }"
        :data-update="UpdateType[option.value]"
      >
        <input
          type="checkbox"
          :checked="filters.updateType === option.value"
          :disabled="option.disabled"
          @change="toggleUpdate(option.value)"
        >
        {{ option.label }}
        <span class="side-count">{{ facets?.updateTypes[option.value] ?? '—' }}</span>
      </label>
    </fieldset>

    <fieldset class="side-group">
      <legend class="side-group__title">
        {{ titleCase('Usage') }}
      </legend>
      <label
        v-for="group in USAGE_GROUPS"
        :key="group"
        class="side-option"
        :data-usage="group"
      >
        <input
          type="checkbox"
          :checked="filters.usage === group"
          @change="toggleUsage(group)"
        >
        {{ usageOptionLabel(group, filters, facets?.staleDays) }}
        <span class="side-count">{{ usageCounts[group] ?? '—' }}</span>
      </label>
    </fieldset>

    <fieldset class="side-group">
      <legend class="side-group__title">
        {{ titleCase('Read By') }}
      </legend>
      <label
        v-for="kind in CLIENT_KIND_OPTIONS"
        :key="kind"
        class="side-option"
        :data-read-by="ClientKind[kind]"
      >
        <input
          type="checkbox"
          :checked="readBy.includes(kind)"
          :disabled="readBy.length === 1 && readBy.includes(kind)"
          @change="toggleReadBy(kind)"
        >
        {{ clientKindLabel(kind) }}
      </label>
    </fieldset>
  </section>
</template>

<style scoped>
.side-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.side-heading {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}

.side-label {
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--tj-text3);
}

.side-views {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.side-view {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--tj-sidebar-row-height);
  padding: 0 10px;
  border: 0;
  border-radius: var(--tj-radius-control);
  background: transparent;
  color: var(--tj-text2);
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}

.side-view.is-active {
  background: var(--tj-raised);
  color: var(--tj-text);
}

.side-clear {
  border: 0;
  padding: 0;
  background: transparent;
  color: var(--tj-accent-tint);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.side-group {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  border: 0;
  min-width: 0;
}

.side-group__title {
  padding: 2px 0 4px;
  font-size: 12px;
  color: var(--tj-text2);
}

.side-group + .side-group {
  margin-top: 10px;
}

.side-option {
  display: flex;
  align-items: center;
  gap: 10px;
  height: var(--tj-filter-row-height);
  font-size: 13px;
  color: var(--tj-text);
}

.side-option.is-disabled {
  color: var(--tj-text3);
}

.side-option input {
  margin: 0;
}

.side-count {
  margin-left: auto;
  font-family: var(--tj-font-mono);
  font-size: 12px;
  color: var(--tj-text3);
}
</style>
