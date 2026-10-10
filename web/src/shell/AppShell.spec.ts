import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import PrimeVue from 'primevue/config'

import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'

import router from '@/router'
import AppShell from './AppShell.vue'

const getUiConfig = vi.hoisted(() => vi.fn())
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  getUiConfig,
}))

// Scaffolding smoke only (tj-grna9p.30): proves the shell mounts under the router, Pinia and
// PrimeVue and reads the config. Coverage of the shell's behaviour belongs to the validator
// (tj-grna9p.40).
describe('app shell smoke', () => {
  beforeEach(() => {
    getUiConfig.mockResolvedValue({
      allowedGroups: [AccountGroup.SIMULATION],
      deploymentLabel: 'Dev',
      serverVersion: '1.2.3',
    })
  })

  it('mounts with the Data section, the toggle and the deployment tag', async () => {
    await router.push('/')
    await router.isReady()
    const wrapper = mount(AppShell, { global: { plugins: [createPinia(), router, PrimeVue] } })
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/data/datasets')
    expect(wrapper.find('[data-testid="deployment-tag"]').text()).toBe('Dev')
    expect(wrapper.find('[data-group="paper"]').attributes('aria-disabled')).toBe('true')
    expect(wrapper.find('[data-group="simulation"]').attributes('aria-pressed')).toBe('true')
    expect(router.currentRoute.value.query.group).toBe('simulation')
  })
})
