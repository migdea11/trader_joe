<script setup lang="ts">
// The Data section's sub-tab row: Datasets, Requests, Health, Usage. A tab that is not built yet is
// disabled in place ("Not built yet") and never navigates; its route exists and shows a placeholder,
// so a typed URL does not 404. Tab names are the page titles (theme/copy.ts DATA_TABS).
import { useRoute } from 'vue-router'

import { DATA_SUB_TABS, NOT_BUILT_TOOLTIP } from './navigation'

const route = useRoute()

// Active on the tab's own path and below it (the Viewer sits under Datasets).
function isActive(path: string): boolean {
  return route.path === path || route.path.startsWith(`${path}/`)
}
</script>

<template>
  <nav
    class="sub-tabs"
    aria-label="Data"
  >
    <template
      v-for="tab in DATA_SUB_TABS"
      :key="tab.label"
    >
      <RouterLink
        v-if="tab.built"
        :to="tab.path"
        class="sub-tabs__tab"
        :class="{ 'is-active': isActive(tab.path) }"
        :aria-current="isActive(tab.path) ? 'page' : undefined"
      >
        {{ tab.label }}
      </RouterLink>
      <button
        v-else
        type="button"
        class="sub-tabs__tab is-disabled"
        aria-disabled="true"
        :title="NOT_BUILT_TOOLTIP"
      >
        {{ tab.label }}
      </button>
    </template>
  </nav>
</template>

<style scoped>
.sub-tabs {
  height: var(--tj-sub-tabs-height);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: var(--tj-bar-padding);
  border-bottom: 1px solid var(--tj-line);
}

.sub-tabs__tab {
  height: var(--tj-control-height);
  display: inline-flex;
  align-items: center;
  padding: 0 12px;
  border: 0;
  border-radius: var(--tj-radius-control);
  background: transparent;
  color: var(--tj-text2);
  font: inherit;
  font-size: 13px;
  text-decoration: none;
}

.sub-tabs__tab.is-active {
  background: var(--tj-raised);
  color: var(--tj-text);
}

.sub-tabs__tab.is-disabled {
  color: var(--tj-text3);
  opacity: 0.55;
  cursor: default;
}
</style>
