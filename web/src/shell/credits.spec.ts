import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'

import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'

import { ApiError } from '@/api'
import { CHART_CREDITS, ECHARTS_NOTICE, LIGHTWEIGHT_CHARTS_NOTICE, TRADINGVIEW_URL } from '@/components/charts/credits'
import router from '@/router'
import { useUiConfigStore } from '@/stores/uiConfig'
import AboutContent from './AboutContent.vue'
import AppShell from './AppShell.vue'
import { THIRD_PARTY_CREDITS } from './credits'
import { useShellModals } from './useShellModals'

// tj-grna9p.54: the About and Credits modal. Licences are compared with the INSTALLED packages
// (node_modules/<name>/package.json), never with a value recalled here.
const WEB = resolve(import.meta.dirname, '../..')
const packageJson = JSON.parse(readFileSync(resolve(WEB, 'package.json'), 'utf8')) as {
  version: string
  dependencies: Record<string, string>
}

function installed(name: string): { license: string; version: string } {
  return JSON.parse(readFileSync(resolve(WEB, 'node_modules', name, 'package.json'), 'utf8'))
}

// Which credit entry carries each runtime dependency. A dependency added to package.json without a
// line here fails the completeness test: that is the point.
const CREDIT_FOR: Record<string, string> = {
  '@bufbuild/protobuf': '@bufbuild/protobuf',
  '@lucide/vue': 'Lucide',
  '@primeuix/themes': 'PrimeVue and @primeuix/themes',
  'ag-grid-community': 'AG Grid Community (ag-grid-community and ag-grid-vue3)',
  'ag-grid-vue3': 'AG Grid Community (ag-grid-community and ag-grid-vue3)',
  echarts: 'Apache ECharts',
  'lightweight-charts': 'TradingView Lightweight Charts',
  pinia: 'Pinia',
  primevue: 'PrimeVue and @primeuix/themes',
  vue: 'Vue',
  'vue-echarts': 'vue-echarts',
  'vue-router': 'Vue Router',
}

const ALL_CREDITS = [...CHART_CREDITS, ...THIRD_PARTY_CREDITS]

function creditNamed(name: string) {
  return ALL_CREDITS.find((c) => c.name === name)
}

const mocks = vi.hoisted(() => ({
  getUiConfig: vi.fn(),
  getDatasetFacets: vi.fn(),
  listDatasets: vi.fn(),
  getDataset: vi.fn(),
  getDatasetBars: vi.fn(),
}))
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...mocks,
}))

const mounted: VueWrapper[] = []

beforeEach(() => {
  setActivePinia(createPinia())
  mocks.getUiConfig.mockResolvedValue({ allowedGroups: [AccountGroup.SIMULATION], deploymentLabel: '', serverVersion: '0.4.0' })
  mocks.getDatasetFacets.mockResolvedValue({ all: 0n, needsAttention: 0n, sources: [], updateTypes: [], statuses: [] })
  mocks.listDatasets.mockResolvedValue({ items: [], nextCursor: '' })
  mocks.getDataset.mockRejectedValue(new ApiError({ status: 404, reason: 'NOT_FOUND' }))
  mocks.getDatasetBars.mockResolvedValue({ bars: [], nextCursor: '' })
})

afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount())
  useShellModals().closeAbout()
  useShellModals().closeSettings()
})

describe('credits completeness', () => {
  it('every runtime dependency in package.json is credited', () => {
    const missing = Object.keys(packageJson.dependencies).filter((name) => {
      const entry = CREDIT_FOR[name]
      return entry === undefined || creditNamed(entry) === undefined
    })
    expect(missing).toEqual([])
  })

  it('each credit names the licence the installed package declares', () => {
    for (const name of Object.keys(packageJson.dependencies)) {
      const declared = installed(name).license
      const credit = creditNamed(CREDIT_FOR[name]!)!
      if (declared.startsWith('SEE LICENSE IN')) {
        // PrimeVue and @primeuix ship their own licence (PrimeUI), which is not MIT.
        expect(credit.licence, name).toContain('PrimeUI')
        expect(credit.licence, name).toContain('not MIT')
      } else {
        expect(credit.licence, name).toBe(declared.replace(/^\((.*)\)$/, '$1'))
      }
    }
  })

  it('carries the Lucide, Feather and IBM Plex notices the licences ask for', () => {
    const thirdParty = (name: string) => THIRD_PARTY_CREDITS.find((c) => c.name === name)!
    const lucide = thirdParty('Lucide')
    expect(lucide.licence).toBe('ISC')
    expect(lucide.copyright).toContain('Lucide Icons and Contributors')
    const feather = THIRD_PARTY_CREDITS.find((c) => c.name.startsWith('Feather'))!
    expect(feather.licence).toBe('MIT')
    expect(feather.copyright).toContain('Cole Bemis')
    for (const font of ['IBM Plex Sans', 'IBM Plex Mono']) {
      const credit = thirdParty(font)
      expect(credit.licence).toBe('SIL Open Font License 1.1')
      expect(credit.copyright).toContain('IBM Corp.')
      expect(credit.licenceText).toContain('SIL OPEN FONT LICENSE Version 1.1')
      expect(credit.note).toContain('Fontsource')
    }
  })

  it('the ECharts NOTICE is the installed package NOTICE, verbatim', () => {
    const notice = readFileSync(resolve(WEB, 'node_modules/echarts/NOTICE'), 'utf8')
    expect(ECHARTS_NOTICE).toBe(notice.trim())
  })

  it('the Lightweight Charts notice names TradingView and links tradingview.com', () => {
    // The npm package ships no NOTICE (the builder took it from the upstream v5.2.1 tag), so only its
    // shape is pinned here, plus the version that text belongs to.
    expect(installed('lightweight-charts').version).toBe('5.2.1')
    expect(LIGHTWEIGHT_CHARTS_NOTICE).toMatch(/^TradingView Lightweight Charts™\n/)
    expect(LIGHTWEIGHT_CHARTS_NOTICE).toContain('TradingView, Inc. https://www.tradingview.com/')
    expect(TRADINGVIEW_URL).toBe('https://www.tradingview.com/')
  })
})

describe('About and Credits content', () => {
  function about(): VueWrapper {
    const wrapper = mount(AboutContent)
    mounted.push(wrapper)
    return wrapper
  }

  it('links TradingView with a safe rel and a new tab', () => {
    const link = about().find('[data-testid="tradingview-link"]')
    expect(link.attributes('href')).toBe('https://www.tradingview.com/')
    expect(link.attributes('rel')).toBe('noopener noreferrer')
    expect(link.attributes('target')).toBe('_blank')
  })

  it('shows each chart NOTICE in full', () => {
    const wrapper = about()
    const notices = wrapper.findAll('pre.about__notice').map((p) => p.text())
    expect(notices).toContain(LIGHTWEIGHT_CHARTS_NOTICE)
    expect(notices).toContain(ECHARTS_NOTICE)
  })

  it('lists every credit with its licence and its link, and the font licence text', () => {
    const wrapper = about()
    for (const credit of ALL_CREDITS) {
      const item = wrapper.find(`[data-testid="credit-${credit.name}"]`)
      expect(item.exists(), credit.name).toBe(true)
      expect(item.text()).toContain(credit.licence)
      expect(item.find(`a[href="${credit.url}"]`).attributes('rel')).toBe('noopener noreferrer')
    }
    const plex = wrapper.find('[data-testid="credit-IBM Plex Sans"] details pre')
    expect(plex.text()).toContain('SIL OPEN FONT LICENSE Version 1.1')
  })

  it('renders the server and UI versions, and Unknown for an empty server version', async () => {
    await useUiConfigStore().load()
    const wrapper = about()
    expect(wrapper.find('[data-testid="about-server-version"]').text()).toBe('0.4.0')
    expect(wrapper.find('[data-testid="about-ui-version"]').text()).toBe(packageJson.version)

    mocks.getUiConfig.mockResolvedValue({ allowedGroups: [], deploymentLabel: '', serverVersion: '' })
    await useUiConfigStore().load()
    await flushPromises()
    expect(wrapper.find('[data-testid="about-server-version"]').text()).toBe('Unknown')
  })
})

describe('reachable from every routed screen', () => {
  it.each(['/data/datasets', '/data/datasets/ds-1/viewer', '/data/requests', '/data/health', '/data/usage', '/no/such/page'])(
    'opens from %s through Settings',
    async (path) => {
      await router.push(path)
      await router.isReady()
      const shell = mount(AppShell, { global: { plugins: [createPinia(), router, PrimeVue] }, attachTo: document.body })
      mounted.push(shell)
      await flushPromises()
      await shell.find('button[aria-label="Settings"]').trigger('click')
      await flushPromises()
      const link = [...document.body.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'About and Credits')
      link!.click()
      await flushPromises()
      const tradingView = document.body.querySelector('[data-testid="tradingview-link"]')
      expect(tradingView?.getAttribute('href')).toBe('https://www.tradingview.com/')
    },
  )
})
