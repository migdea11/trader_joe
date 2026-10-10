import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'

import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'

import router from '@/router'
import { useDatasetCatalogStore } from '@/stores/datasetCatalog'
import { useUiConfigStore } from '@/stores/uiConfig'
import AppShell from './AppShell.vue'
import StatusFooter from './StatusFooter.vue'
import { useFooterSummary } from './useFooterSummary'

const mocks = vi.hoisted(() => ({
  getUiConfig: vi.fn(),
  getDatasetFacets: vi.fn(),
  listDatasets: vi.fn(),
}))
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...mocks,
}))

// Owner feedback tj-sww0b1 item 6: the footer shows only what is nowhere else, is absent when it has
// nothing, and "Updated <time>" is plain text (the Refresh link beside it was dropped in 3ca6538).
const mounted: VueWrapper[] = []

beforeEach(() => {
  setActivePinia(createPinia())
  const { setFooterSummary, setFooterUpdated, setFooterProblem } = useFooterSummary()
  setFooterSummary(null)
  setFooterUpdated(null)
  setFooterProblem(null)
  mocks.getUiConfig.mockResolvedValue({ allowedGroups: [AccountGroup.SIMULATION], deploymentLabel: '', serverVersion: '' })
  mocks.getDatasetFacets.mockResolvedValue({ all: 3n, needsAttention: 0n, sources: [], updateTypes: [], statuses: [] })
  mocks.listDatasets.mockResolvedValue({ items: [], nextCursor: '' })
})

afterEach(() => {
  mounted.splice(0).forEach((wrapper) => wrapper.unmount())
})

describe('status footer', () => {
  it('is not rendered when nothing is set', () => {
    const wrapper = mount(StatusFooter)
    mounted.push(wrapper)
    expect(wrapper.find('footer').exists()).toBe(false)
  })

  it('shows the problem line when the deployment config failed', async () => {
    useUiConfigStore().status = 'error'
    const wrapper = mount(StatusFooter)
    mounted.push(wrapper)
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-testid="footer-problem"]').text()).toBe('Could not load the deployment configuration')
  })

  it('shows Updated as plain text with no button after a list load', async () => {
    await router.push('/data/datasets')
    await router.isReady()
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(AppShell, { global: { plugins: [pinia, router, PrimeVue] } })
    mounted.push(wrapper)
    await flushPromises()

    // jsdom has no layout, so the grid asks for no rows; report a loaded page as the grid would.
    useDatasetCatalogStore().listPage({ loadedRows: 3, done: true })
    await flushPromises()

    const footer = wrapper.find('footer')
    expect(footer.find('[data-testid="footer-updated"]').text()).toMatch(/^Updated \S/)
    expect(footer.find('button').exists()).toBe(false)
    expect(footer.find('a').exists()).toBe(false)
  })
})
