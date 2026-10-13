import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import { effectScope, nextTick } from 'vue'

import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'

import { fakeEvents } from '@/__tests__/fakeEvents'
import router from '@/router'
import { EVENTS_CONNECTION_BADGES } from '@/theme/semantics'
import { colors } from '@/theme/tokens'
import AppShell from './AppShell.vue'
import EventsIndicator from './EventsIndicator.vue'
import TopBar from './TopBar.vue'
import { reloadScreens, registerEventsReload, useEventsConnection, useEventsReload } from './useEventsReload'

// tj-grna9p.76: the shell's events indicator and reload on reconnect. The shared events client is the
// fixture in src/__tests__/fakeEvents.ts (its state ref and its reload signal); createEventsClient
// itself is covered over a mock socket in src/api/events.spec.ts. The API is mocked at the typed
// client (@/api) for the specs that mount the whole shell.
vi.mock('@/api/events', async (importOriginal) =>
  (await import('@/__tests__/fakeEvents')).fakeEventsModule(await importOriginal<object>()),
)
const mocks = vi.hoisted(() => ({
  getUiConfig: vi.fn(),
  getDatasetFacets: vi.fn(),
  listDatasets: vi.fn(),
}))
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...mocks,
}))

const mounted: VueWrapper[] = []
const scopes: ReturnType<typeof effectScope>[] = []

/** A component's setup stand-in: runs `setup` in a scope that `stop()` disposes, like an unmount. */
function scope(setup: () => void): ReturnType<typeof effectScope> {
  const s = effectScope()
  s.run(setup)
  scopes.push(s)
  return s
}

beforeEach(() => {
  fakeEvents.reset()
  setActivePinia(createPinia())
  mocks.getUiConfig.mockResolvedValue({ allowedGroups: [AccountGroup.SIMULATION], deploymentLabel: '', serverVersion: '' })
  mocks.getDatasetFacets.mockResolvedValue({ all: 0n, needsAttention: 0n, running: 0n, sources: [], updateTypes: [], statuses: [] })
  mocks.listDatasets.mockResolvedValue({ items: [], nextCursor: '' })
})

afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount())
  scopes.splice(0).forEach((s) => s.stop())
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('events indicator', () => {
  it.each([
    ['connecting', 'Connecting', colors.status.info],
    ['open', 'Connected', colors.status.ok],
    ['reconnecting', 'Reconnecting', colors.status.warn],
  ] as const)('state %s reads %s in words, with the colour only supporting it', async (state, word, colour) => {
    fakeEvents.state.value = state
    const indicator = mount(EventsIndicator)
    mounted.push(indicator)
    const root = indicator.find('[data-testid="events-indicator"]')
    expect(root.attributes('role')).toBe('status')
    expect(root.attributes('data-state')).toBe(state)
    expect(root.text()).toBe(word)
    expect(root.attributes('title')).not.toBe('')
    expect(EVENTS_CONNECTION_BADGES[state]).toEqual({ label: word, color: colour })
  })

  it('follows the client: a drop reads Reconnecting, a re-open Connected, without a remount', async () => {
    const indicator = mount(EventsIndicator)
    mounted.push(indicator)
    expect(indicator.text()).toBe('Connecting')
    fakeEvents.state.value = 'open'
    await nextTick()
    expect(indicator.text()).toBe('Connected')
    fakeEvents.state.value = 'reconnecting'
    await nextTick()
    expect(indicator.text()).toBe('Reconnecting')
    expect(indicator.attributes('title')).toContain('retrying')
    fakeEvents.reconnect()
    await nextTick()
    expect(indicator.text()).toBe('Connected')
  })

  it('never says Live: that word and its colour belong to the Live trading group', () => {
    for (const badge of Object.values(EVENTS_CONNECTION_BADGES)) {
      expect(badge.label).not.toMatch(/live/i)
      expect(badge.color).not.toBe(colors.group.live)
    }
  })

  it('sits in the top bar', async () => {
    await router.push('/data/datasets')
    const bar = mount(TopBar, { global: { plugins: [createPinia(), router, PrimeVue] } })
    mounted.push(bar)
    expect(bar.find('.top-bar__right [data-testid="events-indicator"]').exists()).toBe(true)
  })
})

describe('reload on reconnect', () => {
  it('the shell registers ONE reload handler with the client while mounted, and none after', async () => {
    await router.push('/data/datasets')
    const shell = mount(AppShell, { global: { plugins: [createPinia(), router, PrimeVue] } })
    await flushPromises()
    expect(fakeEvents.reloadHandlers()).toBe(1)
    expect(shell.find('[data-testid="events-indicator"]').text()).toBe('Connecting')
    shell.unmount()
    expect(fakeEvents.reloadHandlers()).toBe(0)
  })

  it('mounting the shell under the fake opens no real WebSocket', async () => {
    const constructed = vi.fn()
    vi.stubGlobal('WebSocket', constructed)
    await router.push('/data/datasets')
    const shell = mount(AppShell, { global: { plugins: [createPinia(), router, PrimeVue] } })
    mounted.push(shell)
    await flushPromises()
    fakeEvents.reconnect()
    await flushPromises()
    expect(constructed).not.toHaveBeenCalled()
  })

  it('a reconnect runs each mounted screen refetch exactly once', () => {
    scope(useEventsConnection)
    const datasets = vi.fn()
    const detail = vi.fn()
    scope(() => useEventsReload(datasets))
    scope(() => useEventsReload(detail))
    fakeEvents.reconnect()
    expect(datasets).toHaveBeenCalledTimes(1)
    expect(detail).toHaveBeenCalledTimes(1)
    fakeEvents.reconnect()
    expect(datasets).toHaveBeenCalledTimes(2)
    expect(detail).toHaveBeenCalledTimes(2)
  })

  it('only mounted screens refetch: a screen that left is dropped with its scope', () => {
    scope(useEventsConnection)
    const stays = vi.fn()
    const leaves = vi.fn()
    scope(() => useEventsReload(stays))
    const left = scope(() => useEventsReload(leaves))
    left.stop()
    fakeEvents.reconnect()
    expect(stays).toHaveBeenCalledTimes(1)
    expect(leaves).not.toHaveBeenCalled()
  })

  it('with the shell gone, a reconnect refetches nothing', () => {
    const shell = scope(useEventsConnection)
    const screen = vi.fn()
    scope(() => useEventsReload(screen))
    shell.stop()
    fakeEvents.reconnect()
    expect(screen).not.toHaveBeenCalled()
  })

  it('a rejected refetch is logged and does not starve another screen; a throwing one neither', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const unhandled = vi.fn()
    process.on('unhandledRejection', unhandled)
    try {
      scope(useEventsConnection)
      const healthy = vi.fn(async () => undefined)
      scope(() => useEventsReload(() => Promise.reject(new Error('503'))))
      scope(() =>
        useEventsReload(() => {
          throw new Error('sync')
        }),
      )
      scope(() => useEventsReload(healthy))
      expect(() => fakeEvents.reconnect()).not.toThrow()
      expect(healthy).toHaveBeenCalledTimes(1)
      await flushPromises()
      expect(consoleError).toHaveBeenCalledTimes(2)
      const logged = consoleError.mock.calls.map(([message]) => String(message))
      expect(logged.some((m) => m.includes('refetch failed'))).toBe(true)
      expect(logged.some((m) => m.includes('refetch threw'))).toBe(true)
      expect(unhandled).not.toHaveBeenCalled()
    } finally {
      process.off('unhandledRejection', unhandled)
    }
  })

  it('registerEventsReload returns the unregister; reloadScreens runs what is registered', () => {
    const handler = vi.fn()
    const off = registerEventsReload(handler)
    reloadScreens()
    off()
    reloadScreens()
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('a reconnect on a screen that registers nothing (Requests, not built) is harmless', async () => {
    await router.push('/data/requests')
    const shell = mount(AppShell, { global: { plugins: [createPinia(), router, PrimeVue] } })
    mounted.push(shell)
    await flushPromises()
    const facets = mocks.getDatasetFacets.mock.calls.length
    expect(() => fakeEvents.reconnect()).not.toThrow()
    await flushPromises()
    expect(mocks.getDatasetFacets.mock.calls.length).toBe(facets)
    expect(shell.find('main h1').text()).toBe('Requests')
  })
})
