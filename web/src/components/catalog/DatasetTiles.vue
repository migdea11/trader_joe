<script setup lang="ts">
// The three summary tiles (tj-grna9p.32): Datasets with the daily and bulk split, Failed, Late. They
// count the whole catalog and do not follow the sidebar filters. Quota, Stale and Last Hour come in
// later phases. Words and colours are the canvas's (Failed in the fail colour, Late in warn).
import { computed } from 'vue'
import { storeToRefs } from 'pinia'

import { useDatasetCatalogStore } from '@/stores/datasetCatalog'
import { colors } from '@/theme/tokens'
import { UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { formatNumber } from '@/format/formatters'

const { tiles, tilesError } = storeToRefs(useDatasetCatalogStore())

const cards = computed(() => {
  const counts = tiles.value
  const value = (n: number | undefined): string => (n === undefined ? '—' : formatNumber(n))
  return [
    {
      key: 'datasets',
      label: 'Datasets',
      value: value(counts?.all),
      color: colors.text,
      note:
        counts === null
          ? ''
          : `${formatNumber(counts.updateTypes[UpdateType.DAILY])} daily · ${formatNumber(counts.updateTypes[UpdateType.STATIC])} bulk`,
    },
    {
      key: 'failed',
      label: 'Failed',
      value: value(counts?.status.failed),
      color: colors.status.fail,
      note: 'overdue or with gaps',
    },
    {
      key: 'late',
      label: 'Late',
      value: value(counts?.status.late),
      color: colors.status.warn,
      note: 'past its expected update',
    },
  ]
})
</script>

<template>
  <div class="tiles">
    <div
      v-for="card in cards"
      :key="card.key"
      class="tile"
      :data-tile="card.key"
    >
      <span class="tile__label">{{ card.label }}</span>
      <span
        class="tile__value"
        :style="{ color: card.color }"
      >{{ card.value }}</span>
      <span class="tile__note">{{ tilesError ? 'Could not load counts' : card.note }}</span>
    </div>
  </div>
</template>

<style scoped>
.tiles {
  display: flex;
  gap: var(--tj-tile-gap);
}

.tile {
  flex: 1;
  min-width: 0;
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

.tile__note {
  font-size: 12px;
  color: var(--tj-text3);
}
</style>
