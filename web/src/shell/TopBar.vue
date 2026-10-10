<script setup lang="ts">
// The top bar (canvas "Shell" boards): logo, the six sections, and on the right the time zone
// control, the deployment tag, the trading-group toggle and the Settings button. Only Data is built;
// the other sections are disabled in place with a "Not built yet" tooltip and no route.
import { ChevronDown, Globe, Settings } from '@lucide/vue'
import { useRoute } from 'vue-router'

import { useFormatters } from '@/format/useFormatters'
import { useUiConfigStore } from '@/stores/uiConfig'
import { NOT_BUILT_TOOLTIP, SECTIONS } from './navigation'
import TradingGroupToggle from './TradingGroupToggle.vue'
import { useShellModals } from './useShellModals'

const route = useRoute()
const config = useUiConfigStore()
const { timeZone } = useFormatters()
const { openSettings } = useShellModals()

// The Data section is active for every /data route.
function isActive(to: string): boolean {
  const base = `/${to.split('/')[1]}`
  return route.path === base || route.path.startsWith(`${base}/`)
}
</script>

<template>
  <header class="top-bar">
    <div class="top-bar__brand">
      <span
        class="top-bar__logo"
        aria-hidden="true"
      />
      <span class="top-bar__name">trader_joe</span>
    </div>

    <nav
      class="top-bar__sections"
      aria-label="Sections"
    >
      <template
        v-for="section in SECTIONS"
        :key="section.label"
      >
        <RouterLink
          v-if="section.to"
          :to="section.to"
          class="top-bar__section"
          :class="{ 'is-active': isActive(section.to) }"
          :aria-current="isActive(section.to) ? 'page' : undefined"
        >
          <component
            :is="section.icon"
            :size="16"
            aria-hidden="true"
          />
          {{ section.label }}
        </RouterLink>
        <button
          v-else
          type="button"
          class="top-bar__section is-disabled"
          aria-disabled="true"
          :title="NOT_BUILT_TOOLTIP"
        >
          <component
            :is="section.icon"
            :size="16"
            aria-hidden="true"
          />
          {{ section.label }}
        </button>
      </template>
    </nav>

    <div class="top-bar__right">
      <button
        type="button"
        class="top-bar__zone"
        :aria-label="`Time zone: ${timeZone}, change`"
        @click="openSettings"
      >
        <Globe
          :size="14"
          aria-hidden="true"
        />
        {{ timeZone }}
        <ChevronDown
          :size="14"
          aria-hidden="true"
        />
      </button>
      <span
        class="top-bar__tag tj-mono"
        :class="{ 'is-unset': !config.hasDeploymentLabel }"
        :title="config.hasDeploymentLabel ? 'Deployment' : 'The server sent no deployment label'"
        data-testid="deployment-tag"
      >{{ config.deploymentTag }}</span>
      <TradingGroupToggle />
      <button
        type="button"
        class="top-bar__settings"
        aria-label="Settings"
        title="Settings"
        @click="openSettings"
      >
        <Settings
          :size="18"
          aria-hidden="true"
        />
      </button>
    </div>
  </header>
</template>

<style scoped>
.top-bar {
  height: var(--tj-top-bar-height);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 20px;
  padding: var(--tj-bar-padding);
  border-bottom: 1px solid var(--tj-line);
}

.top-bar__brand {
  display: flex;
  align-items: center;
  gap: 10px;
}

.top-bar__logo {
  width: 22px;
  height: 22px;
  border-radius: 5px;
  background: var(--tj-accent-fill);
}

.top-bar__name {
  font-weight: 600;
}

.top-bar__sections {
  display: flex;
  gap: 2px;
}

.top-bar__section {
  display: flex;
  align-items: center;
  gap: 8px;
  height: var(--tj-top-bar-height);
  padding: 0 14px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--tj-text2);
  font: inherit;
  font-size: 14px;
  text-decoration: none;
}

.top-bar__section.is-active {
  color: var(--tj-text);
  border-bottom-color: var(--tj-accent-tint);
}

.top-bar__section.is-disabled {
  color: var(--tj-text3);
  opacity: 0.55;
  cursor: default;
}

.top-bar__right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: 13px;
  color: var(--tj-text2);
}

.top-bar__zone {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 10px;
  background: transparent;
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
  color: var(--tj-text2);
  font: inherit;
  cursor: pointer;
}

.top-bar__tag {
  color: var(--tj-text3);
}

.top-bar__tag.is-unset {
  font-style: italic;
}

.top-bar__settings {
  display: inline-flex;
  padding: 4px;
  border: 0;
  background: transparent;
  color: var(--tj-text2);
  cursor: pointer;
}
</style>
