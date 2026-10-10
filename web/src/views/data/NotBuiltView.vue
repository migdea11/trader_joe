<script setup lang="ts">
// The placeholder for a Data tab whose screen is a later phase (Requests, Health, Usage). The tab
// itself is disabled in the sub-tab row; this route exists so a typed or bookmarked URL does not 404.
// The page title is the tab name, from the route meta.
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import { NOT_BUILT_TOOLTIP } from '@/shell/navigation'

const route = useRoute()
const title = computed(() => (typeof route.meta.title === 'string' ? route.meta.title : 'Page'))
</script>

<template>
  <section>
    <h1 class="page-title">
      {{ title }}
    </h1>
    <p class="page-note">
      {{ NOT_BUILT_TOOLTIP }}. This screen arrives in a later phase.
    </p>
  </section>
</template>

<style scoped>
.page-title {
  margin: 0 0 8px;
  font-size: 20px;
  font-weight: 600;
}

.page-note {
  margin: 0;
  color: var(--tj-text2);
}
</style>
