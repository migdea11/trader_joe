import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import PrimeVue from 'primevue/config'
import { create } from '@bufbuild/protobuf'
import { TimestampSchema } from '@bufbuild/protobuf/wkt'

import { BarSchema } from '@generated/trader_joe/proto/market/v1/bar_pb'
import { DataSource, DataType, Feed, Granularity, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'

import { ApiError } from '@/api'
import router from '@/router'
import AppShell from '@/shell/AppShell.vue'

const mocks = vi.hoisted(() => ({
  getUiConfig: vi.fn(),
  getDataset: vi.fn(),
  getDatasetBars: vi.fn(),
}))
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...mocks,
}))

// Scaffolding smoke only (tj-grna9p.33): proves the Viewer mounts in the shell, fills its header
// from the dataset detail and shows the not-found state. The chart needs a canvas, which jsdom lacks,
// so it is stubbed here. Coverage of the behaviour belongs to the validator (tj-grna9p.40).
function summary(overrides: Record<string, unknown> = {}) {
  return {
    id: 'abc',
    assetSymbol: 'VFV',
    dataType: DataType.MARKET_ACTIVITY,
    source: DataSource.ALPACA_API,
    feed: Feed.IEX,
    granularity: Granularity.ONE_DAY,
    updateType: UpdateType.DAILY,
    owner: 'operator',
    barCount: 2n,
    firstBar: create(TimestampSchema, { seconds: 1_788_000_000n }),
    lastBar: create(TimestampSchema, { seconds: 1_788_086_400n }),
    siblings: [],
    ...overrides,
  }
}

function mountShell() {
  return mount(AppShell, {
    global: {
      plugins: [createPinia(), router, PrimeVue],
      stubs: { PriceChart: true, ZoomNavigator: true, BarTable: true },
    },
  })
}

describe('dataset viewer smoke', () => {
  beforeEach(() => {
    mocks.getUiConfig.mockResolvedValue({
      allowedGroups: [AccountGroup.SIMULATION],
      deploymentLabel: 'Prod',
      serverVersion: '1.2.3',
    })
    mocks.getDatasetBars.mockResolvedValue({
      bars: [create(BarSchema, { open: 1, high: 2, low: 1, close: 2, volume: 10 })],
      nextCursor: '',
    })
  })

  it('shows the header from the detail, with an unknown badge when health is unset', async () => {
    mocks.getDataset.mockResolvedValue(summary())
    await router.push('/data/datasets/abc/viewer')
    await router.isReady()
    const wrapper = mountShell()
    await flushPromises()

    expect(wrapper.find('[data-testid="viewer-title"]').text()).toBe('VFV · bars · 1d')
    expect(wrapper.find('[data-testid="viewer-badge"]').text()).toBe('Unknown')
    expect(wrapper.find('[data-testid="viewer-meta"]').text()).toContain('Alpaca · IEX')
    expect(wrapper.find('[data-adjustment="split"]').attributes('disabled')).toBeDefined()
  })

  it('shows the not-found state for a 404', async () => {
    mocks.getDataset.mockRejectedValue(new ApiError({ status: 404 }))
    await router.push('/data/datasets/nope/viewer')
    const wrapper = mountShell()
    await flushPromises()

    expect(wrapper.find('[data-testid="viewer-not-found"]').exists()).toBe(true)
  })
})
