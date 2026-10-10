import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'

import AboutContent from './AboutContent.vue'

// Scaffolding smoke only (tj-grna9p.54): proves the credits body mounts and renders the TradingView
// link. Coverage of the credits belongs to the validator (tj-grna9p.40).
describe('about content smoke', () => {
  it('renders the TradingView link and the versions', () => {
    const wrapper = mount(AboutContent, { global: { plugins: [createPinia()] } })
    const link = wrapper.find('[data-testid="tradingview-link"]')

    expect(link.attributes('href')).toBe('https://www.tradingview.com/')
    expect(link.attributes('rel')).toBe('noopener noreferrer')
    expect(wrapper.find('[data-testid="about-server-version"]').text()).toBe('Unknown')
  })
})
