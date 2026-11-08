<script setup lang="ts">
// The aggregate asset-list history strip (tj-7zjov1.37, owner ruling 2026-10-24): one block per expected
// day of the panel's source (Saved, Missing, Empty, Not due), a bar per day for how much of the asset list
// changed, and settings-change markers. It is NOT a per-asset table: per-asset events are reached from the
// member list and the dataset detail. A marker carries its version, effective pass and date, actor and
// reason; a settings version no pass has run under is Pending, not a boundary. The server decides every
// state and count; this draws them. Position on the strip is the marker's instant within the window read.
import { computed } from 'vue'
import { AssetListDayState, PanelMarkerKind } from '@generated/trader_joe/proto/ui/v1/panels_pb'
import type {
  AssetListHistory,
  PanelBoundaries,
  PanelSettingsVersion,
  PanelSourceSettingsVersion,
} from '@generated/trader_joe/proto/ui/v1/panels_pb'

import { timestampToInstant } from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import { titleCase } from '@/theme/copy'

import { dayStateLabel, effectivePassText, markerLabel, stripMarkers } from './panelModel'
import type { StripMarker } from './panelModel'

const props = defineProps<{
  history: AssetListHistory
  boundaries: PanelBoundaries | null
  panelVersions: readonly PanelSettingsVersion[]
  sourceVersions: readonly PanelSourceSettingsVersion[]
}>()

const { dateTime } = useFormatters()

const LEGEND = [
  AssetListDayState.SAVED,
  AssetListDayState.MISSING,
  AssetListDayState.EMPTY,
  AssetListDayState.NOT_DUE,
]

const STATE_CLASS: Partial<Record<AssetListDayState, string>> = {
  [AssetListDayState.SAVED]: 'is-saved',
  [AssetListDayState.MISSING]: 'is-missing',
  [AssetListDayState.EMPTY]: 'is-empty',
  [AssetListDayState.NOT_DUE]: 'is-not-due',
}

const maxChanged = computed(() => Math.max(1, ...props.history.days.map((day) => day.changedCount)))

// The window the history was read for; a marker is placed within it.
const window = computed(() => ({
  start: props.history.start === undefined ? 0 : timestampToInstant(props.history.start),
  end: props.history.end === undefined ? 0 : timestampToInstant(props.history.end),
}))

const markers = computed<StripMarker[]>(() =>
  props.boundaries === null
    ? []
    : stripMarkers(props.boundaries, props.panelVersions, props.sourceVersions, window.value),
)

// Newest first for the reading list under the strip.
const markerList = computed(() => [...markers.value].reverse())

function isSettings(entry: StripMarker): boolean {
  return (
    entry.marker.kind === PanelMarkerKind.PANEL_SETTINGS || entry.marker.kind === PanelMarkerKind.SOURCE_SETTINGS
  )
}

function markerTitle(entry: StripMarker): string {
  const { marker } = entry
  const when = marker.at === undefined ? '' : dateTime(timestampToInstant(marker.at))
  const what = isSettings(entry) ? `${markerLabel(marker.kind)} version ${marker.versionNo}` : `${markerLabel(marker.kind)} ${marker.label}`
  return `${what}, ${when}`
}

function effectiveText(entry: StripMarker): string {
  if (!isSettings(entry)) return ''
  if (entry.effectivePass === undefined) return 'Pending: no pass has run yet'
  return `In effect from the ${effectivePassText(entry.effectivePass, dateTime)}`
}
</script>

<template>
  <section
    class="pn-card strip"
    aria-labelledby="panel-history-title"
    data-testid="panel-history"
  >
    <h2
      id="panel-history-title"
      class="pn-card__title"
    >
      {{ titleCase('Asset List History') }}
    </h2>
    <ul
      class="legend"
      aria-label="Day states"
    >
      <li
        v-for="state in LEGEND"
        :key="state"
        class="legend__item"
      >
        <span
          class="legend__swatch"
          :class="STATE_CLASS[state]"
        />
        {{ dayStateLabel(state) }}
      </li>
    </ul>

    <p
      v-if="history.days.length === 0"
      class="pn-muted"
      data-testid="history-empty"
    >
      No expected days in this window.
    </p>
    <div
      v-else
      class="days"
      role="list"
      aria-label="One block per expected day, with the number of assets whose listing changed"
      data-testid="history-days"
    >
      <div
        v-for="day in history.days"
        :key="day.day"
        class="day"
        role="listitem"
        :data-day="day.day"
        :data-state="dayStateLabel(day.state)"
        :title="`${day.day}: ${dayStateLabel(day.state)}${day.state === AssetListDayState.SAVED ? `, ${day.changedCount} changed` : ''}`"
        :aria-label="`${day.day}: ${dayStateLabel(day.state)}${day.state === AssetListDayState.SAVED ? `, ${day.changedCount} changed` : ''}`"
      >
        <span
          class="day__bar"
          :style="{ height: `${(day.changedCount / maxChanged) * 100}%` }"
        />
        <span
          class="day__block"
          :class="STATE_CLASS[day.state]"
        />
      </div>
    </div>
    <p class="pn-hint">
      The bar over a day is how many assets' listing changed that day, across the source's whole asset list.
    </p>

    <div
      v-if="markers.length > 0"
      class="lane"
      aria-hidden="true"
      data-testid="history-lane"
    >
      <span
        v-for="(entry, index) in markers"
        :key="index"
        class="lane__marker"
        :class="{ 'is-pending': entry.pending }"
        :style="{ left: `${entry.position}%` }"
        :title="markerTitle(entry)"
        data-testid="history-marker"
      />
    </div>

    <ul
      v-if="markerList.length > 0"
      class="markers"
      aria-label="Settings changes and collect changes"
      data-testid="history-markers"
    >
      <li
        v-for="(entry, index) in markerList"
        :key="index"
        class="marker"
        :class="{ 'is-pending': entry.pending }"
        data-testid="history-marker-row"
      >
        <span class="marker__head">
          <strong>{{ markerLabel(entry.marker.kind) }}</strong>
          <template v-if="isSettings(entry)"> version {{ entry.marker.versionNo }}</template>
          <template v-else-if="entry.marker.label">: {{ entry.marker.label }}</template>
          <span class="marker__when">
            {{ entry.marker.at === undefined ? '' : dateTime(timestampToInstant(entry.marker.at)) }}
          </span>
        </span>
        <span
          v-if="isSettings(entry)"
          class="marker__effective"
        >{{ effectiveText(entry) }}</span>
        <span class="marker__who">By {{ entry.marker.actor }}<template v-if="entry.marker.reason">: {{ entry.marker.reason }}</template></span>
      </li>
    </ul>
    <p
      v-else
      class="pn-muted"
      data-testid="history-no-markers"
    >
      No settings changes in this window.
    </p>
  </section>
</template>

<style scoped>
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 12px;
  color: var(--tj-text2);
}

.legend__item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.legend__swatch,
.day__block {
  display: block;
  width: 100%;
  height: 14px;
  border: 1px solid transparent;
  border-radius: 2px;
}

.legend__swatch {
  width: 14px;
}

.is-saved {
  background: var(--tj-status-ok);
}

.is-missing {
  background: var(--tj-status-fail);
}

.is-empty {
  background: var(--tj-status-warn);
}

/* Not due is outlined and empty, so the four states differ by shape and fill, not by colour alone. */
.is-not-due {
  background: transparent;
  border-color: var(--tj-text3);
}

.days {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 64px;
}

.day {
  flex: 1;
  min-width: 3px;
  height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  gap: 2px;
}

.day__bar {
  display: block;
  width: 100%;
  min-height: 0;
  background: var(--tj-accent-tint);
  border-radius: 1px;
}

.lane {
  position: relative;
  height: 14px;
  border-top: 1px solid var(--tj-line);
}

.lane__marker {
  position: absolute;
  top: 2px;
  width: 0;
  height: 0;
  margin-left: -5px;
  border-left: 5px solid transparent;
  border-right: 5px solid transparent;
  border-bottom: 9px solid var(--tj-accent-tint);
}

/* A pending version is drawn dimmed: it is not a boundary yet. */
.lane__marker.is-pending {
  opacity: 0.4;
}

.markers {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.marker {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 13px;
}

.marker.is-pending {
  color: var(--tj-text2);
}

.marker__when {
  margin-left: 8px;
  font-family: var(--tj-font-mono);
  font-size: 12px;
  color: var(--tj-text3);
}

.marker__effective,
.marker__who {
  font-size: 12px;
  color: var(--tj-text2);
}
</style>
