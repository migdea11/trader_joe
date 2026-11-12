<script setup lang="ts">
// The Panels page (tj-7zjov1.37, ADR tj-26jt9w addendum 20; the design canvas Main board): a panel is a
// named selection of one source's assets whose datasets the panel manager keeps. A selector picks the
// panel; the page shows its status (membership counts, with First Pass Pending until a pass has run), the
// settings panel (edit, enable or disable, each with an optional reason), the source settings, the
// aggregate asset-list history strip with its settings-change markers, and the member list with each
// member's corporate-action events. New Panel is a UI action (the reason is optional).
//
// The server decides every count, state and health; the page words them. No polling: a load happens on
// mount, on a selection change, after a write, on Refresh and once per reconnect of the events socket.
// There is no panel event type, so a write is followed by a REST re-read.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { Plus, RefreshCw } from '@lucide/vue'

import { int64ToNumber } from '@/api'
import { sourceLabel } from '@/catalog/labels'
import { panelIntervalLabel } from '@/components/panels/panelModel'
import PanelCreateForm from '@/components/panels/PanelCreateForm.vue'
import PanelHistoryStrip from '@/components/panels/PanelHistoryStrip.vue'
import PanelMemberList from '@/components/panels/PanelMemberList.vue'
import PanelSettingsForm from '@/components/panels/PanelSettingsForm.vue'
import PanelSourceSettingsForm from '@/components/panels/PanelSourceSettingsForm.vue'
import '@/components/panels/panels.css'
import { formatNumber } from '@/format/formatters'
import { useEventsReload } from '@/shell/useEventsReload'
import { useFooterSummary } from '@/shell/useFooterSummary'
import { usePanelsStore } from '@/stores/panels'
import { titleCase } from '@/theme/copy'

const store = usePanelsStore()
const {
  panels,
  listStatus,
  listError,
  selectedId,
  detail,
  detailStatus,
  panelVersions,
  sourceVersions,
  history,
  boundaries,
  members,
  hasMoreMembers,
  loadingMoreMembers,
  errors,
  loadedTick,
} = storeToRefs(store)
const { setFooterUpdated, setFooterProblem } = useFooterSummary()

const creating = ref(false)

onMounted(() => void store.load())
useEventsReload(() => store.load())

const firstPassPending = computed(
  () => detail.value !== null && detail.value.currentVersion?.effectivePass === undefined,
)

const tiles = computed(() => {
  const counts = detail.value?.counts
  const count = (value: bigint | undefined): string => (value === undefined ? '—' : formatNumber(int64ToNumber(value)))
  return [
    { key: 'members', label: titleCase('Members'), value: count(counts?.members) },
    { key: 'collecting', label: titleCase('Collecting'), value: count(counts?.collecting) },
    { key: 'draining', label: titleCase('Draining'), value: count(counts?.draining) },
    { key: 'lagging', label: titleCase('Lagging'), value: count(counts?.lagging) },
  ]
})

function onSelect(event: Event): void {
  void store.select((event.target as HTMLSelectElement).value)
}

// --- the footer ----------------------------------------------------------------------------------

watch(loadedTick, () => setFooterUpdated(Date.now()), { immediate: true })

watch(
  [errors, listError],
  () => {
    const failed = listError.value ?? Object.values(errors.value)[0]
    if (failed === undefined) return setFooterProblem(null)
    const id = failed.errorId === undefined ? '' : ` (error id ${failed.errorId})`
    setFooterProblem(`Could not load panels: ${failed.message}${id}`)
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  setFooterUpdated(null)
  setFooterProblem(null)
})
</script>

<template>
  <section class="panels">
    <div class="panels__head">
      <h1 class="panels__title">
        Panels
      </h1>
      <div class="panels__selector">
        <label
          class="pn-label"
          for="panel-select"
        >{{ titleCase('Panel') }}</label>
        <select
          id="panel-select"
          class="pn-input"
          :value="selectedId ?? ''"
          :disabled="panels.length === 0"
          data-testid="panel-select"
          @change="onSelect"
        >
          <option
            v-if="panels.length === 0"
            value=""
          >
            No panels
          </option>
          <option
            v-for="item in panels"
            :key="item.id"
            :value="item.id"
          >
            {{ item.name }} · {{ sourceLabel(item.source) }} · {{ panelIntervalLabel(item.granularity) }}{{ item.enabled ? '' : ' (disabled)' }}
          </option>
        </select>
      </div>
      <div class="panels__tools">
        <button
          type="button"
          class="pn-btn"
          :disabled="creating"
          data-testid="panel-new"
          @click="creating = true"
        >
          <Plus
            :size="16"
            aria-hidden="true"
          />
          {{ titleCase('New Panel') }}
        </button>
        <button
          type="button"
          class="pn-btn"
          data-testid="panels-refresh"
          @click="store.load()"
        >
          <RefreshCw
            :size="16"
            aria-hidden="true"
          />
          Refresh
        </button>
      </div>
    </div>

    <PanelCreateForm
      v-if="creating"
      @close="creating = false"
    />

    <p
      v-if="listError"
      class="pn-notice pn-notice--error"
      role="alert"
      data-testid="panels-error"
    >
      Could not load the panels. {{ listError.message }}
      <span
        v-if="listError.errorId"
        class="pn-error-id"
      >Error ID: {{ listError.errorId }}</span>
    </p>
    <p
      v-else-if="listStatus === 'loading'"
      class="pn-muted"
      role="status"
    >
      Loading panels
    </p>
    <p
      v-else-if="listStatus === 'ready' && panels.length === 0"
      class="pn-muted"
      data-testid="panels-empty"
    >
      There are no panels yet. Choose New Panel to create one.
    </p>

    <template v-if="detail">
      <h2 class="panels__name">
        {{ detail.name }}
        <span class="panels__source">{{ sourceLabel(detail.source) }}</span>
        <span
          class="panels__source"
          data-testid="panel-interval"
        >{{ titleCase('Bar Interval') }}: {{ panelIntervalLabel(detail.granularity) }}</span>
      </h2>

      <p
        v-if="firstPassPending"
        class="pn-notice"
        role="status"
        data-testid="first-pass-pending"
      >
        First pass pending. The panel manager runs a panel's first pass within a few minutes; the members and the
        history appear once it has.
      </p>

      <div
        class="tiles"
        data-testid="panel-tiles"
      >
        <div
          v-for="tile in tiles"
          :key="tile.key"
          class="tile"
          :data-tile="tile.key"
        >
          <span class="tile__label">{{ tile.label }}</span>
          <span class="tile__value">{{ tile.value }}</span>
        </div>
      </div>

      <div class="cards">
        <PanelSettingsForm :detail="detail" />
        <PanelSourceSettingsForm :detail="detail" />
      </div>
      <p
        v-if="errors.versions"
        class="pn-notice pn-notice--error"
        role="alert"
        data-testid="versions-error"
      >
        Could not load the settings versions, so the markers show no effective pass. {{ errors.versions.message }}
      </p>

      <p
        v-if="errors.history || errors.markers"
        class="pn-notice pn-notice--error"
        role="alert"
        data-testid="history-error"
      >
        Could not load the asset list history. {{ (errors.history ?? errors.markers)?.message }}
      </p>
      <PanelHistoryStrip
        v-if="history"
        :history="history"
        :boundaries="boundaries"
        :panel-versions="panelVersions"
        :source-versions="sourceVersions"
      />

      <section
        class="pn-card"
        aria-labelledby="panel-members-title"
        data-testid="panel-members"
      >
        <h2
          id="panel-members-title"
          class="pn-card__title"
        >
          {{ titleCase('Members') }}
        </h2>
        <p
          v-if="errors.members"
          class="pn-notice pn-notice--error"
          role="alert"
          data-testid="members-error"
        >
          Could not load the members. {{ errors.members.message }}
        </p>
        <p
          v-else-if="members.length === 0"
          class="pn-muted"
          data-testid="members-empty"
        >
          {{ firstPassPending ? 'No members yet: the first pass has not run.' : 'This panel has no members.' }}
        </p>
        <PanelMemberList
          v-else
          :members="members"
          :has-more="hasMoreMembers"
          :loading-more="loadingMoreMembers"
          @more="store.loadMoreMembers()"
        />
      </section>
    </template>
    <p
      v-else-if="detailStatus === 'loading'"
      class="pn-muted"
      role="status"
    >
      Loading the panel
    </p>
    <p
      v-else-if="detailStatus === 'not-found' || detailStatus === 'error'"
      class="pn-notice pn-notice--error"
      role="alert"
      data-testid="detail-error"
    >
      Could not load the panel. {{ errors.detail?.message }}
      <span
        v-if="errors.detail?.errorId"
        class="pn-error-id"
      >Error ID: {{ errors.detail.errorId }}</span>
    </p>
  </section>
</template>

<style scoped>
.panels {
  min-height: 100%;
  display: flex;
  flex-direction: column;
  gap: var(--tj-page-gap);
}

.panels__head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 12px;
}

.panels__title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
  align-self: center;
}

.panels__selector {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 240px;
}

.panels__tools {
  margin-left: auto;
  display: flex;
  gap: 8px;
}

.panels__name {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}

.panels__source {
  margin-left: 8px;
  font-size: 12px;
  font-weight: 400;
  color: var(--tj-text2);
}

.tiles {
  display: flex;
  flex-wrap: wrap;
  gap: var(--tj-tile-gap);
}

.tile {
  flex: 1;
  min-width: 140px;
  padding: var(--tj-card-padding);
  background: var(--tj-surface);
  border: 1px solid var(--tj-line);
  border-radius: var(--tj-radius-card);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.tile__label {
  font-size: 12px;
  color: var(--tj-text2);
}

.tile__value {
  font-family: var(--tj-font-mono);
  font-size: 22px;
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: var(--tj-page-gap);
  align-items: start;
}
</style>
