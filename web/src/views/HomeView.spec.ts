import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import HomeView from './HomeView.vue'

// Scaffolding smoke only (tj-grna9p.28): proves the Vitest + jsdom + Vue + PrimeVue harness
// actually runs end to end. Coverage of real views belongs to the validator (tj-grna9p.40).
describe('scaffold smoke', () => {
  it('mounts the placeholder home view', () => {
    const wrapper = mount(HomeView)

    expect(wrapper.text()).toContain('trader_joe')
  })
})
