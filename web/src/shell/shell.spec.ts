import { readFileSync } from 'node:fs'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import type { Pinia } from 'pinia'
import PrimeVue from 'primevue/config'

import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'

import { ApiError } from '@/api'
import {
  MISSING,
  formatBytes,
  formatCompact,
  formatDate,
  formatDateTime,
  formatDays,
  formatDaysAgo,
  formatDuration,
  formatNumber,
  formatPercent,
  formatTime,
  timestampToInstant,
} from '@/format/formatters'
import { useFormatters } from '@/format/useFormatters'
import router from '@/router'
import { SETTINGS_STORAGE_KEY, useSettingsStore } from '@/stores/settings'
import { useTradingGroupStore } from '@/stores/tradingGroup'
import { useUiConfigStore } from '@/stores/uiConfig'
import { colors } from '@/theme/tokens'
import AppShell from './AppShell.vue'
import DataSubTabs from './DataSubTabs.vue'
import SettingsPanel from './SettingsPanel.vue'
import TopBar from './TopBar.vue'
import TradingGroupToggle from './TradingGroupToggle.vue'
import { useShellModals } from './useShellModals'

// tj-grna9p.30 phase 1 shell: the browser settings store, the formatter module, the trading-group
// toggle and its URL sync, the UiConfig fallbacks, and the sections and sub-tabs not built yet.
// The API is mocked at the typed client (@/api); everything below it is the real code.
const mocks = vi.hoisted(() => ({
  getUiConfig: vi.fn(),
  getDatasetFacets: vi.fn(),
  listDatasets: vi.fn(),
  // The Usage tab (tj-grna9p.62) loads these on mount; they stay pending here.
  getUsageSummary: vi.fn(),
  getLastReadBuckets: vi.fn(),
  getReadsPerDay: vi.fn(),
  getStaleDatasets: vi.fn(),
  getStorageCurrent: vi.fn(),
}))
// The shell opens the shared events socket on mount (tj-grna9p.76): replaced so no real WebSocket opens.
vi.mock('@/api/events', async (importOriginal) =>
  (await import('@/__tests__/fakeEvents')).fakeEventsModule(await importOriginal<object>()),
)
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...mocks,
}))

let pinia: Pinia
const mounted: VueWrapper[] = []

function config(groups: AccountGroup[], label = '', version = ''): void {
  mocks.getUiConfig.mockResolvedValue({ allowedGroups: groups, deploymentLabel: label, serverVersion: version })
}

function mountWith<T>(component: T): VueWrapper {
  const wrapper = mount(component as never, { global: { plugins: [pinia, router, PrimeVue] }, attachTo: document.body })
  mounted.push(wrapper)
  return wrapper
}

// Stands in for a browser with blocked site data: reading window.localStorage itself throws.
function blockStorage(): void {
  vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
    throw new DOMException('blocked', 'SecurityError')
  })
}

function browserZoneIs(zone: string): void {
  const real = Intl.DateTimeFormat.prototype.resolvedOptions
  vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions').mockImplementation(function (this: Intl.DateTimeFormat) {
    return { ...real.call(this), timeZone: zone }
  })
}

beforeEach(() => {
  window.localStorage.clear()
  pinia = createPinia()
  setActivePinia(pinia)
  config([AccountGroup.SIMULATION])
  // `clients` is always present on a decoded DatasetFacets (a repeated field); the Usage sidebar reads it.
  mocks.getDatasetFacets.mockResolvedValue({ all: 0n, needsAttention: 0n, sources: [], updateTypes: [], statuses: [], clients: [] })
  for (const usage of [mocks.getUsageSummary, mocks.getLastReadBuckets, mocks.getReadsPerDay, mocks.getStaleDatasets, mocks.getStorageCurrent]) {
    usage.mockReturnValue(new Promise(() => undefined))
  }
  mocks.listDatasets.mockResolvedValue({ items: [], nextCursor: '' })
})

afterEach(() => {
  mounted.splice(0).forEach((wrapper) => wrapper.unmount())
  useShellModals().closeSettings()
  useShellModals().closeAbout()
  vi.restoreAllMocks()
})

describe('settings store', () => {
  // The stale threshold left Settings (tj-grna9p.88, owner ruling tj-utqd77 Q3): the store holds the
  // time zone override only, and an object saved by an older build still loads.
  it('defaults to the browser zone with nothing stored, and holds no stale threshold', () => {
    browserZoneIs('America/Toronto')
    const settings = useSettingsStore()
    expect(settings.timeZoneOverride).toBeNull()
    expect(settings.timeZone).toBe('America/Toronto')
    expect(settings.storageOk).toBe(true)
    expect(Object.keys(settings.$state).sort()).toEqual(['storageOk', 'timeZoneOverride'])
    expect('staleDays' in settings).toBe(false)
    expect('setStaleDays' in settings).toBe(false)
  })

  it('a saved override survives a reload (a new store over the same storage), stored without stale days', () => {
    const first = useSettingsStore()
    expect(first.setTimeZoneOverride('Asia/Tokyo')).toBe(true)
    expect(JSON.parse(window.localStorage.getItem(SETTINGS_STORAGE_KEY)!)).toEqual({ v: 1, timeZone: 'Asia/Tokyo' })

    setActivePinia(createPinia())
    const reloaded = useSettingsStore()
    expect(reloaded.timeZoneOverride).toBe('Asia/Tokyo')
    expect(reloaded.timeZone).toBe('Asia/Tokyo')
  })

  it('Use Browser Zone clears the override and persists that', () => {
    browserZoneIs('America/Toronto')
    const settings = useSettingsStore()
    settings.setTimeZoneOverride('Europe/London')
    settings.useBrowserZone()
    expect(settings.timeZone).toBe('America/Toronto')
    setActivePinia(createPinia())
    expect(useSettingsStore().timeZoneOverride).toBeNull()
  })

  it('refuses an unknown zone and keeps the current one', () => {
    const settings = useSettingsStore()
    settings.setTimeZoneOverride('Asia/Tokyo')
    expect(settings.setTimeZoneOverride('Mars/Olympus')).toBe(false)
    expect(settings.setTimeZoneOverride('')).toBe(false)
    expect(settings.timeZoneOverride).toBe('Asia/Tokyo')
  })

  it.each([30, 0, 3651, -5, 2.5, '30', null])(
    'an older saved object with staleDays %j still loads its zone, and the next write drops the key',
    (old) => {
      window.localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify({ v: 1, timeZone: 'Asia/Tokyo', staleDays: old }))
      const settings = useSettingsStore()
      expect(settings.timeZoneOverride).toBe('Asia/Tokyo')
      expect(settings.storageOk).toBe(true)
      expect(settings.setTimeZoneOverride('Europe/London')).toBe(true)
      expect(JSON.parse(window.localStorage.getItem(SETTINGS_STORAGE_KEY)!)).toEqual({ v: 1, timeZone: 'Europe/London' })
    },
  )

  it.each([
    ['unreadable JSON', '{nope'],
    ['a JSON scalar', '42'],
    ['another version', JSON.stringify({ v: 2, timeZone: 'Asia/Tokyo', staleDays: 30 })],
    ['an unknown zone', JSON.stringify({ v: 1, timeZone: 'Mars/Olympus', staleDays: 30 })],
  ])('%s falls back to the browser zone', (_name, raw) => {
    window.localStorage.setItem(SETTINGS_STORAGE_KEY, raw)
    const settings = useSettingsStore()
    expect(settings.timeZoneOverride).toBeNull()
    expect(settings.storageOk).toBe(true)
  })

  it('with storage throwing: browser zone, changes kept in memory, storage flagged', () => {
    browserZoneIs('America/Toronto')
    blockStorage()
    const settings = useSettingsStore()
    expect(settings.timeZone).toBe('America/Toronto')
    expect(settings.storageOk).toBe(false)
    expect(settings.setTimeZoneOverride('Asia/Tokyo')).toBe(true)
    expect(settings.timeZone).toBe('Asia/Tokyo')
    expect(settings.storageOk).toBe(false)
  })

  it('with getItem and setItem throwing, the store still works in memory', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('quota')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    const settings = useSettingsStore()
    expect(settings.storageOk).toBe(false)
    expect(settings.setTimeZoneOverride('Asia/Tokyo')).toBe(true)
    expect(settings.timeZoneOverride).toBe('Asia/Tokyo')
    expect(settings.storageOk).toBe(false)
  })

  it('a write that fails after a good read flips storageOk, and a later good write restores it', () => {
    const settings = useSettingsStore()
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementationOnce(() => {
      throw new Error('quota')
    })
    settings.setTimeZoneOverride('Asia/Tokyo')
    expect(settings.storageOk).toBe(false)
    settings.setTimeZoneOverride('Europe/London')
    expect(setItem).toHaveBeenCalledTimes(2)
    expect(settings.storageOk).toBe(true)
  })

  it('with storage throwing, the shell renders in the browser zone and the panel shows the quiet hint', async () => {
    browserZoneIs('America/Toronto')
    blockStorage()
    await router.push('/data/datasets')
    const shell = mountWith(AppShell)
    await flushPromises()
    expect(shell.find('[data-testid="time-zone-select"]').text()).toContain('America/Toronto')
    useShellModals().openSettings()
    await flushPromises()
    expect(document.body.querySelector('[data-testid="storage-hint"]')!.textContent).toContain(
      'settings last only until you reload',
    )
  })

  it('the panel says Saved in this browser when storage works', async () => {
    useShellModals().openSettings()
    mountWith(SettingsPanel)
    await flushPromises()
    expect(document.body.querySelector('[data-testid="storage-hint"]')!.textContent!.trim()).toBe('Saved in this browser')
  })

  // tj-grna9p.93 (owner rulings tj-utqd77 Q3 and the Read By scope): the stale threshold is a fixed
  // constant and Read By is a screen filter, so the Settings panel offers neither.
  it('the panel has no stale threshold control and no Read By control: the time zone is its only setting', async () => {
    useShellModals().openSettings()
    mountWith(SettingsPanel)
    await flushPromises()
    const panel = document.body.querySelector('.settings')!
    expect(panel.textContent).toContain('Time Zone')
    expect(panel.textContent).not.toMatch(/stale|read by|\bSDK\b/i)
    expect(panel.querySelectorAll('input[type="number"]')).toHaveLength(0)
    expect([...panel.querySelectorAll('input[type="checkbox"]')]).toHaveLength(0)
  })
})

describe('formatters, as on the Visual language page', () => {
  const at = (iso: string) => Date.parse(iso)

  it('dates are YYYY/MM/DD and times HH:mm or HH:mm:ss', () => {
    expect(formatDate(at('2026-10-06T12:00:00Z'), 'UTC')).toBe('2026/10/06')
    expect(formatTime(at('2026-10-06T08:12:00Z'), 'UTC')).toBe('08:12')
    expect(formatTime(at('2026-10-06T06:10:04Z'), 'UTC', { seconds: true })).toBe('06:10:04')
    expect(formatDateTime(at('2026-10-06T00:05:00Z'), 'UTC')).toBe('2026/10/06 00:05')
    expect(formatDateTime(new Date(at('2026-10-06T23:59:59Z')), 'UTC', { seconds: true })).toBe('2026/10/06 23:59:59')
  })

  it('the zone moves the calendar day', () => {
    expect(formatDateTime(at('2026-10-06T03:59:00Z'), 'America/Toronto')).toBe('2026/10/05 23:59')
    expect(formatDateTime(at('2026-10-06T15:00:00Z'), 'Asia/Tokyo')).toBe('2026/10/07 00:00')
  })

  it('numbers, bytes, durations, compact counts, percents and ages', () => {
    expect(formatNumber(1944)).toBe('1,944')
    expect(formatNumber(132.389, 2)).toBe('132.39')
    expect(formatBytes(412e6)).toBe('412 MB')
    expect(formatBytes(18.4e9)).toBe('18.4 GB')
    expect(formatBytes(999)).toBe('999 B')
    expect(formatDuration(3800)).toBe('3.8 s')
    expect(formatDuration(38000)).toBe('38 s')
    expect(formatDuration(125000)).toBe('2 min 5 s')
    expect(formatDuration(450)).toBe('450 ms')
    expect(formatCompact(84000)).toBe('84k')
    expect(formatCompact(155000)).toBe('155k')
    expect(formatCompact(950)).toBe('950')
    expect(formatPercent(0.61)).toBe('61%')
    expect(formatDaysAgo(1, 'short')).toBe('1 d ago')
    expect(formatDaysAgo(97)).toBe('97 days ago')
    expect(formatDaysAgo(1)).toBe('1 day ago')
    expect(formatDays(97)).toBe('97 d')
  })

  it('a missing or impossible value is the dash', () => {
    expect(MISSING).toBe('—')
    expect(formatDate(Number.NaN, 'UTC')).toBe(MISSING)
    expect(formatDate(0, 'Mars/Olympus')).toBe(MISSING)
    expect(formatNumber(null)).toBe(MISSING)
    expect(formatBytes(-1)).toBe(MISSING)
    expect(formatDuration(undefined)).toBe(MISSING)
    expect(formatPercent(Number.POSITIVE_INFINITY)).toBe(MISSING)
  })

  it('a protobuf Timestamp becomes epoch milliseconds', () => {
    expect(timestampToInstant({ seconds: 1_780_000_000n, nanos: 999_999_999 })).toBe(1_780_000_000_999)
    expect(timestampToInstant({ seconds: 5 })).toBe(5000)
  })

  it('a fixture time renders in America/Toronto by default and in the override after setting it', () => {
    browserZoneIs('America/Toronto')
    const formatters = useFormatters()
    const fixture = at('2026-10-06T12:30:00Z')
    expect(formatters.dateTime(fixture)).toBe('2026/10/06 08:30')
    useSettingsStore().setTimeZoneOverride('Asia/Tokyo')
    expect(formatters.dateTime(fixture)).toBe('2026/10/06 21:30')
    expect(formatters.date(fixture)).toBe('2026/10/06')
    expect(formatters.time(fixture, { seconds: true })).toBe('21:30:00')
  })
})

describe('UiConfig store fallbacks', () => {
  it.each([
    ['', null],
    ['  ', null],
    ['prod', null],
    ['Production', null],
    [' PROD ', null],
    ['dev', 'dev'],
    [' staging ', 'staging'],
  ])('deployment label %j shows tag %j', async (label, tag) => {
    config([AccountGroup.SIMULATION], label)
    const store = useUiConfigStore()
    await store.load()
    expect(store.deploymentTag).toBe(tag)
  })

  it('an empty server version reads Unknown', async () => {
    config([AccountGroup.SIMULATION], '', '  ')
    const store = useUiConfigStore()
    await store.load()
    expect(store.serverVersionText).toBe('Unknown')
    config([AccountGroup.SIMULATION], '', '0.4.0')
    await store.load()
    expect(store.serverVersionText).toBe('0.4.0')
  })

  it('UNSPECIFIED and values this build does not know are never allowed', async () => {
    config([AccountGroup.UNSPECIFIED, 99 as AccountGroup, AccountGroup.PAPER])
    const store = useUiConfigStore()
    await store.load()
    expect(store.allowedGroups).toEqual(['ACCOUNT_GROUP_PAPER'])
  })

  it('a failed load allows nothing (fails closed) and reports error', async () => {
    config([AccountGroup.SIMULATION, AccountGroup.PAPER])
    const store = useUiConfigStore()
    await store.load()
    mocks.getUiConfig.mockRejectedValue(new ApiError({ status: 503 }))
    await store.load()
    expect(store.status).toBe('error')
    expect(store.allowedGroups).toEqual([])
  })

  it('a second load cancels the first, and the first answer is dropped', async () => {
    let resolveFirst: (value: unknown) => void = () => undefined
    mocks.getUiConfig.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFirst = resolve
        }),
    )
    config([AccountGroup.PAPER], 'second')
    const store = useUiConfigStore()
    const first = store.load()
    const firstSignal = mocks.getUiConfig.mock.calls[0]![0] as AbortSignal
    await store.load()
    expect(firstSignal.aborted).toBe(true)
    resolveFirst({ allowedGroups: [AccountGroup.LIVE], deploymentLabel: 'first', serverVersion: '' })
    await first
    expect(store.deploymentLabel).toBe('second')
    expect(store.allowedGroups).toEqual(['ACCOUNT_GROUP_PAPER'])
  })
})

describe('trading group toggle', () => {
  async function toggleWith(groups: AccountGroup[]): Promise<VueWrapper> {
    config(groups)
    await useUiConfigStore().load()
    const wrapper = mountWith(TradingGroupToggle)
    await flushPromises()
    return wrapper
  }

  it('with config [SIMULATION, PAPER] Live renders greyed in place, third, never removed', async () => {
    const wrapper = await toggleWith([AccountGroup.SIMULATION, AccountGroup.PAPER])
    const segments = wrapper.findAll('button')
    expect(segments.map((s) => s.attributes('data-group'))).toEqual(['simulation', 'paper', 'live'])
    expect(segments.map((s) => s.attributes('aria-disabled'))).toEqual(['false', 'false', 'true'])
    expect(segments[2]!.classes()).toContain('is-disabled')
    expect(segments[2]!.attributes('title')).toBe('Not available on this deployment')
  })

  it('a disallowed segment ignores a click', async () => {
    const wrapper = await toggleWith([AccountGroup.SIMULATION])
    await wrapper.find('[data-group="paper"]').trigger('click')
    expect(useTradingGroupStore().selected).toBe('simulation')
    expect(wrapper.find('[data-group="simulation"]').attributes('aria-pressed')).toBe('true')
  })

  it('the selected segment is the accent fill with white text', async () => {
    const wrapper = await toggleWith([AccountGroup.SIMULATION, AccountGroup.PAPER])
    await wrapper.find('[data-group="paper"]').trigger('click')
    const style = wrapper.find('[data-group="paper"]').attributes('style')
    expect(style).toContain('background: rgb(110, 75, 200)')
    expect(style).toContain('color: rgb(255, 255, 255)')
    expect(wrapper.find('[data-group="simulation"]').attributes('style')).toBeUndefined()
  })

  it('Live selected is orange with DARK text', async () => {
    const wrapper = await toggleWith([AccountGroup.SIMULATION, AccountGroup.LIVE])
    await wrapper.find('[data-group="live"]').trigger('click')
    const style = wrapper.find('[data-group="live"]').attributes('style')
    expect(colors.group.live).toBe('#f0883e')
    expect(style).toContain('background: rgb(240, 136, 62)')
    expect(style).toContain('color: rgb(13, 17, 23)')
  })

  it('with the config failed every segment is greyed and none selected', async () => {
    mocks.getUiConfig.mockRejectedValue(new ApiError({ status: 0 }))
    await useUiConfigStore().load()
    const wrapper = mountWith(TradingGroupToggle)
    expect(wrapper.findAll('button').map((s) => s.attributes('aria-disabled'))).toEqual(['true', 'true', 'true'])
    expect(wrapper.findAll('[aria-pressed="true"]')).toHaveLength(0)
  })
})

describe('trading group URL sync', () => {
  async function shellAt(path: string, groups: AccountGroup[]): Promise<VueWrapper> {
    config(groups)
    await router.push(path)
    await router.isReady()
    const shell = mountWith(AppShell)
    await flushPromises()
    return shell
  }

  it('an allowed ?group in the URL is selected', async () => {
    await shellAt('/data/datasets?group=paper', [AccountGroup.SIMULATION, AccountGroup.PAPER])
    expect(useTradingGroupStore().selected).toBe('paper')
    expect(router.currentRoute.value.query.group).toBe('paper')
  })

  it.each(['live', 'bogus'])('a disallowed or unknown ?group=%s is corrected to the group in effect', async (value) => {
    await shellAt(`/data/datasets?group=${value}`, [AccountGroup.SIMULATION, AccountGroup.PAPER])
    expect(useTradingGroupStore().selected).toBe('simulation')
    expect(router.currentRoute.value.query.group).toBe('simulation')
  })

  it('a missing ?group is filled in once the config loads', async () => {
    await shellAt('/data/datasets', [AccountGroup.PAPER])
    expect(router.currentRoute.value.query.group).toBe('paper')
  })

  it('selecting a group writes it to the URL', async () => {
    const shell = await shellAt('/data/datasets', [AccountGroup.SIMULATION, AccountGroup.PAPER])
    await shell.find('[data-group="paper"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query.group).toBe('paper')
  })

  it('the group travels across a navigation that does not name one; other parameters do not', async () => {
    await shellAt('/data/datasets?group=paper&status=failed', [AccountGroup.SIMULATION, AccountGroup.PAPER])
    await router.push('/data/requests')
    await flushPromises()
    expect(router.currentRoute.value.query).toEqual({ group: 'paper' })
  })
})

describe('sections and sub-tabs not built yet', () => {
  it('the five unbuilt sections are disabled in place with the tooltip, and only Data links', async () => {
    await router.push('/data/datasets')
    const bar = mountWith(TopBar)
    const sections = bar.findAll('.top-bar__section')
    expect(sections.map((s) => s.text())).toEqual(['Overview', 'Data', 'Strategies', 'Portfolio', 'Orders', 'Ops'])
    const disabled = sections.filter((s) => s.attributes('aria-disabled') === 'true')
    expect(disabled.map((s) => s.text())).toEqual(['Overview', 'Strategies', 'Portfolio', 'Orders', 'Ops'])
    for (const section of disabled) {
      expect(section.element.tagName).toBe('BUTTON')
      expect(section.attributes('title')).toBe('Not built yet')
      expect(section.attributes('href')).toBeUndefined()
    }
    const data = sections[1]!
    expect(data.attributes('href')).toBe('/data/datasets')
    expect(data.attributes('aria-current')).toBe('page')
  })

  it('clicking a disabled section or sub-tab does not navigate', async () => {
    await router.push('/data/datasets')
    const bar = mountWith(TopBar)
    const tabs = mountWith(DataSubTabs)
    await bar.findAll('.top-bar__section.is-disabled')[0]!.trigger('click')
    await tabs.findAll('.sub-tabs__tab.is-disabled')[0]!.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/data/datasets')
  })

  // tj-grna9p.62 built the Usage tab: it links now; Requests and Health stay disabled in place.
  it('Requests and Health are disabled in place; Usage links; Datasets links and stays active under the Viewer', async () => {
    await router.push('/data/datasets/abc/viewer')
    const tabs = mountWith(DataSubTabs)
    const all = tabs.findAll('.sub-tabs__tab')
    expect(all.map((t) => t.text())).toEqual(['Datasets', 'Requests', 'Health', 'Usage'])
    expect(all.slice(1, 3).map((t) => [t.attributes('aria-disabled'), t.attributes('title')])).toEqual([
      ['true', 'Not built yet'],
      ['true', 'Not built yet'],
    ])
    const usage = all[3]!
    expect(usage.attributes('aria-disabled')).toBeUndefined()
    expect(usage.attributes('title')).toBeUndefined()
    expect(usage.attributes('href')).toBe('/data/usage')
    expect(usage.attributes('aria-current')).toBeUndefined()
    expect(all[0]!.attributes('aria-current')).toBe('page')
  })

  it('the Usage tab is the current page on /data/usage, and following it from Datasets opens it', async () => {
    await router.push('/data/datasets')
    const tabs = mountWith(DataSubTabs)
    await tabs.findAll('.sub-tabs__tab')[3]!.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('data-usage')
    expect(tabs.findAll('.sub-tabs__tab')[3]!.attributes('aria-current')).toBe('page')
  })

  it('a typed URL /data/usage shows the Usage screen and its sidebar, not the placeholder', async () => {
    await router.push('/data/usage')
    const shell = mountWith(AppShell)
    await flushPromises()
    expect(shell.find('main h1').text()).toBe('Usage')
    expect(shell.find('main').text()).not.toContain('Not built yet')
    expect(shell.findAll('aside.app-shell__sidebar').at(-1)!.find('[data-testid="read-by"]').exists()).toBe(true)
  })

  it.each([
    ['/data/requests', 'Requests'],
    ['/data/health', 'Health'],
  ])('a typed URL %s shows the placeholder titled %s', async (path, title) => {
    await router.push(path)
    const shell = mountWith(AppShell)
    await flushPromises()
    expect(shell.find('main h1').text()).toBe(title)
    expect(shell.find('main').text()).toContain('Not built yet')
    // The sidebar column is present but empty on a route that declares no sidebar.
    expect(shell.find('aside.app-shell__sidebar').exists()).toBe(true)
    expect(shell.find('aside.app-shell__sidebar').text()).toBe('')
  })

  it('the root and /data land on Datasets', async () => {
    await router.push('/')
    expect(router.currentRoute.value.path).toBe('/data/datasets')
    await router.push('/data')
    expect(router.currentRoute.value.path).toBe('/data/datasets')
  })
})

describe('browser tab title (tj-sww0b1 item 2)', () => {
  it('is TraderJoe', async () => {
    const { readFileSync } = await import('node:fs')
    const html = readFileSync(`${import.meta.dirname}/../../index.html`, 'utf8')
    expect(/<title>([^<]*)<\/title>/.exec(html)?.[1]).toBe('TraderJoe')
  })
})

// tj-grna9p.110 (owner ruling 2026-10-11, direction 2026-10-12): the top bar shows the mark, then the
// product name TraderJoe in the logo's two colours, "Trader" in the text colour and "Joe" in the
// accent tint. The colours are scoped CSS, which Vitest does not process, so they are read from
// TopBar.vue on disk; the structure is read from the rendered shell.
describe('top bar product name (tj-grna9p.110)', () => {
  async function shellAt(): Promise<VueWrapper> {
    await router.push('/data/datasets')
    const shell = mountWith(AppShell)
    await flushPromises()
    return shell
  }

  function rule(selector: string): string {
    const source = readFileSync(`${import.meta.dirname}/TopBar.vue`, 'utf8')
    const match = new RegExp(`\\n${selector.replace(/\./g, '\\.')}\\s*\\{([^}]*)\\}`).exec(source)
    expect(match, `${selector} rule`).not.toBeNull()
    return match![1]!
  }

  it('reads exactly TraderJoe, as two adjacent spans Trader and Joe with nothing between them', async () => {
    const shell = await shellAt()
    const name = shell.find('.top-bar__name')
    expect(name.element.textContent).toBe('TraderJoe')
    const nodes = [...name.element.childNodes]
    // No whitespace (or any other) text node between or around the two spans.
    expect(nodes.map((n) => n.nodeType)).toEqual([Node.ELEMENT_NODE, Node.ELEMENT_NODE])
    const [trader, joe] = nodes as HTMLElement[]
    expect(trader!.tagName).toBe('SPAN')
    expect(joe!.tagName).toBe('SPAN')
    expect(trader!.textContent).toBe('Trader')
    expect(joe!.textContent).toBe('Joe')
    expect(trader!.classList.contains('top-bar__name-trader')).toBe(true)
    expect(joe!.classList.contains('top-bar__name-joe')).toBe(true)
    // Nothing hides the name from assistive technology: it is the visible accessible name.
    expect(name.attributes('aria-hidden')).toBeUndefined()
    expect(name.attributes('aria-label')).toBeUndefined()
  })

  it('Trader is in the text colour and Joe in the accent tint, the logo\'s two colours', () => {
    expect(rule('.top-bar__name-trader')).toMatch(/(^|[;\s])color:\s*var\(--tj-text\)\s*;/)
    expect(rule('.top-bar__name-joe')).toMatch(/(^|[;\s])color:\s*var\(--tj-accent-tint\)\s*;/)
  })

  it('the mark comes first, then the name, in the brand block', async () => {
    const shell = await shellAt()
    const brand = shell.find('.top-bar__brand')
    const children = [...brand.element.children]
    expect(children.map((c) => c.getAttribute('class'))).toEqual([
      expect.stringContaining('brand-mark'),
      'top-bar__name',
    ])
    expect(children[0]!.tagName.toLowerCase()).toBe('svg')
  })

  it('no trader_joe anywhere the user can see or hear: the rendered shell, its About modal, the tab title', async () => {
    const shell = await shellAt()
    expect(shell.html()).not.toContain('trader_joe')
    expect(shell.text()).toContain('TraderJoe')
    useShellModals().openAbout()
    await flushPromises()
    // The About and Credits modal is open (its TradingView notice is there), and it is clean too.
    expect(document.body.textContent).toContain('TradingView')
    expect(document.body.innerHTML).not.toContain('trader_joe')
    expect(document.title).not.toContain('trader_joe')
  })
})

describe('Settings and About', () => {
  it('the top bar Settings button opens the panel, and its link opens About and Credits', async () => {
    await router.push('/data/datasets')
    const shell = mountWith(AppShell)
    await flushPromises()
    await shell.find('button[aria-label="Settings"]').trigger('click')
    await flushPromises()
    expect(useShellModals().settingsOpen.value).toBe(true)
    const about = [...document.body.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'About and Credits')
    expect(about).toBeDefined()
    about!.click()
    await flushPromises()
    expect(useShellModals().settingsOpen.value).toBe(false)
    expect(useShellModals().aboutOpen.value).toBe(true)
    expect(document.body.textContent).toContain('TradingView')
  })

  it('Use Browser Zone in the panel is disabled until an override exists', async () => {
    useShellModals().openSettings()
    mountWith(SettingsPanel)
    await flushPromises()
    const reset = () =>
      [...document.body.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Use Browser Zone')!
    expect(reset().disabled).toBe(true)
    useSettingsStore().setTimeZoneOverride('Asia/Tokyo')
    await flushPromises()
    expect(reset().disabled).toBe(false)
    reset().click()
    await flushPromises()
    expect(useSettingsStore().timeZoneOverride).toBeNull()
  })
})
