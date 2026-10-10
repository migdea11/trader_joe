import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import type { Pinia } from 'pinia'
import PrimeVue from 'primevue/config'
import Select from 'primevue/select'

import router from '@/router'
import { useSettingsStore } from '@/stores/settings'
import { useUiConfigStore } from '@/stores/uiConfig'
import SettingsPanel from './SettingsPanel.vue'
import TopBar from './TopBar.vue'
import { useShellModals } from './useShellModals'

// Owner feedback tj-sww0b1 items 4 and 5: the deployment tag (nothing for an empty or prod label, a
// highlighted tag otherwise) and the top bar's time zone control, which must be a real select that
// sets the same override as the Settings panel rather than a second Settings button.
let pinia: Pinia
const mounted: VueWrapper[] = []

function mountWith(component: typeof TopBar | typeof SettingsPanel): VueWrapper {
  const wrapper = mount(component, { global: { plugins: [pinia, router, PrimeVue] } })
  mounted.push(wrapper)
  return wrapper
}

beforeEach(() => {
  try {
    window.localStorage.clear()
  } catch {
    // jsdom always has storage; the guard only mirrors the store's own.
  }
  pinia = createPinia()
  setActivePinia(pinia)
})

afterEach(() => {
  mounted.splice(0).forEach((wrapper) => wrapper.unmount())
  useShellModals().closeSettings()
})

describe('deployment tag', () => {
  it.each(['', 'prod', 'PRODUCTION'])('renders nothing for %j', (label) => {
    useUiConfigStore().deploymentLabel = label
    const wrapper = mountWith(TopBar)
    expect(wrapper.find('[data-testid="deployment-tag"]').exists()).toBe(false)
  })

  it('renders dev as the highlighted tag', () => {
    useUiConfigStore().deploymentLabel = 'dev'
    const tag = mountWith(TopBar).find('[data-testid="deployment-tag"]')
    expect(tag.text()).toBe('dev')
    // The highlight is the tag class itself (warn border and wash in TopBar.vue).
    expect(tag.classes()).toContain('top-bar__tag')
  })

  it('keeps the full text of a long label in the title for the truncated tag', () => {
    const label = 'x'.repeat(40)
    useUiConfigStore().deploymentLabel = label
    const tag = mountWith(TopBar).find('[data-testid="deployment-tag"]')
    expect(tag.attributes('title')).toBe(label)
    expect(tag.classes()).toContain('top-bar__tag')
  })
})

describe('time zone select', () => {
  it('sets the shared override, and the Settings panel shows the same zone', async () => {
    const zone = 'Asia/Tokyo'
    const bar = mountWith(TopBar)
    const barSelect = bar.findComponent(Select)
    expect(barSelect.exists()).toBe(true)

    barSelect.vm.$emit('update:modelValue', zone)
    await bar.vm.$nextTick()

    expect(useSettingsStore().timeZoneOverride).toBe(zone)
    expect(bar.findComponent(Select).props('modelValue')).toBe(zone)

    useShellModals().openSettings()
    const panel = mountWith(SettingsPanel)
    await panel.vm.$nextTick()
    expect(panel.findComponent(Select).props('modelValue')).toBe(zone)
  })
})
