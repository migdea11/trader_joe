import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { create } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import { Granularity, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { DatasetSummarySchema } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { useDatasetDetailStore } from '@/stores/datasetDetail'
import type { WriteOutcome } from '@/stores/datasetDetail'

import RetentionCard from './RetentionCard.vue'

// A pass-through spy on the one int64 conversion (src/api/int64.ts throws past 2^53 rather than
// rounding; pinned in api.spec.ts), so the delete text's bar count can be seen to go through it.
const int64 = vi.hoisted(() => ({ int64ToNumber: vi.fn() }))
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...int64,
}))

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

beforeEach(async () => {
  setActivePinia(createPinia())
  const real = await vi.importActual<typeof import('@/api/int64')>('@/api/int64')
  int64.int64ToNumber.mockReset()
  int64.int64ToNumber.mockImplementation(real.int64ToNumber)
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

  it('the delete text bar count goes through the one int64 conversion, never Number()', async () => {
    const card = mount(RetentionCard, { props: { dataset: create(DatasetSummarySchema, { ...DATASET, barCount: 7_654_321n }) } })
    await card.find('[data-testid="delete-open"]').trigger('click')
    expect(int64.int64ToNumber).toHaveBeenCalledWith(7_654_321n)
    expect(card.find('[data-testid="delete-confirm"]').text()).toContain('This deletes VFV 1d and its 7,654,321 bars at once.')
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

// tj-grna9p.142: Never is the explicit no-expiry (the server stores null). The card says Never in the
// fact, the placeholder and the button; the button still only empties the field, and the write it
// leads to omits the expiry (store.setExpiry(undefined, ...)), never a far-future date.
describe('RetentionCard and Never', () => {
  const STORED = timestampFromMs(Date.UTC(2027, 0, 1, 5))

  function mountCard(extra: Partial<typeof DATASET>) {
    const store = useDatasetDetailStore()
    const setExpiry = vi.fn().mockResolvedValue({ kind: 'ok' })
    store.setExpiry = setExpiry
    const card = mount(RetentionCard, { props: { dataset: create(DatasetSummarySchema, { ...DATASET, ...extra }) } })
    return { card, setExpiry }
  }
  const apply = (card: ReturnType<typeof mountCard>['card']) => card.find('[data-testid="expiry-apply"]')

  it('no stored expiry: the fact, the placeholder and the button all read Never', () => {
    const { card } = mountCard({ updateType: UpdateType.STATIC })
    expect(card.find('[data-fact="expiry"]').text()).toBe('Never')
    expect(card.find('[data-testid="expiry-input"]').attributes('placeholder')).toBe('Never')
    expect(card.find('[data-testid="expiry-clear"]').text()).toBe('Never')
    expect(card.text()).not.toContain('Keep Forever')
    expect(card.text()).not.toContain('None')
  })

  it('a subscription with no stored expiry: the fact reads Never too', () => {
    const { card } = mountCard({ updateType: UpdateType.DAILY })
    expect(card.find('[data-fact="expiry"]').text()).toBe('Never')
  })

  it('STATIC with a stored expiry: Never empties the field, Set to Never sends no expiry', async () => {
    const { card, setExpiry } = mountCard({ updateType: UpdateType.STATIC, expiry: STORED })
    expect(card.find('[data-fact="expiry"]').text()).not.toBe('Never')
    await card.find('[data-testid="expiry-input"]').setValue('2027/05/05')
    await card.find('[data-testid="expiry-clear"]').trigger('click')
    expect((card.find('[data-testid="expiry-input"]').element as HTMLInputElement).value).toBe('')
    expect(apply(card).text()).toBe('Set to Never')
    await apply(card).trigger('click')
    await flushPromises()
    expect(setExpiry).toHaveBeenCalledTimes(1)
    expect(setExpiry).toHaveBeenCalledWith(undefined, undefined)
  })

  it('DAILY with a stored expiry: Never leads to Resume Subscription, which also sends no expiry', async () => {
    const { card, setExpiry } = mountCard({ updateType: UpdateType.DAILY, expiry: STORED })
    await card.find('[data-testid="expiry-input"]').setValue('2027/05/05')
    await card.find('[data-testid="expiry-clear"]').trigger('click')
    expect(apply(card).text()).toBe('Resume Subscription')
    await apply(card).trigger('click')
    await flushPromises()
    expect(setExpiry).toHaveBeenCalledWith(undefined, undefined)
  })

  // tj-grna9p.144: the retention hint names the Never control, not "clearing the expiry".
  // Runs of whitespace are one space: the v-if branch leaves a second one where it joins the first sentence.
  // The hint is the Expiry Time field's own (not the expiry type's, not the invalid-day error).
  const retentionHint = (card: ReturnType<typeof mountCard>['card']) =>
    (card.find('#retention-expiry').element.closest('.field')!.querySelector('p.hint:not(.hint--error)')!.textContent ?? '')
      .replace(/\s+/g, ' ')
      .trim()

  it('STATIC: the hint says the dataset is deleted on that date and Never keeps it', () => {
    const { card } = mountCard({ updateType: UpdateType.STATIC })
    const hint = retentionHint(card)
    expect(hint).toMatch(/^The day starts at midnight in \S+\. The dataset is deleted on that date\. Choosing Never keeps it\.$/)
  })

  it.each([
    ['DAILY', UpdateType.DAILY],
    ['STREAM', UpdateType.STREAM],
  ])('%s: the hint says an expiry retires the subscription at once and Never before then resumes it', (_name, updateType) => {
    const { card } = mountCard({ updateType })
    expect(retentionHint(card)).toMatch(
      /^The day starts at midnight in \S+\. Setting an expiry retires this subscription at once: it stops updating, stays readable, and is deleted on that date\. Choosing Never before then resumes it\.$/,
    )
  })

  it('the invalid-day hint names Never', async () => {
    const { card } = mountCard({ updateType: UpdateType.STATIC })
    await card.find('[data-testid="expiry-input"]').setValue('2026/02/30')
    expect(card.find('[data-testid="expiry-problem"]').text()).toBe(
      'Type the day as YYYY/MM/DD, or choose Never to keep the dataset forever.',
    )
  })
})
