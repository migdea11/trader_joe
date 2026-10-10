<script setup lang="ts">
// The grid's Status cell: the badge for the row's freshness, Unknown where it has none. A row still
// loading has no data and renders nothing.
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { ICellRendererParams } from 'ag-grid-community'

import StatusBadge from '@/components/StatusBadge.vue'
import { rowBadge } from '@/catalog/rows'

defineProps<{ params: ICellRendererParams<DatasetSummary> }>()
</script>

<template>
  <StatusBadge
    v-if="params.data"
    :badge="rowBadge(params.data)"
  />
</template>
