// The Panels page's store (web/src/stores/panels.ts, tj-7zjov1.37): the selector, the selected panel's
// sections (each failing on its own), the 90-day history window, late answers for a panel no longer
// selected dropped, and the three writes, each followed by a REST re-read (there is no panel event type).
// The typed client is mocked at @/api; everything above it is the real store.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { create } from '@bufbuild/protobuf'
import { createPinia, setActivePinia } from 'pinia'

import { DataSource } from '@generated/trader_joe/proto/market/v1/enums_pb'
import {
  AssetListHistorySchema,
  PanelBoundariesSchema,
  PanelDetailSchema,
  PanelListItemSchema,
  PanelListSchema,
  PanelMemberPageSchema,
  PanelMemberSchema,
  PanelSettingsVersionPageSchema,
  PanelSettingsVersionSchema,
  PanelSourceSettingsVersionPageSchema,
  PanelSourceSettingsVersionSchema,
} from '@generated/trader_joe/proto/ui/v1/panels_pb'

import { ApiError } from '@/api'

import { HISTORY_DAYS, MEMBERS_PAGE_SIZE, historyWindow, usePanelsStore } from './panels'

const api = vi.hoisted(() => ({
  listPanels: vi.fn(),
  getPanel: vi.fn(),
  getPanelSettingsVersions: vi.fn(),
  getSourceSettingsVersions: vi.fn(),
  getAssetListHistory: vi.fn(),
  getPanelBoundaries: vi.fn(),
  getPanelMembers: vi.fn(),
  createPanel: vi.fn(),
  savePanelSettings: vi.fn(),
  saveSourceSettings: vi.fn(),
}))
vi.mock('@/api', async (importOriginal) => ({ ...(await importOriginal<typeof import('@/api')>()), ...api }))

const A = '0f8fad5b-d9cb-469f-a165-70867728950e'
const B = '7c9e6679-7425-40de-944b-e07fc1f90ae7'
const C = '16fd2706-8baf-433b-82eb-8c7fada847da'
const NOW = Date.UTC(2026, 10, 1, 12)
const DAY = 86_400_000

const item = (id: string, name: string, source = DataSource.ALPACA_API) => create(PanelListItemSchema, { id, name, source, enabled: true })
const detailOf = (id: string, source = DataSource.ALPACA_API) => create(PanelDetailSchema, { id, name: `panel ${id}`, source })
const member = (symbol: string) => create(PanelMemberSchema, { vendorAssetId: `v-${symbol}`, symbol })

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function answerEveryRead(): void {
  api.listPanels.mockResolvedValue(create(PanelListSchema, { items: [item(A, 'Alpha'), item(B, 'Beta', DataSource.IB_API)] }))
  api.getPanel.mockImplementation(async (id: string) => detailOf(id, id === B ? DataSource.IB_API : DataSource.ALPACA_API))
  api.getPanelSettingsVersions.mockImplementation(async (id: string) =>
    create(PanelSettingsVersionPageSchema, { items: [create(PanelSettingsVersionSchema, { versionNo: id === A ? 1 : 7 })] }),
  )
  api.getSourceSettingsVersions.mockImplementation(async (source: DataSource) =>
    create(PanelSourceSettingsVersionPageSchema, { items: [create(PanelSourceSettingsVersionSchema, { source, versionNo: source })] }),
  )
  api.getAssetListHistory.mockImplementation(async (id: string) => create(AssetListHistorySchema, { days: [{ day: id }] }))
  api.getPanelBoundaries.mockImplementation(async (id: string) => create(PanelBoundariesSchema, { markers: [{ label: id }] }))
  api.getPanelMembers.mockImplementation(async (id: string) =>
    create(PanelMemberPageSchema, { items: [member(`${id.slice(0, 4)}-1`)], nextCursor: id === A ? 'next-a' : '' }),
  )
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  for (const fn of Object.values(api)) fn.mockReset()
  answerEveryRead()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('historyWindow', () => {
  it('is [now - 90 days, now + 1 day): today block is in it and the read stays under 366 days', () => {
    expect(HISTORY_DAYS).toBe(90)
    const { start, end } = historyWindow(NOW)
    expect(start.getTime()).toBe(NOW - 90 * DAY)
    expect(end.getTime()).toBe(NOW + DAY)
    expect((end.getTime() - start.getTime()) / DAY).toBeLessThanOrEqual(366)
  })

  it('defaults to the clock', () => {
    expect(historyWindow().start.getTime()).toBe(NOW - 90 * DAY)
  })
})

describe('load: the selector, then the selected panel', () => {
  it('lists the panels, selects the first and reads its five sections and its source versions', async () => {
    const store = usePanelsStore()
    await store.load()
    expect(store.listStatus).toBe('ready')
    expect(store.panels.map((p) => p.id)).toEqual([A, B])
    expect(store.selectedId).toBe(A)
    expect(store.selected?.name).toBe('Alpha')
    expect(store.detailStatus).toBe('ready')
    expect(store.detail?.id).toBe(A)
    expect(store.panelVersions.map((v) => v.versionNo)).toEqual([1])
    expect(store.sourceVersions.map((v) => v.source)).toEqual([DataSource.ALPACA_API])
    expect(store.history?.days[0]?.day).toBe(A)
    expect(store.boundaries?.markers[0]?.label).toBe(A)
    expect(store.members.map((m) => m.symbol)).toEqual(['0f8f-1'])
    expect(store.hasMoreMembers).toBe(true)
    expect(store.errors).toEqual({})
  })

  it('asks each read with the ruled parameters: versions limit 500, members a page of 100, one window for both', async () => {
    await usePanelsStore().load()
    const { start, end } = historyWindow(NOW)
    expect(api.getPanel).toHaveBeenCalledWith(A, expect.any(AbortSignal))
    expect(api.getPanelSettingsVersions).toHaveBeenCalledWith(A, expect.objectContaining({ limit: 500 }))
    expect(api.getPanelMembers).toHaveBeenCalledWith(A, expect.objectContaining({ limit: MEMBERS_PAGE_SIZE }))
    expect(MEMBERS_PAGE_SIZE).toBe(100)
    for (const read of [api.getAssetListHistory, api.getPanelBoundaries]) {
      expect(read).toHaveBeenCalledTimes(1)
      expect(read.mock.calls[0]![0]).toBe(A)
      expect(read.mock.calls[0]![1].start).toEqual(start)
      expect(read.mock.calls[0]![1].end).toEqual(end)
    }
    // The source versions are the detail's source's, read after the detail names it.
    expect(api.getSourceSettingsVersions).toHaveBeenCalledWith(DataSource.ALPACA_API, expect.objectContaining({ limit: 500 }))
  })

  it('no panels: nothing selected, no panel read', async () => {
    api.listPanels.mockResolvedValue(create(PanelListSchema))
    const store = usePanelsStore()
    await store.load()
    expect(store.listStatus).toBe('ready')
    expect(store.selectedId).toBeNull()
    expect(store.detail).toBeNull()
    expect(store.detailStatus).toBe('idle')
    expect(api.getPanel).not.toHaveBeenCalled()
  })

  it('keeps the selection while the panel is still listed; takes the first once it is gone', async () => {
    const store = usePanelsStore()
    await store.load()
    await store.select(B)
    await store.load()
    expect(store.selectedId).toBe(B)
    api.listPanels.mockResolvedValue(create(PanelListSchema, { items: [item(C, 'Gamma'), item(A, 'Alpha')] }))
    await store.load()
    expect(store.selectedId).toBe(C)
    expect(store.detail?.id).toBe(C)
  })

  it('a failed list read is an error with its id; the panels held are kept', async () => {
    const store = usePanelsStore()
    await store.load()
    api.listPanels.mockRejectedValue(new ApiError({ status: 500, errorId: 'e-list' }))
    await store.loadList()
    expect(store.listStatus).toBe('error')
    expect(store.listError?.errorId).toBe('e-list')
    expect(store.panels).toHaveLength(2)
  })

  it('bumps loadedTick on every settled load', async () => {
    const store = usePanelsStore()
    await store.load()
    expect(store.loadedTick).toBe(2)
  })
})

describe('the sections fail on their own', () => {
  it.each([
    ['history', 'getAssetListHistory'],
    ['markers', 'getPanelBoundaries'],
    ['members', 'getPanelMembers'],
    ['versions', 'getPanelSettingsVersions'],
    ['versions', 'getSourceSettingsVersions'],
  ] as const)('a failed %s read (%s) leaves the rest loaded', async (section, read) => {
    api[read].mockRejectedValue(new ApiError({ status: 500, errorId: `e-${read}` }))
    const store = usePanelsStore()
    await store.load()
    expect(Object.keys(store.errors)).toEqual([section])
    expect(store.errors[section]?.errorId).toBe(`e-${read}`)
    expect(store.detailStatus).toBe('ready')
    expect(store.detail?.id).toBe(A)
  })

  it('a 404 detail is not-found; a 500 detail is error; the other sections still load', async () => {
    api.getPanel.mockRejectedValueOnce(new ApiError({ status: 404 }))
    const store = usePanelsStore()
    await store.load()
    expect(store.detailStatus).toBe('not-found')
    expect(store.errors.detail).toBeDefined()
    expect(store.history).not.toBeNull()
    expect(api.getSourceSettingsVersions).not.toHaveBeenCalled()

    api.getPanel.mockRejectedValueOnce(new ApiError({ status: 500 }))
    await store.select(B)
    expect(store.detailStatus).toBe('error')
  })

  it('a re-read of the same panel keeps what it shows and clears the old errors', async () => {
    const store = usePanelsStore()
    api.getAssetListHistory.mockRejectedValueOnce(new ApiError({ status: 500 }))
    await store.load()
    expect(store.errors.history).toBeDefined()
    const reading = store.loadSelected()
    expect(store.detail?.id).toBe(A)
    expect(store.detailStatus).toBe('ready')
    await reading
    expect(store.errors).toEqual({})
    expect(store.history?.days[0]?.day).toBe(A)
  })
})

describe('select and late answers', () => {
  it('selecting the panel already chosen reads nothing', async () => {
    const store = usePanelsStore()
    await store.load()
    api.getPanel.mockClear()
    await store.select(A)
    expect(api.getPanel).not.toHaveBeenCalled()
  })

  it('selecting another panel clears the shown one first and reads the new one', async () => {
    const store = usePanelsStore()
    await store.load()
    const reading = store.select(B)
    expect(store.detail).toBeNull()
    expect(store.members).toEqual([])
    expect(store.detailStatus).toBe('loading')
    await reading
    expect(store.detail?.id).toBe(B)
    expect(store.sourceVersions.map((v) => v.source)).toEqual([DataSource.IB_API])
  })

  it('a late answer for a panel no longer selected is dropped, the earlier read aborted', async () => {
    const store = usePanelsStore()
    await store.load()
    const late = deferred<ReturnType<typeof detailOf>>()
    api.getPanel.mockImplementationOnce(() => late.promise)
    const first = store.select(B)
    const signal = api.getPanel.mock.calls.at(-1)![1] as AbortSignal
    await store.select(C)
    expect(signal.aborted).toBe(true)
    expect(store.detail?.id).toBe(C)
    late.resolve(detailOf(B, DataSource.IB_API))
    await first
    expect(store.selectedId).toBe(C)
    expect(store.detail?.id).toBe(C)
    expect(store.history?.days[0]?.day).toBe(C)
  })

  it('a late failure for a panel no longer selected leaves no error behind', async () => {
    const store = usePanelsStore()
    await store.load()
    const late = deferred<never>()
    api.getAssetListHistory.mockImplementationOnce(() => late.promise)
    const first = store.select(B)
    await store.select(C)
    late.reject(new ApiError({ status: 500 }))
    await first
    expect(store.errors).toEqual({})
  })

  it('a late source-versions answer is dropped too', async () => {
    const store = usePanelsStore()
    await store.load()
    const late = deferred<unknown>()
    api.getSourceSettingsVersions.mockImplementationOnce(() => late.promise)
    const first = store.select(B)
    await vi.waitFor(() => expect(api.getSourceSettingsVersions).toHaveBeenCalledWith(DataSource.IB_API, expect.anything()))
    await store.select(C)
    late.resolve(create(PanelSourceSettingsVersionPageSchema, { items: [create(PanelSourceSettingsVersionSchema, { versionNo: 99 })] }))
    await first
    expect(store.sourceVersions.map((v) => v.versionNo)).not.toContain(99)
  })

  it('a late list answer is dropped when a newer list read started', async () => {
    const store = usePanelsStore()
    const late = deferred<unknown>()
    api.listPanels.mockImplementationOnce(() => late.promise)
    const first = store.loadList()
    await store.loadList()
    late.resolve(create(PanelListSchema, { items: [item(C, 'Gamma')] }))
    await first
    expect(store.panels.map((p) => p.id)).toEqual([A, B])
  })
})

describe('loadMoreMembers', () => {
  it('appends the next page by its cursor', async () => {
    const store = usePanelsStore()
    await store.load()
    api.getPanelMembers.mockResolvedValueOnce(create(PanelMemberPageSchema, { items: [member('ZZ')], nextCursor: '' }))
    await store.loadMoreMembers()
    expect(api.getPanelMembers).toHaveBeenLastCalledWith(A, { cursor: 'next-a', limit: 100, signal: expect.any(AbortSignal) })
    expect(store.members.map((m) => m.symbol)).toEqual(['0f8f-1', 'ZZ'])
    expect(store.hasMoreMembers).toBe(false)
  })

  it('does nothing without a cursor', async () => {
    const store = usePanelsStore()
    await store.load()
    await store.select(B)
    api.getPanelMembers.mockClear()
    await store.loadMoreMembers()
    expect(api.getPanelMembers).not.toHaveBeenCalled()
  })

  it('drops a page that arrives after the selection changed', async () => {
    const store = usePanelsStore()
    await store.load()
    const late = deferred<unknown>()
    api.getPanelMembers.mockImplementationOnce(() => late.promise)
    const more = store.loadMoreMembers()
    await store.select(B)
    late.resolve(create(PanelMemberPageSchema, { items: [member('LATE')] }))
    await more
    expect(store.members.map((m) => m.symbol)).not.toContain('LATE')
    expect(store.loadingMoreMembers).toBe(false)
  })

  // tj-7zjov1.248: the page's members are a cursor walk of ONE read. A page asked for under an earlier
  // selection or an earlier reload belongs to a different walk, even when the panel id matches again, so
  // it is dropped, page and error alike; the cursor is left as the newer read set it.
  const latePage = () => create(PanelMemberPageSchema, { items: [member('LATE')], nextCursor: 'late-cursor' })

  function holdNextMembersPage() {
    const late = deferred<unknown>()
    api.getPanelMembers.mockImplementationOnce(() => late.promise)
    return late
  }

  it('A -> B -> A with a page out: the page from the first A is dropped', async () => {
    const store = usePanelsStore()
    await store.load()
    const late = holdNextMembersPage()
    const more = store.loadMoreMembers()
    await store.select(B)
    await store.select(A)
    expect(store.selectedId).toBe(A)
    late.resolve(latePage())
    await more
    expect(store.members.map((m) => m.symbol)).toEqual(['0f8f-1'])
    expect(store.hasMoreMembers).toBe(true)
    await store.loadMoreMembers()
    expect(api.getPanelMembers).toHaveBeenLastCalledWith(A, { cursor: 'next-a', limit: 100, signal: expect.any(AbortSignal) })
  })

  it('A -> B -> A by list re-reads alone (no panel read between): the page is still dropped', async () => {
    const store = usePanelsStore()
    await store.load()
    const late = holdNextMembersPage()
    const more = store.loadMoreMembers()
    api.listPanels.mockResolvedValueOnce(create(PanelListSchema, { items: [item(B, 'Beta')] }))
    await store.loadList()
    expect(store.selectedId).toBe(B)
    api.listPanels.mockResolvedValueOnce(create(PanelListSchema, { items: [item(A, 'Alpha')] }))
    await store.loadList()
    expect(store.selectedId).toBe(A)
    late.resolve(latePage())
    await more
    expect(store.members).toEqual([])
    expect(store.hasMoreMembers).toBe(false)
  })

  it('a same-panel reload while Load More is out: the old page is not appended to the new first page', async () => {
    const store = usePanelsStore()
    await store.load()
    const late = holdNextMembersPage()
    const more = store.loadMoreMembers()
    await store.loadSelected()
    expect(store.members.map((m) => m.symbol)).toEqual(['0f8f-1'])
    late.resolve(latePage())
    await more
    expect(store.members.map((m) => m.symbol)).toEqual(['0f8f-1'])
    expect(store.hasMoreMembers).toBe(true)
    expect(store.loadingMoreMembers).toBe(false)
  })

  it('a same-panel reload while Load More is out: a stale ERROR is dropped too', async () => {
    const store = usePanelsStore()
    await store.load()
    const late = holdNextMembersPage()
    const more = store.loadMoreMembers()
    await store.loadSelected()
    late.reject(new ApiError({ status: 500, errorId: 'e-stale' }))
    await more
    expect(store.errors).toEqual({})
    expect(store.loadingMoreMembers).toBe(false)
  })

  it('A -> B -> A with a page out: a stale ERROR is dropped', async () => {
    const store = usePanelsStore()
    await store.load()
    const late = holdNextMembersPage()
    const more = store.loadMoreMembers()
    await store.select(B)
    await store.select(A)
    late.reject(new ApiError({ status: 500, errorId: 'e-stale' }))
    await more
    expect(store.errors).toEqual({})
  })

  it('nothing changed: Load More appends page after page, and a current failure is shown', async () => {
    const store = usePanelsStore()
    await store.load()
    await store.loadSelected()
    api.getPanelMembers.mockResolvedValueOnce(create(PanelMemberPageSchema, { items: [member('P2')], nextCursor: 'c3' }))
    await store.loadMoreMembers()
    api.getPanelMembers.mockResolvedValueOnce(create(PanelMemberPageSchema, { items: [member('P3')], nextCursor: 'c4' }))
    await store.loadMoreMembers()
    expect(api.getPanelMembers).toHaveBeenLastCalledWith(A, { cursor: 'c3', limit: 100, signal: expect.any(AbortSignal) })
    expect(store.members.map((m) => m.symbol)).toEqual(['0f8f-1', 'P2', 'P3'])
    expect(store.hasMoreMembers).toBe(true)
    api.getPanelMembers.mockRejectedValueOnce(new ApiError({ status: 500, errorId: 'e-now' }))
    await store.loadMoreMembers()
    expect(store.errors.members?.errorId).toBe('e-now')
    expect(store.members.map((m) => m.symbol)).toEqual(['0f8f-1', 'P2', 'P3'])
  })

  // tj-7zjov1.252: a Load More left out by a selection change or a reload is ABORTED and its flag freed, so a
  // Load More on the new walk starts at once. Only the current request's cleanup frees the flag: the stale one
  // settling later must not free a new request's flag, or a second Load More on one cursor appends a page twice.
  const membersSignal = (call = -1) => (api.getPanelMembers.mock.calls.at(call)![1] as { signal: AbortSignal }).signal

  /** Every panel's first page has a cursor, so B can walk too. */
  function everyPanelHasMore(): void {
    api.getPanelMembers.mockImplementation(async (id: string) =>
      create(PanelMemberPageSchema, { items: [member(`${id.slice(0, 4)}-1`)], nextCursor: `next-${id.slice(0, 4)}` }),
    )
  }

  /** The next members read settles only when aborted, rejecting the way fetch does. */
  function abortableNextMembersPage(): void {
    api.getPanelMembers.mockImplementationOnce(
      (_id: string, params: { signal: AbortSignal }) =>
        new Promise((_resolve, reject) => {
          params.signal.addEventListener('abort', () => reject(new DOMException('The operation was aborted.', 'AbortError')))
        }),
    )
  }

  it('A -> B with a page out: Load More on B sends at once, appends once; the stale page never lands', async () => {
    const store = usePanelsStore()
    await store.load()
    everyPanelHasMore()
    const late = holdNextMembersPage()
    const staleMore = store.loadMoreMembers()
    const staleSignal = membersSignal()
    await store.select(B)
    expect(staleSignal.aborted).toBe(true)
    expect(store.loadingMoreMembers).toBe(false)
    expect(store.members.map((m) => m.symbol)).toEqual(['7c9e-1'])

    const fresh = deferred<unknown>()
    api.getPanelMembers.mockImplementationOnce(() => fresh.promise)
    const calls = api.getPanelMembers.mock.calls.length
    const freshMore = store.loadMoreMembers()
    expect(api.getPanelMembers).toHaveBeenCalledTimes(calls + 1)
    expect(api.getPanelMembers).toHaveBeenLastCalledWith(B, { cursor: 'next-7c9e', limit: 100, signal: expect.any(AbortSignal) })
    expect(membersSignal().aborted).toBe(false)
    expect(store.loadingMoreMembers).toBe(true)

    // The stale request settles while B's is out: its cleanup must leave B's flag and data alone.
    late.resolve(latePage())
    await staleMore
    expect(store.loadingMoreMembers).toBe(true)
    expect(store.members.map((m) => m.symbol)).toEqual(['7c9e-1'])
    expect(store.hasMoreMembers).toBe(true)

    // So a second Load More on the same cursor is still refused: exactly one page is fetched and appended.
    const second = store.loadMoreMembers()
    expect(api.getPanelMembers).toHaveBeenCalledTimes(calls + 1)
    fresh.resolve(create(PanelMemberPageSchema, { items: [member('B2')], nextCursor: '' }))
    await Promise.all([freshMore, second])
    expect(api.getPanelMembers).toHaveBeenCalledTimes(calls + 1)
    expect(store.members.map((m) => m.symbol)).toEqual(['7c9e-1', 'B2'])
    expect(store.members.map((m) => m.symbol)).not.toContain('LATE')
    expect(store.loadingMoreMembers).toBe(false)
    expect(store.errors).toEqual({})
  })

  it('A -> B: the aborted request rejects, and its abort error is never shown as a failure', async () => {
    const store = usePanelsStore()
    await store.load()
    abortableNextMembersPage()
    const staleMore = store.loadMoreMembers()
    const staleSignal = membersSignal()
    await store.select(B)
    expect(staleSignal.aborted).toBe(true)
    await staleMore
    expect(store.errors).toEqual({})
    expect(store.errors.members).toBeUndefined()
    expect(store.loadingMoreMembers).toBe(false)
  })

  it('the selection cleared (the list came back empty) with a page out: the request is aborted, no error shown', async () => {
    const store = usePanelsStore()
    await store.load()
    abortableNextMembersPage()
    const staleMore = store.loadMoreMembers()
    const staleSignal = membersSignal()
    expect(store.loadingMoreMembers).toBe(true)
    api.listPanels.mockResolvedValueOnce(create(PanelListSchema, { items: [] }))
    await store.loadList()
    expect(store.selectedId).toBeNull()
    expect(staleSignal.aborted).toBe(true)
    expect(store.loadingMoreMembers).toBe(false)
    await staleMore
    expect(store.errors).toEqual({})
    expect(store.members).toEqual([])
    expect(store.loadingMoreMembers).toBe(false)
  })

  it('a same-panel reload with a page out aborts it, and the next Load More sends at once on the new walk', async () => {
    const store = usePanelsStore()
    await store.load()
    abortableNextMembersPage()
    const staleMore = store.loadMoreMembers()
    const staleSignal = membersSignal()
    await store.loadSelected()
    expect(staleSignal.aborted).toBe(true)
    expect(store.loadingMoreMembers).toBe(false)
    await staleMore
    expect(store.errors).toEqual({})

    api.getPanelMembers.mockResolvedValueOnce(create(PanelMemberPageSchema, { items: [member('P2')], nextCursor: '' }))
    const calls = api.getPanelMembers.mock.calls.length
    await store.loadMoreMembers()
    expect(api.getPanelMembers).toHaveBeenCalledTimes(calls + 1)
    expect(api.getPanelMembers).toHaveBeenLastCalledWith(A, { cursor: 'next-a', limit: 100, signal: expect.any(AbortSignal) })
    expect(store.members.map((m) => m.symbol)).toEqual(['0f8f-1', 'P2'])
    expect(store.loadingMoreMembers).toBe(false)
  })

  it('nothing changed: a double Load More sends one request and appends its page once', async () => {
    const store = usePanelsStore()
    await store.load()
    const page = holdNextMembersPage()
    const calls = api.getPanelMembers.mock.calls.length
    const first = store.loadMoreMembers()
    const second = store.loadMoreMembers()
    expect(api.getPanelMembers).toHaveBeenCalledTimes(calls + 1)
    page.resolve(create(PanelMemberPageSchema, { items: [member('P2')], nextCursor: '' }))
    await Promise.all([first, second])
    expect(api.getPanelMembers).toHaveBeenCalledTimes(calls + 1)
    expect(membersSignal().aborted).toBe(false)
    expect(store.members.map((m) => m.symbol)).toEqual(['0f8f-1', 'P2'])
    expect(store.loadingMoreMembers).toBe(false)
  })
})

describe('the writes, each followed by a re-read', () => {
  it('create: posts the params, re-lists, selects the new panel and re-reads it', async () => {
    const store = usePanelsStore()
    await store.load()
    const created = detailOf(C)
    api.createPanel.mockResolvedValue(created)
    api.listPanels.mockResolvedValue(create(PanelListSchema, { items: [item(A, 'Alpha'), item(C, 'Gamma')] }))
    api.getPanel.mockClear()
    const params = { name: 'Gamma', source: DataSource.ALPACA_API, reason: 'new' }
    const outcome = await store.create(params)
    expect(api.createPanel).toHaveBeenCalledWith(params)
    expect(outcome).toEqual({ kind: 'created', detail: created })
    expect(store.selectedId).toBe(C)
    expect(api.getPanel).toHaveBeenCalledWith(C, expect.any(AbortSignal))
    expect(store.panels.map((p) => p.id)).toEqual([A, C])
    expect(store.writing).toBe(false)
  })

  it('saveSettings: posts to the selected panel, then re-lists and re-reads it', async () => {
    const store = usePanelsStore()
    await store.load()
    api.savePanelSettings.mockResolvedValue(create(PanelSettingsVersionSchema, { versionNo: 2 }))
    api.listPanels.mockClear()
    api.getPanel.mockClear()
    const params = { enabled: false, size: 3, reason: 'pause' }
    expect(await store.saveSettings(params)).toEqual({ kind: 'ok' })
    expect(api.savePanelSettings).toHaveBeenCalledWith(A, params)
    expect(api.listPanels).toHaveBeenCalledTimes(1)
    expect(api.getPanel).toHaveBeenCalledWith(A, expect.any(AbortSignal))
  })

  it('saveSettings with nothing selected is stale and sends nothing', async () => {
    const store = usePanelsStore()
    expect(await store.saveSettings({ reason: 'r' })).toEqual({ kind: 'stale' })
    expect(api.savePanelSettings).not.toHaveBeenCalled()
  })

  it('saveSettings whose answer arrives after the selection changed is stale, and re-reads nothing', async () => {
    const store = usePanelsStore()
    await store.load()
    const answer = deferred<unknown>()
    api.savePanelSettings.mockImplementationOnce(() => answer.promise)
    const saving = store.saveSettings({ reason: 'r' })
    await store.select(B)
    api.listPanels.mockClear()
    api.getPanel.mockClear()
    answer.resolve(create(PanelSettingsVersionSchema))
    expect(await saving).toEqual({ kind: 'stale' })
    expect(api.listPanels).not.toHaveBeenCalled()
    expect(api.getPanel).not.toHaveBeenCalled()
  })

  it('saveSource: posts to the source, then re-reads the selected panel', async () => {
    const store = usePanelsStore()
    await store.load()
    api.saveSourceSettings.mockResolvedValue(create(PanelSourceSettingsVersionSchema))
    api.getPanel.mockClear()
    const params = { rampDatasetsPerHour: 1, requestCapPerHour: 2, corporateActionWindowPreDays: 0, corporateActionWindowPostDays: 3, reason: 'r' }
    expect(await store.saveSource(DataSource.ALPACA_API, params)).toEqual({ kind: 'ok' })
    expect(api.saveSourceSettings).toHaveBeenCalledWith(DataSource.ALPACA_API, params)
    expect(api.getPanel).toHaveBeenCalledWith(A, expect.any(AbortSignal))
  })

  // A 422 is re-read (tj-7zjov1.246, below); every other refusal is not.
  it.each([
    [new ApiError({ status: 404 }), 'not-found'],
    [new ApiError({ status: 503, reason: 'DATABASE_UNAVAILABLE' }), 'unavailable'],
    [new ApiError({ status: 409, reason: 'OVERRIDE_REQUIRED' }), 'override-required'],
    [new ApiError({ status: 500 }), 'error'],
    [new Error('network'), 'error'],
  ])('a refused write (%o) is the outcome %s, with no re-read', async (error, kind) => {
    const store = usePanelsStore()
    await store.load()
    api.savePanelSettings.mockRejectedValue(error)
    api.createPanel.mockRejectedValue(error)
    api.saveSourceSettings.mockRejectedValue(error)
    api.getPanel.mockClear()
    expect((await store.saveSettings({ reason: 'r' })).kind).toBe(kind)
    expect((await store.create({ name: 'n', source: DataSource.ALPACA_API, reason: 'r' })).kind).toBe(kind)
    expect((await store.saveSource(DataSource.ALPACA_API, { reason: 'r' })).kind).toBe(kind)
    expect(api.getPanel).not.toHaveBeenCalled()
    expect(api.listPanels).toHaveBeenCalledTimes(1)
    expect(api.getSourceSettingsVersions).toHaveBeenCalledTimes(1)
    expect(store.writing).toBe(false)
  })

  it('one write at a time: a second while the first is out is stale and sends nothing', async () => {
    const store = usePanelsStore()
    await store.load()
    const answer = deferred<unknown>()
    api.savePanelSettings.mockImplementationOnce(() => answer.promise)
    const first = store.saveSettings({ reason: 'r' })
    expect(store.writing).toBe(true)
    expect(await store.saveSource(DataSource.ALPACA_API, { reason: 'r' })).toEqual({ kind: 'stale' })
    expect(await store.create({ name: 'n', source: DataSource.ALPACA_API, reason: 'r' })).toEqual({ kind: 'stale' })
    expect(api.saveSourceSettings).not.toHaveBeenCalled()
    expect(api.createPanel).not.toHaveBeenCalled()
    answer.resolve(create(PanelSettingsVersionSchema))
    expect(await first).toEqual({ kind: 'ok' })
    expect(store.writing).toBe(false)
  })
})

// tj-7zjov1.246: a 422 is answered from a re-read, never from the body. Create re-reads the list (R1) and,
// when it carried source settings, the source's first version (R4); the two saves re-read the panel and
// compare the saved version number with the one the form was built on. Nothing moved, or a re-read that
// itself fails, leaves the plain invalid outcome with its error id.
describe('a 422 is re-read to say what changed', () => {
  const refused = () => new ApiError({ status: 422, reason: 'INVALID_REQUEST', errorId: 'e-422' })
  const invalid = { kind: 'invalid', error: { message: 'Request failed (HTTP 422)', errorId: 'e-422' } }
  let panelVersion = 1
  let sourceVersion = 1

  beforeEach(() => {
    panelVersion = 1
    sourceVersion = 1
    api.getPanel.mockImplementation(async (id: string) =>
      create(PanelDetailSchema, {
        id,
        name: `panel ${id}`,
        source: DataSource.ALPACA_API,
        currentVersion: create(PanelSettingsVersionSchema, { versionNo: panelVersion }),
        currentSourceVersion: create(PanelSourceSettingsVersionSchema, { source: DataSource.ALPACA_API, versionNo: sourceVersion }),
      }),
    )
    api.createPanel.mockRejectedValue(refused())
    api.savePanelSettings.mockRejectedValue(refused())
    api.saveSourceSettings.mockRejectedValue(refused())
  })

  const listing = (...names: string[]) =>
    create(PanelListSchema, { items: [item(A, 'Alpha'), ...names.map((name, i) => item(`${C.slice(0, -1)}${i}`, name))] })
  const withSourceSettings = { rampDatasetsPerHour: 1, requestCapPerHour: 10 }
  const sourceReads = () => api.getSourceSettingsVersions.mock.calls.filter((args) => args[1]?.limit === 1)

  describe('create', () => {
    it('the name was taken in the meantime: the list is re-read and the outcome names it', async () => {
      const store = usePanelsStore()
      await store.load()
      api.listPanels.mockResolvedValue(listing('Gamma'))
      api.listPanels.mockClear()
      expect(await store.create({ name: 'Gamma', source: DataSource.ALPACA_API, reason: 'r' })).toEqual({
        kind: 'moved-on',
        what: 'name-taken',
        name: 'Gamma',
        error: invalid.error,
      })
      expect(api.listPanels).toHaveBeenCalledTimes(1)
      expect(store.panels.map((p) => p.name)).toContain('Gamma')
      expect(store.writing).toBe(false)
    })

    it('a listed name that differs only in case is no clash: the store compares exact case', async () => {
      const store = usePanelsStore()
      await store.load()
      api.listPanels.mockResolvedValue(listing('gamma'))
      expect(await store.create({ name: 'Gamma', source: DataSource.ALPACA_API, reason: 'r' })).toEqual(invalid)
    })

    it('nothing moved and no source settings sent: invalid with its error id, the source not read', async () => {
      const store = usePanelsStore()
      await store.load()
      api.listPanels.mockClear()
      expect(await store.create({ name: 'Gamma', source: DataSource.ALPACA_API, reason: 'r' })).toEqual(invalid)
      expect(api.listPanels).toHaveBeenCalledTimes(1)
      expect(sourceReads()).toEqual([])
    })

    it('source settings sent (ramp and cap) and the source has a version now: the outcome says so', async () => {
      const store = usePanelsStore()
      await store.load()
      const outcome = await store.create({ name: 'Gamma', source: DataSource.IB_API, reason: 'r', ...withSourceSettings })
      expect(outcome).toEqual({ kind: 'moved-on', what: 'source-has-settings', error: invalid.error })
      expect(sourceReads()).toEqual([[DataSource.IB_API, { limit: 1 }]])
    })

    // ADR tj-2ayia2 D4: any of the four source fields present is source settings sent (a window-only or
    // cap-only create is valid now), 0 included for a window day; all four absent, or present as undefined as
    // the create form passes an empty field, is none sent.
    it.each([
      ['only the ramp', { rampDatasetsPerHour: 1 }],
      ['only the cap', { requestCapPerHour: 2 }],
      ['only the pre days', { corporateActionWindowPreDays: 30 }],
      ['only the pre days, 0', { corporateActionWindowPreDays: 0 }],
      ['only the post days', { corporateActionWindowPostDays: 30 }],
      ['the window days, the rates undefined', { rampDatasetsPerHour: undefined, requestCapPerHour: undefined, corporateActionWindowPreDays: 5, corporateActionWindowPostDays: 5 }],
    ])('%s sent and the source has a version now: the source is re-read and the outcome says so', async (_label, settings) => {
      const store = usePanelsStore()
      await store.load()
      const outcome = await store.create({ name: 'Gamma', source: DataSource.IB_API, reason: 'r', ...settings })
      expect(outcome).toEqual({ kind: 'moved-on', what: 'source-has-settings', error: invalid.error })
      expect(sourceReads()).toEqual([[DataSource.IB_API, { limit: 1 }]])
    })

    it('all four present as undefined (every field empty on the form): none sent, the source not read', async () => {
      const store = usePanelsStore()
      await store.load()
      const empty = {
        rampDatasetsPerHour: undefined,
        requestCapPerHour: undefined,
        corporateActionWindowPreDays: undefined,
        corporateActionWindowPostDays: undefined,
      }
      expect(await store.create({ name: 'Gamma', source: DataSource.IB_API, reason: 'r', ...empty })).toEqual(invalid)
      expect(sourceReads()).toEqual([])
    })

    // tj-7zjov1.254 F2: with no name in the request there is no name to have been taken, so the name check
    // is skipped, even against a listed panel stored with the empty name: never 'A panel named ""'.
    it('no name in the request: the name check is skipped, even against a listed empty name', async () => {
      const store = usePanelsStore()
      await store.load()
      api.listPanels.mockResolvedValue(listing(''))
      expect(await store.create({ source: DataSource.ALPACA_API, reason: 'r' })).toEqual(invalid)
      const outcome = await store.create({ source: DataSource.IB_API, reason: 'r', ...withSourceSettings })
      expect(outcome).toEqual({ kind: 'moved-on', what: 'source-has-settings', error: invalid.error })
    })

    it('the outcome carries the error of the 422 it answers', async () => {
      const store = usePanelsStore()
      await store.load()
      api.listPanels.mockResolvedValue(listing('Gamma'))
      api.createPanel.mockRejectedValue(new ApiError({ status: 422, reason: 'INVALID_REQUEST', errorId: 'e-other' }))
      const outcome = await store.create({ name: 'Gamma', source: DataSource.ALPACA_API, reason: 'r' })
      expect(outcome).toMatchObject({ kind: 'moved-on', what: 'name-taken', name: 'Gamma', error: { errorId: 'e-other' } })
    })

    it('source settings sent and the source still has none: invalid with its error id', async () => {
      const store = usePanelsStore()
      await store.load()
      api.getSourceSettingsVersions.mockResolvedValue(create(PanelSourceSettingsVersionPageSchema))
      expect(await store.create({ name: 'Gamma', source: DataSource.IB_API, reason: 'r', ...withSourceSettings })).toEqual(invalid)
      expect(sourceReads()).toHaveLength(1)
    })

    it('the name check comes first: a taken name with source settings sent is name-taken', async () => {
      const store = usePanelsStore()
      await store.load()
      api.listPanels.mockResolvedValue(listing('Gamma'))
      const outcome = await store.create({ name: 'Gamma', source: DataSource.IB_API, reason: 'r', ...withSourceSettings })
      expect(outcome).toEqual({ kind: 'moved-on', what: 'name-taken', name: 'Gamma', error: invalid.error })
    })

    it('a failed list re-read falls back to invalid without throwing', async () => {
      const store = usePanelsStore()
      await store.load()
      api.listPanels.mockRejectedValue(new ApiError({ status: 500, errorId: 'e-list' }))
      expect(await store.create({ name: 'Gamma', source: DataSource.ALPACA_API, reason: 'r' })).toEqual(invalid)
      expect(store.writing).toBe(false)
    })

    it('a failed source re-read falls back to invalid without throwing', async () => {
      const store = usePanelsStore()
      await store.load()
      api.getSourceSettingsVersions.mockRejectedValue(new ApiError({ status: 500, errorId: 'e-src' }))
      expect(await store.create({ name: 'Gamma', source: DataSource.IB_API, reason: 'r', ...withSourceSettings })).toEqual(invalid)
      expect(store.writing).toBe(false)
    })
  })

  describe('saveSettings', () => {
    it('the saved version moved in the meantime: the panel is re-read and the outcome says so', async () => {
      const store = usePanelsStore()
      await store.load()
      panelVersion = 2
      api.listPanels.mockClear()
      api.getPanel.mockClear()
      expect(await store.saveSettings({ reason: 'r' })).toEqual({ kind: 'moved-on', what: 'settings', error: invalid.error })
      expect(api.listPanels).toHaveBeenCalledTimes(1)
      expect(api.getPanel).toHaveBeenCalledWith(A, expect.any(AbortSignal))
      expect(store.detail?.currentVersion?.versionNo).toBe(2)
    })

    it('the version is the one the form saw: invalid with its error id, after the re-read', async () => {
      const store = usePanelsStore()
      await store.load()
      api.getPanel.mockClear()
      expect(await store.saveSettings({ reason: 'r' })).toEqual(invalid)
      expect(api.getPanel).toHaveBeenCalledTimes(1)
    })

    it('a source version that moved is not a panel settings move', async () => {
      const store = usePanelsStore()
      await store.load()
      sourceVersion = 5
      expect(await store.saveSettings({ reason: 'r' })).toEqual(invalid)
    })

    // tj-7zjov1.254 F5: a selection that moves off the panel during the re-read answers stale, as the
    // success path does, never a 'changed in the meantime' for a panel the form was not built on.
    it('the selection changes during the re-read: stale, though the newly selected panel reads differently', async () => {
      const store = usePanelsStore()
      await store.load()
      api.listPanels.mockImplementationOnce(async () => {
        void store.select(B)
        return create(PanelListSchema, { items: [item(A, 'Alpha'), item(B, 'Beta')] })
      })
      expect(await store.saveSettings({ reason: 'r' })).toEqual({ kind: 'stale' })
      expect(store.selectedId).toBe(B)
      expect(store.detail?.id).toBe(B)
      expect(store.writing).toBe(false)
    })

    it('the list re-read drops the panel: stale, never moved-on', async () => {
      const store = usePanelsStore()
      await store.load()
      api.listPanels.mockResolvedValue(create(PanelListSchema, { items: [item(B, 'Beta')] }))
      expect(await store.saveSettings({ reason: 'r' })).toEqual({ kind: 'stale' })
      expect(store.selectedId).toBe(B)
    })

    it('the loaded detail no longer names the panel (away and back, its read failing): stale', async () => {
      const store = usePanelsStore()
      await store.load()
      api.getPanel.mockRejectedValue(new ApiError({ status: 500, errorId: 'e-panel' }))
      api.listPanels.mockImplementationOnce(async () => {
        void store.select(B)
        void store.select(A)
        return create(PanelListSchema, { items: [item(A, 'Alpha'), item(B, 'Beta')] })
      })
      expect(await store.saveSettings({ reason: 'r' })).toEqual({ kind: 'stale' })
      expect(store.selectedId).toBe(A)
      expect(store.detail).toBeNull()
    })

    it('a failed panel re-read falls back to invalid without throwing, the shown panel kept', async () => {
      const store = usePanelsStore()
      await store.load()
      api.getPanel.mockRejectedValue(new ApiError({ status: 500, errorId: 'e-panel' }))
      expect(await store.saveSettings({ reason: 'r' })).toEqual(invalid)
      expect(store.detail?.currentVersion?.versionNo).toBe(1)
      expect(store.writing).toBe(false)
    })
  })

  describe('saveSource', () => {
    it('the source version moved in the meantime: the panel is re-read and the outcome says so', async () => {
      const store = usePanelsStore()
      await store.load()
      sourceVersion = 2
      api.getPanel.mockClear()
      expect(await store.saveSource(DataSource.ALPACA_API, { reason: 'r' })).toEqual({
        kind: 'moved-on',
        what: 'source-settings',
        error: invalid.error,
      })
      expect(api.getPanel).toHaveBeenCalledWith(A, expect.any(AbortSignal))
    })

    it('the version is the one the form saw: invalid with its error id', async () => {
      const store = usePanelsStore()
      await store.load()
      expect(await store.saveSource(DataSource.ALPACA_API, { reason: 'r' })).toEqual(invalid)
    })

    it('a panel settings version that moved is not a source settings move', async () => {
      const store = usePanelsStore()
      await store.load()
      panelVersion = 9
      expect(await store.saveSource(DataSource.ALPACA_API, { reason: 'r' })).toEqual(invalid)
    })

    // tj-7zjov1.254 F5: comparing the source versions of two different panels says nothing; a selection
    // that changed during the re-read answers stale.
    it('the selection changes during the re-read: stale, though the newly selected panel reads differently', async () => {
      const store = usePanelsStore()
      await store.load()
      api.getPanel.mockImplementation(async (id: string) =>
        create(PanelDetailSchema, {
          id,
          name: `panel ${id}`,
          source: DataSource.ALPACA_API,
          currentVersion: create(PanelSettingsVersionSchema, { versionNo: panelVersion }),
          currentSourceVersion: create(PanelSourceSettingsVersionSchema, {
            source: DataSource.ALPACA_API,
            versionNo: id === B ? 7 : sourceVersion,
          }),
        }),
      )
      api.listPanels.mockImplementationOnce(async () => {
        void store.select(B)
        return create(PanelListSchema, { items: [item(A, 'Alpha'), item(B, 'Beta')] })
      })
      expect(await store.saveSource(DataSource.ALPACA_API, { reason: 'r' })).toEqual({ kind: 'stale' })
      expect(store.selectedId).toBe(B)
      expect(store.detail?.currentSourceVersion?.versionNo).toBe(7)
      expect(store.writing).toBe(false)
    })

    it('the selection changes during the re-read and the versions agree: still stale, not invalid', async () => {
      const store = usePanelsStore()
      await store.load()
      api.listPanels.mockImplementationOnce(async () => {
        void store.select(B)
        return create(PanelListSchema, { items: [item(A, 'Alpha'), item(B, 'Beta')] })
      })
      expect(await store.saveSource(DataSource.ALPACA_API, { reason: 'r' })).toEqual({ kind: 'stale' })
    })

    it('a failed panel re-read falls back to invalid without throwing', async () => {
      const store = usePanelsStore()
      await store.load()
      api.getPanel.mockRejectedValue(new ApiError({ status: 500 }))
      api.listPanels.mockRejectedValue(new ApiError({ status: 500 }))
      expect(await store.saveSource(DataSource.ALPACA_API, { reason: 'r' })).toEqual(invalid)
      expect(store.writing).toBe(false)
    })
  })
})
