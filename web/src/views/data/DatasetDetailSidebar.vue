<script setup lang="ts">
// The Dataset detail sidebar (tj-grna9p.34), the `sidebar` view of the data-dataset-detail route:
// a link up to All Datasets and the screen's sections. Overview, Coverage, History and Retention
// scroll to their section; Usage is shown disabled (a later phase). The sidebar leads nowhere but up
// (the catalog) and within the page: the Viewer is the parent, reached by the breadcrumb, so there is
// no item that goes back to it (owner ruling 2026-10-13, tj-grna9p.116).
import { NOT_BUILT_TOOLTIP } from '@/shell/navigation'
import { titleCase } from '@/theme/copy'

const SECTIONS = [
  { key: 'overview', label: titleCase('Overview') },
  { key: 'coverage', label: titleCase('Coverage') },
  { key: 'history', label: titleCase('History') },
  { key: 'retention', label: titleCase('Retention') },
] as const

function jump(key: string): void {
  document.getElementById(`detail-${key}`)?.scrollIntoView?.({ block: 'start' })
}
</script>

<template>
  <section
    class="side-section"
    aria-label="Dataset"
  >
    <RouterLink
      class="side-link"
      :to="{ name: 'data-datasets' }"
      data-testid="back-to-datasets"
    >
      {{ titleCase('All Datasets') }}
    </RouterLink>
  </section>

  <nav
    class="side-section"
    aria-label="Sections"
  >
    <span class="side-label">Sections</span>
    <button
      v-for="section in SECTIONS"
      :key="section.key"
      type="button"
      class="side-item"
      :data-section="section.key"
      @click="jump(section.key)"
    >
      {{ section.label }}
    </button>
    <button
      type="button"
      class="side-item"
      disabled
      :title="NOT_BUILT_TOOLTIP"
      data-section="usage"
    >
      {{ titleCase('Usage') }}
    </button>
  </nav>
</template>

<style scoped>
.side-section {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.side-label {
  margin-bottom: 4px;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--tj-text3);
}

.side-link {
  color: var(--tj-accent-tint);
  font-size: 13px;
  text-decoration: none;
}

.side-item {
  display: flex;
  align-items: center;
  height: var(--tj-filter-row-height);
  padding: 0 8px;
  border: 0;
  border-radius: var(--tj-radius-control);
  background: transparent;
  color: var(--tj-text);
  font: inherit;
  font-size: 13px;
  text-align: left;
  text-decoration: none;
  cursor: pointer;
}

.side-item:hover:not(:disabled) {
  background: var(--tj-raised);
}

.side-item:disabled {
  color: var(--tj-text3);
  cursor: not-allowed;
}
</style>
