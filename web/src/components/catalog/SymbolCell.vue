<script setup lang="ts">
// The grid's Symbol cell: a real link to the Viewer, so the row is reachable by keyboard and opens in
// a new tab with a modified click. A plain click is handled by the row (DatasetGrid.vue), which
// navigates within the app; the link's own navigation is cancelled so the page does not reload.
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { ICellRendererParams } from 'ag-grid-community'

const props = defineProps<{
  params: ICellRendererParams<DatasetSummary> & { href: (id: string) => string }
}>()

function onClick(event: MouseEvent): void {
  if (!event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) event.preventDefault()
}
</script>

<template>
  <a
    v-if="props.params.data"
    class="symbol-link"
    :href="props.params.href(props.params.data.id)"
    @click="onClick"
  >{{ props.params.data.assetSymbol }}</a>
</template>

<style scoped>
.symbol-link {
  color: var(--tj-accent-tint);
  text-decoration: none;
  font-weight: 500;
}
</style>
