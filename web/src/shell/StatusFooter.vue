<script setup lang="ts">
// The footer status line. It shows only what exists nowhere else on the screen (useFooterSummary):
// the screen's own summary text, "Updated <time>" for the data on screen, and a problem when the deployment config or a request failed. Versions live in About,
// the zone, the trading group and the deployment tag in the top bar. With nothing to show the
// footer is not rendered, so no empty strip is left.
import { computed } from 'vue'

import { useFormatters } from '@/format/useFormatters'
import { useUiConfigStore } from '@/stores/uiConfig'
import { useFooterSummary } from './useFooterSummary'

const { footerSummary, footerUpdatedAt, footerProblem } = useFooterSummary()
const { dateTime } = useFormatters()
const config = useUiConfigStore()

const problem = computed(() => {
  if (config.status === 'error') return 'Could not load the deployment configuration'
  return footerProblem.value
})

const visible = computed(
  () => footerSummary.value !== null || footerUpdatedAt.value !== null || problem.value !== null,
)
</script>

<template>
  <footer
    v-if="visible"
    class="status-footer"
  >
    <span
      v-if="footerSummary !== null"
      data-testid="footer-summary"
    >{{ footerSummary }}</span>
    <span class="status-footer__right">
      <span
        v-if="problem !== null"
        class="status-footer__problem"
        role="status"
        data-testid="footer-problem"
      >{{ problem }}</span>
      <span
        v-if="footerUpdatedAt !== null"
        data-testid="footer-updated"
      >Updated {{ dateTime(footerUpdatedAt) }}</span>
    </span>
  </footer>
</template>

<style scoped>
.status-footer {
  flex-shrink: 0;
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 8px 28px;
  border-top: 1px solid var(--tj-line);
  font-size: 12px;
  color: var(--tj-text3);
}

.status-footer__right {
  margin-left: auto;
  display: flex;
  gap: 16px;
}

.status-footer__problem {
  color: var(--tj-status-fail);
}

</style>
