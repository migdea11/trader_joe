<script setup lang="ts">
// The Settings panel (phase 1): the time zone override and the About and Credits link. Stale After
// days moves in with its screens (tj-grna9p.87); the store already holds it. Settings are saved in
// this browser only (tj-grna9p.58); when storage is unavailable the panel says so quietly.
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Select from 'primevue/select'
import { storeToRefs } from 'pinia'

import { useSettingsStore } from '@/stores/settings'
import { useShellModals } from './useShellModals'
import { useZoneOptions } from './useZoneOptions'

const settings = useSettingsStore()
const { timeZone, timeZoneOverride, storageOk } = storeToRefs(settings)
const { settingsOpen, closeSettings, openAbout } = useShellModals()

const { zoneOptions, browserZone } = useZoneOptions()

function onZoneChange(zone: string | null): void {
  if (zone !== null) settings.setTimeZoneOverride(zone)
}

function showAbout(): void {
  closeSettings()
  openAbout()
}
</script>

<template>
  <Dialog
    v-model:visible="settingsOpen"
    modal
    header="Settings"
    :style="{ width: '28rem' }"
    :draggable="false"
  >
    <div class="settings">
      <div class="settings__field">
        <label
          id="settings-time-zone-label"
          class="settings__label"
        >Time Zone</label>
        <Select
          :model-value="timeZone"
          :options="zoneOptions"
          filter
          aria-labelledby="settings-time-zone-label"
          class="settings__select"
          @update:model-value="onZoneChange"
        />
        <div class="settings__row">
          <Button
            label="Use Browser Zone"
            size="small"
            severity="secondary"
            :disabled="timeZoneOverride === null"
            @click="settings.useBrowserZone()"
          />
          <span class="settings__hint">Browser zone: {{ browserZone }}</span>
        </div>
      </div>

      <p
        class="settings__hint"
        data-testid="storage-hint"
      >
        {{
          storageOk
            ? 'Saved in this browser'
            : 'This browser blocks storage, so settings last only until you reload'
        }}
      </p>

      <Button
        label="About and Credits"
        size="small"
        variant="text"
        class="settings__about"
        @click="showAbout"
      />
    </div>
  </Dialog>
</template>

<style scoped>
.settings {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.settings__field {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.settings__label {
  font-size: 12px;
  color: var(--tj-text2);
}

.settings__row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.settings__hint {
  margin: 0;
  font-size: 12px;
  color: var(--tj-text3);
}

.settings__about {
  align-self: flex-start;
}
</style>
