import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import PrimeVue from 'primevue/config'

import { UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { FreshnessStatus } from '@generated/trader_joe/proto/ui/v1/data_pb'
import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'

import router from '@/router'
import AppShell from '@/shell/AppShell.vue'

const mocks = vi.hoisted(() => ({
  getUiConfig: vi.fn(),
  getDatasetFacets: vi.fn(),
  listDatasets: vi.fn(),
}))
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...mocks,
}))

// Scaffolding smoke only (tj-grna9p.32): proves the catalog screen mounts in the shell, loads the
// facets into the sidebar and tiles, and mounts the grid. Coverage of its
// behaviour belongs to the validator (tj-grna9p.40).
describe('datasets screen smoke', () => {
  beforeEach(() => {
    mocks.getUiConfig.mockResolvedValue({
      allowedGroups: [AccountGroup.SIMULATION],
      deploymentLabel: 'Prod',
      serverVersion: '1.2.3',
    })
    mocks.getDatasetFacets.mockResolvedValue({
      all: 38n,
      needsAttention: 2n,
      sources: [],
      updateTypes: [
        { updateType: UpdateType.DAILY, count: 26n },
        { updateType: UpdateType.STATIC, count: 12n },
      ],
      statuses: [
        { status: FreshnessStatus.FRESH, count: 30n },
        { status: FreshnessStatus.LATE, count: 1n },
        { status: FreshnessStatus.OVERDUE, count: 1n },
      ],
    })
    mocks.listDatasets.mockResolvedValue({ items: [], nextCursor: '' })
  })

  it('shows the view counts and tiles and mounts the grid', async () => {
    await router.push('/data/datasets?view=needs-attention')
    await router.isReady()
    const wrapper = mount(AppShell, { global: { plugins: [createPinia(), router, PrimeVue] } })
    await flushPromises()

    expect(wrapper.find('[data-view="all"]').text()).toContain('38')
    expect(wrapper.find('[data-view="needs-attention"]').attributes('aria-current')).toBe('true')
    expect(wrapper.find('[data-tile="failed"]').text()).toContain('1')
    expect(wrapper.find('[data-tile="datasets"]').text()).toContain('26 daily · 12 bulk')
    // jsdom has no layout, so the grid asks for no rows here; the page-fetch smoke is in
    // cursorDatasource.spec.ts.
    expect(wrapper.find('.ag-root-wrapper').exists()).toBe(true)
  })
})
