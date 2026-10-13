<script setup lang="ts">
// The grid's Status cell: Running while the dataset's latest run is in flight, otherwise the badge
// for its freshness, Unknown where it has none. A row still loading has no data and renders nothing.
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
