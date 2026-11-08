<script setup lang="ts">
// The panel settings fields the create form and the settings form share: enabled, cadence, the
// allow and deny symbols and the four optional filters. It edits a PanelDraft in place (v-model) and
// decides nothing: validation words come from panelModel.ts and the server has the last say.
import { AssetType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { PanelCadence } from '@generated/trader_joe/proto/ui/v1/panels_pb'

import { titleCase } from '@/theme/copy'

import { CADENCE_CHOICES, DEFAULT_CADENCE_LABEL } from './panelModel'
import type { PanelDraft } from './panelModel'

const draft = defineModel<PanelDraft>({ required: true })
defineProps<{
  /** Id prefix, so two forms on a page keep their labels apart. */
  idPrefix: string
  disabled: boolean
  /** The create form may leave the cadence unnamed (the store defaults it); a save must name one. */
  cadenceOptional: boolean
}>()

const ASSET_CLASS_CHOICES = [
  { value: AssetType.STOCK, label: 'Stock' },
  { value: AssetType.CRYPTO, label: 'Crypto' },
  { value: AssetType.OPTION, label: 'Option' },
]

const TRI_CHOICES = [
  { value: '', label: 'Any' },
  { value: 'true', label: 'Yes' },
  { value: 'false', label: 'No' },
]
</script>

<template>
  <div class="pn-grid">
    <div class="pn-field">
      <label
        class="pn-label"
        :for="`${idPrefix}-cadence`"
      >{{ titleCase('Cadence') }}</label>
      <select
        :id="`${idPrefix}-cadence`"
        v-model="draft.cadence"
        class="pn-input"
        :disabled="disabled"
        :data-testid="`${idPrefix}-cadence`"
      >
        <option
          v-if="cadenceOptional"
          :value="PanelCadence.UNSPECIFIED"
        >
          {{ DEFAULT_CADENCE_LABEL }}
        </option>
        <option
          v-else
          :value="PanelCadence.UNSPECIFIED"
          disabled
        >
          Choose a cadence
        </option>
        <option
          v-for="choice in CADENCE_CHOICES"
          :key="choice.value"
          :value="choice.value"
        >
          {{ choice.label }}
        </option>
      </select>
    </div>
    <div class="pn-field">
      <label
        class="pn-label"
        :for="`${idPrefix}-asset-class`"
      >{{ titleCase('Asset Class') }}</label>
      <select
        :id="`${idPrefix}-asset-class`"
        v-model="draft.assetClass"
        class="pn-input"
        :disabled="disabled"
        :data-testid="`${idPrefix}-asset-class`"
      >
        <option :value="AssetType.UNSPECIFIED">
          Any
        </option>
        <option
          v-for="choice in ASSET_CLASS_CHOICES"
          :key="choice.value"
          :value="choice.value"
        >
          {{ choice.label }}
        </option>
      </select>
    </div>
    <div class="pn-field">
      <label
        class="pn-label"
        :for="`${idPrefix}-exchange`"
      >{{ titleCase('Exchange') }}</label>
      <input
        :id="`${idPrefix}-exchange`"
        v-model="draft.exchange"
        class="pn-input"
        type="text"
        placeholder="Any"
        :disabled="disabled"
        :data-testid="`${idPrefix}-exchange`"
      >
    </div>
    <div class="pn-field">
      <label
        class="pn-label"
        :for="`${idPrefix}-active`"
      >{{ titleCase('Active') }}</label>
      <select
        :id="`${idPrefix}-active`"
        v-model="draft.active"
        class="pn-input"
        :disabled="disabled"
        :data-testid="`${idPrefix}-active`"
      >
        <option
          v-for="choice in TRI_CHOICES"
          :key="choice.value"
          :value="choice.value"
        >
          {{ choice.label }}
        </option>
      </select>
    </div>
    <div class="pn-field">
      <label
        class="pn-label"
        :for="`${idPrefix}-tradable`"
      >{{ titleCase('Tradable') }}</label>
      <select
        :id="`${idPrefix}-tradable`"
        v-model="draft.tradable"
        class="pn-input"
        :disabled="disabled"
        :data-testid="`${idPrefix}-tradable`"
      >
        <option
          v-for="choice in TRI_CHOICES"
          :key="choice.value"
          :value="choice.value"
        >
          {{ choice.label }}
        </option>
      </select>
    </div>
  </div>
  <div class="pn-grid">
    <div class="pn-field">
      <label
        class="pn-label"
        :for="`${idPrefix}-allow`"
      >{{ titleCase('Allow Symbols') }}</label>
      <input
        :id="`${idPrefix}-allow`"
        v-model="draft.allowSymbols"
        class="pn-input"
        type="text"
        placeholder="Empty restricts nothing"
        :disabled="disabled"
        :data-testid="`${idPrefix}-allow`"
      >
    </div>
    <div class="pn-field">
      <label
        class="pn-label"
        :for="`${idPrefix}-deny`"
      >{{ titleCase('Deny Symbols') }}</label>
      <input
        :id="`${idPrefix}-deny`"
        v-model="draft.denySymbols"
        class="pn-input"
        type="text"
        placeholder="Removed from the selection"
        :disabled="disabled"
        :data-testid="`${idPrefix}-deny`"
      >
    </div>
  </div>
  <p class="pn-hint">
    Separate symbols with commas or spaces. A filter set to Any is not applied.
  </p>
</template>
