// tj-7zjov1.267 (pins tj-7zjov1.266): a panel-owned dataset has all four retention writes withheld in
// the UI. Design: ADR tj-26jt9w addendum 23 rule B and the owner's ruling A on tj-7zjov1.263 (start
// collecting, stop collecting, set expiry and delete are ALL withheld; the controls stay visible but
// disabled, with one line that the panel manages the dataset and a link to the Panels tab; UI only,
// the server still accepts the writes). Both callers of those writes are pinned here: the Dataset
// detail's Retention card and the Usage tab's Set Expiry dialog.
//
// "Disabled" alone is not the pin: a control re-enabled by hand (the attribute removed, as a stale
// template or a devtools edit would) must still send nothing, so the submit paths are guarded in code.
// Each such test removes the attribute and fires the handler, and asserts no store write and no
// network request.
//
// The owner match is exact: 'Panel', 'PANEL', ' panel' and '' are other owners (the override path),
// never the panel.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import type { DOMWrapper, VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { create } from '@bufbuild/protobuf'
import type { MessageInitShape } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import { Granularity, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { DatasetSummarySchema } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { DatasetSummary } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { OPERATOR_CLIENT_ID, PANEL_MANAGED_TEXT, PANEL_OWNER } from '@/api'
import SetExpiryDialog from '@/components/usage/SetExpiryDialog.vue'
import { useDatasetDetailStore } from '@/stores/datasetDetail'
import { useUsageStore } from '@/stores/usage'

import RetentionCard from './RetentionCard.vue'

/** The line the design names, word for word (tj-7zjov1.266). */
const DESIGN_TEXT = 'Managed by its panel: this dataset is started, stopped and kept by the panel manager.'

const BASE = {
  id: 'ds-panel-1',
  assetSymbol: 'VFV',
  granularity: Granularity.ONE_DAY,
  updateType: UpdateType.DAILY,
  barCount: 10n,
  expiry: timestampFromMs(Date.UTC(2027, 0, 1, 5)),
}

type DatasetInit = MessageInitShape<typeof DatasetSummarySchema>

function dataset(owner: string, extra: DatasetInit = {}): DatasetSummary {
  return create(DatasetSummarySchema, { ...BASE, owner, ...extra })
}

let fetchSpy: ReturnType<typeof vi.fn>

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  // Nothing in this file may reach the wire: a request of any kind is a failure.
  fetchSpy = vi.fn(() => Promise.reject(new Error('no request expected')))
  vi.stubGlobal('fetch', fetchSpy)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

/** Re-enable a control by hand, then fire it: only a code guard can stop the write now. */
async function forceClick(control: DOMWrapper<Element>): Promise<void> {
  control.element.removeAttribute('disabled')
  await control.trigger('click')
  await flushPromises()
}

async function forceEnter(control: DOMWrapper<Element>): Promise<void> {
  control.element.removeAttribute('disabled')
  await control.trigger('keydown', { key: 'Enter' })
  await flushPromises()
}

// ---------------------------------------------------------------------------------------------------
// The Retention card (Dataset detail)
// ---------------------------------------------------------------------------------------------------

function mountCard(owner: string, extra: DatasetInit = {}) {
  const store = useDatasetDetailStore()
  const setExpiry = vi.fn().mockResolvedValue({ kind: 'ok' })
  const setCollecting = vi.fn().mockResolvedValue({ kind: 'ok' })
  const deleteNow = vi.fn().mockResolvedValue({ kind: 'ok' })
  store.setExpiry = setExpiry
  store.setCollecting = setCollecting
  store.deleteNow = deleteNow
  const card = mount(RetentionCard, {
    props: { dataset: dataset(owner, extra) },
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
  const writes = () => setExpiry.mock.calls.length + setCollecting.mock.calls.length + deleteNow.mock.calls.length
  return { card, setExpiry, setCollecting, deleteNow, writes }
}

const tid = (wrapper: VueWrapper, id: string) => wrapper.find(`[data-testid="${id}"]`)

const CARD_CONTROLS = ['expiry-input', 'expiry-tomorrow', 'expiry-clear', 'expiry-apply', 'collecting-toggle', 'delete-open']

describe('the constant', () => {
  it("PANEL_OWNER is 'panel' and is not the operator", () => {
    expect(PANEL_OWNER).toBe('panel')
    expect(PANEL_OWNER).not.toBe(OPERATOR_CLIENT_ID)
  })

  it('the managed line is the design text', () => {
    expect(PANEL_MANAGED_TEXT).toBe(DESIGN_TEXT)
  })
})

describe('RetentionCard: a panel-owned dataset', () => {
  it('every write control is shown and disabled, each described by the panel line', () => {
    const { card } = mountCard(PANEL_OWNER)
    const line = tid(card, 'panel-managed')
    expect(line.exists()).toBe(true)
    const lineId = line.attributes('id')
    expect(lineId).toBeTruthy()
    for (const id of CARD_CONTROLS) {
      const control = tid(card, id)
      expect(control.exists(), id).toBe(true)
      expect(control.attributes('disabled'), id).toBeDefined()
      expect(control.attributes('aria-describedby'), id).toBe(lineId)
    }
  })

  it('the line reads the design text and links to the Panels tab', () => {
    const { card } = mountCard(PANEL_OWNER)
    const line = tid(card, 'panel-managed')
    expect(line.text()).toContain(DESIGN_TEXT)
    const link = line.findComponent(RouterLinkStub)
    expect(link.exists()).toBe(true)
    expect(link.props('to')).toBe('/data/panels')
    expect(link.attributes('data-testid')).toBe('panel-managed-link')
    expect(link.text()).toBe('Panels')
  })

  it('no override: no Held By warning, no reason prompt, no confirmation', () => {
    const { card } = mountCard(PANEL_OWNER)
    expect(tid(card, 'override-warning').exists()).toBe(false)
    expect(tid(card, 'override-prompt').exists()).toBe(false)
    expect(tid(card, 'override-reason').exists()).toBe(false)
    expect(tid(card, 'override-confirm').exists()).toBe(false)
    expect(card.text()).not.toContain('Held by')
  })

  it('a stopped subscription: Start Collecting is withheld too', () => {
    const { card } = mountCard(PANEL_OWNER, { collecting: false })
    const toggle = tid(card, 'collecting-toggle')
    expect(toggle.text()).toBe('Start Collecting')
    expect(toggle.attributes('disabled')).toBeDefined()
  })

  it('Set Expiry re-enabled by hand sends nothing (button and Enter)', async () => {
    const { card, setExpiry } = mountCard(PANEL_OWNER)
    const input = tid(card, 'expiry-input')
    input.element.removeAttribute('disabled')
    await input.setValue('2027/05/05')
    await forceClick(tid(card, 'expiry-apply'))
    await forceEnter(input)
    // Never too: an empty field on a dataset with an expiry is Set to Never.
    await input.setValue('')
    await forceClick(tid(card, 'expiry-apply'))
    expect(setExpiry).not.toHaveBeenCalled()
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('Stop Collecting re-enabled by hand sends nothing', async () => {
    const { card, setCollecting } = mountCard(PANEL_OWNER)
    expect(tid(card, 'collecting-toggle').text()).toBe('Stop Collecting')
    await forceClick(tid(card, 'collecting-toggle'))
    expect(setCollecting).not.toHaveBeenCalled()
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('Start Collecting re-enabled by hand sends nothing', async () => {
    const { card, setCollecting } = mountCard(PANEL_OWNER, { collecting: false })
    await forceClick(tid(card, 'collecting-toggle'))
    expect(setCollecting).not.toHaveBeenCalled()
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('Delete Now re-enabled by hand does not open the confirmation and sends nothing', async () => {
    const { card, deleteNow } = mountCard(PANEL_OWNER)
    await forceClick(tid(card, 'delete-open'))
    expect(tid(card, 'delete-confirm').exists()).toBe(false)
    expect(deleteNow).not.toHaveBeenCalled()
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('an owner that turns into the panel on refetch: the controls disable and the open delete sends nothing', async () => {
    const { card, deleteNow, writes } = mountCard(OPERATOR_CLIENT_ID)
    for (const id of CARD_CONTROLS) expect(tid(card, id).attributes('disabled'), id).toBeUndefined()
    // The operator opens the delete confirmation and types the name, then a refetch shows the panel.
    await tid(card, 'delete-open').trigger('click')
    await tid(card, 'delete-typed').setValue('VFV 1d')
    await card.setProps({ dataset: dataset(PANEL_OWNER) })
    expect(tid(card, 'panel-managed').exists()).toBe(true)
    const lineId = tid(card, 'panel-managed').attributes('id')
    for (const id of ['expiry-input', 'expiry-tomorrow', 'expiry-clear', 'expiry-apply', 'collecting-toggle']) {
      expect(tid(card, id).attributes('disabled'), id).toBeDefined()
    }
    // The confirmation that was already open is withheld too: disabled and described by the panel line
    // (tj-7zjov1.266 RE:, fixed in e67592c), not an enabled Delete Now that silently does nothing.
    expect(tid(card, 'delete-confirm').exists()).toBe(true)
    for (const id of ['delete-typed', 'delete-submit']) {
      expect(tid(card, id).attributes('disabled'), id).toBeDefined()
      expect(tid(card, id).attributes('aria-describedby'), id).toBe(lineId)
    }
    await forceClick(tid(card, 'delete-submit'))
    await forceEnter(tid(card, 'delete-typed'))
    expect(deleteNow).not.toHaveBeenCalled()
    expect(writes()).toBe(0)
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})

describe('RetentionCard: owners that are not the panel are unchanged', () => {
  it('the operator: every control enabled, no panel line, no override, the writes go through', async () => {
    const { card, setExpiry, setCollecting } = mountCard(OPERATOR_CLIENT_ID)
    expect(tid(card, 'panel-managed').exists()).toBe(false)
    expect(tid(card, 'override-warning').exists()).toBe(false)
    for (const id of CARD_CONTROLS) {
      expect(tid(card, id).attributes('disabled'), id).toBeUndefined()
      expect(tid(card, id).attributes('aria-describedby'), id).toBeUndefined()
    }
    await tid(card, 'expiry-input').setValue('2027/05/05')
    await tid(card, 'expiry-apply').trigger('click')
    await flushPromises()
    await tid(card, 'collecting-toggle').trigger('click')
    await flushPromises()
    expect(setExpiry).toHaveBeenCalledTimes(1)
    expect(setExpiry.mock.calls[0]![1]).toBeUndefined()
    expect(setCollecting).toHaveBeenCalledWith(false, undefined)
  })

  it.each(['Panel', 'PANEL', ' panel', 'panel ', 'panels', '', 'someone'])(
    "owner %j is another owner: no panel line, the override path is offered and works",
    async (owner) => {
      const { card, setCollecting, deleteNow } = mountCard(owner)
      expect(tid(card, 'panel-managed').exists()).toBe(false)
      expect(tid(card, 'override-warning').exists()).toBe(true)
      expect(tid(card, 'override-prompt').exists()).toBe(true)
      for (const id of ['expiry-input', 'expiry-tomorrow', 'expiry-clear', 'delete-open']) {
        expect(tid(card, id).attributes('disabled'), id).toBeUndefined()
      }
      // Gated by the reason and the confirmation, as before: then it sends the override.
      expect(tid(card, 'collecting-toggle').attributes('disabled')).toBeDefined()
      await tid(card, 'override-reason').setValue('owner asked')
      await tid(card, 'override-confirm').setValue(true)
      expect(tid(card, 'collecting-toggle').attributes('disabled')).toBeUndefined()
      await tid(card, 'collecting-toggle').trigger('click')
      await flushPromises()
      expect(setCollecting).toHaveBeenCalledWith(false, { reason: 'owner asked', confirmed: true })
      await tid(card, 'delete-open').trigger('click')
      expect(tid(card, 'delete-confirm').exists()).toBe(true)
      await tid(card, 'override-reason').setValue('owner asked')
      await tid(card, 'override-confirm').setValue(true)
      await tid(card, 'delete-typed').setValue('VFV 1d')
      await tid(card, 'delete-submit').trigger('click')
      await flushPromises()
      expect(deleteNow).toHaveBeenCalledWith({ reason: 'owner asked', confirmed: true })
    },
  )
})

// ---------------------------------------------------------------------------------------------------
// The Set Expiry dialog (Usage tab, Stale Datasets)
// ---------------------------------------------------------------------------------------------------

function mountDialog(owner: string) {
  const store = useUsageStore()
  const setExpiry = vi.fn().mockResolvedValue({ kind: 'ok' })
  store.setExpiry = setExpiry
  const dialog = mount(SetExpiryDialog, {
    props: { dataset: dataset(owner) },
    global: { stubs: { RouterLink: RouterLinkStub } },
    attachTo: document.body,
  })
  return { dialog, setExpiry }
}

const DIALOG_CONTROLS = ['expiry-day', 'expiry-tomorrow', 'expiry-submit']

describe('SetExpiryDialog: a panel-owned dataset', () => {
  it('Expiry Day, Tomorrow and Set Expiry are shown and disabled, described by the panel line', () => {
    const { dialog } = mountDialog(PANEL_OWNER)
    const line = tid(dialog, 'panel-managed')
    expect(line.exists()).toBe(true)
    const lineId = line.attributes('id')
    expect(lineId).toBeTruthy()
    for (const id of DIALOG_CONTROLS) {
      const control = tid(dialog, id)
      expect(control.exists(), id).toBe(true)
      expect(control.attributes('disabled'), id).toBeDefined()
      expect(control.attributes('aria-describedby'), id).toBe(lineId)
    }
    // Cancel still closes the dialog.
    expect(tid(dialog, 'expiry-cancel').attributes('disabled')).toBeUndefined()
    dialog.unmount()
  })

  it('the line reads the design text and links to the Panels tab; no override', () => {
    const { dialog } = mountDialog(PANEL_OWNER)
    const line = tid(dialog, 'panel-managed')
    expect(line.text()).toContain(DESIGN_TEXT)
    const link = line.findComponent(RouterLinkStub)
    expect(link.props('to')).toBe('/data/panels')
    expect(link.attributes('data-testid')).toBe('panel-managed-link')
    expect(tid(dialog, 'override-warning').exists()).toBe(false)
    expect(tid(dialog, 'override-prompt').exists()).toBe(false)
    expect(tid(dialog, 'override-reason').exists()).toBe(false)
    dialog.unmount()
  })

  it('Set Expiry re-enabled by hand sends nothing (button and Enter)', async () => {
    const { dialog, setExpiry } = mountDialog(PANEL_OWNER)
    const day = tid(dialog, 'expiry-day')
    day.element.removeAttribute('disabled')
    await day.setValue('2027/05/05')
    await forceClick(tid(dialog, 'expiry-submit'))
    await forceEnter(day)
    expect(setExpiry).not.toHaveBeenCalled()
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(dialog.emitted()).not.toHaveProperty('close')
    dialog.unmount()
  })
})

describe('SetExpiryDialog: owners that are not the panel are unchanged', () => {
  it('the operator: enabled, no panel line, no override, the write goes through', async () => {
    const { dialog, setExpiry } = mountDialog(OPERATOR_CLIENT_ID)
    expect(tid(dialog, 'panel-managed').exists()).toBe(false)
    expect(tid(dialog, 'override-warning').exists()).toBe(false)
    for (const id of ['expiry-day', 'expiry-tomorrow']) expect(tid(dialog, id).attributes('disabled'), id).toBeUndefined()
    await tid(dialog, 'expiry-day').setValue('2027/05/05')
    expect(tid(dialog, 'expiry-submit').attributes('disabled')).toBeUndefined()
    await tid(dialog, 'expiry-submit').trigger('click')
    await flushPromises()
    expect(setExpiry).toHaveBeenCalledTimes(1)
    expect(setExpiry.mock.calls[0]![0]).toBe(BASE.id)
    expect(setExpiry.mock.calls[0]![2]).toBeUndefined()
    dialog.unmount()
  })

  it.each(['Panel', 'PANEL', ' panel', 'panels', '', 'someone'])(
    'owner %j is another owner: the override path is offered and works',
    async (owner) => {
      const { dialog, setExpiry } = mountDialog(owner)
      expect(tid(dialog, 'panel-managed').exists()).toBe(false)
      expect(tid(dialog, 'override-warning').exists()).toBe(true)
      expect(tid(dialog, 'override-prompt').exists()).toBe(true)
      for (const id of ['expiry-day', 'expiry-tomorrow']) expect(tid(dialog, id).attributes('disabled'), id).toBeUndefined()
      await tid(dialog, 'expiry-day').setValue('2027/05/05')
      expect(tid(dialog, 'expiry-submit').attributes('disabled')).toBeDefined()
      await tid(dialog, 'override-reason').setValue('owner asked')
      await tid(dialog, 'expiry-submit').trigger('click')
      await flushPromises()
      expect(setExpiry).toHaveBeenCalledTimes(1)
      expect(setExpiry.mock.calls[0]![2]).toEqual({ reason: 'owner asked', confirmed: true })
      dialog.unmount()
    },
  )
})
