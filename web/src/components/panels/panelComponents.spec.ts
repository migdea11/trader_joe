// The Panels page's components (tj-7zjov1.37), each mounted alone over a real Pinia with the store's writes
// replaced and the typed client mocked at @/api:
// - PanelSettingsForm: Save Settings sends the form, Enable or Disable sends the SAVED settings with only
//   `enabled` flipped (unsaved edits are not smuggled in); the reason is optional (tj-7zjov1.362.4).
// - PanelCreateForm: cadence Monthly (default) sent UNSPECIFIED unless picked; the four source settings
//   fields only while the chosen source's versions page is empty, all four optional (ADR tj-2ayia2 D6: an
//   empty ramp or cap is no limit and left out; a typed one is ramp 1 or more, cap 2 or more, at most the
//   int32 maximum); only name and source are required; an optional reason, no size. Validated on click
//   (tj-7zjov1.362.2): nothing marked on open, a press with gaps marks every one, focuses the first and
//   sends nothing; the marks then follow the fields.
// - PanelSourceSettingsForm: ramp and cap optional by the same rule, validated live (Save off on a bad one);
//   the window days required, 0 or more; an optional reason.
// tj-7zjov1.362.4 (ADR tj-46jkey sections 1 and 2): no form and no fact shows a size; a blank reason is sent
// as '' and a typed one trimmed, on all three writes.
// - PanelHistoryStrip: one block per expected day, the change bar, markers with version, pass, actor and
//   reason; a version no pass has run under is Pending (dimmed), not a boundary.
// - PanelMemberList and PanelCorporateActions: Unknown health for a member without a dataset, the events
//   view per dataset.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { create } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'
import { createPinia, setActivePinia } from 'pinia'

import { AssetType, DataSource } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { DatasetHealthStatus } from '@generated/trader_joe/proto/ui/v1/data_pb'
import {
  AssetListDayState,
  AssetListHistorySchema,
  CorporateActionEventPageSchema,
  CorporateActionEventSchema,
  EffectivePassSchema,
  PanelBoundariesSchema,
  PanelCadence,
  PanelDetailSchema,
  PanelListItemSchema,
  PanelMarkerKind,
  PanelMarkerSchema,
  PanelMemberSchema,
  PanelPassKind,
  PanelSettingsSchema,
  PanelSettingsVersionSchema,
  PanelSourceSettingsSchema,
  PanelSourceSettingsVersionPageSchema,
  PanelSourceSettingsVersionSchema,
  PanelTransition,
} from '@generated/trader_joe/proto/ui/v1/panels_pb'
import type { PanelDetail } from '@generated/trader_joe/proto/ui/v1/panels_pb'

import { ApiError } from '@/api'
import { usePanelsStore } from '@/stores/panels'
import type { PanelWriteOutcome } from '@/stores/panels'

import PanelCorporateActions from './PanelCorporateActions.vue'
import PanelCreateForm from './PanelCreateForm.vue'
import PanelHistoryStrip from './PanelHistoryStrip.vue'
import PanelMemberList from './PanelMemberList.vue'
import PanelSettingsForm from './PanelSettingsForm.vue'
import PanelSourceSettingsForm from './PanelSourceSettingsForm.vue'

const api = vi.hoisted(() => ({
  getSourceSettingsVersions: vi.fn(),
  getCorporateActions: vi.fn(),
}))
vi.mock('@/api', async (importOriginal) => ({ ...(await importOriginal<typeof import('@/api')>()), ...api }))

const PANEL_ID = '0f8fad5b-d9cb-469f-a165-70867728950e'
const DATASET_ID = '7c9e6679-7425-40de-944b-e07fc1f90ae7'
const DAY = 86_400_000
const T0 = Date.UTC(2026, 7, 3)

const SAVED = create(PanelSettingsSchema, {
  enabled: true,
  cadence: PanelCadence.WEEKLY,
  allowSymbols: ['AAPL', 'MSFT'],
  denySymbols: ['TSLA'],
  assetClass: AssetType.STOCK,
  exchange: 'NASDAQ',
  active: true,
  tradable: false,
})
const SAVED_INIT = {
  enabled: true,
  cadence: PanelCadence.WEEKLY,
  allowSymbols: ['AAPL', 'MSFT'],
  denySymbols: ['TSLA'],
  assetClass: AssetType.STOCK,
  exchange: 'NASDAQ',
  active: true,
  tradable: false,
}

function detail(
  overrides: { settings?: typeof SAVED; versionNo?: number; effective?: boolean; source?: boolean; reason?: string } = {},
): PanelDetail {
  return create(PanelDetailSchema, {
    id: PANEL_ID,
    name: 'Large caps',
    source: DataSource.ALPACA_API,
    currentVersion: create(PanelSettingsVersionSchema, {
      versionNo: overrides.versionNo ?? 3,
      settings: overrides.settings ?? SAVED,
      actor: 'operator',
      reason: overrides.reason ?? 'widen',
      savedAt: timestampFromMs(T0),
      effectivePass:
        overrides.effective === false ? undefined : create(EffectivePassSchema, { kind: PanelPassKind.MEMBERSHIP, startedAt: timestampFromMs(T0 + DAY) }),
    }),
    currentSourceVersion:
      overrides.source === false
        ? undefined
        : create(PanelSourceSettingsVersionSchema, {
            source: DataSource.ALPACA_API,
            versionNo: 2,
            actor: 'operator',
            settings: create(PanelSourceSettingsSchema, {
              rampDatasetsPerHour: 5,
              requestCapPerHour: 200,
              corporateActionWindowPreDays: 90,
              corporateActionWindowPostDays: 30,
            }),
          }),
  })
}

function stubWrites(outcome: PanelWriteOutcome = { kind: 'ok' }) {
  const store = usePanelsStore()
  const saveSettings = vi.fn().mockResolvedValue(outcome)
  const saveSource = vi.fn().mockResolvedValue(outcome)
  const createPanel = vi.fn().mockResolvedValue(outcome)
  store.saveSettings = saveSettings
  store.saveSource = saveSource
  store.create = createPanel
  return { store, saveSettings, saveSource, create: createPanel }
}

const byId = (wrapper: VueWrapper, id: string) => wrapper.find(`[data-testid="${id}"]`)

// Create Panel pressed as the operator presses it: a click on the button. A disabled button sends nothing, and
// jsdom submits only an attached form, so the create forms mount with attachTo (auto-unmounted below).
async function press(form: VueWrapper): Promise<void> {
  await byId(form, 'create-submit').trigger('click')
  await flushPromises()
}

const mountCreate = () => mount(PanelCreateForm, { attachTo: document.body })
const focused = (form: VueWrapper, id: string) => expect(document.activeElement).toBe(byId(form, id).element)

enableAutoUnmount(afterEach)

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  for (const fn of Object.values(api)) fn.mockReset()
})

// ---------------------------------------------------------------------------------------------------

describe('PanelSettingsForm', () => {
  it('shows the saved settings, the version, the reason and the effective pass', () => {
    stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    expect(form.find('[data-fact="enabled"]').text()).toBe('Enabled')
    expect(form.find('[data-fact="cadence"]').text()).toBe('Weekly')
    expect(form.find('[data-fact="version"]').text()).toMatch(/^Version 3 saved .+ by operator$/)
    expect(form.find('[data-fact="effective"]').text()).toMatch(/^membership pass, /)
    expect(byId(form, 'current-reason').text()).toBe('Reason: widen')
    expect(byId(form, 'settings-toggle').text()).toBe('Disable Panel')
  })

  // tj-7zjov1.362.4: no size anywhere, neither as a saved fact nor as a field.
  it('shows no size: no Size fact, no size field, no Size label', () => {
    stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    expect(form.find('[data-fact="size"]').exists()).toBe(false)
    expect(byId(form, 'settings-size').exists()).toBe(false)
    expect(form.find('#settings-size').exists()).toBe(false)
    expect(form.findAll('dt').map((dt) => dt.text())).not.toContain('Size')
    expect(form.findAll('label').map((label) => label.text())).not.toContain('Size')
  })

  it('a saved version with an empty reason shows no Reason line', () => {
    stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail({ reason: '' }) } })
    expect(byId(form, 'current-reason').exists()).toBe(false)
    expect(form.text()).not.toContain('Reason:')
  })

  it('the reason field reads optional: no asterisk, no aria-required, no error, the optional placeholder', () => {
    stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    expect(form.find('label[for="settings-reason"]').text()).toBe('Reason (optional)')
    expect(form.find('label[for="settings-reason"] .pn-required').exists()).toBe(false)
    expect(byId(form, 'settings-reason').attributes('aria-required')).toBeUndefined()
    expect(byId(form, 'settings-reason').attributes('placeholder')).toBe('Optional: why the settings change')
    expect(byId(form, 'settings-problem').exists()).toBe(false)
  })

  it('a version no pass has run under reads Pending', () => {
    stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail({ effective: false }) } })
    expect(form.find('[data-fact="effective"]').text()).toBe('Pending: no pass has run yet')
  })

  it('the form starts from the saved settings', () => {
    stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    expect((byId(form, 'settings-allow').element as HTMLInputElement).value).toBe('AAPL, MSFT')
    expect((byId(form, 'settings-exchange').element as HTMLInputElement).value).toBe('NASDAQ')
  })

  // tj-7zjov1.362.4: the reason no longer gates either write. Untouched, or blank after typing, both
  // buttons are on and each sends once with reason ''.
  it.each([
    ['never typed', null],
    ['typed blank', '   '],
    ['typed whitespace only', '\n\t'],
  ])('with no reason (%s) Save Settings sends, with reason \'\'', async (_label, typed) => {
    const { saveSettings } = stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    if (typed !== null) await byId(form, 'settings-reason').setValue(typed)
    expect(byId(form, 'settings-save').attributes('disabled')).toBeUndefined()
    expect(byId(form, 'settings-toggle').attributes('disabled')).toBeUndefined()
    expect(byId(form, 'settings-problem').exists()).toBe(false)
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSettings).toHaveBeenCalledTimes(1)
    expect(saveSettings).toHaveBeenCalledWith({ ...SAVED_INIT, reason: '' })
  })

  it.each([
    ['never typed', null],
    ['typed blank', '   '],
  ])('with no reason (%s) Disable sends the saved settings, with reason \'\'', async (_label, typed) => {
    const { saveSettings } = stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    if (typed !== null) await byId(form, 'settings-reason').setValue(typed)
    await byId(form, 'settings-toggle').trigger('click')
    await flushPromises()
    expect(saveSettings).toHaveBeenCalledTimes(1)
    expect(saveSettings).toHaveBeenCalledWith({ ...SAVED_INIT, enabled: false, reason: '' })
  })

  it('with no reason Enable on a disabled panel sends, with reason \'\'', async () => {
    const { saveSettings } = stubWrites()
    const disabled = create(PanelSettingsSchema, { ...SAVED, enabled: false })
    const form = mount(PanelSettingsForm, { props: { detail: detail({ settings: disabled }) } })
    await byId(form, 'settings-toggle').trigger('click')
    await flushPromises()
    expect(saveSettings).toHaveBeenCalledWith({ ...SAVED_INIT, enabled: true, reason: '' })
  })

  it('Save Settings sends a typed reason trimmed', async () => {
    const { saveSettings } = stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    await byId(form, 'settings-reason').setValue('  \tnarrow it\n ')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSettings).toHaveBeenCalledWith({ ...SAVED_INIT, reason: 'narrow it' })
  })

  it('Disable sends the SAVED settings with only enabled flipped, ignoring unsaved edits, and the trimmed reason', async () => {
    const { saveSettings } = stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    await byId(form, 'settings-deny').setValue('GME')
    await byId(form, 'settings-reason').setValue('  pause it  ')
    await byId(form, 'settings-toggle').trigger('click')
    await flushPromises()
    expect(saveSettings).toHaveBeenCalledTimes(1)
    expect(saveSettings).toHaveBeenCalledWith({ ...SAVED_INIT, enabled: false, reason: 'pause it' })
  })

  it('Enable on a disabled panel sends enabled true and the rest as saved', async () => {
    const { saveSettings } = stubWrites()
    const disabled = create(PanelSettingsSchema, { ...SAVED, enabled: false })
    const form = mount(PanelSettingsForm, { props: { detail: detail({ settings: disabled }) } })
    expect(byId(form, 'settings-toggle').text()).toBe('Enable Panel')
    expect(form.find('[data-fact="enabled"]').text()).toBe('Disabled')
    await byId(form, 'settings-reason').setValue('resume')
    await byId(form, 'settings-toggle').trigger('click')
    await flushPromises()
    expect(saveSettings).toHaveBeenCalledWith({ ...SAVED_INIT, enabled: true, reason: 'resume' })
  })

  it('Save Settings sends the edited form, absent filters left out', async () => {
    const { saveSettings } = stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    await byId(form, 'settings-allow').setValue('')
    await byId(form, 'settings-exchange').setValue('')
    await byId(form, 'settings-active').findAll('option')[0]!.setValue()
    await byId(form, 'settings-asset-class').findAll('option')[0]!.setValue()
    await byId(form, 'settings-cadence').findAll('option')[1]!.setValue()
    await byId(form, 'settings-reason').setValue('narrow')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSettings).toHaveBeenCalledWith({
      enabled: true,
      cadence: PanelCadence.DAILY,
      allowSymbols: [],
      denySymbols: ['TSLA'],
      assetClass: undefined,
      exchange: undefined,
      active: undefined,
      tradable: false,
      reason: 'narrow',
    })
  })

  it('no cadence stops Save Settings but not Disable', async () => {
    const { saveSettings } = stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    await byId(form, 'settings-cadence').findAll('option')[0]!.setValue()
    expect(byId(form, 'settings-problem').text()).toBe('Choose a cadence.')
    expect(byId(form, 'settings-save').attributes('disabled')).toBeDefined()
    expect(byId(form, 'settings-toggle').attributes('disabled')).toBeUndefined()
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSettings).not.toHaveBeenCalled()
  })

  it('the settings form keeps its one-line problem: no required marker and no per-field mark (tj-crl7pm)', async () => {
    stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    await byId(form, 'settings-cadence').findAll('option')[0]!.setValue()
    expect(byId(form, 'settings-problem').text()).toBe('Choose a cadence.')
    expect(form.find('.pn-required').exists()).toBe(false)
    expect(form.find('.pn-field-error').exists()).toBe(false)
    expect(form.find('[aria-required]').exists()).toBe(false)
    expect(form.find('[aria-invalid]').exists()).toBe(false)
    await byId(form, 'settings-cadence').findAll('option')[1]!.setValue()
    expect(byId(form, 'settings-problem').exists()).toBe(false)
  })

  it('validate-on-click is the create form only: Save Settings stays off while blocked and a submit marks nothing (tj-7zjov1.362.2)', async () => {
    const { saveSettings } = stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() }, attachTo: document.body })
    await byId(form, 'settings-cadence').findAll('option')[0]!.setValue()
    await byId(form, 'settings-reason').setValue('r')
    expect(byId(form, 'settings-save').attributes('disabled')).toBeDefined()
    expect(byId(form, 'settings-problem').text()).toBe('Choose a cadence.')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSettings).not.toHaveBeenCalled()
    expect(form.find('[aria-invalid]').exists()).toBe(false)
    expect(form.find('.pn-required').exists()).toBe(false)
    expect(form.find('.pn-field-error').exists()).toBe(false)
    expect(document.activeElement).toBe(document.body)
    expect(byId(form, 'settings-problem').text()).toBe('Choose a cadence.')
  })

  it('a refusal is worded in place with its error id, the form kept', async () => {
    stubWrites({ kind: 'invalid', error: { message: 'Request failed (HTTP 422)', errorId: 'e-9' } })
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    await byId(form, 'settings-reason').setValue('why')
    await byId(form, 'settings-toggle').trigger('click')
    await flushPromises()
    expect(byId(form, 'settings-notice').text()).toContain('The store refused these settings, so nothing was saved. Check the fields.')
    expect(byId(form, 'settings-notice').text()).toContain('Error ID: e-9')
    expect((byId(form, 'settings-reason').element as HTMLTextAreaElement).value).toBe('why')
  })

  it('an ok write clears the reason; a newer saved version starts the form over', async () => {
    stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    await byId(form, 'settings-deny').setValue('GME')
    await byId(form, 'settings-reason').setValue('why')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect((byId(form, 'settings-reason').element as HTMLTextAreaElement).value).toBe('')
    expect(byId(form, 'settings-notice').exists()).toBe(false)
    await byId(form, 'settings-deny').setValue('GME')
    await form.setProps({ detail: detail({ versionNo: 4 }) })
    expect((byId(form, 'settings-deny').element as HTMLInputElement).value).toBe('TSLA')
  })

  it('a stale answer shows nothing', async () => {
    stubWrites({ kind: 'stale' })
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    await byId(form, 'settings-reason').setValue('why')
    await byId(form, 'settings-toggle').trigger('click')
    await flushPromises()
    expect(byId(form, 'settings-notice').exists()).toBe(false)
  })

  it('while a write is out both buttons are disabled', async () => {
    const { store } = stubWrites()
    store.writing = true
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    await byId(form, 'settings-reason').setValue('why')
    expect(byId(form, 'settings-save').attributes('disabled')).toBeDefined()
    expect(byId(form, 'settings-toggle').attributes('disabled')).toBeDefined()
  })
})

// tj-7zjov1.248: the toggle body is the saved PanelSettings as read with only `enabled` flipped. The
// form's text round trip trimmed the exchange and dropped an empty-but-present exchange and a present
// UNSPECIFIED asset class: each a new settings version that changes membership on the next pass. Bodies
// are compared with toStrictEqual, so key presence counts (a size key coming back reds every row).
describe('PanelSettingsForm: Enable or Disable sends the saved settings exactly', () => {
  const BARE = { cadence: PanelCadence.MONTHLY, allowSymbols: ['VFV'], denySymbols: [] as string[] }

  async function toggleBody(fields: Record<string, unknown>): Promise<Record<string, unknown>> {
    const { saveSettings } = stubWrites()
    const saved = create(PanelSettingsSchema, { enabled: true, ...BARE, ...fields })
    const form = mount(PanelSettingsForm, { props: { detail: detail({ settings: saved }) } })
    await byId(form, 'settings-reason').setValue('flip')
    await byId(form, 'settings-toggle').trigger('click')
    await flushPromises()
    expect(saveSettings).toHaveBeenCalledTimes(1)
    return saveSettings.mock.calls[0]![0] as Record<string, unknown>
  }

  const base = (enabled: boolean) => ({ enabled, ...BARE, reason: 'flip' })

  it.each([
    ["exchange ' X ' (not trimmed)", { exchange: ' X ' }, { exchange: ' X ' }],
    ['an empty-but-present exchange', { exchange: '' }, { exchange: '' }],
    ['asset class absent', {}, {}],
    ['asset class UNSPECIFIED, present as saved', { assetClass: AssetType.UNSPECIFIED }, { assetClass: AssetType.UNSPECIFIED }],
    ['asset class STOCK', { assetClass: AssetType.STOCK }, { assetClass: AssetType.STOCK }],
    ['active and tradable absent', {}, {}],
    ['active false, tradable false', { active: false, tradable: false }, { active: false, tradable: false }],
    ['active true, tradable true', { active: true, tradable: true }, { active: true, tradable: true }],
    ['active false, tradable absent', { active: false }, { active: false }],
    ['symbols as saved', { allowSymbols: [' vfv ', 'A,B'], denySymbols: ['x y'] }, { allowSymbols: [' vfv ', 'A,B'], denySymbols: ['x y'] }],
  ])('Disable with %s', async (_label, fields, sent) => {
    expect(await toggleBody(fields)).toStrictEqual({ ...base(false), ...sent })
  })

  it('Enable on a disabled panel with every hazard at once', async () => {
    const { saveSettings } = stubWrites()
    const saved = create(PanelSettingsSchema, {
      enabled: false,
      ...BARE,
      exchange: '',
      assetClass: AssetType.UNSPECIFIED,
      active: false,
    })
    const form = mount(PanelSettingsForm, { props: { detail: detail({ settings: saved }) } })
    await byId(form, 'settings-reason').setValue('resume')
    await byId(form, 'settings-toggle').trigger('click')
    await flushPromises()
    expect(saveSettings.mock.calls[0]![0]).toStrictEqual({
      ...base(true),
      exchange: '',
      assetClass: AssetType.UNSPECIFIED,
      active: false,
      reason: 'resume',
    })
  })

  it('Save Settings still goes through the form: the exchange is trimmed, a blank one and UNSPECIFIED left out', async () => {
    const { saveSettings } = stubWrites()
    const saved = create(PanelSettingsSchema, { enabled: true, ...BARE, exchange: ' X ', assetClass: AssetType.UNSPECIFIED })
    const form = mount(PanelSettingsForm, { props: { detail: detail({ settings: saved }) } })
    await byId(form, 'settings-reason').setValue('edit')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSettings).toHaveBeenCalledTimes(1)
    expect(saveSettings.mock.calls[0]![0]).toMatchObject({ exchange: 'X', assetClass: undefined })
    expect(Object.hasOwn(saveSettings.mock.calls[0]![0] as object, 'size')).toBe(false)

    saveSettings.mockClear()
    await byId(form, 'settings-exchange').setValue('   ')
    await byId(form, 'settings-reason').setValue('edit')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSettings.mock.calls[0]![0]).toMatchObject({ exchange: undefined })
  })
})

// ---------------------------------------------------------------------------------------------------

describe('PanelCreateForm', () => {
  const EMPTY_PAGE = create(PanelSourceSettingsVersionPageSchema)
  const HELD_PAGE = create(PanelSourceSettingsVersionPageSchema, { items: [create(PanelSourceSettingsVersionSchema, { versionNo: 1 })] })

  async function fill(form: VueWrapper, sourceIndex: number): Promise<void> {
    await byId(form, 'create-name').setValue('  Large caps  ')
    await byId(form, 'create-source').findAll('option')[sourceIndex]!.setValue()
    await flushPromises()
    await byId(form, 'create-reason').setValue('  first panel  ')
  }

  // tj-7zjov1.362.4: no size on the create form, and the reason reads optional and is never required.
  it('has no size: no size field, no Size label, no size error', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(EMPTY_PAGE)
    stubWrites()
    const form = mountCreate()
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    await press(form)
    expect(byId(form, 'create-size').exists()).toBe(false)
    expect(form.find('#create-size').exists()).toBe(false)
    expect(byId(form, 'create-size-error').exists()).toBe(false)
    expect(form.findAll('label').map((label) => label.text().replace(/\s*\*$/, ''))).not.toContain('Size')
  })

  it('the reason reads optional: no asterisk, no aria-required, and a press on an empty form leaves it unmarked', async () => {
    stubWrites()
    const form = mountCreate()
    const label = form.find('label[for="create-reason"]')
    expect(label.text()).toBe('Reason (optional)')
    expect(label.find('.pn-required').exists()).toBe(false)
    expect(byId(form, 'create-reason').attributes('aria-required')).toBeUndefined()
    expect(byId(form, 'create-reason').attributes('placeholder')).toBe('Optional: why the panel is created')
    await press(form)
    expect(byId(form, 'create-reason').attributes('aria-invalid')).toBeUndefined()
    expect(byId(form, 'create-reason').attributes('aria-describedby')).toBeUndefined()
    expect(byId(form, 'create-reason-error').exists()).toBe(false)
    expect(form.find('#create-reason-error').exists()).toBe(false)
  })

  it.each([
    ['never typed', null],
    ['typed blank', '  \n '],
  ])('the create form with only name and source (reason %s) sends once, with reason \'\'', async (_label, typed) => {
    api.getSourceSettingsVersions.mockResolvedValue(HELD_PAGE)
    const { create: createPanel } = stubWrites({ kind: 'created', detail: detail() })
    const form = mountCreate()
    await byId(form, 'create-name').setValue('n')
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    if (typed !== null) await byId(form, 'create-reason').setValue(typed)
    await press(form)
    expect(createPanel).toHaveBeenCalledTimes(1)
    expect(createPanel.mock.calls[0]![0]).toMatchObject({ name: 'n', source: DataSource.ALPACA_API, reason: '' })
    expect(Object.hasOwn(createPanel.mock.calls[0]![0] as object, 'size')).toBe(false)
    expect(form.findAll('[aria-invalid]')).toHaveLength(0)
  })

  it('the cadence offers Monthly (default) first, selected', () => {
    stubWrites()
    const form = mount(PanelCreateForm)
    const options = byId(form, 'create-cadence').findAll('option')
    expect(options.map((o) => o.text())).toEqual(['Monthly (default)', 'Daily', 'Weekly', 'Monthly'])
    expect((options[0]!.element as HTMLOptionElement).selected).toBe(true)
  })

  it('reads the chosen source versions page (limit 1), by the source', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(HELD_PAGE)
    stubWrites()
    const form = mount(PanelCreateForm)
    await byId(form, 'create-source').findAll('option')[2]!.setValue()
    await flushPromises()
    expect(api.getSourceSettingsVersions).toHaveBeenCalledWith(DataSource.IB_API, { limit: 1 })
  })

  it('a source with a settings version: no source fields; the body carries none', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(HELD_PAGE)
    const { create: createPanel } = stubWrites({ kind: 'created', detail: detail() })
    const form = mount(PanelCreateForm)
    await fill(form, 1)
    expect(byId(form, 'create-ramp').exists()).toBe(false)
    expect(byId(form, 'create-problem').exists()).toBe(false)
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(createPanel).toHaveBeenCalledWith({
      enabled: true,
      cadence: PanelCadence.UNSPECIFIED,
      allowSymbols: [],
      denySymbols: [],
      assetClass: undefined,
      exchange: undefined,
      active: undefined,
      tradable: undefined,
      name: 'Large caps',
      source: DataSource.ALPACA_API,
      reason: 'first panel',
    })
    expect(form.emitted('close')).toHaveLength(1)
  })

  // ADR tj-2ayia2 D6: on a source with no settings every one of the four fields is optional; empty means no
  // limit (the rates) or the store default (the window days), and an empty field reaches the store as nothing.
  it('a source with no settings version: all four empty send at once, every source field undefined', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(EMPTY_PAGE)
    const { create: createPanel } = stubWrites({ kind: 'created', detail: detail() })
    const form = mountCreate()
    await fill(form, 1)
    for (const field of ['ramp', 'cap', 'pre', 'post']) expect(byId(form, `create-${field}`).exists()).toBe(true)
    await press(form)
    expect(createPanel).toHaveBeenCalledTimes(1)
    const sent = createPanel.mock.calls[0]![0]
    expect(sent.rampDatasetsPerHour).toBeUndefined()
    expect(sent.requestCapPerHour).toBeUndefined()
    expect(sent.corporateActionWindowPreDays).toBeUndefined()
    expect(sent.corporateActionWindowPostDays).toBeUndefined()
    expect(sent).toMatchObject({ name: 'Large caps', source: DataSource.ALPACA_API, reason: 'first panel' })
    expect(form.findAll('[aria-invalid]')).toHaveLength(0)
    expect(form.emitted('close')).toHaveLength(1)
  })

  it.each([
    ['ramp', ' 1 ', { rampDatasetsPerHour: 1 }],
    ['ramp', '2147483647', { rampDatasetsPerHour: 2_147_483_647 }],
    ['cap', '2', { requestCapPerHour: 2 }],
    ['cap', '2147483647', { requestCapPerHour: 2_147_483_647 }],
  ] as const)('a %s of %j alone is sent as a number, the other rate left out', async (field, typed, expected) => {
    api.getSourceSettingsVersions.mockResolvedValue(EMPTY_PAGE)
    const { create: createPanel } = stubWrites({ kind: 'created', detail: detail() })
    const form = mountCreate()
    await fill(form, 1)
    await byId(form, `create-${field}`).setValue(typed)
    await press(form)
    expect(createPanel).toHaveBeenCalledTimes(1)
    const sent = createPanel.mock.calls[0]![0]
    expect(sent).toMatchObject(expected)
    const other = field === 'ramp' ? 'requestCapPerHour' : 'rampDatasetsPerHour'
    expect(sent[other]).toBeUndefined()
  })

  it.each([
    ['ramp', '0', 'Give a whole number, 1 to 2147483647, or leave it empty for no limit.'],
    ['ramp', '2147483648', 'Give a whole number, 1 to 2147483647, or leave it empty for no limit.'],
    ['ramp', '-1', 'Give a whole number, 1 to 2147483647, or leave it empty for no limit.'],
    ['ramp', 'x', 'Give a whole number, 1 to 2147483647, or leave it empty for no limit.'],
    ['ramp', '1.5', 'Give a whole number, 1 to 2147483647, or leave it empty for no limit.'],
    ['cap', '0', 'Give a whole number, 2 to 2147483647, or leave it empty for no limit.'],
    ['cap', '1', 'Give a whole number, 2 to 2147483647, or leave it empty for no limit.'],
    ['cap', '2147483648', 'Give a whole number, 2 to 2147483647, or leave it empty for no limit.'],
    ['cap', '-1', 'Give a whole number, 2 to 2147483647, or leave it empty for no limit.'],
    ['cap', 'x', 'Give a whole number, 2 to 2147483647, or leave it empty for no limit.'],
    ['cap', '2.5', 'Give a whole number, 2 to 2147483647, or leave it empty for no limit.'],
  ] as const)('a %s of %j stops the create, marks it with its words and focuses it', async (field, typed, words) => {
    api.getSourceSettingsVersions.mockResolvedValue(EMPTY_PAGE)
    const { create: createPanel } = stubWrites({ kind: 'created', detail: detail() })
    const form = mountCreate()
    await fill(form, 1)
    await byId(form, `create-${field}`).setValue(typed)
    expect(byId(form, `create-${field}-error`).exists()).toBe(false)
    await press(form)
    expect(createPanel).not.toHaveBeenCalled()
    expect(byId(form, `create-${field}-error`).text()).toBe(words)
    expect(form.findAll('[aria-invalid="true"]').map((el) => el.attributes('id'))).toEqual([`create-${field}`])
    focused(form, `create-${field}`)
    // Emptied again, it is no limit: the mark goes and the create sends with the rate left out.
    await byId(form, `create-${field}`).setValue('')
    expect(byId(form, `create-${field}-error`).exists()).toBe(false)
    await press(form)
    expect(createPanel).toHaveBeenCalledTimes(1)
    expect(createPanel.mock.calls[0]![0][field === 'ramp' ? 'rampDatasetsPerHour' : 'requestCapPerHour']).toBeUndefined()
  })

  it('the rate hints: both say empty is no limit, the cap gives its minimum and why', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(EMPTY_PAGE)
    stubWrites()
    const form = mountCreate()
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    expect(byId(form, 'create-ramp-hint').text()).toBe('Leave empty for no limit.')
    expect(byId(form, 'create-cap-hint').text()).toBe('Leave empty for no limit. Minimum 2: each new panel dataset costs 2 requests.')
    expect(byId(form, 'create-ramp-hint').attributes('id')).toBe('create-ramp-hint')
    expect(byId(form, 'create-cap-hint').attributes('id')).toBe('create-cap-hint')
    expect(byId(form, 'create-ramp').attributes('aria-describedby')).toBe('create-ramp-hint')
    expect(byId(form, 'create-cap').attributes('aria-describedby')).toBe('create-cap-hint')
    expect(form.find('label[for="create-ramp"]').text()).toBe('New Datasets per Hour')
    expect(form.find('label[for="create-cap"]').text()).toBe('Requests per Hour')
  })

  it('window days, when given, are sent; a bad one stops the create', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(EMPTY_PAGE)
    const { create: createPanel } = stubWrites({ kind: 'created', detail: detail() })
    const form = mountCreate()
    await fill(form, 1)
    await byId(form, 'create-ramp').setValue('5')
    await byId(form, 'create-cap').setValue('100')
    await byId(form, 'create-pre').setValue('x')
    expect(byId(form, 'create-pre-error').exists()).toBe(false)
    await press(form)
    expect(createPanel).not.toHaveBeenCalled()
    expect(byId(form, 'create-pre-error').text()).toBe('Give a whole number, 0 to 2147483647.')
    expect(byId(form, 'create-post-error').exists()).toBe(false)
    focused(form, 'create-pre')
    await byId(form, 'create-pre').setValue('30')
    await byId(form, 'create-post').setValue('0')
    await press(form)
    expect(createPanel).toHaveBeenCalledTimes(1)
    expect(createPanel.mock.calls[0]![0]).toMatchObject({
      rampDatasetsPerHour: 5,
      requestCapPerHour: 100,
      corporateActionWindowPreDays: 30,
      corporateActionWindowPostDays: 0,
    })
  })

  it('a window-only create: the rates empty, the window days sent, the rates left out', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(EMPTY_PAGE)
    const { create: createPanel } = stubWrites({ kind: 'created', detail: detail() })
    const form = mountCreate()
    await fill(form, 1)
    await byId(form, 'create-pre').setValue('7')
    await press(form)
    expect(createPanel).toHaveBeenCalledTimes(1)
    const sent = createPanel.mock.calls[0]![0]
    expect(sent.corporateActionWindowPreDays).toBe(7)
    expect(sent.corporateActionWindowPostDays).toBeUndefined()
    expect(sent.rampDatasetsPerHour).toBeUndefined()
    expect(sent.requestCapPerHour).toBeUndefined()
  })

  it('a picked cadence, disabled and the filters are sent as chosen', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(HELD_PAGE)
    const { create: createPanel } = stubWrites({ kind: 'created', detail: detail() })
    const form = mount(PanelCreateForm)
    await fill(form, 1)
    await byId(form, 'create-enabled').setValue(false)
    await byId(form, 'create-cadence').findAll('option')[2]!.setValue()
    await byId(form, 'create-tradable').findAll('option')[1]!.setValue()
    await byId(form, 'create-allow').setValue('AAPL MSFT')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(createPanel.mock.calls[0]![0]).toMatchObject({
      enabled: false,
      cadence: PanelCadence.WEEKLY,
      tradable: true,
      allowSymbols: ['AAPL', 'MSFT'],
    })
  })

  it.each([
    ['name', 'Give the panel a name.'],
    ['source', 'Choose a source.'],
  ])('without a %s the create is refused in words once pressed and nothing is sent', async (missing, words) => {
    api.getSourceSettingsVersions.mockResolvedValue(HELD_PAGE)
    const { create: createPanel } = stubWrites()
    const form = mountCreate()
    if (missing !== 'name') await byId(form, 'create-name').setValue('n')
    if (missing !== 'source') await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    expect(form.findAll('[data-testid$="-error"]')).toHaveLength(0)
    expect(byId(form, 'create-submit').attributes('disabled')).toBeUndefined()
    await press(form)
    expect(createPanel).not.toHaveBeenCalled()
    expect(byId(form, `create-${missing}-error`).text()).toBe(words)
    expect(form.findAll('[data-testid$="-error"]')).toHaveLength(1)
    focused(form, `create-${missing}`)
    expect(byId(form, 'create-submit').attributes('disabled')).toBeUndefined()
    await press(form)
    expect(createPanel).not.toHaveBeenCalled()
  })

  it('while the source page is out the create waits; a failed read says so', async () => {
    let answer!: (value: unknown) => void
    api.getSourceSettingsVersions.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)))
    const { create: createPanel } = stubWrites()
    const form = mountCreate()
    await byId(form, 'create-name').setValue('n')
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    expect(byId(form, 'create-source-error').exists()).toBe(false)
    await press(form)
    expect(createPanel).not.toHaveBeenCalled()
    expect(byId(form, 'create-source-error').text()).toBe('Reading the source settings.')
    answer(EMPTY_PAGE)
    await flushPromises()
    expect(byId(form, 'create-ramp').exists()).toBe(true)
    expect(byId(form, 'create-source-error').exists()).toBe(false)

    api.getSourceSettingsVersions.mockRejectedValueOnce(new ApiError({ status: 500 }))
    await byId(form, 'create-source').findAll('option')[2]!.setValue()
    await flushPromises()
    expect(byId(form, 'create-source-error').text()).toBe('The source settings could not be read. Choose the source again.')
    expect(byId(form, 'create-ramp').exists()).toBe(false)
  })

  it('a late answer for a source no longer chosen is dropped', async () => {
    let first!: (value: unknown) => void
    api.getSourceSettingsVersions
      .mockImplementationOnce(() => new Promise((resolve) => (first = resolve)))
      .mockResolvedValueOnce(HELD_PAGE)
    stubWrites()
    const form = mount(PanelCreateForm)
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await byId(form, 'create-source').findAll('option')[2]!.setValue()
    await flushPromises()
    first(EMPTY_PAGE)
    await flushPromises()
    expect(byId(form, 'create-ramp').exists()).toBe(false)
  })

  it('a refusal is worded in place and the form stays open', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(HELD_PAGE)
    stubWrites({ kind: 'unavailable', error: { message: 'x', errorId: 'e-503' } })
    const form = mount(PanelCreateForm)
    await fill(form, 1)
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(byId(form, 'create-notice').text()).toContain('Run make migrate')
    expect(byId(form, 'create-notice').text()).toContain('Error ID: e-503')
    expect(form.emitted('close')).toBeUndefined()
  })

  it('Cancel closes without a write', async () => {
    const { create: createPanel } = stubWrites()
    const form = mount(PanelCreateForm)
    await byId(form, 'create-cancel').trigger('click')
    expect(form.emitted('close')).toHaveLength(1)
    expect(createPanel).not.toHaveBeenCalled()
  })
})

// tj-crl7pm, re-sequenced by tj-7zjov1.362.2 (owner ruling 2026-11-06: validate on click): every mandatory
// field carries a required marker, neutral on open. Nothing is marked until Create Panel is pressed; a press
// with gaps marks every empty or invalid field at once (aria-invalid, its own message linked by
// aria-describedby, the marker turned pn-required--marked), focuses the first in screen order and sends
// nothing. From then on the marks follow the fields live, each staying until its field is valid. The button
// is off only while a write is out; the summary line counts the marks and never says the button is off.
describe('PanelCreateForm: validated on click, every missing field marked at once', () => {
  const EMPTY_PAGE = create(PanelSourceSettingsVersionPageSchema)
  const HELD_PAGE = create(PanelSourceSettingsVersionPageSchema, { items: [create(PanelSourceSettingsVersionSchema, { versionNo: 1 })] })
  // tj-7zjov1.362.4, then ADR tj-2ayia2 D6 (owner ruling 2026-11-07): name and source are the only mandatory
  // fields in every source state. Ramp and cap show only while the source has no settings, optional (no
  // asterisk, no aria-required, empty never marked); a typed value out of range is marked like any other.
  // Size is gone and the reason is optional, so neither is mandatory nor ever marked.
  const MANDATORY = ['name', 'source'] as const
  const OPENED = {
    name: 'Give the panel a name.',
    source: 'Choose a source.',
  }
  const COUNT = 'Give a whole number, 0 to 2147483647.'
  const RAMP = 'Give a whole number, 1 to 2147483647, or leave it empty for no limit.'
  const CAP = 'Give a whole number, 2 to 2147483647, or leave it empty for no limit.'
  // The rate fields carry a hint, always linked; a mark puts its message first.
  const HINTED = new Set(['ramp', 'cap'])

  const marker = (form: VueWrapper, field: string) => form.find(`label[for="create-${field}"] .pn-required`)

  function expectOptional(form: VueWrapper, field: string): void {
    expect(marker(form, field).exists()).toBe(false)
    expect(form.find(`label[for="create-${field}"]`).text()).not.toContain('*')
    expect(byId(form, `create-${field}`).attributes('aria-required')).toBeUndefined()
  }

  function expectMarked(form: VueWrapper, field: string, words: string): void {
    const input = byId(form, `create-${field}`)
    expect(input.attributes('aria-invalid')).toBe('true')
    expect(input.attributes('aria-describedby')).toBe(
      HINTED.has(field) ? `create-${field}-error create-${field}-hint` : `create-${field}-error`,
    )
    const message = form.find(`#create-${field}-error`)
    expect(message.exists()).toBe(true)
    expect(message.attributes('data-testid')).toBe(`create-${field}-error`)
    expect(message.classes()).toContain('pn-field-error')
    expect(message.text()).toBe(words)
    // A required field's asterisk turns to the error colour with its mark (the window days carry none).
    if (marker(form, field).exists()) expect(marker(form, field).classes()).toContain('pn-required--marked')
  }

  function expectUnmarked(form: VueWrapper, field: string): void {
    const input = byId(form, `create-${field}`)
    expect(input.attributes('aria-invalid')).toBeUndefined()
    expect(input.attributes('aria-describedby')).toBe(HINTED.has(field) ? `create-${field}-hint` : undefined)
    expect(byId(form, `create-${field}-error`).exists()).toBe(false)
    if (marker(form, field).exists()) expect(marker(form, field).classes()).not.toContain('pn-required--marked')
  }

  function expectRequired(form: VueWrapper, field: string): void {
    expect(marker(form, field).exists()).toBe(true)
    expect(marker(form, field).text()).toBe('*')
    expect(marker(form, field).attributes('aria-hidden')).toBe('true')
    expect(byId(form, `create-${field}`).attributes('aria-required')).toBe('true')
  }

  /** The summary counts the marks; the button is never off for them, only while a write is out. */
  function expectSummary(form: VueWrapper, count: number): void {
    const summary = byId(form, 'create-problem')
    const submit = byId(form, 'create-submit')
    expect(submit.attributes('disabled')).toBeUndefined()
    if (count === 0) {
      expect(summary.exists()).toBe(false)
      expect(submit.attributes('aria-describedby')).toBeUndefined()
      expect(form.findAll('.pn-field-error')).toHaveLength(0)
      expect(form.findAll('[aria-invalid]')).toHaveLength(0)
      expect(form.findAll('.pn-required--marked')).toHaveLength(0)
      return
    }
    expect(summary.text()).toBe(
      count === 1
        ? '1 marked field needs a value before the panel can be created.'
        : `${count} marked fields need a value before the panel can be created.`,
    )
    expect(summary.attributes('id')).toBe('create-problem')
    expect(submit.attributes('aria-describedby')).toBe('create-problem')
    expect(form.findAll('.pn-field-error')).toHaveLength(count)
    expect(form.findAll('[aria-invalid="true"]')).toHaveLength(count)
  }

  it('on open nothing is marked, every required label has its asterisk in the neutral state, no summary, the button on', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(EMPTY_PAGE)
    const { create: createPanel } = stubWrites()
    const form = mountCreate()
    for (const field of MANDATORY) {
      expectRequired(form, field)
      expectUnmarked(form, field)
      expect(marker(form, field).classes()).toEqual(['pn-required'])
    }
    expectSummary(form, 0)
    expect(form.text()).toContain('Fields marked * are required.')
    // A source with no settings: its four fields appear optional, with no asterisk, unmarked, bad values
    // included, until the first press.
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    for (const field of ['ramp', 'cap', 'pre', 'post']) {
      expectOptional(form, field)
      expectUnmarked(form, field)
    }
    await byId(form, 'create-ramp').setValue('0')
    await byId(form, 'create-cap').setValue('1')
    await byId(form, 'create-pre').setValue('x')
    for (const field of ['ramp', 'cap', 'pre']) expectUnmarked(form, field)
    expectSummary(form, 0)
    expect(form.findAll('label .pn-required')).toHaveLength(2)
    expect(createPanel).not.toHaveBeenCalled()
  })

  it('the asterisk is a neutral text colour until marked, then the error colour (panels.css)', () => {
    // Vitest stubs imported CSS, so the stylesheet is read as text, as CoverageStrip.spec.ts reads its styles.
    const panelsCss = readFileSync(resolve(import.meta.dirname, 'panels.css'), 'utf8')
    expect(panelsCss).toContain('.pn-required')
    // The declarations of the one top-level rule whose selector list names exactly this selector.
    const rule = (selector: string) => {
      const blocks = panelsCss
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .split('}')
        .map((block) => block.split('{'))
        .filter((parts) => parts.length === 2 && parts[0]!.split(',').some((s) => s.trim() === selector))
      expect(blocks.map((parts) => parts[0]!.trim()), selector).toHaveLength(1)
      return blocks[0]![1]!
    }
    expect(rule('.pn-required')).toMatch(/color:\s*var\(--tj-text2\)/)
    expect(rule('.pn-required')).not.toContain('--tj-status-fail')
    expect(rule('.pn-required--marked')).toMatch(/color:\s*var\(--tj-status-fail\)/)
  })

  it('a click on an empty form marks the two at once, focuses the name and sends nothing', async () => {
    const { create: createPanel } = stubWrites()
    const form = mountCreate()
    await press(form)
    expect(createPanel).not.toHaveBeenCalled()
    for (const field of MANDATORY) {
      expectRequired(form, field)
      expectMarked(form, field, OPENED[field])
    }
    expectSummary(form, 2)
    expect(byId(form, 'create-problem').text()).toBe('2 marked fields need a value before the panel can be created.')
    focused(form, 'create-name')
  })

  it('the asterisks and aria-required are exactly name and source, on a source with settings and on one without', async () => {
    const starred = (form: VueWrapper) =>
      form.findAll('label .pn-required').map((star) => star.element.closest('label')!.getAttribute('for'))
    const ariaRequired = (form: VueWrapper) => form.findAll('[aria-required="true"]').map((el) => el.attributes('id'))
    api.getSourceSettingsVersions.mockResolvedValue(HELD_PAGE)
    stubWrites()
    const held = mountCreate()
    expect(starred(held)).toEqual(['create-name', 'create-source'])
    expect(ariaRequired(held)).toEqual(['create-name', 'create-source'])
    await byId(held, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    expect(starred(held)).toEqual(['create-name', 'create-source'])
    held.unmount()

    api.getSourceSettingsVersions.mockResolvedValue(EMPTY_PAGE)
    const empty = mountCreate()
    await byId(empty, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    expect(byId(empty, 'create-ramp').exists()).toBe(true)
    expect(starred(empty)).toEqual(['create-name', 'create-source'])
    expect(ariaRequired(empty)).toEqual(['create-name', 'create-source'])
    // Pressed with every rate field empty or bad: the set does not grow.
    await press(empty)
    expect(starred(empty)).toEqual(['create-name', 'create-source'])
    await byId(empty, 'create-ramp').setValue('0')
    expect(starred(empty)).toEqual(['create-name', 'create-source'])
    expect(ariaRequired(empty)).toEqual(['create-name', 'create-source'])
  })

  it('a source without settings: a click with only a source chosen marks the name alone, focus on the name', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(EMPTY_PAGE)
    const { create: createPanel } = stubWrites()
    const form = mountCreate()
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    await press(form)
    expect(createPanel).not.toHaveBeenCalled()
    expect(form.findAll('[aria-invalid="true"]').map((el) => el.attributes('id'))).toEqual(['create-name'])
    for (const field of ['ramp', 'cap', 'pre', 'post']) expectUnmarked(form, field)
    expectSummary(form, 1)
    focused(form, 'create-name')
  })

  it('Enter submits the same way: the form marks, focuses the first and sends nothing', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(HELD_PAGE)
    const { create: createPanel } = stubWrites()
    const form = mountCreate()
    await byId(form, 'create-name').setValue('n')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(createPanel).not.toHaveBeenCalled()
    expectSummary(form, 1)
    expect(byId(form, 'create-problem').text()).toBe('1 marked field needs a value before the panel can be created.')
    focused(form, 'create-source')
  })

  it('cadence, the optional filters and the reason carry no marker and are never marked, pressed or not', async () => {
    stubWrites()
    const form = mountCreate()
    for (const pressed of [false, true]) {
      if (pressed) await press(form)
      for (const field of ['cadence', 'allow', 'deny', 'exchange', 'reason']) {
        expect(marker(form, field).exists()).toBe(false)
        expect(byId(form, `create-${field}`).attributes('aria-required')).toBeUndefined()
        expectUnmarked(form, field)
      }
    }
  })

  it('after a press the marks follow live: fixing a field clears only its mark and the count follows', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(HELD_PAGE)
    const { create: createPanel } = stubWrites({ kind: 'created', detail: detail() })
    const form = mountCreate()
    await press(form)
    expectSummary(form, 2)
    await byId(form, 'create-name').setValue('n')
    expectUnmarked(form, 'name')
    expectMarked(form, 'source', OPENED.source)
    expectSummary(form, 1)
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    for (const field of MANDATORY) {
      expectUnmarked(form, field)
      expectRequired(form, field)
    }
    expectSummary(form, 0)
    // Still live with no second press: a field broken again is marked again at once.
    await byId(form, 'create-name').setValue('  ')
    expectMarked(form, 'name', OPENED.name)
    expectSummary(form, 1)
    expect(createPanel).not.toHaveBeenCalled()
    await byId(form, 'create-name').setValue('n')
    expectSummary(form, 0)
    await press(form)
    expect(createPanel).toHaveBeenCalledTimes(1)
  })

  it('a valid form sends exactly once on the first press, and nothing is marked', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(HELD_PAGE)
    const { create: createPanel } = stubWrites({ kind: 'invalid', error: { message: 'x', errorId: 'e-1' } })
    const form = mountCreate()
    await byId(form, 'create-name').setValue('n')
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    await press(form)
    expect(createPanel).toHaveBeenCalledTimes(1)
    expectSummary(form, 0)
  })

  it('while a write is out the button is off and a second submit sends nothing; it is back on after', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(HELD_PAGE)
    const { store, create: createPanel } = stubWrites()
    let finish!: (outcome: PanelWriteOutcome) => void
    createPanel.mockImplementation(() => {
      store.writing = true
      return new Promise<PanelWriteOutcome>((resolve) => (finish = resolve)).finally(() => (store.writing = false))
    })
    const form = mountCreate()
    expect(byId(form, 'create-submit').attributes('disabled')).toBeUndefined()
    await byId(form, 'create-name').setValue('n')
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    await press(form)
    expect(createPanel).toHaveBeenCalledTimes(1)
    expect(byId(form, 'create-submit').attributes('disabled')).toBeDefined()
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(createPanel).toHaveBeenCalledTimes(1)
    finish({ kind: 'invalid', error: { message: 'x', errorId: 'e-2' } })
    await flushPromises()
    expect(byId(form, 'create-submit').attributes('disabled')).toBeUndefined()
  })

  it.each([
    ['nothing filled', {}, 'create-name'],
    ['a name only', { name: 'n' }, 'create-source'],
    ['a name only, a reason typed', { name: 'n', reason: 'r' }, 'create-source'],
    ['no name, the source held', { source: 'held' }, 'create-name'],
    ['no name, the source empty and nothing else', { source: 'empty' }, 'create-name'],
    ['no name, the rest filled', { source: 'empty', ramp: '1', cap: '2', reason: 'r' }, 'create-name'],
    ['no name, a bad ramp and cap', { source: 'empty', ramp: '0', cap: '1' }, 'create-name'],
    ['a ramp of 0', { name: 'n', source: 'empty', ramp: '0' }, 'create-ramp'],
    ['a bad ramp and a bad cap', { name: 'n', source: 'empty', ramp: 'x', cap: '1' }, 'create-ramp'],
    ['a bad ramp, no reason, bad window days', { name: 'n', source: 'empty', ramp: '2147483648', pre: 'x', post: 'x' }, 'create-ramp'],
    ['the ramp empty, a cap of 1', { name: 'n', source: 'empty', cap: '1' }, 'create-cap'],
    ['a good ramp, a cap over the maximum', { name: 'n', source: 'empty', ramp: '1', cap: '2147483648' }, 'create-cap'],
    ['the rates empty, a bad pre day and no reason', { name: 'n', source: 'empty', pre: 'x' }, 'create-pre'],
    ['a bad post day only', { name: 'n', source: 'empty', ramp: '1', cap: '2', post: '-1', reason: 'r' }, 'create-post'],
  ] as const)('focus goes to the first invalid field in screen order: %s', async (_case, given: Record<string, string>, first) => {
    api.getSourceSettingsVersions.mockResolvedValue(given.source === 'empty' ? EMPTY_PAGE : HELD_PAGE)
    const { create: createPanel } = stubWrites()
    const form = mountCreate()
    if (given.name) await byId(form, 'create-name').setValue(given.name)
    if (given.source) {
      await byId(form, 'create-source').findAll('option')[1]!.setValue()
      await flushPromises()
    }
    for (const field of ['ramp', 'cap', 'pre', 'post', 'reason']) {
      if (given[field] !== undefined) await byId(form, `create-${field}`).setValue(given[field])
    }
    expect(document.activeElement).not.toBe(byId(form, first).element)
    await press(form)
    expect(createPanel).not.toHaveBeenCalled()
    focused(form, first)
    expect(form.find('[aria-invalid="true"]').element).toBe(byId(form, first).element)
  })

  it('a second press after a fix moves the focus to the next invalid field', async () => {
    stubWrites()
    const form = mountCreate()
    await press(form)
    focused(form, 'create-name')
    await byId(form, 'create-name').setValue('n')
    await press(form)
    focused(form, 'create-source')
  })

  it('a source with no settings version: ramp and cap empty are never marked; a typed bad one is, live, with its words', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(EMPTY_PAGE)
    const { create: createPanel } = stubWrites()
    const form = mountCreate()
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    // The name left empty keeps the create from sending, so the marks can be watched across presses.
    await press(form)
    expect(createPanel).not.toHaveBeenCalled()
    focused(form, 'create-name')
    for (const field of ['ramp', 'cap', 'pre', 'post']) {
      expectOptional(form, field)
      expectUnmarked(form, field)
    }
    expectSummary(form, 1)
    await byId(form, 'create-ramp').setValue('0')
    await byId(form, 'create-cap').setValue('1')
    expectMarked(form, 'ramp', RAMP)
    expectMarked(form, 'cap', CAP)
    expectOptional(form, 'ramp')
    expectOptional(form, 'cap')
    expectSummary(form, 3)
    await byId(form, 'create-ramp').setValue('1')
    expectUnmarked(form, 'ramp')
    expectMarked(form, 'cap', CAP)
    expectSummary(form, 2)
    await byId(form, 'create-cap').setValue('2')
    expectUnmarked(form, 'cap')
    expectSummary(form, 1)
    await byId(form, 'create-ramp').setValue('2147483648')
    expectMarked(form, 'ramp', RAMP)
    await byId(form, 'create-ramp').setValue('  ')
    expectUnmarked(form, 'ramp')
    await byId(form, 'create-cap').setValue('')
    expectUnmarked(form, 'cap')
    expectSummary(form, 1)
    await byId(form, 'create-pre').setValue('x')
    await byId(form, 'create-post').setValue('1.5')
    expectMarked(form, 'pre', COUNT)
    expectMarked(form, 'post', COUNT)
    expectSummary(form, 3)
    await byId(form, 'create-pre').setValue('')
    await byId(form, 'create-post').setValue('0')
    expectUnmarked(form, 'pre')
    expectUnmarked(form, 'post')
    expectSummary(form, 1)
    await byId(form, 'create-name').setValue('n')
    expectSummary(form, 0)
    await press(form)
    expect(createPanel).toHaveBeenCalledTimes(1)
  })

  it('a source with a settings version: no ramp, cap or window day field and nothing marked for them', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(HELD_PAGE)
    stubWrites()
    const form = mountCreate()
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    await press(form)
    for (const field of ['ramp', 'cap', 'pre', 'post']) {
      expect(byId(form, `create-${field}`).exists()).toBe(false)
      expect(byId(form, `create-${field}-error`).exists()).toBe(false)
    }
    // Only the name is left: no size and no reason to mark.
    expectSummary(form, 1)
    expectMarked(form, 'name', OPENED.name)
    focused(form, 'create-name')
  })

  it('a source-settings read still out, then failed, is shown on the Source field once pressed', async () => {
    let answer!: (value: unknown) => void
    api.getSourceSettingsVersions.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)))
    const { create: createPanel } = stubWrites()
    const form = mountCreate()
    await byId(form, 'create-name').setValue('n')
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    expectUnmarked(form, 'source')
    expectSummary(form, 0)
    await press(form)
    expect(createPanel).not.toHaveBeenCalled()
    expectMarked(form, 'source', 'Reading the source settings.')
    expectSummary(form, 1)
    focused(form, 'create-source')
    answer(HELD_PAGE)
    await flushPromises()
    expectUnmarked(form, 'source')
    expectSummary(form, 0)

    api.getSourceSettingsVersions.mockRejectedValueOnce(new ApiError({ status: 500 }))
    await byId(form, 'create-source').findAll('option')[2]!.setValue()
    await flushPromises()
    expectMarked(form, 'source', 'The source settings could not be read. Choose the source again.')
    expectSummary(form, 1)
  })

  it('a name the loaded list holds is marked on the name field only once pressed, like every other mark', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(HELD_PAGE)
    const { store, create: createPanel } = stubWrites()
    store.panels = [create(PanelListItemSchema, { name: 'Large caps' })]
    const form = mountCreate()
    await byId(form, 'create-name').setValue(' Large caps ')
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    expectUnmarked(form, 'name')
    expectSummary(form, 0)
    await press(form)
    expect(createPanel).not.toHaveBeenCalled()
    expectMarked(form, 'name', 'A panel named "Large caps" already exists. Choose another name.')
    expectSummary(form, 1)
    focused(form, 'create-name')
    await byId(form, 'create-name').setValue('Small caps')
    expectUnmarked(form, 'name')
    expectSummary(form, 0)
  })
})

// ---------------------------------------------------------------------------------------------------

describe('PanelSourceSettingsForm', () => {
  it('shows the source version and starts from its four numbers', () => {
    stubWrites()
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail() } })
    expect(form.text()).toContain('Shared by every panel on')
    expect(byId(form, 'source-version').text()).toMatch(/^Version 2 by operator; pending: no pass has run yet$/)
    expect(['source-ramp', 'source-cap', 'source-pre', 'source-post'].map((id) => (byId(form, id).element as HTMLInputElement).value)).toEqual([
      '5',
      '200',
      '90',
      '30',
    ])
  })

  it('Save sends the four numbers to the panel source, at the low edges, window days 0 included, with the trimmed reason', async () => {
    const { saveSource } = stubWrites()
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail() } })
    await byId(form, 'source-ramp').setValue(' 1 ')
    await byId(form, 'source-cap').setValue('2')
    await byId(form, 'source-pre').setValue('0')
    await byId(form, 'source-reason').setValue(' slow down ')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSource).toHaveBeenCalledWith(DataSource.ALPACA_API, {
      rampDatasetsPerHour: 1,
      requestCapPerHour: 2,
      corporateActionWindowPreDays: 0,
      corporateActionWindowPostDays: 30,
      reason: 'slow down',
    })
  })

  it('Save sends the int32 maximum for both rates', async () => {
    const { saveSource } = stubWrites()
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail() } })
    await byId(form, 'source-ramp').setValue('2147483647')
    await byId(form, 'source-cap').setValue('2147483647')
    await byId(form, 'source-post').setValue('2147483647')
    expect(byId(form, 'source-problem').exists()).toBe(false)
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSource.mock.calls[0]![1]).toMatchObject({
      rampDatasetsPerHour: 2_147_483_647,
      requestCapPerHour: 2_147_483_647,
      corporateActionWindowPostDays: 2_147_483_647,
    })
  })

  // ADR tj-2ayia2 D6: an emptied rate is no limit and is left out of the write; the other stays as typed.
  it.each([
    ['ramp', { rampDatasetsPerHour: undefined, requestCapPerHour: 200 }],
    ['cap', { rampDatasetsPerHour: 5, requestCapPerHour: undefined }],
  ] as const)('an emptied %s is no limit: Save stays on and sends it undefined', async (field, rates) => {
    const { saveSource } = stubWrites()
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail() } })
    await byId(form, `source-${field}`).setValue('   ')
    expect(byId(form, 'source-problem').exists()).toBe(false)
    expect(byId(form, 'source-save').attributes('disabled')).toBeUndefined()
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSource).toHaveBeenCalledTimes(1)
    const sent = saveSource.mock.calls[0]![1]
    expect(sent.rampDatasetsPerHour).toBe(rates.rampDatasetsPerHour)
    expect(sent.requestCapPerHour).toBe(rates.requestCapPerHour)
    expect(sent).toMatchObject({ corporateActionWindowPreDays: 90, corporateActionWindowPostDays: 30 })
  })

  it('both rates emptied: Save sends neither, the window days as they are', async () => {
    const { saveSource } = stubWrites()
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail() } })
    await byId(form, 'source-ramp').setValue('')
    await byId(form, 'source-cap').setValue('')
    await form.find('form').trigger('submit')
    await flushPromises()
    const sent = saveSource.mock.calls[0]![1]
    expect(sent.rampDatasetsPerHour).toBeUndefined()
    expect(sent.requestCapPerHour).toBeUndefined()
    expect(sent).toMatchObject({ corporateActionWindowPreDays: 90, corporateActionWindowPostDays: 30, reason: '' })
  })

  it('a saved version with no rates (no limit) starts with ramp and cap blank, the window days filled, Save on', async () => {
    const { saveSource } = stubWrites()
    const noLimit = detail()
    noLimit.currentSourceVersion!.settings = create(PanelSourceSettingsSchema, {
      corporateActionWindowPreDays: 90,
      corporateActionWindowPostDays: 0,
    })
    const form = mount(PanelSourceSettingsForm, { props: { detail: noLimit } })
    expect(['source-ramp', 'source-cap', 'source-pre', 'source-post'].map((id) => (byId(form, id).element as HTMLInputElement).value)).toEqual([
      '',
      '',
      '90',
      '0',
    ])
    expect(byId(form, 'source-problem').exists()).toBe(false)
    expect(byId(form, 'source-save').attributes('disabled')).toBeUndefined()
    await form.find('form').trigger('submit')
    await flushPromises()
    const sent = saveSource.mock.calls[0]![1]
    expect(sent.rampDatasetsPerHour).toBeUndefined()
    expect(sent.requestCapPerHour).toBeUndefined()
    expect(sent).toMatchObject({ corporateActionWindowPreDays: 90, corporateActionWindowPostDays: 0 })
  })

  it('a saved version with only a cap starts with the ramp blank and the cap filled', () => {
    stubWrites()
    const capOnly = detail()
    capOnly.currentSourceVersion!.settings = create(PanelSourceSettingsSchema, {
      requestCapPerHour: 2,
      corporateActionWindowPreDays: 1,
      corporateActionWindowPostDays: 1,
    })
    const form = mount(PanelSourceSettingsForm, { props: { detail: capOnly } })
    expect((byId(form, 'source-ramp').element as HTMLInputElement).value).toBe('')
    expect((byId(form, 'source-cap').element as HTMLInputElement).value).toBe('2')
  })

  it('the rate fields read optional: no asterisk, no aria-required; the hint says empty is no limit, the cap minimum and how to pause', () => {
    stubWrites()
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail() } })
    for (const field of ['ramp', 'cap']) {
      expect(form.find(`label[for="source-${field}"]`).text()).not.toContain('*')
      expect(byId(form, `source-${field}`).attributes('aria-required')).toBeUndefined()
    }
    expect(form.find('label[for="source-ramp"]').text()).toBe('New Datasets per Hour')
    expect(form.find('label[for="source-cap"]').text()).toBe('Requests per Hour')
    const hint = form.findAll('.pn-hint').map((p) => p.text().replace(/\s+/g, ' '))
    expect(hint).toContain(
      'Leave new datasets per hour or requests per hour empty for no limit. The requests minimum is 2: each new panel dataset costs 2 requests. To pause a panel, turn Enabled off. The corporate action days are the span before a dataset\'s start and after its end that actions are pulled for.',
    )
    expect(form.text()).not.toContain('pauses')
  })

  // Live validation (tj-crl7pm): the one-line problem and Save follow each keystroke, no press needed.
  it.each([
    ['ramp', '0', 'New datasets per hour: Give a whole number, 1 to 2147483647, or leave it empty for no limit.'],
    ['ramp', '2147483648', 'New datasets per hour: Give a whole number, 1 to 2147483647, or leave it empty for no limit.'],
    ['ramp', '-1', 'New datasets per hour: Give a whole number, 1 to 2147483647, or leave it empty for no limit.'],
    ['ramp', 'x', 'New datasets per hour: Give a whole number, 1 to 2147483647, or leave it empty for no limit.'],
    ['ramp', '1.5', 'New datasets per hour: Give a whole number, 1 to 2147483647, or leave it empty for no limit.'],
    ['cap', '0', 'Requests per hour: Give a whole number, 2 to 2147483647, or leave it empty for no limit.'],
    ['cap', '1', 'Requests per hour: Give a whole number, 2 to 2147483647, or leave it empty for no limit.'],
    ['cap', '2147483648', 'Requests per hour: Give a whole number, 2 to 2147483647, or leave it empty for no limit.'],
    ['cap', '-1', 'Requests per hour: Give a whole number, 2 to 2147483647, or leave it empty for no limit.'],
    ['cap', 'x', 'Requests per hour: Give a whole number, 2 to 2147483647, or leave it empty for no limit.'],
    ['cap', '2.5', 'Requests per hour: Give a whole number, 2 to 2147483647, or leave it empty for no limit.'],
  ] as const)('a %s of %j: the problem line says so, Save is off and a submit sends nothing; a fix turns Save on', async (field, typed, words) => {
    const { saveSource } = stubWrites()
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail() } })
    expect(byId(form, 'source-save').attributes('disabled')).toBeUndefined()
    await byId(form, `source-${field}`).setValue(typed)
    expect(byId(form, 'source-problem').text()).toBe(words)
    expect(byId(form, 'source-save').attributes('disabled')).toBeDefined()
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSource).not.toHaveBeenCalled()
    await byId(form, `source-${field}`).setValue('')
    expect(byId(form, 'source-problem').exists()).toBe(false)
    expect(byId(form, 'source-save').attributes('disabled')).toBeUndefined()
  })

  it('a bad ramp is named before a bad cap, and a bad cap before bad window days', async () => {
    stubWrites()
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail() } })
    await byId(form, 'source-ramp').setValue('0')
    await byId(form, 'source-cap').setValue('1')
    await byId(form, 'source-pre').setValue('x')
    expect(byId(form, 'source-problem').text()).toMatch(/^New datasets per hour: /)
    await byId(form, 'source-ramp').setValue('1')
    expect(byId(form, 'source-problem').text()).toMatch(/^Requests per hour: /)
    await byId(form, 'source-cap').setValue('2')
    expect(byId(form, 'source-problem').text()).toBe('Give each corporate action days field as a whole number, 0 to 2147483647.')
  })

  // tj-7zjov1.362.4: the reason is optional on a source settings save too.
  it.each([
    ['never typed', null],
    ['typed blank', ' \t '],
  ])('with no reason (%s) Save sends once, with reason \'\'', async (_label, typed) => {
    const { saveSource } = stubWrites()
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail() } })
    if (typed !== null) await byId(form, 'source-reason').setValue(typed)
    expect(byId(form, 'source-problem').exists()).toBe(false)
    expect(byId(form, 'source-save').attributes('disabled')).toBeUndefined()
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSource).toHaveBeenCalledTimes(1)
    expect(saveSource).toHaveBeenCalledWith(DataSource.ALPACA_API, {
      rampDatasetsPerHour: 5,
      requestCapPerHour: 200,
      corporateActionWindowPreDays: 90,
      corporateActionWindowPostDays: 30,
      reason: '',
    })
  })

  it('the reason field reads optional: no asterisk, no aria-required, the optional placeholder', () => {
    stubWrites()
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail() } })
    expect(form.find('label[for="source-reason"]').text()).toBe('Reason (optional)')
    expect(form.find('.pn-required').exists()).toBe(false)
    expect(byId(form, 'source-reason').attributes('aria-required')).toBeUndefined()
    expect(byId(form, 'source-reason').attributes('placeholder')).toBe('Optional: why the source settings change')
  })

  // The window days stay required on this form: empty, negative, text or over the maximum stops the save.
  it.each(['', ' ', '-1', 'x', '1.5', '2147483648'])('a window day of %j stops the save with the window words', async (bad) => {
    const { saveSource } = stubWrites()
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail() } })
    for (const field of ['pre', 'post']) {
      await byId(form, `source-${field}`).setValue(bad)
      expect(byId(form, 'source-problem').text()).toBe('Give each corporate action days field as a whole number, 0 to 2147483647.')
      expect(byId(form, 'source-save').attributes('disabled')).toBeDefined()
      await form.find('form').trigger('submit')
      await flushPromises()
      expect(saveSource).not.toHaveBeenCalled()
      await byId(form, `source-${field}`).setValue('0')
      expect(byId(form, 'source-problem').exists()).toBe(false)
    }
  })

  it('no source version: the fields start blank; the window days must be given, the rates need not be', async () => {
    const { saveSource } = stubWrites()
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail({ source: false }) } })
    expect(byId(form, 'source-version').exists()).toBe(false)
    expect(['source-ramp', 'source-cap', 'source-pre', 'source-post'].map((id) => (byId(form, id).element as HTMLInputElement).value)).toEqual([
      '',
      '',
      '',
      '',
    ])
    expect(byId(form, 'source-problem').text()).toBe('Give each corporate action days field as a whole number, 0 to 2147483647.')
    expect(byId(form, 'source-save').attributes('disabled')).toBeDefined()
    await byId(form, 'source-pre').setValue('90')
    expect(byId(form, 'source-save').attributes('disabled')).toBeDefined()
    await byId(form, 'source-post').setValue('30')
    expect(byId(form, 'source-problem').exists()).toBe(false)
    expect(byId(form, 'source-save').attributes('disabled')).toBeUndefined()
    await form.find('form').trigger('submit')
    await flushPromises()
    const sent = saveSource.mock.calls[0]![1]
    expect(sent.rampDatasetsPerHour).toBeUndefined()
    expect(sent.requestCapPerHour).toBeUndefined()
    expect(sent).toMatchObject({ corporateActionWindowPreDays: 90, corporateActionWindowPostDays: 30 })
  })

  it('a refusal is worded in place', async () => {
    stubWrites({ kind: 'not-found' })
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail() } })
    await byId(form, 'source-reason').setValue('r')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(byId(form, 'source-notice').text()).toBe('This panel no longer exists, so nothing was changed. Reload the page.')
  })
})

// ---------------------------------------------------------------------------------------------------

describe('PanelHistoryStrip', () => {
  const START = T0
  const END = T0 + 100 * DAY
  const history = (days: { day: string; state: AssetListDayState; changedCount: number }[]) =>
    create(AssetListHistorySchema, { start: timestampFromMs(START), end: timestampFromMs(END), days })
  const pass = create(EffectivePassSchema, { kind: PanelPassKind.RELEASE, startedAt: timestampFromMs(T0 + 50 * DAY) })
  const panelVersions = [create(PanelSettingsVersionSchema, { versionNo: 2, effectivePass: pass }), create(PanelSettingsVersionSchema, { versionNo: 3 })]
  const boundaries = create(PanelBoundariesSchema, {
    markers: [
      create(PanelMarkerSchema, { kind: PanelMarkerKind.PANEL_SETTINGS, versionNo: 2, at: timestampFromMs(T0 + 25 * DAY), actor: 'operator', reason: 'widen' }),
      create(PanelMarkerSchema, { kind: PanelMarkerKind.COLLECT_STOP, at: timestampFromMs(T0 + 40 * DAY), actor: 'operator', label: 'VFV' }),
      create(PanelMarkerSchema, { kind: PanelMarkerKind.PANEL_SETTINGS, versionNo: 3, at: timestampFromMs(T0 + 75 * DAY), actor: 'operator', reason: 'narrow' }),
    ],
  })

  function strip(days = [
    { day: '2026-10-28', state: AssetListDayState.SAVED, changedCount: 4 },
    { day: '2026-10-29', state: AssetListDayState.MISSING, changedCount: 0 },
    { day: '2026-10-30', state: AssetListDayState.EMPTY, changedCount: 0 },
    { day: '2026-10-31', state: AssetListDayState.SAVED, changedCount: 2 },
    { day: '2026-11-02', state: AssetListDayState.NOT_DUE, changedCount: 0 },
  ]) {
    return mount(PanelHistoryStrip, { props: { history: history(days), boundaries, panelVersions, sourceVersions: [] } })
  }

  it('one block per expected day, in the read order, worded by state', () => {
    const blocks = byId(strip(), 'history-days').findAll('[role="listitem"]')
    expect(blocks.map((b) => [b.attributes('data-day'), b.attributes('data-state')])).toEqual([
      ['2026-10-28', 'Saved'],
      ['2026-10-29', 'Missing'],
      ['2026-10-30', 'Empty'],
      ['2026-10-31', 'Saved'],
      ['2026-11-02', 'Not due'],
    ])
    expect(blocks[0]!.attributes('aria-label')).toBe('2026-10-28: Saved, 4 changed')
    expect(blocks[1]!.attributes('aria-label')).toBe('2026-10-29: Missing')
  })

  it('the change bar is the day count against the largest in the window', () => {
    const bars = byId(strip(), 'history-days').findAll('.day__bar')
    expect(bars.map((b) => b.attributes('style'))).toEqual(['height: 100%;', 'height: 0%;', 'height: 0%;', 'height: 50%;', 'height: 0%;'])
  })

  it('a window with no expected day says so', () => {
    expect(byId(strip([]), 'history-empty').text()).toBe('No expected days in this window.')
  })

  it('markers sit at their instant in the window; the pending one is dimmed', () => {
    const lane = byId(strip(), 'history-lane').findAll('[data-testid="history-marker"]')
    expect(lane.map((m) => [m.attributes('style'), m.classes('is-pending')])).toEqual([
      ['left: 25%;', false],
      ['left: 40%;', false],
      ['left: 75%;', true],
    ])
  })

  it('the reading list is newest first, with version, effective pass or Pending, actor and reason', () => {
    const rows = byId(strip(), 'history-markers').findAll('[data-testid="history-marker-row"]')
    expect(rows).toHaveLength(3)
    expect(rows[0]!.classes('is-pending')).toBe(true)
    expect(rows[0]!.text()).toContain('Panel settings version 3')
    expect(rows[0]!.text()).toContain('Pending: no pass has run yet')
    expect(rows[0]!.text()).toContain('By operator: narrow')
    expect(rows[1]!.text()).toContain('Collect stopped: VFV')
    expect(rows[1]!.text()).not.toContain('Pending')
    expect(rows[2]!.classes('is-pending')).toBe(false)
    expect(rows[2]!.text()).toContain('Panel settings version 2')
    expect(rows[2]!.text()).toMatch(/In effect from the release pass, /)
    expect(rows[2]!.text()).toContain('By operator: widen')
  })

  // tj-7zjov1.362.4: a version saved with no reason reads 'By <actor>' alone, no colon and no empty reason.
  it('a marker with an empty reason shows the actor alone', () => {
    const bare = create(PanelBoundariesSchema, {
      markers: [create(PanelMarkerSchema, { kind: PanelMarkerKind.PANEL_SETTINGS, versionNo: 2, at: timestampFromMs(T0 + 25 * DAY), actor: 'operator', reason: '' })],
    })
    const wrapper = mount(PanelHistoryStrip, { props: { history: history([]), boundaries: bare, panelVersions, sourceVersions: [] } })
    const row = byId(wrapper, 'history-markers').find('[data-testid="history-marker-row"]')
    expect(row.find('.marker__who').text()).toBe('By operator')
    expect(row.text()).not.toContain('By operator:')
  })

  it('no markers in the window: no lane, and the empty line', () => {
    const wrapper = mount(PanelHistoryStrip, {
      props: { history: history([]), boundaries: create(PanelBoundariesSchema), panelVersions: [], sourceVersions: [] },
    })
    expect(byId(wrapper, 'history-lane').exists()).toBe(false)
    expect(byId(wrapper, 'history-no-markers').text()).toBe('No settings changes in this window.')
    const unread = mount(PanelHistoryStrip, { props: { history: history([]), boundaries: null, panelVersions: [], sourceVersions: [] } })
    expect(byId(unread, 'history-no-markers').exists()).toBe(true)
  })

  it('is the aggregate view: no per-asset table', () => {
    expect(strip().find('table').exists()).toBe(false)
  })
})

// ---------------------------------------------------------------------------------------------------

describe('PanelMemberList', () => {
  const members = [
    create(PanelMemberSchema, {
      vendorAssetId: 'v1',
      symbol: 'AAPL',
      status: PanelTransition.ENTERED,
      datasetId: DATASET_ID,
      health: DatasetHealthStatus.LATE,
      lastBar: timestampFromMs(T0),
      appearedOn: '2026-08-03',
    }),
    create(PanelMemberSchema, { vendorAssetId: 'v2', symbol: 'NEWCO', status: PanelTransition.DEFERRED, cause: 'ramp reached' }),
  ]

  function list(hasMore = false) {
    return mount(PanelMemberList, { props: { members, hasMore, loadingMore: false }, global: { stubs: { RouterLink: RouterLinkStub } } })
  }

  it('a row per member: status, cause, health badge, last bar and the list days', () => {
    const rows = list().findAll('[data-testid="member-row"]')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.find('[data-cell="status"]').text()).toBe('Entered')
    expect(rows[0]!.find('[data-cell="health"]').text()).toBe('Late')
    expect(rows[0]!.text()).toContain('2026-08-03')
    expect(rows[1]!.find('[data-cell="status"]').text()).toBe('Deferred ramp reached')
  })

  it('a member without health (no dataset yet) shows Unknown, no link and no events button', () => {
    const row = list().findAll('[data-testid="member-row"]')[1]!
    expect(row.find('[data-cell="health"]').text()).toBe('Unknown')
    expect(row.findComponent(RouterLinkStub).exists()).toBe(false)
    expect(row.find('button').exists()).toBe(false)
  })

  it('a member with a dataset links to the Dataset detail', () => {
    const link = list().findAll('[data-testid="member-row"]')[0]!.findComponent(RouterLinkStub)
    expect(link.props('to')).toEqual({ name: 'data-dataset-detail', params: { id: DATASET_ID } })
  })

  it('Show Events opens the dataset corporate actions in place; Hide Events closes them', async () => {
    api.getCorporateActions.mockResolvedValue(create(CorporateActionEventPageSchema))
    const wrapper = list()
    const button = byId(wrapper, 'member-actions-AAPL')
    expect(button.text()).toBe('Show Events')
    expect(button.attributes('aria-expanded')).toBe('false')
    await button.trigger('click')
    await flushPromises()
    expect(byId(wrapper, 'member-actions-row').exists()).toBe(true)
    expect(api.getCorporateActions).toHaveBeenCalledWith(DATASET_ID, {})
    expect(byId(wrapper, 'member-actions-AAPL').text()).toBe('Hide Events')
    await byId(wrapper, 'member-actions-AAPL').trigger('click')
    expect(byId(wrapper, 'member-actions-row').exists()).toBe(false)
  })

  it('Load More shows only when there is more, and asks for it', async () => {
    expect(byId(list(), 'members-more').exists()).toBe(false)
    const wrapper = list(true)
    await byId(wrapper, 'members-more').trigger('click')
    expect(wrapper.emitted('more')).toHaveLength(1)
  })
})

// ---------------------------------------------------------------------------------------------------

describe('PanelCorporateActions', () => {
  const event = (id: string, type: string) =>
    create(CorporateActionEventSchema, { vendorActionId: id, actionType: type, exDate: timestampFromMs(T0), asOf: timestampFromMs(T0 + DAY) })

  it('reads the dataset events on mount and shows one row each, worded as the vendor sent them', async () => {
    api.getCorporateActions.mockResolvedValue(create(CorporateActionEventPageSchema, { items: [event('a1', 'forward_split'), event('a2', 'cash_dividend')] }))
    const wrapper = mount(PanelCorporateActions, { props: { datasetId: DATASET_ID } })
    expect(wrapper.text()).toContain('Loading corporate actions')
    await flushPromises()
    const rows = wrapper.findAll('[data-testid="corporate-action-row"]')
    expect(rows.map((r) => r.find('td').text())).toEqual(['forward_split', 'cash_dividend'])
    expect(byId(wrapper, 'corporate-actions-more').exists()).toBe(false)
  })

  it('no events: the empty line', async () => {
    api.getCorporateActions.mockResolvedValue(create(CorporateActionEventPageSchema))
    const wrapper = mount(PanelCorporateActions, { props: { datasetId: DATASET_ID } })
    await flushPromises()
    expect(byId(wrapper, 'corporate-actions-empty').text()).toBe('No corporate actions recorded for this dataset.')
  })

  it('Load More appends the next page by its cursor', async () => {
    api.getCorporateActions
      .mockResolvedValueOnce(create(CorporateActionEventPageSchema, { items: [event('a1', 'x')], nextCursor: 'c2' }))
      .mockResolvedValueOnce(create(CorporateActionEventPageSchema, { items: [event('a2', 'y')] }))
    const wrapper = mount(PanelCorporateActions, { props: { datasetId: DATASET_ID } })
    await flushPromises()
    await byId(wrapper, 'corporate-actions-more').trigger('click')
    await flushPromises()
    expect(api.getCorporateActions).toHaveBeenLastCalledWith(DATASET_ID, { cursor: 'c2' })
    expect(wrapper.findAll('[data-testid="corporate-action-row"]')).toHaveLength(2)
    expect(byId(wrapper, 'corporate-actions-more').exists()).toBe(false)
  })

  it('a failure is worded with its error id, and Try Again reads once more', async () => {
    api.getCorporateActions
      .mockRejectedValueOnce(new ApiError({ status: 500, errorId: 'e-ca' }))
      .mockResolvedValueOnce(create(CorporateActionEventPageSchema))
    const wrapper = mount(PanelCorporateActions, { props: { datasetId: DATASET_ID } })
    await flushPromises()
    expect(byId(wrapper, 'corporate-actions-error').text()).toContain('Error ID: e-ca')
    await byId(wrapper, 'corporate-actions-error').find('button').trigger('click')
    await flushPromises()
    expect(byId(wrapper, 'corporate-actions-error').exists()).toBe(false)
    expect(byId(wrapper, 'corporate-actions-empty').exists()).toBe(true)
  })
})
