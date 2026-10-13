<script setup lang="ts">
// The events-connection indicator in the top bar: a status dot plus a word (Connecting, Connected,
// Reconnecting), never colour alone. A polite live region so a drop is announced once. It reads the
// shared client's state; the shell opens the socket (useEventsConnection), so this only shows it.
import { computed } from 'vue'

import { eventsState } from '@/api/events'
import StatusBadge from '@/components/StatusBadge.vue'
import { EVENTS_CONNECTION_BADGES, EVENTS_CONNECTION_HINTS } from '@/theme/semantics'

const badge = computed(() => EVENTS_CONNECTION_BADGES[eventsState.value])
const hint = computed(() => EVENTS_CONNECTION_HINTS[eventsState.value])
</script>

<template>
  <span
    class="events-indicator"
    role="status"
    :title="hint"
    :data-state="eventsState"
    data-testid="events-indicator"
  >
    <StatusBadge :badge="badge" />
  </span>
</template>

<style scoped>
.events-indicator {
  display: inline-flex;
}
</style>
