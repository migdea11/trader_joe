// The Panels page (web/src/views/panels/PanelListView.vue, tj-7zjov1.37) over the real store, with the
// typed client mocked at @/api: the selector, First Pass Pending, the counts tiles, the settings and source
// cards, the history strip with its markers, the member list, the empty and error states, New Panel, the
// re-read on Refresh and on an events reconnect. Then a naming pin over every new Panels file: the word is
// panel (no world or universe, ADR tj-26jt9w addendum 20). The dataset health naming (the old word for
// dataset state never appears) is pinned for every production file under web/src, these included, by
// theme/healthNaming.spec.ts, whose guard also keeps that word out of specs; it is not repeated here.
import { readdirSync, readFileSync } from 'node:fs'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { create } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'
import { createPinia, setActivePinia } from 'pinia'

import { DataSource } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { DatasetHealthStatus } from '@generated/trader_joe/proto/ui/v1/data_pb'
import {
  AssetListDayState,
  AssetListHistorySchema,
  EffectivePassSchema,
  PanelBoundariesSchema,
  PanelCadence,
  PanelDetailSchema,
  PanelListItemSchema,
  PanelListSchema,
  PanelMarkerKind,
  PanelMarkerSchema,
  PanelMemberPageSchema,
  PanelMemberSchema,
  PanelMembershipCountsSchema,
  PanelPassKind,
  PanelSettingsSchema,
  PanelSettingsVersionPageSchema,
  PanelSettingsVersionSchema,
  PanelSourceSettingsVersionPageSchema,
  PanelTransition,
} from '@generated/trader_joe/proto/ui/v1/panels_pb'

import { ApiError } from '@/api'
import { useFooterSummary } from '@/shell/useFooterSummary'
import { reloadScreens } from '@/shell/useEventsReload'

import PanelListView from './PanelListView.vue'

vi.mock('@/api/events', async (importOriginal) =>
  (await import('@/__tests__/fakeEvents')).fakeEventsModule(await importOriginal<object>()),
)
const api = vi.hoisted(() => ({
  listPanels: vi.fn(),
  getPanel: vi.fn(),
  getPanelSettingsVersions: vi.fn(),
  getSourceSettingsVersions: vi.fn(),
  getAssetListHistory: vi.fn(),
  getPanelBoundaries: vi.fn(),
  getPanelMembers: vi.fn(),
  getCorporateActions: vi.fn(),
  createPanel: vi.fn(),
  savePanelSettings: vi.fn(),
  saveSourceSettings: vi.fn(),
}))
vi.mock('@/api', async (importOriginal) => ({ ...(await importOriginal<typeof import('@/api')>()), ...api }))

const A = '0f8fad5b-d9cb-469f-a165-70867728950e'
const B = '7c9e6679-7425-40de-944b-e07fc1f90ae7'
const DATASET_ID = '16fd2706-8baf-433b-82eb-8c7fada847da'
const DAY = 86_400_000
const T0 = Date.UTC(2026, 7, 3)

const pass = create(EffectivePassSchema, { kind: PanelPassKind.MEMBERSHIP, startedAt: timestampFromMs(T0 + DAY) })

function detailOf(id: string, effective = true) {
  return create(PanelDetailSchema, {
    id,
    name: id === A ? 'Large caps' : 'Small caps',
    source: DataSource.ALPACA_API,
    currentVersion: create(PanelSettingsVersionSchema, {
      versionNo: 2,
      settings: create(PanelSettingsSchema, { enabled: true, cadence: PanelCadence.MONTHLY }),
      actor: 'operator',
      reason: 'first',
      effectivePass: effective ? pass : undefined,
    }),
    counts: create(PanelMembershipCountsSchema, { members: 1234n, collecting: 1200n, draining: 3n, lagging: 0n }),
  })
}

const HISTORY = create(AssetListHistorySchema, {
  start: timestampFromMs(T0),
  end: timestampFromMs(T0 + 100 * DAY),
  days: [{ day: '2026-10-30', state: AssetListDayState.SAVED, changedCount: 3 }],
})
const BOUNDARIES = create(PanelBoundariesSchema, {
  markers: [create(PanelMarkerSchema, { kind: PanelMarkerKind.PANEL_SETTINGS, versionNo: 2, at: timestampFromMs(T0 + 10 * DAY), actor: 'operator', reason: 'first' })],
})
const MEMBERS = create(PanelMemberPageSchema, {
  items: [
    create(PanelMemberSchema, { vendorAssetId: 'v1', symbol: 'AAPL', status: PanelTransition.ENTERED, datasetId: DATASET_ID, health: DatasetHealthStatus.FRESH }),
    create(PanelMemberSchema, { vendorAssetId: 'v2', symbol: 'NEWCO', status: PanelTransition.CREATED }),
  ],
  nextCursor: '',
})

function answerEveryRead(): void {
  api.listPanels.mockResolvedValue(
    create(PanelListSchema, {
      items: [
        create(PanelListItemSchema, { id: A, name: 'Large caps', source: DataSource.ALPACA_API, enabled: true }),
        create(PanelListItemSchema, { id: B, name: 'Small caps', source: DataSource.ALPACA_API, enabled: false }),
      ],
    }),
  )
  api.getPanel.mockImplementation(async (id: string) => detailOf(id))
  api.getPanelSettingsVersions.mockResolvedValue(create(PanelSettingsVersionPageSchema, { items: [create(PanelSettingsVersionSchema, { versionNo: 2, effectivePass: pass })] }))
  api.getSourceSettingsVersions.mockResolvedValue(create(PanelSourceSettingsVersionPageSchema))
  api.getAssetListHistory.mockResolvedValue(HISTORY)
  api.getPanelBoundaries.mockResolvedValue(BOUNDARIES)
  api.getPanelMembers.mockResolvedValue(MEMBERS)
}

const mounted: VueWrapper[] = []

async function page(): Promise<VueWrapper> {
  const wrapper = mount(PanelListView, { global: { stubs: { RouterLink: RouterLinkStub } } })
  mounted.push(wrapper)
  await flushPromises()
  return wrapper
}

const byId = (wrapper: VueWrapper, id: string) => wrapper.find(`[data-testid="${id}"]`)

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  for (const fn of Object.values(api)) fn.mockReset()
  answerEveryRead()
})

afterEach(() => {
  mounted.splice(0).forEach((wrapper) => wrapper.unmount())
})

describe('PanelListView', () => {
  it('the title is the tab name, Panels', async () => {
    expect((await page()).find('h1').text()).toBe('Panels')
  })

  it('the selector lists every panel with its source and a disabled one marked; the first is selected and shown', async () => {
    const view = await page()
    const options = byId(view, 'panel-select').findAll('option')
    expect(options.map((o) => o.text())).toEqual(['Large caps · Alpaca', 'Small caps · Alpaca (disabled)'])
    expect((byId(view, 'panel-select').element as HTMLSelectElement).value).toBe(A)
    expect(view.find('.panels__name').text()).toContain('Large caps')
  })

  it('the counts tiles show the server counts', async () => {
    const tiles = byId(await page(), 'panel-tiles').findAll('.tile')
    expect(tiles.map((t) => [t.attributes('data-tile'), t.find('.tile__label').text(), t.find('.tile__value').text()])).toEqual([
      ['members', 'Members', '1,234'],
      ['collecting', 'Collecting', '1,200'],
      ['draining', 'Draining', '3'],
      ['lagging', 'Lagging', '0'],
    ])
  })

  it('shows the settings card, the source card, the history strip with its marker and the members', async () => {
    const view = await page()
    expect(byId(view, 'panel-settings').exists()).toBe(true)
    expect(byId(view, 'source-settings').exists()).toBe(true)
    expect(byId(view, 'history-days').findAll('[role="listitem"]')).toHaveLength(1)
    expect(byId(view, 'history-marker-row').text()).toContain('Panel settings version 2')
    expect(byId(view, 'history-marker-row').classes('is-pending')).toBe(false)
    const rows = view.findAll('[data-testid="member-row"]')
    expect(rows.map((r) => r.find('[data-cell="health"]').text())).toEqual(['Healthy', 'Unknown'])
    expect(byId(view, 'first-pass-pending').exists()).toBe(false)
  })

  it('a panel whose current version has no effective pass reads First Pass Pending, and its empty member list says why', async () => {
    api.getPanel.mockImplementation(async (id: string) => detailOf(id, false))
    api.getPanelMembers.mockResolvedValue(create(PanelMemberPageSchema))
    const view = await page()
    expect(byId(view, 'first-pass-pending').text()).toMatch(/^First pass pending\./)
    expect(byId(view, 'members-empty').text()).toBe('No members yet: the first pass has not run.')
  })

  it('a panel with no members after a pass says so', async () => {
    api.getPanelMembers.mockResolvedValue(create(PanelMemberPageSchema))
    expect(byId(await page(), 'members-empty').text()).toBe('This panel has no members.')
  })

  it('choosing another panel in the selector reads it', async () => {
    const view = await page()
    await byId(view, 'panel-select').findAll('option')[1]!.setValue()
    await flushPromises()
    expect(api.getPanel).toHaveBeenLastCalledWith(B, expect.any(AbortSignal))
    expect(view.find('.panels__name').text()).toContain('Small caps')
  })

  it('no panels: the empty line, the selector disabled, no panel shown', async () => {
    api.listPanels.mockResolvedValue(create(PanelListSchema))
    const view = await page()
    expect(byId(view, 'panels-empty').text()).toBe('There are no panels yet. Choose New Panel to create one.')
    expect(byId(view, 'panel-select').attributes('disabled')).toBeDefined()
    expect(byId(view, 'panel-settings').exists()).toBe(false)
  })

  it('a failed list read is worded with its error id, and the footer names the problem', async () => {
    api.listPanels.mockRejectedValue(new ApiError({ status: 500, errorId: 'e-list' }))
    const view = await page()
    expect(byId(view, 'panels-error').text()).toContain('Could not load the panels.')
    expect(byId(view, 'panels-error').text()).toContain('Error ID: e-list')
    expect(useFooterSummary().footerProblem.value).toContain('(error id e-list)')
  })

  it('a failed detail read is worded in place', async () => {
    api.getPanel.mockRejectedValue(new ApiError({ status: 404, errorId: 'e-404' }))
    const view = await page()
    expect(byId(view, 'detail-error').text()).toContain('Could not load the panel.')
    expect(byId(view, 'detail-error').text()).toContain('Error ID: e-404')
  })

  it.each([
    ['history', 'getAssetListHistory', 'history-error'],
    ['markers', 'getPanelBoundaries', 'history-error'],
    ['members', 'getPanelMembers', 'members-error'],
    ['versions', 'getPanelSettingsVersions', 'versions-error'],
  ] as const)('a failed %s read is worded in its own section, the rest of the page up', async (_section, read, notice) => {
    api[read].mockRejectedValue(new ApiError({ status: 500 }))
    const view = await page()
    expect(byId(view, notice).exists()).toBe(true)
    expect(byId(view, 'panel-settings').exists()).toBe(true)
    expect(byId(view, 'panel-tiles').exists()).toBe(true)
  })

  it('New Panel opens the create form; Cancel closes it', async () => {
    const view = await page()
    expect(byId(view, 'panel-create').exists()).toBe(false)
    await byId(view, 'panel-new').trigger('click')
    expect(byId(view, 'panel-create').exists()).toBe(true)
    expect(byId(view, 'panel-new').attributes('disabled')).toBeDefined()
    await byId(view, 'create-cancel').trigger('click')
    expect(byId(view, 'panel-create').exists()).toBe(false)
  })

  // tj-7zjov1.362.4: name and source are enough; no reason typed reaches the client as ''.
  it('a create posts once, closes the form and shows the new panel', async () => {
    const C = '9b2f7d4e-1c3a-4e5f-8a6b-7c8d9e0f1a2b'
    const created = create(PanelDetailSchema, { id: C, name: 'Mid caps', source: DataSource.ALPACA_API, currentVersion: { versionNo: 1, settings: { enabled: true } } })
    api.createPanel.mockResolvedValue(created)
    const view = await page()
    api.listPanels.mockResolvedValue(create(PanelListSchema, { items: [create(PanelListItemSchema, { id: C, name: 'Mid caps', source: DataSource.ALPACA_API })] }))
    api.getPanel.mockResolvedValue(created)
    api.getSourceSettingsVersions.mockResolvedValue(create(PanelSourceSettingsVersionPageSchema, { items: [{ versionNo: 1 }] }))
    await byId(view, 'panel-new').trigger('click')
    await byId(view, 'create-name').setValue('Mid caps')
    await byId(view, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    await view.find('[data-testid="panel-create"] form').trigger('submit')
    await flushPromises()
    expect(api.createPanel).toHaveBeenCalledTimes(1)
    expect(api.createPanel.mock.calls[0]![0]).toMatchObject({ name: 'Mid caps', source: DataSource.ALPACA_API, reason: '' })
    expect(Object.hasOwn(api.createPanel.mock.calls[0]![0] as object, 'size')).toBe(false)
    expect(byId(view, 'panel-create').exists()).toBe(false)
    expect(view.find('.panels__name').text()).toContain('Mid caps')
    expect(byId(view, 'first-pass-pending').exists()).toBe(true)
  })

  it('Disable through the page posts the saved settings with enabled false, then re-reads', async () => {
    api.savePanelSettings.mockResolvedValue(create(PanelSettingsVersionSchema, { versionNo: 3 }))
    const view = await page()
    api.getPanel.mockClear()
    await byId(view, 'settings-reason').setValue('pause')
    await byId(view, 'settings-toggle').trigger('click')
    await flushPromises()
    expect(api.savePanelSettings).toHaveBeenCalledWith(A, {
      enabled: false,
      cadence: PanelCadence.MONTHLY,
      allowSymbols: [],
      denySymbols: [],
      assetClass: undefined,
      exchange: undefined,
      active: undefined,
      tradable: undefined,
      includeOtc: false,
      reason: 'pause',
    })
    expect(api.getPanel).toHaveBeenCalledWith(A, expect.any(AbortSignal))
  })

  it('Refresh and an events reconnect each re-read the list and the panel; nothing polls', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval'] })
    try {
      const view = await page()
      expect(api.listPanels).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(10 * 60_000)
      expect(api.listPanels).toHaveBeenCalledTimes(1)
      await byId(view, 'panels-refresh').trigger('click')
      await flushPromises()
      expect(api.listPanels).toHaveBeenCalledTimes(2)
      reloadScreens()
      await flushPromises()
      expect(api.listPanels).toHaveBeenCalledTimes(3)
      expect(api.getPanel).toHaveBeenCalledTimes(3)
    } finally {
      vi.useRealTimers()
    }
  })

  it('the footer names the load time and clears on leaving', async () => {
    const view = await page()
    const footer = useFooterSummary()
    expect(footer.footerUpdatedAt.value).not.toBeNull()
    expect(footer.footerProblem.value).toBeNull()
    view.unmount()
    mounted.splice(mounted.indexOf(view), 1)
    expect(footer.footerUpdatedAt.value).toBeNull()
  })
})

// ---------------------------------------------------------------------------------------------------
// NAMING (ADR tj-26jt9w addendum 20 and the dataset health rename, tj-7zjov1.204)

const WEB_SRC = `${import.meta.dirname}/../../`
const NEW_FILES = [
  ...readdirSync(`${WEB_SRC}components/panels`)
    .filter((name) => !name.endsWith('.spec.ts'))
    .map((name) => `components/panels/${name}`),
  'views/panels/PanelListView.vue',
  'stores/panels.ts',
  'api/panels.ts',
]

describe('the Panels files are named and worded as ruled', () => {
  it('scans the eleven production files of the tab', () => {
    expect(NEW_FILES.sort()).toEqual(
      [
        'api/panels.ts',
        'components/panels/PanelCorporateActions.vue',
        'components/panels/PanelCreateForm.vue',
        'components/panels/PanelFilterFields.vue',
        'components/panels/PanelHistoryStrip.vue',
        'components/panels/PanelMemberList.vue',
        'components/panels/PanelSettingsForm.vue',
        'components/panels/PanelSourceSettingsForm.vue',
        'components/panels/panelModel.ts',
        'components/panels/panels.css',
        'stores/panels.ts',
        'views/panels/PanelListView.vue',
      ].sort(),
    )
  })

  // Code, strings and markup; comments aside (panelModel.ts's header names the retired words to rule them
  // out, which is no string a user sees). The rendered page is checked whole below.
  const withoutComments = (text: string): string =>
    text
      .replace(/<!--[\s\S]*?-->/g, '')
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .split('\n')
      .map((line) => line.replace(/(^|\s)\/\/.*$/, ''))
      .join('\n')

  it.each(NEW_FILES)('%s holds no world or universe string', (file) => {
    const text = withoutComments(readFileSync(`${WEB_SRC}${file}`, 'utf-8'))
    expect(text.match(/world|universe/gi) ?? []).toEqual([])
  })

  it('the comment stripping keeps code: a string naming the word is still caught', () => {
    expect(withoutComments("const a = 'Universe' // world\n/* universe */\n<!-- world -->")).toBe("const a = 'Universe'\n\n")
  })

  it('no new component is a bare Panel.vue: each carries a distinguishing name', () => {
    const vue = NEW_FILES.filter((f) => f.endsWith('.vue')).map((f) => f.split('/').at(-1)!)
    expect(vue).not.toContain('Panel.vue')
    for (const name of vue) expect(name, name).toMatch(/^Panel[A-Z][A-Za-z]+\.vue$/)
  })

  it('the rendered page, create form open, holds no world or universe word and no market name', async () => {
    const view = await page()
    await byId(view, 'panel-new').trigger('click')
    expect(view.html()).not.toMatch(/world|universe/i)
    expect(view.text()).not.toMatch(/\b(Canada|Canadian|TSX|NYSE|NASDAQ|XNYS)\b/)
  })
})
