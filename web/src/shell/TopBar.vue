<script setup lang="ts">
// The top bar (canvas "Shell" boards): logo, the six sections, and on the right the time zone
// control, the deployment tag, the trading-group toggle and the Settings button. Only Data is built;
// the other sections are disabled in place with a "Not built yet" tooltip and no route.
import { Settings } from '@lucide/vue'
import Select from 'primevue/select'
import { useRoute } from 'vue-router'

import BrandMark from '@/components/brand/BrandMark.vue'
import { useFormatters } from '@/format/useFormatters'
import { useSettingsStore } from '@/stores/settings'
import { useUiConfigStore } from '@/stores/uiConfig'
import { NOT_BUILT_TOOLTIP, SECTIONS } from './navigation'
import TradingGroupToggle from './TradingGroupToggle.vue'
import { useShellModals } from './useShellModals'
import { useZoneOptions } from './useZoneOptions'

const route = useRoute()
const config = useUiConfigStore()
const settings = useSettingsStore()
const { timeZone } = useFormatters()
const { openSettings } = useShellModals()
const { zoneOptions } = useZoneOptions()

// The same override the Settings panel sets: picking here and there is one setting.
function onZoneChange(zone: string | null): void {
  if (zone !== null) settings.setTimeZoneOverride(zone)
}

// The Data section is active for every /data route.
function isActive(to: string): boolean {
  const base = `/${to.split('/')[1]}`
  return route.path === base || route.path.startsWith(`${base}/`)
}
</script>

<template>
  <header class="top-bar">
    <div class="top-bar__brand">
      <BrandMark />
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
      <Select
        :model-value="timeZone"
        :options="zoneOptions"
        filter
        aria-label="Time zone"
        class="top-bar__zone"
        data-testid="time-zone-select"
        @update:model-value="onZoneChange"
      />
      <span
        v-if="config.deploymentTag !== null"
        class="top-bar__tag tj-mono"
        :title="config.deploymentTag"
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

/* A compact real select: the PrimeVue Select's own border, radius and colours come from the
   preset; only the height, width and type size are set here. */
.top-bar__zone {
  height: 32px;
  min-width: 11rem;
  font-size: 13px;
}

/* A highlighted tag for a non-prod deployment: warn text and border on a 16% warn wash. Warn on
   the ground measures 7.5:1. Long labels truncate; the full text is in the title. */
.top-bar__tag {
  max-width: 14ch;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 0 8px;
  line-height: calc(var(--tj-tag-height) - 2px);
  border: 1px solid var(--tj-status-warn);
  border-radius: var(--tj-radius-tag);
  background: color-mix(in srgb, var(--tj-status-warn), transparent 84%);
  color: var(--tj-status-warn);
  font-size: 12px;
  font-weight: 500;
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
