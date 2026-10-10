<script setup lang="ts">
// The app shell (tj-grna9p.30): top bar, Data sub-tabs, the 248px sidebar and the main area, then the
// footer status line. Screens fill two slots, both router views of the matched route:
//   default   the main area
//   sidebar   the 248px left column (the screen's own views, filters and controls)
// A route that declares no `sidebar` component leaves the column empty but present, so the main
// area never shifts. The shell loads the UiConfig once and keeps the trading group in the URL.
import { onMounted } from 'vue'

import { useUiConfigStore } from '@/stores/uiConfig'
import { useTradingGroupUrlSync } from '@/stores/useTradingGroupUrlSync'
import AboutModalHost from './AboutModalHost.vue'
import DataSubTabs from './DataSubTabs.vue'
import SettingsPanel from './SettingsPanel.vue'
import StatusFooter from './StatusFooter.vue'
import TopBar from './TopBar.vue'

const config = useUiConfigStore()
useTradingGroupUrlSync()
onMounted(() => {
  void config.load()
})
</script>

<template>
  <div class="app-shell">
    <TopBar />
    <DataSubTabs />
    <div class="app-shell__body">
      <aside
        class="app-shell__sidebar"
        aria-label="Screen controls"
      >
        <RouterView name="sidebar" />
      </aside>
      <main class="app-shell__main">
        <RouterView />
      </main>
    </div>
    <StatusFooter />
    <SettingsPanel />
    <AboutModalHost />
  </div>
</template>

<style scoped>
.app-shell {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--tj-ground);
  color: var(--tj-text);
  overflow: hidden;
}

.app-shell__body {
  flex: 1;
  min-height: 0;
  display: flex;
}

.app-shell__sidebar {
  width: var(--tj-sidebar-width);
  flex-shrink: 0;
  box-sizing: border-box;
  padding: var(--tj-sidebar-padding);
  border-right: 1px solid var(--tj-line);
  display: flex;
  flex-direction: column;
  gap: 20px;
  overflow-y: auto;
}

.app-shell__main {
  flex: 1;
  min-width: 0;
  padding: var(--tj-main-padding);
  overflow: auto;
}
</style>
