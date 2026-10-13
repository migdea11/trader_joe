import { afterEach, describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent } from 'vue'

import PageBreadcrumbs from './PageBreadcrumbs.vue'
import type { BreadcrumbItem } from './PageBreadcrumbs.vue'

// tj-grna9p.113 (owner 2026-10-13): the breadcrumb trail, a way back up the hierarchy without the
// browser's Back button. A nav labelled Breadcrumb holding an ordered list; every ancestor is a real
// link (keyboard reachable, no tabindex trick), the last item is the current page as plain text with
// aria-current=page, and the separators are decoration hidden from assistive technology. The labels
// arrive ready to show (each screen titleCases its fixed words; DatasetDetail.spec.ts and
// DatasetViewer.spec.ts pin the screens' trails). Mounted under a memory-history router with the
// same route names the app uses, so hrefs are real router output.
const Blank = defineComponent({ render: () => null })

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/data/datasets', name: 'data-datasets', component: Blank },
      { path: '/data/datasets/:id', name: 'data-dataset-detail', component: Blank },
      { path: '/data/datasets/:id/viewer', name: 'data-dataset-viewer', component: Blank },
    ],
  })
}

const mounted: VueWrapper[] = []

async function render(items: BreadcrumbItem[]): Promise<{ wrapper: VueWrapper; router: ReturnType<typeof makeRouter> }> {
  const router = makeRouter()
  await router.push('/data/datasets/ds-1')
  await router.isReady()
  const wrapper = mount(PageBreadcrumbs, { props: { items }, global: { plugins: [router] }, attachTo: document.body })
  mounted.push(wrapper)
  return { wrapper, router }
}

afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount())
})

const DETAIL_TRAIL: BreadcrumbItem[] = [
  { label: 'Datasets', to: { name: 'data-datasets' } },
  { label: 'VFV · bars · 1d', to: { name: 'data-dataset-viewer', params: { id: 'a/b' } } },
  { label: 'Details' },
]

describe('PageBreadcrumbs', () => {
  it('is a nav labelled Breadcrumb around an ordered list, one item per crumb, in order', async () => {
    const { wrapper } = await render(DETAIL_TRAIL)
    const nav = wrapper.find('[data-testid="breadcrumbs"]')
    expect(nav.element.tagName).toBe('NAV')
    expect(nav.attributes('aria-label')).toBe('Breadcrumb')
    const list = nav.find('ol')
    expect(list.exists()).toBe(true)
    expect(list.findAll(':scope > li').map((li) => li.text())).toEqual(['Datasets', 'VFV · bars · 1d', 'Details'])
  })

  it('every ancestor is a real link to its route, escaping an id that is not URL-safe', async () => {
    const { wrapper } = await render(DETAIL_TRAIL)
    const links = wrapper.findAll('[data-testid="breadcrumb-link"]')
    expect(links.map((a) => [a.element.tagName, a.text(), a.attributes('href')])).toEqual([
      ['A', 'Datasets', '/data/datasets'],
      ['A', 'VFV · bars · 1d', '/data/datasets/a%2Fb/viewer'],
    ])
    // Keyboard reachable: an anchor with an href is in the tab order unless something takes it out.
    for (const a of links) expect(a.attributes('tabindex')).toBeUndefined()
    // No ancestor claims to be the current page.
    for (const a of links) expect(a.attributes('aria-current')).toBeUndefined()
  })

  it('the current crumb is plain text, not a link, with aria-current=page; it is the only one', async () => {
    const { wrapper } = await render(DETAIL_TRAIL)
    const current = wrapper.find('[data-testid="breadcrumb-current"]')
    expect(current.element.tagName).toBe('SPAN')
    expect(current.text()).toBe('Details')
    expect(current.attributes('aria-current')).toBe('page')
    expect(current.find('a').exists()).toBe(false)
    expect(current.attributes('href')).toBeUndefined()
    expect(wrapper.findAll('[aria-current]')).toHaveLength(1)
    expect(wrapper.findAll('a')).toHaveLength(2)
  })

  it('the last crumb is the current page even when it is handed a destination', async () => {
    const { wrapper } = await render([
      { label: 'Datasets', to: { name: 'data-datasets' } },
      { label: 'VFV · bars · 1d', to: { name: 'data-dataset-viewer', params: { id: 'ds-1' } } },
    ])
    expect(wrapper.findAll('[data-testid="breadcrumb-link"]').map((a) => a.text())).toEqual(['Datasets'])
    expect(wrapper.find('[data-testid="breadcrumb-current"]').text()).toBe('VFV · bars · 1d')
    expect(wrapper.find('[data-testid="breadcrumb-current"]').attributes('aria-current')).toBe('page')
  })

  it('a separator sits between crumbs, never after the last, and is hidden from assistive technology', async () => {
    const { wrapper } = await render(DETAIL_TRAIL)
    const items = wrapper.findAll('li')
    const seps = items.map((li) => li.findAll('svg'))
    expect(seps.map((s) => s.length)).toEqual([1, 1, 0])
    for (const s of seps.flat()) expect(s.attributes('aria-hidden')).toBe('true')
    // The separator adds no text, so a screen reader hears only the labels.
    expect(items.map((li) => li.text())).toEqual(['Datasets', 'VFV · bars · 1d', 'Details'])
    expect(wrapper.find('nav').text()).not.toMatch(/[>/›»]/)
  })

  it('a single crumb is the current page alone, with no link and no separator', async () => {
    const { wrapper } = await render([{ label: 'Datasets' }])
    expect(wrapper.findAll('a')).toHaveLength(0)
    expect(wrapper.findAll('svg')).toHaveLength(0)
    expect(wrapper.find('[data-testid="breadcrumb-current"]').attributes('aria-current')).toBe('page')
  })

  // The owner's ruling (tj-grna9p.113): the dataset crumb shows the symbol and carries the whole
  // identity as a hover title. The hint is a title attribute only: never an aria-label, so the
  // accessible name stays the visible label; a crumb without a hint has no title at all.
  it('a hint becomes the title of a link or the current crumb, never an aria-label; no hint, no title', async () => {
    const { wrapper } = await render([
      { label: 'Datasets', to: { name: 'data-datasets' } },
      { label: 'VFV', to: { name: 'data-dataset-viewer', params: { id: 'a/b' } }, hint: 'VFV · bars · 1d · Alpaca · IEX' },
      { label: 'Details' },
    ])
    const links = wrapper.findAll('[data-testid="breadcrumb-link"]')
    expect(links.map((a) => [a.text(), a.attributes('href'), a.attributes('title')])).toEqual([
      ['Datasets', '/data/datasets', undefined],
      ['VFV', '/data/datasets/a%2Fb/viewer', 'VFV · bars · 1d · Alpaca · IEX'],
    ])
    expect(wrapper.find('[data-testid="breadcrumb-current"]').attributes('title')).toBeUndefined()
    expect(wrapper.findAll('[aria-label]').map((e) => e.element.tagName)).toEqual(['NAV'])

    const viewer = await render([
      { label: 'Datasets', to: { name: 'data-datasets' } },
      { label: 'VFV', hint: 'VFV · bars · 1d · Alpaca · IEX' },
    ])
    const current = viewer.wrapper.find('[data-testid="breadcrumb-current"]')
    expect(current.text()).toBe('VFV')
    expect(current.attributes('title')).toBe('VFV · bars · 1d · Alpaca · IEX')
    expect(current.attributes('aria-label')).toBeUndefined()
    expect(viewer.wrapper.find('[data-testid="breadcrumb-link"]').attributes('title')).toBeUndefined()
  })

  it('following an ancestor navigates to its route', async () => {
    const { wrapper, router } = await render(DETAIL_TRAIL)
    await wrapper.findAll('[data-testid="breadcrumb-link"]')[1]!.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('data-dataset-viewer')
    expect(router.currentRoute.value.params.id).toBe('a/b')
  })
})
