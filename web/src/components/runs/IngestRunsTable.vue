<script setup lang="ts">
// A dataset's collection runs as a table (tj-grna9p.34): when it started and finished, how long it
// took, what triggered it and who, the range it asked for, the bars it wrote, and its state.
//
// A failed run carries its typed error in words under the state: the reason, the domain it came
// from, and the server's message. Running is its own state with no finish and no duration yet (the
// screen does not tick a clock; a finished run arrives as an event). There is no Quota Key column:
// the server does not record it per run yet. The table holds no data of its own: the screen passes
// the rows, which are already newest first.
import type { IngestRun } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { int64ToNumber } from '@/api'
import StatusBadge from '@/components/StatusBadge.vue'
import { MISSING, formatDuration, formatNumber, timestampToInstant } from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import { titleCase } from '@/theme/copy'
import { describeRunError, runDurationMs, runStateBadge, triggerLabel } from './runRows'

defineProps<{ runs: readonly IngestRun[] }>()

const { date, dateTime } = useFormatters()

const COLUMNS = ['Started', 'Finished', 'Duration', 'Trigger', 'Actor', 'Range', 'Bars Written', 'State'].map(titleCase)

function started(run: IngestRun): string {
  return run.startedAt === undefined ? MISSING : dateTime(timestampToInstant(run.startedAt))
}

function finished(run: IngestRun): string {
  return run.finishedAt === undefined ? MISSING : dateTime(timestampToInstant(run.finishedAt))
}

function duration(run: IngestRun): string {
  const ms = runDurationMs(run)
  return ms === null ? MISSING : formatDuration(ms)
}

// The range is half-open [start, end): the end day is not included. An unset end is an open request.
function range(run: IngestRun): string {
  const from = run.start === undefined ? MISSING : date(timestampToInstant(run.start))
  if (run.end === undefined) return `${from} onward`
  return `${from} to ${date(timestampToInstant(run.end))}`
}

function bars(run: IngestRun): string {
  return formatNumber(int64ToNumber(run.barsWritten))
}
</script>

<template>
  <div class="runs">
    <table
      class="runs__table"
      data-testid="runs-table"
    >
      <thead>
        <tr>
          <th
            v-for="column in COLUMNS"
            :key="column"
            scope="col"
            :class="{ runs__num: column === 'Bars Written' }"
            :title="column === 'Range' ? 'Half-open: the end is not included.' : undefined"
          >
            {{ column }}
          </th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="run in runs"
          :key="run.id"
          :data-run-id="run.id"
          :data-state="runStateBadge(run.state).label"
        >
          <td class="runs__mono">
            {{ started(run) }}
          </td>
          <td class="runs__mono">
            {{ finished(run) }}
          </td>
          <td class="runs__mono">
            {{ duration(run) }}
          </td>
          <td>{{ triggerLabel(run.trigger) }}</td>
          <td>{{ run.actor === '' ? MISSING : run.actor }}</td>
          <td class="runs__mono">
            {{ range(run) }}
          </td>
          <td class="runs__num runs__mono">
            {{ bars(run) }}
          </td>
          <td>
            <StatusBadge :badge="runStateBadge(run.state)" />
            <template v-if="describeRunError(run.error)">
              <span
                class="runs__error"
                data-testid="run-error"
              >{{ describeRunError(run.error)?.headline }}</span>
              <span
                v-if="describeRunError(run.error)?.message"
                class="runs__error-message"
              >{{ describeRunError(run.error)?.message }}</span>
            </template>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.runs {
  overflow-x: auto;
}

.runs__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.runs__table th {
  padding: 6px 12px 6px 0;
  text-align: left;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--tj-text3);
  border-bottom: 1px solid var(--tj-line);
  white-space: nowrap;
}

.runs__table td {
  padding: 8px 12px 8px 0;
  vertical-align: top;
  border-bottom: 1px solid var(--tj-line);
  white-space: nowrap;
}

.runs__mono {
  font-family: var(--tj-font-mono);
  font-size: 12px;
}

.runs__num {
  text-align: right;
}

.runs__error {
  display: block;
  margin-top: 2px;
  font-size: 12px;
  color: var(--tj-status-fail);
}

.runs__error-message {
  display: block;
  max-width: 360px;
  white-space: normal;
  font-size: 12px;
  color: var(--tj-text2);
}
</style>
