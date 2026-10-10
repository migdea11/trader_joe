<script setup lang="ts">
// The Datasets sidebar (tj-grna9p.32), the `sidebar` view of the data-datasets route: Views (All
// Datasets, Needs Attention) and Filters (Status, Source, Update) with counts from the facets. The
// server filters by one value per group, so each group is a single choice: ticking an option
// replaces the group's current one, and ticking it again clears the group. Stream is shown disabled
// (a later phase). Everything is written to the URL (catalog/useCatalogFilters.ts).
import { computed } from 'vue'
import { storeToRefs } from 'pinia'

import { DataSource, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'

import type { StatusGroup } from '@/api'
import { hasActiveFilters, STATUS_GROUPS, withView } from '@/catalog/filters'
import type { CatalogView } from '@/catalog/filters'
import { sourceLabel } from '@/catalog/labels'
import { useCatalogFilters } from '@/catalog/useCatalogFilters'
import { useDatasetCatalogStore } from '@/stores/datasetCatalog'
import { titleCase } from '@/theme/copy'

const { filters, setFilters } = useCatalogFilters()
const { facets } = storeToRefs(useDatasetCatalogStore())

const VIEWS: readonly { view: CatalogView; label: string }[] = [
  { view: 'all', label: titleCase('All Datasets') },
  { view: 'needs-attention', label: titleCase('Needs Attention') },
]

const STATUS_LABELS: Record<StatusGroup, string> = {
  healthy: 'Healthy',
  late: 'Late',
  failed: 'Failed',
  retired: 'Retired',
}

function viewCount(view: CatalogView): number | null {
  if (facets.value === null) return null
  return view === 'all' ? facets.value.all : facets.value.needsAttention
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
        :key="entry.view"
        type="button"
        class="side-view"
        :class="{ 'is-active': filters.view === entry.view }"
        :aria-current="filters.view === entry.view ? 'true' : undefined"
        :data-view="entry.view"
        @click="setFilters(withView(filters, entry.view))"
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
        Status
      </legend>
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
  accent-color: var(--tj-accent-tint);
  margin: 0;
  width: 15px;
  height: 15px;
}

.side-count {
  margin-left: auto;
  font-family: var(--tj-font-mono);
  font-size: 12px;
  color: var(--tj-text3);
}
</style>
