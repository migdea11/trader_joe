import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { create } from '@bufbuild/protobuf'
import type { MessageInitShape } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import { DataSource, Granularity } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { ClientKind, DatasetSummarySchema } from '@generated/trader_joe/proto/ui/v1/data_pb'
import {
  LastReadBucketsSchema,
  ReadsPerDaySchema,
  StaleDatasetPageSchema,
  StaleDatasetRowSchema,
  StorageCurrentSchema,
  UsageSummarySchema,
} from '@generated/trader_joe/proto/ui/v1/stats_pb'
import type { StaleDatasetRow } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { ApiError } from '@/api'

import { STALE_PAGE_SIZE, useUsageStore } from './usage'

// tj-grna9p.62: the Usage tab's store. The API is mocked at the typed client (@/api); the store is the
// real one. Fixtures are create(Schema) from the generated messages.
const mocks = vi.hoisted(() => ({
  getDatasetFacets: vi.fn(),
  getUsageSummary: vi.fn(),
  getLastReadBuckets: vi.fn(),
  getReadsPerDay: vi.fn(),
  getStaleDatasets: vi.fn(),
  getStorageCurrent: vi.fn(),
  setDatasetExpiry: vi.fn(),
}))
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...mocks,
}))

const T0 = Date.UTC(2026, 9, 14, 12)

function staleRow(id: string, extra: MessageInitShape<typeof DatasetSummarySchema> = {}): StaleDatasetRow {
  return create(StaleDatasetRowSchema, {
    dataset: { id, assetSymbol: id.toUpperCase(), granularity: Granularity.ONE_DAY, owner: 'operator', ...extra },
    estBytes: 1000n,
  })
}
const page = (items: StaleDatasetRow[], nextCursor = '') => create(StaleDatasetPageSchema, { items, nextCursor, staleDays: 90 })
const ids = (rows: readonly StaleDatasetRow[]) => rows.map((row) => row.dataset?.id)
const OVERRIDE = { reason: 'cleanup', confirmed: true } as const

beforeEach(() => {
  setActivePinia(createPinia())
  // mockReset, not clear: a once-implementation a test left unconsumed must not leak into the next.
  for (const mock of Object.values(mocks)) mock.mockReset()
  mocks.getDatasetFacets.mockResolvedValue({
    clients: [{ clientId: 'bot-1', clientKind: ClientKind.SDK, datasets: 2n }],
    sources: [{ source: DataSource.ALPACA_API, count: 3n }],
    updateTypes: [],
    statuses: [],
  })
  mocks.getUsageSummary.mockResolvedValue(create(UsageSummarySchema, { stale: 2n, total: 9n, staleDays: 90 }))
  mocks.getLastReadBuckets.mockResolvedValue(create(LastReadBucketsSchema, { neverRead: 1n }))
  mocks.getReadsPerDay.mockResolvedValue(create(ReadsPerDaySchema, {}))
  mocks.getStaleDatasets.mockResolvedValue(page([staleRow('a'), staleRow('b')]))
  mocks.getStorageCurrent.mockResolvedValue(create(StorageCurrentSchema, { totalBytes: 5000n }))
  mocks.setDatasetExpiry.mockImplementation((id: string) => Promise.resolve(create(DatasetSummarySchema, { id, owner: 'operator', expiry: timestampFromMs(T0) })))
})

async function loaded() {
  const store = useUsageStore()
  await store.load()
  vi.clearAllMocks()
  return store
}

const lastArgs = (mock: ReturnType<typeof vi.fn>) => mock.mock.calls.at(-1)![0] as Record<string, unknown>

describe('load: exactly what each route is asked', () => {
  it('the default selection: SDK, stale_days 90, no source; reads over 30 days with no dataset_id', async () => {
    const store = useUsageStore()
    await store.load()
    const signal = expect.any(AbortSignal)
    expect(mocks.getDatasetFacets).toHaveBeenCalledWith({ signal })
    expect(mocks.getUsageSummary).toHaveBeenCalledWith({ clientKinds: [ClientKind.SDK], staleDays: 90, source: undefined, signal })
    expect(mocks.getLastReadBuckets).toHaveBeenCalledWith({ clientKinds: [ClientKind.SDK], source: undefined, signal })
    expect(mocks.getReadsPerDay).toHaveBeenCalledWith({ clientKinds: [ClientKind.SDK], days: 30, source: undefined, signal })
    expect(mocks.getStaleDatasets).toHaveBeenCalledWith({
      clientKinds: [ClientKind.SDK],
      staleDays: 90,
      source: undefined,
      limit: STALE_PAGE_SIZE,
      signal,
    })
    expect(STALE_PAGE_SIZE).toBe(100)
    // Storage takes no client parameter (the route answers 422).
    expect(mocks.getStorageCurrent).toHaveBeenCalledWith({ source: undefined, signal })
    for (const mock of [mocks.getUsageSummary, mocks.getLastReadBuckets, mocks.getReadsPerDay, mocks.getStaleDatasets]) {
      expect(lastArgs(mock)).not.toHaveProperty('datasetId')
      expect(lastArgs(mock)).not.toHaveProperty('expiresBefore')
    }
  })

  it('holds what came back: the figures, the rows and cursor, the picker clients and sources', async () => {
    mocks.getStaleDatasets.mockResolvedValue(page([staleRow('a')], 'k1'))
    const store = useUsageStore()
    await store.load()
    expect(store.summary?.stale).toBe(2n)
    expect(store.buckets?.neverRead).toBe(1n)
    expect(store.storage?.totalBytes).toBe(5000n)
    expect(ids(store.rows)).toEqual(['a'])
    expect(store.hasMore).toBe(true)
    expect(store.staleDays).toBe(90)
    expect(store.staleStatus).toBe('ready')
    expect(store.clients.map((c) => c.clientId)).toEqual(['bot-1'])
    expect(store.sources.map((s) => s.source)).toEqual([DataSource.ALPACA_API])
  })

  it('each section fails on its own, with its message and id; the others still show', async () => {
    mocks.getLastReadBuckets.mockRejectedValue(new ApiError({ status: 500, errorId: 'b-1' }))
    const store = useUsageStore()
    await store.load()
    expect(store.errors.buckets).toEqual({ message: 'Request failed (HTTP 500)', errorId: 'b-1' })
    expect(store.errors.summary).toBeUndefined()
    expect(store.summary).not.toBeNull()
    mocks.getLastReadBuckets.mockResolvedValue(create(LastReadBucketsSchema, {}))
    await store.load()
    expect(store.errors.buckets).toBeUndefined()
  })
})

describe('the selection', () => {
  it('Read By kinds: the usage figures and the stale page again under them; not storage, not the picker', async () => {
    const store = await loaded()
    await store.setClientKinds([ClientKind.SDK, ClientKind.UI])
    const both = [ClientKind.SDK, ClientKind.UI]
    expect(lastArgs(mocks.getUsageSummary).clientKinds).toEqual(both)
    expect(lastArgs(mocks.getLastReadBuckets).clientKinds).toEqual(both)
    expect(lastArgs(mocks.getReadsPerDay).clientKinds).toEqual(both)
    expect(lastArgs(mocks.getStaleDatasets).clientKinds).toEqual(both)
    expect(mocks.getStorageCurrent).not.toHaveBeenCalled()
    expect(mocks.getDatasetFacets).not.toHaveBeenCalled()
  })

  it('picked clients are sent as client ids and WIN: no kinds go with them', async () => {
    const store = await loaded()
    await store.setClientIds(['bot-1', 'bot-2'])
    for (const mock of [mocks.getUsageSummary, mocks.getLastReadBuckets, mocks.getReadsPerDay, mocks.getStaleDatasets]) {
      expect(lastArgs(mock).clientIds).toEqual(['bot-1', 'bot-2'])
      expect(lastArgs(mock).clientKinds).toBeUndefined()
    }
    expect(store.clientsPicked).toBe(true)
    await store.setClientIds([])
    expect(lastArgs(mocks.getUsageSummary).clientKinds).toEqual([ClientKind.SDK])
    expect(lastArgs(mocks.getUsageSummary).clientIds).toBeUndefined()
  })

  it('an empty kinds choice is read as the default SDK', async () => {
    const store = await loaded()
    await store.setClientKinds([])
    expect(lastArgs(mocks.getUsageSummary).clientKinds).toEqual([ClientKind.SDK])
  })

  it('a Source is a filter on every route, storage included; unpicking it sends none', async () => {
    const store = await loaded()
    await store.setSource(DataSource.ALPACA_API)
    for (const mock of [mocks.getUsageSummary, mocks.getLastReadBuckets, mocks.getReadsPerDay, mocks.getStaleDatasets, mocks.getStorageCurrent]) {
      expect(lastArgs(mock).source).toBe(DataSource.ALPACA_API)
    }
    await store.setSource(undefined)
    expect(lastArgs(mocks.getStorageCurrent).source).toBeUndefined()
  })

  it('Clear puts back SDK, no clients and no source, and asks again', async () => {
    const store = await loaded()
    await store.setClientIds(['bot-1'])
    await store.setSource(DataSource.ALPACA_API)
    vi.clearAllMocks()
    await store.clearFilters()
    expect(store.clientKinds).toEqual([ClientKind.SDK])
    expect(store.clientIds).toEqual([])
    expect(store.source).toBeUndefined()
    expect(lastArgs(mocks.getUsageSummary)).toEqual(expect.objectContaining({ clientKinds: [ClientKind.SDK], source: undefined }))
    expect(mocks.getStorageCurrent).toHaveBeenCalledTimes(1)
  })
})

describe('the Stale Datasets page', () => {
  it('Load More follows the cursor with 100 rows, appends without duplicates, and stops at an empty cursor', async () => {
    mocks.getStaleDatasets.mockResolvedValueOnce(page([staleRow('a'), staleRow('b')], 'k1'))
    const store = useUsageStore()
    await store.load()
    mocks.getStaleDatasets.mockResolvedValueOnce(page([staleRow('b'), staleRow('c')], ''))
    await store.loadMoreStale()
    expect(lastArgs(mocks.getStaleDatasets)).toEqual(expect.objectContaining({ cursor: 'k1', limit: 100, staleDays: 90 }))
    expect(ids(store.rows)).toEqual(['a', 'b', 'c'])
    expect(store.hasMore).toBe(false)
    const calls = mocks.getStaleDatasets.mock.calls.length
    await store.loadMoreStale()
    expect(mocks.getStaleDatasets.mock.calls.length).toBe(calls)
  })

  it('a failed page keeps the rows and the cursor', async () => {
    mocks.getStaleDatasets.mockResolvedValueOnce(page([staleRow('a')], 'k1'))
    const store = useUsageStore()
    await store.load()
    mocks.getStaleDatasets.mockRejectedValueOnce(new ApiError({ status: 500 }))
    await store.loadMoreStale()
    expect(store.moreError?.message).toBe('Request failed (HTTP 500)')
    expect(ids(store.rows)).toEqual(['a'])
    expect(store.cursor).toBe('k1')
  })

  it('a live reload keeps the pages already loaded (asks for as many rows as are held) and keeps them on failure', async () => {
    const many = Array.from({ length: 150 }, (_, i) => staleRow(`r${i}`))
    mocks.getStaleDatasets.mockResolvedValueOnce(page(many.slice(0, 100), 'k1'))
    const store = useUsageStore()
    await store.load()
    mocks.getStaleDatasets.mockResolvedValueOnce(page(many.slice(100), ''))
    await store.loadMoreStale()
    mocks.getStaleDatasets.mockResolvedValueOnce(page(many))
    await store.reloadLive()
    expect(lastArgs(mocks.getStaleDatasets).limit).toBe(150)
    expect(lastArgs(mocks.getStaleDatasets).cursor).toBeUndefined()
    mocks.getStaleDatasets.mockRejectedValueOnce(new ApiError({ status: 503 }))
    await store.reloadLive()
    expect(store.rows).toHaveLength(150)
    expect(store.staleStatus).toBe('ready')
    expect(store.errors.stale?.message).toBe('Request failed (HTTP 503)')
  })

  it('a first load that fails is the error state', async () => {
    mocks.getStaleDatasets.mockRejectedValue(new ApiError({ status: 500 }))
    const store = useUsageStore()
    await store.load()
    expect(store.staleStatus).toBe('error')
  })
})

describe('setExpiry', () => {
  it('the operator own dataset: NEVER an override, even when one is handed in', async () => {
    const store = await loaded()
    const at = new Date(T0)
    await store.setExpiry('a', at, OVERRIDE)
    expect(mocks.setDatasetExpiry).toHaveBeenCalledWith('a', { expiry: at, override: undefined })
  })

  it('another owner dataset: the override is sent', async () => {
    mocks.getStaleDatasets.mockResolvedValue(page([staleRow('a', { owner: 'bot-7' })]))
    const store = await loaded()
    await store.setExpiry('a', new Date(T0), OVERRIDE)
    expect(mocks.setDatasetExpiry.mock.calls[0]![1].override).toEqual(OVERRIDE)
  })

  it('200: the row takes the answer retention facts, keeps its usage, and the server decides membership by a refetch', async () => {
    const usage = { lastReadAt: timestampFromMs(T0 - 100 * 86_400_000), readByClients30d: ['held'] }
    mocks.getStaleDatasets.mockResolvedValue(page([staleRow('a', { usage }), staleRow('b')]))
    const store = await loaded()
    const heldUsage = store.rows[0]!.dataset!.usage
    mocks.setDatasetExpiry.mockResolvedValueOnce(
      create(DatasetSummarySchema, { id: 'a', owner: 'operator', expiry: timestampFromMs(T0), reaperPending: true, usage: { readByClients30d: ['sdk'] } }),
    )
    let answerPage: (value: unknown) => void = () => undefined
    mocks.getStaleDatasets.mockImplementationOnce(() => new Promise((resolve) => (answerPage = resolve)))
    expect(await store.setExpiry('a', new Date(T0))).toEqual({ kind: 'ok' })
    const row = store.rows[0]!.dataset!
    expect(row.expiry).toEqual(timestampFromMs(T0))
    expect(row.reaperPending).toBe(true)
    expect(row.usage).toEqual(heldUsage)
    expect(row.usage?.readByClients30d).toEqual(['held'])
    expect(mocks.getUsageSummary).toHaveBeenCalledTimes(1)
    expect(mocks.getStaleDatasets).toHaveBeenCalledTimes(1)
    // The refetch drops the row when the server no longer lists it.
    answerPage(page([staleRow('b')]))
    await vi.waitFor(() => expect(ids(store.rows)).toEqual(['b']))
  })

  it('404: the row is dropped with a notice naming the dataset; the outcome says not-found', async () => {
    const store = await loaded()
    mocks.setDatasetExpiry.mockRejectedValueOnce(new ApiError({ status: 404, reason: 'NOT_FOUND' }))
    expect((await store.setExpiry('a', new Date(T0))).kind).toBe('not-found')
    expect(ids(store.rows)).toEqual(['b'])
    expect(store.droppedNotice).toBe('A 1d no longer exists, so it was removed from the list.')
    store.dismissDroppedNotice()
    expect(store.droppedNotice).toBeNull()
  })

  it('409: the page is refetched (keeping loaded rows) before the outcome, so the row shows its new owner', async () => {
    const store = await loaded()
    mocks.setDatasetExpiry.mockRejectedValueOnce(new ApiError({ status: 409, reason: 'OVERRIDE_REQUIRED' }))
    mocks.getStaleDatasets.mockResolvedValueOnce(page([staleRow('a', { owner: 'bot-7' }), staleRow('b')]))
    expect((await store.setExpiry('a', new Date(T0))).kind).toBe('override-required')
    expect(mocks.getStaleDatasets).toHaveBeenCalledTimes(1)
    expect(lastArgs(mocks.getStaleDatasets)).toEqual(expect.objectContaining({ limit: 100 }))
    expect(lastArgs(mocks.getStaleDatasets).cursor).toBeUndefined()
    expect(store.rows[0]!.dataset!.owner).toBe('bot-7')
    expect(store.writingId).toBeNull()
  })

  it.each([
    [new ApiError({ status: 503, reason: 'DATABASE_UNAVAILABLE' }), 'unavailable'],
    [new ApiError({ status: 422, reason: 'INVALID_REQUEST' }), 'invalid'],
    [new ApiError({ status: 500 }), 'error'],
  ])('a refusal (%s) is its outcome and changes no row', async (error, kind) => {
    const store = await loaded()
    const before = store.rows
    mocks.setDatasetExpiry.mockRejectedValueOnce(error)
    expect((await store.setExpiry('a', new Date(T0))).kind).toBe(kind)
    expect(store.rows).toBe(before)
    expect(store.writingId).toBeNull()
  })

  it('one write at a time: writingId is the dataset in flight; a second write is refused unsent', async () => {
    const store = await loaded()
    let answer: (value: unknown) => void = () => undefined
    mocks.setDatasetExpiry.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)))
    const first = store.setExpiry('a', new Date(T0))
    expect(store.writingId).toBe('a')
    expect((await store.setExpiry('b', new Date(T0))).kind).toBe('error')
    expect(mocks.setDatasetExpiry).toHaveBeenCalledTimes(1)
    answer(create(DatasetSummarySchema, { id: 'a', owner: 'operator' }))
    expect((await first).kind).toBe('ok')
    expect(store.writingId).toBeNull()
  })

  it('a row the page does not hold is refused unsent', async () => {
    const store = await loaded()
    expect((await store.setExpiry('nope', new Date(T0))).kind).toBe('error')
    expect(mocks.setDatasetExpiry).not.toHaveBeenCalled()
  })

  describe('late answers are dropped (generation)', () => {
    async function inFlight() {
      const store = await loaded()
      const handle: { resolve: (value: unknown) => void; reject: (error: unknown) => void } = {
        resolve: () => undefined,
        reject: () => undefined,
      }
      mocks.setDatasetExpiry.mockImplementationOnce(
        () => new Promise((resolve, reject) => Object.assign(handle, { resolve, reject })),
      )
      const pending = store.setExpiry('a', new Date(T0))
      return { store, handle, pending }
    }

    it.each([
      ['a Read By change', (s: ReturnType<typeof useUsageStore>) => s.setClientKinds([ClientKind.UI])],
      ['a client pick', (s: ReturnType<typeof useUsageStore>) => s.setClientIds(['bot-1'])],
      ['a Source pick', (s: ReturnType<typeof useUsageStore>) => s.setSource(DataSource.ALPACA_API)],
      ['Clear', (s: ReturnType<typeof useUsageStore>) => s.clearFilters()],
    ])('after %s, the 200 is stale and the reloaded rows are left alone', async (_name, change) => {
      const { store, handle, pending } = await inFlight()
      mocks.getStaleDatasets.mockResolvedValue(page([staleRow('a'), staleRow('z')]))
      await change(store)
      const rows = store.rows
      handle.resolve(create(DatasetSummarySchema, { id: 'a', owner: 'operator', reaperPending: true }))
      expect(await pending).toEqual({ kind: 'stale' })
      expect(store.rows).toBe(rows)
      expect(store.rows[0]!.dataset!.reaperPending).toBe(false)
    })

    it('a late 404 after a selection change does not drop a row nor raise the notice', async () => {
      const { store, handle, pending } = await inFlight()
      await store.setClientKinds([ClientKind.UI])
      handle.reject(new ApiError({ status: 404, reason: 'NOT_FOUND' }))
      expect(await pending).toEqual({ kind: 'stale' })
      expect(ids(store.rows)).toEqual(['a', 'b'])
      expect(store.droppedNotice).toBeNull()
    })

    it('a late 409 after a selection change is stale: no refetch for it, no outcome to re-prompt', async () => {
      const { store, handle, pending } = await inFlight()
      await store.setClientKinds([ClientKind.UI])
      const calls = mocks.getStaleDatasets.mock.calls.length
      handle.reject(new ApiError({ status: 409, reason: 'OVERRIDE_REQUIRED' }))
      expect(await pending).toEqual({ kind: 'stale' })
      expect(mocks.getStaleDatasets.mock.calls.length).toBe(calls)
    })

    it('a 409 whose refetch is overtaken by a selection change is stale, and that refetch is not applied', async () => {
      const { store, handle, pending } = await inFlight()
      let answerRefetch: (value: unknown) => void = () => undefined
      mocks.getStaleDatasets.mockImplementationOnce(() => new Promise((resolve) => (answerRefetch = resolve)))
      handle.reject(new ApiError({ status: 409, reason: 'OVERRIDE_REQUIRED' }))
      await vi.waitFor(() => expect(mocks.getStaleDatasets).toHaveBeenCalledTimes(1))
      mocks.getStaleDatasets.mockResolvedValue(page([staleRow('ui-1')]))
      await store.setClientKinds([ClientKind.UI])
      answerRefetch(page([staleRow('a', { owner: 'bot-7' })]))
      expect(await pending).toEqual({ kind: 'stale' })
      expect(ids(store.rows)).toEqual(['ui-1'])
    })

    it('reset drops the answer too', async () => {
      const { store, handle, pending } = await inFlight()
      store.reset()
      handle.resolve(create(DatasetSummarySchema, { id: 'a' }))
      expect(await pending).toEqual({ kind: 'stale' })
      expect(store.rows).toEqual([])
    })
  })
})

describe('reset', () => {
  it('forgets the selection and every figure', async () => {
    const store = useUsageStore()
    await store.load()
    await store.setClientIds(['bot-1'])
    await store.setSource(DataSource.ALPACA_API)
    store.reset()
    expect([store.clientKinds, store.clientIds, store.source]).toEqual([[ClientKind.SDK], [], undefined])
    expect([store.summary, store.buckets, store.reads, store.storage]).toEqual([null, null, null, null])
    expect(store.rows).toEqual([])
    expect(store.staleStatus).toBe('idle')
  })
})
