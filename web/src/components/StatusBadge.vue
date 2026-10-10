<script setup lang="ts">
// A status dot plus its label, from a BadgeSpec (theme/semantics.ts). The label carries the
// meaning; the colour only supports it. Pass the spec from freshnessBadge(), collectionBadge(),
// requestBadge() or STALE_BADGE rather than a literal.
import type { BadgeSpec } from '@/theme/semantics'

defineProps<{ badge: BadgeSpec }>()
</script>

<template>
  <span
    class="status-badge"
    :style="{ color: badge.color }"
  >
    <span
      class="status-badge__dot"
      :style="{ background: badge.color }"
    />
    {{ badge.label }}
  </span>
</template>

<style scoped>
.status-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  white-space: nowrap;
}

.status-badge__dot {
  width: var(--tj-status-dot);
  height: var(--tj-status-dot);
  border-radius: 4px;
}
</style>
