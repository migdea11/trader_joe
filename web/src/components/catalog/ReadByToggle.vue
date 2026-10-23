<script setup lang="ts">
// The Read By choice (SDK, UI) as a row of checkboxes, for a section header (tj-grna9p.89). The same
// constants and rules as the Datasets sidebar's Read By group (tj-grna9p.88): the options are
// CLIENT_KIND_OPTIONS, an unset or empty choice reads as DEFAULT_CLIENT_KINDS (effectiveClientKinds),
// and at least one kind stays ticked, so the last one cannot be cleared.
import { computed } from 'vue'
import { ClientKind } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { CLIENT_KIND_OPTIONS, effectiveClientKinds } from '@/catalog/filters'
import { clientKindLabel } from '@/catalog/usage'
import { titleCase } from '@/theme/copy'

const props = defineProps<{ modelValue: readonly ClientKind[] }>()
const emit = defineEmits<{ 'update:modelValue': [kinds: ClientKind[]] }>()

const chosen = computed(() => effectiveClientKinds({ clientKinds: [...props.modelValue] }))

function toggle(kind: ClientKind): void {
  const current = chosen.value
  const next = current.includes(kind) ? current.filter((entry) => entry !== kind) : [...current, kind]
  if (next.length === 0) return
  emit('update:modelValue', next)
}
</script>

<template>
  <div
    class="read-by"
    role="group"
    :aria-label="titleCase('Read By')"
    data-testid="read-by"
  >
    <span class="read-by__title">{{ titleCase('Read By') }}</span>
    <label
      v-for="kind in CLIENT_KIND_OPTIONS"
      :key="kind"
      class="read-by__option"
      :data-read-by="ClientKind[kind]"
    >
      <input
        type="checkbox"
        :checked="chosen.includes(kind)"
        :disabled="chosen.length === 1 && chosen.includes(kind)"
        @change="toggle(kind)"
      >
      {{ clientKindLabel(kind) }}
    </label>
  </div>
</template>

<style scoped>
.read-by {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
}

.read-by__title {
  font-size: 12px;
  color: var(--tj-text2);
}

.read-by__option {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--tj-text);
}

.read-by__option input {
  margin: 0;
}
</style>
