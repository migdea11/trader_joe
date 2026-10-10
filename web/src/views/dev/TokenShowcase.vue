<script setup lang="ts">
// Dev-only token showcase for the owner's visual check (tj-grna9p.60). Registered by the router
// only when import.meta.env.DEV, so it is absent from the production build.
import Button from 'primevue/button'
import { Database, Settings } from '@lucide/vue'

import StatusBadge from '@/components/StatusBadge.vue'
import {
  ACCOUNT_GROUP_VALUES,
  FRESHNESS_STATUS_VALUES,
  GROUP_SPECS,
  STALE_BADGE,
  RUNNING_BADGE,
  freshnessBadge,
  type AccountGroupName,
} from '@/theme/semantics'
import { colors, fonts } from '@/theme/tokens'

const neutrals = [
  ['Ground', colors.ground],
  ['Surface', colors.surface],
  ['Raised', colors.raised],
  ['Line', colors.line],
  ['Text', colors.text],
  ['Text 2', colors.text2],
  ['Text 3', colors.text3],
] as const

const accents = [
  ['Accent Fill', colors.accentFill],
  ['Accent Tint', colors.accentTint],
  ['Up', colors.up],
  ['Down', colors.down],
] as const

const statuses = Object.entries(colors.status)
const groups = ACCOUNT_GROUP_VALUES

// The toggle: one selected segment at a time. Simulation and Paper select with the accent fill;
// Live selects with orange and dark text.
const selected: AccountGroupName = 'ACCOUNT_GROUP_SIMULATION'
const liveSelected: AccountGroupName = 'ACCOUNT_GROUP_LIVE'
</script>

<template>
  <main class="showcase">
    <h1>Token Showcase</h1>

    <section>
      <h2>Neutrals</h2>
      <div class="swatches">
        <div
          v-for="[name, value] in neutrals"
          :key="name"
          class="swatch"
        >
          <div
            class="swatch__chip"
            :style="{ background: value }"
          />
          <div>{{ name }}</div>
          <code>{{ value }}</code>
        </div>
      </div>
    </section>

    <section>
      <h2>Accent, Gains and Losses</h2>
      <div class="swatches">
        <div
          v-for="[name, value] in accents"
          :key="name"
          class="swatch"
        >
          <div
            class="swatch__chip"
            :style="{ background: value }"
          />
          <div>{{ name }}</div>
          <code>{{ value }}</code>
        </div>
      </div>
    </section>

    <section>
      <h2>Status</h2>
      <div class="row">
        <div
          v-for="[name, value] in statuses"
          :key="name"
          class="swatch"
        >
          <div
            class="swatch__chip"
            :style="{ background: value }"
          />
          <div>{{ name }}</div>
          <code>{{ value }}</code>
        </div>
      </div>
    </section>

    <section>
      <h2>Chart Series</h2>
      <div class="row">
        <div
          v-for="(value, index) in colors.series"
          :key="value"
          class="swatch"
        >
          <div
            class="swatch__chip"
            :style="{ background: value }"
          />
          <code>{{ index + 1 }} {{ value }}</code>
        </div>
      </div>
    </section>

    <section>
      <h2>Buttons</h2>
      <div class="row">
        <Button label="Request Data" />
        <Button
          label="Refresh"
          severity="secondary"
          variant="outlined"
        />
        <Button
          aria-label="Settings"
          variant="text"
        >
          <template #icon>
            <Settings
              :size="16"
              aria-hidden="true"
            />
          </template>
        </Button>
        <Database
          :size="16"
          :color="colors.text2"
          aria-hidden="true"
        />
      </div>
    </section>

    <section>
      <h2>Health Badges</h2>
      <div class="row">
        <span
          v-for="status in FRESHNESS_STATUS_VALUES"
          :key="status"
          class="badge-cell"
        >
          <StatusBadge :badge="freshnessBadge(status)" />
          <code>{{ status.replace('FRESHNESS_STATUS_', '') }}</code>
        </span>
        <span class="badge-cell">
          <StatusBadge :badge="freshnessBadge(undefined)" />
          <code>(unset)</code>
        </span>
        <span class="badge-cell">
          <StatusBadge :badge="RUNNING_BADGE" />
          <code>RUNNING run</code>
        </span>
        <span class="badge-cell">
          <StatusBadge :badge="STALE_BADGE" />
          <code>usage</code>
        </span>
      </div>
    </section>

    <section>
      <h2>Trading Group Toggle</h2>
      <div class="row">
        <div
          role="group"
          aria-label="Trading group"
          class="toggle"
        >
          <span
            v-for="group in groups"
            :key="group"
            class="toggle__segment"
            :style="
              group === selected
                ? { background: GROUP_SPECS[group].selectedFill, color: GROUP_SPECS[group].onSelected }
                : { color: colors.text3, opacity: 0.55 }
            "
          >
            {{ GROUP_SPECS[group].label }}
          </span>
        </div>
        <div
          role="group"
          aria-label="Trading group, live selected"
          class="toggle"
        >
          <span
            v-for="group in groups"
            :key="group"
            class="toggle__segment"
            :style="
              group === liveSelected
                ? { background: GROUP_SPECS[group].selectedFill, color: GROUP_SPECS[group].onSelected }
                : { color: colors.text3 }
            "
          >
            {{ GROUP_SPECS[group].label }}
          </span>
        </div>
        <span
          v-for="group in groups"
          :key="group"
          class="tag"
          :style="{ color: GROUP_SPECS[group].color, borderColor: GROUP_SPECS[group].color }"
        >
          {{ GROUP_SPECS[group].label }}
        </span>
      </div>
    </section>

    <section>
      <h2>Type</h2>
      <p :style="{ fontFamily: fonts.sans }">
        IBM Plex Sans 400, <strong>600</strong>: VFV · bars · 1d
      </p>
      <p :style="{ fontFamily: fonts.mono }">
        IBM Plex Mono: 2019/01/02 → 2026/10/05 1234567.89
      </p>
    </section>
  </main>
</template>

<style scoped>
.showcase {
  padding: var(--tj-main-padding);
  display: flex;
  flex-direction: column;
  gap: 22px;
}

h1 {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}

h2 {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 600;
}

.swatches,
.row {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  align-items: center;
}

.swatch {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 132px;
  color: var(--tj-text2);
}

.swatch__chip {
  height: 56px;
  border-radius: var(--tj-radius-control);
  border: 1px solid var(--tj-line);
}

.badge-cell {
  display: inline-flex;
  flex-direction: column;
  gap: 4px;
}

.toggle {
  display: inline-flex;
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-control);
}

.toggle__segment {
  height: var(--tj-segment-height);
  padding: 0 12px;
  display: inline-flex;
  align-items: center;
  border-radius: var(--tj-radius-tag);
  font-weight: 500;
}

.tag {
  display: inline-flex;
  align-items: center;
  height: var(--tj-tag-height);
  padding: 0 10px;
  border: 1px solid;
  border-radius: var(--tj-radius-tag);
  font-size: 12px;
  font-weight: 500;
}
</style>
