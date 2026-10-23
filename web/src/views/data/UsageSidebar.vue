<script setup lang="ts">
// The Usage sidebar (tj-grna9p.62), the `sidebar` view of the data-usage route: Read By (the shared
// SDK / UI toggle, default SDK), a Client multi-select fed by the clients seen reading, and a Source
// filter. Picking clients sends client_id, which wins over Read By, so the toggle says so. There is
// no Stale After control: the threshold is the fixed 90 days.
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import { DataSource } from '@generated/trader_joe/proto/market/v1/enums_pb'

import { DEFAULT_CLIENT_KINDS, int64ToNumber } from '@/api'
import { sourceLabel } from '@/catalog/labels'
import { clientKindLabel } from '@/catalog/usage'
import ReadByToggle from '@/components/catalog/ReadByToggle.vue'
import { formatNumber } from '@/format/formatters'
import { useUsageStore } from '@/stores/usage'
import { titleCase } from '@/theme/copy'
import { effectiveClientKinds } from '@/catalog/filters'

const store = useUsageStore()
const { clientKinds, clientIds, clients, sources, source, clientsPicked } = storeToRefs(store)

const filtered = computed(() => {
  const kinds = effectiveClientKinds({ clientKinds: [...clientKinds.value] })
  const defaultKinds =
    kinds.length === DEFAULT_CLIENT_KINDS.length && kinds.every((kind) => DEFAULT_CLIENT_KINDS.includes(kind))
  return clientsPicked.value || source.value !== undefined || !defaultKinds
})

function toggleClient(id: string): void {
  const next = clientIds.value.includes(id) ? clientIds.value.filter((entry) => entry !== id) : [...clientIds.value, id]
  void store.setClientIds(next)
}

function toggleSource(value: DataSource): void {
  void store.setSource(source.value === value ? undefined : value)
}
</script>

<template>
  <section
    class="side-section"
    aria-label="Filters"
  >
    <div class="side-heading">
      <span class="side-label">Filters</span>
      <button
        v-if="filtered"
        type="button"
        class="side-clear"
        data-testid="usage-clear"
        @click="store.clearFilters()"
      >
        Clear
      </button>
    </div>

    <div class="side-group">
      <ReadByToggle
        :model-value="clientKinds"
        @update:model-value="store.setClientKinds($event)"
      />
      <p
        v-if="clientsPicked"
        class="side-hint"
        data-testid="read-by-overridden"
      >
        The clients chosen below count instead of Read By.
      </p>
    </div>

    <fieldset
      class="side-group"
      data-testid="usage-clients"
    >
      <legend class="side-group__title">
        {{ titleCase('Client') }}
      </legend>
      <p
        v-if="clients.length === 0"
        class="side-hint"
      >
        No client has read a dataset yet.
      </p>
      <label
        v-for="entry in clients"
        :key="entry.clientId"
        class="side-option"
        :data-client="entry.clientId"
      >
        <input
          type="checkbox"
          :checked="clientIds.includes(entry.clientId)"
          @change="toggleClient(entry.clientId)"
        >
        <span class="side-option__text">{{ entry.clientId }}</span>
        <span class="side-kind">{{ clientKindLabel(entry.clientKind) }}</span>
        <span class="side-count">{{ formatNumber(int64ToNumber(entry.datasets)) }}</span>
      </label>
    </fieldset>

    <fieldset
      class="side-group"
      data-testid="usage-sources"
    >
      <legend class="side-group__title">
        {{ titleCase('Source') }}
      </legend>
      <label
        v-for="entry in sources"
        :key="entry.source"
        class="side-option"
        :data-source="DataSource[entry.source]"
      >
        <input
          type="checkbox"
          :checked="source === entry.source"
          @change="toggleSource(entry.source)"
        >
        <span class="side-option__text">{{ sourceLabel(entry.source) }}</span>
        <span class="side-count">{{ entry.count }}</span>
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

.side-group + .side-group {
  margin-top: 10px;
}

.side-group__title {
  padding: 2px 0 4px;
  font-size: 12px;
  color: var(--tj-text2);
}

.side-hint {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--tj-text3);
}

.side-option {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: var(--tj-filter-row-height);
  font-size: 13px;
  color: var(--tj-text);
}

.side-option input {
  margin: 0;
}

.side-option__text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.side-kind {
  font-size: 11px;
  color: var(--tj-text3);
}

.side-count {
  margin-left: auto;
  font-family: var(--tj-font-mono);
  font-size: 12px;
  color: var(--tj-text3);
}
</style>
