import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { create } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import { Granularity, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { DatasetState, DatasetSummarySchema } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { useDatasetDetailStore } from '@/stores/datasetDetail'
import type { WriteOutcome } from '@/stores/datasetDetail'
import { useSettingsStore } from '@/stores/settings'

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
  window.localStorage.clear()
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
    const setCollecting = vi.fn().mockResolvedValue({ kind: 'ok' })
    store.setExpiry = setExpiry
    store.setCollecting = setCollecting
    const card = mount(RetentionCard, { props: { dataset: create(DatasetSummarySchema, { ...DATASET, ...extra }) } })
    return { card, setExpiry, setCollecting }
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

  // tj-grna9p.177 (decision tj-hhy3dk): the apply is Set Expiry / Set to Never for every update type.
  it.each([
    ['DAILY', UpdateType.DAILY],
    ['STREAM', UpdateType.STREAM],
  ])('%s with a stored expiry: Never leads to Set to Never, which sends no expiry and never the collect write', async (_name, updateType) => {
    const { card, setExpiry, setCollecting } = mountCard({ updateType, expiry: STORED })
    await card.find('[data-testid="expiry-input"]').setValue('2027/05/05')
    expect(apply(card).text()).toBe('Set Expiry')
    await card.find('[data-testid="expiry-clear"]').trigger('click')
    expect(apply(card).text()).toBe('Set to Never')
    await apply(card).trigger('click')
    await flushPromises()
    expect(setExpiry).toHaveBeenCalledWith(undefined, undefined)
    expect(setCollecting).not.toHaveBeenCalled()
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
  ])('%s: the hint says the dataset is deleted on that date, Never keeps it, and an expiry does not stop collection', (_name, updateType) => {
    // tj-grna9p.177 (decision tj-hhy3dk): expiry deletes and never retires; collection is its own switch.
    const { card } = mountCard({ updateType })
    expect(retentionHint(card)).toMatch(
      /^The day starts at midnight in \S+\. The dataset is deleted on that date\. Choosing Never keeps it\. An expiry does not stop collection\.$/,
    )
    expect(retentionHint(card)).not.toMatch(/retire|resume/i)
  })

  // tj-grna9p.177: the hint pinned exactly, raw (no whitespace collapsing: the .144 double-space nit), in a
  // fixed zone, through its data-testid.
  it.each([
    ['STATIC', UpdateType.STATIC, 'The day starts at midnight in UTC. The dataset is deleted on that date. Choosing Never keeps it.'],
    [
      'DAILY',
      UpdateType.DAILY,
      'The day starts at midnight in UTC. The dataset is deleted on that date. Choosing Never keeps it. An expiry does not stop collection.',
    ],
    [
      'STREAM',
      UpdateType.STREAM,
      'The day starts at midnight in UTC. The dataset is deleted on that date. Choosing Never keeps it. An expiry does not stop collection.',
    ],
  ])('%s: the expiry-hint text is exactly the rendered sentence', (_name, updateType, text) => {
    useSettingsStore().setTimeZoneOverride('UTC')
    const { card } = mountCard({ updateType })
    const hint = card.find('[data-testid="expiry-hint"]')
    expect(hint.exists()).toBe(true)
    expect((hint.element.textContent ?? '').trim()).toBe(text)
    // The testid is the Deleted On field's own hint, the one retentionHint reads.
    expect(retentionHint(card)).toBe(text)
  })

  it('the invalid-day hint names Never', async () => {
    const { card } = mountCard({ updateType: UpdateType.STATIC })
    await card.find('[data-testid="expiry-input"]').setValue('2026/02/30')
    expect(card.find('[data-testid="expiry-problem"]').text()).toBe(
      'Type the day as YYYY/MM/DD, or choose Never to keep the dataset forever.',
    )
  })
})

// tj-grna9p.177 (decision tj-hhy3dk, UI section): a subscription (DAILY, STREAM) has a Start/Stop
// Collecting control and a Collecting fact; a bulk (STATIC) dataset has no control, and its fact shows only
// while a fetch is running (collecting true). The expiry is the deletion date only, labelled Deleted On.
describe('RetentionCard and collecting', () => {
  const STORED = timestampFromMs(Date.UTC(2027, 0, 1, 5))

  function mountCard(extra: Partial<typeof DATASET>, outcome: WriteOutcome = { kind: 'ok' }) {
    const store = useDatasetDetailStore()
    const setExpiry = vi.fn().mockResolvedValue({ kind: 'ok' })
    const setCollecting = vi.fn().mockResolvedValue(outcome)
    const refresh = vi.fn().mockResolvedValue(undefined)
    store.setExpiry = setExpiry
    store.setCollecting = setCollecting
    store.refresh = refresh
    const card = mount(RetentionCard, { props: { dataset: create(DatasetSummarySchema, { ...DATASET, ...extra }) } })
    return { card, setExpiry, setCollecting, refresh }
  }
  const toggle = (card: ReturnType<typeof mountCard>['card']) => card.find('[data-testid="collecting-toggle"]')
  const fact = (card: ReturnType<typeof mountCard>['card']) => card.find('[data-fact="collecting"]')
  const labelOf = (card: ReturnType<typeof mountCard>['card'], name: string) =>
    card.find(`[data-fact="${name}"]`).element.parentElement!.querySelector('dt')!.textContent!.trim()

  it.each([
    ['DAILY', UpdateType.DAILY],
    ['STREAM', UpdateType.STREAM],
  ])('%s collecting: the fact reads Collecting and the control Stop Collecting, which sends collect false', async (_name, updateType) => {
    const { card, setCollecting, setExpiry } = mountCard({ updateType, collecting: true })
    expect(fact(card).text()).toBe('Collecting')
    expect(labelOf(card, 'collecting')).toBe('Collecting')
    expect(toggle(card).text()).toBe('Stop Collecting')
    expect(toggle(card).attributes('disabled')).toBeUndefined()
    await toggle(card).trigger('click')
    await flushPromises()
    expect(setCollecting).toHaveBeenCalledTimes(1)
    expect(setCollecting).toHaveBeenCalledWith(false, undefined)
    expect(setExpiry).not.toHaveBeenCalled()
  })

  it.each([
    ['DAILY', UpdateType.DAILY],
    ['STREAM', UpdateType.STREAM],
  ])('%s stopped (collecting false): the fact reads Stopped and the control Start Collecting, which sends collect true', async (_name, updateType) => {
    const { card, setCollecting } = mountCard({ updateType, collecting: false })
    expect(fact(card).text()).toBe('Stopped: not collecting')
    expect(toggle(card).text()).toBe('Start Collecting')
    await toggle(card).trigger('click')
    await flushPromises()
    expect(setCollecting).toHaveBeenCalledWith(true, undefined)
  })

  it('a subscription the server marks RETIRED is stopped even with collecting unset: Start Collecting', async () => {
    const { card, setCollecting } = mountCard({ updateType: UpdateType.DAILY, state: DatasetState.RETIRED })
    expect(fact(card).text()).toBe('Stopped: not collecting')
    expect(toggle(card).text()).toBe('Start Collecting')
    await toggle(card).trigger('click')
    await flushPromises()
    expect(setCollecting).toHaveBeenCalledWith(true, undefined)
  })

  it('a subscription with collecting unset and not RETIRED reads Collecting (Stop Collecting)', () => {
    const { card } = mountCard({ updateType: UpdateType.DAILY })
    expect(fact(card).text()).toBe('Collecting')
    expect(toggle(card).text()).toBe('Stop Collecting')
  })

  it.each([
    ['collecting true', true, 'A fetch is running'],
    ['collecting false', false, null],
    ['collecting unset', undefined, null],
  ])('STATIC (%s): no collect control ever; the fact is %s', (_name, collecting, text) => {
    const { card } = mountCard({ updateType: UpdateType.STATIC, ...(collecting === undefined ? {} : { collecting }) })
    expect(toggle(card).exists()).toBe(false)
    expect(card.text()).not.toMatch(/Start Collecting|Stop Collecting/)
    if (text === null) {
      expect(fact(card).exists()).toBe(false)
    } else {
      expect(fact(card).text()).toBe(text)
    }
  })

  it('STATIC that the server marks RETIRED still has no control and no Stopped fact', () => {
    const { card } = mountCard({ updateType: UpdateType.STATIC, state: DatasetState.RETIRED, collecting: false })
    expect(toggle(card).exists()).toBe(false)
    expect(card.text()).not.toContain('Stopped')
  })

  it('a stopped subscription with an expiry shows both: Stopped, and Deleted On with the date', () => {
    useSettingsStore().setTimeZoneOverride('UTC')
    const { card } = mountCard({ updateType: UpdateType.DAILY, state: DatasetState.RETIRED, collecting: false, expiry: STORED })
    expect(fact(card).text()).toBe('Stopped: not collecting')
    expect(labelOf(card, 'expiry')).toBe('Deleted On')
    expect(card.find('[data-fact="expiry"]').text()).toBe('2027/01/01 05:00')
    // The field carries the same label; the apply is the expiry write's own words.
    expect(card.find('label[for="retention-expiry"]').text()).toBe('Deleted On')
    expect(card.find('[data-testid="expiry-apply"]').text()).toBe('Set to Never')
    expect(toggle(card).text()).toBe('Start Collecting')
  })

  it('no user-facing Retire, Resume or Retired word on any card', () => {
    for (const extra of [
      { updateType: UpdateType.DAILY, collecting: true },
      { updateType: UpdateType.STREAM, state: DatasetState.RETIRED, collecting: false, expiry: STORED },
      { updateType: UpdateType.STATIC, collecting: true, expiry: STORED },
    ]) {
      const { card } = mountCard(extra)
      expect(card.text()).not.toMatch(/retire|resume/i)
      expect(card.text()).not.toContain('Expiry Time')
      card.unmount()
    }
  })

  describe('another owner dataset: the expiry write flow', () => {
    const held = { updateType: UpdateType.DAILY, collecting: true, owner: 'bot-7' }

    it('the control stays disabled until a non-blank reason AND the confirmation, then sends the trimmed override', async () => {
      const { card, setCollecting } = mountCard(held)
      expect(toggle(card).attributes('disabled')).toBeDefined()
      await card.find('[data-testid="override-reason"]').setValue('   ')
      await card.find('[data-testid="override-confirm"]').setValue(true)
      expect(toggle(card).attributes('disabled')).toBeDefined()
      await card.find('[data-testid="override-reason"]').setValue('  owner asked  ')
      await card.find('[data-testid="override-confirm"]').setValue(false)
      expect(toggle(card).attributes('disabled')).toBeDefined()
      await toggle(card).trigger('click')
      await flushPromises()
      expect(setCollecting).not.toHaveBeenCalled()
      await card.find('[data-testid="override-confirm"]').setValue(true)
      expect(toggle(card).attributes('disabled')).toBeUndefined()
      await toggle(card).trigger('click')
      await flushPromises()
      expect(setCollecting).toHaveBeenCalledWith(false, { reason: 'owner asked', confirmed: true })
      // ok: the prompt empties, as after Set Expiry.
      expect((card.find('[data-testid="override-reason"]').element as HTMLTextAreaElement).value).toBe('')
      expect((card.find('[data-testid="override-confirm"]').element as HTMLInputElement).checked).toBe(false)
    })

    it('409 OVERRIDE_REQUIRED: the fixed notice, a refetch, and the reason is kept', async () => {
      const { card, setCollecting, refresh } = mountCard(held, { kind: 'override-required' })
      await card.find('[data-testid="override-reason"]').setValue('owner asked')
      await card.find('[data-testid="override-confirm"]').setValue(true)
      await toggle(card).trigger('click')
      await flushPromises()
      expect(setCollecting).toHaveBeenCalledTimes(1)
      expect(card.find('[data-testid="retention-notice"]').text()).toBe(
        'This dataset is held by another owner. Give a reason and confirm to continue.',
      )
      expect(refresh).toHaveBeenCalledTimes(1)
      expect((card.find('[data-testid="override-reason"]').element as HTMLTextAreaElement).value).toBe('owner asked')
    })

    it('the operator own dataset never sends an override and shows no prompt', async () => {
      const { card, setCollecting } = mountCard({ updateType: UpdateType.STREAM, collecting: false })
      expect(card.find('[data-testid="override-prompt"]').exists()).toBe(false)
      await toggle(card).trigger('click')
      await flushPromises()
      expect(setCollecting).toHaveBeenCalledWith(true, undefined)
    })
  })

  it('a 422 answer is shown as a bug with its error id; the control stays as it was', async () => {
    const { card } = mountCard({ updateType: UpdateType.DAILY, collecting: true }, {
      kind: 'invalid',
      error: { message: 'Request failed (HTTP 422)', errorId: 'e-422' },
    })
    await toggle(card).trigger('click')
    await flushPromises()
    expect(card.find('[data-testid="retention-notice"]').text()).toContain('This is a bug in the UI; nothing was changed.')
    expect(card.find('[data-testid="retention-notice"]').text()).toContain('Error ID: e-422')
    expect(toggle(card).text()).toBe('Stop Collecting')
  })

  it('a 404 answer emits gone; a stale answer does nothing', async () => {
    const gone = mountCard({ updateType: UpdateType.DAILY, collecting: true }, { kind: 'not-found' })
    await toggle(gone.card).trigger('click')
    await flushPromises()
    expect(gone.card.emitted('gone')).toHaveLength(1)
    gone.card.unmount()
    const stale = mountCard({ updateType: UpdateType.DAILY, collecting: true }, { kind: 'stale' })
    await toggle(stale.card).trigger('click')
    await flushPromises()
    expect(stale.card.find('[data-testid="retention-notice"]').exists()).toBe(false)
    expect(stale.card.emitted()).not.toHaveProperty('gone')
  })
})
