<script setup lang="ts">
// The grid's Usage cell (tj-grna9p.88): the age of the last read by the selected clients ("3 d ago",
// whole UTC days) or "Never read". "UI only" is added where the operator's own reads are all there
// is under an SDK filter. It never says Stale: that is the server's call, shown by the Stale view.
// A row still loading renders nothing.
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { ICellRendererParams } from 'ag-grid-community'

import { isUiOnly, usageCell } from '@/catalog/usage'
import type { CatalogFilters } from '@/catalog/filters'
import { formatDaysAgo } from '@/format/formatters'

const props = defineProps<{
  params: ICellRendererParams<DatasetSummary> & {
    /** The filters in force, for the Read By choice. */
    filters: () => CatalogFilters
  }
}>()

function text(row: DatasetSummary): string {
  const cell = usageCell(row)
  if (cell.kind === 'never') return 'Never read'
  return cell.days === 0 ? 'today' : formatDaysAgo(cell.days, 'short')
}
</script>

<template>
  <span
    v-if="props.params.data"
    class="usage-cell"
  >
    {{ text(props.params.data) }}
    <span
      v-if="isUiOnly(props.params.data, props.params.filters())"
      class="usage-cell__note"
      data-testid="usage-ui-only"
    >UI only</span>
  </span>
</template>

<style scoped>
.usage-cell {
  white-space: nowrap;
}

.usage-cell__note {
  margin-left: 6px;
  font-size: 12px;
  color: var(--tj-text3);
}
</style>
