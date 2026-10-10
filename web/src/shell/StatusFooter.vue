<script setup lang="ts">
// The footer status line (canvas boards): the screen's own count text on the left (useFooterSummary),
// then now and the active zone, the versions and the About and Credits link on the right. "Now" is
// re-read on navigation and when the tab becomes visible, never on a timer (useNow).
import { useFormatters } from '@/format/useFormatters'
import { useUiConfigStore } from '@/stores/uiConfig'
import { useFooterSummary } from './useFooterSummary'
import { useNow } from './useNow'
import { useShellModals } from './useShellModals'

const { footerSummary } = useFooterSummary()
const { now } = useNow()
const { dateTime, timeZone } = useFormatters()
const config = useUiConfigStore()
const { openAbout } = useShellModals()
</script>

<template>
  <footer class="status-footer">
    <span data-testid="footer-summary">{{ footerSummary }}</span>
    <span class="status-footer__right">
      <span data-testid="footer-versions">Server {{ config.serverVersionText }} · UI {{ config.uiVersionText }}</span>
      <button
        type="button"
        class="status-footer__link"
        @click="openAbout"
      >About and Credits</button>
      <span data-testid="footer-now">{{ dateTime(now) }} · {{ timeZone }}</span>
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
  display: flex;
  gap: 16px;
}

.status-footer__link {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--tj-accent-tint);
  font: inherit;
  cursor: pointer;
}
</style>
