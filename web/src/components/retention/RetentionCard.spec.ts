import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { create } from '@bufbuild/protobuf'

import { Granularity } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { DatasetSummarySchema } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { useDatasetDetailStore } from '@/stores/datasetDetail'
import type { WriteOutcome } from '@/stores/datasetDetail'

import RetentionCard from './RetentionCard.vue'

// tj-grna9p.89: the card alone, with the store's two writes replaced, for the one outcome the page
// cannot show: 'stale' (the page left the dataset while the write was in flight, so the card that
// sent it is usually gone). The card ignores it: no notice, no navigation event, nothing reset.
// The page-level behaviour is pinned in views/data/DatasetDetail.spec.ts.
const DATASET = create(DatasetSummarySchema, {
  id: 'ds-1',
  assetSymbol: 'VFV',
  granularity: Granularity.ONE_DAY,
  owner: 'operator',
  barCount: 10n,
})

beforeEach(() => {
  setActivePinia(createPinia())
})

function mountWith(outcome: WriteOutcome) {
  const store = useDatasetDetailStore()
  const setExpiry = vi.fn().mockResolvedValue(outcome)
  const deleteNow = vi.fn().mockResolvedValue(outcome)
  store.setExpiry = setExpiry
  store.deleteNow = deleteNow
  const card = mount(RetentionCard, { props: { dataset: DATASET } })
  return { card, setExpiry, deleteNow }
}

describe('RetentionCard and the stale outcome', () => {
  it('a stale Set Expiry answer: no notice, the field is kept, no event', async () => {
    const { card, setExpiry } = mountWith({ kind: 'stale' })
    await card.find('[data-testid="expiry-input"]').setValue('2027/01/01')
    await card.find('[data-testid="expiry-apply"]').trigger('click')
    await flushPromises()
    expect(setExpiry).toHaveBeenCalledTimes(1)
    expect(card.find('[data-testid="retention-notice"]').exists()).toBe(false)
    expect((card.find('[data-testid="expiry-input"]').element as HTMLInputElement).value).toBe('2027/01/01')
    expect(card.emitted()).not.toHaveProperty('deleted')
    expect(card.emitted()).not.toHaveProperty('gone')
  })

  it('a stale Delete Now answer: no deleted event (no navigation) and no notice', async () => {
    const { card, deleteNow } = mountWith({ kind: 'stale' })
    await card.find('[data-testid="delete-open"]').trigger('click')
    await card.find('[data-testid="delete-typed"]').setValue('VFV 1d')
    await card.find('[data-testid="delete-submit"]').trigger('click')
    await flushPromises()
    expect(deleteNow).toHaveBeenCalledTimes(1)
    expect(card.emitted()).not.toHaveProperty('deleted')
    expect(card.find('[data-testid="retention-notice"]').exists()).toBe(false)
  })

  it('by contrast, an ok Delete Now emits deleted (the screen leaves)', async () => {
    const { card } = mountWith({ kind: 'ok' })
    await card.find('[data-testid="delete-open"]').trigger('click')
    await card.find('[data-testid="delete-typed"]').setValue('VFV 1d')
    await card.find('[data-testid="delete-submit"]').trigger('click')
    await flushPromises()
    expect(card.emitted('deleted')).toHaveLength(1)
  })
})
