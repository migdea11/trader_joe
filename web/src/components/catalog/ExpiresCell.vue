<script setup lang="ts">
// The grid's Expires cell (tj-grna9p.88): the expiry date, or a dash when nothing is scheduled, and
// a "Would be deleted" flag where the reaper's pending set holds the dataset (the server's
// reaper_pending; true in dry-run too, where nothing is deleted yet).
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { ICellRendererParams } from 'ag-grid-community'

import { MISSING, timestampToInstant } from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import { colors } from '@/theme/tokens'

defineProps<{ params: ICellRendererParams<DatasetSummary> }>()

const { date } = useFormatters()
</script>

<template>
  <span
    v-if="params.data"
    class="expires-cell"
  >
    {{ params.data.expiry === undefined ? MISSING : date(timestampToInstant(params.data.expiry)) }}
    <span
      v-if="params.data.reaperPending"
      class="expires-cell__flag"
      :style="{ color: colors.status.warn }"
      data-testid="would-be-deleted"
    >Would be deleted</span>
  </span>
</template>

<style scoped>
.expires-cell {
  white-space: nowrap;
}

.expires-cell__flag {
  margin-left: 8px;
  font-size: 12px;
}
</style>
