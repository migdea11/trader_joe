// The Panels page's components (tj-7zjov1.37), each mounted alone over a real Pinia with the store's writes
// replaced and the typed client mocked at @/api:
// - PanelSettingsForm: Save Settings sends the form, Enable or Disable sends the SAVED settings with only
//   `enabled` flipped (unsaved edits are not smuggled in); both need a non-blank reason.
// - PanelCreateForm: cadence Monthly (default) sent UNSPECIFIED unless picked; the four source settings
//   fields only while the chosen source's versions page is empty (ramp and cap then required, window days
//   optional); a required reason.
// - PanelSourceSettingsForm: all four numbers sent, 0 included; a required reason.
// - PanelHistoryStrip: one block per expected day, the change bar, markers with version, pass, actor and
//   reason; a version no pass has run under is Pending (dimmed), not a boundary.
// - PanelMemberList and PanelCorporateActions: Unknown health for a member without a dataset, the events
//   view per dataset.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
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
  size: 40,
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
  size: 40,
  allowSymbols: ['AAPL', 'MSFT'],
  denySymbols: ['TSLA'],
  assetClass: AssetType.STOCK,
  exchange: 'NASDAQ',
  active: true,
  tradable: false,
}

function detail(overrides: { settings?: typeof SAVED; versionNo?: number; effective?: boolean; source?: boolean } = {}): PanelDetail {
  return create(PanelDetailSchema, {
    id: PANEL_ID,
    name: 'Large caps',
    source: DataSource.ALPACA_API,
    currentVersion: create(PanelSettingsVersionSchema, {
      versionNo: overrides.versionNo ?? 3,
      settings: overrides.settings ?? SAVED,
      actor: 'operator',
      reason: 'widen',
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
    expect(form.find('[data-fact="size"]').text()).toBe('40')
    expect(form.find('[data-fact="version"]').text()).toMatch(/^Version 3 saved .+ by operator$/)
    expect(form.find('[data-fact="effective"]').text()).toMatch(/^membership pass, /)
    expect(byId(form, 'current-reason').text()).toBe('Reason: widen')
    expect(byId(form, 'settings-toggle').text()).toBe('Disable Panel')
  })

  it('a version no pass has run under reads Pending', () => {
    stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail({ effective: false }) } })
    expect(form.find('[data-fact="effective"]').text()).toBe('Pending: no pass has run yet')
  })

  it('the form starts from the saved settings', () => {
    stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    expect((byId(form, 'settings-size').element as HTMLInputElement).value).toBe('40')
    expect((byId(form, 'settings-allow').element as HTMLInputElement).value).toBe('AAPL, MSFT')
    expect((byId(form, 'settings-exchange').element as HTMLInputElement).value).toBe('NASDAQ')
  })

  it('without a reason neither Save Settings nor Disable is allowed, and a click sends nothing', async () => {
    const { saveSettings } = stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    expect(byId(form, 'settings-problem').text()).toBe('Give a reason for the change.')
    expect(byId(form, 'settings-save').attributes('disabled')).toBeDefined()
    expect(byId(form, 'settings-toggle').attributes('disabled')).toBeDefined()
    await form.find('form').trigger('submit')
    await byId(form, 'settings-toggle').trigger('click')
    await byId(form, 'settings-reason').setValue('   ')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSettings).not.toHaveBeenCalled()
  })

  it('Disable sends the SAVED settings with only enabled flipped, ignoring unsaved edits, and the trimmed reason', async () => {
    const { saveSettings } = stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    await byId(form, 'settings-size').setValue('7')
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
    await byId(form, 'settings-size').setValue('12')
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
      size: 12,
      allowSymbols: [],
      denySymbols: ['TSLA'],
      assetClass: undefined,
      exchange: undefined,
      active: undefined,
      tradable: false,
      reason: 'narrow',
    })
  })

  it('a bad size stops Save Settings but not Disable', async () => {
    const { saveSettings } = stubWrites()
    const form = mount(PanelSettingsForm, { props: { detail: detail() } })
    await byId(form, 'settings-size').setValue('-3')
    await byId(form, 'settings-reason').setValue('r')
    expect(byId(form, 'settings-problem').text()).toBe('Give the size as a whole number, 0 or more.')
    expect(byId(form, 'settings-save').attributes('disabled')).toBeDefined()
    expect(byId(form, 'settings-toggle').attributes('disabled')).toBeUndefined()
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSettings).not.toHaveBeenCalled()
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
    await byId(form, 'settings-size').setValue('9')
    await byId(form, 'settings-reason').setValue('why')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect((byId(form, 'settings-reason').element as HTMLTextAreaElement).value).toBe('')
    expect(byId(form, 'settings-notice').exists()).toBe(false)
    await byId(form, 'settings-size').setValue('9')
    await form.setProps({ detail: detail({ versionNo: 4 }) })
    expect((byId(form, 'settings-size').element as HTMLInputElement).value).toBe('40')
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
// form's text round trip turned a 10-digit size into 0, trimmed the exchange, and dropped an empty-but-
// present exchange and a present UNSPECIFIED asset class: each a new settings version that changes
// membership on the next pass. Bodies are compared with toStrictEqual, so key presence counts.
describe('PanelSettingsForm: Enable or Disable sends the saved settings exactly', () => {
  const BARE = { cadence: PanelCadence.MONTHLY, size: 25, allowSymbols: ['VFV'], denySymbols: [] as string[] }

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
    ['size 2147483647 (int32 max)', { size: 2_147_483_647 }, { size: 2_147_483_647 }],
    ['size 1000000000', { size: 1_000_000_000 }, { size: 1_000_000_000 }],
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
      size: 2_147_483_647,
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
      size: 2_147_483_647,
      exchange: '',
      assetClass: AssetType.UNSPECIFIED,
      active: false,
      reason: 'resume',
    })
  })

  it('a saved 10-digit size: Save Settings is refused in words and sends nothing; Disable still sends it as saved', async () => {
    const { saveSettings } = stubWrites()
    const saved = create(PanelSettingsSchema, { enabled: true, ...BARE, size: 2_147_483_647 })
    const form = mount(PanelSettingsForm, { props: { detail: detail({ settings: saved }) } })
    await byId(form, 'settings-reason').setValue('why')
    expect(byId(form, 'settings-problem').text()).toBe('Give the size as a whole number, 0 or more.')
    expect(byId(form, 'settings-save').attributes('disabled')).toBeDefined()
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSettings).not.toHaveBeenCalled()
    await byId(form, 'settings-toggle').trigger('click')
    await flushPromises()
    expect(saveSettings).toHaveBeenCalledTimes(1)
    expect(saveSettings.mock.calls[0]![0]).toMatchObject({ enabled: false, size: 2_147_483_647 })
  })

  it('Save Settings still goes through the form: the exchange is trimmed, a blank one and UNSPECIFIED left out', async () => {
    const { saveSettings } = stubWrites()
    const saved = create(PanelSettingsSchema, { enabled: true, ...BARE, exchange: ' X ', assetClass: AssetType.UNSPECIFIED })
    const form = mount(PanelSettingsForm, { props: { detail: detail({ settings: saved }) } })
    await byId(form, 'settings-reason').setValue('edit')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSettings).toHaveBeenCalledTimes(1)
    expect(saveSettings.mock.calls[0]![0]).toMatchObject({ exchange: 'X', assetClass: undefined, size: 25 })

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
    await byId(form, 'create-size').setValue('50')
    await byId(form, 'create-reason').setValue('  first panel  ')
  }

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
      size: 50,
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

  it('a source with no settings version: ramp and cap required, window days optional and absent when blank', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(EMPTY_PAGE)
    const { create: createPanel } = stubWrites({ kind: 'created', detail: detail() })
    const form = mount(PanelCreateForm)
    await fill(form, 1)
    expect(byId(form, 'create-ramp').exists()).toBe(true)
    expect(byId(form, 'create-pre').exists()).toBe(true)
    expect(byId(form, 'create-problem').text()).toBe(
      'This source has no settings yet: give the new datasets per hour and the requests per hour.',
    )
    await byId(form, 'create-ramp').setValue('0')
    expect(byId(form, 'create-problem').exists()).toBe(true)
    await byId(form, 'create-cap').setValue('200')
    expect(byId(form, 'create-problem').exists()).toBe(false)
    await form.find('form').trigger('submit')
    await flushPromises()
    const sent = createPanel.mock.calls[0]![0]
    expect(sent).toMatchObject({ rampDatasetsPerHour: 0, requestCapPerHour: 200 })
    expect(sent.corporateActionWindowPreDays).toBeUndefined()
    expect(sent.corporateActionWindowPostDays).toBeUndefined()
  })

  it('window days, when given, are sent; a bad one stops the create', async () => {
    api.getSourceSettingsVersions.mockResolvedValue(EMPTY_PAGE)
    const { create: createPanel } = stubWrites({ kind: 'created', detail: detail() })
    const form = mount(PanelCreateForm)
    await fill(form, 1)
    await byId(form, 'create-ramp').setValue('5')
    await byId(form, 'create-cap').setValue('100')
    await byId(form, 'create-pre').setValue('x')
    expect(byId(form, 'create-problem').text()).toBe('Give each corporate action day count as a whole number.')
    await form.find('form').trigger('submit')
    expect(createPanel).not.toHaveBeenCalled()
    await byId(form, 'create-pre').setValue('30')
    await byId(form, 'create-post').setValue('0')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(createPanel.mock.calls[0]![0]).toMatchObject({
      rampDatasetsPerHour: 5,
      requestCapPerHour: 100,
      corporateActionWindowPreDays: 30,
      corporateActionWindowPostDays: 0,
    })
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
    ['reason', 'Give a reason for the change.'],
    ['size', 'Give the size as a whole number, 0 or more.'],
  ])('without a %s the create is refused in words and nothing is sent', async (missing, words) => {
    api.getSourceSettingsVersions.mockResolvedValue(HELD_PAGE)
    const { create: createPanel } = stubWrites()
    const form = mount(PanelCreateForm)
    if (missing !== 'name') await byId(form, 'create-name').setValue('n')
    if (missing !== 'source') await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    if (missing !== 'size') await byId(form, 'create-size').setValue('1')
    if (missing !== 'reason') await byId(form, 'create-reason').setValue('r')
    expect(byId(form, 'create-problem').text()).toBe(words)
    expect(byId(form, 'create-submit').attributes('disabled')).toBeDefined()
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(createPanel).not.toHaveBeenCalled()
  })

  it('while the source page is out the create waits; a failed read says so', async () => {
    let answer!: (value: unknown) => void
    api.getSourceSettingsVersions.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)))
    stubWrites()
    const form = mount(PanelCreateForm)
    await byId(form, 'create-name').setValue('n')
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    expect(byId(form, 'create-problem').text()).toBe('Reading the source settings.')
    answer(EMPTY_PAGE)
    await flushPromises()
    expect(byId(form, 'create-ramp').exists()).toBe(true)

    api.getSourceSettingsVersions.mockRejectedValueOnce(new ApiError({ status: 500 }))
    await byId(form, 'create-source').findAll('option')[2]!.setValue()
    await flushPromises()
    expect(byId(form, 'create-problem').text()).toBe('The source settings could not be read. Choose the source again.')
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

  it('Save sends all four numbers, 0 included, to the panel source, with the trimmed reason', async () => {
    const { saveSource } = stubWrites()
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail() } })
    await byId(form, 'source-ramp').setValue('0')
    await byId(form, 'source-reason').setValue(' pause creation ')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSource).toHaveBeenCalledWith(DataSource.ALPACA_API, {
      rampDatasetsPerHour: 0,
      requestCapPerHour: 200,
      corporateActionWindowPreDays: 90,
      corporateActionWindowPostDays: 30,
      reason: 'pause creation',
    })
  })

  it('a blank reason or a bad number stops the save', async () => {
    const { saveSource } = stubWrites()
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail() } })
    expect(byId(form, 'source-problem').text()).toBe('Give a reason for the change.')
    await form.find('form').trigger('submit')
    await byId(form, 'source-reason').setValue('r')
    await byId(form, 'source-cap').setValue('-1')
    expect(byId(form, 'source-problem').text()).toBe('Give each number as a whole number, 0 or more.')
    expect(byId(form, 'source-save').attributes('disabled')).toBeDefined()
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(saveSource).not.toHaveBeenCalled()
  })

  it('no source version: the fields start blank and must be given', () => {
    stubWrites()
    const form = mount(PanelSourceSettingsForm, { props: { detail: detail({ source: false }) } })
    expect(byId(form, 'source-version').exists()).toBe(false)
    expect((byId(form, 'source-ramp').element as HTMLInputElement).value).toBe('')
    expect(byId(form, 'source-problem').text()).toBe('Give each number as a whole number, 0 or more.')
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
