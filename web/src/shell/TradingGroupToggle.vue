<script setup lang="ts">
// Simulation | Paper | Live (tj-u3r3mo). All three segments always render; a group the server does
// not allow is greyed IN PLACE with aria-disabled (tj-0rpt9t), so the bar never shifts. The selected
// segment takes the fill and text colour from GROUP_SPECS: the accent fill with white text, and for
// Live orange with dark text (onLive). Data pages ignore this toggle (tj-yq0htn).
import { GROUP_SPECS } from '@/theme/semantics'
import { useTradingGroupStore } from '@/stores/tradingGroup'

const NOT_ALLOWED_TOOLTIP = 'Not available on this deployment'

const store = useTradingGroupStore()

function segmentStyle(option: (typeof store.options)[number]): Record<string, string> | undefined {
  if (store.selected !== option.key) return undefined
  const spec = GROUP_SPECS[option.group]
  return { background: spec.selectedFill, color: spec.onSelected }
}
</script>

<template>
  <div
    class="group-toggle"
    role="group"
    aria-label="Trading group"
  >
    <button
      v-for="option in store.options"
      :key="option.key"
      type="button"
      class="group-toggle__segment"
      :class="{ 'is-selected': store.selected === option.key, 'is-disabled': !option.allowed }"
      :style="segmentStyle(option)"
      :aria-pressed="store.selected === option.key"
      :aria-disabled="!option.allowed"
      :title="option.allowed ? undefined : NOT_ALLOWED_TOOLTIP"
      :data-group="option.key"
      @click="store.select(option.key)"
    >
      {{ option.label }}
    </button>
  </div>
</template>

<style scoped>
.group-toggle {
  display: inline-flex;
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
}

.group-toggle__segment {
  height: var(--tj-segment-height);
  padding: 0 12px;
  display: inline-flex;
  align-items: center;
  border: 0;
  border-radius: var(--tj-radius-tag);
  background: transparent;
  color: var(--tj-text2);
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}

.group-toggle__segment.is-disabled {
  color: var(--tj-text3);
  opacity: 0.55;
  cursor: default;
}
</style>
