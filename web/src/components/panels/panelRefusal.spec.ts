// A refused panel write, end to end (tj-7zjov1.246): the three forms over the REAL store and the REAL typed
// client, with only the network replaced (global fetch answers from a small fake store per test).
// - The create form refuses, before any POST, a trimmed name equal to one in the loaded list, exact case.
// - A 422 is answered from a re-read, never from its body: create re-reads the list and, when it sent source
//   settings, the source's first version; the two saves re-read the panel and compare the saved version.
//   What moved is named; when nothing moved (or the re-read itself fails) the route's fixed copy shows,
//   with the error id. The forms stay as they were.
// - Nothing from the 422 body but its error id reaches the page: the body carries LEAK in every other field.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'

import { usePanelsStore } from '@/stores/panels'

import PanelCreateForm from './PanelCreateForm.vue'
import PanelSettingsForm from './PanelSettingsForm.vue'
import PanelSourceSettingsForm from './PanelSourceSettingsForm.vue'

const PANEL_ID = '0f8fad5b-d9cb-469f-a165-70867728950e'
const LEAK = 'ZEBRA-7731'
const BODY_422 = {
  type: `https://errors.test/${LEAK}`,
  title: `Invalid request ${LEAK}`,
  status: 422,
  detail: `panel name ${LEAK} already exists`,
  message: LEAK,
  reason: 'INVALID_REQUEST',
  error_id: 'e-race',
  errors: [{ loc: ['body', 'name'], msg: LEAK }],
}

type Read = 'list' | 'panel' | 'source'

interface FakeStore {
  /** Names listed after the first panel (Large caps). */
  names: string[]
  panelVersion: number
  sourceVersion: number
  sourceHasSettings: boolean
  /** Reads answering 500 from now on. */
  failing: Set<Read>
  /** Runs when a write arrives, before it is refused: the race. */
  onWrite: () => void
  /** Runs when the list is read, before it answers: a selection change during the re-read. */
  onList: () => void
}

/** The second listed panel (names[0]), whose versions differ from the first's. */
const OTHER_ID = '00000000-0000-4000-8000-000000000001'

let fake: FakeStore
const fetchMock = vi.fn<typeof fetch>()

function json(body: unknown, status = 200, type = 'application/json'): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': type } })
}

const failure = () => json({ status: 500, reason: 'INTERNAL', error_id: 'e-read' }, 500, 'application/problem+json')

function detailJson() {
  return {
    id: PANEL_ID,
    name: 'Large caps',
    source: 'DATA_SOURCE_ALPACA_API',
    currentVersion: {
      versionNo: fake.panelVersion,
      settings: { enabled: true, cadence: 'PANEL_CADENCE_MONTHLY' },
      actor: 'operator',
      reason: 'first',
    },
    currentSourceVersion: {
      source: 'DATA_SOURCE_ALPACA_API',
      versionNo: fake.sourceVersion,
      actor: 'operator',
      settings: { rampDatasetsPerHour: 5, requestCapPerHour: 200, corporateActionWindowPreDays: 90, corporateActionWindowPostDays: 30 },
    },
  }
}

async function answer(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const path = new URL(String(input), 'http://edge.test').pathname.replace('/api/store/ui/v1', '')
  if ((init?.method ?? 'GET') !== 'GET') {
    fake.onWrite()
    return json(BODY_422, 422, 'application/problem+json')
  }
  if (path === '/panels') {
    fake.onList()
    if (fake.failing.has('list')) return failure()
    const items = ['Large caps', ...fake.names].map((name, i) => ({
      id: i === 0 ? PANEL_ID : `00000000-0000-4000-8000-00000000000${i}`,
      name,
      source: 'DATA_SOURCE_ALPACA_API',
      enabled: true,
    }))
    return json({ items })
  }
  if (path.startsWith('/panel-sources/')) {
    if (fake.failing.has('source')) return failure()
    return json({ items: fake.sourceHasSettings ? [{ source: 'DATA_SOURCE_ALPACA_API', versionNo: fake.sourceVersion }] : [] })
  }
  if (path === `/panels/${PANEL_ID}`) return fake.failing.has('panel') ? failure() : json(detailJson())
  if (path === `/panels/${OTHER_ID}`) {
    const first = detailJson()
    return json({
      ...first,
      id: OTHER_ID,
      name: fake.names[0],
      currentVersion: { ...first.currentVersion, versionNo: 9 },
      currentSourceVersion: { ...first.currentSourceVersion, versionNo: 9 },
    })
  }
  if (path.endsWith('/members') || path.endsWith('/settings-versions')) return json({ items: [] })
  return json({})
}

const posts = () => fetchMock.mock.calls.filter(([, init]) => (init?.method ?? 'GET') !== 'GET')
const byId = (wrapper: VueWrapper, id: string) => wrapper.find(`[data-testid="${id}"]`)
const valueOf = (wrapper: VueWrapper, id: string) => (byId(wrapper, id).element as HTMLInputElement).value

function expectNoLeak(wrapper: VueWrapper): void {
  expect(wrapper.html()).not.toContain(LEAK)
  expect(document.body.innerHTML).not.toContain(LEAK)
}

beforeEach(async () => {
  fake = {
    names: [],
    panelVersion: 1,
    sourceVersion: 1,
    sourceHasSettings: true,
    failing: new Set(),
    onWrite: () => {},
    onList: () => {},
  }
  fetchMock.mockReset()
  fetchMock.mockImplementation(answer)
  vi.stubGlobal('fetch', fetchMock)
  window.localStorage.clear()
  setActivePinia(createPinia())
  await usePanelsStore().load()
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

// ---------------------------------------------------------------------------------------------------

describe('PanelCreateForm: the name is checked against the loaded list before any POST', () => {
  async function fill(form: VueWrapper, name: string): Promise<void> {
    await byId(form, 'create-name').setValue(name)
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    await byId(form, 'create-reason').setValue('a reason')
  }

  // Validated on click (tj-7zjov1.362.2): the clash waits for the first press like every other mark, and the
  // button stays on; the press marks the name, focuses it and sends nothing.
  it.each(['Large caps', '  Large caps  '])('%j is refused in words once pressed, the button on, and no POST is made', async (typed) => {
    const form = mount(PanelCreateForm, { attachTo: document.body })
    await fill(form, typed)
    expect(byId(form, 'create-name-error').exists()).toBe(false)
    expect(byId(form, 'create-name').attributes('aria-invalid')).toBeUndefined()
    expect(byId(form, 'create-submit').attributes('disabled')).toBeUndefined()
    await byId(form, 'create-submit').trigger('click')
    await flushPromises()
    expect(posts()).toEqual([])
    expect(byId(form, 'create-name-error').text()).toBe('A panel named "Large caps" already exists. Choose another name.')
    expect(byId(form, 'create-name').attributes('aria-invalid')).toBe('true')
    expect(byId(form, 'create-name').attributes('aria-describedby')).toBe('create-name-error')
    expect(document.activeElement).toBe(byId(form, 'create-name').element)
    expect(byId(form, 'create-submit').attributes('disabled')).toBeUndefined()
    expect(byId(form, 'create-notice').exists()).toBe(false)
  })

  it('a name differing only in case is no clash: the form sends it', async () => {
    const form = mount(PanelCreateForm, { attachTo: document.body })
    await fill(form, 'large caps')
    expect(byId(form, 'create-problem').exists()).toBe(false)
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(posts()).toHaveLength(1)
    expect(JSON.parse(String(posts()[0]![1]!.body))).toMatchObject({ name: 'large caps' })
  })

  it('a name the list does not hold is sent trimmed', async () => {
    const form = mount(PanelCreateForm, { attachTo: document.body })
    await fill(form, '  Small caps ')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(JSON.parse(String(posts()[0]![1]!.body))).toMatchObject({ name: 'Small caps' })
  })
})

describe('PanelCreateForm: a 422 is re-read', () => {
  async function fill(form: VueWrapper, name = '  Gamma  '): Promise<void> {
    await byId(form, 'create-name').setValue(name)
    await byId(form, 'create-source').findAll('option')[1]!.setValue()
    await flushPromises()
    await byId(form, 'create-reason').setValue('a reason')
    if (byId(form, 'create-ramp').exists()) {
      await byId(form, 'create-ramp').setValue('0')
      await byId(form, 'create-cap').setValue('200')
    }
  }

  async function submit(form: VueWrapper): Promise<string> {
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(posts()).toHaveLength(1)
    expect(form.emitted('close')).toBeUndefined()
    expectNoLeak(form)
    return byId(form, 'create-notice').text()
  }

  it('the name was taken in the meantime: the notice names it, the form kept and now refusing the name', async () => {
    fake.onWrite = () => fake.names.push('Gamma')
    const form = mount(PanelCreateForm, { attachTo: document.body })
    await fill(form)
    expect(byId(form, 'create-name-error').exists()).toBe(false)
    expect(await submit(form)).toBe(
      'A panel named "Gamma" was created in the meantime, so nothing was created. Choose another name. Error ID: e-race',
    )
    expect(valueOf(form, 'create-name')).toBe('  Gamma  ')
    expect(valueOf(form, 'create-reason')).toBe('a reason')
    // The sent press counted as the first attempt, so the re-read's clash is marked at once, with no second press.
    expect(byId(form, 'create-name-error').text()).toBe('A panel named "Gamma" already exists. Choose another name.')
    expect(byId(form, 'create-name').attributes('aria-invalid')).toBe('true')
    expect(form.find('label[for="create-name"] .pn-required').classes()).toContain('pn-required--marked')
    expect(byId(form, 'create-problem').text()).toBe('1 marked field needs a value before the panel can be created.')
    expect(byId(form, 'create-submit').attributes('disabled')).toBeUndefined()
    await byId(form, 'create-submit').trigger('click')
    await flushPromises()
    expect(posts()).toHaveLength(1)
    expect(document.activeElement).toBe(byId(form, 'create-name').element)
  })

  it('a clash in another case is no race: the fixed copy shows', async () => {
    fake.onWrite = () => fake.names.push('gamma')
    const form = mount(PanelCreateForm, { attachTo: document.body })
    await fill(form)
    expect(await submit(form)).toBe('The store refused the panel, so nothing was created. Check the fields. Error ID: e-race')
  })

  it('the source has settings now: the notice says so and the source fields drop away', async () => {
    fake.sourceHasSettings = false
    fake.onWrite = () => (fake.sourceHasSettings = true)
    const form = mount(PanelCreateForm, { attachTo: document.body })
    await fill(form)
    expect(byId(form, 'create-ramp').exists()).toBe(true)
    expect(await submit(form)).toBe(
      'This source has settings now, so nothing was created. Its settings fields are gone: check the form and create again. Error ID: e-race',
    )
    expect(byId(form, 'create-ramp').exists()).toBe(false)
    expect(byId(form, 'create-cap').exists()).toBe(false)
    expect(valueOf(form, 'create-name')).toBe('  Gamma  ')
    expect(valueOf(form, 'create-reason')).toBe('a reason')
  })

  it('nothing moved: the fixed create copy with the error id, the input kept', async () => {
    const form = mount(PanelCreateForm, { attachTo: document.body })
    await fill(form)
    expect(await submit(form)).toBe('The store refused the panel, so nothing was created. Check the fields. Error ID: e-race')
    expect(valueOf(form, 'create-name')).toBe('  Gamma  ')
    expect(valueOf(form, 'create-reason')).toBe('a reason')
  })

  it('source settings sent and the source still has none: the fixed copy, the source fields kept', async () => {
    fake.sourceHasSettings = false
    const form = mount(PanelCreateForm, { attachTo: document.body })
    await fill(form)
    expect(await submit(form)).toBe('The store refused the panel, so nothing was created. Check the fields. Error ID: e-race')
    expect(valueOf(form, 'create-ramp')).toBe('0')
    expect(valueOf(form, 'create-cap')).toBe('200')
  })

  it('the list re-read fails: the fixed copy, nothing thrown', async () => {
    fake.onWrite = () => fake.failing.add('list')
    const form = mount(PanelCreateForm, { attachTo: document.body })
    await fill(form)
    expect(await submit(form)).toBe('The store refused the panel, so nothing was created. Check the fields. Error ID: e-race')
    expect(usePanelsStore().writing).toBe(false)
  })

  it('the source re-read fails: the fixed copy, nothing thrown, the source fields kept', async () => {
    fake.sourceHasSettings = false
    fake.onWrite = () => fake.failing.add('source')
    const form = mount(PanelCreateForm, { attachTo: document.body })
    await fill(form)
    expect(await submit(form)).toBe('The store refused the panel, so nothing was created. Check the fields. Error ID: e-race')
    expect(byId(form, 'create-ramp').exists()).toBe(true)
  })
})

// ---------------------------------------------------------------------------------------------------

// The saves' forms take the store's detail, as the page passes it, so a re-read reaches them.
function mountOverStore(form: typeof PanelSettingsForm | typeof PanelSourceSettingsForm): VueWrapper {
  const Host = defineComponent({
    setup() {
      const store = usePanelsStore()
      return () => (store.detail === null ? null : h(form, { detail: store.detail }))
    },
  })
  return mount(Host, { attachTo: document.body })
}

describe('PanelSettingsForm: a 422 re-reads the panel', () => {
  async function disable(form: VueWrapper): Promise<string> {
    await byId(form, 'settings-reason').setValue('why')
    await byId(form, 'settings-toggle').trigger('click')
    await flushPromises()
    expect(posts()).toHaveLength(1)
    expectNoLeak(form)
    return byId(form, 'settings-notice').text()
  }

  it('the settings moved on in the meantime: the notice says so and the page shows the new version', async () => {
    fake.onWrite = () => (fake.panelVersion = 4)
    const form = mountOverStore(PanelSettingsForm)
    expect(form.find('[data-fact="version"]').text()).toMatch(/^Version 1/)
    expect(await disable(form)).toBe(
      "This panel's settings changed in the meantime, so nothing was saved. The page shows the current settings. Error ID: e-race",
    )
    expect(form.find('[data-fact="version"]').text()).toMatch(/^Version 4/)
  })

  // tj-7zjov1.254 F5: the selection moves to another panel during the re-read: stale, so the newly
  // selected panel's form shows no notice, and nothing calls its settings changed.
  it('the selection changes during the re-read: no notice for the newly selected panel', async () => {
    fake.names = ['Small caps']
    await usePanelsStore().loadList()
    const form = mountOverStore(PanelSettingsForm)
    fake.onWrite = () => (fake.onList = () => void usePanelsStore().select(OTHER_ID))
    await byId(form, 'settings-reason').setValue('why')
    await byId(form, 'settings-toggle').trigger('click')
    await flushPromises()
    expect(posts()).toHaveLength(1)
    expect(usePanelsStore().selectedId).toBe(OTHER_ID)
    expect(form.find('[data-fact="version"]').text()).toMatch(/^Version 9/)
    expect(byId(form, 'settings-notice').exists()).toBe(false)
    expect(form.text()).not.toContain('changed in the meantime')
    expectNoLeak(form)
  })

  it('nothing moved: the fixed settings copy with the error id, the input kept', async () => {
    const form = mountOverStore(PanelSettingsForm)
    await byId(form, 'settings-deny').setValue('GME')
    expect(await disable(form)).toBe('The store refused these settings, so nothing was saved. Check the fields. Error ID: e-race')
    expect(valueOf(form, 'settings-reason')).toBe('why')
    expect(valueOf(form, 'settings-deny')).toBe('GME')
  })

  it('a source settings move is not a panel settings move: the fixed copy', async () => {
    fake.onWrite = () => (fake.sourceVersion = 6)
    const form = mountOverStore(PanelSettingsForm)
    expect(await disable(form)).toBe('The store refused these settings, so nothing was saved. Check the fields. Error ID: e-race')
  })

  it('the panel re-read fails: the fixed copy, nothing thrown, the form kept', async () => {
    fake.onWrite = () => {
      fake.failing.add('panel')
      fake.failing.add('list')
    }
    const form = mountOverStore(PanelSettingsForm)
    expect(await disable(form)).toBe('The store refused these settings, so nothing was saved. Check the fields. Error ID: e-race')
    expect(valueOf(form, 'settings-reason')).toBe('why')
    expect(usePanelsStore().writing).toBe(false)
  })
})

describe('PanelSourceSettingsForm: a 422 re-reads the panel and its source version', () => {
  async function save(form: VueWrapper): Promise<string> {
    await byId(form, 'source-cap').setValue('150')
    await byId(form, 'source-reason').setValue('why')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(posts()).toHaveLength(1)
    expectNoLeak(form)
    return byId(form, 'source-notice').text()
  }

  it('the source settings moved on in the meantime: the notice says so and the page shows the new version', async () => {
    fake.onWrite = () => (fake.sourceVersion = 3)
    const form = mountOverStore(PanelSourceSettingsForm)
    expect(await save(form)).toBe(
      'These source settings changed in the meantime, so nothing was saved. The page shows the current settings. Error ID: e-race',
    )
    expect(byId(form, 'source-version').text()).toMatch(/^Version 3/)
  })

  // tj-7zjov1.254 F5: the selected panel changes during the re-read: stale, never a comparison of two
  // panels' source versions, so the newly selected panel's form shows no notice.
  it('the selection changes during the re-read: no notice for the newly selected panel', async () => {
    fake.names = ['Small caps']
    await usePanelsStore().loadList()
    const form = mountOverStore(PanelSourceSettingsForm)
    fake.onWrite = () => (fake.onList = () => void usePanelsStore().select(OTHER_ID))
    await byId(form, 'source-cap').setValue('150')
    await byId(form, 'source-reason').setValue('why')
    await form.find('form').trigger('submit')
    await flushPromises()
    expect(posts()).toHaveLength(1)
    expect(usePanelsStore().selectedId).toBe(OTHER_ID)
    expect(byId(form, 'source-version').text()).toMatch(/^Version 9/)
    expect(byId(form, 'source-notice').exists()).toBe(false)
    expect(form.text()).not.toContain('changed in the meantime')
    expectNoLeak(form)
  })

  it('nothing moved: the fixed source copy with the error id, the input kept', async () => {
    const form = mountOverStore(PanelSourceSettingsForm)
    expect(await save(form)).toBe('The store refused these source settings, so nothing was saved. Check the fields. Error ID: e-race')
    expect(valueOf(form, 'source-cap')).toBe('150')
    expect(valueOf(form, 'source-reason')).toBe('why')
  })

  it('a panel settings move is not a source settings move: the fixed copy', async () => {
    fake.onWrite = () => (fake.panelVersion = 8)
    const form = mountOverStore(PanelSourceSettingsForm)
    expect(await save(form)).toBe('The store refused these source settings, so nothing was saved. Check the fields. Error ID: e-race')
  })

  it('the panel re-read fails: the fixed copy, nothing thrown, the form kept', async () => {
    fake.onWrite = () => fake.failing.add('panel')
    const form = mountOverStore(PanelSourceSettingsForm)
    expect(await save(form)).toBe('The store refused these source settings, so nothing was saved. Check the fields. Error ID: e-race')
    expect(valueOf(form, 'source-cap')).toBe('150')
    expect(usePanelsStore().writing).toBe(false)
  })
})
