<script setup lang="ts">
// The grid's Symbol cell: a real link to the Viewer (the data is the default destination), so the row
// is reachable by keyboard, screen readers, open-in-new-tab and right-click. It is styled as plain
// text, not as a link: the whole row is the click target (DatasetGrid.vue opens the Viewer), and an
// accent-coloured symbol would suggest only the symbol is. A plain click on it is cancelled here for
// the row to handle; a modified click is the browser's. The details icon lives in the Coverage cell.
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { ICellRendererParams } from 'ag-grid-community'

const props = defineProps<{
  params: ICellRendererParams<DatasetSummary> & {
    /** The Viewer's URL for a dataset id. */
    href: (id: string) => string
  }
}>()

function onSymbolClick(event: MouseEvent): void {
  const modified = event.metaKey || event.ctrlKey || event.shiftKey || event.altKey
  if (!modified) event.preventDefault()
}
</script>

<template>
  <a
    v-if="props.params.data"
    class="symbol-link"
    :href="props.params.href(props.params.data.id)"
    @click="onSymbolClick"
  >{{ props.params.data.assetSymbol }}</a>
</template>

<style scoped>
/* Normal text at rest; the underline shows only on hover and keyboard focus (the global
   :focus-visible ring in theme/base.css stays). */
.symbol-link {
  color: var(--tj-text);
  font-weight: 600;
  text-decoration: none;
}

.symbol-link:hover,
.symbol-link:focus-visible {
  text-decoration: underline;
}
</style>
