import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia } from 'pinia'
import PrimeVue from 'primevue/config'

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

// Owner feedback tj-sww0b1 item 7, client half: with no filter one facets response serves both the
// sidebar and the tiles; a filter on arrival needs the filtered counts and the whole-catalog tiles.
let wrapper: VueWrapper | null = null

beforeEach(() => {
  vi.clearAllMocks()
  mocks.getUiConfig.mockResolvedValue({ allowedGroups: [AccountGroup.SIMULATION], deploymentLabel: '', serverVersion: '' })
  mocks.getDatasetFacets.mockResolvedValue({ all: 3n, needsAttention: 1n, sources: [], updateTypes: [], statuses: [] })
  mocks.listDatasets.mockResolvedValue({ items: [], nextCursor: '' })
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

async function mountAt(path: string): Promise<void> {
  await router.push(path)
  await router.isReady()
  wrapper = mount(AppShell, { global: { plugins: [createPinia(), router, PrimeVue] } })
  await flushPromises()
}

describe('datasets facets requests on mount', () => {
  it('sends exactly one with no filter', async () => {
    await mountAt('/data/datasets')
    expect(mocks.getDatasetFacets).toHaveBeenCalledTimes(1)
  })

  it('sends two with the needs-attention view', async () => {
    await mountAt('/data/datasets?view=needs-attention')
    expect(mocks.getDatasetFacets).toHaveBeenCalledTimes(2)
  })
})
